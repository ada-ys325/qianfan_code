#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_DIR="${ROOT}/datasets/dev/odyssey_2_12_smoke"
COMPOSE_FILE="${TASK_DIR}/environment/docker-compose.yaml"
IMAGE_NAME="dumatebench-odyssey-2-12-smoke:latest"

cd "${TASK_DIR}"

mkdir -p run_outputs run_logs
find run_outputs -mindepth 1 -maxdepth 1 -exec rm -rf {} +
find run_logs -mindepth 1 -maxdepth 1 -exec rm -rf {} +

cleanup() {
  docker compose -f "${COMPOSE_FILE}" logs --no-color > run_logs/compose.log 2>/dev/null || true
  docker compose -f "${COMPOSE_FILE}" down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose -f "${COMPOSE_FILE}" down --remove-orphans >/dev/null 2>&1 || true
if ! docker compose -f "${COMPOSE_FILE}" build 2>&1 | tee run_logs/docker_build.log; then
  if grep -Eqi 'auth\.docker\.io|failed to fetch oauth token|context deadline exceeded|i/o timeout' run_logs/docker_build.log && \
    docker image inspect "${IMAGE_NAME}" >/dev/null 2>&1; then
    echo "warning: Docker build could not reach Docker Hub; reusing local ${IMAGE_NAME}" >&2
  else
    exit 1
  fi
fi
set +e
docker compose -f "${COMPOSE_FILE}" run --rm \
  -e OPENAI_API_KEY \
  -e OPENAI_BASE_URL \
  -e DUMATE_MODEL \
  -e DUMATE_TRUSTED_BASE_URLS="${DUMATE_TRUSTED_BASE_URLS:-https://cn.huayanapi.com:27502/v1}" \
  task \
  /opt/dumate/command_agent.py \
    --in-container \
    --task-dir /opt/dumate/task \
    --trusted-base-url "https://cn.huayanapi.com:27502/v1" \
    "$@"
agent_rc=$?
"${DUMATE_EVALUATOR_PYTHON:-python3}" evaluator/evaluator.py --task-dir "${TASK_DIR}"
evaluator_rc=$?
set -e

"${DUMATE_EVALUATOR_PYTHON:-python3}" - "${TASK_DIR}/run_logs/agent_status.json" "${agent_rc}" "${evaluator_rc}" <<'PY'
import json
import sys
from pathlib import Path

status_path = Path(sys.argv[1])
agent_rc = int(sys.argv[2])
evaluator_rc = int(sys.argv[3])
try:
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
except json.JSONDecodeError:
    status = {}
status["agent_container_returncode"] = agent_rc
status["evaluator_returncode"] = evaluator_rc
status_path.parent.mkdir(parents=True, exist_ok=True)
status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False))
PY

if [ "${evaluator_rc}" -ne 0 ]; then
  exit "${evaluator_rc}"
fi
exit "${agent_rc}"
