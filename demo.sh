#!/usr/bin/env bash
# Offline demo with fixture pages, a demo inbox and PIN 1234. Data goes to ./data-demo
cd "$(dirname "$0")"
export TRACKER_HOME="$PWD/data-demo"
python3 -m shared.cli demo --pin "${PIN:-1234}"
PYTHONUNBUFFERED=1 python3 -m shared.cli app --dev-approvals
