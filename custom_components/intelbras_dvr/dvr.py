"""Async HTTP client for Intelbras / Dahua DVRs using Digest Authentication."""
from __future__ import annotations

import asyncio
import logging
import re
import urllib.parse
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from typing import Any, Callable

import httpx

from .const import (
    CONFIG_MANAGER_CGI,
    DEFAULT_HTTP_PORT,
    DEFAULT_RTSP_PORT,
    EVENT_MANAGER_CGI,
    LOGIN_LOCKOUT_BACKOFF,
    MAGICBOX_CGI,
    MEDIAFILEFIND_CGI,
    PTZ_CGI,
    RECORD_MODE_SCHEDULE,
    RTSP_LIVE_PATH,
    RTSP_PLAYBACK_PATH,
    SNAPSHOT_CGI,
    SPLIT_CGI,
    STORAGE_CGI,
    UPGRADE_CGI,
)

_LOGGER = logging.getLogger(__name__)

_ITEM_RE = re.compile(r"^items\[(\d+)\]\.([A-Za-z]+)(?:\[\d+\])?=(.*)$")
_FIND_TIME_FMT = "%Y-%m-%d %H:%M:%S"
_PLAYBACK_TIME_FMT = "%Y_%m_%d_%H_%M_%S"
_FIND_PAGE_SIZE = 100


class DVRError(Exception):
    """Base exception for DVR communication errors."""


