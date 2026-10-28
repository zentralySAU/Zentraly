"""Tests for Zentraly reset actions."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

from freezegun.api import FrozenDateTimeFactory
import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from zentraly import (
    ButtonCapability,
    ZentralyApiError,
    ZentralyButtonApi,
    ZentralyConnectionError,
    ZentralyDeviceInfo,
    ZentralyInvalidResponseError,
)

from homeassistant.components.button import DOMAIN as BUTTON_DOMAIN
from homeassistant.components.zentraly.button import (
    ZentralyButton,
    _create_button_entities,
)
from homeassistant.components.zentraly.const import DOMAIN
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.const import ATTR_ENTITY_ID, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.util import dt as dt_util

from .conftest import DEVICE_ID
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
    [
        pytest.param(
            "button",
            "ZTBIN",
            lambda device: _create_button_entities(device, AsyncMock()),
            id="button",
        )
    ],
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
            lambda device: _create_button_entities(device, AsyncMock()),
            "reset_boiler",
            "available",
            False,
            id="boiler-reset",
        ),
        pytest.param(
            "button",
            lambda device: _create_button_entities(device, AsyncMock()),
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


@pytest.mark.usefixtures("setup_diagnostic_integration")
async def test_refresh_device_information(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device_info: AsyncMock,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """The diagnostic action refreshes versions on the existing physical device."""
    entity_id = entity_registry.async_get_entity_id(
        Platform.BUTTON, DOMAIN, f"{DEVICE_ID}_refresh_device_info"
    )
    entity = entity_registry.async_get(entity_id)
    assert entity.entity_category is EntityCategory.DIAGNOSTIC
    mock_device_info.reset_mock()
    mock_device_info.return_value = ZentralyDeviceInfo("1.1", "2.1")
    await hass.services.async_call(
        BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
    )
    mock_device_info.assert_awaited_once_with()
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, DEVICE_ID), mock_config_entry.entry_id
    )
    assert device.sw_version == "1.1"
    assert device.hw_version == "2.1"
    assert entity.device_id == device.id


@pytest.mark.usefixtures("setup_diagnostic_integration")
async def test_refresh_failure(
    hass: HomeAssistant,
    mock_device_info: AsyncMock,
    entity_registry: er.EntityRegistry,
) -> None:
    """Manual failures are translated instead of reporting a successful refresh."""
    entity_id = entity_registry.async_get_entity_id(
        Platform.BUTTON, DOMAIN, f"{DEVICE_ID}_refresh_device_info"
    )
    mock_device_info.side_effect = ZentralyConnectionError
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    assert error.value.translation_key == "cannot_connect"


@pytest.mark.parametrize(
    ("info", "error_key", "firmware", "hardware"),
    [
        pytest.param(
            ZentralyDeviceInfo(None, "2.1"),
            "firmware_read_failed",
            "1.0",
            "2.1",
            id="firmware",
        ),
        pytest.param(
            ZentralyDeviceInfo("1.1", None),
            "hardware_read_failed",
            "1.1",
            "2.0",
            id="hardware",
        ),
        pytest.param(
            ZentralyDeviceInfo(), "versions_read_failed", "1.0", "2.0", id="both"
        ),
        pytest.param(
            ZentralyDeviceInfo(None, "2.0"),
            "firmware_read_failed",
            "1.0",
            "2.0",
            id="unchanged-hardware",
        ),
    ],
)
@pytest.mark.usefixtures("setup_diagnostic_integration")
async def test_incomplete_refresh(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_device_info: AsyncMock,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
    info: ZentralyDeviceInfo,
    error_key: str,
    firmware: str,
    hardware: str,
) -> None:
    """Report failed fields while retaining cached and successfully read versions."""
    entity_id = entity_registry.async_get_entity_id(
        Platform.BUTTON, DOMAIN, f"{DEVICE_ID}_refresh_device_info"
    )
    mock_device_info.return_value = info
    with pytest.raises(HomeAssistantError) as error:
        await hass.services.async_call(
            BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    assert error.value.translation_domain == DOMAIN
    assert error.value.translation_key == error_key
    device = device_registry.async_get_device_by_identifier(
        (DOMAIN, DEVICE_ID), mock_config_entry.entry_id
    )
    assert device.sw_version == firmware
    assert device.hw_version == hardware


@pytest.mark.usefixtures("setup_diagnostic_integration")
async def test_disconnect_while_press_waits(
    hass: HomeAssistant,
    mock_device_info: AsyncMock,
    entity_registry: er.EntityRegistry,
    connection_state: Callable[[bool], None],
) -> None:
    """A press accepted before disconnection fails when its queued read starts."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def read() -> ZentralyDeviceInfo:
        started.set()
        await release.wait()
        return ZentralyDeviceInfo("1.1", "2.1")

    mock_device_info.reset_mock()
    mock_device_info.side_effect = read
    entity_id = entity_registry.async_get_entity_id(
        Platform.BUTTON, DOMAIN, f"{DEVICE_ID}_refresh_device_info"
    )
    first = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    )
    await started.wait()
    second = hass.async_create_task(
        hass.services.async_call(
            BUTTON_DOMAIN, "press", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not second.done()
        connection_state(False)
    finally:
        release.set()
        await first
    with pytest.raises(HomeAssistantError) as error:
        await second
    assert error.value.translation_domain == DOMAIN
    assert error.value.translation_key == "cannot_connect"
    mock_device_info.assert_awaited_once_with()


@pytest.fixture
async def setup_diagnostic_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_climate_api: MagicMock,
    mock_device_info: AsyncMock,
) -> None:
    """Load climate and diagnostic actions with public library mocks."""
    with patch(
        "homeassistant.components.zentraly.get_device_platforms",
        return_value=frozenset({Platform.CLIMATE, Platform.BUTTON}),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()


@pytest.mark.parametrize(
    "independent_platform", [(Platform.BUTTON, "reset_boiler")], indirect=True
)
async def test_platform_without_other_platforms(
    hass: HomeAssistant, independent_platform: str
) -> None:
    """This platform obtains its own prerequisites without any other platform."""
    assert hass.states.get(independent_platform).state == "unknown"


@pytest.mark.parametrize("periodic_polling", [True, False])
@pytest.mark.parametrize(
    "independent_platform", [(Platform.BUTTON, "reset_boiler")], indirect=True
)
async def test_boiler_button_polls_only_prerequisites(
    hass: HomeAssistant,
    independent_platform: str,
    mock_api: MagicMock,
    periodic_polling: bool,
    freezer: FrozenDateTimeFactory,
) -> None:
    """An isolated button refreshes availability without sending an action."""
    assert hass.states.get(independent_platform).state == "unknown"
    mock_api.async_execute_command.reset_mock()
    freezer.tick(timedelta(minutes=5))
    async_fire_time_changed(hass, dt_util.utcnow())
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_api.async_execute_command.await_count == int(periodic_polling)
    assert all(
        call.args[0](1)["cmd"] == "readAttr"
        for call in mock_api.async_execute_command.call_args_list
    )
