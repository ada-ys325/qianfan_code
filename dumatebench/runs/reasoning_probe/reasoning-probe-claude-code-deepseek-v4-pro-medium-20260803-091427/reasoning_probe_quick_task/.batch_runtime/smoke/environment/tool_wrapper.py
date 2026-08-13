#!/usr/bin/env python3
import json
import os
import random
import subprocess
import sys
import time
from pathlib import Path


CONFIG = Path(os.environ.get("DUMATE_TOOL_FAULT_CONFIG", "/opt/dumate/tool_faults.yaml"))
STATE = Path(os.environ.get("DUMATE_TOOL_FAULT_STATE", "/tmp/dumate_tool_fault_state.json"))
LOG = Path(os.environ.get("DUMATE_TOOL_FAULT_LOG", "/logs/tool_faults.jsonl"))


def parse_scalar(value):
    value = value.strip().strip('"').strip("'")
    if value.lower() == "true":
        return True
    if value.lower() == "false":
        return False
    if value.startswith("[") and value.endswith("]"):
        inner = value[1:-1].strip()
        if not inner:
            return []
        return [parse_scalar(part.strip()) for part in inner.split(",")]
    try:
        if "." in value:
            return float(value)
        return int(value)
    except ValueError:
        return value


def load_config(path):
    config = {"enabled": False, "seed": 0, "tools": {}}
    current_tool = None
    current_fault = None
    for raw in path.read_text().splitlines():
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            continue
        if indent == 0 and ":" in stripped and not stripped.endswith(":"):
            key, value = stripped.split(":", 1)
            config[key.strip()] = parse_scalar(value)
            continue
        if indent == 2 and stripped.endswith(":"):
            current_tool = stripped[:-1]
            config["tools"][current_tool] = {"faults": []}
            current_fault = None
            continue
        if current_tool and stripped.startswith("- "):
            current_fault = {}
            item = stripped[2:]
            if ":" in item:
                key, value = item.split(":", 1)
                current_fault[key.strip()] = parse_scalar(value)
            config["tools"][current_tool]["faults"].append(current_fault)
            continue
        if current_fault is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            current_fault[key.strip()] = parse_scalar(value)
    return config


def read_state():
    if STATE.exists():
        return json.loads(STATE.read_text())
    return {}


def write_state(state):
    STATE.parent.mkdir(parents=True, exist_ok=True)
    STATE.write_text(json.dumps(state, indent=2))


def log(record):
    record["ts"] = time.time()
    LOG.parent.mkdir(parents=True, exist_ok=True)
    with LOG.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def maybe_fault(tool, config):
    if not config.get("enabled"):
        return None
    state = read_state()
    tool_state = state.setdefault(tool, {})
    faults = config.get("tools", {}).get(tool, {}).get("faults", [])
    for fault in faults:
        kind = fault.get("kind", "TOOL_FAULT")
        used = int(tool_state.get(kind, 0))
        max_injections = int(fault.get("max_injections", 0))
        if used >= max_injections:
            continue
        seed = int(config.get("seed", 0)) + sum(ord(ch) for ch in f"{tool}:{kind}:{used}")
        fault["_seed"] = seed
        if random.Random(seed).random() <= float(fault.get("probability", 1.0)):
            tool_state[kind] = used + 1
            write_state(state)
            return fault
    write_state(state)
    return None


def exec_real(tool, argv):
    real = {
        "tesseract": "/usr/bin/tesseract",
        "calendar_write": "/opt/dumate/bin/calendar_write_real.py",
        "mail_send": "/opt/dumate/bin/mail_send_real.py",
        "ocr_extract": "/opt/dumate/bin/ocr_extract_real.py",
    }.get(tool)
    if not real:
        print(f"unknown wrapped tool: {tool}", file=sys.stderr)
        return 127
    return subprocess.run([real] + argv).returncode


def option_value(argv, name):
    if name not in argv:
        return None
    index = argv.index(name)
    if index + 1 >= len(argv):
        return None
    return argv[index + 1]


def option_values(argv, name):
    values = []
    index = 0
    while index < len(argv):
        if argv[index] == name and index + 1 < len(argv):
            values.append(argv[index + 1])
            index += 2
            continue
        index += 1
    return values


def mailboxes_from_args(argv):
    recipients = []
    for raw in option_values(argv, "--to"):
        recipients.extend(part.strip() for part in raw.split(",") if part.strip())
    sender = option_value(argv, "--sender") or option_value(argv, "--from") or "agent"
    values = recipients + [sender]
    mailboxes = []
    for value in values:
        name = value.split("@", 1)[0].strip() if "@" in value else value.strip()
        if name:
            mailboxes.append(name)
    return sorted(set(mailboxes))


