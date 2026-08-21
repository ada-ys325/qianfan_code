import json
import tempfile
import threading
import unittest
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from dumatebench.agents.native_agent import (
    ModelProxy,
    anthropic_to_response_payload,
    build_command,
    build_prompt,
    claude_auth_environment,
    is_anthropic_model,
    responses_input_to_anthropic_messages,
    responses_tools_to_anthropic_tools,
    resolve_api_key,
    resolve_base_url,
    summarize_native_cost,
    task_timeout,
    write_codex_config,
)


class NativeAgentTest(unittest.TestCase):
    def test_task_timeout_and_prompt_use_task_contract(self):
        with tempfile.TemporaryDirectory() as tmp:
            task_dir = Path(tmp)
            (task_dir / "task.yaml").write_text("agent:\n  timeout_sec: 321\n", encoding="utf-8")
            (task_dir / "instruction.md").write_text("Save /outputs/result.txt", encoding="utf-8")

            self.assertEqual(task_timeout(task_dir, 0), 321)
            prompt = build_prompt(task_dir)

        self.assertIn("/workspace", prompt)
        self.assertIn("Save /outputs/result.txt", prompt)

    def test_backend_specific_environment_resolution(self):
        env = {
            "OPENAI_BASE_URL": "https://openai.example/v1/",
            "OPENAI_API_KEY": "openai-key",
            "ANTHROPIC_BASE_URL": "https://anthropic.example/v1/",
            "ANTHROPIC_API_KEY": "anthropic-key",
        }

        self.assertEqual(resolve_base_url("codex", "", env), "https://openai.example/v1")
        self.assertEqual(resolve_base_url("claude-code", "", env), "https://anthropic.example")
        self.assertEqual(resolve_base_url("claude-code", "https://gateway.example/v1", env), "https://gateway.example")
        self.assertEqual(resolve_api_key("codex", env), "openai-key")
        self.assertEqual(resolve_api_key("claude-code", env), "anthropic-key")
        self.assertEqual(claude_auth_environment(env), {"ANTHROPIC_API_KEY": "anthropic-key"})

        env["DUMATE_AGENT_API_KEY"] = "gateway-key"
        self.assertEqual(claude_auth_environment(env), {"ANTHROPIC_AUTH_TOKEN": "gateway-key"})

    def test_codex_config_uses_responses_provider_without_secret(self):
        with tempfile.TemporaryDirectory() as tmp:
            home = Path(tmp)
            write_codex_config(home, "gpt-test", "http://127.0.0.1:1234/v1")
            config = (home / "config.toml").read_text(encoding="utf-8")

        self.assertIn('model = "gpt-test"', config)
        self.assertIn('wire_api = "responses"', config)
        self.assertIn('env_key = "DUMATE_NATIVE_API_KEY"', config)
        self.assertNotIn("secret", config)

    def test_native_commands_are_noninteractive(self):
        codex, codex_stdin = build_command("codex", "gpt-test", "do task", 0)
        claude, claude_stdin = build_command("claude-code", "model-test", "do task", 0)
        limited_claude, _ = build_command("claude-code", "model-test", "do task", 7)

        self.assertEqual(codex_stdin, "do task")
        self.assertIn("--ephemeral", codex)
        self.assertIn("--dangerously-bypass-approvals-and-sandbox", codex)
        self.assertIsNone(claude_stdin)
        self.assertIn("--bare", claude)
        self.assertIn("--dangerously-skip-permissions", claude)
        self.assertNotIn("--max-turns", claude)
        self.assertIn("--max-turns", limited_claude)
        self.assertIn("7", limited_claude)

    def test_model_proxy_forwards_path_body_and_response(self):
        seen = {}

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen["path"] = self.path
                seen["body"] = json.loads(body)
                payload = b'{"ok":true}'
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format_string, *args):
                del format_string, args

        try:
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        except PermissionError:
            self.skipTest("local socket binding is disabled by this test sandbox")
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
                proxy = ModelProxy(upstream_url, Path(tmp) / "proxy.jsonl")
                local_url = proxy.start()
                request = urllib.request.Request(
                    f"{local_url}/responses",
                    data=b'{"model":"gpt-test"}',
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    result = json.loads(response.read())
                proxy.stop()
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)

        self.assertEqual(result, {"ok": True})
        self.assertEqual(seen["path"], "/v1/responses")
        self.assertEqual(seen["body"], {"model": "gpt-test"})

    def test_responses_adapter_converts_to_anthropic_messages(self):
        messages = responses_input_to_anthropic_messages([
            {"role": "user", "content": [{"type": "input_text", "text": "hello"}]},
            {"type": "function_call", "call_id": "call_1", "name": "shell", "arguments": '{"cmd":"pwd"}'},
            {"type": "function_call_output", "call_id": "call_1", "output": "/workspace"},
        ])
        tools = responses_tools_to_anthropic_tools([
            {"type": "function", "name": "shell", "description": "Run shell", "parameters": {"type": "object"}},
        ])
        response = anthropic_to_response_payload({
            "content": [
                {"type": "text", "text": "done"},
                {"type": "tool_use", "id": "toolu_1", "name": "shell", "input": {"cmd": "ls"}},
            ],
            "usage": {"input_tokens": 3, "output_tokens": 4},
        }, "claude-opus-4-8")

        self.assertEqual(messages[0]["role"], "user")
        self.assertEqual(messages[0]["content"][0]["text"], "hello")
        self.assertEqual(messages[1]["content"][0]["type"], "tool_use")
        self.assertEqual(messages[2]["content"][0]["type"], "tool_result")
        self.assertEqual(tools[0]["name"], "shell")
        self.assertEqual(response["output"][0]["type"], "message")
        self.assertEqual(response["output"][1]["type"], "function_call")
        self.assertEqual(response["usage"]["total_tokens"], 7)

    def test_model_proxy_adapts_claude_responses_to_messages(self):
        seen = {}

        class UpstreamHandler(BaseHTTPRequestHandler):
            def do_POST(self):  # noqa: N802
                body = self.rfile.read(int(self.headers["Content-Length"]))
                seen["path"] = self.path
                seen["body"] = json.loads(body)
                seen["x_api_key"] = self.headers.get("x-api-key")
                payload = json.dumps({
                    "id": "msg_test",
                    "type": "message",
                    "role": "assistant",
                    "content": [{"type": "text", "text": "Hi from Claude"}],
                    "stop_reason": "end_turn",
                    "usage": {"input_tokens": 5, "output_tokens": 3},
                }).encode()
                self.send_response(200)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

            def log_message(self, format_string, *args):
                del format_string, args

        try:
            upstream = ThreadingHTTPServer(("127.0.0.1", 0), UpstreamHandler)
        except PermissionError:
            self.skipTest("local socket binding is disabled by this test sandbox")
        thread = threading.Thread(target=upstream.serve_forever, daemon=True)
        thread.start()
        try:
            with tempfile.TemporaryDirectory() as tmp:
                upstream_url = f"http://127.0.0.1:{upstream.server_address[1]}/v1"
                proxy = ModelProxy(upstream_url, Path(tmp) / "proxy.jsonl", api_key="secret")
                local_url = proxy.start()
                request = urllib.request.Request(
                    f"{local_url}/responses",
                    data=json.dumps({
                        "model": "claude-opus-4-8",
                        "input": "Say hi",
                        "max_output_tokens": 16,
                    }).encode(),
                    headers={"Content-Type": "application/json"},
                    method="POST",
                )
                with urllib.request.urlopen(request, timeout=5) as response:
                    result = json.loads(response.read())
                proxy.stop()
        finally:
            upstream.shutdown()
            upstream.server_close()
            thread.join(timeout=5)

        self.assertEqual(seen["path"], "/v1/messages")
        self.assertEqual(seen["x_api_key"], "secret")
        self.assertEqual(seen["body"]["model"], "claude-opus-4-8")
        self.assertEqual(seen["body"]["messages"][0]["content"][0]["text"], "Say hi")
        self.assertEqual(result["object"], "response")
        self.assertEqual(result["output"][0]["content"][0]["text"], "Hi from Claude")

    def test_messages_adapter_model_detection_includes_gateway_chat_models(self):
        self.assertTrue(is_anthropic_model("claude-opus-4-8"))
        self.assertTrue(is_anthropic_model("anthropic/claude-opus-4-8"))
        self.assertTrue(is_anthropic_model("glm-5.2"))
        self.assertTrue(is_anthropic_model("GLM-5.2"))
        self.assertTrue(is_anthropic_model("ds/deepseek-v4-pro"))
        self.assertTrue(is_anthropic_model("deepseek-v4-pro"))
        self.assertFalse(is_anthropic_model("gpt-5.5"))

    def test_summarize_native_cost_reads_usage_and_proxy_calls(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            stdout = root / "native_agent.jsonl"
            proxy = root / "model_proxy.jsonl"
            stdout.write_text(
                json.dumps({
                    "type": "result",
                    "usage": {"input_tokens": 10, "output_tokens": 5, "cache_read_input_tokens": 2},
                    "total_cost_usd": 0.0012,
                })
                + "\n",
                encoding="utf-8",
            )
            proxy.write_text(
                json.dumps({"status": 200}) + "\n" + json.dumps({"status": 429}) + "\n",
                encoding="utf-8",
            )

            cost = summarize_native_cost(
                stdout,
                proxy,
                backend="claude-code",
                model="claude-test",
                elapsed_seconds=12.5,
            )

        self.assertEqual(cost["api_calls"], 2)
        self.assertEqual(cost["api_status_counts"], {"200": 1, "429": 1})
        self.assertEqual(cost["input_tokens"], 10)
        self.assertEqual(cost["output_tokens"], 5)
        self.assertEqual(cost["cache_tokens"], 2)
        self.assertEqual(cost["total_tokens"], 17)
        self.assertEqual(cost["total_cost_usd"], 0.0012)


if __name__ == "__main__":
    unittest.main()
