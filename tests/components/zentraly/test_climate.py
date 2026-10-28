"""Tests for Zentraly thermostat states, services and refreshes."""

import asyncio
from collections.abc import Callable
from contextlib import AbstractContextManager, nullcontext
from dataclasses import replace
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, call, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry, async_fire_time_changed
from zentraly import (
    ClimateCapability,
    ClimateConfiguration,
    ClimateOperationMode,
    ZentralyApi,
    ZentralyApiError,
    ZentralyClimateApi,
    ZentralyCommandRejectedError,
    ZentralyConnectionBusyError,
    ZentralyConnectionError,
    ZentralyInvalidResponseError,
    ZentralyValidationError,
)
from zentraly.devices.zttin import ZttinCommands

from homeassistant.components.climate import (
    ATTR_CURRENT_HUMIDITY,
    ATTR_CURRENT_TEMPERATURE,
    ATTR_HVAC_ACTION,
    ATTR_HVAC_MODE,
    ATTR_PRESET_MODE,
    ATTR_TARGET_TEMP_STEP,
    PRESET_AWAY,
    PRESET_NONE,
    ClimateEntityFeature,
    HVACAction,
    HVACMode,
)
from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.climate import ZentralyClimate
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.const import (
    ATTR_ENTITY_ID,
    ATTR_TEMPERATURE,
    STATE_UNAVAILABLE,
    STATE_UNKNOWN,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError, ServiceValidationError
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent, async_update_entity
from homeassistant.util import dt as dt_util
from homeassistant.util.unit_system import US_CUSTOMARY_SYSTEM

from .conftest import ENTITY_ID
from .helpers import (
    Action,
    async_check_capability_action_errors,
    async_check_platform_device_availability,
)


@pytest.mark.usefixtures("setup_integration")
@pytest.mark.parametrize(
    ("mode", "operation"),
    [
        pytest.param(HVACMode.OFF, ClimateOperationMode.OFF, id="off"),
        pytest.param(HVACMode.HEAT, ClimateOperationMode.MANUAL, id="heat"),
        pytest.param(HVACMode.AUTO, ClimateOperationMode.AUTO, id="auto"),
    ],
)
async def test_hvac_mode(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    mode: HVACMode,
    operation: ClimateOperationMode,
) -> None:
    """Translate Home Assistant mode actions into device operations."""
    await hass.services.async_call(
        "climate",
        "set_hvac_mode",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_HVAC_MODE: mode},
        blocking=True,
    )
    assert hass.states.get(ENTITY_ID).state == mode
    mock_climate_api.async_set_operation_mode.assert_awaited_once_with(operation)


@pytest.mark.usefixtures("setup_integration")
@pytest.mark.parametrize(
    ("success", "expected", "expectation"),
    [
        pytest.param(True, 22.0, nullcontext(), id="success"),
        pytest.param(False, 21.0, pytest.raises(HomeAssistantError), id="failure"),
    ],
)
async def test_temperature_write(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    success: bool,
    expected: float,
    expectation: AbstractContextManager,
) -> None:
    """A failed service action preserves the last reported setpoint."""
    mock_climate_api.async_set_target_temperature.return_value = success
    with expectation:
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 22.0},
            blocking=True,
        )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == expected
    mock_climate_api.async_set_target_temperature.assert_awaited_once_with(22.0)


@pytest.mark.usefixtures("setup_integration")
async def test_away_report(hass: HomeAssistant, mock_climate_api: MagicMock) -> None:
    """Library reports update the registered entity's Away target and heat demand."""
    mock_climate_api.add_state_listener.call_args.args[0](
        {
            ClimateCapability.LOCAL_TEMPERATURE: 19.0,
            ClimateCapability.TARGET_TEMPERATURE: 17.0,
            ClimateCapability.OPERATION_MODE: ClimateOperationMode.AWAY,
            ClimateCapability.HEAT_DEMAND: True,
            ClimateCapability.HUMIDITY: 52.0,
        }
    )
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 19.0
    assert state.attributes[ATTR_TEMPERATURE] == 17.0
    assert state.attributes[ATTR_CURRENT_HUMIDITY] == 52.0
    assert state.attributes[ATTR_PRESET_MODE] == PRESET_AWAY
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING


