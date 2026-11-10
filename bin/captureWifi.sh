#!/bin/bash
#
# Script to capture packets off a given WiFi channel
# N.B. This requires that the WiFi device is set in monitor mode (see monitorWifi.sh).
#
# Usage: captureWifi.sh [output.pcap]   (default traces/capture_<date>_<time>.pcap)

set -euo pipefail

WIFI_DEV="${WIFI_DEV:-wlp0s20f3}"
REPO="$(cd "$(dirname "$0")/.." && pwd)"
OUT_FILE="${1:-${REPO}/traces/capture_$(date +%Y%m%d_%H%M%S).pcap}"

if ! iw dev "${WIFI_DEV}" info | grep -q 'type monitor'; then
    echo "${WIFI_DEV} is not in monitor mode; run monitorWifi.sh first" >&2
    exit 1
fi
if [[ -e "${OUT_FILE}" ]]; then
    echo "${OUT_FILE} already exists; not overwriting it" >&2
    exit 1
fi

echo "Capturing to ${OUT_FILE} (Ctrl-C to stop)"
sudo tcpdump -i "${WIFI_DEV}" -Z "${USER}" -w "${OUT_FILE}"
