from __future__ import annotations

import argparse
import json
import os
from pathlib import Path

from .llm import OpenAIJsonClient
from .runner import ImageJudgeRunner

def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="llm-judge-image")
    sub = p.add_subparsers(dest="command", required=True)
    for name in ("generate-rubric", "judge", "run"):
        x = sub.add_parser(name)
        x.add_argument("--task-dir", required=True, type=Path)
        x.add_argument("--candidate-dir", type=Path, default=None)
        x.add_argument("--reference-dir", type=Path, default=None)
        x.add_argument("--output-dir", required=True, type=Path)
        x.add_argument("--rubric", type=Path, default=None)
        x.add_argument("--model", default=os.getenv("IMAGE_JUDGE_MODEL", "gpt-4o-mini"))
        x.add_argument("--base-url", default=os.getenv("OPENAI_BASE_URL", "https://api.openai.com/v1"))
        x.add_argument("--api-key-env", default="OPENAI_API_KEY")
        x.add_argument("--max-bytes", type=int, default=20 * 1024 * 1024)
        x.add_argument("--max-count", type=int, default=32)
        x.add_argument("--max-total-bytes", type=int, default=100 * 1024 * 1024)
        x.add_argument("--transport-mode", choices=("data_url", "url", "direct_file"), default="data_url")
        x.add_argument("--svg-policy", choices=("rasterize", "reject"), default="rasterize")
        x.add_argument("--timeout", type=int, default=120)
    return p

def main(argv: list[str] | None = None) -> int:
    args = parser().parse_args(argv)
    if args.command in {"judge", "run"} and args.candidate_dir is None:
        raise SystemExit("error: --candidate-dir is required for judge/run (no candidate images)")
    client = OpenAIJsonClient(args.model, args.base_url, os.getenv(args.api_key_env), timeout=args.timeout, transport_mode=args.transport_mode)
    runner = ImageJudgeRunner(client, max_bytes=args.max_bytes, max_count=args.max_count, max_total_bytes=args.max_total_bytes, transport_mode=args.transport_mode, svg_policy=args.svg_policy)
    try:
        if args.command == "generate-rubric":
            result = runner.generate_rubric(args.task_dir, args.reference_dir)
            args.output_dir.mkdir(parents=True, exist_ok=True)
            (args.output_dir / "rubric.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        elif args.command == "judge":
            if not args.rubric:
                raise SystemExit("error: --rubric is required for judge")
            result = runner.judge(args.task_dir, args.candidate_dir, args.reference_dir, json.loads(args.rubric.read_text(encoding="utf-8")))
            args.output_dir.mkdir(parents=True, exist_ok=True)
            (args.output_dir / "judge_result.json").write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
        else:
            result = runner.run(args.task_dir, args.candidate_dir, args.reference_dir, args.output_dir)
        print(json.dumps({"weighted_score": result.get("weighted_score"), "gate": result.get("gate"), "output_dir": str(args.output_dir)}, ensure_ascii=False))
        return 0
    except Exception as exc:
        raise SystemExit(f"{type(exc).__name__}: {exc}") from exc

if __name__ == "__main__":
    raise SystemExit(main())