@pytest.mark.usefixtures("setup_integration")
async def test_missing_readings_clear_previous_values(
    hass: HomeAssistant, mock_climate_api: MagicMock
) -> None:
    """Missing readings become unknown without marking the device disconnected."""
    mock_climate_api.async_get_current_temperature.return_value = None
    mock_climate_api.async_get_target_temperature.return_value = None
    mock_climate_api.async_get_operation_mode.return_value = None
    mock_climate_api.async_get_humidity.return_value = None
    mock_climate_api.async_get_heat_demand.return_value = None
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done(wait_background_tasks=True)
    state = hass.states.get(ENTITY_ID)
    assert state.state == STATE_UNKNOWN
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] is None
    assert state.attributes[ATTR_TEMPERATURE] is None
    assert ATTR_CURRENT_HUMIDITY not in state.attributes
    assert ATTR_HVAC_ACTION not in state.attributes


@pytest.mark.usefixtures("setup_integration")
@pytest.mark.parametrize(
    ("mode", "operation"),
    [
        pytest.param(HVACMode.AUTO, ClimateOperationMode.AUTO, id="auto"),
        pytest.param(HVACMode.OFF, ClimateOperationMode.OFF, id="off"),
        pytest.param(HVACMode.HEAT, ClimateOperationMode.MANUAL, id="heat"),
    ],
)
async def test_temperature_with_explicit_mode(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    mode: HVACMode,
    operation: ClimateOperationMode,
) -> None:
    """An explicit mode is written after the setpoint's manual-mode side effect."""
    writes = MagicMock()
    writes.attach_mock(mock_climate_api.async_set_target_temperature, "temperature")
    writes.attach_mock(mock_climate_api.async_set_operation_mode, "mode")
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 22.0, ATTR_HVAC_MODE: mode},
        blocking=True,
    )
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_TEMPERATURE] == 22.0
    assert state.state == mode
    assert writes.mock_calls == [call.temperature(22.0), call.mode(operation)]


@pytest.mark.usefixtures("setup_integration")
@pytest.mark.parametrize(
    ("initial", "requested", "operation", "success", "expected", "expectation"),
    [
        pytest.param(
            ClimateOperationMode.MANUAL,
            PRESET_AWAY,
            ClimateOperationMode.AWAY,
            True,
            PRESET_AWAY,
            nullcontext(),
            id="enter-away",
        ),
        pytest.param(
            ClimateOperationMode.MANUAL,
            PRESET_AWAY,
            ClimateOperationMode.AWAY,
            False,
            PRESET_NONE,
            pytest.raises(HomeAssistantError),
            id="enter-failure",
        ),
        pytest.param(
            ClimateOperationMode.AWAY,
            PRESET_NONE,
            ClimateOperationMode.MANUAL,
            True,
            PRESET_NONE,
            nullcontext(),
            id="exit-away",
        ),
        pytest.param(
            ClimateOperationMode.AWAY,
            PRESET_NONE,
            ClimateOperationMode.MANUAL,
            False,
            PRESET_AWAY,
            pytest.raises(HomeAssistantError),
            id="exit-failure",
        ),
    ],
)
async def test_preset_write(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    initial: ClimateOperationMode,
    requested: str,
    operation: ClimateOperationMode,
    success: bool,
    expected: str,
    expectation: AbstractContextManager,
) -> None:
    """Only confirmed preset actions update the reported thermostat state."""
    mock_climate_api.add_state_listener.call_args.args[0](
        {ClimateCapability.OPERATION_MODE: initial}
    )
    mock_climate_api.async_set_operation_mode.return_value = success
    with expectation:
        await hass.services.async_call(
            "climate",
            "set_preset_mode",
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_PRESET_MODE: requested},
            blocking=True,
        )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_PRESET_MODE] == expected
    mock_climate_api.async_set_operation_mode.assert_awaited_once_with(operation)


