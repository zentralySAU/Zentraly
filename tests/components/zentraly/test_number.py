"""Tests for Zentraly timer selection races."""

import asyncio
from collections.abc import Callable, Generator
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from zentraly import (
    NumberCapability,
    SwitchCapability,
    ZentralyApi,
    ZentralyApiError,
    ZentralyCommandRejectedError,
    ZentralyConnectionError,
    ZentralyInvalidResponseError,
    ZentralyNumberApi,
)
from zentraly.commands.base import ActionCommandExecutor, ZentralyDeviceCommands
from zentraly.devices.zteim import ZteimCommands

from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.components.zentraly.number import (
    ZentralyNumber,
    _create_number_entities,
)
from homeassistant.const import ATTR_ENTITY_ID, CONF_DEVICE_ID, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import ServiceValidationError
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util

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

from tests.common import MockConfigEntry, async_fire_time_changed


@pytest.mark.parametrize(
    ("capability", "expected"),
    [
        pytest.param(NumberCapability.TIMER, (1.0, 1440.0, 1.0), id="timer"),
        pytest.param(
            NumberCapability.HIGH_VOLTAGE_LIMIT, (245.0, 265.0, 5.0), id="high-voltage"
        ),
        pytest.param(
            NumberCapability.LOW_VOLTAGE_LIMIT, (150.0, 190.0, 5.0), id="low-voltage"
        ),
        pytest.param(
            NumberCapability.HIGH_POWER_LIMIT, (200.0, 2200.0, 50.0), id="power"
        ),
    ],
)
def test_model_number_range(
    capability: NumberCapability, expected: tuple[float, float, float]
) -> None:
    """The typed metadata contract preserves current number limits."""
    device = create_device(MagicMock(spec=ZentralyApi), "ZTEIM0100000001", "aa")
    assert ZentralyNumberApi(device).get_range(capability) == expected


class CompactTimerCommands(ZteimCommands):
    """A timer variant declaring only its supported capability and range."""

    capabilities = frozenset({NumberCapability.TIMER})
    number_ranges = {NumberCapability.TIMER: (1, 60, 1)}


def test_number_range_from_another_model() -> None:
    """A model declaration changes the entity range without API changes."""
    device = create_device(MagicMock(spec=ZentralyApi), "ZTEIM0100000001", "aa")
    device.commands = CompactTimerCommands()
    api = ZentralyNumberApi(device)
    entity = ZentralyNumber(
        device=device, number_api=api, capability=NumberCapability.TIMER
    )
    assert (entity.native_min_value, entity.native_max_value, entity.native_step) == (
        1,
        60,
        1,
    )
    assert api.get_range(NumberCapability.HIGH_POWER_LIMIT) is None


@pytest.fixture
def number_api() -> MagicMock:
    """Return a timer API with deterministic responses."""
    api = MagicMock(spec=ZentralyNumberApi)
    api.get_range.return_value = (0, 120, 1)
    api.async_set_timer = AsyncMock(return_value=True)
    api.async_get_timer = AsyncMock(return_value=10.0)
    return api


@pytest.fixture
def timer(hass: HomeAssistant, number_api: MagicMock) -> Generator[ZentralyNumber]:
    """Return a timer entity with state publishing isolated from platform setup."""
    device = MagicMock(spec=ZentralyDevice)
    device.device_id = "ZTEIM0100000001"
    device.connected = True
    entity = ZentralyNumber(
        device=device, number_api=number_api, capability=NumberCapability.TIMER
    )
    entity.hass = hass
    entity.entity_id = "number.zentraly_timer"
    with (
        patch.object(entity, "async_write_ha_state"),
        patch.object(entity, "_async_write_ha_state"),
    ):
        yield entity


@pytest.fixture
def schedule() -> Generator[MagicMock]:
    """Capture debounce callbacks so tests control when each write starts."""
    with patch("homeassistant.components.zentraly.number.async_call_later") as mock:
        yield mock


async def test_missing_timer_is_unknown(
    timer: ZentralyNumber, number_api: MagicMock
) -> None:
    """A missing reading clears the old timer without declaring a device outage."""
    await timer.async_update()
    assert timer.native_value == 10.0
    number_api.async_get_timer.return_value = None
    await timer.async_update()
    assert timer.native_value is None
    assert timer.available


