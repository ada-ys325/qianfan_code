#!/usr/bin/env python3
import argparse
import json
import pwd
import random
import subprocess
import time
from pathlib import Path


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
    config = {
        "enabled": False,
        "seed": 0,
        "affected_user": "",
        "apply_tc_to_all_traffic": False,
        "exempt_base_urls": [],
        "exempt_domains": [],
        "startup_faults": [],
        "periodic_faults": {"enabled": False, "faults": []},
    }
    section = None
    current = None
    periodic_section = None
    periodic_faults = []
    for raw in path.read_text().splitlines():
        stripped = raw.strip()
        indent = len(raw) - len(raw.lstrip(" "))
        if not stripped or stripped.startswith("#"):
            continue
        if indent == 0 and stripped.endswith(":"):
            if section == "startup_faults" and current:
                config["startup_faults"].append(current)
            section = stripped[:-1]
            current = None
            continue
        if indent == 0 and ":" in stripped:
            key, value = stripped.split(":", 1)
            config[key.strip()] = parse_scalar(value)
            continue
        if section == "startup_faults" and stripped.startswith("- "):
            if current:
                config["startup_faults"].append(current)
            current = {}
            item = stripped[2:]
            if ":" in item:
                key, value = item.split(":", 1)
                current[key.strip()] = parse_scalar(value)
            continue
        if section in {"exempt_base_urls", "exempt_domains"} and stripped.startswith("- "):
            config[section].append(parse_scalar(stripped[2:]))
            continue
        if section == "startup_faults" and current is not None and ":" in stripped:
            key, value = stripped.split(":", 1)
            current[key.strip()] = parse_scalar(value)
            continue
        if section == "periodic_faults":
            if indent == 2 and ":" in stripped and not stripped.startswith("- "):
                key, value = stripped.split(":", 1)
                key = key.strip()
                if value.strip():
                    config["periodic_faults"][key] = parse_scalar(value)
                    periodic_section = None
                else:
                    periodic_section = key
                    if key == "faults":
                        config["periodic_faults"]["faults"] = periodic_faults
            elif periodic_section == "faults" and stripped.startswith("- "):
                item = stripped[2:]
                fault = {}
                if ":" in item:
                    key, value = item.split(":", 1)
                    fault[key.strip()] = parse_scalar(value)
                periodic_faults.append(fault)
            elif periodic_section == "faults" and periodic_faults and ":" in stripped:
                key, value = stripped.split(":", 1)
                periodic_faults[-1][key.strip()] = parse_scalar(value)
    if current:
        config["startup_faults"].append(current)
    if periodic_faults:
        config["periodic_faults"]["faults"] = periodic_faults
    return config


def run(cmd, check=False):
    result = subprocess.run(cmd, text=True, capture_output=True)
    if check and result.returncode != 0:
        raise RuntimeError(f"{cmd} failed: {result.stderr}")
    return result


def log(log_path, record):
    record["ts"] = time.time()
    log_path.parent.mkdir(parents=True, exist_ok=True)
    with log_path.open("a") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def affected_owner_args(config):
    user = str(config.get("affected_user", "")).strip()
    if not user:
        return []
    try:
        uid = pwd.getpwnam(user).pw_uid
    except KeyError:
        return []
    return ["-m", "owner", "--uid-owner", str(uid)]


def affected_uid(config):
    user = str(config.get("affected_user", "")).strip()
    if not user:
        return None
    try:
        return pwd.getpwnam(user).pw_uid
    except KeyError:
        return None


def iptables(action, args):
    return run(["iptables", action, "OUTPUT"] + args)


def iptables_table(table, action, chain, args):
    return run(["iptables", "-t", table, action, chain] + args)


def apply_dns_fail(fault, config, log_path):
    owner_args = affected_owner_args(config)
    rules = [
        owner_args + ["-p", "udp", "--dport", "53", "-j", "REJECT"],
        owner_args + ["-p", "tcp", "--dport", "53", "-j", "REJECT"],
    ]
    for rule in rules:
        iptables("-I", rule)
    log(log_path, {
        "event": "apply",
        "kind": "dns_fail",
        "affected_user": config.get("affected_user", ""),
        "duration_seconds": fault["duration_seconds"],
    })
    return lambda: [iptables("-D", rule) for rule in rules]


def apply_block_ip(fault, config, log_path):
    ip = str(fault["ip"])
    ports = fault.get("ports", [])
    owner_args = affected_owner_args(config)
    rules = []
    if ports:
        for port in ports:
            rules.append(owner_args + ["-p", "tcp", "-d", ip, "--dport", str(port), "-j", "REJECT"])
    else:
        rules.append(owner_args + ["-d", ip, "-j", "REJECT"])
    for rule in rules:
        iptables("-I", rule)
    log(log_path, {
        "event": "apply",
        "kind": "block_ip",
        "affected_user": config.get("affected_user", ""),
        "ip": ip,
        "ports": ports,
        "duration_seconds": fault["duration_seconds"],
    })
    return lambda: [iptables("-D", rule) for rule in rules]


