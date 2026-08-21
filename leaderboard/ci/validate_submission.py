"""Trusted local entry point for DuMateBench submission structure checks.

The formal Harbor manifest is verified by ``harbor_verify.py`` in the GitHub
workflow. This command remains useful for checking a local evidence bundle
before it is uploaded or attached for manual review.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from dumatebench_cli.submission import validate_submission, validate_submission_manifest


def main() -> int:
    parser = argparse.ArgumentParser(description="Validate a DuMateBench submission bundle.")
    parser.add_argument("submission", type=Path)
    args = parser.parse_args()

    if args.submission.is_file():
        errors = validate_submission_manifest(args.submission)
        description = "Harbor submission manifest"
    else:
        errors = validate_submission(args.submission)
        description = "submission bundle"
    if errors:
        print("Submission validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1

    if args.submission.is_file():
        print(f"{description} validation passed.")
        print("Run leaderboard/ci/harbor_verify.py in trusted CI for Harbor verification.")
    else:
        summary_path = args.submission / "batch_summary.jsonl"
        records = [json.loads(line) for line in summary_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"{description} validation passed: {len(records)} task(s).")
        print("Submitted reward values are evidence only, not an official score.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
