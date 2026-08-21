from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from dumatebench_cli.submission import pack_submission_from_harbor_job


class HarborSubmissionTests(unittest.TestCase):
    def test_pack_manifest_from_harbor_job(self) -> None:
        with tempfile.TemporaryDirectory() as temp_dir:
            root = Path(temp_dir)
            job = root / "job"
            job.mkdir()
            (job / "result.json").write_text(json.dumps({"id": "job-12345678"}), encoding="utf-8")
            for index, task_name in enumerate(("dumate/task-a", "dumate/task-a", "other/task")):
                trial = job / f"trial-{index}"
                trial.mkdir()
                (trial / "result.json").write_text(
                    json.dumps({"task_name": task_name, "exception_info": None}),
                    encoding="utf-8",
                )
            output = root / "submission.json"

            result = pack_submission_from_harbor_job(
                job,
                output,
                agent_name="agent",
                agent_org="org",
                model_name="model",
                model_provider="provider",
            )

            manifest = json.loads(output.read_text(encoding="utf-8"))
            self.assertEqual(result.task_count, 1)
            self.assertEqual(manifest["schema_version"], 2)
            self.assertEqual(manifest["harbor_job_id"], "job-12345678")
            self.assertEqual(manifest["metadata"]["task_ids"], ["task-a"])
            self.assertEqual(len(result.warnings), 1)


if __name__ == "__main__":
    unittest.main()
