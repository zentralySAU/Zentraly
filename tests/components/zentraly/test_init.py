"""Tests for the Zentraly integration setup."""

import asyncio
from datetime import timedelta
from unittest.mock import AsyncMock, MagicMock, patch

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
from homeassistant.components.zentraly.platforms import get_device_platforms
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
from homeassistant.helpers import device_registry as dr, entity_registry as er
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


async def test_reconnect_starts_reauth(hass: HomeAssistant) -> None:
    """A loaded entry requests new credentials after a reconnect rejection."""
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

    with patch.object(
        entry.runtime_data.api,
        "_async_connect_once",
        side_effect=ZentralyAuthenticationError,
    ):
        await entry.runtime_data.api._async_connection_loop()
        await hass.async_block_till_done()

    flows = hass.config_entries.flow.async_progress_by_handler(DOMAIN)
    assert len(flows) == 1
    assert flows[0]["context"]["source"] == "reauth"
    assert flows[0]["context"]["entry_id"] == entry.entry_id
    assert flows[0]["step_id"] == "auth"
    assert entry.data[CONF_PASSWORD] == PASSWORD


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


async def test_setup_parent_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Test setting up a Zentraly parent device."""

    entry = _parent_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            new_callable=AsyncMock,
            return_value=PARENT_MAC,
        ),
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_connect",
            new_callable=AsyncMock,
        ) as mock_connect,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as mock_forward,
    ):
        result = await hass.config_entries.async_setup(
            entry.entry_id,
        )

    assert result is True
    assert entry.state is ConfigEntryState.LOADED

    runtime_device = entry.runtime_data.device

    assert entry.runtime_data.api is runtime_device.api

    assert runtime_device.device_id == PARENT_DEVICE_ID
    assert runtime_device.mac == PARENT_MAC
    assert runtime_device.device_model is DeviceModel.ZTTIN

    assert get_device_platforms(PARENT_DEVICE_ID) == frozenset(
        {
            Platform.BUTTON,
            Platform.CLIMATE,
            Platform.NUMBER,
            Platform.SELECT,
            Platform.SWITCH,
        }
    )

    assert entry.runtime_data.children == {}

    parent_device = device_registry.async_get_device_by_identifier(
        (
            DOMAIN,
            PARENT_DEVICE_ID,
        ),
        entry.entry_id,
    )

    assert parent_device is not None
    assert parent_device.manufacturer == "Zentraly"
    assert parent_device.model == "Termostato Inalámbrico Wi-Fi"
    assert parent_device.model_id == "ZTTIN"
    assert parent_device.name == PARENT_DEVICE_ID
    assert parent_device.serial_number == PARENT_DEVICE_ID
    assert (
        dr.CONNECTION_NETWORK_MAC,
        dr.format_mac(PARENT_MAC),
    ) in parent_device.connections

    mock_connect.assert_awaited_once()

    mock_forward.assert_awaited_once_with(
        entry,
        [
            Platform.BUTTON,
            Platform.CLIMATE,
            Platform.NUMBER,
            Platform.SELECT,
            Platform.SWITCH,
        ],
    )


async def test_setup_parent_with_child(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Update existing parent and child metadata without changing user identity."""

    entry = _parent_entry(
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEVICE,
                "title": CHILD_DEVICE_ID,
                "unique_id": CHILD_DEVICE_ID,
                "data": {
                    CONF_DEVICE_ID: CHILD_DEVICE_ID,
                    CONF_MAC: CHILD_MAC,
                },
            }
        ]
    )
    entry.add_to_hass(hass)
    previous_parent = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, PARENT_DEVICE_ID)},
        name=PARENT_DEVICE_ID,
        model="zttin",
        sw_version="1.2.3",
        hw_version="2",
    )
    previous_child = device_registry.async_get_or_create(
        config_entry_id=entry.entry_id,
        config_subentry_id=next(iter(entry.subentries)),
        identifiers={(DOMAIN, CHILD_DEVICE_ID)},
        name=CHILD_DEVICE_ID,
        model="ztbin",
        via_device_id=previous_parent.id,
    )
    device_registry.async_update_device(previous_parent.id, name_by_user="Living room")
    device_registry.async_update_device(previous_child.id, name_by_user="Boiler room")
    previous_entity = entity_registry.async_get_or_create(
        "switch",
        DOMAIN,
        f"{CHILD_DEVICE_ID}_forced_mode",
        config_entry=entry,
        config_subentry_id=next(iter(entry.subentries)),
        device_id=previous_child.id,
        suggested_object_id="my_boiler",
    )

    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            new_callable=AsyncMock,
            return_value=PARENT_MAC,
        ),
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_connect",
            new_callable=AsyncMock,
        ) as mock_connect,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as mock_forward,
    ):
        result = await hass.config_entries.async_setup(
            entry.entry_id,
        )

    assert result is True
    assert entry.state is ConfigEntryState.LOADED

    runtime_data = entry.runtime_data

    assert len(runtime_data.children) == 1

    child = next(iter(runtime_data.children.values()))

    assert child.device_id == CHILD_DEVICE_ID
    assert child.mac == CHILD_MAC
    assert child.device_model is DeviceModel.ZTBIN

    assert get_device_platforms(CHILD_DEVICE_ID) == frozenset(
        {
            Platform.BINARY_SENSOR,
            Platform.BUTTON,
            Platform.SENSOR,
            Platform.SWITCH,
        }
    )

    # Parent and child must use exactly the same gateway API.
    assert runtime_data.device.api is runtime_data.api
    assert child.api is runtime_data.api

    parent_device = device_registry.async_get_device_by_identifier(
        (
            DOMAIN,
            PARENT_DEVICE_ID,
        ),
        entry.entry_id,
    )

    child_device = device_registry.async_get_device_by_identifier(
        (
            DOMAIN,
            CHILD_DEVICE_ID,
        ),
        entry.entry_id,
    )

    assert parent_device is not None
    assert child_device is not None

    assert parent_device.manufacturer == "Zentraly"
    assert parent_device.model == "Termostato Inalámbrico Wi-Fi"
    assert parent_device.model_id == "ZTTIN"
    assert parent_device.name == PARENT_DEVICE_ID
    assert parent_device.serial_number == PARENT_DEVICE_ID
    assert (
        dr.CONNECTION_NETWORK_MAC,
        dr.format_mac(PARENT_MAC),
    ) in parent_device.connections

    assert child_device.manufacturer == "Zentraly"
    assert child_device.model == "Boiler Inalámbrico"
    assert child_device.model_id == "ZTBIN"
    assert child_device.name == CHILD_DEVICE_ID
    assert child_device.serial_number == CHILD_DEVICE_ID
    assert (
        dr.CONNECTION_NETWORK_MAC,
        dr.format_mac(CHILD_MAC),
    ) in child_device.connections

    assert parent_device.id == previous_parent.id
    assert child_device.id == previous_child.id
    assert parent_device.name_by_user == "Living room"
    assert child_device.name_by_user == "Boiler room"
    assert parent_device.sw_version == "1.2.3"
    assert parent_device.hw_version == "2"
    assert len(dr.async_entries_for_config_entry(device_registry, entry.entry_id)) == 2
    assert entity_registry.async_get(previous_entity.entity_id) == previous_entity
    assert child_device.via_device_id == parent_device.id

    mock_connect.assert_awaited_once()
    mock_forward.assert_awaited_once()

    forward_entry, forward_platforms = mock_forward.await_args.args

    assert forward_entry is entry
    assert set(forward_platforms) == {
        Platform.BUTTON,
        Platform.CLIMATE,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.BINARY_SENSOR,
        Platform.SENSOR,
        Platform.SWITCH,
    }


