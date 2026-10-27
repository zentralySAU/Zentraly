"""Reusable checks and setup for platform-specific Zentraly tests."""

import asyncio
from collections.abc import AsyncIterator, Callable, Coroutine
from dataclasses import dataclass, field
from datetime import timedelta
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, PropertyMock, patch

import pytest
from zentraly import (
    BinarySensorCapability,
    NumberCapability,
    SelectCapability,
    SelectOperationMode,
    SensorCapability,
    SwitchCapability,
    ZentralyApi,
    ZentralyApiError,
    ZentralyBinarySensorApi,
    ZentralyConnectionError,
    ZentralyDeviceInfo,
    ZentralyOutputType,
    ZentralySensorApi,
)

from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.const import DOMAIN
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
    ATTR_ENTITY_ID,
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MAC,
    CONF_PASSWORD,
    CONF_PORT,
    Platform,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry, async_fire_time_changed

type Action = Callable[[ZentralyDevice], Coroutine[Any, Any, bool]]


async def async_check_opentherm_loss_during_read(
    hass: HomeAssistant,
    domain: str,
    factory: Callable[[ZentralyDevice], list[Entity]],
    capability: str,
    api_class: type[ZentralySensorApi | ZentralyBinarySensorApi],
    getter: str,
    value: float | bool,
) -> None:
    """A response cannot restore values invalidated by loss of OpenTherm."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTTWZ0100000001")
    api._set_connected(True)
    device = create_device(api, "ZTBIN0100000001", "bb")
    device.set_output_type(ZentralyOutputType.OPENTHERM)
    entity = next(
        entity for entity in factory(device) if entity.translation_key == capability
    )
    entity.entity_id = f"{domain}.zentraly_test"
    component = EntityComponent(logging.getLogger(__name__), domain, hass)
    started = asyncio.Event()
    release = asyncio.Event()

    async def read() -> float | bool:
        started.set()
        await release.wait()
        return value

    with patch.object(api_class, getter, side_effect=read):
        await component.async_add_entities([entity])
        await started.wait()
        try:
            device.set_output_type(ZentralyOutputType.ON_OFF)
        finally:
            release.set()
        await hass.async_block_till_done()
    assert hass.states.get(entity.entity_id).state == "unknown"
    await entity.async_remove()


async def async_check_platform_device_availability(
    hass: HomeAssistant,
    domain: str,
    model: str,
    factory: Callable[[ZentralyDevice], list[Entity]],
) -> None:
    """Every platform publishes a child outage and recovery without gateway loss."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTTWZ0100000001")
    api._set_connected(True)
    device = create_device(api, f"{model}0100000001", "bb")
    device.set_output_type(ZentralyOutputType.OPENTHERM)
    entity = factory(device)[0]
    entity.hass = hass
    entity.entity_id = f"{domain}.zentraly_test"
    component = EntityComponent(logging.getLogger(__name__), domain, hass)
    with patch.object(entity, "async_write_ha_state") as publish:
        with patch(
            "homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"
        ):
            await component.async_add_entities([entity])
        publish.reset_mock()
        assert entity.available
        with patch.object(api, "async_execute_command", return_value=None):
            await device.async_execute_command(lambda rid: {})
        assert not entity.available
        assert device.connected
        publish.assert_called_once_with()
        with patch.object(
            api, "async_execute_command", return_value=(1, {"status": 200})
        ):
            await device.async_execute_command(lambda rid: {})
        assert entity.available
        assert publish.call_count == 2
        api._set_connected(False)
        assert not entity.available
        await entity.async_remove()
    assert api._report_listeners == {}
    assert api._connection_state_listeners == set()


