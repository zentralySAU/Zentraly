"""Tests for Zentraly binary sensor states."""

from collections.abc import Callable
import logging
from unittest.mock import MagicMock, patch

import pytest
from zentraly import (
    BinarySensorCapability,
    ZentralyApi,
    ZentralyBinarySensorApi,
    ZentralyOutputType,
    ZentralySensorApi,
)

from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.binary_sensor import (
    ZentralyBinarySensor,
    _create_binary_sensor_entities,
)
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.const import Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent

from .helpers import (
    StateCase,
    async_check_opentherm_loss_during_read,
    async_check_output_change_published_immediately,
    async_check_periodic_read_policy,
    async_check_platform_device_availability,
    async_check_platform_refresh_lifecycle,
    async_check_report_during_read,
)


@pytest.mark.parametrize(
    "value",
    [
        pytest.param(True, id="on"),
        pytest.param(False, id="off"),
        pytest.param(None, id="unknown"),
    ],
)
async def test_boiler_state(platform_device: MagicMock, value: bool | None) -> None:
    """Expose the boiler state returned by the device."""
    api = MagicMock(spec=ZentralyBinarySensorApi)
    api.async_get_boiler_on.return_value = value
    entity = ZentralyBinarySensor(
        device=platform_device,
        binary_sensor_api=api,
        capability=BinarySensorCapability.BOILER_ON,
    )
    await entity.async_update()
    assert entity.is_on is value
    api.async_get_boiler_on.assert_awaited_once_with()


async def test_disconnected(platform_device: MagicMock) -> None:
    """Do not query an unavailable gateway."""
    platform_device.connected = False
    api = MagicMock(spec=ZentralyBinarySensorApi)
    entity = ZentralyBinarySensor(
        device=platform_device,
        binary_sensor_api=api,
        capability=BinarySensorCapability.BOILER_ON,
    )
    await entity.async_update()
    assert not entity.available
    api.async_get_boiler_on.assert_not_awaited()


def test_report(platform_device: MagicMock) -> None:
    """Reports update the state while malformed values are ignored."""
    api = MagicMock(spec=ZentralyBinarySensorApi)
    entity = ZentralyBinarySensor(
        device=platform_device,
        binary_sensor_api=api,
        capability=BinarySensorCapability.BOILER_ON,
    )
    with patch.object(entity, "async_write_ha_state") as publish:
        entity._handle_state_update({BinarySensorCapability.BOILER_ON: True})
        entity._handle_state_update({BinarySensorCapability.BOILER_ON: "invalid"})
    assert entity.is_on is True
    publish.assert_called_once_with()


async def test_opentherm_loss_clears_state(platform_device: MagicMock) -> None:
    """An output change clears an earlier OpenTherm flag without another read."""
    api = MagicMock(spec=ZentralyBinarySensorApi)
    api.async_get_ot_dhw_enabled.return_value = True
    entity = ZentralyBinarySensor(
        device=platform_device,
        binary_sensor_api=api,
        capability=BinarySensorCapability.OT_DHW_ENABLED,
    )
    await entity.async_update()
    assert entity.is_on is True
    platform_device.opentherm_connected = False
    with patch.object(entity, "async_write_ha_state") as publish:
        entity._handle_device_state()
    assert entity.is_on is None
    assert entity.available
    publish.assert_called_once_with()


@pytest.mark.parametrize(
    ("domain", "factory", "capability", "api_class", "getter", "value"),
    [
        pytest.param(
            "binary_sensor",
            _create_binary_sensor_entities,
            "ot_dhw_enabled",
            ZentralyBinarySensorApi,
            "async_get_ot_dhw_enabled",
            True,
            id="binary_sensor",
        )
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
    await async_check_opentherm_loss_during_read(
        hass, domain, factory, capability, api_class, getter, value
    )


@pytest.mark.parametrize(
    ("domain", "model", "factory"),
    [
        pytest.param(
            "binary_sensor", "ZTBIN", _create_binary_sensor_entities, id="binary-sensor"
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
            "binary_sensor",
            _create_binary_sensor_entities,
            "ot_dhw_enabled",
            "is_on",
            None,
            id="binary-sensor-unknown",
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
    [
        pytest.param(
            "binary_sensor", "ZTBIN", _create_binary_sensor_entities, id="binary-sensor"
        )
    ],
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
    "state_case",
    [
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


@pytest.mark.parametrize("model", ["ZTBIN", "ZTTWZ"])
async def test_opentherm_report_updates_three_indicators(
    hass: HomeAssistant, model: str
) -> None:
    """Output type is applied before a status word, regardless of attribute order."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTTWZ0100000001")
    api._set_connected(True)
    device = create_device(api, f"{model}0100000001", "aa")
    device.set_output_type(ZentralyOutputType.ON_OFF)
    entities = [
        e
        for e in _create_binary_sensor_entities(device)
        if e.translation_key.startswith("ot_")
    ]
    component = EntityComponent(logging.getLogger(__name__), "binary_sensor", hass)
    with patch.object(api, "async_execute_command") as execute:
        with patch(
            "homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"
        ):
            await component.async_add_entities(entities)
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {"mac": "aa", "ep": 1, "cluster": 65535, "id": 0, "val": 3},
                    {"mac": "aa", "ep": 1, "cluster": 65535, "id": 1000, "val": 1},
                ],
            }
        )
        assert [e.is_on for e in entities] == [True, True, True]
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {"mac": "aa", "ep": 1, "cluster": 65535, "id": 0, "val": 32},
                ],
            }
        )
        assert [e.is_on for e in entities] == [False, False, False]
        api._handle_report(
            {
                "cmd": "report",
                "data": [
                    {"mac": "aa", "ep": 1, "cluster": 65535, "id": 1000, "val": 0},
                    {"mac": "aa", "ep": 1, "cluster": 65535, "id": 0, "val": 3},
                ],
            }
        )
        assert [e.is_on for e in entities] == [None, None, None]
        execute.assert_not_awaited()
        for entity in entities:
            await entity.async_remove()
    assert api._report_listeners == {}
