"""Base entity classes and structured DeviceInfo builders for the Intelbras DVR integration."""
from __future__ import annotations

from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_FEATURES, CONF_HTTP_PORT, DEFAULT_HTTP_PORT, DOMAIN
from .coordinator import IntelbrasCoordinator, IntelbrasEventCoordinator


def dvr_device_info(entry: ConfigEntry, coordinator: IntelbrasCoordinator) -> DeviceInfo:
    """Return structured DeviceInfo for the DVR parent device."""
    sys_info: dict[str, str] = (coordinator.data or {}).get("system_info", {})
    host = entry.data.get(CONF_HOST, "")
    http_port = entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
    config_url = (
        f"http://{host}"
        if http_port == DEFAULT_HTTP_PORT
        else f"http://{host}:{http_port}"
    )
    return DeviceInfo(
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Intelbras",
        model=sys_info.get("updateSerial") or sys_info.get("deviceType") or "DVR",
        serial_number=sys_info.get("serialNumber"),
        sw_version=(coordinator.data or {}).get("software_version"),
        configuration_url=config_url if host else None,
    )


def camera_device_info(
    entry: ConfigEntry,
    coordinator: IntelbrasCoordinator | IntelbrasEventCoordinator | None = None,
    channel: int = 0,
    channel_name: str = "",
) -> DeviceInfo:
    """Return structured DeviceInfo for a camera child device linked to the DVR."""
    main_coord: IntelbrasCoordinator | None = None
    if isinstance(coordinator, IntelbrasCoordinator):
        main_coord = coordinator
    elif isinstance(coordinator, IntelbrasEventCoordinator):
        main_coord = coordinator.main_coordinator

    remote_info: dict[str, Any] = {}
    if main_coord and main_coord.data:
        remote_info = (main_coord.data.get("remote_devices") or {}).get(channel, {})

    sig_format = "HDCVI"
    if main_coord and hasattr(main_coord, "client"):
        sig_format = main_coord.client.get_channel_signal_format(channel)
    elif coordinator and hasattr(coordinator, "client"):
        sig_format = coordinator.client.get_channel_signal_format(channel)

    manufacturer: str = remote_info.get("manufacturer") or "Intelbras"
    model: str = remote_info.get("model") or f"{sig_format} (CH {channel + 1})"
    sw_version: str | None = remote_info.get("software_version")
    serial_number: str | None = remote_info.get("serial_number")
    config_url: str | None = remote_info.get("configuration_url")

    host = entry.data.get(CONF_HOST, "")
    if not config_url and host:
        http_port = entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
        config_url = (
            f"http://{host}"
            if http_port == DEFAULT_HTTP_PORT
            else f"http://{host}:{http_port}"
        )

    runtime = getattr(entry, "runtime_data", None)
    dvr_dev_id: str | None = getattr(runtime, "dvr_device_id", None) if runtime else None

    info = DeviceInfo(
        identifiers={(DOMAIN, f"{entry.entry_id}_ch{channel}")},
        name=channel_name or f"Channel {channel + 1}",
        manufacturer=manufacturer,
        model=model,
        sw_version=sw_version,
        serial_number=serial_number,
        configuration_url=config_url,
    )
    if dvr_dev_id:
        info["via_device_id"] = dvr_dev_id
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

    @property
    def available(self) -> bool:
        """Return True when DVR data is loaded and consecutive failures are below threshold."""
        if (
            self.coordinator.data is not None
            and self.coordinator._consecutive_failures < 3
        ):
            return True
        return super().available


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
        self._attr_device_info = camera_device_info(
            entry=entry,
            coordinator=coordinator,
            channel=channel,
            channel_name=channel_name,
        )

    @property
    def available(self) -> bool:
        """Return True when DVR data is loaded and consecutive failures are below threshold."""
        if (
            self.coordinator.data is not None
            and self.coordinator._consecutive_failures < 3
        ):
            return True
        return super().available

    @property
    def _video_color(self) -> dict[str, int]:
        """Return the current picture settings for this channel."""
        colors: dict[int, dict[str, int]] = (self.coordinator.data or {}).get(
            "video_color", {}
        )
        return colors.get(self._channel, {})

    def _is_feature_enabled(self, feature: str) -> bool:
        """Return True if the given feature is enabled for this channel."""
        features_map: dict[str, list[str]] = self._entry.data.get(CONF_FEATURES, {})
        return feature in features_map.get(str(self._channel), [])
