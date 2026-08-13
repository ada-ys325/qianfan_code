#!/usr/bin/env python3
import json
import sys


def main():
    state = json.loads(sys.stdin.read())
    step = int(state.get("step", 1))
    if step == 1:
        print(json.dumps({"command": "find /workspace -maxdepth 3 -type f -print", "reason": "inspect workspace"}))
    else:
        print(json.dumps({"finish": True, "reason": "example adapter stops after one command"}))


if __name__ == "__main__":
    main()
