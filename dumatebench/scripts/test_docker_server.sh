#!/usr/bin/env bash
set -Eeuo pipefail

ROOT="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
TASK_NAME="${TASK_NAME:-odyssey_2_12_smoke}"
TASK_DIR="${TASK_DIR:-${ROOT}/datasets/dev/${TASK_NAME}}"
COMPOSE_FILE="${COMPOSE_FILE:-${TASK_DIR}/environment/docker-compose.yaml}"
LOG_DIR="${LOG_DIR:-${TASK_DIR}/run_logs/docker_server_test}"
OUTPUT_DIR="${OUTPUT_DIR:-${TASK_DIR}/run_outputs}"
BUILD_TIMEOUT="${BUILD_TIMEOUT:-45m}"
RUN_TIMEOUT="${RUN_TIMEOUT:-30m}"
EVAL_TIMEOUT="${EVAL_TIMEOUT:-10m}"
HEARTBEAT_INTERVAL="${HEARTBEAT_INTERVAL:-30}"
PYTHON_BIN="${PYTHON_BIN:-python3}"
SKIP_EVAL="${SKIP_EVAL:-0}"
NO_CACHE="${NO_CACHE:-0}"
KEEP_CONTAINER="${KEEP_CONTAINER:-0}"
TAIL_LINES="${TAIL_LINES:-120}"
BUILD_METHOD="${BUILD_METHOD:-compose}"
IMAGE_NAME="${IMAGE_NAME:-dumatebench-odyssey-2-12-smoke:latest}"
USE_TEMP_CONTEXT="${USE_TEMP_CONTEXT:-0}"
CONTEXT_SCAN="${CONTEXT_SCAN:-1}"
TEMP_CONTEXT_DIR="${TEMP_CONTEXT_DIR:-/tmp/dumatebench-build-context-${TASK_NAME}-$$}"
TEMP_CONTEXT_CREATED=0
RUN_PREFLIGHT="${RUN_PREFLIGHT:-1}"
PREFLIGHT_TIMEOUT="${PREFLIGHT_TIMEOUT:-10m}"
PROBE_CONTEXT_DIR="${PROBE_CONTEXT_DIR:-/tmp/dumatebench-docker-probe-$$}"
PROBE_CONTEXT_CREATED=0

export COMPOSE_BAKE="${COMPOSE_BAKE:-false}"
export COMPOSE_DOCKER_CLI_BUILD="${COMPOSE_DOCKER_CLI_BUILD:-1}"
export DOCKER_BUILDKIT="${DOCKER_BUILDKIT:-1}"
export BUILDKIT_PROGRESS="${BUILDKIT_PROGRESS:-plain}"

BUILD_LOG="${LOG_DIR}/build.log"
RUN_LOG="${LOG_DIR}/run.log"
EVAL_LOG="${LOG_DIR}/eval.log"
COMPOSE_LOG="${LOG_DIR}/compose.log"
CONTEXT_LOG="${LOG_DIR}/context.log"
PREFLIGHT_LOG="${LOG_DIR}/preflight.log"
PROBE_LOG="${LOG_DIR}/probe_build.log"

usage() {
  cat <<USAGE
Usage:
  dumatebench/scripts/test_docker_server.sh

Useful environment variables:
  TASK_NAME=odyssey_2_12_smoke      Dataset task under dumatebench/datasets/dev.
  TASK_DIR=/path/to/task            Full task directory, overrides TASK_NAME.
  BUILD_TIMEOUT=45m                 Timeout for docker compose build.
  RUN_TIMEOUT=30m                   Timeout for the smoke container run.
  EVAL_TIMEOUT=10m                  Timeout for the Python evaluator.
  HEARTBEAT_INTERVAL=30             Seconds between "still running" messages.
  BUILD_METHOD=compose              Use "compose" or "docker" for image build.
  IMAGE_NAME=dumatebench-...:latest Image tag for BUILD_METHOD=docker.
  CONTEXT_SCAN=1                    Print build-context size and largest files.
  USE_TEMP_CONTEXT=1                Build from a minimal /tmp context.
  RUN_PREFLIGHT=1                   Pull base image and build a tiny probe image.
  PREFLIGHT_TIMEOUT=10m             Timeout for each Docker preflight step.
  NO_CACHE=1                        Build with --no-cache.
  SKIP_EVAL=1                       Skip the Python evaluator.
  KEEP_CONTAINER=1                  Do not run docker compose down on exit.
  TAIL_LINES=120                    Lines to print from logs when a step fails.
  DUMATE_BASE_IMAGE=python:3.12-slim
  DUMATE_APT_DEBIAN_MIRROR=http://...
  DUMATE_APT_SECURITY_MIRROR=http://...

Logs:
  ${LOG_DIR}
USAGE
}

