"""DataUpdateCoordinators for the Intelbras DVR integration."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import timedelta
from typing import Any, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHANNELS,
    CONF_MONITOR_EVENTS,
    CONF_MONITOR_NETWORK,
    CONF_MONITOR_STORAGE,
    CONF_MONITOR_USERS,
    CONF_PTZ_CHANNELS,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_BRIGHTNESS,
    DEFAULT_SATURATION_ACUTANCE,
    DEFAULT_SCAN_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
    FIRMWARE_CHECK_INTERVAL,
    LOGIN_LOCKOUT_BACKOFF,
    RECORD_MODE_SCHEDULE,
)
from .dvr import DVRAuthError, DVRConnectionError, IntelbrasClient

_LOGGER = logging.getLogger(__name__)


class DVRRuntimeState(TypedDict, total=False):
    """Typed dictionary representing the main coordinator state."""

    system_info: dict[str, str]
    software_version: str | None
    remote_devices: dict[int, dict[str, Any]]
    monitor_caps: list[int]
    total_channels: int
    monitor_mode: dict[str, Any]
    video_color: dict[int, dict[str, int]]
    storage_info: dict[str, Any]
    record_modes: dict[int, int]
    encode_info: dict[int, dict[str, Any]]
    ptz_presets: dict[int, dict[int, str]]
    firmware_update: dict[str, Any]
    active_users: list[dict[str, Any]]
    storage_diagnostics: dict[str, Any]
    network_interfaces: list[dict[str, Any]]
    system_time: str | None
    online: bool
    response_time_ms: float | None
    consecutive_failures: int


# ---------------------------------------------------------------------------
# Main coordinator — slow poll for system info, video colour, disk, PTZ presets
# ---------------------------------------------------------------------------


class IntelbrasCoordinator(DataUpdateCoordinator[DVRRuntimeState]):
    """Coordinate periodic data refresh from the Intelbras DVR.

    Polls every DEFAULT_SCAN_INTERVAL seconds (default 60 s).
    Manages heavier data: system info, video colour, disk, PTZ presets,
    recording modes, monitor capabilities, and firmware update checks.
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
        self._auth_probe_failures: int = 0
        self._auth_issue_created: bool = False
        self._cleared_startup_issue: bool = False
        lockout_state = (
            hass.data.get(DOMAIN, {})
            .get("auth_lockout_state", {})
            .get(entry.entry_id)
        )
        if isinstance(lockout_state, dict):
            self._auth_probe_failures = int(lockout_state.get("streak", 0))
            if self._auth_probe_failures >= 3:
                self._auth_issue_created = True
        self._last_response_time_ms: float | None = None

        self._cached_system_info: dict[str, str] | None = None
        self._cached_software_version: str | None = None
        self._cached_remote_devices: dict[int, dict[str, Any]] | None = None
        self._cached_monitor_caps: list[int] | None = None
        self._titles_synced: bool = False
        self._cached_ptz_presets: dict[int, dict[int, str]] = {}
        self._presets_loaded: bool = False
        self._last_firmware_check: float = 0.0
        self._cached_firmware_info: dict[str, Any] = {
            "available": False,
            "latest_version": None,
            "release_notes": None,
        }
        self._refresh_video_color_task: asyncio.Task[None] | None = None
        self._refresh_monitor_task: asyncio.Task[None] | None = None

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
        """Fetch PTZ presets sequentially on startup or entry reconfiguration."""
        ptz_chs = self.ptz_channels
        if not ptz_chs:
            self._presets_loaded = True
            return
        _LOGGER.debug(
            "Fetching PTZ presets on initialization/reconfiguration for channels: %s",
            ptz_chs,
        )
        for ch in ptz_chs:
            if self.client.auth_lockout_remaining > 0:
                return
            try:
                res = await self.client.get_ptz_presets(ch)
                if isinstance(res, dict):
                    self._cached_ptz_presets[ch] = res
            except DVRAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning(
                    "Could not fetch PTZ presets for channel %d: %s", ch, exc
                )
        self._presets_loaded = True

    def _publish_auth_lockout(self) -> None:
        """Persist auth lockout deadline and streak in hass.data across reloads."""
        state_map = self.hass.data.setdefault(DOMAIN, {}).setdefault(
            "auth_lockout_state", {}
        )
        state_map[self.config_entry.entry_id] = {
            "deadline": self.client.lockout_deadline,
            "streak": max(self._auth_probe_failures, self.client.auth_failure_streak),
        }

    def _clear_auth_issue(self) -> None:
        """Reset auth failure counters and clear any active Repairs warning."""
        had_auth_issue = (
            self._auth_probe_failures > 0
            or self._auth_issue_created
            or not self._cleared_startup_issue
        )
        self._auth_probe_failures = 0
        self._auth_issue_created = False
        self._cleared_startup_issue = True
        self.hass.data.get(DOMAIN, {}).get("auth_lockout_state", {}).pop(
            self.config_entry.entry_id, None
        )
        if had_auth_issue:
            ir.async_delete_issue(
                self.hass,
                DOMAIN,
                f"auth_retrying_{self.config_entry.entry_id}",
            )

    def _handle_auth_error(
        self, exc: Exception | None = None, *, probed: bool = True
    ) -> DVRRuntimeState:
        """Handle auth failure cleanly without blocking polling or forcing reauth."""
        self._consecutive_failures += 1
        if probed:
            self._auth_probe_failures = max(
                self._auth_probe_failures + 1, self.client.auth_failure_streak
            )
            self._publish_auth_lockout()

        if self._auth_probe_failures >= 3 and not self._auth_issue_created:
            ir.async_create_issue(
                self.hass,
                DOMAIN,
                f"auth_retrying_{self.config_entry.entry_id}",
                is_fixable=False,
                severity=ir.IssueSeverity.WARNING,
                translation_key="auth_retrying",
                translation_placeholders={
                    "host": str(
                        self.config_entry.data.get(CONF_HOST, self.client.host)
                    ),
                },
            )
            self._auth_issue_created = True

        remaining = self.client.auth_lockout_remaining
        if self._consecutive_failures >= 3 or self.data is None:
            retry_after = (
                max(float(DEFAULT_SCAN_INTERVAL), remaining + 2.0)
                if remaining > 0
                else None
            )
            raise UpdateFailed(
                translation_domain=DOMAIN,
                translation_key="auth_failed",
                retry_after=retry_after,
            ) from exc

        _LOGGER.warning(
            "DVR authentication error (%d/3); keeping previous state and retrying automatically (cooldown %.0fs): %s",
            self._consecutive_failures,
            remaining,
            exc or "anti-lockout cooldown active",
        )
        return self.data

    async def _async_update_data(self) -> DVRRuntimeState:
        """Fetch fresh state from the DVR sequentially with minimal overhead."""
        channels = self.selected_channels

        if self.client.auth_lockout_remaining > 0:
            return self._handle_auth_error(probed=False)

        try:
            # Fetch PTZ presets only on startup or reconfig
            if not self._presets_loaded:
                await self.async_load_ptz_presets()

            # Cache static system info / software version / remote devices / monitor caps on first run
            if (
                self._cached_system_info is None
                or self._cached_software_version is None
                or self._cached_remote_devices is None
                or self._cached_monitor_caps is None
            ):
                if self._cached_system_info is None:
                    try:
                        sys_info_res = await self.client.get_system_info()
                        if isinstance(sys_info_res, dict):
                            self._cached_system_info = sys_info_res
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_system_info error: %s", exc)

                if self._cached_software_version is None:
                    try:
                        sw_version_res = await self.client.get_software_version()
                        if (
                            isinstance(sw_version_res, str)
                            and sw_version_res.strip()
                            and sw_version_res.strip() != "Unknown"
                        ):
                            self._cached_software_version = sw_version_res.strip()
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_software_version error: %s", exc)

                    if (
                        self._cached_software_version is None
                        and isinstance(self._cached_system_info, dict)
                    ):
                        self._cached_software_version = (
                            self._cached_system_info.get("softwareVersion")
                            or self._cached_system_info.get("version")
                        )

                if not self._titles_synced:
                    try:
                        active_titles_res = await self.client.get_channel_titles()
                        if isinstance(active_titles_res, dict) and active_titles_res:
                            cleaned_names = {
                                str(ch): name for ch, name in active_titles_res.items()
                            }
                            if cleaned_names != self.config_entry.data.get(
                                "channel_names", {}
                            ):
                                self.hass.config_entries.async_update_entry(
                                    self.config_entry,
                                    data={
                                        **self.config_entry.data,
                                        "channel_names": cleaned_names,
                                    },
                                )
                            self._titles_synced = True
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_channel_titles error: %s", exc)

                if self._cached_monitor_caps is None:
                    try:
                        monitor_caps_res = await self.client.get_monitor_caps()
                        if isinstance(monitor_caps_res, list) and monitor_caps_res:
                            self._cached_monitor_caps = monitor_caps_res
                        else:
                            self._cached_monitor_caps = [1, 4, 8, 9, 16]
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_monitor_caps error: %s", exc)
                        self._cached_monitor_caps = [1, 4, 8, 9, 16]

                if self._cached_remote_devices is None:
                    try:
                        remote_dev_res = await self.client.get_remote_devices()
                        if isinstance(remote_dev_res, dict):
                            self._cached_remote_devices = remote_dev_res
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_remote_devices error: %s", exc)

            now = time.monotonic()
            should_check_firmware = (
                now - self._last_firmware_check
            ) >= FIRMWARE_CHECK_INTERVAL

            t0 = time.monotonic()
            batch_config = await self.client.get_batch_config(channels)

            monitor_mode: dict[str, Any] = {}
            try:
                monitor_mode = await self.client.get_monitor_mode(channel=1)
            except DVRAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("DVR endpoint error (using default): %s", exc)

            storage_info: dict[str, Any] = {}
            try:
                storage_info = await self.client.get_storage_info()
            except DVRAuthError:
                raise
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("DVR endpoint error (using default): %s", exc)

            active_users: list[dict[str, Any]] = []
            if self.config_entry.data.get(CONF_MONITOR_USERS, True):
                try:
                    active_users = await self.client.get_active_users()
                except DVRAuthError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("DVR get_active_users error: %s", exc)

            storage_diagnostics: dict[str, Any] = {}
            if self.config_entry.data.get(CONF_MONITOR_STORAGE, True):
                try:
                    storage_diagnostics = await self.client.get_storage_diagnostics()
                except DVRAuthError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("DVR get_storage_diagnostics error: %s", exc)

            network_interfaces: list[dict[str, Any]] = []
            if self.config_entry.data.get(CONF_MONITOR_NETWORK, True):
                try:
                    network_interfaces = await self.client.get_network_interfaces()
                except DVRAuthError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("DVR get_network_interfaces error: %s", exc)

            system_time: str | None = None
            try:
                system_time = await self.client.get_system_time()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("DVR get_system_time error: %s", exc)

            self._last_response_time_ms = round((time.monotonic() - t0) * 1000, 1)
            _LOGGER.debug("DVR monitor mode: %r", monitor_mode)

            if should_check_firmware:
                try:
                    fw_info = await self.client.check_firmware_update()
                    self._cached_firmware_info = fw_info
                    self._last_firmware_check = now
                except DVRAuthError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Firmware check skipped: %s", exc)

        except DVRAuthError as exc:
            return self._handle_auth_error(exc, probed=True)
        except Exception as exc:
            self._consecutive_failures += 1
            if self._consecutive_failures >= 3:
                raise UpdateFailed(
                    f"Cannot reach DVR after {self._consecutive_failures} attempts: {exc}"
                ) from exc
            _LOGGER.warning(
                "DVR poll error (%d/3); keeping previous state: %s",
                self._consecutive_failures,
                exc,
            )
            if self.data is not None:
                return self.data
            raise UpdateFailed(f"Cannot reach DVR: {exc}") from exc

        self._consecutive_failures = 0
        self._clear_auth_issue()

        encode_info: dict[int, dict[str, Any]] = {}
        if isinstance(batch_config, tuple) and len(batch_config) == 3:
            video_color, record_modes, encode_info = batch_config
        elif isinstance(batch_config, tuple) and len(batch_config) == 2:
            video_color, record_modes = batch_config
        else:
            video_color, record_modes = {}, {ch: RECORD_MODE_SCHEDULE for ch in channels}

        total_channels = (
            self.client.total_channels
            or len(self.channel_names)
            or len(channels)
            or 16
        )

        return {
            "system_info": self._cached_system_info or {},
            "software_version": self._cached_software_version,
            "remote_devices": self._cached_remote_devices or {},
            "monitor_caps": self._cached_monitor_caps or [1, 4, 8, 9, 16],
            "total_channels": total_channels,
            "monitor_mode": monitor_mode,
            "video_color": video_color,
            "storage_info": storage_info,
            "record_modes": record_modes,
            "encode_info": encode_info,
            "ptz_presets": self._cached_ptz_presets,
            "firmware_update": self._cached_firmware_info,
            "active_users": active_users,
            "storage_diagnostics": storage_diagnostics,
            "network_interfaces": network_interfaces,
            "system_time": system_time,
            "online": True,
            "response_time_ms": self._last_response_time_ms,
            "consecutive_failures": self._consecutive_failures,
        }

    async def async_refresh_storage(self) -> None:
        """Force a fresh query of storage diagnostics and notify listeners."""
        try:
            diag = await self.client.get_storage_diagnostics()
            if self.data is not None:
                self.data["storage_diagnostics"] = diag
                self.async_set_updated_data(dict(self.data))
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Manual storage refresh failed: %s", exc)

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

    async def async_refresh_monitor_mode(self) -> None:
        """Quickly refresh only the physical monitor mode in the background (debounced)."""
        if self._refresh_monitor_task and not self._refresh_monitor_task.done():
            self._refresh_monitor_task.cancel()

        async def _debounced_monitor_refresh() -> None:
            try:
                await asyncio.sleep(0.5)
                mode_info = await self.client.get_monitor_mode(channel=1)
                if mode_info and self.data is not None:
                    self.data["monitor_mode"] = mode_info
                    self.async_update_listeners()
            except asyncio.CancelledError:
                return
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Failed to quick-refresh monitor mode: %s", exc)

        self._refresh_monitor_task = asyncio.create_task(_debounced_monitor_refresh())


