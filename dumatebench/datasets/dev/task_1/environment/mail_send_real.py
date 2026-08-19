#!/usr/bin/env python3
import argparse
import re
from email.message import EmailMessage
from pathlib import Path


def mailbox_name(address):
    name = address.strip()
    if "@" in name:
        name = name.split("@", 1)[0]
    return name or "unknown"


def split_recipients(values):
    recipients = []
    for value in values:
        recipients.extend(part.strip() for part in value.split(",") if part.strip())
    return recipients


def safe_filename(subject):
    filename = re.sub(r"[^A-Za-z0-9._-]+", "_", subject.strip()).strip("._")
    return filename or "message"


def unique_message_path(mailbox_dir, subject):
    stem = safe_filename(subject)
    candidate = mailbox_dir / f"{stem}.eml"
    index = 2
    while candidate.exists():
        candidate = mailbox_dir / f"{stem}_{index}.eml"
        index += 1
    return candidate


def format_address(name):
    value = name.strip()
    if "@" in value:
        return value
    return f"{value}@example.com"


def write_message(mail_root, mailbox, message, subject):
    mailbox_dir = mail_root / mailbox
    mailbox_dir.mkdir(parents=True, exist_ok=True)
    path = unique_message_path(mailbox_dir, subject)
    path.write_text(message.as_string(), encoding="utf-8")
    return path


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--sender", "--from", dest="sender", default="agent")
    parser.add_argument("--to", required=True, action="append")
    parser.add_argument("--subject", required=True)
    parser.add_argument("--body", required=True)
    parser.add_argument("--mail-root", default="/outputs/emails")
    args = parser.parse_args()

    recipients = split_recipients(args.to)
    if not recipients:
        raise SystemExit("at least one recipient is required")

    mail_root = Path(args.mail_root)
    message = EmailMessage()
    message["From"] = format_address(args.sender)
    message["To"] = ", ".join(format_address(recipient) for recipient in recipients)
    message["Subject"] = args.subject
    message.set_content(args.body)

    written = []
    for recipient in recipients:
        written.append(write_message(mail_root, mailbox_name(recipient), message, args.subject))
    written.append(write_message(mail_root, mailbox_name(args.sender), message, args.subject))

    print("sent mail:")
    for path in written:
        print(path)


if __name__ == "__main__":
    main()
