"""Time entities for camera day/night schedule and analog picture periods."""
from __future__ import annotations

import logging
from datetime import time as dt_time
from typing import TYPE_CHECKING, Any

from homeassistant.components.time import TimeEntity, TimeEntityDescription
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

if TYPE_CHECKING:
    from . import IntelbrasDVRConfigEntry
from .const import CONF_CHANNELS, CONF_FEATURES, CameraFeature
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity

_LOGGER = logging.getLogger(__name__)

DAY_START_TIME_DESCRIPTION = TimeEntityDescription(
    key="day_start_time",
    translation_key="day_start_time",
    icon="mdi:weather-sunset-up",
    entity_category=EntityCategory.CONFIG,
)

DAY_END_TIME_DESCRIPTION = TimeEntityDescription(
    key="day_end_time",
    translation_key="day_end_time",
    icon="mdi:weather-sunset-down",
    entity_category=EntityCategory.CONFIG,
)

PERIOD_1_START_DESCRIPTION = TimeEntityDescription(
    key="period_1_start_time",
    translation_key="period_1_start_time",
    icon="mdi:clock-start",
    entity_category=EntityCategory.CONFIG,
)

PERIOD_1_END_DESCRIPTION = TimeEntityDescription(
    key="period_1_end_time",
    translation_key="period_1_end_time",
    icon="mdi:clock-end",
    entity_category=EntityCategory.CONFIG,
)

PERIOD_2_START_DESCRIPTION = TimeEntityDescription(
    key="period_2_start_time",
    translation_key="period_2_start_time",
    icon="mdi:clock-start",
    entity_category=EntityCategory.CONFIG,
)

PERIOD_2_END_DESCRIPTION = TimeEntityDescription(
    key="period_2_end_time",
    translation_key="period_2_end_time",
    icon="mdi:clock-end",
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntelbrasDVRConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up time entities for camera schedules and analog periods."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }

    entities: list[TimeEntity] = []

    for ch in channels:
        feats = features_map.get(str(ch), [])
        name = channel_names.get(ch, f"Channel {ch + 1}")
        profiles_enabled = (
            CameraFeature.PROFILES in feats
            or (CameraFeature.PROFILES not in feats and CameraFeature.VIDEO_COLOR in feats)
        )
        if not profiles_enabled:
            continue

        # Intelbras IP cameras: Schedule Day Start and Day End
        if coordinator.is_intelbras_ip(ch):
            entities.append(CameraDayStartTimeEntity(coordinator, entry, ch, name))
            entities.append(CameraDayEndTimeEntity(coordinator, entry, ch, name))

        # Analog cameras: Period 1 and Period 2 (Start and End)
        elif coordinator.is_analog(ch):
            entities.append(
                CameraAnalogPeriodTimeEntity(
                    coordinator, entry, ch, name, period_idx=0, which="start"
                )
            )
            entities.append(
                CameraAnalogPeriodTimeEntity(
                    coordinator, entry, ch, name, period_idx=0, which="end"
                )
            )
            entities.append(
                CameraAnalogPeriodTimeEntity(
                    coordinator, entry, ch, name, period_idx=1, which="start"
                )
            )
            entities.append(
                CameraAnalogPeriodTimeEntity(
                    coordinator, entry, ch, name, period_idx=1, which="end"
                )
            )

    async_add_entities(entities)


class CameraDayStartTimeEntity(CameraChannelEntity, TimeEntity):
    """Time entity configuring the day start (sunrise) time for an Intelbras IP camera."""

    entity_description: TimeEntityDescription = DAY_START_TIME_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the Day Start time entity."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_day_start_time"

    @property
    def native_value(self) -> dt_time | None:
        """Return the current day start time."""
        return self.coordinator.get_ip_schedule_time(self._channel, "start")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return schedule metadata attributes."""
        return self.coordinator.get_profile_mode_info(self._channel)

    async def async_set_value(self, value: dt_time) -> None:
        """Update day start time on the camera."""
        ok = await self.coordinator.client.set_ip_schedule_times(
            self._channel, sunrise=value
        )
        if ok:
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error(
                "Failed to set day start time to %s on channel %d",
                value,
                self._channel + 1,
            )


class CameraDayEndTimeEntity(CameraChannelEntity, TimeEntity):
    """Time entity configuring the day end (sunset) time for an Intelbras IP camera."""

    entity_description: TimeEntityDescription = DAY_END_TIME_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the Day End time entity."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_day_end_time"

    @property
    def native_value(self) -> dt_time | None:
        """Return the current day end time."""
        return self.coordinator.get_ip_schedule_time(self._channel, "end")

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return schedule metadata attributes."""
        return self.coordinator.get_profile_mode_info(self._channel)

    async def async_set_value(self, value: dt_time) -> None:
        """Update day end time on the camera."""
        ok = await self.coordinator.client.set_ip_schedule_times(
            self._channel, sunset=value
        )
        if ok:
            await self.coordinator.async_request_refresh()
        else:
            _LOGGER.error(
                "Failed to set day end time to %s on channel %d",
                value,
                self._channel + 1,
            )


class CameraAnalogPeriodTimeEntity(CameraChannelEntity, TimeEntity):
    """Time entity configuring start or end time for Period 1 or 2 on an analog camera."""

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
        period_idx: int,
        which: str,
    ) -> None:
        """Initialise the analog period time entity."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._period_idx = period_idx
        self._which = which
        self._attr_unique_id = (
            f"{entry.entry_id}_ch{channel}_period_{period_idx + 1}_{which}_time"
        )
        if period_idx == 0:
            self.entity_description = (
                PERIOD_1_START_DESCRIPTION
                if which == "start"
                else PERIOD_1_END_DESCRIPTION
            )
        else:
            self.entity_description = (
                PERIOD_2_START_DESCRIPTION
                if which == "start"
                else PERIOD_2_END_DESCRIPTION
            )

    @property
    def native_value(self) -> dt_time | None:
        """Return the current start or end time of the period."""
        return self.coordinator.get_analog_period_time(
            self._channel, self._period_idx, self._which
        )

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return schedule metadata attributes."""
        return self.coordinator.get_analog_periods_info(self._channel)

    async def async_set_value(self, value: dt_time) -> None:
        """Update start or end time of the period on the DVR."""
        start_time = value if self._which == "start" else None
        end_time = value if self._which == "end" else None
        await self.coordinator.async_set_analog_period_time(
            self._channel,
            self._period_idx,
            start_time=start_time,
            end_time=end_time,
        )
