#!/usr/bin/env python3
"""Prepare web_reference folders for DuMateBench tasks that need web retrieval."""

from __future__ import annotations

import argparse
import json
import os
import re
import shlex
import shutil
import subprocess
import sys
import time
import urllib.request
from dataclasses import dataclass
from datetime import date
from pathlib import Path
from typing import Any


ROOT = Path(__file__).resolve().parents[1]
REPO_ROOT = ROOT.parent
DEFAULT_TASKS_DIR = ROOT / "datasets/dev"
FEATURE_FILE = "task_type_feature.json"
INSTRUCTION_FILE = "instruction.md"
CHECKS_FILE = Path("evaluator/checks.yaml")
WEB_REFERENCE_DIR = "web_reference"
REJECTED_DIR = "web_reference_rejected"
ASSETS_DIR = "assets"
CODEX_CLI_FALLBACKS = [
    Path("/Applications/ChatGPT.app/Contents/Resources/codex"),
]

CURRENT_DAY_PATTERNS = [re.compile(pattern) for pattern in [r"今天", r"今日", r"当天", r"本日"]]
CURRENT_YEAR_PATTERNS = [re.compile(pattern) for pattern in [r"今年", r"本年"]]
RECENT_DAY_PATTERNS = [
    re.compile(pattern)
    for pattern in [
        r"近几天",
        r"最近几天",
        r"近些天",
        r"这几天",
        r"近一周",
        r"最近一周",
        r"近几日",
        r"最近几日",
    ]
]
RECENT_YEAR_PATTERNS = [
    re.compile(pattern)
    for pattern in [
        r"(?<!截至)(?<!截止)(?<!到)(?<!至)(?<!过去)近几年",
        r"最近几年",
        r"近些年",
        r"近年来",
        r"最近一段时间",
    ]
]
YEAR_RE = re.compile(r"(?:19|20)\d{2}\s*年|截至\s*(?:19|20)\d{2}|截止到?\s*(?:19|20)\d{2}")
DATE_RE = re.compile(r"(?:19|20)\d{2}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*[日号]?|(?:19|20)\d{2}[-/.]\d{1,2}[-/.]\d{1,2}")


@dataclass
class TaskResult:
    task_id: str
    task_dir: str
    time_updates: list[dict[str, str]]
    collected: bool
    kept_files: list[str]
    rejected_files: list[str]
    kept_assets: list[str]
    rejected_assets: list[str]
    status: str
    error: str = ""

    def as_dict(self) -> dict[str, Any]:
        return {
            "task_id": self.task_id,
            "task_dir": self.task_dir,
            "time_updates": self.time_updates,
            "collected": self.collected,
            "kept_files": self.kept_files,
            "rejected_files": self.rejected_files,
            "kept_assets": self.kept_assets,
            "rejected_assets": self.rejected_assets,
            "status": self.status,
            "error": self.error,
        }


