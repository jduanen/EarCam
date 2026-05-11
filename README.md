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

# Live video in an OpenCV window (press q to quit)
python earcam.py

# Live video + save every frame as a JPEG
python earcam.py --save ./frames

# Non-default camera IP
python earcam.py --camera 192.168.1.1
```

Example `--info` output:

```
Querying device info...
Device  : Soulear Soulear-1dab0 1.0.0
SSID    : Soulear-1dab0
Battery : 85%
```

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
| 0x0004 | 10006 | client→cam | OpenVideo | none | none | err=2 if sent to port 10005 |
| 0x0005 | 10005 | client→cam | — | none | none | err=2 |
| 0x0006 | 10005 | client→cam | ? | none | 2 bytes (0x0004) | Possibly GetResolution or GetMode |
| 0x0007 | 10005 | client→cam | ? | none | none | Returns ok; purpose unknown |
| 0x0008 | 10005 | client→cam | PowerOff? | none | none | **DANGEROUS**: any non-empty payload powers off camera |
| 0x0009 | 10007 | cam→client | Heartbeat | — | 17 bytes | Camera pushes every ~2 s; not request/response |
| 0x000a | 10005 | client→cam | SetLed | ? | — | Timed out with empty, 0x00, and 0x01; payload format unknown |
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
6       2     pos_x         possibly accelerometer/orientation data
8       2     pos_y
10      2     pos_z
12      2     res_width     frame width in pixels
14      2     res_height    frame height in pixels
16      …     JPEG data     partial JPEG; reassemble all chunks to get one JPEG image
```

### Connection sequence

1. Client sends **GetDeviceInfo** (type `0x0001`) to port 10005 → camera responds with device metadata
2. Client sends **OpenVideo** (type `0x0004`) to port 10006 → camera starts streaming
3. Client binds UDP port 22789 and receives JPEG chunk packets
4. Camera begins pushing **Heartbeat** (type `0x0009`) to client port 10007 every ~2 s
5. Chunks with the same `n_frame` are reassembled into a complete JPEG and displayed

### Frame reassembly notes

- `n_chunk` is a **global** monotonically increasing counter — it does not reset to 0 at the start of each frame. Frame boundaries come from `n_frame` changing.
- Both `n_chunk` and `n_frame` are 8-bit and roll over at 256. Chunk ordering within a frame uses `(n_chunk - first_chunk_seen) % 256`.
- `total_chunks` is set to the frame's total chunk count on **every** chunk (not just the last). Frame completion is detected when the number of received chunks equals `total_chunks`.
- `last_chunk` is `0x01` only on the final chunk of a frame; it is redundant with `total_chunks` but useful as a fast boundary check.

## Files

```
earcam.py        Main client — connect, stream, display
probe.py         Command probe/write tool for reverse-engineering the protocol
requirements.txt opencv-python, numpy
```

## Troubleshooting

**`ERROR: No response from camera`** — confirm you are connected to the Soulear WiFi and that your IP is in the 192.168.1.x range (`ip addr`).

**`Waiting for frames...`** — the OpenVideo handshake succeeded but no chunks are arriving. Check that nothing else (e.g., the phone app) is connected to the camera at the same time; the camera may only stream to one client.

**Corrupted or missing frames** — the camera sends on channel 7; nearby interference can cause packet loss. Move closer to the camera.