def apply_latency_loss(fault, config, log_path):
    delay = int(fault.get("delay_ms", 0))
    loss = float(fault.get("loss_percent", 0))
    uid = affected_uid(config)

    if uid is not None:
        mark = str(fault.get("fwmark", "0x10"))
        mark_rule = ["-m", "owner", "--uid-owner", str(uid), "-j", "MARK", "--set-mark", mark]
        iptables_table("mangle", "-I", "OUTPUT", mark_rule)
        run(["tc", "qdisc", "del", "dev", "eth0", "root"])
        commands = [
            ["tc", "qdisc", "add", "dev", "eth0", "root", "handle", "1:", "htb", "default", "10"],
            ["tc", "class", "add", "dev", "eth0", "parent", "1:", "classid", "1:10", "htb", "rate", "1000mbit", "quantum", "1514"],
            ["tc", "class", "add", "dev", "eth0", "parent", "1:", "classid", "1:20", "htb", "rate", "1000mbit", "quantum", "1514"],
        ]
        netem = ["tc", "qdisc", "add", "dev", "eth0", "parent", "1:20", "handle", "20:", "netem"]
        if delay:
            netem += ["delay", f"{delay}ms"]
        if loss:
            netem += ["loss", f"{loss}%"]
        commands.append(netem)
        commands.append(["tc", "filter", "add", "dev", "eth0", "parent", "1:", "protocol", "ip", "handle", mark, "fw", "flowid", "1:20"])

        results = [run(cmd) for cmd in commands]
        log(log_path, {
            "event": "apply",
            "kind": "latency_loss",
            "mode": "fwmark_user_scoped",
            "affected_user": config.get("affected_user", ""),
            "fwmark": mark,
            "delay_ms": delay,
            "loss_percent": loss,
            "duration_seconds": fault["duration_seconds"],
            "returncodes": [result.returncode for result in results],
            "stderr": "\n".join(result.stderr.strip() for result in results if result.stderr.strip()),
        })

        def cleanup_scoped():
            run(["tc", "qdisc", "del", "dev", "eth0", "root"])
            iptables_table("mangle", "-D", "OUTPUT", mark_rule)

        return cleanup_scoped

    if not config.get("apply_tc_to_all_traffic", False):
        log(log_path, {
            "event": "skip",
            "kind": "latency_loss",
            "reason": "tc netem has no affected_user scope and apply_tc_to_all_traffic is false",
        })
        return lambda: None

    run(["tc", "qdisc", "del", "dev", "eth0", "root"])
    args = ["tc", "qdisc", "add", "dev", "eth0", "root", "netem"]
    if delay:
        args += ["delay", f"{delay}ms"]
    if loss:
        args += ["loss", f"{loss}%"]
    result = run(args)
    log(log_path, {
        "event": "apply",
        "kind": "latency_loss",
        "mode": "all_traffic",
        "delay_ms": delay,
        "loss_percent": loss,
        "duration_seconds": fault["duration_seconds"],
        "returncode": result.returncode,
        "stderr": result.stderr.strip(),
    })
    return lambda: run(["tc", "qdisc", "del", "dev", "eth0", "root"])


def apply_fault(fault, config, log_path):
    kind = fault.get("kind")
    if kind == "dns_fail":
        return apply_dns_fail(fault, config, log_path)
    if kind == "block_ip":
        return apply_block_ip(fault, config, log_path)
    if kind == "latency_loss":
        return apply_latency_loss(fault, config, log_path)
    log(log_path, {"event": "skip", "kind": kind, "reason": "unknown fault kind"})
    return lambda: None


def schedule_fault(fault, config, rng, log_path):
    probability = float(fault.get("probability", 1.0))
    if rng.random() > probability:
        log(log_path, {
            "event": "skip",
            "kind": fault.get("kind"),
            "phase": fault.get("_phase", ""),
            "iteration": fault.get("_iteration"),
            "probability": probability,
        })
        return
    duration = int(fault.get("duration_seconds", 1))
    log(log_path, {
        "event": "selected",
        "kind": fault.get("kind"),
        "phase": fault.get("_phase", ""),
        "iteration": fault.get("_iteration"),
        "probability": probability,
        "duration_seconds": duration,
    })
    cleanup = apply_fault(fault, config, log_path)
    time.sleep(duration)
    cleanup()
    log(log_path, {
        "event": "clear",
        "kind": fault.get("kind"),
        "phase": fault.get("_phase", ""),
        "iteration": fault.get("_iteration"),
    })


def main():
    parser = argparse.ArgumentParser()
    parser.add_argument("--config", default="/opt/dumate/network_faults.yaml")
    parser.add_argument("--log", default="/logs/network_faults.jsonl")
    args = parser.parse_args()

    config = load_config(Path(args.config))
    log_path = Path(args.log)
    rng = random.Random(int(config.get("seed", 0)))
    log(log_path, {"event": "start", "enabled": bool(config.get("enabled"))})
    log(log_path, {
        "event": "exemptions",
        "exempt_base_urls": config.get("exempt_base_urls", []),
        "exempt_domains": config.get("exempt_domains", []),
        "note": "In-container LLM calls use trusted base_url settings; iptables and tc/fwmark faults are scoped to the configured affected_user by default.",
    })
    if not config.get("enabled"):
        return

    for fault in config.get("startup_faults", []):
        fault["_phase"] = "startup"
        schedule_fault(fault, config, rng, log_path)

    periodic = config.get("periodic_faults", {})
    if periodic.get("enabled"):
        interval = int(periodic.get("interval_seconds", 60))
        iteration = 0
        while True:
            time.sleep(interval)
            iteration += 1
            for fault in periodic.get("faults", []):
                fault["_phase"] = "periodic"
                fault["_iteration"] = iteration
                schedule_fault(fault, config, rng, log_path)


if __name__ == "__main__":
    main()
