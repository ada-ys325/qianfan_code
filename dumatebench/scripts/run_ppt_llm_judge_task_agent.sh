#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${ROOT}/.." && pwd)"
TASK_ID="${DUMATE_TASK_ID:-24d6778af4354ccbbd19ee5a5e529beb_ses_124aedee2ffeZgWPdornLUhXG6}"
TASK_DIR="${DUMATE_TASK_DIR:-${ROOT}/datasets/dev/${TASK_ID}}"
COMPOSE_FILE="${TASK_DIR}/environment/docker-compose.yaml"
IMAGE_NAME="${DUMATE_IMAGE_NAME:-dumatebench-ppt-llm-judge-task:latest}"
BASE_IMAGE="${DUMATE_BASE_IMAGE:-python:3.12-slim}"
if [ -n "${DUMATE_EVALUATOR_PYTHON:-}" ]; then
  EVALUATOR_PYTHON="${DUMATE_EVALUATOR_PYTHON}"
elif [ -n "${CONDA_PREFIX:-}" ] && [ -x "${CONDA_PREFIX}/bin/python" ]; then
  EVALUATOR_PYTHON="${CONDA_PREFIX}/bin/python"
else
  EVALUATOR_PYTHON="conda run -n dumatebench python"
fi
JUDGE_MODEL="${DUMATE_JUDGE_MODEL:-gpt-4o}"
JUDGE_MIN_SCORE="${DUMATE_JUDGE_MIN_SCORE:-70}"
JUDGE_INPUT_FILE="${DUMATE_JUDGE_INPUT_FILE:-workspace_seed/uploads/演示文稿9.pptx}"
JUDGE_OUTPUT_FILE="${DUMATE_JUDGE_OUTPUT_FILE:-run_outputs/pptx/演示文稿9_优化.pptx}"
JUDGE_REPORT_FILE="${DUMATE_JUDGE_REPORT_FILE:-run_outputs/ppt_llm_judge.json}"
TRUSTED_BASE_URL="${DUMATE_TRUSTED_BASE_URLS:-https://cn.huayanapi.com:27502/v1}"

if [ ! -d "${TASK_DIR}" ]; then
  echo "Task directory not found: ${TASK_DIR}" >&2
  exit 2
fi
if [ ! -f "${COMPOSE_FILE}" ]; then
  echo "Docker compose file not found: ${COMPOSE_FILE}" >&2
  exit 2
fi
if [ -z "${OPENAI_API_KEY:-}" ]; then
  echo "OPENAI_API_KEY is required for the agent and PPT judge." >&2
  exit 2
fi
if [ -z "${OPENAI_BASE_URL:-}" ]; then
  echo "OPENAI_BASE_URL is required for the agent and PPT judge." >&2
  exit 2
fi

export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"
export DUMATE_EVALUATE_PY="${DUMATE_EVALUATE_PY:-${ROOT}/evaluator/evaluate.py}"

if ! ${EVALUATOR_PYTHON} - <<'PY' >/dev/null 2>&1
import openai  # noqa: F401
import pptx  # noqa: F401
PY
then
  echo "Host judge dependencies are missing. Install them with:" >&2
  echo "  conda run -n dumatebench python -m pip install -r ${ROOT}/requirements.txt" >&2
  exit 2
fi

if ! command -v soffice >/dev/null 2>&1; then
  echo "warning: soffice was not found on the host PATH; PPT judge will use structure-only mode without slide images." >&2
  echo "         Install LibreOffice and expose soffice, e.g. on macOS:" >&2
  echo "         brew install --cask libreoffice" >&2
  echo "         export PATH=\"/Applications/LibreOffice.app/Contents/MacOS:\$PATH\"" >&2
fi
if ! command -v pdftoppm >/dev/null 2>&1; then
  echo "warning: pdftoppm was not found on the host PATH; PPT judge cannot convert rendered PDFs to slide images." >&2
  echo "         Install Poppler, e.g. on macOS: brew install poppler" >&2
fi

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
  image_task_id="$(docker image inspect -f '{{ index .Config.Labels "org.dumatebench.task_id" }}' "${IMAGE_NAME}" 2>/dev/null || true)"
  if grep -Eqi 'auth\.docker\.io|failed to fetch oauth token|context deadline exceeded|i/o timeout|EOF|failed to resolve source metadata|registry-1\.docker\.io|docker\.io/library/python' run_logs/docker_build.log && \
    [ "${image_task_id}" = "${TASK_ID}" ]; then
    echo "warning: Docker build could not reach Docker Hub; reusing local ${IMAGE_NAME} for ${TASK_ID}" >&2
  else
    echo "Docker build failed. If the error is a Docker Hub network issue, retry after running:" >&2
    echo "  docker pull ${BASE_IMAGE}" >&2
    echo "or use another accessible Python 3.12 slim-compatible base image:" >&2
    echo "  DUMATE_BASE_IMAGE=mirror-or-local/python:3.12-slim ${ROOT}/scripts/run_ppt_llm_judge_task_agent.sh --max-steps 20" >&2
    echo "If the build reaches apt-get and then stalls, configure reachable Debian mirrors:" >&2
    echo "  DUMATE_APT_DEBIAN_MIRROR=http://mirrors.aliyun.com/debian DUMATE_APT_SECURITY_MIRROR=http://mirrors.aliyun.com/debian-security ${ROOT}/scripts/run_ppt_llm_judge_task_agent.sh --max-steps 20" >&2
    echo "If ${IMAGE_NAME} exists but was created from another task, remove it and rebuild:" >&2
    echo "  docker image rm ${IMAGE_NAME}" >&2
    exit 1
  fi
