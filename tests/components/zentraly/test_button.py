"""Tests for Zentraly reset actions."""

from collections.abc import Callable
from typing import Any
from unittest.mock import MagicMock

import pytest
from zentraly import (
    ButtonCapability,
    ZentralyApiError,
    ZentralyButtonApi,
    ZentralyConnectionError,
    ZentralyInvalidResponseError,
)

from homeassistant.components.zentraly.button import (
    ZentralyButton,
    _create_button_entities,
)
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity import Entity

from .helpers import (
    Action,
    async_check_capability_action_errors,
    async_check_output_change_published_immediately,
    async_check_platform_device_availability,
)


@pytest.mark.parametrize(
    ("capability", "method"),
    [
        pytest.param(ButtonCapability.RESET_DEVICE, "async_reset_device", id="device"),
        pytest.param(ButtonCapability.RESET_BOILER, "async_reset_boiler", id="boiler"),
    ],
)
async def test_press(
    platform_device: MagicMock, capability: ButtonCapability, method: str
) -> None:
    """Route each reset button to its own command."""
    api = MagicMock(spec=ZentralyButtonApi)
    entity = ZentralyButton(
        device=platform_device, button_api=api, capability=capability
    )
    await entity.async_press()
    getattr(api, method).assert_awaited_once_with()
    assert entity.available


@pytest.mark.parametrize(
    ("connected", "opentherm"),
    [
        pytest.param(False, True, id="gateway-disconnected"),
        pytest.param(True, False, id="opentherm-disconnected"),
    ],
)
async def test_unavailable_boiler(
    platform_device: MagicMock, connected: bool, opentherm: bool
) -> None:
    """Do not send a boiler reset when the connection is unavailable."""
    platform_device.connected = connected
    platform_device.opentherm_connected = opentherm
    api = MagicMock(spec=ZentralyButtonApi)
    entity = ZentralyButton(
        device=platform_device, button_api=api, capability=ButtonCapability.RESET_BOILER
    )
    with pytest.raises(HomeAssistantError):
        await entity.async_press()
    assert not entity.available
    api.async_reset_boiler.assert_not_awaited()


@pytest.mark.parametrize(
    ("domain", "model", "factory"),
    [pytest.param("button", "ZTBIN", _create_button_entities, id="button")],
)
async def test_platform_device_availability(
    hass: HomeAssistant,
    domain: str,
    model: str,
    factory: Callable[[ZentralyDevice], list[Entity]],
) -> None:
    """Every platform publishes a child outage and recovery without gateway loss."""
    await async_check_platform_device_availability(hass, domain, model, factory)


@pytest.mark.parametrize(
    ("domain", "factory", "capability", "attribute", "expected"),
    [
        pytest.param(
            "button",
            _create_button_entities,
            "reset_boiler",
            "available",
            False,
            id="boiler-reset",
        ),
        pytest.param(
            "button",
            _create_button_entities,
            "reset_device",
            "available",
            True,
            id="device-reset",
        ),
    ],
)
async def test_output_change_published_immediately(
    hass: HomeAssistant,
    domain: str,
    factory: Callable[[ZentralyDevice], list[Entity]],
    capability: str,
    attribute: str,
    expected: bool | str | None,
) -> None:
    """Existing entities react to an output-only report without polling."""
    await async_check_output_change_published_immediately(
        hass, domain, factory, capability, attribute, expected
    )


@pytest.mark.parametrize(
    ("device_id", "action"),
    [
        pytest.param(
            "ZTEIM0100000001",
            lambda d: ZentralyButtonApi(d).async_reset_device(),
            id="button",
        )
    ],
)
@pytest.mark.parametrize(
    ("response", "error"),
    [
        pytest.param(None, ZentralyConnectionError, id="transport-error"),
        pytest.param(
            (1, {"status": 200, "cmd": "unexpected", "rid": 1}),
            ZentralyInvalidResponseError,
            id="platform-parser-error",
        ),
    ],
)
async def test_capability_action_errors(
    device_id: str,
    action: Action,
    response: tuple[int, dict[str, Any]] | None,
    error: type[ZentralyApiError],
) -> None:
    """Preserve the failure category through each capability API."""
    await async_check_capability_action_errors(device_id, action, response, error)
