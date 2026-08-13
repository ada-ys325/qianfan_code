from __future__ import annotations

import base64
import json
import os
import time
import urllib.error
import urllib.request
from pathlib import Path
from typing import Any

class ProviderError(RuntimeError):
    pass
class AttachmentError(ProviderError):
    pass
class ProviderAttachmentError(AttachmentError):
    pass

def attachment_parts(inventory: list[dict[str, Any]], mode: str = "data_url") -> list[dict[str, Any]]:
    parts: list[dict[str, Any]] = []
    for item in inventory:
        transport = item.get("transport", {})
        if transport.get("status") != "ready":
            continue
        raw = item.get("_bytes")
        if raw is None:
            try:
                raw = Path(item["path"]).read_bytes()
            except OSError as exc:
                raise AttachmentError(f"cannot read image {item.get('path')}: {exc}") from exc
        mime = item.get("mime") or "application/octet-stream"
        if mode == "data_url":
            source = "data:" + mime + ";base64," + base64.b64encode(raw).decode("ascii")
        elif mode == "direct_file":
            source = str(item["path"])
        elif mode == "url":
            source = item.get("url")
            if not source:
                raise AttachmentError(f"URL transport requires url for {item.get('path')}")
        else:
            raise AttachmentError(f"unsupported transport mode: {mode}")
        parts.extend([{"type": "text", "text": f"[{item['role']} {item['id']}]"}, {"type": "image_url", "image_url": {"url": source}}])
    return parts

class OpenAIJsonClient:
    def __init__(self, model: str, base_url: str, api_key: str | None = None, *, timeout: int = 120, retries: int = 5, transport_mode: str = "data_url") -> None:
        self.model, self.base_url, self.api_key = model, base_url.rstrip("/"), api_key or os.getenv("OPENAI_API_KEY")
        self.timeout, self.retries, self.transport_mode = timeout, retries, transport_mode
    def complete_json(
        self,
        messages: list[dict[str, Any]],
        attachments: list[dict[str, Any]] | None = None,
        response_format: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        payload_messages = list(messages)
        if attachments:
            payload_messages.append({"role": "user", "content": attachment_parts(attachments, self.transport_mode)})
        payload = {"model": self.model, "messages": payload_messages, "temperature": 0, "response_format": response_format or {"type": "json_object"}}
        request = urllib.request.Request(self.base_url + "/chat/completions", data=json.dumps(payload).encode(), headers={"Content-Type": "application/json", **({"Authorization": f"Bearer {self.api_key}"} if self.api_key else {})})
        last: Exception | None = None
        for attempt in range(self.retries + 1):
            try:
                with urllib.request.urlopen(request, timeout=self.timeout) as response:
                    body = json.loads(response.read().decode("utf-8"))
                content = body["choices"][0]["message"]["content"]
                if isinstance(content, list):
                    content = "".join(str(x.get("text", "")) for x in content if isinstance(x, dict))
                if not isinstance(content, str):
                    raise ProviderError("provider returned non-text content")
                return json.loads(content)
            except urllib.error.HTTPError as exc:
                detail = exc.read().decode("utf-8", errors="replace")[:1000]
                if attachments and exc.code in {400, 413, 415, 422}:
                    raise ProviderAttachmentError(f"provider rejected image attachments ({exc.code}): {detail}") from exc
                last = ProviderError(f"provider HTTP error {exc.code}: {detail}")
            except (urllib.error.URLError, KeyError, IndexError, json.JSONDecodeError, ProviderError) as exc:
                last = exc
            if attempt < self.retries:
                time.sleep(0.5 * (attempt + 1))
        raise ProviderError(f"provider JSON request failed: {last}") from last
