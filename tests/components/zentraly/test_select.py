"""Tests for Zentraly operating mode selection."""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
import logging
from typing import Any
from unittest.mock import MagicMock, patch

import pytest
from zentraly import (
    SelectCapability,
    SelectOperationMode,
    SwitchCapability,
    ZentralyApi,
    ZentralyApiError,
    ZentralyConnectionError,
    ZentralyInvalidResponseError,
    ZentralySelectApi,
)

from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.components.zentraly.select import (
    ZentralySelect,
    _create_select_entities,
)
from homeassistant.const import EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent

from .helpers import (
    Action,
    StateCase,
    async_check_action_waits_for_read,
    async_check_capability_action_errors,
    async_check_partial_report_updates_entity,
    async_check_periodic_read_policy,
    async_check_platform_device_availability,
    async_check_platform_refresh_lifecycle,
    async_check_report_during_read,
)


@pytest.mark.parametrize(
    ("success", "expected", "expectation"),
    [
        pytest.param(True, "auto", nullcontext(), id="success"),
        pytest.param(False, "manual", pytest.raises(HomeAssistantError), id="failure"),
    ],
)
async def test_select_mode(
    platform_device: MagicMock,
    success: bool,
    expected: str,
    expectation: AbstractContextManager,
) -> None:
    """Only a successful command changes the displayed option."""
    api = MagicMock(spec=ZentralySelectApi)
    api.get_options.return_value = [
        SelectOperationMode.MANUAL,
        SelectOperationMode.AUTO,
    ]
    api.async_get_operation_mode.return_value = SelectOperationMode.MANUAL
    api.async_set_operation_mode.return_value = success
    entity = ZentralySelect(
        device=platform_device,
        select_api=api,
        capability=SelectCapability.OPERATION_MODE,
    )
    await entity.async_update()
    with patch.object(entity, "async_write_ha_state"), expectation:
        await entity.async_select_option("auto")
    assert entity.current_option == expected
    assert entity.options == ["manual", "auto"]
    api.async_set_operation_mode.assert_awaited_once_with(SelectOperationMode.AUTO)


async def test_disconnected_select(platform_device: MagicMock) -> None:
    """Do not send a mode change through a disconnected gateway."""
    platform_device.connected = False
    api = MagicMock(spec=ZentralySelectApi)
    api.get_options.return_value = [SelectOperationMode.AUTO]
    entity = ZentralySelect(
        device=platform_device,
        select_api=api,
        capability=SelectCapability.OPERATION_MODE,
    )
    with pytest.raises(HomeAssistantError):
        await entity.async_select_option("auto")
    api.async_set_operation_mode.assert_not_awaited()


async def test_missing_mode_is_unknown(platform_device: MagicMock) -> None:
    """A missing mode clears the old selection without marking the device offline."""
    api = MagicMock(spec=ZentralySelectApi)
    api.get_options.return_value = [SelectOperationMode.MANUAL]
    api.async_get_operation_mode.side_effect = [SelectOperationMode.MANUAL, None]
    entity = ZentralySelect(
        device=platform_device,
        select_api=api,
        capability=SelectCapability.OPERATION_MODE,
    )
    await entity.async_update()
    assert entity.current_option == "manual"
    await entity.async_update()
    assert entity.current_option is None
    assert entity.available


@pytest.mark.parametrize(
    ("domain", "model", "factory"),
    [pytest.param("select", "ZTEIM", _create_select_entities, id="select")],
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
    ("domain", "model", "factory"),
    [pytest.param("select", "ZTEIM", _create_select_entities, id="select")],
)
async def test_platform_refresh_lifecycle(
    hass: HomeAssistant,
    domain: str,
    model: str,
    factory: Callable[[ZentralyDevice], list[Entity]],
) -> None:
    """Recover after an initial failure, serialize refreshes and stop on unload."""
    await async_check_platform_refresh_lifecycle(hass, domain, model, factory)


@pytest.mark.parametrize(
    (
        "domain",
        "model",
        "factory",
        "capability",
        "cluster",
        "attribute_id",
        "raw",
        "property_name",
        "expected",
    ),
    [
        pytest.param(
            "select",
            "ZTEIM",
            _create_select_entities,
            "operation_mode",
            65006,
            28,
            2,
            "current_option",
            "auto",
            id="mode",
        )
    ],
)
async def test_partial_report_updates_entity(
    hass: HomeAssistant,
    domain: str,
    model: str,
    factory: Callable[[ZentralyDevice], list[Entity]],
    capability: str,
    cluster: int,
    attribute_id: int,
    raw: int,
    property_name: str,
    expected: bool | float | str,
) -> None:
    """Valid fields survive malformed neighbors; unrelated reports preserve state."""
    await async_check_partial_report_updates_entity(
        hass,
        domain,
        model,
        factory,
        capability,
        cluster,
        attribute_id,
        raw,
        property_name,
        expected,
    )


