# EarCam

Python client for the **Soulear ear inspection camera**. Replaces the proprietary Android app with a Linux command-line viewer that displays the live video stream in an OpenCV window and optionally saves frames to disk.

## Hardware

| Field | Value |
|---|---|
| Device | Soulear-xxxxx |
| Chipset | Taixin Semi TXW816-810 |
| WiFi | 802.11b/g/n, 2.4 GHz, channel 7 |
| Camera IP | 192.168.1.1 |
| Security | Open (no password) |

The camera acts as a WiFi Direct Group Owner/soft AP. Your machine connects to it as a client and receives the video stream over UDP.

From what it dumps over its GetDeviceInfo interface (cmd=0x0001), it seems to be a rebrand of a Beken BK7231U WiFi camera, running BK7231U-XRH-FBPRO firmware. BK7231U is a WiFi/BLE MCU that supports SoftAP and STA mode access.

This is similar to a Taixen TXW816-810 product, a tear-down of which can be found at: https://www.elektroda.com/news/news4129331.html. In this tear-down, a log of the UART output indicates that it is using hgSDK-v2.5.0.7 (downloadable from Taixen), BK7231U-XRH-FBPRO, a HI708 sensor at 480×480, and an AP named Soulear-ae45b with DHCP from 192.168.1.10. They used an STM32 Blue Pill acting as CKLink to dump the 1MB flash (in chunks, at 1200KHz). The features include: streaming video, enable/disable the LED, switch between left/right ears, switch wide/focused lens, switch between "horizontal" and "mirror" mode video. They found a 21-pin connector for the camera and LEDs, a 2.7V 170mAh battery, and a PCB with the following (labeled) pins: 3.3V (from MCU), 5V (from USB, not shared ground with MCU), CHIP_EN, DP (PC6, UART Tx), CLK (PA10, TCLK), TMS (PA9), USB-DET? (PA8). The UART output logs all actions taken on startup and when running the camera from the Android app.

## Setup

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
```

## Usage

Connect your machine to the **Soulear-xxxxx** WiFi network first (open network, no password), then:

```bash
# Verify the camera responds and print device info
python earcam.py --info

# Live video in an OpenCV window
python earcam.py

# Live video + save every frame as a JPEG
python earcam.py --save ./frames

# Non-default camera IP
python earcam.py --camera 192.168.1.1
```

Keys in the video window:

| Key | Action |
|---|---|
| `l` | Turn the camera light on/off (the light is turned on when streaming starts, as the Android app does) |
| `m` | Mirror the image horizontally; this is what the app's left ear/right ear switch does |
| `u` | Keep the image upright as the camera rotates, using the camera's accelerometer (on by default) |
| `q` | Quit |

The frame rate and current light, mirror and upright state are shown in the top-left corner of the window. Saved frames include the mirroring and rotation but not the status text. When the camera points straight up or down, the rotation stays at its last value.

If no valid frame arrives for 0.75 seconds, the window marks the image **NO LIVE VIDEO** rather than presenting a frozen image as live. The window and quit controls remain responsive while waiting for video. Every five seconds, the terminal reports the frame rate and cumulative counts of incomplete frames, JPEG decode errors, and older queued frames replaced by newer ones.

Example `--info` output:

```
Querying device info...
Device  : Soulear Soulear-1dab0 1.0.0
SSID    : Soulear-1dab0
Battery : 85%
```

## Helper scripts (`bin/`)

These run from any directory. They use the Wi-Fi interface `wlp0s20f3` unless you set `WIFI_DEV`, e.g. `WIFI_DEV=wlan0 bin/connectWifi.sh`.

| Script | Purpose |
|---|---|
| `connectWifi.sh [SSID]` | Connect to the camera. With no SSID, it picks the first visible `Soulear-*` network |
| `monitorWifi.sh [channel]` | Put the Wi-Fi into monitor mode on a channel (default 4) to sniff the Android app's traffic. This takes the machine off Wi-Fi |
| `captureWifi.sh [file.pcap]` | Record everything on that channel (default `traces/capture_<date>_<time>.pcap`); stop with Ctrl-C. Needs monitor mode, and never overwrites an existing file |
| `resetWifi.sh` | Put the Wi-Fi back into normal mode after a capture |
| `refreshVenv.sh` | Rebuild `venv` from scratch, e.g. after a system Python upgrade breaks it. Activate it afterwards with `source venv/bin/activate` |

Because the camera's network is open, monitor mode sees the phone's traffic in plain text. A typical capture is `monitorWifi.sh`, then `captureWifi.sh` while using the app, then `resetWifi.sh` and `connectWifi.sh`.

## Protocol exploration

`probe.py` sweeps a range of command types, reports which ones respond, and optionally writes specific payloads — useful for reverse-engineering the command set.

```bash
# Sweep the default range (0x0000–0x001f) with empty payloads
python probe.py