fi

set +e
docker compose -f "${COMPOSE_FILE}" run --rm \
  -e OPENAI_API_KEY \
  -e OPENAI_BASE_URL \
  -e DUMATE_MODEL \
  -e DUMATE_TRUSTED_BASE_URLS="${TRUSTED_BASE_URL}" \
  task \
  /opt/dumate/command_agent.py \
    --in-container \
    --task-dir /opt/dumate/task \
    --trusted-base-url "${OPENAI_BASE_URL}" \
    "$@"
agent_rc=$?

${EVALUATOR_PYTHON} evaluator/evaluator.py --task-dir "${TASK_DIR}"
evaluator_rc=$?

${EVALUATOR_PYTHON} -m dumatebench.evaluator.llm_judge.ppt \
  --task-dir "${TASK_DIR}" \
  --instruction-file instruction.md \
  --input-file "${JUDGE_INPUT_FILE}" \
  --output-file "${JUDGE_OUTPUT_FILE}" \
  --model "${JUDGE_MODEL}" \
  --min-score "${JUDGE_MIN_SCORE}" \
  --judge-output-file "${JUDGE_REPORT_FILE}"
judge_rc=$?
set -e

${EVALUATOR_PYTHON} - "${TASK_DIR}" "${agent_rc}" "${evaluator_rc}" "${judge_rc}" "${JUDGE_MIN_SCORE}" <<'PY'
import json
import sys
from pathlib import Path

task_dir = Path(sys.argv[1])
agent_rc = int(sys.argv[2])
evaluator_rc = int(sys.argv[3])
judge_rc = int(sys.argv[4])
judge_min_score = float(sys.argv[5])

reward_path = task_dir / "run_outputs" / "reward.json"
judge_path = task_dir / "run_outputs" / "ppt_llm_judge.json"
combined_path = task_dir / "run_outputs" / "reward_with_ppt_judge.json"
status_path = task_dir / "run_logs" / "agent_status.json"

reward = json.loads(reward_path.read_text()) if reward_path.exists() else {}
judge = json.loads(judge_path.read_text()) if judge_path.exists() else {}
judge_result = judge.get("result", {})
base_score = max(0.0, min(1.0, float(reward.get("partial_pass", 0.0))))
try:
    judge_score = float(judge_result.get("score", 0.0))
except (TypeError, ValueError):
    judge_score = 0.0
if judge_score > 1.0:
    judge_score = judge_score / 100.0
judge_score = max(0.0, min(1.0, judge_score))
final_score = round((base_score + judge_score) / 2.0, 4)
min_final_score = max(0.0, min(1.0, judge_min_score / 100.0))

combined = dict(reward)
combined["ppt_llm_judge"] = judge_result
combined["base_complete_pass"] = reward.get("complete_pass", 0)
combined["base_partial_pass"] = base_score
combined["ppt_llm_judge_score"] = judge_score
combined["final_score"] = final_score
combined["complete_pass_with_ppt_judge"] = int(final_score >= min_final_score)
combined["partial_pass_with_ppt_judge"] = final_score
combined_path.write_text(json.dumps(combined, indent=2, ensure_ascii=False), encoding="utf-8")

try:
    status = json.loads(status_path.read_text()) if status_path.exists() else {}
except json.JSONDecodeError:
    status = {}
status["agent_container_returncode"] = agent_rc
status["evaluator_returncode"] = evaluator_rc
status["ppt_judge_returncode"] = judge_rc
status["reward_with_ppt_judge"] = str(combined_path)
status_path.parent.mkdir(parents=True, exist_ok=True)
status_path.write_text(json.dumps(status, indent=2, ensure_ascii=False), encoding="utf-8")
PY

echo
echo "Base checklist reward: ${TASK_DIR}/run_outputs/reward.json"
echo "PPT judge report:      ${TASK_DIR}/${JUDGE_REPORT_FILE}"
echo "Combined reward:       ${TASK_DIR}/run_outputs/reward_with_ppt_judge.json"
echo "Agent logs:            ${TASK_DIR}/run_logs/agent_llm.log"

if [ "${evaluator_rc}" -ne 0 ]; then
  exit "${evaluator_rc}"
fi
if [ "${judge_rc}" -ne 0 ]; then
  exit "${judge_rc}"
fi
exit "${agent_rc}"
