#!/bin/bash
#
# Script to connect the WiFi interface to a Soulear camera
#
# Usage: connectWifi.sh [SSID]   (default: the first visible network named "Soulear-*")

set -euo pipefail

WIFI_DEV="${WIFI_DEV:-wlp0s20f3}"

if ! iw dev "${WIFI_DEV}" info | grep -q 'type managed'; then
    echo "${WIFI_DEV} is not in managed mode; run resetWifi.sh first" >&2
    exit 1
fi

SOULEAR="${1:-}"
if [[ -z "${SOULEAR}" ]]; then
    SOULEAR="$(nmcli -t -f SSID dev wifi list ifname "${WIFI_DEV}" --rescan yes \
               | grep -m1 '^Soulear-' || true)"
    if [[ -z "${SOULEAR}" ]]; then
        echo "No Soulear-* network found; is the camera switched on?" >&2
        exit 1
    fi
fi

nmcli dev wifi connect "${SOULEAR}" ifname "${WIFI_DEV}"
