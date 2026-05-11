#!/usr/bin/env python3
"""Probe the Soulear camera for supported command types.

Sends each command type with an empty payload and reports which ones
get a valid response, the error code, and any payload returned.

Only probes request/response commands; unsolicited camera pushes
(e.g. type 0x0009 on port 10007) are not discovered this way.
"""

import argparse
import socket
import struct

CAMERA_IP = '192.168.1.1'
CMD_PORT = 10005
MAGIC = 0xffeeffee
CMD_HDR_FMT = '<IHHBBH'
CMD_HDR_SZ = struct.calcsize(CMD_HDR_FMT)

# Commands that are unsafe to send with any non-empty payload.
# Skipped automatically when --retry is used, unless --force is given.
DANGEROUS = {
    0x0008: 'powers off camera when sent with any payload',
}


def _build(msg_id: int, msg_type: int, payload: bytes = b'') -> bytes:
    return struct.pack(CMD_HDR_FMT, MAGIC, msg_id, msg_type, 1, 0, len(payload)) + payload


def _parse(data: bytes) -> dict | None:
    if len(data) < CMD_HDR_SZ:
        return None
    magic, msg_id, msg_type, unk, err_code, length = struct.unpack_from(CMD_HDR_FMT, data)
    if magic != MAGIC:
        return None
    return {
        'id': msg_id,
        'type': msg_type,
        'err_code': err_code,
        'payload': data[CMD_HDR_SZ: CMD_HDR_SZ + length],
    }


def _send_one(sock: socket.socket, camera_ip: str, port: int,
              msg_type: int, payload: bytes) -> tuple[bool, dict | None]:
    """Send one command. Returns (timed_out, parsed_response)."""
    sock.sendto(_build(msg_type, msg_type, payload), (camera_ip, port))
    try:
        data, _ = sock.recvfrom(4096)
    except socket.timeout:
        return True, None
    return False, _parse(data)


def _print_result(msg_type: int, resp: dict | None, sent_payload: bytes, show_sent: bool = False):
    if resp is None:
        print(f'  0x{msg_type:04x}  bad response')
        return
    status = 'ok' if resp['err_code'] == 0 else f'err={resp["err_code"]}'
    pl = resp['payload'].hex() if resp['payload'] else '(empty)'
    suffix = f'  [sent={sent_payload.hex() or "(empty)"}]' if show_sent else ''
    print(f'  0x{msg_type:04x}  {status}  payload={pl}{suffix}')


def probe(camera_ip: str, port: int, start: int, end: int, timeout: float,
          retry_payloads: list[bytes] = (), skip: set[int] = frozenset(),
          force: bool = False, write_payload: bytes | None = None):
    sock = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    sock.settimeout(timeout)
    try:
        for msg_type in range(start, end + 1):
            if msg_type in skip:
                print(f'  0x{msg_type:04x}  skipped (--skip)')
                continue
            has_payload = write_payload is not None or retry_payloads
            if has_payload and msg_type in DANGEROUS and not force:
                print(f'  0x{msg_type:04x}  skipped (dangerous: {DANGEROUS[msg_type]})')
                continue

            if write_payload is not None:
                timed_out, resp = _send_one(sock, camera_ip, port, msg_type, write_payload)
                if timed_out:
                    print(f'  0x{msg_type:04x}  timeout')
                else:
                    _print_result(msg_type, resp, write_payload)
            else:
                for attempt, payload in enumerate([b''] + list(retry_payloads)):
                    timed_out, resp = _send_one(sock, camera_ip, port, msg_type, payload)
                    if timed_out:
                        if attempt == len(retry_payloads):  # last attempt
                            print(f'  0x{msg_type:04x}  timeout')
                        continue
                    _print_result(msg_type, resp, payload, show_sent=(attempt > 0))
                    break
    finally:
        sock.close()


def main():
    ap = argparse.ArgumentParser(
        description='Probe Soulear camera for supported command types',
        epilog='Connect to the Soulear WiFi before running.',
    )
    ap.add_argument('--camera', default=CAMERA_IP, metavar='IP')
    ap.add_argument('--port', type=int, default=CMD_PORT, metavar='PORT',
                    help=f'UDP port to probe (default: {CMD_PORT})')
    ap.add_argument('--start', type=lambda x: int(x, 0), default=0x0000, metavar='TYPE',
                    help='First command type to probe, hex ok (default: 0x0000)')
    ap.add_argument('--end', type=lambda x: int(x, 0), default=0x001f, metavar='TYPE',
                    help='Last command type to probe, hex ok (default: 0x001f)')
    ap.add_argument('--timeout', type=float, default=1.0, metavar='SECS',
                    help='Per-command response timeout in seconds (default: 1.0)')
    ap.add_argument('--retry', nargs='+', default=[], metavar='HEX',
                    help='Hex payloads to retry with on timeout, e.g. --retry 00 01 ff')
    ap.add_argument('--write', nargs='+', default=None, metavar='HEX',
                    help='Send this payload to every type in the range, e.g. --write 01 or --write 01 02 03')
    ap.add_argument('--skip', nargs='+', default=[], metavar='TYPE',
                    help='Command types to skip entirely, hex ok, e.g. --skip 0x0008')
    ap.add_argument('--force', action='store_true',
                    help='Send payloads even to commands in the built-in dangerous list')
    args = ap.parse_args()

    try:
        retry_payloads = [bytes.fromhex(p) for p in args.retry]
    except ValueError as e:
        ap.error(f'invalid --retry payload: {e}')

    try:
        skip = {int(t, 0) for t in args.skip}
    except ValueError as e:
        ap.error(f'invalid --skip type: {e}')

    write_payload = None
    if args.write is not None:
        try:
            write_payload = bytes(int(b, 16) for b in args.write)
        except ValueError as e:
            ap.error(f'invalid --write byte: {e}')

    if args.write and args.retry:
        ap.error('--write and --retry are mutually exclusive')

    print(f'Probing {args.camera}:{args.port}  types 0x{args.start:04x}–0x{args.end:04x}  timeout={args.timeout}s')
    if write_payload is not None:
        print(f'Write payload: {write_payload.hex()}')
    elif retry_payloads:
        print(f'Retry payloads on timeout: {[p.hex() for p in retry_payloads]}')
    probe(args.camera, args.port, args.start, args.end, args.timeout,
          retry_payloads, skip, args.force, write_payload)


if __name__ == '__main__':
    main()
