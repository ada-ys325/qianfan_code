#!/usr/bin/env bash
set -euo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
REPO_ROOT="$(cd "${ROOT}/.." && pwd)"
TASK_ID="${DUMATE_TASK_ID:-24d6778af4354ccbbd19ee5a5e529beb_ses_124aedee2ffeZgWPdornLUhXG6}"
TASK_DIR="${DUMATE_TASK_DIR:-${ROOT}/datasets/dev/${TASK_ID}}"
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
JUDGE_REPORT_FILE="${DUMATE_JUDGE_REPORT_FILE:-run_outputs/ppt_llm_judge_only.json}"
COMBINED_REWARD_FILE="${DUMATE_COMBINED_REWARD_FILE:-run_outputs/reward_with_ppt_judge_only.json}"
RENDER_SLIDES="${DUMATE_RENDER_SLIDES:-true}"
MAX_RENDERED_SLIDES="${DUMATE_MAX_RENDERED_SLIDES:-8}"

if [ ! -d "${TASK_DIR}" ]; then
  echo "Task directory not found: ${TASK_DIR}" >&2
  exit 2
fi
if [ -z "${OPENAI_API_KEY:-}" ] && [ -z "${DUMATE_JUDGE_MOCK_RESPONSE:-}" ]; then
  echo "OPENAI_API_KEY is required unless DUMATE_JUDGE_MOCK_RESPONSE is set." >&2
  exit 2
fi
if [ -z "${OPENAI_BASE_URL:-}" ] && [ -z "${DUMATE_JUDGE_MOCK_RESPONSE:-}" ]; then
  echo "OPENAI_BASE_URL is required unless DUMATE_JUDGE_MOCK_RESPONSE is set." >&2
  exit 2
fi

export PYTHONPATH="${REPO_ROOT}:${PYTHONPATH:-}"

if ! ${EVALUATOR_PYTHON} - <<'PY' >/dev/null 2>&1
import openai  # noqa: F401
import pptx  # noqa: F401
PY
then
  echo "Host judge Python dependencies are missing. Install them with:" >&2
  echo "  conda run -n dumatebench python -m pip install -r ${ROOT}/requirements.txt" >&2
  exit 2
fi

NO_RENDER_SLIDES=0
if [ "${RENDER_SLIDES}" = "false" ] || [ "${RENDER_SLIDES}" = "0" ]; then
  NO_RENDER_SLIDES=1
else
  if ! command -v soffice >/dev/null 2>&1; then
    echo "warning: soffice was not found on the host PATH; judge will fall back to structure-only evidence." >&2
    echo "         On macOS: brew install --cask libreoffice" >&2
    echo "         Then: export PATH=\"/Applications/LibreOffice.app/Contents/MacOS:\$PATH\"" >&2
  fi
  if ! command -v pdftoppm >/dev/null 2>&1; then
    echo "warning: pdftoppm was not found on the host PATH; judge cannot render slide images." >&2
    echo "         On macOS: brew install poppler" >&2
  fi
fi

mkdir -p "${TASK_DIR}/$(dirname "${JUDGE_REPORT_FILE}")"

run_judge() {
  ${EVALUATOR_PYTHON} -m dumatebench.evaluator.llm_judge.ppt \
    --task-dir "${TASK_DIR}" \
    --instruction-file instruction.md \
    --input-file "${JUDGE_INPUT_FILE}" \
    --output-file "${JUDGE_OUTPUT_FILE}" \
    --model "${JUDGE_MODEL}" \
    --min-score "${JUDGE_MIN_SCORE}" \
    --judge-output-file "${JUDGE_REPORT_FILE}" \
    --combined-reward-file "${COMBINED_REWARD_FILE}" \
    --max-rendered-slides "${MAX_RENDERED_SLIDES}" \
    "$@"
}

if [ "${NO_RENDER_SLIDES}" -eq 1 ] && [ -n "${DUMATE_JUDGE_MOCK_RESPONSE:-}" ]; then
  run_judge --no-render-slides --mock-response "${DUMATE_JUDGE_MOCK_RESPONSE}"
elif [ "${NO_RENDER_SLIDES}" -eq 1 ]; then
  run_judge --no-render-slides
elif [ -n "${DUMATE_JUDGE_MOCK_RESPONSE:-}" ]; then
  run_judge --mock-response "${DUMATE_JUDGE_MOCK_RESPONSE}"
else
  run_judge
fi

echo
echo "PPT judge report: ${TASK_DIR}/${JUDGE_REPORT_FILE}"
echo "Combined reward:  ${TASK_DIR}/${COMBINED_REWARD_FILE}"
