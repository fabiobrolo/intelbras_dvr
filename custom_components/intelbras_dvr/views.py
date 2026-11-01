"""HTTP views for Intelbras DVR integration."""
from __future__ import annotations

import asyncio
from datetime import datetime, time, timedelta
from http import HTTPStatus
import logging
import os
import shutil
import time as time_mod
from typing import Any
import unicodedata
import urllib.parse

from aiohttp import web

from homeassistant.components.http import HomeAssistantView
from homeassistant.core import HomeAssistant
from homeassistant.util import dt as dt_util

from .const import CONF_CHANNELS, CONF_RTSP_PORT, DEFAULT_RTSP_PORT, DOMAIN
from .dvr import IntelbrasClient

_LOGGER = logging.getLogger(__name__)

CACHE_TTL = 30.0  # seconds to cache live thumbnail in memory
DISK_CACHE_MAX_AGE_SECONDS = 30 * 86400  # 30 days retention for recording thumbnails on disk

DATA_PLAYBACK_ACTIVE_UNTIL = "playback_active_until"
DATA_ACTIVE_PLAYBACK_STREAM = "active_playback_stream"
DATA_ACTIVE_THUMBNAIL_PROCS = "active_thumbnail_procs"
DATA_BROWSE_SCOPE = "browse_scope"
DATA_BROWSE_GENERATION = "browse_generation"
DATA_BROWSE_LAST_ACTIVE = "browse_last_active"
DATA_SCOPE_CACHE_TASK = "scope_cache_task"
DATA_ACTIVE_HTTP_WAITERS = "active_http_thumb_waiters"


def set_browse_scope(hass: HomeAssistant, identifier: str) -> None:
    """Set the current media browser navigation scope and cancel previous level's tasks."""
    data = hass.data.setdefault(DOMAIN, {})
    old_scope = data.get(DATA_BROWSE_SCOPE, "")
    data[DATA_BROWSE_SCOPE] = identifier
    data[DATA_BROWSE_GENERATION] = data.get(DATA_BROWSE_GENERATION, 0) + 1
    data[DATA_BROWSE_LAST_ACTIVE] = time_mod.monotonic()

    if identifier != old_scope:
        old_task = data.get(DATA_SCOPE_CACHE_TASK)
        if old_task and not old_task.done():
            old_task.cancel()
            data[DATA_SCOPE_CACHE_TASK] = None
        abort_running_thumbnail_processes(hass)


def abort_running_thumbnail_processes(hass: HomeAssistant) -> None:
    """Immediately kill any running thumbnail FFmpeg processes to free the DVR."""
    data = hass.data.get(DOMAIN, {})
    procs = list(data.get(DATA_ACTIVE_THUMBNAIL_PROCS, set()))
    for p in procs:
        try:
            p.kill()
        except Exception:
            pass


def is_recording_thumb_in_scope(hass: HomeAssistant, entry_id: str, channel: int, start_s: str) -> bool:
    """Check if the requested recording thumbnail belongs to the user's current browsing scope."""
    data = hass.data.get(DOMAIN, {})
    scope = data.get(DATA_BROWSE_SCOPE, "")
    if not scope:
        return True

    parts = scope.split("|")
    kind = parts[0]
    if len(parts) >= 3 and (parts[1] != entry_id or int(parts[2]) != channel):
        return False

    if kind == "HOUR" and len(parts) >= 5:
        day_s = parts[3]
        hour_s = parts[4]
        in_scope = start_s.startswith(f"{day_s}{hour_s}")
        if in_scope:
            data[DATA_BROWSE_LAST_ACTIVE] = time_mod.monotonic()
        return in_scope

    if kind in ("FILES", "ALLFILES") and len(parts) >= 4:
        day_s = parts[3]
        in_scope = start_s.startswith(day_s)
        if in_scope:
            data[DATA_BROWSE_LAST_ACTIVE] = time_mod.monotonic()
        return in_scope

    if kind in ("DAY", "CH"):
        return False

    return True


def is_day_thumb_in_scope(hass: HomeAssistant, entry_id: str, channel: int, day_s: str) -> bool:
    """Check if the requested day thumbnail belongs to the user's current browsing scope."""
    data = hass.data.get(DOMAIN, {})
    scope = data.get(DATA_BROWSE_SCOPE, "")
    if not scope:
        return True

    parts = scope.split("|")
    kind = parts[0]
    if len(parts) >= 3 and (parts[1] != entry_id or int(parts[2]) != channel):
        return False

    if kind == "DAY":
        data[DATA_BROWSE_LAST_ACTIVE] = time_mod.monotonic()
        return True
    if kind in ("FILES", "ALLFILES") and len(parts) >= 4:
        in_scope = (parts[3] == day_s)
        if in_scope:
            data[DATA_BROWSE_LAST_ACTIVE] = time_mod.monotonic()
        return in_scope

    return False


