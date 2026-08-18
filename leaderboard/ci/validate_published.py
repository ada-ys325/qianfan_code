"""Validate ``leaderboard/published.json``, the maintainer-curated allowlist.

Merging a bot PR into ``submissions/`` proves that a Harbor job passed
verification. Public visibility uses a separate maintainer decision: a
maintainer adds the submission path to this file in an ordinary PR. The
leaderboard site is expected to read only the paths listed here, so this
module's only job is to make sure
the list stays well-formed and every entry still points at a real, verified,
merged submission manifest.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

SUBMISSION_PATH_RE = re.compile(
    r"^submissions/dumatebench/[A-Za-z0-9._-]+/[A-Za-z0-9._-]+__[A-Za-z0-9._-]+\.json$"
)


def validate_published(published_path: Path, repo_root: Path) -> list[str]:
    """Check that ``published.json`` is a de-duplicated list of real, verified manifests.

    ``repo_root`` is the checked-out repository root the paths are relative
    to, so this also catches typos and stale entries left behind after a
    submission file is renamed or removed.
    """
    errors: list[str] = []
    published_path = published_path.resolve()
    if not published_path.is_file():
        return [f"published.json does not exist: {published_path}"]

    try:
        entries = json.loads(published_path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError) as exc:
        return [f"published.json is not valid JSON: {exc}"]

    if not isinstance(entries, list):
        return ["published.json must contain a JSON array of submission paths"]

    seen: set[str] = set()
    for index, entry in enumerate(entries):
        if not isinstance(entry, str):
            errors.append(f"published.json[{index}] must be a string path")
            continue
        if not SUBMISSION_PATH_RE.fullmatch(entry):
            errors.append(f"published.json[{index}] is not a valid submission path: {entry!r}")
            continue
        if entry in seen:
            errors.append(f"published.json lists {entry!r} more than once")
            continue
        seen.add(entry)

        manifest_path = repo_root / entry
        if not manifest_path.is_file():
            errors.append(f"published.json references a missing submission: {entry}")
            continue

        try:
            manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
        except (OSError, json.JSONDecodeError) as exc:
            errors.append(f"{entry}: not valid JSON: {exc}")
            continue
        if not isinstance(manifest, dict) or not isinstance(manifest.get("verification"), dict):
            errors.append(f"{entry}: has no recorded Harbor verification; cannot be published")
            continue
        if manifest["verification"].get("status") != "verified":
            errors.append(f"{entry}: verification status is not 'verified'")

    return errors


def main() -> int:
    import argparse
    import sys

    parser = argparse.ArgumentParser(description="Validate leaderboard/published.json.")
    parser.add_argument("published", type=Path)
    parser.add_argument("--repo-root", type=Path, default=Path("."))
    args = parser.parse_args()

    errors = validate_published(args.published, args.repo_root.resolve())
    if errors:
        print("published.json validation failed:")
        for error in errors:
            print(f"- {error}")
        return 1
    print("published.json validation passed.")
    return 0


if __name__ == "__main__":
    import sys

    sys.exit(main())
