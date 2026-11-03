"""Update entity for DVR firmware version tracking and upgrade."""
from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from homeassistant.components.update import (
    UpdateDeviceClass,
    UpdateEntity,
    UpdateEntityDescription,
    UpdateEntityFeature,
)
from homeassistant.const import CONF_HOST, EntityCategory
from homeassistant.core import HomeAssistant
from homeassistant.exceptions import HomeAssistantError
from homeassistant.helpers.entity_platform import AddEntitiesCallback

if TYPE_CHECKING:
    from . import IntelbrasDVRConfigEntry
from .const import CONF_HTTP_PORT, DEFAULT_HTTP_PORT, DOMAIN
from .coordinator import IntelbrasCoordinator
from .entity import DVREntity

_LOGGER = logging.getLogger(__name__)

FIRMWARE_UPDATE_DESCRIPTION = UpdateEntityDescription(
    key="dvr_firmware_update",
    translation_key="dvr_firmware_update",
    device_class=UpdateDeviceClass.FIRMWARE,
    entity_category=EntityCategory.CONFIG,
)


async def async_setup_entry(
    hass: HomeAssistant,
    entry: IntelbrasDVRConfigEntry,
    async_add_entities: AddEntitiesCallback,
) -> None:
    """Set up the DVR firmware update entity."""
    coordinator: IntelbrasCoordinator = entry.runtime_data.coordinator
    async_add_entities([DVRFirmwareUpdate(coordinator, entry)])


class DVRFirmwareUpdate(DVREntity, UpdateEntity):
    """Update entity that tracks the DVR firmware version."""

    entity_description: UpdateEntityDescription = FIRMWARE_UPDATE_DESCRIPTION
    _attr_supported_features = (
        UpdateEntityFeature.INSTALL
        | UpdateEntityFeature.RELEASE_NOTES
        | UpdateEntityFeature.PROGRESS
    )

    def __init__(
        self, coordinator: IntelbrasCoordinator, entry: IntelbrasDVRConfigEntry
    ) -> None:
        """Initialise the firmware update entity."""
        super().__init__(coordinator, entry)
        self._attr_unique_id = f"{entry.entry_id}_firmware_update"
        self._installing = False
        self._install_progress: int | None = None

    @property
    def installed_version(self) -> str | None:
        """Return the firmware version currently running on the DVR."""
        return (self.coordinator.data or {}).get("software_version") or None

    @property
    def latest_version(self) -> str | None:
        """Return the latest firmware version available from the Intelbras server."""
        fw: dict[str, Any] = (self.coordinator.data or {}).get(
            "firmware_update", {}
        )
        latest = fw.get("latest_version")
        if latest is None:
            return self.installed_version
        return latest

    @property
    def release_notes(self) -> str | None:
        """Return firmware release notes if the DVR provides them."""
        fw: dict[str, Any] = (self.coordinator.data or {}).get(
            "firmware_update", {}
        )
        return fw.get("release_notes")

    @property
    def release_url(self) -> str | None:
        """Link to the DVR's own web interface firmware upgrade page."""
        host = self._entry.data.get(CONF_HOST, "")
        port = self._entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
        port_str = f":{port}" if port != DEFAULT_HTTP_PORT else ""
        return f"http://{host}{port_str}/#/system/upgrade" if host else None

    @property
    def in_progress(self) -> bool | int:
        """Return True (or a percentage) while an install is running."""
        if self._install_progress is not None:
            return self._install_progress
        return self._installing

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
            ok = await self.coordinator.client.trigger_firmware_update(
                version=version or self.latest_version
            )
        except Exception as exc:  # noqa: BLE001
            self._installing = False
            self._install_progress = None
            self.async_write_ha_state()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_update_failed",
                translation_placeholders={
                    "error": str(exc),
                    "url": self.release_url or "",
                },
            ) from exc

        if not ok:
            self._installing = False
            self._install_progress = None
            self.async_write_ha_state()
            raise HomeAssistantError(
                translation_domain=DOMAIN,
                translation_key="firmware_update_rejected",
                translation_placeholders={"url": self.release_url or ""},
            )

        self._install_progress = 50
        self.async_write_ha_state()
        _LOGGER.info(
            "DVR firmware upgrade initiated. The device will reboot automatically."
        )
        self._installing = False
        self._install_progress = None
        self.async_write_ha_state()