def parse_args(argv: list[str] | None = None) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description=(
            "Find tasks whose task_type_feature.json has web_retrieval=1, clarify vague "
            "relative time wording, collect web references with Codex or Claude Code, then validate them with Claude."
        )
    )
    parser.add_argument("--tasks-dir", type=Path, default=DEFAULT_TASKS_DIR, help="Root to scan recursively.")
    parser.add_argument("--limit", type=int, default=0, help="Maximum number of web tasks to process; 0 means all.")
    parser.add_argument("--summary-file", type=Path, default=None, help="JSONL summary path.")
    parser.add_argument("--current-date", default=date.today().isoformat(), help="Current date in YYYY-MM-DD.")
    parser.add_argument("--current-year", type=int, default=None, help="Current year; defaults to the year of --current-date.")
    parser.add_argument(
        "--collector-backend",
        choices=["codex", "claude"],
        default="codex",
        help="Collection backend. codex uses Codex CLI; claude uses Claude Code CLI.",
    )
    parser.add_argument(
        "--collector-command",
        default="codex",
        help="Command used for collection. Defaults to codex; with --collector-backend claude, the implicit default becomes claude.",
    )
    parser.add_argument("--collector-model", default="gpt-5.5")
    parser.add_argument(
        "--collector-full-auto",
        action="store_true",
        help="Pass --full-auto to `codex exec`. Off by default because some server Codex builds fail with it.",
    )
    parser.add_argument(
        "--legacy-codex-approval",
        action="store_true",
        help="Pass --ask-for-approval never before `exec` for Codex versions that need it.",
    )
    parser.add_argument(
        "--collector-no-sandbox",
        action="store_true",
        help="Run Codex with --dangerously-bypass-approvals-and-sandbox. Useful on servers missing Codex sandbox helpers.",
    )
    parser.add_argument(
        "--collector-ephemeral",
        action="store_true",
        help="Pass --ephemeral to `codex exec`. Off by default because some server Codex builds fail with it.",
    )
    parser.add_argument(
        "--collector-shell",
        default="auto",
        help="Shell path passed to Codex through SHELL. Use auto, empty string to inherit, or e.g. /bin/bash.",
    )
    parser.add_argument(
        "--claude-collector-permission-mode",
        default="auto",
        help="Claude Code permission mode for collection. auto uses bypassPermissions except under root/sudo; use empty string to omit.",
    )
    parser.add_argument(
        "--claude-collector-allowed-tools",
        default="WebSearch,WebFetch,Bash,Read,Write,Edit,LS",
        help="Comma-separated Claude Code tools allowed for collection; use empty string to omit.",
    )
    parser.add_argument("--validator-command", default="claude", help="Command used for validation.")
    parser.add_argument("--validator-model", default="claude-opus-4-8")
    parser.add_argument(
        "--validator-backend",
        choices=["auto", "cli", "openai-compatible"],
        default="auto",
        help="Validation backend. auto uses Claude CLI when available, otherwise OpenAI-compatible chat completions.",
    )
    parser.add_argument(
        "--validator-base-url",
        default=os.environ.get("OPENAI_BASE_URL") or os.environ.get("CUSTOM_BASE_URL") or "https://cn.huayanapi.com:27502/v1",
        help="OpenAI-compatible base URL used by --validator-backend openai-compatible.",
    )
    parser.add_argument("--validator-api-key-env", default="OPENAI_API_KEY")
    parser.add_argument("--timeout", type=int, default=1800, help="Timeout in seconds for each model command.")
    parser.add_argument("--continue-on-error", action="store_true")
    parser.add_argument(
        "--skip-existing",
        action="store_true",
        help="Skip tasks that already have existing web references according to --skip-existing-mode.",
    )
    parser.add_argument(
        "--skip-existing-mode",
        choices=["validated", "any"],
        default="validated",
        help=(
            "When --skip-existing is set, validated skips only tasks with a successful validation_manifest.json; "
            "any skips tasks that already have md/txt/json reference files."
        ),
    )
    parser.add_argument("--preflight-only", action="store_true", help="Only check collector/validator prerequisites and exit.")
    parser.add_argument(
        "--download-assets",
        action="store_true",
        help=f"Ask Codex to download useful images/PDFs/etc. into {WEB_REFERENCE_DIR}/{ASSETS_DIR}/.",
    )
    parser.add_argument(
        "--time-fix-only",
        action="store_true",
        help="Only clarify vague time wording in instruction.md and evaluator/checks.yaml; do not collect or validate references.",
    )
    parser.add_argument("--dry-run", action="store_true", help="Do not call external model CLIs; write deterministic test references.")
    parser.add_argument("--no-time-fix", action="store_true", help="Do not edit instruction.md/checks.yaml.")
    args = parser.parse_args(argv)
    args.current_date_obj = parse_current_date(args.current_date)
    if args.current_year is None:
        args.current_year = args.current_date_obj.year
    return args


def resolve_path(path: Path) -> Path:
    if path.is_absolute():
        return path
    if path.exists():
        return path
    repo_path = REPO_ROOT / path
    return repo_path if repo_path.exists() else path


def load_json(path: Path) -> dict[str, Any]:
    try:
        obj = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}
    return obj if isinstance(obj, dict) else {}


def is_web_task(feature_path: Path) -> bool:
    return int(load_json(feature_path).get("web_retrieval", 0) or 0) == 1


def discover_web_tasks(tasks_dir: Path, limit: int = 0) -> list[Path]:
    if not tasks_dir.is_dir():
        raise SystemExit(f"tasks dir not found: {tasks_dir}")
    task_dirs = sorted(
        {path.parent for path in tasks_dir.rglob(FEATURE_FILE) if path.is_file() and is_web_task(path)},
        key=lambda path: str(path.relative_to(tasks_dir)),
    )
    return task_dirs[:limit] if limit > 0 else task_dirs


def parse_current_date(value: str) -> date:
    try:
        return date.fromisoformat(value)
    except ValueError as exc:
        raise SystemExit(f"--current-date must use YYYY-MM-DD, got: {value}") from exc


def format_chinese_date(value: date) -> str:
    return f"{value.year}年{value.month}月{value.day}日"


def _has_nearby_year(text: str, start: int, end: int) -> bool:
    right = min(len(text), end + 36)
    return bool(YEAR_RE.search(text[end:right]))


def _has_nearby_date(text: str, start: int, end: int) -> bool:
    right = min(len(text), end + 48)
    return bool(DATE_RE.search(text[end:right]))