die() {
  echo "[docker-test] ERROR: $*" >&2
  exit 1
}

have_timeout() {
  command -v timeout >/dev/null 2>&1
}

log_size() {
  local log_file="$1"
  if [ -f "${log_file}" ]; then
    wc -c < "${log_file}" | tr -d ' '
  else
    echo 0
  fi
}

print_tail() {
  local log_file="$1"
  if [ -f "${log_file}" ]; then
    echo
    echo "[docker-test] last ${TAIL_LINES} lines from ${log_file}:"
    tail -n "${TAIL_LINES}" "${log_file}" || true
  fi
}

copy_path_to_context() {
  local relative_path="$1"
  local source_path="${ROOT}/${relative_path}"
  local dest_path="${TEMP_CONTEXT_DIR}/${relative_path}"

  [ -e "${source_path}" ] || die "required build-context path is missing: ${source_path}"
  mkdir -p "$(dirname "${dest_path}")"
  cp -a "${source_path}" "${dest_path}"
}

inspect_context() {
  [ "${CONTEXT_SCAN}" = "1" ] || return 0

  echo
  echo "[docker-test] === build context scan ==="
  echo "[docker-test] context root: ${ROOT}"
  echo "[docker-test] log: ${CONTEXT_LOG}"
  {
    echo "[docker-test] context root: ${ROOT}"
    echo "[docker-test] total size:"
    du -sh "${ROOT}" 2>&1 || true
    echo
    echo "[docker-test] largest top-level entries:"
    du -xh -d 2 "${ROOT}" 2>/dev/null | sort -h | tail -n 30 || true
    echo
    echo "[docker-test] largest files:"
    find "${ROOT}" -xdev -type f -printf '%s %p\n' 2>/dev/null | sort -n | tail -n 30 || true
  } | tee "${CONTEXT_LOG}"
}

prepare_temp_context() {
  [ "${USE_TEMP_CONTEXT}" = "1" ] || return 0

  echo
  echo "[docker-test] === prepare minimal build context ==="
  echo "[docker-test] temp context: ${TEMP_CONTEXT_DIR}"
  rm -rf "${TEMP_CONTEXT_DIR}"
  mkdir -p "${TEMP_CONTEXT_DIR}"
  TEMP_CONTEXT_CREATED=1

  copy_path_to_context "agents/command_agent.py"
  copy_path_to_context "datasets/dev/${TASK_NAME}/workspace_seed"
  copy_path_to_context "datasets/dev/${TASK_NAME}/instruction.md"
  copy_path_to_context "datasets/dev/${TASK_NAME}/task.yaml"
  copy_path_to_context "datasets/dev/${TASK_NAME}/evaluator"
  copy_path_to_context "datasets/dev/${TASK_NAME}/network_faults.yaml"
  copy_path_to_context "datasets/dev/${TASK_NAME}/tool_faults.yaml"
  copy_path_to_context "datasets/dev/${TASK_NAME}/environment"

  du -sh "${TEMP_CONTEXT_DIR}" || true
}

prepare_probe_context() {
  rm -rf "${PROBE_CONTEXT_DIR}"
  mkdir -p "${PROBE_CONTEXT_DIR}"
  PROBE_CONTEXT_CREATED=1
  {
    printf 'ARG DUMATE_BASE_IMAGE=python:3.12-slim\n'
    printf 'FROM ${DUMATE_BASE_IMAGE}\n'
    printf 'RUN python --version\n'
  } > "${PROBE_CONTEXT_DIR}/Dockerfile"
}

docker_build_command_prefix() {
  if [ "${DOCKER_BUILDKIT:-1}" = "0" ]; then
    printf '%s\n' "docker build"
  else
    printf '%s\n' "docker build --progress=plain"
  fi
}

