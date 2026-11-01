"""Constants for the Intelbras DVR integration."""
from __future__ import annotations

from enum import StrEnum

from homeassistant.const import Platform

DOMAIN = "intelbras_dvr"

# ---------------------------------------------------------------------------
# Configuration keys
# ---------------------------------------------------------------------------

CONF_HTTP_PORT = "http_port"
CONF_RTSP_PORT = "rtsp_port"
CONF_CHANNELS = "channels"
CONF_FEATURES = "features"
CONF_PTZ_CHANNELS = "ptz_channels"  # zero-based list of channels with PTZ heads
CONF_SNAPSHOT_INTERVAL = "snapshot_interval"

# ---------------------------------------------------------------------------
# Defaults
# ---------------------------------------------------------------------------

DEFAULT_HTTP_PORT = 80
DEFAULT_RTSP_PORT = 554
DEFAULT_SNAPSHOT_INTERVAL = 1.0  # seconds between camera snapshot refreshes
DEFAULT_SCAN_INTERVAL = 60       # seconds — regular polling (slow poll)
FIRMWARE_CHECK_INTERVAL = 21600  # seconds — 6 hours

# Back-off after a 401 Unauthorized to prevent DVR IP ban
LOGIN_LOCKOUT_BACKOFF = 10.0  # seconds

# PTZ momentary button move duration (seconds)
PTZ_MOVE_DURATION = 0.5

# Recording modes returned/sent via RecordMode CGI config
RECORD_MODE_SCHEDULE = 0  # honours DVR schedule (events, motion, time, etc.)
RECORD_MODE_MANUAL = 1    # continuous / always on
RECORD_MODE_OFF = 2       # off / stopped

# ---------------------------------------------------------------------------
# CGI endpoint paths
# ---------------------------------------------------------------------------

CONFIG_MANAGER_CGI = "/cgi-bin/configManager.cgi"
SPLIT_CGI = "/cgi-bin/split.cgi"
MAGICBOX_CGI = "/cgi-bin/magicBox.cgi"
SNAPSHOT_CGI = "/cgi-bin/snapshot.cgi"
MEDIAFILEFIND_CGI = "/cgi-bin/mediaFileFind.cgi"
EVENT_MANAGER_CGI = "/cgi-bin/eventManager.cgi"
STORAGE_CGI = "/cgi-bin/storageDevice.cgi"
PTZ_CGI = "/cgi-bin/ptz.cgi"
UPGRADE_CGI = "/cgi-bin/upgrade.cgi"
RTSP_LIVE_PATH = "/cam/realmonitor?channel={channel}&subtype={subtype}"
RTSP_PLAYBACK_PATH = "/cam/playback?channel={channel}&starttime={start}&endtime={end}"

# Media browser recordings
MEDIA_BROWSE_DAYS = 7  # Days listed per channel
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


ALL_CAMERA_FEATURES: list[str] = list(CameraFeature)

# Features that are only offered when PTZ is auto-detected for a channel
PTZ_ONLY_FEATURES: frozenset[str] = frozenset({CameraFeature.PTZ})

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

