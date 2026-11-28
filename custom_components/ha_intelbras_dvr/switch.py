"""Switch entities: privacy mask per channel."""
from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from homeassistant.components.switch import SwitchEntity, SwitchEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

if TYPE_CHECKING:
    from . import IntelbrasDVRConfigEntry
from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
    DEFAULT_BRIGHTNESS,
    DEFAULT_SATURATION_ACUTANCE,
    DOMAIN,
    CameraFeature,
)
from .coordinator import IntelbrasCoordinator, IntelbrasEventCoordinator
from .entity import CameraChannelEntity

_LOGGER = logging.getLogger(__name__)

PRIVACY_MASK_DESCRIPTION = SwitchEntityDescription(
    key="privacy_mask",
    translation_key="privacy_mask",
    icon="mdi:eye-off",
)

CUSTOM_TITLE_OVERLAY_DESCRIPTION = SwitchEntityDescription(
    key="custom_title_overlay",
    translation_key="custom_title_overlay",
    icon="mdi:card-text-outline",
    entity_category=EntityCategory.CONFIG,
)

CHANNEL_TITLE_OVERLAY_DESCRIPTION = SwitchEntityDescription(
    key="channel_title_overlay",
    translation_key="channel_title_overlay",
    icon="mdi:format-title",
    entity_category=EntityCategory.CONFIG,
)

TIME_TITLE_OVERLAY_DESCRIPTION = SwitchEntityDescription(
    key="time_title_overlay",
    translation_key="time_title_overlay",
    icon="mdi:clock-outline",
    entity_category=EntityCategory.CONFIG,
)

PERIOD_1_ACTIVE_DESCRIPTION = SwitchEntityDescription(
    key="period_1_active",
    translation_key="period_1_active",
    icon="mdi:clock-check-outline",
    entity_category=EntityCategory.CONFIG,
)

PERIOD_2_ACTIVE_DESCRIPTION = SwitchEntityDescription(
    key="period_2_active",
    translation_key="period_2_active",
    icon="mdi:clock-check-outline",
    entity_category=EntityCategory.CONFIG,
)

AUTO_NOISE_REDUCTION_DESCRIPTION = SwitchEntityDescription(
    key="auto_noise_reduction",
    translation_key="auto_noise_reduction",
    icon="mdi:waveform",
    entity_category=EntityCategory.CONFIG,
)

MOTION_DETECTION_ENABLE_DESCRIPTION = SwitchEntityDescription(
    key="motion_detection_enable",
    translation_key="motion_detection_enable",
    icon="mdi:motion-sensor",
    entity_category=EntityCategory.CONFIG,
)



async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntelbrasDVRConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up per-camera switch entities."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    event_coordinator = entry.runtime_data.event_coordinator
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }

    entities: list[SwitchEntity] = []
    has_any_overlay = any(CameraFeature.OVERLAY in f for f in features_map.values())
    for ch in channels:
        name = channel_names.get(ch, f"Channel {ch + 1}")
        feats: list[str] = features_map.get(str(ch), [])
        if CameraFeature.MASK in feats:
            entities.append(
                PrivacyMaskSwitch(coordinator, entry, ch, name, event_coordinator)
            )
        if CameraFeature.MOTION in feats:
            entities.append(
                CameraMotionDetectionSwitch(coordinator, entry, ch, name)
            )
        overlay_enabled = CameraFeature.OVERLAY in feats if has_any_overlay else True
        if overlay_enabled:
            if coordinator.supports_custom_title(ch):
                entities.append(
                    CameraCustomTitleOverlaySwitch(coordinator, entry, ch, name)
                )
            if coordinator.supports_channel_title_overlay(ch):
                entities.append(
                    CameraChannelTitleOverlaySwitch(coordinator, entry, ch, name)
                )
            if coordinator.supports_time_title_overlay(ch):
                entities.append(
                    CameraTimeTitleOverlaySwitch(coordinator, entry, ch, name)
                )
        profiles_enabled = (
            CameraFeature.PROFILES in feats
            or (CameraFeature.PROFILES not in feats and CameraFeature.VIDEO_COLOR in feats)
        )
        if profiles_enabled:
            if coordinator.supports_analog_periods(ch):
                entities.append(CameraPeriod1ActiveSwitch(coordinator, entry, ch, name))
                entities.append(CameraPeriod2ActiveSwitch(coordinator, entry, ch, name))
            if coordinator.supports_auto_noise_reduction(ch):
                entities.append(CameraAutoNoiseReductionSwitch(coordinator, entry, ch, name))

    async_add_entities(entities)


