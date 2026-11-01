"""Update entity for DVR firmware version tracking and upgrade."""
from __future__ import annotations

import logging
from typing import Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityFeature,
)
from homeassistant.config_entries import ConfigEntry
from homeassistant.const import EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

from .const import CONF_HTTP_PORT, DEFAULT_HTTP_PORT
from .coordinator import IntelbrasCoordinator
from .entity import DVREntity

_LOGGER = logging.getLogger(__name__)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: ConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the DVR firmware update entity."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    async_add_entities([DVRFirmwareUpdate(coordinator, entry)])


class DVRFirmwareUpdate(DVREntity, UpdateEntity):
    """
    Update entity that tracks the DVR firmware version.

    - ``installed_version``: read from the coordinator data (magicBox getSoftwareVersion).
    - ``latest_version``: read from the periodic firmware update check
      (magicBox getLatestSoftwareVersion). Returns ``None`` if the DVR
      firmware does not support the check endpoint — in that case Home
      Assistant shows the entity as "Up to date" and no install button appears.
    - ``async_install()``: calls ``upgrade.cgi?action=upgradeSystem`` if the
      DVR accepted the firmware check. Falls back to a link to the DVR web
      interface when not supported.
    """

    _attr_device_class = UpdateDeviceClass.FIRMWARE
    _attr_entity_category = EntityCategory.CONFIG
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.RELEASE_NOTES
        | UpdateEntityFeature.PROGRESS
    )
    _attr_translation_key = "dvr_firmware_update"

    def __init__(self, coordinator: IntelbrasCoordinator, entry: ConfigEntry) -> None:
        """Initialise the firmware update entity."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_firmware_update"
        self._installing = False
        self._install_progress: int | None = None

    # ------------------------------------------------------------------
    # UpdateEntity properties
    # ------------------------------------------------------------------

    @property
    def installed_version(self) -> str | None:
        """Return the firmware version currently running on the DVR."""
        return (self.coordinator.data or {}).get("software_version") or None

    @property
    def latest_version(self) -> str | None:
        """
        Return the latest firmware version available from the Intelbras server.

        ``None`` when the DVR does not support the online check endpoint.
        """
        fw: dict[str, Any] = (self.coordinator.data or {}).get("firmware_update", {})
        latest = fw.get("latest_version")
        if latest is None:
            return self.installed_version
        return latest

    @property
    def release_notes(self) -> str | None:
        """Return firmware release notes if the DVR provides them."""
        fw: dict[str, Any] = (self.coordinator.data or {}).get("firmware_update", {})
        return fw.get("release_notes")

    @property
    def release_url(self) -> str | None:
        """Link to the DVR's own web interface firmware upgrade page."""
        host = self._entry.data.get("host", "")
        port = self._entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
        port_str = f":{port}" if port != DEFAULT_HTTP_PORT else ""
        return f"http://{host}{port_str}/#/system/upgrade"

    @property
    def in_progress(self) -> bool | int:
        """Return True (or a percentage) while an install is running."""
        if self._install_progress is not None:
            return self._install_progress
        return self._installing

    # ------------------------------------------------------------------
    # Install
    # ------------------------------------------------------------------

    async def async_install(
        self,
        version: str | None,
        backup: bool,  # noqa: FBT001
        **kwargs: Any,
    ) -> None:
        """Trigger the firmware upgrade on the DVR."""
        self._installing = True
        self._install_progress = 0
        self.async_write_ha_state()

        try:
            ok = await self.coordinator.client.trigger_firmware_update()
        except Exception as exc:  # noqa: BLE001
            self._installing = False
            self._install_progress = None
            self.async_write_ha_state()
            raise HomeAssistantError(
                f"Firmware upgrade failed: {exc}\n"
                f"You can upgrade manually at {self.release_url}"
            ) from exc

        if not ok:
            self._installing = False
            self._install_progress = None
            self.async_write_ha_state()
            raise HomeAssistantError(
                "The DVR did not accept the upgrade command. "
                f"Please update manually at {self.release_url}"
            )

        self._install_progress = 50
        self.async_write_ha_state()
        _LOGGER.info(
            "DVR firmware upgrade initiated. The device will reboot automatically."
        )
        self._installing = False
        self._install_progress = None
        self.async_write_ha_state()

