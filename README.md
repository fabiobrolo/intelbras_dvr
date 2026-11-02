# Intelbras DVR for Home Assistant

<p align="center">
  <img src="https://raw.githubusercontent.com/fabiobrolo/intelbras_dvr/main/custom_components/intelbras_dvr/brand/logo.png" alt="Intelbras DVR" width="300"/>
</p>

[![HACS Custom](https://img.shields.io/badge/HACS-Custom-orange.svg)](https://github.com/hacs/default)
[![Version](https://img.shields.io/badge/version-0.3.4-blue.svg)](https://github.com/fabiobrolo/intelbras_dvr/releases)
[![Home Assistant](https://img.shields.io/badge/home--assistant-2024.4%2B-brightgreen.svg)](https://www.home-assistant.io)

[🇺🇸 English](README.md) | [🇧🇷 Português](README.pt-BR.md)

> ⚠️ **Disclaimer**: Intelbras® and Dahua® are registered trademarks of their respective owners. This is an unofficial community integration without any affiliation, endorsement, or support from Intelbras S.A. or Dahua Technology.

---

High-performance custom integration for **Home Assistant** dedicated to **Intelbras** DVRs and NVRs (and compatible Dahua-based devices). 100% local communication via HTTP CGI APIs and RTSP video streams, with zero cloud dependency.

Designed following modern Home Assistant standards (HA 2024.4+ / 2026.x / Python 3.14), featuring `runtime_data`, real-time push event stream, complete multi-language localization (English and Brazilian Portuguese), non-blocking event loop execution, dynamic audio transcoding, and recording playback in the Media Browser with live preview thumbnails.

---

## 🚀 Recent Improvements & Architectural Upgrades (v0.3.4)

1. **100% Dynamic Hardware & Configuration Discovery:**
   * Automatically queries DVR CGI (`Encode`, `RemoteDevice`, `VideoIn`, `ChannelTitle`, `ptz.cgi?action=getPresets`) to dynamically detect active analog and IP channels, PTZ capability, audio codecs, and video profiles.
   * Dynamically discovers `go2rtc` URL from Home Assistant runtime/configuration when audio transcoding is needed.
   * Zero hardcoded channel numbers, camera names, or IP addresses.

2. **Adaptive Hybrid Playback Engine:**
   * **Channels without Audio:** Streamed directly from the DVR over RTSP via native Home Assistant HLS with 0% extra CPU consumption and zero latency.
   * **Channels with Audio (e.g. Video Doorbell, cameras with built-in microphone or coaxial HDCVI audio):** Dynamically registered with `go2rtc` on demand using `#video=copy#audio=aac#async`. AAC transcode ensures audio playback in modern browsers and mobile apps without audio stutter or desync. The transient stream is automatically cleaned up when playback ends.

3. **Smart Channel Discovery & Unassigned Slot Filtering:**
   * Batch queries `ChannelTitle`, `VideoIn`, and `RemoteDevice` during `config_flow`, `options_flow`, and `async_setup_entry`.
   * Unassigned IP channel slots (such as factory-default empty slots with `RemoteDevice.Enable=false` and `SignalFormats=UNKNOWN`) are automatically filtered out, preventing empty channels from appearing in Home Assistant.

4. **100% Non-Blocking Asyncio Event Loop Compliance:**
   * Eliminated all synchronous file open operations from the main asyncio event loop.
   * Diagnostic logging routed through standard Home Assistant logging facilities (`_LOGGER.debug`) and disk thumbnail cache accesses strictly delegated to the executor thread pool (`async_add_executor_job`).

5. **Full Translation Parity:**
   * 100% key parity across `strings.json`, `translations/en.json`, `translations/pt.json`, and `translations/pt-BR.json` covering all entity titles, states, options, selectors, errors, and services.

---

## 📊 Tested Hardware Compatibility Matrix

The table below lists verified hardware models, firmware versions, and their operational feature status:

| Model / Firmware | Live RTSP (Main / Sub) | Snapshots (CGI / RTSP) | Recording Playback (HLS) | Dynamic AAC Audio (go2rtc) | Recording Thumbnails | Real-Time Push Events | Picture Controls | Privacy Mask | HDMI/VGA Split Monitor | PTZ Controls | Diagnostics & Reboot |
| :--- | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: | :---: |
| **Intelbras iMHDX 3008**<br>`4.001.00IB001.0.T` | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported | ✅ Supported |

---

## 🌟 Key Features Overview

### 🎥 Cameras & Video Streams
* **Main Stream:** High-resolution stream for recording and detailed surveillance.
* **Sub Stream:** Low-resolution stream (`sub_stream`) designed for dashboard overviews and low-bandwidth connections.
* **Snapshot Entity:** Dedicated still-image camera entity with configurable refresh intervals.
* **Media Browser Integration:** Browse and watch past recordings directly in Home Assistant with native scrubbing.

### ⚡ Instant Real-Time Events (Push Stream)
* Persistent HTTP event connection delivering motion and alarm events with sub-second latency (< 100ms) without polling overhead.
* Binary sensors for **Motion**, **Video Loss**, **Video Tampering**, and **Event Stream connectivity**.

### 🎛️ Picture Controls & Privacy
* Fine-grained picture adjustments per channel.
* Privacy mask switch to blank out cameras when home.
* Defaults reset button.

### 🕹️ PTZ Controls & Presets
* Directional movement buttons with automatic stop duration.
* Instant preset selector.

### 🖥️ Physical Monitor Output (HDMI/VGA)
* Change monitor split view on the physical display connected to the DVR.

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

## ⚙️ Configuration

1. In Home Assistant, navigate to **Settings > Devices & Services > Add Integration**.
2. Search for **Intelbras DVR**.
3. **Step 1 - Connection:** Enter host/IP address, username, password, HTTP port (default `80`), and RTSP port (default `554`).
4. **Step 2 - Camera Channels:** The integration detects all active channels and excludes unassigned empty slots. Select which cameras to import.
5. **Step 3 - Feature Selection:** Choose the platforms and features to enable for the selected cameras (Main Stream, Sub Stream, Snapshot, Motion, Picture Controls, PTZ, etc.).

> 💡 **Reconfiguration:** Click **Configure** on the integration card at any time to add/remove cameras or update features.

---

## 🛠️ Services

| Service | Description | Parameters |
|---|---|---|
| `intelbras_dvr.set_video_color` | Adjust picture settings on a camera channel | `channel`, `brightness`, `contrast`, `hue`, `saturation`, `acutance`, `profile` |
| `intelbras_dvr.mask_camera` | Black out or restore camera video | `channel`, `action` (`mask` / `unmask`) |
| `intelbras_dvr.set_monitor_split` | Change HDMI/VGA physical monitor split mode | `mode` (`split1`, `split4`, `split8`, `split9`, `split16`), `group`, `channel` |
| `intelbras_dvr.record_camera` | Temporarily switch camera to continuous manual recording | `channel`, `duration` (seconds) |
| `intelbras_dvr.ptz_move` | Move PTZ camera in a direction with timed stop | `channel`, `direction`, `speed`, `duration` |
| `intelbras_dvr.ptz_stop` | Stop ongoing PTZ movement | `channel`, `direction` |
| `intelbras_dvr.ptz_preset` | Move PTZ camera to a saved preset | `channel`, `preset` |
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