@pytest.mark.usefixtures("setup_integration")
async def test_failed_hvac_mode(
    hass: HomeAssistant, mock_climate_api: MagicMock
) -> None:
    """A rejected mode write exposes a translated HA error without changing state."""
    mock_climate_api.async_set_operation_mode.return_value = False
    with pytest.raises(HomeAssistantError) as exc:
        await hass.services.async_call(
            "climate",
            "set_hvac_mode",
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_HVAC_MODE: HVACMode.OFF},
            blocking=True,
        )
    assert exc.value.translation_key == "action_failed"
    assert exc.value.translation_domain == "zentraly"
    assert hass.states.get(ENTITY_ID).state == HVACMode.HEAT


@pytest.mark.usefixtures("setup_integration")
async def test_periodic_refresh_during_reconnect(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    connection_state: Callable[[bool], None],
) -> None:
    """A periodic refresh shares HA's update guard with a reconnect refresh."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def read() -> float:
        started.set()
        await release.wait()
        return 20.0

    mock_climate_api.async_get_current_temperature.reset_mock()
    mock_climate_api.async_get_current_temperature.side_effect = read
    connection_state(False)
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE
    connection_state(True)
    await started.wait()
    now = dt_util.utcnow()
    try:
        async_fire_time_changed(hass, now + timedelta(minutes=5))
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        mock_climate_api.async_get_current_temperature.assert_awaited_once_with()
    finally:
        release.set()
        await hass.async_block_till_done()
    async_fire_time_changed(hass, now + timedelta(minutes=10))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_climate_api.async_get_current_temperature.await_count == 2
    assert hass.states.get(ENTITY_ID).attributes[ATTR_CURRENT_TEMPERATURE] == 20.0


@pytest.mark.usefixtures("setup_integration")
@pytest.mark.parametrize(
    "error",
    [
        pytest.param(ZentralyConnectionError, id="failure"),
        pytest.param(asyncio.CancelledError, id="cancelled"),
    ],
)
async def test_failed_refresh_cleans_up_reads(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    error: type[BaseException],
    connection_state: Callable[[bool], None],
) -> None:
    """Failed or cancelled reads clean up their siblings and allow a later refresh."""
    started = asyncio.Event()
    cleaned_up = asyncio.Event()
    release = asyncio.Event()

    async def failing_read() -> float:
        await started.wait()
        raise error()

    async def pending_read() -> float:
        started.set()
        try:
            await release.wait()
        finally:
            await asyncio.sleep(0)
            cleaned_up.set()
        return 21.0

    mock_climate_api.async_get_current_temperature.side_effect = failing_read
    mock_climate_api.async_get_target_temperature.side_effect = pending_read
    try:
        connection_state(True)
        await hass.async_block_till_done()
        assert cleaned_up.is_set()
    finally:
        release.set()
        await hass.async_block_till_done()
    mock_climate_api.async_get_current_temperature.side_effect = None
    mock_climate_api.async_get_target_temperature.side_effect = None
    connection_state(True)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 21.0


@pytest.mark.usefixtures("setup_integration")
async def test_unload(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_climate_api: MagicMock,
) -> None:
    """Unload disconnects the client, removes listeners and stops climate polling."""
    reads = mock_climate_api.async_get_current_temperature.await_count
    assert await hass.config_entries.async_unload(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_api.async_disconnect.assert_awaited_once_with()
    mock_climate_api.add_state_listener.return_value.assert_called_once_with()
    assert mock_api.add_connection_state_listener.return_value.call_count == 2
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done(wait_background_tasks=True)
    assert mock_climate_api.async_get_current_temperature.await_count == reads
    assert hass.states.get(ENTITY_ID).state == STATE_UNAVAILABLE


@pytest.mark.usefixtures("setup_integration")
async def test_action_waits_for_refresh(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    connection_state: Callable[[bool], None],
) -> None:
    """A pending refresh cannot overwrite the state from a subsequent action."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def read() -> float:
        started.set()
        await release.wait()
        return 19.0

    mock_climate_api.async_get_current_temperature.side_effect = read
    connection_state(True)
    await started.wait()
    action = hass.async_create_task(
        hass.services.async_call(
            "climate",
            "set_temperature",
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 22.0},
            blocking=True,
        )
    )
    try:
        await asyncio.sleep(0)
        await asyncio.sleep(0)
        mock_climate_api.async_set_target_temperature.assert_not_awaited()
        assert not action.done()
    finally:
        release.set()
        await action
        await hass.async_block_till_done()
    mock_climate_api.async_set_target_temperature.assert_awaited_once_with(22.0)
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 22.0


