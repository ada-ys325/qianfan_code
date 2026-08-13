import importlib.util
import io
import tempfile
import unittest
from contextlib import redirect_stderr
from pathlib import Path


WRAPPER_PATH = (
    Path(__file__).resolve().parents[1]
    / "datasets/dev/odyssey_2_12_smoke/environment/tool_wrapper.py"
)


def load_wrapper():
    spec = importlib.util.spec_from_file_location("tool_wrapper", WRAPPER_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


class ToolWrapperTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.wrapper = load_wrapper()
        self.wrapper.LOG = self.root / "tool_faults.jsonl"

    def tearDown(self):
        self.tmp.cleanup()

    def test_load_config_parses_inline_lists_and_new_faults(self):
        config_path = self.root / "tool_faults.yaml"
        config_path.write_text(
            "\n".join(
                [
                    "enabled: true",
                    "seed: 1",
                    "tools:",
                    "  calendar_write:",
                    "    faults:",
                    "      - kind: OUTPUT_FIELD_MISSING",
                    "        probability: 1.0",
                    "        max_injections: 1",
                    "        fields: [DESCRIPTION, LOCATION]",
                    "        exit_code: 0",
                ]
            )
        )

        config = self.wrapper.load_config(config_path)
        fault = config["tools"]["calendar_write"]["faults"][0]
        self.assertEqual(fault["kind"], "OUTPUT_FIELD_MISSING")
        self.assertEqual(fault["fields"], ["DESCRIPTION", "LOCATION"])

    def test_output_field_missing_strips_ics_fields_after_real_tool(self):
        calendar = self.root / "Alice.ics"
        calendar.write_text(
            "\n".join(
                [
                    "BEGIN:VCALENDAR",
                    "BEGIN:VEVENT",
                    "SUMMARY:Meeting",
                    "DESCRIPTION:Should be removed",
                    "DTSTART:20200101T100000",
                    "END:VEVENT",
                    "END:VCALENDAR",
                ]
            )
            + "\n"
        )
        self.wrapper.exec_real = lambda tool, argv: 0

        rc = self.wrapper.inject_fault(
            "calendar_write",
            ["--output", str(calendar)],
            {
                "kind": "OUTPUT_FIELD_MISSING",
                "fields": ["DESCRIPTION"],
                "exit_code": 0,
            },
        )

        self.assertEqual(rc, 0)
        text = calendar.read_text()
        self.assertIn("SUMMARY:Meeting", text)
        self.assertNotIn("DESCRIPTION:", text)
        self.assertIn('"removed_fields": ["DESCRIPTION"]', self.wrapper.LOG.read_text())

    def test_nondeterministic_timeout_returns_timeout_code(self):
        with redirect_stderr(io.StringIO()):
            rc = self.wrapper.inject_fault(
                "mail_send",
                [],
                {
                    "kind": "NONDETERMINISTIC_TIMEOUT",
                    "_seed": 42,
                    "min_timeout_seconds": 0,
                    "max_timeout_seconds": 0,
                    "exit_code": 124,
                    "stderr": "Mail backend request timed out.",
                },
            )

        self.assertEqual(rc, 124)
        self.assertIn('"kind": "NONDETERMINISTIC_TIMEOUT"', self.wrapper.LOG.read_text())

    def test_delayed_response_executes_real_tool(self):
        calls = []
        self.wrapper.exec_real = lambda tool, argv: calls.append((tool, argv)) or 0

        rc = self.wrapper.inject_fault(
            "tesseract",
            ["input.png", "out"],
            {"kind": "DELAYED_RESPONSE", "delay_seconds": 0},
        )

        self.assertEqual(rc, 0)
        self.assertEqual(calls, [("tesseract", ["input.png", "out"])])
        self.assertIn('"kind": "DELAYED_RESPONSE"', self.wrapper.LOG.read_text())

    def test_output_field_missing_strips_eml_headers_after_real_tool(self):
        mail_root = self.root / "emails"
        bob = mail_root / "Bob"
        alice = mail_root / "Alice"
        bob.mkdir(parents=True)
        alice.mkdir(parents=True)
        for mailbox in [bob, alice]:
            (mailbox / "Project.eml").write_text(
                "\n".join(
                    [
                        "From: Alice@example.com",
                        "To: Bob@example.com",
                        "Subject: Project",
                        "",
                        "Body text",
                    ]
                )
                + "\n"
            )
        self.wrapper.exec_real = lambda tool, argv: 0

        rc = self.wrapper.inject_fault(
            "mail_send",
            [
                "--sender",
                "Alice",
                "--to",
                "Bob",
                "--subject",
                "Project",
                "--body",
                "Body text",
                "--mail-root",
                str(mail_root),
            ],
            {
                "kind": "OUTPUT_FIELD_MISSING",
                "fields": ["Subject"],
                "exit_code": 0,
            },
        )

        self.assertEqual(rc, 0)
        self.assertNotIn("Subject:", (bob / "Project.eml").read_text())
        self.assertNotIn("Subject:", (alice / "Project.eml").read_text())
        self.assertIn('"removed_fields": ["SUBJECT"]', self.wrapper.LOG.read_text())


if __name__ == "__main__":
    unittest.main()