async def test_setup_zteim_device(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
) -> None:
    """Test setting up an independent ZTEIM device."""

    entry = _zteim_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            new_callable=AsyncMock,
            return_value=ZTEIM_MAC,
        ),
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_connect",
            new_callable=AsyncMock,
        ) as mock_connect,
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ) as mock_forward,
    ):
        result = await hass.config_entries.async_setup(
            entry.entry_id,
        )

    assert result is True
    assert entry.state is ConfigEntryState.LOADED

    runtime_device = entry.runtime_data.device

    assert entry.runtime_data.api is runtime_device.api
    assert runtime_device.device_id == ZTEIM_DEVICE_ID
    assert runtime_device.mac == ZTEIM_MAC
    assert runtime_device.device_model is DeviceModel.ZTEIM
    assert entry.runtime_data.children == {}

    assert get_device_platforms(ZTEIM_DEVICE_ID) == frozenset(
        {
            Platform.BUTTON,
            Platform.NUMBER,
            Platform.SELECT,
            Platform.SENSOR,
            Platform.SWITCH,
        }
    )

    registry_device = device_registry.async_get_device_by_identifier(
        (
            DOMAIN,
            ZTEIM_DEVICE_ID,
        ),
        entry.entry_id,
    )

    assert registry_device is not None
    assert registry_device.manufacturer == "Zentraly"
    assert registry_device.model == "Enchufe zentraly mini"
    assert registry_device.model_id == "ZTEIM"
    assert registry_device.name == ZTEIM_DEVICE_ID
    assert registry_device.serial_number == ZTEIM_DEVICE_ID
    assert (
        dr.CONNECTION_NETWORK_MAC,
        dr.format_mac(ZTEIM_MAC),
    ) in registry_device.connections

    mock_connect.assert_awaited_once()
    mock_forward.assert_awaited_once_with(
        entry,
        [
            Platform.BUTTON,
            Platform.NUMBER,
            Platform.SELECT,
            Platform.SENSOR,
            Platform.SWITCH,
        ],
    )


async def test_setup_unexpected_mac(
    hass: HomeAssistant,
) -> None:
    """Test setup retries when the discovered MAC does not match."""

    entry = _parent_entry()
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            new_callable=AsyncMock,
            return_value="001122334455",
        ),
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_connect",
            new_callable=AsyncMock,
        ) as mock_connect,
    ):
        result = await hass.config_entries.async_setup(
            entry.entry_id,
        )

    assert result is False
    assert entry.state is ConfigEntryState.SETUP_RETRY

    mock_connect.assert_not_awaited()


