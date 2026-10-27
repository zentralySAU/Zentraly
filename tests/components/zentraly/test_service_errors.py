"""Tests for action failure categories and translated errors."""

from homeassistant.core import HomeAssistant
from homeassistant.helpers.translation import async_get_translations


async def test_exception_translations(hass: HomeAssistant) -> None:
    """Exception messages and placeholders are available in generated translations."""
    translations = await async_get_translations(hass, "en", "exceptions", {"zentraly"})
    assert translations["component.zentraly.exceptions.invalid_action.message"] == (
        "The requested value or operation is not supported by this device."
    )
    assert (
        translations[
            "component.zentraly.exceptions.setup_cannot_connect.message"
        ].format(device_id="ZTTIN0100000001")
        == "Unable to connect to Zentraly device ZTTIN0100000001."
    )