def is_playback_active(hass: HomeAssistant) -> bool:
    """Return True if video playback is currently active."""
    data = hass.data.get(DOMAIN, {})
    now = time_mod.monotonic()
    active_until = data.get(DATA_PLAYBACK_ACTIVE_UNTIL, 0.0)
    if active_until > now:
        return True

    stream = data.get(DATA_ACTIVE_PLAYBACK_STREAM)
    if stream is not None:
        try:
            hls = stream.get_provider("hls")
            if hls is not None:
                last_acc = getattr(hls, "last_accessed", None) or getattr(hls, "_last_accessed", None)
                if last_acc and (time_mod.time() - last_acc < 15.0):
                    return True
        except Exception:
            pass

    return False


def set_playback_active(hass: HomeAssistant, duration: float = 45.0) -> None:
    """Mark playback as active for the specified duration and abort running thumbnail tasks."""
    data = hass.data.setdefault(DOMAIN, {})
    now = time_mod.monotonic()
    curr = data.get(DATA_PLAYBACK_ACTIVE_UNTIL, 0.0)
    if curr > 1e9:
        curr = 0.0
    data[DATA_PLAYBACK_ACTIVE_UNTIL] = max(curr, now + duration)

    cache_task = data.get(DATA_SCOPE_CACHE_TASK)
    if cache_task and not cache_task.done():
        cache_task.cancel()
        data[DATA_SCOPE_CACHE_TASK] = None

    abort_running_thumbnail_processes(hass)


def clear_playback_active(hass: HomeAssistant) -> None:
    """Clear playback active flag."""
    if DOMAIN in hass.data:
        hass.data[DOMAIN][DATA_PLAYBACK_ACTIVE_UNTIL] = 0.0


def _read_disk_cache(file_path: str) -> bytes | None:
    """Read cached thumbnail file from disk."""
    try:
        if os.path.isfile(file_path):
            with open(file_path, "rb") as f:
                return f.read()
    except Exception:
        pass
    return None


def _write_disk_cache(file_path: str, data: bytes) -> None:
    """Write thumbnail file to disk cache."""
    try:
        os.makedirs(os.path.dirname(file_path), exist_ok=True)
        with open(file_path, "wb") as f:
            f.write(data)
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Failed to write recording thumbnail to disk cache: %s", exc)


class IntelbrasThumbnailView(HomeAssistantView):
    """View to serve live camera thumbnail snapshots for the media browser."""

    url = "/api/intelbras_dvr/thumbnail/{entry_id}/{channel}"
    name = "api:intelbras_dvr:thumbnail"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._cache: dict[tuple[str, int], dict[str, Any]] = {}
        self._sem = asyncio.Semaphore(2)

    async def get(
        self, request: web.Request, entry_id: str, channel: str
    ) -> web.StreamResponse:
        return await async_handle_thumbnail(
            self.hass, entry_id, channel, self._cache, self._sem
        )

    async def _async_fetch_from_ha_camera(
        self, entry: Any, ch_idx: int
    ) -> bytes | None:
        return await async_fetch_from_ha_camera(self.hass, entry, ch_idx)


class IntelbrasRecordingThumbnailView(HomeAssistantView):
    """View to serve recording snapshot thumbnails extracted via ffmpeg or cached."""

    url = "/api/intelbras_dvr/recording_thumb/{entry_id}/{channel}/{start}/{end}"
    name = "api:intelbras_dvr:recording_thumb"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._cache: dict[tuple[str, int, str], bytes] = {}
        self._sem = asyncio.Semaphore(1)
        self.cache_dir = hass.config.path(".storage", "intelbras_dvr_thumbnails")
        self._check_and_purge_legacy_cache()

    def _check_and_purge_legacy_cache(self) -> None:
        """Purge legacy contaminated cache once and prune thumbnails older than 30 days."""
        v_file = os.path.join(self.cache_dir, ".cache_v2")

        def _sync_purge() -> None:
            try:
                os.makedirs(self.cache_dir, exist_ok=True)
                if not os.path.isfile(v_file):
                    for fname in os.listdir(self.cache_dir):
                        if fname.endswith(".jpg"):
                            try:
                                os.remove(os.path.join(self.cache_dir, fname))
                            except Exception:
                                pass
                    with open(v_file, "w") as f:
                        f.write("v2")
                    _LOGGER.info("Purged contaminated recording thumbnails cache")
                else:
                    cutoff = time_mod.time() - DISK_CACHE_MAX_AGE_SECONDS
                    for fname in os.listdir(self.cache_dir):
                        if fname.endswith(".jpg"):
                            fpath = os.path.join(self.cache_dir, fname)
                            try:
                                if os.path.getmtime(fpath) < cutoff:
                                    os.remove(fpath)
                            except Exception:
                                pass
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Could not maintain thumbnails disk cache: %s", exc)

        self.hass.async_add_executor_job(_sync_purge)

    async def get(
        self, request: web.Request, entry_id: str, channel: str, start: str, end: str
    ) -> web.StreamResponse:
        return await async_handle_recording_thumbnail(
            self.hass,
            entry_id,
            channel,
            start,
            end,
            self._cache,
            self._sem,
            self.cache_dir,
            request=request,
        )


