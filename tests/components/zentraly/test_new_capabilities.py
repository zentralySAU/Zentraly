"""Registration and service behavior for gateway and new configuration entities."""

from datetime import timedelta
from unittest.mock import MagicMock, patch

import pytest
from zentraly import NumberCapability, SensorCapability, SwitchCapability

from homeassistant.const import ATTR_ENTITY_ID, CONF_DEVICE_ID, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry, async_fire_time_changed


@pytest.mark.usefixtures("mock_device_info")
@pytest.mark.parametrize("model", ["ZTHZB", "ZTHG2", "ZTAAK"])
async def test_gateway_with_only_configuration_entities(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    entity_registry: er.EntityRegistry,
    model: str,
) -> None:
    """A gateway loads without climate or any primary entity."""
    device_id = model + "0100000001"
    hass.config_entries.async_update_entry(
        mock_config_entry,
        data={**mock_config_entry.data, CONF_DEVICE_ID: device_id},
        unique_id=device_id,
    )
    mock_api.device_id = device_id
    with patch(
        "homeassistant.components.zentraly.switch.ZentralySwitchApi.async_get_always_on_led",
        return_value=True,
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
    entries = er.async_entries_for_config_entry(
        entity_registry, mock_config_entry.entry_id
    )
    assert {entry.domain for entry in entries} == {"button", "switch"}
    assert len(entries) == 2
    assert all(entry.entity_category is EntityCategory.CONFIG for entry in entries)
    assert hass.states.get(f"switch.{device_id.lower()}_always_on_led").state == "on"


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


@pytest.mark.usefixtures("mock_device_info")
async def test_battery_sensor(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    entity_registry: er.EntityRegistry,
) -> None:
    """Battery is a diagnostic percent sensor and receives direct reports."""
    device_id = "ZTTZB0100000001"
    hass.config_entries.async_update_entry(
        mock_config_entry, data={**mock_config_entry.data, CONF_DEVICE_ID: device_id}
    )
    mock_api.device_id = device_id
    with (
        patch(
            "homeassistant.components.zentraly.get_device_platforms",
            return_value=frozenset({Platform.SENSOR}),
        ),
        patch(
            "homeassistant.components.zentraly.sensor.ZentralySensorApi", autospec=True
        ) as api_class,
    ):
        api = api_class.return_value
        api.supports.side_effect = {SensorCapability.BATTERY_LEVEL}.__contains__
        api.async_get_battery_level.return_value = 75
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
        await hass.async_block_till_done()
        entries = er.async_entries_for_config_entry(
            entity_registry, mock_config_entry.entry_id
        )
        assert len(entries) == 1
        entity = entries[0]
        assert entity.entity_category is EntityCategory.DIAGNOSTIC
        state = hass.states.get(entity.entity_id)
        assert state.state == "75"
        assert state.attributes["device_class"] == "battery"
        assert state.attributes["unit_of_measurement"] == "%"
        api.async_get_battery_level.assert_awaited_once()
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
        await hass.async_block_till_done()
        api.async_get_battery_level.assert_awaited_once()
        api.add_state_listener.call_args.args[0]({SensorCapability.BATTERY_LEVEL: 0})
        assert hass.states.get(entity.entity_id).state == "0"


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
        api.async_set_disconnect_on_error.return_value = True
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
