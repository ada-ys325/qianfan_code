import subprocess
import sys
import tempfile
import unittest
from email import policy
from email.parser import BytesParser
from pathlib import Path


MAIL_SEND_PATH = (
    Path(__file__).resolve().parents[1]
    / "datasets/dev/odyssey_2_12_smoke/environment/mail_send_real.py"
)


class MailSendRealTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)

    def tearDown(self):
        self.tmp.cleanup()

    def parse_message(self, path):
        return BytesParser(policy=policy.default).parsebytes(path.read_bytes())

    def test_mail_send_writes_eml_for_sender_and_recipient(self):
        result = subprocess.run(
            [
                sys.executable,
                str(MAIL_SEND_PATH),
                "--sender",
                "Alice",
                "--to",
                "Bob",
                "--subject",
                "Project Update",
                "--body",
                "The report is ready.",
                "--mail-root",
                str(self.root / "emails"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        bob_messages = list((self.root / "emails" / "Bob").glob("*.eml"))
        alice_messages = list((self.root / "emails" / "Alice").glob("*.eml"))
        self.assertEqual(len(bob_messages), 1)
        self.assertEqual(len(alice_messages), 1)

        message = self.parse_message(bob_messages[0])
        self.assertEqual(message["From"], "Alice@example.com")
        self.assertEqual(message["To"], "Bob@example.com")
        self.assertEqual(message["Subject"], "Project Update")
        self.assertIn("The report is ready.", message.get_content())

    def test_mail_send_accepts_repeated_and_comma_separated_recipients(self):
        result = subprocess.run(
            [
                sys.executable,
                str(MAIL_SEND_PATH),
                "--sender",
                "Alice",
                "--to",
                "Bob,Carol",
                "--to",
                "Tom@example.com",
                "--subject",
                "Meeting",
                "--body",
                "Join at 10.",
                "--mail-root",
                str(self.root / "emails"),
            ],
            text=True,
            capture_output=True,
            check=False,
        )

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(list((self.root / "emails" / "Bob").glob("*.eml")))
        self.assertTrue(list((self.root / "emails" / "Carol").glob("*.eml")))
        self.assertTrue(list((self.root / "emails" / "Tom").glob("*.eml")))
        self.assertTrue(list((self.root / "emails" / "Alice").glob("*.eml")))


if __name__ == "__main__":
    unittest.main()
