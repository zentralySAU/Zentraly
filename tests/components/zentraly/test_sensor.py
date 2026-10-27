"""Tests for Zentraly sensor values and units."""

from collections.abc import AsyncIterator, Callable
from datetime import timedelta
import logging
from types import MappingProxyType
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from zentraly import (
    SensorCapability,
    ZentralyApi,
    ZentralyBinarySensorApi,
    ZentralyOutputType,
    ZentralySensorApi,
)

from homeassistant.components.zentraly import create_device
from homeassistant.components.zentraly.const import DOMAIN
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.components.zentraly.sensor import (
    ZentralySensor,
    _create_sensor_entities,
)
from homeassistant.config_entries import ConfigSubentry
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_MAC,
    EntityCategory,
    Platform,
    UnitOfVolumeFlowRate,
)
from homeassistant.core import HomeAssistant
from homeassistant.helpers import entity_registry as er
from homeassistant.helpers.entity import Entity
from homeassistant.helpers.entity_component import EntityComponent
from homeassistant.util import dt as dt_util

from .helpers import (
    StateCase,
    async_check_opentherm_loss_during_read,
    async_check_output_change_published_immediately,
    async_check_partial_report_updates_entity,
    async_check_periodic_read_policy,
    async_check_platform_device_availability,
    async_check_platform_refresh_lifecycle,
    async_check_report_during_read,
)

from tests.common import MockConfigEntry, async_fire_time_changed


@pytest.mark.parametrize("model", ["ZTBIN", "ZTTWZ"])
@pytest.mark.parametrize(
    ("capability", "attribute_id", "expected"),
    [
        pytest.param(SensorCapability.CH_SETPOINT, 1, 20.5, id="ch-setpoint"),
        pytest.param(SensorCapability.DHW_SETPOINT, 56, 20.5, id="dhw-setpoint"),
        pytest.param(SensorCapability.MODULATION_LEVEL, 17, 2050, id="modulation"),
        pytest.param(SensorCapability.CH_WATER_PRESSURE, 18, 2050, id="pressure"),
        pytest.param(SensorCapability.DHW_FLOW_RATE, 19, 2050, id="flow"),
        pytest.param(SensorCapability.FEED_TEMPERATURE, 25, 2050, id="feed"),
        pytest.param(SensorCapability.DHW_TEMPERATURE, 26, 2050, id="dhw"),
    ],
)
async def test_opentherm_measurement_scale(
    model: str,
    capability: SensorCapability,
    attribute_id: int,
    expected: float,
) -> None:
    """Convert setpoints to Celsius while preserving unscaled measurements."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTTWZ0100000001")
    api._set_connected(True)
    device = create_device(api, f"{model}0100000001", "bb")
    device.set_output_type(ZentralyOutputType.OPENTHERM)
    entity = ZentralySensor(
        device=device,
        sensor_api=ZentralySensorApi(device),
        capability=capability,
    )

    with patch.object(
        api,
        "async_execute_command",
        return_value=(
            1,
            {
                "cmd": "readAttr",
                "rid": 1,
                "status": 200,
                "attrs": [{"id": attribute_id, "val": 2050}],
            },
        ),
    ):
        await entity.async_update()

    assert entity.native_value == expected


async def test_flow_rate(platform_device: MagicMock) -> None:
    """Expose the measured flow using Home Assistant's standard unit."""
    api = MagicMock(spec=ZentralySensorApi)
    api.async_get_dhw_flow_rate.return_value = 7.5
    entity = ZentralySensor(
        device=platform_device,
        sensor_api=api,
        capability=SensorCapability.DHW_FLOW_RATE,
    )

    await entity.async_update()

    assert entity.native_value == 7.5
    assert entity.native_unit_of_measurement == UnitOfVolumeFlowRate.LITERS_PER_MINUTE
    assert entity.unique_id == "ZTTIN0100000631_dhw_flow_rate"
    api.async_get_dhw_flow_rate.assert_awaited_once_with()


@pytest.mark.parametrize(
    ("connected", "opentherm"),
    [
        pytest.param(False, True, id="gateway-disconnected"),
        pytest.param(True, False, id="opentherm-disconnected"),
    ],
)
async def test_flow_unavailable(
    platform_device: MagicMock, connected: bool, opentherm: bool
) -> None:
    """Do not request flow when its connection is unavailable."""
    platform_device.connected = connected
    platform_device.opentherm_connected = opentherm
    api = MagicMock(spec=ZentralySensorApi)
    entity = ZentralySensor(
        device=platform_device,
        sensor_api=api,
        capability=SensorCapability.DHW_FLOW_RATE,
    )
    await entity.async_update()
    assert entity.native_value is None
    api.async_get_dhw_flow_rate.assert_not_awaited()


