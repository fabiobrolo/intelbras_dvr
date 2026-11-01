"""Integration setup, runtime data type, and service registration."""
from __future__ import annotations

import asyncio
import importlib
import logging
import sys
from dataclasses import dataclass
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import ConfigEntryNotReady, HomeAssistantError
from homeassistant.helpers import device_registry as dr, entity_registry as er
import homeassistant.helpers.config_validation as cv

from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
    CONF_HTTP_PORT,
    CONF_PTZ_CHANNELS,
    CONF_RTSP_PORT,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_HTTP_PORT,
    DEFAULT_RTSP_PORT,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
    PLATFORMS,
    PTZ_DIRECTION_CODES,
    RECORD_MODE_MANUAL,
    RECORD_MODE_SCHEDULE,
    CameraFeature,
)
from .coordinator import IntelbrasCoordinator, IntelbrasEventCoordinator
from .dvr import DVRAuthError, DVRConnectionError, IntelbrasClient

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# Runtime data stored on the config entry
# ---------------------------------------------------------------------------


@dataclass
class IntelbrasDVRData:
    """Runtime data stored in ``config_entry.runtime_data``."""

    client: IntelbrasClient
    coordinator: IntelbrasCoordinator
    event_coordinator: IntelbrasEventCoordinator


type IntelbrasDVRConfigEntry = ConfigEntry[IntelbrasDVRData]


# ---------------------------------------------------------------------------
# Service schemas
# ---------------------------------------------------------------------------

_ENTRY_ID_FIELD = vol.Optional("entry_id")
_CHANNEL_FIELD = vol.Required("channel")

SET_VIDEO_COLOR_SCHEMA = vol.Schema(
    {
        _ENTRY_ID_FIELD: cv.string,
        _CHANNEL_FIELD: vol.Coerce(int),
        vol.Optional("profile", default=0): vol.Coerce(int),
        vol.Optional("brightness", default=50): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("contrast", default=50): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("hue", default=50): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("saturation", default=80): vol.All(vol.Coerce(int), vol.Range(min=0, max=100)),
        vol.Optional("acutance", default=1): vol.All(vol.Coerce(int), vol.Range(min=0, max=15)),
    },
    extra=vol.REMOVE_EXTRA,
)

SET_MONITOR_SPLIT_SCHEMA = vol.Schema(
    {
        _ENTRY_ID_FIELD: cv.string,
        vol.Required("mode"): cv.string,
        vol.Optional("group", default=0): vol.Coerce(int),
        vol.Optional("channel", default=1): vol.Coerce(int),
    },
    extra=vol.REMOVE_EXTRA,
)

MASK_CAMERA_SCHEMA = vol.Schema(
    {
        _ENTRY_ID_FIELD: cv.string,
        _CHANNEL_FIELD: vol.Coerce(int),
        vol.Required("action"): vol.In(["mask", "unmask"]),
    },
    extra=vol.REMOVE_EXTRA,
)

REBOOT_SCHEMA = vol.Schema(
    {_ENTRY_ID_FIELD: cv.string},
    extra=vol.REMOVE_EXTRA,
)

RECORD_CAMERA_SCHEMA = vol.Schema(
    {
        _ENTRY_ID_FIELD: cv.string,
        _CHANNEL_FIELD: vol.Coerce(int),
        vol.Optional("duration", default=60): vol.All(
            vol.Coerce(int), vol.Range(min=5, max=3600)
        ),
    },
    extra=vol.REMOVE_EXTRA,
)

PTZ_MOVE_SCHEMA = vol.Schema(
    {
        _ENTRY_ID_FIELD: cv.string,
        _CHANNEL_FIELD: vol.Coerce(int),
        vol.Required("direction"): vol.In(list(PTZ_DIRECTION_CODES.keys())),
        vol.Optional("speed", default=4): vol.All(vol.Coerce(int), vol.Range(min=1, max=8)),
        vol.Optional("duration", default=0.5): vol.All(
            vol.Coerce(float), vol.Range(min=0.1, max=60.0)
        ),
    },
    extra=vol.REMOVE_EXTRA,
)

PTZ_STOP_SCHEMA = vol.Schema(
    {
        _ENTRY_ID_FIELD: cv.string,
        _CHANNEL_FIELD: vol.Coerce(int),
        vol.Required("direction"): vol.In(list(PTZ_DIRECTION_CODES.keys())),
    },
    extra=vol.REMOVE_EXTRA,
)

