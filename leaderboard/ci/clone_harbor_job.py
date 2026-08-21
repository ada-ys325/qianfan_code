"""Create a leaderboard-owned Harbor snapshot for a submission job."""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path

REPOSITORY_ROOT = Path(__file__).resolve().parents[2]
if str(REPOSITORY_ROOT) not in sys.path:
    sys.path.insert(0, str(REPOSITORY_ROOT))

from leaderboard.ci.harbor_verify import HarborVerificationError, harbor_json


def job_uuid(value: str) -> str:
    return value.rstrip("/").rsplit("/", 1)[-1]


def _validate_manifest(path: Path) -> list[str]:
    try:
        from dumatebench_cli.submission import validate_submission_manifest
    except ModuleNotFoundError:
        sys.path.insert(0, str(REPOSITORY_ROOT / "dumatebench_cli"))
        from dumatebench_cli.submission import validate_submission_manifest
    return validate_submission_manifest(path)


async def clone_job(source_job_id: str, name_prefix: str) -> str:
    from uuid import UUID

    from harbor.hub import copy_job
    from harbor.upload.db_client import UploadDB

    result = await copy_job(source_job_id, name=f"{name_prefix}/{source_job_id}")
    if not result.complete:
        raise RuntimeError(
            f"Harbor copy did not complete: {result.n_remaining} object(s) remaining, "
            f"{result.n_failed} failed"
        )
    await UploadDB().update_job_visibility(UUID(result.job_id), "public")
    return str(result.job_id)


async def delete_clone(source_job_id: str, name_prefix: str) -> bool:
    """Delete one clone only after verifying its ownership prefix."""
    from harbor.hub.client import HubClient

    try:
        overview = harbor_json(["job", "show", source_job_id])
    except HarborVerificationError:
        print(f"Skipping {source_job_id}: Harbor job is already gone.", file=sys.stderr)
        return False
    name = str(overview.get("name") or "")
    if not name.startswith(f"{name_prefix}/"):
        print(
            f"Skipping {source_job_id}: Harbor name {name!r} does not match "
            f"clone prefix {name_prefix!r}.",
            file=sys.stderr,
        )
        return False
    deleted = await HubClient().delete_job(source_job_id)
    if not deleted:
        print(
            f"Could not delete {source_job_id}: it may be gone, not owned, "
            "or already linked to a leaderboard entry.",
            file=sys.stderr,
        )
        return False
    print(f"Deleted leaderboard-owned Harbor clone {source_job_id}.", file=sys.stderr)
    return True


def main() -> int:
    parser = argparse.ArgumentParser(description="Snapshot a DuMateBench Harbor job.")
    parser.add_argument("manifest", type=Path)
    parser.add_argument("--delete", action="store_true", help="Delete the referenced clone instead of copying a job.")
    parser.add_argument("--name-prefix", required=True)
    parser.add_argument("--output", type=Path)
    args = parser.parse_args()

    errors = _validate_manifest(args.manifest)
    if errors:
        for error in errors:
            print(error, file=sys.stderr)
        return 1
    manifest = json.loads(args.manifest.read_text(encoding="utf-8"))
    source_job_id = job_uuid(str(manifest["harbor_job_id"]))
    try:
        if args.delete:
            asyncio.run(delete_clone(source_job_id, args.name_prefix))
            return 0
        if args.output is None:
            parser.error("--output is required unless --delete is used")
        clone_id = asyncio.run(clone_job(source_job_id, args.name_prefix))
    except Exception as exc:  # noqa: BLE001 - report Harbor failures to CI
        print(f"Harbor snapshot failed: {type(exc).__name__}: {exc}", file=sys.stderr)
        return 1

    promoted = dict(manifest)
    promoted["harbor_job_id"] = clone_id
    promoted["verification"] = {
        "status": "snapshot_pending_verification",
        "source_harbor_job_id": source_job_id,
        "snapshot_name_prefix": args.name_prefix,
    }
    args.output.write_text(
        json.dumps(promoted, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    print(f"Created leaderboard-owned Harbor job {clone_id}.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
