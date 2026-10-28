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

"""Unit tests for ArtemisRunner and top-level SDK exports."""

from __future__ import annotations

from unittest.mock import AsyncMock, MagicMock, patch

import pytest

import artemis
from artemis import (
    Agent,
    ArtemisRunner,
    Builders,
    ConcurrencyMode,
    StreamEvent,
    StreamEventType,
    Task,
    TaskResult,
)


def test_top_level_exports():
    assert ArtemisRunner is artemis.runner.ArtemisRunner
    assert Agent is artemis.sdk.agent.Agent
    assert Builders is artemis.sdk.builders.Builders
    assert ConcurrencyMode.GLOBAL == "global"
    assert StreamEventType.THOUGHT == "thought"
    task = Task(goal="Open Settings", profile="flash", device_serial="emulator-5554")
    assert task.device_id == "emulator-5554"
    res = TaskResult(trace_id="t-1", status="completed", turns=3, output="done")
    assert res.trace_id == "t-1"
    ev = StreamEvent(event_type=StreamEventType.ACTION, step_number=1, payload={"action": "tap"})
    assert ev.step_number == 1


def test_artemis_runner_lifecycle_and_execution(tmp_path, monkeypatch):
    fake_agent = MagicMock()
    fake_agent.init = AsyncMock()
    fake_agent.run_task = AsyncMock(return_value={"status": "success", "summary": "Wi-Fi ON"})
    fake_agent.clean = AsyncMock()

    with patch("artemis.runner.Agent", return_value=fake_agent) as agent_cls:
        with ArtemisRunner(
            device_serial="emulator-5554",
            profile="flash",
            timeout=30.0,
            traces_dir=tmp_path,
        ) as bot:
            agent_cls.assert_called_once_with(device_serial="emulator-5554")
            fake_agent.init.assert_awaited_once()
            outcome = bot.run("Check Wi-Fi status")
            assert outcome == {"status": "success", "summary": "Wi-Fi ON"}
            fake_agent.run_task.assert_awaited_once_with(
                goal="Check Wi-Fi status",
                profile="flash",
                output=None,
            )

        fake_agent.clean.assert_awaited_once()

        with pytest.raises(RuntimeError, match="ArtemisRunner is closed"):
            bot.run("Another goal")


def test_artemis_runner_timeout_configurations(monkeypatch):
    runner = ArtemisRunner(timeout=45.0)
    assert runner.timeout == 45.0

    runner_none = ArtemisRunner(timeout=None)
    assert runner_none.timeout is None

    runner_zero = ArtemisRunner(timeout=0)
    assert runner_zero.timeout is None

    monkeypatch.setenv("ARTEMIS_TASK_TIMEOUT", "1200")
    # Reload settings default or pass unset
    from artemis.runner import _parse_runner_timeout
    assert _parse_runner_timeout(1200) == 1200.0
    assert _parse_runner_timeout("unlimited") is None
    assert _parse_runner_timeout("0") is None
    assert _parse_runner_timeout(0) is None
