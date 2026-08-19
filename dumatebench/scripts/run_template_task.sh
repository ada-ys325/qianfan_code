#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_DIR="${ROOT}/datasets/dev/template_task"
COMPOSE_FILE="${TASK_DIR}/environment/docker-compose.yaml"

cd "${TASK_DIR}"

mkdir -p run_outputs run_logs
rm -rf run_outputs/* run_logs/*

cleanup() {
  docker compose -f "${COMPOSE_FILE}" logs --no-color > run_logs/compose.log 2>/dev/null || true
  docker compose -f "${COMPOSE_FILE}" down --remove-orphans >/dev/null 2>&1 || true
}
trap cleanup EXIT

docker compose -f "${COMPOSE_FILE}" down --remove-orphans >/dev/null 2>&1 || true
docker compose -f "${COMPOSE_FILE}" build
docker compose -f "${COMPOSE_FILE}" run --rm task /opt/dumate/agent_smoke.sh
python3 evaluator/evaluator.py --task-dir "${TASK_DIR}"
