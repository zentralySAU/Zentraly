"""Tests for the Zentraly integration setup."""

import asyncio
from datetime import timedelta
from unittest.mock import MagicMock, PropertyMock, patch

import pytest
from zentraly import (
    ClimateOperationMode,
    DeviceModel,
    ZentralyApi,
    ZentralyAuthenticationError,
    ZentralyClimateApi,
    ZentralyConnectionError,
    ZentralyDeviceInfo,
    get_device_commands,
)
from zentraly.connection import ZentralyConnection

from homeassistant.components.zentraly import _async_refresh_device_info, create_device
from homeassistant.components.zentraly.const import DOMAIN
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
from homeassistant.exceptions import ConfigEntryError
from homeassistant.helpers import device_registry as dr
from homeassistant.util import dt as dt_util

from tests.common import MockConfigEntry, async_fire_time_changed

PARENT_DEVICE_ID = "ZTTIN0100000631"
CHILD_DEVICE_ID = "ZTBIN0100000021"
ZTEIM_DEVICE_ID = "ZTEIM0100000001"

HOST = "192.168.1.42"
PORT = 80

PARENT_MAC = "dcda0c58c8d8"
CHILD_MAC = "1020ba12316c"
ZTEIM_MAC = "aabbccddeeff"

PASSWORD = "test-password"


async def test_unload_clears_real_pending_requests(
    hass: HomeAssistant, transport: tuple[ZentralyConnection, MagicMock]
) -> None:
    """Unloading through Home Assistant closes transport and fails queued work."""
    connection, websocket = transport
    entry = _parent_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            return_value=PARENT_MAC,
        ),
        patch("homeassistant.components.zentraly.ZentralyApi.async_connect"),
        patch.object(hass.config_entries, "async_forward_entry_setups"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    with patch(
        "homeassistant.components.zentraly.models.ZentralyDevice.async_get_device_info",
        return_value=ZentralyDeviceInfo(),
    ):
        api = entry.runtime_data.api
        api._connection = connection
        api._set_connected(True)
        api._keepalive_task = asyncio.create_task(api._async_keepalive_loop())
        keepalive = api._keepalive_task
        tasks = [
            asyncio.create_task(
                connection.async_send_command(
                    lambda rid: {"cmd": "readAttr", "rid": rid, "mac": PARENT_MAC}
                )
            )
            for _ in range(25)
        ]
        for _ in range(6):
            await asyncio.sleep(0)
        assert len(connection._pending_requests) == 20
        assert connection._waiting == 5
        with patch.object(
            hass.config_entries, "async_unload_platforms", return_value=True
        ):
            assert await hass.config_entries.async_unload(entry.entry_id)
        assert await asyncio.gather(*tasks) == [(rid, None) for rid in range(1, 26)]
        assert not connection.connected
        assert connection._requests == {}
        assert connection._pending_requests == {}
        assert connection._waiting == 0
        assert connection._send_queue.empty()
        assert connection._sender_task is None
        assert connection._receiver_task is None
        assert api._keepalive_task is None
        assert keepalive.cancelled()
        assert not api._authentication_error_listeners
        websocket.close.assert_awaited_once()


SUBENTRY_TYPE_DEVICE = "device"


@pytest.mark.parametrize(
    ("error", "expected", "key"),
    [
        pytest.param(
            ZentralyAuthenticationError,
            ConfigEntryState.SETUP_ERROR,
            "authentication_failed",
            id="authentication",
        ),
        pytest.param(
            ZentralyConnectionError,
            ConfigEntryState.SETUP_RETRY,
            "setup_cannot_connect",
            id="connection",
        ),
    ],
)
async def test_translated_setup_error(
    hass: HomeAssistant,
    error: type[Exception],
    expected: ConfigEntryState,
    key: str,
) -> None:
    """Setup failures expose translated messages with the device identifier."""
    entry = _parent_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            side_effect=error,
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is expected
    assert entry.error_reason_translation_key == key
    assert entry.error_reason_translation_placeholders == {
        "device_id": PARENT_DEVICE_ID
    }


def test_translated_unsupported_model() -> None:
    """Unsupported models expose a translation key instead of hardcoded UI text."""
    api = ZentralyApi(HOST, PORT, PASSWORD, "UNKNOWN")
    with pytest.raises(ConfigEntryError) as exc:
        create_device(api, "UNKNOWN", PARENT_MAC)
    assert exc.value.translation_key == "unsupported_model"
    assert exc.value.translation_placeholders == {"device_id": "UNKNOWN"}


@pytest.mark.parametrize(
    ("data", "key"),
    [
        pytest.param({CONF_MAC: CHILD_MAC}, "missing_device_id", id="id"),
        pytest.param({CONF_DEVICE_ID: CHILD_DEVICE_ID}, "missing_mac", id="mac"),
    ],
)
async def test_translated_invalid_subentry(
    hass: HomeAssistant, data: dict[str, str], key: str
) -> None:
    """Invalid stored child data produces a localized initialization error."""
    entry = _parent_entry(
        subentries_data=[
            {
                "subentry_type": "device",
                "title": "Child",
                "unique_id": CHILD_DEVICE_ID,
                "data": data,
            }
        ]
    )
    entry.add_to_hass(hass)
    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            return_value=PARENT_MAC,
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.state is ConfigEntryState.SETUP_ERROR
    assert entry.error_reason_translation_key == key
    assert entry.error_reason_translation_placeholders == {
        "subentry_id": next(iter(entry.subentries))
    }


async def test_translated_no_platforms(hass: HomeAssistant) -> None:
    """A model without platforms reports a translated setup error."""
    entry = _parent_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            return_value=PARENT_MAC,
        ),
        patch(
            "homeassistant.components.zentraly.get_runtime_platforms", return_value=[]
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
    assert entry.error_reason_translation_key == "no_platforms"


def _parent_entry(
    *,
    subentries_data: list[dict] | None = None,
) -> MockConfigEntry:
    """Return a mock Zentraly config entry."""

    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=PARENT_DEVICE_ID,
        data={
            CONF_HOST: HOST,
            CONF_PORT: PORT,
            CONF_DEVICE_ID: PARENT_DEVICE_ID,
            CONF_PASSWORD: PASSWORD,
            CONF_MAC: PARENT_MAC,
        },
        subentries_data=subentries_data,
    )


