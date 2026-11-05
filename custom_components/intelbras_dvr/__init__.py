"""Integration setup, runtime data type, and service registration for Intelbras DVR."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass, field
import logging
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import ConfigEntry
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, ServiceCall
from homeassistant.exceptions import (
    ConfigEntryAuthFailed,
    ConfigEntryNotReady,
    HomeAssistantError,
    ServiceValidationError,
)
from homeassistant.helpers import (
    config_validation as cv,
    device_registry as dr,
    entity_registry as er,
    translation,
)

from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
    CONF_HTTP_PORT,
    CONF_PTZ_CHANNELS,
    CONF_RTSP_PORT,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_ACUTANCE,
    DEFAULT_BRIGHTNESS,
    DEFAULT_CONTRAST,
    DEFAULT_HTTP_PORT,
    DEFAULT_HUE,
    DEFAULT_RTSP_PORT,
    DEFAULT_SATURATION_ACUTANCE,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
    MAX_ACUTANCE,
    MAX_COLOR_VALUE,
    PLATFORMS,
    PTZ_DIRECTION_CODES,
    PTZ_MOVE_DURATION,
    RECORD_MODE_MANUAL,
    RECORD_MODE_SCHEDULE,
    CameraFeature,
)
from .coordinator import IntelbrasCoordinator, IntelbrasEventCoordinator
from .dvr import DVRAuthError, DVRConnectionError, IntelbrasClient
from .views import DATA_PLAYBACK_ACTIVE_UNTIL, register_or_update_views

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
    translations: dict[str, str] = field(default_factory=dict)
    dvr_device_id: str | None = None


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
        vol.Optional("brightness", default=DEFAULT_BRIGHTNESS): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_COLOR_VALUE)
        ),
        vol.Optional("contrast", default=DEFAULT_CONTRAST): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_COLOR_VALUE)
        ),
        vol.Optional("hue", default=DEFAULT_HUE): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_COLOR_VALUE)
        ),
        vol.Optional("saturation", default=DEFAULT_SATURATION_ACUTANCE): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_COLOR_VALUE)
        ),
        vol.Optional("acutance", default=DEFAULT_ACUTANCE): vol.All(
            vol.Coerce(int), vol.Range(min=0, max=MAX_ACUTANCE)
        ),
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
        vol.Optional("speed", default=4): vol.All(
            vol.Coerce(int), vol.Range(min=1, max=8)
        ),
        vol.Optional("duration", default=PTZ_MOVE_DURATION): vol.All(
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
    ptz_tasks: dict[tuple[int, int], asyncio.Task[None]] = domain_data.setdefault(
        "ptz_tasks", {}
    )
    record_tasks: dict[tuple[int, int], asyncio.Task[None]] = domain_data.setdefault(
        "record_tasks", {}
    )

    def _runtime(call: ServiceCall) -> IntelbrasDVRData:
        """Resolve the correct runtime data for a service call."""
        entries = hass.config_entries.async_entries(DOMAIN)
        target_id: str | None = call.data.get("entry_id")

        if target_id:
            entry = hass.config_entries.async_get_entry(target_id)
            if entry is None or not hasattr(entry, "runtime_data") or entry.runtime_data is None:
                raise ServiceValidationError(
                    translation_domain=DOMAIN,
                    translation_key="entry_not_found",
                    translation_placeholders={"entry_id": target_id},
                )
            return entry.runtime_data

        loaded = [
            e
            for e in entries
            if hasattr(e, "runtime_data") and e.runtime_data is not None
        ]
        if not loaded:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="no_entries_configured",
            )
        if len(loaded) > 1:
            raise ServiceValidationError(
                translation_domain=DOMAIN,
                translation_key="multiple_entries_configured",
            )
        return loaded[0].runtime_data

    async def handle_set_video_color(call: ServiceCall) -> None:
        data = _runtime(call)
        channel = int(call.data["channel"])
        ok = await data.client.set_video_color(
            channel=channel,
            brightness=call.data["brightness"],
            contrast=call.data["contrast"],
            hue=call.data["hue"],
            acutance=call.data["acutance"],
            saturation=call.data["saturation"],
            profile=call.data["profile"],
        )
        if not ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_video_color_failed",
                translation_placeholders={"channel": str(channel)},
            )
        await data.coordinator.async_refresh_video_color()

    async def handle_set_monitor_split(call: ServiceCall) -> None:
        data = _runtime(call)
        mode: str = call.data["mode"]
        group: int = call.data["group"]
        ok = await asyncio.shield(
            data.client.set_monitor_split(
                mode=mode,
                group=group,
                channel=call.data.get("channel", 1),
            )
        )
        if not ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_monitor_split_failed",
                translation_placeholders={"mode": str(mode)},
            )
        if data.coordinator.data is not None:
            data.coordinator.data["monitor_mode"] = {"mode": mode, "group": group}
            data.coordinator.async_update_listeners()
        await data.coordinator.async_refresh_monitor_mode()

    async def handle_mask_camera(call: ServiceCall) -> None:
        data = _runtime(call)
        channel: int = call.data["channel"]
        mask: bool = call.data["action"] == "mask"
        data.event_coordinator.suppress_video_blind(channel, duration=30.0)
        ok = await data.client.mask_channels([channel], mask=mask)
        if not ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="mask_camera_failed",
                translation_placeholders={"channel": str(channel)},
            )
        await data.coordinator.async_refresh_video_color()

    async def handle_reboot(call: ServiceCall) -> None:
        data = _runtime(call)
        ok = await data.client.reboot()
        if not ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="reboot_failed",
            )

    async def handle_record_camera(call: ServiceCall) -> None:
        """Enable manual recording for a fixed duration, then restore schedule mode."""
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
                translation_domain=DOMAIN,
                translation_key="record_camera_failed",
                translation_placeholders={"channel": str(channel)},
            )
        await data.coordinator.async_refresh_record_modes()
        _LOGGER.info("Manual recording started on channel %d for %ds.", channel, duration)

        async def _restore_schedule() -> None:
            try:
                await asyncio.sleep(duration)
                await data.client.set_record_mode(channel, RECORD_MODE_SCHEDULE)
                _LOGGER.info(
                    "Manual recording stopped on channel %d (returned to schedule).",
                    channel,
                )
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
                translation_domain=DOMAIN,
                translation_key="ptz_stop_failed",
                translation_placeholders={"channel": str(channel)},
            )

    async def handle_ptz_preset(call: ServiceCall) -> None:
        """Move PTZ camera to a saved preset position."""
        data = _runtime(call)
        channel: int = call.data["channel"]
        preset: int = call.data["preset"]
        ok = await data.client.ptz_goto_preset(channel, preset)
        if not ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="ptz_preset_failed",
                translation_placeholders={
                    "channel": str(channel),
                    "preset": str(preset),
                },
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
        register_or_update_views(hass)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not register views in async_setup: %s", exc)

    return True


# ---------------------------------------------------------------------------
# Config entry lifecycle
# ---------------------------------------------------------------------------


async def async_setup_entry(hass: HomeAssistant, entry: IntelbrasDVRConfigEntry) -> bool:
    """Set up an Intelbras DVR instance from a config entry."""
    client = IntelbrasClient(
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
    except DVRAuthError as exc:
        await client.close()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="auth_failed",
        ) from exc
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not refresh channel titles during setup: %s", exc)

    coordinator = IntelbrasCoordinator(hass, entry, client)
    event_coordinator = IntelbrasEventCoordinator(
        hass, entry, client, main_coordinator=coordinator
    )

    try:
        await coordinator.async_config_entry_first_refresh()
    except ConfigEntryAuthFailed:
        await client.close()
        raise
    except DVRAuthError as exc:
        await client.close()
        raise ConfigEntryAuthFailed(
            translation_domain=DOMAIN,
            translation_key="auth_failed",
        ) from exc
    except (DVRConnectionError, ConfigEntryNotReady) as exc:
        await client.close()
        raise ConfigEntryNotReady(
            translation_domain=DOMAIN,
            translation_key="cannot_connect",
            translation_placeholders={"host": str(entry.data[CONF_HOST])},
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

    # Load localized strings for dynamic select options and media browser labels
    lang = hass.config.language or "en"
    loaded_translations: dict[str, str] = {}
    try:
        for category in ("entity", "selector"):
            trans = await translation.async_get_translations(
                hass, lang, category, [DOMAIN]
            )
            loaded_translations.update(trans)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Could not load translations for %s: %s", lang, exc)

    dev_reg = dr.async_get(hass)
    sys_info = (coordinator.data or {}).get("system_info", {})
    host = entry.data[CONF_HOST]
    http_port = entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
    dvr_config_url = (
        f"http://{host}"
        if http_port == DEFAULT_HTTP_PORT
        else f"http://{host}:{http_port}"
    )
    dvr_device = dev_reg.async_get_or_create(
        config_entry_id=entry.entry_id,
        identifiers={(DOMAIN, entry.entry_id)},
        name=entry.title,
        manufacturer="Intelbras",
        model=sys_info.get("updateSerial") or sys_info.get("deviceType") or "DVR",
        serial_number=sys_info.get("serialNumber"),
        sw_version=(coordinator.data or {}).get("software_version"),
        configuration_url=dvr_config_url,
    )

    remote_devs = (coordinator.data or {}).get("remote_devices", {})
    channel_names = {int(k): v for k, v in entry.data.get("channel_names", {}).items()}
    for ch in entry.data.get(CONF_CHANNELS, []):
        ch_name = channel_names.get(ch, f"Channel {ch + 1}")
        remote_info = remote_devs.get(ch, {})
        sig_fmt = client.get_channel_signal_format(ch)
        mfr = remote_info.get("manufacturer") or "Intelbras"
        mdl = remote_info.get("model") or f"{sig_fmt} (CH {ch + 1})"
        sw_ver = remote_info.get("software_version")
        sn = remote_info.get("serial_number")
        cfg_url = remote_info.get("configuration_url") or dvr_config_url

        dev_reg.async_get_or_create(
            config_entry_id=entry.entry_id,
            identifiers={(DOMAIN, f"{entry.entry_id}_ch{ch}")},
            via_device_id=dvr_device.id,
            name=ch_name,
            manufacturer=mfr,
            model=mdl,
            sw_version=sw_ver,
            serial_number=sn,
            configuration_url=cfg_url,
        )

    entry.runtime_data = IntelbrasDVRData(
        client=client,
        coordinator=coordinator,
        event_coordinator=event_coordinator,
        translations=loaded_translations,
        dvr_device_id=dvr_device.id,
    )

    try:
        if DOMAIN in hass.data:
            hass.data[DOMAIN][DATA_PLAYBACK_ACTIVE_UNTIL] = 0.0
        register_or_update_views(hass)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.warning("Could not register/update views in setup_entry: %s", exc)

    async def _async_cleanup_deselected() -> None:
        configured_channels: set[int] = set(entry.data.get(CONF_CHANNELS, []))
        ptz_channels: set[int] = set(entry.data.get(CONF_PTZ_CHANNELS, []))
        features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
        active_ptz_channels: set[int] = {
            ch
            for ch in ptz_channels
            if ch in configured_channels
            and CameraFeature.PTZ in features_map.get(str(ch), [])
        }

        device_reg = dr.async_get(hass)
        existing_devices = dr.async_entries_for_config_entry(
            device_reg, entry.entry_id
        )
        for dev in existing_devices:
            for domain, identifier in dev.identifiers:
                if domain == DOMAIN and identifier.startswith(f"{entry.entry_id}_ch"):
                    try:
                        ch_str = identifier.split("_ch")[-1]
                        ch_idx = int(ch_str)
                        if ch_idx not in configured_channels:
                            _LOGGER.info(
                                "Removing deselected camera device '%s' (ch %d)",
                                dev.name,
                                ch_idx,
                            )
                            device_reg.async_remove_device(dev.id)
                    except ValueError:
                        pass

        entity_reg = er.async_get(hass)
        prefix = f"{entry.entry_id}_ch"
        for ent in er.async_entries_for_config_entry(entity_reg, entry.entry_id):
            uid = ent.unique_id or ""
            if uid.startswith(prefix) and "_ptz_" in uid:
                try:
                    ch_part = uid[len(prefix) :].split("_", 1)[0]
                    ch_idx = int(ch_part)
                    if ch_idx not in active_ptz_channels:
                        _LOGGER.info(
                            "Removing orphaned PTZ entity '%s' (ch %d)",
                            ent.entity_id,
                            ch_idx,
                        )
                        entity_reg.async_remove(ent.entity_id)
                except ValueError:
                    pass

    hass.async_create_task(_async_cleanup_deselected())

    await hass.config_entries.async_forward_entry_setups(entry, PLATFORMS)

    entry.async_on_unload(entry.add_update_listener(_async_reload_entry))
    return True


async def async_unload_entry(
    hass: HomeAssistant, entry: IntelbrasDVRConfigEntry
) -> bool:
    """Unload a config entry and close the HTTP client."""
    if hasattr(entry, "runtime_data") and entry.runtime_data.event_coordinator:
        await entry.runtime_data.event_coordinator.async_stop()

    unloaded = await hass.config_entries.async_unload_platforms(entry, PLATFORMS)
    if unloaded and hasattr(entry, "runtime_data") and entry.runtime_data:
        await entry.runtime_data.client.close()
    return unloaded


async def async_remove_config_entry_device(
    hass: HomeAssistant,
    config_entry: IntelbrasDVRConfigEntry,
    device_entry: dr.DeviceEntry,
) -> bool:
    """Allow manual removal of a camera child device if its channel is not active."""
    configured_channels: set[int] = set(config_entry.data.get(CONF_CHANNELS, []))
    for domain, identifier in device_entry.identifiers:
        if domain == DOMAIN:
            if identifier == config_entry.entry_id:
                return False
            if identifier.startswith(f"{config_entry.entry_id}_ch"):
                try:
                    ch_idx = int(identifier.split("_ch")[-1])
                    return ch_idx not in configured_channels
                except ValueError:
                    return True
    return True


async def _async_reload_entry(
    hass: HomeAssistant, entry: IntelbrasDVRConfigEntry
) -> None:
    """Reload the config entry when structural options change."""
    if hasattr(entry, "runtime_data") and entry.runtime_data:
        coord = entry.runtime_data.coordinator
        old_opts = coord._last_synced_options
        diff_keys = {
            k for k, v in entry.options.items() if old_opts.get(k) != v
        } | {k for k, v in old_opts.items() if entry.options.get(k) != v}
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