class PrivacyMaskSwitch(CameraChannelEntity, SwitchEntity):
    """Switch that masks (blacks out) or restores a single camera channel."""

    entity_description: SwitchEntityDescription = PRIVACY_MASK_DESCRIPTION
    _attr_assumed_state = False

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
        event_coordinator: IntelbrasEventCoordinator | None = None,
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
        """Return True when the channel is masked (brightness = 0 and saturation = 0)."""
        if self._optimistic_until and time.monotonic() < self._optimistic_until:
            return self._optimistic_state is True
        color = self._video_color
        if not color:
            return False
        return (
            color.get("Brightness", DEFAULT_BRIGHTNESS) == 0
            and color.get("Saturation", DEFAULT_SATURATION_ACUTANCE) == 0
        )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Apply the privacy mask (black picture) to this channel."""
        self._optimistic_state = True
        self._optimistic_until = time.monotonic() + 4.0
        self.async_write_ha_state()

        color = self._video_color
        self.coordinator.client.save_camera_colors(self._channel, color)

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

        if self._event_coordinator:
            self._event_coordinator.suppress_video_blind(
                self._channel, duration=30.0
            )

        ok = await self.coordinator.client.mask_channels(
            [self._channel], mask=True
        )
        if ok:
            self._optimistic_until = time.monotonic() + 3.0
            await self.coordinator.async_refresh_video_color()
            self.async_write_ha_state()
        else:
            self._optimistic_until = 0.0
            self._optimistic_state = None
            self.async_write_ha_state()
            _LOGGER.error(
                "Failed to apply privacy mask on channel %d.", self._channel
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Remove the privacy mask and restore the normal picture."""
        self._optimistic_state = False
        self._optimistic_until = time.monotonic() + 4.0
        self.async_write_ha_state()

        saved = self.coordinator.client.get_saved_camera_colors(self._channel)

        if self.coordinator.data and "video_color" in self.coordinator.data:
            self.coordinator.data["video_color"][self._channel] = dict(saved)
            self.coordinator.async_update_listeners()

        if self._event_coordinator:
            self._event_coordinator.suppress_video_blind(
                self._channel, duration=30.0
            )

        ok = await self.coordinator.client.mask_channels(
            [self._channel], mask=False
        )
        if ok:
            self._optimistic_until = time.monotonic() + 3.0
            await self.coordinator.async_refresh_video_color()
            self.async_write_ha_state()
        else:
            self._optimistic_until = 0.0
            self._optimistic_state = None
            self.async_write_ha_state()
            _LOGGER.error(
                "Failed to remove privacy mask on channel %d.", self._channel
            )