@pytest.mark.usefixtures("setup_integration")
async def test_temperature_with_unsupported_mode(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
) -> None:
    """Reject an unsupported mode before writing either value."""
    before = hass.states.get(ENTITY_ID)
    with pytest.raises(ServiceValidationError) as error:
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {
                ATTR_ENTITY_ID: ENTITY_ID,
                ATTR_TEMPERATURE: 22.0,
                ATTR_HVAC_MODE: HVACMode.COOL,
            },
            blocking=True,
        )
    assert error.value.translation_key == "invalid_action"
    mock_climate_api.async_set_target_temperature.assert_not_awaited()
    mock_climate_api.async_set_operation_mode.assert_not_awaited()
    assert hass.states.get(ENTITY_ID) == before


@pytest.mark.usefixtures("setup_integration")
@pytest.mark.parametrize(
    ("operation_mode", "target", "hvac_mode"),
    [
        pytest.param(ClimateOperationMode.AUTO, 24.0, HVACMode.AUTO, id="normal"),
        pytest.param(ClimateOperationMode.AWAY, 24.0, HVACMode.HEAT, id="away"),
    ],
)
async def test_report_during_refresh(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    connection_state: Callable[[bool], None],
    operation_mode: ClimateOperationMode,
    target: float,
    hvac_mode: HVACMode,
) -> None:
    """Reports stay current while unaffected fields accept their read results."""
    started = asyncio.Event()
    release = asyncio.Event()

    async def read_humidity() -> float:
        started.set()
        await release.wait()
        return 60.0

    mock_climate_api.supports.side_effect = None
    mock_climate_api.supports.return_value = True
    mock_climate_api.async_get_away_temperature.return_value = 17.0
    mock_climate_api.async_get_humidity.side_effect = read_humidity
    connection_state(True)
    await started.wait()
    try:
        mock_climate_api.add_state_listener.call_args.args[0](
            {
                ClimateCapability.LOCAL_TEMPERATURE: 22.0,
                ClimateCapability.TARGET_TEMPERATURE: 24.0,
                ClimateCapability.AWAY_TEMPERATURE: 18.0,
                ClimateCapability.OPERATION_MODE: operation_mode,
                ClimateCapability.HEAT_DEMAND: True,
            }
        )
    finally:
        release.set()
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 22.0
    assert state.attributes[ATTR_TEMPERATURE] == target
    assert state.attributes[ATTR_CURRENT_HUMIDITY] == 60.0
    assert state.state == hvac_mode
    assert state.attributes[ATTR_HVAC_ACTION] == HVACAction.HEATING

    mock_climate_api.async_get_humidity.side_effect = None
    connection_state(True)
    await hass.async_block_till_done()
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_CURRENT_TEMPERATURE] == 19.0
    assert state.attributes[ATTR_TEMPERATURE] == 21.0


@pytest.mark.usefixtures("setup_integration")
async def test_away_uses_reported_target(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    connection_state: Callable[[bool], None],
) -> None:
    """Away configuration never replaces the effective target reading."""
    mock_climate_api.supports.side_effect = None
    mock_climate_api.supports.return_value = True
    mock_climate_api.async_get_away_temperature.return_value = 17.0
    mock_climate_api.async_get_operation_mode.return_value = ClimateOperationMode.AWAY
    connection_state(True)
    await hass.async_block_till_done()
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 21.0

    await hass.services.async_call(
        "climate",
        "set_preset_mode",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_PRESET_MODE: PRESET_NONE},
        blocking=True,
    )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 21.0


