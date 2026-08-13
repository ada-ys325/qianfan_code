#!/usr/bin/env python3
from pathlib import Path
import json

output = Path('/outputs/probe.txt')
ok = output.is_file() and output.read_text(encoding='utf-8', errors='ignore').strip() == 'reasoning-probe-ok'
reward = {
    'complete_pass': int(ok),
    'partial_pass': 1.0 if ok else 0.0,
    'checks': [{'name': 'probe_file', 'passed': bool(ok), 'detail': str(output)}],
}
Path('/outputs/reward.json').write_text(json.dumps(reward, indent=2) + '\n', encoding='utf-8')
raise SystemExit(0 if ok else 1)
