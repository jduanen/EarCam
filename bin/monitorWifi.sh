#!/bin/bash
#
# Script to set the WiFi to monitor mode on a given channel
#
# Usage: monitorWifi.sh [channel]   (default 4, the Soulear-830bb camera's channel)

set -euo pipefail

CHAN="${1:-4}"
WIFI_DEV="${WIFI_DEV:-wlp0s20f3}"

if ! [[ "${CHAN}" =~ ^([1-9]|1[0-4])$ ]]; then
    echo "Usage: $0 [channel 1-14]" >&2
    exit 1
fi

sudo nmcli dev set "${WIFI_DEV}" managed no   # keep NetworkManager from switching it back
sudo systemctl stop wpa_supplicant
sudo ip link set "${WIFI_DEV}" down
sudo iw dev "${WIFI_DEV}" set type monitor
sudo ip link set "${WIFI_DEV}" up
sudo iw dev "${WIFI_DEV}" set channel "${CHAN}"
iw dev "${WIFI_DEV}" info
