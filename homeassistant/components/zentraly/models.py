"""Home Assistant runtime and registry information for Zentraly."""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field

from zentraly import ZentralyApi, ZentralyDevice as LibraryDevice

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers import device_registry as dr
from homeassistant.helpers.device_registry import DeviceInfo

from .const import DOMAIN


@dataclass(slots=True)
class ZentralyDevice(LibraryDevice):
    """Adapt a library device to Home Assistant's device registry."""

    via_device_id: str | None = None

    @property
    def device_info(self) -> DeviceInfo:
        """Return Home Assistant device information."""

        device_info = DeviceInfo(
            identifiers={
                (
                    DOMAIN,
                    self.device_id,
                )
            },
            connections={
                (
                    dr.CONNECTION_NETWORK_MAC,
                    self.mac,
                )
            },
            manufacturer="Zentraly",
            model=self.model,
            model_id=self.device_model.name,
            name=self.device_id,
            serial_number=self.device_id,
            configuration_url=self.configuration_url,
        )

        if self.firmware_version is not None:
            device_info["sw_version"] = self.firmware_version

        if self.hardware_version is not None:
            device_info["hw_version"] = self.hardware_version

        if self.via_device_id is not None:
            device_info["via_device_id"] = self.via_device_id

        return device_info


@dataclass(slots=True)
class ZentralyData:
    """Runtime data for Zentraly."""

    api: ZentralyApi
    device: ZentralyDevice
    children: dict[str, ZentralyDevice] = field(default_factory=dict)

    device_info_refresh: dict[str, Callable[[], Awaitable[None]]] = field(
        default_factory=dict
    )

    def get_child(
        self,
        subentry_id: str,
    ) -> ZentralyDevice | None:
        """Return a child device by config subentry ID."""

        return self.children.get(subentry_id)


type ZentralyConfigEntry = ConfigEntry[ZentralyData]
