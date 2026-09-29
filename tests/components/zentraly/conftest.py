"""Fixtures for Zentraly tests."""

import asyncio
from collections.abc import AsyncIterator, Callable, Generator
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import pytest
from zentraly import (
    ClimateOperationMode,
    DeviceModel,
    ZentralyDeviceInfo,
    get_device_commands,
)
from zentraly.connection import ZentralyConnection

from homeassistant.components.zentraly.const import DOMAIN
from homeassistant.components.zentraly.models import ZentralyDevice
from homeassistant.const import (
    CONF_DEVICE_ID,
    CONF_HOST,
    CONF_MAC,
    CONF_PASSWORD,
    CONF_PORT,
    Platform,
)
from homeassistant.core import HomeAssistant

from tests.common import MockConfigEntry


@pytest.fixture
async def transport() -> AsyncIterator[tuple[ZentralyConnection, MagicMock]]:
    """Open transport loops with a controlled WebSocket."""
    incoming: asyncio.Queue[aiohttp.WSMessage] = asyncio.Queue()

    async def receive() -> AsyncIterator[aiohttp.WSMessage]:
        while True:
            yield await incoming.get()

    websocket = MagicMock(spec=aiohttp.ClientWebSocketResponse)
    websocket.closed = False
    websocket.send_json = AsyncMock()
    websocket.close = AsyncMock()
    websocket.__aiter__.side_effect = receive
    session = MagicMock(spec=aiohttp.ClientSession)
    session.ws_connect = AsyncMock(return_value=websocket)
    session.close = AsyncMock()
    connection = ZentralyConnection("192.168.1.42", 80, session=session)
    await connection.async_connect()
    try:
        yield connection, websocket
    finally:
        await connection.async_disconnect()


@pytest.fixture
def platform_device() -> MagicMock:
    """Return a connected device for isolated platform behavior tests."""
    device = MagicMock(spec=ZentralyDevice)
    device.device_id = "ZTTIN0100000631"
    device.connected = True
    device.capability_enabled.return_value = True
    device.opentherm_connected = True
    return device


@pytest.fixture
def mock_setup_entry() -> Generator[AsyncMock]:
    """Mock setting up a Zentraly config entry."""

    with patch(
        "homeassistant.components.zentraly.async_setup_entry",
        return_value=True,
    ) as mock_setup:
        yield mock_setup


DEVICE_ID = "ZTTIN0100000631"
MAC = "dcda0c58c8d8"
HOST = "192.168.1.42"
PORT = 12345
PASSWORD = "test-password"
ENTITY_ID = "climate.zttin0100000631"


@pytest.fixture
def mock_config_entry(hass: HomeAssistant) -> MockConfigEntry:
    """Return a registered thermostat config entry."""
    entry = MockConfigEntry(
        domain=DOMAIN,
        unique_id=DEVICE_ID,
        data={
            CONF_HOST: HOST,
            CONF_PORT: PORT,
            CONF_DEVICE_ID: DEVICE_ID,
            CONF_PASSWORD: PASSWORD,
            CONF_MAC: MAC,
        },
    )
    entry.add_to_hass(hass)
    return entry


@pytest.fixture
def mock_api_class() -> Generator[MagicMock]:
    """Mock the library client where the integration uses it."""
    with (
        patch(
            "homeassistant.components.zentraly.ZentralyApi", autospec=True
        ) as api_class,
        patch(
            "homeassistant.components.zentraly.config_flow.ZentralyApi", new=api_class
        ),
    ):
        yield api_class


@pytest.fixture
def mock_api(mock_api_class: MagicMock) -> MagicMock:
    """Return a connected library client."""
    api = mock_api_class.return_value
    api.device_id = DEVICE_ID
    api.host = HOST
    api.port = PORT
    api.connected = True
    api.async_validate_password.return_value = MAC
    return api


@pytest.fixture
def mock_climate_api() -> Generator[MagicMock]:
    """Return the public climate API with representative thermostat readings."""
    with patch(
        "homeassistant.components.zentraly.climate.ZentralyClimateApi", autospec=True
    ) as api_class:
        api = api_class.return_value
        commands = get_device_commands(DeviceModel.ZTTIN)
        api.configuration = commands.climate_configuration
        api.supports.side_effect = commands.capabilities.__contains__
        api.async_get_humidity.return_value = 45.0
        api.async_get_current_temperature.return_value = 19.0
        api.async_get_target_temperature.return_value = 21.0
        api.async_get_operation_mode.return_value = ClimateOperationMode.MANUAL
        api.async_get_heat_demand.return_value = False
        api.async_set_target_temperature.return_value = True
        api.async_set_operation_mode.return_value = True
        yield api


@pytest.fixture
def mock_device_info() -> Generator[AsyncMock]:
    """Mock the inherited public library metadata method."""
    with patch(
        "homeassistant.components.zentraly.models.ZentralyDevice.async_get_device_info",
        return_value=ZentralyDeviceInfo("1.0", "2.0"),
    ) as read_info:
        yield read_info


@pytest.fixture
def connection_state(mock_api: MagicMock) -> Callable[[bool], None]:
    """Deliver a library connection event to all registered listeners."""

    def notify(connected: bool) -> None:
        mock_api.connected = connected
        for listener in mock_api.add_connection_state_listener.call_args_list:
            listener.args[0](connected)

    return notify


@pytest.fixture
async def setup_integration(
    hass: HomeAssistant,
    mock_config_entry: MockConfigEntry,
    mock_api: MagicMock,
    mock_climate_api: MagicMock,
    mock_device_info: AsyncMock,
) -> None:
    """Load the integration and its real climate platform through Home Assistant."""
    with patch(
        "homeassistant.components.zentraly.get_device_platforms",
        return_value=frozenset({Platform.CLIMATE}),
    ):
        assert await hass.config_entries.async_setup(mock_config_entry.entry_id)
    await hass.async_block_till_done()
