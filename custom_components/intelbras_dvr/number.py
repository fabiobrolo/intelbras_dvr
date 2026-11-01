"""Number entities for per-channel video picture controls."""
from __future__ import annotations

import asyncio
import logging
import time
from dataclasses import dataclass
from typing import Any

from homeassistant.components.number import NumberDeviceClass, NumberEntity, NumberEntityDescription, NumberMode
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
    CONF_SNAPSHOT_INTERVAL,
    DEFAULT_SNAPSHOT_INTERVAL,
    DOMAIN,
    CameraFeature,
)
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity, DVREntity

_LOGGER = logging.getLogger(__name__)

# Default values used when resetting picture settings.
DEFAULT_VIDEO_COLOR: dict[str, int] = {
    "Brightness": 50,
    "Contrast": 50,
    "Hue": 50,
    "Saturation": 80,
    "Acutance": 1,
}


@dataclass(frozen=True, kw_only=True)
class VideoColorDescription(NumberEntityDescription):
    """Extend NumberEntityDescription with the DVR property key."""

    dvr_key: str  # property name in the VideoColor response


VIDEO_COLOR_DESCRIPTIONS: tuple[VideoColorDescription, ...] = (
    VideoColorDescription(
        key="brightness",
        translation_key="brightness",
        icon="mdi:brightness-6",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        dvr_key="Brightness",
    ),
    VideoColorDescription(
        key="contrast",
        translation_key="contrast",
        icon="mdi:contrast-circle",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        dvr_key="Contrast",
    ),
    VideoColorDescription(
        key="hue",
        translation_key="hue",
        icon="mdi:palette",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        dvr_key="Hue",
    ),
    VideoColorDescription(
        key="saturation",
        translation_key="saturation",
        icon="mdi:invert-colors",
        native_min_value=0,
        native_max_value=100,
        native_step=1,
        dvr_key="Saturation",
    ),
    VideoColorDescription(
        key="acutance",
        translation_key="acutance",
        icon="mdi:image-filter-none",
        native_min_value=0,
        native_max_value=15,
        native_step=1,
        dvr_key="Acutance",
    ),
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up video colour number sliders for channels with the feature enabled."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }

    entities: list[NumberEntity] = [
        DVRSnapshotIntervalNumber(coordinator, entry),
    ]
    for ch in channels:
        if CameraFeature.VIDEO_COLOR not in features_map.get(str(ch), []):
            continue
        ch_name = channel_names.get(ch, f"Channel {ch + 1}")
        supports_acutance = coordinator.client.channel_supports_acutance(ch)
        for desc in VIDEO_COLOR_DESCRIPTIONS:
            if desc.dvr_key == "Acutance" and not supports_acutance:
                continue
            entities.append(
                VideoColorNumber(
                    coordinator=coordinator,
                    entry=entry,
                    channel=ch,
                    channel_name=ch_name,
                    description=desc,
                )
            )

    async_add_entities(entities)


class VideoColorNumber(CameraChannelEntity, NumberEntity):
    """A slider that reads and writes a single picture setting on one channel."""

    _attr_mode = NumberMode.SLIDER
    entity_description: VideoColorDescription

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
        description: VideoColorDescription,
    ) -> None:
        """Initialise the number entity."""
        super().__init__(coordinator, entry, channel, channel_name)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_{description.key}"
        self._attr_native_min_value = description.native_min_value
        self._attr_native_max_value = description.native_max_value
        self._attr_native_step = description.native_step
        self._optimistic_value: float | None = None
        self._optimistic_until: float = 0.0

    @property
    def available(self) -> bool:
        """Return True if coordinator is available and privacy mask is not active."""
        if not super().available:
            return False
        color = self._video_color
        if not color:
            return True
        # Block controls when privacy mask is active (Brightness == 0 and Saturation == 0)
        return not (color.get("Brightness") == 0 and color.get("Saturation") == 0)

    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update while respecting optimistic debounce."""
        if self._optimistic_until and time.monotonic() >= self._optimistic_until:
            self._optimistic_until = 0.0
            self._optimistic_value = None
        super()._handle_coordinator_update()

    @property
    def native_value(self) -> float | None:
        """Return the current setting value; use optimistic value if pending."""
        if self._optimistic_until and time.monotonic() < self._optimistic_until:
            return self._optimistic_value
        color = self._video_color
        if not color:
            return None
        return float(color.get(self.entity_description.dvr_key, 50))

    async def async_set_native_value(self, value: float) -> None:
        """Write the new value to the DVR, preserving all other settings."""
        if not self.available:
            _LOGGER.warning(
                "Cannot change %s on channel %d while privacy mask is active.",
                self.entity_description.dvr_key,
                self._channel,
            )
            return

        # Optimistic update — reflect the new value immediately in the UI.
        self._optimistic_value = value
        self._optimistic_until = time.monotonic() + 4.0
        self.async_write_ha_state()

        # Read current values so we only change the one property.
        color = self._video_color
        supports_acutance = self.coordinator.client.channel_supports_acutance(self._channel)
        kwargs: dict[str, Any] = {
            "channel": self._channel,
            "brightness": int(color.get("Brightness", DEFAULT_VIDEO_COLOR["Brightness"])),
            "contrast": int(color.get("Contrast", DEFAULT_VIDEO_COLOR["Contrast"])),
            "hue": int(color.get("Hue", DEFAULT_VIDEO_COLOR["Hue"])),
            "acutance": int(color.get("Acutance", DEFAULT_VIDEO_COLOR["Acutance"])) if supports_acutance else None,
            "saturation": int(color.get("Saturation", DEFAULT_VIDEO_COLOR["Saturation"])),
            "profile": None,
        }
        # Map DVR key to method argument name
        key_map = {
            "Brightness": "brightness",
            "Contrast": "contrast",
            "Hue": "hue",
            "Acutance": "acutance",
            "Saturation": "saturation",
        }
        kwargs[key_map[self.entity_description.dvr_key]] = int(value)

        # Update in-memory coordinator data immediately
        if self.coordinator.data and "video_color" in self.coordinator.data:
            self.coordinator.data["video_color"].setdefault(self._channel, {})[self.entity_description.dvr_key] = int(value)

        ok = await self.coordinator.client.set_video_color(**kwargs)
        if ok:
            # Keep optimistic value active for 3 seconds so background debounce doesn't glitch slider
            self._optimistic_until = time.monotonic() + 3.0
            await self.coordinator.async_refresh_video_color()
            self.async_write_ha_state()
        else:
            # Revert optimistic value on failure.
            self._optimistic_until = 0.0
            self._optimistic_value = None
            await self.coordinator.async_refresh_video_color()
            self.async_write_ha_state()
            _LOGGER.error(
                "Failed to set %s=%s on channel %d.",
                self.entity_description.dvr_key,
                value,
                self._channel,
            )


class DVRSnapshotIntervalNumber(DVREntity, NumberEntity):
    """A slider on the DVR parent device to configure the snapshot update rate / caching interval."""

    _attr_translation_key = "snapshot_interval"
    _attr_icon = "mdi:camera-timer"
    _attr_mode = NumberMode.SLIDER
    _attr_native_min_value = 0.5
    _attr_native_max_value = 30.0
    _attr_native_step = 0.5
    _attr_native_unit_of_measurement = "s"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
    ) -> None:
        """Initialise the snapshot interval number entity."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_snapshot_interval"

    @property
    def native_value(self) -> float:
        """Return the current snapshot interval in seconds."""
        return float(getattr(self.coordinator, "snapshot_interval", DEFAULT_SNAPSHOT_INTERVAL))

    async def async_set_native_value(self, value: float) -> None:
        """Set a new snapshot interval in seconds and update coordinator."""
        val = round(max(0.5, min(30.0, float(value))), 1)
        self.coordinator.snapshot_interval = val
        new_options = dict(self._entry.options)
        new_options[CONF_SNAPSHOT_INTERVAL] = val
        self.hass.config_entries.async_update_entry(self._entry, options=new_options)
        self.async_write_ha_state()

