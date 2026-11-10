#!/bin/bash
#
# Script to capture packets off a given WiFi channel
# N.B. This requires that the WiFi device is set in monitor mode.


WIFI_DEV="wlp0s20f3"
OUT_FILE="${HOME}/Code/EarCam/traces/controlsTrace2.pcap"

sudo tcpdump -i ${WIFI_DEV} -Z ${USER} -w ${OUT_FILE}
