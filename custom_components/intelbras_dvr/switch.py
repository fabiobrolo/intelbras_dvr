"""Switch entities: privacy mask and manual recording per channel."""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

from homeassistant.components.switch import SwitchEntity
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
    DOMAIN,
    CameraFeature,
)
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-camera switch entities."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    event_coordinator = getattr(entry.runtime_data, "event_coordinator", None)
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }

    entities: list = []
    for ch in channels:
        name = channel_names.get(ch, f"Channel {ch + 1}")
        feats: list[str] = features_map.get(str(ch), [])
        if CameraFeature.MASK in feats:
            entities.append(PrivacyMaskSwitch(coordinator, entry, ch, name, event_coordinator))

    async_add_entities(entities)


# ---------------------------------------------------------------------------
# Privacy mask
# ---------------------------------------------------------------------------


class PrivacyMaskSwitch(CameraChannelEntity, SwitchEntity):
    """Switch that masks (blacks out) or restores a single camera channel."""

    _attr_translation_key = "privacy_mask"
    _attr_icon = "mdi:eye-off"
    _attr_assumed_state = False

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
        event_coordinator: Any = None,
    ) -> None:
        """Initialise the privacy mask switch."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_privacy_mask"
        self._event_coordinator = event_coordinator
        self._optimistic_state: bool | None = None
        self._optimistic_until: float = 0.0

    def _handle_coordinator_update(self) -> None:
        """Handle coordinator update while respecting optimistic debounce."""
        if self._optimistic_until and time.monotonic() >= self._optimistic_until:
            self._optimistic_until = 0.0
            self._optimistic_state = None
        super()._handle_coordinator_update()

    @property
    def is_on(self) -> bool:
        """
        Return True when the channel is masked.

        Inferred from picture settings: masked channels have brightness = 0
        and saturation = 0.
        """
        if self._optimistic_until and time.monotonic() < self._optimistic_until:
            return self._optimistic_state is True
        color = self._video_color
        if not color:
            return False
        return color.get("Brightness", 50) == 0 and color.get("Saturation", 80) == 0

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Apply the privacy mask (black picture) to this channel."""
        self._optimistic_state = True
        self._optimistic_until = time.monotonic() + 4.0
        self.async_write_ha_state()

        # Cache existing picture parameters before applying privacy mask
        color = self._video_color
        self.coordinator.client.save_camera_colors(self._channel, color)

        # Immediately update in-memory coordinator data so all picture sliders reflect 0 instantly
        if self.coordinator.data and "video_color" in self.coordinator.data:
            masked_color = {
                "Brightness": 0,
                "Contrast": 0,
                "Hue": 0,
                "Saturation": 0,
            }
            if self.coordinator.client.channel_supports_acutance(self._channel):
                masked_color["Acutance"] = 0
            self.coordinator.data["video_color"][self._channel] = masked_color
        if self._event_coordinator and hasattr(self._event_coordinator, "suppress_video_blind"):
            self._event_coordinator.suppress_video_blind(self._channel, duration=30.0)

        ok = await self.coordinator.client.mask_channels([self._channel], mask=True)
        if ok:
            self._optimistic_until = time.monotonic() + 3.0
            await self.coordinator.async_refresh_video_color()
            self.async_write_ha_state()
        else:
            self._optimistic_until = 0.0
            self._optimistic_state = None
            self.async_write_ha_state()
            _LOGGER.error("Failed to apply privacy mask on channel %d.", self._channel)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Remove the privacy mask and restore the normal picture."""
        self._optimistic_state = False
        self._optimistic_until = time.monotonic() + 4.0
        self.async_write_ha_state()

        # Retrieve saved settings
        saved = self.coordinator.client.get_saved_camera_colors(self._channel)

        # Immediately update in-memory coordinator data so all 5 sliders restore instantly
        if self.coordinator.data and "video_color" in self.coordinator.data:
            self.coordinator.data["video_color"][self._channel] = dict(saved)
            self.coordinator.async_update_listeners()

        if self._event_coordinator and hasattr(self._event_coordinator, "suppress_video_blind"):
            self._event_coordinator.suppress_video_blind(self._channel, duration=30.0)

        ok = await self.coordinator.client.mask_channels([self._channel], mask=False)
        if ok:
            self._optimistic_until = time.monotonic() + 3.0
            await self.coordinator.async_refresh_video_color()
            self.async_write_ha_state()
        else:
            self._optimistic_until = 0.0
            self._optimistic_state = None
            self.async_write_ha_state()
            _LOGGER.error("Failed to remove privacy mask on channel %d.", self._channel)