def _apply_time_patterns(
    text: str,
    patterns: list[re.Pattern[str]],
    replacement_suffix: str,
    replacements: list[str],
    *,
    require_date: bool = False,
) -> str:
    def replace_match(match: re.Match[str]) -> str:
        has_context = _has_nearby_date(text, match.start(), match.end()) if require_date else _has_nearby_year(
            text, match.start(), match.end()
        )
        if has_context:
            return match.group(0)
        phrase = match.group(0)
        replacements.append(phrase)
        return f"{phrase}{replacement_suffix}"

    updated = text
    for pattern in patterns:
        source = updated
        updated = pattern.sub(lambda match: replace_match(match), updated)
        if updated != source:
            text = updated
    return updated


def clarify_vague_time_text(text: str, current_year: int, current_date: date | None = None) -> tuple[str, list[str]]:
    current_date = current_date or date(current_year, 1, 1)
    replacements: list[str] = []
    current_date_text = format_chinese_date(current_date)
    updated = text
    updated = _apply_time_patterns(updated, CURRENT_DAY_PATTERNS, f"（指 {current_date_text}）", replacements, require_date=True)
    updated = _apply_time_patterns(updated, CURRENT_YEAR_PATTERNS, f"（指 {current_year} 年）", replacements)
    updated = _apply_time_patterns(
        updated,
        RECENT_DAY_PATTERNS,
        f"（以 {current_date_text} 为当前日期）",
        replacements,
        require_date=True,
    )
    updated = _apply_time_patterns(updated, RECENT_YEAR_PATTERNS, f"（以 {current_year} 年为当前年份）", replacements)
    return updated, replacements


def clarify_task_time(task_dir: Path, current_year: int, current_date: date | None = None) -> list[dict[str, str]]:
    updates: list[dict[str, str]] = []
    for rel_path in [Path(INSTRUCTION_FILE), CHECKS_FILE]:
        path = task_dir / rel_path
        if not path.is_file():
            continue
        original = path.read_text(encoding="utf-8", errors="ignore")
        updated, replacements = clarify_vague_time_text(original, current_year, current_date)
        if updated == original:
            continue
        path.write_text(updated, encoding="utf-8")
        updates.append({"file": str(rel_path), "phrases": ", ".join(sorted(set(replacements)))})
    return updates


def read_optional(path: Path, max_chars: int = 12000) -> str:
    if not path.is_file():
        return ""
    text = path.read_text(encoding="utf-8", errors="ignore")
    return text[:max_chars]


def build_collector_prompt(
    task_dir: Path,
    current_year: int,
    download_assets: bool = False,
    current_date: date | None = None,
) -> str:
    instruction = read_optional(task_dir / INSTRUCTION_FILE)
    checks = read_optional(task_dir / CHECKS_FILE)
    current_date_text = format_chinese_date(current_date or date(current_year, 1, 1))
    asset_requirements = ""
    if download_assets:
        asset_requirements = f"""
6. 如任务需要图片、PDF、网页截图、数据文件等原始资源，请下载到 `{WEB_REFERENCE_DIR}/{ASSETS_DIR}/`。
7. assets 文件名必须稳定、可读，使用 `asset_序号_简短主题.扩展名`；不要保存无关、重复、低清、侵权风险高或无法核验来源的资源。
8. 每个下载的 asset 必须在对应 `ref_*.md` 中注明相对路径、原始 URL、来源页面、访问日期、文件用途，以及为什么该资源对任务必要。
9. 如果无法合法或可靠地下载原始资源，请不要伪造文件；在 `ref_*.md` 中记录来源 URL 和未下载原因。
"""
    return f"""你是 DuMateBench 的网络资料搜集助手。请使用实时网络检索，为下面任务搜集完成任务所需的真实、高质量资料。

要求：
1. 只把资料写入当前任务目录下的 `{WEB_REFERENCE_DIR}` 文件夹。
2. 为每个有用来源写一个 Markdown 文件，文件名使用 `ref_序号_简短主题.md`。
3. 每个文件必须包含：标题、URL、访问日期、发布时间或适用时间、与任务相关的关键事实摘要、为什么对任务有用。
4. 优先使用官方来源、权威机构、原始数据页或可信媒体；不要保存明显无关、过时、低质量或无法核验的内容。
5. 当前日期按 {current_date_text} 理解；当前年份按 {current_year} 年理解。
{asset_requirements}

instruction.md:
{instruction}

evaluator/checks.yaml:
{checks}
"""


