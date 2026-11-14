#!/usr/bin/env python3
"""EarCam: Python client for Soulear ear inspection camera

Camera acts as a WiFi Direct Group Owner / soft AP.
Connect your machine to the "Soulear-*****" open WiFi network first.

Got help with protocol reverse-engineering from ~/Code2/Suear-Web-Viewer (Sean Pesce).
"""

import argparse
import math
import os
import queue
import socket
import struct
import threading
import time
from collections import deque

import cv2
import numpy as np


CAMERA_IP = '192.168.1.1'
CMD_PORT = 10005
STREAM_INIT_PORT = 10006
STREAM_RECV_PORT = 22789
MAGIC = 0xffeeffee

# Command header: magic(u32) id(u16) type(u16) unk(u8) err_code(u8) length(u16)
CMD_HDR_FMT = '<IHHBBH'
CMD_HDR_SZ = struct.calcsize(CMD_HDR_FMT)   # 12

# Stream chunk header: unk1 n_chunk n_frame last_chunk total_chunks unk5 x y z width height
CHUNK_HDR_FMT = '<BBBBBBHHHHH'
CHUNK_HDR_SZ = struct.calcsize(CHUNK_HDR_FMT)  # 16


def _build_cmd(msg_id: int, msg_type: int, payload: bytes = b'') -> bytes:
    return struct.pack(CMD_HDR_FMT, MAGIC, msg_id, msg_type, 1, 0, len(payload)) + payload


def _parse_cmd_response(data: bytes) -> dict:
    magic, msg_id, msg_type, unk, err_code, length = struct.unpack_from(CMD_HDR_FMT, data)
    return {
        'magic': magic,
        'type': msg_type,
        'err_code': err_code,
        'payload': data[CMD_HDR_SZ: CMD_HDR_SZ + length],
    }


def _parse_chunk_hdr(data: bytes) -> dict:
    _, n_chunk, n_frame, last_chunk, total_chunks, _, px, py, pz, res_w, res_h = \
        struct.unpack_from(CHUNK_HDR_FMT, data)
    return {
        'n_chunk': n_chunk,
        'n_frame': n_frame,
        'total_chunks': total_chunks,
        'res_width': res_w,
        'res_height': res_h,
        'accel': _parse_accel(data[6:10]),
    }


def _parse_accel(b: bytes) -> tuple[int, int, int]:
    """Decode the accelerometer packed into chunk-header bytes 6-9.

    Three 10-bit sign-magnitude fields (9-bit magnitude, then a sign bit), about 64 counts per g.
    X and Y lie in the image plane; Z points along the lens.
    """
    v = int.from_bytes(b, 'little')

    def axis(off: int) -> int:
        mag = (v >> off) & 0x1ff
        return -mag if (v >> (off + 9)) & 1 else mag

    return axis(0), axis(10), axis(20)


def _parse_device_info(data: bytes) -> dict:
    # Offsets (no padding, _pack_=1):
    # 0: unk0(1) 1: vendor(32) 33: product_id(32) 65: fw_version(16)
    # 81: ssid(32) 113: unk32(4) 117: unk16(2) 119: power_info(2) 121: capacity(1)
    if len(data) < 122:
        return {}
    vendor = data[1:33].split(b'\x00', 1)[0].decode('ascii', errors='replace')
    product_id = data[33:65].split(b'\x00', 1)[0].decode('ascii', errors='replace')
    fw_version = data[65:81].split(b'\x00', 1)[0].decode('ascii', errors='replace')
    ssid = data[81:113].split(b'\x00', 1)[0].decode('ascii', errors='replace')
    power_info = struct.unpack_from('<H', data, 119)[0]
    capacity = data[121]
    return {
        'vendor': vendor,
        'product_id': product_id,
        'fw_version': fw_version,
        'ssid': ssid,
        'battery_pct': power_info >> 9,
        'charging': bool(((power_info << 0x17) & 0xffffffff) >> 0x1f),
        'capacity': capacity,
    }


