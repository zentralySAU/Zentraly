"""Tests for Zentraly switch actions."""

from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
import logging
from typing import Any, Self, override
from unittest.mock import MagicMock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry
from zentraly import (
    SwitchCapability,
    ZentralyApi,
    ZentralyApiError,
    ZentralyConnectionError,
    ZentralyInvalidResponseError,
    ZentralySwitchApi,
)
from zentraly.commands.base import ReportUpdates, ZentralyDeviceCommands

from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.components.zentraly.switch import (
    ZentralySwitch,
    _create_switch_entities,
)
from homeassistant.const import ATTR_ENTITY_ID, CONF_DEVICE_ID, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent

from .helpers import (
    Action,
    StateCase,
    async_check_action_waits_for_read,
    async_check_capability_action_errors,
    async_check_output_change_published_immediately,
    async_check_partial_report_updates_entity,
    async_check_periodic_read_policy,
    async_check_platform_device_availability,
    async_check_platform_refresh_lifecycle,
    async_check_report_during_read,
)


@pytest.mark.parametrize(
    ("action", "value"),
    [
        pytest.param("async_turn_on", True, id="on"),
        pytest.param("async_turn_off", False, id="off"),
    ],
)
@pytest.mark.parametrize(
    ("success", "expectation"),
    [
        pytest.param(True, nullcontext(), id="success"),
        pytest.param(False, pytest.raises(HomeAssistantError), id="failure"),
    ],
)
async def test_power_action(
    platform_device: MagicMock,
    action: str,
    value: bool,
    success: bool,
    expectation: AbstractContextManager,
) -> None:
    """Update the switch only after a successful power command."""
    api = MagicMock(spec=ZentralySwitchApi)
    api.async_get_power.return_value = not value
    api.async_set_power.return_value = success
    entity = ZentralySwitch(
        device=platform_device, switch_api=api, capability=SwitchCapability.POWER
    )
    await entity.async_update()
    with patch.object(entity, "async_write_ha_state"), expectation:
        await getattr(entity, action)()
    assert entity.is_on == (success == value)
    api.async_set_power.assert_awaited_once_with(value)


async def test_disconnected_action(platform_device: MagicMock) -> None:
    """Do not send a power command through a disconnected gateway."""
    platform_device.connected = False
    api = MagicMock(spec=ZentralySwitchApi)
    entity = ZentralySwitch(
        device=platform_device, switch_api=api, capability=SwitchCapability.POWER
    )
    with pytest.raises(HomeAssistantError):
        await entity.async_turn_on()
    api.async_set_power.assert_not_awaited()


async def test_missing_power_is_unknown(platform_device: MagicMock) -> None:
    """An unknown power value does not retain an old on state."""
    api = MagicMock(spec=ZentralySwitchApi)
    api.async_get_power.side_effect = [True, None]
    entity = ZentralySwitch(
        device=platform_device, switch_api=api, capability=SwitchCapability.POWER
    )
    await entity.async_update()
    assert entity.is_on is True
    await entity.async_update()
    assert entity.is_on is None
    assert entity.available


@pytest.mark.parametrize(
    ("domain", "model", "factory"),
    [pytest.param("switch", "ZTEIM", _create_switch_entities, id="switch")],
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
            "switch",
            _create_switch_entities,
            "comfort_mode",
            "available",
            False,
            id="opentherm-switch",
        )
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
    ("domain", "model", "factory"),
    [pytest.param("switch", "ZTEIM", _create_switch_entities, id="switch")],
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
            "switch",
            "ZTTWZ",
            _create_switch_entities,
            "always_on_display",
            65513,
            101,
            1,
            "is_on",
            True,
            id="setting",
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
            lambda d: ZentralySwitchApi(d).async_set_power(True),
            id="switch",
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
                Platform.SWITCH,
                "ZentralySwitchApi",
                "ZTEIM",
                SwitchCapability.POWER,
                "async_get_power",
                False,
                True,
                "off",
                "on",
                "async_set_power",
                "turn_on",
                {},
            ),
            id="switch",
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
                Platform.SWITCH,
                "ZentralySwitchApi",
                "ZTEIM",
                SwitchCapability.POWER,
                "async_get_power",
                False,
                True,
                "off",
                "on",
                "async_set_power",
                "turn_on",
                {},
            ),
            id="switch",
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
                Platform.SWITCH,
                "ZentralySwitchApi",
                "ZTEIM",
                SwitchCapability.POWER,
                "async_get_power",
                False,
                True,
                "off",
                "on",
                "async_set_power",
                "turn_on",
                {},
            ),
            id="switch",
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


