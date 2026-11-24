# Intelbras DVR Integration — Project Rules

These guardrails are mandatory for every change in the `intelbras_dvr` repository.

## 1. 100% Localization & Language Discipline
- **Code in English:** 100% of `.py` source code, identifiers, comments, docstrings, and log messages must be written in English.
- **100% Localizable UI:** Every entity name, state, selector option, config/options flow text, Repairs issue, service field, and `HomeAssistantError` / `ServiceValidationError` must use `translation_key` (never hardcoded user-facing strings).
- **4-File Translation Parity:** Always keep **100% key parity** across:
  - `custom_components/intelbras_dvr/strings.json`
  - `custom_components/intelbras_dvr/translations/en.json`
  - `custom_components/intelbras_dvr/translations/pt.json`
  - `custom_components/intelbras_dvr/translations/pt-BR.json`

## 2. Home Assistant Modern Best Practices (HA 2024.4+ / 2026.x)
- **DVR/NVR Hub Architecture:** Keep `manifest.json` as `"integration_type": "hub"`. This integration targets Intelbras/Dahua **DVRs and NVRs** (parent recorder + child channel devices via `via_device_id`), not standalone single IP cameras.
- **Typed Runtime & Entity Descriptions:** Use `ConfigEntry[IntelbrasDVRData]` (`entry.runtime_data`), `EntityDescription` dataclasses, `_attr_has_entity_name = True`, `DeviceInfo` with `hw_version`/`sw_version`/`connections` (MAC), and `SupportsResponse.ONLY` for query services.
- **DVR Stability & Tiered Polling:** Protect the embedded DVR HTTP server by keeping CGI calls serialized with `CGI_REQUEST_PACING`, keeping the 60 s fast poll strictly to lightweight local tables (`VideoColor`, `RecordMode`), and placing heavy/remote tables (`Encode`, `MotionDetect`, `VideoWidget`, `VideoInOptions`, storage, network, PTZ) in the 5-minute slow poll.

## 3. Features Gated by Config Flow & Options Flow
- Every optional entity **and** its corresponding polling query in `coordinator.py` must be gated by the user's selected features in `config_flow.py` / `options_flow` (e.g. `CameraFeature.MOTION` gates both motion entities and `get_motion_detect_config()`).
- LAN auto-discovery (`DHDiscover` UDP `37810`) must suggest only **unconfigured DVR/NVR hubs**, filtering out standalone IP cameras (`IPC`) and already-configured recorders.

## 4. Diagnostics Always Up to Date
- Whenever a new feature, coordinator field, client cache, or event state is added, update `custom_components/intelbras_dvr/diagnostics.py` so the debug JSON required by GitHub issue templates always covers 100% of active features.
- Keep all sensitive fields (`username`, `password`, `host`, `ip`, `ip_address`, `configuration_url`, `serialNumber`, `serial_number`, `mac`, `mac_address`, `unique_id`) redacted in `TO_REDACT`.

## 5. Documentation Parity (READMEs)
- Every change that adds/modifies entities, services, polling tiers, discovery behavior, or integration scope must update **both** `README.md` (English) and `README.pt-BR.md` (Brazilian Portuguese).

## 6. Release & Versioning Gate
- **Never** bump version (`manifest.json` / `const.py`), commit, tag, push, or publish a GitHub release automatically.
- Always validate syntax/translations, deploy to Home Assistant, verify live, ask the user to test, and **wait for explicit user approval** (including the version number) before publishing any new release.
