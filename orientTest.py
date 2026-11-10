#!/usr/bin/env python3
"""Guided orientation test for the Soulear camera (uses stdlib only)

Opens the video stream, then for each prompted position records chunk-header
bytes 6-9 for a few seconds and saves one JPEG. Results go next to this script:
orient_log.csv and orient_<n>_<label>.jpg.
"""
import collections, csv, os, socket, struct, threading, time

CAMERA_IP = '192.168.1.1'
HERE = os.path.dirname(os.path.abspath(__file__))
SAMPLE_SECS = 3.0
POSITIONS = [
    ('roll000', 'Point the camera level (horizontal) with the picture upright as you would normally hold it'),
    ('roll090', 'Still level, rotate the camera 90 degrees clockwise (as seen from behind it, looking where it points)'),
    ('roll180', 'Still level, rotate another 90 degrees clockwise (180 total, picture upside down)'),
    ('roll270', 'Still level, rotate another 90 degrees clockwise (270 total)'),
    ('roll360', 'Still level, rotate back to the starting position (upright)'),
    ('roll045', 'Still level, rotate 45 degrees clockwise from upright'),
    ('pitch_up', 'Point the camera straight up at the ceiling'),
    ('pitch_down', 'Point the camera straight down at the floor'),
]

latest = {'hdr': None, 'jpg': None}
lock = threading.Lock()


def recv_loop(sock):
    frames = {}
    while True:
        data, _ = sock.recvfrom(2048)
        if len(data) < 16:
            continue
        n_chunk, n_frame, total = data[1], data[2], data[4]
        f = frames.setdefault(n_frame, {})
        f[n_chunk] = data[16:]
        with lock:
            latest['hdr'] = data[6:10]
        if total and len(f) == total:
            first = min(f) if max(f) - min(f) < 200 else min(k for k in f if k > 128)
            jpg = b''.join(f[k] for k in sorted(f, key=lambda i: (i - first) % 256))
            with lock:
                latest['jpg'] = jpg
            frames.pop(n_frame)
            for k in [k for k in frames if (n_frame - k) % 256 > 8]:
                frames.pop(k)  # drop stale incomplete frames


def main():
    init = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    init.settimeout(5)
    init.sendto(struct.pack('<IHHBBH', 0xffeeffee, 1, 0x0004, 1, 0, 0), (CAMERA_IP, 10006))
    try:
        init.recvfrom(4096)
    except socket.timeout:
        raise SystemExit('No response to OpenVideo. Connected to the Soulear Wi-Fi, phone app closed?')
    stream = socket.socket(socket.AF_INET, socket.SOCK_DGRAM)
    stream.bind(('0.0.0.0', 22789))
    threading.Thread(target=recv_loop, args=(stream,), daemon=True).start()

    time.sleep(1)
    if latest['hdr'] is None:
        raise SystemExit('Stream opened but no video arriving.')

    log_path = os.path.join(HERE, 'orient_log.csv')
    with open(log_path, 'w', newline='') as fh:
        w = csv.writer(fh)
        w.writerow(['label', 't', 'b6', 'b7', 'b8', 'b9'])
        for i, (label, prompt) in enumerate(POSITIONS):
            input(f'\n[{i + 1}/{len(POSITIONS)}] {prompt}.\n   Hold it still, then press Enter...')
            seen = collections.Counter()
            t0 = time.time()
            while time.time() - t0 < SAMPLE_SECS:
                with lock:
                    h = latest['hdr']
                w.writerow([label, f'{time.time() - t0:.2f}', *h])
                seen[h.hex(' ')] += 1
                time.sleep(0.05)
            with lock:
                jpg = latest['jpg']
            if jpg:
                with open(os.path.join(HERE, f'orient_{i + 1}_{label}.jpg'), 'wb') as jf:
                    jf.write(jpg)
            top = ', '.join(f'{k} (x{n})' for k, n in seen.most_common(3))
            print(f'   recorded: {top}')
    print(f'\nDone. Results in {HERE}')


if __name__ == '__main__':
    main()
