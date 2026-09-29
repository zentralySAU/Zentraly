"""Tests for shared availability across Zentraly platforms."""

import asyncio
from collections.abc import Callable
from datetime import timedelta
import logging
from unittest.mock import patch

import pytest
from zentraly import (
    ZentralyApi,
    ZentralyBinarySensorApi,
    ZentralyConnectionError,
    ZentralyDeviceInfo,
    ZentralyOutputType,
    ZentralySensorApi,
)

from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.binary_sensor import (
    _create_binary_sensor_entities,
)
from homeassistant.components.zentraly.button import _create_button_entities
from homeassistant.components.zentraly.climate import ZentralyClimate
from homeassistant.components.zentraly.const import DOMAIN
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.components.zentraly.number import _create_number_entities
from homeassistant.components.zentraly.select import _create_select_entities
from homeassistant.components.zentraly.sensor import _create_sensor_entities
from homeassistant.components.zentraly.switch import _create_switch_entities
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import (
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


@pytest.mark.parametrize(
    ("domain", "factory", "capability", "api_class", "getter", "value"),
    [
        pytest.param(
            "sensor",
            _create_sensor_entities,
            "feed_temperature",
            ZentralySensorApi,
            "async_get_feed_temperature",
            40.0,
            id="sensor",
        ),
        pytest.param(
            "binary_sensor",
            _create_binary_sensor_entities,
            "ot_dhw_enabled",
            ZentralyBinarySensorApi,
            "async_get_ot_dhw_enabled",
            True,
            id="binary_sensor",
        ),
    ],
)
async def test_opentherm_loss_during_read(
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


@pytest.mark.parametrize(
    ("domain", "model", "factory"),
    [
        pytest.param("sensor", "ZTBIN", _create_sensor_entities, id="sensor"),
        pytest.param(
            "binary_sensor", "ZTBIN", _create_binary_sensor_entities, id="binary-sensor"
        ),
        pytest.param("switch", "ZTEIM", _create_switch_entities, id="switch"),
        pytest.param("button", "ZTBIN", _create_button_entities, id="button"),
        pytest.param("number", "ZTEIM", _create_number_entities, id="number"),
        pytest.param("select", "ZTEIM", _create_select_entities, id="select"),
        pytest.param(
            "climate", "ZTTIN", lambda device: [ZentralyClimate(device)], id="climate"
        ),
    ],
)
async def test_platform_device_availability(
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


@pytest.mark.parametrize(
    ("domain", "factory", "capability", "attribute", "expected"),
    [
        pytest.param(
            "sensor",
            _create_sensor_entities,
            "error_id",
            "native_value",
            None,
            id="sensor-unknown",
        ),
        pytest.param(
            "binary_sensor",
            _create_binary_sensor_entities,
            "ot_dhw_enabled",
            "is_on",
            None,
            id="binary-sensor-unknown",
        ),
        pytest.param(
            "switch",
            _create_switch_entities,
            "comfort_mode",
            "available",
            False,
            id="opentherm-switch",
        ),
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
        pytest.param(
            "sensor",
            _create_sensor_entities,
            "output_type",
            "native_value",
            "on_off",
            id="output-sensor",
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


@pytest.mark.parametrize(
    ("domain", "model", "factory"),
    [
        pytest.param("sensor", "ZTBIN", _create_sensor_entities, id="sensor"),
        pytest.param(
            "binary_sensor", "ZTBIN", _create_binary_sensor_entities, id="binary-sensor"
        ),
        pytest.param("number", "ZTEIM", _create_number_entities, id="number"),
        pytest.param("select", "ZTEIM", _create_select_entities, id="select"),
        pytest.param("switch", "ZTEIM", _create_switch_entities, id="switch"),
    ],
)
async def test_platform_refresh_lifecycle(
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
