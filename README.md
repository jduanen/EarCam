# EarCam

**WIP** Video not working

Python client for the **Soulear ear inspection camera**. Replaces the proprietary Android app with a Linux command-line viewer that displays the live video stream in an OpenCV window and optionally saves frames to disk.

## Hardware

| Field | Value |
|---|---|
| Device | Soulear-xxxxx |
| Chipset | Taixin Semi TXW816-810 |
| WiFi | 802.11b/g/n, 2.4 GHz, channel 7 |
| Camera IP | 192.168.1.1 |
| Security | Open (no password) |

The camera acts as a WiFi Direct Group Owner / soft AP. Your machine connects to it as a client and receives the video stream over UDP.

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

## Protocol

The camera uses a custom UDP protocol with magic bytes `0xffeeffee`.

### Ports

| Port | Direction | Purpose |
|---|---|---|
| 10005 UDP | client → camera | Command/control (GetDeviceInfo, SetLed, etc.) |
| 10006 UDP | client → camera | Stream init (OpenVideo command) |
| 22785 UDP | camera → client | JPEG chunk stream |

### Command frame (12 bytes, little-endian)

```
Offset  Size  Field       Notes
0       4     magic       always 0xffeeffee
4       2     id          increments with each request/response pair
6       2     type        0x0001=GetDeviceInfo  0x0004=OpenVideo  0x000a=SetLed
8       1     unk         always 0x01 in requests
9       1     err_code    0=success
10      2     length      payload byte count
12      …     payload
```

### Stream chunk frame (16-byte header + up to 1456 bytes JPEG data)

```
Offset  Size  Field         Notes
0       1     unk1          always 0x01
1       1     n_chunk       chunk index within frame (8-bit, rolls over at 256)
2       1     n_frame       frame counter (8-bit, rolls over at 256)
3       1     last_chunk    0x01 on the final chunk of a frame
4       1     total_chunks  0 on all non-final chunks; = total chunk count on last
5       1     unk5
6       6     position      three uint16 fields (X/Y/Z, possibly orientation)
12      2     res_width     frame width in pixels
14      2     res_height    frame height in pixels
16      …     JPEG data     partial JPEG; reassemble all chunks to get one JPEG image
```

### Connection sequence

1. Client sends **GetDeviceInfo** (type `0x0001`) to port 10005 → camera responds with device metadata
2. Client sends **OpenVideo** (type `0x0004`) to port 10006 → camera starts streaming
3. Client binds UDP port 22785 and receives JPEG chunk packets
4. Chunks with the same `n_frame` are reassembled in `n_chunk` order into a complete JPEG
5. Each complete JPEG is one video frame

### Frame reassembly notes

- `n_chunk` and `n_frame` are both 8-bit and roll over at 256
- Chunk order uses `(n_chunk - first_chunk) % 256` to handle rollover correctly
- `total_chunks` is nonzero only on the last chunk of a frame; it gives the total chunk count, which is used to detect frame completion

## Files

```
earcam.py        Main client — connect, stream, display
requirements.txt opencv-python, numpy
```

## Troubleshooting

**`ERROR: No response from camera`** — confirm you are connected to the Soulear-830bb WiFi and that your IP is in the 192.168.1.x range (`ip addr`).

**`Waiting for frames...`** — the OpenVideo handshake succeeded but no chunks are arriving. Check that nothing else (e.g., the phone app) is connected to the camera at the same time; the camera may only stream to one client.

**Corrupted or missing frames** — the camera sends on channel 7; nearby interference can cause packet loss. Move closer to the camera.
