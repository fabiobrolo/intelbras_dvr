"""Media source: DVR recordings via mediaFileFind and RTSP/HLS playback."""
from __future__ import annotations

import asyncio
from datetime import date, datetime, time, timedelta
import logging
import time as time_mod
import urllib.parse

import aiohttp

from homeassistant.components.camera import DynamicStreamSettings
from homeassistant.components.media_player import MediaClass, MediaType
from homeassistant.components.media_source import (
    BrowseMediaSource,
    MediaSource,
    MediaSourceItem,
    PlayMedia,
    Unresolvable,
)
from homeassistant.components.stream import create_stream
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.aiohttp_client import async_get_clientsession
from homeassistant.util import dt as dt_util

from .const import (
    CONF_CHANNELS,
    CONF_RTSP_PORT,
    DEFAULT_RTSP_PORT,
    DOMAIN,
    HLS_MIME,
    MEDIA_SLICE_MINUTES,
)
from .dvr import IntelbrasClient
from .views import (
    DATA_ACTIVE_PLAYBACK_STREAM,
    clear_playback_active,
    is_playback_active,
    set_browse_scope,
    set_playback_active,
    start_scope_cache,
)

_LOGGER = logging.getLogger(__name__)

_COMPACT_FMT = "%Y%m%d%H%M%S"
_DAY_FMT = "%Y%m%d"


async def async_get_media_source(hass: HomeAssistant) -> "IntelbrasMediaSource":
    """Register the DVR media source."""
    return IntelbrasMediaSource(hass)