def test_flow_report(platform_device: MagicMock) -> None:
    """Accept a numeric report and preserve it after malformed input."""
    api = MagicMock(spec=ZentralySensorApi)
    entity = ZentralySensor(
        device=platform_device,
        sensor_api=api,
        capability=SensorCapability.DHW_FLOW_RATE,
    )
    with patch.object(entity, "async_write_ha_state") as publish:
        entity._handle_state_update({SensorCapability.DHW_FLOW_RATE: 8.0})
        entity._handle_state_update({SensorCapability.DHW_FLOW_RATE: "invalid"})
    assert entity.native_value == 8.0
    publish.assert_called_once_with()


async def test_opentherm_loss_clears_flow(platform_device: MagicMock) -> None:
    """An output change immediately clears the previous OpenTherm reading."""
    api = MagicMock(spec=ZentralySensorApi)
    api.async_get_dhw_flow_rate.return_value = 7.5
    entity = ZentralySensor(
        device=platform_device,
        sensor_api=api,
        capability=SensorCapability.DHW_FLOW_RATE,
    )
    await entity.async_update()
    assert entity.native_value == 7.5
    platform_device.opentherm_connected = False
    with patch.object(entity, "async_write_ha_state") as publish:
        entity._handle_device_state()
    assert entity.native_value is None
    assert entity.available
    publish.assert_called_once_with()


