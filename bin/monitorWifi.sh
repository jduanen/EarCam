#!/bin/bash
#
# Script to set the WiFi to monitor mode on a given channel

#### FIXME take these as args
CHAN=4
WIFI_DEV="wlp0s20f3"

sudo systemctl stop wpa_supplicant
sudo ip link set ${WIFI_DEV} down
sudo iw dev ${WIFI_DEV} set type monitor
sudo ip link set ${WIFI_DEV} up
sudo iw dev ${WIFI_DEV} set channel ${WIFI_CHAN}
iw dev ${WIFI_DEV} info
