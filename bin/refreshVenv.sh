#!/bin/bash
#
# Refresh the venv (e.g. after a system Python upgrade breaks it).
# Afterwards, activate it in your shell with: source venv/bin/activate

set -euo pipefail

cd "$(dirname "$0")/.."
rm -rf venv
python3 -m venv venv
venv/bin/pip install -r requirements.txt
