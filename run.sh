#!/usr/bin/env bash
# Run Signal Siege locally. Pass --host 0.0.0.0 to expose it on your LAN.
set -euo pipefail
cd "$(dirname "$0")"
exec python3 game_server.py "$@"
