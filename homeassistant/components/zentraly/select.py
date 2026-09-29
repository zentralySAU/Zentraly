"""Select platform for Zentraly."""

from datetime import datetime
from typing import Any, assert_never, override

from zentraly import (
    DisplayMode,
    SelectCapability,
    SelectOperationMode,
    ZentralySelectApi,
)

from homeassistant.components.select import SelectEntity
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


def _create_select_entities(
    device: ZentralyDevice,
) -> list[ZentralySelect]:
    """Create select entities supported by a Zentraly device."""

    entities: list[ZentralySelect] = []
    endpoints = device.channel_endpoints
    for endpoint in endpoints:
        select_api = ZentralySelectApi(device, endpoint=endpoint)
        entities.extend(
            ZentralySelect(
                device=device,
                select_api=select_api,
                capability=capability,
                channel=endpoint if len(endpoints) > 1 else None,
            )
            for capability in SelectCapability
            if select_api.supports(capability)
        )
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZentralyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zentraly select entities."""

    parent_entities = _create_select_entities(
        entry.runtime_data.device,
    )

    if parent_entities:
        async_add_entities(
            parent_entities,
        )

    for subentry_id, child in entry.runtime_data.children.items():
        child_entities = _create_select_entities(
            child,
        )

        if not child_entities:
            continue

        async_add_entities(
            child_entities,
            config_subentry_id=subentry_id,
        )


class ZentralySelect(SelectEntity):
    """Representation of a Zentraly select."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        device: ZentralyDevice,
        select_api: ZentralySelectApi,
        capability: SelectCapability,
        channel: int | None = None,
    ) -> None:
        """Initialize the Zentraly select."""

        self._device = device
        self._select_api = select_api
        self._capability = capability
        self._state_version = 0
        self._endpoint = channel if channel is not None else 1

        self._attr_unique_id = f"{device.device_id}_{capability.value}"
        self._attr_translation_key = capability.value
        if channel is not None:
            self._attr_unique_id += f"_channel_{channel}"
            self._attr_translation_key += "_channel"
            self._attr_translation_placeholders = {"channel": str(channel)}

        if capability is SelectCapability.DISPLAY_MODE:
            self._attr_entity_category = EntityCategory.CONFIG

        self._attr_options = [
            option.value for option in select_api.get_options(capability)
        ]

    @override
    async def async_added_to_hass(self) -> None:
        """Register Zentraly listeners and periodic state refresh."""

        await super().async_added_to_hass()

        self.async_on_remove(self._device.add_state_listener(self._handle_device_state))

        self.async_on_remove(
            self._device.add_connection_state_listener(self._handle_connection_state)
        )

        self.async_on_remove(
            self._select_api.add_state_listener(self._handle_state_update)
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
        """Refresh select state periodically."""

        self.async_schedule_update_ha_state(force_refresh=True)

    @property
    @override
    def available(self) -> bool:
        """Return availability independently of a missing attribute value."""
        return (
            self._device.available
            and self._device.connected
            and self._device.capability_enabled(
                self._capability, endpoint=self._endpoint
            )
        )

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

        self._attr_available = connected
        self._state_version += 1

        if not connected:
            self.async_write_ha_state()
            return

        self.async_schedule_update_ha_state(force_refresh=True)

    def _handle_state_update(
        self,
        updates: dict[SelectCapability, Any],
    ) -> None:
        """Handle select updates received from Zentraly reports."""

        if self._capability not in updates:
            return

        value = updates[self._capability]

        if value not in self._select_api.get_options(self._capability):
            return

        self._state_version += 1
        option = value.value

        if option not in self._attr_options:
            self._attr_current_option = None
            self.async_write_ha_state()
            return

        self._attr_current_option = option
        self.async_write_ha_state()

    async def async_update(self) -> None:
        """Update select state from the Zentraly device."""

        self._attr_available = self._device.connected

        if not self._device.connected:
            return

        state_version = self._state_version
        value: DisplayMode | SelectOperationMode | None
        if self._capability is SelectCapability.DISPLAY_MODE:
            value = await self._select_api.async_get_display_mode()
        elif self._capability is SelectCapability.OPERATION_MODE:
            value = await self._select_api.async_get_operation_mode()
        else:
            assert_never(self._capability)

        if state_version != self._state_version:
            return

        if value is None:
            self._attr_current_option = None
            return

        option = value.value

        if option not in self._attr_options:
            self._attr_current_option = None
            return

        self._attr_current_option = option

    @override
    @translate_action_errors
    async def async_select_option(
        self,
        option: str,
    ) -> None:
        """Select a Zentraly option."""

        if not self._device.connected:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            )

        try:
            value = next(
                value
                for value in self._select_api.get_options(self._capability)
                if value.value == option
            )
        except StopIteration as err:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_action"
            ) from err

        if self._capability is SelectCapability.DISPLAY_MODE and isinstance(
            value, DisplayMode
        ):
            success = await self._select_api.async_set_display_mode(value)
        elif self._capability is SelectCapability.OPERATION_MODE and isinstance(
            value, SelectOperationMode
        ):
            success = await self._select_api.async_set_operation_mode(value)
        else:
            raise ServiceValidationError(
                translation_domain=DOMAIN, translation_key="invalid_action"
            )

        if not success:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="action_failed"
            )

        self._attr_current_option = value.value
        self.async_write_ha_state()

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return device information."""

        return self._device.device_info