def strip_ics_fields(path, fields):
    target = Path(path)
    if not target.exists():
        return []
    removed = []
    wanted = {str(field).upper() for field in fields}
    kept = []
    for raw in target.read_text(errors="ignore").splitlines():
        key = raw.split(":", 1)[0].split(";", 1)[0].upper()
        if key in wanted:
            removed.append(key)
            continue
        kept.append(raw)
    target.write_text("\n".join(kept) + "\n")
    return removed


def strip_jsonl_fields(path, fields):
    target = Path(path)
    if not target.exists():
        return []
    removed = []
    wanted = {str(field) for field in fields}
    lines = []
    for raw in target.read_text(errors="ignore").splitlines():
        try:
            record = json.loads(raw)
        except json.JSONDecodeError:
            lines.append(raw)
            continue
        if isinstance(record, dict):
            for field in wanted:
                if field in record:
                    removed.append(field)
                    record.pop(field, None)
            lines.append(json.dumps(record, ensure_ascii=False))
        else:
            lines.append(raw)
    target.write_text("\n".join(lines) + "\n")
    return removed


def strip_eml_fields(path, fields):
    target = Path(path)
    if not target.exists():
        return []
    wanted = {str(field).upper() for field in fields}
    removed = []
    lines = target.read_text(errors="ignore").splitlines()
    kept = []
    in_body = False
    for raw in lines:
        if not in_body and raw == "":
            in_body = True
            if "BODY" not in wanted and "CONTENT" not in wanted:
                kept.append(raw)
            else:
                removed.append("BODY")
            continue
        if not in_body:
            key = raw.split(":", 1)[0].upper()
            if key in wanted:
                removed.append(key)
                continue
        if in_body and ("BODY" in wanted or "CONTENT" in wanted):
            continue
        kept.append(raw)
    target.write_text("\n".join(kept) + "\n")
    return removed


def inject_output_field_missing(tool, argv, fault):
    fields = fault.get("fields", fault.get("missing_fields", []))
    if isinstance(fields, str):
        fields = [fields]
    real_rc = exec_real(tool, argv)
    removed = []
    if tool == "calendar_write":
        output = option_value(argv, "--output")
        if output:
            removed = strip_ics_fields(output, fields)
    elif tool == "mail_send":
        mail_root = Path(option_value(argv, "--mail-root") or "/outputs/emails")
        for mailbox in mailboxes_from_args(argv):
            for message_path in (mail_root / mailbox).glob("*.eml"):
                removed.extend(strip_eml_fields(message_path, fields))
    log({
        "tool": tool,
        "fault_injected": True,
        "kind": fault.get("kind"),
        "fields": fields,
        "removed_fields": sorted(set(removed)),
        "argv": argv,
    })
    stderr = fault.get("stderr")
    if stderr:
        print(stderr, file=sys.stderr)
    return int(fault.get("exit_code", real_rc))


def inject_fault(tool, argv, fault):
    kind = fault.get("kind", "TOOL_FAULT")
    if kind == "DELAYED_RESPONSE":
        delay = float(fault.get("delay_seconds", 1))
        log({"tool": tool, "fault_injected": True, "kind": kind, "delay_seconds": delay, "argv": argv})
        time.sleep(delay)
        return exec_real(tool, argv)
    if kind == "NONDETERMINISTIC_TIMEOUT":
        rng = random.Random(int(fault.get("_seed", 0)) + 7919)
        min_seconds = float(fault.get("min_timeout_seconds", fault.get("timeout_seconds", 5)))
        max_seconds = float(fault.get("max_timeout_seconds", min_seconds))
        timeout_seconds = rng.uniform(min_seconds, max_seconds)
        log({
            "tool": tool,
            "fault_injected": True,
            "kind": kind,
            "timeout_seconds": round(timeout_seconds, 3),
            "argv": argv,
        })
        time.sleep(timeout_seconds)
        print(
            fault.get("stderr", f"Tool execution timed out after {timeout_seconds:.1f} seconds."),
            file=sys.stderr,
        )
        return int(fault.get("exit_code", 124))
    if kind == "OUTPUT_FIELD_MISSING":
        return inject_output_field_missing(tool, argv, fault)

    log({"tool": tool, "fault_injected": True, "kind": kind, "argv": argv})
    print(fault.get("stderr", "Injected tool fault."), file=sys.stderr)
    return int(fault.get("exit_code", 1))


def main():
    tool = Path(sys.argv[0]).name
    argv = sys.argv[1:]
    config = load_config(CONFIG)
    fault = maybe_fault(tool, config)
    if fault:
        return inject_fault(tool, argv, fault)
    log({"tool": tool, "fault_injected": False, "argv": argv})
    return exec_real(tool, argv)


if __name__ == "__main__":
    raise SystemExit(main())