class DVRAuthError(DVRError):
    """Raised when the DVR returns 401 Unauthorized."""


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
    """Async HTTP client with Digest Auth for the Intelbras / Dahua CGI API."""

    def __init__(
        self,
        host: str,
        username: str,
        password: str,
        http_port: int = DEFAULT_HTTP_PORT,
        rtsp_port: int = DEFAULT_RTSP_PORT,
    ) -> None:
        """Initialise the client."""
        self.host = host
        self.username = username
        self.password = password
        self.http_port = http_port
        self.rtsp_port = rtsp_port
        self._lockout_until: float = 0.0
        self._client: httpx.AsyncClient | None = None
        self._http_sem: asyncio.Semaphore = asyncio.Semaphore(4)
        self._saved_video_colors: dict[int, dict[str, int]] = {}
        self._channel_supports_acutance: set[int] = set()
        self._channel_profiles: dict[int, set[int]] = {}
        self._earliest_date_cache: dict[int, date] = {}
        self._dvr_earliest_date: date | None = None
        self._encode_config: dict[int, dict[str, Any]] = {}
        self._remote_devices: dict[int, dict[str, Any]] = {}

    def channel_supports_acutance(self, channel: int) -> bool:
        """Return True if the camera channel supports acutance (sharpness)."""
        return channel in self._channel_supports_acutance

    def channel_is_ip(self, channel: int) -> bool:
        """Return True if the channel is an active IP camera."""
        if getattr(self, "_remote_devices", None):
            if channel not in self._remote_devices:
                return False
            dev = self._remote_devices[channel]
            return dev.get("Enable") == "true" and dev.get("Address") not in ("", "0.0.0.0", "192.168.0.0")
        return False

    def channel_is_analog(self, channel: int) -> bool:
        """Return True if the channel is an analog BNC camera."""
        return not self.channel_is_ip(channel)

    def get_channel_profiles(self, channel: int) -> list[int]:
        """Return all configured VideoColor profile indexes for a channel."""
        return sorted(list(self._channel_profiles.get(channel, {0})))

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

    async def _get(
        self,
        path: str,
        params: dict[str, Any] | None = None,
        timeout: float = 10.0,
    ) -> httpx.Response:
        """Perform a GET request with lockout protection against 401 ban and bounded concurrency."""
        loop = asyncio.get_running_loop()
        if loop.time() < self._lockout_until:
            raise DVRAuthError(
                f"Temporary lockout active for {self._lockout_until - loop.time():.0f}s "
                "due to previous 401 Unauthorized."
            )

        client = await self._get_http_client()
        url = self.base_url + path
        async with self._http_sem:
            if loop.time() < self._lockout_until:
                raise DVRAuthError(
                    f"Temporary lockout active for {self._lockout_until - loop.time():.0f}s "
                    "due to previous 401 Unauthorized."
                )
            try:
                resp = await client.get(url, params=params, timeout=timeout)
            except httpx.ConnectError as exc:
                raise DVRConnectionError(f"Cannot connect to DVR at {url}") from exc
            except httpx.TimeoutException as exc:
                raise DVRConnectionError(f"Timeout connecting to DVR at {url}") from exc

        if resp.status_code == 401:
            self._lockout_until = loop.time() + LOGIN_LOCKOUT_BACKOFF
            _LOGGER.error(
                "DVR returned 401 Unauthorized. Lockout activated for %ds.",
                int(LOGIN_LOCKOUT_BACKOFF),
            )
            raise DVRAuthError("Invalid credentials (401 Unauthorized)")
        if resp.status_code == 200:
            self._lockout_until = 0.0
        return resp

    # ------------------------------------------------------------------
    # System information
    # ------------------------------------------------------------------

    async def get_system_info(self) -> dict[str, str]:
        """Return hardware information (serial number, model, processor)."""
        resp = await self._get(f"{MAGICBOX_CGI}?action=getSystemInfo")
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
        """
        titles: dict[int, str] = {}
        remote_devices: dict[int, dict[str, str]] = {}

        try:
            resp = await self._get(
                CONFIG_MANAGER_CGI,
                params={"action": "getConfig", "name": ["ChannelTitle", "RemoteDevice"]},
                timeout=5.0,
            )
            if resp.status_code == 200:
                title_pat = re.compile(r"table\.ChannelTitle\[(\d+)\]\.Name=(.*)")
                rem_pat = re.compile(r"table\.RemoteDevice\.uuid:System_CONFIG_NETCAMERA_INFO_(\d+)\.([A-Za-z0-9_\[\]\.]+)=(.*)")

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
                if remote_devices:
                    self._remote_devices = remote_devices
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
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Failed ChannelTitle query: %s", exc)

        if not active_only or not titles:
            return titles

        # Filter out inactive/unassigned channels (default placeholder name and no active IP camera)
        active_titles: dict[int, str] = {}
        for ch, name in titles.items():
            rem = remote_devices.get(ch, {})
            rem_enable = rem.get("Enable", "")
            rem_addr = rem.get("Address", "")

            clean = name.strip().lower()
            is_default_name = clean in (
                f"cam {ch + 1}",
                f"channel {ch + 1}",
                f"canal {ch + 1}",
                f"cam{ch + 1}",
                "",
            )
            is_ip_active = (
                rem_enable == "true"
                and rem_addr not in ("", "0.0.0.0", "192.168.0.0")
            )

            if is_ip_active or not is_default_name:
                active_titles[ch] = name or f"Channel {ch + 1}"

        return active_titles

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
                    p = int(http_port) if http_port.isdigit() else 80
                    if p == 8000 and manufacturer == "Reolink":
                        p = 80
                    config_url = f"http://{ip}" if p == 80 else f"http://{ip}:{p}"
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
                "configuration_url": config_url,
            }

        return parsed

    async def detect_ptz_channels(self, channels: list[int] | None = None) -> list[int]:
        """Detect zero-based channel indices that support PTZ control.

        Queries ``ptz.cgi?action=getPresets`` (1-based channel numbering) in parallel.
        Note: ``ptz.cgi?action=getStatus`` is intentionally NOT used because the DVR's
        ONVIF stack returns ``status.MoveStatus=Idle`` even for fixed non-PTZ ONVIF cameras.
        """
        if channels is None:
            try:
                titles = await self.get_channel_titles()
                channels = list(titles.keys())
            except Exception:
                channels = []

        if not channels:
            return []

        async def _check_channel(ch: int) -> int | None:
            try:
                resp = await self._get(
                    PTZ_CGI,
                    params={"action": "getPresets", "channel": ch + 1},
                    timeout=2.0,
                )
                if resp.status_code == 200 and "presets[" in resp.text:
                    return ch
            except Exception:
                pass
            return None

        results = await asyncio.gather(*(_check_channel(ch) for ch in channels))
        return sorted({ch for ch in results if ch is not None})

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
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Failed to detect ONVIF channels: %s", exc)
            return set()

    # ------------------------------------------------------------------
    # Firmware update
    # ------------------------------------------------------------------

    async def check_firmware_update(self) -> dict[str, Any]:
        """
        Query the DVR's built-in firmware update check.

        Uses ``upgrade.cgi?action=check`` (standard Dahua/Intelbras API).
        Returns a dict:
          - "available": bool (True if a newer version is found)
          - "latest_version": str | None
          - "release_notes": str | None
        Falls back gracefully if the endpoint is not supported by the model.
        """
        result: dict[str, Any] = {
            "available": False,
            "latest_version": None,
            "release_notes": None,
        }
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
                if notes_match:
                    result["release_notes"] = notes_match.group(1).strip()
        except (DVRConnectionError, DVRAuthError) as exc:
            _LOGGER.debug("DVR firmware check failed/unsupported: %s", exc)
        return result

    async def trigger_firmware_update(self) -> bool:
        """Trigger an online firmware upgrade on the DVR if supported."""
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

        for line in resp.text.splitlines():
            line_str = line.strip()
            match = pattern.match(line_str)
            if match:
                ch = int(match.group(1))
                profile = int(match.group(2)) if match.group(2) is not None else 0
                prop = match.group(3)
                val = int(match.group(4))

                self._channel_profiles.setdefault(ch, set()).add(profile)

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
        """Update picture settings for a specific channel across its valid profile(s)."""
        profiles_to_update = [profile] if profile is not None else self.get_channel_profiles(channel)

        success = True
        for prof in profiles_to_update:
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
                continue

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
        """Set display split mode on physical monitor output."""
        resp = await self._get(
            SPLIT_CGI,
            params={
                "action": "setMode",
                "channel": channel,
                "mode": mode,
                "group": group,
            },
        )
        return resp.status_code == 200 and "OK" in resp.text

    # ------------------------------------------------------------------
    # Privacy masking
    # ------------------------------------------------------------------

    def save_camera_colors(self, channel: int, colors: dict[str, Any] | None) -> None:
        """Save camera picture settings before privacy masking. Protect against saving 0 values."""
        if not colors:
            return
        b = int(colors.get("Brightness", 0))
        s = int(colors.get("Saturation", 0))
        # Never overwrite good saved settings with a blacked out picture!
        if b == 0 and s == 0:
            return
        self._saved_video_colors[channel] = dict(colors)

    def get_saved_camera_colors(self, channel: int) -> dict[str, int]:
        """Retrieve saved camera picture settings before privacy masking."""
        default_sat = 80 if self.channel_supports_acutance(channel) else 50
        fallback = {
            "Brightness": 50,
            "Contrast": 50,
            "Saturation": default_sat,
            "Hue": 50,
            "Acutance": 1,
        }
        saved = self._saved_video_colors.get(channel)
        if not saved:
            return fallback
        # Sanity check: if saved values were somehow 0, fallback
        if int(saved.get("Brightness", 0)) == 0 and int(saved.get("Saturation", 0)) == 0:
            return fallback
        return dict(saved)

    async def mask_channels(self, channels: list[int], mask: bool = True) -> bool:
        """Black out (or restore) one or more channels using fast batched picture settings."""
        if not channels:
            return True

        if mask:
            # Only fetch current colors for channels that have not yet been saved
            missing_channels = [ch for ch in channels if ch not in self._saved_video_colors]
            if missing_channels:
                try:
                    current_colors = await self.get_video_color()
                    for ch in missing_channels:
                        if ch in current_colors:
                            self.save_camera_colors(ch, current_colors[ch])
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Could not pre-fetch current colors before mask: %s", exc)

            # Build single batched setConfig request for all channels and profiles
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
        else:
            # Restore all channels using batched setConfig request
            params = {"action": "setConfig"}
            for ch in channels:
                saved = self.get_saved_camera_colors(ch)
                profs = self.get_channel_profiles(ch)
                b = saved.get("Brightness", 50)
                c = saved.get("Contrast", 50)
                s = saved.get("Saturation", 80 if self.channel_supports_acutance(ch) else 50)
                h = saved.get("Hue", 50)
                a = saved.get("Acutance", 1)
                for prof in profs:
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
    ) -> tuple[dict[int, dict[str, int]], dict[int, int], dict[int, dict[str, Any]]]:
        """Fetch VideoColor, RecordMode, and Encode settings in a single HTTP request using combined name parameters."""
        resp = await self._get(
            CONFIG_MANAGER_CGI,
            params={"action": "getConfig", "name": ["VideoColor", "RecordMode", "Encode"]},
        )
        if resp.status_code != 200:
            return {}, {ch: RECORD_MODE_SCHEDULE for ch in channels}, {}

        colors: dict[int, dict[str, int]] = {}
        modes: dict[int, int] = {}
        encode_info: dict[int, dict[str, Any]] = {}

        vc_pattern = re.compile(
            r"table\.VideoColor\[(\d+)\](?:\[(\d+)\])?\.([A-Za-z]+)=(\d+)"
        )
        acutance_pattern = re.compile(r"table\.VideoColor\[(\d+)\].*Acutance=(\d+)")
        rm_pattern = re.compile(r"table\.RecordMode\[(\d+)\]\.Mode=(\d+)")

        for line in resp.text.splitlines():
            line_str = line.strip()

            ac_match = acutance_pattern.match(line_str)
            if ac_match:
                ch = int(ac_match.group(1))
                self._channel_supports_acutance.add(ch)

            vc_match = vc_pattern.match(line_str)
            if vc_match:
                ch = int(vc_match.group(1))
                profile = int(vc_match.group(2)) if vc_match.group(2) is not None else 0
                prop = vc_match.group(3)
                val = int(vc_match.group(4))

                self._channel_profiles.setdefault(ch, set()).add(profile)
                if profile == 0:
                    colors.setdefault(ch, {})[prop] = val
                continue

            rm_match = rm_pattern.match(line_str)
            if rm_match:
                ch = int(rm_match.group(1))
                modes[ch] = int(rm_match.group(2))
                continue

        self._parse_encode_config(resp.text)
        encode_info = self._encode_config

        for ch in channels:
            modes.setdefault(ch, RECORD_MODE_SCHEDULE)

        return colors, modes, encode_info

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
                    ch_dict["audio_enable"] = (val.lower() == "true")
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

        # If channel has no audio recorded, direct RTSP passthrough from DVR is fastest and lightest
        if not has_audio:
            return dvr_playback_url, False

        # If channel has audio recorded, browsers & Home Assistant HLS fMP4 require AAC audio
        # with proper extradata headers, and #async prevents FFmpeg probe deadlocks.
        # #video=copy preserves native resolution & codec (H.264 / H.265) without recompression.
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

    # ------------------------------------------------------------------
    # Maintenance / Reboot / AutoLogout
    # ------------------------------------------------------------------

    async def reboot(self) -> bool:
        """Send a reboot command to the DVR."""
        resp = await self._get(f"{MAGICBOX_CGI}?action=reboot")
        return resp.status_code == 200 and "OK" in resp.text

    async def async_disable_auto_logout(self) -> bool:
        """Disable AutoLogout on the DVR to prevent IP cameras from sleeping/hibernating."""
        resp = await self._get(
            CONFIG_MANAGER_CGI,
            params={
                "action": "setConfig",
                "table.General.AutoLogout": 0,
            },
        )
        return resp.status_code == 200 and "OK" in resp.text

    # ------------------------------------------------------------------
    # Events & Alarms
    # ------------------------------------------------------------------

    async def get_event_status(self, channels: list[int]) -> dict[int, dict[str, bool]]:
        """Check active alarms (Motion, VideoLoss, VideoBlind) via getEventIndexes."""
        status: dict[int, dict[str, bool]] = {
            ch: {"motion": False, "video_loss": False, "video_blind": False}
            for ch in channels
        }
        event_types = {
            "VideoMotion": "motion",
            "VideoLoss": "video_loss",
            "VideoBlind": "video_blind",
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
            except (DVRConnectionError, DVRAuthError) as exc:
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
            codes = ["VideoMotion", "SmartMotionHuman", "SmartMotionVehicle", "VideoLoss", "VideoBlind"]

        codes_param = f"[{','.join(codes)}]"
        stream_path = f"{EVENT_MANAGER_CGI}?action=attach&codes={codes_param}&heartbeat={heartbeat}"
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
        }

        async with httpx.AsyncClient(
            auth=httpx.DigestAuth(self.username, self.password),
            verify=False,
            timeout=httpx.Timeout(connect=10.0, read=None, write=10.0, pool=None),
        ) as stream_client:
            async with stream_client.stream("GET", url) as response:
                if response.status_code == 401:
                    raise DVRAuthError("Unauthorized connecting to DVR event stream (401)")
                if response.status_code != 200:
                    raise DVRConnectionError(
                        f"DVR event stream returned unexpected HTTP status {response.status_code}"
                    )

                _LOGGER.info("Connected to Intelbras DVR persistent event stream on %s", self.host)
                aiter = response.aiter_lines()

                while not (stop_event and stop_event.is_set()):
                    try:
                        line = await asyncio.wait_for(aiter.__anext__(), timeout=25.0)
                    except StopAsyncIteration:
                        _LOGGER.warning("DVR event stream closed by remote server.")
                        break
                    except asyncio.TimeoutError:
                        _LOGGER.warning("DVR event stream missed heartbeats for 25s; reconnecting...")
                        break

                    if heartbeat_callback is not None:
                        try:
                            heartbeat_callback()
                        except Exception:
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

    def get_rtsp_url(self, channel: int, subtype: int = 0) -> str:
        """Return the RTSP stream URL for a given channel and stream type."""
        user_enc = urllib.parse.quote(self.username, safe="")
        pass_enc = urllib.parse.quote(self.password, safe="")
        port = self.rtsp_port if self.rtsp_port != DEFAULT_RTSP_PORT else 554
        path = RTSP_LIVE_PATH.format(channel=channel + 1, subtype=subtype)
        return f"rtsp://{user_enc}:{pass_enc}@{self.host}:{port}{path}"

    def get_snapshot_url(self, channel: int) -> str:
        """Return the HTTP snapshot URL for a given channel (0-based index)."""
        return f"{self.base_url}{SNAPSHOT_CGI}?channel={channel + 1}"

    async def get_snapshot(self, channel: int, timeout: float = 2.5) -> bytes | None:
        """Fetch a still image JPEG for a channel (0-based index)."""
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
    # Playback & Recordings search
    # ------------------------------------------------------------------

    async def find_earliest_recording_date(
        self,
        channel: int,
        stream_type: str = "main",
    ) -> date | None:
        """Find the earliest available recording date for a channel on the DVR."""
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
            _LOGGER.debug("Failed finding earliest recording date for channel %d: %s", channel, exc)
        finally:
            try:
                await self._get(
                    MEDIAFILEFIND_CGI,
                    params={"action": "destroy", "object": token},
                    timeout=3.0,
                )
            except Exception:  # noqa: BLE001
                pass

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
            await self._get(
                MEDIAFILEFIND_CGI,
                params={"action": "destroy", "object": token},
            )
            return []

        recordings: list[Recording] = []
        try:
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
            await self._get(
                MEDIAFILEFIND_CGI,
                params={"action": "destroy", "object": token},
            )

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
        port = rtsp_port or (self.rtsp_port if self.rtsp_port != DEFAULT_RTSP_PORT else 554)
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