def command_parts(command: str, *, fallbacks: list[Path] | None = None) -> list[str]:
    parts = shlex.split(command)
    if not parts:
        raise ValueError("empty command")
    executable = parts[0]
    if shutil.which(executable) or Path(executable).is_file():
        return parts
    for fallback in fallbacks or []:
        if fallback.is_file():
            return [str(fallback), *parts[1:]]
    return parts


def command_available(command: str) -> bool:
    try:
        parts = command_parts(command)
    except ValueError:
        return False
    return bool(shutil.which(parts[0]) or Path(parts[0]).is_file())


def build_codex_command(
    command: str,
    model: str,
    task_dir: Path,
    prompt: str,
    *,
    legacy_approval: bool = False,
    full_auto: bool = False,
    no_sandbox: bool = False,
    ephemeral: bool = False,
) -> list[str]:
    cmd = [
        *command_parts(command, fallbacks=CODEX_CLI_FALLBACKS),
        "--search",
    ]
    if no_sandbox:
        cmd.append("--dangerously-bypass-approvals-and-sandbox")
    elif legacy_approval:
        cmd.extend(["--ask-for-approval", "never"])
    cmd.extend(
        [
            "exec",
            *(["--full-auto"] if full_auto and not legacy_approval and not no_sandbox else []),
            "--skip-git-repo-check",
            *(["--ephemeral"] if ephemeral else []),
            "-m",
            model,
            "-C",
            str(task_dir.resolve()),
            *(["-s", "workspace-write"] if not no_sandbox else []),
            prompt,
        ]
    )
    return cmd


def effective_collector_command(args: argparse.Namespace) -> str:
    if args.collector_backend == "claude" and args.collector_command == "codex":
        return "claude"
    return args.collector_command


def build_claude_collector_command(
    command: str,
    model: str,
    prompt: str,
    *,
    permission_mode: str = "auto",
    allowed_tools: str = "WebSearch,WebFetch,Bash,Read,Write,Edit,LS",
) -> list[str]:
    cmd = [*command_parts(command), "-p", prompt, "--model", model]
    permission_mode = resolve_claude_permission_mode(permission_mode)
    if permission_mode:
        cmd.extend(["--permission-mode", permission_mode])
    if allowed_tools.strip():
        cmd.extend(["--allowedTools", allowed_tools.strip()])
    return cmd


def running_as_root() -> bool:
    return hasattr(os, "geteuid") and os.geteuid() == 0


def resolve_claude_permission_mode(permission_mode: str) -> str:
    permission_mode = permission_mode.strip()
    if permission_mode != "auto":
        return permission_mode
    return "" if running_as_root() or os.environ.get("SUDO_USER") else "bypassPermissions"


def resolve_collector_shell(shell: str) -> str:
    shell = shell.strip()
    if shell == "":
        return ""
    if shell != "auto":
        if not Path(shell).is_file():
            raise FileNotFoundError(f"collector shell not found: {shell}")
        return shell
    env_shell = os.environ.get("SHELL", "").strip()
    if env_shell and Path(env_shell).is_file():
        return env_shell
    for candidate in ["/bin/bash", "/usr/bin/bash", "/bin/sh", "/usr/bin/sh"]:
        if Path(candidate).is_file():
            return candidate
    return ""


def run_codex_collection(
    task_dir: Path,
    model: str,
    command: str,
    timeout: int,
    current_year: int,
    current_date: date,
    download_assets: bool,
    collector_shell: str = "auto",
    legacy_approval: bool = False,
    full_auto: bool = False,
    no_sandbox: bool = False,
    ephemeral: bool = False,
) -> None:
    web_dir = task_dir / WEB_REFERENCE_DIR
    web_dir.mkdir(parents=True, exist_ok=True)
    if download_assets:
        (web_dir / ASSETS_DIR).mkdir(parents=True, exist_ok=True)
    prompt = build_collector_prompt(task_dir, current_year, download_assets, current_date)
    cmd = build_codex_command(
        command,
        model,
        task_dir,
        prompt,
        legacy_approval=legacy_approval,
        full_auto=full_auto,
        no_sandbox=no_sandbox,
        ephemeral=ephemeral,
    )
    env = os.environ.copy()
    shell = resolve_collector_shell(collector_shell)
    if shell:
        env["SHELL"] = shell
    try:
        subprocess.run(cmd, cwd=task_dir, check=True, timeout=timeout, env=env)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"collector command not found: {command!r}. Install Codex CLI or pass --collector-command /path/to/codex."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(
            f"collector command failed with exit code {exc.returncode}. "
            "If the log contains 'No such file or directory (os error 2)' on Linux, "
            "rerun with --collector-shell /bin/bash or install the shell named by $SHELL."
        ) from exc


