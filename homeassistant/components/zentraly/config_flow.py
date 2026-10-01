"""Config flow for the Zentraly integration."""

from collections.abc import Mapping
import logging
from types import MappingProxyType
from typing import Any, override

import probatio
from zentraly import (
    DeviceModel,
    ZentralyApi,
    ZentralyAuthenticationError,
    ZentralyConnectionError,
    get_device_model,
    get_max_child_devices,
    is_allowed_child_device,
    supports_child_devices,
    supports_zeroconf_setup,
)

from homeassistant.config_entries import (
    SOURCE_REAUTH,
    ConfigEntry,
    ConfigEntryState,
    ConfigFlow as HAConfigFlow,
    ConfigFlowResult,
    ConfigSubentry,
    ConfigSubentryFlow,
    SubentryFlowResult,
)
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MAC,
    CONF_PASSWORD,
    CONF_PORT,
)
from homeassistant.core import HomeAssistant, callback
from homeassistant.helpers import selector
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.helpers.service_info.zeroconf import ZeroconfServiceInfo

from .const import DOMAIN

_LOGGER = logging.getLogger(__name__)

SUBENTRY_TYPE_DEVICE = "device"
CONF_PARENT = "parent"

PASSWORD_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_PASSWORD): str,
    }
)

CHILD_DEVICE_SCHEMA = probatio.Schema(
    {
        probatio.Required(CONF_DEVICE_ID): str,
        probatio.Required(CONF_MAC): str,
    }
)


def _device_id_is_configured_as_subentry(
    config_entries: list[ConfigEntry],
    device_id: str,
) -> bool:
    """Return whether a device ID is configured as a Zentraly subentry."""

    for entry in config_entries:
        for subentry in entry.subentries.values():
            if subentry.unique_id == device_id:
                return True

            if subentry.data.get(CONF_DEVICE_ID) == device_id:
                return True

    return False


def _device_id_is_configured(
    config_entries: list[ConfigEntry],
    device_id: str,
) -> bool:
    """Return whether a device ID is already configured in Zentraly."""

    for entry in config_entries:
        if entry.unique_id == device_id:
            return True

        if entry.data.get(CONF_DEVICE_ID) == device_id:
            return True

        for subentry in entry.subentries.values():
            if subentry.unique_id == device_id:
                return True

            if subentry.data.get(CONF_DEVICE_ID) == device_id:
                return True

    return False


def _child_limit_reached(
    entry: ConfigEntry,
    parent_device_id: str,
) -> bool:
    """Return whether a parent reached its child-device limit."""

    max_children = get_max_child_devices(parent_device_id)
    if max_children is None:
        return False

    child_count = len(
        entry.get_subentries_of_type(
            SUBENTRY_TYPE_DEVICE,
        )
    )

    return child_count >= max_children


def _parent_error(entry: ConfigEntry) -> str | None:
    """Return why an entry cannot currently accept a child."""
    if entry.state is not ConfigEntryState.LOADED:
        return "entry_not_loaded"
    device_id = entry.data.get(CONF_DEVICE_ID)
    if not isinstance(device_id, str) or not supports_child_devices(device_id):
        return "unsupported_parent"
    if _child_limit_reached(entry, device_id):
        return "max_children"
    return None


def _normalize_mac(mac: str) -> str:
    """Normalize a child MAC address."""
    normalized_mac = mac.strip().lower().replace(":", "").replace("-", "")
    if len(normalized_mac) not in (12, 16):
        raise probatio.Invalid(
            "MAC address must contain 12 or 16 hexadecimal characters"
        )
    if any(character not in "0123456789abcdef" for character in normalized_mac):
        raise probatio.Invalid("MAC address must contain only hexadecimal characters")
    return normalized_mac


