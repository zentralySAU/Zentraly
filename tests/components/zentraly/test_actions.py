"""Tests for action failure categories and translated errors."""

from collections.abc import Callable, Coroutine
from typing import Any
from unittest.mock import MagicMock

import pytest
from zentraly import (
    ZentralyApi,
    ZentralyApiError,
    ZentralyCommandRejectedError,
    ZentralyConnectionBusyError,
    ZentralyConnectionError,
    ZentralyInvalidResponseError,
    ZentralyValidationError,
)

from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.actions import translate_action_errors
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.translation import async_get_translations

type Action = Callable[[ZentralyDevice], Coroutine[Any, Any, bool]]


@pytest.mark.parametrize(
    ("response", "error"),
    [
        pytest.param(None, ZentralyConnectionError, id="timeout"),
        pytest.param((1, {"status": 400}), ZentralyCommandRejectedError, id="rejected"),
        pytest.param((1, {}), ZentralyInvalidResponseError, id="missing-status"),
        pytest.param(
            (1, {"status": "200"}), ZentralyInvalidResponseError, id="invalid-status"
        ),
    ],
)
async def test_shared_action_errors(
    response: tuple[int, dict[str, Any]] | None,
    error: type[ZentralyApiError],
) -> None:
    """Classify transport and status errors once at the shared execution boundary."""
    api = MagicMock(spec=ZentralyApi)
    api.async_execute_command.return_value = response
    device = create_device(api, "ZTEIM0100000001", "aabbccddeeff")
    with pytest.raises(error):
        await device.async_execute_action_command(
            lambda rid: {"cmd": "zclCmd", "rid": rid}
        )


@pytest.mark.parametrize(
    ("error", "ha_error", "key"),
    [
        pytest.param(
            ZentralyConnectionBusyError,
            HomeAssistantError,
            "connection_busy",
            id="saturation",
        ),
        pytest.param(
            ZentralyConnectionError,
            HomeAssistantError,
            "cannot_connect",
            id="connection",
        ),
        pytest.param(
            ZentralyInvalidResponseError,
            HomeAssistantError,
            "invalid_response",
            id="response",
        ),
        pytest.param(
            ZentralyCommandRejectedError,
            HomeAssistantError,
            "command_rejected",
            id="rejected",
        ),
        pytest.param(
            ZentralyValidationError,
            ServiceValidationError,
            "invalid_action",
            id="validation",
        ),
    ],
)
async def test_error_translation(
    error: type[ZentralyApiError], ha_error: type[HomeAssistantError], key: str
) -> None:
    """Expose translation metadata instead of model-specific technical text."""

    @translate_action_errors
    async def action() -> None:
        raise error("Technical protocol detail")

    with pytest.raises(ha_error) as exc:
        await action()
    assert exc.value.translation_domain == "zentraly"
    assert exc.value.translation_key == key
    assert isinstance(exc.value.__cause__, error)


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