def run_claude_collection(
    task_dir: Path,
    model: str,
    command: str,
    timeout: int,
    current_year: int,
    current_date: date,
    download_assets: bool,
    permission_mode: str = "auto",
    allowed_tools: str = "WebSearch,WebFetch,Bash,Read,Write,Edit,LS",
) -> None:
    web_dir = task_dir / WEB_REFERENCE_DIR
    web_dir.mkdir(parents=True, exist_ok=True)
    if download_assets:
        (web_dir / ASSETS_DIR).mkdir(parents=True, exist_ok=True)
    prompt = build_collector_prompt(task_dir, current_year, download_assets, current_date)
    cmd = build_claude_collector_command(
        command,
        model,
        prompt,
        permission_mode=permission_mode,
        allowed_tools=allowed_tools,
    )
    try:
        subprocess.run(cmd, cwd=task_dir, check=True, timeout=timeout)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"collector command not found: {command!r}. Install Claude Code CLI or pass --collector-command /path/to/claude."
        ) from exc
    except subprocess.CalledProcessError as exc:
        raise RuntimeError(f"Claude collector command failed with exit code {exc.returncode}.") from exc


def run_collection(task_dir: Path, args: argparse.Namespace) -> None:
    command = effective_collector_command(args)
    if args.collector_backend == "claude":
        run_claude_collection(
            task_dir,
            args.collector_model,
            command,
            args.timeout,
            args.current_year,
            args.current_date_obj,
            args.download_assets,
            args.claude_collector_permission_mode,
            args.claude_collector_allowed_tools,
        )
        return
    run_codex_collection(
        task_dir,
        args.collector_model,
        command,
        args.timeout,
        args.current_year,
        args.current_date_obj,
        args.download_assets,
        args.collector_shell,
        args.legacy_codex_approval,
        args.collector_full_auto,
        args.collector_no_sandbox,
        args.collector_ephemeral,
    )


def preflight_collector(tasks_dir: Path, args: argparse.Namespace) -> None:
    prompt = "请只回复 ok，不要创建或修改文件。"
    env = os.environ.copy()
    command = effective_collector_command(args)
    if args.collector_backend == "claude":
        cmd = build_claude_collector_command(
            command,
            args.collector_model,
            prompt,
            permission_mode=args.claude_collector_permission_mode,
            allowed_tools=args.claude_collector_allowed_tools,
        )
    else:
        cmd = build_codex_command(
            command,
            args.collector_model,
            tasks_dir,
            prompt,
            legacy_approval=args.legacy_codex_approval,
            full_auto=args.collector_full_auto,
            no_sandbox=args.collector_no_sandbox,
            ephemeral=args.collector_ephemeral,
        )
        shell = resolve_collector_shell(args.collector_shell)
        if shell:
            env["SHELL"] = shell
    completed = subprocess.run(cmd, cwd=tasks_dir, timeout=min(args.timeout, 120), env=env)
    if completed.returncode != 0:
        raise RuntimeError(
            f"collector preflight failed with exit code {completed.returncode}. "
            "Check that the selected collector CLI is installed and can run non-interactively."
        )


def write_dry_run_reference(
    task_dir: Path,
    current_year: int,
    download_assets: bool = False,
    current_date: date | None = None,
) -> None:
    web_dir = task_dir / WEB_REFERENCE_DIR
    web_dir.mkdir(parents=True, exist_ok=True)
    asset_line = ""
    if download_assets:
        assets_dir = web_dir / ASSETS_DIR
        assets_dir.mkdir(parents=True, exist_ok=True)
        (assets_dir / "asset_001_dry_run.txt").write_text("dry-run asset\n", encoding="utf-8")
        (assets_dir / "low_quality_asset.txt").write_text("unverifiable dry-run asset\n", encoding="utf-8")
        asset_line = f"\nAsset: {ASSETS_DIR}/asset_001_dry_run.txt\n"
    (web_dir / "ref_001_dry_run.md").write_text(
        "\n".join(
            [
                "# Dry-run web reference",
                "",
                "URL: https://example.com/dry-run",
                f"Access date: {date.today().isoformat()}",
                f"Current-date assumption: {(current_date or date(current_year, 1, 1)).isoformat()}",
                f"Current-year assumption: {current_year}",
                asset_line.rstrip(),
                "",
                "This file is generated only to verify the web_reference workflow without network calls.",
            ]
        )
        + "\n",
        encoding="utf-8",
    )
    (web_dir / "low_quality_note.md").write_text("Unverifiable placeholder.\n", encoding="utf-8")


def reference_files(web_dir: Path) -> list[Path]:
    if not web_dir.is_dir():
        return []
    return sorted(
        [
            path
            for path in web_dir.iterdir()
            if path.is_file()
            and path.name != "validation_manifest.json"
            and path.suffix.lower() in {".md", ".txt", ".json"}
        ],
        key=lambda path: path.name,
    )


