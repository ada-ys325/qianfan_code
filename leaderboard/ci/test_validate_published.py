from __future__ import annotations

import json
import tempfile
import unittest
from pathlib import Path

from leaderboard.ci.validate_published import validate_published


def _write_manifest(repo_root: Path, rel_path: str, verification: dict | None) -> None:
    path = repo_root / rel_path
    path.parent.mkdir(parents=True, exist_ok=True)
    manifest: dict = {"schema_version": 1, "harbor_job_id": "job-0000000000"}
    if verification is not None:
        manifest["verification"] = verification
    path.write_text(json.dumps(manifest), encoding="utf-8")


class ValidatePublishedTests(unittest.TestCase):
    def test_accepts_verified_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            rel = "submissions/dumatebench/v1/agentA__modelB.json"
            _write_manifest(repo_root, rel, {"status": "verified"})
            published = repo_root / "leaderboard" / "published.json"
            published.parent.mkdir(parents=True, exist_ok=True)
            published.write_text(json.dumps([rel]), encoding="utf-8")

            self.assertEqual(validate_published(published, repo_root), [])

    def test_rejects_missing_file(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            published = repo_root / "leaderboard" / "published.json"
            published.parent.mkdir(parents=True, exist_ok=True)
            rel = "submissions/dumatebench/v1/missing__model.json"
            published.write_text(json.dumps([rel]), encoding="utf-8")

            errors = validate_published(published, repo_root)
            self.assertTrue(any("missing submission" in e for e in errors))

    def test_rejects_unverified_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            rel = "submissions/dumatebench/v1/agentA__modelB.json"
            _write_manifest(repo_root, rel, None)
            published = repo_root / "leaderboard" / "published.json"
            published.parent.mkdir(parents=True, exist_ok=True)
            published.write_text(json.dumps([rel]), encoding="utf-8")

            errors = validate_published(published, repo_root)
            self.assertTrue(any("no recorded Harbor verification" in e for e in errors))

    def test_rejects_bad_status(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            rel = "submissions/dumatebench/v1/agentA__modelB.json"
            _write_manifest(repo_root, rel, {"status": "snapshot_pending_verification"})
            published = repo_root / "leaderboard" / "published.json"
            published.parent.mkdir(parents=True, exist_ok=True)
            published.write_text(json.dumps([rel]), encoding="utf-8")

            errors = validate_published(published, repo_root)
            self.assertTrue(any("verification status is not 'verified'" in e for e in errors))

    def test_rejects_duplicate_entry(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            rel = "submissions/dumatebench/v1/agentA__modelB.json"
            _write_manifest(repo_root, rel, {"status": "verified"})
            published = repo_root / "leaderboard" / "published.json"
            published.parent.mkdir(parents=True, exist_ok=True)
            published.write_text(json.dumps([rel, rel]), encoding="utf-8")

            errors = validate_published(published, repo_root)
            self.assertTrue(any("more than once" in e for e in errors))

    def test_rejects_malformed_path(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            published = repo_root / "leaderboard" / "published.json"
            published.parent.mkdir(parents=True, exist_ok=True)
            published.write_text(json.dumps(["not/a/submission/path.json"]), encoding="utf-8")

            errors = validate_published(published, repo_root)
            self.assertTrue(any("not a valid submission path" in e for e in errors))

    def test_rejects_non_array(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            published = repo_root / "leaderboard" / "published.json"
            published.parent.mkdir(parents=True, exist_ok=True)
            published.write_text(json.dumps({"not": "a list"}), encoding="utf-8")

            errors = validate_published(published, repo_root)
            self.assertTrue(any("JSON array" in e for e in errors))

    def test_empty_list_is_valid(self) -> None:
        with tempfile.TemporaryDirectory() as tmp:
            repo_root = Path(tmp)
            published = repo_root / "leaderboard" / "published.json"
            published.parent.mkdir(parents=True, exist_ok=True)
            published.write_text("[]", encoding="utf-8")

            self.assertEqual(validate_published(published, repo_root), [])


if __name__ == "__main__":
    unittest.main()
