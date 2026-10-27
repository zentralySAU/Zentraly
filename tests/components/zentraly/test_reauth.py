"""Tests for Zentraly device setup."""

from unittest.mock import patch

from zentraly import ZentralyAuthenticationError

from homeassistant.components.zentraly.const import DOMAIN
from homeassistant.const import CONF_PASSWORD
from homeassistant.core import HomeAssistant

from .test_init import PARENT_MAC, PASSWORD, _parent_entry


async def test_reconnect_starts_reauth(hass: HomeAssistant) -> None:
    """A loaded entry requests new credentials after a reconnect rejection."""
    entry = _parent_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            return_value=PARENT_MAC,
        ),
        patch("homeassistant.components.zentraly.ZentralyApi.async_connect"),
        patch.object(hass.config_entries, "async_forward_entry_setups"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)

    with patch.object(
        entry.runtime_data.api,
        "_async_connect_once",
        side_effect=ZentralyAuthenticationError,
    ):
        await entry.runtime_data.api._async_connection_loop()
        await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    assert flows[0]["step_id"] == "auth"
    assert entry.data[CONF_PASSWORD] == PASSWORD