@pytest.mark.parametrize(
    ("domain", "model", "factory"),
    [
        pytest.param(
            "climate", "ZTTIN", lambda device: [ZentralyClimate(device)], id="climate"
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
    ("device_id", "action"),
    [
        pytest.param(
            "ZTTIN0100000001",
            lambda d: ZentralyClimateApi(d).async_set_target_temperature(22),
            id="climate",
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


@pytest.mark.parametrize("model", ["ZTTWZ", "ZTTIN", "ZTREA"])
async def test_climate_away_report(hass: HomeAssistant, model: str) -> None:
    """Climate uses the reported target in away and ignores its configuration value."""
    api = ZentralyApi("192.168.1.42", 80, "password", f"{model}0100000001")
    api._set_connected(True)
    device = create_device(api, api.device_id, "aa")
    entity = ZentralyClimate(device)
    entity.entity_id = "climate.zentraly_report"
    component = EntityComponent(logging.getLogger(__name__), "climate", hass)
    with patch.object(api, "async_execute_command") as execute:
        with patch(
            "homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"
        ):
            await component.async_add_entities([entity])
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {"mac": "aa", "ep": 1, "cluster": 65513, "id": 28, "val": 3},
                    {"mac": "aa", "ep": 1, "cluster": 65513, "id": 18, "val": 1800},
                    {"mac": "aa", "ep": 1, "cluster": 65513, "id": 17, "val": 2500},
                ],
            }
        )
        assert entity.target_temperature == 18.0
        assert entity.preset_mode == "away"
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {"mac": "aa", "ep": 1, "cluster": 65513, "id": 0, "val": 2100},
                ],
            }
        )
        assert entity.current_temperature == 21.0
        assert entity.target_temperature == 18.0
        execute.assert_not_awaited()
        execute.return_value = None
        with patch.object(entity._climate_api, "async_get_away_temperature") as away:
            await entity.async_update()
            away.assert_not_awaited()
        await entity.async_remove()
    assert api._report_listeners == {}


