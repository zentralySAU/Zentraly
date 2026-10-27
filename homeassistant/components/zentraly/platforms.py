"""Map device capabilities to Home Assistant platforms."""

from zentraly import (
    BinarySensorCapability,
    ButtonCapability,
    ClimateCapability,
    DeviceModel,
    NumberCapability,
    SelectCapability,
    SensorCapability,
    SwitchCapability,
    get_device_commands,
    get_device_model,
)

from homeassistant.const import Platform


def get_device_platforms(device_id: str) -> frozenset[Platform]:
    """Select platforms without duplicating the library's model catalog."""
    model = get_device_model(device_id)
    if model is DeviceModel.UNKNOWN:
        return frozenset()
    capabilities = get_device_commands(model).capabilities
    return frozenset({Platform.BUTTON}) | frozenset(
        platform
        for capability_type, platform in (
            (BinarySensorCapability, Platform.BINARY_SENSOR),
            (ButtonCapability, Platform.BUTTON),
            (ClimateCapability, Platform.CLIMATE),
            (NumberCapability, Platform.NUMBER),
            (SelectCapability, Platform.SELECT),
            (SensorCapability, Platform.SENSOR),
            (SwitchCapability, Platform.SWITCH),
        )
        if any(isinstance(capability, capability_type) for capability in capabilities)
    )
