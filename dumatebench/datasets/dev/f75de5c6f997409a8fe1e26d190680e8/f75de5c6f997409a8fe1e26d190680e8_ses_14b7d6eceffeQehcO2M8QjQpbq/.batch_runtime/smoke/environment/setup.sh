#!/usr/bin/env bash
set -euo pipefail

mkdir -p /workspace /outputs /logs
rm -rf /workspace/*
cp -a /workspace_seed/. /workspace/

chown -R agent:agent /workspace /outputs /logs
