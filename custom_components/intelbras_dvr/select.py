"""Select entities for physical monitor output split mode, recording mode, and PTZ presets."""
from __future__ import annotations

import asyncio
import logging
import math
from typing import TYPE_CHECKING

from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.core import HomeAssistant
from homeassistant.helpers.entity_platform import AddEntitiesCallback

if TYPE_CHECKING:
    from . import IntelbrasDVRConfigEntry
from .const import (
    CONF_CHANNELS,
    CONF_FEATURES,
    CONF_PTZ_CHANNELS,
    RECORD_MODE_MANUAL,
    RECORD_MODE_OFF,
    RECORD_MODE_SCHEDULE,
    CameraFeature,
)
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity, DVREntity
from .ptz import PTZPresetSelect

_LOGGER = logging.getLogger(__name__)

MONITOR_SELECT_DESCRIPTION = SelectEntityDescription(
    key="monitor_select",
    translation_key="monitor_select",
    icon="mdi:monitor-multiple",
)

RECORD_MODE_OPTIONS: list[str] = ["schedule", "manual", "off"]

RECORD_MODE_DESCRIPTION = SelectEntityDescription(
    key="record_mode",
    translation_key="record_mode",
    icon="mdi:record-rec",
    options=RECORD_MODE_OPTIONS,
)

RECORD_MODE_TO_DVR: dict[str, int] = {
    "schedule": RECORD_MODE_SCHEDULE,
    "manual": RECORD_MODE_MANUAL,
    "off": RECORD_MODE_OFF,
    # Backward compatibility with legacy option strings
    "Agendado": RECORD_MODE_SCHEDULE,
    "Manual / Contínuo": RECORD_MODE_MANUAL,
    "Desativado": RECORD_MODE_OFF,
    "agendado": RECORD_MODE_SCHEDULE,
    "desativado": RECORD_MODE_OFF,
}

