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

"""Run ARTEMIS tasks from any Python project, with no ARTEMIS server to start.

Copy this file into your project root, next to your .env. Usage:

    from artemis_runner import ArtemisRunner

    with ArtemisRunner(device_serial="emulator-5554") as artemis:
        print(artemis.run("Open Settings and check the Wi-Fi status"))
"""

import asyncio
import os
from pathlib import Path
from typing import Any

from dotenv import load_dotenv

# Folder with your .env and (optional) artemis.jsonc. Change it if this file is not in the root.
PROJECT_DIR = Path(__file__).resolve().parent

# ARTEMIS reads its settings when imported, so this project's values must be loaded first.
load_dotenv(PROJECT_DIR / ".env")
os.environ.setdefault("ARTEMIS_TRACES_DIR", str(PROJECT_DIR / "traces"))
if (PROJECT_DIR / "artemis.jsonc").is_file():
    os.environ.setdefault("ARTEMIS_ARTEMIS_JSONC", str(PROJECT_DIR / "artemis.jsonc"))

from artemis import Agent  # noqa: E402

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
        timeout: float | None = 300.0,
    ):
        """Create the runner (nothing connects until start() or `with`).

        Args:
            device_serial: ADB serial from `adb devices`; None picks the first device.
            profile: Default ARTEMIS profile, 'flash' (fast) or 'pro' (plans and checks).
            timeout: Default seconds per task; None waits without limit.
        """
        self.device_serial = device_serial
        self.profile = profile
        self.timeout = timeout
        self.agent: Agent | None = None
        # One loop for the whole run: the agent keeps async state between tasks.
        self._loop = asyncio.new_event_loop()

    def start(self) -> None:
        """Connect to the device and start the agent (no-op if already started)."""
        if self.agent is not None:
            return
        if not any(os.environ.get(name, "").strip() for name in LLM_KEY_NAMES):
            raise RuntimeError(f"No LLM API key found. Set one in {PROJECT_DIR / '.env'}.")
        agent = Agent(device_serial=self.device_serial)
        self._loop.run_until_complete(agent.init())
        self.agent = agent

    def run(
        self,
        goal: str,
        profile: str | None = None,
        timeout: float | None = None,
        output: Any = None,
    ) -> Any:
        """Run one task and return ARTEMIS's answer.

        Args:
            goal: What to do on the phone, in plain language.
            profile: Overrides the default profile for this task.
            timeout: Overrides the default timeout; the task is stopped when it expires.
            output: Optional answer format: a text description or a Pydantic model class.

        Raises:
            TimeoutError: The task took longer than the timeout.
        """
        if self._loop.is_closed():
            raise RuntimeError("ArtemisRunner is closed; create a new one.")
        if self.agent is None:
            self.start()
        task = self.agent.run_task(goal=goal, profile=profile or self.profile, output=output)
        limit = timeout if timeout is not None else self.timeout
        return self._loop.run_until_complete(asyncio.wait_for(task, limit))

    def close(self) -> None:
        """Stop the agent and release the event loop."""
        if self._loop.is_closed():
            return
        if self.agent is not None:
            self._loop.run_until_complete(self.agent.clean())
            self.agent = None
        # Cancel background work the agent left behind (e.g. its LLM connection pre-warm)
        pending = asyncio.all_tasks(self._loop)
        for task in pending:
            task.cancel()
        if pending:
            self._loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        self._loop.close()

    def __enter__(self) -> "ArtemisRunner":
        self.start()
        return self

    def __exit__(self, *exc: Any) -> None:
        self.close()
