from __future__ import annotations

import base64
import json
import os
import subprocess
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

from .artifacts import MediaConfig


class MediaTransportError(RuntimeError):
    pass


def _read_media(path: Path, artifact_path: str, config: MediaConfig) -> bytes:
    with path.open("rb") as handle:
        size = os.fstat(handle.fileno()).st_size
        if size > config.max_bytes:
            raise MediaTransportError(
                f"cannot attach {artifact_path}: media size {size} exceeds configured limit {config.max_bytes}"
            )
        raw = handle.read(config.max_bytes + 1)
    if len(raw) > config.max_bytes:
        raise MediaTransportError(
            f"cannot attach {artifact_path}: media grew beyond configured limit {config.max_bytes}"
        )
    return raw


def _run_media_command(command: list[str], *, pass_fd: int | None = None) -> bytes:
    try:
        pass_fds = (pass_fd,) if pass_fd is not None else ()
        completed = subprocess.run(
            command, stdout=subprocess.PIPE, stderr=subprocess.PIPE, check=True,
            timeout=60, pass_fds=pass_fds,
        )
    except FileNotFoundError as exc:
        raise MediaTransportError(
            f"video frame extraction tool is unavailable: {command[0]}; "
            "set DU_MATE_FFMPEG_PATH/DU_MATE_FFPROBE_PATH"
        ) from exc
    except subprocess.TimeoutExpired as exc:
        raise MediaTransportError(f"video frame extraction timed out: {command[0]}") from exc
    except subprocess.CalledProcessError as exc:
        detail = exc.stderr.decode("utf-8", errors="replace").strip()[:500]
        suffix = f": {detail}" if detail else ""
        raise MediaTransportError(f"video frame extraction failed: {command[0]} exited {exc.returncode}{suffix}") from exc
    return completed.stdout


def _extract_video_frames(path: Path, artifact_path: str, config: MediaConfig) -> list[tuple[float, bytes]]:
    size = path.stat().st_size
    if size > config.max_bytes:
        raise MediaTransportError(
            f"cannot attach {artifact_path}: media size {size} exceeds configured limit {config.max_bytes}"
        )
    source = str(path)
    duration_raw = _run_media_command([
        config.ffprobe_path, "-v", "error", "-show_entries", "format=duration",
        "-of", "default=noprint_wrappers=1:nokey=1", source,
    ])
    try:
        duration = float(duration_raw.decode("ascii").strip())
    except (UnicodeDecodeError, ValueError) as exc:
        raise MediaTransportError(f"cannot determine video duration: {artifact_path}") from exc
    if not 0 < duration < 24 * 60 * 60:
        raise MediaTransportError(f"invalid video duration for {artifact_path}: {duration}")
    timestamps = [(index + 0.5) * duration / config.video_frame_count
                  for index in range(config.video_frame_count)]
    frames = []
    for timestamp in timestamps:
        frame = _run_media_command([
            config.ffmpeg_path, "-hide_banner", "-loglevel", "error", "-ss", f"{timestamp:.3f}",
            "-i", source, "-frames:v", "1", "-vf", "scale=min(1280\\,iw):-2",
            "-f", "image2pipe", "-vcodec", "mjpeg", "pipe:1",
        ])
        if not frame:
            raise MediaTransportError(f"ffmpeg returned an empty frame for {artifact_path} at {timestamp:.3f}s")
        if len(frame) > config.video_frame_max_bytes:
            raise MediaTransportError(
                f"video frame for {artifact_path} exceeds configured limit {config.video_frame_max_bytes}"
            )
        frames.append((timestamp, frame))
    return frames


def _video_frame_parts(artifact: dict[str, Any], config: MediaConfig) -> list[dict[str, Any]]:
    frames = _extract_video_frames(Path(artifact["source_path"]), artifact["path"], config)
    parts: list[dict[str, Any]] = []
    for timestamp, frame in frames:
        data = base64.b64encode(frame).decode("ascii")
        parts.extend([
            {"type": "text", "text": f"视频帧：{artifact['path']} @ {timestamp:.3f}s"},
            {"type": "image_url", "image_url": {
                "url": f"data:image/jpeg;base64,{data}", "detail": "low",
            }},
        ])
    return parts


