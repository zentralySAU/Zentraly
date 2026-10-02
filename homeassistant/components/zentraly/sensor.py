"""Sensor platform for Zentraly."""

from datetime import datetime
from typing import Any, override

from zentraly import SensorCapability, ZentralyOutputType, ZentralySensorApi

from homeassistant.components.sensor import SensorDeviceClass, SensorEntity
from homeassistant.const import (
    PERCENTAGE,
    SIGNAL_STRENGTH_DECIBELS_MILLIWATT,
    EntityCategory,
    UnitOfElectricCurrent,
    UnitOfElectricPotential,
    UnitOfEnergy,
    UnitOfPower,
    UnitOfPressure,
    UnitOfTemperature,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import DOMAIN, SCAN_INTERVAL
from .models import ZentralyConfigEntry, ZentralyDevice

PARALLEL_UPDATES = 0

_OPENTHERM_CAPABILITIES = frozenset(
    {
        SensorCapability.ERROR_ID,
        SensorCapability.CH_SETPOINT,
        SensorCapability.MODULATION_LEVEL,
        SensorCapability.CH_WATER_PRESSURE,
        SensorCapability.DHW_FLOW_RATE,
        SensorCapability.FEED_TEMPERATURE,
        SensorCapability.DHW_TEMPERATURE,
        SensorCapability.DHW_SETPOINT,
    }
)

_DIAGNOSTIC_CAPABILITIES = frozenset(
    {
        SensorCapability.ERROR_ID,
        SensorCapability.OUTPUT_TYPE,
        SensorCapability.RSSI,
        SensorCapability.WIFI_SIGNAL_POWER,
        SensorCapability.BATTERY_LEVEL,
        SensorCapability.CH_SETPOINT,
        SensorCapability.MODULATION_LEVEL,
        SensorCapability.CH_WATER_PRESSURE,
        SensorCapability.DHW_FLOW_RATE,
        SensorCapability.FEED_TEMPERATURE,
        SensorCapability.DHW_TEMPERATURE,
        SensorCapability.DHW_SETPOINT,
    }
)

_REPORT_ONLY_CAPABILITIES = frozenset(
    {
        SensorCapability.RSSI,
    }
)


def _create_sensor_entities(
    device: ZentralyDevice,
) -> list[ZentralySensor]:
    """Create sensor entities supported by a Zentraly device."""

    sensor_api = ZentralySensorApi(device)

    return [
        ZentralySensor(
            device=device,
            sensor_api=sensor_api,
            capability=capability,
        )
        for capability in SensorCapability
        if sensor_api.supports(capability)
        and not (
            device.via_device_id is not None
            and capability is SensorCapability.WIFI_SIGNAL_POWER
        )
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZentralyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zentraly sensor entities."""

    parent_entities = _create_sensor_entities(
        entry.runtime_data.device,
    )

    if parent_entities:
        async_add_entities(
            parent_entities,
        )

    entity_registry = er.async_get(hass)
    for subentry_id, child in entry.runtime_data.children.items():
        if entity_id := entity_registry.async_get_entity_id(
            "sensor", DOMAIN, f"{child.device_id}_wifi_signal_power"
        ):
            registry_entry = entity_registry.entities[entity_id]
            if (
                registry_entry.config_entry_id == entry.entry_id
                and registry_entry.config_subentry_id == subentry_id
            ):
                entity_registry.async_remove(entity_id)

        child_entities = _create_sensor_entities(
            child,
        )

        if not child_entities:
            continue

        async_add_entities(
            child_entities,
            config_subentry_id=subentry_id,
        )


class ZentralySensor(SensorEntity):
    """Representation of a Zentraly sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        device: ZentralyDevice,
        sensor_api: ZentralySensorApi,
        capability: SensorCapability,
    ) -> None:
        """Initialize the Zentraly sensor."""

        self._device = device
        self._sensor_api = sensor_api
        self._capability = capability
        self._state_version = 0

        self._attr_unique_id = f"{device.device_id}_{capability.value}"
        self._attr_translation_key = capability.value

        if capability in _DIAGNOSTIC_CAPABILITIES:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

        if capability in (
            SensorCapability.RSSI,
            SensorCapability.WIFI_SIGNAL_POWER,
        ):
            self._attr_device_class = SensorDeviceClass.SIGNAL_STRENGTH
            self._attr_native_unit_of_measurement = SIGNAL_STRENGTH_DECIBELS_MILLIWATT

        elif capability is SensorCapability.BATTERY_LEVEL:
            self._attr_device_class = SensorDeviceClass.BATTERY
            self._attr_native_unit_of_measurement = PERCENTAGE

        elif capability is SensorCapability.OUTPUT_TYPE:
            self._attr_device_class = SensorDeviceClass.ENUM
            self._attr_options = [
                output_type.value for output_type in ZentralyOutputType
            ]

        elif capability is SensorCapability.VOLTAGE:
            self._attr_device_class = SensorDeviceClass.VOLTAGE
            self._attr_native_unit_of_measurement = UnitOfElectricPotential.VOLT

        elif capability is SensorCapability.CURRENT:
            self._attr_device_class = SensorDeviceClass.CURRENT
            self._attr_native_unit_of_measurement = UnitOfElectricCurrent.AMPERE

        elif capability is SensorCapability.POWER:
            self._attr_device_class = SensorDeviceClass.POWER
            self._attr_native_unit_of_measurement = UnitOfPower.WATT

        elif capability is SensorCapability.DAILY_ENERGY:
            self._attr_device_class = SensorDeviceClass.ENERGY
            self._attr_native_unit_of_measurement = UnitOfEnergy.KILO_WATT_HOUR

        elif capability in (
            SensorCapability.CH_SETPOINT,
            SensorCapability.FEED_TEMPERATURE,
            SensorCapability.DHW_TEMPERATURE,
            SensorCapability.DHW_SETPOINT,
        ):
            self._attr_device_class = SensorDeviceClass.TEMPERATURE
            self._attr_native_unit_of_measurement = UnitOfTemperature.CELSIUS

        elif capability is SensorCapability.MODULATION_LEVEL:
            self._attr_native_unit_of_measurement = PERCENTAGE

        elif capability is SensorCapability.CH_WATER_PRESSURE:
            self._attr_device_class = SensorDeviceClass.PRESSURE
            self._attr_native_unit_of_measurement = UnitOfPressure.BAR

        elif capability is SensorCapability.DHW_FLOW_RATE:
            self._attr_native_unit_of_measurement = (
                UnitOfVolumeFlowRate.LITERS_PER_MINUTE
            )

    @override
    async def async_added_to_hass(self) -> None:
        """Register Zentraly listeners and periodic state refresh."""

        await super().async_added_to_hass()

        self.async_on_remove(self._device.add_state_listener(self._handle_device_state))

        self.async_on_remove(
            self._device.add_connection_state_listener(self._handle_connection_state)
        )

        self.async_on_remove(
            self._sensor_api.add_state_listener(self._handle_state_update)
        )

        if self._capability not in _REPORT_ONLY_CAPABILITIES:
            if self._device.supports_periodic_polling:
                self.async_on_remove(
                    async_track_time_interval(
                        self.hass,
                        self._async_periodic_refresh,
                        SCAN_INTERVAL,
                    )
                )

            self.async_schedule_update_ha_state(force_refresh=True)

    async def _async_periodic_refresh(
        self,
        now: datetime,
    ) -> None:
        """Refresh sensor state periodically as a synchronization fallback."""

        self.async_schedule_update_ha_state(force_refresh=True)

    @property
    @override
    def available(self) -> bool:
        """Return availability independently of a missing attribute value."""
        return self._device.available and self._device.connected

    def _handle_device_state(self) -> None:
        """Publish shared device state without starting another query."""
        if (
            not self.available
            or self._capability is SensorCapability.OUTPUT_TYPE
            or (
                self._capability in _OPENTHERM_CAPABILITIES
                and not self._device.opentherm_connected
            )
        ):
            self._state_version += 1
        if (
            self._capability in _OPENTHERM_CAPABILITIES
            and not self._device.opentherm_connected
        ):
            self._attr_native_value = None
        if self._capability is SensorCapability.OUTPUT_TYPE:
            value = self._device.output_type
            self._attr_native_value = value.value if value is not None else None
        self.async_write_ha_state()

    def _handle_connection_state(
        self,
        connected: bool,
    ) -> None:
        """Handle Zentraly connection-state changes."""

        self._attr_available = connected
        self._state_version += 1

        if not connected:
            self.async_write_ha_state()
            return

        if self._capability in _REPORT_ONLY_CAPABILITIES:
            self.async_write_ha_state()
            return

        self.async_schedule_update_ha_state(force_refresh=True)

    def _handle_state_update(
        self,
        updates: dict[SensorCapability, Any],
    ) -> None:
        """Handle sensor state updates received from Zentraly reports."""

        if self._capability not in updates:
            return

        value = updates[self._capability]

        if self._capability is SensorCapability.OUTPUT_TYPE:
            if not isinstance(value, ZentralyOutputType):
                return

            self._state_version += 1
            self._attr_native_value = value.value
            self.async_write_ha_state()
            return

        if self._capability in _OPENTHERM_CAPABILITIES:
            if not self._device.opentherm_connected:
                self._state_version += 1
                self._attr_native_value = None
                self.async_write_ha_state()
                return

        if not isinstance(value, int | float):
            return

        self._state_version += 1
        self._attr_native_value = value
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Update sensor state from the Zentraly device."""

        self._attr_available = self._device.connected

        if not self._device.connected:
            return

        if self._capability in _REPORT_ONLY_CAPABILITIES:
            return

        state_version = self._state_version
        if self._capability is SensorCapability.OUTPUT_TYPE:
            output_type = await self._sensor_api.async_get_output_type()

            if state_version != self._state_version:
                return

            if output_type is None:
                self._attr_native_value = None
                return

            self._attr_native_value = output_type.value
            return

        if (
            self._capability in _OPENTHERM_CAPABILITIES
            and not self._device.opentherm_connected
        ):
            self._attr_native_value = None
            return

        value: int | float | None

        if self._capability is SensorCapability.ERROR_ID:
            value = await self._sensor_api.async_get_error_id()

        elif self._capability is SensorCapability.WIFI_SIGNAL_POWER:
            value = await self._sensor_api.async_get_wifi_signal_power()

        elif self._capability is SensorCapability.VOLTAGE:
            value = await self._sensor_api.async_get_voltage()

        elif self._capability is SensorCapability.CURRENT:
            value = await self._sensor_api.async_get_current()

        elif self._capability is SensorCapability.POWER:
            value = await self._sensor_api.async_get_power()

        elif self._capability is SensorCapability.DAILY_ENERGY:
            value = await self._sensor_api.async_get_daily_energy()

        elif self._capability is SensorCapability.CH_SETPOINT:
            value = await self._sensor_api.async_get_ch_setpoint()

        elif self._capability is SensorCapability.MODULATION_LEVEL:
            value = await self._sensor_api.async_get_modulation_level()

        elif self._capability is SensorCapability.CH_WATER_PRESSURE:
            value = await self._sensor_api.async_get_ch_water_pressure()

        elif self._capability is SensorCapability.DHW_FLOW_RATE:
            value = await self._sensor_api.async_get_dhw_flow_rate()

        elif self._capability is SensorCapability.FEED_TEMPERATURE:
            value = await self._sensor_api.async_get_feed_temperature()

        elif self._capability is SensorCapability.DHW_TEMPERATURE:
            value = await self._sensor_api.async_get_dhw_temperature()

        elif self._capability is SensorCapability.DHW_SETPOINT:
            value = await self._sensor_api.async_get_dhw_setpoint()

        elif self._capability is SensorCapability.BATTERY_LEVEL:
            value = await self._sensor_api.async_get_battery_level()

        else:
            return

        if state_version == self._state_version:
            self._attr_native_value = value

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return device information."""

        return self._device.device_info
