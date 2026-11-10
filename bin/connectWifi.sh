#!/bin/bash
#
# Script to connect the WiFi interface to a Soulear camera


WIFI_DEV="wlp0s20f3"

#### FIXME make this detect an SSID starting with the prefix "Soulear-"
SOULEAR="Soulear-830bb"

nmcli dev wifi connect ${SOULEAR}

#### FIXME only do this if the above command worked, and grep to find 'type managed' and return error if not found
iw dev ${WIFI_DEV} info  # should say 'type managed'
