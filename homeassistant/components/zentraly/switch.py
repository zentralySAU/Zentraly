"""Switch platform for Zentraly."""

from datetime import datetime
from typing import Any, assert_never, override

from zentraly import SwitchCapability, ZentralySwitchApi

from homeassistant.components.switch import SwitchEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback
from homeassistant.helpers.event import async_track_time_interval

from .actions import translate_action_errors
from .const import DOMAIN, SCAN_INTERVAL
from .models import ZentralyConfigEntry, ZentralyDevice

PARALLEL_UPDATES = 1

_OPENTHERM_CAPABILITIES = frozenset(
    {
        SwitchCapability.COMFORT_MODE,
    }
)

_CONFIG_CAPABILITIES = frozenset(
    {
        SwitchCapability.CHILD_LOCK,
        SwitchCapability.ALWAYS_ON_DISPLAY,
        SwitchCapability.ALWAYS_ON_LED,
        SwitchCapability.COMFORT_MODE,
        SwitchCapability.FORCED_MODE,
        SwitchCapability.RETURN_TO_CRONO,
        SwitchCapability.TIMER_OFF_ENABLE,
        SwitchCapability.HIGH_VOLTAGE_PROTECTION,
        SwitchCapability.LOW_VOLTAGE_PROTECTION,
        SwitchCapability.HIGH_POWER_PROTECTION,
    }
)