class IntelbrasDayThumbnailView(HomeAssistantView):
    """View to serve preview thumbnails for a whole day."""

    url = "/api/intelbras_dvr/day_thumb/{entry_id}/{channel}/{day_s}"
    name = "api:intelbras_dvr:day_thumb"
    requires_auth = False

    def __init__(self, hass: HomeAssistant) -> None:
        self.hass = hass
        self._cache: dict[tuple[str, int, str], bytes] = {}
        self.cache_dir = hass.config.path(".storage", "intelbras_dvr_thumbnails")

    async def get(
        self, request: web.Request, entry_id: str, channel: str, day_s: str
    ) -> web.StreamResponse:
        return await async_handle_day_thumbnail(
            self.hass,
            entry_id,
            channel,
            day_s,
            self._cache,
            self.cache_dir,
            request=request,
        )


def register_or_update_views(hass: HomeAssistant) -> None:
    """Register or hot-reload all Intelbras DVR HTTP views, even if aiohttp router is frozen."""
    view_specs: list[tuple[str, type[HomeAssistantView], str]] = [
        ("intelbras_dvr_thumbnail_view", IntelbrasThumbnailView, "/api/intelbras_dvr/thumbnail/"),
        ("intelbras_dvr_recording_thumbnail_view", IntelbrasRecordingThumbnailView, "/api/intelbras_dvr/recording_thumb/"),
        ("intelbras_dvr_day_thumbnail_view", IntelbrasDayThumbnailView, "/api/intelbras_dvr/day_thumb/"),
    ]

    app = getattr(hass.http, "app", None)
    router = getattr(app, "router", None) if app is not None else None

    for data_key, view_cls, url_prefix in view_specs:
        existing = hass.data.get(data_key)
        if existing is None:
            instance = view_cls(hass)
            hass.data[data_key] = instance
        else:
            existing.__class__ = view_cls
            existing.hass = hass
            if data_key == "intelbras_dvr_thumbnail_view":
                if not hasattr(existing, "_sem"):
                    existing._sem = asyncio.Semaphore(2)
                if not hasattr(existing, "_cache"):
                    existing._cache = {}
            elif data_key == "intelbras_dvr_recording_thumbnail_view":
                if not hasattr(existing, "_sem"):
                    existing._sem = asyncio.Semaphore(1)
                if not hasattr(existing, "_cache"):
                    existing._cache = {}
                if not hasattr(existing, "cache_dir"):
                    existing.cache_dir = hass.config.path(".storage", "intelbras_dvr_thumbnails")
            elif data_key == "intelbras_dvr_day_thumbnail_view":
                if not hasattr(existing, "_cache"):
                    existing._cache = {}
                if not hasattr(existing, "cache_dir"):
                    existing.cache_dir = hass.config.path(".storage", "intelbras_dvr_thumbnails")
            instance = existing

        route_found = False
        if router is not None:
            for route in list(router.routes()):
                res_str = str(getattr(route, "resource", ""))
                if url_prefix in res_str:
                    route_found = True

                    def _make_handler(view_inst: HomeAssistantView):
                        async def _handler(request: web.Request) -> web.StreamResponse:
                            return await view_inst.get(request, **request.match_info)
                        return _handler

                    route._handler = _make_handler(instance)

        if not route_found and getattr(hass, "http", None) is not None:
            was_frozen = False
            if router is not None and getattr(router, "_frozen", False):
                was_frozen = True
                router._frozen = False
            try:
                hass.http.register_view(instance)
                _LOGGER.info("Registered HTTP view %s (%s)", view_cls.__name__, instance.url)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.warning("Failed registering HTTP view %s: %s", view_cls.__name__, exc)
            finally:
                if router is not None and was_frozen:
                    router._frozen = True