class IntelbrasMediaSource(MediaSource):
    """DVR recordings media source provider: channels -> days -> hours -> clips."""

    name = "Intelbras DVR"

    def __init__(self, hass: HomeAssistant) -> None:
        super().__init__(DOMAIN)
        self.hass = hass
        self._earliest_date_cache: dict[tuple[str, int], tuple[float, date]] = {}

    def _is_pt(self) -> bool:
        """Return True if the Home Assistant language is Portuguese."""
        lang = (getattr(self.hass.config, "language", "") or "").lower()
        return lang.startswith("pt")

    def _entries(self) -> list[ConfigEntry]:
        return [
            entry
            for entry in self.hass.config_entries.async_entries(DOMAIN)
            if hasattr(entry, "runtime_data") and entry.runtime_data is not None
        ]

    def _client(self, entry_id: str) -> tuple[ConfigEntry, IntelbrasClient]:
        entry = self.hass.config_entries.async_get_entry(entry_id)
        if entry is None or not hasattr(entry, "runtime_data") or entry.runtime_data is None:
            raise Unresolvable("DVR is not loaded")
        return entry, entry.runtime_data.client

    def _channel_names(self, entry: ConfigEntry) -> dict[int, str]:
        raw: dict[str, str] = entry.data.get("channel_names", {})
        return {int(k): v for k, v in raw.items()}

    # ---------- Playback & Dynamic go2rtc Discovery ----------

    async def _resolve_go2rtc_url(self) -> str | None:
        """Dynamically discover the configured go2rtc URL in Home Assistant."""
        go2rtc_data = self.hass.data.get("go2rtc")
        if isinstance(go2rtc_data, str) and go2rtc_data.startswith("http"):
            return go2rtc_data
        if isinstance(go2rtc_data, dict) and isinstance(go2rtc_data.get("url"), str):
            return go2rtc_data["url"]
        if hasattr(go2rtc_data, "url") and isinstance(getattr(go2rtc_data, "url"), str):
            return getattr(go2rtc_data, "url")

        domain_data = self.hass.data.setdefault(DOMAIN, {})
        if "resolved_go2rtc_url" in domain_data:
            return domain_data["resolved_go2rtc_url"]

        for entry in self.hass.config_entries.async_entries("webrtc"):
            url = entry.data.get("url") or entry.options.get("url")
            if isinstance(url, str) and url.startswith("http"):
                domain_data["resolved_go2rtc_url"] = url
                return url

        def _read_go2rtc_from_config() -> str | None:
            try:
                cfg_path = self.hass.config.path("configuration.yaml")
                with open(cfg_path, encoding="utf-8") as fh:
                    in_go2rtc = False
                    for raw_line in fh:
                        line = raw_line.split("#", 1)[0].rstrip()
                        if not line.strip():
                            continue
                        if not line.startswith((" ", "\t")):
                            in_go2rtc = line.strip().startswith("go2rtc:")
                            continue
                        if in_go2rtc and "url:" in line:
                            val = line.split("url:", 1)[1].strip().strip("'\"")
                            if val.startswith("http"):
                                return val
            except Exception:  # noqa: BLE001
                pass
            return None

        url = await self.hass.async_add_executor_job(_read_go2rtc_from_config)
        domain_data["resolved_go2rtc_url"] = url
        return url

    async def _prepare_playback_url(
        self,
        entry_id: str,
        client: IntelbrasClient,
        channel: int,
        dvr_playback_url: str,
    ) -> tuple[str, str | None]:
        """Ensure playback RTSP stream has browser-compatible AAC audio using go2rtc if needed."""
        try:
            src, needs_go2rtc = await client.get_channel_playback_directive(channel, dvr_playback_url)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Error querying dynamic playback directive for channel %s: %s", channel, exc)
            src, needs_go2rtc = dvr_playback_url, False

        if not needs_go2rtc:
            _LOGGER.debug("Channel %s using direct DVR RTSP (no audio recorded in DVR)", channel)
            return dvr_playback_url, None

        go2rtc_url = await self._resolve_go2rtc_url()
        if not go2rtc_url:
            _LOGGER.debug("No go2rtc URL configured in Home Assistant; falling back to direct DVR RTSP")
            return dvr_playback_url, None

        try:
            parsed = urllib.parse.urlparse(go2rtc_url)
            go2rtc_host = parsed.hostname
            if not go2rtc_host:
                return dvr_playback_url, None

            stream_name = f"intelbras_playback_{entry_id}_{channel}"
            put_api = f"{go2rtc_url.rstrip('/')}/api/streams?name={stream_name}&src={urllib.parse.quote(src)}"

            session = async_get_clientsession(self.hass)
            async with session.put(put_api, timeout=aiohttp.ClientTimeout(total=4.0)) as resp:
                if resp.status in (200, 201):
                    go2rtc_rtsp = f"rtsp://{go2rtc_host}:8554/{stream_name}"
                    _LOGGER.info(
                        "Registered go2rtc dynamic playback stream for channel %s: %s -> %s",
                        channel,
                        stream_name,
                        go2rtc_rtsp,
                    )
                    return go2rtc_rtsp, stream_name
                _LOGGER.debug("go2rtc PUT failed with HTTP %s", resp.status)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("go2rtc playback registration failed, falling back to DVR RTSP: %s", exc)

        return dvr_playback_url, None

    async def _cleanup_go2rtc_playback(self, stream_name: str | None) -> None:
        """Remove temporary playback stream from go2rtc when playback ends."""
        if not stream_name:
            return
        go2rtc_url = await self._resolve_go2rtc_url()
        if not go2rtc_url:
            return

        try:
            del_api = f"{go2rtc_url.rstrip('/')}/api/streams?name={stream_name}"
            session = async_get_clientsession(self.hass)
            async with session.delete(del_api, timeout=aiohttp.ClientTimeout(total=2.0)):
                _LOGGER.debug("Cleaned up go2rtc playback stream: %s", stream_name)
        except Exception as exc:  # noqa: BLE001
            _LOGGER.debug("Error cleaning up go2rtc stream %s: %s", stream_name, exc)

    async def async_resolve_media(self, item: MediaSourceItem) -> PlayMedia:
        parts = (item.identifier or "").split("|")
        if len(parts) != 5 or parts[0] != "PLAY":
            raise Unresolvable(f"Invalid identifier: {item.identifier}")
        _, entry_id, channel, start_s, end_s = parts
        entry, client = self._client(entry_id)
        try:
            start = datetime.strptime(start_s, _COMPACT_FMT)
            end = datetime.strptime(end_s, _COMPACT_FMT)
        except ValueError as ex:
            raise Unresolvable(f"Invalid interval: {item.identifier}") from ex

        ch_idx = int(channel)
        ch1 = ch_idx + 1

        # Immediately assert playback priority to kill running thumbnail procs and block new ones
        set_playback_active(self.hass, duration=45.0)

        domain_data = self.hass.data.setdefault(DOMAIN, {})
        old_stream = domain_data.get(DATA_ACTIVE_PLAYBACK_STREAM)
        old_g2r = domain_data.get("active_playback_g2r")
        if old_stream is not None:
            try:
                await old_stream.stop()
            except Exception as ex:  # noqa: BLE001
                _LOGGER.debug("Error stopping previous stream: %s", ex)
            domain_data[DATA_ACTIVE_PLAYBACK_STREAM] = None
        if old_g2r:
            await self._cleanup_go2rtc_playback(old_g2r)
            domain_data["active_playback_g2r"] = None

        rtsp_port = entry.options.get(CONF_RTSP_PORT, entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT))
        raw_dvr_url = client.playback_rtsp_url(ch1, start, end, rtsp_port=rtsp_port)

        url, go2rtc_stream_name = await self._prepare_playback_url(entry_id, client, ch1, raw_dvr_url)

        _LOGGER.info(
            "Starting Intelbras DVR playback stream for channel %s (%s to %s) via %s",
            channel,
            start_s,
            end_s,
            url,
        )

        try:
            stream = create_stream(
                self.hass,
                url,
                {},
                DynamicStreamSettings(),
            )
            stream.add_provider("hls", timeout=300)
        except Exception as ex:
            clear_playback_active(self.hass)
            await self._cleanup_go2rtc_playback(go2rtc_stream_name)
            _LOGGER.error("Failed to create playback stream for %s: %s", url, ex)
            raise Unresolvable(f"DVR stream error: {ex}") from ex

        domain_data[DATA_ACTIVE_PLAYBACK_STREAM] = stream
        domain_data["active_playback_g2r"] = go2rtc_stream_name

        stream_url = stream.endpoint_url("hls").replace("master_", "")
        return PlayMedia(stream_url, HLS_MIME)

    # ---------- Navigation ----------

    async def async_browse_media(self, item: MediaSourceItem) -> BrowseMediaSource:
        identifier = item.identifier or ""
        set_browse_scope(self.hass, identifier)

        domain_data = self.hass.data.setdefault(DOMAIN, {})
        old_g2r = domain_data.get("active_playback_g2r")
        if old_g2r and not is_playback_active(self.hass):
            self.hass.async_create_background_task(
                self._cleanup_go2rtc_playback(old_g2r),
                name="intelbras_cleanup_go2rtc",
            )
            domain_data["active_playback_g2r"] = None

        if not identifier:
            return self._browse_root()
        kind = identifier.split("|")[0]
        if kind == "CH":
            return self._browse_channels(identifier)
        if kind == "DAY":
            return await self._browse_days(identifier)
        if kind == "FILES":
            return await self._browse_hours(identifier)
        if kind == "ALLFILES":
            return await self._browse_all_day_slices(identifier)
        if kind == "HOUR":
            return await self._browse_slices(identifier)
        raise Unresolvable(f"Unknown path: {identifier}")

    def _browse_root(self) -> BrowseMediaSource:
        entries = self._entries()
        if not entries:
            raise Unresolvable("No DVR configured")
        if len(entries) == 1:
            return self._browse_channels(f"CH|{entries[0].entry_id}")
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=None,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title="Intelbras DVR",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=[
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"CH|{entry.entry_id}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="",
                    title=entry.title,
                    can_play=False,
                    can_expand=True,
                )
                for entry in entries
            ],
        )

    def _channel_thumbnail(self, entry: ConfigEntry, ch: int) -> str:
        """Return optimal preview thumbnail URL for channel."""
        return f"/api/intelbras_dvr/thumbnail/{entry.entry_id}/{ch}"

    def _browse_channels(self, identifier: str) -> BrowseMediaSource:
        _, entry_id = identifier.split("|")
        entry, _ = self._client(entry_id)
        channel_names = self._channel_names(entry)
        configured_channels = entry.data.get(CONF_CHANNELS, [])

        def _is_channel_active(ch: int, name: str) -> bool:
            clean = name.strip().lower()
            if not clean:
                return False
            if clean in (
                f"cam {ch + 1}",
                f"channel {ch + 1}",
                f"canal {ch + 1}",
                f"cam{ch + 1}",
                f"channel{ch + 1}",
            ):
                return ch in configured_channels
            return True

        all_channels = [
            ch for ch in sorted(set(channel_names.keys()) | set(configured_channels))
            if _is_channel_active(ch, channel_names.get(ch, ""))
        ]
        if not all_channels:
            all_channels = list(range(entry.data.get("channel_count", 8)))

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=entry.title,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.CHANNEL,
            children=[
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"DAY|{entry_id}|{ch}",
                    media_class=MediaClass.CHANNEL,
                    media_content_type=MediaType.PLAYLIST,
                    title=channel_names.get(ch, f"Channel {ch + 1}"),
                    thumbnail=self._channel_thumbnail(entry, ch),
                    can_play=False,
                    can_expand=True,
                    children_media_class=MediaClass.DIRECTORY,
                )
                for ch in all_channels
            ],
        )

    async def _browse_days(self, identifier: str) -> BrowseMediaSource:
        _, entry_id, channel = identifier.split("|")
        entry, client = self._client(entry_id)
        ch_idx = int(channel)
        channel_names = self._channel_names(entry)
        ch_name = channel_names.get(ch_idx, f"Channel {ch_idx + 1}")
        ch_thumb = self._channel_thumbnail(entry, ch_idx)

        today = dt_util.now().date()
        days_count = 10

        cache_key = (entry_id, ch_idx)
        now_ts = time_mod.monotonic()
        earliest_date = None
        if cache_key in self._earliest_date_cache:
            cached_ts, cached_date = self._earliest_date_cache[cache_key]
            if now_ts - cached_ts < 600.0:
                earliest_date = cached_date

        if earliest_date is None:
            try:
                ch1 = ch_idx + 1
                earliest_date = await asyncio.wait_for(
                    client.find_earliest_recording_date(ch1),
                    timeout=6.0,
                )
                if earliest_date:
                    self._earliest_date_cache[cache_key] = (now_ts, earliest_date)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("Earliest date lookup failed for ch %d: %s", ch_idx, exc)

        if not earliest_date and getattr(client, "_dvr_earliest_date", None):
            earliest_date = client._dvr_earliest_date

        if earliest_date and earliest_date <= today:
            days_count = max(1, min(180, (today - earliest_date).days + 1))

        children = [
            BrowseMediaSource(
                domain=DOMAIN,
                identifier=f"FILES|{entry_id}|{channel}|{day.strftime(_DAY_FMT)}",
                media_class=MediaClass.DIRECTORY,
                media_content_type="",
                title=day.strftime("%d/%m/%Y"),
                thumbnail=f"/api/intelbras_dvr/day_thumb/{entry_id}/{channel}/{day.strftime(_DAY_FMT)}",
                can_play=False,
                can_expand=True,
                children_media_class=MediaClass.DIRECTORY,
            )
            for day in (today - timedelta(days=n) for n in range(days_count))
        ]
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=ch_name,
            thumbnail=ch_thumb,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    async def _day_recordings(self, entry_id: str, channel: str, day_s: str):
        _, client = self._client(entry_id)
        day = datetime.strptime(day_s, _DAY_FMT).date()
        start = datetime.combine(day, time.min)
        end = datetime.combine(day, time.max.replace(microsecond=0))
        ch1 = int(channel) + 1
        try:
            recordings = await client.find_recordings(ch1, start, end)
        except Exception as ex:  # noqa: BLE001
            _LOGGER.warning("Listing recordings failed: %s", ex)
            raise Unresolvable("DVR unavailable") from ex
        return day, recordings

    async def _browse_hours(self, identifier: str) -> BrowseMediaSource:
        """One day: folders per hour, only for hours containing recordings."""
        _, entry_id, channel, day_s = identifier.split("|")
        day, recordings = await self._day_recordings(entry_id, channel, day_s)
        ch_idx = int(channel)

        day_start = datetime.combine(day, time.min)
        day_end_clamped = datetime.combine(day, time.max.replace(microsecond=0))
        hours = sorted(
            {
                hour
                for rec in recordings
                if rec.end > day_start and rec.start <= day_end_clamped
                for hour in range(
                    max(rec.start, day_start).hour,
                    min(rec.end - timedelta(seconds=1), day_end_clamped).hour + 1,
                )
            }
        )
        children = []
        step = timedelta(minutes=MEDIA_SLICE_MINUTES)
        items_to_cache = []

        if recordings:
            first_rec = recordings[0]
            actual_first_start = max(first_rec.start, day_start)
            start_c = actual_first_start.strftime(_COMPACT_FMT)
            end_c = (actual_first_start + step).strftime(_COMPACT_FMT)
            all_day_thumb = f"/api/intelbras_dvr/recording_thumb/{entry_id}/{channel}/{start_c}/{end_c}?v=3"
            items_to_cache.append((entry_id, ch_idx, start_c, end_c))
            all_day_title = (
                f"📅 {day.strftime('%d/%m/%Y')} – Ver Tudo (Linha do Tempo)"
                if self._is_pt()
                else f"📅 {day.strftime('%d/%m/%Y')} – View All (Timeline)"
            )
            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"ALLFILES|{entry_id}|{channel}|{day_s}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="",
                    title=all_day_title,
                    thumbnail=all_day_thumb,
                    can_play=False,
                    can_expand=True,
                    children_media_class=MediaClass.VIDEO,
                )
            )

        for hour in hours:
            hour_start = datetime.combine(day, time(hour=hour))
            hour_end = hour_start + timedelta(hours=1)
            hour_thumb = None
            slot = hour_start
            while slot < hour_end:
                slot_end = slot + step
                matching_recs = [rec for rec in recordings if rec.start < slot_end and rec.end > slot]
                if matching_recs:
                    actual_start = max(slot, min(rec.start for rec in matching_recs))
                    st_c = actual_start.strftime(_COMPACT_FMT)
                    et_c = slot_end.strftime(_COMPACT_FMT)
                    hour_thumb = f"/api/intelbras_dvr/recording_thumb/{entry_id}/{channel}/{st_c}/{et_c}?v=3"
                    if (entry_id, ch_idx, st_c, et_c) not in items_to_cache:
                        items_to_cache.append((entry_id, ch_idx, st_c, et_c))
                    break
                slot = slot_end

            children.append(
                BrowseMediaSource(
                    domain=DOMAIN,
                    identifier=f"HOUR|{entry_id}|{channel}|{day_s}|{hour:02d}",
                    media_class=MediaClass.DIRECTORY,
                    media_content_type="",
                    title=f"{hour:02d}:00 – {hour:02d}:59",
                    thumbnail=hour_thumb,
                    can_play=False,
                    can_expand=True,
                    children_media_class=MediaClass.VIDEO,
                )
            )
        if items_to_cache:
            start_scope_cache(self.hass, identifier, items_to_cache)

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=day.strftime("%d/%m/%Y"),
            thumbnail=f"/api/intelbras_dvr/day_thumb/{entry_id}/{channel}/{day_s}",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.DIRECTORY,
            children=children,
        )

    async def _browse_slices(self, identifier: str) -> BrowseMediaSource:
        """One hour: clips of MEDIA_SLICE_MINUTES length covered by recordings."""
        _, entry_id, channel, day_s, hour_s = identifier.split("|")
        day, recordings = await self._day_recordings(entry_id, channel, day_s)
        ch_idx = int(channel)
        entry, _ = self._client(entry_id)
        ch_thumb = self._channel_thumbnail(entry, ch_idx)

        hour = int(hour_s)
        hour_start = datetime.combine(day, time(hour=hour))
        hour_end = hour_start + timedelta(hours=1)
        step = timedelta(minutes=MEDIA_SLICE_MINUTES)
        children = []
        items_to_cache = []
        slot = hour_start
        while slot < hour_end:
            slot_end = slot + step
            matching_recs = [rec for rec in recordings if rec.start < slot_end and rec.end > slot]
            if matching_recs:
                actual_start = max(slot, min(rec.start for rec in matching_recs))
                start_compact = actual_start.strftime(_COMPACT_FMT)
                end_compact = slot_end.strftime(_COMPACT_FMT)
                thumb_url = f"/api/intelbras_dvr/recording_thumb/{entry_id}/{channel}/{start_compact}/{end_compact}?v=3"
                items_to_cache.append((entry_id, ch_idx, start_compact, end_compact))
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=(
                            f"PLAY|{entry_id}|{channel}"
                            f"|{start_compact}"
                            f"|{end_compact}"
                        ),
                        media_class=MediaClass.VIDEO,
                        media_content_type=MediaType.VIDEO,
                        title=f"{slot.strftime('%H:%M')} – {slot_end.strftime('%H:%M')}",
                        thumbnail=thumb_url,
                        can_play=True,
                        can_expand=False,
                    )
                )
            slot = slot_end
        if items_to_cache:
            start_scope_cache(self.hass, identifier, items_to_cache)

        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=f"{day.strftime('%d/%m/%Y')} {hour:02d}h",
            thumbnail=ch_thumb,
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.VIDEO,
            children=children,
        )

    async def _browse_all_day_slices(self, identifier: str) -> BrowseMediaSource:
        """One full day: flat list of all slices for timeline ribbon and gallery."""
        _, entry_id, channel, day_s = identifier.split("|")
        day, recordings = await self._day_recordings(entry_id, channel, day_s)
        ch_idx = int(channel)
        entry, _ = self._client(entry_id)
        channel_names = self._channel_names(entry)
        ch_name = channel_names.get(ch_idx, f"Channel {ch_idx + 1}")

        step = timedelta(minutes=MEDIA_SLICE_MINUTES)
        children = []
        day_start = datetime.combine(day, time.min)
        day_end = day_start + timedelta(days=1)
        items_to_cache = []
        slot = day_start
        while slot < day_end:
            slot_end = slot + step
            matching_recs = [rec for rec in recordings if rec.start < slot_end and rec.end > slot]
            if matching_recs:
                actual_start = max(slot, min(rec.start for rec in matching_recs))
                start_compact = actual_start.strftime(_COMPACT_FMT)
                end_compact = slot_end.strftime(_COMPACT_FMT)
                thumb_url = f"/api/intelbras_dvr/recording_thumb/{entry_id}/{channel}/{start_compact}/{end_compact}?v=3"
                items_to_cache.append((entry_id, ch_idx, start_compact, end_compact))
                children.append(
                    BrowseMediaSource(
                        domain=DOMAIN,
                        identifier=(
                            f"PLAY|{entry_id}|{channel}"
                            f"|{start_compact}"
                            f"|{end_compact}"
                        ),
                        media_class=MediaClass.VIDEO,
                        media_content_type=MediaType.VIDEO,
                        title=f"{slot.strftime('%H:%M')} – {slot_end.strftime('%H:%M')}",
                        thumbnail=thumb_url,
                        can_play=True,
                        can_expand=False,
                    )
                )
            slot = slot_end

        if items_to_cache:
            start_scope_cache(self.hass, identifier, items_to_cache)

        timeline_label = "Linha do Tempo" if self._is_pt() else "Timeline"
        return BrowseMediaSource(
            domain=DOMAIN,
            identifier=identifier,
            media_class=MediaClass.DIRECTORY,
            media_content_type="",
            title=f"{ch_name} – {day.strftime('%d/%m/%Y')} ({timeline_label})",
            thumbnail=f"/api/intelbras_dvr/day_thumb/{entry_id}/{channel}/{day_s}",
            can_play=False,
            can_expand=True,
            children_media_class=MediaClass.VIDEO,
            children=children,
        )