async def test_model_validation() -> None:
    """A rejected model value is translated before any network write."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTTIN0100000001")
    api._connected = True
    device = create_device(api, api.device_id, "aabbccddeeff")
    with patch.object(api._connection, "async_send_command") as send:

        async def build_only(builder: Callable[[int], dict[str, Any]]) -> None:
            builder(1)

        send.side_effect = build_only
        with pytest.raises(ZentralyValidationError):
            await ZentralyClimateApi(device).async_set_target_temperature(100)


@pytest.mark.parametrize(
    "model",
    [
        pytest.param("ZTTIN", id="zttin"),
        pytest.param("ZTTWZ", id="zttwz"),
        pytest.param("ZTREA", id="ztrea"),
    ],
)
def test_existing_climate_configuration(hass: HomeAssistant, model: str) -> None:
    """Existing models expose the same range, step, modes, and features."""
    api = MagicMock(spec=ZentralyApi)
    device = create_device(api, f"{model}0100000001", "aa")
    entity = ZentralyClimate(device)
    entity.hass = hass
    assert entity.min_temp == 5
    assert entity.max_temp == 30
    assert entity.target_temperature_step == 0.5
    assert entity.hvac_modes == [HVACMode.OFF, HVACMode.HEAT, HVACMode.AUTO]
    assert entity.preset_modes == [PRESET_NONE, PRESET_AWAY]
    assert entity.supported_features & ClimateEntityFeature.TARGET_TEMPERATURE
    assert entity.supported_features & ClimateEntityFeature.PRESET_MODE


class RestrictedClimateCommands(ZttinCommands):
    """Test model configured without changes to the API or entity."""

    climate_configuration = ClimateConfiguration(
        minimum_temperature=10,
        maximum_temperature=25,
        temperature_step=1,
        operation_modes=(ClimateOperationMode.OFF, ClimateOperationMode.MANUAL),
    )


def test_model_configuration_controls_ui_and_commands(hass: HomeAssistant) -> None:
    """One declaration changes the UI limits and protocol validation together."""
    device = create_device(MagicMock(spec=ZentralyApi), "ZTTIN0100000001", "aa")
    commands = RestrictedClimateCommands()
    device.commands = commands
    entity = ZentralyClimate(device)
    entity.hass = hass
    assert entity.min_temp == 10
    assert entity.max_temp == 25
    assert entity.target_temperature_step == 1
    assert entity.hvac_modes == [HVACMode.OFF, HVACMode.HEAT]
    assert entity.preset_modes is None
    assert not entity.supported_features & ClimateEntityFeature.PRESET_MODE
    assert (
        commands.build_write_target_temperature(1, "aa", 16)["attrs"][0]["val"] == 1600
    )
    with pytest.raises(ValueError):
        commands.build_write_target_temperature(1, "aa", 5)
    with pytest.raises(ValueError):
        commands.build_write_target_temperature(1, "aa", 20.5)
    with pytest.raises(ValueError):
        commands.build_write_operation_mode(1, "aa", ClimateOperationMode.AUTO)
    with pytest.raises(ValueError):
        commands.build_write_operation_mode(1, "aa", ClimateOperationMode.AWAY)


@pytest.mark.parametrize(
    ("mode_after", "expected"),
    [
        pytest.param(None, HVACMode.AUTO, id="preserve-mode"),
        pytest.param(
            ClimateOperationMode.MANUAL, HVACMode.HEAT, id="manual-after-write"
        ),
    ],
)
async def test_mode_after_setpoint(
    mode_after: ClimateOperationMode | None, expected: HVACMode
) -> None:
    """Only models declaring a mode transition update the displayed mode."""
    device = create_device(MagicMock(spec=ZentralyApi), "ZTTIN0100000001", "aa")
    with patch.object(
        device.commands,
        "climate_configuration",
        replace(ZttinCommands.climate_configuration, mode_after_setpoint=mode_after),
    ):
        entity = ZentralyClimate(device)
        entity._apply_operation_mode(ClimateOperationMode.AUTO)
        with (
            patch.object(
                entity._climate_api, "async_set_target_temperature", return_value=True
            ),
            patch.object(entity, "async_write_ha_state"),
        ):
            await entity.async_set_temperature(temperature=22)
    assert entity.hvac_mode is expected


@pytest.mark.usefixtures("setup_integration")
@pytest.mark.parametrize(
    ("error", "ha_error", "key"),
    [
        pytest.param(ZentralyApiError, HomeAssistantError, "action_failed", id="api"),
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
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    error: type[ZentralyApiError],
    ha_error: type[HomeAssistantError],
    key: str,
) -> None:
    """Expose translation metadata instead of model-specific technical text."""

    mock_climate_api.async_set_target_temperature.side_effect = error(
        "Technical protocol detail"
    )
    with pytest.raises(ha_error) as exc:
        await hass.services.async_call(
            "climate",
            "set_temperature",
            {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 22},
            blocking=True,
        )
    assert exc.value.translation_domain == "zentraly"
    assert exc.value.translation_key == key
    assert isinstance(exc.value.__cause__, error)


@pytest.mark.usefixtures("setup_integration")
async def test_fahrenheit_temperature_write(
    hass: HomeAssistant, mock_climate_api: MagicMock
) -> None:
    """Converted Fahrenheit requests use the supported native temperature step."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: 70.0},
        blocking=True,
    )
    mock_climate_api.async_set_target_temperature.assert_awaited_once_with(21.0)
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == 70


