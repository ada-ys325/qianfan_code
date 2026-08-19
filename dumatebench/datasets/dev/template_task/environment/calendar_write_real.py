#!/usr/bin/env python3
import argparse
from datetime import datetime
from pathlib import Path

from icalendar import Calendar, Event


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--input-text", required=True)
    parser.add_argument("--output", required=True)
    parser.add_argument("--summary", default="Converted handwritten meeting agenda")
    args = parser.parse_args()

    text = Path(args.input_text).read_text(errors="ignore").strip()
    if not text:
        text = "Meeting agenda text could not be reliably extracted by OCR in this smoke run."

    cal = Calendar()
    cal.add("prodid", "-//DuMateBench template_task//EN")
    cal.add("version", "2.0")

    event = Event()
    event.add("uid", "template-task@dumatebench")
    event.add("summary", args.summary)
    event.add("description", text[:1200])
    event.add("dtstart", datetime(2020, 5, 1, 10, 0, 0))
    event.add("dtend", datetime(2020, 5, 1, 11, 0, 0))
    cal.add_component(event)

    out = Path(args.output)
    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_bytes(cal.to_ical())
    print(f"wrote {out}")


if __name__ == "__main__":
    main()
