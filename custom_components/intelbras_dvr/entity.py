"""Base entity classes for the Intelbras DVR integration."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_HTTP_PORT, DEFAULT_HTTP_PORT, DOMAIN
from .coordinator import IntelbrasCoordinator


def dvr_device_info(entry: ConfigEntry, coordinator: IntelbrasCoordinator) -> DeviceInfo:
    """Return DeviceInfo for the DVR parent device."""
    sys_info: dict[str, str] = (coordinator.data or {}).get("system_info", {})
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Intelbras",
        model=sys_info.get("updateSerial", "DVR"),
        serial_number=sys_info.get("serialNumber"),
        sw_version=(coordinator.data or {}).get("software_version"),
        configuration_url=(
            f"http://{entry.data['host']}"
            f":{entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)}"
        ),
    )


def camera_device_info(
    entry: ConfigEntry,
    coordinator: Any = None,
    channel: int = 0,
    channel_name: str = "",
    **kwargs: Any,
) -> DeviceInfo:
    """Return DeviceInfo for a camera child device linked to the DVR."""
    ch = kwargs.get("channel", channel)
    name = kwargs.get("channel_name", channel_name)
    
    # Retrieve remote device metadata if available
    remote_info = {}
    if coordinator and getattr(coordinator, "data", None):
        remote_info = (coordinator.data.get("remote_devices") or {}).get(ch, {})

    manufacturer = remote_info.get("manufacturer") or "Intelbras"
    model = remote_info.get("model") or f"HDCVI (CH {ch + 1})"
    sw_version = remote_info.get("software_version")
    serial_number = remote_info.get("serial_number")
    config_url = remote_info.get("configuration_url")
    if not config_url and entry.data.get("host"):
        # For analog cameras, point to DVR web interface
        http_p = entry.data.get("http_port", 80)
        config_url = f"http://{entry.data['host']}" if http_p == 80 else f"http://{entry.data['host']}:{http_p}"

    info_kwargs: dict[str, Any] = {
        "identifiers": {(DOMAIN, f"{entry.entry_id}_ch{ch}")},
        "name": name,
        "manufacturer": manufacturer,
        "model": model,
    }
    if sw_version:
        info_kwargs["sw_version"] = sw_version
    if serial_number:
        info_kwargs["serial_number"] = serial_number
    if config_url:
        info_kwargs["configuration_url"] = config_url

    info = DeviceInfo(**info_kwargs)
    if coordinator and hasattr(coordinator, "hass"):
        from homeassistant.helpers import device_registry as dr
        dev_reg = dr.async_get(coordinator.hass)
        if hasattr(dev_reg, "async_get_device_by_identifier"):
            parent = dev_reg.async_get_device_by_identifier((DOMAIN, entry.entry_id), entry.entry_id)
        else:
            parent = dev_reg.async_get_device(identifiers={(DOMAIN, entry.entry_id)})
        if parent:
            info["via_device_id"] = parent.id
            return info
    
    info["via_device"] = (DOMAIN, entry.entry_id)  # type: ignore[typeddict-item]
    return info


class DVREntity(CoordinatorEntity[IntelbrasCoordinator]):
    """Base entity bound to the DVR parent device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the DVR entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_device_info = dvr_device_info(entry, coordinator)


class CameraChannelEntity(CoordinatorEntity[IntelbrasCoordinator]):
    """Base entity bound to a specific camera channel child device."""

    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the camera channel entity."""
        super().__init__(coordinator)
        self._entry = entry
        self._channel = channel
        self._channel_name = channel_name
        self._attr_device_info = camera_device_info(entry, coordinator, channel, channel_name)

    @property
    def _event_data(self) -> dict[str, bool]:
        """Return the current alarm/event state for this channel."""
        events: dict = (self.coordinator.data or {}).get("event_status", {})
        return events.get(self._channel, {})

    @property
    def _video_color(self) -> dict[str, int]:
        """Return the current picture settings for this channel."""
        colors: dict = (self.coordinator.data or {}).get("video_color", {})
        return colors.get(self._channel, {})

    def _is_feature_enabled(self, feature: str) -> bool:
        """Return True if the given feature is enabled for this channel."""
        from .const import CONF_FEATURES

        features_map: dict[str, list[str]] = self._entry.data.get(CONF_FEATURES, {})
        return feature in features_map.get(str(self._channel), [])

