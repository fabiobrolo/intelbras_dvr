"""Config flow and options flow for the Intelbras DVR integration."""
from __future__ import annotations

from collections.abc import Mapping
import functools
import json
import logging
from pathlib import Path
from typing import Any

import voluptuous as vol

from homeassistant.config_entries import (
    ConfigEntry,
    ConfigFlow,
    ConfigFlowResult,
    OptionsFlow,
)
from homeassistant.const import CONF_HOST, CONF_PASSWORD, CONF_USERNAME
from homeassistant.core import HomeAssistant, callback
from homeassistant.data_entry_flow import AbortFlow
from homeassistant.helpers import (
    device_registry as dr,
    entity_registry as er,
    issue_registry as ir,
)
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
    CONF_MONITOR_EVENTS,
    CONF_MONITOR_NETWORK,
    CONF_MONITOR_STORAGE,
    CONF_MONITOR_USERS,
    CONF_PTZ_CHANNELS,
    CONF_RTSP_PORT,
    DEFAULT_HTTP_PORT,
    DEFAULT_RTSP_PORT,
    DOMAIN,
    PTZ_ONLY_FEATURES,
    SYSTEM_FEATURES,
    CameraFeature,
)
from .dvr import DVRAuthError, DVRConnectionError, IntelbrasClient

_LOGGER = logging.getLogger(__name__)