async def async_handle_thumbnail(
    hass: HomeAssistant,
    entry_id: str,
    channel: str,
    cache: dict[tuple[str, int], dict[str, Any]],
    sem: asyncio.Semaphore,
) -> web.StreamResponse:
    """Process camera thumbnail request with memory caching and fallback."""
    try:
        ch_idx = int(channel)
    except ValueError:
        return web.Response(
            status=HTTPStatus.BAD_REQUEST, text="Invalid channel number"
        )

    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or not hasattr(entry, "runtime_data")
        or entry.runtime_data is None
    ):
        return web.Response(status=HTTPStatus.NOT_FOUND, text="DVR not loaded")

    cache_key = (entry_id, ch_idx)
    now = time_mod.monotonic()
    cached = cache.get(cache_key)
    if cached and (now - cached["ts"]) < CACHE_TTL:
        return web.Response(
            body=cached["data"],
            content_type="image/jpeg",
            headers={"Cache-Control": f"public, max-age={int(CACHE_TTL)}"},
        )

    client: IntelbrasClient = entry.runtime_data.client

    async with sem:
        now = time_mod.monotonic()
        cached = cache.get(cache_key)
        if cached and (now - cached["ts"]) < CACHE_TTL:
            return web.Response(
                body=cached["data"],
                content_type="image/jpeg",
                headers={"Cache-Control": f"public, max-age={int(CACHE_TTL)}"},
            )

        channel_names = entry.data.get("channel_names", {})
        ch_name = channel_names.get(str(ch_idx), channel_names.get(ch_idx, ""))
        clean_name = (
            unicodedata.normalize("NFKD", ch_name)
            .encode("ASCII", "ignore")
            .decode("ASCII")
            .lower()
            .strip()
        )
        configured_channels = entry.data.get(CONF_CHANNELS, [])
        if not clean_name or (
            clean_name in (f"cam {ch_idx + 1}", f"channel {ch_idx + 1}", f"canal {ch_idx + 1}")
            and ch_idx not in configured_channels
        ):
            return web.Response(status=HTTPStatus.NOT_FOUND, text="Channel not configured")

        data: bytes | None = None

        is_ip_channel = (
            client.channel_is_ip(ch_idx)
            if hasattr(client, "channel_is_ip")
            else False
        )

        # 1. Analog BNC channels support direct fast snapshot.cgi from DVR
        if not is_ip_channel:
            try:
                data = await client.get_snapshot(ch_idx, timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("DVR snapshot failed for channel %d: %s", ch_idx, exc)

        # 2. IP channels or analog fallback: fetch from matching camera entity in HA
        if not data:
            data = await async_fetch_from_ha_camera(hass, entry, ch_idx)

        # 3. Stream extraction fallback using sub-stream RTSP
        if not data:
            rtsp_port = entry.options.get(
                CONF_RTSP_PORT, entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
            )
            ch1 = ch_idx + 1
            host = entry.data["host"]
            username = urllib.parse.quote(entry.data["username"], safe="")
            password = urllib.parse.quote(entry.data["password"], safe="")
            sub_url = f"rtsp://{username}:{password}@{host}:{rtsp_port}/cam/realmonitor?channel={ch1}&subtype={1}"
            try:
                data = await async_extract_stream_frame(hass, sub_url, timeout=3.5)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Sub-stream frame extraction failed for ch %d: %s", ch_idx, exc)

        # 4. If still not found and is_ip_channel, try DVR snapshot as last resort
        if not data and is_ip_channel:
            try:
                data = await client.get_snapshot(ch_idx, timeout=2.0)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("DVR IP snapshot failed for channel %d: %s", ch_idx, exc)

        if data:
            cache[cache_key] = {"data": data, "ts": now}
            return web.Response(
                body=data,
                content_type="image/jpeg",
                headers={"Cache-Control": f"public, max-age={int(CACHE_TTL)}"},
            )

        if cached:
            return web.Response(
                body=cached["data"],
                content_type="image/jpeg",
                headers={"Cache-Control": "no-cache"},
            )

        return web.Response(
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            text="Camera snapshot unavailable",
        )


async def async_extract_stream_frame(
    hass: HomeAssistant, stream_url: str, timeout: float = 5.0
) -> bytes | None:
    """Extract a single frame from an RTSP stream (live or playback) using FFmpeg over TCP with downscaling."""
    ffmpeg_bin = shutil.which("ffmpeg") or "/usr/bin/ffmpeg"
    cmd = [
        ffmpeg_bin,
        "-y",
        "-hide_banner",
        "-loglevel",
        "warning",
        "-rtsp_transport",
        "tcp",
        "-fflags",
        "nobuffer",
        "-flags",
        "low_delay",
        "-probesize",
        "65536",
        "-analyzeduration",
        "100000",
        "-skip_frame",
        "nokey",
        "-i",
        stream_url,
        "-an",
        "-sn",
        "-vframes",
        "1",
        "-vf",
        "scale=320:-2",
        "-q:v",
        "5",
        "-c:v",
        "mjpeg",
        "-threads",
        "1",
        "-f",
        "image2",
        "-",
    ]

    proc = None
    t0 = time_mod.monotonic()
    safe_url = stream_url.split("@")[-1] if "@" in stream_url else stream_url
    data_map = hass.data.setdefault(DOMAIN, {})
    active_procs: set = data_map.setdefault(DATA_ACTIVE_THUMBNAIL_PROCS, set())

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        active_procs.add(proc)
        stdout_data, stderr_data = await asyncio.wait_for(
            proc.communicate(), timeout=timeout
        )
        elapsed = time_mod.monotonic() - t0
        err_msg = stderr_data.decode("utf-8", errors="ignore").strip() if stderr_data else ""
        if (
            proc.returncode == 0
            and stdout_data
            and stdout_data.startswith(b"\xff\xd8")
            and len(stdout_data) > 500
        ):
            _LOGGER.debug(
                "[VIEWS] SUCCESS: Fast frame extracted in %.2fs (%d bytes) for %s",
                elapsed,
                len(stdout_data),
                safe_url,
            )
            return stdout_data

        _LOGGER.debug(
            "[VIEWS] FAILED: Code %s in %.2fs (stdout: %db) | Stderr: %s | URL: %s",
            proc.returncode,
            elapsed,
            len(stdout_data) if stdout_data else 0,
            err_msg,
            safe_url,
        )
    except TimeoutError:
        elapsed = time_mod.monotonic() - t0
        _LOGGER.debug("[VIEWS] TIMEOUT: FFmpeg timed out after %.2fs for %s", elapsed, safe_url)
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("[VIEWS] EXCEPTION: %s for %s", exc, safe_url)
        if proc:
            try:
                proc.kill()
                await proc.wait()
            except Exception:  # noqa: BLE001
                pass
    finally:
        if proc:
            active_procs.discard(proc)
            if proc.returncode is None:
                try:
                    proc.kill()
                except Exception:
                    pass
    return None


async def _async_extract_recording_frame(
    hass: HomeAssistant, playback_url: str, timeout: float = 12.0
) -> bytes | None:
    """Extract a single frame from an RTSP playback stream using FFmpeg directly."""
    return await async_extract_stream_frame(hass, playback_url, timeout=timeout)


async def _scope_cache_loop(
    hass: HomeAssistant,
    scope_id: str,
    items: list[tuple[str, int, str, str]],
    cache_dir: str,
    sem: asyncio.Semaphore,
    mem_cache: dict[tuple[str, int, str], bytes] | None = None,
) -> None:
    """Background worker that generates recording thumbnails for the active scope when idle."""
    data_dom = hass.data.setdefault(DOMAIN, {})
    for entry_id, ch_idx, start_s, end_s in items:
        if data_dom.get(DATA_BROWSE_SCOPE, "") != scope_id:
            break

        cache_key = (entry_id, ch_idx, start_s)
        if mem_cache is not None and cache_key in mem_cache:
            continue

        cache_file = os.path.join(cache_dir, f"{entry_id}_{ch_idx}_{start_s}.jpg")
        disk_data = await hass.async_add_executor_job(_read_disk_cache, cache_file)
        if disk_data:
            if mem_cache is not None:
                mem_cache[cache_key] = disk_data
            continue

        while is_playback_active(hass) or data_dom.get(DATA_ACTIVE_HTTP_WAITERS, 0) > 0:
            await asyncio.sleep(0.25)
            if data_dom.get(DATA_BROWSE_SCOPE, "") != scope_id:
                return

        if data_dom.get(DATA_BROWSE_SCOPE, "") != scope_id:
            break

        entry = hass.config_entries.async_get_entry(entry_id)
        if not entry or not hasattr(entry, "runtime_data") or not entry.runtime_data:
            break

        client: IntelbrasClient = entry.runtime_data.client
        rtsp_port = entry.options.get(
            CONF_RTSP_PORT, entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
        )
        ch1 = ch_idx + 1
        try:
            start_dt = datetime.strptime(start_s, "%Y%m%d%H%M%S")
            end_dt = datetime.strptime(end_s, "%Y%m%d%H%M%S")
        except ValueError:
            continue

        playback_url = client.playback_rtsp_url(ch1, start_dt, end_dt, rtsp_port=rtsp_port)

        try:
            async with sem:
                if data_dom.get(DATA_BROWSE_SCOPE, "") != scope_id:
                    break
                if is_playback_active(hass) or data_dom.get(DATA_ACTIVE_HTTP_WAITERS, 0) > 0:
                    continue

                if mem_cache is not None and cache_key in mem_cache:
                    continue

                disk_data = await hass.async_add_executor_job(_read_disk_cache, cache_file)
                if disk_data:
                    if mem_cache is not None:
                        mem_cache[cache_key] = disk_data
                    continue

                data = await _async_extract_recording_frame(hass, playback_url, timeout=6.0)
                await asyncio.sleep(0.15)
                if data:
                    if mem_cache is not None:
                        mem_cache[cache_key] = data
                    await hass.async_add_executor_job(_write_disk_cache, cache_file, data)
        except (asyncio.CancelledError, GeneratorExit):
            break
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Scope cache worker error for ch %d [%s]: %s", ch_idx, start_s, exc)


def start_scope_cache(
    hass: HomeAssistant,
    scope_id: str,
    items: list[tuple[str, int, str, str]],
) -> None:
    """Start background thumbnail caching for the active browsing scope."""
    domain_data = hass.data.setdefault(DOMAIN, {})
    old_task = domain_data.get(DATA_SCOPE_CACHE_TASK)
    if old_task and not old_task.done():
        old_task.cancel()

    view = hass.data.get("intelbras_dvr_recording_thumbnail_view")
    sem = getattr(view, "_sem", None) or asyncio.Semaphore(1)
    mem_cache = getattr(view, "_cache", None)
    cache_dir = getattr(view, "cache_dir", hass.config.path(".storage", "intelbras_dvr_thumbnails"))

    task = hass.async_create_background_task(
        _scope_cache_loop(hass, scope_id, items, cache_dir, sem, mem_cache),
        name=f"intelbras_dvr_scope_cache_{scope_id}",
    )
    domain_data[DATA_SCOPE_CACHE_TASK] = task


def _is_client_disconnected(request: web.Request | None) -> bool:
    """Return True if the HTTP client disconnected/cancelled the request."""
    if request is None:
        return False
    transport = getattr(request, "transport", None)
    if transport is None or transport.is_closing():
        return True
    return False


async def async_handle_recording_thumbnail(
    hass: HomeAssistant,
    entry_id: str,
    channel: str,
    start_s: str,
    end_s: str,
    cache: dict[tuple[str, int, str], bytes],
    sem: asyncio.Semaphore,
    cache_dir: str,
    request: web.Request | None = None,
) -> web.StreamResponse:
    """Handle extraction and serving of a recording thumbnail."""
    try:
        ch_idx = int(channel)
        start_dt = datetime.strptime(start_s, "%Y%m%d%H%M%S")
        end_dt = datetime.strptime(end_s, "%Y%m%d%H%M%S")
    except ValueError:
        return web.Response(
            status=HTTPStatus.BAD_REQUEST, text="Invalid parameters"
        )

    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or not hasattr(entry, "runtime_data")
        or entry.runtime_data is None
    ):
        return web.Response(status=HTTPStatus.NOT_FOUND, text="DVR not loaded")

    cache_key = (entry_id, ch_idx, start_s)
    cached = cache.get(cache_key)
    if cached:
        return web.Response(
            body=cached,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    cache_file = os.path.join(cache_dir, f"{entry_id}_{ch_idx}_{start_s}.jpg")
    disk_data = await hass.async_add_executor_job(_read_disk_cache, cache_file)
    if disk_data:
        cache[cache_key] = disk_data
        return web.Response(
            body=disk_data,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    if is_playback_active(hass):
        return web.Response(
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            text="Playback active",
            headers={
                "Retry-After": "5",
                "Cache-Control": "no-cache, no-store, must-revalidate",
            },
        )

    data_dom = hass.data.setdefault(DOMAIN, {})
    data_dom[DATA_BROWSE_LAST_ACTIVE] = time_mod.monotonic()
    arrival_gen = data_dom.get(DATA_BROWSE_GENERATION, 0)

    client: IntelbrasClient = entry.runtime_data.client
    rtsp_port = entry.options.get(
        CONF_RTSP_PORT, entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
    )
    ch1 = ch_idx + 1
    playback_url = client.playback_rtsp_url(ch1, start_dt, end_dt, rtsp_port=rtsp_port)

    data_dom[DATA_ACTIVE_HTTP_WAITERS] = data_dom.get(DATA_ACTIVE_HTTP_WAITERS, 0) + 1
    try:
        async with sem:
            if _is_client_disconnected(request):
                return web.Response(
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    text="Client disconnected",
                    headers={"Cache-Control": "no-cache, no-store"},
                )

            if (
                data_dom.get(DATA_BROWSE_GENERATION, 0) != arrival_gen
                and not is_recording_thumb_in_scope(hass, entry_id, ch_idx, start_s)
            ):
                return web.Response(
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    text="Scope navigated away",
                    headers={"Retry-After": "10", "Cache-Control": "no-cache, no-store"},
                )

            if is_playback_active(hass):
                return web.Response(
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    text="Playback active",
                    headers={"Retry-After": "5", "Cache-Control": "no-cache, no-store"},
                )

            cached = cache.get(cache_key)
            if cached:
                return web.Response(
                    body=cached,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"},
                )

            disk_data = await hass.async_add_executor_job(_read_disk_cache, cache_file)
            if disk_data:
                cache[cache_key] = disk_data
                return web.Response(
                    body=disk_data,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"},
                )

            data = await _async_extract_recording_frame(hass, playback_url, timeout=6.0)

            await asyncio.sleep(0.15)

            if data:
                cache[cache_key] = data
                await hass.async_add_executor_job(_write_disk_cache, cache_file, data)
                return web.Response(
                    body=data,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"},
                )
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Extraction failed for ch %d [%s]: %s", ch_idx, start_s, exc)
    finally:
        data_dom[DATA_ACTIVE_HTTP_WAITERS] = max(
            0, data_dom.get(DATA_ACTIVE_HTTP_WAITERS, 1) - 1
        )

    return web.Response(
        status=HTTPStatus.SERVICE_UNAVAILABLE,
        text="Recording thumbnail unavailable",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


async def async_handle_day_thumbnail(
    hass: HomeAssistant,
    entry_id: str,
    channel: str,
    day_s: str,
    cache: dict[tuple[str, int, str], bytes],
    cache_dir: str,
    request: web.Request | None = None,
) -> web.StreamResponse:
    """Handle serving a preview thumbnail for an entire day."""
    try:
        ch_idx = int(channel)
        day_dt = datetime.strptime(day_s, "%Y%m%d").date()
    except ValueError:
        return web.Response(
            status=HTTPStatus.BAD_REQUEST, text="Invalid parameters"
        )

    entry = hass.config_entries.async_get_entry(entry_id)
    if (
        entry is None
        or not hasattr(entry, "runtime_data")
        or entry.runtime_data is None
    ):
        return web.Response(status=HTTPStatus.NOT_FOUND, text="DVR not loaded")

    today = dt_util.now().date()
    if day_dt == today:
        thumb_view = hass.data.get("intelbras_dvr_thumbnail_view")
        if thumb_view:
            return await async_handle_thumbnail(
                hass, entry_id, channel, thumb_view._cache, thumb_view._sem
            )

    cache_key = (entry_id, ch_idx, day_s)
    cached = cache.get(cache_key)
    if cached:
        return web.Response(
            body=cached,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    cache_file = os.path.join(cache_dir, f"{entry_id}_{ch_idx}_{day_s}_day.jpg")
    disk_data = await hass.async_add_executor_job(_read_disk_cache, cache_file)
    if disk_data:
        cache[cache_key] = disk_data
        return web.Response(
            body=disk_data,
            content_type="image/jpeg",
            headers={"Cache-Control": "public, max-age=86400"},
        )

    if is_playback_active(hass):
        return web.Response(
            status=HTTPStatus.SERVICE_UNAVAILABLE,
            text="Playback active",
            headers={"Retry-After": "10", "Cache-Control": "no-cache, no-store"},
        )

    data_dom = hass.data.setdefault(DOMAIN, {})
    data_dom[DATA_BROWSE_LAST_ACTIVE] = time_mod.monotonic()
    arrival_gen = data_dom.get(DATA_BROWSE_GENERATION, 0)

    client: IntelbrasClient = entry.runtime_data.client
    ch1 = ch_idx + 1
    start_search = datetime.combine(day_dt, time.min)
    end_search = datetime.combine(day_dt, time.max.replace(microsecond=0))

    rec_view = hass.data.get("intelbras_dvr_recording_thumbnail_view")
    sem = getattr(rec_view, "_sem", None) or asyncio.Semaphore(1)

    data_dom[DATA_ACTIVE_HTTP_WAITERS] = data_dom.get(DATA_ACTIVE_HTTP_WAITERS, 0) + 1
    try:
        async with sem:
            if _is_client_disconnected(request):
                return web.Response(
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    text="Client disconnected",
                    headers={"Cache-Control": "no-cache, no-store"},
                )

            if (
                data_dom.get(DATA_BROWSE_GENERATION, 0) != arrival_gen
                and not is_day_thumb_in_scope(hass, entry_id, ch_idx, day_s)
            ) or is_playback_active(hass):
                return web.Response(
                    status=HTTPStatus.SERVICE_UNAVAILABLE,
                    text="Scope navigated away or playback active",
                    headers={"Retry-After": "10", "Cache-Control": "no-cache, no-store"},
                )

            cached = cache.get(cache_key)
            if cached:
                return web.Response(
                    body=cached,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"},
                )

            disk_data = await hass.async_add_executor_job(_read_disk_cache, cache_file)
            if disk_data:
                cache[cache_key] = disk_data
                return web.Response(
                    body=disk_data,
                    content_type="image/jpeg",
                    headers={"Cache-Control": "public, max-age=86400"},
                )

            try:
                recordings = await client.find_recordings(ch1, start_search, end_search)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Day thumbnail search failed for ch %d on %s: %s", ch_idx, day_s, exc)
                recordings = []

            if recordings:
                daytime_recs = [r for r in recordings if 10 <= r.start.hour <= 16]
                rec = daytime_recs[len(daytime_recs) // 2] if daytime_recs else recordings[len(recordings) // 2]

                rtsp_port = entry.options.get(
                    CONF_RTSP_PORT, entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
                )
                rec_end = max(rec.start + timedelta(seconds=30), min(rec.end, rec.start + timedelta(minutes=5)))
                playback_url = client.playback_rtsp_url(ch1, rec.start, rec_end, rtsp_port=rtsp_port)

                data = await _async_extract_recording_frame(hass, playback_url, timeout=6.0)
                await asyncio.sleep(0.15)
                if data:
                    cache[cache_key] = data
                    await hass.async_add_executor_job(_write_disk_cache, cache_file, data)
                    return web.Response(
                        body=data,
                        content_type="image/jpeg",
                        headers={"Cache-Control": "public, max-age=86400"},
                    )
    except (asyncio.CancelledError, ConnectionResetError):
        pass
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug("Day thumbnail extraction error for ch %d on %s: %s", ch_idx, day_s, exc)
    finally:
        data_dom[DATA_ACTIVE_HTTP_WAITERS] = max(
            0, data_dom.get(DATA_ACTIVE_HTTP_WAITERS, 1) - 1
        )

    return web.Response(
        status=HTTPStatus.SERVICE_UNAVAILABLE,
        text="Day thumbnail unavailable",
        headers={"Cache-Control": "no-cache, no-store, must-revalidate"},
    )


async def async_fetch_from_ha_camera(
    hass: HomeAssistant, entry: Any, ch_idx: int
) -> bytes | None:
    """Fallback to any matching camera entity registered in Home Assistant for live previews."""
    try:
        channel_names: dict[str, str] = entry.data.get("channel_names", {})
        ch_name = channel_names.get(str(ch_idx), channel_names.get(ch_idx, ""))
        if not ch_name:
            return None

        clean = (
            unicodedata.normalize("NFKD", ch_name)
            .encode("ASCII", "ignore")
            .decode("ASCII")
            .lower()
            .strip()
        )
        if clean in (f"cam {ch_idx + 1}", f"channel {ch_idx + 1}", f"canal {ch_idx + 1}", f"cam{ch_idx + 1}", f"channel{ch_idx + 1}"):
            return None

        from homeassistant.components.camera import async_get_image

        cam_ids = [
            s.entity_id
            for s in hass.states.async_all("camera")
            if "frigate" not in s.entity_id and "preview" not in s.entity_id
        ]

        stopwords = {"cam", "camera", "ch", "channel", "canal", "dvr", "intelbras"}
        raw_words = clean.replace("-", " ").replace("_", " ").split()
        words = [w for w in raw_words if w not in stopwords and len(w) >= 3]

        if not words:
            return None

        target_entity_id: str | None = None
        for cid in cam_ids:
            slug = cid.split(".", 1)[-1].lower()
            if all(w in slug for w in words):
                target_entity_id = cid
                break

        if target_entity_id:
            _LOGGER.debug(
                "Intelbras live thumbnail ch %d (%s) fallback to %s",
                ch_idx,
                ch_name,
                target_entity_id,
            )
            image = await async_get_image(hass, target_entity_id, timeout=4)
            if image and image.content:
                return image.content
    except Exception as exc:  # noqa: BLE001
        _LOGGER.debug(
            "Camera entity fallback error for channel %d (%s): %s",
            ch_idx,
            ch_name,
            exc,
        )
    return None