class ChannelReportCommands(ZentralyDeviceCommands):
    """Minimal channel provider to exercise shared routing independently of models."""

    channel_endpoints = (1, 2, 3)
    capabilities = frozenset({SwitchCapability.POWER})

    @override
    def for_endpoint(self, endpoint: int) -> Self:
        return self

    def parse_report_entry(self, entry: dict[str, Any]) -> ReportUpdates:
        """Leave endpoint filtering to the shared platform under test."""
        return {SwitchCapability.POWER: bool(entry["val"])}


async def test_channel_report_dispatch(hass: HomeAssistant) -> None:
    """The shared platform routes a report only to its MAC and endpoint."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTEIM0100000001")
    api._set_connected(True)
    device = create_device(api, api.device_id, "aa")
    device.commands = ChannelReportCommands()
    entities = _create_switch_entities(device)
    component = EntityComponent(logging.getLogger(__name__), "switch", hass)
    with patch("homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"):
        await component.async_add_entities(entities)
    api._handle_report(
        {
            "cmd": "report",
            "data": [
                {"mac": "bb", "ep": 1, "cluster": 65006, "id": 0, "val": 1},
                {"mac": "aa", "ep": 3, "cluster": 65006, "id": 0, "val": 1},
            ],
        }
    )
    assert [e.is_on for e in entities] == [None, None, True]
    for entity in entities:
        await entity.async_remove()
    assert api._report_listeners == {}


async def test_successful_action_builds_and_sends_command() -> None:
    """The action route preserves the original valid command and result."""
    api = MagicMock(spec=ZentralyApi)
    device = create_device(api, "ZTEIM0100000001", "aabbccddeeff")

    async def execute(
        builder: Callable[[int], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        command = builder(7)
        assert command["rid"] == 7
        assert command["mac"] == "aabbccddeeff"
        assert command["cmd"] == "zclCmd"
        return 7, {"cmd": "zclCmd", "rid": 7, "status": 200}

    api.async_execute_command.side_effect = execute
    assert await ZentralySwitchApi(device).async_set_power(True)


@pytest.mark.usefixtures("mock_device_info")
async def test_disconnect_setting(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    entity_registry: er.EntityRegistry,
) -> None:
    """Changing the error setting does not call power or other switches."""
    device_id = "ZTEIE0100000001"
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_DEVICE_ID: device_id}
    )
    mock_api.device_id = device_id
    with (
        patch(
            "homeassistant.components.zentraly.get_device_platforms",
            return_value=frozenset({Platform.SWITCH}),
        ),
        patch(
            "homeassistant.components.zentraly.switch.ZentralySwitchApi", autospec=True
        ) as api_class,
    ):
        api = api_class.return_value
        api.supports.side_effect = {SwitchCapability.DISCONNECT_ON_ERROR}.__contains__
        api.async_get_disconnect_on_error.return_value = False

        async def write(value: bool) -> bool:
            api.async_get_disconnect_on_error.return_value = value
            return True

        api.async_set_disconnect_on_error.side_effect = write
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        entity_id = f"switch.{device_id.lower()}_disconnect_on_error"
        assert (
            entity_registry.async_get(entity_id).entity_category
            is EntityCategory.CONFIG
        )
        assert hass.states.get(entity_id).state == "off"
        await hass.services.async_call(
            "switch", "turn_on", {ATTR_ENTITY_ID: entity_id}, blocking=True
        )
        api.async_set_disconnect_on_error.assert_awaited_once_with(True)
        api.async_set_power.assert_not_awaited()
        assert hass.states.get(entity_id).state == "on"
