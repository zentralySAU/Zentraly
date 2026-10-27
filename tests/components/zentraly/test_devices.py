"""Tests for Zentraly device setup."""

from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from pytest_homeassistant.common import MockConfigEntry
from zentraly import DeviceModel

from homeassistant.components.zentraly.const import DOMAIN
from homeassistant.components.zentraly.platforms import get_device_platforms
from homeassistant.config_entries import ConfigEntryState
from homeassistant.const import CONF_DEVICE_ID, CONF_MAC, EntityCategory, Platform
from homeassistant.core import HomeAssistant
from homeassistant.helpers import device_registry as dr, entity_registry as er

from .test_init import (
    CHILD_DEVICE_ID,
    CHILD_MAC,
    PARENT_DEVICE_ID,
    PARENT_MAC,
    SUBENTRY_TYPE_DEVICE,
    ZTEIM_DEVICE_ID,
    ZTEIM_MAC,
    _parent_entry,
    _zteim_entry,
)


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
    assert len(entries) == 3
    assert {entry.entity_category for entry in entries} == {
        EntityCategory.CONFIG,
        EntityCategory.DIAGNOSTIC,
    }
    assert hass.states.get(f"switch.{device_id.lower()}_always_on_led").state == "on"


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
    with patch.object(
        hass.config_entries, "async_unload_platforms_or_raise", return_value=None
    ):
        result = await hass.config_entries.async_remove(entry.entry_id)
    await hass.async_block_till_done()
    assert result == {"require_restart": False}
    assert hass.config_entries.async_get_entry(entry.entry_id) is None
    assert all(device_registry.async_get(device.id) is None for device in devices)
    assert all(
        entity_registry.async_get(entity.entity_id) is None for entity in entities
    )
