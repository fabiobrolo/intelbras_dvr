"""Config flow and options flow for the Intelbras DVR integration."""
from __future__ import annotations

import asyncio
import logging
from typing import Any

import voluptuous as vol

from homeassistant import config_entries
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.core import callback
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.helpers import device_registry as dr, entity_registry as er
from homeassistant.helpers.selector import (
    SelectOptionDict,
    SelectSelector,
    SelectSelectorConfig,
    SelectSelectorMode,
)

from .const import (
    ALL_CAMERA_FEATURES,
    CONF_CHANNELS,
    CONF_FEATURES,
    CONF_HTTP_PORT,
    CONF_PTZ_CHANNELS,
    CONF_RTSP_PORT,
    DEFAULT_HTTP_PORT,
    DEFAULT_RTSP_PORT,
    DOMAIN,
    PTZ_ONLY_FEATURES,
    CameraFeature,
)
from .dvr import DVRAuthError, DVRConnectionError, IntelbrasClient

_LOGGER = logging.getLogger(__name__)

# Features available to every channel (before PTZ detection)
_BASE_FEATURES = [f for f in ALL_CAMERA_FEATURES if f not in PTZ_ONLY_FEATURES]


class IntelbrasDVRConfigFlow(config_entries.ConfigFlow, domain=DOMAIN):
    """Handle a config flow for Intelbras DVR."""

    VERSION = 2

    def __init__(self) -> None:
        """Initialise flow state."""
        self._host: str = ""
        self._username: str = ""
        self._password: str = ""
        self._http_port: int = DEFAULT_HTTP_PORT
        self._rtsp_port: int = DEFAULT_RTSP_PORT
        self._serial: str = ""
        self._title: str = ""
        self._available_channels: dict[int, str] = {}
        self._ptz_channels: list[int] = []
        self._onvif_channels: set[int] = set()
        self._selected_channels: list[int] = []

    # ------------------------------------------------------------------
    # Step 1 – credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Handle the initial step: DVR credentials."""
        errors: dict[str, str] = {}

        if user_input is not None:
            self._host = user_input[CONF_HOST].strip()
            self._username = user_input[CONF_USERNAME].strip()
            self._password = user_input[CONF_PASSWORD].strip()
            self._http_port = user_input.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
            self._rtsp_port = user_input.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)

            client = IntelbrasClient(
                host=self._host,
                username=self._username,
                password=self._password,
                http_port=self._http_port,
                rtsp_port=self._rtsp_port,
            )
            try:
                info = await client.get_system_info()
                if not info or "serialNumber" not in info:
                    errors["base"] = "cannot_connect"
                else:
                    self._serial = info["serialNumber"]
                    model = info.get("updateSerial", "DVR")
                    self._title = f"Intelbras {model} ({self._host})"

                    await self.async_set_unique_id(self._serial)
                    self._abort_if_unique_id_configured(updates={CONF_HOST: self._host})

                    self._available_channels = await client.get_channel_titles()
                    if not self._available_channels:
                        errors["base"] = "no_channels"
                    else:
                        # Detect PTZ-capable and ONVIF channels in parallel
                        detect_ptz = getattr(client, "detect_ptz_channels", None)
                        ptz_coro = (
                            detect_ptz(list(self._available_channels.keys()))
                            if detect_ptz
                            else None
                        )
                        get_onvif = getattr(client, "get_onvif_channels", None)
                        onvif_coro = get_onvif() if get_onvif else None

                        (
                            self._ptz_channels,
                            self._onvif_channels,
                        ) = await _gather_ignore_errors(
                            ptz_coro,
                            onvif_coro,
                            defaults=([], set()),
                        )
                        self._ptz_channels = [
                            ch for ch in self._ptz_channels if ch in self._available_channels
                        ]
                        return await self.async_step_channels()
            except AbortFlow:
                raise
            except DVRAuthError:
                errors["base"] = "invalid_auth"
            except DVRConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error connecting to DVR")
                errors["base"] = "unknown"
            finally:
                await client.close()

        return self.async_show_form(
            step_id="user",
            data_schema=vol.Schema(
                {
                    vol.Required(CONF_HOST): str,
                    vol.Required(CONF_USERNAME): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_HTTP_PORT, default=DEFAULT_HTTP_PORT): int,
                    vol.Optional(CONF_RTSP_PORT, default=DEFAULT_RTSP_PORT): int,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 – camera selection
    # ------------------------------------------------------------------

    async def async_step_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Let the user select which camera channels to configure."""
        errors: dict[str, str] = {}

        if user_input is not None:
            # SelectSelector returns a list; guard against None
            selected: list[str] = user_input.get(CONF_CHANNELS) or []
            if not selected:
                errors[CONF_CHANNELS] = "no_channels_selected"
            else:
                self._selected_channels = [int(c) for c in selected]
                return await self.async_step_features()

        options = _channel_options(
            self._available_channels, self._ptz_channels, self._onvif_channels
        )
        return self.async_show_form(
            step_id="channels",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_CHANNELS, default=[]): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={
                "found": str(len(self._available_channels)),
                "ptz": str(len(self._ptz_channels)),
            },
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 3 – feature selection
    # ------------------------------------------------------------------

    async def async_step_features(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Let the user choose which features to enable for the selected cameras."""
        has_ptz = any(ch in self._ptz_channels for ch in self._selected_channels)

        if user_input is not None:
            enabled: list[str] = user_input.get(CONF_FEATURES) or []
            return self.async_create_entry(
                title=self._title,
                data={
                    CONF_HOST: self._host,
                    CONF_USERNAME: self._username,
                    CONF_PASSWORD: self._password,
                    CONF_HTTP_PORT: self._http_port,
                    CONF_RTSP_PORT: self._rtsp_port,
                    CONF_CHANNELS: self._selected_channels,
                    CONF_PTZ_CHANNELS: [
                        ch for ch in self._ptz_channels
                        if ch in self._selected_channels
                    ],
                    CONF_FEATURES: {
                        str(ch): (
                            enabled
                            if ch in self._ptz_channels
                            else [f for f in enabled if f != CameraFeature.PTZ]
                        )
                        for ch in self._selected_channels
                    },
                    "channel_names": {
                        str(ch): name
                        for ch, name in self._available_channels.items()
                    },
                },
            )

        default_features = _default_features(has_ptz)
        feature_options = _feature_options(has_ptz)

        return self.async_show_form(
            step_id="features",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_FEATURES, default=default_features): SelectSelector(
                        SelectSelectorConfig(
                            options=feature_options,
                            translation_key="features",
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={
                "cameras": ", ".join(
                    self._available_channels.get(ch, f"Channel {ch + 1}")
                    for ch in self._selected_channels
                ),
            },
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: config_entries.ConfigEntry,
    ) -> IntelbrasDVROptionsFlow:
        """Return the options flow handler."""
        return IntelbrasDVROptionsFlow(config_entry)


class IntelbrasDVROptionsFlow(config_entries.OptionsFlow):
    """Allow reconfiguring cameras and features without reinstalling."""

    def __init__(
        self, config_entry: config_entries.ConfigEntry | None = None
    ) -> None:
        """Initialise the options flow."""
        super().__init__()
        if config_entry is not None:
            self._config_entry = config_entry
        self._available_channels: dict[int, str] = {}
        self._ptz_channels: list[int] = []
        self._onvif_channels: set[int] = set()
        self._selected_channels: list[int] = []

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Re-query the DVR for available channels, PTZ, and ONVIF capabilities, then proceed."""
        client = IntelbrasClient(
            host=self.config_entry.data[CONF_HOST],
            username=self.config_entry.data[CONF_USERNAME],
            password=self.config_entry.data[CONF_PASSWORD],
            http_port=self.config_entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
            rtsp_port=self.config_entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
        )
        existing_channel_names = self.config_entry.data.get("channel_names", {})
        configured_channels = set(self.config_entry.data.get(CONF_CHANNELS, []))
        fallback_channels = {
            int(k): v
            for k, v in existing_channel_names.items()
            if not configured_channels or int(k) in configured_channels
        }

        try:
            try:
                self._available_channels = await client.get_channel_titles()
            except Exception:  # noqa: BLE001
                self._available_channels = {}
            if not self._available_channels:
                self._available_channels = fallback_channels

            candidate_channels = list(self._available_channels.keys())
            detect_ptz = getattr(client, "detect_ptz_channels", None)
            ptz_coro = detect_ptz(candidate_channels) if detect_ptz else None

            get_onvif = getattr(client, "get_onvif_channels", None)
            onvif_coro = get_onvif() if get_onvif else None

            (
                self._ptz_channels,
                self._onvif_channels,
            ) = await _gather_ignore_errors(
                ptz_coro,
                onvif_coro,
                defaults=(
                    [
                        ch
                        for ch in self.config_entry.data.get(CONF_PTZ_CHANNELS, [])
                        if ch in self._available_channels
                    ],
                    set(),
                ),
            )
            self._ptz_channels = [
                ch for ch in self._ptz_channels if ch in self._available_channels
            ]
        finally:
            await client.close()

        return await self.async_step_channels()

    async def async_step_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Update camera channel selection."""
        errors: dict[str, str] = {}

        if user_input is not None:
            selected: list[str] = user_input.get(CONF_CHANNELS) or []
            if not selected:
                errors[CONF_CHANNELS] = "no_channels_selected"
            else:
                self._selected_channels = [int(c) for c in selected]
                return await self.async_step_features()

        currently: list[str] = [
            str(c)
            for c in self.config_entry.data.get(CONF_CHANNELS, [])
            if c in self._available_channels
        ]
        options = _channel_options(
            self._available_channels, self._ptz_channels, self._onvif_channels
        )
        return self.async_show_form(
            step_id="channels",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_CHANNELS, default=currently): SelectSelector(
                        SelectSelectorConfig(
                            options=options,
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
            description_placeholders={
                "found": str(len(self._available_channels)),
                "ptz": str(len(self._ptz_channels)),
            },
            errors=errors,
        )

    async def async_step_features(
        self, user_input: dict[str, Any] | None = None
    ) -> config_entries.FlowResult:
        """Update feature selection for the chosen channels."""
        has_ptz = any(ch in self._ptz_channels for ch in self._selected_channels)

        if user_input is not None:
            enabled: list[str] = user_input.get(CONF_FEATURES) or []
            current_features: dict[str, list[str]] = dict(
                self.config_entry.data.get(CONF_FEATURES, {})
            )
            for ch in self._selected_channels:
                current_features[str(ch)] = (
                    enabled
                    if ch in self._ptz_channels
                    else [f for f in enabled if f != CameraFeature.PTZ]
                )
            # Remove channels that were deselected
            for key in list(current_features):
                if int(key) not in self._selected_channels:
                    del current_features[key]

            # Clean up devices and entities for channels that were deselected
            deselected_channels = set(self.config_entry.data.get(CONF_CHANNELS, [])) - set(self._selected_channels)
            if deselected_channels:
                device_reg = dr.async_get(self.hass)
                existing_devices = dr.async_entries_for_config_entry(device_reg, self.config_entry.entry_id)
                for dev in existing_devices:
                    for domain, identifier in dev.identifiers:
                        if domain == DOMAIN and identifier.startswith(f"{self.config_entry.entry_id}_ch"):
                            try:
                                ch_idx = int(identifier.split("_ch")[-1])
                                if ch_idx in deselected_channels:
                                    _LOGGER.info("Removing deselected camera device '%s' (ch %d)", dev.name, ch_idx)
                                    device_reg.async_update_device(dev.id, remove_config_entry_id=self.config_entry.entry_id)
                            except ValueError:
                                pass

            # Clean up PTZ entities for channels that do not have PTZ enabled
            active_ptz_channels = {
                ch
                for ch in self._ptz_channels
                if ch in self._selected_channels and CameraFeature.PTZ in enabled
            }
            entity_reg = er.async_get(self.hass)
            for ent in er.async_entries_for_config_entry(entity_reg, self.config_entry.entry_id):
                uid = ent.unique_id or ""
                prefix = f"{self.config_entry.entry_id}_ch"
                if uid.startswith(prefix) and "_ptz_" in uid:
                    try:
                        ch_part = uid[len(prefix):].split("_", 1)[0]
                        ch_idx = int(ch_part)
                        if ch_idx not in active_ptz_channels:
                            entity_reg.async_remove(ent.entity_id)
                    except ValueError:
                        pass

            updated_data = {
                **self.config_entry.data,
                CONF_CHANNELS: self._selected_channels,
                CONF_PTZ_CHANNELS: [
                    ch for ch in self._ptz_channels if ch in self._selected_channels
                ],
                CONF_FEATURES: current_features,
                "channel_names": {
                    str(ch): name
                    for ch, name in self._available_channels.items()
                },
            }
            self.hass.config_entries.async_update_entry(self.config_entry, data=updated_data)
            await self.hass.config_entries.async_reload(self.config_entry.entry_id)
            return self.async_create_entry(title="", data={})

        # Pre-fill features from the union of saved features across all selected channels
        # (Using only the first channel would drop 'ptz' whenever CH 1 is not a PTZ camera)
        stored_features: dict[str, list[str]] = self.config_entry.data.get(CONF_FEATURES, {})
        union_stored: set[str] = set()
        for ch in self._selected_channels:
            union_stored.update(stored_features.get(str(ch), []))

        if union_stored:
            current_enabled = [
                f
                for f in ALL_CAMERA_FEATURES
                if f in union_stored and (f != CameraFeature.PTZ or has_ptz)
            ]
            if has_ptz and CameraFeature.PTZ not in current_enabled:
                ptz_selected = [ch for ch in self._selected_channels if ch in self._ptz_channels]
                if any(str(ch) not in stored_features for ch in ptz_selected):
                    current_enabled.append(CameraFeature.PTZ)
        else:
            current_enabled = _default_features(has_ptz)

        return self.async_show_form(
            step_id="features",
            data_schema=vol.Schema(
                {
                    vol.Optional(CONF_FEATURES, default=current_enabled): SelectSelector(
                        SelectSelectorConfig(
                            options=_feature_options(has_ptz),
                            translation_key="features",
                            multiple=True,
                            mode=SelectSelectorMode.LIST,
                        )
                    ),
                }
            ),
        )


# ---------------------------------------------------------------------------
# Private helpers
# ---------------------------------------------------------------------------


def _channel_options(
    available: dict[int, str],
    ptz: list[int],
    onvif: set[int] | None = None,
) -> list[SelectOptionDict]:
    """Build the SelectSelector options list for channel selection."""
    onvif_set = onvif or set()
    result = []
    for ch, name in sorted(available.items()):
        tags = []
        if ch in ptz:
            tags.append("[PTZ]")
        if ch in onvif_set:
            tags.append("[ONVIF]")
        tag_suffix = f" {' '.join(tags)}" if tags else ""
        result.append(
            SelectOptionDict(
                value=str(ch),
                label=f"CH {ch + 1}: {name}{tag_suffix}",
            )
        )
    return result


def _feature_options(has_ptz: bool) -> list[str]:
    """Build the SelectSelector options list for feature selection using translation keys."""
    return [
        f
        for f in ALL_CAMERA_FEATURES
        if f != CameraFeature.PTZ or has_ptz
    ]


def _default_features(has_ptz: bool) -> list[str]:
    """Return the default set of features to pre-select."""
    defaults = [f for f in ALL_CAMERA_FEATURES if f not in PTZ_ONLY_FEATURES]
    if has_ptz:
        defaults.append(CameraFeature.PTZ)
    return defaults


async def _gather_ignore_errors(
    *coros: Any,
    defaults: tuple[Any, ...],
) -> tuple[Any, ...]:
    """Run coroutines concurrently; replace None or exceptions with the corresponding default."""
    valid_tasks = []
    for coro, default in zip(coros, defaults):
        if coro is not None and asyncio.iscoroutine(coro):
            valid_tasks.append(coro)
        else:
            async def _dummy(val=default):
                return val
            valid_tasks.append(_dummy())

    results = await asyncio.gather(*valid_tasks, return_exceptions=True)
    return tuple(
        default if (isinstance(r, Exception) or r is None) else r
        for r, default in zip(results, defaults)
    )
