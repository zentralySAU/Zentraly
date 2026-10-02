"""Binary sensor platform for Zentraly."""

from datetime import datetime
from typing import Any, assert_never, override

from zentraly import BinarySensorCapability, ZentralyBinarySensorApi

from homeassistant.components.binary_sensor import BinarySensorEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .const import SCAN_INTERVAL
from .models import ZentralyConfigEntry, ZentralyDevice

PARALLEL_UPDATES = 0

_OPENTHERM_CAPABILITIES = frozenset(
    {
        BinarySensorCapability.OT_HEATING_WATER_ACTIVE,
        BinarySensorCapability.OT_DHW_ENABLED,
        BinarySensorCapability.OT_WINTER_MODE,
    }
)

_DIAGNOSTIC_CAPABILITIES = frozenset(
    {
        BinarySensorCapability.OT_HEATING_WATER_ACTIVE,
        BinarySensorCapability.OT_DHW_ENABLED,
        BinarySensorCapability.OT_WINTER_MODE,
    }
)


def _create_binary_sensor_entities(
    device: ZentralyDevice,
) -> list[ZentralyBinarySensor]:
    """Create binary sensor entities supported by a Zentraly device."""

    binary_sensor_api = ZentralyBinarySensorApi(device)

    return [
        ZentralyBinarySensor(
            device=device,
            binary_sensor_api=binary_sensor_api,
            capability=capability,
        )
        for capability in BinarySensorCapability
        if binary_sensor_api.supports(capability)
    ]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZentralyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zentraly binary sensor entities."""

    parent_entities = _create_binary_sensor_entities(
        entry.runtime_data.device,
    )

    if parent_entities:
        async_add_entities(
            parent_entities,
        )

    for subentry_id, child in entry.runtime_data.children.items():
        child_entities = _create_binary_sensor_entities(
            child,
        )

        if not child_entities:
            continue

        async_add_entities(
            child_entities,
            config_subentry_id=subentry_id,
        )


class ZentralyBinarySensor(BinarySensorEntity):
    """Representation of a Zentraly binary sensor."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        device: ZentralyDevice,
        binary_sensor_api: ZentralyBinarySensorApi,
        capability: BinarySensorCapability,
    ) -> None:
        """Initialize the Zentraly binary sensor."""

        self._device = device
        self._binary_sensor_api = binary_sensor_api
        self._capability = capability
        self._state_version = 0

        self._attr_unique_id = f"{device.device_id}_{capability.value}"
        self._attr_translation_key = capability.value

        if capability in _DIAGNOSTIC_CAPABILITIES:
            self._attr_entity_category = EntityCategory.DIAGNOSTIC

    @override
    async def async_added_to_hass(self) -> None:
        """Register Zentraly listeners and periodic state refresh."""

        await super().async_added_to_hass()

        self.async_on_remove(self._device.add_state_listener(self._handle_device_state))

        self.async_on_remove(
            self._device.add_connection_state_listener(self._handle_connection_state)
        )

        self.async_on_remove(
            self._binary_sensor_api.add_state_listener(self._handle_state_update)
        )

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
        """Refresh binary sensor state periodically."""

        self.async_schedule_update_ha_state(force_refresh=True)

    @property
    @override
    def available(self) -> bool:
        """Return availability independently of a missing attribute value."""
        return self._device.available and self._device.connected

    def _handle_device_state(self) -> None:
        """Publish shared device state without starting another query."""
        if not self.available or (
            self._capability in _OPENTHERM_CAPABILITIES
            and not self._device.opentherm_connected
        ):
            self._state_version += 1
        if (
            self._capability in _OPENTHERM_CAPABILITIES
            and not self._device.opentherm_connected
        ):
            self._attr_is_on = None
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

        self.async_schedule_update_ha_state(force_refresh=True)

    def _handle_state_update(
        self,
        updates: dict[BinarySensorCapability, Any],
    ) -> None:
        """Handle binary sensor updates received from Zentraly reports."""

        if self._capability not in updates:
            return

        value = updates[self._capability]

        if value is None and self._capability in _OPENTHERM_CAPABILITIES:
            self._state_version += 1
            self._attr_is_on = None
            self.async_write_ha_state()
            return

        if not isinstance(value, bool):
            return

        self._state_version += 1
        self._attr_is_on = value
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Update binary sensor state from the Zentraly device."""

        self._attr_available = self._device.connected

        if not self._device.connected:
            return

        if (
            self._capability in _OPENTHERM_CAPABILITIES
            and not self._device.opentherm_connected
        ):
            self._attr_is_on = None
            return

        state_version = self._state_version
        value: bool | None

        if self._capability is BinarySensorCapability.BOILER_ON:
            value = await self._binary_sensor_api.async_get_boiler_on()

        elif self._capability is BinarySensorCapability.OT_HEATING_WATER_ACTIVE:
            value = await self._binary_sensor_api.async_get_ot_heating_water_active()

        elif self._capability is BinarySensorCapability.OT_DHW_ENABLED:
            value = await self._binary_sensor_api.async_get_ot_dhw_enabled()

        elif self._capability is BinarySensorCapability.OT_WINTER_MODE:
            value = await self._binary_sensor_api.async_get_ot_winter_mode()

        else:
            assert_never(self._capability)

        if state_version == self._state_version:
            self._attr_is_on = value

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return device information."""

        return self._device.device_info
