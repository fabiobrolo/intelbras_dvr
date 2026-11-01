"""DataUpdateCoordinators for the Intelbras DVR integration."""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
import time
from datetime import timedelta
from typing import Any

from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed

from .const import (
    CONF_CHANNELS,
    CONF_PTZ_CHANNELS,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
    FIRMWARE_CHECK_INTERVAL,
)
from .dvr import DVRAuthError, DVRConnectionError, IntelbrasClient

_LOGGER = logging.getLogger(__name__)

# How often to poll alarm / motion event state (seconds).
# Kept intentionally short so that binary sensors react within a few seconds.
EVENT_SCAN_INTERVAL = 5


# ---------------------------------------------------------------------------
# Main coordinator — slow poll for system info, video colour, disk, PTZ presets
# ---------------------------------------------------------------------------


class IntelbrasCoordinator(DataUpdateCoordinator[dict[str, Any]]):
    """Coordinate periodic data refresh from the Intelbras DVR.

    Polls every DEFAULT_SCAN_INTERVAL seconds (default 60 s).
    Manages heavier data: system info, video colour, disk, PTZ presets,
    recording modes, and firmware update checks.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: IntelbrasClient,
    ) -> None:
        """Initialise the main coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.data.get('host', 'dvr')}",
            update_interval=timedelta(seconds=DEFAULT_SCAN_INTERVAL),
        )
        self.client = client
        self.config_entry = entry
        self.snapshot_interval: float = float(
            entry.options.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL)
        )
        self._consecutive_failures: int = 0
        self._last_response_time_ms: float | None = None

        # Ensure helper & config_flow submodules are hot-reloaded on entry reload
        for sub in ("entity", "ptz", "config_flow"):
            mod_name = f"custom_components.intelbras_dvr.{sub}"
            try:
                if mod_name in sys.modules:
                    importlib.reload(sys.modules[mod_name])
                else:
                    importlib.import_module(mod_name)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Could not reload %s: %s", mod_name, exc)

        # Register or hot-reload all HTTP thumbnail views dynamically on coordinator creation
        try:
            from .views import register_or_update_views

            register_or_update_views(hass)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.warning("Could not register/update Intelbras HTTP views: %s", exc)
        self._cached_system_info: dict[str, str] | None = None
        self._cached_software_version: str | None = None
        self._cached_remote_devices: dict[int, dict[str, Any]] | None = None
        self._cached_ptz_presets: dict[int, dict[int, str]] = {}
        self._presets_loaded: bool = False
        self._last_firmware_check: float = 0.0
        self._cached_firmware_info: dict[str, Any] = {
            "available": False,
            "latest_version": None,
            "release_notes": None,
        }
        self._refresh_video_color_task: asyncio.Task[None] | None = None

    @property
    def selected_channels(self) -> list[int]:
        """Return the zero-based channel indexes chosen by the user."""
        return list(self.config_entry.data.get(CONF_CHANNELS, []))

    @property
    def ptz_channels(self) -> list[int]:
        """Return the zero-based channel indexes with PTZ capability."""
        return list(self.config_entry.data.get(CONF_PTZ_CHANNELS, []))

    @property
    def channel_names(self) -> dict[int, str]:
        """Return the saved mapping of channel index to channel name."""
        raw: dict[str, str] = self.config_entry.data.get("channel_names", {})
        return {int(k): v for k, v in raw.items()}

    async def async_load_ptz_presets(self) -> None:
        """Fetch PTZ presets once on startup or entry reconfiguration."""
        ptz_chs = self.ptz_channels
        if not ptz_chs:
            self._presets_loaded = True
            return
        _LOGGER.debug("Fetching PTZ presets on initialization/reconfiguration for channels: %s", ptz_chs)
        preset_tasks = [self.client.get_ptz_presets(ch) for ch in ptz_chs]
        preset_results = await asyncio.gather(*preset_tasks, return_exceptions=True)
        for ch, res in zip(ptz_chs, preset_results):
            if isinstance(res, dict):
                self._cached_ptz_presets[ch] = res
            elif isinstance(res, Exception):
                _LOGGER.warning("Could not fetch PTZ presets for channel %d: %s", ch, res)
        self._presets_loaded = True

    async def _async_update_data(self) -> dict[str, Any]:
        """Fetch fresh state from the DVR with minimal overhead."""
        channels = self.selected_channels

        # Fetch PTZ presets only on startup or reconfig
        if not self._presets_loaded:
            await self.async_load_ptz_presets()

        # Cache static system info / software version / remote devices on first run
        if self._cached_system_info is None or self._cached_software_version is None or self._cached_remote_devices is None:
            init_tasks = [
                self.client.get_system_info(),
                self.client.get_software_version(),
                self.client.get_channel_titles(),
            ]
            init_results = await asyncio.gather(*init_tasks, return_exceptions=True)
            sys_info_res, sw_version_res, active_titles_res = init_results

            if isinstance(active_titles_res, dict) and active_titles_res:
                cleaned_names = {str(ch): name for ch, name in active_titles_res.items()}
                if cleaned_names != self.config_entry.data.get("channel_names", {}):
                    self.hass.config_entries.async_update_entry(
                        self.config_entry,
                        data={**self.config_entry.data, "channel_names": cleaned_names},
                    )

            remote_dev_res = await self.client.get_remote_devices()
            if isinstance(remote_dev_res, dict):
                self._cached_remote_devices = remote_dev_res
            if isinstance(sys_info_res, dict):
                self._cached_system_info = sys_info_res
            if isinstance(sw_version_res, str) and sw_version_res.strip() and sw_version_res.strip() != "Unknown":
                self._cached_software_version = sw_version_res.strip()
            elif isinstance(self._cached_system_info, dict):
                self._cached_software_version = (
                    self._cached_system_info.get("softwareVersion")
                    or self._cached_system_info.get("version")
                )

        now = time.monotonic()
        should_check_firmware = (now - self._last_firmware_check) >= FIRMWARE_CHECK_INTERVAL

        # Dynamic tasks: Grouped config (VideoColor + RecordMode + Encode), Monitor mode, Storage info
        tasks: list = [
            self.client.get_batch_config(channels),
            self.client.get_monitor_mode(channel=1),
            self.client.get_storage_info(),
        ]

        t0 = time.monotonic()
        results = await asyncio.gather(*tasks, return_exceptions=True)
        self._last_response_time_ms = round((time.monotonic() - t0) * 1000, 1)
        batch_config, monitor_mode, storage_info = results
        _LOGGER.debug("DVR monitor mode: %r", monitor_mode)

        if should_check_firmware:
            try:
                fw_info = await self.client.check_firmware_update()
                self._cached_firmware_info = fw_info
                self._last_firmware_check = now
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Firmware check skipped: %s", exc)

        def _safe(result: Any, default: Any) -> Any:
            if isinstance(result, Exception):
                _LOGGER.debug("DVR endpoint error (using default): %s", result)
                return default
            return result

        if isinstance(batch_config, (DVRAuthError, DVRConnectionError)):
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                raise UpdateFailed(
                    f"Cannot reach DVR after {self._consecutive_failures} attempts: {batch_config}"
                ) from batch_config
            _LOGGER.warning(
                "DVR poll error (%d/3); keeping previous state: %s",
                self._consecutive_failures,
                batch_config,
            )
            if self.data is not None:
                return self.data
            raise UpdateFailed(f"Cannot reach DVR: {batch_config}") from batch_config

        self._consecutive_failures = 0

        encode_info = {}
        if isinstance(batch_config, tuple) and len(batch_config) == 3:
            video_color, record_modes, encode_info = batch_config
        elif isinstance(batch_config, tuple) and len(batch_config) == 2:
            video_color, record_modes = batch_config
        else:
            video_color, record_modes = {}, {ch: 0 for ch in channels}

        return {
            "system_info": self._cached_system_info or {},
            "software_version": self._cached_software_version,
            "remote_devices": self._cached_remote_devices or {},
            "monitor_mode": _safe(monitor_mode, {"mode": "split8", "group": 0}),
            "video_color": _safe(video_color, {}),
            "storage_info": _safe(storage_info, {}),
            "record_modes": _safe(record_modes, {}),
            "encode_info": _safe(encode_info, {}),
            "ptz_presets": self._cached_ptz_presets,
            "firmware_update": self._cached_firmware_info,
            "online": True,
            "response_time_ms": self._last_response_time_ms,
            "consecutive_failures": self._consecutive_failures,
        }

    async def async_refresh_video_color(self) -> None:
        """Quickly refresh only video color from the DVR and notify listeners (debounced)."""
        if self._refresh_video_color_task and not self._refresh_video_color_task.done():
            self._refresh_video_color_task.cancel()

        async def _debounced_refresh() -> None:
            await asyncio.sleep(1.0)
            try:
                video_color = await self.client.get_video_color()
                if self.data is not None:
                    self.data["video_color"] = video_color
                    self.async_update_listeners()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Failed to quick-refresh video color: %s", exc)

        self._refresh_video_color_task = asyncio.create_task(_debounced_refresh())

    async def async_refresh_record_modes(self) -> None:
        """Quickly refresh only record modes from the DVR and notify listeners."""
        try:
            modes = await self.client.get_record_modes(self.selected_channels)
            if self.data is not None:
                self.data["record_modes"] = modes
                self.async_update_listeners()
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Failed to quick-refresh record modes: %s", exc)


