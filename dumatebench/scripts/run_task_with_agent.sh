#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_DIR="${DUMATE_TASK_DIR:-${ROOT}/datasets/dev/template_task}"

if [ "$#" -lt 1 ]; then
  echo "usage: $0 '<agent command>' [adapter_runner args...]" >&2
  echo "example: $0 'python3 agents/examples/echo_agent.py' --max-steps 3" >&2
  exit 2
fi

AGENT_CMD="$1"
shift

python3 "${ROOT}/agents/adapter_runner.py" \
  --task-dir "${TASK_DIR}" \
  --agent-cmd "${AGENT_CMD}" \
  "$@"