class JpgFrame:
    """Reassembles a single JPEG frame from UDP chunks.

    Anchor chunk ordering at the JPEG start, not the first packet to arrive.
    Wi-Fi can deliver packets out of order, including across the 8-bit rollover.
    """

    def __init__(self, n_frame: int):
        self.n_frame = n_frame
        self.chunks: dict[int, bytes] = {}
        self.total: int | None = None
        self._first_chunk: int | None = None
        self.created_at = time.monotonic()
        self.width = 0
        self.height = 0
        self.accel = (0, 0, 0)

    def add_chunk(self, n_chunk: int, chunk_data: bytes, total_chunks: int,
                  width: int = 0, height: int = 0, accel: tuple[int, int, int] = (0, 0, 0)):
        if chunk_data.startswith(b'\xff\xd8'):
            self._first_chunk = n_chunk
        if not self.chunks:
            self.width = width
            self.height = height
            self.accel = accel
        self.chunks[n_chunk] = bytes(chunk_data)
        if total_chunks:
            self.total = total_chunks

    @property
    def complete(self) -> bool:
        if self.total is None or self._first_chunk is None or len(self.chunks) != self.total:
            return False
        return all((self._first_chunk + i) % 256 in self.chunks for i in range(self.total))

    @property
    def data(self) -> bytes:
        if not self.complete:
            return b''
        first = self._first_chunk
        return b''.join(self.chunks[(first + i) % 256] for i in range(self.total))


