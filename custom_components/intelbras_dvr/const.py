"""Constants for the Intelbras DVR integration."""
from __future__ import annotations

from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "intelbras_dvr"
INTEGRATION_VERSION = "0.6.1"

# ---------------------------------------------------------------------------
# Configuration keys
# ---------------------------------------------------------------------------

CONF_HTTP_PORT = "http_port"
CONF_RTSP_PORT = "rtsp_port"
CONF_CHANNELS = "channels"
CONF_FEATURES = "features"
CONF_PTZ_CHANNELS = "ptz_channels"  # zero-based list of channels with PTZ heads
CONF_SNAPSHOT_INTERVAL = "snapshot_interval"
CONF_MONITOR_USERS = "monitor_users"
CONF_MONITOR_STORAGE = "monitor_storage"
CONF_MONITOR_NETWORK = "monitor_network"
CONF_MONITOR_EVENTS = "monitor_events"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_HTTP_PORT = 80
DEFAULT_RTSP_PORT = 554
DEFAULT_SNAPSHOT_INTERVAL = 1.0  # seconds between camera snapshot refreshes
DEFAULT_SCAN_INTERVAL = 60       # seconds — regular polling (slow poll)
FIRMWARE_CHECK_INTERVAL = 21600  # seconds — 6 hours
DEFAULT_GO2RTC_RTSP_PORT = 8554

# Picture setting defaults & ranges
DEFAULT_BRIGHTNESS = 50
DEFAULT_CONTRAST = 50
DEFAULT_HUE = 50
DEFAULT_SATURATION_ANALOG = 50
DEFAULT_SATURATION_ACUTANCE = 80
DEFAULT_ACUTANCE = 1
MAX_COLOR_VALUE = 100
MAX_ACUTANCE = 15

# Back-off after unresolved 401/403 Unauthorized to prevent DVR IP/account ban
LOGIN_LOCKOUT_BACKOFF = 65.0  # seconds (escalates up to LOGIN_LOCKOUT_MAX_BACKOFF)
LOGIN_LOCKOUT_MAX_BACKOFF = 600.0  # seconds (DVR LoginFailLockTime default)

# PTZ momentary button move duration (seconds)
PTZ_MOVE_DURATION = 0.5

# Recording modes returned/sent via RecordMode CGI config
RECORD_MODE_SCHEDULE = 0  # honours DVR schedule (events, motion, time, etc.)
RECORD_MODE_MANUAL = 1    # continuous / always on
RECORD_MODE_OFF = 2       # off / stopped

# ---------------------------------------------------------------------------
# CGI endpoint paths (HTTP API V3.35_Intelbras)
# ---------------------------------------------------------------------------

CONFIG_MANAGER_CGI = "/cgi-bin/configManager.cgi"
SPLIT_CGI = "/cgi-bin/split.cgi"
ENCODE_CGI = "/cgi-bin/encode.cgi"
DEV_VIDEO_INPUT_CGI = "/cgi-bin/devVideoInput.cgi"
MAGICBOX_CGI = "/cgi-bin/magicBox.cgi"
SNAPSHOT_CGI = "/cgi-bin/snapshot.cgi"
MEDIAFILEFIND_CGI = "/cgi-bin/mediaFileFind.cgi"
EVENT_MANAGER_CGI = "/cgi-bin/eventManager.cgi"
STORAGE_CGI = "/cgi-bin/storageDevice.cgi"
USER_MANAGER_CGI = "/cgi-bin/userManager.cgi"
NET_APP_CGI = "/cgi-bin/netApp.cgi"
GLOBAL_CGI = "/cgi-bin/global.cgi"
LOG_CGI = "/cgi-bin/log.cgi"
PTZ_CGI = "/cgi-bin/ptz.cgi"
UPGRADE_CGI = "/cgi-bin/upgrade.cgi"
UPGRADER_CGI = "/cgi-bin/upgrader.cgi"
CLOUD_UPGRADER_CHECK_API = "/cgi-bin/api/CloudUpgrader/check"
CLOUD_UPGRADER_EXEC_API = "/cgi-bin/api/CloudUpgrader/execute"
RTSP_LIVE_PATH = "/cam/realmonitor?channel={channel}&subtype={subtype}"
RTSP_PLAYBACK_PATH = "/cam/playback?channel={channel}&starttime={start}&endtime={end}"

# Media browser recordings
MEDIA_BROWSE_DAYS = 10  # Fallback days listed per channel when earliest date cannot be queried
MEDIA_SLICE_MINUTES = 5  # Time slice granularity in minutes within an hour
HLS_MIME = "application/x-mpegURL"

# PTZ movement direction codes (Dahua / Intelbras CGI)
PTZ_DIRECTION_CODES: dict[str, str] = {
    "up": "Up",
    "down": "Down",
    "left": "Right",
    "right": "Left",
    "upper_left": "UpperRight",
    "upper_right": "UpperLeft",
    "lower_left": "LowerRight",
    "lower_right": "LowerLeft",
    "zoom_in": "ZoomTele",
    "zoom_out": "ZoomWide",
    "focus_near": "FocusNear",
    "focus_far": "FocusFar",
    "home": "Home",
    "auto_scan": "AutoScan",
}


# ---------------------------------------------------------------------------
# Feature enum
# ---------------------------------------------------------------------------


class CameraFeature(StrEnum):
    """Features that can be toggled per camera channel."""

    STREAM_MAIN = "stream_main"
    STREAM_SUB = "stream_sub"
    SNAPSHOT = "snapshot"
    MOTION = "motion"
    VIDEO_LOSS = "video_loss"
    VIDEO_BLIND = "video_blind"
    MASK = "mask"
    VIDEO_COLOR = "video_color"
    PTZ = "ptz"
    RECORDING = "recording"  # manual recording switch
    CROSS_LINE = "cross_line"  # IVS Tripwire detection
    CROSS_REGION = "cross_region"  # IVS Perimeter intrusion detection


ALL_CAMERA_FEATURES: list[str] = list(CameraFeature)

SERVICE_GET_LOGS = "get_logs"

# Features that are only offered when PTZ is auto-detected for a channel
PTZ_ONLY_FEATURES: frozenset[str] = frozenset({CameraFeature.PTZ})

# System-level features offered in config/options flow
SYSTEM_FEATURES: frozenset[str] = frozenset({
    CONF_MONITOR_USERS,
    CONF_MONITOR_STORAGE,
    CONF_MONITOR_NETWORK,
    CONF_MONITOR_EVENTS,
})

PLATFORMS: list[Platform] = [
    Platform.BINARY_SENSOR,
    Platform.BUTTON,
    Platform.CAMERA,
    Platform.NUMBER,
    Platform.SELECT,
    Platform.SENSOR,
    Platform.SWITCH,
    Platform.UPDATE,
]