def asset_files(web_dir: Path) -> list[Path]:
    assets_dir = web_dir / ASSETS_DIR
    if not assets_dir.is_dir():
        return []
    return sorted([path for path in assets_dir.rglob("*") if path.is_file()], key=lambda path: str(path.relative_to(web_dir)))


def relative_reference_path(path: Path, web_dir: Path) -> str:
    return str(path.relative_to(web_dir))


def build_validation_prompt(task_dir: Path, files: list[Path], assets: list[Path] | None = None) -> str:
    instruction = read_optional(task_dir / INSTRUCTION_FILE)
    checks = read_optional(task_dir / CHECKS_FILE)
    web_dir = task_dir / WEB_REFERENCE_DIR
    file_blocks = []
    for path in files:
        file_blocks.append(f"--- FILE: {relative_reference_path(path, web_dir)} ---\n{read_optional(path, 8000)}")
    asset_blocks = []
    for path in assets or []:
        try:
            size = path.stat().st_size
        except OSError:
            size = 0
        asset_blocks.append(f"- {relative_reference_path(path, web_dir)} ({size} bytes)")
    asset_text = "\n".join(asset_blocks) if asset_blocks else "无"
    return f"""你是网络资料质检员。请检查 `{WEB_REFERENCE_DIR}` 中的资料是否真实、权威、高质量，并且对完成任务有直接帮助。

只输出 JSON，不要输出 Markdown。格式：
{{
  "files": [
    {{"file": "文件名", "keep": true, "reason": "简短原因"}}
  ],
  "assets": [
    {{"file": "assets/文件名", "keep": true, "reason": "简短原因"}}
  ]
}}

保留标准：
- 与任务要求和 evaluator 检查点直接相关。
- 有明确 URL 或可核验出处。
- 内容没有明显幻觉、过时、低质量或泛泛而谈。
- assets 必须被某个 ref_*.md 明确引用并说明来源、用途；孤立、无来源、重复、损坏或与任务无关的 asset 应删除。

instruction.md:
{instruction}

evaluator/checks.yaml:
{checks}

待检查资料：
{chr(10).join(file_blocks)}

待检查 assets:
{asset_text}
"""


def parse_json_object(text: str) -> dict[str, Any]:
    text = text.strip()
    try:
        obj = json.loads(text)
    except json.JSONDecodeError:
        match = re.search(r"\{.*\}", text, re.S)
        if not match:
            raise
        obj = json.loads(match.group(0))
    if not isinstance(obj, dict):
        raise ValueError("validator output is not a JSON object")
    return obj


def run_claude_validation(
    task_dir: Path,
    model: str,
    command: str,
    timeout: int,
    files: list[Path],
    assets: list[Path] | None = None,
) -> dict[str, Any]:
    prompt = build_validation_prompt(task_dir, files, assets)
    cmd = [*command_parts(command), "-p", prompt, "--model", model]
    try:
        completed = subprocess.run(cmd, cwd=task_dir, check=True, timeout=timeout, capture_output=True, text=True)
    except FileNotFoundError as exc:
        raise FileNotFoundError(
            f"validator command not found: {command!r}. Install Claude CLI or pass --validator-command /path/to/claude."
        ) from exc
    return parse_json_object(completed.stdout)


def run_openai_compatible_validation(
    task_dir: Path,
    model: str,
    base_url: str,
    api_key_env: str,
    timeout: int,
    files: list[Path],
    assets: list[Path] | None = None,
) -> dict[str, Any]:
    api_key = os.environ.get(api_key_env, "").strip()
    if not api_key:
        raise RuntimeError(
            f"{api_key_env} is required for OpenAI-compatible validation. "
            "Set it or install Claude CLI and use --validator-backend cli."
        )
    payload = {
        "model": model,
        "messages": [
            {
                "role": "system",
                "content": "你是严格的网络资料质检员。只输出 JSON，不要输出 Markdown。",
            },
            {"role": "user", "content": build_validation_prompt(task_dir, files, assets)},
        ],
        "temperature": 0,
        "max_tokens": 2000,
        "response_format": {"type": "json_object"},
    }
    request = urllib.request.Request(
        base_url.rstrip("/") + "/chat/completions",
        data=json.dumps(payload, ensure_ascii=False).encode("utf-8"),
        headers={"Authorization": f"Bearer {api_key}", "Content-Type": "application/json"},
        method="POST",
    )
    with urllib.request.urlopen(request, timeout=timeout) as response:
        raw = response.read().decode("utf-8")
    obj = json.loads(raw)
    return parse_json_object(obj["choices"][0]["message"]["content"])