PTZ_PRESET_SCHEMA = vol.Schema(
    {
        _ENTRY_ID_FIELD: cv.string,
        _CHANNEL_FIELD: vol.Coerce(int),
        vol.Required("preset"): vol.Coerce(int),
    },
    extra=vol.REMOVE_EXTRA,
)


# ---------------------------------------------------------------------------
# Global setup — service registration
# ---------------------------------------------------------------------------


async def async_setup(hass: HomeAssistant, config: dict[str, Any]) -> bool:
    """Register domain-level services (called once regardless of config entries)."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    ptz_tasks: dict[tuple[int, int], asyncio.Task[None]] = domain_data.setdefault("ptz_tasks", {})
    record_tasks: dict[tuple[int, int], asyncio.Task[None]] = domain_data.setdefault("record_tasks", {})

    def _runtime(call: ServiceCall) -> IntelbrasDVRData:
        """Resolve the correct runtime data for a service call."""
        entries = hass.config_entries.async_entries(DOMAIN)
        target_id: str | None = call.data.get("entry_id")

        if target_id:
            entry = hass.config_entries.async_get_entry(target_id)
            if entry is None or not hasattr(entry, "runtime_data"):
                raise HomeAssistantError(
                    f"No Intelbras DVR entry found with entry_id '{target_id}'."
                )
            return entry.runtime_data  # type: ignore[return-value]

        loaded = [e for e in entries if hasattr(e, "runtime_data")]
        if not loaded:
            raise HomeAssistantError("No Intelbras DVR integration is configured.")
        if len(loaded) > 1:
            raise HomeAssistantError(
                "Multiple DVRs are configured. Specify 'entry_id' in the service call."
            )
        return loaded[0].runtime_data  # type: ignore[return-value]

    async def handle_set_video_color(call: ServiceCall) -> None:
        data = _runtime(call)
        ok = await data.client.set_video_color(
            channel=call.data["channel"],
            brightness=call.data["brightness"],
            contrast=call.data["contrast"],
            hue=call.data["hue"],
            acutance=call.data["acutance"],
            saturation=call.data["saturation"],
            profile=call.data["profile"],
        )
        if not ok:
            raise HomeAssistantError(
                f"Failed to set video color for channel {call.data['channel']}."
            )
        await data.coordinator.async_request_refresh()

    async def handle_set_monitor_split(call: ServiceCall) -> None:
        data = _runtime(call)
        mode = call.data["mode"]
        group = call.data["group"]
        ok = await data.client.set_monitor_split(
            mode=mode,
            group=group,
            channel=call.data.get("channel", 1),
        )
        if not ok:
            raise HomeAssistantError(
                f"Failed to set monitor split to mode '{mode}'."
            )
        if data.coordinator.data and "monitor_mode" in data.coordinator.data:
            data.coordinator.data["monitor_mode"] = {"mode": mode, "group": group}

    async def handle_mask_camera(call: ServiceCall) -> None:
        data = _runtime(call)
        channel: int = call.data["channel"]
        mask: bool = call.data["action"] == "mask"
        if hasattr(data, "event_coordinator") and hasattr(data.event_coordinator, "suppress_video_blind"):
            data.event_coordinator.suppress_video_blind(channel, duration=30.0)
        ok = await data.client.mask_channels([channel], mask=mask)
        if not ok:
            raise HomeAssistantError(
                f"Failed to {'mask' if mask else 'unmask'} channel {channel}."
            )
        await data.coordinator.async_refresh_video_color()

    async def handle_reboot(call: ServiceCall) -> None:
        data = _runtime(call)
        ok = await data.client.reboot()
        if not ok:
            raise HomeAssistantError("Reboot command was not acknowledged by the DVR.")

    async def handle_record_camera(call: ServiceCall) -> None:
        """
        Enable manual recording for a fixed duration, then restore schedule mode.

        Cancels any previous timer on the same channel so rapid triggers extend
        the manual recording window cleanly.
        """
        data = _runtime(call)
        channel: int = call.data["channel"]
        duration: int = call.data["duration"]
        task_key = (id(data.client), channel)

        old_task = record_tasks.pop(task_key, None)
        if old_task and not old_task.done():
            old_task.cancel()

        ok = await data.client.set_record_mode(channel, RECORD_MODE_MANUAL)
        if not ok:
            raise HomeAssistantError(
                f"Failed to start manual recording on channel {channel}."
            )
        await data.coordinator.async_refresh_record_modes()
        _LOGGER.info("Manual recording started on channel %d for %ds.", channel, duration)

        async def _restore_schedule() -> None:
            try:
                await asyncio.sleep(duration)
                await data.client.set_record_mode(channel, RECORD_MODE_SCHEDULE)
                _LOGGER.info("Manual recording stopped on channel %d (returned to schedule).", channel)
                await data.coordinator.async_refresh_record_modes()
            except asyncio.CancelledError:
                return
            finally:
                if record_tasks.get(task_key) is asyncio.current_task():
                    record_tasks.pop(task_key, None)

        record_tasks[task_key] = hass.async_create_background_task(
            _restore_schedule(),
            name=f"intelbras_dvr_record_ch{channel}",
        )

    async def handle_ptz_move(call: ServiceCall) -> None:
        """Start PTZ movement and auto-stop after `duration` seconds."""
        data = _runtime(call)
        channel: int = call.data["channel"]
        code = PTZ_DIRECTION_CODES[call.data["direction"]]
        speed: int = call.data["speed"]
        duration: float = call.data["duration"]
        task_key = (id(data.client), channel)

        old_task = ptz_tasks.pop(task_key, None)
        if old_task and not old_task.done():
            old_task.cancel()

        await data.client.ptz_move(channel, code, speed)

        async def _auto_stop() -> None:
            try:
                await asyncio.sleep(duration)
                await data.client.ptz_stop(channel, code)
            except asyncio.CancelledError:
                return
            finally:
                if ptz_tasks.get(task_key) is asyncio.current_task():
                    ptz_tasks.pop(task_key, None)

        ptz_tasks[task_key] = hass.async_create_background_task(
            _auto_stop(),
            name=f"intelbras_dvr_ptz_stop_ch{channel}",
        )

    async def handle_ptz_stop(call: ServiceCall) -> None:
        """Immediately stop a PTZ movement."""
        data = _runtime(call)
        channel: int = call.data["channel"]
        code = PTZ_DIRECTION_CODES[call.data["direction"]]
        task_key = (id(data.client), channel)
        old_task = ptz_tasks.pop(task_key, None)
        if old_task and not old_task.done():
            old_task.cancel()
        ok = await data.client.ptz_stop(channel, code)
        if not ok:
            raise HomeAssistantError(
                f"Failed to stop PTZ movement on channel {channel}."
            )

    async def handle_ptz_preset(call: ServiceCall) -> None:
        """Move PTZ camera to a saved preset position."""
        data = _runtime(call)
        channel: int = call.data["channel"]
        preset: int = call.data["preset"]
        ok = await data.client.ptz_goto_preset(channel, preset)
        if not ok:
            raise HomeAssistantError(
                f"Failed to move channel {channel} to PTZ preset {preset}."
            )

    for name, handler, schema in [
        ("set_video_color", handle_set_video_color, SET_VIDEO_COLOR_SCHEMA),
        ("set_monitor_split", handle_set_monitor_split, SET_MONITOR_SPLIT_SCHEMA),
        ("mask_camera", handle_mask_camera, MASK_CAMERA_SCHEMA),
        ("reboot", handle_reboot, REBOOT_SCHEMA),
        ("record_camera", handle_record_camera, RECORD_CAMERA_SCHEMA),
        ("ptz_move", handle_ptz_move, PTZ_MOVE_SCHEMA),
        ("ptz_stop", handle_ptz_stop, PTZ_STOP_SCHEMA),
        ("ptz_preset", handle_ptz_preset, PTZ_PRESET_SCHEMA),
    ]:
        hass.services.async_register(DOMAIN, name, handler, schema=schema)

    try:
        from .views import register_or_update_views

        register_or_update_views(hass)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not register views in async_setup: %s", exc)

    return True


# ---------------------------------------------------------------------------
# Config entry lifecycle
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Set up an Intelbras DVR instance from a config entry."""
    submodules = [
        "const",
        "dvr",
        "entity",
        "views",
        "coordinator",
        "ptz",
        "camera",
        "select",
        "number",
        "switch",
        "sensor",
        "binary_sensor",
        "button",
        "update",
        "config_flow",
        "media_source",
    ]
    for sub in submodules:
        mod_name = f"custom_components.intelbras_dvr.{sub}"
        if mod_name in sys.modules:
            try:
                importlib.reload(sys.modules[mod_name])
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Could not reload submodule %s: %s", mod_name, exc)
        elif sub == "config_flow":
            try:
                importlib.import_module(mod_name)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Could not import submodule %s: %s", mod_name, exc)

    try:
        from .brand_assets import ensure_brand_assets

        await hass.async_add_executor_job(ensure_brand_assets)
    except Exception:  # noqa: BLE001
        pass

    dvr_mod = sys.modules["custom_components.intelbras_dvr.dvr"]
    coord_mod = sys.modules["custom_components.intelbras_dvr.coordinator"]

    client = dvr_mod.IntelbrasClient(
        host=entry.data[CONF_HOST],
        username=entry.data[CONF_USERNAME],
        password=entry.data[CONF_PASSWORD],
        http_port=entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
        rtsp_port=entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
    )

    try:
        active_titles = await client.get_channel_titles()
        if active_titles:
            cleaned_names = {str(ch): name for ch, name in active_titles.items()}
            if cleaned_names != entry.data.get("channel_names", {}):
                hass.config_entries.async_update_entry(
                    entry,
                    data={**entry.data, "channel_names": cleaned_names},
                )
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not refresh channel titles during setup: %s", exc)

    coordinator = coord_mod.IntelbrasCoordinator(hass, entry, client)
    event_coordinator = coord_mod.IntelbrasEventCoordinator(
        hass, entry, client, main_coordinator=coordinator
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except (dvr_mod.DVRConnectionError, dvr_mod.DVRAuthError, DVRConnectionError, DVRAuthError) as exc:
        await client.close()
        raise ConfigEntryNotReady(
            f"Cannot connect to DVR at {entry.data[CONF_HOST]}: {exc}"
        ) from exc

    try:
        await client.async_disable_auto_logout()
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Failed to disable AutoLogout on DVR: %s", exc)

    try:
        await event_coordinator.async_refresh()
    except Exception:  # noqa: BLE001
        pass

    await event_coordinator.async_start()

    dev_reg = dr.async_get(hass)
    sys_info = (coordinator.data or {}).get("system_info", {})
    parent_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Intelbras",
        model=sys_info.get("updateSerial", "DVR"),
        serial_number=sys_info.get("serialNumber"),
        sw_version=(coordinator.data or {}).get("software_version"),
        configuration_url=f"http://{entry.data['host']}:{entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)}",
    )

    remote_devs = (coordinator.data or {}).get("remote_devices", {})
    channel_names = {int(k): v for k, v in entry.data.get("channel_names", {}).items()}
    for ch in entry.data.get(CONF_CHANNELS, []):
        ch_name = channel_names.get(ch, f"Channel {ch + 1}")
        remote_info = remote_devs.get(ch, {})
        mfr = remote_info.get("manufacturer") or "Intelbras"
        mdl = remote_info.get("model") or f"HDCVI (CH {ch + 1})"
        sw_ver = remote_info.get("software_version")
        sn = remote_info.get("serial_number")
        cfg_url = remote_info.get("configuration_url")
        if not cfg_url and entry.data.get("host"):
            http_p = entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
            cfg_url = f"http://{entry.data['host']}" if http_p == 80 else f"http://{entry.data['host']}:{http_p}"

        dev_kwargs: dict[str, Any] = {
            "config_entry_id": entry.entry_id,
            "identifiers": {(DOMAIN, f"{entry.entry_id}_ch{ch}")},
            "name": ch_name,
            "manufacturer": mfr,
            "model": mdl,
        }
        if sw_ver:
            dev_kwargs["sw_version"] = sw_ver
        if sn:
            dev_kwargs["serial_number"] = sn
        if cfg_url:
            dev_kwargs["configuration_url"] = cfg_url
        if parent_device:
            dev_kwargs["via_device_id"] = parent_device.id
        else:
            dev_kwargs["via_device"] = (DOMAIN, entry.entry_id)

        dev_reg.async_get_or_create(**dev_kwargs)

    entry.runtime_data = IntelbrasDVRData(
        client=client,
        coordinator=coordinator,
        event_coordinator=event_coordinator,
    )

    try:
        from . import views

        for task in asyncio.all_tasks():
            if "intelbras_dvr_playback_priority" in task.get_name():
                task.cancel()

        if DOMAIN in hass.data and hasattr(views, "DATA_PLAYBACK_ACTIVE_UNTIL"):
            hass.data[DOMAIN][views.DATA_PLAYBACK_ACTIVE_UNTIL] = 0.0

        views.register_or_update_views(hass)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not register/update views in setup_entry: %s", exc)

    async def _async_cleanup_deselected() -> None:
        configured_channels: set[int] = set(entry.data.get(CONF_CHANNELS, []))
        ptz_channels: set[int] = set(entry.data.get(CONF_PTZ_CHANNELS, []))
        features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
        active_ptz_channels: set[int] = {
            ch
            for ch in ptz_channels
            if ch in configured_channels and CameraFeature.PTZ in features_map.get(str(ch), [])
        }

        device_reg = dr.async_get(hass)
        existing_devices = dr.async_entries_for_config_entry(device_reg, entry.entry_id)
        for dev in existing_devices:
            for domain, identifier in dev.identifiers:
                if domain == DOMAIN and identifier.startswith(f"{entry.entry_id}_ch"):
                    try:
                        ch_str = identifier.split("_ch")[-1]
                        ch_idx = int(ch_str)
                        if ch_idx not in configured_channels:
                            _LOGGER.info("Removing deselected camera device '%s' (ch %d)", dev.name, ch_idx)
                            device_reg.async_update_device(dev.id, remove_config_entry_id=entry.entry_id)
                    except ValueError:
                        pass

        entity_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_ch"
        for ent in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
            uid = ent.unique_id or ""
            if uid.startswith(prefix) and "_ptz_" in uid:
                try:
                    ch_part = uid[len(prefix):].split("_", 1)[0]
                    ch_idx = int(ch_part)
                    if ch_idx not in active_ptz_channels:
                        _LOGGER.info("Removing orphaned PTZ entity '%s' (ch %d)", ent.entity_id, ch_idx)
                        entity_reg.async_remove(ent.entity_id)
                except ValueError:
                    pass

    hass.async_create_task(_async_cleanup_deselected())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    try:
        if "media_source" in hass.data and DOMAIN in hass.data["media_source"]:
            from .media_source import IntelbrasMediaSource

            hass.data["media_source"][DOMAIN] = IntelbrasMediaSource(hass)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Failed updating media_source provider: %s", exc)

    if hasattr(entry, "runtime_data") and getattr(entry.runtime_data, "coordinator", None):
        entry.runtime_data.coordinator._last_synced_options = dict(entry.options)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(hass: HomeAssistant, entry: ConfigEntry) -> bool:
    """Unload a config entry and close the HTTP client."""
    if hasattr(entry, "runtime_data") and entry.runtime_data.event_coordinator:
        await entry.runtime_data.event_coordinator.async_stop()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded:
        await entry.runtime_data.client.close()  # type: ignore[union-attr]
    return unloaded


async def _async_reload_entry(hass: HomeAssistant, entry: ConfigEntry) -> None:
    """Reload the config entry when structural options change."""
    if hasattr(entry, "runtime_data") and getattr(entry.runtime_data, "coordinator", None):
        coord = entry.runtime_data.coordinator
        old_opts = getattr(coord, "_last_synced_options", {})
        diff_keys = {k for k, v in entry.options.items() if old_opts.get(k) != v} | {
            k for k, v in old_opts.items() if entry.options.get(k) != v
        }
        if diff_keys <= {CONF_SNAPSHOT_INTERVAL}:
            coord.snapshot_interval = float(
                entry.options.get(CONF_SNAPSHOT_INTERVAL, DEFAULT_SNAPSHOT_INTERVAL)
            )
            coord._last_synced_options = dict(entry.options)
            _LOGGER.debug(
                "Updated snapshot_interval to %s without reloading config entry",
                coord.snapshot_interval,
            )
            return
        coord._last_synced_options = dict(entry.options)

    await hass.config_entries.async_reload(entry.entry_id)