class IntelbrasDVRConfigFlow(ConfigFlow, domain=DOMAIN):
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
        self._system_caps: dict[str, bool] = {}
        self._reauth_entry: ConfigEntry | None = None
        self._system_caps: dict[str, bool] = {}

    # ------------------------------------------------------------------
    # Step 1 – credentials
    # ------------------------------------------------------------------

    async def async_step_user(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
                backoff_on_401=False,
            )
            try:
                info = await client.get_system_info()
                if not info or "serialNumber" not in info:
                    errors["base"] = "cannot_connect"
                else:
                    self._serial = info["serialNumber"]
                    model = info.get("updateSerial") or info.get("deviceType") or "DVR"
                    self._title = f"Intelbras {model} ({self._host})"

                    await self.async_set_unique_id(self._serial)
                    self._abort_if_unique_id_configured(
                        updates={
                            CONF_HOST: self._host,
                            CONF_HTTP_PORT: self._http_port,
                            CONF_RTSP_PORT: self._rtsp_port,
                        }
                    )

                    (
                        self._available_channels,
                        self._ptz_channels,
                        self._onvif_channels,
                    ) = await client.async_discover_channels()
                    self._system_caps = await client.async_discover_system_capabilities()
                    if not self._available_channels:
                        errors["base"] = "no_channels"
                    else:
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
                    vol.Required(CONF_HOST, default=self._host): str,
                    vol.Required(CONF_USERNAME, default=self._username): str,
                    vol.Required(CONF_PASSWORD): str,
                    vol.Optional(CONF_HTTP_PORT, default=self._http_port): int,
                    vol.Optional(CONF_RTSP_PORT, default=self._rtsp_port): int,
                }
            ),
            errors=errors,
        )

    # ------------------------------------------------------------------
    # Step 2 – camera selection
    # ------------------------------------------------------------------

    async def async_step_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Let the user select which camera channels to configure."""
        errors: dict[str, str] = {}

        if user_input is not None:
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
    ) -> ConfigFlowResult:
        """Let the user choose which features to enable for the selected cameras."""
        has_ptz = any(ch in self._ptz_channels for ch in self._selected_channels)

        if user_input is not None:
            enabled: list[str] = user_input.get(CONF_FEATURES) or []
            camera_features = [f for f in enabled if f not in SYSTEM_FEATURES]
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
                        ch
                        for ch in self._ptz_channels
                        if ch in self._selected_channels
                    ],
                    CONF_FEATURES: {
                        str(ch): (
                            camera_features
                            if ch in self._ptz_channels
                            else [f for f in camera_features if f != CameraFeature.PTZ]
                        )
                        for ch in self._selected_channels
                    },
                    "channel_names": {
                        str(ch): name
                        for ch, name in self._available_channels.items()
                    },
                    CONF_MONITOR_USERS: CONF_MONITOR_USERS in enabled,
                    CONF_MONITOR_STORAGE: CONF_MONITOR_STORAGE in enabled,
                    CONF_MONITOR_NETWORK: CONF_MONITOR_NETWORK in enabled,
                    CONF_MONITOR_EVENTS: CONF_MONITOR_EVENTS in enabled,
                },
            )

        default_features = _default_features(has_ptz, self._system_caps)
        feature_options = _feature_options(has_ptz, self._system_caps, self.hass)

        schema_dict: dict[Any, Any] = {
            vol.Optional(
                CONF_FEATURES, default=default_features
            ): SelectSelector(
                SelectSelectorConfig(
                    options=feature_options,
                    translation_key="features",
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        }

        return self.async_show_form(
            step_id="features",
            data_schema=vol.Schema(schema_dict),
            description_placeholders={
                "cameras": ", ".join(
                    self._available_channels.get(ch, f"Channel {ch + 1}")
                    for ch in self._selected_channels
                ),
            },
        )

    # ------------------------------------------------------------------
    # Reauthentication & Reconfiguration flows
    # ------------------------------------------------------------------

    async def async_step_reauth(
        self, entry_data: Mapping[str, Any]
    ) -> ConfigFlowResult:
        """Handle reauthentication when DVR credentials become invalid."""
        self._reauth_entry = self.hass.config_entries.async_get_entry(
            self.context["entry_id"]
        )
        return await self.async_step_reauth_confirm()

    async def async_step_reauth_confirm(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Confirm new credentials for the DVR."""
        errors: dict[str, str] = {}
        entry = self._reauth_entry
        if entry is None:
            return self.async_abort(reason="unknown")

        if user_input is not None:
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD].strip()
            client = IntelbrasClient(
                host=entry.data[CONF_HOST],
                username=username,
                password=password,
                http_port=entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
                rtsp_port=entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
                backoff_on_401=False,
            )
            try:
                info = await client.get_system_info()
                if not info:
                    errors["base"] = "cannot_connect"
                else:
                    self.hass.data.get(DOMAIN, {}).get("auth_lockout_state", {}).pop(
                        entry.entry_id, None
                    )
                    ir.async_delete_issue(
                        self.hass, DOMAIN, f"auth_retrying_{entry.entry_id}"
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                        },
                    )
            except DVRAuthError:
                errors["base"] = "invalid_auth"
            except DVRConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during reauth")
                errors["base"] = "unknown"
            finally:
                await client.close()

        return self.async_show_form(
            step_id="reauth_confirm",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(CONF_PASSWORD): str,
                }
            ),
            description_placeholders={"host": str(entry.data.get(CONF_HOST, ""))},
            errors=errors,
        )

    async def async_step_reconfigure(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Allow reconfiguring host, ports, and credentials from the UI."""
        errors: dict[str, str] = {}
        entry = self._get_reconfigure_entry()

        if user_input is not None:
            host = user_input[CONF_HOST].strip()
            username = user_input[CONF_USERNAME].strip()
            password = user_input[CONF_PASSWORD].strip()
            http_port = user_input.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT)
            rtsp_port = user_input.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT)

            client = IntelbrasClient(
                host=host,
                username=username,
                password=password,
                http_port=http_port,
                rtsp_port=rtsp_port,
                backoff_on_401=False,
            )
            try:
                info = await client.get_system_info()
                if not info:
                    errors["base"] = "cannot_connect"
                else:
                    self.hass.data.get(DOMAIN, {}).get("auth_lockout_state", {}).pop(
                        entry.entry_id, None
                    )
                    ir.async_delete_issue(
                        self.hass, DOMAIN, f"auth_retrying_{entry.entry_id}"
                    )
                    return self.async_update_reload_and_abort(
                        entry,
                        data_updates={
                            CONF_HOST: host,
                            CONF_USERNAME: username,
                            CONF_PASSWORD: password,
                            CONF_HTTP_PORT: http_port,
                            CONF_RTSP_PORT: rtsp_port,
                        },
                    )
            except DVRAuthError:
                errors["base"] = "invalid_auth"
            except DVRConnectionError:
                errors["base"] = "cannot_connect"
            except Exception:  # noqa: BLE001
                _LOGGER.exception("Unexpected error during reconfigure")
                errors["base"] = "unknown"
            finally:
                await client.close()

        return self.async_show_form(
            step_id="reconfigure",
            data_schema=vol.Schema(
                {
                    vol.Required(
                        CONF_HOST, default=entry.data.get(CONF_HOST, "")
                    ): str,
                    vol.Required(
                        CONF_USERNAME, default=entry.data.get(CONF_USERNAME, "")
                    ): str,
                    vol.Required(
                        CONF_PASSWORD, default=entry.data.get(CONF_PASSWORD, "")
                    ): str,
                    vol.Optional(
                        CONF_HTTP_PORT,
                        default=entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
                    ): int,
                    vol.Optional(
                        CONF_RTSP_PORT,
                        default=entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
                    ): int,
                }
            ),
            errors=errors,
        )

    @staticmethod
    @callback
    def async_get_options_flow(
        config_entry: ConfigEntry,
    ) -> IntelbrasDVROptionsFlow:
        """Return the options flow handler."""
        return IntelbrasDVROptionsFlow()


class IntelbrasDVROptionsFlow(OptionsFlow):
    """Allow reconfiguring cameras and features without reinstalling."""

    def __init__(self) -> None:
        """Initialise the options flow."""
        self._available_channels: dict[int, str] = {}
        self._ptz_channels: list[int] = []
        self._onvif_channels: set[int] = set()
        self._selected_channels: list[int] = []
        self._system_caps: dict[str, bool] = {}

    async def async_step_init(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
        """Re-query the DVR for available channels, PTZ, and ONVIF capabilities, then proceed."""
        client = IntelbrasClient(
            host=self.config_entry.data[CONF_HOST],
            username=self.config_entry.data[CONF_USERNAME],
            password=self.config_entry.data[CONF_PASSWORD],
            http_port=self.config_entry.data.get(CONF_HTTP_PORT, DEFAULT_HTTP_PORT),
            rtsp_port=self.config_entry.data.get(CONF_RTSP_PORT, DEFAULT_RTSP_PORT),
        )
        lockout_state = (
            self.hass.data.get(DOMAIN, {})
            .get("auth_lockout_state", {})
            .get(self.config_entry.entry_id)
        )
        if isinstance(lockout_state, dict):
            client.set_lockout_until(
                float(lockout_state.get("deadline", 0.0)),
                int(lockout_state.get("streak", 0)),
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
                (
                    self._available_channels,
                    self._ptz_channels,
                    self._onvif_channels,
                ) = await client.async_discover_channels()
                self._system_caps = await client.async_discover_system_capabilities()
            except Exception:  # noqa: BLE001
                self._available_channels = {}
                self._ptz_channels = []
                self._onvif_channels = set()
                self._system_caps = {}

            if not self._available_channels:
                self._available_channels = fallback_channels
                self._ptz_channels = [
                    ch
                    for ch in self.config_entry.data.get(CONF_PTZ_CHANNELS, [])
                    if ch in self._available_channels
                ]
        finally:
            await client.close()

        return await self.async_step_channels()

    async def async_step_channels(
        self, user_input: dict[str, Any] | None = None
    ) -> ConfigFlowResult:
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
    ) -> ConfigFlowResult:
        """Update feature selection for the chosen channels."""
        has_ptz = any(ch in self._ptz_channels for ch in self._selected_channels)

        if user_input is not None:
            enabled: list[str] = user_input.get(CONF_FEATURES) or []
            camera_features = [f for f in enabled if f not in SYSTEM_FEATURES]
            current_features: dict[str, list[str]] = dict(
                self.config_entry.data.get(CONF_FEATURES, {})
            )
            for ch in self._selected_channels:
                current_features[str(ch)] = (
                    camera_features
                    if ch in self._ptz_channels
                    else [f for f in camera_features if f != CameraFeature.PTZ]
                )
            for key in list(current_features):
                if int(key) not in self._selected_channels:
                    del current_features[key]

            # Clean up devices and entities for channels that were deselected
            deselected_channels = set(
                self.config_entry.data.get(CONF_CHANNELS, [])
            ) - set(self._selected_channels)
            if deselected_channels:
                device_reg = dr.async_get(self.hass)
                existing_devices = dr.async_entries_for_config_entry(
                    device_reg, self.config_entry.entry_id
                )
                for dev in existing_devices:
                    for domain, identifier in dev.identifiers:
                        if domain == DOMAIN and identifier.startswith(
                            f"{self.config_entry.entry_id}_ch"
                        ):
                            try:
                                ch_idx = int(identifier.split("_ch")[-1])
                                if ch_idx in deselected_channels:
                                    _LOGGER.info(
                                        "Removing deselected camera device '%s' (ch %d)",
                                        dev.name,
                                        ch_idx,
                                    )
                                    device_reg.async_remove_device(dev.id)
                            except ValueError:
                                pass

            # Clean up PTZ entities for channels that do not have PTZ enabled
            active_ptz_channels = {
                ch
                for ch in self._ptz_channels
                if ch in self._selected_channels and CameraFeature.PTZ in enabled
            }
            entity_reg = er.async_get(self.hass)
            for ent in er.async_entries_for_config_entry(
                entity_reg, self.config_entry.entry_id
            ):
                uid = ent.unique_id or ""
                prefix = f"{self.config_entry.entry_id}_ch"
                if uid.startswith(prefix) and "_ptz_" in uid:
                    try:
                        ch_part = uid[len(prefix) :].split("_", 1)[0]
                        ch_idx = int(ch_part)
                        if ch_idx not in active_ptz_channels:
                            entity_reg.async_remove(ent.entity_id)
                    except ValueError:
                        pass

            updated_data = {
                **self.config_entry.data,
                CONF_CHANNELS: self._selected_channels,
                CONF_PTZ_CHANNELS: [
                    ch
                    for ch in self._ptz_channels
                    if ch in self._selected_channels
                ],
                CONF_FEATURES: current_features,
                "channel_names": {
                    str(ch): name
                    for ch, name in self._available_channels.items()
                },
                CONF_MONITOR_USERS: CONF_MONITOR_USERS in enabled,
                CONF_MONITOR_STORAGE: CONF_MONITOR_STORAGE in enabled,
                CONF_MONITOR_NETWORK: CONF_MONITOR_NETWORK in enabled,
                CONF_MONITOR_EVENTS: CONF_MONITOR_EVENTS in enabled,
            }
            if self.hass.config_entries.async_update_entry(
                self.config_entry, data=updated_data
            ):
                self.hass.config_entries.async_schedule_reload(
                    self.config_entry.entry_id
                )
            return self.async_create_entry(
                title="", data=dict(self.config_entry.options)
            )

        stored_features: dict[str, list[str]] = self.config_entry.data.get(
            CONF_FEATURES, {}
        )
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
                ptz_selected = [
                    ch
                    for ch in self._selected_channels
                    if ch in self._ptz_channels
                ]
                if any(str(ch) not in stored_features for ch in ptz_selected):
                    current_enabled.append(CameraFeature.PTZ)
        else:
            current_enabled = [
                f
                for f in ALL_CAMERA_FEATURES
                if f not in PTZ_ONLY_FEATURES
            ]
            if has_ptz:
                current_enabled.append(CameraFeature.PTZ)

        if self.config_entry.data.get(CONF_MONITOR_USERS, True) and self._system_caps.get("users", True):
            current_enabled.append(CONF_MONITOR_USERS)
        if self.config_entry.data.get(CONF_MONITOR_STORAGE, True) and self._system_caps.get("storage", True):
            current_enabled.append(CONF_MONITOR_STORAGE)
        if self.config_entry.data.get(CONF_MONITOR_NETWORK, True) and self._system_caps.get("network", True):
            current_enabled.append(CONF_MONITOR_NETWORK)
        if self.config_entry.data.get(CONF_MONITOR_EVENTS, True):
            current_enabled.append(CONF_MONITOR_EVENTS)

        options_schema: dict[Any, Any] = {
            vol.Optional(
                CONF_FEATURES, default=current_enabled
            ): SelectSelector(
                SelectSelectorConfig(
                    options=_feature_options(has_ptz, self._system_caps, self.hass),
                    translation_key="features",
                    multiple=True,
                    mode=SelectSelectorMode.LIST,
                )
            ),
        }

        return self.async_show_form(
            step_id="features",
            data_schema=vol.Schema(options_schema),
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


@functools.lru_cache(maxsize=8)
def _load_translations(lang: str) -> dict[str, str]:
    """Load selector feature options translation for a given language."""
    base_dir = Path(__file__).parent / "translations"
    candidates = [
        base_dir / f"{lang}.json",
        base_dir / f"{lang.split('-')[0]}.json" if "-" in lang else None,
        base_dir / "en.json",
        Path(__file__).parent / "strings.json",
    ]
    for p in candidates:
        if p and p.is_file():
            try:
                with open(p, encoding="utf-8") as f:
                    data = json.load(f)
                    opts = (
                        data.get("selector", {})
                        .get("features", {})
                        .get("options", {})
                    )
                    if opts:
                        return opts
            except Exception:
                continue
    return {}


def _feature_options(
    has_ptz: bool,
    system_caps: dict[str, bool] | None = None,
    hass: HomeAssistant | None = None,
) -> list[SelectOptionDict]:
    """Build the SelectSelector options list for feature selection with translated labels."""
    caps = system_caps or {}
    lang = hass.config.language if hass and hasattr(hass, "config") else "pt-BR"
    translations = _load_translations(lang)

    keys = [
        f
        for f in ALL_CAMERA_FEATURES
        if f != CameraFeature.PTZ or has_ptz
    ]
    if caps.get("users", True):
        keys.append(CONF_MONITOR_USERS)
    if caps.get("storage", True):
        keys.append(CONF_MONITOR_STORAGE)
    if caps.get("network", True):
        keys.append(CONF_MONITOR_NETWORK)
    keys.append(CONF_MONITOR_EVENTS)

    return [
        SelectOptionDict(
            value=k,
            label=translations.get(k, k),
        )
        for k in keys
    ]


def _default_features(
    has_ptz: bool, system_caps: dict[str, bool] | None = None
) -> list[str]:
    """Return the default set of features to pre-select."""
    caps = system_caps or {}
    defaults = [f for f in ALL_CAMERA_FEATURES if f not in PTZ_ONLY_FEATURES]
    if has_ptz:
        defaults.append(CameraFeature.PTZ)
    if caps.get("users", True):
        defaults.append(CONF_MONITOR_USERS)
    if caps.get("storage", True):
        defaults.append(CONF_MONITOR_STORAGE)
    if caps.get("network", True):
        defaults.append(CONF_MONITOR_NETWORK)
    defaults.append(CONF_MONITOR_EVENTS)
    return defaults
