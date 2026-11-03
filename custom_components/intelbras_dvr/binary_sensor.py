"""Binary sensor entities: motion, video loss, video blind, and event stream status.

These entities use the dedicated ``IntelbrasEventCoordinator`` which maintains a
persistent HTTP push stream (``eventManager.cgi?action=attach``) so that alarm
state changes are reflected in real time in Home Assistant automations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

if TYPE_CHECKING:
    from . import IntelbrasDVRConfigEntry
from .const import CONF_CHANNELS, CONF_FEATURES, DOMAIN, CameraFeature
from .coordinator import IntelbrasEventCoordinator
from .entity import camera_device_info, dvr_device_info


@dataclass(frozen=True, kw_only=True)
class DVRBinarySensorDescription(BinarySensorEntityDescription):
    """Extend BinarySensorEntityDescription with the event dict key."""

    event_key: str
    required_feature: CameraFeature


BINARY_SENSOR_DESCRIPTIONS: tuple[DVRBinarySensorDescription, ...] = (
    DVRBinarySensorDescription(
        key="motion",
        translation_key="motion",
        device_class=BinarySensorDeviceClass.MOTION,
        event_key="motion",
        required_feature=CameraFeature.MOTION,
    ),
    DVRBinarySensorDescription(
        key="video_loss",
        translation_key="video_loss",
        device_class=BinarySensorDeviceClass.PROBLEM,
        event_key="video_loss",
        required_feature=CameraFeature.VIDEO_LOSS,
    ),
    DVRBinarySensorDescription(
        key="video_blind",
        translation_key="video_blind",
        device_class=BinarySensorDeviceClass.TAMPER,
        event_key="video_blind",
        required_feature=CameraFeature.VIDEO_BLIND,
    ),
)

EVENT_STREAM_DESCRIPTION = BinarySensorEntityDescription(
    key="event_stream",
    translation_key="event_stream",
    device_class=BinarySensorDeviceClass.CONNECTIVITY,
    entity_category=EntityCategory.DIAGNOSTIC,
    entity_registry_visible_default=False,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntelbrasDVRConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up binary sensor entities for all selected channels."""
    event_coordinator: IntelbrasEventCoordinator = entry.runtime_data.event_coordinator
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }

    entities: list[BinarySensorEntity] = [
        DVREventStreamBinarySensor(event_coordinator, entry),
    ]
    for ch in channels:
        feats: list[str] = features_map.get(str(ch), [])
        name = channel_names.get(ch, f"Channel {ch + 1}")
        for desc in BINARY_SENSOR_DESCRIPTIONS:
            if desc.required_feature in feats:
                entities.append(
                    DVRBinarySensor(event_coordinator, entry, ch, name, desc)
                )

    async_add_entities(entities)


class DVRBinarySensor(
    CoordinatorEntity[IntelbrasEventCoordinator],
    BinarySensorEntity,
):
    """Binary sensor backed by the real-time event stream coordinator."""

    entity_description: DVRBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IntelbrasEventCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channel: int,
        channel_name: str,
        description: DVRBinarySensorDescription,
    ) -> None:
        """Initialise the binary sensor."""
        super().__init__(coordinator)
        self.entity_description = description
        self._channel = channel
        self._channel_name = channel_name
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_{description.key}"
        self._attr_device_info = camera_device_info(
            entry=entry,
            coordinator=coordinator,
            channel=channel,
            channel_name=channel_name,
        )

    @property
    def is_on(self) -> bool:
        """Return True when the event is currently active."""
        ch_data: dict[str, bool] = (
            (self.coordinator.data or {}).get(self._channel) or {}
        )
        return ch_data.get(self.entity_description.event_key, False)

    @property
    def available(self) -> bool:
        """Return True when the coordinator has successfully fetched data."""
        return self.coordinator.last_update_success


class DVREventStreamBinarySensor(
    CoordinatorEntity[IntelbrasEventCoordinator],
    BinarySensorEntity,
):
    """Binary sensor reporting whether the real-time event stream is connected."""

    entity_description: BinarySensorEntityDescription = EVENT_STREAM_DESCRIPTION
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IntelbrasEventCoordinator,
        entry: IntelbrasDVRConfigEntry,
    ) -> None:
        """Initialise the event stream connectivity sensor."""
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_event_stream"
        if coordinator.main_coordinator:
            self._attr_device_info = dvr_device_info(entry, coordinator.main_coordinator)
        else:
            self._attr_device_info = DeviceInfo(
                identifiers={(DOMAIN, entry.entry_id)},
                name=entry.title,
                manufacturer="Intelbras",
            )

    @property
    def is_on(self) -> bool:
        """Return True if the event stream is currently connected."""
        return bool(self.coordinator.stream_connected)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return stream diagnostic attributes."""
        return {
            "reconnect_count": self.coordinator.reconnect_count,
            "last_heartbeat": self.coordinator.last_heartbeat,
        }
