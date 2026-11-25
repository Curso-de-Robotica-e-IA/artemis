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


def test_artemis_runner_include_steps():
    fake_agent = MagicMock()
    fake_agent.init = AsyncMock()
    fake_agent.run_task = AsyncMock(return_value={"status": "completed", "explanation": "Done"})
    fake_agent.clean = AsyncMock()
    mock_steps = [
        {
            "step_number": 1,
            "action": "click",
            "command": "click",
            "coordinates": [500, 500],
            "pixel_coordinates": [540, 1200],
            "args": {"target": [500, 500]},
        }
    ]
    fake_agent.get_steps = MagicMock(return_value=mock_steps)

    with patch("artemis.runner.Agent", return_value=fake_agent):
        with ArtemisRunner(device_serial="test-device") as bot:
            res = bot.run("Test goal", include_steps=True)
            assert res["status"] == "completed"
            assert res["output"] == {"status": "completed", "explanation": "Done"}
            assert res["steps"] == mock_steps
            assert bot.last_steps == mock_steps
            assert bot.get_steps() == mock_steps


def test_artemis_runner_status_resolution_for_pro_and_failed_tasks():
    fake_agent = MagicMock()
    fake_agent.init = AsyncMock()
    fake_agent.clean = AsyncMock()
    fake_agent.get_steps = MagicMock(return_value=[])

    # Case 1: Pro task returns None, but agent._tasks[-1].status is 'failed'
    failed_task = MagicMock()
    failed_task.status = "failed"
    fake_agent._tasks = [failed_task]
    fake_agent.run_task = AsyncMock(return_value=None)

    with patch("artemis.runner.Agent", return_value=fake_agent):
        with ArtemisRunner(device_serial="test-device", profile="pro") as bot:
            res = bot.run("Test failing pro goal", return_steps=True)
            assert res["status"] == "failed"

            # Case 2: Pro task returns test_summary with task_status == 'blocked'
            fake_agent.run_task = AsyncMock(
                return_value={"test_summary": {"task_status": "blocked", "failed": 1}}
            )
            res_blocked = bot.run("Test blocked pro goal", return_steps=True)
            assert res_blocked["status"] == "failed"


def test_artemis_runner_extended_parameters_and_utilities(tmp_path):
    fake_agent = MagicMock()
    fake_agent.init = AsyncMock()
    fake_agent.run_task = AsyncMock(return_value="ok")
    fake_agent.install_apk = AsyncMock()
    fake_agent.get_screenshot = AsyncMock(return_value="fake_image")
    fake_agent.clean = AsyncMock()

    fake_builder = MagicMock()
    fake_builder.with_max_steps.return_value = fake_builder
    fake_builder.using_profile.return_value = fake_builder
    fake_builder.with_output_description.return_value = fake_builder
    fake_builder.with_name.return_value = fake_builder
    fake_builder.with_locked_app_package.return_value = fake_builder
    fake_builder.with_app_path.return_value = fake_builder
    fake_builder.with_trace_recording.return_value = fake_builder
    fake_builder.build.return_value = "built_request"
    fake_agent.new_task = MagicMock(return_value=fake_builder)

    with patch("artemis.runner.Agent", return_value=fake_agent) as agent_cls:
        with ArtemisRunner(
            device_serial="emulator-5554",
            profile="pro",
            traces_dir=tmp_path,
            session_id="sess-123",
            concurrency_mode="per_device",
            max_concurrency=2,
            api_key="test-key",
        ) as bot:
            agent_cls.assert_called_once_with(
                device_serial="emulator-5554",
                session_id="sess-123",
                concurrency_mode="per_device",
                max_concurrency=2,
            )
            fake_agent.init.assert_awaited_once_with(api_key="test-key")
            assert fake_agent._tmp_traces_dir == tmp_path.resolve()

            # Direct kwargs forwarding without max_steps
            bot.run(
                "Goal 1",
                locked_app_package="com.android.settings",
                app_path="app.apk",
                name="test_1",
            )
            fake_agent.run_task.assert_awaited_with(
                goal="Goal 1",
                profile="pro",
                output=None,
                locked_app_package="com.android.settings",
                app_path="app.apk",
                name="test_1",
            )

            # TaskRequestBuilder path when max_steps is provided
            bot.run(
                "Goal 2",
                output="summary",
                locked_app_package="com.android.settings",
                app_path="app.apk",
                name="test_2",
                max_steps=15,
            )
            fake_agent.new_task.assert_called_once_with("Goal 2")
            fake_builder.with_max_steps.assert_called_once_with(15)
            fake_agent.run_task.assert_awaited_with(request="built_request")

            # Utility methods
            bot.install_apk("app.apk")
            fake_agent.install_apk.assert_awaited_once_with("app.apk")
            assert bot.get_screenshot() == "fake_image"
            bot.stop()
            fake_agent.stop_current_task.assert_called()
