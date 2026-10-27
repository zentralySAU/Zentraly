"""Tests for reusable configuration controls and their wire conversions."""

from collections.abc import AsyncIterator
import logging
from unittest.mock import patch

import pytest
from zentraly import ZentralyApi, ZentralyApiError, ZentralySwitchApi

from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.number import (
    ZentralyNumber,
    _create_number_entities,
)
from homeassistant.components.zentraly.select import (
    ZentralySelect,
    _create_select_entities,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_component import EntityComponent


@pytest.fixture(params=["ZTTWZ", "ZTTIN"])
async def display_controls(
    request: pytest.FixtureRequest,
    hass: HomeAssistant,
) -> AsyncIterator[tuple[ZentralyApi, ZentralyNumber, ZentralySelect]]:
    """Load dependent controls with no assumed enabling-switch state."""
    api = ZentralyApi("192.168.1.42", 80, "password", f"{request.param}0100000001")
    api._set_connected(True)
    device = create_device(api, api.device_id, "aa")
    numbers = {e.translation_key: e for e in _create_number_entities(device)}
    brightness = numbers["display_brightness"]
    away = numbers["away_temperature"]
    display = _create_select_entities(device)[0]
    brightness.entity_id = "number.display_brightness"
    display.entity_id = "select.display_mode"
    number_component = EntityComponent(logging.getLogger(__name__), "number", hass)
    select_component = EntityComponent(logging.getLogger(__name__), "select", hass)
    with patch("homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"):
        await number_component.async_add_entities([brightness])
    with patch("homeassistant.helpers.entity.Entity.async_schedule_update_ha_state"):
        await select_component.async_add_entities([display])
    assert not brightness.available
    assert not display.available
    assert away.available
    yield api, brightness, display
    await brightness.async_remove()
    await display.async_remove()
    assert api._report_listeners == {}


async def test_display_dependency_report(
    hass: HomeAssistant,
    display_controls: tuple[ZentralyApi, ZentralyNumber, ZentralySelect],
) -> None:
    """A report enables controls before applying their values, then disables them."""
    api, brightness, display = display_controls
    api._handle_report(
        {
            "cmd": "report",
            "data": [
                {"mac": "aa", "ep": 1, "cluster": 65513, "id": 100, "val": 30},
                {"mac": "aa", "ep": 1, "cluster": 65513, "id": 102, "val": 1},
                {"mac": "aa", "ep": 1, "cluster": 65513, "id": 101, "val": 1},
            ],
        }
    )
    assert brightness.available
    assert display.available
    assert hass.states.get(brightness.entity_id).state == "30.0"
    assert hass.states.get(display.entity_id).state == "temperature"
    api._handle_report(
        {
            "cmd": "report",
            "data": [
                {"mac": "aa", "ep": 1, "cluster": 65513, "id": 101, "val": 0},
            ],
        }
    )
    assert not brightness.available
    assert not display.available
    assert hass.states.get(brightness.entity_id).state == "unavailable"
    assert hass.states.get(display.entity_id).state == "unavailable"
    assert brightness.native_value == 30.0
    assert display.current_option == "temperature"


async def test_display_dependency_poll_and_write(
    display_controls: tuple[ZentralyApi, ZentralyNumber, ZentralySelect],
) -> None:
    """Reads and successful writes publish switch state; rejected writes do not."""
    api, brightness, display = display_controls
    switches = ZentralySwitchApi(brightness._device)
    with patch.object(
        api,
        "async_execute_command",
        return_value=(
            1,
            {
                "cmd": "readAttr",
                "rid": 1,
                "status": 200,
                "attrs": [{"id": 101, "val": 1}],
            },
        ),
    ):
        assert await switches.async_get_always_on_display() is True
    assert brightness.available
    assert display.available
    with patch.object(
        api,
        "async_execute_command",
        return_value=(
            1,
            {
                "cmd": "writeAttr",
                "rid": 1,
                "status": 200,
            },
        ),
    ):
        assert await switches.async_set_always_on_display(False)
        assert not brightness.available
        assert not display.available
        assert await switches.async_set_always_on_display(True)
        assert brightness.available
        assert display.available
    with (
        patch.object(
            api,
            "async_execute_command",
            return_value=(
                1,
                {
                    "cmd": "writeAttr",
                    "rid": 1,
                    "status": 500,
                },
            ),
        ),
        pytest.raises(ZentralyApiError),
    ):
        await switches.async_set_always_on_display(False)
    assert brightness.available
    assert display.available