def _create_switch_entities(
    device: ZentralyDevice,
) -> list[ZentralySwitch]:
    """Create switch entities supported by a Zentraly device."""

    entities: list[ZentralySwitch] = []
    endpoints = device.channel_endpoints
    for endpoint in endpoints:
        switch_api = ZentralySwitchApi(device, endpoint=endpoint)
        entities.extend(
            ZentralySwitch(
                device=device,
                switch_api=switch_api,
                capability=capability,
                channel=endpoint if len(endpoints) > 1 else None,
            )
            for capability in SwitchCapability
            if switch_api.supports(capability)
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZentralyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zentraly switch entities."""

    parent_entities = _create_switch_entities(
        entry.runtime_data.device,
    )

    if parent_entities:
        async_add_entities(
            parent_entities,
        )

    for subentry_id, child in entry.runtime_data.children.items():
        child_entities = _create_switch_entities(
            child,
        )

        if not child_entities:
            continue

        async_add_entities(
            child_entities,
            config_subentry_id=subentry_id,
        )


class ZentralySwitch(SwitchEntity):
    """Representation of a Zentraly switch."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        device: ZentralyDevice,
        switch_api: ZentralySwitchApi,
        capability: SwitchCapability,
        channel: int | None = None,
    ) -> None:
        """Initialize the Zentraly switch."""

        self._device = device
        self._switch_api = switch_api
        self._capability = capability
        self._state_version = 0

        self._attr_unique_id = f"{device.device_id}_{capability.value}"
        self._attr_translation_key = capability.value
        if channel is not None:
            self._attr_unique_id += f"_channel_{channel}"
            self._attr_translation_key += "_channel"
            self._attr_translation_placeholders = {"channel": str(channel)}

        if capability in _CONFIG_CAPABILITIES:
            self._attr_entity_category = EntityCategory.CONFIG

    @override
    async def async_added_to_hass(self) -> None:
        """Register Zentraly listeners and periodic state refresh."""

        await super().async_added_to_hass()

        self.async_on_remove(self._device.add_state_listener(self._handle_device_state))

        self.async_on_remove(
            self._device.add_connection_state_listener(self._handle_connection_state)
        )

        self.async_on_remove(
            self._switch_api.add_state_listener(self._handle_state_update)
        )

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
        """Refresh switch state periodically."""

        self.async_schedule_update_ha_state(force_refresh=True)

    @property
    @override
    def available(self) -> bool:
        """Return availability independently of a missing attribute value."""
        if (
            self._capability in _OPENTHERM_CAPABILITIES
            and not self._device.opentherm_connected
        ):
            return False
        return self._device.available and self._device.connected

    def _handle_device_state(self) -> None:
        """Invalidate reads when shared availability changes."""
        if not self.available:
            self._state_version += 1
        self.async_write_ha_state()

    def _handle_connection_state(
        self,
        connected: bool,
    ) -> None:
        """Handle Zentraly connection-state changes."""

        self._state_version += 1
        self._update_availability(connected)

        if not self._attr_available:
            self.async_write_ha_state()
            return

        self.async_schedule_update_ha_state(force_refresh=True)

    def _update_availability(
        self,
        connected: bool,
    ) -> None:
        """Update switch availability."""

        if not connected:
            self._attr_available = False
            return

        if (
            self._capability in _OPENTHERM_CAPABILITIES
            and not self._device.opentherm_connected
        ):
            self._attr_available = False
            return

        self._attr_available = True

    def _handle_state_update(
        self,
        updates: dict[SwitchCapability, Any],
    ) -> None:
        """Handle switch updates received from Zentraly reports."""

        self._update_availability(self._device.connected)

        if not self._attr_available:
            self.async_write_ha_state()
            return

        if self._capability not in updates:
            return

        value = updates[self._capability]

        if not isinstance(value, bool):
            return

        self._state_version += 1
        self._attr_is_on = value
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Update switch state from the Zentraly device."""

        self._update_availability(self._device.connected)

        if not self._attr_available:
            return

        state_version = self._state_version
        value: bool | None

        if self._capability is SwitchCapability.POWER:
            value = await self._switch_api.async_get_power()

        elif self._capability is SwitchCapability.CHILD_LOCK:
            value = await self._switch_api.async_get_child_lock()

        elif self._capability is SwitchCapability.ALWAYS_ON_DISPLAY:
            value = await self._switch_api.async_get_always_on_display()

        elif self._capability is SwitchCapability.ALWAYS_ON_LED:
            value = await self._switch_api.async_get_always_on_led()

        elif self._capability is SwitchCapability.COMFORT_MODE:
            value = await self._switch_api.async_get_comfort_mode()

        elif self._capability is SwitchCapability.FORCED_MODE:
            value = await self._switch_api.async_get_forced_mode()

        elif self._capability is SwitchCapability.TIMER_OFF_ENABLE:
            value = await self._switch_api.async_get_timer_off_enable()

        elif self._capability is SwitchCapability.RETURN_TO_CRONO:
            value = await self._switch_api.async_get_return_to_crono()

        elif self._capability is SwitchCapability.HIGH_VOLTAGE_PROTECTION:
            value = await self._switch_api.async_get_high_voltage_protection()

        elif self._capability is SwitchCapability.LOW_VOLTAGE_PROTECTION:
            value = await self._switch_api.async_get_low_voltage_protection()

        elif self._capability is SwitchCapability.HIGH_POWER_PROTECTION:
            value = await self._switch_api.async_get_high_power_protection()

        else:
            assert_never(self._capability)

        if state_version == self._state_version:
            self._attr_is_on = value

    @override
    @translate_action_errors
    async def async_turn_on(
        self,
        **kwargs: Any,
    ) -> None:
        """Turn the Zentraly switch on."""

        success = await self._async_set_state(True)

        if not success:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="action_failed"
            )

        self._attr_is_on = True
        self.async_write_ha_state()

    @override
    @translate_action_errors
    async def async_turn_off(
        self,
        **kwargs: Any,
    ) -> None:
        """Turn the Zentraly switch off."""

        success = await self._async_set_state(False)

        if not success:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="action_failed"
            )

        self._attr_is_on = False
        self.async_write_ha_state()

    async def _async_set_state(
        self,
        enabled: bool,
    ) -> bool:
        """Set the Zentraly switch state."""

        self._update_availability(self._device.connected)

        if not self._attr_available:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            )

        if self._capability is SwitchCapability.POWER:
            return await self._switch_api.async_set_power(enabled)

        if self._capability is SwitchCapability.CHILD_LOCK:
            return await self._switch_api.async_set_child_lock(enabled)

        if self._capability is SwitchCapability.ALWAYS_ON_DISPLAY:
            return await self._switch_api.async_set_always_on_display(enabled)

        if self._capability is SwitchCapability.ALWAYS_ON_LED:
            return await self._switch_api.async_set_always_on_led(enabled)

        if self._capability is SwitchCapability.COMFORT_MODE:
            return await self._switch_api.async_set_comfort_mode(enabled)

        if self._capability is SwitchCapability.FORCED_MODE:
            return await self._switch_api.async_set_forced_mode(enabled)

        if self._capability is SwitchCapability.TIMER_OFF_ENABLE:
            return await self._switch_api.async_set_timer_off_enable(enabled)

        if self._capability is SwitchCapability.RETURN_TO_CRONO:
            return await self._switch_api.async_set_return_to_crono(enabled)

        if self._capability is SwitchCapability.HIGH_VOLTAGE_PROTECTION:
            return await self._switch_api.async_set_high_voltage_protection(enabled)

        if self._capability is SwitchCapability.LOW_VOLTAGE_PROTECTION:
            return await self._switch_api.async_set_low_voltage_protection(enabled)

        if self._capability is SwitchCapability.HIGH_POWER_PROTECTION:
            return await self._switch_api.async_set_high_power_protection(enabled)

        raise ServiceValidationError(
            translation_domain=DOMAIN, translation_key="invalid_action"
        )

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return device information."""

        return self._device.device_info