async def _async_validate_child(
    hass: HomeAssistant, entry: ConfigEntry, user_input: dict[str, Any]
) -> tuple[dict[str, str], dict[str, str]]:
    """Validate child identity for either configuration entry point."""
    device_id = str(user_input[CONF_DEVICE_ID]).strip().upper()
    try:
        mac = _normalize_mac(str(user_input[CONF_MAC]))
    except probatio.Invalid:
        return {}, {"base": "invalid_mac"}
    if get_device_model(device_id) is DeviceModel.UNKNOWN:
        return {}, {"base": "unsupported_device"}
    if not is_allowed_child_device(entry.data[CONF_DEVICE_ID], device_id):
        return {}, {"base": "unsupported_child"}
    if _device_id_is_configured(hass.config_entries.async_entries(DOMAIN), device_id):
        return {}, {"base": "already_configured"}
    try:
        await entry.runtime_data.api.async_validate_child_device(device_id, mac)
    except ZentralyConnectionError:
        return {}, {"base": "cannot_connect"}
    except TypeError, ValueError:
        return {}, {"base": "invalid_device"}
    return {CONF_DEVICE_ID: device_id, CONF_MAC: mac}, {}


class ZentralyConfigFlow(HAConfigFlow, domain=DOMAIN):
    """Handle a Zentraly config flow."""

    def __init__(self) -> None:
        """Initialize the config flow."""

        self.data: dict[str, Any] = {}
        self._parent_entry_id = ""

    @classmethod
    @callback
    @override
    def async_get_supported_subentry_types(
        cls,
        config_entry: ConfigEntry,
    ) -> dict[str, type[ConfigSubentryFlow]]:
        """Return the subentries supported by this config entry."""

        device_id = config_entry.data.get(CONF_DEVICE_ID)

        if not isinstance(device_id, str):
            return {}

        if not supports_child_devices(device_id):
            return {}

        if _child_limit_reached(config_entry, device_id):
            return {}

        return {
            SUBENTRY_TYPE_DEVICE: ZentralyDeviceSubentryFlow,
        }

    @override
    async def async_step_zeroconf(
        self,
        discovery_info: ZeroconfServiceInfo,
    ) -> ConfigFlowResult:
        """Handle Zeroconf discovery."""

        device_id = discovery_info.name.split(".")[0]

        if not supports_zeroconf_setup(device_id):
            return self.async_abort(reason="unsupported_device")

        entries = self.hass.config_entries.async_entries(DOMAIN)

        if _device_id_is_configured_as_subentry(
            entries,
            device_id,
        ):
            return self.async_abort(reason="already_configured")

        self.data[CONF_HOST] = discovery_info.host
        self.data[CONF_PORT] = discovery_info.port
        self.data[CONF_DEVICE_ID] = device_id

        self.context.update(
            {
                "title_placeholders": {
                    "name": device_id,
                }
            }
        )

        _LOGGER.info(
            "Zentraly device discovered: %s at %s:%s",
            device_id,
            self.data[CONF_HOST],
            self.data[CONF_PORT],
        )

        await self.async_set_unique_id(device_id)

        self._abort_if_unique_id_configured(
            updates={
                CONF_HOST: discovery_info.host,
                CONF_PORT: discovery_info.port,
            },
            reload_on_update=False,
        )

        return await self.async_step_auth()

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Request a replacement password for the existing device."""
        entry = self._get_reauth_entry()
        self.data = dict(entry.data)
        self.context["title_placeholders"] = {"name": entry.data[CONF_DEVICE_ID]}
        return await self.async_step_auth()

    async def async_step_auth(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Handle password authentication."""

        errors: dict[str, str] = {}

        if user_input is not None:
            password = user_input[CONF_PASSWORD]

            api = ZentralyApi(
                session=async_get_clientsession(self.hass),
                host=self.data[CONF_HOST],
                port=self.data[CONF_PORT],
                password=password,
                device_id=self.data[CONF_DEVICE_ID],
            )

            try:
                mac = await api.async_validate_password()

            except ZentralyAuthenticationError:
                errors["base"] = "invalid_auth"

            except ZentralyConnectionError:
                errors["base"] = "cannot_connect"

            else:
                if self.source == SOURCE_REAUTH:
                    entry = self._get_reauth_entry()
                    if mac.lower() != entry.data[CONF_MAC].lower():
                        return self.async_abort(reason="wrong_device")

                    reload_by_listener = (
                        bool(entry.update_listeners)
                        and password != entry.data[CONF_PASSWORD]
                    )
                    result = self.async_update_and_abort(
                        entry, data_updates={CONF_PASSWORD: password}
                    )
                    if not reload_by_listener:
                        self.hass.config_entries.async_schedule_reload(entry.entry_id)
                    return result

                self.data[CONF_PASSWORD] = password
                self.data[CONF_MAC] = mac

                _LOGGER.info(
                    "Zentraly device validated: device_id=%s mac=%s ip=%s",
                    self.data[CONF_DEVICE_ID],
                    mac,
                    self.data[CONF_HOST],
                )

                return self.async_create_entry(
                    title=self.data[CONF_DEVICE_ID],
                    data=self.data,
                )

        return self.async_show_form(
            step_id="auth",
            data_schema=PASSWORD_SCHEMA,
            errors=errors,
            description_placeholders=self.context["title_placeholders"],
        )

    @override
    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> ConfigFlowResult:
        """Select a configured parent for a child device."""
        configured_parents = [
            entry
            for entry in self._async_current_entries()
            if isinstance(device_id := entry.data.get(CONF_DEVICE_ID), str)
            and supports_child_devices(device_id)
        ]
        if not configured_parents:
            return self.async_abort(reason="no_parents_configured")
        parents = {
            entry.entry_id: entry
            for entry in configured_parents
            if _parent_error(entry) is None
        }
        if not parents:
            return self.async_abort(reason="no_available_parents")

        errors: dict[str, str] = {}
        if user_input is not None:
            if user_input[CONF_PARENT] in parents:
                self._parent_entry_id = user_input[CONF_PARENT]
                return await self.async_step_child()
            errors["base"] = "parent_unavailable"

        return self.async_show_form(
            step_id="user",
            data_schema=probatio.Schema(
                {
                    probatio.Required(CONF_PARENT): selector.SelectSelector(
                        selector.SelectSelectorConfig(
                            options=[
                                selector.SelectOptionDict(
                                    value=entry.entry_id,
                                    label=f"{entry.title} ({entry.data[CONF_DEVICE_ID]})",
                                )
                                for entry in parents.values()
                            ],
                            mode=selector.SelectSelectorMode.LIST,
                        )
                    )
                }
            ),
            errors=errors,
        )

    async def async_step_child(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Add a child to the selected existing configuration entry."""
        entry = self.hass.config_entries.async_get_entry(self._parent_entry_id)
        if entry is None:
            return self.async_abort(reason="parent_unavailable")
        if error := _parent_error(entry):
            return self.async_abort(reason=error)

        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_validate_child(self.hass, entry, user_input)
            if not errors:
                # Validation awaits the device; the parent may change meanwhile.
                if (
                    self.hass.config_entries.async_get_entry(entry.entry_id)
                    is not entry
                ):
                    return self.async_abort(reason="parent_unavailable")
                if error := _parent_error(entry):
                    return self.async_abort(reason=error)
                if _device_id_is_configured(
                    self.hass.config_entries.async_entries(DOMAIN), data[CONF_DEVICE_ID]
                ):
                    errors["base"] = "already_configured"
                else:
                    self.hass.config_entries.async_add_subentry(
                        entry,
                        ConfigSubentry(
                            data=MappingProxyType(data),
                            subentry_type=SUBENTRY_TYPE_DEVICE,
                            title=data[CONF_DEVICE_ID],
                            unique_id=data[CONF_DEVICE_ID],
                        ),
                    )
                    return self.async_abort(reason="child_added")

        return self.async_show_form(
            step_id="child",
            data_schema=CHILD_DEVICE_SCHEMA,
            errors=errors,
            description_placeholders={"parent": entry.title},
        )


class ZentralyDeviceSubentryFlow(ConfigSubentryFlow):
    """Handle Zentraly child-device subentries."""

    async def async_step_device(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Handle the child-device subentry entry point."""

        return await self.async_step_user(user_input)

    async def async_step_user(
        self,
        user_input: dict[str, Any] | None = None,
    ) -> SubentryFlowResult:
        """Add a Zentraly child device."""

        entry = self._get_entry()

        if error := _parent_error(entry):
            return self.async_abort(reason=error)

        errors: dict[str, str] = {}
        if user_input is not None:
            data, errors = await _async_validate_child(self.hass, entry, user_input)
            if not errors:
                return self.async_create_entry(
                    title=data[CONF_DEVICE_ID],
                    unique_id=data[CONF_DEVICE_ID],
                    data=data,
                )

        return self.async_show_form(
            step_id="user",
            data_schema=CHILD_DEVICE_SCHEMA,
            errors=errors,
        )