# ---------------------------------------------------------------------------
# Event coordinator — push stream for motion / alarm / video-loss sensors
# ---------------------------------------------------------------------------


class IntelbrasEventCoordinator(DataUpdateCoordinator[dict[int, dict[str, bool]]]):
    """
    Push-based coordinator dedicated to alarm / motion event state.

    Maintains a persistent HTTP stream via eventManager.cgi?action=attach,
    streaming binary sensor changes (motion, video loss, video blind)
    with sub-second latency and zero repeated DVR logins.
    """

    config_entry: ConfigEntry

    def __init__(
        self,
        hass: HomeAssistant,
        entry: ConfigEntry,
        client: IntelbrasClient,
        main_coordinator: IntelbrasCoordinator | None = None,
    ) -> None:
        """Initialise the event coordinator."""
        super().__init__(
            hass,
            _LOGGER,
            name=f"{DOMAIN}_{entry.data.get('host', 'dvr')}_events",
            update_interval=None,  # Push-based: events arrive via persistent HTTP stream
        )
        self.client = client
        self.config_entry = entry
        self.snapshot_interval: float = float(
            entry.options.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL)
        )
        self.main_coordinator = main_coordinator
        self._unmasked_suppression: dict[int, float] = {}
        self._stream_task: asyncio.Task[None] | None = None
        self._stop_event = asyncio.Event()

        self.stream_connected: bool = False
        self.reconnect_count: int = 0
        self.last_heartbeat: str | None = None
        self._pulse_reset_tasks: dict[tuple[int, str], asyncio.TimerHandle] = {}

    def suppress_video_blind(self, channel: int, duration: float = 30.0) -> None:
        """Suppress video_blind false alarms when privacy mask is toggled."""
        self._unmasked_suppression[channel] = time.monotonic() + duration

    @property
    def selected_channels(self) -> list[int]:
        """Return the zero-based channel indexes chosen by the user."""
        return list(self.config_entry.data.get(CONF_CHANNELS, []))

    async def async_start(self) -> None:
        """Start the background stream listener task."""
        self._stop_event.clear()
        if self._stream_task is None or self._stream_task.done():
            self._stream_task = self.hass.async_create_background_task(
                self._run_stream(),
                name=f"{self.name}_stream",
            )

    async def async_stop(self) -> None:
        """Stop the background stream listener task."""
        self._stop_event.set()
        for handle in self._pulse_reset_tasks.values():
            handle.cancel()
        self._pulse_reset_tasks.clear()

        if self._stream_task and not self._stream_task.done():
            self._stream_task.cancel()
            try:
                await self._stream_task
            except asyncio.CancelledError:
                pass
            self._stream_task = None

    def _is_video_blind_suppressed(self, channel: int) -> bool:
        """Check if video_blind is suppressed by privacy mask or timer."""
        now = time.monotonic()
        main_coord = self.main_coordinator
        if main_coord is None:
            runtime = getattr(self.config_entry, "runtime_data", None)
            if runtime is not None:
                main_coord = getattr(runtime, "coordinator", None)
        video_color_data = (main_coord.data or {}).get("video_color", {}) if main_coord else {}

        suppress_until = self._unmasked_suppression.get(channel, 0.0)
        vc = video_color_data.get(channel, {})
        is_masked = bool(vc) and vc.get("Brightness", 50) == 0 and vc.get("Saturation", 80) == 0
        is_suppressed = now < suppress_until
        return is_masked or is_suppressed

    def _handle_event(self, channel: int, sensor_key: str, is_active: bool) -> None:
        """Process and dispatch an event state change to Home Assistant listeners."""
        if self.data is None:
            self.data = {
                ch: {"motion": False, "video_loss": False, "video_blind": False}
                for ch in self.selected_channels
            }

        if channel not in self.data:
            self.data[channel] = {"motion": False, "video_loss": False, "video_blind": False}

        pulse_key = (channel, sensor_key)
        if pulse_key in self._pulse_reset_tasks:
            self._pulse_reset_tasks.pop(pulse_key).cancel()

        if sensor_key == "video_blind" and is_active:
            if self._is_video_blind_suppressed(channel):
                _LOGGER.debug(
                    "Suppressing video_blind event on channel %d due to mask/suppression",
                    channel,
                )
                is_active = False
            elif channel in self._unmasked_suppression:
                del self._unmasked_suppression[channel]

        current_val = self.data[channel].get(sensor_key)
        if current_val != is_active:
            self.data[channel][sensor_key] = is_active
            self.async_set_updated_data(dict(self.data))

    def _handle_stream_event(
        self,
        channel: int,
        sensor_key: str,
        is_active: bool,
        is_pulse: bool = False,
    ) -> None:
        """Callback invoked by the stream listener."""
        self._handle_event(channel, sensor_key, is_active)
        if is_pulse and is_active:
            pulse_key = (channel, sensor_key)
            self._pulse_reset_tasks[pulse_key] = self.hass.loop.call_later(
                5.0,
                self._handle_event,
                channel,
                sensor_key,
                False,
            )

    def _apply_baseline(self, status: dict[int, dict[str, bool]]) -> None:
        """Merge baseline event status into current coordinator state."""
        channels = self.selected_channels
        filtered: dict[int, dict[str, bool]] = {}
        for ch in channels:
            ch_status = status.get(ch, {"motion": False, "video_loss": False, "video_blind": False})
            ch_dict = dict(ch_status)
            if self._is_video_blind_suppressed(ch):
                ch_dict["video_blind"] = False
            filtered[ch] = ch_dict

        self.data = filtered
        self.async_set_updated_data(filtered)

    def _handle_heartbeat(self) -> None:
        """Invoked on heartbeat or line received from stream."""
        was_connected = self.stream_connected
        self.stream_connected = True
        self.last_heartbeat = time.strftime("%Y-%m-%d %H:%M:%S")
        if not was_connected:
            self.async_set_updated_data(dict(self.data or {}))

    async def _run_stream(self) -> None:
        """Persistent background worker that connects to the event stream with auto-reconnect."""
        backoff = 2.0
        while not self._stop_event.is_set():
            try:
                _LOGGER.info("Starting Intelbras DVR persistent event stream...")
                channels = self.selected_channels
                try:
                    baseline = await self.client.get_event_status(channels)
                    self._apply_baseline(baseline)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Initial baseline event fetch failed before stream attach: %s", exc)

                self.stream_connected = True
                self.last_heartbeat = time.strftime("%Y-%m-%d %H:%M:%S")
                self.async_set_updated_data(dict(self.data or {}))
                backoff = 2.0
                await self.client.listen_event_stream(
                    event_callback=self._handle_stream_event,
                    codes=["VideoMotion", "SmartMotionHuman", "SmartMotionVehicle", "VideoLoss", "VideoBlind"],
                    heartbeat=5,
                    stop_event=self._stop_event,
                    heartbeat_callback=self._handle_heartbeat,
                )
            except asyncio.CancelledError:
                self.stream_connected = False
                self.async_set_updated_data(dict(self.data or {}))
                _LOGGER.debug("Intelbras DVR event stream worker cancelled.")
                break
            except Exception as exc:  # noqa: BLE001
                self.stream_connected = False
                self.reconnect_count += 1
                self.async_set_updated_data(dict(self.data or {}))
                if self._stop_event.is_set():
                    break
                _LOGGER.warning(
                    "Intelbras DVR event stream disconnected (%s). Reconnecting in %.1fs...",
                    exc,
                    backoff,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=backoff)
                except asyncio.TimeoutError:
                    pass
                backoff = min(backoff * 2, 60.0)

    async def _async_update_data(self) -> dict[int, dict[str, bool]]:
        """Fetch active alarm / event indexes from the DVR on demand (manual refresh)."""
        channels = self.selected_channels
        try:
            status = await self.client.get_event_status(channels)
            filtered: dict[int, dict[str, bool]] = {}
            for ch in channels:
                ch_status = status.get(ch, {"motion": False, "video_loss": False, "video_blind": False})
                ch_dict = dict(ch_status)
                if self._is_video_blind_suppressed(ch):
                    ch_dict["video_blind"] = False
                filtered[ch] = ch_dict
            return filtered
        except (DVRConnectionError, DVRAuthError) as exc:
            _LOGGER.debug("Event poll skipped (DVR unreachable): %s", exc)
            return self.data or {ch: {"motion": False, "video_loss": False, "video_blind": False} for ch in channels}