@pytest.mark.parametrize(
    ("device_id", "action"),
    [
        pytest.param(
            "ZTEIM0100000001",
            lambda d: ZentralySelectApi(d).async_set_operation_mode(
                SelectOperationMode.MANUAL
            ),
            id="select",
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


@pytest.mark.parametrize(
    "state_case",
    [
        pytest.param(
            StateCase(
                Platform.SELECT,
                "ZentralySelectApi",
                "ZTEIM",
                SelectCapability.OPERATION_MODE,
                "async_get_operation_mode",
                SelectOperationMode.MANUAL,
                SelectOperationMode.AUTO,
                "manual",
                "auto",
                "async_set_operation_mode",
                "select_option",
                {"option": "auto"},
                metadata={
                    "get_options.return_value": [
                        SelectOperationMode.MANUAL,
                        SelectOperationMode.AUTO,
                    ]
                },
            ),
            id="select",
        )
    ],
)
async def test_report_during_read(
    hass: HomeAssistant,
    state_case: StateCase,
    state_entity: tuple[str, MagicMock],
    connection_state: Callable[[bool], None],
) -> None:
    """An old response cannot replace a report; later reads still work."""
    await async_check_report_during_read(
        hass, state_case, state_entity, connection_state
    )


@pytest.mark.parametrize(
    "state_case",
    [
        pytest.param(
            StateCase(
                Platform.SELECT,
                "ZentralySelectApi",
                "ZTEIM",
                SelectCapability.OPERATION_MODE,
                "async_get_operation_mode",
                SelectOperationMode.MANUAL,
                SelectOperationMode.AUTO,
                "manual",
                "auto",
                "async_set_operation_mode",
                "select_option",
                {"option": "auto"},
                metadata={
                    "get_options.return_value": [
                        SelectOperationMode.MANUAL,
                        SelectOperationMode.AUTO,
                    ]
                },
            ),
            id="select",
        )
    ],
)
async def test_action_waits_for_read(
    hass: HomeAssistant,
    state_case: StateCase,
    state_entity: tuple[str, MagicMock],
    connection_state: Callable[[bool], None],
) -> None:
    """A service waits for a pending read and leaves the confirmed new value."""
    await async_check_action_waits_for_read(
        hass, state_case, state_entity, connection_state
    )


@pytest.mark.parametrize(
    "state_case",
    [
        pytest.param(
            StateCase(
                Platform.SELECT,
                "ZentralySelectApi",
                "ZTEIM",
                SelectCapability.OPERATION_MODE,
                "async_get_operation_mode",
                SelectOperationMode.MANUAL,
                SelectOperationMode.AUTO,
                "manual",
                "auto",
                "async_set_operation_mode",
                "select_option",
                {"option": "auto"},
                metadata={
                    "get_options.return_value": [
                        SelectOperationMode.MANUAL,
                        SelectOperationMode.AUTO,
                    ]
                },
            ),
            id="select",
        )
    ],
)
@pytest.mark.parametrize("periodic_polling", [True, False])
async def test_periodic_read_policy(
    hass: HomeAssistant,
    state_case: StateCase,
    state_entity: tuple[str, MagicMock],
    periodic_polling: bool,
    connection_state: Callable[[bool], None],
) -> None:
    """Suppress only recurring reads; initial, report and reconnect paths work."""
    await async_check_periodic_read_policy(
        hass, state_case, state_entity, periodic_polling, connection_state
    )


@pytest.mark.parametrize(("option", "raw"), [("temperature", 1), ("time", 0)])
@pytest.mark.parametrize("model", ["ZTTWZ", "ZTTIN"])
async def test_display_setting_roundtrip(
    model: str, hass: HomeAssistant, option: str, raw: int
) -> None:
    """Display configuration uses its own enum and never writes operation mode."""
    api = ZentralyApi("192.168.1.42", 80, "password", f"{model}0100000001")
    api._set_connected(True)
    device = create_device(api, api.device_id, "aa")
    device.set_switch_state(SwitchCapability.ALWAYS_ON_DISPLAY, True)
    entity = _create_select_entities(device)[0]
    entity.entity_id = "select.zentraly_display"
    component = EntityComponent(logging.getLogger(__name__), "select", hass)
    sent: list[dict[str, Any]] = []

    async def execute(
        builder: Callable[[int], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        command = builder(1)
        sent.append(command)
        return 1, {**command, "status": 200, "attrs": [{"id": 102, "val": raw}]}

    with patch.object(api, "async_execute_command", side_effect=execute):
        with patch(
            "homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"
        ):
            await component.async_add_entities([entity])
        assert entity.current_option is None
        assert entity.options == ["temperature", "time"]
        assert entity.entity_category is EntityCategory.CONFIG
        await entity.async_update()
        assert entity.current_option == option
        sent.clear()
        await entity.async_select_option(option)
        assert sent == [
            {
                "cmd": "writeAttr",
                "rid": 1,
                "mac": "aa",
                "cluster": 65513,
                "ep": 1,
                "attrs": [{"id": 102, "type": 41, "val": raw}],
            }
        ]
        sent.clear()
        with pytest.raises(ServiceValidationError):
            await entity.async_select_option("manual")
        assert sent == []
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {"mac": "aa", "ep": 1, "cluster": 65513, "id": 102, "val": 1 - raw}
                ],
            }
        )
        assert entity.current_option == entity.options[raw]
        api._handle_report(
            {
                "cmd": "report",
                "data": [{"mac": "aa", "ep": 1, "cluster": 65513, "id": 102, "val": 2}],
            }
        )
        assert entity.current_option == entity.options[raw]
        await entity.async_remove()
    assert api._report_listeners == {}


@pytest.mark.parametrize(
    "independent_platform", [(Platform.SELECT, "display_mode")], indirect=True
)
async def test_platform_without_other_platforms(
    hass: HomeAssistant, independent_platform: str
) -> None:
    """This platform obtains its own prerequisites without any other platform."""
    assert hass.states.get(independent_platform).state == "temperature"