def run_validation(
    task_dir: Path,
    args: argparse.Namespace,
    files: list[Path],
    assets: list[Path] | None = None,
) -> dict[str, Any]:
    if args.validator_backend == "cli":
        return run_claude_validation(task_dir, args.validator_model, args.validator_command, args.timeout, files, assets)
    if args.validator_backend == "openai-compatible":
        return run_openai_compatible_validation(
            task_dir, args.validator_model, args.validator_base_url, args.validator_api_key_env, args.timeout, files, assets
        )
    if command_available(args.validator_command):
        return run_claude_validation(task_dir, args.validator_model, args.validator_command, args.timeout, files, assets)
    return run_openai_compatible_validation(
        task_dir, args.validator_model, args.validator_base_url, args.validator_api_key_env, args.timeout, files, assets
    )


def dry_run_validation(files: list[Path], assets: list[Path] | None = None, web_dir: Path | None = None) -> dict[str, Any]:
    web_dir = web_dir or (files[0].parent if files else Path("."))
    return {
        "files": [
            {
                "file": path.name,
                "keep": path.name.startswith("ref_"),
                "reason": "dry-run keeps generated reference files and rejects placeholders",
            }
            for path in files
        ],
        "assets": [
            {
                "file": relative_reference_path(path, web_dir),
                "keep": path.name.startswith("asset_"),
                "reason": "dry-run keeps generated assets and rejects placeholders",
            }
            for path in assets or []
        ],
    }


def move_rejected(path: Path, web_dir: Path, rejected_dir: Path) -> None:
    rel_path = path.relative_to(web_dir)
    target = rejected_dir / rel_path
    target.parent.mkdir(parents=True, exist_ok=True)
    if target.exists():
        target = target.with_name(f"{int(time.time())}_{target.name}")
    shutil.move(str(path), str(target))


def apply_validation(
    task_dir: Path,
    files: list[Path],
    validation: dict[str, Any],
    assets: list[Path] | None = None,
) -> tuple[list[str], list[str], list[str], list[str]]:
    web_dir = task_dir / WEB_REFERENCE_DIR
    rejected_dir = task_dir / REJECTED_DIR
    file_decisions = {
        str(item.get("file", "")): bool(item.get("keep", False))
        for item in validation.get("files", [])
        if isinstance(item, dict)
    }
    asset_decisions = {
        str(item.get("file", "")): bool(item.get("keep", False))
        for item in validation.get("assets", [])
        if isinstance(item, dict)
    }
    kept: list[str] = []
    rejected: list[str] = []
    for path in files:
        rel_path = relative_reference_path(path, web_dir)
        keep = file_decisions.get(rel_path, file_decisions.get(path.name, False))
        if keep:
            kept.append(rel_path)
            continue
        move_rejected(path, web_dir, rejected_dir)
        rejected.append(rel_path)
    kept_assets: list[str] = []
    rejected_assets: list[str] = []
    for path in assets or []:
        rel_path = relative_reference_path(path, web_dir)
        keep = asset_decisions.get(rel_path, False)
        if keep:
            kept_assets.append(rel_path)
            continue
        move_rejected(path, web_dir, rejected_dir)
        rejected_assets.append(rel_path)
    (web_dir / "validation_manifest.json").write_text(
        json.dumps(
            {
                "kept_files": kept,
                "rejected_files": rejected,
                "kept_assets": kept_assets,
                "rejected_assets": rejected_assets,
                "validator": validation,
            },
            ensure_ascii=False,
            indent=2,
        )
        + "\n",
        encoding="utf-8",
    )
    return kept, rejected, kept_assets, rejected_assets


