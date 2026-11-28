"""Text entity platform for Intelbras DVR (OSD dynamic text overlay)."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING

from homeassistant.components.text import TextEntity, TextEntityDescription, TextMode
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

if TYPE_CHECKING:
    from . import IntelbrasDVRConfigEntry
from .const import CONF_CHANNELS, CONF_FEATURES, CameraFeature
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity

_LOGGER = logging.getLogger(__name__)

PARALLEL_UPDATES = 1

CHANNEL_TITLE_TEXT_DESCRIPTION = TextEntityDescription(
    key="channel_title_text",
    translation_key="channel_title_text",
    icon="mdi:card-text",
    entity_category=EntityCategory.CONFIG,
    native_max=64,
    native_min=1,
    mode=TextMode.TEXT,
)

CUSTOM_TITLE_TEXT_DESCRIPTION = TextEntityDescription(
    key="custom_title_text",
    translation_key="custom_title_text",
    icon="mdi:card-text-outline",
    entity_category=EntityCategory.CONFIG,
    native_max=64,
    native_min=0,
    mode=TextMode.TEXT,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntelbrasDVRConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up text entities for Intelbras DVR channels."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }
    features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})

    entities: list[TextEntity] = []
    has_any_overlay = any(CameraFeature.OVERLAY in f for f in features_map.values())
    for ch in channels:
        feats = features_map.get(str(ch))
        overlay_enabled = CameraFeature.OVERLAY in feats if (feats is not None and has_any_overlay) else True
        if not overlay_enabled:
            continue
        ch_name = channel_names.get(ch, f"Channel {ch + 1}")
        if coordinator.supports_channel_title(ch):
            entities.append(DVRTextChannelTitle(coordinator, entry, ch, ch_name))
        if coordinator.supports_custom_title(ch):
            entities.append(DVRTextCustomTitle(coordinator, entry, ch, ch_name))

    async_add_entities(entities)


class DVRTextChannelTitle(CameraChannelEntity, TextEntity):
    """Text entity for viewing and setting on-screen camera channel title."""

    entity_description = CHANNEL_TITLE_TEXT_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the channel title text entity."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_channel_title_text"
        self._optimistic_value: str | None = None

    @property
    def native_value(self) -> str:
        """Return current on-screen channel title."""
        if self._optimistic_value is not None:
            return self._optimistic_value
        return self.coordinator.get_channel_title(self._channel)

    async def async_set_value(self, value: str) -> None:
        """Set new on-screen channel title name."""
        self._optimistic_value = value
        self.async_write_ha_state()

        success = await self.coordinator.client.set_channel_title(
            channel=self._channel, title=value
        )
        if success:
            self._optimistic_value = None
            await self.coordinator.async_request_refresh()
        else:
            self._optimistic_value = None
            self.async_write_ha_state()
            _LOGGER.error(
                "Failed to set channel title text on channel %d", self._channel + 1
            )


class DVRTextCustomTitle(CameraChannelEntity, TextEntity):
    """Text entity for setting custom OSD overlay text on the camera channel."""

    entity_description = CUSTOM_TITLE_TEXT_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the custom title text entity."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_custom_title_text"
        self._optimistic_value: str | None = None

    @property
    def native_value(self) -> str:
        """Return the current custom title text from coordinator or optimistic cache."""
        if self._optimistic_value is not None:
            return self._optimistic_value
        return self.coordinator.get_custom_title_text(self._channel)

    async def async_set_value(self, value: str) -> None:
        """Set new custom title text on the camera stream."""
        self._optimistic_value = value
        self.async_write_ha_state()

        success = await self.coordinator.client.set_custom_title_text(
            channel=self._channel, text=value, index=0
        )
        if success:
            if self.coordinator.data:
                vw = self.coordinator.data.setdefault("video_widget", {})
                ch_data = vw.setdefault(self._channel, {})
                ch_data["custom_title_text"] = value
                self._optimistic_value = None
                self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        else:
            self._optimistic_value = None
            self.async_write_ha_state()
            _LOGGER.error(
                "Failed to set custom title text on channel %d", self._channel + 1
            )
