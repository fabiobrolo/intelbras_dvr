"""Button entities: DVR reboot, full refresh, per-camera refresh, and image reset."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
    CONF_PTZ_CHANNELS,
    DOMAIN,
    CameraFeature,
)
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity, DVREntity
from .number import DEFAULT_VIDEO_COLOR
from .ptz import PTZ_BUTTON_DESCRIPTIONS, PTZDirectionButton

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up DVR-level and per-camera button entities."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    ptz_channels: list[int] = entry.data.get(CONF_PTZ_CHANNELS, [])
    features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }

    entities: list = [
        DVRRebootButton(coordinator, entry),
        DVRRefreshButton(coordinator, entry),
    ]

    for ch in channels:
        name = channel_names.get(ch, f"Channel {ch + 1}")
        feats: list[str] = features_map.get(str(ch), [])
        entities.append(CameraRefreshButton(coordinator, entry, ch, name))
        if CameraFeature.VIDEO_COLOR in feats:
            entities.append(CameraResetDefaultsButton(coordinator, entry, ch, name))

    for ch in ptz_channels:
        if ch in channels:
            feats = features_map.get(str(ch), [])
            if CameraFeature.PTZ in feats:
                name = channel_names.get(ch, f"Channel {ch + 1}")
                for desc in PTZ_BUTTON_DESCRIPTIONS:
                    entities.append(
                        PTZDirectionButton(coordinator, entry, ch, name, desc)
                    )

    async_add_entities(entities)


class DVRRebootButton(DVREntity, ButtonEntity):
    """Button that sends a reboot command to the DVR."""

    _attr_translation_key = "dvr_reboot"
    _attr_icon = "mdi:restart"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(self, coordinator: IntelbrasCoordinator, entry: ConfigEntry) -> None:
        """Initialise the reboot button."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_reboot"

    async def async_press(self) -> None:
        """Send reboot command to the DVR."""
        ok = await self.coordinator.client.reboot()
        if not ok:
            _LOGGER.error("DVR did not acknowledge the reboot command.")
        else:
            _LOGGER.info("DVR reboot initiated.")


class DVRRefreshButton(DVREntity, ButtonEntity):
    """Button that forces an immediate refresh of all DVR data."""

    _attr_translation_key = "dvr_refresh"
    _attr_icon = "mdi:refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(self, coordinator: IntelbrasCoordinator, entry: ConfigEntry) -> None:
        """Initialise the refresh button."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_refresh_all"

    async def async_press(self) -> None:
        """Request an immediate coordinator refresh."""
        await self.coordinator.async_request_refresh()



class CameraRefreshButton(CameraChannelEntity, ButtonEntity):
    """Button that requests a data refresh reflecting the current DVR state for one camera."""

    _attr_translation_key = "camera_refresh"
    _attr_icon = "mdi:camera-refresh"
    _attr_entity_category = EntityCategory.DIAGNOSTIC

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the per-camera refresh button."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_refresh"

    async def async_press(self) -> None:
        """Pull the latest state from the DVR into Home Assistant."""
        await self.coordinator.async_request_refresh()


class CameraResetDefaultsButton(CameraChannelEntity, ButtonEntity):
    """
    Button that resets the camera's picture settings to factory defaults.

    Defaults: Brightness=50, Contrast=50, Hue=50, Saturation=80, Acutance=1
    """

    _attr_translation_key = "camera_reset_defaults"
    _attr_icon = "mdi:image-filter-center-focus"
    _attr_entity_category = EntityCategory.CONFIG

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the reset-defaults button."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_reset_defaults"

    @property
    def available(self) -> bool:
        """Return True if coordinator is available and privacy mask is not active."""
        if not super().available:
            return False
        colors: dict = (self.coordinator.data or {}).get("video_color", {})
        color = colors.get(self._channel, {})
        if not color:
            return True
        return not (color.get("Brightness") == 0 and color.get("Saturation") == 0)

    async def async_press(self) -> None:
        """Apply factory-default picture settings to this camera channel."""
        if not self.available:
            _LOGGER.warning(
                "Cannot reset picture settings on channel %d while privacy mask is active.",
                self._channel,
            )
            return

        supports_acutance = self.coordinator.client.channel_supports_acutance(self._channel)
        defaults = {
            "Brightness": DEFAULT_VIDEO_COLOR["Brightness"],
            "Contrast": DEFAULT_VIDEO_COLOR["Contrast"],
            "Hue": DEFAULT_VIDEO_COLOR["Hue"],
            "Saturation": DEFAULT_VIDEO_COLOR["Saturation"] if supports_acutance else 50,
        }
        if supports_acutance:
            defaults["Acutance"] = DEFAULT_VIDEO_COLOR["Acutance"]

        if self.coordinator.data and "video_color" in self.coordinator.data:
            self.coordinator.data["video_color"][self._channel] = dict(defaults)
            self.coordinator.async_update_listeners()

        ok = await self.coordinator.client.set_video_color(
            channel=self._channel,
            brightness=defaults["Brightness"],
            contrast=defaults["Contrast"],
            hue=defaults["Hue"],
            saturation=defaults["Saturation"],
            acutance=defaults.get("Acutance"),
            profile=None,
        )
        if ok:
            await self.coordinator.async_refresh_video_color()
        else:
            await self.coordinator.async_refresh_video_color()
            _LOGGER.error(
                "Failed to reset picture defaults on channel %d.", self._channel
            )

