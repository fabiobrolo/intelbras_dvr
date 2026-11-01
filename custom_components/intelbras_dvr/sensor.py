"""Sensor entities: DVR system diagnostics and per-channel IP addresses."""
from __future__ import annotations

from typing import Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorStateClass,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory, PERCENTAGE
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_CHANNELS
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity, DVREntity


# ---------------------------------------------------------------------------
# DVR-level sensors
# ---------------------------------------------------------------------------


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DVR diagnostic sensors and per-channel IP sensors."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }

    entities: list[SensorEntity] = [
        DVRStatusSensor(coordinator, entry),
        DVRDiskUsageSensor(coordinator, entry),
        DVRMonitorModeSensor(coordinator, entry),
        DVRResponseTimeSensor(coordinator, entry),
        DVRConsecutiveFailuresSensor(coordinator, entry),
    ]

    remote_devs = (coordinator.data or {}).get("remote_devices", {})
    for ch in channels:
        name = channel_names.get(ch, f"Channel {ch + 1}")
        if ch in remote_devs and remote_devs[ch].get("ip_address"):
            entities.append(CameraIPSensor(coordinator, entry, ch, name))

    async_add_entities(entities)


class DVRStatusSensor(DVREntity, SensorEntity):
    """Reports whether the DVR is currently reachable."""

    _attr_translation_key = "dvr_status"
    _attr_device_class = SensorDeviceClass.ENUM
    _attr_options = ["online", "offline"]
    _attr_icon = "mdi:cctv"

    def __init__(self, coordinator: IntelbrasCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str:
        return "online" if (self.coordinator.data or {}).get("online") else "offline"


class DVRDiskUsageSensor(DVREntity, SensorEntity):
    """Reports the percentage of disk capacity used on the DVR."""

    _attr_translation_key = "disk_usage"
    _attr_icon = "mdi:harddisk"
    _attr_native_unit_of_measurement = PERCENTAGE
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IntelbrasCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_disk_usage"

    @staticmethod
    def _parse_mb(value: Any) -> float | None:
        """Try to convert a raw storage value to a number (MB or Bytes)."""
        if value is None:
            return None
        try:
            return float(str(value).strip())
        except (ValueError, TypeError):
            return None

    @property
    def native_value(self) -> float | None:
        """
        Compute disk usage percentage from the DVR storage info.

        The Intelbras CGI returns a flat dict with keys like:
          table.StorageInfo[0].TotalCapacity  (MB)
          table.StorageInfo[0].UsedCapacity   (MB)
        This method scans all keys regardless of prefix/index.
        """
        storage: dict[str, Any] = (self.coordinator.data or {}).get("storage_info", {})
        if not storage:
            return None

        total_mb = 0.0
        used_mb = 0.0
        found = False

        for key, val in storage.items():
            key_lower = key.lower()
            parsed = self._parse_mb(val)
            if parsed is None:
                continue
            if "totalcapacity" in key_lower or "total_capacity" in key_lower or "totalbytes" in key_lower:
                total_mb += parsed
                found = True
            elif "usedcapacity" in key_lower or "used_capacity" in key_lower or "usedbytes" in key_lower:
                used_mb += parsed
                found = True

        if not found or total_mb <= 0:
            return None
        return round(used_mb / total_mb * 100, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        storage: dict[str, Any] = (self.coordinator.data or {}).get("storage_info", {})
        return {
            k: v
            for k, v in storage.items()
            if any(x in k.lower() for x in ("capacity", "status", "health"))
        }


class DVRMonitorModeSensor(DVREntity, SensorEntity):
    """Reports the current split mode of the physical monitor output."""

    _attr_translation_key = "monitor_mode"
    _attr_icon = "mdi:monitor"

    def __init__(self, coordinator: IntelbrasCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_monitor_mode"

    @property
    def native_value(self) -> str | None:
        mode_data: dict = (self.coordinator.data or {}).get("monitor_mode", {})
        mode = mode_data.get("mode", "")
        group = mode_data.get("group", 0)
        return f"{mode} (group {group})" if mode else None


# ---------------------------------------------------------------------------
# Per-channel sensors
# ---------------------------------------------------------------------------


class CameraIPSensor(CameraChannelEntity, SensorEntity):
    """Reports the IP address of an IP camera connected to the DVR (disabled by default)."""

    _attr_translation_key = "ip_address"
    _attr_icon = "mdi:ip-network"
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_enabled_default = False

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the IP address sensor."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_ip_address"

    @property
    def native_value(self) -> str | None:
        """Return the current IP address of the camera."""
        remote_devs = (self.coordinator.data or {}).get("remote_devices", {})
        info = remote_devs.get(self._channel, {})
        return info.get("ip_address")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return additional network attributes."""
        remote_devs = (self.coordinator.data or {}).get("remote_devices", {})
        info = remote_devs.get(self._channel, {})
        attrs: dict[str, Any] = {}
        if info.get("http_port"):
            attrs["http_port"] = info["http_port"]
        if info.get("protocol"):
            attrs["protocol"] = info["protocol"]
        if info.get("configuration_url"):
            attrs["configuration_url"] = info["configuration_url"]
        return attrs


class DVRResponseTimeSensor(DVREntity, SensorEntity):
    """Reports the response time of the DVR API in milliseconds."""

    _attr_translation_key = "response_time"
    _attr_icon = "mdi:timer-outline"
    _attr_native_unit_of_measurement = "ms"
    _attr_device_class = SensorDeviceClass.DURATION
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_visible_default = False

    def __init__(self, coordinator: IntelbrasCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_response_time"

    @property
    def native_value(self) -> float | None:
        return (self.coordinator.data or {}).get("response_time_ms")


class DVRConsecutiveFailuresSensor(DVREntity, SensorEntity):
    """Reports consecutive communication failures with the DVR."""

    _attr_translation_key = "consecutive_failures"
    _attr_icon = "mdi:alert-circle-outline"
    _attr_state_class = SensorStateClass.MEASUREMENT
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_visible_default = False

    def __init__(self, coordinator: IntelbrasCoordinator, entry: ConfigEntry) -> None:
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_consecutive_failures"

    @property
    def native_value(self) -> int:
        return (self.coordinator.data or {}).get("consecutive_failures", 0)

