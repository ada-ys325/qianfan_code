"""CLI for generating DuMateBench workspace noise."""

from __future__ import annotations

import argparse
import json
from pathlib import Path

from .injector import NoiseConfig, NoiseInjector


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Generate file-level and data-level noise for DuMateBench task files.",
    )
    parser.add_argument(
        "inputs",
        nargs="+",
        type=Path,
        help="Input files or directories used as the source for noise generation.",
    )
    parser.add_argument(
        "-o",
        "--output-dir",
        type=Path,
        default=None,
        help="Directory where noise files are written. Defaults to each input file's parent.",
    )
    parser.add_argument("--seed", type=int, default=0, help="Deterministic random seed.")
    parser.add_argument(
        "--file-noise-count",
        type=int,
        default=3,
        help="Number of filename/project distractors per input file.",
    )
    parser.add_argument(
        "--data-noise-count",
        type=int,
        default=2,
        help="Number of content-level distractors per input file.",
    )
    parser.add_argument(
        "--no-file-noise",
        action="store_true",
        help="Disable similar filename, backup, temporary, and unrelated project files.",
    )
    parser.add_argument(
        "--no-data-noise",
        action="store_true",
        help="Disable format-aware content distractors.",
    )
    parser.add_argument(
        "--manifest",
        type=Path,
        default=None,
        help="Path for the generated JSON manifest. Defaults to <output-dir>/noise_manifest.json.",
    )
    parser.add_argument(
        "--recursive",
        action="store_true",
        help="When an input is a directory, include files recursively.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    files = _expand_inputs(args.inputs, recursive=args.recursive)
    if not files:
        parser.error("no input files found")

    config = NoiseConfig(
        output_dir=args.output_dir,
        seed=args.seed,
        file_noise_count=args.file_noise_count,
        data_noise_count=args.data_noise_count,
        include_file_noise=not args.no_file_noise,
        include_data_noise=not args.no_data_noise,
    )
    manifest = NoiseInjector(config).generate(files)

    manifest_path = args.manifest
    if manifest_path is None:
        base_dir = args.output_dir or files[0].parent
        manifest_path = base_dir / "noise_manifest.json"
    manifest_path.parent.mkdir(parents=True, exist_ok=True)
    manifest_path.write_text(json.dumps(manifest, ensure_ascii=False, indent=2) + "\n")
    print(json.dumps({"manifest": str(manifest_path), "records": len(manifest["records"])}, ensure_ascii=False))
    return 0


def _expand_inputs(inputs: list[Path], *, recursive: bool) -> list[Path]:
    files: list[Path] = []
    for input_path in inputs:
        path = input_path.expanduser()
        if path.is_file():
            files.append(path)
        elif path.is_dir():
            iterator = path.rglob("*") if recursive else path.glob("*")
            files.extend(item for item in iterator if item.is_file())
        else:
            raise FileNotFoundError(path)
    return sorted(files)
