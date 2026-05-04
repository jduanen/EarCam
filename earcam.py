#!/usr/bin/env python3
"""EarCam: Python client for Soulear ear inspection camera.

Protocol reverse-engineered from ~/Code2/Suear-Web-Viewer (Sean Pesce).
Camera acts as a WiFi Direct Group Owner / soft AP.
Connect your machine to the "Soulear-830bb" open WiFi network first.
"""

import argparse
import os
import queue
import socket
import struct
import threading
from collections import deque

import cv2
import numpy as np


CAMERA_IP = '192.168.1.1'
CMD_PORT = 10005
STREAM_INIT_PORT = 10006
STREAM_RECV_PORT = 22785
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
        'total_chunks': total_chunks,  # nonzero only on last chunk of a frame
        'res_width': res_w,
        'res_height': res_h,
    }


def _parse_device_info(data: bytes) -> dict:
    # Offsets (no padding, _pack_=1):
    # 0: unk0(1) 1: vendor(32) 33: product_id(32) 65: fw_version(16)
    # 81: ssid(32) 113: unk32(4) 117: unk16(2) 119: power_info(2) 121: capacity(1)
    if len(data) < 122:
        return {}
    vendor = data[1:33].rstrip(b'\x00').decode('ascii', errors='replace')
    product_id = data[33:65].rstrip(b'\x00').decode('ascii', errors='replace')
    fw_version = data[65:81].rstrip(b'\x00').decode('ascii', errors='replace')
    ssid = data[81:113].rstrip(b'\x00').decode('ascii', errors='replace')
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

    Fixes vs. Suear-Web-Viewer: no fixed-size buffer (avoids overflow for large frames),
    and chunk ordering uses modular arithmetic to handle 8-bit index rollover correctly.
    """

    def __init__(self, n_frame: int):
        self.n_frame = n_frame
        self.chunks: dict[int, bytes] = {}
        self.total: int | None = None
        self._first_chunk: int | None = None
        self.width = 0
        self.height = 0

    def add_chunk(self, n_chunk: int, chunk_data: bytes, total_chunks: int,
                  width: int = 0, height: int = 0):
        if self._first_chunk is None:
            self._first_chunk = n_chunk
            self.width = width
            self.height = height
        self.chunks[n_chunk] = bytes(chunk_data)
        if total_chunks:               # nonzero on the last chunk only
            self.total = total_chunks

    @property
    def complete(self) -> bool:
        return self.total is not None and len(self.chunks) == self.total

    @property
    def data(self) -> bytes:
        if not self.chunks or self._first_chunk is None:
            return b''
        first = self._first_chunk
        ordered = sorted(self.chunks, key=lambda i: (i - first) % 256)
        return b''.join(self.chunks[i] for i in ordered)


class SoulearClient:
    _MAX_FRAME_SLOTS = 8
    _SOCK_TIMEOUT = 5.0
    _FRAME_QUEUE_SZ = 4

    def __init__(self, camera_ip: str = CAMERA_IP):
        self.camera_ip = camera_ip
        self._msg_id = 0
        self._cmd_sock: socket.socket | None = None
        self._stream_sock: socket.socket | None = None
        self._frame_queue: queue.Queue[JpgFrame] = queue.Queue(maxsize=self._FRAME_QUEUE_SZ)
        self._stop = threading.Event()

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

    def open_stream(self):
        init_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        init_sock.settimeout(self._SOCK_TIMEOUT)
        try:
            resp = self._send_cmd(0x0004, port=STREAM_INIT_PORT, sock=init_sock)
        finally:
            init_sock.close()
        if resp['err_code'] != 0:
            raise IOError(f'OpenVideo command failed (err_code={resp["err_code"]})')
        self._stream_sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
        self._stream_sock.bind(('0.0.0.0', STREAM_RECV_PORT))

    def _recv_loop(self):
        frames: dict[int, JpgFrame] = {}
        arrival: deque[int] = deque()  # frame numbers in arrival order

        while not self._stop.is_set():
            try:
                self._stream_sock.settimeout(1.0)
                data, _ = self._stream_sock.recvfrom(CHUNK_HDR_SZ + 1500)
            except socket.timeout:
                continue
            except OSError:
                break

            if len(data) < CHUNK_HDR_SZ:
                continue

            hdr = _parse_chunk_hdr(data)
            chunk_data = data[CHUNK_HDR_SZ:]
            n_frame = hdr['n_frame']

            if n_frame not in frames:
                # Evict oldest incomplete frame if slots are full
                if len(frames) >= self._MAX_FRAME_SLOTS:
                    old = arrival.popleft()
                    frames.pop(old, None)
                frames[n_frame] = JpgFrame(n_frame)
                arrival.append(n_frame)

            frames[n_frame].add_chunk(
                hdr['n_chunk'], chunk_data, hdr['total_chunks'],
                hdr['res_width'], hdr['res_height'],
            )

            if frames[n_frame].complete:
                frame = frames.pop(n_frame)
                try:
                    arrival.remove(n_frame)
                except ValueError:
                    pass
                try:
                    self._frame_queue.put_nowait(frame)
                except queue.Full:
                    pass  # drop; display loop is behind

    def run(self, save_dir: str | None = None, info_only: bool = False):
        print('Querying device info...')
        try:
            info = self.get_device_info()
        except socket.timeout:
            print('ERROR: No response from camera. Are you connected to the Soulear-830bb WiFi?')
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
            return

        if save_dir:
            os.makedirs(save_dir, exist_ok=True)

        print('Opening video stream...')
        self.open_stream()

        recv_thread = threading.Thread(target=self._recv_loop, daemon=True)
        recv_thread.start()
        print("Streaming — press 'q' to quit")

        frame_count = 0
        while True:
            try:
                frame = self._frame_queue.get(timeout=3.0)
            except queue.Empty:
                print('Waiting for frames... (is the phone app / another client disconnected?)')
                continue

            jpg = frame.data
            arr = np.frombuffer(jpg, dtype=np.uint8)
            img = cv2.imdecode(arr, cv2.IMREAD_COLOR)
            if img is None:
                continue  # corrupted reassembly, skip

            cv2.imshow('EarCam', img)
            if save_dir:
                path = os.path.join(save_dir, f'frame_{frame_count:05d}.jpg')
                cv2.imwrite(path, img)
            frame_count += 1

            if cv2.waitKey(1) & 0xFF == ord('q'):
                break

        self._stop.set()
        if self._stream_sock:
            self._stream_sock.close()
        cv2.destroyAllWindows()
        print(f'Done — {frame_count} frames displayed.')


def main():
    ap = argparse.ArgumentParser(
        description='EarCam: live viewer for Soulear ear inspection camera',
        epilog='Connect to the Soulear-830bb open WiFi network before running.',
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