def append_jsonl(path: Path, obj: dict[str, Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("a", encoding="utf-8") as file:
        file.write(json.dumps(obj, ensure_ascii=False) + "\n")


def has_existing_references(task_dir: Path) -> bool:
    return bool(reference_files(task_dir / WEB_REFERENCE_DIR))


def validation_manifest_path(task_dir: Path) -> Path:
    return task_dir / WEB_REFERENCE_DIR / "validation_manifest.json"


def load_validation_manifest(task_dir: Path) -> dict[str, Any]:
    path = validation_manifest_path(task_dir)
    if not path.is_file():
        return {}
    return load_json(path)


def has_validated_references(task_dir: Path) -> bool:
    manifest = load_validation_manifest(task_dir)
    kept_files = manifest.get("kept_files", [])
    if not isinstance(kept_files, list) or not kept_files:
        return False
    web_dir = task_dir / WEB_REFERENCE_DIR
    return any((web_dir / str(path)).is_file() for path in kept_files)


def skipped_task_result(task_dir: Path, args: argparse.Namespace, time_updates: list[dict[str, str]]) -> TaskResult:
    manifest = load_validation_manifest(task_dir)
    kept_files = manifest.get("kept_files", [])
    rejected_files = manifest.get("rejected_files", [])
    kept_assets = manifest.get("kept_assets", [])
    rejected_assets = manifest.get("rejected_assets", [])
    return TaskResult(
        task_id=task_dir.name,
        task_dir=str(task_dir),
        time_updates=time_updates,
        collected=False,
        kept_files=kept_files if isinstance(kept_files, list) else [],
        rejected_files=rejected_files if isinstance(rejected_files, list) else [],
        kept_assets=kept_assets if isinstance(kept_assets, list) else [],
        rejected_assets=rejected_assets if isinstance(rejected_assets, list) else [],
        status=f"skipped_{args.skip_existing_mode}",
    )


def should_skip_existing_task(task_dir: Path, args: argparse.Namespace) -> bool:
    if not args.skip_existing:
        return False
    if args.skip_existing_mode == "any":
        return has_existing_references(task_dir)
    return has_validated_references(task_dir)


def process_task(task_dir: Path, args: argparse.Namespace) -> TaskResult:
    time_updates = [] if args.no_time_fix else clarify_task_time(task_dir, args.current_year, args.current_date_obj)
    if args.time_fix_only:
        return TaskResult(
            task_id=task_dir.name,
            task_dir=str(task_dir),
            time_updates=time_updates,
            collected=False,
            kept_files=[],
            rejected_files=[],
            kept_assets=[],
            rejected_assets=[],
            status="time_fixed",
        )
    collected = False
    if should_skip_existing_task(task_dir, args):
        return skipped_task_result(task_dir, args, time_updates)
    elif args.dry_run:
        write_dry_run_reference(task_dir, args.current_year, args.download_assets, args.current_date_obj)
        collected = True
    else:
        run_collection(task_dir, args)
        collected = True

    files = reference_files(task_dir / WEB_REFERENCE_DIR)
    assets = asset_files(task_dir / WEB_REFERENCE_DIR)
    if not files:
        raise RuntimeError(f"no reference files found in {task_dir / WEB_REFERENCE_DIR}")
    validation = dry_run_validation(files, assets, task_dir / WEB_REFERENCE_DIR) if args.dry_run else run_validation(
        task_dir, args, files, assets
    )
    kept, rejected, kept_assets, rejected_assets = apply_validation(task_dir, files, validation, assets)
    return TaskResult(
        task_id=task_dir.name,
        task_dir=str(task_dir),
        time_updates=time_updates,
        collected=collected,
        kept_files=kept,
        rejected_files=rejected,
        kept_assets=kept_assets,
        rejected_assets=rejected_assets,
        status="ok",
    )


def main(argv: list[str] | None = None) -> int:
    args = parse_args(argv)
    tasks_dir = resolve_path(args.tasks_dir)
    summary_file = resolve_path(args.summary_file) if args.summary_file else tasks_dir / "web_reference_summary.jsonl"
    if args.preflight_only:
        preflight_collector(tasks_dir, args)
        print(json.dumps({"preflight": "ok", "tasks_dir": str(tasks_dir)}, ensure_ascii=False), flush=True)
        return 0
    task_dirs = discover_web_tasks(tasks_dir, args.limit)
    print(json.dumps({"web_task_count": len(task_dirs), "tasks_dir": str(tasks_dir)}, ensure_ascii=False), flush=True)
    for index, task_dir in enumerate(task_dirs, start=1):
        try:
            result = process_task(task_dir, args)
            if result.status == "time_fixed":
                print(
                    f"[{index}/{len(task_dirs)}] time-fixed {task_dir.name}: updated {len(result.time_updates)} files",
                    flush=True,
                )
            else:
                print(f"[{index}/{len(task_dirs)}] prepared {task_dir.name}: kept {len(result.kept_files)}", flush=True)
        except Exception as exc:  # noqa: BLE001 - batch mode should preserve the failing task id.
            result = TaskResult(
                task_id=task_dir.name,
                task_dir=str(task_dir),
                time_updates=[],
                collected=False,
                kept_files=[],
                rejected_files=[],
                kept_assets=[],
                rejected_assets=[],
                status="error",
                error=repr(exc),
            )
            print(f"[{index}/{len(task_dirs)}] error {task_dir.name}: {exc}", file=sys.stderr, flush=True)
            append_jsonl(summary_file, result.as_dict())
            if not args.continue_on_error:
                raise
            continue
        append_jsonl(summary_file, result.as_dict())
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
