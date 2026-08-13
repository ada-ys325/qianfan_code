import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from dumatebench.scripts.run_task_batch import (
    ROOT,
    build_agent_command,
    dedupe_tasks_by_name,
    discover_tasks,
    has_complete_existing_outputs,
    _infer_output_files_from_checks,
    parse_args,
    prepare_task_view,
    prepare_runtime,
    read_checklist_reward,
    run_unified_llm_judge_if_possible,
    write_final_reward,
)


class BatchRunnerTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(dir=ROOT)
        self.root = Path(self.tmp.name)
        self.template = self.root / "template"
        self.task = self.root / "target_task"
        self._make_template_task(self.template)
        self.nested_task = self.root / "group" / "nested_task"
        self._make_target_task(self.task)
        self._make_target_task(self.nested_task)
        self._make_target_task(self.task / ".batch_runtime" / "smoke" / "fake_task")

    def tearDown(self):
        self.tmp.cleanup()

    def test_discover_tasks_finds_task_directories(self):
        tasks = discover_tasks(self.root, "*task", 0)

        self.assertEqual(tasks, [self.nested_task, self.task])

    def test_discover_tasks_can_scan_only_one_level(self):
        tasks = discover_tasks(self.root, "*task", 0, recursive=False)

        self.assertEqual(tasks, [self.task])

    def test_dedupe_tasks_by_name_prefers_deeper_paths(self):
        duplicate = self.root / "group" / self.task.name
        self._make_target_task(duplicate)

        tasks = dedupe_tasks_by_name([self.task, duplicate, self.nested_task])

        self.assertEqual(tasks, [self.nested_task, duplicate])

    def test_parse_args_has_batch_max_steps_and_forwards_extra_agent_args(self):
        args = parse_args(["--max-steps", "30", "--", "--model", "gpt-4o-mini"])

        self.assertEqual(args.max_steps, 30)
        self.assertEqual(args.agent_args, ["--model", "gpt-4o-mini"])

    def test_parse_args_accepts_native_agent_backend(self):
        args = parse_args([
            "--agent-backend",
            "codex",
            "--agent-model",
            "gpt-test",
            "--agent-base-url",
            "https://gateway.example/v1",
        ])

        self.assertEqual(args.agent_backend, "codex")
        self.assertEqual(args.agent_model, "gpt-test")
        self.assertEqual(args.agent_base_url, "https://gateway.example/v1")
        self.assertEqual(args.agent_max_turns, 0)

    def test_parse_args_accepts_isolated_run_options(self):
        args = parse_args(["--run-id", "exp-a", "--runs-root", "/tmp/dumate-runs"])

        self.assertEqual(args.run_id, "exp-a")
        self.assertEqual(args.runs_root, "/tmp/dumate-runs")

    def test_build_agent_command_selects_native_runner_without_embedding_key_or_turn_limit(self):
        args = parse_args([
            "--agent-backend",
            "claude-code",
            "--agent-model",
            "test-model",
            "--agent-base-url",
            "https://gateway.example/v1",
        ])

        command = build_agent_command(Path("compose.yaml"), args, {"DUMATE_AGENT_API_KEY": "secret-value"})

        self.assertIn("/opt/dumate/native_agent.py", command)
        self.assertIn("claude-code", command)
        self.assertIn("test-model", command)
        self.assertNotIn("secret-value", command)
        self.assertNotIn("--max-turns", command)

    def test_build_agent_command_can_pass_explicit_native_turn_limit(self):
        args = parse_args([
            "--agent-backend",
            "claude-code",
            "--agent-model",
            "test-model",
            "--agent-base-url",
            "https://gateway.example/v1",
            "--agent-max-turns",
            "7",
        ])

        command = build_agent_command(Path("compose.yaml"), args, {})

        self.assertIn("--max-turns", command)
        self.assertIn("7", command)

    def test_write_final_reward_uses_zero_judge_score_when_artifact_is_missing(self):
        run_outputs = self.task / "run_outputs"
        run_outputs.mkdir()
        (run_outputs / "reward.json").write_text(
            '{"complete_pass": 0, "partial_pass": 0.2, "checks": ['
            '{"id": "output", "detail": "{\\"file\\": \\"run_outputs/reports/missing.xlsx\\"}"}'
            "]}\n",
            encoding="utf-8",
        )

        final_path = write_final_reward(self.task, "run_outputs/reward_with_llm_judge.json")

        self.assertIsNotNone(final_path)
        final = Path(final_path)
        self.assertTrue(final.is_file())
        data = json.loads(final.read_text(encoding="utf-8"))
        self.assertEqual(data["base_partial_pass"], 0.2)
        self.assertEqual(data["llm_judge_score"], 0.0)
        self.assertEqual(data["final_score"], 0.1)
        self.assertEqual(data["llm_judge"]["status"], "missing_artifact")

    def test_write_final_reward_creates_score_when_checklist_reward_is_missing(self):
        (self.task / "evaluator" / "checks.yaml").write_text(
            '- id: output\n'
            '  type: evaluate_file_exist\n'
            '  detail:\n'
            '    file: "run_outputs/reports/missing.xlsx"\n',
            encoding="utf-8",
        )

        final_path = write_final_reward(self.task, "run_outputs/reward_with_llm_judge.json")

        self.assertIsNotNone(final_path)
        data = json.loads(Path(final_path).read_text(encoding="utf-8"))
        self.assertEqual(data["base_partial_pass"], 0.0)
        self.assertEqual(data["llm_judge_score"], 0.0)
        self.assertEqual(data["final_score"], 0.0)
        self.assertEqual(data["llm_judge"]["status"], "missing_artifact")
        self.assertEqual(data["llm_judge"]["output_file"], "run_outputs/reports/missing.xlsx")

    def test_write_final_reward_reuses_existing_unified_judge_score(self):
        output = self.task / "run_outputs" / "reports" / "answer.xlsx"
        output.parent.mkdir(parents=True)
        output.write_text("artifact", encoding="utf-8")
        (self.task / "run_outputs" / "reward.json").write_text(
            '{"complete_pass": 0, "partial_pass": 0.4, "checks": ['
            '{"id": "output", "detail": "{\\"file\\": \\"run_outputs/reports/answer.xlsx\\"}"}'
            "]}\n",
            encoding="utf-8",
        )
        (self.task / "run_outputs" / "llm_judge_score.json").write_text(
            '{"judge_score": 0.8, "final_score": 0.6}\n',
            encoding="utf-8",
        )

        final_path = write_final_reward(self.task, "run_outputs/reward_with_llm_judge.json")

        data = json.loads(Path(final_path).read_text(encoding="utf-8"))
        self.assertEqual(data["llm_judge_score"], 0.8)
        self.assertEqual(data["final_score"], 0.6)
        self.assertEqual(data["llm_judge"]["status"], "ok")

    def test_run_unified_llm_judge_is_called_when_artifact_exists(self):
        output = self.task / "run_outputs" / "scripts" / "answer.docx"
        output.parent.mkdir(parents=True)
        output.write_text("artifact", encoding="utf-8")
        (self.task / "run_outputs" / "reward.json").write_text(
            '{"complete_pass": 0, "partial_pass": 0.4, "checks": ['
            '{"id": "output", "detail": "{\\"file\\": \\"run_outputs/scripts/answer.docx\\"}"}'
            "]}\n",
            encoding="utf-8",
        )
        args = parse_args(["--llm-judge-model", "mock-model"])

        def fake_judge(task_dir, judge_args):
            self.assertEqual(Path(task_dir), self.task)
            self.assertEqual(judge_args["output_file"], "run_outputs/scripts/answer.docx")
            self.assertEqual(judge_args["model"], "mock-model")
            report_path = self.task / "run_outputs" / "llm_judge_score.json"
            report_path.write_text('{"judge_score": 0.9, "status": "ok"}\n', encoding="utf-8")
            return {"judge_score": 0.9}

        with patch("dumatebench.evaluator.llm_judge.unified.run_llm_judge_score", side_effect=fake_judge) as run_judge:
            run_unified_llm_judge_if_possible(self.task, args, read_checklist_reward(self.task))

        self.assertEqual(run_judge.call_count, 1)
        final_path = write_final_reward(self.task, "run_outputs/reward_with_llm_judge.json")
        data = json.loads(Path(final_path).read_text(encoding="utf-8"))
        self.assertEqual(data["llm_judge_score"], 0.9)
        self.assertEqual(data["final_score"], 0.65)

    def test_run_unified_llm_judge_averages_multiple_artifacts(self):
        first = self.task / "run_outputs" / "reports" / "a.docx"
        second = self.task / "run_outputs" / "reports" / "b.pdf"
        first.parent.mkdir(parents=True)
        first.write_text("artifact a", encoding="utf-8")
        second.write_text("artifact b", encoding="utf-8")
        (self.task / "run_outputs" / "reward.json").write_text(
            '{"complete_pass": 0, "partial_pass": 0.6, "checks": ['
            '{"id": "a", "detail": "{\\"file\\": \\"run_outputs/reports/a.docx\\"}"},'
            '{"id": "b", "detail": "{\\"file\\": \\"run_outputs/reports/b.pdf\\"}"}'
            "]}\n",
            encoding="utf-8",
        )
        args = parse_args(["--llm-judge-model", "mock-model"])

        def fake_judge(task_dir, judge_args):
            self.assertEqual(Path(task_dir), self.task)
            score = 0.2 if judge_args["output_file"].endswith("a.docx") else 0.8
            Path(task_dir, judge_args["judge_output_file"]).parent.mkdir(parents=True, exist_ok=True)
            Path(task_dir, judge_args["judge_output_file"]).write_text(
                json.dumps({"status": "ok", "judge_score": score}) + "\n",
                encoding="utf-8",
            )
            return {"status": "ok", "judge_score": score}

        with patch("dumatebench.evaluator.llm_judge.unified.run_llm_judge_score", side_effect=fake_judge) as run_judge:
            run_unified_llm_judge_if_possible(self.task, args, read_checklist_reward(self.task))

        self.assertEqual(run_judge.call_count, 2)
        aggregate = json.loads((self.task / "run_outputs" / "llm_judge_score.json").read_text(encoding="utf-8"))
        self.assertEqual(aggregate["judge_score"], 0.5)
        self.assertEqual(len(aggregate["artifact_reports"]), 2)
        final_path = write_final_reward(self.task, "run_outputs/reward_with_llm_judge.json")
        data = json.loads(Path(final_path).read_text(encoding="utf-8"))
        self.assertEqual(data["llm_judge_score"], 0.5)
        self.assertEqual(data["final_score"], 0.55)
        self.assertEqual(data["llm_judge"]["output_files"], ["run_outputs/reports/a.docx", "run_outputs/reports/b.pdf"])

    def test_infer_output_files_reads_instruction_targets(self):
        (self.task / "instruction.md").write_text(
            "请输出 run_outputs/reports/a.docx 和 run_outputs/reports/b.pdf。",
            encoding="utf-8",
        )

        outputs = _infer_output_files_from_checks(self.task, {"checks": []})

        self.assertEqual(outputs, ["run_outputs/reports/a.docx", "run_outputs/reports/b.pdf"])

    def test_complete_existing_outputs_requires_all_three_reward_files(self):
        run_outputs = self.task / "run_outputs"
        run_outputs.mkdir()
        (run_outputs / "reward.json").write_text('{"partial_pass": 1.0}\n', encoding="utf-8")
        (run_outputs / "llm_judge_score.json").write_text('{"status": "ok", "judge_score": 0.8}\n', encoding="utf-8")

        self.assertFalse(has_complete_existing_outputs(self.task, "run_outputs/reward_with_llm_judge.json"))

        (run_outputs / "reward_with_llm_judge.json").write_text(
            '{"llm_judge": {"status": "ok"}, "llm_judge_score": 0.8, "final_score": 0.9}\n',
            encoding="utf-8",
        )

        self.assertTrue(has_complete_existing_outputs(self.task, "run_outputs/reward_with_llm_judge.json"))

    def test_complete_existing_outputs_rejects_failed_judge_report(self):
        run_outputs = self.task / "run_outputs"
        run_outputs.mkdir()
        (run_outputs / "reward.json").write_text('{"partial_pass": 1.0}\n', encoding="utf-8")
        (run_outputs / "llm_judge_score.json").write_text('{"status": "failed", "judge_score": 0.0}\n', encoding="utf-8")
        (run_outputs / "reward_with_llm_judge.json").write_text(
            '{"llm_judge": {"status": "failed"}, "llm_judge_score": 0.0, "final_score": 0.5}\n',
            encoding="utf-8",
        )

        self.assertFalse(has_complete_existing_outputs(self.task, "run_outputs/reward_with_llm_judge.json"))

    def test_prepare_runtime_reuses_template_faults_and_points_dockerfile_at_target_task(self):
        runtime = prepare_runtime(self.task, self.template, "smoke", "dumatebench-test")

        self.assertTrue((runtime / "network_faults.yaml").is_file())
        self.assertTrue((runtime / "tool_faults.yaml").is_file())
        self.assertTrue((runtime / "agents" / "command_agent.py").is_file())
        self.assertTrue((runtime / "agents" / "native_agent.py").is_file())
        self.assertTrue((runtime / "task_context" / "workspace_seed").is_dir())
        dockerfile = (runtime / "environment" / "Dockerfile").read_text(encoding="utf-8")
        compose = (runtime / "docker-compose.yaml").read_text(encoding="utf-8")
        context_line = next(line for line in compose.splitlines() if line.strip().startswith("context:"))
        build_context = Path(context_line.split(":", 1)[1].strip())

        self.assertIn("COPY task_context/workspace_seed/", dockerfile)
        self.assertIn("COPY task_context/instruction.md", dockerfile)
        self.assertIn("COPY task_context/evaluator/", dockerfile)
        self.assertIn("COPY ./network_faults.yaml", dockerfile)
        self.assertIn("dumatebench-test-target_task:latest", compose)
        self.assertEqual(build_context.resolve(), runtime.resolve())
        self.assertIn("dockerfile: environment/Dockerfile", compose)
        self.assertIn('cpus: "2"', compose)
        self.assertIn("mem_limit: 4096m", compose)
        self.assertIn('dumatebench.storage_mb: "12000"', compose)
        self.assertIn(f"{self.task.resolve().as_posix()}/run_outputs:/outputs", compose)
        setup = (runtime / "environment" / "setup.sh").read_text(encoding="utf-8")
        self.assertIn("cp -a /workspace_seed/. /workspace/", setup)
        self.assertNotIn("meeting_agenda.pdf", setup)
        self.assertNotIn("/outputs/calendar", setup)
        self.assertNotIn("/outputs/emails", setup)
        self.assertNotIn("/outputs/reports", setup)
        self.assertNotIn("/outputs/pptx", setup)

    def test_prepare_runtime_supports_task_directory_outside_repo_root(self):
        with tempfile.TemporaryDirectory() as external_tmp:
            external_task = Path(external_tmp) / "external_task"
            self._make_target_task(external_task)

            runtime = prepare_runtime(external_task, self.template, "smoke", "dumatebench-test")

            dockerfile = (runtime / "environment" / "Dockerfile").read_text(encoding="utf-8")
            compose = (runtime / "docker-compose.yaml").read_text(encoding="utf-8")
            context_line = next(line for line in compose.splitlines() if line.strip().startswith("context:"))
            build_context = Path(context_line.split(":", 1)[1].strip())

            self.assertIn("COPY task_context/workspace_seed/", dockerfile)
            self.assertIn("COPY ./network_faults.yaml", dockerfile)
            self.assertEqual(build_context.resolve(), runtime.resolve())
            self.assertIn("dockerfile: environment/Dockerfile", compose)
            self.assertIn(f"{external_task.resolve().as_posix()}/run_outputs:/outputs", compose)

    def test_prepare_runtime_can_mount_isolated_run_outputs_and_logs(self):
        run_dir = self.root / "runs" / "exp-a" / self.task.name
        output_dir = run_dir / "run_outputs"
        logs_dir = run_dir / "run_logs"
        output_dir.mkdir(parents=True)
        logs_dir.mkdir()

        runtime = prepare_runtime(
            self.task,
            self.template,
            "smoke",
            "dumatebench-test",
            runtime_parent=run_dir,
            output_dir=output_dir,
            logs_dir=logs_dir,
        )

        compose = (runtime / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn(f"{output_dir.resolve().as_posix()}:/outputs", compose)
        self.assertIn(f"{logs_dir.resolve().as_posix()}:/logs", compose)
        self.assertNotIn(f"{self.task.resolve().as_posix()}/run_outputs:/outputs", compose)

    def test_prepare_task_view_links_task_files_to_isolated_outputs(self):
        run_dir = self.root / "runs" / "exp-a" / self.task.name
        (run_dir / "run_outputs").mkdir(parents=True)
        (run_dir / "run_logs").mkdir()

        view = prepare_task_view(self.task, run_dir)
        (view / "run_outputs" / "reward.json").write_text('{"partial_pass": 1.0}\n', encoding="utf-8")

        self.assertTrue((view / "instruction.md").exists())
        self.assertTrue((view / "evaluator").exists())
        self.assertTrue((run_dir / "run_outputs" / "reward.json").is_file())
        self.assertFalse((self.task / "run_outputs" / "reward.json").exists())

    def test_prepare_runtime_uses_backend_specific_image_for_native_agent(self):
        runtime = prepare_runtime(
            self.task,
            self.template,
            "native",
            "dumatebench-test",
            agent_backend="codex",
        )

        compose = (runtime / "docker-compose.yaml").read_text(encoding="utf-8")
        self.assertIn("dumatebench-test-codex-target_task:latest", compose)
        self.assertIn("DUMATE_AGENT_BACKEND: ${DUMATE_AGENT_BACKEND:-react}", compose)

    @staticmethod
    def _make_template_task(path: Path) -> None:
        (path / "environment").mkdir(parents=True)
        (path / "workspace_seed").mkdir()
        (path / "evaluator").mkdir()
        (path / "instruction.md").write_text("template instruction", encoding="utf-8")
        (path / "task.yaml").write_text("task_id: template\n", encoding="utf-8")
        (path / "network_faults.yaml").write_text("enabled: true\n", encoding="utf-8")
        (path / "tool_faults.yaml").write_text("enabled: true\n", encoding="utf-8")
        (path / "environment" / "setup.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        (path / "environment" / "entrypoint.sh").write_text("#!/usr/bin/env bash\n", encoding="utf-8")
        template_rel = path.resolve().relative_to(ROOT).as_posix()
        (path / "environment" / "Dockerfile").write_text(
            "\n".join(
                [
                    "FROM python:3.12-slim",
                    f"COPY {template_rel}/workspace_seed/ /workspace_seed/",
                    f"COPY {template_rel}/instruction.md /opt/dumate/task/instruction.md",
                    f"COPY {template_rel}/task.yaml /opt/dumate/task/task.yaml",
                    f"COPY {template_rel}/evaluator/ /opt/dumate/task/evaluator/",
                    f"COPY {template_rel}/network_faults.yaml /opt/dumate/network_faults.yaml",
                    f"COPY {template_rel}/tool_faults.yaml /opt/dumate/tool_faults.yaml",
                    f"COPY {template_rel}/environment/setup.sh /opt/dumate/setup.sh",
                ]
            )
            + "\n",
            encoding="utf-8",
        )

    @staticmethod
    def _make_target_task(path: Path) -> None:
        (path / "workspace_seed").mkdir(parents=True)
        (path / "evaluator").mkdir()
        (path / "instruction.md").write_text("target instruction", encoding="utf-8")
        (path / "task.yaml").write_text(
            "task_id: target\n"
            "environment:\n"
            "  cpus: 2\n"
            "  memory_mb: 4096\n"
            "  storage_mb: 12000\n",
            encoding="utf-8",
        )
        (path / "evaluator" / "evaluator.py").write_text("print('{}')\n", encoding="utf-8")


if __name__ == "__main__":
    unittest.main()
