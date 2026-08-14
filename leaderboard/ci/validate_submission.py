"""Trusted GitHub CI entry point for the file-based submission intake.

The workflow checks out this script from the base branch and supplies a
submission directory fetched from the PR as data. This command validates file
completeness only; Harbor-backed authenticity and score recomputation are the
next phase of the leaderboard implementation.
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
        print("Harbor fetch and score recomputation are the next verification step.")
    else:
        summary_path = args.submission / "batch_summary.jsonl"
        records = [json.loads(line) for line in summary_path.read_text(encoding="utf-8").splitlines() if line.strip()]
        print(f"{description} validation passed: {len(records)} task(s).")
        print("Submitted reward values are evidence only, not an official score.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