@pytest.mark.parametrize(
    "success", [pytest.param(True, id="success"), pytest.param(False, id="failure")]
)
async def test_old_write_keeps_new_selection(
    timer: ZentralyNumber,
    number_api: MagicMock,
    schedule: MagicMock,
    success: bool,
) -> None:
    """Finishing the 30-minute write cannot replace a pending 40-minute selection."""
    started = asyncio.Event()
    result = asyncio.get_running_loop().create_future()

    async def write(value: float) -> bool:
        started.set()
        return await result

    number_api.async_set_timer.side_effect = write
    await timer.async_set_native_value(30)
    task = asyncio.create_task(schedule.call_args.args[2](dt_util.utcnow()))
    await started.wait()
    await timer.async_set_native_value(40)
    result.set_result(success)
    await task

    assert timer.native_value == 40
    number_api.async_get_timer.assert_not_awaited()
    number_api.async_set_timer.assert_awaited_once_with(30)

    await schedule.call_args.args[2](dt_util.utcnow())
    assert number_api.async_set_timer.await_args_list == [call(30), call(40)]


async def test_recovery_read_keeps_new_selection(
    timer: ZentralyNumber,
    number_api: MagicMock,
    schedule: MagicMock,
) -> None:
    """A recovery read started before a new selection cannot overwrite it."""
    started = asyncio.Event()
    result = asyncio.get_running_loop().create_future()

    async def read() -> float:
        started.set()
        return await result

    number_api.async_set_timer.return_value = False
    number_api.async_get_timer.side_effect = read
    await timer.async_set_native_value(30)
    task = asyncio.create_task(schedule.call_args.args[2](dt_util.utcnow()))
    await started.wait()
    await timer.async_set_native_value(40)
    result.set_result(10.0)
    await task

    assert timer.native_value == 40
    number_api.async_get_timer.assert_awaited_once()


async def test_timer_debounce(
    hass: HomeAssistant,
    timer: ZentralyNumber,
    number_api: MagicMock,
    schedule: MagicMock,
) -> None:
    """Only the latest selection is scheduled, using the existing three seconds."""
    await timer.async_set_native_value(30)
    cancel = schedule.return_value
    await timer.async_set_native_value(40)

    cancel.assert_called_once_with()
    assert schedule.call_args.args[:2] == (hass, 3.0)
    number_api.async_set_timer.assert_not_awaited()
    await schedule.call_args.args[2](dt_util.utcnow())
    number_api.async_set_timer.assert_awaited_once_with(40)
    assert timer.native_value == 40


async def test_current_write_failure_recovers_device_value(
    timer: ZentralyNumber, number_api: MagicMock, schedule: MagicMock
) -> None:
    """Keep recovery when there is no newer selection."""
    number_api.async_set_timer.return_value = False
    await timer.async_set_native_value(30)
    await schedule.call_args.args[2](dt_util.utcnow())

    number_api.async_get_timer.assert_awaited_once()
    assert timer.native_value == 10.0


