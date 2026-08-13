#!/usr/bin/env bash
set -euo pipefail

usage() {
  cat <<'EOF'
Usage:
  rerun_prepare_web_references_claude.sh TASKS_DIR [extra prepare_web_references.py args...]

Environment overrides:
  PYTHON                 Python executable, default: python3
  COLLECTOR_MODEL        Claude collector model, default: claude-opus-4-8
  VALIDATOR_MODEL        Claude validator model, default: claude-opus-4-8
  TIMEOUT                Per-task timeout seconds, default: 1800
  SUMMARY_FILE           Summary JSONL path, default: TASKS_DIR/web_reference_rerun_summary.jsonl

This wrapper is restart-safe: it uses --skip-existing --skip-existing-mode validated,
so reruns skip only tasks that already have a valid web_reference/validation_manifest.json
with kept reference files. Partial failed outputs are retried.
EOF
}

if [[ "${1:-}" == "-h" || "${1:-}" == "--help" || $# -lt 1 ]]; then
  usage
  exit 0
fi

tasks_dir="$1"
shift

script_dir="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
prepare_script="${script_dir}/prepare_web_references.py"

python_bin="${PYTHON:-python3}"
collector_model="${COLLECTOR_MODEL:-claude-opus-4-8}"
validator_model="${VALIDATOR_MODEL:-claude-opus-4-8}"
timeout_seconds="${TIMEOUT:-1800}"
summary_file="${SUMMARY_FILE:-${tasks_dir%/}/web_reference_rerun_summary.jsonl}"

exec "${python_bin}" "${prepare_script}" \
  --tasks-dir "${tasks_dir}" \
  --collector-backend claude \
  --collector-model "${collector_model}" \
  --claude-collector-permission-mode auto \
  --validator-model "${validator_model}" \
  --timeout "${timeout_seconds}" \
  --download-assets \
  --skip-existing \
  --skip-existing-mode validated \
  --continue-on-error \
  --summary-file "${summary_file}" \
  "$@"
