"""PTZ helper entities: direction buttons and preset selector per PTZ-capable channel."""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass

from homeassistant.components.button import ButtonEntity, ButtonEntityDescription
from homeassistant.components.select import SelectEntity
from homeassistant.config_entries import ConfigEntry

from .const import PTZ_MOVE_DURATION
from .coordinator import IntelbrasCoordinator
from .entity import CameraChannelEntity

_LOGGER = logging.getLogger(__name__)


# ---------------------------------------------------------------------------
# PTZ direction button descriptors
# ---------------------------------------------------------------------------


@dataclass(frozen=True, kw_only=True)
class PTZButtonDescription(ButtonEntityDescription):
    """Extend ButtonEntityDescription with the PTZ direction code."""

    ptz_code: str  # CGI code sent to ptz.cgi


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


# ---------------------------------------------------------------------------
# PTZ direction button
# ---------------------------------------------------------------------------


class PTZDirectionButton(CameraChannelEntity, ButtonEntity):
    """
    Momentary PTZ movement button.

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


# ---------------------------------------------------------------------------
# PTZ preset selector
# ---------------------------------------------------------------------------


class PTZPresetSelect(CameraChannelEntity, SelectEntity):
    """
    Select entity that lists saved PTZ presets for a channel.

    Changing the selection instantly moves the camera to the chosen preset.
    Presets are read from the coordinator data (refreshed every poll cycle).
    """

    _attr_translation_key = "ptz_preset"
    _attr_icon = "mdi:map-marker"

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
        if self._current_option in self.options:
            return self._current_option
        if self.options:
            return self.options[0]
        return None

    async def async_select_option(self, option: str) -> None:
        """Move the camera to the selected preset."""
        presets = self._presets
        if not presets:
            return
        # Parse index from "<index>: <name>"
        try:
            preset_index = int(option.split(":", 1)[0].strip())
        except (ValueError, IndexError):
            _LOGGER.warning("Cannot parse PTZ preset option: %s", option)
            return
        ok = await self.coordinator.client.ptz_goto_preset(self._channel, preset_index)
        if ok:
            self._current_option = option
            self.async_write_ha_state()
        else:
            _LOGGER.error(
                "Failed to move channel %d to PTZ preset %d.",
                self._channel,
                preset_index,
            )

