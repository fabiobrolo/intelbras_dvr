"""Select entity for choosing the physical monitor output split mode."""
from __future__ import annotations

import asyncio
import logging

from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry
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
from .ptz import PTZPresetSelect

_LOGGER = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Option definitions
# Each entry: (label, cgi_mode, group_index)
#
# The DVR API uses:
#   split1  -> single channel view (group = channel 0-based index in selected list)
#   split4  -> 4-channel quad  (group selects the block of 4)
#   split8  -> 8-channel view  (group selects the block of 8)
#   split9  -> 9-channel view  (group selects the block of 9)
#   split16 -> 16-channel view (group = 0)
# ---------------------------------------------------------------------------

# Fixed split-mode options (label, mode, group)
_FIXED_OPTIONS: list[tuple[str, str, int]] = [
    ("Visualização de 4: 1-4",  "split4", 0),
    ("Visualização de 4: 5-8",  "split4", 1),
    ("Visualização de 4: 9-12", "split4", 2),
    ("Visualização de 8: 1-8",  "split8", 0),
    ("Visualização de 8: 5-12", "split8", 1),
    ("Visualização de 9: 1-9",  "split9", 0),
    ("Visualização de 9: 4-12", "split9", 1),
    ("Visualização de 16",      "split16", 0),
]


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up monitor output select and PTZ preset selectors."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    channels: list[int] = entry.data.get(CONF_CHANNELS, [])
    ptz_channels: list[int] = entry.data.get(CONF_PTZ_CHANNELS, [])
    features_map: dict[str, list[str]] = entry.data.get(CONF_FEATURES, {})
    channel_names: dict[int, str] = {
        int(k): v for k, v in entry.data.get("channel_names", {}).items()
    }

    entities: list[SelectEntity] = [
        MonitorSplitSelect(coordinator, entry, channels, channel_names)
    ]

    for ch in channels:
        feats = features_map.get(str(ch), [])
        name = channel_names.get(ch, f"Channel {ch + 1}")
        if CameraFeature.RECORDING in feats:
            entities.append(CameraRecordModeSelect(coordinator, entry, ch, name))
        if ch in ptz_channels and CameraFeature.PTZ in feats:
            entities.append(PTZPresetSelect(coordinator, entry, ch, name))

    async_add_entities(entities)


class MonitorSplitSelect(DVREntity, SelectEntity):
    """Select entity controlling the split/view mode of the DVR's physical monitor output."""

    _attr_translation_key = "monitor_select"
    _attr_icon = "mdi:monitor-multiple"

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channels: list[int],
        channel_names: dict[int, str],
    ) -> None:
        """Initialise the select entity."""
        super().__init__(coordinator, entry)
        self._channels = sorted(channels)
        self._channel_names = channel_names
        self._attr_unique_id = f"{entry.entry_id}_monitor_select"
        self._optimistic_option: str | None = None

        # Build option list:
        #   1. "Visualização de 1: <camera name>" for each configured channel
        #   2. Fixed split-mode options
        self._label_to_action: dict[str, tuple[str, int]] = {}
        option_labels: list[str] = []

        all_ch = sorted(self._channel_names.keys())
        for idx, ch in enumerate(all_ch):
            cam_name = self._channel_names.get(ch, f"Channel {ch + 1}")
            label = f"Visualização de 1: {cam_name}"
            self._label_to_action[label] = ("split1", idx)
            option_labels.append(label)

        for label, mode, group in _FIXED_OPTIONS:
            self._label_to_action[label] = (mode, group)
            option_labels.append(label)

        self._attr_options = option_labels

    @property
    def current_option(self) -> str | None:
        """Return the label that corresponds to the current DVR monitor mode."""
        if self._optimistic_option is not None:
            return self._optimistic_option

        mode_data: dict = (self.coordinator.data or {}).get("monitor_mode", {})
        mode: str = str(mode_data.get("mode", "split8")).strip()
        try:
            group: int = int(mode_data.get("group", 0))
        except (ValueError, TypeError):
            group = 0

        if mode == "split1":
            # Map group index back to channel label
            all_ch = sorted(self._channel_names.keys())
            if group < len(all_ch):
                ch = all_ch[group]
                cam_name = self._channel_names.get(ch, f"Channel {ch + 1}")
                return f"Visualização de 1: {cam_name}"
            return None

        # Find a matching fixed option
        for label, m, g in _FIXED_OPTIONS:
            if m == mode and g == group:
                return label

        return None

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._optimistic_option = None
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        """Apply the selected split mode to the physical monitor."""
        action = self._label_to_action.get(option)
        if action is None:
            _LOGGER.warning("Unknown monitor option selected: %s", option)
            return
        mode, group = action

        self._optimistic_option = option
        self.async_write_ha_state()

        ok = await self.coordinator.client.set_monitor_split(mode=mode, group=group)
        if ok:
            if self.coordinator.data and "monitor_mode" in self.coordinator.data:
                self.coordinator.data["monitor_mode"] = {"mode": mode, "group": group}
            # Wait 1.0s for DVR hardware HDMI/VGA output to transition before refreshing
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
        else:
            self._optimistic_option = None
            self.async_write_ha_state()
            _LOGGER.error("Failed to set monitor to '%s'.", option)


RECORD_MODE_OPTIONS: list[str] = ["schedule", "manual", "off"]
RECORD_MODE_TO_DVR: dict[str, int] = {
    "schedule": 0,
    "manual": 1,
    "off": 2,
    # Backward compatibility with legacy Portuguese option strings
    "Agendado": 0,
    "Manual / Contínuo": 1,
    "Desativado": 2,
    "agendado": 0,
    "manual": 1,
    "desativado": 2,
}
DVR_TO_RECORD_MODE: dict[int, str] = {
    0: "schedule",
    1: "manual",
    2: "off",
}


class CameraRecordModeSelect(CameraChannelEntity, SelectEntity):
    """Select entity for changing the recording mode of a camera channel."""

    _attr_translation_key = "record_mode"
    _attr_icon = "mdi:record-rec"
    _attr_options = RECORD_MODE_OPTIONS

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the recording mode select entity."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_record_mode"
        self._optimistic_option: str | None = None

    @property
    def current_option(self) -> str | None:
        """Return the current recording mode option."""
        if self._optimistic_option is not None:
            return self._optimistic_option
        modes: dict[int, int] = (self.coordinator.data or {}).get("record_modes", {})
        mode_val = modes.get(self._channel, 0)
        return DVR_TO_RECORD_MODE.get(mode_val, "schedule")

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._optimistic_option = None
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        """Change the camera channel recording mode on the DVR."""
        mode_val = RECORD_MODE_TO_DVR.get(option)
        if mode_val is None:
            _LOGGER.warning("Unknown record mode option: %s", option)
            return

        self._optimistic_option = option
        self.async_write_ha_state()

        ok = await self.coordinator.client.set_record_mode(self._channel, mode_val)
        if ok:
            if self.coordinator.data and "record_modes" in self.coordinator.data:
                self.coordinator.data["record_modes"][self._channel] = mode_val
            await self.coordinator.async_refresh_record_modes()
            self._optimistic_option = None
            self.async_write_ha_state()
        else:
            self._optimistic_option = None
            self.async_write_ha_state()
            _LOGGER.error("Failed to set record mode to '%s' on channel %d.", option, self._channel)
