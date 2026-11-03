# Intelbras DVR for Home Assistant

<p align="center">
  <img src="https://raw.githubusercontent.com/fabiobrolo/intelbras_dvr/main/custom_components/intelbras_dvr/brand/logo.png" alt="Intelbras DVR" width="300"/>
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/default)
[![Version](https://img.shields.io/badge/version-0.4.0-blue.svg)](https://github.com/fabiobrolo/intelbras_dvr/releases)
[![Home Assistant](https://img.shields.io/badge/home--assistant-2024.4%2B-brightgreen.svg)](https://www.home-assistant.io)

[🇺🇸 English](README.md) | [🇧🇷 Português](README.pt-BR.md)

> ⚠️ **Disclaimer**: Intelbras® and Dahua® are registered trademarks of their respective owners. This is an unofficial community integration without any affiliation, endorsement, or support from Intelbras S.A. or Dahua Technology.

---

High-performance custom integration for **Home Assistant** dedicated to **Intelbras** DVRs and NVRs (and compatible Dahua-based devices) adhering to the **HTTP Protocol Specification V3.35**. 100% local communication via HTTP CGI APIs (`local_push` event stream + `DataUpdateCoordinator`) and RTSP video streams, with zero cloud dependency.

Designed following modern Home Assistant Platinum-tier architectural standards (HA 2024.4+ / 2026.x / Python 3.14), featuring typed `ConfigEntry[IntelbrasDVRData]` (`runtime_data`), `EntityDescription` dataclasses across all platforms, 100% dynamic hardware/channel/split discovery, native Reauth and Reconfigure flows, complete multi-language localization (English and Brazilian Portuguese), non-blocking event loop execution, dynamic audio transcoding, and recording playback in the Media Browser with live preview thumbnails.

---

## 🚀 Recent Architectural Upgrades (v0.4.0)

1. **Platinum-Tier Home Assistant Architecture:**
   * Strict PEP 695 typing (`type IntelbrasDVRConfigEntry = ConfigEntry[IntelbrasDVRData]`) and structured `DVRRuntimeState(TypedDict)` in `DataUpdateCoordinator`.
   * Standardized `EntityDescription` dataclasses (`SensorEntityDescription`, `BinarySensorEntityDescription`, `ButtonEntityDescription`, `NumberEntityDescription`, `SelectEntityDescription`, `SwitchEntityDescription`, `UpdateEntityDescription`, `CameraEntityDescription`) across all platforms.
   * Native `reauth` and `reconfigure` flows in `config_flow.py`, allowing seamless password updates or IP/port changes from the Home Assistant UI without recreating entities.
   * Proper exception routing (`ConfigEntryAuthFailed`, `ConfigEntryNotReady`, `ServiceValidationError`, and `HomeAssistantError`) with full `translation_domain` and `translation_key` localization.

2. **100% Dynamic Hardware & Configuration Discovery (HTTP API V3.35):**
   * Unified `async_discover_channels()` querying `ChannelTitle`, `VideoIn` (`devVideoInput.cgi?action=getCollect`), and `RemoteDevice` to dynamically detect total channels (`4`, `8`, `16`, `32`), active analog/IP cameras, signal formats (`HDCVI`, `AHD`, `TVI`, `CVBS`, `IP`), PTZ presets (`ptz.cgi?action=getPresets`), audio codecs, and video profiles.
   * **Dynamic Monitor Split Discovery:** Queries `encode.cgi?action=getCaps` (`PreviewSplitNumList`) and `split.cgi?action=getMode` to dynamically build localized HDMI/VGA split options (`split1`, `split4`, `split8`, `split9`, `split16`, `split25`, `split36`) and channel groups tailored to your DVR's exact channel count.
   * **V3.35 Firmware Upgrader Support:** Checks both `CloudUpgrader` JSON-RPC / `upgrade.cgi?action=getInfo` and `upgrader.cgi?action=getState` for cloud firmware updates.
   * **AutoLogout Prevention:** Automatically sets both `GUISet[0].AutoLogout=0` (V3.35 §4.5.14) and `General.AutoLogout=0` on setup to keep the physical HDMI/VGA monitor active 24/7.
   * Zero hardcoded channel numbers, camera names, monitor split options, or IP addresses.

3. **Adaptive Hybrid Playback Engine:**
   * **Channels without Audio:** Streamed directly from the DVR over RTSP via native Home Assistant HLS with 0% extra CPU consumption and zero latency.
   * **Channels with Audio (e.g. Video Doorbell, cameras with built-in microphone or coaxial HDCVI audio):** Dynamically registered with `go2rtc` on demand using `#video=copy#audio=aac#async`. AAC transcode ensures audio playback in modern browsers and mobile apps without audio stutter or desync. The transient stream is automatically cleaned up when playback ends.

4. **Complete Localization (i18n):**
   * 100% key parity across `strings.json`, `translations/en.json`, `translations/pt.json`, and `translations/pt-BR.json` covering all entity titles, states, dynamic monitor split templates, config/options/reauth/reconfigure steps, exceptions, and services.

---

## 📊 Tested Hardware Compatibility Matrix

The table below lists verified hardware models, firmware versions, and their operational feature status:

| Model / Firmware | Live RTSP (Main / Sub) | Snapshots (CGI / RTSP) | Recording Playback (HLS) | Dynamic AAC Audio (go2rtc) | Recording Thumbnails | Real-Time Push Events | Picture Controls | Privacy Mask | HDMI/VGA Split Monitor | PTZ Controls | Diagnostics & Reboot |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Intelbras iMHDX 3008**<br>`4.001.00IB001.0.T` | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported |
| **Intelbras MHDX 1108**<br>`4.001.00IB000.1` | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported |

---

## 🌟 Generated Entities & Platforms

### 🖥️ Main DVR Device Entities
| Platform | Entity (`translation_key`) | Category | Description |
| :--- | :--- | :--- | :--- |
| `sensor` | `dvr_status` | Diagnostic | Connection status (`online` / `offline`), firmware version, serial number, and last heartbeat timestamp |
| `sensor` | `monitor_mode` | Diagnostic | Active HDMI/VGA split mode (`split1`, `split4`, `split8`, `split9`, `split16`, etc.) with `group` and `raw_value` attributes |
| `sensor` | `disk_status` | Diagnostic | Hard drive capacity (`total_gb`, `free_gb`), usage percentage, and health status |
| `binary_sensor` | `event_stream` | Diagnostic | Real-time HTTP push event stream connectivity (`eventManager.cgi`) |
| `select` | `monitor_split` | Primary | Dynamically generated selector for HDMI/VGA monitor split and channel views |
| `button` | `dvr_reboot` | Config | Hardware reboot button for the DVR |
| `update` | `firmware` | Config | Firmware update checker and one-click cloud upgrade trigger |

### 🎥 Per-Channel Camera Device Entities (Modular via Config/Options Flow)
| Platform | Entity (`translation_key`) | Feature Toggle | Description |
| :--- | :--- | :--- | :--- |
| `camera` | `main_stream` | `main_stream` | High-resolution RTSP stream (`subtype=0`) with CGI/FFmpeg snapshot fallback |
| `camera` | `sub_stream` | `sub_stream` | Low-bandwidth RTSP stream (`subtype=1`) ideal for wall panels and dashboards |
| `camera` | `snapshot` | `snapshot` | Still-image camera entity with configurable refresh interval (`snapshot_interval`) |
| `binary_sensor` | `motion` | `motion` | Real-time motion detection (`VideoMotion` / `SmartMotionHuman` / `SmartMotionVehicle`) |
| `binary_sensor` | `video_loss` | `motion` | Real-time camera disconnection / video loss detection (`VideoLoss`) |
| `binary_sensor` | `tampering` | `motion` | Real-time lens masking / video blind detection (`VideoBlind`) |
| `select` | `color_profile` | `picture` | Active picture adjustment profile (`day`, `night`, `normal`) |
| `number` | `brightness`, `contrast`, `saturation`, `hue`, `sharpness` | `picture` | Per-profile picture sliders (`0`–`100`) |
| `button` | `reset_picture_defaults` | `picture` | Resets current picture profile to factory default values (`50`) |
| `switch` | `privacy_mask` | `privacy` | Blanks out the camera video feed (`Brightness=0, Contrast=0, Saturation=0`) and restores previous values when turned off |
| `button` | `ptz_up`, `ptz_down`, `ptz_left`, `ptz_right`, `ptz_zoom_in`, `ptz_zoom_out` | `ptz` | Directional and zoom PTZ controls with timed auto-stop (created only on channels with PTZ support) |
| `select` | `ptz_preset` | `ptz` | Moves the PTZ camera to a discovered preset (created only on channels with configured PTZ presets) |

---

## 📦 Installation

### Via HACS (Recommended)
1. Ensure [HACS](https://hacs.xyz/) is installed.
2. Open **HACS** in Home Assistant.
3. Click the three dots in the top right corner and choose **Custom repositories**.
4. Enter URL: `https://github.com/fabiobrolo/intelbras_dvr`
5. Select category: **Integration**.
6. Click **Add**, find **Intelbras DVR** in the list, and click **Download**.
7. Restart Home Assistant.

### Manual Installation
1. Copy the `custom_components/intelbras_dvr/` directory into your `/config/custom_components/` folder.
2. Restart Home Assistant.

---

## ⚙️ Configuration, Reconfiguration & Reauthentication

1. In Home Assistant, navigate to **Settings > Devices & Services > Add Integration**.
2. Search for **Intelbras DVR**.
3. **Step 1 - Connection (`user`):** Enter host/IP address, username, password, HTTP port (default `80`), and RTSP port (default `554`).
4. **Step 2 - Camera Channels (`channels`):** The integration dynamically inspects all DVR channels, displays each camera's custom title and PTZ status, and filters out unassigned empty IP slots. Select which cameras to import.
5. **Step 3 - Feature Selection (`features`):** Choose the features to enable (`main_stream`, `sub_stream`, `snapshot`, `motion`, `picture`, `privacy`, `ptz`) and the snapshot cache interval (`snapshot_interval`, default `30` seconds).

> 💡 **Options Flow (Add/Remove Cameras & Features):** Click **Configure** on the integration card at any time to enable/disable camera channels or features. Deselected channels automatically have their orphan devices cleaned up from the Device Registry.
>
> 🔧 **Reconfigure Flow:** Open the three-dot menu on the integration card and select **Reconfigure** to update the DVR's IP address, HTTP/RTSP ports, or credentials without losing entity history.
>
> 🔐 **Reauthentication Flow:** If the DVR password is changed, Home Assistant will automatically prompt you to re-enter credentials via the native repair notification.

---

## 🛠️ Services

| Service | Description | Parameters |
|---|---|---|
| `intelbras_dvr.set_video_color` | Adjust picture settings on a camera channel | `channel` (`1`–`32`), `brightness`, `contrast`, `hue`, `saturation`, `acutance` (`0`–`100`), `profile` (`0`=Day, `1`=Night, `2`=Normal) |
| `intelbras_dvr.mask_camera` | Black out or restore camera video | `channel` (`1`–`32`), `action` (`mask` / `unmask`) |
| `intelbras_dvr.set_monitor_split` | Change HDMI/VGA physical monitor split mode | `mode` (`split1`, `split4`, `split8`, `split9`, `split16`, `split25`, `split36`), `group` (e.g. `0`, `1`, `2`), `channel` (`1`–`32`) |
| `intelbras_dvr.record_camera` | Temporarily switch camera to continuous manual recording | `channel` (`1`–`32`), `duration` (`5`–`3600` seconds) |
| `intelbras_dvr.ptz_move` | Move PTZ camera in a direction with timed stop | `channel` (`1`–`32`), `direction` (`Up`, `Down`, `Left`, `Right`, `ZoomWide`, `ZoomTele`, etc.), `speed` (`1`–`8`), `duration` (`0.1`–`10.0`s) |
| `intelbras_dvr.ptz_stop` | Stop ongoing PTZ movement | `channel` (`1`–`32`), `direction` (optional, default `Up`) |
| `intelbras_dvr.ptz_preset` | Move PTZ camera to a saved preset | `channel` (`1`–`32`), `preset` (`1`–`255`) |
| `intelbras_dvr.reboot` | Reboot the DVR hardware | `entry_id` (optional) |

---

## 📺 Timeline & Thumbnails with `custom:advanced-camera-card`

```yaml
type: custom:advanced-camera-card
cameras:
  - camera_entity: camera.my_dvr_camera
    live_provider: ha # or go2rtc
    media:
      type: folder
      folders:
        - dvr_recordings

folders:
  - id: dvr_recordings
    type: ha
    ha:
      url: /media-browser/browser/app%2Cmedia-source%3A%2F%2Fintelbras_dvr%2FALLFILES%7C<ENTRY_ID>%7C<CH>%7C<YYYYMMDD>
      path:
        - matchers:
            - type: title
          parsers:
            - type: date
              regexp: '(?<value>\d{2}:\d{2})'
              format: 'HH:mm'

timeline:
  show_recordings: true
  style: ribbon
  window_seconds: 3600
```

---

## 👨‍💻 Author & Maintainer

* **Fábio Brolo** ([@fabiobrolo](https://github.com/fabiobrolo))

## 📄 License

Distributed under the MIT License. See `LICENSE` for details.
