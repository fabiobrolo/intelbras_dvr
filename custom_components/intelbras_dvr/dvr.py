"""Async HTTP client for Intelbras / Dahua DVRs using Digest Authentication."""
from __future__ import annotations

import asyncio
import logging
import re
import time
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, time as dt_time, timedelta
from typing import Any, Callable

import httpx

from .const import (
    CGI_REQUEST_PACING,
    CLOUD_UPGRADER_CHECK_API,
    CLOUD_UPGRADER_EXEC_API,
    CONFIG_MANAGER_CGI,
    DEFAULT_ACUTANCE,
    DEFAULT_BRIGHTNESS,
    DEFAULT_CONTRAST,
    DEFAULT_HTTP_PORT,
    DEFAULT_HUE,
    DEFAULT_RTSP_PORT,
    DEFAULT_SATURATION_ACUTANCE,
    DEFAULT_SATURATION_ANALOG,
    DEV_VIDEO_INPUT_CGI,
    ENCODE_CGI,
    EVENT_MANAGER_CGI,
    GLOBAL_CGI,
    LOG_CGI,
    LOGIN_LOCKOUT_BACKOFF,
    LOGIN_LOCKOUT_MAX_BACKOFF,
    MAGICBOX_CGI,
    MEDIAFILEFIND_CGI,
    NET_APP_CGI,
    PTZ_CGI,
    RECORD_MODE_SCHEDULE,
    RTSP_LIVE_PATH,
    RTSP_PLAYBACK_PATH,
    SNAPSHOT_CGI,
    SPLIT_CGI,
    STORAGE_CGI,
    UPGRADE_CGI,
    USER_MANAGER_CGI,
)

_LOGGER = logging.getLogger(__name__)

_ITEM_RE = re.compile(r"^items\[(\d+)\]\.([A-Za-z]+)(?:\[\d+\])?=(.*)$")
_FIND_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_PLAYBACK_TIME_FMT = "%Y_%m_%d_%H_%M_%S"
_FIND_PAGE_SIZE = 100


class DVRError(Exception):
    """Base exception for DVR communication errors."""


class DVRAuthError(DVRError):
    """Raised when the DVR returns 401/403 authentication failure."""


class DVRConnectionError(DVRError):
    """Raised when the DVR cannot be reached."""


@dataclass
class Recording:
    """A single recording segment returned by mediaFileFind."""

    channel: int
    start: datetime
    end: datetime
    file_path: str = ""
    length: int = 0
    rec_type: str = "dav"
    flags: list[str] = field(default_factory=list)


