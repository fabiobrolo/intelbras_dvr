"""Sensor entities: DVR system diagnostics and per-channel IP addresses."""
from __future__ import annotations

from typing import TYPE_CHECKING, Any

from homeassistant.components.sensor import (
    SensorDeviceClass,
    SensorEntity,
    SensorEntityDescription,
    SensorStateClass,
)
from homeassistant.const import EntityCategory, PERCENTAGE, UnitOfTime
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

if TYPE_CHECKING:
    from . import IntelbrasDVRConfigEntry
from .const import (
    CONF_CHANNELS,
    CONF_MONITOR_NETWORK,
    CONF_MONITOR_STORAGE,
    CONF_MONITOR_USERS,
)
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity, DVREntity

DVR_STATUS_DESCRIPTION = SensorEntityDescription(
    key="dvr_status",
    translation_key="dvr_status",
    device_class=SensorDeviceClass.ENUM,
    options=["online", "offline"],
    icon="mdi:cctv",
)

DISK_USAGE_DESCRIPTION = SensorEntityDescription(
    key="disk_usage",
    translation_key="disk_usage",
    icon="mdi:harddisk",
    native_unit_of_measurement=PERCENTAGE,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
)

ACTIVE_USERS_DESCRIPTION = SensorEntityDescription(
    key="active_users",
    translation_key="active_users",
    icon="mdi:account-multiple",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
)

STORAGE_STATUS_DESCRIPTION = SensorEntityDescription(
    key="storage_status",
    translation_key="storage_status",
    icon="mdi:harddisk",
    entity_category=EntityCategory.DIAGNOSTIC,
)

NETWORK_SPEED_DESCRIPTION = SensorEntityDescription(
    key="network_speed",
    translation_key="network_speed",
    icon="mdi:ethernet",
    native_unit_of_measurement="Mbit/s",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
)

SYSTEM_TIME_DESCRIPTION = SensorEntityDescription(
    key="system_time",
    translation_key="system_time",
    icon="mdi:clock-outline",
    entity_category=EntityCategory.DIAGNOSTIC,
)

MONITOR_MODE_DESCRIPTION = SensorEntityDescription(
    key="monitor_mode",
    translation_key="monitor_mode",
    icon="mdi:monitor",
)

RESPONSE_TIME_DESCRIPTION = SensorEntityDescription(
    key="response_time",
    translation_key="response_time",
    icon="mdi:timer-outline",
    native_unit_of_measurement=UnitOfTime.MILLISECONDS,
    device_class=SensorDeviceClass.DURATION,
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_visible_default=False,
)

CONSECUTIVE_FAILURES_DESCRIPTION = SensorEntityDescription(
    key="consecutive_failures",
    translation_key="consecutive_failures",
    icon="mdi:alert-circle-outline",
    state_class=SensorStateClass.MEASUREMENT,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_visible_default=False,
)

CAMERA_IP_DESCRIPTION = SensorEntityDescription(
    key="ip_address",
    translation_key="ip_address",
    icon="mdi:ip-network",
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_enabled_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntelbrasDVRConfigEntry,
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
        DVRSystemTimeSensor(coordinator, entry),
    ]

    if entry.data.get(CONF_MONITOR_USERS, True):
        entities.append(DVRActiveUsersSensor(coordinator, entry))

    if entry.data.get(CONF_MONITOR_STORAGE, True):
        entities.append(DVRStorageStatusSensor(coordinator, entry))

    if entry.data.get(CONF_MONITOR_NETWORK, True):
        entities.append(DVRNetworkSpeedSensor(coordinator, entry))

    remote_devs = (coordinator.data or {}).get("remote_devices", {})
    for ch in channels:
        name = channel_names.get(ch, f"Channel {ch + 1}")
        if ch in remote_devs and remote_devs[ch].get("ip_address"):
            entities.append(CameraIPSensor(coordinator, entry, ch, name))

    async_add_entities(entities)


class DVRStatusSensor(DVREntity, SensorEntity):
    """Reports whether the DVR is currently reachable."""

    entity_description: SensorEntityDescription = DVR_STATUS_DESCRIPTION

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the DVR status sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_status"

    @property
    def native_value(self) -> str:
        """Return online or offline."""
        return "online" if (self.coordinator.data or {}).get("online") else "offline"


class DVRDiskUsageSensor(DVREntity, SensorEntity):
    """Reports the percentage of disk capacity used on the DVR."""

    entity_description: SensorEntityDescription = DISK_USAGE_DESCRIPTION

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the disk usage sensor."""
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
        """Compute disk usage percentage from the DVR storage info."""
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
            if (
                "totalcapacity" in key_lower
                or "total_capacity" in key_lower
                or "totalbytes" in key_lower
            ):
                total_mb += parsed
                found = True
            elif (
                "usedcapacity" in key_lower
                or "used_capacity" in key_lower
                or "usedbytes" in key_lower
            ):
                used_mb += parsed
                found = True

        if not found or total_mb <= 0:
            return None
        return round(used_mb / total_mb * 100, 1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return raw storage capacity and health attributes."""
        storage: dict[str, Any] = (self.coordinator.data or {}).get("storage_info", {})
        return {
            k: v
            for k, v in storage.items()
            if any(x in k.lower() for x in ("capacity", "status", "health"))
        }


