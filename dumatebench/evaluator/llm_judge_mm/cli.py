from __future__ import annotations

import argparse
from pathlib import Path

from .artifacts import MediaConfig
from .llm import LLMClient
from .runner import JudgeRunner, load_task_inputs, read_json, write_json


def _common(parser: argparse.ArgumentParser) -> None:
    parser.add_argument("--task-dir", type=Path, required=True)
    parser.add_argument("--model", default="gpt-4o-mini")
    parser.add_argument("--base-url", default=None)
    parser.add_argument("--outputs-dir", type=Path, default=None)
    parser.add_argument("--reference-dir", type=Path, default=None)
    parser.add_argument("--media-mode", choices=("data_url", "url", "disabled"), default=None)
    parser.add_argument("--media-base-url", default=None)
    parser.add_argument("--media-max-bytes", type=int, default=None)
    parser.add_argument("--video-mode", choices=("frames", "video_url"), default=None)
    parser.add_argument("--video-frame-count", type=int, default=None)
    parser.add_argument("--video-frame-max-bytes", type=int, default=None)
    parser.add_argument("--ffmpeg-path", default=None)
    parser.add_argument("--ffprobe-path", default=None)


def _config(args: argparse.Namespace) -> MediaConfig:
    env = MediaConfig.from_env()
    config = MediaConfig(
        mode=args.media_mode or env.mode,
        base_url=args.media_base_url or env.base_url,
        max_bytes=args.media_max_bytes or env.max_bytes,
        video_mode=args.video_mode or env.video_mode,
        video_frame_count=args.video_frame_count or env.video_frame_count,
        video_frame_max_bytes=args.video_frame_max_bytes or env.video_frame_max_bytes,
        ffmpeg_path=args.ffmpeg_path or env.ffmpeg_path,
        ffprobe_path=args.ffprobe_path or env.ffprobe_path,
    )
    config.validate()
    return config


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Isolated MP3/MP4 multimodal LLM judge")
    sub = parser.add_subparsers(dest="command", required=True)
    rubric = sub.add_parser("generate-rubric")
    _common(rubric)
    rubric.add_argument("--rubric-out", type=Path, required=True)
    judge = sub.add_parser("judge")
    _common(judge)
    judge.add_argument("--rubric", type=Path, required=True)
    judge.add_argument("--result-out", type=Path, required=True)
    judge.add_argument("--judge-runs", type=int, default=1)
    args = parser.parse_args(argv)
    config = _config(args)
    client = LLMClient(model=args.model, base_url=args.base_url)
    runner = JudgeRunner(client, media_config=config)
    inputs = load_task_inputs(args.task_dir, outputs_dir=args.outputs_dir, reference_dir=args.reference_dir, media_config=config)
    if args.command == "generate-rubric":
        result = runner.generate_rubric(task_id=args.task_dir.name, instruction=inputs["instruction"],
            checks_path=inputs["checks_path"], references=inputs["references"])
        write_json(args.rubric_out, result)
    else:
        rubric_value = read_json(args.rubric)
        result = runner.evaluate(instruction=inputs["instruction"], rubric=rubric_value,
            artifacts=inputs["artifacts"], references=inputs["references"], judge_runs=args.judge_runs)
        write_json(args.result_out, result)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
