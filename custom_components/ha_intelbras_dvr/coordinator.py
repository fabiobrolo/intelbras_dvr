"""DataUpdateCoordinators for the Intelbras DVR integration."""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import time as dt_time, timedelta
from typing import Any, TypedDict

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers import issue_registry as ir
from homeassistant.helpers.update_coordinator import DataUpdateCoordinator, UpdateFailed
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
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
    SLOW_DIAGNOSTICS_INTERVAL,
    CameraFeature,
)
from .dvr import DVRAuthError, DVRConnectionError, IntelbrasClient

_LOGGER = logging.getLogger(__name__)


class DVRRuntimeState(TypedDict, total=False):
    """Typed dictionary representing the main coordinator state."""

    system_info: dict[str, str]
    software_version: str | None
    hardware_version: str | None
    remote_devices: dict[int, dict[str, Any]]
    monitor_caps: list[int]
    total_channels: int
    monitor_mode: dict[str, Any]
    video_color: dict[int, dict[str, int]]
    storage_info: dict[str, Any]
    record_modes: dict[int, int]
    encode_info: dict[int, dict[str, Any]]
    motion_detect: dict[int, dict[str, Any]]
    video_widget: dict[int, dict[str, Any]]
    video_in_options: dict[int, dict[str, Any]]
    video_input_caps: dict[int, dict[str, Any]]
    video_denoise: dict[int, dict[str, Any]]
    image_enhancement: dict[int, dict[str, Any]]
    ptz_presets: dict[int, dict[int, str]]
    ptz_status: dict[int, dict[str, Any]]
    firmware_update: dict[str, Any]
    active_users: list[dict[str, Any]]
    user_permissions: dict[str, Any]
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
        saved_names = entry.data.get("channel_names", {})
        if saved_names:
            self.client._channel_names.update({int(k): v for k, v in saved_names.items()})
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
        self._cached_hardware_version: str | None = None
        self._hw_version_loaded: bool = False
        self._cached_remote_devices: dict[int, dict[str, Any]] | None = None
        self._cached_monitor_caps: list[int] | None = None
        self._cached_user_permissions: dict[str, Any] | None = None
        self._titles_synced: bool = False
        self._cached_ptz_presets: dict[int, dict[int, str]] = {}
        self._presets_loaded: bool = False
        self._video_caps_loaded: bool = False
        self._last_slow_poll: float = 0.0
        self._cached_motion_detect: dict[int, dict[str, Any]] = {}
        self._cached_storage_info: dict[str, Any] = {}
        self._cached_storage_diagnostics: dict[str, Any] = {}
        self._cached_network_interfaces: list[dict[str, Any]] = []
        self._cached_ptz_status: dict[int, dict[str, Any]] = {}
        self._last_firmware_check: float = 0.0
        self._cached_firmware_info: dict[str, Any] = {
            "available": False,
            "latest_version": None,
            "release_notes": None,
        }
        self._refresh_video_color_task: asyncio.Task[None] | None = None
        self._refresh_monitor_task: asyncio.Task[None] | None = None
        self._selected_config_profile: dict[int, str] = {}

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

                if not self._hw_version_loaded:
                    try:
                        self._cached_hardware_version = (
                            await self.client.get_hardware_version()
                        )
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_hardware_version error: %s", exc)
                    finally:
                        self._hw_version_loaded = True

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

            if not self._video_caps_loaded:
                try:
                    await self.client.async_discover_video_input_caps(channels)
                    self._video_caps_loaded = True
                except DVRAuthError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("DVR getCaps discovery error: %s", exc)

            if self._cached_user_permissions is None:
                try:
                    perms = await self.client.async_validate_user_permissions(channels)
                    if isinstance(perms, dict):
                        self._cached_user_permissions = perms
                        if perms.get("checked", False):
                            host_str = str(
                                self.config_entry.data.get(CONF_HOST, self.client.host)
                            )
                            if not perms.get("has_all_permissions", True):
                                missing_list = perms.get("missing_permissions", [])
                                ir.async_create_issue(
                                    self.hass,
                                    DOMAIN,
                                    f"insufficient_permissions_{self.config_entry.entry_id}",
                                    is_fixable=False,
                                    severity=ir.IssueSeverity.WARNING,
                                    translation_key="insufficient_permissions",
                                    translation_placeholders={
                                        "username": self.client.username,
                                        "host": host_str,
                                        "missing_permissions": ", ".join(missing_list[:10]),
                                    },
                                )
                            else:
                                ir.async_delete_issue(
                                    self.hass,
                                    DOMAIN,
                                    f"insufficient_permissions_{self.config_entry.entry_id}",
                                )

                            if not perms.get("sharable", True):
                                ir.async_create_issue(
                                    self.hass,
                                    DOMAIN,
                                    f"user_not_sharable_{self.config_entry.entry_id}",
                                    is_fixable=False,
                                    severity=ir.IssueSeverity.WARNING,
                                    translation_key="user_not_sharable",
                                    translation_placeholders={
                                        "username": self.client.username,
                                        "host": host_str,
                                    },
                                )
                            else:
                                ir.async_delete_issue(
                                    self.hass,
                                    DOMAIN,
                                    f"user_not_sharable_{self.config_entry.entry_id}",
                                )
                except DVRAuthError:
                    raise
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("DVR user permission check error: %s", exc)

            now = time.monotonic()
            should_run_slow_poll = (
                self._last_slow_poll == 0.0
                or (now - self._last_slow_poll) >= SLOW_DIAGNOSTICS_INTERVAL
            )
            should_check_firmware = (
                now - self._last_firmware_check
            ) >= FIRMWARE_CHECK_INTERVAL

            t0 = time.monotonic()
            batch_config = await self.client.get_batch_config(
                channels, include_extended=should_run_slow_poll
            )

            monitor_mode: dict[str, Any] = {}
            try:
                monitor_mode = await self.client.get_monitor_mode(channel=1)
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

            system_time: str | None = None
            try:
                system_time = await self.client.get_system_time()
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("DVR get_system_time error: %s", exc)

            storage_info: dict[str, Any] = self._cached_storage_info
            storage_diagnostics: dict[str, Any] = self._cached_storage_diagnostics
            network_interfaces: list[dict[str, Any]] = self._cached_network_interfaces

            if should_run_slow_poll:
                if self.config_entry.data.get(CONF_MONITOR_STORAGE, True):
                    try:
                        diag = await self.client.get_storage_diagnostics()
                        raw_info = diag.pop("raw_info", None)
                        storage_diagnostics = diag
                        self._cached_storage_diagnostics = diag
                        if isinstance(raw_info, dict) and raw_info:
                            storage_info = raw_info
                            self._cached_storage_info = raw_info
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_storage_diagnostics error: %s", exc)
                else:
                    try:
                        s_info = await self.client.get_storage_info()
                        if s_info:
                            storage_info = s_info
                            self._cached_storage_info = s_info
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_storage_info error: %s", exc)

                if self.config_entry.data.get(CONF_MONITOR_NETWORK, True):
                    try:
                        network_interfaces = await self.client.get_network_interfaces()
                        self._cached_network_interfaces = network_interfaces
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_network_interfaces error: %s", exc)

                features_map: dict[str, list[str]] = self.config_entry.data.get(
                    CONF_FEATURES, {}
                )
                has_motion_feature = any(
                    features_map.get(str(ch)) is None
                    or CameraFeature.MOTION in features_map.get(str(ch), [])
                    for ch in channels
                )
                if has_motion_feature:
                    try:
                        md = await self.client.get_motion_detect_config()
                        if isinstance(md, dict) and md:
                            self._cached_motion_detect = md
                    except DVRAuthError:
                        raise
                    except Exception as exc:  # noqa: BLE001
                        _LOGGER.debug("DVR get_motion_detect_config error: %s", exc)

            self._last_response_time_ms = round((time.monotonic() - t0) * 1000, 1)
            _LOGGER.debug(
                "DVR poll completed in %.1f ms (slow_poll=%s, monitor_mode=%r)",
                self._last_response_time_ms,
                should_run_slow_poll,
                monitor_mode,
            )

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
        video_widget: dict[int, dict[str, Any]] = {}
        video_in_options: dict[int, dict[str, Any]] = {}
        if isinstance(batch_config, tuple) and len(batch_config) == 5:
            video_color, record_modes, encode_info, video_widget, video_in_options = batch_config
        elif isinstance(batch_config, tuple) and len(batch_config) == 3:
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

        ptz_status: dict[int, dict[str, Any]] = self._cached_ptz_status
        if should_run_slow_poll:
            fresh_ptz: dict[int, dict[str, Any]] = {}
            for ptz_ch in self.ptz_channels:
                try:
                    st = await self.client.get_ptz_status(ptz_ch)
                    if st:
                        fresh_ptz[ptz_ch] = st
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("Could not fetch PTZ status for channel %d: %s", ptz_ch, exc)
            if fresh_ptz:
                self._cached_ptz_status = fresh_ptz
                ptz_status = fresh_ptz
            self._last_slow_poll = now

        return {
            "system_info": self._cached_system_info or {},
            "software_version": self._cached_software_version,
            "hardware_version": self._cached_hardware_version,
            "remote_devices": self._cached_remote_devices or {},
            "monitor_caps": self._cached_monitor_caps or [1, 4, 8, 9, 16],
            "total_channels": total_channels,
            "monitor_mode": monitor_mode,
            "video_color": video_color,
            "storage_info": storage_info,
            "record_modes": record_modes,
            "encode_info": encode_info,
            "motion_detect": self._cached_motion_detect,
            "video_widget": video_widget,
            "video_in_options": video_in_options,
            "video_input_caps": self.client._video_input_caps,
            "video_denoise": self.client._video_denoise,
            "image_enhancement": self.client._image_enhancement,
            "ptz_presets": self._cached_ptz_presets,
            "ptz_status": ptz_status,
            "firmware_update": self._cached_firmware_info,
            "active_users": active_users,
            "user_permissions": self._cached_user_permissions or {},
            "storage_diagnostics": storage_diagnostics,
            "network_interfaces": network_interfaces,
            "system_time": system_time,
            "online": True,
            "response_time_ms": self._last_response_time_ms,
            "consecutive_failures": self._consecutive_failures,
        }

    async def async_force_full_refresh(self) -> None:
        """Force an immediate full refresh including slow-poll extended and diagnostic tables."""
        self._last_slow_poll = 0.0
        await self.async_request_refresh()

    async def async_refresh_storage(self) -> None:
        """Force a fresh query of storage diagnostics and notify listeners."""
        try:
            diag = await self.client.get_storage_diagnostics()
            raw_info = diag.pop("raw_info", None)
            self._cached_storage_diagnostics = diag
            if isinstance(raw_info, dict) and raw_info:
                self._cached_storage_info = raw_info
            if self.data is not None:
                self.data["storage_diagnostics"] = diag
                if isinstance(raw_info, dict) and raw_info:
                    self.data["storage_info"] = raw_info
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
                batch = await self.client.get_batch_config(self.selected_channels)
                if self.data is not None and isinstance(batch, tuple) and len(batch) >= 5:
                    self.data["video_color"] = batch[0]
                    self.data["video_widget"] = batch[3]
                    self.data["video_in_options"] = batch[4]
                    self.data["video_denoise"] = self.client._video_denoise
                    self.data["image_enhancement"] = self.client._image_enhancement
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

    # -----------------------------------------------------------------------
    # Video Widget (OSD text / overlay) helpers
    # -----------------------------------------------------------------------

    def supports_video_widget(self, channel: int) -> bool:
        """Check if VideoWidget configuration is available for this channel."""
        if not self.data:
            return False
        return channel in self.data.get("video_widget", {})

    def supports_custom_title(self, channel: int) -> bool:
        """Check if Custom Title overlay is supported for this channel.

        Returns False on this DVR series because IP cameras do not have
        CustomTitle in VideoWidget and analog cameras lack rendering support.
        """
        return False

    def supports_channel_title(self, channel: int) -> bool:
        """Check if on-screen Channel Title text is supported for this channel."""
        return channel in self.client.channel_names

    def get_channel_title(self, channel: int) -> str:
        """Return the on-screen channel title name."""
        return self.client.channel_names.get(channel, "")

    def supports_channel_title_overlay(self, channel: int) -> bool:
        """Check if channel title overlay toggle is supported."""
        if not self.data:
            return False
        return "channel_title_blend" in self.data.get("video_widget", {}).get(channel, {})

    def supports_time_title_overlay(self, channel: int) -> bool:
        """Check if time title overlay toggle is supported."""
        if not self.data:
            return False
        return "time_title_blend" in self.data.get("video_widget", {}).get(channel, {})

    def get_custom_title_text(self, channel: int) -> str:
        """Return the custom title text for the channel."""
        if not self.data:
            return ""
        return self.data.get("video_widget", {}).get(channel, {}).get("custom_title_text", "")

    def is_custom_title_enabled(self, channel: int) -> bool:
        """Return True if custom title overlay is enabled."""
        if not self.data:
            return False
        return self.data.get("video_widget", {}).get(channel, {}).get("custom_title_blend", False)

    def is_channel_title_enabled(self, channel: int) -> bool:
        """Return True if channel title overlay is enabled."""
        if not self.data:
            return False
        return self.data.get("video_widget", {}).get(channel, {}).get("channel_title_blend", False)

    def is_time_title_enabled(self, channel: int) -> bool:
        """Return True if time title overlay is enabled."""
        if not self.data:
            return False
        return self.data.get("video_widget", {}).get(channel, {}).get("time_title_blend", False)

    # -----------------------------------------------------------------------
    # Camera Type & Classification helpers
    # -----------------------------------------------------------------------

    def is_intelbras_ip(self, channel: int) -> bool:
        """Return True if channel is an Intelbras IP camera (Private protocol)."""
        remotes = self.client.remote_devices or (
            self.data.get("remote_devices", {}) if self.data else {}
        )
        dev = remotes.get(channel)
        if isinstance(dev, dict):
            proto = dev.get("ProtocolType") or dev.get("protocol")
            return proto == "Private"
        return False

    def is_onvif(self, channel: int) -> bool:
        """Return True if channel is an ONVIF IP camera."""
        remotes = self.client.remote_devices or (
            self.data.get("remote_devices", {}) if self.data else {}
        )
        dev = remotes.get(channel)
        if isinstance(dev, dict):
            proto = dev.get("ProtocolType") or dev.get("protocol")
            return proto == "Onvif"
        return False

    def is_analog(self, channel: int) -> bool:
        """Return True if channel is an analog (coaxial) camera."""
        remotes = self.client.remote_devices or (
            self.data.get("remote_devices", {}) if self.data else {}
        )
        return channel not in remotes

    # -----------------------------------------------------------------------
    # Video In Options (Day/Night, WDR, Profiles) helpers
    # -----------------------------------------------------------------------

    def supports_video_in_options(self, channel: int) -> bool:
        """Check if VideoInOptions is available for this channel."""
        if not self.data:
            return False
        return channel in self.data.get("video_in_options", {})

    def supports_day_night(self, channel: int) -> bool:
        """Check if Day/Night mode is supported for this channel."""
        return self.is_intelbras_ip(channel) and self.client.channel_supports_day_night(channel)

    def get_day_night_mode(self, channel: int) -> str:
        """Return Day/Night mode string ('black_white', 'color', 'schedule') for the effective profile."""
        prof = self.get_effective_profile_index(channel)
        return self.client.get_day_night_mode(channel, profile=prof)

    def supports_wdr(self, channel: int) -> bool:
        """Check if Wide Dynamic Range is supported for this channel."""
        if not self.is_intelbras_ip(channel) or not self.data:
            return False
        vio = self.data.get("video_in_options", {}).get(channel, {})
        return "wdr_mode" in vio or "normal_wdr_mode" in vio or "night_wdr_mode" in vio

    def is_wdr_enabled(self, channel: int) -> bool:
        """Return True if WDR is enabled in the currently effective profile."""
        if not self.data:
            return False
        prof = self.get_effective_profile_index(channel)
        return self.client.get_light_compensation_mode(channel, profile=prof) == "wdr"

    def supports_profile_mode(self, channel: int) -> bool:
        """Check if Profile Mode is supported for this channel (Intelbras IP cameras only)."""
        if not self.is_intelbras_ip(channel) or not self.data:
            return False
        vio = self.data.get("video_in_options", {}).get(channel, {})
        return "switch_mode" in vio

    def supports_fixed_profile(self, channel: int) -> bool:
        """Check if Fixed Profile selection is supported (Intelbras IP cameras only)."""
        return self.is_intelbras_ip(channel)

    def get_profile_mode(self, channel: int) -> str:
        """Return Profile mode ('regular', 'fixed', 'schedule', 'day_night')."""
        if not self.data:
            return "regular"
        vio = self.data.get("video_in_options", {}).get(channel, {})
        mode_val = vio.get("switch_mode", 0)
        # 0=regular/day, 1=day_night (auto sensor), 2=schedule, 3=night (fixed)
        mode_map = {0: "regular", 1: "day_night", 2: "schedule", 3: "fixed"}
        return mode_map.get(mode_val, "regular")

    def get_fixed_profile(self, channel: int) -> str:
        """Return Fixed profile selection ('day' or 'night')."""
        if not self.data:
            return "day"
        vio = self.data.get("video_in_options", {}).get(channel, {})
        return "night" if vio.get("switch_mode") == 3 else "day"

    def get_ip_schedule_time(self, channel: int, which: str) -> dt_time:
        """Return day start (sunrise) or day end (sunset) schedule time."""
        vio = self.data.get("video_in_options", {}).get(channel, {}) if self.data else {}
        if which == "start":
            hour = min(23, max(0, vio.get("sunrise_hour", 6)))
            minute = min(59, max(0, vio.get("sunrise_minute", 0)))
            return dt_time(hour, minute)
        hour = min(23, max(0, vio.get("sunset_hour", 18)))
        minute = min(59, max(0, vio.get("sunset_minute", 0)))
        return dt_time(hour, minute)

    def get_profile_mode_info(self, channel: int) -> dict[str, Any]:
        """Return schedule metadata attributes for Profile Mode entity."""
        if not self.data:
            return {}
        vio = self.data.get("video_in_options", {}).get(channel, {})
        info: dict[str, Any] = {}
        if "sunrise_hour" in vio:
            info["day_start_time"] = (
                f"{vio['sunrise_hour']:02d}:{vio.get('sunrise_minute', 0):02d}:00"
            )
        if "sunset_hour" in vio:
            info["day_end_time"] = (
                f"{vio['sunset_hour']:02d}:{vio.get('sunset_minute', 0):02d}:00"
            )
        info["fixed_profile"] = "night" if vio.get("switch_mode") == 3 else "day"

        if vio.get("switch_mode") == 2 and "sunset_hour" in vio and "sunrise_hour" in vio:
            sunset = dt_time(vio["sunset_hour"], vio.get("sunset_minute", 0))
            sunrise = dt_time(vio["sunrise_hour"], vio.get("sunrise_minute", 0))
            now_time = dt_util.now().time()
            is_night = (
                (now_time >= sunset or now_time < sunrise)
                if sunset > sunrise
                else (sunset <= now_time < sunrise)
            )
            info["active_period"] = "night" if is_night else "day"
        return info

    def get_profile_period_info(self, channel: int) -> dict[str, Any]:
        """Return schedule metadata attributes (alias for get_profile_mode_info)."""
        return self.get_profile_mode_info(channel)

    # -----------------------------------------------------------------------
    # Analog Camera Periods (TimeSection) helpers
    # -----------------------------------------------------------------------

    def get_analog_period_time(self, channel: int, period_idx: int, which: str) -> dt_time:
        """Return start or end time for analog camera period 1 (idx 0) or period 2 (idx 1)."""
        periods = self.client.get_analog_periods(channel)
        p = periods.get(period_idx, {})
        if which == "start":
            if "start" in p:
                return p["start"]
            return dt_time(6, 0) if period_idx == 0 else dt_time(18, 0)
        if "end" in p:
            return p["end"]
        return dt_time(18, 0) if period_idx == 0 else dt_time(6, 0)

    def get_analog_periods_info(self, channel: int) -> dict[str, Any]:
        """Return schedule metadata attributes for an analog camera."""
        periods = self.client.get_analog_periods(channel)
        p0 = periods.get(0, {})
        p1 = periods.get(1, {})
        p0_start = p0.get("start", dt_time(6, 0))
        p0_end = p0.get("end", dt_time(18, 0))
        p1_start = p1.get("start", dt_time(18, 0))
        p1_end = p1.get("end", dt_time(6, 0))

        info: dict[str, Any] = {
            "period_1": f"{p0_start.strftime('%H:%M:%S')} - {p0_end.strftime('%H:%M:%S')}",
            "period_2": f"{p1_start.strftime('%H:%M:%S')} - {p1_end.strftime('%H:%M:%S')}",
        }

        now_time = dt_util.now().time()
        if p0_start < p0_end:
            in_p0 = p0_start <= now_time < p0_end
        else:
            in_p0 = now_time >= p0_start or now_time < p0_end

        info["active_period"] = 1 if in_p0 else 2
        return info

    def supports_light_compensation(self, channel: int) -> bool:
        """Check if Light Compensation (BLC, HLC, or WDR) is supported for this channel."""
        return self.client.channel_supports_light_compensation(channel)

    def supports_denoise_2d(self, channel: int) -> bool:
        """Check if 2D noise reduction is supported for this channel."""
        return self.client.channel_supports_denoise_2d(channel)

    def supports_image_enhancement(self, channel: int) -> bool:
        """Check if image enhancement is supported for this channel."""
        return self.client.channel_supports_image_enhancement(channel)

    def supports_auto_noise_reduction(self, channel: int) -> bool:
        """Check if auto noise reduction is supported for this channel."""
        return self.is_analog(channel)

    def supports_acutance(self, channel: int) -> bool:
        """Check if acutance (video adjustment) is supported for this channel."""
        return self.client.channel_supports_acutance(channel)

    def supports_analog_periods(self, channel: int) -> bool:
        """Check if analog period schedule is supported for this channel."""
        return self.is_analog(channel)

    def get_analog_period_active(self, channel: int, period_idx: int) -> bool:
        """Return True if the specified analog period is active."""
        return self.client.get_analog_period_active(channel, period_idx)

    async def async_set_analog_period_active(
        self, channel: int, period_idx: int, active: bool
    ) -> None:
        """Enable or disable an analog period with schedule validation."""
        ok = await self.client.set_analog_period_active(channel, period_idx, active)
        if not ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_analog_period_active_failed",
                translation_placeholders={
                    "period": str(period_idx + 1),
                    "channel": str(channel + 1),
                },
            )
        await self.async_request_refresh()

    async def async_set_analog_period_time(
        self,
        channel: int,
        period_idx: int,
        start_time: dt_time | None = None,
        end_time: dt_time | None = None,
    ) -> None:
        """Update start or end time for an analog period with validation."""
        if start_time is not None and end_time is not None and start_time == end_time:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="analog_period_same_start_end",
            )

        ok = await self.client.set_analog_period_time(
            channel, period_idx, start_time=start_time, end_time=end_time
        )
        if not ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_analog_period_time_failed",
                translation_placeholders={
                    "period": str(period_idx + 1),
                    "channel": str(channel + 1),
                },
            )
        await self.async_request_refresh()

    def supports_scene_mode(self, channel: int) -> bool:
        """Check if Scene Mode / Picture Style is supported for this channel."""
        if not self.is_intelbras_ip(channel) or not self.data:
            return False
        vc = self.data.get("video_color", {}).get(channel, {})
        return "picture_style" in vc

    def get_scene_mode(self, channel: int) -> str:
        """Return current Scene Mode / Picture Style ('standard', 'soft', 'vivid')."""
        if not self.data:
            return "standard"
        vc = self.data.get("video_color", {}).get(channel, {})
        return vc.get("picture_style", "standard")

    def get_ptz_status(self, channel: int) -> dict[str, Any]:
        """Return cached PTZ position and movement status for a channel."""
        if not self.data:
            return {}
        return self.data.get("ptz_status", {}).get(channel, {})

    def supports_analog_period_config(self, channel: int) -> bool:
        """Check if analog period config selector is supported for this channel."""
        return self.supports_analog_periods(channel)

    def supports_ip_profile_config(self, channel: int) -> bool:
        """Check if IP profile config selector is supported for this channel."""
        return self.is_intelbras_ip(channel)

    def get_selected_config_profile(self, channel: int) -> str:
        """Return currently selected view/edit profile or period mode for the channel."""
        return self._selected_config_profile.get(channel, "active")

    def set_selected_config_profile(self, channel: int, option: str) -> None:
        """Update selected view/edit profile or period mode and notify entities."""
        self._selected_config_profile[channel] = option
        self.async_update_listeners()

    def get_effective_profile_index(self, channel: int) -> int:
        """Return the effective DVR profile index (0, 1, or 2) to view/edit for a channel."""
        opt = self.get_selected_config_profile(channel)
        if opt in ("period_1", "day"):
            return 0
        if opt in ("period_2", "night"):
            return 1
        if opt == "regular":
            return 2
        return self.client.get_active_profile_index(channel)

    def get_motion_detect_info(self, channel: int) -> dict[str, Any]:
        """Return cached MotionDetect metadata for a channel."""
        md_map: dict[int, dict[str, Any]] = (
            (self.data or {}).get("motion_detect") or self._cached_motion_detect
        )
        return md_map.get(channel, {})

    def is_motion_detection_enabled(self, channel: int) -> bool | None:
        """Return True/False if motion detection state is known for a channel."""
        info = self.get_motion_detect_info(channel)
        val = info.get("enable")
        return bool(val) if val is not None else None

    async def async_set_motion_detection_enabled(
        self, channel: int, enabled: bool
    ) -> bool:
        """Toggle motion detection for a channel and update cached coordinator state."""
        ok = await self.client.set_motion_detect_enabled(channel, enabled)
        if ok:
            self._cached_motion_detect.setdefault(channel, {})["enable"] = enabled
            if self.data is not None:
                md = dict(self.data.get("motion_detect") or self._cached_motion_detect)
                ch_md = dict(md.get(channel, {}))
                ch_md["enable"] = enabled
                md[channel] = ch_md
                self.data["motion_detect"] = md
                self.async_set_updated_data(dict(self.data))
        return ok

    async def async_set_motion_detect_param(
        self, channel: int, field: str, value: int
    ) -> bool:
        """Update motion detection sensitivity or threshold and update cached state."""
        ok = await self.client.set_motion_detect_param(channel, field, value)
        if ok:
            clamped = max(0, min(100, int(value)))
            self._cached_motion_detect.setdefault(channel, {})[field] = clamped
            if self.data is not None:
                md = dict(self.data.get("motion_detect") or self._cached_motion_detect)
                ch_md = dict(md.get(channel, {}))
                ch_md[field] = clamped
                md[channel] = ch_md
                self.data["motion_detect"] = md
                self.async_set_updated_data(dict(self.data))
        return ok



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
        saved_names = entry.data.get("channel_names", {})
        if saved_names:
            self.client._channel_names.update({int(k): v for k, v in saved_names.items()})
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
