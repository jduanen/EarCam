#!/bin/bash
#
# Script to reset the WiFi to normal mode

set -euo pipefail

WIFI_DEV="${WIFI_DEV:-wlp0s20f3}"

sudo ip link set "${WIFI_DEV}" down
sudo iw dev "${WIFI_DEV}" set type managed
sudo ip link set "${WIFI_DEV}" up
sudo systemctl unmask --runtime wpa_supplicant   # harmless if you never masked it
sudo systemctl start wpa_supplicant
sudo nmcli dev set "${WIFI_DEV}" managed yes