async def async_check_output_change_published_immediately(
    hass: HomeAssistant,
    domain: str,
    factory: Callable[[ZentralyDevice], list[Entity]],
    capability: str,
    attribute: str,
    expected: bool | str | None,
) -> None:
    """Existing entities react to an output-only report without polling."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTTWZ0100000001")
    api._set_connected(True)
    device = create_device(api, "ZTBIN0100000001", "bb")
    device.set_output_type(ZentralyOutputType.OPENTHERM)
    entity = next(
        entity for entity in factory(device) if entity.translation_key == capability
    )
    entity.hass = hass
    entity.entity_id = f"{domain}.zentraly_test"
    component = EntityComponent(logging.getLogger(__name__), domain, hass)
    with (
        patch.object(entity, "async_write_ha_state") as publish,
        patch.object(api, "async_execute_command") as execute,
    ):
        with patch(
            "homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"
        ):
            await component.async_add_entities([entity])
        publish.reset_mock()
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {"mac": "bb", "ep": 1, "cluster": 65535, "id": 1000, "val": 0}
                ],
            }
        )
        assert getattr(entity, attribute) == expected
        assert publish.called
        execute.assert_not_awaited()
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {"mac": "bb", "ep": 1, "cluster": 65535, "id": 1000, "val": 1}
                ],
            }
        )
        assert entity.available
        await entity.async_remove()
    assert api._report_listeners == {}


async def async_check_platform_refresh_lifecycle(
    hass: HomeAssistant,
    domain: str,
    model: str,
    factory: Callable[[ZentralyDevice], list[Entity]],
) -> None:
    """Recover after an initial failure, serialize refreshes and stop on unload."""
    device_id = f"{model}0100000001"
    api = ZentralyApi("192.168.1.42", 80, "password", device_id)
    api._set_connected(True)
    device = create_device(api, device_id, "aabbccddeeff")
    device.set_output_type(ZentralyOutputType.OPENTHERM)
    entity = factory(device)[0]
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=device_id,
        data={
            CONF_DEVICE_ID: device_id,
            CONF_MAC: device.mac,
            CONF_HOST: api.host,
            CONF_PORT: api.port,
            CONF_PASSWORD: "password",
        },
    )
    entry.add_to_hass(hass)
    with (
        patch("homeassistant.components.zentraly.ZentralyApi", return_value=api),
        patch.object(api, "async_validate_password", return_value=device.mac),
        patch.object(api, "async_connect"),
        patch(
            "homeassistant.components.zentraly.models.ZentralyDevice.async_get_device_info",
            return_value=ZentralyDeviceInfo(),
        ),
        patch(
            "homeassistant.components.zentraly.get_device_platforms",
            return_value=[Platform(domain)],
        ),
        patch(
            f"homeassistant.components.zentraly.{domain}._create_{domain}_entities",
            return_value=[entity],
        ),
        patch.object(
            entity, "async_update", side_effect=[ZentralyConnectionError(), None]
        ) as refresh,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert hass.states.get(entity.entity_id) is not None
        refresh.assert_awaited_once_with()
        now = dt_util.utcnow()
        async_fire_time_changed(hass, now + timedelta(minutes=4))
        await hass.async_block_till_done()
        assert refresh.await_count == 1
        async_fire_time_changed(hass, now + timedelta(minutes=5))
        await hass.async_block_till_done()
        assert refresh.await_count == 2

        started = asyncio.Event()
        release = asyncio.Event()
        scheduled = asyncio.Event()

        async def update() -> None:
            started.set()
            await release.wait()

        original_schedule = entity.async_schedule_update_ha_state

        def schedule(force_refresh: bool = False) -> None:
            original_schedule(force_refresh)
            scheduled.set()

        refresh.side_effect = update
        api._set_connected(False)
        api._set_connected(True)
        await started.wait()
        try:
            with patch.object(
                entity, "async_schedule_update_ha_state", side_effect=schedule
            ):
                async_fire_time_changed(hass, now + timedelta(minutes=10))
                async with asyncio.timeout(1):
                    await scheduled.wait()
                assert refresh.await_count == 3
        finally:
            release.set()
            await hass.async_block_till_done()
        assert await hass.config_entries.async_unload(entry.entry_id)
        async_fire_time_changed(hass, now + timedelta(minutes=15))
        await hass.async_block_till_done()
        assert refresh.await_count == 3
        assert api._connection_state_listeners == set()


async def async_check_partial_report_updates_entity(
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
    api = ZentralyApi("192.168.1.42", 80, "password", f"{model}0100000001")
    api._set_connected(True)
    device = create_device(api, api.device_id, "aa")
    device.set_output_type(ZentralyOutputType.OPENTHERM)
    entity = next(e for e in factory(device) if e.translation_key == capability)
    entity.entity_id = f"{domain}.zentraly_report"
    component = EntityComponent(logging.getLogger(__name__), domain, hass)
    report = {"mac": "aa", "ep": 1, "cluster": cluster, "id": attribute_id, "val": raw}
    with patch.object(api, "async_execute_command") as execute:
        with patch(
            "homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"
        ):
            await component.async_add_entities([entity])
        api._handle_report(
            {"cmd": "report", "data": [None, report, {**report, "val": "invalid"}]}
        )
        assert getattr(entity, property_name) == expected
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {**report, "mac": "bb", "val": 0},
                    {**report, "ep": 2, "val": 0},
                    {**report, "id": 99999, "val": 0},
                    {"mac": "aa", "ep": 1, "cluster": cluster, "id": attribute_id},
                ],
            }
        )
        assert getattr(entity, property_name) == expected
        execute.assert_not_awaited()
        await entity.async_remove()
    assert api._report_listeners == {}


async def async_check_capability_action_errors(
    device_id: str,
    action: Action,
    response: tuple[int, dict[str, Any]] | None,
    error: type[ZentralyApiError],
) -> None:
    """Preserve the failure category through each capability API."""
    api = MagicMock(spec=ZentralyApi)
    api.async_execute_command.return_value = response
    device = create_device(api, device_id, "aabbccddeeff")
    with pytest.raises(error):
        await action(device)


async def async_check_report_during_read(
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


async def async_check_action_waits_for_read(
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


async def async_check_periodic_read_policy(
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