class CameraCustomTitleOverlaySwitch(CameraChannelEntity, SwitchEntity):
    """Switch to toggle custom title overlay on the camera video."""

    entity_description = CUSTOM_TITLE_OVERLAY_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_custom_title_overlay"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool:
        """Return True if custom title overlay is enabled."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.is_custom_title_enabled(self._channel)

    def _handle_coordinator_update(self) -> None:
        """Reset optimistic state on poll."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable custom title overlay."""
        self._optimistic_state = True
        self.async_write_ha_state()
        ok = await self.coordinator.client.set_video_widget_overlay(
            self._channel, "custom_title", True, index=0
        )
        if ok:
            if self.coordinator.data:
                vw = self.coordinator.data.setdefault("video_widget", {})
                ch_data = vw.setdefault(self._channel, {})
                ch_data["custom_title_blend"] = True
                self._optimistic_state = None
                self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        else:
            self._optimistic_state = None
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable custom title overlay."""
        self._optimistic_state = False
        self.async_write_ha_state()
        ok = await self.coordinator.client.set_video_widget_overlay(
            self._channel, "custom_title", False, index=0
        )
        if ok:
            if self.coordinator.data:
                vw = self.coordinator.data.setdefault("video_widget", {})
                ch_data = vw.setdefault(self._channel, {})
                ch_data["custom_title_blend"] = False
                self._optimistic_state = None
                self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        else:
            self._optimistic_state = None
            self.async_write_ha_state()


class CameraChannelTitleOverlaySwitch(CameraChannelEntity, SwitchEntity):
    """Switch to toggle channel title overlay on the camera video."""

    entity_description = CHANNEL_TITLE_OVERLAY_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_channel_title_overlay"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool:
        """Return True if channel title overlay is enabled."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.is_channel_title_enabled(self._channel)

    def _handle_coordinator_update(self) -> None:
        """Reset optimistic state on poll."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable channel title overlay."""
        self._optimistic_state = True
        self.async_write_ha_state()
        ok = await self.coordinator.client.set_video_widget_overlay(
            self._channel, "channel_title", True
        )
        if ok:
            if self.coordinator.data:
                vw = self.coordinator.data.setdefault("video_widget", {})
                ch_data = vw.setdefault(self._channel, {})
                ch_data["channel_title_blend"] = True
                self._optimistic_state = None
                self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        else:
            self._optimistic_state = None
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable channel title overlay."""
        self._optimistic_state = False
        self.async_write_ha_state()
        ok = await self.coordinator.client.set_video_widget_overlay(
            self._channel, "channel_title", False
        )
        if ok:
            if self.coordinator.data:
                vw = self.coordinator.data.setdefault("video_widget", {})
                ch_data = vw.setdefault(self._channel, {})
                ch_data["channel_title_blend"] = False
                self._optimistic_state = None
                self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        else:
            self._optimistic_state = None
            self.async_write_ha_state()


class CameraTimeTitleOverlaySwitch(CameraChannelEntity, SwitchEntity):
    """Switch to toggle time title (clock) overlay on the camera video."""

    entity_description = TIME_TITLE_OVERLAY_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the switch."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_time_title_overlay"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool:
        """Return True if time title overlay is enabled."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.is_time_title_enabled(self._channel)

    def _handle_coordinator_update(self) -> None:
        """Reset optimistic state on poll."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable time title overlay."""
        self._optimistic_state = True
        self.async_write_ha_state()
        ok = await self.coordinator.client.set_video_widget_overlay(
            self._channel, "time_title", True
        )
        if ok:
            if self.coordinator.data:
                vw = self.coordinator.data.setdefault("video_widget", {})
                ch_data = vw.setdefault(self._channel, {})
                ch_data["time_title_blend"] = True
                self._optimistic_state = None
                self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        else:
            self._optimistic_state = None
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable time title overlay."""
        self._optimistic_state = False
        self.async_write_ha_state()
        ok = await self.coordinator.client.set_video_widget_overlay(
            self._channel, "time_title", False
        )
        if ok:
            if self.coordinator.data:
                vw = self.coordinator.data.setdefault("video_widget", {})
                ch_data = vw.setdefault(self._channel, {})
                ch_data["time_title_blend"] = False
                self._optimistic_state = None
                self.coordinator.async_set_updated_data(dict(self.coordinator.data))
        else:
            self._optimistic_state = None
            self.async_write_ha_state()


class CameraPeriod1ActiveSwitch(CameraChannelEntity, SwitchEntity):
    """Switch to enable or disable Period 1 for an analog camera."""

    entity_description: SwitchEntityDescription = PERIOD_1_ACTIVE_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise Period 1 Active switch."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_period_1_active"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool:
        """Return True if Period 1 is active."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.get_analog_period_active(self._channel, 0)

    def _handle_coordinator_update(self) -> None:
        """Reset optimistic state on poll."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on Period 1."""
        self._optimistic_state = True
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_analog_period_active(self._channel, 0, True)
        finally:
            self._optimistic_state = None
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off Period 1."""
        self._optimistic_state = False
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_analog_period_active(self._channel, 0, False)
        finally:
            self._optimistic_state = None
            self.async_write_ha_state()


class CameraPeriod2ActiveSwitch(CameraChannelEntity, SwitchEntity):
    """Switch to enable or disable Period 2 for an analog camera."""

    entity_description: SwitchEntityDescription = PERIOD_2_ACTIVE_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise Period 2 Active switch."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_period_2_active"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool:
        """Return True if Period 2 is active."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.get_analog_period_active(self._channel, 1)

    def _handle_coordinator_update(self) -> None:
        """Reset optimistic state on poll."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Turn on Period 2."""
        self._optimistic_state = True
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_analog_period_active(self._channel, 1, True)
        finally:
            self._optimistic_state = None
            self.async_write_ha_state()

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Turn off Period 2."""
        self._optimistic_state = False
        self.async_write_ha_state()
        try:
            await self.coordinator.async_set_analog_period_active(self._channel, 1, False)
        finally:
            self._optimistic_state = None
            self.async_write_ha_state()


class CameraAutoNoiseReductionSwitch(CameraChannelEntity, SwitchEntity):
    """Switch to toggle automatic noise reduction (GainAuto) on an analog camera."""

    entity_description = AUTO_NOISE_REDUCTION_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the auto noise reduction switch."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_auto_noise_reduction"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool:
        """Return True if auto noise reduction is enabled for effective profile."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        prof = self.coordinator.get_effective_profile_index(self._channel)
        return self.coordinator.client.get_gain_auto(self._channel, profile=prof)

    def _handle_coordinator_update(self) -> None:
        """Reset optimistic state on poll."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable auto noise reduction for effective profile."""
        self._optimistic_state = True
        self.async_write_ha_state()
        prof = self.coordinator.get_effective_profile_index(self._channel)
        ok = await self.coordinator.client.set_gain_auto(self._channel, True, profile=prof)
        if ok:
            await self.coordinator.async_refresh_video_color()
            self._optimistic_state = None
            self.async_write_ha_state()
        else:
            self._optimistic_state = False
            self.async_write_ha_state()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="auto_noise_reduction_failed",
            )

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable auto noise reduction for effective profile."""
        self._optimistic_state = False
        self.async_write_ha_state()
        prof = self.coordinator.get_effective_profile_index(self._channel)
        ok = await self.coordinator.client.set_gain_auto(self._channel, False, profile=prof)
        if ok:
            await self.coordinator.async_refresh_video_color()
            self._optimistic_state = None
            self.async_write_ha_state()
        else:
            self._optimistic_state = True
            self.async_write_ha_state()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="auto_noise_reduction_failed",
            )


class CameraMotionDetectionSwitch(CameraChannelEntity, SwitchEntity):
    """Switch to enable or disable DVR motion detection (MotionDetect[ch].Enable)."""

    entity_description: SwitchEntityDescription = MOTION_DETECTION_ENABLE_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the per-channel motion detection switch."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_motion_detection_enable"
        self._optimistic_state: bool | None = None

    @property
    def is_on(self) -> bool | None:
        """Return True when DVR motion detection is enabled on this channel."""
        if self._optimistic_state is not None:
            return self._optimistic_state
        return self.coordinator.is_motion_detection_enabled(self._channel)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return motion detection sensitivity, threshold, and event handler flags."""
        info = self.coordinator.get_motion_detect_info(self._channel)
        attrs: dict[str, Any] = {}
        for key in (
            "sensitivity",
            "threshold",
            "warm_light_enable",
            "beep_enable",
            "record_enable",
            "record_latch",
        ):
            if key in info:
                attrs[key] = info[key]
        return attrs

    def _handle_coordinator_update(self) -> None:
        """Reset optimistic state on coordinator refresh."""
        self._optimistic_state = None
        super()._handle_coordinator_update()

    async def _async_set_enabled(self, enabled: bool) -> None:
        """Apply motion detection state with optimistic UI feedback."""
        self._optimistic_state = enabled
        self.async_write_ha_state()
        ok = await self.coordinator.async_set_motion_detection_enabled(
            self._channel, enabled
        )
        self._optimistic_state = None
        self.async_write_ha_state()
        if not ok:
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="set_motion_detection_failed",
                translation_placeholders={"channel": str(self._channel)},
            )

    async def async_turn_on(self, **kwargs: Any) -> None:
        """Enable motion detection on this channel."""
        await self._async_set_enabled(True)

    async def async_turn_off(self, **kwargs: Any) -> None:
        """Disable motion detection on this channel."""
        await self._async_set_enabled(False)



