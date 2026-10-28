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
from typing import Any

from dotenv import load_dotenv

from artemis.config import settings
from artemis.sdk.agent import Agent

load_dotenv()

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

LLM_KEY_NAMES = (
    "GEMINI_API_KEY",
    "GOOGLE_API_KEY",
    "OPENAI_API_KEY",
    "ANTHROPIC_API_KEY",
    "OPEN_ROUTER_API_KEY",
    "XAI_API_KEY",
)


class ArtemisRunner:
    """Synchronous wrapper around the ARTEMIS agent: start once, run tasks, close."""

    def __init__(
        self,
        device_serial: str | None = None,
        profile: str = "flash",
        timeout: float | None | object = _UNSET,
        traces_dir: Path | str | None = None,
    ):
        """Create the runner (no connections until start() or context manager enter).

        Args:
            device_serial: ADB serial from `adb devices`; None picks the first device.
            profile: Default ARTEMIS profile, 'flash' (fast) or 'pro' (plans and checks).
            timeout: Default seconds per task; None or <= 0 waits without limit.
                If unset, defaults to the ARTEMIS_TASK_TIMEOUT env var or 600.0s.
            traces_dir: Custom destination directory for session traces and recordings.
        """
        self.device_serial = device_serial
        self.profile = profile
        if timeout is _UNSET:
            self.timeout = settings.ARTEMIS_TASK_TIMEOUT
        else:
            self.timeout = _parse_runner_timeout(timeout)
        self._custom_traces_dir = traces_dir is not None
        self._prev_traces_dir = os.environ.get("ARTEMIS_TRACES_DIR")
        if traces_dir:
            os.environ["ARTEMIS_TRACES_DIR"] = str(Path(traces_dir).resolve())
        self.agent: Agent | None = None
        self._loop = asyncio.new_event_loop()

    def start(self) -> None:
        """Connect to the device and initialize the agent."""
        if self.agent is not None:
            return
        agent = Agent(device_serial=self.device_serial)
        self._loop.run_until_complete(agent.init())
        self.agent = agent

    def run(
        self,
        goal: str,
        profile: str | None = None,
        timeout: float | None | object = _UNSET,
        output: Any = None,
    ) -> Any:
        """Run one task and return the outcome.

        Args:
            goal: What to do on the mobile device, in natural language.
            profile: Overrides the default profile for this task.
            timeout: Overrides the default timeout in seconds; None or <= 0 waits without limit.
            output: Optional structured output description or Pydantic model class.
        """
        if self._loop.is_closed():
            raise RuntimeError("ArtemisRunner is closed; create a new instance.")
        if self.agent is None:
            self.start()
        assert self.agent is not None
        task = self.agent.run_task(goal=goal, profile=profile or self.profile, output=output)
        limit = self.timeout if timeout is _UNSET else _parse_runner_timeout(timeout)
        return self._loop.run_until_complete(asyncio.wait_for(task, limit))

    def close(self) -> None:
        """Clean up the agent and release event loop resources."""
        if self._loop.is_closed():
            return
        if self.agent is not None:
            self._loop.run_until_complete(self.agent.clean())
            self.agent = None
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
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