async def test_output_poll_recovers_unavailable_device() -> None:
    """Polling continues after a device timeout so it can recover without reports."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTTWZ0100000001")
    api._set_connected(True)
    device = create_device(api, "ZTBIN0100000001", "bb")
    entity = ZentralySensor(
        device=device,
        sensor_api=ZentralySensorApi(device),
        capability=SensorCapability.OUTPUT_TYPE,
    )
    with patch.object(api, "async_execute_command", return_value=None):
        await entity.async_update()
    assert not entity.available
    with patch.object(
        api,
        "async_execute_command",
        return_value=(
            1,
            {
                "cmd": "readAttr",
                "rid": 1,
                "status": 200,
                "attrs": [{"id": 1000, "val": 1}],
            },
        ),
    ):
        await entity.async_update()
    assert entity.available
    assert entity.native_value == "opentherm"
    with patch.object(
        api,
        "async_execute_command",
        return_value=(2, {"cmd": "readAttr", "rid": 2, "status": 200, "attrs": []}),
    ):
        await entity.async_update()
    assert entity.available
    assert entity.native_value is None
    assert device.output_type is ZentralyOutputType.OPENTHERM


async def test_rssi_is_report_only(hass: HomeAssistant) -> None:
    """Do not query RSSI on setup, reconnection or a periodic interval."""
    api = ZentralyApi("192.168.1.42", 80, "password", "ZTTWZ0100000001")
    api._set_connected(True)
    device = create_device(api, "ZTBIN0100000001", "bb")
    entity = ZentralySensor(
        device=device,
        sensor_api=ZentralySensorApi(device),
        capability=SensorCapability.RSSI,
    )
    component = EntityComponent(logging.getLogger(__name__), "sensor", hass)
    with patch.object(entity, "async_update") as refresh:
        await component.async_add_entities([entity])
        await hass.async_block_till_done()
        api._set_connected(False)
        api._set_connected(True)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
        await hass.async_block_till_done()
        refresh.assert_not_awaited()
        entity._handle_state_update({SensorCapability.RSSI: -65})
        assert entity.native_value == -65
        await entity.async_remove()


@pytest.mark.parametrize("model", ["ZTTWZ", "ZTMWZ", "ZTEIE"])
def test_child_sensor_capabilities(model: str) -> None:
    """Child topology omits only Wi-Fi, retaining every other sensor capability."""
    api = ZentralyApi("192.168.1.42", 80, "password", model + "0100000001")
    direct = create_device(api, api.device_id, "aabbccddee11")
    child = create_device(api, api.device_id, "aabbccddee11", via_device_id="gateway")
    direct_ids = {entity.unique_id for entity in _create_sensor_entities(direct)}
    child_ids = {entity.unique_id for entity in _create_sensor_entities(child)}
    assert direct_ids - child_ids == {f"{api.device_id}_wifi_signal_power"}
    assert child_ids < direct_ids


@pytest.fixture
async def wifi_topology(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_device_info: AsyncMock,
    entity_registry: er.EntityRegistry,
    request: pytest.FixtureRequest,
) -> AsyncIterator[tuple[str, MagicMock]]:
    """Load a dual device, including a registry entry created by older code."""
    device_id = "ZTEIE0100000001"
    entry = mock_config_entry
    subentry_id = None
    parent_id = device_id
    if request.param == "child":
        parent_id = "ZTHG20100000001"
        subentry = ConfigSubentry(
            data=MappingProxyType(
                {CONF_DEVICE_ID: device_id, CONF_MAC: "aabbccddee11"}
            ),
            subentry_type="device",
            title=device_id,
            unique_id=device_id,
        )
        hass.config_entries.async_add_subentry(entry, subentry)
        subentry_id = subentry.subentry_id
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_DEVICE_ID: parent_id}, unique_id=parent_id
    )
    mock_api.device_id = parent_id
    entity_registry.async_get_or_create(
        "sensor",
        DOMAIN,
        f"{device_id}_wifi_signal_power",
        config_entry=entry,
        config_subentry_id=subentry_id,
    )
    sensor_api = MagicMock(spec=ZentralySensorApi)
    sensor_api.supports.side_effect = {
        SensorCapability.WIFI_SIGNAL_POWER,
        SensorCapability.POWER,
    }.__contains__
    sensor_api.async_get_wifi_signal_power.return_value = -50
    sensor_api.async_get_power.return_value = 25
    gateway_api = MagicMock(spec=ZentralySensorApi)
    gateway_api.supports.return_value = False

    def sensor_factory(device: ZentralyDevice) -> MagicMock:
        return sensor_api if device.device_id == device_id else gateway_api

    with (
        patch(
            "homeassistant.components.zentraly.get_device_platforms",
            return_value=frozenset({Platform.SENSOR}),
        ),
        patch(
            "homeassistant.components.zentraly.sensor.ZentralySensorApi",
            side_effect=sensor_factory,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        yield device_id, sensor_api


@pytest.mark.parametrize(
    ("wifi_topology", "wifi_reads"),
    [pytest.param("direct", 1, id="direct"), pytest.param("child", 0, id="child")],
    indirect=["wifi_topology"],
)
async def test_wifi_reads_follow_topology(
    hass: HomeAssistant,
    wifi_topology: tuple[str, MagicMock],
    wifi_reads: int,
    entity_registry: er.EntityRegistry,
    connection_state: Callable[[bool], None],
) -> None:
    """Suppress child Wi-Fi reads and stale registry entries while retaining power."""
    device_id, sensor_api = wifi_topology
    wifi_entity = entity_registry.async_get_entity_id(
        "sensor", DOMAIN, f"{device_id}_wifi_signal_power"
    )
    assert bool(wifi_entity) == bool(wifi_reads)
    assert sensor_api.async_get_wifi_signal_power.await_count == wifi_reads
    sensor_api.async_get_power.assert_awaited_once()
    async_fire_time_changed(hass, dt_util.utcnow() + timedelta(minutes=5))
    await hass.async_block_till_done()
    assert sensor_api.async_get_wifi_signal_power.await_count == wifi_reads * 2
    assert sensor_api.async_get_power.await_count == 2
    connection_state(True)
    await hass.async_block_till_done()
    assert sensor_api.async_get_wifi_signal_power.await_count == wifi_reads * 3
    assert sensor_api.async_get_power.await_count == 3
    for listener in sensor_api.add_state_listener.call_args_list:
        listener.args[0](
            {SensorCapability.WIFI_SIGNAL_POWER: -40, SensorCapability.POWER: 60}
        )
    await hass.async_block_till_done()
    assert hass.states.get(f"sensor.{device_id.lower()}_power").state == "60"
    assert bool(
        entity_registry.async_get_entity_id(
            "sensor", DOMAIN, f"{device_id}_wifi_signal_power"
        )
    ) == bool(wifi_reads)


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
    [pytest.param("sensor", "ZTBIN", _create_sensor_entities, id="sensor")],
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
            "sensor",
            _create_sensor_entities,
            "error_id",
            "native_value",
            None,
            id="sensor-unknown",
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
    await async_check_output_change_published_immediately(
        hass, domain, factory, capability, attribute, expected
    )


@pytest.mark.parametrize(
    ("domain", "model", "factory"),
    [pytest.param("sensor", "ZTBIN", _create_sensor_entities, id="sensor")],
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
            "sensor",
            "ZTTWZ",
            _create_sensor_entities,
            "ch_setpoint",
            65535,
            1,
            2050,
            "native_value",
            20.5,
            id="measurement",
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
    "state_case",
    [
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
