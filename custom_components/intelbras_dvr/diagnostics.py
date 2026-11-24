"""Diagnostics support for Intelbras DVR integration."""
from __future__ import annotations

from typing import Any, Mapping

from homeassistant.components.diagnostics import async_redact_data
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceEntry

from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
    CONF_HTTP_PORT,
    CONF_PTZ_CHANNELS,
    CONF_RTSP_PORT,
    INTEGRATION_VERSION,
)

TO_REDACT = {
    CONF_USERNAME,
    CONF_PASSWORD,
    "host",
    "ip",
    "ip_address",
    "configuration_url",
    "password",
    "username",
    "serialNumber",
    "serial_number",
    "mac",
    "mac_address",
    "unique_id",
}


async def async_get_config_entry_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
) -> Mapping[str, Any]:
    """Return diagnostics for a config entry."""
    runtime_data = getattr(entry, "runtime_data", None)
    coordinator = getattr(runtime_data, "coordinator", None) if runtime_data else None
    event_coordinator = getattr(runtime_data, "event_coordinator", None) if runtime_data else None
    client = getattr(runtime_data, "client", None) if runtime_data else None

    coord_data: dict[str, Any] = dict(coordinator.data or {}) if (coordinator and coordinator.data) else {}
    event_data: dict[str, Any] = dict(event_coordinator.data or {}) if (event_coordinator and event_coordinator.data) else {}

    raw_diagnostics: dict[str, Any] = {
        "integration_version": INTEGRATION_VERSION,
        "entry": {
            "entry_id": entry.entry_id,
            "version": entry.version,
            "title": entry.title,
            "data": async_redact_data(dict(entry.data), TO_REDACT),
            "options": async_redact_data(dict(entry.options), TO_REDACT),
            "pref_disable_new_entities": entry.pref_disable_new_entities,
            "pref_disable_polling": entry.pref_disable_polling,
        },
        "coordinator": {
            "online": coord_data.get("online", False),
            "response_time_ms": coord_data.get("response_time_ms"),
            "consecutive_failures": coord_data.get("consecutive_failures", 0),
            "system_time": coord_data.get("system_time"),
            "software_version": coord_data.get("software_version"),
            "hardware_version": coord_data.get("hardware_version"),
            "system_info": async_redact_data(coord_data.get("system_info", {}), TO_REDACT),
            "total_channels": coord_data.get("total_channels"),
            "monitor_caps": coord_data.get("monitor_caps"),
            "monitor_mode": coord_data.get("monitor_mode"),
            "video_color": coord_data.get("video_color"),
            "encode_info": coord_data.get("encode_info"),
            "motion_detect": coord_data.get("motion_detect"),
            "video_widget": coord_data.get("video_widget"),
            "video_in_options": coord_data.get("video_in_options"),
            "video_input_caps": coord_data.get("video_input_caps"),
            "video_denoise": coord_data.get("video_denoise"),
            "image_enhancement": coord_data.get("image_enhancement"),
            "ptz_presets": coord_data.get("ptz_presets"),
            "ptz_status": coord_data.get("ptz_status"),
            "record_modes": coord_data.get("record_modes"),
            "firmware_update": coord_data.get("firmware_update"),
            "remote_devices": {
                str(ch): async_redact_data(info, TO_REDACT)
                for ch, info in (coord_data.get("remote_devices") or {}).items()
            },
            "selected_config_profile": (
                dict(coordinator._selected_config_profile) if coordinator else {}
            ),
            "storage_info": coord_data.get("storage_info"),
            "storage_diagnostics": coord_data.get("storage_diagnostics"),
            "network_interfaces": [
                async_redact_data(iface, TO_REDACT)
                for iface in coord_data.get("network_interfaces", [])
            ],
            "active_users": [
                async_redact_data(u, TO_REDACT)
                for u in coord_data.get("active_users", [])
            ],
            "user_permissions": async_redact_data(
                coord_data.get("user_permissions", {}), TO_REDACT
            ),
        },
        "event_coordinator": {
            "stream_connected": event_coordinator.stream_connected if event_coordinator else False,
            "reconnect_count": event_coordinator.reconnect_count if event_coordinator else 0,
            "last_heartbeat": event_coordinator.last_heartbeat if event_coordinator else None,
            "system_events": dict(event_coordinator.system_events) if event_coordinator else {},
            "active_events": event_data,
        },
        "client": {
            "total_channels": client.total_channels if client else None,
            "channel_names": client.channel_names if client else {},
            "channel_signal_formats": (
                dict(client._channel_signal_formats) if client else {}
            ),
            "remote_channel_count": len(client.remote_devices) if client else 0,
            "video_color_by_profile": (
                dict(client._video_color_by_profile) if client else {}
            ),
            "video_color_periods": (
                dict(client._video_color_periods) if client else {}
            ),
            "auth_lockout_remaining": client.auth_lockout_remaining if client else 0,
        },
    }

    return async_redact_data(raw_diagnostics, TO_REDACT)


async def async_get_device_diagnostics(
    hass: HomeAssistant,
    entry: ConfigEntry,
    device: DeviceEntry,
) -> Mapping[str, Any]:
    """Return diagnostics for a specific device."""
    data = dict(await async_get_config_entry_diagnostics(hass, entry))
    data["device_registry"] = {
        "id": device.id,
        "name": device.name,
        "name_by_user": device.name_by_user,
        "model": device.model,
        "manufacturer": device.manufacturer,
        "sw_version": device.sw_version,
        "hw_version": device.hw_version,
        "area_id": device.area_id,
        "disabled_by": str(device.disabled_by),
        "entry_type": str(device.entry_type),
        "via_device_id": device.via_device_id,
    }
    return data