class SoulearClient:
    _MAX_FRAME_SLOTS = 8
    _MAX_FRAME_AGE = 0.5
    _SOCK_TIMEOUT = 5.0
    _FRAME_QUEUE_SZ = 1

    def __init__(self, camera_ip: str = CAMERA_IP):
        self.camera_ip = camera_ip
        self._msg_id = 0
        self._cmd_sock: socket.socket | None = None
        self._stream_sock: socket.socket | None = None
        self._frame_queue: queue.Queue[JpgFrame] = queue.Queue(maxsize=self._FRAME_QUEUE_SZ)
        self._stop = threading.Event()
        self.stats = {'packets': 0, 'frames': 0, 'incomplete': 0, 'queue_drops': 0,
                      'decode_errors': 0}

    def _next_id(self) -> int:
        self._msg_id = (self._msg_id + 1) & 0xffff
        return self._msg_id

    def _send_cmd(self, msg_type: int, payload: bytes = b'', port: int = CMD_PORT,
                  sock: socket.socket | None = None) -> dict:
        own_sock = sock is None
        if own_sock:
            if self._cmd_sock is None:
                self._cmd_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
                self._cmd_sock.settimeout(self._SOCK_TIMEOUT)
            sock = self._cmd_sock
        pkt = _build_cmd(self._next_id(), msg_type, payload)
        sock.sendto(pkt, (self.camera_ip, port))
        resp_data, _ = sock.recvfrom(4096)
        return _parse_cmd_response(resp_data)

    def get_device_info(self) -> dict:
        resp = self._send_cmd(0x0001)
        return _parse_device_info(resp['payload'])

    def set_led(self, on: bool):
        # Payloads captured from the Android app (traces/controlsTrace2.pcap)
        payload = bytes([0x11, 0x01, 0x64]) if on else bytes([0x11, 0x00, 0x00])
        resp = self._send_cmd(0x000a, payload)
        if resp['err_code'] != 0:
            raise IOError(f'SetLed command failed (err_code={resp["err_code"]})')

    def open_stream(self):
        # Listen before asking the camera to send; otherwise the first frame can
        # already be in flight when the socket is finally bound.
        stream_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        try:
            stream_sock.setsockopt(socket.SOL_SOCKET, socket.SO_RCVBUF, 4 * 1024 * 1024)
            stream_sock.settimeout(0.2)
            stream_sock.bind(('0.0.0.0', STREAM_RECV_PORT))
            init_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
            try:
                init_sock.settimeout(self._SOCK_TIMEOUT)
                # Payload = receive port + 2 zero bytes, as the Android app sends.
                # Without it the camera may acknowledge but send no video.
                resp = self._send_cmd(0x0004, struct.pack('<HH', STREAM_RECV_PORT, 0),
                                      port=STREAM_INIT_PORT, sock=init_sock)
                if resp['err_code'] != 0:
                    raise IOError(f'OpenVideo command failed (err_code={resp["err_code"]})')
            finally:
                init_sock.close()
        except BaseException:
            stream_sock.close()
            raise
        self._stream_sock = stream_sock

    def _publish_frame(self, frame: JpgFrame):
        # Prefer current video over a backlog of older images.
        try:
            self._frame_queue.put_nowait(frame)
        except queue.Full:
            try:
                self._frame_queue.get_nowait()
                self.stats['queue_drops'] += 1
            except queue.Empty:
                pass
            self._frame_queue.put_nowait(frame)

    def _recv_loop(self):
        frames: dict[int, JpgFrame] = {}
        arrival: deque[int] = deque()  # frame numbers in arrival order

        while not self._stop.is_set():
            try:
                data, peer = self._stream_sock.recvfrom(65535)
            except socket.timeout:
                continue
            except OSError:
                break

            if peer[0] != self.camera_ip or len(data) <= CHUNK_HDR_SZ:
                continue
            self.stats['packets'] += 1

            hdr = _parse_chunk_hdr(data)
            chunk_data = data[CHUNK_HDR_SZ:]
            n_frame = hdr['n_frame']

            # Expire partial frames before their 8-bit IDs can be reused.
            now = time.monotonic()
            while arrival and now - frames[arrival[0]].created_at > self._MAX_FRAME_AGE:
                frames.pop(arrival.popleft())
                self.stats['incomplete'] += 1

            if n_frame not in frames:
                # Evict oldest incomplete frame if slots are full
                if len(frames) >= self._MAX_FRAME_SLOTS:
                    old = arrival.popleft()
                    frames.pop(old, None)
                    self.stats['incomplete'] += 1
                frames[n_frame] = JpgFrame(n_frame)
                arrival.append(n_frame)

            frames[n_frame].add_chunk(
                hdr['n_chunk'], chunk_data, hdr['total_chunks'],
                hdr['res_width'], hdr['res_height'], hdr['accel'],
            )

            if frames[n_frame].complete:
                frame = frames.pop(n_frame)
                try:
                    arrival.remove(n_frame)
                except ValueError:
                    pass
                self.stats['frames'] += 1
                self._publish_frame(frame)

    def run(self, save_dir: str | None = None, info_only: bool = False):
        print('Querying device info...')
        try:
            info = self.get_device_info()
        except socket.timeout:
            print('ERROR: No response from camera. Is it on and connected over Wi-Fi?')
            return

        vendor = info.get('vendor', '?')
        model = info.get('product_id', '?')
        fw = info.get('fw_version', '?')
        ssid = info.get('ssid', '?')
        batt = info.get('battery_pct', '?')
        charging = ' (charging)' if info.get('charging') else ''
        print(f'Device  : {vendor} {model}  fw {fw}')
        print(f'SSID    : {ssid}')
        print(f'Battery : {batt}%{charging}')

        if info_only:
            self._cmd_sock.close()
            return

        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

        print('Opening video stream...')
        self.open_stream()

        led_on, mirror, upright = True, False, True
        roll = 0.0
        try:
            self.set_led(True)  # the Android app also turns the light on when it connects
        except OSError as e:
            print(f'Warning: could not turn on LED ({e})')

        recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        recv_thread.start()
        print("Streaming — 'l' light on/off, 'm' mirror (left/right ear), 'u' keep upright, 'q' quit")

        frame_count = 0
        img, frame = None, None
        last_frame_at = 0.0
        stats_at = time.monotonic()
        stats_frames = 0
        fps = 0.0
        try:
            cv2.namedWindow('EarCam', cv2.WINDOW_NORMAL)
            while True:
                new_frame = False
                try:
                    incoming = self._frame_queue.get(timeout=0.02)
                except queue.Empty:
                    incoming = None
                if incoming is not None:
                    decoded = cv2.imdecode(np.frombuffer(incoming.data, dtype=np.uint8),
                                           cv2.IMREAD_COLOR)
                    if decoded is not None:
                        img, frame = decoded, incoming
                        last_frame_at = time.monotonic()
                        frame_count += 1
                        new_frame = True
                    else:
                        self.stats['decode_errors'] += 1

                now = time.monotonic()
                if now - stats_at >= 5.0:
                    fps = (frame_count - stats_frames) / (now - stats_at)
                    print(f'Video: {fps:.1f} fps; incomplete={self.stats["incomplete"]}, '
                          f'decode_errors={self.stats["decode_errors"]}, '
                          f'queue_drops={self.stats["queue_drops"]}')
                    stats_at, stats_frames = now, frame_count

                view = img.copy() if img is not None else np.zeros((480, 640, 3), np.uint8)
                if frame is not None:
                    if upright:
                        x, y, _ = frame.accel
                        if math.hypot(x, y) >= 32:
                            roll = math.degrees(math.atan2(x, y)) + 90
                        h, w = view.shape[:2]
                        view = cv2.warpAffine(view, cv2.getRotationMatrix2D(
                            (w / 2, h / 2), roll, 1.0), (w, h))
                    if mirror:
                        view = cv2.flip(view, 1)
                    if save_dir and new_frame:
                        path = os.path.join(save_dir, f'frame_{frame_count - 1:05d}.jpg')
                        cv2.imwrite(path, view)

                status = (f'{fps:.1f} fps  LED {"on" if led_on else "off"}'
                          f'  Mirror {"on" if mirror else "off"}'
                          f'  Upright {"on" if upright else "off"}')
                cv2.putText(view, status, (8, 24), cv2.FONT_HERSHEY_SIMPLEX,
                            0.5, (0, 255, 255), 1)
                if now - last_frame_at > 0.75:
                    cv2.putText(view, 'NO LIVE VIDEO - check camera / Wi-Fi', (8, 54),
                                cv2.FONT_HERSHEY_SIMPLEX, 0.5, (0, 0, 255), 2)
                cv2.imshow('EarCam', view)

                # Pump GUI events even if packets stop arriving or a JPEG is bad.
                key = cv2.waitKey(1) & 0xFF
                if key == ord('q') or cv2.getWindowProperty('EarCam', cv2.WND_PROP_VISIBLE) < 1:
                    break
                if key == ord('m'):
                    mirror = not mirror
                if key == ord('u'):
                    upright = not upright
                if key == ord('l'):
                    try:
                        self.set_led(not led_on)
                        led_on = not led_on
                    except OSError as e:
                        print(f'LED command failed: {e}')
        finally:
            self._stop.set()
            if self._stream_sock:
                self._stream_sock.close()
            if self._cmd_sock:
                self._cmd_sock.close()
            recv_thread.join(timeout=1.0)
            cv2.destroyAllWindows()
        print(f'Done — {frame_count} frames displayed.')


def main():
    ap = argparse.ArgumentParser(
        description='EarCam: live viewer for Soulear ear inspection camera',
        epilog='Connect to the camera\'s Soulear WiFi network before running.',
    )
    ap.add_argument('--camera', default=CAMERA_IP, metavar='IP',
                    help=f'Camera IP address (default: {CAMERA_IP})')
    ap.add_argument('--save', metavar='DIR',
                    help='Save each JPEG frame to DIR')
    ap.add_argument('--info', action='store_true',
                    help='Print device info and exit without streaming')
    args = ap.parse_args()

    SoulearClient(args.camera).run(save_dir=args.save, info_only=args.info)


if __name__ == '__main__':
    main()