run_preflight() {
  [ "${RUN_PREFLIGHT}" = "1" ] || return 0

  echo
  echo "[docker-test] === docker preflight ==="
  echo "[docker-test] This checks whether Docker can pull and build a tiny image before using the project context."
  {
    echo "[docker-test] docker info:"
    docker info
    echo
    echo "[docker-test] docker buildx version:"
    docker buildx version 2>&1 || true
    echo
    echo "[docker-test] docker buildx ls:"
    docker buildx ls 2>&1 || true
  } | tee "${PREFLIGHT_LOG}"

  local base_image="${DUMATE_BASE_IMAGE:-python:3.12-slim}"
  run_logged "docker pull base image" "${PREFLIGHT_TIMEOUT}" "${PREFLIGHT_LOG}.pull" \
    docker pull "${base_image}"

  prepare_probe_context
  local probe_build_args
  read -r -a probe_build_args <<< "$(docker_build_command_prefix)"
  run_logged "tiny docker build probe" "${PREFLIGHT_TIMEOUT}" "${PROBE_LOG}" \
    "${probe_build_args[@]}" \
      -t "dumatebench-docker-probe:latest" \
      -f "${PROBE_CONTEXT_DIR}/Dockerfile" \
      --build-arg "DUMATE_BASE_IMAGE=${base_image}" \
      "${PROBE_CONTEXT_DIR}"
}

run_logged() {
  local label="$1"
  local timeout_value="$2"
  local log_file="$3"
  shift 3

  echo
  echo "[docker-test] === ${label} ==="
  echo "[docker-test] command: $*"
  echo "[docker-test] log: ${log_file}"
  mkdir -p "$(dirname "${log_file}")"
  : > "${log_file}"

  local status=0 cmd_pid="" tail_pid="" last_size=0 same_size_ticks=0 elapsed=0
  set +e
  if have_timeout; then
    timeout "${timeout_value}" "$@" > "${log_file}" 2>&1 &
  else
    echo "[docker-test] command 'timeout' was not found; running without timeout" | tee -a "${log_file}"
    "$@" >> "${log_file}" 2>&1 &
  fi
  cmd_pid=$!

  tail -n +1 -f "${log_file}" &
  tail_pid=$!

  while kill -0 "${cmd_pid}" 2>/dev/null; do
    local waited=0
    while [ "${waited}" -lt "${HEARTBEAT_INTERVAL}" ] && kill -0 "${cmd_pid}" 2>/dev/null; do
      sleep 1
      waited=$((waited + 1))
    done
    kill -0 "${cmd_pid}" 2>/dev/null || break
    elapsed=$((elapsed + HEARTBEAT_INTERVAL))
    local current_size
    current_size="$(log_size "${log_file}")"
    if [ "${current_size}" = "${last_size}" ]; then
      same_size_ticks=$((same_size_ticks + 1))
      echo "[docker-test] ${label} still running; no new log output for $((same_size_ticks * HEARTBEAT_INTERVAL))s, elapsed ${elapsed}s"
    else
      same_size_ticks=0
      echo "[docker-test] ${label} still running; log grew from ${last_size} to ${current_size} bytes, elapsed ${elapsed}s"
      last_size="${current_size}"
    fi
  done

  wait "${cmd_pid}"
  status=$?
  kill "${tail_pid}" >/dev/null 2>&1 || true
  wait "${tail_pid}" >/dev/null 2>&1 || true

  if have_timeout && [ "${status}" -eq 124 ]; then
    echo "[docker-test] ${label} timed out after ${timeout_value}" | tee -a "${log_file}"
  fi
  set -e

  if [ "${status}" -ne 0 ]; then
    echo "[docker-test] ${label} failed with exit code ${status}" >&2
    print_tail "${log_file}"
    return "${status}"
  fi
}

cleanup() {
  mkdir -p "${LOG_DIR}"
  docker compose -f "${COMPOSE_FILE}" logs --no-color > "${COMPOSE_LOG}" 2>/dev/null || true
  if [ "${KEEP_CONTAINER}" != "1" ]; then
    docker compose -f "${COMPOSE_FILE}" down --remove-orphans >/dev/null 2>&1 || true
  fi
  if [ "${TEMP_CONTEXT_CREATED}" = "1" ]; then
    rm -rf "${TEMP_CONTEXT_DIR}" || true
  fi
  if [ "${PROBE_CONTEXT_CREATED}" = "1" ]; then
    rm -rf "${PROBE_CONTEXT_DIR}" || true
  fi
}