DVR_TO_RECORD_MODE: dict[int, str] = {
    RECORD_MODE_SCHEDULE: "schedule",
    RECORD_MODE_MANUAL: "manual",
    RECORD_MODE_OFF: "off",
}


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntelbrasDVRConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up monitor output select, recording mode selects, and PTZ preset selectors."""
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

    entity_description: SelectEntityDescription = MONITOR_SELECT_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
        channels: list[int],
        channel_names: dict[int, str],
    ) -> None:
        """Initialise the monitor split select entity."""
        super().__init__(coordinator, entry)
        self._channels = sorted(channels)
        self._channel_names = channel_names
        self._attr_unique_id = f"{entry.entry_id}_monitor_select"
        self._optimistic_option: str | None = None
        self._label_to_action: dict[str, tuple[str, int]] = {}
        self._action_to_label: dict[tuple[str, int], str] = {}
        self._rebuild_options()

    def _get_templates(self) -> tuple[str, str, str]:
        """Return localized templates (single_view, range_view, full_view)."""
        runtime = getattr(self._entry, "runtime_data", None)
        trans: dict[str, str] = getattr(runtime, "translations", {}) if runtime else {}
        prefix = "component.intelbras_dvr.selector.monitor_view_format.options."
        single_tpl = trans.get(f"{prefix}single_view")
        range_tpl = trans.get(f"{prefix}range_view")
        full_tpl = trans.get(f"{prefix}full_view")
        if single_tpl and range_tpl and full_tpl:
            return single_tpl, range_tpl, full_tpl

        lang = ""
        if self.coordinator and self.coordinator.hass:
            lang = (self.coordinator.hass.config.language or "").lower()
        if lang.startswith("pt"):
            return (
                "Visualização de 1: {name}",
                "Visualização de {count}: {start}-{end}",
                "Visualização de {count}",
            )
        return (
            "View 1: {name}",
            "View {count}: {start}-{end}",
            "View {count}",
        )

    def _rebuild_options(self) -> None:
        """Dynamically build monitor split options from DVR capabilities and active channels."""
        single_tpl, range_tpl, full_tpl = self._get_templates()
        self._label_to_action.clear()
        self._action_to_label.clear()
        option_labels: list[str] = []

        all_ch = sorted(self._channel_names.keys()) or self._channels
        for idx, ch in enumerate(all_ch):
            cam_name = self._channel_names.get(ch, f"Channel {ch + 1}")
            label = single_tpl.format(name=cam_name)
            action = ("split1", idx)
            self._label_to_action[label] = action
            self._action_to_label[action] = label
            option_labels.append(label)
            # Register alternate language aliases for automation compatibility
            self._label_to_action[f"Visualização de 1: {cam_name}"] = action
            self._label_to_action[f"View 1: {cam_name}"] = action
            self._label_to_action[f"split1:{idx}"] = action

        data = self.coordinator.data or {}
        monitor_caps: list[int] = list(data.get("monitor_caps") or [1, 4, 8, 9, 16])
        max_ch_idx = max(
            int(data.get("total_channels") or 0),
            (max(all_ch) + 1) if all_ch else 0,
            4,
        )
        # Round effective channel span to standard DVR block boundary (4, 8, 12, 16, 24, 32)
        effective_total = max(4, math.ceil(max_ch_idx / 4) * 4)

        for split_num in sorted(set(monitor_caps)):
            if split_num <= 1:
                continue
            mode = f"split{split_num}"
            if split_num >= effective_total:
                label = full_tpl.format(count=split_num)
                action = (mode, 0)
                self._label_to_action[label] = action
                self._action_to_label[action] = label
                option_labels.append(label)
                self._label_to_action[f"Visualização de {split_num}"] = action
                self._label_to_action[f"View {split_num}"] = action
                self._label_to_action[mode] = action
                self._label_to_action[f"{mode}:0"] = action
            else:
                num_groups = math.ceil(effective_total / split_num)
                for group in range(num_groups):
                    if (group + 1) * split_num <= effective_total:
                        start = group * split_num + 1
                        end = (group + 1) * split_num
                    else:
                        start = max(1, effective_total - split_num + 1)
                        end = effective_total
                    label = range_tpl.format(count=split_num, start=start, end=end)
                    action = (mode, group)
                    self._label_to_action[label] = action
                    self._action_to_label[action] = label
                    option_labels.append(label)
                    self._label_to_action[
                        f"Visualização de {split_num}: {start}-{end}"
                    ] = action
                    self._label_to_action[f"View {split_num}: {start}-{end}"] = action
                    self._label_to_action[f"{mode}:{group}"] = action

        self._attr_options = option_labels

    @property
    def current_option(self) -> str | None:
        """Return the label that corresponds to the current DVR monitor mode."""
        if self._optimistic_option is not None:
            return self._optimistic_option

        mode_data: dict = (self.coordinator.data or {}).get("monitor_mode", {})
        if not mode_data:
            return None
        mode: str = str(mode_data.get("mode", "")).strip()
        if not mode:
            return None
        try:
            group: int = int(mode_data.get("group", 0))
        except (ValueError, TypeError):
            group = 0

        return self._action_to_label.get((mode, group))

    def _handle_coordinator_update(self) -> None:
        """Handle updated data from the coordinator."""
        self._optimistic_option = None
        self._rebuild_options()
        super()._handle_coordinator_update()

    async def async_select_option(self, option: str) -> None:
        """Apply the selected split mode to the physical monitor."""
        action = self._label_to_action.get(option)
        if action is None:
            _LOGGER.warning("Unknown monitor option selected: %s", option)
            return
        mode, group = action
        canonical_label = self._action_to_label.get(action, option)

        self._optimistic_option = canonical_label
        self.async_write_ha_state()

        ok = await self.coordinator.client.set_monitor_split(mode=mode, group=group)
        if ok:
            if self.coordinator.data is not None:
                self.coordinator.data["monitor_mode"] = {"mode": mode, "group": group}
            await asyncio.sleep(1.0)
            await self.coordinator.async_request_refresh()
        else:
            self._optimistic_option = None
            self.async_write_ha_state()
            _LOGGER.error("Failed to set monitor to '%s'.", option)


class CameraRecordModeSelect(CameraChannelEntity, SelectEntity):
    """Select entity for changing the recording mode of a camera channel."""

    entity_description: SelectEntityDescription = RECORD_MODE_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: IntelbrasDVRConfigEntry,
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
        mode_val = modes.get(self._channel, RECORD_MODE_SCHEDULE)
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

        canonical_opt = DVR_TO_RECORD_MODE.get(mode_val, "schedule")
        self._optimistic_option = canonical_opt
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
            _LOGGER.error(
                "Failed to set record mode to '%s' on channel %d.",
                option,
                self._channel,
            )
