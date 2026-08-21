"""Harbor BaseAgent bridge for the DuMateBench adapter contract.

Wraps the stdin/stdout JSON protocol defined in
``dumatebench/agents/agent_contract.md`` (and already implemented in
``adapter.py``'s ``build_state``/``parse_action``) so it can run under
Harbor's execution engine instead of our own ``run_task_batch.py`` container
lifecycle. Harbor drives the container; this class only translates each
adapter turn into calls on ``environment.exec()``.

Requires the ``harbor`` package (``pip install harbor``) at runtime. Import
of harbor's base classes is deferred into ``__init__``/class body via a
try/except so this module can be imported (and its pure helpers unit-tested)
without harbor installed.
"""

from __future__ import annotations

import json
import time
from typing import Any

from dumatebench_cli.adapter import (
    DEFAULT_SYSTEM_PROMPT,
    AdapterError,
    StepRecord,
    build_state,
    parse_action,
)

try:
    from harbor.agents.base import BaseAgent
    from harbor.environments.base import BaseEnvironment
    from harbor.models.agent.context import AgentContext

    _HARBOR_AVAILABLE = True
except ImportError:  # pragma: no cover - exercised only without harbor installed
    BaseAgent = object  # type: ignore[assignment,misc]
    BaseEnvironment = Any  # type: ignore[assignment,misc]
    AgentContext = Any  # type: ignore[assignment,misc]
    _HARBOR_AVAILABLE = False


AGENT_PATH = "/opt/dumate/wrappers:/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin"
MAX_OBS_CHARS = 6000


def _truncate(text: str, limit: int = MAX_OBS_CHARS) -> str:
    if len(text) <= limit:
        return text
    return text[:limit] + f"\n...[truncated {len(text) - limit} chars]"


class DumateBenchAgent(BaseAgent):  # type: ignore[misc]
    """Runs a DuMateBench adapter-contract agent inside a Harbor environment.

    ``model_name`` is unused directly by this class -- the adapter command
    itself decides which model/policy to call -- but is accepted so Harbor's
    orchestration (which always passes it) does not need a special case.
    """

    SUPPORTS_WINDOWS: bool = False

    def __init__(
        self,
        logs_dir: Any,
        agent_cmd: str,
        model_name: str | None = None,
        max_steps: int = 20,
        adapter_timeout_sec: int = 180,
        system_prompt: str = DEFAULT_SYSTEM_PROMPT,
        **kwargs: Any,
    ) -> None:
        if not _HARBOR_AVAILABLE:
            raise ImportError(
                "The 'harbor' package is required to run DumateBenchAgent. "
                "Install it with `pip install harbor`."
            )
        super().__init__(logs_dir, model_name=model_name, **kwargs)
        self._agent_cmd = agent_cmd
        self._max_steps = max_steps
        self._adapter_timeout_sec = adapter_timeout_sec
        self._system_prompt = system_prompt

    @staticmethod
    def name() -> str:
        return "dumatebench-adapter"

    def version(self) -> str | None:
        return "0.1.0"

    async def setup(self, environment: "BaseEnvironment") -> None:
        # The adapter contract requires no in-container install step: the
        # agent process is invoked on the host side (call_adapter), not
        # inside the task container.
        return

    async def _call_adapter(self, state: dict[str, Any]) -> dict[str, Any]:
        """Run the adapter command on the host, mirroring adapter.call_adapter.

        The adapter reads task state from stdin and writes its next action to
        stdout; it is not part of the task environment, so this runs as a
        host-side subprocess rather than through ``environment.exec()``.
        """
        import shlex
        import subprocess

        result = subprocess.run(
            shlex.split(self._agent_cmd),
            input=json.dumps(state, ensure_ascii=False),
            text=True,
            capture_output=True,
            timeout=self._adapter_timeout_sec,
        )
        if result.returncode != 0:
            raise AdapterError(
                f"Agent adapter failed ({result.returncode}): {self._agent_cmd}\n"
                f"STDOUT:\n{result.stdout}\nSTDERR:\n{result.stderr}"
            )
        return parse_action(result.stdout.strip())

    async def _exec_in_environment(
        self, environment: "BaseEnvironment", command: str
    ) -> dict[str, Any]:
        """Run one adapter-issued command inside the Harbor environment.

        Mirrors adapter.exec_in_task's semantics (same user, PATH, and fault
        injection env vars) but through Harbor's exec() instead of
        ``docker compose exec``.
        """
        start = time.time()
        result = await environment.exec(
            command=command,
            cwd="/workspace",
            user="agent",
            env={
                "HOME": "/home/agent",
                "PATH": AGENT_PATH,
                "DUMATE_TOOL_FAULT_CONFIG": "/opt/dumate/tool_faults.yaml",
                "DUMATE_TOOL_FAULT_LOG": "/logs/tool_faults.jsonl",
            },
        )
        elapsed = round(time.time() - start, 3)
        output = result.stdout or ""
        if result.stderr:
            output += "\nSTDERR:\n" + result.stderr
        return {
            "returncode": result.return_code,
            "elapsed_sec": elapsed,
            "output": _truncate(output),
        }

    async def run(
        self,
        instruction: str,
        environment: "BaseEnvironment",
        context: "AgentContext",
    ) -> None:
        history: list[StepRecord] = []
        finish_reason: str | None = None

        for step in range(1, self._max_steps + 1):
            state = build_state(instruction, step, self._max_steps, history, self._system_prompt)
            action = await self._call_adapter(state)

            if action["finish"]:
                finish_reason = action.get("reason", "")
                break

            observation = await self._exec_in_environment(environment, action["command"])
            history.append(StepRecord(action=action, observation=observation))
        else:
            finish_reason = f"Agent reached max steps ({self._max_steps}) without finish=true."

        context.metadata = {
            "finish_reason": finish_reason,
            "steps_taken": len(history),
            "history": [
                {"action": record.action, "observation": record.observation}
                for record in history
            ],
        }