class IntelbrasClient:
    """Async HTTP client with Digest Auth for the Intelbras / Dahua CGI API (V3.35)."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        http_port: int = DEFAULT_HTTP_PORT,
        rtsp_port: int = DEFAULT_RTSP_PORT,
        backoff_on_401: bool = True,
    ) -> None:
        """Initialise the client."""
        self.host = host
        self.username = username
        self.password = password
        self.http_port = http_port
        self.rtsp_port = rtsp_port
        self._backoff_on_401: bool = backoff_on_401
        self._lockout_until: float = 0.0
        self._auth_failure_streak: int = 0
        self._client: httpx.AsyncClient | None = None
        self._request_lock: asyncio.Lock = asyncio.Lock()
        self._last_request_ts: float = 0.0
        self._saved_video_colors: dict[int, dict[str, int]] = {}
        self._saved_video_colors_by_profile: dict[int, dict[int, dict[str, int]]] = {}
        self._channel_supports_acutance: set[int] = set()
        self._channel_profiles: dict[int, set[int]] = {}
        self._earliest_date_cache: dict[int, date] = {}
        self._dvr_earliest_date: date | None = None
        self._encode_config: dict[int, dict[str, Any]] = {}
        self._remote_devices: dict[int, dict[str, Any]] = {}
        self._channel_signal_formats: dict[int, str] = {}
        self._total_channels: int = 0
        self._channel_names: dict[int, str] = {}
        self._video_widget: dict[int, dict[str, Any]] = {}
        self._video_in_options: dict[int, dict[str, Any]] = {}
        self._video_color: dict[int, dict[str, Any]] = {}
        self._video_color_by_profile: dict[int, dict[int, dict[str, Any]]] = {}
        self._video_color_periods: dict[int, dict[int, dict[str, Any]]] = {}
        self._video_input_caps: dict[int, dict[str, Any]] = {}
        self._video_denoise: dict[int, dict[str, Any]] = {}
        self._image_enhancement: dict[int, dict[str, Any]] = {}
        self._monitor_caps: list[int] | None = None
        self._latest_fw_version: str | None = None
        self._monitor_lock = asyncio.Lock()

    @property
    def channel_names(self) -> dict[int, str]:
        """Return the cached mapping of channel index to channel title."""
        return self._channel_names

    @property
    def remote_devices(self) -> dict[int, dict[str, Any]]:
        """Return the remote IP device metadata mapping."""
        return self._remote_devices

    @property
    def auth_lockout_remaining(self) -> float:
        """Return seconds remaining in the current anti-lockout cooldown."""
        return max(0.0, self._lockout_until - time.monotonic())

    @property
    def lockout_deadline(self) -> float:
        """Return the monotonic timestamp until which auth retries are paused."""
        return self._lockout_until

    @property
    def auth_failure_streak(self) -> int:
        """Return the number of unresolved consecutive auth failures."""
        return self._auth_failure_streak

    def set_lockout_until(self, deadline: float, streak: int = 1) -> None:
        """Restore a persisted anti-lockout deadline and failure streak."""
        if deadline > time.monotonic() and deadline > self._lockout_until:
            self._lockout_until = deadline
        if streak > self._auth_failure_streak:
            self._auth_failure_streak = streak

    def reset_auth_backoff(self) -> None:
        """Reset auth cooldown and streak after any authenticated response."""
        self._lockout_until = 0.0
        self._auth_failure_streak = 0

    def _record_auth_failure(self) -> float:
        """Record an unresolved auth failure and activate escalating anti-lockout cooldown."""
        self._auth_failure_streak += 1
        if not self._backoff_on_401:
            return 0.0
        schedule = (
            LOGIN_LOCKOUT_BACKOFF,
            180.0,
            300.0,
            LOGIN_LOCKOUT_MAX_BACKOFF,
        )
        idx = min(self._auth_failure_streak - 1, len(schedule) - 1)
        cooldown = schedule[idx]
        self._lockout_until = max(self._lockout_until, time.monotonic() + cooldown)
        return self.auth_lockout_remaining

    @property
    def total_channels(self) -> int:
        """Return the total number of channels reported by the DVR."""
        return self._total_channels

    def channel_supports_acutance(self, channel: int) -> bool:
        """Return True if the camera channel supports acutance (sharpness)."""
        return channel in self._channel_supports_acutance

    def channel_is_ip(self, channel: int) -> bool:
        """Return True if the channel is an active IP camera."""
        if self._remote_devices:
            if channel not in self._remote_devices:
                return False
            dev = self._remote_devices[channel]
            return dev.get("Enable") == "true" and dev.get("Address") not in (
                "",
                "0.0.0.0",
                "192.168.0.0",
            )
        return False

    def channel_is_analog(self, channel: int) -> bool:
        """Return True if the channel is an analog BNC camera."""
        return not self.channel_is_ip(channel)

    def get_video_input_caps(self, channel: int) -> dict[str, Any]:
        """Return cached video input capabilities for a channel."""
        return self._video_input_caps.get(channel, {})

    def channel_supports_backlight(self, channel: int) -> bool:
        """Return True if the channel supports backlight compensation (BLC)."""
        return self._video_input_caps.get(channel, {}).get("backlight", 0) > 0

    def channel_supports_hlc(self, channel: int) -> bool:
        """Return True if the channel supports highlight compensation (HLC / GlareInhibition)."""
        return self._video_input_caps.get(channel, {}).get("glare_inhibition", 0) > 0

    def channel_supports_wdr(self, channel: int) -> bool:
        """Return True if the channel supports Wide Dynamic Range (WDR)."""
        return self._video_input_caps.get(channel, {}).get("wide_dynamic_range", 0) > 0

    def channel_supports_light_compensation(self, channel: int) -> bool:
        """Return True if the channel supports any light compensation mode."""
        caps = self._video_input_caps.get(channel, {})
        return (
            caps.get("backlight", 0) > 0
            or caps.get("glare_inhibition", 0) > 0
            or caps.get("wide_dynamic_range", 0) > 0
        )

    def channel_supports_denoise_2d(self, channel: int) -> bool:
        """Return True if the channel supports 2D noise reduction."""
        return self.channel_is_analog(channel) and self._video_input_caps.get(channel, {}).get("denoise_2d", False)

    def channel_supports_image_enhancement(self, channel: int) -> bool:
        """Return True if the channel supports image enhancement."""
        return self.channel_is_analog(channel) and self._video_input_caps.get(channel, {}).get("image_enhancement", False)

    def channel_supports_day_night(self, channel: int) -> bool:
        """Return True if the channel supports Day/Night color mode."""
        return self._video_input_caps.get(channel, {}).get("day_night_color", False)

    def get_channel_signal_format(self, channel: int) -> str:
        """Return the detected video signal format for a channel (e.g. HDCVI, AHD, TVI, CVBS, IP)."""
        if self.channel_is_ip(channel):
            return "IP"
        return self._channel_signal_formats.get(channel, "HDCVI")

    def get_channel_profiles(self, channel: int) -> list[int]:
        """Return all configured VideoColor profile indexes for a channel."""
        return sorted(self._channel_profiles.get(channel, {0}))

    @property
    def base_url(self) -> str:
        """Return the base HTTP URL for the DVR."""
        if self.http_port and self.http_port != DEFAULT_HTTP_PORT:
            return f"http://{self.host}:{self.http_port}"
        return f"http://{self.host}"

    async def _get_http_client(self) -> httpx.AsyncClient:
        """Return (or create) the persistent async HTTP session."""
        if self._client is None or self._client.is_closed:
            self._client = httpx.AsyncClient(
                auth=httpx.DigestAuth(self.username, self.password),
                verify=False,
                timeout=httpx.Timeout(15.0, connect=8.0),
                limits=httpx.Limits(max_keepalive_connections=5, max_connections=10),
            )
        return self._client

    async def close(self) -> None:
        """Close the underlying HTTP session."""
        if self._client and not self._client.is_closed:
            await self._client.aclose()
            self._client = None

    async def _reset_http_client_locked(self) -> None:
        """Close and clear the cached HTTP client while holding ``_request_lock``."""
        if self._client is not None and not self._client.is_closed:
            await self._client.aclose()
        self._client = None

    async def _request_with_retry(
        self,
        method: str,
        path: str,
        *,
        params: dict[str, Any] | None = None,
        json: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> httpx.Response:
        """Execute a serialized HTTP request with single fresh-client self-heal on transient 401."""
        remaining = self.auth_lockout_remaining
        if remaining > 0:
            raise DVRAuthError(
                f"Temporary lockout active for {remaining:.0f}s "
                "due to previous authentication failure."
            )

        url = self.base_url + path
        req_params = params
        if req_params:
            # Dahua/Intelbras CGI requires literal brackets in table names like VideoInOptions[0]
            # urlencode with safe="[]:" ensures brackets are not converted to %5B / %5D
            qs = urllib.parse.urlencode(req_params, doseq=True, safe="[]:", quote_via=urllib.parse.quote)
            joiner = "&" if "?" in url else "?"
            url = f"{url}{joiner}{qs}"
            req_params = None

        async with self._request_lock:
            remaining = self.auth_lockout_remaining
            if remaining > 0:
                raise DVRAuthError(
                    f"Temporary lockout active for {remaining:.0f}s "
                    "due to previous authentication failure."
                )

            can_self_heal = self._backoff_on_401 and self._auth_failure_streak == 0
            max_attempts = 2 if can_self_heal else 1

            for attempt in range(max_attempts):
                if CGI_REQUEST_PACING > 0 and self._last_request_ts > 0:
                    elapsed = time.monotonic() - self._last_request_ts
                    if elapsed < CGI_REQUEST_PACING:
                        await asyncio.sleep(CGI_REQUEST_PACING - elapsed)
                client = await self._get_http_client()
                try:
                    if method == "POST":
                        resp = await client.post(url, json=json, timeout=timeout)
                    else:
                        resp = await client.get(url, params=req_params, timeout=timeout)
                except httpx.ConnectError as exc:
                    raise DVRConnectionError(f"Cannot connect to DVR at {url}") from exc
                except httpx.TimeoutException as exc:
                    raise DVRConnectionError(f"Timeout connecting to DVR at {url}") from exc
                except httpx.HTTPError as exc:
                    raise DVRConnectionError(
                        f"HTTP error communicating with DVR at {url}: {exc}"
                    ) from exc
                finally:
                    self._last_request_ts = time.monotonic()

                if resp.status_code != 401:
                    client.cookies.clear()
                    if resp.status_code == 200:
                        self.reset_auth_backoff()
                    return resp

                _LOGGER.debug(
                    "DVR returned 401 for %s (attempt %d/%d, streak=%d); resetting Digest session.",
                    path,
                    attempt + 1,
                    max_attempts,
                    self._auth_failure_streak,
                )
                await self._reset_http_client_locked()
                if attempt + 1 < max_attempts:
                    await asyncio.sleep(0.35)

            cooldown = self._record_auth_failure()
            if cooldown > 0:
                _LOGGER.warning(
                    "DVR returned 401 Unauthorized on %s; anti-lockout cooldown active for %ds (streak=%d).",
                    path,
                    int(cooldown),
                    self._auth_failure_streak,
                )
            else:
                _LOGGER.error("DVR returned 401 Unauthorized on %s.", path)
            raise DVRAuthError("Invalid credentials (401 Unauthorized)")

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> httpx.Response:
        """Perform a serialized GET request with Digest self-healing and anti-lockout protection."""
        return await self._request_with_retry("GET", path, params=params, timeout=timeout)

    async def _post_json(
        self,
        path: str,
        payload: dict[str, Any],
        timeout: float = 10.0,
    ) -> httpx.Response:
        """Perform a serialized POST JSON request (used by V3.35 endpoints such as CloudUpgrader)."""
        return await self._request_with_retry("POST", path, json=payload, timeout=timeout)

    # ------------------------------------------------------------------
    # System information & Dynamic Hardware Discovery
    # ------------------------------------------------------------------

    async def get_system_info(self) -> dict[str, str]:
        """Return hardware information (serial number, model, processor)."""
        resp = await self._get(f"{MAGICBOX_CGI}?action=getSystemInfo")
        if resp.status_code == 403:
            self._record_auth_failure()
            raise DVRAuthError("Access forbidden (403 Forbidden)")
        result: dict[str, str] = {}
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if "=" in line:
                    key, value = line.strip().split("=", 1)
                    result[key.strip()] = value.strip()
        return result

    async def get_software_version(self) -> str:
        """Return the DVR firmware version string."""
        resp = await self._get(f"{MAGICBOX_CGI}?action=getSoftwareVersion")
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.startswith("version="):
                    return line.split("=", 1)[1].strip()
        return "Unknown"

    async def get_channel_titles(self, active_only: bool = True) -> dict[int, str]:
        """Return a mapping of zero-based channel index to channel name.

        When active_only is True, filters out unassigned IP channels or inactive ports
        by inspecting RemoteDevice metadata and default placeholder titles from the DVR.
        Also populates ``self._total_channels`` and ``self._channel_signal_formats``.
        """
        titles: dict[int, str] = {}
        remote_devices: dict[int, dict[str, str]] = {}
        video_in_props: dict[int, dict[str, str]] = {}

        try:
            resp = await self._get(
                CONFIG_MANAGER_CGI,
                params={
                    "action": "getConfig",
                    "name": ["ChannelTitle", "RemoteDevice", "VideoIn"],
                },
                timeout=6.0,
            )
            if resp.status_code == 200:
                title_pat = re.compile(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)")
                rem_pat = re.compile(
                    r"table\.RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.([A-Za-z0-9_\[\]\.]+)=(.*)"
                )
                vin_pat = re.compile(r"table\.VideoIn\[(\d+)\]\.([A-Za-z0-9_\[\]\.]+)=(.*)")

                for line in resp.text.splitlines():
                    line_str = line.strip()
                    m_t = title_pat.match(line_str)
                    if m_t:
                        titles[int(m_t.group(1))] = m_t.group(2).replace("|", " ").strip()
                        continue
                    m_r = rem_pat.match(line_str)
                    if m_r:
                        remote_devices.setdefault(int(m_r.group(1)), {})[m_r.group(2)] = m_r.group(3).strip()
                        continue
                    m_v = vin_pat.match(line_str)
                    if m_v:
                        video_in_props.setdefault(int(m_v.group(1)), {})[m_v.group(2)] = m_v.group(3).strip()
                        continue

                if remote_devices:
                    self._remote_devices = remote_devices
                for ch, props in video_in_props.items():
                    sig = (
                        props.get("SignalFormat")
                        or props.get("Format")
                        or props.get("Mode")
                        or ""
                    ).upper()
                    if any(fmt in sig for fmt in ("HDCVI", "CVI", "AHD", "TVI", "CVBS", "NTSC", "PAL")):
                        self._channel_signal_formats[ch] = "HDCVI" if "CVI" in sig else sig
        except DVRAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Failed multi-param channel query: %s", exc)

        # Fallback to single ChannelTitle query if multi-query failed
        if not titles:
            try:
                resp = await self._get(
                    CONFIG_MANAGER_CGI,
                    params={"action": "getConfig", "name": "ChannelTitle"},
                    timeout=4.0,
                )
                if resp.status_code == 200:
                    pattern = re.compile(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)")
                    for line in resp.text.splitlines():
                        match = pattern.match(line.strip())
                        if match:
                            titles[int(match.group(1))] = match.group(2).replace("|", " ").strip()
            except DVRAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Failed ChannelTitle query: %s", exc)

        if titles:
            self._total_channels = max(self._total_channels, len(titles), max(titles.keys()) + 1)
            self._channel_names.update(titles)

        if not active_only or not titles:
            return titles

        # Filter out inactive/unassigned channels (default placeholder name and no active IP camera)
        active_titles: dict[int, str] = {}
        for ch, name in titles.items():
            rem = remote_devices.get(ch, self._remote_devices.get(ch, {}))
            rem_enable = rem.get("Enable", "")
            rem_addr = rem.get("Address", "")

            clean = name.strip().lower()
            is_default_name = clean in (
                f"cam {ch + 1}",
                f"channel {ch + 1}",
                f"canal {ch + 1}",
                f"cam{ch + 1}",
                f"channel{ch + 1}",
                f"canal{ch + 1}",
                "",
            )
            is_ip_active = (
                rem_enable == "true"
                and rem_addr not in ("", "0.0.0.0", "192.168.0.0")
            )

            if is_ip_active or not is_default_name:
                active_titles[ch] = name or f"Channel {ch + 1}"

        return active_titles

    async def async_discover_channels(self) -> tuple[dict[int, str], list[int], set[int]]:
        """Discover active channels, PTZ-capable channels, and ONVIF channels dynamically."""
        available_channels = await self.get_channel_titles(active_only=True)
        if not available_channels:
            return {}, [], set()

        candidate_channels = list(available_channels.keys())
        ptz_res: list[int] = []
        onvif_res: set[int] = set()
        try:
            ptz_res = await self.detect_ptz_channels(candidate_channels)
        except DVRAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("PTZ channel discovery failed: %s", exc)
        try:
            onvif_res = await self.get_onvif_channels()
        except DVRAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("ONVIF channel discovery failed: %s", exc)

        ptz_channels: list[int] = [ch for ch in ptz_res if ch in available_channels]
        return available_channels, ptz_channels, onvif_res

    async def async_discover_system_capabilities(self) -> dict[str, bool]:
        """Probe the DVR to determine which optional system monitoring features are supported."""
        caps = {
            "users": False,
            "storage": False,
            "network": False,
            "logs": False,
        }
        try:
            r = await self._get(f"{USER_MANAGER_CGI}?action=getActiveUserInfoAll")
            if r.status_code == 200 and "users[" in r.text:
                caps["users"] = True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("DVR active users probe failed: %s", exc)

        try:
            r = await self._get(f"{STORAGE_CGI}?action=factory.getPortInfo")
            if r.status_code == 200 and "info." in r.text:
                caps["storage"] = True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("DVR storage diagnostics probe failed: %s", exc)

        try:
            r = await self._get(f"{NET_APP_CGI}?action=getInterfaces")
            if r.status_code == 200 and "netInterface[" in r.text:
                caps["network"] = True
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("DVR network interfaces probe failed: %s", exc)

        try:
            r = await self._get(
                f"{LOG_CGI}?action=startFind&condition.Type=All&condition.StartTime=2026-01-01%2000:00:00&condition.EndTime=2026-01-01%2000:00:01"
            )
            if r.status_code == 200 and "token=" in r.text:
                caps["logs"] = True
                for line in r.text.splitlines():
                    if line.startswith("token="):
                        tok = line.split("=", 1)[1].strip()
                        await self._get(f"{LOG_CGI}?action=stopFind&token={tok}")
                        break
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("DVR logs query probe failed: %s", exc)

        return caps

    async def async_discover_video_input_caps(
        self, channels: list[int]
    ) -> dict[int, dict[str, Any]]:
        """Query devVideoInput.cgi?action=getCaps for each channel to discover hardware capabilities."""
        caps_map: dict[int, dict[str, Any]] = {}

        async def _fetch_one(ch: int) -> tuple[int, str]:
            try:
                resp = await self._get(
                    f"{DEV_VIDEO_INPUT_CGI}?action=getCaps&channel={ch + 1}",
                    timeout=5.0,
                )
                if resp.status_code == 200:
                    return ch, resp.text
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Failed to getCaps for channel %d: %s", ch + 1, exc)
            return ch, ""

        results = await asyncio.gather(*[_fetch_one(ch) for ch in channels])
        for ch, text in results:
            c = {
                "backlight": 0,
                "glare_inhibition": 0,
                "wide_dynamic_range": 0,
                "gain": False,
                "gain_auto": False,
                "day_night_color": False,
                "denoise_2d": False,
                "denoise_3d": False,
                "image_enhancement": False,
                "set_color": False,
            }
            if text:
                for line in text.splitlines():
                    line = line.strip()
                    if line.startswith("caps.Backlight="):
                        try:
                            c["backlight"] = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("caps.GlareInhibition="):
                        try:
                            c["glare_inhibition"] = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("caps.WideDynamicRange="):
                        try:
                            c["wide_dynamic_range"] = int(line.split("=")[1])
                        except ValueError:
                            pass
                    elif line.startswith("caps.Gain="):
                        c["gain"] = line.split("=")[1].lower() == "true"
                    elif line.startswith("caps.GainAuto="):
                        c["gain_auto"] = line.split("=")[1].lower() == "true"
                    elif line.startswith("caps.DayNightColor="):
                        c["day_night_color"] = line.split("=")[1].lower() == "true"
                    elif line.startswith("caps.VideoInDenoise.2D.Support="):
                        c["denoise_2d"] = line.split("=")[1].lower() == "true"
                    elif line.startswith("caps.VideoInDenoise.3D.Support="):
                        c["denoise_3d"] = line.split("=")[1].lower() == "true"
                    elif line.startswith("caps.ImageEnhancement.Support="):
                        c["image_enhancement"] = line.split("=")[1].lower() == "true"
                    elif line.startswith("caps.SetColor="):
                        c["set_color"] = line.split("=")[1].lower() == "true"
            caps_map[ch] = c
            self._video_input_caps[ch] = c

        return caps_map

    async def get_monitor_caps(self) -> list[int]:
        """Discover supported preview split modes (e.g. [1, 4, 8, 9, 16]) from the DVR.

        Uses ``GET /cgi-bin/encode.cgi?action=getCaps`` (HTTP API V3.35 Section 4.5.2,
        ``caps.PreviewSplitNumList[i]``), falling back to total channel capacity.
        """
        if self._monitor_caps is not None:
            return self._monitor_caps

        split_nums: list[int] = []
        try:
            resp = await self._get(
                ENCODE_CGI,
                params={"action": "getCaps"},
                timeout=4.0,
            )
            if resp.status_code == 200:
                pat = re.compile(r"caps\.PreviewSplitNumList\[\d+\]=(\d+)")
                enc_pat = re.compile(r"caps\.VideoEncodeDevices\[(\d+)\]\.")
                max_enc_idx = -1
                for line in resp.text.splitlines():
                    s_line = line.strip()
                    m = pat.match(s_line)
                    if m:
                        val = int(m.group(1))
                        if val > 0 and val not in split_nums:
                            split_nums.append(val)
                    m_enc = enc_pat.match(s_line)
                    if m_enc:
                        max_enc_idx = max(max_enc_idx, int(m_enc.group(1)))
                if max_enc_idx >= 0:
                    self._total_channels = max(self._total_channels, max_enc_idx + 1)
        except DVRAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("encode.cgi?action=getCaps not available: %s", exc)

        if not self._total_channels:
            try:
                resp_vin = await self._get(
                    DEV_VIDEO_INPUT_CGI,
                    params={"action": "getCollect"},
                    timeout=3.0,
                )
                if resp_vin.status_code == 200:
                    m_col = re.search(r"result=(\d+)", resp_vin.text)
                    if m_col:
                        self._total_channels = max(self._total_channels, int(m_col.group(1)))
            except DVRAuthError:
                raise
            except Exception:  # noqa: BLE001
                pass

        total = self._total_channels or 16
        if not split_nums:
            # Dynamically derive standard Dahua/Intelbras split modes from channel capacity
            for candidate in (1, 4, 8, 9, 16, 25, 36):
                if candidate == 1 or candidate <= total or (candidate == 16 and total >= 10):
                    split_nums.append(candidate)

        self._monitor_caps = sorted(set(split_nums))
        return self._monitor_caps

    async def get_remote_devices(self, refresh: bool = False) -> dict[int, dict[str, Any]]:
        """Query connected remote IP cameras and their hardware metadata."""
        devices: dict[int, dict[str, str]] = {}
        if not refresh and self._remote_devices:
            devices = self._remote_devices
        else:
            resp = await self._get(
                CONFIG_MANAGER_CGI,
                params={"action": "getConfig", "name": "RemoteDevice"},
            )
            if resp.status_code != 200:
                return {}

            pattern = re.compile(
                r"table\.RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.([A-Za-z0-9_\[\]\.]+)=(.*)"
            )
            for line in resp.text.splitlines():
                match = pattern.match(line.strip())
                if match:
                    idx = int(match.group(1))
                    prop = match.group(2)
                    val = match.group(3).strip()
                    devices.setdefault(idx, {})[prop] = val

            if devices:
                self._remote_devices = devices

        parsed: dict[int, dict[str, Any]] = {}
        for idx, d in devices.items():
            if d.get("Enable") != "true":
                continue
            vendor = d.get("Vendor", "")
            dev_class = d.get("DeviceClass", "")
            dev_type = d.get("DeviceType", "")
            model = dev_class or dev_type or "IP Camera"

            if "reolink" in model.lower() or "reolink" in vendor.lower():
                manufacturer = "Reolink"
            elif vendor in ("Private", "Intelbras") or model.startswith("VIP-") or model.startswith("VHD-"):
                manufacturer = "Intelbras"
            elif vendor and vendor != "Onvif":
                manufacturer = vendor
            else:
                manufacturer = "Intelbras" if model.startswith("VIP-") else (vendor or "ONVIF")

            ip = d.get("Address", "")
            http_port = d.get("HttpPort", "")
            https_port = d.get("HttpsPort", "")

            config_url = None
            if ip and ip not in ("0.0.0.0", "192.168.0.0"):
                if http_port and http_port not in ("", "0"):
                    p = int(http_port) if http_port.isdigit() else DEFAULT_HTTP_PORT
                    if p == 8000 and manufacturer == "Reolink":
                        p = DEFAULT_HTTP_PORT
                    config_url = f"http://{ip}" if p == DEFAULT_HTTP_PORT else f"http://{ip}:{p}"
                elif https_port and https_port not in ("", "0"):
                    p = int(https_port) if https_port.isdigit() else 443
                    config_url = f"https://{ip}" if p == 443 else f"https://{ip}:{p}"
                else:
                    config_url = f"http://{ip}"

            parsed[idx] = {
                "channel": idx,
                "name": d.get("VideoInputs[0].Name") or d.get("Name") or f"Channel {idx + 1}",
                "manufacturer": manufacturer,
                "vendor": vendor,
                "model": model,
                "software_version": d.get("Version") or None,
                "serial_number": d.get("SerialNo") or None,
                "ip_address": ip if ip not in ("", "0.0.0.0", "192.168.0.0") else None,
                "http_port": int(http_port) if http_port.isdigit() else None,
                "protocol": d.get("ProtocolType"),
                "ProtocolType": d.get("ProtocolType"),
                "configuration_url": config_url,
            }

        return parsed

    async def detect_ptz_channels(self, channels: list[int] | None = None) -> list[int]:
        """Detect zero-based channel indices that support PTZ control.

        Queries ``ptz.cgi?action=getPresets`` (1-based channel numbering) sequentially.
        Note: ``ptz.cgi?action=getStatus`` is intentionally NOT used because the DVR's
        ONVIF stack returns ``status.MoveStatus=Idle`` even for fixed non-PTZ ONVIF cameras.
        """
        if channels is None:
            try:
                titles = await self.get_channel_titles()
                channels = list(titles.keys())
            except DVRAuthError:
                raise
            except Exception:  # noqa: BLE001
                channels = []

        if not channels:
            return []

        detected: list[int] = []
        for ch in channels:
            try:
                resp = await self._get(
                    PTZ_CGI,
                    params={"action": "getPresets", "channel": ch + 1},
                    timeout=2.0,
                )
                if resp.status_code == 200 and "presets[" in resp.text:
                    detected.append(ch)
            except DVRAuthError:
                raise
            except Exception:  # noqa: BLE001
                pass
        return sorted(set(detected))

    async def get_onvif_channels(self) -> set[int]:
        """Return zero-based indices of channels connected via ONVIF."""
        try:
            remote = await self.get_remote_devices()
            return {
                ch
                for ch, dev in remote.items()
                if (dev.get("protocol") or "").lower() == "onvif"
                or (dev.get("vendor") or "").lower() == "onvif"
            }
        except DVRAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Failed to detect ONVIF channels: %s", exc)
            return set()

    # ------------------------------------------------------------------
    # Firmware update (V3.35 CloudUpgrader + legacy upgrade.cgi)
    # ------------------------------------------------------------------

    async def check_firmware_update(self) -> dict[str, Any]:
        """Query the DVR's built-in firmware update check.

        Tries HTTP API V3.35 ``POST /cgi-bin/api/CloudUpgrader/check`` first,
        then falls back to ``GET /cgi-bin/upgrade.cgi?action=check``.
        """
        result: dict[str, Any] = {
            "available": False,
            "latest_version": None,
            "release_notes": None,
        }

        # 1. Try V3.35 CloudUpgrader JSON API (Section 4.12.2)
        try:
            resp = await self._post_json(CLOUD_UPGRADER_CHECK_API, {"way": 0}, timeout=8.0)
            if resp.status_code == 200 and resp.content:
                data = resp.json()
                info = data.get("info") if isinstance(data, dict) else None
                if isinstance(info, dict):
                    state = str(info.get("State", "None"))
                    new_ver = info.get("NewVersion")
                    attention = info.get("Attention")
                    if state not in ("None", "") and new_ver:
                        result["available"] = True
                        result["latest_version"] = str(new_ver).strip()
                        self._latest_fw_version = result["latest_version"]
                    if attention:
                        result["release_notes"] = str(attention).strip()
                    return result
        except DVRAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("CloudUpgrader check not supported, trying upgrade.cgi: %s", exc)

        # 2. Fallback to legacy upgrade.cgi?action=check
        try:
            resp = await self._get(f"{UPGRADE_CGI}?action=check", timeout=8.0)
            if resp.status_code == 200:
                text = resp.text
                found_match = re.search(r"(?:status|found)=(\w+)", text, re.IGNORECASE)
                ver_match = re.search(r"version=([^\r\n]+)", text, re.IGNORECASE)
                notes_match = re.search(r"description=([^\r\n]+)", text, re.IGNORECASE)

                if found_match and found_match.group(1).lower() in ("true", "1", "yes", "found"):
                    result["available"] = True
                if ver_match:
                    result["latest_version"] = ver_match.group(1).strip()
                    self._latest_fw_version = result["latest_version"]
                if notes_match:
                    result["release_notes"] = notes_match.group(1).strip()
        except (DVRConnectionError, DVRAuthError) as exc:
            _LOGGER.debug("DVR firmware check failed/unsupported: %s", exc)
        return result

    async def trigger_firmware_update(self, version: str | None = None) -> bool:
        """Trigger an online firmware upgrade on the DVR if supported."""
        target_ver = version or self._latest_fw_version
        if target_ver:
            try:
                resp = await self._post_json(
                    CLOUD_UPGRADER_EXEC_API,
                    {"NewVersion": target_ver, "way": 0},
                    timeout=10.0,
                )
                if resp.status_code == 200:
                    data = resp.json()
                    if isinstance(data, dict) and data.get("result") is True:
                        return True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("CloudUpgrader execute failed, trying upgrade.cgi: %s", exc)

        for action in ("upgradeSystem", "start"):
            try:
                resp = await self._get(f"{UPGRADE_CGI}?action={action}", timeout=10.0)
                if resp.status_code == 200 and "OK" in resp.text:
                    return True
            except (DVRConnectionError, DVRAuthError) as exc:
                _LOGGER.debug("DVR firmware trigger (%s) failed: %s", action, exc)
        return False

    # ------------------------------------------------------------------
    # Video colour / picture settings
    # ------------------------------------------------------------------

    async def get_video_color(self) -> dict[int, dict[str, int]]:
        """Return current picture settings for all channels and all profiles."""
        resp = await self._get(
            CONFIG_MANAGER_CGI,
            params={"action": "getConfig", "name": "VideoColor"},
        )
        colors: dict[int, dict[str, int]] = {}
        if resp.status_code != 200:
            return colors

        pattern = re.compile(
            r"table\.VideoColor\[(\d+)\](?:\[(\d+)\])?\.([A-Za-z]+)=(\d+)"
        )
        acutance_pattern = re.compile(r"table\.VideoColor\[(\d+)\].*Acutance=(\d+)")
        ts_pattern = re.compile(
            r"table\.VideoColor\[(\d+)\]\[(\d+)\]\.TimeSection=(\d+)\s+(\d{2}):(\d{2}):(\d{2})-(\d{2}):(\d{2}):(\d{2})"
        )

        for line in resp.text.splitlines():
            line_str = line.strip()

            ts_m = ts_pattern.match(line_str)
            if ts_m:
                ch = int(ts_m.group(1))
                period = int(ts_m.group(2))
                active = int(ts_m.group(3)) == 1
                sh, sm, ss = int(ts_m.group(4)), int(ts_m.group(5)), int(ts_m.group(6))
                eh, em, es = int(ts_m.group(7)), int(ts_m.group(8)), int(ts_m.group(9))
                if sh >= 24:
                    sh, sm, ss = 23, 59, 59
                if eh >= 24:
                    eh, em, es = 23, 59, 59
                start = dt_time(sh, sm, ss)
                end = dt_time(eh, em, es)
                self._video_color_periods.setdefault(ch, {})[period] = {
                    "active": active,
                    "start": start,
                    "end": end,
                }
                continue

            match = pattern.match(line_str)
            if match:
                ch = int(match.group(1))
                profile = int(match.group(2)) if match.group(2) is not None else 0
                prop = match.group(3)
                val = int(match.group(4))

                self._channel_profiles.setdefault(ch, set()).add(profile)
                self._video_color_by_profile.setdefault(ch, {}).setdefault(profile, {})[prop] = val

                if profile == 0:
                    colors.setdefault(ch, {})[prop] = val

            ac_match = acutance_pattern.match(line_str)
            if ac_match:
                ch = int(ac_match.group(1))
                self._channel_supports_acutance.add(ch)

        return colors

    async def set_video_color(
        self,
        channel: int,
        brightness: int | None = None,
        contrast: int | None = None,
        hue: int | None = None,
        saturation: int | None = None,
        acutance: int | None = None,
        profile: int | None = None,
    ) -> bool:
        """Update picture settings for a specific channel on its active (or specified) profile."""
        prof = (
            profile
            if profile is not None
            else self.get_active_profile_index(channel)
        )

        prefix = f"VideoColor[{channel}][{prof}]"
        params: dict[str, Any] = {
            "action": "setConfig",
        }
        if brightness is not None:
            params[f"{prefix}.Brightness"] = brightness
        if contrast is not None:
            params[f"{prefix}.Contrast"] = contrast
        if hue is not None:
            params[f"{prefix}.Hue"] = hue
        if saturation is not None:
            params[f"{prefix}.Saturation"] = saturation
        if acutance is not None and self.channel_supports_acutance(channel):
            params[f"{prefix}.Acutance"] = acutance

        if len(params) <= 1:
            return True

        success = True
        resp = await self._get(CONFIG_MANAGER_CGI, params=params)
        if resp.status_code != 200 or "OK" not in resp.text:
            fallback_prefix = f"VideoColor[{channel}]"
            fallback_params: dict[str, Any] = {"action": "setConfig"}
            for k, v in params.items():
                if k != "action":
                    fallback_params[k.replace(prefix, fallback_prefix)] = v
            fb_resp = await self._get(CONFIG_MANAGER_CGI, params=fallback_params)
            if fb_resp.status_code != 200 or "OK" not in fb_resp.text:
                success = False

        if success:
            prof_dict = self._video_color_by_profile.setdefault(channel, {}).setdefault(prof, {})
            base_dict = self._video_color.setdefault(channel, {})
            for k, v in [
                ("Brightness", brightness),
                ("Contrast", contrast),
                ("Hue", hue),
                ("Saturation", saturation),
                ("Acutance", acutance),
            ]:
                if v is not None:
                    prof_dict[k] = v
                    if prof == self.get_active_profile_index(channel):
                        base_dict[k] = v

        return success

    # ------------------------------------------------------------------
    # Storage & Disks
    # ------------------------------------------------------------------

    async def get_storage_info(self) -> dict[str, Any]:
        """Query storage device status and capacity."""
        resp = await self._get(f"{STORAGE_CGI}?action=getDeviceAllInfo")
        info: dict[str, Any] = {}
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    info[k.strip()] = v.strip()
        return info

    async def get_storage_diagnostics(self) -> dict[str, Any]:
        """Query storage device status, damaged disk indicators, and partitions."""
        port_resp = await self._get(f"{STORAGE_CGI}?action=factory.getPortInfo")
        dev_resp = await self._get(f"{STORAGE_CGI}?action=getDeviceAllInfo")

        bad_count = 0
        total_disks = 0
        if port_resp.status_code == 200:
            for line in port_resp.text.splitlines():
                if line.startswith("info.Bad="):
                    try:
                        bad_count = int(line.split("=", 1)[1].strip())
                    except ValueError:
                        pass
                elif line.startswith("info.Total="):
                    try:
                        total_disks = int(line.split("=", 1)[1].strip())
                    except ValueError:
                        pass

        partitions: list[dict[str, Any]] = []
        raw_info: dict[str, str] = {}
        overall_state = "Unknown"
        fs_error = False
        total_bytes = 0.0
        used_bytes = 0.0

        if dev_resp.status_code == 200:
            part_map: dict[int, dict[str, Any]] = {}
            for line in dev_resp.text.splitlines():
                line = line.strip()
                if not line or "=" not in line:
                    continue
                k, v = line.split("=", 1)
                k = k.strip()
                v = v.strip()
                raw_info[k] = v
                if k == "list.info[0].State":
                    overall_state = v
                elif k.startswith("list.info[0].Detail["):
                    m = re.match(r"list\.info\[0\]\.Detail\[(\d+)\]\.(\w+)", k)
                    if m:
                        idx = int(m.group(1))
                        attr = m.group(2)
                        part = part_map.setdefault(idx, {})
                        if attr == "IsError":
                            is_err = v.lower() == "true"
                            part["is_error"] = is_err
                            if is_err:
                                fs_error = True
                        elif attr in ("TotalBytes", "UsedBytes"):
                            try:
                                val = float(v)
                                part[attr.lower()] = val
                                if attr == "TotalBytes":
                                    total_bytes += val
                                else:
                                    used_bytes += val
                            except ValueError:
                                pass
                        else:
                            part[attr.lower()] = v
            partitions = [part_map[i] for i in sorted(part_map.keys())]

        usage_pct = round((used_bytes / total_bytes * 100), 1) if total_bytes > 0 else 0.0

        return {
            "bad_disk_count": bad_count,
            "has_bad_disk": bad_count > 0,
            "total_disks": total_disks,
            "overall_state": overall_state,
            "filesystem_error": fs_error,
            "partitions": partitions,
            "total_bytes": total_bytes,
            "used_bytes": used_bytes,
            "usage_percent": usage_pct,
            "raw_info": raw_info,
        }

    async def get_active_users(self) -> list[dict[str, Any]]:
        """Query active sessions logged into the DVR via userManager.cgi."""
        resp = await self._get(f"{USER_MANAGER_CGI}?action=getActiveUserInfoAll")
        if resp.status_code != 200:
            return []

        user_map: dict[int, dict[str, Any]] = {}
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            m = re.match(r"users\[(\d+)\]\.(\w+)", k.strip())
            if m:
                idx = int(m.group(1))
                attr = m.group(2)
                user_entry = user_map.setdefault(idx, {})
                user_entry[attr] = v.strip()

        sessions = []
        for idx in sorted(user_map.keys()):
            u = user_map[idx]
            sessions.append({
                "index": idx,
                "user": u.get("Name", "unknown"),
                "ip": u.get("ClientAddress", "unknown"),
                "client_type": u.get("ClientType", "unknown"),
                "login_time": u.get("LoginTime", ""),
                "group": u.get("Group", ""),
                "session_id": u.get("SessionNum", ""),
            })
        return sessions

    async def async_validate_user_permissions(
        self, channels: list[int] | None = None
    ) -> dict[str, Any]:
        """Validate whether the configured DVR user has all required permissions and is reusable."""
        required_system = (
            "AuthSysCfg",
            "AuthSysInfo",
            "AuthManuCtr",
            "AuthStoreCfg",
            "AuthEventCfg",
            "AuthMaintence",
        )
        try:
            resp = await self._get(
                USER_MANAGER_CGI,
                params={"action": "getUserInfo", "name": self.username},
                timeout=6.0,
            )
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("User permission check skipped: %s", exc)
            return {
                "checked": False,
                "username": self.username,
                "has_all_permissions": True,
                "sharable": True,
                "missing_permissions": [],
            }

        if resp.status_code == 403 or (resp.status_code == 200 and "Error" in resp.text):
            return {
                "checked": True,
                "username": self.username,
                "group": "user",
                "is_admin": False,
                "sharable": True,
                "authorities_count": 0,
                "missing_system_permissions": ["AuthUserMag", *required_system],
                "missing_channel_permissions": [],
                "missing_permissions": ["AuthUserMag", *required_system],
                "has_all_permissions": False,
            }

        if resp.status_code != 200 or "user." not in resp.text:
            return {
                "checked": False,
                "username": self.username,
                "has_all_permissions": True,
                "sharable": True,
                "missing_permissions": [],
            }

        group = ""
        sharable = True
        reserved = False
        authorities: set[str] = set()
        auth_pat = re.compile(r"^user\.AuthorityList\[\d+\]=(.+)$")

        for line in resp.text.splitlines():
            line_str = line.strip()
            if line_str.startswith("user.Group="):
                group = line_str.split("=", 1)[1].strip()
            elif line_str.startswith("user.Sharable="):
                sharable = line_str.split("=", 1)[1].strip().lower() == "true"
            elif line_str.startswith("user.Reserved="):
                reserved = line_str.split("=", 1)[1].strip().lower() == "true"
            else:
                m = auth_pat.match(line_str)
                if m:
                    perm = m.group(1).strip()
                    if perm:
                        authorities.add(perm)

        is_admin = group.lower() == "admin"
        missing_sys: list[str] = []
        missing_ch: list[str] = []

        if authorities or not is_admin:
            for req in required_system:
                if req not in authorities:
                    missing_sys.append(req)
            for ch in sorted(channels or []):
                mon_key = f"Monitor_{ch + 1:02d}"
                rep_key = f"Replay_{ch + 1:02d}"
                if mon_key not in authorities:
                    missing_ch.append(mon_key)
                if rep_key not in authorities:
                    missing_ch.append(rep_key)

        missing_all = missing_sys + missing_ch
        return {
            "checked": True,
            "username": self.username,
            "group": group or ("admin" if is_admin else "user"),
            "is_admin": is_admin,
            "reserved": reserved,
            "sharable": sharable,
            "authorities_count": len(authorities),
            "missing_system_permissions": missing_sys,
            "missing_channel_permissions": missing_ch,
            "missing_permissions": missing_all,
            "has_all_permissions": len(missing_all) == 0,
        }

    async def get_network_interfaces(self) -> list[dict[str, Any]]:
        """Query network interface status, link speed and MAC address via netApp.cgi."""
        resp = await self._get(f"{NET_APP_CGI}?action=getInterfaces")
        if resp.status_code != 200:
            return []

        net_map: dict[int, dict[str, Any]] = {}
        for line in resp.text.splitlines():
            line = line.strip()
            if not line or "=" not in line:
                continue
            k, v = line.split("=", 1)
            m = re.match(r"netInterface\[(\d+)\]\.(\w+)", k.strip())
            if m:
                idx = int(m.group(1))
                attr = m.group(2)
                entry = net_map.setdefault(idx, {})
                if attr == "Speed":
                    try:
                        entry["speed"] = int(v.strip())
                    except ValueError:
                        entry["speed"] = 0
                elif attr == "ConnStatus":
                    entry["connected"] = (v.strip().lower() == "connected")
                    entry["status"] = v.strip()
                elif attr == "PhysicalAddress":
                    entry["mac_address"] = v.strip()
                elif attr == "Name":
                    entry["name"] = v.strip()
                elif attr == "Type":
                    entry["type"] = v.strip()

        return [net_map[i] for i in sorted(net_map.keys())]

    async def get_system_time(self) -> str | None:
        """Query the current date/time on the DVR clock via global.cgi."""
        resp = await self._get(f"{GLOBAL_CGI}?action=getCurrentTime")
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if line.startswith("result="):
                    return line.split("=", 1)[1].strip()
        return None

    async def query_logs(
        self,
        log_type: str = "All",
        count: int = 20,
        start_time: str | None = None,
        end_time: str | None = None,
    ) -> list[dict[str, Any]]:
        """Query audit and event logs from the DVR via log.cgi."""
        import datetime
        now = datetime.datetime.now()
        st = start_time or (now - datetime.timedelta(days=1)).strftime("%Y-%m-%d %H:%M:%S")
        et = end_time or now.strftime("%Y-%m-%d %H:%M:%S")
        count = min(max(1, count), 100)

        r_start = await self._get(
            f"{LOG_CGI}?action=startFind&condition.Type={log_type}&condition.StartTime={st}&condition.EndTime={et}"
        )
        if r_start.status_code != 200:
            return []

        token = None
        for line in r_start.text.splitlines():
            if line.startswith("token="):
                token = line.split("=", 1)[1].strip()
                break

        if not token:
            return []

        logs: list[dict[str, Any]] = []
        try:
            r_find = await self._get(
                f"{LOG_CGI}?action=doFind&token={token}&count={count}"
            )
            if r_find.status_code == 200:
                item_map: dict[int, dict[str, str]] = {}
                for line in r_find.text.splitlines():
                    line = line.strip()
                    if not line or "=" not in line:
                        continue
                    k, v = line.split("=", 1)
                    m = re.match(r"items\[(\d+)\]\.(\w+)", k.strip())
                    if m:
                        idx = int(m.group(1))
                        attr = m.group(2)
                        item = item_map.setdefault(idx, {})
                        item[attr] = v.strip()

                for idx in sorted(item_map.keys()):
                    it = item_map[idx]
                    logs.append({
                        "index": idx,
                        "time": it.get("Time", ""),
                        "type": it.get("Type", ""),
                        "user": it.get("User", ""),
                        "detail": it.get("Detail", ""),
                    })
        finally:
            try:
                await self._get(f"{LOG_CGI}?action=stopFind&token={token}")
            except Exception:  # noqa: BLE001
                pass

        return logs

    # ------------------------------------------------------------------
    # Monitor / Split output
    # ------------------------------------------------------------------

    async def get_monitor_mode(self, channel: int = 1) -> dict[str, Any]:
        """Return the current split mode for the physical monitor output."""
        resp = await self._get(f"{SPLIT_CGI}?action=getMode&channel={channel}")
        mode_info: dict[str, Any] = {}
        if resp.status_code == 200:
            for line in resp.text.splitlines():
                if "=" in line:
                    k, v = line.strip().split("=", 1)
                    k_clean = k.strip()
                    v_clean = v.strip()
                    if k_clean == "group" and v_clean.isdigit():
                        mode_info[k_clean] = int(v_clean)
                    else:
                        mode_info[k_clean] = v_clean
        return mode_info

    async def set_monitor_split(self, mode: str, group: int = 0, channel: int = 1) -> bool:
        """Set display split mode on physical monitor output with verification and retry."""
        async with self._monitor_lock:
            for attempt in range(1, 4):
                try:
                    resp = await self._get(
                        SPLIT_CGI,
                        params={
                            "action": "setMode",
                            "channel": channel,
                            "mode": mode,
                            "group": group,
                        },
                        timeout=6.0,
                    )
                    if resp.status_code == 200 and "OK" in resp.text:
                        await asyncio.sleep(0.15)
                        try:
                            actual = await self.get_monitor_mode(channel=channel)
                            actual_mode = str(actual.get("mode", "")).strip()
                            actual_group = actual.get("group", 0)
                            if not actual_mode or (
                                actual_mode == mode
                                and (mode != "split1" or actual_group == group)
                            ):
                                return True
                            _LOGGER.warning(
                                "Monitor split verification mismatch (attempt %d/3): requested %s:%d, DVR reported %s:%s",
                                attempt,
                                mode,
                                group,
                                actual_mode,
                                actual_group,
                            )
                        except Exception:  # noqa: BLE001
                            return True
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.warning(
                        "Failed to set monitor split to %s:%d (attempt %d/3): %s",
                        mode,
                        group,
                        attempt,
                        exc,
                    )
                if attempt < 3:
                    await asyncio.sleep(0.35 * attempt)
            return False

    # ------------------------------------------------------------------
    # Privacy masking
    # ------------------------------------------------------------------

    def save_camera_colors(self, channel: int, colors: dict[str, Any] | None) -> None:
        """Save camera picture settings before privacy masking. Protect against saving 0 values."""
        if not colors:
            return
        b = int(colors.get("Brightness", 0))
        s = int(colors.get("Saturation", 0))
        if b == 0 and s == 0:
            return
        self._saved_video_colors[channel] = dict(colors)
        prof_map = self._video_color_by_profile.get(channel, {})
        for prof, p_colors in prof_map.items():
            pb = int(p_colors.get("Brightness", 0))
            ps = int(p_colors.get("Saturation", 0))
            if pb != 0 or ps != 0:
                self._saved_video_colors_by_profile.setdefault(channel, {})[prof] = dict(p_colors)

    def get_saved_camera_colors(
        self, channel: int, profile: int | None = None
    ) -> dict[str, int]:
        """Retrieve saved camera picture settings before privacy masking."""
        default_sat = (
            DEFAULT_SATURATION_ACUTANCE
            if self.channel_supports_acutance(channel)
            else DEFAULT_SATURATION_ANALOG
        )
        fallback = {
            "Brightness": DEFAULT_BRIGHTNESS,
            "Contrast": DEFAULT_CONTRAST,
            "Saturation": default_sat,
            "Hue": DEFAULT_HUE,
            "Acutance": DEFAULT_ACUTANCE,
        }
        if profile is not None:
            prof_saved = self._saved_video_colors_by_profile.get(channel, {}).get(profile)
            if prof_saved and (
                int(prof_saved.get("Brightness", 0)) != 0
                or int(prof_saved.get("Saturation", 0)) != 0
            ):
                return dict(prof_saved)
        saved = self._saved_video_colors.get(channel)
        if not saved:
            return fallback
        if int(saved.get("Brightness", 0)) == 0 and int(saved.get("Saturation", 0)) == 0:
            return fallback
        return dict(saved)

    async def mask_channels(self, channels: list[int], mask: bool = True) -> bool:
        """Black out (or restore) one or more channels using fast batched picture settings."""
        if not channels:
            return True

        if mask:
            missing_channels = [ch for ch in channels if ch not in self._saved_video_colors]
            if missing_channels:
                try:
                    current_colors = await self.get_video_color()
                    for ch in missing_channels:
                        if ch in current_colors:
                            self.save_camera_colors(ch, current_colors[ch])
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Could not pre-fetch current colors before mask: %s", exc)
            for ch in channels:
                for prof, p_colors in self._video_color_by_profile.get(ch, {}).items():
                    pb = int(p_colors.get("Brightness", 0))
                    ps = int(p_colors.get("Saturation", 0))
                    if pb != 0 or ps != 0:
                        self._saved_video_colors_by_profile.setdefault(ch, {})[prof] = dict(p_colors)

            params: dict[str, Any] = {"action": "setConfig"}
            for ch in channels:
                profs = self.get_channel_profiles(ch)
                for prof in profs:
                    prefix = f"VideoColor[{ch}][{prof}]"
                    params[f"{prefix}.Brightness"] = 0
                    params[f"{prefix}.Contrast"] = 0
                    params[f"{prefix}.Saturation"] = 0
                    params[f"{prefix}.Hue"] = 0
                    if self.channel_supports_acutance(ch):
                        params[f"{prefix}.Acutance"] = 0

            resp = await self._get(CONFIG_MANAGER_CGI, params=params)
            return resp.status_code == 200 and "OK" in resp.text

        params = {"action": "setConfig"}
        for ch in channels:
            profs = self.get_channel_profiles(ch)
            for prof in profs:
                saved = self.get_saved_camera_colors(ch, profile=prof)
                b = saved.get("Brightness", DEFAULT_BRIGHTNESS)
                c = saved.get("Contrast", DEFAULT_CONTRAST)
                s = saved.get(
                    "Saturation",
                    DEFAULT_SATURATION_ACUTANCE
                    if self.channel_supports_acutance(ch)
                    else DEFAULT_SATURATION_ANALOG,
                )
                h = saved.get("Hue", DEFAULT_HUE)
                a = saved.get("Acutance", DEFAULT_ACUTANCE)
                prefix = f"VideoColor[{ch}][{prof}]"
                params[f"{prefix}.Brightness"] = b
                params[f"{prefix}.Contrast"] = c
                params[f"{prefix}.Saturation"] = s
                params[f"{prefix}.Hue"] = h
                if self.channel_supports_acutance(ch):
                    params[f"{prefix}.Acutance"] = a

        resp = await self._get(CONFIG_MANAGER_CGI, params=params)
        return resp.status_code == 200 and "OK" in resp.text

    # ------------------------------------------------------------------
    # Recording modes
    # ------------------------------------------------------------------

    async def get_record_modes(self, channels: list[int]) -> dict[int, int]:
        """Return the current RecordMode for each channel (0=auto/schedule, 1=manual, 2=off)."""
        resp = await self._get(
            CONFIG_MANAGER_CGI,
            params={"action": "getConfig", "name": "RecordMode"},
        )
        modes: dict[int, int] = {}
        if resp.status_code == 200:
            pattern = re.compile(r"table\.RecordMode\[(\d+)\]\.Mode=(\d+)")
            for line in resp.text.splitlines():
                match = pattern.match(line.strip())
                if match:
                    ch = int(match.group(1))
                    modes[ch] = int(match.group(2))
        for ch in channels:
            modes.setdefault(ch, RECORD_MODE_SCHEDULE)
        return modes

    async def set_record_mode(self, channel: int, mode: int) -> bool:
        """Set the recording mode for a channel (0=schedule, 1=manual, 2=off)."""
        resp = await self._get(
            CONFIG_MANAGER_CGI,
            params={
                "action": "setConfig",
                f"RecordMode[{channel}].Mode": mode,
            },
        )
        return resp.status_code == 200 and "OK" in resp.text

    # ------------------------------------------------------------------
    # Batch config
    # ------------------------------------------------------------------

    async def get_batch_config(
        self,
        channels: list[int],
        *,
        include_extended: bool = True,
    ) -> tuple[
        dict[int, dict[str, int]],
        dict[int, int],
        dict[int, dict[str, Any]],
        dict[int, dict[str, Any]],
        dict[int, dict[str, Any]],
    ]:
        """Fetch DVR configuration tables in a single HTTP request.

        When ``include_extended`` is False, only lightweight local DVR tables
        (VideoColor, RecordMode, Encode) are queried to avoid synchronous RPC
        calls to remote IP cameras on every 60-second poll cycle.
        When ``include_extended`` is True, VideoWidget, VideoInOptions,
        VideoInDenoise, and ImageEnhancement are also queried, with automatic
        fallback to the lightweight local tables if an IP camera RPC times out.
        """
        base_tables = ["VideoColor", "RecordMode", "Encode"]
        extended_tables = [
            "VideoColor",
            "RecordMode",
            "Encode",
            "VideoWidget",
            "VideoInOptions",
            "VideoInDenoise",
            "ImageEnhancement",
        ]
        used_extended = include_extended
        if include_extended:
            try:
                resp = await self._get(
                    CONFIG_MANAGER_CGI,
                    params={"action": "getConfig", "name": extended_tables},
                )
            except DVRConnectionError as exc:
                _LOGGER.warning(
                    "Extended DVR config query timed out/failed (%s); falling back to lightweight local tables.",
                    exc,
                )
                used_extended = False
                resp = await self._get(
                    CONFIG_MANAGER_CGI,
                    params={"action": "getConfig", "name": base_tables},
                )
        else:
            resp = await self._get(
                CONFIG_MANAGER_CGI,
                params={"action": "getConfig", "name": base_tables},
            )
        if resp.status_code != 200:
            return (
                self._video_color,
                {ch: RECORD_MODE_SCHEDULE for ch in channels},
                self._encode_config,
                self._video_widget,
                self._video_in_options,
            )

        colors: dict[int, dict[str, int]] = {}
        modes: dict[int, int] = {}
        video_widget: dict[int, dict[str, Any]] = {}
        video_in_options: dict[int, dict[str, Any]] = {}

        vc_pattern = re.compile(
            r"table\.VideoColor\[(\d+)\](?:\[(\d+)\])?\.([A-Za-z]+)=(\d+)"
        )
        vc_style_pattern = re.compile(
            r"table\.VideoColor\[(\d+)\](?:\[(\d+)\])?\.Style=([A-Za-z]+)"
        )
        ts_pattern = re.compile(
            r"table\.VideoColor\[(\d+)\]\[(\d+)\]\.TimeSection=(\d+)\s+(\d{2}):(\d{2}):(\d{2})-(\d{2}):(\d{2}):(\d{2})"
        )
        acutance_pattern = re.compile(r"table\.VideoColor\[(\d+)\].*Acutance=(\d+)")
        rm_pattern = re.compile(r"table\.RecordMode\[(\d+)\]\.Mode=(\d+)")

        vw_custom_text = re.compile(
            r"table\.VideoWidget\[(\d+)\]\.CustomTitle\[(\d+)\]\.Text=(.*)"
        )
        vw_custom_blend = re.compile(
            r"table\.VideoWidget\[(\d+)\]\.CustomTitle\[(\d+)\]\.EncodeBlend=(true|false)",
            re.IGNORECASE,
        )
        vw_channel_blend = re.compile(
            r"table\.VideoWidget\[(\d+)\]\.ChannelTitle\.EncodeBlend=(true|false)",
            re.IGNORECASE,
        )
        vw_time_blend = re.compile(
            r"table\.VideoWidget\[(\d+)\]\.TimeTitle\.EncodeBlend=(true|false)",
            re.IGNORECASE,
        )

        vio_bare_dn = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.DayNightColor=(\d+)"
        )
        vio_norm_dn = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NormalOptions\.DayNightColor=(\d+)"
        )
        vio_night_dn = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NightOptions\.DayNightColor=(\d+)"
        )
        vio_switch_mode = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.(?:NightOptions\.)?SwitchMode=(\d+)"
        )
        vio_sunset_hour = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NightOptions\.SunsetHour=(\d+)"
        )
        vio_sunset_min = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NightOptions\.SunsetMinute=(\d+)"
        )
        vio_sunrise_hour = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NightOptions\.SunriseHour=(\d+)"
        )
        vio_sunrise_min = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NightOptions\.SunriseMinute=(\d+)"
        )
        vio_bare_wdr = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.WideDynamicRangeMode=(\d+)"
        )
        vio_norm_wdr = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NormalOptions\.WideDynamicRangeMode=(\d+)"
        )
        vio_night_wdr = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NightOptions\.WideDynamicRangeMode=(\d+)"
        )
        vio_bare_wdr_val = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.WideDynamicRange=(\d+)"
        )
        vio_bare_bl = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.Backlight=(\d+)"
        )
        vio_norm_bl = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NormalOptions\.Backlight=(\d+)"
        )
        vio_night_bl = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NightOptions\.Backlight=(\d+)"
        )
        vio_bare_gi = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.GlareInhibition=(\d+)"
        )
        vio_norm_gi = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NormalOptions\.GlareInhibition=(\d+)"
        )
        vio_night_gi = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NightOptions\.GlareInhibition=(\d+)"
        )
        vio_bare_gain = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.Gain=(\d+)"
        )
        vio_bare_gain_auto = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.GainAuto=(true|false)",
            re.IGNORECASE,
        )
        vio_night_gain_auto = re.compile(
            r"table\.VideoInOptions\[(\d+)\]\.NightOptions\.GainAuto=(true|false)",
            re.IGNORECASE,
        )

        denoise_pattern = re.compile(
            r"table\.VideoInDenoise\[(\d+)\](?:\[(\d+)\])?\.2DLevel=(\d+)"
        )
        enhancement_pattern = re.compile(
            r"table\.ImageEnhancement\[(\d+)\](?:\[(\d+)\])?\.Level=(\d+)"
        )

        for line in resp.text.splitlines():
            line_str = line.strip()

            ac_match = acutance_pattern.match(line_str)
            if ac_match:
                ch = int(ac_match.group(1))
                self._channel_supports_acutance.add(ch)

            ts_m = ts_pattern.match(line_str)
            if ts_m:
                ch = int(ts_m.group(1))
                period = int(ts_m.group(2))
                active = int(ts_m.group(3)) == 1
                sh, sm, ss = int(ts_m.group(4)), int(ts_m.group(5)), int(ts_m.group(6))
                eh, em, es = int(ts_m.group(7)), int(ts_m.group(8)), int(ts_m.group(9))
                if sh >= 24:
                    sh, sm, ss = 23, 59, 59
                if eh >= 24:
                    eh, em, es = 23, 59, 59
                start = dt_time(sh, sm, ss)
                end = dt_time(eh, em, es)
                self._video_color_periods.setdefault(ch, {})[period] = {
                    "active": active,
                    "start": start,
                    "end": end,
                }
                continue

            vc_match = vc_pattern.match(line_str)
            if vc_match:
                ch = int(vc_match.group(1))
                profile = int(vc_match.group(2)) if vc_match.group(2) is not None else 0
                prop = vc_match.group(3)
                val = int(vc_match.group(4))

                self._channel_profiles.setdefault(ch, set()).add(profile)
                self._video_color_by_profile.setdefault(ch, {}).setdefault(profile, {})[prop] = val
                if profile == 0:
                    colors.setdefault(ch, {})[prop] = val
                continue

            m_dn = denoise_pattern.match(line_str)
            if m_dn:
                ch = int(m_dn.group(1))
                prof = int(m_dn.group(2)) if m_dn.group(2) is not None else 0
                val = int(m_dn.group(3))
                self._video_denoise.setdefault(ch, {})["2d_level"] = val
                self._video_denoise.setdefault(ch, {}).setdefault("profiles", {})[prof] = val
                continue

            m_en = enhancement_pattern.match(line_str)
            if m_en:
                ch = int(m_en.group(1))
                prof = int(m_en.group(2)) if m_en.group(2) is not None else 0
                val = int(m_en.group(3))
                self._image_enhancement.setdefault(ch, {})["level"] = val
                self._image_enhancement.setdefault(ch, {}).setdefault("profiles", {})[prof] = val
                continue

            m_bl = vio_bare_bl.match(line_str)
            if m_bl:
                video_in_options.setdefault(int(m_bl.group(1)), {})["backlight"] = int(m_bl.group(2))
                continue

            m_bl_n = vio_norm_bl.match(line_str)
            if m_bl_n:
                video_in_options.setdefault(int(m_bl_n.group(1)), {})["normal_backlight"] = int(m_bl_n.group(2))
                video_in_options.setdefault(int(m_bl_n.group(1)), {})["has_profile_options"] = True
                continue

            m_bl_ni = vio_night_bl.match(line_str)
            if m_bl_ni:
                video_in_options.setdefault(int(m_bl_ni.group(1)), {})["night_backlight"] = int(m_bl_ni.group(2))
                video_in_options.setdefault(int(m_bl_ni.group(1)), {})["has_profile_options"] = True
                continue

            m_gi = vio_bare_gi.match(line_str)
            if m_gi:
                video_in_options.setdefault(int(m_gi.group(1)), {})["glare_inhibition"] = int(m_gi.group(2))
                continue

            m_gi_n = vio_norm_gi.match(line_str)
            if m_gi_n:
                video_in_options.setdefault(int(m_gi_n.group(1)), {})["normal_glare_inhibition"] = int(m_gi_n.group(2))
                video_in_options.setdefault(int(m_gi_n.group(1)), {})["has_profile_options"] = True
                continue

            m_gi_ni = vio_night_gi.match(line_str)
            if m_gi_ni:
                video_in_options.setdefault(int(m_gi_ni.group(1)), {})["night_glare_inhibition"] = int(m_gi_ni.group(2))
                video_in_options.setdefault(int(m_gi_ni.group(1)), {})["has_profile_options"] = True
                continue

            m_style = vc_style_pattern.match(line_str)
            if m_style:
                ch = int(m_style.group(1))
                colors.setdefault(ch, {})["picture_style"] = m_style.group(3).lower()
                continue

            rm_match = rm_pattern.match(line_str)
            if rm_match:
                ch = int(rm_match.group(1))
                modes[ch] = int(rm_match.group(2))
                continue

            m = vw_custom_text.match(line_str)
            if m:
                ch = int(m.group(1))
                idx = int(m.group(2))
                if idx == 0:
                    video_widget.setdefault(ch, {})["custom_title_text"] = m.group(3)
                continue

            m = vw_custom_blend.match(line_str)
            if m:
                ch = int(m.group(1))
                idx = int(m.group(2))
                if idx == 0:
                    video_widget.setdefault(ch, {})["custom_title_blend"] = m.group(3).lower() == "true"
                continue

            m = vw_channel_blend.match(line_str)
            if m:
                ch = int(m.group(1))
                video_widget.setdefault(ch, {})["channel_title_blend"] = m.group(2).lower() == "true"
                continue

            m = vw_time_blend.match(line_str)
            if m:
                ch = int(m.group(1))
                video_widget.setdefault(ch, {})["time_title_blend"] = m.group(2).lower() == "true"
                continue

            m = vio_bare_dn.match(line_str)
            if m:
                ch = int(m.group(1))
                val = int(m.group(2))
                video_in_options.setdefault(ch, {})["day_night_color"] = val
                video_in_options.setdefault(ch, {})["day_night"] = val
                continue

            m = vio_norm_dn.match(line_str)
            if m:
                ch = int(m.group(1))
                val = int(m.group(2))
                video_in_options.setdefault(ch, {})["normal_day_night_color"] = val
                video_in_options.setdefault(ch, {})["has_profile_options"] = True
                continue

            m = vio_night_dn.match(line_str)
            if m:
                ch = int(m.group(1))
                val = int(m.group(2))
                video_in_options.setdefault(ch, {})["night_day_night_color"] = val
                video_in_options.setdefault(ch, {})["has_profile_options"] = True
                continue

            m = vio_switch_mode.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["switch_mode"] = int(m.group(2))
                continue

            m = vio_sunset_hour.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["sunset_hour"] = int(m.group(2))
                continue

            m = vio_sunset_min.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["sunset_minute"] = int(m.group(2))
                continue

            m = vio_sunrise_hour.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["sunrise_hour"] = int(m.group(2))
                continue

            m = vio_sunrise_min.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["sunrise_minute"] = int(m.group(2))
                continue

            m = vio_bare_wdr.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["wdr_mode"] = int(m.group(2))
                continue

            m = vio_norm_wdr.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["normal_wdr_mode"] = int(m.group(2))
                video_in_options.setdefault(ch, {})["has_profile_options"] = True
                continue

            m = vio_night_wdr.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["night_wdr_mode"] = int(m.group(2))
                video_in_options.setdefault(ch, {})["has_profile_options"] = True
                continue

            m = vio_bare_wdr_val.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["wdr_val"] = int(m.group(2))
                continue

            m = vio_bare_gain.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["gain"] = int(m.group(2))
                continue

            m = vio_bare_gain_auto.match(line_str)
            if m:
                ch = int(m.group(1))
                video_in_options.setdefault(ch, {})["gain_auto"] = m.group(2).lower() == "true"
                self._video_in_options.setdefault(ch, {})["gain_auto"] = m.group(2).lower() == "true"
                continue

            m_nga = vio_night_gain_auto.match(line_str)
            if m_nga:
                ch = int(m_nga.group(1))
                video_in_options.setdefault(ch, {})["night_gain_auto"] = m_nga.group(2).lower() == "true"
                self._video_in_options.setdefault(ch, {})["night_gain_auto"] = m_nga.group(2).lower() == "true"
                continue

        self._parse_encode_config(resp.text)
        encode_info = self._encode_config

        for ch in channels:
            modes.setdefault(ch, RECORD_MODE_SCHEDULE)

        if used_extended:
            self._video_widget = video_widget
            self._video_in_options = video_in_options
        else:
            video_widget = self._video_widget
            video_in_options = self._video_in_options

        for ch in channels:
            active_p = self.get_active_profile_index(ch)
            if ch in self._video_color_by_profile and active_p in self._video_color_by_profile[ch]:
                colors[ch] = dict(self._video_color_by_profile[ch][active_p])

        self._video_color = colors
        return colors, modes, encode_info, video_widget, video_in_options

    async def set_custom_title_text(
        self, channel: int, text: str, index: int = 0
    ) -> bool:
        """Set custom OSD text on the camera channel (VideoWidget)."""
        encoded_text = urllib.parse.quote(text, safe="")
        resp = await self._get(
            f"{CONFIG_MANAGER_CGI}?action=setConfig&VideoWidget[{channel}].CustomTitle[{index}].Text={encoded_text}",
            timeout=6.0,
        )
        return resp.status_code == 200 and "ok" in resp.text.lower()

    async def set_video_widget_overlay(
        self, channel: int, widget_type: str, enabled: bool, index: int = 0
    ) -> bool:
        """Toggle OSD overlay on or off on the camera stream."""
        blend_str = "true" if enabled else "false"
        if widget_type == "custom_title":
            key = f"VideoWidget[{channel}].CustomTitle[{index}].EncodeBlend"
        elif widget_type == "channel_title":
            key = f"VideoWidget[{channel}].ChannelTitle.EncodeBlend"
        elif widget_type == "time_title":
            key = f"VideoWidget[{channel}].TimeTitle.EncodeBlend"
        else:
            return False

        resp = await self._get(
            CONFIG_MANAGER_CGI,
            params={"action": "setConfig", key: blend_str},
            timeout=6.0,
        )
        return resp.status_code == 200 and "ok" in resp.text.lower()

    def get_day_night_mode(self, channel: int, profile: int | None = None) -> str:
        """Return Day/Night mode string ('black_white', 'color', 'schedule') for a channel and profile.

        Profile mapping on Intelbras/Dahua DVR:
        - 0 (Day): Root VideoInOptions[ch].DayNightColor
        - 1 (Night): VideoInOptions[ch].NightOptions.DayNightColor
        - 2 (Regular): VideoInOptions[ch].NormalOptions.DayNightColor
        """
        vio = self._video_in_options.get(channel, {})
        has_profiles = vio.get("has_profile_options", False)
        prof = profile if profile is not None else self.get_active_profile_index(channel)

        if has_profiles:
            if prof == 1:
                val = vio.get("night_day_night_color", vio.get("day_night_color", 1))
            elif prof == 2:
                val = vio.get("normal_day_night_color", vio.get("day_night_color", 1))
            else:
                val = vio.get("day_night_color", vio.get("day_night", 1))
        else:
            val = vio.get("day_night_color", vio.get("day_night", 1))

        color_map = {0: "color", 1: "schedule", 2: "black_white"}
        return color_map.get(val, "schedule")

    async def set_day_night_mode(
        self, channel: int, mode: str, profile: int | None = None
    ) -> bool:
        """Set Day/Night mode for a camera channel (0=Color, 1=Schedule, 2=BlackWhite) and specific profile.

        Profile mapping on Intelbras/Dahua DVR:
        - 0 (Day): Root VideoInOptions[ch].DayNightColor
        - 1 (Night): VideoInOptions[ch].NightOptions.DayNightColor
        - 2 (Regular): VideoInOptions[ch].NormalOptions.DayNightColor
        """
        mode_map = {"color": 0, "schedule": 1, "auto": 1, "black_white": 2}
        val = mode_map.get(mode.lower(), 1)
        vio_ch = self._video_in_options.setdefault(channel, {})
        has_profiles = vio_ch.get("has_profile_options", False)
        prof = profile if profile is not None else self.get_active_profile_index(channel)

        params: dict[str, Any] = {"action": "setConfig"}
        if has_profiles:
            if prof == 1:
                params[f"VideoInOptions[{channel}].NightOptions.DayNightColor"] = val
            elif prof == 2:
                params[f"VideoInOptions[{channel}].NormalOptions.DayNightColor"] = val
            else:
                params[f"VideoInOptions[{channel}].DayNightColor"] = val
        else:
            params[f"VideoInOptions[{channel}].DayNightColor"] = val

        resp = await self._get(
            CONFIG_MANAGER_CGI,
            params=params,
            timeout=6.0,
        )
        if resp.status_code == 200 and "ok" in resp.text.lower():
            if has_profiles:
                if prof == 1:
                    vio_ch["night_day_night_color"] = val
                elif prof == 2:
                    vio_ch["normal_day_night_color"] = val
                else:
                    vio_ch["day_night_color"] = val
                    vio_ch["day_night"] = val
            else:
                vio_ch["day_night_color"] = val
                vio_ch["day_night"] = val
            return True
        return False

    def get_active_profile_index(self, channel: int) -> int:
        """Return the active profile index for a channel.

        For analog channels: 0 = Period 1, 1 = Period 2.
        For IP channels: 0 = Day/Normal, 1 = Night, 2 = General.
        """
        if self.channel_is_analog(channel):
            periods = self._video_color_periods.get(channel, {})
            now_time = datetime.now().time()
            p0 = periods.get(0, {})
            p1 = periods.get(1, {})
            if p0.get("active", False):
                s, e = p0.get("start"), p0.get("end")
                if s and e:
                    if s <= e and s <= now_time <= e:
                        return 0
                    elif s > e and (now_time >= s or now_time <= e):
                        return 0
            if p1.get("active", False):
                s, e = p1.get("start"), p1.get("end")
                if s and e:
                    if s <= e and s <= now_time <= e:
                        return 1
                    elif s > e and (now_time >= s or now_time <= e):
                        return 1
            return 0

        vio = self._video_in_options.get(channel, {})
        switch_mode = vio.get("switch_mode", 0)
        if switch_mode == 3:  # Fixed Night
            return 1
        elif switch_mode == 0:  # Regular / Fixed Day
            return 0
        elif switch_mode == 2:  # Schedule
            now_time = datetime.now().time()
            sh = vio.get("sunrise_hour", 6)
            sm = vio.get("sunrise_minute", 0)
            eh = vio.get("sunset_hour", 18)
            em = vio.get("sunset_minute", 0)
            day_start = dt_time(sh, sm)
            day_end = dt_time(eh, em)
            if day_start <= day_end and day_start <= now_time < day_end:
                return 0
            elif day_start > day_end and (now_time >= day_start or now_time < day_end):
                return 0
            return 1
        elif switch_mode == 1:  # Day/Night sensor
            dn = vio.get("day_night_color", 0)
            return 1 if dn == 1 else 0
        return 0

    def get_light_compensation_mode(
        self, channel: int, profile: int | None = None
    ) -> str:
        """Return the current light compensation mode for a camera channel and profile.

        Possible values: 'disabled', 'blc', 'hlc', 'wdr'.
        Profile mapping on Intelbras/Dahua DVR:
        - 0 (Day / Period 1): Root VideoInOptions[ch].*
        - 1 (Night / Period 2): VideoInOptions[ch].NightOptions.*
        - 2 (Regular): VideoInOptions[ch].NormalOptions.*
        """
        vio = self._video_in_options.get(channel, {})
        has_profiles = vio.get("has_profile_options", False)
        prof = profile if profile is not None else self.get_active_profile_index(channel)

        if has_profiles:
            if prof == 1:
                wdr = vio.get("night_wdr_mode", 0)
                hlc = vio.get("night_glare_inhibition", 0)
                blc = vio.get("night_backlight", 0)
            elif prof == 2:
                wdr = vio.get("normal_wdr_mode", 0)
                hlc = vio.get("normal_glare_inhibition", 0)
                blc = vio.get("normal_backlight", 0)
            else:
                wdr = vio.get("wdr_mode", 0)
                hlc = vio.get("glare_inhibition", 0)
                blc = vio.get("backlight", 0)
        else:
            wdr = vio.get("wdr_mode", 0)
            hlc = vio.get("glare_inhibition", 0)
            blc = vio.get("backlight", 0)

        if wdr > 0:
            return "wdr"
        if hlc > 0:
            return "hlc"
        if blc > 0:
            return "blc"
        return "disabled"

    async def set_light_compensation_mode(
        self, channel: int, mode: str, profile: int | None = None
    ) -> bool:
        """Set light compensation mode ('disabled', 'blc', 'hlc', 'wdr') for a camera channel and profile."""
        mode = mode.lower()
        vio_ch = self._video_in_options.setdefault(channel, {})
        has_profiles = vio_ch.get("has_profile_options", False)
        prof = profile if profile is not None else self.get_active_profile_index(channel)

        blc_val = 1 if mode == "blc" else 0
        hlc_val = 50 if mode == "hlc" else 0
        wdr_mode = 1 if mode == "wdr" else 0
        wdr_val = 50 if mode == "wdr" else 0

        params: dict[str, Any] = {"action": "setConfig"}
        if has_profiles:
            if prof == 1:  # Night / Period 2
                params[f"VideoInOptions[{channel}].NightOptions.Backlight"] = blc_val
                params[f"VideoInOptions[{channel}].NightOptions.GlareInhibition"] = hlc_val
                params[f"VideoInOptions[{channel}].NightOptions.WideDynamicRangeMode"] = wdr_mode
                params[f"VideoInOptions[{channel}].NightOptions.WideDynamicRange"] = wdr_val
            elif prof == 2:  # Regular
                params[f"VideoInOptions[{channel}].NormalOptions.Backlight"] = blc_val
                params[f"VideoInOptions[{channel}].NormalOptions.GlareInhibition"] = hlc_val
                params[f"VideoInOptions[{channel}].NormalOptions.WideDynamicRangeMode"] = wdr_mode
                params[f"VideoInOptions[{channel}].NormalOptions.WideDynamicRange"] = wdr_val
            else:  # Day / Period 1
                params[f"VideoInOptions[{channel}].Backlight"] = blc_val
                params[f"VideoInOptions[{channel}].GlareInhibition"] = hlc_val
                params[f"VideoInOptions[{channel}].WideDynamicRangeMode"] = wdr_mode
                params[f"VideoInOptions[{channel}].WideDynamicRange"] = wdr_val
        else:
            params[f"VideoInOptions[{channel}].Backlight"] = blc_val
            params[f"VideoInOptions[{channel}].GlareInhibition"] = hlc_val
            params[f"VideoInOptions[{channel}].WideDynamicRangeMode"] = wdr_mode
            params[f"VideoInOptions[{channel}].WideDynamicRange"] = wdr_val

        resp = await self._get(
            CONFIG_MANAGER_CGI,
            params=params,
            timeout=6.0,
        )
        if resp.status_code == 200 and "ok" in resp.text.lower():
            if has_profiles:
                if prof == 1:
                    vio_ch["night_backlight"] = blc_val
                    vio_ch["night_glare_inhibition"] = hlc_val
                    vio_ch["night_wdr_mode"] = wdr_mode
                    vio_ch["night_wdr_val"] = wdr_val
                elif prof == 2:
                    vio_ch["normal_backlight"] = blc_val
                    vio_ch["normal_glare_inhibition"] = hlc_val
                    vio_ch["normal_wdr_mode"] = wdr_mode
                    vio_ch["normal_wdr_val"] = wdr_val
                else:
                    vio_ch["backlight"] = blc_val
                    vio_ch["glare_inhibition"] = hlc_val
                    vio_ch["wdr_mode"] = wdr_mode
                    vio_ch["wdr_val"] = wdr_val
            else:
                vio_ch["backlight"] = blc_val
                vio_ch["glare_inhibition"] = hlc_val
                vio_ch["wdr_mode"] = wdr_mode
                vio_ch["wdr_val"] = wdr_val
            return True
        return False

    async def set_wide_dynamic_range(
        self, channel: int, enabled: bool, level: int = 50
    ) -> bool:
        """Enable or disable Wide Dynamic Range (WDR) for a camera channel."""
        return await self.set_light_compensation_mode(channel, "wdr" if enabled else "disabled")

    async def set_ip_profile_mode(
        self, channel: int, mode_str: str, fixed_subprofile: str | None = None
    ) -> bool:
        """Set Profile management mode for an Intelbras IP camera channel.

        Supported modes:
        - 'regular': SwitchMode=0 (no sub-options)
        - 'fixed': SwitchMode=0 for Day, SwitchMode=3 for Night
        - 'schedule': SwitchMode=2 (schedule with sunrise/sunset hours)
        - 'day_night': SwitchMode=1 (automatic ambient sensor)
        """
        mode = mode_str.lower()
        if mode == "regular":
            val = 0
        elif mode == "fixed":
            val = 3 if fixed_subprofile == "night" else 0
        elif mode == "schedule":
            val = 2
        elif mode == "day_night":
            val = 1
        elif mode == "night":
            val = 3
        else:
            val = 0

        url = f"{CONFIG_MANAGER_CGI}?action=setConfig&VideoInOptions[{channel}].NightOptions.SwitchMode={val}"
        resp = await self._get(url, timeout=6.0)
        if resp.status_code == 200 and "ok" in resp.text.lower():
            vio_ch = self._video_in_options.setdefault(channel, {})
            vio_ch["switch_mode"] = val
            return True
        return False

    async def set_ip_fixed_profile(self, channel: int, profile: str) -> bool:
        """Set fixed profile (day or night) for an Intelbras IP camera channel."""
        val = 3 if profile.lower() == "night" else 0
        url = f"{CONFIG_MANAGER_CGI}?action=setConfig&VideoInOptions[{channel}].NightOptions.SwitchMode={val}"
        resp = await self._get(url, timeout=6.0)
        if resp.status_code == 200 and "ok" in resp.text.lower():
            vio_ch = self._video_in_options.setdefault(channel, {})
            vio_ch["switch_mode"] = val
            return True
        return False

    async def set_ip_schedule_times(
        self,
        channel: int,
        sunrise: dt_time | None = None,
        sunset: dt_time | None = None,
    ) -> bool:
        """Set day start (sunrise) and day end (sunset) schedule times for an Intelbras IP camera."""
        qs = ["action=setConfig"]
        vio_ch = self._video_in_options.setdefault(channel, {})
        if sunrise is not None:
            qs.append(f"VideoInOptions[{channel}].NightOptions.SunriseHour={sunrise.hour}")
            qs.append(f"VideoInOptions[{channel}].NightOptions.SunriseMinute={sunrise.minute}")
            qs.append(f"VideoInOptions[{channel}].NightOptions.SunriseSecond=0")
        if sunset is not None:
            qs.append(f"VideoInOptions[{channel}].NightOptions.SunsetHour={sunset.hour}")
            qs.append(f"VideoInOptions[{channel}].NightOptions.SunsetMinute={sunset.minute}")
            qs.append(f"VideoInOptions[{channel}].NightOptions.SunsetSecond=0")

        if len(qs) <= 1:
            return True

        url = f"{CONFIG_MANAGER_CGI}?" + "&".join(qs)
        resp = await self._get(url, timeout=6.0)
        if resp.status_code == 200 and "ok" in resp.text.lower():
            if sunrise is not None:
                vio_ch["sunrise_hour"] = sunrise.hour
                vio_ch["sunrise_minute"] = sunrise.minute
            if sunset is not None:
                vio_ch["sunset_hour"] = sunset.hour
                vio_ch["sunset_minute"] = sunset.minute
            return True
        return False

    def get_analog_periods(self, channel: int) -> dict[int, dict[str, Any]]:
        """Return parsed analog period intervals for a channel."""
        return self._video_color_periods.get(channel, {})

    def get_analog_period_active(self, channel: int, period_idx: int) -> bool:
        """Return True if the analog period is active."""
        periods = self._video_color_periods.get(channel, {})
        return periods.get(period_idx, {}).get("active", True if period_idx == 0 else False)

    async def set_analog_period_active(
        self,
        channel: int,
        period_idx: int,
        active: bool,
    ) -> bool:
        """Enable or disable Period 1 (idx 0) or Period 2 (idx 1) on an analog channel."""
        ch_periods = self._video_color_periods.setdefault(channel, {})
        p0 = ch_periods.get(0, {"active": True, "start": dt_time(6, 0), "end": dt_time(18, 0)})
        p1 = ch_periods.get(1, {"active": False, "start": dt_time(18, 0), "end": dt_time(6, 0)})

        p0_start = p0.get("start", dt_time(6, 0))
        p0_end = p0.get("end", dt_time(18, 0))
        p1_start = p1.get("start", dt_time(18, 0))
        p1_end = p1.get("end", dt_time(6, 0))

        if period_idx == 1:
            if active:
                # Turning on Period 2:
                # If Period 0 was 24 hours (default) or matches Period 1, set defaults: Day (06:00-18:00) and Night (18:00-06:00)
                is_24h = p0_start == dt_time(0, 0) and (
                    p0_end >= dt_time(23, 59, 58) or p0_start == p0_end
                )
                if is_24h or (p0_start == p1_start and p0_end == p1_end):
                    p0_start = dt_time(6, 0)
                    p0_end = dt_time(18, 0)
                elif p0_end >= dt_time(23, 59, 58):
                    p0_end = dt_time(18, 0)
                # Period 2 is always the complementary interval of Period 1
                p1_start = p0_end
                p1_end = p0_start
                p0_act = True
                p1_act = True
            else:
                # Turning off Period 2: Period 1 covers 24h on the DVR
                p0_act = True
                p1_act = False
                p0_start = dt_time(0, 0)
                p0_end = dt_time(23, 59, 59)
                p1_start = dt_time(0, 0)
                p1_end = dt_time(23, 59, 59)
        else:
            # Period 0 is the primary profile
            p0_act = True
            if not active:
                # Disabling primary period resets to a single 24h period and turns off Period 2
                p1_act = False
                p0_start = dt_time(0, 0)
                p0_end = dt_time(23, 59, 59)
                p1_start = dt_time(0, 0)
                p1_end = dt_time(23, 59, 59)
            else:
                p1_act = p1.get("active", False)
                if p1_act and p0_end >= dt_time(23, 59, 58):
                    p0_end = dt_time(18, 0)
                    p1_start = p0_end
                    p1_end = p0_start

        if not p1_act:
            p0_end_str = (
                "24:00:00"
                if (p0_end.hour == 23 and p0_end.minute == 59 and p0_end.second >= 58)
                else p0_end.strftime("%H:%M:%S")
            )
            p1_end_str = (
                "24:00:00"
                if (p1_end.hour == 23 and p1_end.minute == 59 and p1_end.second >= 58)
                else p1_end.strftime("%H:%M:%S")
            )
        else:
            p0_end_str = p0_end.strftime("%H:%M:%S")
            p1_end_str = p1_end.strftime("%H:%M:%S")

        p0_str = f"{1 if p0_act else 0}%20{p0_start.strftime('%H:%M:%S')}-{p0_end_str}"
        p1_str = f"{1 if p1_act else 0}%20{p1_start.strftime('%H:%M:%S')}-{p1_end_str}"

        prof_colors = self._video_color_by_profile.get(channel, {})
        base_colors = self._video_color.get(channel, {})
        c0 = prof_colors.get(0) or base_colors
        c1 = prof_colors.get(1) or c0

        b0 = c0.get("Brightness", DEFAULT_BRIGHTNESS)
        con0 = c0.get("Contrast", DEFAULT_CONTRAST)
        h0 = c0.get("Hue", DEFAULT_HUE)
        s0 = c0.get("Saturation", DEFAULT_SATURATION_ANALOG)
        a0 = c0.get("Acutance", DEFAULT_ACUTANCE)

        b1 = c1.get("Brightness", DEFAULT_BRIGHTNESS)
        con1 = c1.get("Contrast", DEFAULT_CONTRAST)
        h1 = c1.get("Hue", DEFAULT_HUE)
        s1 = c1.get("Saturation", DEFAULT_SATURATION_ANALOG)
        a1 = c1.get("Acutance", DEFAULT_ACUTANCE)

        qs = [
            "action=setConfig",
            f"VideoColor[{channel}][0].Brightness={b0}",
            f"VideoColor[{channel}][0].Contrast={con0}",
            f"VideoColor[{channel}][0].Hue={h0}",
            f"VideoColor[{channel}][0].Saturation={s0}",
            f"VideoColor[{channel}][0].Acutance={a0}",
            f"VideoColor[{channel}][0].TimeSection={p0_str}",
            f"VideoColor[{channel}][1].Brightness={b1}",
            f"VideoColor[{channel}][1].Contrast={con1}",
            f"VideoColor[{channel}][1].Hue={h1}",
            f"VideoColor[{channel}][1].Saturation={s1}",
            f"VideoColor[{channel}][1].Acutance={a1}",
            f"VideoColor[{channel}][1].TimeSection={p1_str}",
        ]
        url = f"{CONFIG_MANAGER_CGI}?" + "&".join(qs)
        resp = await self._get(url, timeout=6.0)
        if resp.status_code == 200 and "ok" in resp.text.lower():
            ch_periods[0] = {"active": p0_act, "start": p0_start, "end": p0_end}
            ch_periods[1] = {"active": p1_act, "start": p1_start, "end": p1_end}
            return True
        return False

    async def set_analog_period_time(
        self,
        channel: int,
        period_idx: int,
        start_time: dt_time | None = None,
        end_time: dt_time | None = None,
    ) -> bool:
        """Update start or end time for Period 1 (idx 0) or Period 2 (idx 1) on an analog channel."""
        ch_periods = self._video_color_periods.setdefault(channel, {})
        p0 = ch_periods.get(0, {"active": True, "start": dt_time(6, 0), "end": dt_time(18, 0)})
        p1 = ch_periods.get(1, {"active": False, "start": dt_time(18, 0), "end": dt_time(6, 0)})

        if start_time is not None and start_time.second == 59 and (start_time.hour, start_time.minute) != (23, 59):
            start_time = dt_time(start_time.hour, start_time.minute, 0)
        if end_time is not None and end_time.second == 59 and (end_time.hour, end_time.minute) != (23, 59):
            end_time = dt_time(end_time.hour, end_time.minute, 0)

        if period_idx == 0:
            old_p0_start = p0.get("start", dt_time(6, 0))
            old_p0_end = p0.get("end", dt_time(18, 0))
            p0_start = start_time if start_time is not None else old_p0_start
            p0_end = end_time if end_time is not None else old_p0_end

            # If the channel was in 24h single-period mode (00:00:00 - 23:59:59) and the user
            # edits Period 1 Start first, initialize Period 1 End to 18:00:00 so the DVR firmware
            # never receives "24:00:00" alongside an active Period 2 (which resets all channels).
            if start_time is not None and end_time is None and start_time != dt_time(0, 0):
                if old_p0_end >= dt_time(23, 59, 58) or old_p0_end == dt_time(0, 0):
                    p0_end = dt_time(18, 0) if p0_start != dt_time(18, 0) else dt_time(6, 0)

            # Conversely, if the user edits Period 1 End first while Start was 00:00:00 in 24h mode:
            if (
                end_time is not None
                and start_time is None
                and end_time < dt_time(23, 59, 58)
                and old_p0_start == dt_time(0, 0)
                and old_p0_end >= dt_time(23, 59, 58)
                and not p1.get("active", False)
            ):
                p0_start = dt_time(6, 0) if p0_end != dt_time(6, 0) else dt_time(18, 0)

            if p0_start == p0_end:
                return False

            is_24h = p0_start == dt_time(0, 0) and (
                p0_end >= dt_time(23, 59, 58) or (p0_end.hour == 23 and p0_end.minute == 59)
            )
            if is_24h:
                p0_act = True
                p1_act = False
                p1_start = dt_time(0, 0)
                p1_end = dt_time(23, 59, 59)
            else:
                if p0_end >= dt_time(23, 59, 58):
                    p0_end = dt_time(23, 59, 0)
                p0_act = True
                p1_act = True
                p1_start = p0_end
                p1_end = p0_start
        else:
            old_p1_start = p1.get("start", dt_time(18, 0))
            old_p1_end = p1.get("end", dt_time(6, 0))
            p1_start = start_time if start_time is not None else old_p1_start
            p1_end = end_time if end_time is not None else old_p1_end

            if start_time is not None and end_time is None:
                if old_p1_end >= dt_time(23, 59, 58) or old_p1_end == dt_time(0, 0):
                    p1_end = dt_time(6, 0) if p1_start != dt_time(6, 0) else dt_time(18, 0)
            if end_time is not None and start_time is None:
                if old_p1_start >= dt_time(23, 59, 58) or old_p1_start == dt_time(0, 0):
                    p1_start = dt_time(18, 0) if p1_end != dt_time(18, 0) else dt_time(6, 0)

            if p1_start >= dt_time(23, 59, 58):
                p1_start = dt_time(23, 59, 0)
            if p1_end >= dt_time(23, 59, 58):
                p1_end = dt_time(23, 59, 0)

            if p1_start == p1_end:
                return False

            p0_act = True
            p1_act = True
            p0_end = p1_start
            p0_start = p1_end

        if not p1_act:
            p0_end_str = (
                "24:00:00"
                if (p0_end.hour == 23 and p0_end.minute == 59 and p0_end.second >= 58)
                else p0_end.strftime("%H:%M:%S")
            )
            p1_end_str = (
                "24:00:00"
                if (p1_end.hour == 23 and p1_end.minute == 59 and p1_end.second >= 58)
                else p1_end.strftime("%H:%M:%S")
            )
        else:
            p0_end_str = p0_end.strftime("%H:%M:%S")
            p1_end_str = p1_end.strftime("%H:%M:%S")

        p0_str = f"{1 if p0_act else 0}%20{p0_start.strftime('%H:%M:%S')}-{p0_end_str}"
        p1_str = f"{1 if p1_act else 0}%20{p1_start.strftime('%H:%M:%S')}-{p1_end_str}"

        prof_colors = self._video_color_by_profile.get(channel, {})
        base_colors = self._video_color.get(channel, {})
        c0 = prof_colors.get(0) or base_colors
        c1 = prof_colors.get(1) or c0

        b0 = c0.get("Brightness", DEFAULT_BRIGHTNESS)
        con0 = c0.get("Contrast", DEFAULT_CONTRAST)
        h0 = c0.get("Hue", DEFAULT_HUE)
        s0 = c0.get("Saturation", DEFAULT_SATURATION_ANALOG)
        a0 = c0.get("Acutance", DEFAULT_ACUTANCE)

        b1 = c1.get("Brightness", DEFAULT_BRIGHTNESS)
        con1 = c1.get("Contrast", DEFAULT_CONTRAST)
        h1 = c1.get("Hue", DEFAULT_HUE)
        s1 = c1.get("Saturation", DEFAULT_SATURATION_ANALOG)
        a1 = c1.get("Acutance", DEFAULT_ACUTANCE)

        qs = [
            "action=setConfig",
            f"VideoColor[{channel}][0].Brightness={b0}",
            f"VideoColor[{channel}][0].Contrast={con0}",
            f"VideoColor[{channel}][0].Hue={h0}",
            f"VideoColor[{channel}][0].Saturation={s0}",
            f"VideoColor[{channel}][0].Acutance={a0}",
            f"VideoColor[{channel}][0].TimeSection={p0_str}",
            f"VideoColor[{channel}][1].Brightness={b1}",
            f"VideoColor[{channel}][1].Contrast={con1}",
            f"VideoColor[{channel}][1].Hue={h1}",
            f"VideoColor[{channel}][1].Saturation={s1}",
            f"VideoColor[{channel}][1].Acutance={a1}",
            f"VideoColor[{channel}][1].TimeSection={p1_str}",
        ]
        url = f"{CONFIG_MANAGER_CGI}?" + "&".join(qs)
        resp = await self._get(url, timeout=6.0)
        if resp.status_code == 200 and "ok" in resp.text.lower():
            ch_periods[0] = {"active": p0_act, "start": p0_start, "end": p0_end}
            ch_periods[1] = {"active": p1_act, "start": p1_start, "end": p1_end}
            return True
        return False

    def get_video_color_by_profile(self, channel: int) -> dict[int, dict[str, int]]:
        """Return cached picture settings organized by profile index for a channel."""
        return self._video_color_by_profile.get(channel, {})

    def get_noise_reduction(self, channel: int, profile: int | None = None) -> int:
        """Return cached 2D noise reduction level (0..100) for a camera channel and profile."""
        prof = profile if profile is not None else self.get_active_profile_index(channel)
        prof_val = self._video_denoise.get(channel, {}).get("profiles", {}).get(prof)
        if prof_val is not None:
            return prof_val
        return self._video_denoise.get(channel, {}).get("2d_level", 50)

    async def set_noise_reduction(self, channel: int, level: int, profile: int | None = None) -> bool:
        """Set 2D noise reduction level (0..100) for a camera channel and specific profile."""
        level = max(0, min(100, level))
        if profile is not None:
            profs = [profile]
        else:
            profs = self.get_channel_profiles(channel) or [0, 1]
        params: dict[str, Any] = {"action": "setConfig"}
        for prof in profs:
            params[f"VideoInDenoise[{channel}][{prof}].2DLevel"] = level

        resp = await self._get(CONFIG_MANAGER_CGI, params=params, timeout=6.0)
        if resp.status_code == 200 and "ok" in resp.text.lower():
            ch_denoise = self._video_denoise.setdefault(channel, {})
            ch_denoise["2d_level"] = level
            for prof in profs:
                ch_denoise.setdefault("profiles", {})[prof] = level
            return True
        return False

    def get_image_enhancement(self, channel: int, profile: int | None = None) -> int:
        """Return cached image enhancement level (0..100) for a camera channel and profile."""
        prof = profile if profile is not None else self.get_active_profile_index(channel)
        prof_val = self._image_enhancement.get(channel, {}).get("profiles", {}).get(prof)
        if prof_val is not None:
            return prof_val
        return self._image_enhancement.get(channel, {}).get("level", 50)

    async def set_image_enhancement(self, channel: int, level: int, profile: int | None = None) -> bool:
        """Set image enhancement level (0..100) for a camera channel and specific profile."""
        level = max(0, min(100, level))
        if profile is not None:
            profs = [profile]
        else:
            profs = self.get_channel_profiles(channel) or [0, 1]
        params: dict[str, Any] = {"action": "setConfig"}
        for prof in profs:
            params[f"ImageEnhancement[{channel}][{prof}].Level"] = level
            params[f"ImageEnhancement[{channel}][{prof}].Enable"] = "true" if level > 0 else "false"

        resp = await self._get(CONFIG_MANAGER_CGI, params=params, timeout=6.0)
        if resp.status_code == 200 and "ok" in resp.text.lower():
            ch_enh = self._image_enhancement.setdefault(channel, {})
            ch_enh["level"] = level
            for prof in profs:
                ch_enh.setdefault("profiles", {})[prof] = level
            return True
        return False

    def get_gain_auto(self, channel: int, profile: int = 0) -> bool:
        """Return cached GainAuto (Auto Noise Reduction) for a channel and profile."""
        if profile == 1:
            return self._video_in_options.get(channel, {}).get("night_gain_auto", False)
        return self._video_in_options.get(channel, {}).get("gain_auto", False)

    async def set_gain_auto(self, channel: int, enable: bool, profile: int = 0) -> bool:
        """Set GainAuto (Auto Noise Reduction) on an analog camera channel for a specific profile."""
        val = "true" if enable else "false"
        key = f"VideoInOptions[{channel}].NightOptions.GainAuto" if profile == 1 else f"VideoInOptions[{channel}].GainAuto"
        params: dict[str, Any] = {
            "action": "setConfig",
            key: val,
        }
        resp = await self._get(CONFIG_MANAGER_CGI, params=params, timeout=6.0)
        if resp.status_code == 200 and "ok" in resp.text.lower():
            prop = "night_gain_auto" if profile == 1 else "gain_auto"
            self._video_in_options.setdefault(channel, {})[prop] = enable
            return True
        return False

    async def set_switch_mode(self, channel: int, mode_str: str) -> bool:
        """Set SwitchMode (Profile management mode) for a camera channel."""
        return await self.set_ip_profile_mode(channel, mode_str)

    async def set_picture_style(self, channel: int, style: str) -> bool:
        """Set Picture Style / Scene Mode for a camera channel (Standard, Soft, Vivid)."""
        style_title = style.title()
        if style_title not in ("Standard", "Soft", "Vivid"):
            style_title = "Standard"
        params: dict[str, Any] = {
            "action": "setConfig",
            f"VideoColor[{channel}][0].Style": style_title,
            f"VideoColor[{channel}][1].Style": style_title,
            f"VideoColor[{channel}][2].Style": style_title,
        }
        resp = await self._get(
            CONFIG_MANAGER_CGI,
            params=params,
            timeout=6.0,
        )
        if resp.status_code == 200 and "ok" in resp.text.lower():
            ch_colors = self._video_color.setdefault(channel, {})
            ch_colors["picture_style"] = style_title.lower()
            return True
        return False

    async def set_channel_title(self, channel: int, title: str) -> bool:
        """Set on-screen channel title name."""
        encoded_title = urllib.parse.quote(title, safe="")
        resp = await self._get(
            f"{CONFIG_MANAGER_CGI}?action=setConfig&ChannelTitle[{channel}].Name={encoded_title}",
            timeout=6.0,
        )
        if resp.status_code == 200 and "ok" in resp.text.lower():
            self._channel_names[channel] = title
            return True
        return False

    def _parse_encode_config(self, text: str) -> None:
        """Parse Encode config into self._encode_config."""
        main_pattern = re.compile(
            r"table\.Encode\[(\d+)\]\.MainFormat\[0\]\.([A-Za-z0-9_\.]+)=(.*)"
        )
        extra_pattern = re.compile(
            r"table\.Encode\[(\d+)\]\.ExtraFormat\[0\]\.([A-Za-z0-9_\.]+)=(.*)"
        )
        for line in text.splitlines():
            line_str = line.strip()
            m_main = main_pattern.match(line_str)
            if m_main:
                ch = int(m_main.group(1))
                prop = m_main.group(2)
                val = m_main.group(3).strip()
                ch_dict = self._encode_config.setdefault(
                    ch, {"main": {}, "sub": {}, "audio_enable": False}
                )
                ch_dict["main"][prop] = val
                if prop == "AudioEnable":
                    ch_dict["audio_enable"] = val.lower() == "true"
                elif prop == "Audio.Compression":
                    ch_dict["audio_compression"] = val
                elif prop == "Video.Compression":
                    ch_dict["video_compression"] = val
                elif prop == "Video.FPS":
                    try:
                        ch_dict["video_fps"] = float(val)
                    except ValueError:
                        pass
                elif prop == "Video.Width":
                    try:
                        ch_dict["video_width"] = int(val)
                    except ValueError:
                        pass
                elif prop == "Video.Height":
                    try:
                        ch_dict["video_height"] = int(val)
                    except ValueError:
                        pass
                continue

            m_sub = extra_pattern.match(line_str)
            if m_sub:
                ch = int(m_sub.group(1))
                prop = m_sub.group(2)
                val = m_sub.group(3).strip()
                ch_dict = self._encode_config.setdefault(
                    ch, {"main": {}, "sub": {}, "audio_enable": False}
                )
                ch_dict["sub"][prop] = val
                continue

    async def get_encode_config(self, refresh: bool = False) -> dict[int, dict[str, Any]]:
        """Return encode configuration per zero-based channel index (cached)."""
        if not refresh and self._encode_config:
            return self._encode_config

        try:
            resp = await self._get(
                CONFIG_MANAGER_CGI,
                params={"action": "getConfig", "name": "Encode"},
                timeout=4.0,
            )
            if resp.status_code == 200:
                self._parse_encode_config(resp.text)
        except DVRAuthError:
            raise
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Failed to fetch Encode config from DVR: %s", exc)

        return self._encode_config

    async def get_channel_playback_directive(
        self,
        ch1: int,
        dvr_playback_url: str,
    ) -> tuple[str, bool]:
        """Dynamically determine optimal playback stream source and whether go2rtc is needed.

        Returns (src_directive, needs_go2rtc).
        """
        ch0 = ch1 - 1
        cfg = self._encode_config.get(ch0)
        if not cfg or not cfg.get("main"):
            cfg_all = await self.get_encode_config()
            cfg = cfg_all.get(ch0, {})

        has_audio = cfg.get("audio_enable", False)
        if not has_audio:
            return dvr_playback_url, False

        src = f"ffmpeg:{dvr_playback_url}#video=copy#audio=aac#async"
        return src, True

    def channel_has_audio(self, ch: int) -> bool:
        """Return whether zero-based channel index has audio recording enabled."""
        return bool(self._encode_config.get(ch, {}).get("audio_enable", False))

    # ------------------------------------------------------------------
    # PTZ
    # ------------------------------------------------------------------

    async def ptz_move(
        self,
        channel: int,
        direction_code: str,
        speed: int = 4,
    ) -> bool:
        """Start a continuous PTZ movement (1-based channel numbering in ptz.cgi)."""
        resp = await self._get(
            PTZ_CGI,
            params={
                "action": "start",
                "channel": channel + 1,
                "code": direction_code,
                "arg1": speed,
                "arg2": speed,
                "arg3": 0,
            },
        )
        return resp.status_code == 200 and "OK" in resp.text

    async def ptz_stop(self, channel: int, direction_code: str) -> bool:
        """Stop a continuous PTZ movement (1-based channel numbering in ptz.cgi)."""
        resp = await self._get(
            PTZ_CGI,
            params={
                "action": "stop",
                "channel": channel + 1,
                "code": direction_code,
                "arg1": 0,
                "arg2": 0,
                "arg3": 0,
            },
        )
        return resp.status_code == 200 and "OK" in resp.text

    async def ptz_goto_preset(self, channel: int, preset: int) -> bool:
        """Move PTZ camera to a saved preset position (1-based channel numbering in ptz.cgi)."""
        resp = await self._get(
            PTZ_CGI,
            params={
                "action": "start",
                "channel": channel + 1,
                "code": "GotoPreset",
                "arg1": 0,
                "arg2": preset,
                "arg3": 0,
            },
        )
        return resp.status_code == 200 and "OK" in resp.text

    async def get_ptz_presets(self, channel: int) -> dict[int, str]:
        """Fetch saved PTZ preset positions for a channel via ptz.cgi (1-based channel)."""
        pattern_idx = re.compile(r"presets\[(\d+)\]\.Index=(\d+)")
        pattern_name = re.compile(r"presets\[(\d+)\]\.Name=(.*)")

        resp = await self._get(
            PTZ_CGI,
            params={"action": "getPresets", "channel": channel + 1},
        )
        if resp.status_code == 200 and "presets[" in resp.text:
            indices: dict[int, int] = {}
            names: dict[int, str] = {}
            for line in resp.text.splitlines():
                m_idx = pattern_idx.match(line.strip())
                if m_idx:
                    indices[int(m_idx.group(1))] = int(m_idx.group(2))
                m_name = pattern_name.match(line.strip())
                if m_name:
                    names[int(m_name.group(1))] = m_name.group(2).strip()

            presets: dict[int, str] = {}
            for arr_idx, p_idx in indices.items():
                name = names.get(arr_idx, str(p_idx))
                presets[p_idx] = name
            return presets

        return {}

    async def get_ptz_status(self, channel: int) -> dict[str, Any]:
        """Fetch current PTZ position and status for a channel via ptz.cgi."""
        resp = await self._get(
            PTZ_CGI,
            params={"action": "getStatus", "channel": channel + 1},
        )
        res: dict[str, Any] = {}
        if resp.status_code == 200 and "status." in resp.text:
            pos_pattern = re.compile(r"status\.Postion\[(\d+)\]=([-\d\.]+)")
            for line in resp.text.splitlines():
                line_str = line.strip()
                m_pos = pos_pattern.match(line_str)
                if m_pos:
                    idx = int(m_pos.group(1))
                    val = float(m_pos.group(2))
                    if idx == 0:
                        res["pan"] = val
                    elif idx == 1:
                        res["tilt"] = val
                    elif idx == 2:
                        res["zoom"] = val
                elif line_str.startswith("status.MoveStatus="):
                    res["move_status"] = line_str.split("=", 1)[1].strip()
                elif line_str.startswith("status.ZoomStatus="):
                    res["zoom_status"] = line_str.split("=", 1)[1].strip()
                elif line_str.startswith("status.PresetID="):
                    try:
                        res["preset_id"] = int(line_str.split("=", 1)[1].strip())
                    except ValueError:
                        pass
        return res

    # ------------------------------------------------------------------
    # Maintenance / Reboot / AutoLogout
    # ------------------------------------------------------------------

    async def reboot(self) -> bool:
        """Send a reboot command to the DVR."""
        resp = await self._get(f"{MAGICBOX_CGI}?action=reboot")
        return resp.status_code == 200 and "OK" in resp.text

    async def async_disable_auto_logout(self) -> bool:
        """Disable AutoLogout on the DVR (V3.35 GUISet[0].AutoLogout and General.AutoLogout)."""
        ok = False
        for param_key in ("GUISet[0].AutoLogout", "General.AutoLogout"):
            try:
                resp = await self._get(
                    CONFIG_MANAGER_CGI,
                    params={
                        "action": "setConfig",
                        param_key: 0,
                    },
                    timeout=4.0,
                )
                if resp.status_code == 200 and "OK" in resp.text:
                    ok = True
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("AutoLogout setting %s skipped: %s", param_key, exc)
        return ok

    # ------------------------------------------------------------------
    # Events & Alarms
    # ------------------------------------------------------------------

    async def get_event_status(self, channels: list[int]) -> dict[int, dict[str, bool]]:
        """Check active alarms (Motion, VideoLoss, VideoBlind, IVS) via getEventIndexes."""
        status: dict[int, dict[str, bool]] = {
            ch: {
                "motion": False,
                "video_loss": False,
                "video_blind": False,
                "cross_line": False,
                "cross_region": False,
            }
            for ch in channels
        }
        event_types = {
            "VideoMotion": "motion",
            "VideoLoss": "video_loss",
            "VideoBlind": "video_blind",
            "CrossLineDetection": "cross_line",
            "CrossRegionDetection": "cross_region",
        }
        for cgi_code, sensor_key in event_types.items():
            try:
                resp = await self._get(
                    EVENT_MANAGER_CGI,
                    params={"action": "getEventIndexes", "code": cgi_code},
                    timeout=4.0,
                )
                if resp.status_code == 200:
                    indexes = self._parse_event_indexes(resp.text)
                    for ch in indexes:
                        if ch in status:
                            status[ch][sensor_key] = True
            except DVRAuthError:
                raise
            except DVRConnectionError as exc:
                _LOGGER.debug("Failed checking event %s: %s", cgi_code, exc)
        return status

    @staticmethod
    def _parse_event_indexes(text: str) -> list[int]:
        """Parse index array returned by getEventIndexes CGI."""
        indexes: list[int] = []
        for line in text.splitlines():
            line = line.strip()
            if "=" in line:
                val = line.split("=", 1)[1].strip()
                if val.isdigit():
                    indexes.append(int(val))
        return indexes

    async def listen_event_stream(
        self,
        event_callback: Callable[[int, str, bool, bool], None],
        codes: list[str] | None = None,
        heartbeat: int = 5,
        stop_event: asyncio.Event | None = None,
        heartbeat_callback: Callable[[], None] | None = None,
    ) -> None:
        """Stream events from the DVR in real-time via eventManager.cgi?action=attach.

        Keeps a single persistent HTTP connection open with Digest authentication.
        Eliminates repeated per-request logins in the DVR audit log.
        """
        if codes is None:
            codes = [
                "VideoMotion",
                "SmartMotionHuman",
                "SmartMotionVehicle",
                "VideoLoss",
                "VideoBlind",
                "CrossLineDetection",
                "CrossRegionDetection",
                "StorageNotExist",
                "StorageFailure",
                "StorageLowSpace",
                "LoginFailureAlarm",
            ]

        codes_param = f"[{','.join(codes)}]"
        stream_path = (
            f"{EVENT_MANAGER_CGI}?action=attach&codes={codes_param}&heartbeat={heartbeat}"
        )
        url = self.base_url + stream_path

        event_line_re = re.compile(
            r"Code=(?P<code>[A-Za-z0-9_]+);action=(?P<action>[A-Za-z0-9_]+);index=(?P<index>\d+)"
        )

        event_map = {
            "VideoMotion": "motion",
            "SmartMotionHuman": "motion",
            "SmartMotionVehicle": "motion",
            "VideoLoss": "video_loss",
            "VideoBlind": "video_blind",
            "CrossLineDetection": "cross_line",
            "CrossRegionDetection": "cross_region",
            "StorageNotExist": "storage_not_exist",
            "StorageFailure": "storage_failure",
            "StorageLowSpace": "storage_low_space",
            "LoginFailureAlarm": "login_failure",
        }

        remaining = self.auth_lockout_remaining
        if remaining > 0:
            raise DVRAuthError(
                f"Temporary lockout active for {remaining:.0f}s "
                "due to previous authentication failure."
            )

        async with httpx.AsyncClient(
            auth=httpx.DigestAuth(self.username, self.password),
            verify=False,
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=None),
        ) as stream_client:
            async with stream_client.stream("GET", url) as response:
                if response.status_code in (401, 403):
                    cooldown = self._record_auth_failure()
                    raise DVRAuthError(
                        f"Unauthorized connecting to DVR event stream ({response.status_code}; cooldown {int(cooldown)}s)"
                    )
                if response.status_code != 200:
                    raise DVRConnectionError(
                        f"DVR event stream returned unexpected HTTP status {response.status_code}"
                    )

                self.reset_auth_backoff()
                _LOGGER.info("Connected to Intelbras DVR persistent event stream on %s", self.host)
                if heartbeat_callback is not None:
                    try:
                        heartbeat_callback()
                    except Exception:  # noqa: BLE001
                        pass
                aiter = response.aiter_lines()

                while not (stop_event and stop_event.is_set()):
                    try:
                        line = await asyncio.wait_for(aiter.__anext__(), timeout=25.0)
                    except StopAsyncIteration:
                        _LOGGER.warning("DVR event stream closed by remote server.")
                        break
                    except asyncio.TimeoutError:
                        _LOGGER.warning(
                            "DVR event stream missed heartbeats for 25s; reconnecting..."
                        )
                        break

                    if heartbeat_callback is not None:
                        try:
                            heartbeat_callback()
                        except Exception:  # noqa: BLE001
                            pass

                    line = line.strip()
                    if not line:
                        continue

                    if line == "Heartbeat":
                        _LOGGER.debug("DVR event stream heartbeat received")
                        continue

                    match = event_line_re.search(line)
                    if match:
                        code = match.group("code")
                        action = match.group("action")
                        idx = int(match.group("index"))

                        if code in event_map:
                            sensor_key = event_map[code]
                            is_active = action in ("Start", "Pulse")
                            is_pulse = action == "Pulse"
                            _LOGGER.debug(
                                "DVR stream event: channel=%d, key=%s, active=%s (action=%s)",
                                idx,
                                sensor_key,
                                is_active,
                                action,
                            )
                            event_callback(idx, sensor_key, is_active, is_pulse)

    # ------------------------------------------------------------------
    # Streaming & Snapshots
    # ------------------------------------------------------------------

    def get_rtsp_url(
        self,
        channel: int,
        subtype: int = 0,
        rtsp_port: int | None = None,
    ) -> str:
        """Return the RTSP live stream URL for a given 0-based channel index and subtype."""
        user_enc = urllib.parse.quote(self.username, safe="")
        pass_enc = urllib.parse.quote(self.password, safe="")
        port = rtsp_port or self.rtsp_port or DEFAULT_RTSP_PORT
        path = RTSP_LIVE_PATH.format(channel=channel + 1, subtype=subtype)
        return f"rtsp://{user_enc}:{pass_enc}@{self.host}:{port}{path}"

    def live_rtsp_url(
        self,
        channel: int,
        subtype: int = 0,
        rtsp_port: int | None = None,
    ) -> str:
        """Return the RTSP live stream URL for a 1-based channel number."""
        ch0 = channel - 1 if channel >= 1 else channel
        return self.get_rtsp_url(ch0, subtype=subtype, rtsp_port=rtsp_port)

    def get_snapshot_url(self, channel: int) -> str:
        """Return the HTTP snapshot URL for a given channel (0-based index)."""
        return f"{self.base_url}{SNAPSHOT_CGI}?channel={channel + 1}"

    async def get_snapshot(self, channel: int, timeout: float = 2.5) -> bytes | None:
        """Fetch a still image JPEG for a channel (0-based index)."""
        if self.auth_lockout_remaining > 0:
            return None
        url = f"{SNAPSHOT_CGI}?channel={channel + 1}"
        try:
            resp = await self._get(url, timeout=timeout)
            if resp.status_code == 200 and resp.content.startswith(b"\xff\xd8"):
                return resp.content
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Snapshot failed for channel %d: %s", channel, exc)
        return None

    async def snapshot(self, channel: int, timeout: float = 2.5) -> bytes | None:
        """Fetch snapshot (accepts 1-based channel or 0-based channel)."""
        ch0 = channel - 1 if channel >= 1 else channel
        return await self.get_snapshot(ch0, timeout=timeout)

    # ------------------------------------------------------------------
    # Playback & Recordings search (HTTP API V3.35 Section 4.10.5)
    # ------------------------------------------------------------------

    async def _close_and_destroy_finder(self, token: int) -> None:
        """Properly close and destroy a mediaFileFind object handle per V3.35 spec."""
        for action in ("close", "destroy"):
            try:
                await self._get(
                    MEDIAFILEFIND_CGI,
                    params={"action": action, "object": token},
                    timeout=3.0,
                )
            except Exception:  # noqa: BLE001
                pass

    async def find_earliest_recording_date(
        self,
        channel: int,
        stream_type: str = "main",
    ) -> date | None:
        """Find the earliest available recording date for a 1-based channel on the DVR."""
        if channel in self._earliest_date_cache:
            return self._earliest_date_cache[channel]

        now = datetime.now()
        start_time = now - timedelta(days=90)
        subtype = 0 if stream_type == "main" else 1
        condition = {
            "Channel": channel,
            "StartTime": start_time.strftime(_FIND_TIME_FMT),
            "EndTime": now.strftime(_FIND_TIME_FMT),
            "Types": ["dav"],
            "VideoStream": "Main" if subtype == 0 else "Extra1",
        }
        create_resp = await self._get(
            MEDIAFILEFIND_CGI,
            params={"action": "factory.create"},
            timeout=5.0,
        )
        if create_resp.status_code != 200:
            return self._dvr_earliest_date

        token_match = re.search(r"result=(\d+)", create_resp.text)
        if not token_match:
            return self._dvr_earliest_date
        token = int(token_match.group(1))

        try:
            start_enc = urllib.parse.quote(condition["StartTime"], safe=":")
            end_enc = urllib.parse.quote(condition["EndTime"], safe=":")
            find_path = (
                f"{MEDIAFILEFIND_CGI}?action=findFile&object={token}"
                f"&condition.Channel={condition['Channel']}"
                f"&condition.StartTime={start_enc}"
                f"&condition.EndTime={end_enc}"
                f"&condition.Types%5B0%5D=dav"
                f"&condition.VideoStream={condition['VideoStream']}"
            )
            find_file_resp = await self._get(find_path, timeout=5.0)
            if find_file_resp.status_code != 200 or "OK" not in find_file_resp.text:
                return self._dvr_earliest_date

            find_resp = await self._get(
                MEDIAFILEFIND_CGI,
                params={
                    "action": "findNextFile",
                    "object": token,
                    "count": 1,
                },
                timeout=5.0,
            )
            if find_resp.status_code == 200:
                match = re.search(r"StartTime=(\d{4}-\d{2}-\d{2})", find_resp.text)
                if match:
                    found_date = datetime.strptime(match.group(1), "%Y-%m-%d").date()
                    self._earliest_date_cache[channel] = found_date
                    if self._dvr_earliest_date is None or found_date < self._dvr_earliest_date:
                        self._dvr_earliest_date = found_date
                    return found_date
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug(
                "Failed finding earliest recording date for channel %d: %s",
                channel,
                exc,
            )
        finally:
            await self._close_and_destroy_finder(token)

        return self._dvr_earliest_date

    async def find_recordings(
        self,
        channel: int,
        start_time: datetime,
        end_time: datetime,
        stream_type: str = "main",
    ) -> list[Recording]:
        """Search for recordings in a time window on the DVR using mediaFileFind CGI."""
        subtype = 0 if stream_type == "main" else 1
        condition = {
            "Channel": channel,
            "StartTime": start_time.strftime(_FIND_TIME_FMT),
            "EndTime": end_time.strftime(_FIND_TIME_FMT),
            "Types": ["dav"],
            "VideoStream": "Main" if subtype == 0 else "Extra1",
        }
        create_resp = await self._get(
            MEDIAFILEFIND_CGI,
            params={"action": "factory.create"},
        )
        if create_resp.status_code != 200:
            return []

        token_match = re.search(r"result=(\d+)", create_resp.text)
        if not token_match:
            return []
        token = int(token_match.group(1))

        recordings: list[Recording] = []
        try:
            start_enc = urllib.parse.quote(condition["StartTime"], safe=":")
            end_enc = urllib.parse.quote(condition["EndTime"], safe=":")
            find_path = (
                f"{MEDIAFILEFIND_CGI}?action=findFile&object={token}"
                f"&condition.Channel={condition['Channel']}"
                f"&condition.StartTime={start_enc}"
                f"&condition.EndTime={end_enc}"
                f"&condition.Types%5B0%5D=dav"
                f"&condition.VideoStream={condition['VideoStream']}"
            )
            find_file_resp = await self._get(find_path)
            if find_file_resp.status_code != 200 or "OK" not in find_file_resp.text:
                return []

            while True:
                find_resp = await self._get(
                    MEDIAFILEFIND_CGI,
                    params={
                        "action": "findNextFile",
                        "object": token,
                        "count": _FIND_PAGE_SIZE,
                    },
                )
                if find_resp.status_code != 200:
                    break

                page = self._parse_file_find_page(find_resp.text, channel)
                if not page:
                    break
                recordings.extend(page)
                if len(page) < _FIND_PAGE_SIZE:
                    break
        finally:
            await self._close_and_destroy_finder(token)

        return recordings

    @staticmethod
    def _parse_file_find_page(text: str, channel: int) -> list[Recording]:
        """Parse items returned by findNextFile."""
        raw_items: dict[int, dict[str, str]] = {}
        for line in text.splitlines():
            line = line.strip()
            match = _ITEM_RE.match(line)
            if match:
                idx = int(match.group(1))
                field_name = match.group(2)
                value = match.group(3).strip()
                raw_items.setdefault(idx, {})[field_name] = value

        results: list[Recording] = []
        for d in raw_items.values():
            try:
                start = datetime.strptime(d["StartTime"], _FIND_TIME_FMT)
                end = datetime.strptime(d["EndTime"], _FIND_TIME_FMT)
                results.append(
                    Recording(
                        channel=channel,
                        start=start,
                        end=end,
                        file_path=d.get("FilePath", ""),
                        length=int(d.get("Length", 0)),
                        rec_type=d.get("Type", "dav"),
                    )
                )
            except (KeyError, ValueError):
                continue
        return results

    def playback_rtsp_url(
        self,
        channel: int,
        start_time: datetime,
        end_time: datetime,
        rtsp_port: int | None = None,
        subtype: int = 0,
    ) -> str:
        """Generate RTSP playback URL for a historical time range (expects 1-based channel)."""
        user_enc = urllib.parse.quote(self.username, safe="")
        pass_enc = urllib.parse.quote(self.password, safe="")
        port = rtsp_port or self.rtsp_port or DEFAULT_RTSP_PORT
        st = start_time.strftime(_PLAYBACK_TIME_FMT)
        et = end_time.strftime(_PLAYBACK_TIME_FMT)
        path = RTSP_PLAYBACK_PATH.format(
            channel=channel,
            start=st,
            end=et,
        )
        return f"rtsp://{user_enc}:{pass_enc}@{self.host}:{port}{path}"

    def get_playback_rtsp_url(
        self,
        channel: int,
        start_time: datetime,
        end_time: datetime,
        subtype: int = 0,
    ) -> str:
        """Generate RTSP playback URL for a historical time range (expects 0-based channel)."""
        return self.playback_rtsp_url(channel + 1, start_time, end_time, subtype=subtype)