def _media_parts(artifact: dict[str, Any], config: MediaConfig) -> list[dict[str, Any]]:
    transport = artifact.get("transport") or {}
    if transport.get("status") != "ready":
        raise MediaTransportError(
            f"cannot attach {artifact.get('path')}: {transport.get('reason', 'media is unavailable')}"
        )
    category = artifact["category"]
    mime = artifact["mime_type"]
    path = Path(artifact["source_path"])
    if category == "video" and config.video_mode == "frames":
        return _video_frame_parts(artifact, config)
    if config.mode == "url":
        url = transport["reference"]
        if category == "video":
            return [{"type": "video_url", "video_url": {"url": url}}]
        return [{"type": "audio_url", "audio_url": {"url": url}}]
    raw = _read_media(path, artifact["path"], config)
    data = base64.b64encode(raw).decode("ascii")
    if category == "audio":
        fmt = artifact["suffix"].lstrip(".")
        if fmt == "m4a":
            fmt = "mp4"
        if fmt not in {"mp3", "wav"}:
            raise MediaTransportError(
                f"OpenAI-compatible input_audio does not standardize {mime}; use URL mode or a provider adapter"
            )
        return [{"type": "input_audio", "input_audio": {"data": data, "format": fmt}}]
    return [{"type": "video_url", "video_url": {"url": f"data:{mime};base64,{data}"}}]


def build_multimodal_messages(
    *, text: str, artifacts: list[dict[str, Any]], media_config: MediaConfig,
) -> list[dict[str, Any]]:
    media_config.validate()
    content: list[dict[str, Any]] = [{"type": "text", "text": text}]
    for artifact in artifacts:
        if artifact.get("category") == "text":
            continue
        representation = "sampled image frames" if artifact["category"] == "video" and media_config.video_mode == "frames" else "direct media"
        content.append({
            "type": "text",
            "text": f"媒体附件：{artifact['path']} ({artifact['mime_type']}; representation={representation})",
        })
        try:
            content.extend(_media_parts(artifact, media_config))
        except MediaTransportError as exc:
            artifact["transport"] = {
                "status": "cannot_assess",
                "mode": media_config.mode,
                "reason": str(exc),
            }
            content.append({"type": "text", "text": f"媒体附件不可用：{artifact['path']}；{exc}"})
    return [{"role": "user", "content": content}]


class LLMClient:
    def __init__(
        self, *, model: str, base_url: str | None = None,
        api_key_env: str = "OPENAI_API_KEY", timeout: float = 120.0,
        temperature: float = 0.1, max_tokens: int = 4000, retries: int = 5,
    ) -> None:
        self.model = model
        self.base_url = (base_url or os.environ.get("OPENAI_BASE_URL") or "https://api.openai.com/v1").rstrip("/")
        self.api_key = os.environ.get(api_key_env, "")
        self.timeout = timeout
        self.temperature = temperature
        self.max_tokens = max_tokens
        self.retries = retries

    def complete_json(
        self,
        messages: list[dict[str, Any]],
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload = {
            "model": self.model, "messages": messages, "temperature": self.temperature,
            "max_tokens": self.max_tokens, "response_format": response_format or {"type": "json_object"},
        }
        request = urllib.request.Request(
            f"{self.base_url}/chat/completions", data=json.dumps(payload, ensure_ascii=False).encode(),
            headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})},
            method="POST",
        )
        last_error: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(part.get("text", "") for part in content if isinstance(part, dict))
                value = json.loads(content)
                if not isinstance(value, dict):
                    raise ValueError("model response is not a JSON object")
                return value
            except urllib.error.HTTPError as exc:
                last_error = exc
                if any(isinstance(message.get("content"), list) for message in messages):
                    raise MediaTransportError(
                        f"provider rejected multimodal Chat Completions payload (HTTP {exc.code}); "
                        "check image/audio support and DU_MATE_VIDEO_MODE/DU_MATE_MEDIA_MODE"
                    ) from exc
            except (urllib.error.URLError, TimeoutError, ValueError, KeyError, json.JSONDecodeError) as exc:
                last_error = exc
            if attempt < self.retries:
                time.sleep(0.5 * (attempt + 1))
        raise RuntimeError(f"LLM request failed after {self.retries + 1} attempts") from last_error