async def test_deferred_error_is_logged_and_recovers(
    timer: ZentralyNumber,
    number_api: MagicMock,
    schedule: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """Deferred actions report failure in logs and restore the device value."""
    number_api.async_set_timer.side_effect = ZentralyConnectionError("No response")
    await timer.async_set_native_value(30)
    await schedule.call_args.args[2](dt_util.utcnow())
    assert "Timer write failed" in caplog.text
    assert timer.native_value == 10.0
    number_api.async_get_timer.assert_awaited_once()


@pytest.mark.parametrize(
    "success", [pytest.param(True, id="success"), pytest.param(False, id="failure")]
)
async def test_old_write_finishes_after_new_write(
    timer: ZentralyNumber,
    number_api: MagicMock,
    schedule: MagicMock,
    success: bool,
) -> None:
    """Keep the latest value even after its debounce and write have completed."""
    started = asyncio.Event()
    result = asyncio.get_running_loop().create_future()

    async def write(value: float) -> bool:
        started.set()
        return await result

    number_api.async_set_timer.side_effect = write
    await timer.async_set_native_value(30)
    task = asyncio.create_task(schedule.call_args.args[2](dt_util.utcnow()))
    await started.wait()
    await timer.async_set_native_value(40)
    number_api.async_set_timer.side_effect = None
    await schedule.call_args.args[2](dt_util.utcnow())
    result.set_result(success)
    await task

    assert timer.native_value == 40
    assert number_api.async_set_timer.await_args_list == [call(30), call(40)]
    number_api.async_get_timer.assert_not_awaited()


async def test_periodic_refresh_preserves_pending_timer(
    hass: HomeAssistant,
    timer: ZentralyNumber,
    number_api: MagicMock,
    schedule: MagicMock,
) -> None:
    """Do not replace a timer selection while its debounce is pending."""
    await timer.async_set_native_value(30)
    await timer._async_periodic_refresh(dt_util.utcnow())
    await hass.async_block_till_done()
    number_api.async_get_timer.assert_not_awaited()
    assert timer.native_value == 30
    await schedule.call_args.args[2](dt_util.utcnow())
    await timer._async_periodic_refresh(dt_util.utcnow())
    await hass.async_block_till_done()
    number_api.async_get_timer.assert_awaited_once_with()
    assert timer.native_value == 10


async def test_timer_recovery_read_failure(
    timer: ZentralyNumber,
    number_api: MagicMock,
    schedule: MagicMock,
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A failed recovery read is handled by the Home Assistant update wrapper."""
    number_api.async_set_timer.return_value = False
    number_api.async_get_timer.side_effect = ZentralyConnectionError
    await timer.async_set_native_value(30)
    await schedule.call_args.args[2](dt_util.utcnow())
    assert "Update for number.zentraly_timer fails" in caplog.text
    assert timer.native_value == 30


@pytest.mark.parametrize(
    ("domain", "model", "factory"),
    [pytest.param("number", "ZTEIM", _create_number_entities, id="number")],
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
    [pytest.param("number", "ZTEIM", _create_number_entities, id="number")],
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
            "number",
            "ZTEIM",
            _create_number_entities,
            "high_voltage_limit",
            65006,
            101,
            255,
            "native_value",
            255.0,
            id="limit",
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
            lambda d: ZentralyNumberApi(d).async_set_high_power_limit(1000),
            id="number",
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
                Platform.NUMBER,
                "ZentralyNumberApi",
                "ZTTIN",
                NumberCapability.AWAY_TEMPERATURE,
                "async_get_away_temperature",
                17.0,
                20.0,
                "17.0",
                "20.0",
                "async_set_away_temperature",
                "set_value",
                {"value": 20.0},
                metadata={"get_range.return_value": (5, 30, 1)},
            ),
            id="number",
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
                Platform.NUMBER,
                "ZentralyNumberApi",
                "ZTTIN",
                NumberCapability.AWAY_TEMPERATURE,
                "async_get_away_temperature",
                17.0,
                20.0,
                "17.0",
                "20.0",
                "async_set_away_temperature",
                "set_value",
                {"value": 20.0},
                metadata={"get_range.return_value": (5, 30, 1)},
            ),
            id="number",
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
                Platform.NUMBER,
                "ZentralyNumberApi",
                "ZTTIN",
                NumberCapability.AWAY_TEMPERATURE,
                "async_get_away_temperature",
                17.0,
                20.0,
                "17.0",
                "20.0",
                "async_set_away_temperature",
                "set_value",
                {"value": 20.0},
                metadata={"get_range.return_value": (5, 30, 1)},
            ),
            id="number",
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


TIMER_CASE = StateCase(
    Platform.NUMBER,
    "ZentralyNumberApi",
    "ZTEIM",
    NumberCapability.TIMER,
    "async_get_timer",
    10.0,
    30.0,
    "10.0",
    "30.0",
    "async_set_timer",
    "set_value",
    {"value": 30.0},
    metadata={"get_range.return_value": (1, 1440, 1)},
)


@pytest.mark.parametrize("state_case", [TIMER_CASE])
async def test_deferred_timer_serializes_actions(
    hass: HomeAssistant,
    state_entity: tuple[str, MagicMock],
) -> None:
    """The debounce callback holds the same semaphore as number actions."""
    entity_id, api = state_entity
    started = asyncio.Event()
    release = asyncio.Event()

    async def write(value: float) -> bool:
        started.set()
        await release.wait()
        return True

    api.async_set_timer.side_effect = write
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: entity_id, "value": 30}, blocking=True
    )
    now = dt_util.utcnow()
    async_fire_time_changed(hass, now + timedelta(seconds=4))
    await started.wait()
    action = hass.async_create_task(
        hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: entity_id, "value": 40},
            blocking=True,
        )
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        assert not action.done()
        assert hass.states.get(entity_id).state == "30.0"
    finally:
        release.set()
    await action
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "40.0"
    api.async_set_timer.side_effect = None
    api.async_set_timer.return_value = True
    async_fire_time_changed(hass, now + timedelta(seconds=8))
    await hass.async_block_till_done()
    assert api.async_set_timer.await_count == 2
    api.async_set_timer.assert_awaited_with(40.0)


@pytest.mark.parametrize("state_case", [TIMER_CASE])
async def test_deferred_timer_failure_releases_semaphore(
    hass: HomeAssistant,
    state_entity: tuple[str, MagicMock],
) -> None:
    """A failed deferred write recovers with a read and allows another action."""
    entity_id, api = state_entity
    api.async_set_timer.return_value = False
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: entity_id, "value": 30}, blocking=True
    )
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(seconds=4))
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == "10.0"
    assert api.async_get_timer.await_count == 2
    await hass.services.async_call(
        "number", "set_value", {ATTR_ENTITY_ID: entity_id, "value": 40}, blocking=True
    )
    assert hass.states.get(entity_id).state == "40.0"


async def test_timer_report_preserves_pending_selection(hass: HomeAssistant) -> None:
    """Timer reports share read decoding and cannot overwrite a debounced selection."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTEIM0100000001")
    api._set_connected(True)
    device = create_device(api, api.device_id, "aa")
    entity = next(
        e for e in _create_number_entities(device) if e.translation_key == "timer"
    )
    entity.entity_id = "number.zentraly_report_timer"
    component = EntityComponent(logging.getLogger(__name__), "number", hass)
    report = {
        "cmd": "report",
        "data": [{"mac": "aa", "ep": 1, "cluster": 65006, "id": 9, "val": 601}],
    }
    with patch.object(api, "async_execute_command") as execute:
        with patch(
            "homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"
        ):
            await component.async_add_entities([entity])
        api._handle_report(report)
        assert entity.native_value == 10.0
        await entity.async_set_native_value(30)
        api._handle_report(report)
        assert entity.native_value == 30
        execute.assert_not_awaited()
        await entity.async_remove()
    assert api._report_listeners == {}


@pytest.mark.parametrize(
    "stage",
    [
        pytest.param(0, id="power"),
        pytest.param(1, id="timer"),
        pytest.param(2, id="mode"),
    ],
)
async def test_timer_sequence_failure(stage: int) -> None:
    """Stop the timer sequence when any step returns an invalid payload."""
    responses = [
        (
            1,
            {
                "cmd": "readAttr",
                "rid": 1,
                "status": 200,
                "attrs": [{"id": 0, "val": 1}],
            },
        ),
        (2, {"cmd": "writeAttr", "rid": 2, "status": 200}),
        (3, {"cmd": "writeAttr", "rid": 3, "status": 200}),
    ]
    responses[stage] = (stage + 1, {"cmd": "unexpected", "status": 200})
    api = MagicMock(spec=ZentralyApi)
    api.async_execute_command.side_effect = responses
    device = create_device(api, "ZTEIM0100000001", "aabbccddeeff")
    with pytest.raises(ZentralyInvalidResponseError):
        await ZentralyNumberApi(device).async_set_timer(30)
    assert api.async_execute_command.await_count == stage + 1


@pytest.mark.parametrize(
    ("power_on", "raw_timer"),
    [pytest.param(True, 1800, id="on"), pytest.param(False, 1801, id="off")],
)
async def test_zteim_timer_wire_sequence(power_on: bool, raw_timer: int) -> None:
    """The extracted operation preserves command order and the power-state bit."""
    api = MagicMock(spec=ZentralyApi)
    device = create_device(api, "ZTEIM0100000001", "aabbccddeeff")
    commands: list[dict[str, Any]] = []
    responses = [
        {
            "cmd": "readAttr",
            "rid": 1,
            "status": 200,
            "attrs": [{"id": 0, "val": int(power_on)}],
        },
        {"cmd": "writeAttr", "rid": 2, "status": 200},
        {"cmd": "writeAttr", "rid": 3, "status": 200},
    ]

    async def execute(
        builder: Callable[[int], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        rid = len(commands) + 1
        commands.append(builder(rid))
        return rid, responses[rid - 1]

    api.async_execute_command.side_effect = execute
    assert await ZentralyNumberApi(device).async_set_timer(30)
    assert [command["cmd"] for command in commands] == [
        "readAttr",
        "writeAttr",
        "writeAttr",
    ]
    assert [command["rid"] for command in commands] == [1, 2, 3]
    assert [command["mac"] for command in commands] == [device.mac] * 3
    assert commands[1]["attrs"][0]["val"] == raw_timer
    assert commands[2]["attrs"][0]["val"] == 9


@pytest.mark.parametrize(
    "stage",
    [
        pytest.param(0, id="power"),
        pytest.param(1, id="timer"),
        pytest.param(2, id="mode"),
    ],
)
@pytest.mark.parametrize(
    ("response", "error"),
    [
        pytest.param(None, ZentralyConnectionError, id="timeout"),
        pytest.param(
            (1, {"status": 400}), ZentralyCommandRejectedError, id="rejection"
        ),
    ],
)
async def test_timer_stops_after_failed_command(
    stage: int, response: tuple[int, dict] | None, error: type[ZentralyApiError]
) -> None:
    """A transport failure or rejection stops later steps without replay."""
    responses = [
        (
            1,
            {
                "cmd": "readAttr",
                "rid": 1,
                "status": 200,
                "attrs": [{"id": 0, "val": 1}],
            },
        ),
        (2, {"cmd": "writeAttr", "rid": 2, "status": 200}),
        (3, {"cmd": "writeAttr", "rid": 3, "status": 200}),
    ]
    api = MagicMock(spec=ZentralyApi)
    api.async_execute_command.side_effect = [*responses[:stage], response]
    device = create_device(api, "ZTEIM0100000001", "aabbccddeeff")
    with pytest.raises(error):
        await ZentralyNumberApi(device).async_set_timer(30)
    assert api.async_execute_command.await_count == stage + 1


class SimpleTimerCommands(ZentralyDeviceCommands):
    """Test model with a timer and no power or operation-mode capability."""

    capabilities = frozenset({NumberCapability.TIMER})

    def build_read_timer(self, rid: int, mac: str) -> dict[str, Any]:
        """Build this model's timer query."""
        return {"cmd": "readAttr", "rid": rid, "mac": mac}

    def parse_timer_response(
        self, response: dict[str, Any], expected_rid: int
    ) -> float:
        """Read this model's timer value."""
        return float(response["minutes"])

    async def async_set_timer(
        self, mac: str, value: float, execute: ActionCommandExecutor
    ) -> None:
        """Set a timer with a single model-specific command."""
        await execute(
            lambda rid: {"cmd": "zclCmd", "rid": rid, "mac": mac, "minutes": value}
        )


async def test_timer_without_power_or_mode_capabilities() -> None:
    """Adding a timer model requires no changes to the generic number API."""
    api = MagicMock(spec=ZentralyApi)
    device = create_device(api, "ZTEIM0100000001", "aabbccddeeff")
    device.commands = SimpleTimerCommands()

    async def execute(
        builder: Callable[[int], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        assert builder(1) == {
            "cmd": "zclCmd",
            "rid": 1,
            "mac": device.mac,
            "minutes": 30,
        }
        return 1, {"cmd": "zclCmd", "rid": 1, "status": 200}

    api.async_execute_command.side_effect = execute
    assert await ZentralyNumberApi(device).async_set_timer(30)
    api.async_execute_command.assert_awaited_once()


@pytest.mark.parametrize(
    (
        "capability",
        "attribute",
        "raw",
        "value",
        "limits",
        "unit",
        "invalid",
        "report_raw",
        "report_value",
    ),
    [
        pytest.param(
            NumberCapability.AWAY_TEMPERATURE,
            17,
            1700,
            17.0,
            (5, 30, 1),
            "°C",
            17.5,
            1800,
            18.0,
            id="away-temperature",
        ),
        pytest.param(
            NumberCapability.TEMPERATURE_OFFSET,
            16,
            -120,
            -1.2,
            (-6, 6, 0.1),
            "°C",
            6.1,
            -110,
            -1.1,
            id="temperature-correction",
        ),
        pytest.param(
            NumberCapability.DISPLAY_BRIGHTNESS,
            100,
            0,
            0.0,
            (0, 100, 1),
            "%",
            101,
            20,
            20.0,
            id="display-brightness",
        ),
    ],
)
@pytest.mark.parametrize("model", ["ZTTWZ", "ZTTIN"])
async def test_number_setting_roundtrip(
    hass: HomeAssistant,
    model: str,
    capability: NumberCapability,
    attribute: int,
    raw: int,
    value: float,
    limits: tuple[float, float, float],
    unit: str,
    invalid: float,
    report_raw: int,
    report_value: float,
) -> None:
    """Read, write and report a setting without defaults or extra mode writes."""
    api = ZentralyApi("192.168.1.42", 80, "password", f"{model}0100000001")
    api._set_connected(True)
    device = create_device(api, api.device_id, "aa")
    device.set_switch_state(SwitchCapability.ALWAYS_ON_DISPLAY, True)
    entity = next(
        e
        for e in _create_number_entities(device)
        if e.translation_key == capability.value
    )
    entity.entity_id = "number.zentraly_setting"
    component = EntityComponent(logging.getLogger(__name__), "number", hass)
    sent: list[dict[str, Any]] = []

    async def execute(
        builder: Callable[[int], dict[str, Any]],
    ) -> tuple[int, dict[str, Any]]:
        command = builder(1)
        sent.append(command)
        return 1, {**command, "status": 200, "attrs": [{"id": attribute, "val": raw}]}

    with patch.object(api, "async_execute_command", side_effect=execute):
        with patch(
            "homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"
        ):
            await component.async_add_entities([entity])
        assert entity.native_value is None
        assert entity.entity_category is EntityCategory.CONFIG
        assert (
            entity.native_min_value,
            entity.native_max_value,
            entity.native_step,
        ) == limits
        assert entity.native_unit_of_measurement == unit
        await entity.async_update()
        assert entity.native_value == value
        assert sent == [
            {
                "cmd": "readAttr",
                "rid": 1,
                "mac": "aa",
                "cluster": 65513,
                "ep": 1,
                "attrs": [{"id": attribute, "type": 41}],
            }
        ]
        sent.clear()
        await entity.async_set_native_value(value)
        assert sent == [
            {
                "cmd": "writeAttr",
                "rid": 1,
                "mac": "aa",
                "cluster": 65513,
                "ep": 1,
                "attrs": [{"id": attribute, "type": 41, "val": raw}],
            }
        ]
        sent.clear()
        with pytest.raises(ServiceValidationError):
            await entity.async_set_native_value(invalid)
        assert sent == []
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {
                        "mac": "aa",
                        "ep": 1,
                        "cluster": 65513,
                        "id": attribute,
                        "val": report_raw,
                    }
                ],
            }
        )
        assert entity.native_value == report_value
        assert sent == []
        await entity.async_remove()
    assert api._report_listeners == {}


@pytest.mark.usefixtures("mock_device_info")
@pytest.mark.parametrize(
    ("capability", "setter", "model"),
    [
        pytest.param(
            NumberCapability.BOILER_IGNITION_DELAY,
            "async_set_boiler_ignition_delay",
            "ZTBZB",
            id="ignition",
        ),
        pytest.param(
            NumberCapability.BOILER_SHUTDOWN_DELAY,
            "async_set_boiler_shutdown_delay",
            "ZTBZH",
            id="shutdown",
        ),
    ],
)
async def test_delay_number_service(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    entity_registry: er.EntityRegistry,
    capability: NumberCapability,
    setter: str,
    model: str,
) -> None:
    """Minutes include zero and issue one immediate configuration action."""
    device_id = model + "0100000001"
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_DEVICE_ID: device_id}
    )
    mock_api.device_id = device_id
    with (
        patch(
            "homeassistant.components.zentraly.get_device_platforms",
            return_value=frozenset({Platform.NUMBER}),
        ),
        patch(
            "homeassistant.components.zentraly.number.ZentralyNumberApi", autospec=True
        ) as api_class,
    ):
        api = api_class.return_value
        api.supports.side_effect = {capability}.__contains__
        api.get_range.return_value = (0, 10, 1)
        getattr(api, setter.replace("set_", "get_")).return_value = 4.0
        getattr(api, setter).return_value = True
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        entity_id = f"number.{device_id.lower()}_{capability.value}"
        state = hass.states.get(entity_id)
        assert state.state == "4.0"
        assert (
            state.attributes["min"],
            state.attributes["max"],
            state.attributes["step"],
            state.attributes["unit_of_measurement"],
        ) == (0, 10, 1, "min")
        assert (
            entity_registry.async_get(entity_id).entity_category
            is EntityCategory.CONFIG
        )
        await hass.services.async_call(
            "number",
            "set_value",
            {ATTR_ENTITY_ID: entity_id, "value": 0},
            blocking=True,
        )
        getattr(api, setter).assert_awaited_once_with(0)
        api.async_set_timer.assert_not_awaited()
        assert hass.states.get(entity_id).state == "0.0"
