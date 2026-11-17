# Intelbras DVR for Home Assistant

<p align="center">
  <img src="https://raw.githubusercontent.com/fabiobrolo/intelbras_dvr/main/custom_components/intelbras_dvr/brand/logo.png" alt="Intelbras DVR" width="300"/>
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/default)
[![Home Assistant](https://img.shields.io/badge/home--assistant-2024.4%2B-brightgreen.svg)](https://www.home-assistant.io)
[![License: MIT](https://img.shields.io/badge/License-MIT-yellow.svg)](LICENSE)

[🇺🇸 English](README.md) | [🇧🇷 Português](README.pt-BR.md)

> ⚠️ **Disclaimer**: Intelbras® and Dahua® are registered trademarks of their respective owners. This is an unofficial community integration without any affiliation, endorsement, or support from Intelbras S.A. or Dahua Technology.

---

High-performance custom integration for **Home Assistant** dedicated to **Intelbras** DVRs and NVRs (and compatible Dahua-based devices) adhering to the **HTTP Protocol Specification V3.35**. 100% local communication via HTTP CGI APIs (`local_push` event stream + `DataUpdateCoordinator`) and RTSP video streams, with zero cloud dependency.

Designed following modern Home Assistant Platinum-tier architectural standards (HA 2024.4+ / 2026.x / Python 3.14), featuring typed `ConfigEntry[IntelbrasDVRData]` (`runtime_data`), `EntityDescription` dataclasses across all platforms, 100% dynamic hardware/channel/split discovery, native Reauth and Reconfigure flows, complete multi-language localization (English and Brazilian Portuguese), non-blocking event loop execution, dynamic audio transcoding, and recording playback in the Media Browser with live preview thumbnails.

---

## 🌟 Key Capabilities & Features

### 🎥 Video Streaming & Adaptive Playback Engine
* **Dual RTSP Feeds:** High-resolution Main stream (`subtype=0`) and bandwidth-efficient Sub stream (`subtype=1`) per channel.
* **Smart Snapshots:** Configurable interval still-image camera entities with local caching and fast CGI/FFmpeg capture.
* **Adaptive Hybrid Audio (go2rtc):** Channels with audio (e.g., video doorbells, cameras with built-in microphones or coaxial HDCVI audio) dynamically register with `go2rtc` on demand using `#video=copy#audio=aac#async`, ensuring stutter-free audio across modern web browsers and mobile apps.
* **Media Browser Recording Playback:** Browse and watch past DVR recordings organized by date and camera directly inside Home Assistant, complete with timeline ribbons and generated preview thumbnails.

### 🎨 Image Profiles, Light Compensation & Period Scheduling
* **Dynamic Configuration Selector:**
  * **Analog Cameras (`select.camera_*_period_config`):** Choose between `Active (Real-time)`, `Period 1`, and `Period 2`.
  * **Intelbras IP Cameras (`select.camera_*_profile_config`):** Choose between `Active (Real-time)`, `Day`, `Night`, and `Regular`.
* **Real-Time Live Reflection & Isolated Editing:**
  * In default **Active (Real-time)** mode, all sliders, selectors, and switches automatically reflect the exact hardware state active at the current moment according to the configured schedule.
  * Selecting a specific period or profile immediately updates all controls to display that period's stored values. Any changes made are written directly to that specific profile index on the DVR (`VideoColor[ch][prof]`, `VideoInDenoise[ch][prof]`, `ImageEnhancement[ch][prof]`, `NormalOptions`, `NightOptions`) without altering other periods.
* **Light Compensation Mode (`select.camera_*_light_compensation_mode`):** Mutually exclusive selector supporting `Disabled`, `BLC` (Backlight Compensation), `HLC` (Highlight Compensation), and `WDR` (Wide Dynamic Range), populated dynamically based on camera hardware capabilities (`getCaps`).
* **Day/Night & Scene Modes:** 
  * Switch between `Color`, `Auto`, and `Black & White` (`select.camera_*_day_night_mode`) per profile.
  * Profile management mode selection: `Regular`, `Fixed Profile`, `Schedule`, and `Day/Night` (`select.camera_*_profile_mode`).
  * Scene mode / picture styles: `Standard`, `Soft`, and `Vivid` (`select.camera_*_scene_mode`).
* **Noise Reduction & Image Gain:** 
  * 2D Noise Reduction level slider (`number.camera_*_noise_reduction`) and Auto Noise Reduction switch (`switch.camera_*_auto_noise_reduction`).
  * Image Gain / enhancement slider (`number.camera_*_image_gain`) and Video Adjustment / sharpness (`number.camera_*_acutance`).
* **Continuous Period Scheduling (Analog Cameras):** Robust 24-hour cycle coordination automatically calculates complementary intervals between Period 1 and Period 2, preventing time overlap conflicts when enabling or adjusting periods.

### ✍️ Dynamic OSD Video Overlays
* **On-Screen Text Burning (`text.camera_*_channel_title_text`):** Real-time text entity burned directly into the live video stream and recordings via `ChannelTitle.Name` (up to 64 characters). Update outside temperatures, alarm arming states, gate sensors, or weather notifications dynamically via automations.
* **OSD Visibility Toggles:** Dedicated switches to enable or hide the Channel Name (`switch.camera_*_channel_title_overlay`) and Timestamp Clock (`switch.camera_*_time_title_overlay`).

### 🕹️ PTZ Controls & Telemetry
* **Directional Movement:** Up, Down, Left, Right, and Zoom In / Zoom Out buttons with configurable speed and timed auto-stop to prevent runaway motion.
* **Preset Navigation:** Discover and recall hardware presets directly from `select.camera_*_ptz_preset`.
* **Hardware Coordinates:** State attributes expose real-time `pan`, `tilt`, `zoom`, and `move_status`.

### 🚨 Real-Time Push Events & IVS AI Analytics
* **Sub-Second HTTP Stream (`eventManager.cgi?action=attach`):** Persistent push connection delivering instant alerts with zero repeated logins:
  * Motion Detection (`binary_sensor.camera_*_motion`).
  * Video Loss / disconnection (`binary_sensor.camera_*_video_loss`).
  * Lens Tampering / masking (`binary_sensor.camera_*_tampering`).
  * IVS Tripwire / Linha Virtual (`binary_sensor.camera_*_linha_virtual`).
  * IVS Perimeter Intrusion / Cerca Virtual (`binary_sensor.camera_*_cerca_virtual`).
  * System Storage Failures, Missing Disk, and Low Space alerts.
  * Brute-Force Login Failure Alarm (`binary_sensor.dvr_*_login_failure`).

### 📊 System Health, Diagnostics & Diagnostics.py
* **Connected User Monitoring (`sensor.dvr_*_active_users`):** Real-time tracking of active sessions, client software (`Web3.0`, `Dahua3.0`, mobile apps), IP addresses, and login timestamps.
* **Storage & HDD Diagnostics:** S.A.R.T./hardware health detection (`binary_sensor.dvr_*_storage_failure`), partition health table attributes, total/free capacity, and an on-demand refresh button (`button.dvr_*_refresh_storage`).
* **Network Interface Telemetry:** Link speed (Mbit/s), MAC address, and physical link connectivity binary sensor (`binary_sensor.dvr_*_network_link`).
* **Official Home Assistant Diagnostics:** Integrated `diagnostics.py` supporting one-click JSON download under **Settings > Devices & Services > Intelbras DVR > 3 dots menu** (or per camera device) with automatic redaction of sensitive credentials and serial numbers.
* **Audit & Event Log Service:** Custom service `intelbras_dvr.get_logs` querying structured logs directly into Home Assistant.

### 🖥️ Physical Monitor Output Management (HDMI/VGA)
* **Dynamic Split Selection:** Dynamically queries `encode.cgi?action=getCaps` (`PreviewSplitNumList`) and `split.cgi` to generate split view options (`1x1`, `2x2`, `1+7`, `3x3`, `4x4`, `5x5`, `6x6`) matching your hardware.
* **AutoLogout Prevention:** Automatically keeps the physical HDMI/VGA display active 24/7 by disabling session timeout (`AutoLogout=0`).

### 🛡️ Enterprise Resilience & Authentication Stability
* **Serialized Digest Auth Queue:** Strict async mutex (`_request_lock`) eliminating race conditions, clearing stale CGI cookies, and automatically handling HTTP Digest nonce renegotiations.
* **Anti-Lockout Protection:** Replaces abrupt auth failures with non-blocking **Repairs** warnings and an escalating backoff cooldown, preventing DVR account lockouts.
* **Transient Grace Period:** Retains prior entity states across brief network blips without triggering false offline alerts.

---

## 📊 Hardware Compatibility

Verified models and operational capabilities:

| Model / Firmware | Live RTSP | Snapshots | Playback | Audio (go2rtc) | Push Events | Picture Controls | OSD Overlays | PTZ Controls | HDMI/VGA Split | System Diagnostics |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Intelbras iMHDX 3008**<br>`4.001.00IB001.0.T` | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ | ✅ |

*Compatible with Intelbras MHDX, iMHDX, NVD, and Dahua OEM DVR/NVR recorders running HTTP API V3.35.*

---

## 🌟 Entities & Platforms

### 🖥️ DVR Device Entities
| Platform | Entity Key | Category | Description |
| :--- | :--- | :--- | :--- |
| `sensor` | `dvr_status` | Diagnostic | Connection status (`online` / `offline`), firmware version, serial number |
| `sensor` | `monitor_mode` | Diagnostic | Active HDMI/VGA split mode with view and group metadata |
| `sensor` | `disk_usage` | Diagnostic | Hard drive capacity (`total_gb`, `free_gb`), usage percentage |
| `sensor` | `active_users` | Diagnostic | Count and session details of currently connected clients |
| `sensor` | `network_speed` | Diagnostic | Physical ethernet interface link speed (Mbit/s) |
| `binary_sensor` | `event_stream` | Diagnostic | Real-time HTTP push stream connectivity |
| `binary_sensor` | `storage_failure` | Diagnostic | Hardware disk failure / I/O error warning |
| `binary_sensor` | `storage_not_exist` | Diagnostic | Missing or disconnected storage disk warning |
| `binary_sensor` | `storage_low_space` | Diagnostic | Storage nearly full warning |
| `binary_sensor` | `login_failure` | Diagnostic | Brute force or failed login attempt warning |
| `binary_sensor` | `network_link` | Diagnostic | Physical ethernet cable link connectivity |
| `select` | `monitor_select` | Config | HDMI/VGA physical monitor split view selector |
| `button` | `dvr_reboot` | Config | Hardware reboot button for the DVR |
| `button` | `dvr_refresh` | Diagnostic | Refresh coordinator data on demand |
| `button` | `refresh_storage` | Diagnostic | Refresh HDD partition health telemetry |
| `update` | `firmware_update` | Config | Firmware update checker and cloud upgrade trigger |

### 🎥 Per-Camera Device Entities (Modular via Config & Options Flow)
| Platform | Entity Key | Category | Description |
| :--- | :--- | :--- | :--- |
| `camera` | `main_stream` | None | High-resolution RTSP stream (`subtype=0`) |
| `camera` | `sub_stream` | None | Low-bandwidth RTSP stream (`subtype=1`) |
| `camera` | `snapshot` | None | Still-image camera entity with configurable refresh interval |
| `binary_sensor` | `motion` | None | Real-time motion detection |
| `binary_sensor` | `video_loss` | Diagnostic | Real-time camera disconnection / video loss |
| `binary_sensor` | `video_blind` | Diagnostic | Real-time camera tampering / lens masking |
| `binary_sensor` | `cross_line` | None | IVS Tripwire detection |
| `binary_sensor` | `cross_region` | None | IVS Perimeter Intrusion detection |
| `select` | `period_config` | Config | Period selector (`active`, `period_1`, `period_2`) to view/edit parameters (Analog) |
| `select` | `profile_config` | Config | Profile selector (`active`, `day`, `night`, `regular`) to view/edit parameters (IP) |
| `select` | `light_compensation_mode` | Config | Light Compensation (`disabled`, `blc`, `hlc`, `wdr`) per profile |
| `select` | `day_night_mode` | Config | Day/Night mode (`black_white`, `color`, `schedule`) per profile (IP) |
| `select` | `profile_mode` | Config | Profile mode (`regular`, `fixed`, `schedule`, `day_night`) (IP) |
| `select` | `fixed_profile` | Config | Fixed profile selection (`day`, `night`) (IP) |
| `select` | `scene_mode` | Config | Picture style (`standard`, `soft`, `vivid`) (IP) |
| `select` | `record_mode` | Config | Recording schedule mode (`schedule`, `manual`, `off`) |
| `select` | `ptz_preset` | Config | Move camera to a discovered PTZ preset |
| `number` | `brightness`, `contrast`, `hue`, `saturation` | Config | Picture color adjustment sliders (`0`–`100`) per profile |
| `number` | `acutance` | Config | Video Adjustment / sharpness slider (`0`–`15`) per profile |
| `number` | `noise_reduction` | Config | 2D Noise Reduction level slider (`0`–`100`%) per profile |
| `number` | `image_gain` | Config | Image Gain level slider (`0`–`100`%) per profile |
| `switch` | `auto_noise_reduction` | Config | Toggle Automatic Noise Reduction (`GainAuto`) per profile |
| `switch` | `privacy_mask` | None | Black out or restore camera video feed |
| `switch` | `channel_title_overlay` | Config | Toggle channel name OSD overlay on video |
| `switch` | `time_title_overlay` | Config | Toggle timestamp clock OSD overlay on video |
| `switch` | `period_1_active`, `period_2_active` | Config | Enable or disable analog schedule periods |
| `text` | `channel_title_text` | Config | Dynamic OSD title text burned into video stream (up to 64 chars) |
| `time` | `day_start_time`, `day_end_time` | Config | Schedule day start and end times (IP) |
| `time` | `period_1_start_time`, `period_1_end_time` | Config | Period 1 schedule times (Analog) |
| `time` | `period_2_start_time`, `period_2_end_time` | Config | Period 2 schedule times (Analog) |
| `button` | `camera_reset_defaults` | Config | Reset picture defaults for the active or selected profile |
| `button` | `ptz_up`, `ptz_down`, `ptz_left`, etc. | None | PTZ directional and zoom movement buttons |

---

### 💡 Dynamic OSD Text Automation Example

```yaml
automation:
  - alias: "Update camera OSD with outside temperature"
    trigger:
      - platform: state
        entity_id: sensor.outside_temperature
    action:
      - action: text.set_value
        target:
          entity_id: text.camera_channel_1_channel_title_text
        data:
          value: "Temp: {{ states('sensor.outside_temperature') }}°C"
```

---

## 🔐 Prerequisites: Dedicated DVR User & Permissions (Recommended)

Before adding the integration, it is **strongly recommended** to create a dedicated user account specifically for Home Assistant on your Intelbras DVR (**System > Account > User**):

1. **Why use a dedicated user?**
   * Prevents session conflicts and temporary lockouts when personal accounts (`admin`) are accessed simultaneously from mobile apps (Intelbras iSIC / Guardian) or web browsers.
   * Keeps DVR audit logs clean.
   * Enable the **Reusable** (*Reutilizável*) option on the user account if available on your firmware.

2. **Required Permissions:**
   Assign the user to the **`admin`** group (or a custom group with the following permissions enabled):
   * **Live View / Monitor** & **Playback / Search**: Live RTSP feeds, snapshots, and Media Browser playback.
   * **PTZ Control**: Move cameras and trigger presets.
   * **System / Encode / Camera / Record / Display Settings**: Picture controls, OSD overlays, privacy mask, recording modes, and HDMI/VGA monitor layout.
   * **System Info / Storage / Maintenance**: Disk health diagnostics and reboot.

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

## ⚙️ Configuration, Options & Maintenance

1. In Home Assistant, navigate to **Settings > Devices & Services > Add Integration**.
2. Search for **Intelbras DVR**.
3. **Step 1 - Connection:** Enter host/IP address, username, password, HTTP port (default `80`), and RTSP port (default `554`).
4. **Step 2 - Camera Channels:** The integration automatically discovers all active channels, names, and PTZ capabilities. Select the cameras you want to import.
5. **Step 3 - Feature Selection:** Enable the desired feature sets (`main_stream`, `sub_stream`, `snapshot`, `motion`, `video_color`, `profiles`, `overlay`, `ptz`, `recording`, etc.) and system monitoring options.

> 💡 **Options Flow:** Click **Configure** on the integration card at any time to add or remove camera channels and feature sets without recreating entities.
>
> 🔧 **Reconfigure Flow:** Use **Reconfigure** from the card's menu to update host, ports, or credentials without losing entity history.

---

## 🛠️ Services

| Service | Description | Parameters |
|---|---|---|
| `intelbras_dvr.set_video_color` | Adjust picture settings on a camera channel | `channel` (`1`–`32`), `brightness`, `contrast`, `hue`, `saturation`, `acutance` (`0`–`100`), `profile` (`0`=Day, `1`=Night, `2`=Normal) |
| `intelbras_dvr.mask_camera` | Black out or restore camera video feed | `channel` (`1`–`32`), `action` (`mask` / `unmask`) |
| `intelbras_dvr.set_monitor_split` | Change HDMI/VGA physical monitor split mode | `mode` (`split1`, `split4`, `split8`, `split9`, `split16`, etc.), `group`, `channel` |
| `intelbras_dvr.record_camera` | Switch camera to manual recording for a duration | `channel` (`1`–`32`), `duration` (`5`–`3600` seconds) |
| `intelbras_dvr.ptz_move` | Move PTZ camera in a direction with timed stop | `channel` (`1`–`32`), `direction`, `speed` (`1`–`8`), `duration` (`0.1`–`10.0`s) |
| `intelbras_dvr.ptz_stop` | Stop ongoing PTZ movement | `channel` (`1`–`32`), `direction` |
| `intelbras_dvr.ptz_preset` | Move PTZ camera to a saved preset | `channel` (`1`–`32`), `preset` (`1`–`255`) |
| `intelbras_dvr.get_logs` | Query structured audit and system event logs | `count` (`1`–`100`), `log_type` |
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