# Retry timed-out commands with specific single-byte payloads
python probe.py --retry 00 01

# Write a specific payload to every address in a range
python probe.py --start 0x000a --end 0x000a --write 01

# Write a multi-byte payload
python probe.py --start 0x000a --end 0x000a --write 01 02 03

# Sweep a wider range, skipping a known-dangerous address
python probe.py --start 0x0000 --end 0x00ff --skip 0x0008

# Force sending a payload to a dangerous address (use with care)
python probe.py --start 0x0008 --end 0x0008 --write 00 --force
```

`--write` and `--retry` are mutually exclusive: `--write` always sends exactly the given payload; `--retry` sends an empty payload first and only falls back to the given payloads on timeout.

Commands in the built-in dangerous list are skipped automatically whenever a non-empty payload would be sent, unless `--force` is given:

| Type | Risk |
|---|---|
| 0x0008 | Any non-empty payload powers off the camera |

## Protocol

The camera uses a custom UDP protocol with magic bytes `0xffeeffee`.

### Ports

| Port | Direction | Purpose |
|---|---|---|
| 10005 UDP | client → camera | Command/control |
| 10006 UDP | client → camera | Stream init (OpenVideo command only) |
| 10007 UDP | camera → client | Unsolicited heartbeat/status push from camera |
| 22789 UDP | camera → client | JPEG chunk stream |

### Command frame (12 bytes, little-endian)

```
Offset  Size  Field       Notes
0       4     magic       always 0xffeeffee
4       2     id          increments with each request/response pair
6       2     type        see command table below
8       1     unk         always 0x01 in requests
9       1     err_code    0=success
10      2     length      payload byte count
12      …     payload
```

### Known command types

Discovered by probing with `probe.py`. err=2 = invalid/wrong port; err=255 = not implemented.

| Type | Port | Direction | Name | Sent payload | Response payload | Notes |
|---|---|---|---|---|---|---|
| 0x0000 | 10005 | client→cam | — | none | none | err=2 |
| 0x0001 | 10005 | client→cam | GetDeviceInfo | none | 128-byte device info struct | See layout below |
| 0x0002 | 10005 | client→cam | ? | none | 180 zero bytes | Purpose unknown |
| 0x0003 | 10005 | client→cam | — | none | none | err=255 (not implemented) |
| 0x0004 | 10006 | client→cam | OpenVideo | none, or u16 port + 2 zero bytes | none | err=2 if sent to port 10005. The payload is the client's receive port (the app sends e.g. `9d 9f 00 00` = 40861; earcam.py sends 22789). With no payload the camera acknowledges, but after the app had used it, it sent no video |
| 0x0005 | 10005 | client→cam | — | none | none | err=2 |
| 0x0006 | 10005 | client→cam | ? | none | 2 bytes (0x0004) | Possibly GetResolution or GetMode |
| 0x0007 | 10005 | client→cam | ? | none | none | Returns ok; purpose unknown |
| 0x0008 | 10005 | client→cam | PowerOff? | none | none | **DANGEROUS**: any non-empty payload powers off camera |
| 0x0009 | 10007 | cam→client | Heartbeat | — | 17 bytes | Camera pushes every ~2 s; not request/response |
| 0x000a | 10005 | client→cam | SetLed | on: `11 01 64`, off: `11 00 00` | echo of the request | From an Android app capture; the app sends "on" when it connects. Shorter payloads time out |
| 0x000b–0x001f | 10005 | client→cam | — | — | — | No response to empty or single-byte payloads |

### GetDeviceInfo response payload (128 bytes, offsets from payload start)

```
Offset  Size  Field        Notes
0       1     unk0
1       32    vendor       null-terminated ASCII
33      32    product_id   null-terminated ASCII
65      16    fw_version   null-terminated ASCII
81      32    ssid         null-terminated ASCII
113     4     unk32
117     2     unk16
119     2     power_info   bits[15:9] = battery %; bit[0] = charging flag
121     1     capacity
```

### Heartbeat push (type 0x0009, camera → client port 10007)

The camera sends these unsolicited every ~2 seconds while streaming. The client does not need to respond. Payload is 17 bytes; contents not yet decoded.

### Stream chunk frame (16-byte header + JPEG data)

```
Offset  Size  Field         Notes
0       1     unk1          always 0x01
1       1     n_chunk       global chunk counter (8-bit, rolls over at 256); NOT reset per frame
2       1     n_frame       frame counter (8-bit, rolls over at 256)
3       1     last_chunk    0x01 on the final chunk of a frame
4       1     total_chunks  total chunk count for this frame; present on all chunks, not just the last
5       1     unk5
6       4     accel         3-axis accelerometer (see below)
10      2     mac_tail      last two bytes of the camera's MAC address (85 b4 for 48:78:86:0f:85:b4); constant
12      2     res_width     frame width in pixels
14      2     res_height    frame height in pixels
16      …     JPEG data     partial JPEG; reassemble all chunks to get one JPEG image
```

### Connection sequence

1. Client sends **GetDeviceInfo** (type `0x0001`) to port 10005 → camera responds with device metadata
2. Client binds UDP port 22789 with an increased receive buffer
3. Client sends **OpenVideo** (type `0x0004`) to port 10006 → camera starts streaming to the already-listening socket
4. Camera begins pushing **Heartbeat** (type `0x0009`) to client port 10007 every ~2 s
5. Chunks with the same `n_frame` are reassembled into a complete JPEG and displayed

### Frame reassembly notes

- `n_chunk` is a **global** monotonically increasing counter — it does not reset to 0 at the start of each frame. Frame boundaries come from `n_frame` changing.
- Both `n_chunk` and `n_frame` are 8-bit and roll over at 256. The chunk starting with the JPEG SOI marker (`ff d8`) anchors the order, so packets can arrive out of order, including across rollover.
- `total_chunks` is set to the frame's total chunk count on **every** chunk (not just the last). A frame is complete only when all expected consecutive chunk IDs have arrived. Duplicate packets do not count twice.
- Partial frames expire after 0.5 seconds. The display queue retains only the newest completed frame to avoid accumulating playback latency.
- `last_chunk` is `0x01` only on the final chunk of a frame; it is redundant with `total_chunks` but useful as a fast boundary check.

### Accelerometer (chunk header offsets 6–9)

Read the 4 bytes as a little-endian u32. It holds three 10-bit **sign-magnitude** fields (a 9-bit magnitude followed by a sign bit), at about 64 counts per g:

```
Bits    Field
0–8     X magnitude   (image plane)
9       X sign        (1 = negative)
10–18   Y magnitude   (image plane)
19      Y sign
20–28   Z magnitude   (along the lens: +64 pointing straight up, −64 straight down)
29      Z sign
30–31   unused (0)
```

The camera's roll is `atan2(X, Y)`. The scene in the image turns by the same angle, so rotating the image counter-clockwise by `atan2(X, Y) + 90°` (in degrees, as with `cv2.getRotationMatrix2D`) keeps it upright. The 90° offset reflects how the sensor is mounted relative to the image and was set by checking the live picture. This was decoded from a capture of a full turn plus a guided test at known angles. Across about 8,000 chunks the magnitude averaged 65 (1 g).

### Left/right ear and keeping upright in the Android app

Neither sends a command to the camera. The app mirrors the image itself for left/right ear, and rotates it itself to keep it upright.

## Files

```
earcam.py        Main client — connect, stream, display
test_earcam.py   Offline streaming and GUI regression tests
probe.py         Command probe/write tool for reverse-engineering the protocol
requirements.txt opencv-python, numpy
bin/             Wi-Fi, capture and venv helper scripts (see above)
```

## Troubleshooting

**`ERROR: No response from camera`** — confirm you are connected to the Soulear WiFi and that your IP is in the 192.168.1.x range (`ip addr`).

**`NO LIVE VIDEO`** — no valid frame has arrived recently. Check the camera is on, the WiFi is connected, and nothing else (e.g., the phone app) is using the camera; it may only stream to one client.

**Corrupted or missing frames** — the camera sends on channel 7; nearby interference can cause packet loss. Move closer to the camera.

## Tests

With the dependencies installed, run:

```bash
python -m unittest -v test_earcam
```

Tests use synthetic JPEGs and mocked sockets and GUI calls; no camera, WiFi connection, or display server is required.