class DVRMonitorModeSensor(DVREntity, SensorEntity):
    """Reports the current split mode of the physical monitor output."""

    entity_description: SensorEntityDescription = MONITOR_MODE_DESCRIPTION

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the monitor mode sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_monitor_mode"

    @property
    def native_value(self) -> str | None:
        """Return the active monitor split mode key (e.g. split1, split4, split8)."""
        mode_data: dict[str, Any] = (self.coordinator.data or {}).get(
            "monitor_mode", {}
        )
        mode = str(mode_data.get("mode", "")).strip()
        return mode if mode else None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return the active monitor group and mode details."""
        mode_data: dict[str, Any] = (self.coordinator.data or {}).get(
            "monitor_mode", {}
        )
        if not mode_data:
            return {}
        return {
            "mode": mode_data.get("mode"),
            "group": mode_data.get("group", 0),
        }


class CameraIPSensor(CameraChannelEntity, SensorEntity):
    """Reports the IP address of an IP camera connected to the DVR (disabled by default)."""

    entity_description: SensorEntityDescription = CAMERA_IP_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
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

    entity_description: SensorEntityDescription = RESPONSE_TIME_DESCRIPTION

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the response time sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_response_time"

    @property
    def native_value(self) -> float | None:
        """Return API response time in milliseconds."""
        return (self.coordinator.data or {}).get("response_time_ms")


class DVRConsecutiveFailuresSensor(DVREntity, SensorEntity):
    """Reports consecutive communication failures with the DVR."""

    entity_description: SensorEntityDescription = CONSECUTIVE_FAILURES_DESCRIPTION

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the consecutive failures sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_consecutive_failures"

    @property
    def native_value(self) -> int:
        """Return consecutive failure count."""
        return int((self.coordinator.data or {}).get("consecutive_failures", 0))


class DVRActiveUsersSensor(DVREntity, SensorEntity):
    """Reports the number of active user sessions connected to the DVR."""

    entity_description: SensorEntityDescription = ACTIVE_USERS_DESCRIPTION

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the active users sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_active_users"

    @property
    def native_value(self) -> int:
        """Return the number of active sessions."""
        users = (self.coordinator.data or {}).get("active_users", [])
        return len(users)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return details of connected user sessions."""
        users = (self.coordinator.data or {}).get("active_users", [])
        return {
            "active_sessions": [
                {
                    "user": u.get("user"),
                    "ip": u.get("ip"),
                    "client_type": u.get("client_type"),
                    "login_time": u.get("login_time"),
                    "group": u.get("group"),
                }
                for u in users
            ]
        }


class DVRStorageStatusSensor(DVREntity, SensorEntity):
    """Reports the overall health state of the DVR storage/disk controller."""

    entity_description: SensorEntityDescription = STORAGE_STATUS_DESCRIPTION

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the storage status sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_storage_status"

    @property
    def native_value(self) -> str:
        """Return overall disk status."""
        diag = (self.coordinator.data or {}).get("storage_diagnostics", {})
        return str(diag.get("overall_state", "Normal"))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return storage partitions and diagnostic details."""
        diag = (self.coordinator.data or {}).get("storage_diagnostics", {})
        return {
            "bad_disk_count": diag.get("bad_disk_count", 0),
            "has_bad_disk": diag.get("has_bad_disk", False),
            "filesystem_error": diag.get("filesystem_error", False),
            "total_disks": diag.get("total_disks", 1),
            "partitions": diag.get("partitions", []),
        }


class DVRNetworkSpeedSensor(DVREntity, SensorEntity):
    """Reports the network link speed of the DVR."""

    entity_description: SensorEntityDescription = NETWORK_SPEED_DESCRIPTION

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the network speed sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_network_speed"

    @property
    def native_value(self) -> int | None:
        """Return interface link speed in Mbit/s."""
        ifaces = (self.coordinator.data or {}).get("network_interfaces", [])
        if ifaces and isinstance(ifaces[0], dict):
            return ifaces[0].get("speed")
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return network interface attributes."""
        ifaces = (self.coordinator.data or {}).get("network_interfaces", [])
        if ifaces and isinstance(ifaces[0], dict):
            return {
                "interface": ifaces[0].get("name", "eth0"),
                "mac_address": ifaces[0].get("mac_address", ""),
                "connected": ifaces[0].get("connected", True),
            }
        return {}


class DVRSystemTimeSensor(DVREntity, SensorEntity):
    """Reports the current date/time on the DVR internal clock."""

    entity_description: SensorEntityDescription = SYSTEM_TIME_DESCRIPTION

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the DVR system time sensor."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_system_time"

    @property
    def native_value(self) -> str | None:
        """Return formatted DVR date/time."""
        return (self.coordinator.data or {}).get("system_time")