# ---------------------------------------------------------------------------
# Event coordinator — push stream for motion / alarm / video-loss sensors
# ---------------------------------------------------------------------------


class IntelbrasEventCoordinator(DataUpdateCoordinator[dict[int, dict[str, bool]]]):
    """Push-based coordinator dedicated to alarm / motion event state.

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
        self._stream_backoff: float = 2.0
        self._baseline_fresh_until: float = 0.0
        self._pulse_reset_tasks: dict[tuple[int, str], asyncio.TimerHandle] = {}
        self.system_events: dict[str, bool] = {
            "storage_not_exist": False,
            "storage_failure": False,
            "storage_low_space": False,
            "login_failure": False,
        }
        self._system_reset_tasks: dict[str, asyncio.TimerHandle] = {}

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
        for handle in self._system_reset_tasks.values():
            handle.cancel()
        self._system_reset_tasks.clear()

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
        video_color_data = (
            (main_coord.data or {}).get("video_color", {}) if main_coord else {}
        )

        suppress_until = self._unmasked_suppression.get(channel, 0.0)
        vc = video_color_data.get(channel, {})
        is_masked = (
            bool(vc)
            and vc.get("Brightness", DEFAULT_BRIGHTNESS) == 0
            and vc.get("Saturation", DEFAULT_SATURATION_ACUTANCE) == 0
        )
        is_suppressed = now < suppress_until
        return is_masked or is_suppressed

    def _handle_system_event(
        self, sensor_key: str, is_active: bool, is_pulse: bool = False
    ) -> None:
        """Handle system-level events (storage, login failure)."""
        if sensor_key in self._system_reset_tasks:
            self._system_reset_tasks.pop(sensor_key).cancel()

        self.system_events[sensor_key] = is_active
        self.async_set_updated_data(dict(self.data or {}))

        if is_active and (is_pulse or sensor_key == "login_failure"):
            self._system_reset_tasks[sensor_key] = self.hass.loop.call_later(
                30.0,
                self._handle_system_event,
                sensor_key,
                False,
                False,
            )

    def _handle_event(self, channel: int, sensor_key: str, is_active: bool) -> None:
        """Process and dispatch an event state change to Home Assistant listeners."""
        if self.data is None:
            self.data = {
                ch: {
                    "motion": False,
                    "video_loss": False,
                    "video_blind": False,
                    "cross_line": False,
                    "cross_region": False,
                }
                for ch in self.selected_channels
            }

        if channel not in self.data:
            self.data[channel] = {
                "motion": False,
                "video_loss": False,
                "video_blind": False,
                "cross_line": False,
                "cross_region": False,
            }

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
        if sensor_key in self.system_events:
            self._handle_system_event(sensor_key, is_active, is_pulse)
            return

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
            ch_status = status.get(
                ch,
                {
                    "motion": False,
                    "video_loss": False,
                    "video_blind": False,
                    "cross_line": False,
                    "cross_region": False,
                },
            )
            ch_dict = dict(ch_status)
            if self._is_video_blind_suppressed(ch):
                ch_dict["video_blind"] = False
            filtered[ch] = ch_dict

        self.data = filtered
        self.async_set_updated_data(filtered)

    def _handle_heartbeat(self) -> None:
        """Invoked on stream connection, heartbeat, or event line received."""
        was_connected = self.stream_connected
        self.stream_connected = True
        self._stream_backoff = 2.0
        self.last_heartbeat = dt_util.now().isoformat(timespec="seconds")
        if not was_connected:
            self.async_set_updated_data(dict(self.data or {}))

    async def _run_stream(self) -> None:
        """Persistent background worker that connects to the event stream with auto-reconnect."""
        self._stream_backoff = 2.0
        while not self._stop_event.is_set():
            try:
                _LOGGER.info("Starting Intelbras DVR persistent event stream...")
                channels = self.selected_channels
                if time.monotonic() > self._baseline_fresh_until or self.data is None:
                    try:
                        baseline = await self.client.get_event_status(channels)
                        self._apply_baseline(baseline)
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug(
                            "Initial baseline event fetch failed before stream attach: %s",
                            exc,
                        )

                await self.client.listen_event_stream(
                    event_callback=self._handle_stream_event,
                    codes=[
                        "VideoMotion",
                        "SmartMotionHuman",
                        "SmartMotionVehicle",
                        "VideoLoss",
                        "VideoBlind",
                    ],
                    heartbeat=5,
                    stop_event=self._stop_event,
                    heartbeat_callback=self._handle_heartbeat,
                )
                if not self._stop_event.is_set():
                    self.stream_connected = False
                    self.async_set_updated_data(dict(self.data or {}))
                    try:
                        await asyncio.wait_for(self._stop_event.wait(), timeout=1.0)
                    except asyncio.TimeoutError:
                        pass
            except asyncio.CancelledError:
                self.stream_connected = False
                self.async_set_updated_data(dict(self.data or {}))
                _LOGGER.debug("Intelbras DVR event stream worker cancelled.")
                break
            except DVRAuthError as exc:
                self.stream_connected = False
                self.reconnect_count += 1
                self.async_set_updated_data(dict(self.data or {}))
                if self._stop_event.is_set():
                    break
                wait_for = max(
                    self.client.auth_lockout_remaining + 2.0, LOGIN_LOCKOUT_BACKOFF
                )
                _LOGGER.warning(
                    "Intelbras DVR event stream auth cooldown (%s). Retrying in %.1fs...",
                    exc,
                    wait_for,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait_for)
                except asyncio.TimeoutError:
                    pass
            except Exception as exc:  # noqa: BLE001
                self.stream_connected = False
                self.reconnect_count += 1
                self.async_set_updated_data(dict(self.data or {}))
                if self._stop_event.is_set():
                    break
                wait_for = self._stream_backoff
                _LOGGER.warning(
                    "Intelbras DVR event stream disconnected (%s). Reconnecting in %.1fs...",
                    exc,
                    wait_for,
                )
                try:
                    await asyncio.wait_for(self._stop_event.wait(), timeout=wait_for)
                except asyncio.TimeoutError:
                    pass
                self._stream_backoff = min(self._stream_backoff * 2, 60.0)

    async def _async_update_data(self) -> dict[int, dict[str, bool]]:
        """Fetch active alarm / event indexes from the DVR on demand (manual refresh)."""
        channels = self.selected_channels
        try:
            status = await self.client.get_event_status(channels)
            filtered: dict[int, dict[str, bool]] = {}
            for ch in channels:
                ch_status = status.get(
                    ch, {"motion": False, "video_loss": False, "video_blind": False}
                )
                ch_dict = dict(ch_status)
                if self._is_video_blind_suppressed(ch):
                    ch_dict["video_blind"] = False
                filtered[ch] = ch_dict
            self._baseline_fresh_until = time.monotonic() + 5.0
            return filtered
        except (DVRConnectionError, DVRAuthError) as exc:
            _LOGGER.debug("Event poll skipped (DVR unreachable): %s", exc)
            return self.data or {
                ch: {"motion": False, "video_loss": False, "video_blind": False}
                for ch in channels
            }
