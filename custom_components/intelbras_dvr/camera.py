"""Camera platform: exposes each DVR channel streams and snapshots based on enabled features."""
from __future__ import annotations

import logging
import time as time_mod
from typing import Any
import urllib.parse

from homeassistant.components.camera import Camera, CameraEntityFeature
from homeassistant.config_entries import ConfigEntry
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
    CONF_RTSP_PORT,
    DEFAULT_RTSP_PORT,
    DEFAULT_SNAPSHOT_INTERVAL,
    CameraFeature,
)
from .coordinator import IntelbrasCoordinator
from .entity import camera_device_info
from .views import async_extract_stream_frame, async_fetch_from_ha_camera

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up camera entities per configured DVR channel strictly following selected features."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }

    entities: list[Camera] = []
    for ch in channels:
        name = channel_names.get(ch, f"Channel {ch + 1}")
        feats: list[str] = features_map.get(str(ch), [])

        # 1. Main Stream (High Resolution - subtype 0)
        if CameraFeature.STREAM_MAIN in feats:
            entities.append(DVRMainCamera(coordinator, entry, ch, name))

        # 2. Sub Stream (Low Resolution - subtype 1)
        if CameraFeature.STREAM_SUB in feats:
            entities.append(DVRSubCamera(coordinator, entry, ch, name))

        # 3. Snapshot (Still image only, no RTSP stream)
        if CameraFeature.SNAPSHOT in feats:
            entities.append(DVRSnapshotCamera(coordinator, entry, ch, name))

    if entities:
        async_add_entities(entities)


class DVRBaseCamera(CoordinatorEntity[IntelbrasCoordinator], Camera):
    """Base camera entity for an Intelbras DVR channel."""

    _attr_has_entity_name = True
    _attr_brand = "Intelbras"

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the base camera entity."""
        Camera.__init__(self)
        CoordinatorEntity.__init__(self, coordinator)
        self._entry = entry
        self._channel = channel
        self._channel_name = channel_name
        self._attr_device_info = camera_device_info(entry, coordinator, channel, channel_name)
        self._cached_image: bytes | None = None
        self._cached_image_time: float = 0.0
        self._last_fetch_duration: float = 0.95

    @property
    def is_on(self) -> bool:
        """Return true if camera is on."""
        return True

    @property
    def available(self) -> bool:
        """Return True if entity is available."""
        return self.coordinator.last_update_success

    @staticmethod
    def _build_rtsp_url(entry: ConfigEntry, channel: int, subtype: int = 0) -> str:
        """Build the RTSP URL for a given channel and stream subtype."""
        host = entry.data["host"]
        username = urllib.parse.quote(entry.data["username"], safe="")
        password = urllib.parse.quote(entry.data["password"], safe="")
        port = entry.options.get(
            CONF_RTSP_PORT, entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)
        )
        ch1 = channel + 1
        return f"rtsp://{username}:{password}@{host}:{port}/cam/realmonitor?channel={ch1}&subtype={subtype}"

    @property
    def frame_interval(self) -> float:
        """Return the sleep interval between frames for HA still stream fallback."""
        target = float(getattr(self.coordinator, "snapshot_interval", DEFAULT_SNAPSHOT_INTERVAL))
        last_dur = getattr(self, "_last_fetch_duration", 0.95)
        return max(0.02, round(target - last_dur, 2))

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return entity attributes ensuring FFmpeg uses TCP transport."""
        return {"extra_arguments": "-rtsp_transport tcp"}

    async def async_camera_image(
        self,
        width: int | None = None,
        height: int | None = None,
        force_refresh: bool = False,
    ) -> bytes | None:
        """Return a still snapshot for this channel."""
        now = time_mod.monotonic()
        if self._cached_image and (now - self._cached_image_time) < 0.01:
            return self._cached_image

        t0 = time_mod.monotonic()

        data: bytes | None = None
        client = self.coordinator.client

        # Dynamically detect IP channels vs Analog BNC
        is_ip_channel = (
            client.channel_is_ip(self._channel)
            if hasattr(client, "channel_is_ip")
            else False
        )

        # 1. Analog channels: direct fast snapshot via DVR HTTP CGI
        if not is_ip_channel:
            t_snap0 = time_mod.monotonic()
            try:
                data = await client.get_snapshot(self._channel, timeout=2.0)
                dt_snap = time_mod.monotonic() - t_snap0
                _LOGGER.debug("[SNAP_BENCH] ch=%d data=%db in %.3fs", self._channel, len(data) if data else 0, dt_snap)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("DVR snapshot failed for channel %d: %s", self._channel, exc)

        # 2. IP channels or analog fallback: fetch from matching camera entity in HA
        if not data and self.hass:
            try:
                data = await async_fetch_from_ha_camera(self.hass, self._entry, self._channel)
            except Exception as exc:  # noqa: BLE001
                _LOGGER.debug("HA camera entity fallback failed for channel %d: %s", self._channel, exc)

        # 3. Stream extraction fallback using FFmpeg over TCP
        if not data and self.hass:
            stream_url = await self.stream_source()
            if not stream_url:
                stream_url = self._build_rtsp_url(self._entry, self._channel, subtype=1)

            if stream_url:
                try:
                    data = await async_extract_stream_frame(self.hass, stream_url, timeout=4.0)
                except Exception as exc:  # noqa: BLE001
                    _LOGGER.debug("FFmpeg stream frame extraction failed for channel %d: %s", self._channel, exc)

        # 4. Last resort for IP channels if everything else failed
        if not data and is_ip_channel:
            try:
                data = await client.get_snapshot(self._channel, timeout=2.0)
            except Exception:  # noqa: BLE001
                pass

        if data:
            self._last_fetch_duration = max(0.0, time_mod.monotonic() - t0)
            self._cached_image = data
            self._cached_image_time = time_mod.monotonic()
            return data

        return self._cached_image


