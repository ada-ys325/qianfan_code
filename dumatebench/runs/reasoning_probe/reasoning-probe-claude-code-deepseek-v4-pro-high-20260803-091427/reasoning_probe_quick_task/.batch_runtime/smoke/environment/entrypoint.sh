#!/usr/bin/env bash
set -euo pipefail

/opt/dumate/setup.sh

if [ "${DUMATE_DISABLE_NETWORK_FAULTS:-0}" != "1" ]; then
  /opt/dumate/network_fault_daemon.py \
    --config "${DUMATE_NETWORK_FAULT_CONFIG:-/opt/dumate/network_faults.yaml}" \
    --log /logs/network_faults.jsonl &
fi

if [ "$#" -eq 0 ]; then
  exec sleep infinity
fi

exec "$@"
