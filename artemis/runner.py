# Copyright 2026 Google LLC
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""Synchronous, embeddable runner for ARTEMIS tasks in any Python project.

Usage:
    from artemis import ArtemisRunner

    with ArtemisRunner(device_serial="emulator-5554") as bot:
        result = bot.run("Open Settings and check the Wi-Fi status")
        print(result)
"""

from __future__ import annotations

import asyncio
import os
from pathlib import Path
from typing import TYPE_CHECKING, Any

from artemis.config import settings
from artemis.sdk.agent import Agent

if TYPE_CHECKING:
    from artemis.sdk.types.agent import AgentConfig

_UNSET = object()


def _parse_runner_timeout(val: object) -> float | None:
    if val is None or val is _UNSET:
        return None
    if isinstance(val, (int, float)):
        return None if val <= 0 else float(val)
    s = str(val).strip().lower()
    if s in ("0", "-1", "none", "null", "unlimited", "infinite", "disable", "disabled", "false"):
        return None
    try:
        parsed = float(s)
        return None if parsed <= 0 else parsed
    except ValueError:
        return 600.0


class ArtemisRunner:
    """Synchronous wrapper around the ARTEMIS agent: start once, run tasks, close."""

    def __init__(
        self,
        device_serial: str | None = None,
        profile: str = "flash",
        timeout: float | None | object = _UNSET,
        traces_dir: Path | str | None = None,
        *,
        config: AgentConfig | None = None,
        session_id: str | None = None,
        concurrency_mode: str | None = None,
        max_concurrency: int | None = None,
        api_key: str | None = None,
    ):
        """Create the runner (no connections until start() or context manager enter).

        Args:
            device_serial: ADB serial from `adb devices`; None picks the first device.
            profile: Default ARTEMIS profile, 'flash' (fast) or 'pro' (plans and checks).
            timeout: Default seconds per task; None or <= 0 waits without limit.
                If unset, defaults to the ARTEMIS_TASK_TIMEOUT env var or 600.0s.
            traces_dir: Custom destination directory for session traces and recordings.
            config: Optional pre-built AgentConfig (e.g. via Builders.AgentConfig).
            session_id: Optional canonical session ID for trace and telemetry correlation.
            concurrency_mode: Optional concurrency mode ('per_device' or 'global').
            max_concurrency: Optional maximum concurrent tasks limit.
            api_key: Optional LLM API key passed to Agent.init().
        """
        self.device_serial = device_serial
        self.profile = profile
        self.config = config
        self.session_id = session_id
        self.concurrency_mode = concurrency_mode
        self.max_concurrency = max_concurrency
        self.api_key = api_key
        if timeout is _UNSET:
            self.timeout = settings.ARTEMIS_TASK_TIMEOUT
        else:
            self.timeout = _parse_runner_timeout(timeout)
        self._traces_dir: Path | None = Path(traces_dir).resolve() if traces_dir else None
        self._custom_traces_dir = self._traces_dir is not None
        self._prev_traces_dir = os.environ.get("ARTEMIS_TRACES_DIR")
        if self._traces_dir is not None:
            self._traces_dir.mkdir(parents=True, exist_ok=True)
            os.environ["ARTEMIS_TRACES_DIR"] = str(self._traces_dir)
        self.agent: Agent | None = None
        self._loop = asyncio.new_event_loop()

    def start(self) -> None:
        """Connect to the device and initialize the agent."""
        if self._loop.is_closed():
            raise RuntimeError("ArtemisRunner is closed; create a new instance.")
        if self.agent is not None:
            return
        agent_kwargs: dict[str, Any] = {"device_serial": self.device_serial}
        if self.config is not None:
            agent_kwargs["config"] = self.config
        if self.session_id is not None:
            agent_kwargs["session_id"] = self.session_id
        if self.concurrency_mode is not None:
            agent_kwargs["concurrency_mode"] = self.concurrency_mode
        if self.max_concurrency is not None:
            agent_kwargs["max_concurrency"] = self.max_concurrency

        agent = Agent(**agent_kwargs)
        if self._traces_dir is not None:
            agent._tmp_traces_dir = self._traces_dir
            cfg = getattr(agent, "_config", None)
            defaults = getattr(cfg, "task_request_defaults", None)
            if defaults is not None and hasattr(defaults, "trace_path"):
                defaults.trace_path = self._traces_dir

        init_kwargs: dict[str, Any] = {}
        if self.api_key is not None:
            init_kwargs["api_key"] = self.api_key
        self._loop.run_until_complete(agent.init(**init_kwargs))
        self.agent = agent

    def run(
        self,
        goal: str,
        profile: str | None = None,
        timeout: float | None | object = _UNSET,
        output: Any = None,
        return_steps: bool = False,
        include_steps: bool = False,
        *,
        locked_app_package: str | None = None,
        app_path: str | Path | None = None,
        name: str | None = None,
        max_steps: int | None = None,
    ) -> Any:
        """Run one task and return the outcome.

        Args:
            goal: What to do on the mobile device, in natural language.
            profile: Overrides the default profile for this task.
            timeout: Overrides the default timeout in seconds; None or <= 0 waits without limit.
            output: Optional structured output description or Pydantic model class.
            return_steps: If True, returns a dict with 'output', 'status', and detailed 'steps'.
            include_steps: Alias for return_steps.
            locked_app_package: Optional Android package name to constrain execution to.
            app_path: Optional APK file path to install before running the task.
            name: Optional custom name for the task and trace folder.
            max_steps: Optional maximum number of turns/steps for this task.
        """
        if self._loop.is_closed():
            raise RuntimeError("ArtemisRunner is closed; create a new instance.")
        if self.agent is None:
            self.start()
        assert self.agent is not None

        effective_profile = profile or self.profile
        if max_steps is not None and hasattr(self.agent, "new_task"):
            builder = self.agent.new_task(goal).with_max_steps(max_steps)
            if effective_profile:
                builder.using_profile(effective_profile)
            if output is not None:
                if isinstance(output, str):
                    builder.with_output_description(output)
                else:
                    builder.with_output_format(output)
            if name is not None:
                builder.with_name(name)
            if locked_app_package is not None:
                builder.with_locked_app_package(locked_app_package)
            if app_path is not None:
                builder.with_app_path(app_path)
            if self._traces_dir is not None:
                builder.with_trace_recording(enabled=True, path=str(self._traces_dir))
            task = self.agent.run_task(request=builder.build())
        else:
            run_kwargs: dict[str, Any] = {
                "goal": goal,
                "profile": effective_profile,
                "output": output,
            }
            if locked_app_package is not None:
                run_kwargs["locked_app_package"] = locked_app_package
            if app_path is not None:
                run_kwargs["app_path"] = app_path
            if name is not None:
                run_kwargs["name"] = name
            task = self.agent.run_task(**run_kwargs)

        limit = self.timeout if timeout is _UNSET else _parse_runner_timeout(timeout)
        try:
            raw_result = self._loop.run_until_complete(asyncio.wait_for(task, limit))
        except (TimeoutError, asyncio.CancelledError, KeyboardInterrupt):
            if hasattr(self.agent, "stop_current_task"):
                self.agent.stop_current_task()
            raise

        if return_steps or include_steps:
            steps = self.get_steps()
            status = self._resolve_last_status(raw_result)
            return {
                "output": raw_result,
                "status": status,
                "steps": steps,
                "current_activity": steps[-1].get("current_activity") if steps else None,
            }
        return raw_result

    def _resolve_last_status(self, raw_result: Any) -> str:
        """Resolve authoritative task status from the result payload and agent task history."""
        if isinstance(raw_result, dict):
            if raw_result.get("status") in ("failed", "cancelled", "completed"):
                return str(raw_result["status"])
            test_summary = raw_result.get("test_summary")
            if isinstance(test_summary, dict) and test_summary.get("task_status") == "blocked":
                return "failed"
        tasks = getattr(self.agent, "_tasks", None)
        if isinstance(tasks, list) and tasks:
            last_task_status = getattr(tasks[-1], "status", None)
            if last_task_status in ("completed", "failed", "cancelled"):
                return str(last_task_status)
        return "completed"

    def install_apk(self, apk_path: str | Path) -> None:
        """Install an APK file on the target device synchronously."""
        if self._loop.is_closed():
            raise RuntimeError("ArtemisRunner is closed; create a new instance.")
        if self.agent is None:
            self.start()
        assert self.agent is not None
        self._loop.run_until_complete(self.agent.install_apk(apk_path))

    def get_screenshot(self) -> Any:
        """Capture and return a PIL Image screenshot from the connected device."""
        if self._loop.is_closed():
            raise RuntimeError("ArtemisRunner is closed; create a new instance.")
        if self.agent is None:
            self.start()
        assert self.agent is not None
        return self._loop.run_until_complete(self.agent.get_screenshot())

    def stop(self) -> None:
        """Request cancellation of the currently running task, if any."""
        if self.agent is not None and hasattr(self.agent, "stop_current_task"):
            self.agent.stop_current_task()

    def get_steps(self, session_id: str | None = None) -> list[dict[str, Any]]:
        """Retrieve all recorded steps with commands and coordinates for the last or specified session."""
        if self.agent is not None and hasattr(self.agent, "get_steps"):
            res = self.agent.get_steps(session_id=session_id)
            if isinstance(res, list):
                return res
        return []

    @property
    def last_steps(self) -> list[dict[str, Any]]:
        """List of all recorded steps for the most recent task execution."""
        return self.get_steps()

    def close(self) -> None:
        """Clean up running tasks, disconnect the agent, and release event loop resources."""
        if self._loop.is_closed():
            return
        if self.agent is not None and hasattr(self.agent, "stop_current_task"):
            self.agent.stop_current_task()
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        if self.agent is not None:
            self._loop.run_until_complete(self.agent.clean())
            self.agent = None
        self._loop.close()
        if self._custom_traces_dir:
            if self._prev_traces_dir is None:
                os.environ.pop("ARTEMIS_TRACES_DIR", None)
            else:
                os.environ["ARTEMIS_TRACES_DIR"] = self._prev_traces_dir

    def __enter__(self) -> ArtemisRunner:
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