async def test_unload_parent_with_child(
    hass: HomeAssistant,
) -> None:
    """Test unloading a parent also unloads child platforms and connection."""

    entry = _parent_entry(
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEVICE,
                "title": CHILD_DEVICE_ID,
                "unique_id": CHILD_DEVICE_ID,
                "data": {
                    CONF_DEVICE_ID: CHILD_DEVICE_ID,
                    CONF_MAC: CHILD_MAC,
                },
            }
        ]
    )
    entry.add_to_hass(hass)

    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_disconnect"
        ) as mock_disconnect,
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            new_callable=AsyncMock,
            return_value=PARENT_MAC,
        ),
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_connect",
            new_callable=AsyncMock,
        ),
        patch.object(
            hass.config_entries,
            "async_forward_entry_setups",
            new_callable=AsyncMock,
        ),
    ):
        setup_result = await hass.config_entries.async_setup(
            entry.entry_id,
        )

    assert setup_result is True
    assert entry.state is ConfigEntryState.LOADED

    with (
        patch.object(
            hass.config_entries,
            "async_unload_platforms",
            new_callable=AsyncMock,
            return_value=True,
        ) as mock_unload_platforms,
    ):
        result = await hass.config_entries.async_unload(
            entry.entry_id,
        )

    assert result is True
    assert entry.state is ConfigEntryState.NOT_LOADED

    mock_unload_platforms.assert_awaited_once()

    unload_entry, unload_platforms = mock_unload_platforms.await_args.args

    assert unload_entry is entry
    assert set(unload_platforms) == {
        Platform.BUTTON,
        Platform.CLIMATE,
        Platform.NUMBER,
        Platform.SELECT,
        Platform.BINARY_SENSOR,
        Platform.SENSOR,
        Platform.SWITCH,
    }

    mock_disconnect.assert_awaited_once()


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


async def test_device_info_periodic_refresh(
    hass: HomeAssistant, device_registry: dr.DeviceRegistry
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
        read_info.assert_awaited_once_with()
        registered = device_registry.async_get_device_by_identifier(
            (DOMAIN, PARENT_DEVICE_ID), entry.entry_id
        )
        assert registered.sw_version == "1.0"
        assert registered.hw_version == "2.0"
        assert await hass.config_entries.async_unload(entry.entry_id)
        async_fire_time_changed(hass, now + timedelta(hours=48))
        await hass.async_block_till_done()
        read_info.assert_awaited_once_with()


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
async def test_climate_periodic_refresh_lifecycle(
    hass: HomeAssistant, initial_read: float | ZentralyConnectionError
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
        climate_api.async_get_current_temperature.assert_awaited_once_with()
        assert await hass.config_entries.async_unload(entry.entry_id)
        async_fire_time_changed(hass, now + timedelta(minutes=10))
        await hass.async_block_till_done()
        climate_api.async_get_current_temperature.assert_awaited_once_with()


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


@pytest.mark.usefixtures("mock_device_info")
async def test_remove_gateway_removes_children(
    hass: HomeAssistant,
    device_registry: dr.DeviceRegistry,
    entity_registry: er.EntityRegistry,
) -> None:
    """Removing a gateway cleans up every child device and its entities."""
    entry = _parent_entry(
        subentries_data=[
            {
                "subentry_type": SUBENTRY_TYPE_DEVICE,
                "title": device_id,
                "unique_id": device_id,
                "data": {CONF_DEVICE_ID: device_id, CONF_MAC: mac},
            }
            for device_id, mac in (
                ("ZTTZB0100000001", "4831b7fffec60785"),
                ("ZTTZB0100000002", "4831b7fffec60786"),
            )
        ]
    )
    entry.add_to_hass(hass)
    hass.config_entries.async_update_entry(
        entry, data={**entry.data, CONF_DEVICE_ID: "ZTHG20100000001"}
    )
    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi.async_validate_password",
            return_value=PARENT_MAC,
        ),
        patch("homeassistant.components.zentraly.ZentralyApi.async_connect"),
        patch.object(hass.config_entries, "async_forward_entry_setups"),
    ):
        assert await hass.config_entries.async_setup(entry.entry_id)
    await hass.async_block_till_done()
    devices = dr.async_entries_for_config_entry(device_registry, entry.entry_id)
    assert len(devices) == 3
    entities = [
        entity_registry.async_get_or_create(
            "sensor",
            DOMAIN,
            f"{child.device_id}_battery_level",
            config_entry=entry,
            config_subentry_id=subentry_id,
            device_id=device_registry.async_get_device_by_identifier(
                (DOMAIN, child.device_id), entry.entry_id
            ).id,
        )
        for subentry_id, child in entry.runtime_data.children.items()
    ]
    with patch.object(hass.config_entries, "async_unload_platforms", return_value=True):
        result = await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert result == {"require_restart": False}
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert all(device_registry.async_get(device.id) is None for device in devices)
    assert all(
        entity_registry.async_get(entity.entity_id) is None for entity in entities
    )
