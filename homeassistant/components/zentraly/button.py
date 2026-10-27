"""Button platform for Zentraly."""

from collections.abc import Awaitable, Callable
from typing import override

from zentraly import ButtonCapability, ZentralyButtonApi

from homeassistant.components.button import ButtonEntity
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddConfigEntryEntitiesCallback

from .actions import translate_action_errors
from .const import DOMAIN
from .models import ZentralyConfigEntry, ZentralyDevice

PARALLEL_UPDATES = 1

_OPENTHERM_CAPABILITIES = frozenset(
    {
        ButtonCapability.RESET_BOILER,
    }
)

_CONFIG_CAPABILITIES = frozenset(
    {
        ButtonCapability.RESET_DEVICE,
        ButtonCapability.RESET_BOILER,
    }
)


def _create_button_entities(
    device: ZentralyDevice,
    refresh: Callable[[], Awaitable[None]],
) -> list[ButtonEntity]:
    """Create button entities supported by a Zentraly device."""

    button_api = ZentralyButtonApi(device)

    entities: list[ButtonEntity] = [
        ZentralyButton(
            device=device,
            button_api=button_api,
            capability=capability,
        )
        for capability in ButtonCapability
        if button_api.supports(capability)
    ]

    if device.supports_device_info:
        entities.append(ZentralyRefreshDeviceInfoButton(device, refresh))
    return entities


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ZentralyConfigEntry,
    async_add_entities: AddConfigEntryEntitiesCallback,
) -> None:
    """Set up Zentraly button entities."""

    parent_entities = _create_button_entities(
        entry.runtime_data.device,
        entry.runtime_data.device_info_refresh[entry.runtime_data.device.device_id],
    )

    if parent_entities:
        async_add_entities(
            parent_entities,
            True,
        )

    for subentry_id, child in entry.runtime_data.children.items():
        child_entities = _create_button_entities(
            child,
            entry.runtime_data.device_info_refresh[child.device_id],
        )

        if not child_entities:
            continue

        async_add_entities(
            child_entities,
            True,
            config_subentry_id=subentry_id,
        )


class ZentralyButton(ButtonEntity):
    """Representation of a Zentraly button."""

    _attr_has_entity_name = True
    _attr_should_poll = False

    def __init__(
        self,
        *,
        device: ZentralyDevice,
        button_api: ZentralyButtonApi,
        capability: ButtonCapability,
    ) -> None:
        """Initialize the Zentraly button."""

        self._device = device
        self._button_api = button_api
        self._capability = capability

        self._attr_unique_id = f"{device.device_id}_{capability.value}"
        self._attr_translation_key = capability.value

        if capability in _CONFIG_CAPABILITIES:
            self._attr_entity_category = EntityCategory.CONFIG

    @override
    async def async_added_to_hass(self) -> None:
        """Register the Zentraly connection-state listener."""

        await super().async_added_to_hass()

        self.async_on_remove(self._device.add_state_listener(self.async_write_ha_state))

        self.async_on_remove(
            self._device.add_connection_state_listener(self._handle_connection_state)
        )

    def _handle_connection_state(
        self,
        connected: bool,
    ) -> None:
        """Handle Zentraly connection-state changes."""

        self.async_write_ha_state()

    @property
    @override
    def available(self) -> bool:
        """Return whether the button is available."""

        if not self._device.connected or not self._device.available:
            return False

        if (
            self._capability in _OPENTHERM_CAPABILITIES
            and not self._device.opentherm_connected
        ):
            return False

        return True

    @override
    @translate_action_errors
    async def async_press(self) -> None:
        """Handle the button press."""

        if not self.available:
            raise HomeAssistantError(
                translation_domain=DOMAIN, translation_key="cannot_connect"
            )

        if self._capability is ButtonCapability.RESET_DEVICE:
            success = await self._button_api.async_reset_device()
            if not success:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="action_failed"
                )
            return

        if self._capability is ButtonCapability.RESET_BOILER:
            success = await self._button_api.async_reset_boiler()
            if not success:
                raise HomeAssistantError(
                    translation_domain=DOMAIN, translation_key="action_failed"
                )

    @property
    @override
    def device_info(self) -> DeviceInfo:
        """Return device information."""

        return self._device.device_info


class ZentralyRefreshDeviceInfoButton(ButtonEntity):
    """Read device versions on demand without periodic metadata polling."""

    _attr_has_entity_name = True
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_translation_key = "refresh_device_info"

    def __init__(
        self, device: ZentralyDevice, refresh: Callable[[], Awaitable[None]]
    ) -> None:
        """Initialize the diagnostic action for a parent or child."""
        self._device = device
        self._refresh = refresh
        self._attr_unique_id = f"{device.device_id}_refresh_device_info"
        self._attr_device_info = device.device_info

    @property
    @override
    def available(self) -> bool:
        """Require a connected device for the action."""
        return super().available and self._device.available and self._device.connected

    @override
    async def async_added_to_hass(self) -> None:
        """Track device and connection availability."""
        await super().async_added_to_hass()
        self.async_on_remove(self._device.add_state_listener(self.async_write_ha_state))
        self.async_on_remove(
            self._device.add_connection_state_listener(self._handle_connection_state)
        )

    def _handle_connection_state(self, connected: bool) -> None:
        """Publish connection availability."""
        self.async_write_ha_state()

    @override
    @translate_action_errors
    async def async_press(self) -> None:
        """Refresh versions and report partial failures."""
        await self._refresh()