main() {
  if [ "${1:-}" = "-h" ] || [ "${1:-}" = "--help" ]; then
    usage
    exit 0
  fi

  command -v docker >/dev/null 2>&1 || die "docker is not installed or not in PATH"
  docker compose version >/dev/null 2>&1 || die "docker compose is not available"
  [ -f "${COMPOSE_FILE}" ] || die "compose file not found: ${COMPOSE_FILE}"
  [ -d "${TASK_DIR}" ] || die "task directory not found: ${TASK_DIR}"

  mkdir -p "${LOG_DIR}" "${OUTPUT_DIR}" "${TASK_DIR}/run_logs"
  rm -rf "${OUTPUT_DIR:?}"/* "${TASK_DIR}/run_logs"/*
  mkdir -p "${LOG_DIR}"

  trap cleanup EXIT

  echo "[docker-test] root: ${ROOT}"
  echo "[docker-test] task: ${TASK_DIR}"
  echo "[docker-test] compose: ${COMPOSE_FILE}"
  echo "[docker-test] logs: ${LOG_DIR}"
  echo "[docker-test] docker:"
  docker version
  echo "[docker-test] docker compose:"
  docker compose version
  echo "[docker-test] build method: ${BUILD_METHOD}"
  echo "[docker-test] COMPOSE_BAKE=${COMPOSE_BAKE} DOCKER_BUILDKIT=${DOCKER_BUILDKIT} BUILDKIT_PROGRESS=${BUILDKIT_PROGRESS}"
  echo "[docker-test] USE_TEMP_CONTEXT=${USE_TEMP_CONTEXT} CONTEXT_SCAN=${CONTEXT_SCAN} RUN_PREFLIGHT=${RUN_PREFLIGHT}"

  docker compose -f "${COMPOSE_FILE}" down --remove-orphans >/dev/null 2>&1 || true

  inspect_context
  run_preflight
  prepare_temp_context

  if [ "${BUILD_METHOD}" = "docker" ]; then
    local build_context="${ROOT}"
    if [ "${USE_TEMP_CONTEXT}" = "1" ]; then
      build_context="${TEMP_CONTEXT_DIR}"
    fi
    local dockerfile_path="${build_context}/datasets/dev/${TASK_NAME}/environment/Dockerfile"
    local docker_build_args
    read -r -a docker_build_args <<< "$(docker_build_command_prefix)"
    local build_args=(
      "${docker_build_args[@]}"
      -t "${IMAGE_NAME}"
      -f "${dockerfile_path}"
      --build-arg "DUMATE_BASE_IMAGE=${DUMATE_BASE_IMAGE:-python:3.12-slim}"
      --build-arg "DUMATE_APT_DEBIAN_MIRROR=${DUMATE_APT_DEBIAN_MIRROR:-}"
      --build-arg "DUMATE_APT_SECURITY_MIRROR=${DUMATE_APT_SECURITY_MIRROR:-}"
    )
    if [ "${NO_CACHE}" = "1" ]; then
      build_args+=(--no-cache)
    fi
    build_args+=("${build_context}")
    run_logged "docker build" "${BUILD_TIMEOUT}" "${BUILD_LOG}" "${build_args[@]}"
  elif [ "${BUILD_METHOD}" = "compose" ]; then
    if [ "${USE_TEMP_CONTEXT}" = "1" ]; then
      die "USE_TEMP_CONTEXT=1 requires BUILD_METHOD=docker"
    fi
    local build_args=(docker compose --progress=plain -f "${COMPOSE_FILE}" build)
    if [ "${NO_CACHE}" = "1" ]; then
      build_args+=(--no-cache)
    fi
    run_logged "docker compose build" "${BUILD_TIMEOUT}" "${BUILD_LOG}" "${build_args[@]}"
  else
    die "unsupported BUILD_METHOD=${BUILD_METHOD}; use compose or docker"
  fi

  run_logged "smoke container" "${RUN_TIMEOUT}" "${RUN_LOG}" \
    docker compose -f "${COMPOSE_FILE}" run --rm task /opt/dumate/agent_smoke.sh

  if [ "${SKIP_EVAL}" != "1" ]; then
    run_logged "evaluator" "${EVAL_TIMEOUT}" "${EVAL_LOG}" \
      "${PYTHON_BIN}" "${TASK_DIR}/evaluator/evaluator.py" --task-dir "${TASK_DIR}"
  fi

  echo
  echo "[docker-test] PASS"
  echo "[docker-test] build log: ${BUILD_LOG}"
  echo "[docker-test] run log: ${RUN_LOG}"
  if [ "${SKIP_EVAL}" != "1" ]; then
    echo "[docker-test] eval log: ${EVAL_LOG}"
  fi
  echo "[docker-test] compose log: ${COMPOSE_LOG}"
}

main "$@"