class DVRMainCamera(DVRBaseCamera):
    """Main stream camera entity (High Resolution / subtype 0)."""

    _attr_name = None  # Primary entity named directly after the camera device
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the main stream camera."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_stream"
        self._rtsp_url = self._build_rtsp_url(entry, channel, subtype=0)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return stream attributes including encode settings and TCP transport."""
        attrs = super().extra_state_attributes
        enc = (self.coordinator.data or {}).get("encode_info", {}).get(self._channel, {}).get("main", {})
        if enc:
            if "resolution" in enc:
                attrs["resolution"] = enc["resolution"]
            if "FPS" in enc:
                try:
                    attrs["fps"] = int(float(enc["FPS"]))
                except (ValueError, TypeError):
                    attrs["fps"] = enc["FPS"]
            if "BitRate" in enc:
                try:
                    attrs["bitrate_kbps"] = int(enc["BitRate"])
                except (ValueError, TypeError):
                    attrs["bitrate_kbps"] = enc["BitRate"]
            if "Compression" in enc:
                attrs["codec"] = enc["Compression"]
            if "BitRateControl" in enc:
                attrs["bitrate_control"] = enc["BitRateControl"]
        return attrs

    async def stream_source(self) -> str | None:
        """Return the RTSP URL for the main stream."""
        return self._rtsp_url


class DVRSubCamera(DVRBaseCamera):
    """Sub stream camera entity (Low Resolution / subtype 1, bandwidth-efficient)."""

    _attr_translation_key = "sub_stream"
    _attr_supported_features = CameraEntityFeature.STREAM

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the sub stream camera."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_substream"
        self._rtsp_url = self._build_rtsp_url(entry, channel, subtype=1)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return stream attributes including encode settings and TCP transport."""
        attrs = super().extra_state_attributes
        enc = (self.coordinator.data or {}).get("encode_info", {}).get(self._channel, {}).get("sub", {})
        if enc:
            if "resolution" in enc:
                attrs["resolution"] = enc["resolution"]
            if "FPS" in enc:
                try:
                    attrs["fps"] = int(float(enc["FPS"]))
                except (ValueError, TypeError):
                    attrs["fps"] = enc["FPS"]
            if "BitRate" in enc:
                try:
                    attrs["bitrate_kbps"] = int(enc["BitRate"])
                except (ValueError, TypeError):
                    attrs["bitrate_kbps"] = enc["BitRate"]
            if "Compression" in enc:
                attrs["codec"] = enc["Compression"]
            if "BitRateControl" in enc:
                attrs["bitrate_control"] = enc["BitRateControl"]
        return attrs

    async def stream_source(self) -> str | None:
        """Return the RTSP URL for the sub stream."""
        return self._rtsp_url


class DVRSnapshotCamera(DVRBaseCamera):
    """Snapshot-only camera entity (still images via HTTP CGI/RTSP, no live RTSP stream)."""

    _attr_translation_key = "snapshot"
    _attr_supported_features = CameraEntityFeature(0)
    _attr_icon = "mdi:camera"

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the snapshot-only camera."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_snapshot"

    async def stream_source(self) -> str | None:
        """Return None as snapshot entity does not stream video."""
        return None
