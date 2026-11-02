"""Binary sensor entities: motion, video loss, and video blind per channel.

These entities use the dedicated ``IntelbrasEventCoordinator`` which polls
every 5 seconds so that alarm state changes are reflected quickly in
Home Assistant automations.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from homeassistant.components.binary_sensor import (
    BinarySensorDeviceClass,
    BinarySensorEntity,
    BinarySensorEntityDescription,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.helpers.device_registry import DeviceInfo
from homeassistant.helpers.entity_platform import AddEntitiesCallback
from homeassistant.helpers.update_coordinator import CoordinatorEntity

from .const import CONF_CHANNELS, CONF_FEATURES, DOMAIN, CameraFeature
from .coordinator import IntelbrasEventCoordinator
from .entity import camera_device_info, dvr_device_info


@dataclass(frozen=True, kw_only=True)
class DVRBinarySensorDescription(BinarySensorEntityDescription):
    """Extend BinarySensorEntityDescription with the event dict key."""

    event_key: str  # key inside the per-channel event dict
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


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
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
    """
    Binary sensor backed by the fast event coordinator (5 s poll).

    State is derived from the per-channel event dict stored in
    ``IntelbrasEventCoordinator.data``.
    """

    entity_description: DVRBinarySensorDescription
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IntelbrasEventCoordinator,
        entry: ConfigEntry,
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
        self._attr_unique_id = (
            f"{entry.entry_id}_ch{channel}_{description.key}"
        )
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

    _attr_translation_key = "event_stream"
    _attr_device_class = BinarySensorDeviceClass.CONNECTIVITY
    _attr_entity_category = EntityCategory.DIAGNOSTIC
    _attr_entity_registry_visible_default = False
    _attr_has_entity_name = True

    def __init__(
        self,
        coordinator: IntelbrasEventCoordinator,
        entry: ConfigEntry,
    ) -> None:
        super().__init__(coordinator)
        self._entry = entry
        self._attr_unique_id = f"{entry.entry_id}_event_stream"
        if hasattr(coordinator, "main_coordinator") and coordinator.main_coordinator:
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
        return getattr(self.coordinator, "stream_connected", False)

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return stream diagnostic attributes."""
        return {
            "reconnect_count": getattr(self.coordinator, "reconnect_count", 0),
            "last_heartbeat": getattr(self.coordinator, "last_heartbeat", None),
        }
