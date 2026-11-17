"""PTZ helper entities: direction buttons and preset selector per PTZ-capable channel."""
from __future__ import annotations

import asyncio
from dataclasses import dataclass
import logging
from typing import Any

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.components.select import SelectEntity, SelectEntityDescription
from homeassistant.config_entries import ConfigEntry
from homeassistant.helpers.restore_state import RestoreEntity

from .const import PTZ_MOVE_DURATION
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity

_LOGGER = logging.getLogger(__name__)


@dataclass(frozen=True, kw_only=True)
class PTZButtonDescription(ButtonEntityDescription):
    """Extend ButtonEntityDescription with the PTZ direction code."""

    ptz_code: str


PTZ_BUTTON_DESCRIPTIONS: tuple[PTZButtonDescription, ...] = (
    PTZButtonDescription(
        key="ptz_up",
        translation_key="ptz_up",
        icon="mdi:arrow-up-bold",
        ptz_code="Up",
    ),
    PTZButtonDescription(
        key="ptz_down",
        translation_key="ptz_down",
        icon="mdi:arrow-down-bold",
        ptz_code="Down",
    ),
    PTZButtonDescription(
        key="ptz_left",
        translation_key="ptz_left",
        icon="mdi:arrow-left-bold",
        ptz_code="Right",
    ),
    PTZButtonDescription(
        key="ptz_right",
        translation_key="ptz_right",
        icon="mdi:arrow-right-bold",
        ptz_code="Left",
    ),
    PTZButtonDescription(
        key="ptz_zoom_in",
        translation_key="ptz_zoom_in",
        icon="mdi:magnify-plus",
        ptz_code="ZoomTele",
    ),
    PTZButtonDescription(
        key="ptz_zoom_out",
        translation_key="ptz_zoom_out",
        icon="mdi:magnify-minus",
        ptz_code="ZoomWide",
    ),
)

PTZ_PRESET_DESCRIPTION = SelectEntityDescription(
    key="ptz_preset",
    translation_key="ptz_preset",
    icon="mdi:map-marker",
)


class PTZDirectionButton(CameraChannelEntity, ButtonEntity):
    """Momentary PTZ movement button.

    Pressing the button starts the PTZ movement and automatically stops it
    after PTZ_MOVE_DURATION seconds. Rapid repeated presses cancel the
    previous stop timer so the movement is not interrupted prematurely.
    """

    entity_description: PTZButtonDescription

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
        description: PTZButtonDescription,
    ) -> None:
        """Initialise the PTZ direction button."""
        super().__init__(coordinator, entry, channel, channel_name)
        self.entity_description = description
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_{description.key}"
        self._stop_task: asyncio.Task[None] | None = None

    async def async_press(self) -> None:
        """Start moving, then schedule an automatic stop after PTZ_MOVE_DURATION."""
        code = self.entity_description.ptz_code
        if self._stop_task and not self._stop_task.done():
            self._stop_task.cancel()

        await self.coordinator.client.ptz_move(self._channel, code)

        async def _delayed_stop() -> None:
            try:
                await asyncio.sleep(PTZ_MOVE_DURATION)
                await self.coordinator.client.ptz_stop(self._channel, code)
            except asyncio.CancelledError:
                return

        if self.hass:
            self._stop_task = self.hass.async_create_background_task(
                _delayed_stop(),
                name=f"intelbras_dvr_ptz_btn_ch{self._channel}_{code}",
            )
        else:
            self._stop_task = asyncio.create_task(_delayed_stop())


class PTZPresetSelect(CameraChannelEntity, SelectEntity, RestoreEntity):
    """Select entity that lists saved PTZ presets for a channel.

    Changing the selection instantly moves the camera to the chosen preset.
    """

    entity_description: SelectEntityDescription = PTZ_PRESET_DESCRIPTION

    def __init__(
        self,
        coordinator: IntelbrasCoordinator,
        entry: ConfigEntry,
        channel: int,
        channel_name: str,
    ) -> None:
        """Initialise the preset selector."""
        super().__init__(coordinator, entry, channel, channel_name)
        self._attr_unique_id = f"{entry.entry_id}_ch{channel}_ptz_preset"
        self._current_option: str | None = None

    async def async_added_to_hass(self) -> None:
        """Restore previous selected preset on startup."""
        await super().async_added_to_hass()
        last_state = await self.async_get_last_state()
        if last_state and last_state.state in self.options:
            self._current_option = last_state.state

    @property
    def _presets(self) -> dict[int, str]:
        """Return the current preset map from coordinator data."""
        all_presets: dict[int, dict[int, str]] = (
            self.coordinator.data or {}
        ).get("ptz_presets", {})
        return all_presets.get(self._channel, {})

    @property
    def options(self) -> list[str]:
        """Return preset names as the list of selectable options."""
        presets = self._presets
        if not presets:
            return []
        return [f"{idx}: {name}" for idx, name in sorted(presets.items())]

    @property
    def current_option(self) -> str | None:
        """Return the currently selected preset option."""
        # 1. If camera hardware reports an active preset_id (e.g. Dahua native PTZ)
        ptz_status = self.coordinator.get_ptz_status(self._channel)
        preset_id = ptz_status.get("preset_id")
        if preset_id is not None and preset_id > 0:
            for opt in self.options:
                if opt.startswith(f"{preset_id}:"):
                    return opt

        # 2. Return last commanded preset (or restored from previous session)
        if self._current_option in self.options:
            return self._current_option

        # 3. Return None if position/preset is unconfirmed
        return None

    @property
    def extra_state_attributes(self) -> dict[str, Any]:
        """Return PTZ coordinates and status attributes."""
        status = self.coordinator.get_ptz_status(self._channel)
        attrs: dict[str, Any] = {}
        if "pan" in status:
            attrs["pan"] = status["pan"]
        if "tilt" in status:
            attrs["tilt"] = status["tilt"]
        if "zoom" in status:
            attrs["zoom"] = status["zoom"]
        if "move_status" in status:
            attrs["move_status"] = status["move_status"]
        if "zoom_status" in status:
            attrs["zoom_status"] = status["zoom_status"]
        return attrs

    async def async_select_option(self, option: str) -> None:
        """Move the camera to the selected preset."""
        presets = self._presets
        if not presets:
            return
        try:
            preset_index = int(option.split(":", 1)[0].strip())
        except (ValueError, IndexError):
            _LOGGER.warning("Cannot parse PTZ preset option: %s", option)
            return
        ok = await self.coordinator.client.ptz_goto_preset(
            self._channel, preset_index
        )
        if ok:
            self._current_option = option
            self.async_write_ha_state()
        else:
            _LOGGER.error(
                "Failed to move channel %d to PTZ preset %d.",
                self._channel,
                preset_index,
            )
