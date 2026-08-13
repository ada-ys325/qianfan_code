import json
import tempfile
import unittest
from datetime import date
from pathlib import Path
from unittest.mock import patch

from dumatebench.scripts.prepare_web_references import (
    asset_files,
    build_claude_collector_command,
    build_codex_command,
    build_collector_prompt,
    clarify_vague_time_text,
    command_parts,
    discover_web_tasks,
    effective_collector_command,
    parse_args,
    process_task,
    resolve_claude_permission_mode,
    resolve_collector_shell,
    run_validation,
    should_skip_existing_task,
)


class PrepareWebReferencesTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def _make_task(self, rel: str, web_retrieval: int) -> Path:
        task = self.root / rel
        (task / "evaluator").mkdir(parents=True)
        (task / "instruction.md").write_text("请搜集近几年相关数据并写报告。\n", encoding="utf-8")
        (task / "evaluator" / "checks.yaml").write_text(
            "checks:\n  - id: time\n    value: 近年来的数据\n",
            encoding="utf-8",
        )
        (task / "task_type_feature.json").write_text(
            json.dumps({"web_retrieval": web_retrieval}) + "\n",
            encoding="utf-8",
        )
        return task

    def test_discover_web_tasks_recurses_from_feature_files(self):
        shallow = self._make_task("shallow_task", 1)
        nested = self._make_task("group/nested_task", 1)
        self._make_task("offline_task", 0)

        self.assertEqual(discover_web_tasks(self.root), [nested, shallow])

    def test_clarify_vague_time_text_adds_current_year_once(self):
        updated, phrases = clarify_vague_time_text("请整理近几年趋势，并参考公开资料。", 2026)

        self.assertIn("近几年（以 2026 年为当前年份）", updated)
        self.assertIn("近几年", phrases)

    def test_clarify_vague_time_text_adds_current_date_for_day_phrases(self):
        updated, phrases = clarify_vague_time_text(
            "请根据今天、今年、近几天和近几年资料更新报告。",
            2026,
            date(2026, 7, 23),
        )

        self.assertIn("今天（指 2026年7月23日）", updated)
        self.assertIn("今年（指 2026 年）", updated)
        self.assertIn("近几天（以 2026年7月23日 为当前日期）", updated)
        self.assertIn("近几年（以 2026 年为当前年份）", updated)
        self.assertCountEqual(phrases, ["今天", "今年", "近几天", "近几年"])

    def test_clarify_vague_time_text_does_not_repeat_when_date_is_nearby(self):
        updated, phrases = clarify_vague_time_text(
            "今天（指 2026年7月23日）请整理材料，今年（指 2026 年）也要覆盖。",
            2026,
            date(2026, 7, 23),
        )

        self.assertEqual(updated.count("2026年7月23日"), 1)
        self.assertEqual(updated.count("2026 年"), 1)
        self.assertEqual(phrases, [])

    def test_command_parts_uses_fallback_when_executable_is_missing(self):
        fallback = self.root / "codex"
        fallback.write_text("#!/bin/sh\n", encoding="utf-8")

        self.assertEqual(command_parts("missing-codex --flag", fallbacks=[fallback]), [str(fallback), "--flag"])

    def test_build_codex_command_uses_plain_exec_and_skips_git_check(self):
        cmd = build_codex_command("codex", "gpt-5.5", self.root, "prompt")

        self.assertNotIn("--full-auto", cmd)
        self.assertNotIn("--ephemeral", cmd)
        self.assertIn("--skip-git-repo-check", cmd)
        self.assertLess(cmd.index("exec"), cmd.index("--skip-git-repo-check"))
        self.assertEqual(cmd[cmd.index("-s") + 1], "workspace-write")
        self.assertEqual(cmd[cmd.index("-C") + 1], str(self.root.resolve()))

    def test_build_codex_command_can_use_full_auto(self):
        cmd = build_codex_command("codex", "gpt-5.5", self.root, "prompt", full_auto=True)

        self.assertLess(cmd.index("exec"), cmd.index("--full-auto"))

    def test_build_codex_command_can_use_ephemeral(self):
        cmd = build_codex_command("codex", "gpt-5.5", self.root, "prompt", ephemeral=True)

        self.assertLess(cmd.index("exec"), cmd.index("--ephemeral"))

    def test_build_codex_command_can_use_legacy_approval(self):
        cmd = build_codex_command("codex", "gpt-5.5", self.root, "prompt", legacy_approval=True)

        self.assertLess(cmd.index("--ask-for-approval"), cmd.index("exec"))
        self.assertEqual(cmd[cmd.index("--ask-for-approval") + 1], "never")
        self.assertNotIn("--full-auto", cmd)

    def test_build_codex_command_can_bypass_sandbox(self):
        cmd = build_codex_command("codex", "gpt-5.5", self.root, "prompt", no_sandbox=True)

        self.assertLess(cmd.index("--dangerously-bypass-approvals-and-sandbox"), cmd.index("exec"))
        self.assertNotIn("--full-auto", cmd)
        self.assertNotIn("-s", cmd)
        self.assertIn("--skip-git-repo-check", cmd)

    def test_parse_args_uses_auto_validator_backend(self):
        args = parse_args([])

        self.assertEqual(args.validator_backend, "auto")

    def test_parse_args_defaults_current_year_from_current_date(self):
        args = parse_args(["--current-date", "2026-07-23"])

        self.assertEqual(args.current_date_obj, date(2026, 7, 23))
        self.assertEqual(args.current_year, 2026)

    def test_parse_args_defaults_to_codex_collector_backend(self):
        args = parse_args([])

        self.assertEqual(args.collector_backend, "codex")

    def test_effective_collector_command_switches_implicit_default_for_claude(self):
        args = parse_args(["--collector-backend", "claude"])

        self.assertEqual(effective_collector_command(args), "claude")

    def test_effective_collector_command_preserves_explicit_command(self):
        args = parse_args(["--collector-backend", "claude", "--collector-command", "/opt/bin/claude"])

        self.assertEqual(effective_collector_command(args), "/opt/bin/claude")

    def test_build_claude_collector_command_uses_print_mode_and_tools(self):
        cmd = build_claude_collector_command(
            "claude",
            "claude-opus-4-8",
            "prompt",
            permission_mode="bypassPermissions",
        )

        self.assertEqual(cmd[:2], ["claude", "-p"])
        self.assertIn("--model", cmd)
        self.assertEqual(cmd[cmd.index("--model") + 1], "claude-opus-4-8")
        self.assertIn("--permission-mode", cmd)
        self.assertEqual(cmd[cmd.index("--permission-mode") + 1], "bypassPermissions")
        self.assertIn("--allowedTools", cmd)
        self.assertIn("WebSearch", cmd[cmd.index("--allowedTools") + 1])

    def test_resolve_claude_permission_mode_omits_auto_for_root(self):
        with patch("dumatebench.scripts.prepare_web_references.running_as_root", return_value=True):
            self.assertEqual(resolve_claude_permission_mode("auto"), "")

    def test_resolve_claude_permission_mode_uses_bypass_for_non_root(self):
        with patch("dumatebench.scripts.prepare_web_references.running_as_root", return_value=False):
            with patch.dict("os.environ", {}, clear=True):
                self.assertEqual(resolve_claude_permission_mode("auto"), "bypassPermissions")

    def test_parse_args_supports_preflight_only(self):
        args = parse_args(["--preflight-only"])

        self.assertTrue(args.preflight_only)

    def test_parse_args_supports_time_fix_only(self):
        args = parse_args(["--time-fix-only"])

        self.assertTrue(args.time_fix_only)

    def test_skip_existing_validated_does_not_skip_partial_references(self):
        task = self._make_task("group/partial_task", 1)
        web_dir = task / "web_reference"
        web_dir.mkdir()
        (web_dir / "ref_partial.md").write_text("partial\n", encoding="utf-8")
        args = parse_args(["--skip-existing"])

        self.assertFalse(should_skip_existing_task(task, args))

    def test_skip_existing_validated_skips_manifest_with_kept_file(self):
        task = self._make_task("group/completed_task", 1)
        web_dir = task / "web_reference"
        web_dir.mkdir()
        (web_dir / "ref_done.md").write_text("done\n", encoding="utf-8")
        (web_dir / "validation_manifest.json").write_text(
            json.dumps({"kept_files": ["ref_done.md"], "rejected_files": [], "kept_assets": [], "rejected_assets": []}) + "\n",
            encoding="utf-8",
        )
        args = parse_args(["--skip-existing"])

        self.assertTrue(should_skip_existing_task(task, args))

    def test_skip_existing_any_skips_partial_references(self):
        task = self._make_task("group/any_task", 1)
        web_dir = task / "web_reference"
        web_dir.mkdir()
        (web_dir / "ref_partial.md").write_text("partial\n", encoding="utf-8")
        args = parse_args(["--skip-existing", "--skip-existing-mode", "any"])

        self.assertTrue(should_skip_existing_task(task, args))

    def test_resolve_collector_shell_auto_returns_existing_shell(self):
        shell = resolve_collector_shell("auto")

        self.assertTrue(shell == "" or Path(shell).is_file())

    def test_resolve_collector_shell_rejects_missing_explicit_shell(self):
        with self.assertRaises(FileNotFoundError):
            resolve_collector_shell(str(self.root / "missing-shell"))

    def test_build_collector_prompt_can_request_assets(self):
        task = self._make_task("group/web_task", 1)

        prompt = build_collector_prompt(task, 2026, download_assets=True)

        self.assertIn("web_reference/assets/", prompt)
        self.assertIn("asset_序号_简短主题", prompt)

    def test_auto_validation_falls_back_to_openai_compatible(self):
        task = self._make_task("group/web_task", 1)
        ref = task / "web_reference" / "ref.md"
        ref.parent.mkdir()
        ref.write_text("reference", encoding="utf-8")
        args = parse_args(["--validator-command", "missing-claude"])

        with patch("dumatebench.scripts.prepare_web_references.command_available", return_value=False):
            with patch("dumatebench.scripts.prepare_web_references.run_openai_compatible_validation") as run_api:
                run_api.return_value = {"files": [{"file": "ref.md", "keep": True, "reason": "ok"}]}

                result = run_validation(task, args, [ref])

        self.assertEqual(result["files"][0]["file"], "ref.md")
        self.assertEqual(run_api.call_count, 1)

    def test_dry_run_process_updates_time_and_filters_reference_files(self):
        task = self._make_task("group/web_task", 1)
        args = parse_args(["--tasks-dir", str(self.root), "--dry-run", "--current-date", "2026-07-23"])

        result = process_task(task, args)

        self.assertEqual(result.status, "ok")
        self.assertEqual(result.kept_files, ["ref_001_dry_run.md"])
        self.assertEqual(result.rejected_files, ["low_quality_note.md"])
        self.assertIn("近几年（以 2026 年为当前年份）", (task / "instruction.md").read_text(encoding="utf-8"))
        self.assertIn("近年来（以 2026 年为当前年份）", (task / "evaluator" / "checks.yaml").read_text(encoding="utf-8"))
        self.assertTrue((task / "web_reference" / "ref_001_dry_run.md").is_file())
        self.assertFalse((task / "web_reference" / "low_quality_note.md").exists())
        self.assertTrue((task / "web_reference_rejected" / "low_quality_note.md").is_file())

    def test_time_fix_only_process_does_not_create_web_reference(self):
        task = self._make_task("group/time_only_task", 1)
        args = parse_args(["--tasks-dir", str(self.root), "--time-fix-only", "--current-date", "2026-07-23"])

        with patch("dumatebench.scripts.prepare_web_references.run_collection") as run_collection:
            with patch("dumatebench.scripts.prepare_web_references.run_validation") as run_validation_mock:
                result = process_task(task, args)

        self.assertEqual(result.status, "time_fixed")
        self.assertFalse(result.collected)
        self.assertEqual(result.kept_files, [])
        self.assertFalse((task / "web_reference").exists())
        self.assertIn("近几年（以 2026 年为当前年份）", (task / "instruction.md").read_text(encoding="utf-8"))
        self.assertEqual(run_collection.call_count, 0)
        self.assertEqual(run_validation_mock.call_count, 0)

    def test_dry_run_download_assets_filters_assets(self):
        task = self._make_task("group/web_asset_task", 1)
        args = parse_args(["--tasks-dir", str(self.root), "--dry-run", "--download-assets", "--current-year", "2026"])

        result = process_task(task, args)

        self.assertEqual(result.kept_assets, ["assets/asset_001_dry_run.txt"])
        self.assertEqual(result.rejected_assets, ["assets/low_quality_asset.txt"])
        self.assertEqual([str(path.relative_to(task / "web_reference")) for path in asset_files(task / "web_reference")], ["assets/asset_001_dry_run.txt"])
        self.assertTrue((task / "web_reference" / "assets" / "asset_001_dry_run.txt").is_file())
        self.assertFalse((task / "web_reference" / "assets" / "low_quality_asset.txt").exists())
        self.assertTrue((task / "web_reference_rejected" / "assets" / "low_quality_asset.txt").is_file())


if __name__ == "__main__":
    unittest.main()