def _zteim_entry() -> MockConfigEntry:
    """Return a mock ZTEIM config entry."""

    return MockConfigEntry(
        domain=DOMAIN,
        unique_id=ZTEIM_DEVICE_ID,
        data={
            CONF_HOST: HOST,
            CONF_PORT: PORT,
            CONF_DEVICE_ID: ZTEIM_DEVICE_ID,
            CONF_PASSWORD: PASSWORD,
            CONF_MAC: ZTEIM_MAC,
        },
    )


async def test_refresh_device_info(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Update registry versions via the public API and retain last known values."""
    entry = _parent_entry()
    entry.add_to_hass(hass)
    api = MagicMock(spec=ZentralyApi)
    api.async_execute_command.return_value = (1, {"status": 0})
    api.connected = True
    api.host = HOST
    api.port = PORT
    device = create_device(api, PARENT_DEVICE_ID, PARENT_MAC)
    registered = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id, **device.device_info
    )
    with patch.object(
        type(device),
        "async_get_device_info",
        side_effect=[
            ZentralyDeviceInfo("1.0", "2.0"),
            ZentralyDeviceInfo(hardware_version="2.1"),
            ZentralyDeviceInfo(),
        ],
    ) as read_info:
        await _async_refresh_device_info(device, device_registry, registered.id)
        assert device_registry.async_get(registered.id).sw_version == "1.0"
        assert device_registry.async_get(registered.id).hw_version == "2.0"
        await _async_refresh_device_info(device, device_registry, registered.id)
        assert device_registry.async_get(registered.id).sw_version == "1.0"
        assert device_registry.async_get(registered.id).hw_version == "2.1"
        with patch.object(device_registry, "async_update_device") as update:
            await _async_refresh_device_info(device, device_registry, registered.id)
            api.connected = False
            await _async_refresh_device_info(device, device_registry, registered.id)
        update.assert_not_called()
        assert read_info.await_count == 3


@pytest.mark.parametrize("periodic_polling", [True, False])
async def test_device_info_periodic_refresh(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry, periodic_polling: bool
) -> None:
    """Run the scheduled public version read and cancel it when unloading."""
    entry = _parent_entry()
    entry.add_to_hass(hass)
    api = MagicMock(spec=ZentralyApi)
    api.async_execute_command.return_value = (1, {"status": 0})
    api.connected = True
    api.device_id = PARENT_DEVICE_ID
    api.host = HOST
    api.port = PORT
    api.async_validate_password.return_value = PARENT_MAC
    with (
        patch(
            "homeassistant.components.zentraly.models.ZentralyDevice.supports_periodic_polling",
            new_callable=PropertyMock,
            return_value=periodic_polling,
        ),
        patch("homeassistant.components.zentraly.ZentralyApi", return_value=api),
        patch.object(hass.config_entries, "async_forward_entry_setups"),
        patch(
            "homeassistant.components.zentraly.models.ZentralyDevice.async_get_device_info",
            return_value=ZentralyDeviceInfo("1.0", "2.0"),
        ) as read_info,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        read_info.assert_awaited_once_with()
        read_info.reset_mock()
        now = dt_util.utcnow()
        async_fire_time_changed(hass, now + timedelta(minutes=5))
        await hass.async_block_till_done()
        read_info.assert_not_awaited()
        async_fire_time_changed(hass, now + timedelta(hours=24))
        await hass.async_block_till_done()
        assert read_info.await_count == int(periodic_polling)
        registered = device_registry.async_get_device_by_identifier(
            (DOMAIN, PARENT_DEVICE_ID), entry.entry_id
        )
        assert registered.sw_version == "1.0"
        assert registered.hw_version == "2.0"
        assert await hass.config_entries.async_unload(entry.entry_id)
        async_fire_time_changed(hass, now + timedelta(hours=48))
        await hass.async_block_till_done()
        assert read_info.await_count == int(periodic_polling)


@pytest.mark.parametrize(
    ("info", "firmware", "hardware"),
    [
        pytest.param(
            ZentralyDeviceInfo(firmware_version="1.1"), "1.1", "2.0", id="firmware-only"
        ),
        pytest.param(
            ZentralyDeviceInfo(hardware_version="2.1"), "1.0", "2.1", id="hardware-only"
        ),
        pytest.param(ZentralyDeviceInfo(), "1.0", "2.0", id="no-readings"),
        pytest.param(ZentralyConnectionError(), "1.0", "2.0", id="read-error"),
    ],
)
async def test_partial_device_info_after_restart(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    info: ZentralyDeviceInfo | ZentralyConnectionError,
    firmware: str,
    hardware: str,
) -> None:
    """A new runtime must retain persisted versions absent from its first reading."""
    entry = _parent_entry()
    entry.add_to_hass(hass)
    api = MagicMock(spec=ZentralyApi)
    api.async_execute_command.return_value = (1, {"status": 0})
    api.connected = True
    api.host = HOST
    api.port = PORT
    device = create_device(api, PARENT_DEVICE_ID, PARENT_MAC)
    registered = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        **device.device_info,
        sw_version="1.0",
        hw_version="2.0",
    )
    assert device.firmware_version is None
    assert device.hardware_version is None
    with patch.object(type(device), "async_get_device_info", side_effect=[info]):
        await _async_refresh_device_info(device, device_registry, registered.id)
    updated = device_registry.async_get(registered.id)
    assert updated.sw_version == firmware
    assert updated.hw_version == hardware


@pytest.mark.parametrize(
    "initial_read",
    [19.0, ZentralyConnectionError()],
    ids=["success", "connection-error"],
)
@pytest.mark.parametrize("periodic_polling", [True, False])
async def test_climate_periodic_refresh_lifecycle(
    hass: HomeAssistant,
    initial_read: float | ZentralyConnectionError,
    periodic_polling: bool,
) -> None:
    """Refresh climate every five minutes and cancel polling when unloading."""
    entry = _parent_entry()
    entry.add_to_hass(hass)
    api = MagicMock(spec=ZentralyApi)
    api.async_execute_command.return_value = (1, {"status": 0})
    api.device_id = PARENT_DEVICE_ID
    api.host = HOST
    api.port = PORT
    api.connected = True
    api.async_validate_password.return_value = PARENT_MAC
    climate_api = MagicMock(spec=ZentralyClimateApi)
    commands = get_device_commands(DeviceModel.ZTTIN)
    climate_api.configuration = commands.climate_configuration
    climate_api.supports.side_effect = lambda capability: (
        capability in commands.capabilities
    )
    climate_api.async_get_humidity.return_value = 45.0
    climate_api.async_get_current_temperature.side_effect = [initial_read, 19.0]
    climate_api.async_get_target_temperature.return_value = 21.0
    climate_api.async_get_operation_mode.return_value = ClimateOperationMode.MANUAL
    climate_api.async_get_heat_demand.return_value = False
    climate_api.async_set_target_temperature.return_value = True
    climate_api.async_set_operation_mode.return_value = True
    with (
        patch(
            "homeassistant.components.zentraly.models.ZentralyDevice.supports_periodic_polling",
            new_callable=PropertyMock,
            return_value=periodic_polling,
        ),
        patch(
            "homeassistant.components.zentraly.get_device_platforms",
            return_value=[Platform.CLIMATE],
        ),
        patch("homeassistant.components.zentraly.ZentralyApi", return_value=api),
        patch(
            "homeassistant.components.zentraly.climate.ZentralyClimateApi",
            return_value=climate_api,
        ),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.LOADED
        assert len(hass.states.async_all("climate")) == 1
        climate_api.async_get_current_temperature.reset_mock()
        now = dt_util.utcnow()
        async_fire_time_changed(hass, now + timedelta(minutes=4))
        await hass.async_block_till_done()
        climate_api.async_get_current_temperature.assert_not_awaited()
        async_fire_time_changed(hass, now + timedelta(minutes=5))
        await hass.async_block_till_done()
        assert climate_api.async_get_current_temperature.await_count == int(
            periodic_polling
        )
        assert await hass.config_entries.async_unload(entry.entry_id)
        async_fire_time_changed(hass, now + timedelta(minutes=10))
        await hass.async_block_till_done()
        assert climate_api.async_get_current_temperature.await_count == int(
            periodic_polling
        )


async def test_setup_failure_disconnects(hass: HomeAssistant) -> None:
    """Close the connection when platform setup fails after connecting."""
    entry = _parent_entry()
    entry.add_to_hass(hass)
    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            return_value=PARENT_MAC,
        ),
        patch("homeassistant.components.zentraly.ZentralyApi.async_connect"),
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_disconnect"
        ) as disconnect,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            side_effect=ConfigEntryError("Platform setup failed"),
        ),
    ):
        assert not await hass.config_entries.async_setup(entry.entry_id)
        await hass.async_block_till_done()
        assert entry.state is ConfigEntryState.SETUP_ERROR
        disconnect.assert_awaited_once_with()


async def test_device_info_connection_and_unload(hass: HomeAssistant) -> None:
    """Deduplicate reconnect reads and cancel pending metadata on unload."""
    entry = _parent_entry()
    entry.add_to_hass(hass)
    api = MagicMock(spec=ZentralyApi)
    api.connected = False
    api.device_id = PARENT_DEVICE_ID
    api.host = HOST
    api.port = PORT
    api.async_validate_password.return_value = PARENT_MAC
    started = asyncio.Event()
    cancelled = asyncio.Event()

    async def read() -> ZentralyDeviceInfo:
        started.set()
        try:
            await asyncio.Future()
        finally:
            cancelled.set()
        return ZentralyDeviceInfo()

    with (
        patch("homeassistant.components.zentraly.ZentralyApi", return_value=api),
        patch.object(hass.config_entries, "async_forward_entry_setups"),
        patch(
            "homeassistant.components.zentraly.models.ZentralyDevice.async_get_device_info",
            side_effect=read,
        ) as read_info,
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
        read_info.assert_not_awaited()
        listener = api.add_connection_state_listener.call_args.args[0]
        listener(False)
        read_info.assert_not_awaited()
        api.connected = True
        listener(True)
        await started.wait()
        listener(True)
        async_fire_time_changed(hass, dt_util.utcnow() + timedelta(hours=24))
        await hass.async_block_till_done()
        read_info.assert_awaited_once_with()
        assert await hass.config_entries.async_unload(entry.entry_id)
        assert cancelled.is_set()
        api.add_connection_state_listener.return_value.assert_called_once_with()
