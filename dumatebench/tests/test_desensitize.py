import json
from pathlib import Path
import unittest

from dumatebench.desensitize import create_whitelist_fields, mask_json_bytes, mask_text
from dumatebench.desensitize.__main__ import _should_process


class DesensitizeTest(unittest.TestCase):
    def test_masks_secret_key_patterns(self):
        text = 'token="sk-abcdefghijklmnopqrstuvwxyz123456" and aws_secret_access_key="ABCDEFGHIJKLMNOPQRSTUVWXYZ12345678901234"'

        masked, stats = mask_text(text)

        self.assertNotIn("sk-abcdefghijklmnopqrstuvwxyz123456", masked)
        self.assertNotIn("ABCDEFGHIJKLMNOPQRSTUVWXYZ12345678901234", masked)
        self.assertEqual(stats.rule_hits["secret_key"], 2)

    def test_masks_dlp_patterns(self):
        text = "张三 11010119900307123X phone 13800138000 mail a@example.com"

        masked, stats = mask_text(text)

        self.assertNotIn("11010119900307123X", masked)
        self.assertNotIn("13800138000", masked)
        self.assertNotIn("a@example.com", masked)
        self.assertEqual(stats.rule_hits["ID_CARD"], 1)
        self.assertEqual(stats.rule_hits["PHONE_CN"], 1)
        self.assertEqual(stats.rule_hits["EMAIL"], 1)

    def test_json_whitelist_and_sensitive_field_masking(self):
        payload = {
            "request_id": "sk-abcdefghijklmnopqrstuvwxyz123456",
            "password": "plain-secret",
            "nested": {"email": "user@example.com"},
        }

        masked_bytes, stats = mask_json_bytes(json.dumps(payload).encode(), create_whitelist_fields())
        masked = json.loads(masked_bytes)

        self.assertEqual(masked["request_id"], payload["request_id"])
        self.assertEqual(masked["password"], "***")
        self.assertEqual(masked["nested"]["email"], "***")
        self.assertEqual(masked["desensitize_rule_hits"]["SECRET_KV_QUOTED"], 1)
        self.assertEqual(masked["desensitize_rule_hits"]["EMAIL"], 1)
        self.assertEqual(stats.masked_secrets, 2)

    def test_json_output_stays_pretty_printed(self):
        payload = {"name": "a@example.com", "safe": 1}

        masked_bytes, _ = mask_json_bytes(json.dumps(payload).encode(), create_whitelist_fields())
        masked_text = masked_bytes.decode()

        self.assertIn('\n  "name": "***"', masked_text)
        self.assertTrue(masked_text.endswith("\n"))

    def test_default_cli_filter_only_allows_requested_task_files(self):
        root = Path("dumatebench/datasets/dev/task")

        self.assertTrue(_should_process(root / "instruction.md"))
        self.assertTrue(_should_process(root / "session_chat_history.json"))
        self.assertTrue(_should_process(root / "workspace_seed" / "user.md"))
        self.assertTrue(_should_process(root / "workspace_seed" / "session_chat_history.json"))
        self.assertTrue(_should_process(root / "work_space_seed" / "notes.md"))
        self.assertTrue(_should_process(root / "evaluator" / "checks.yaml"))
        self.assertFalse(_should_process(root / "task.yaml"))
        self.assertFalse(_should_process(root / "annotation_review.json"))
        self.assertFalse(_should_process(root / "environment" / "tool_wrapper.log"))
        self.assertFalse(_should_process(root / "evaluator" / "evaluator.py"))
        self.assertFalse(_should_process(root / "workspace_seed" / "notes.txt"))


if __name__ == "__main__":
    unittest.main()
