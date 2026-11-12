#!/usr/bin/env bash
# Wrapper: run the tracker CLI from this folder. TRACKER_HOME defaults to ./data
cd "$(dirname "$0")"
[ -d .venv ] && source .venv/bin/activate
exec python3 -m shared.cli "$@"