@pytest.mark.parametrize(
    ("service", "initial", "expected"),
    [
        pytest.param(
            "turn_on", ClimateOperationMode.OFF, ClimateOperationMode.MANUAL, id="on"
        ),
        pytest.param(
            "turn_off", ClimateOperationMode.AUTO, ClimateOperationMode.OFF, id="off"
        ),
        pytest.param(
            "toggle",
            ClimateOperationMode.OFF,
            ClimateOperationMode.MANUAL,
            id="toggle-on",
        ),
        pytest.param(
            "toggle",
            ClimateOperationMode.AWAY,
            ClimateOperationMode.OFF,
            id="toggle-off-away",
        ),
    ],
)
@pytest.mark.usefixtures("setup_integration")
async def test_power_actions(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    service: str,
    initial: ClimateOperationMode,
    expected: ClimateOperationMode,
) -> None:
    """Standard power actions use supported modes and clear the Away preset."""
    mock_climate_api.async_get_operation_mode.return_value = initial
    mock_climate_api.add_state_listener.call_args.args[0](
        {ClimateCapability.OPERATION_MODE: initial}
    )
    await hass.services.async_call(
        "climate", service, {ATTR_ENTITY_ID: ENTITY_ID}, blocking=True
    )
    mock_climate_api.async_set_operation_mode.assert_awaited_once_with(expected)
    assert hass.states.get(ENTITY_ID).attributes[ATTR_PRESET_MODE] == PRESET_NONE


@pytest.mark.parametrize(
    ("direction", "native_target", "display_target"),
    [
        pytest.param(-1, 20.5, 69, id="decrement"),
        pytest.param(1, 21.5, 71, id="increment"),
    ],
)
@pytest.mark.usefixtures("setup_integration")
async def test_fahrenheit_display_step(
    hass: HomeAssistant,
    mock_climate_api: MagicMock,
    direction: int,
    native_target: float,
    display_target: int,
) -> None:
    """Fahrenheit controls move the target while writing only Celsius half degrees."""
    hass.config.units = US_CUSTOMARY_SYSTEM
    await async_update_entity(hass, ENTITY_ID)
    state = hass.states.get(ENTITY_ID)
    assert state.attributes[ATTR_TEMPERATURE] == 70
    assert state.attributes[ATTR_TARGET_TEMP_STEP] == 1.0
    requested = (
        state.attributes[ATTR_TEMPERATURE]
        + direction * state.attributes[ATTR_TARGET_TEMP_STEP]
    )
    await hass.services.async_call(
        "climate",
        "set_temperature",
        {ATTR_ENTITY_ID: ENTITY_ID, ATTR_TEMPERATURE: requested},
        blocking=True,
    )
    mock_climate_api.async_set_target_temperature.assert_awaited_once_with(
        native_target
    )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_TEMPERATURE] == display_target


async def test_disabled_polling_keeps_reports_and_reconnect(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_climate_api: MagicMock,
    mock_device_info: AsyncMock,
    connection_state: Callable[[bool], None],
) -> None:
    """The entry preference suppresses recurring reads without suppressing reports."""
    hass.config_entries.async_update_entry(mock_config_entry, pref_disable_polling=True)
    with patch(
        "homeassistant.components.zentraly.get_device_platforms",
        return_value=frozenset({Platform.CLIMATE}),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
    mock_climate_api.async_get_current_temperature.assert_awaited_once()
    mock_climate_api.async_get_current_temperature.reset_mock()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done(wait_background_tasks=True)
    mock_climate_api.async_get_current_temperature.assert_not_awaited()
    mock_climate_api.add_state_listener.call_args.args[0](
        {ClimateCapability.LOCAL_TEMPERATURE: 23.0}
    )
    assert hass.states.get(ENTITY_ID).attributes[ATTR_CURRENT_TEMPERATURE] == 23.0
    connection_state(False)
    connection_state(True)
    await hass.async_block_till_done()
    mock_climate_api.async_get_current_temperature.assert_awaited_once()


@pytest.mark.parametrize(
    "independent_platform", [(Platform.CLIMATE, "climate")], indirect=True
)
async def test_platform_without_other_platforms(
    hass: HomeAssistant, independent_platform: str
) -> None:
    """This platform obtains its own prerequisites without any other platform."""
    assert hass.states.get(independent_platform).state == "heat"
