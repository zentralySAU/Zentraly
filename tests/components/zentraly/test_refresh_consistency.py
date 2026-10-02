"""Verify report and service ordering through registered HA entities."""

import asyncio
from collections.abc import AsyncIterator, Callable
from dataclasses import dataclass, field
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from zentraly import (
    BinarySensorCapability,
    NumberCapability,
    SelectCapability,
    SelectOperationMode,
    SensorCapability,
    SwitchCapability,
)

from homeassistant.const import ATTR_ENTITY_ID, CONF_DEVICE_ID, Platform
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry, async_fire_time_changed


@dataclass(frozen=True)
class StateCase:
    """A platform with one public capability and an observable state."""

    platform: Platform
    api_class: str
    model: str
    capability: (
        SensorCapability
        | BinarySensorCapability
        | NumberCapability
        | SelectCapability
        | SwitchCapability
    )
    getter: str
    initial: float | bool | SelectOperationMode
    report: float | bool | SelectOperationMode
    initial_state: str
    report_state: str
    setter: str | None = None
    service: str | None = None
    service_data: dict[str, float | str] | None = None
    metadata: dict[str, object] = field(default_factory=dict)


CASES = [
    pytest.param(
        StateCase(
            Platform.SENSOR,
            "ZentralySensorApi",
            "ZTEIM",
            SensorCapability.VOLTAGE,
            "async_get_voltage",
            220.0,
            230.0,
            "220.0",
            "230.0",
        ),
        id="sensor",
    ),
    pytest.param(
        StateCase(
            Platform.BINARY_SENSOR,
            "ZentralyBinarySensorApi",
            "ZTTWZ",
            BinarySensorCapability.BOILER_ON,
            "async_get_boiler_on",
            False,
            True,
            "off",
            "on",
        ),
        id="binary_sensor",
    ),
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
    ),
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
    ),
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
    ),
]

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


@pytest.fixture
def periodic_polling() -> bool:
    """Keep periodic reads enabled unless a test overrides the model policy."""
    return True


@pytest.fixture
async def state_entity(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_device_info: AsyncMock,
    state_case: StateCase,
    periodic_polling: bool,
) -> AsyncIterator[tuple[str, MagicMock]]:
    """Load a real platform while mocking only its public library API."""
    device_id = f"{state_case.model}0100000001"
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_DEVICE_ID: device_id},
        unique_id=device_id,
    )
    mock_api.device_id = device_id
    with (
        patch(
            "homeassistant.components.zentraly.models.ZentralyDevice.supports_periodic_polling",
            new_callable=PropertyMock,
            return_value=periodic_polling,
        ),
        patch(
            "homeassistant.components.zentraly.get_device_platforms",
            return_value=frozenset({state_case.platform}),
        ),
        patch(
            f"homeassistant.components.zentraly.{state_case.platform}.{state_case.api_class}",
            autospec=True,
        ) as api_class,
    ):
        api = api_class.return_value
        api.supports.side_effect = {state_case.capability}.__contains__
        # The factories query metadata through the public API of their platform.
        api.configure_mock(
            **{
                state_case.getter + ".return_value": state_case.initial,
                **state_case.metadata,
            }
        )
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        states = hass.states.async_all(state_case.platform)
        assert len(states) == 1
        yield states[0].entity_id, api


@pytest.mark.parametrize("state_case", CASES)
async def test_report_during_read(
    hass: HomeAssistant,
    state_case: StateCase,
    state_entity: tuple[str, MagicMock],
    connection_state: Callable[[bool], None],
) -> None:
    """An old response cannot replace a report; later reads still work."""
    entity_id, api = state_entity
    started = asyncio.Event()
    release = asyncio.Event()

    async def read() -> float | bool | SelectOperationMode:
        started.set()
        await release.wait()
        return state_case.initial

    getter = getattr(api, state_case.getter)
    getter.side_effect = read
    connection_state(True)
    await started.wait()
    try:
        api.add_state_listener.call_args.args[0](
            {state_case.capability: state_case.report}
        )
    finally:
        release.set()
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == state_case.report_state

    getter.side_effect = None
    connection_state(True)
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == state_case.initial_state


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


@pytest.mark.parametrize("state_case", CASES[2:])
async def test_action_waits_for_read(
    hass: HomeAssistant,
    state_case: StateCase,
    state_entity: tuple[str, MagicMock],
    connection_state: Callable[[bool], None],
) -> None:
    """A service waits for a pending read and leaves the confirmed new value."""
    entity_id, api = state_entity
    assert state_case.setter is not None
    assert state_case.service is not None
    assert state_case.service_data is not None
    setter = getattr(api, state_case.setter)
    setter.return_value = True
    started = asyncio.Event()
    release = asyncio.Event()

    async def read() -> float | bool | SelectOperationMode:
        started.set()
        await release.wait()
        return state_case.initial

    getattr(api, state_case.getter).side_effect = read
    connection_state(True)
    await started.wait()
    action = hass.async_create_task(
        hass.services.async_call(
            state_case.platform,
            state_case.service,
            {ATTR_ENTITY_ID: entity_id, **state_case.service_data},
            blocking=True,
        )
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        setter.assert_not_awaited()
        assert not action.done()
    finally:
        release.set()
    await action
    await hass.async_block_till_done()
    setter.assert_awaited_once()
    assert hass.states.get(entity_id).state == state_case.report_state


@pytest.mark.parametrize("state_case", CASES)
@pytest.mark.parametrize("periodic_polling", [True, False])
async def test_periodic_read_policy(
    hass: HomeAssistant,
    state_case: StateCase,
    state_entity: tuple[str, MagicMock],
    periodic_polling: bool,
    connection_state: Callable[[bool], None],
) -> None:
    """Suppress only recurring reads; initial, report and reconnect paths work."""
    entity_id, api = state_entity
    getter = getattr(api, state_case.getter)
    getter.assert_awaited_once()
    getter.reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert getter.await_count == int(periodic_polling)
    api.add_state_listener.call_args.args[0]({state_case.capability: state_case.report})
    await hass.async_block_till_done()
    assert hass.states.get(entity_id).state == state_case.report_state
    getter.reset_mock()
    connection_state(True)
    await hass.async_block_till_done()
    getter.assert_awaited_once()
    assert hass.states.get(entity_id).state == state_case.initial_state
