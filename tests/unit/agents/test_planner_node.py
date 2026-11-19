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

from unittest.mock import Mock, patch

from artemis.agents.planner.planner import PlannerNode
from artemis.context import DevicePlatform, ArtemisContext
import pytest


class DummyState:
    def __init__(
        self,
        initial_goal,
        needs_replan=False,
        operator_raw_data=None,
        operator_replan_reason=None,
    ):
        self.initial_goal = initial_goal
        self.needs_replan = needs_replan
        self.operator_raw_data = operator_raw_data
        self.operator_replan_reason = operator_replan_reason


@pytest.fixture
def mock_context():
    ctx = Mock(spec=ArtemisContext)
    ctx.device = Mock()
    ctx.device.mobile_platform = DevicePlatform.ANDROID
    ctx.adb_client = Mock()
    ctx.ui_adb_client = Mock()
    mock_screen_data = Mock()
    mock_screen_data.base64 = "YmFzZTY0"
    mock_screen_data.elements = []
    mock_screen_data.width = 1080
    mock_screen_data.height = 2400
    ctx.ui_adb_client.get_screen_data.return_value = mock_screen_data
    ctx.data_engine = None

    return ctx


@pytest.mark.asyncio
async def test_planner_initial_plan(mock_context):
    state = DummyState(
        initial_goal="Test Goal",
        operator_raw_data={"screenshot_b64": "dummy_base64_string"},
    )
    node = PlannerNode(mock_context)

    mock_llm = Mock()
    mock_response = Mock()
    mock_response.content = "- [ ] Subgoal 1"
    mock_response.tool_calls = []

    async def mock_astream(*args, **kwargs):
        yield mock_response

    mock_llm.astream.side_effect = mock_astream
    mock_llm.bind_tools = Mock(return_value=mock_llm)

    with patch("artemis.agents.planner.planner.get_llm", return_value=mock_llm):
        await node(state)

        assert mock_llm.astream.called
        args, kwargs = mock_llm.astream.call_args
        messages = args[0]

        system_msg = messages[0].content
        assert "## Role" in system_msg

        human_msg_content = messages[1].content
        assert isinstance(human_msg_content, list)
        assert human_msg_content[0]["type"] == "text"
        assert "Goal: Test Goal" in human_msg_content[0]["text"]


def test_validate_plan_format_single_sourced_status_alphabet():
    """Regression: the Planner's format gate is single-sourced from
    plan_grammar — a legal in-progress '[/]' line must never be rejected."""
    from artemis.agents.planner.planner import validate_plan_format

    valid, err = validate_plan_format(
        "- [x] Done milestone\n- [/] In-progress milestone\n  - [ ] Sub\n- [!] Blocked\n"
    )
    assert valid is True and err == ""

    # Every grammar status char is accepted; unknown ones are still rejected.
    from artemis.utils.plan_grammar import STATUS_CHARS

    for c in STATUS_CHARS:
        ok, _ = validate_plan_format(f"- [{c}] Milestone\n")
        assert ok is True

    bad, message = validate_plan_format("- [z] Bogus status\n")
    assert bad is False
    assert "Invalid task status" in message

    empty, message = validate_plan_format("# just prose\n")
    assert empty is False
    assert "at least one subgoal" in message


def test_validate_plan_format_require_all_pending():
    """Initial plan validation rejects any non-pending status such as '- [x]'."""
    from artemis.agents.planner.planner import validate_plan_format

    valid, err = validate_plan_format(
        "- [ ] Open settings\n- [ ] [Loop] Check items\n  - [ ] Inspect item #1\n",
        require_all_pending=True,
    )
    assert valid is True and err == ""

    for bad_plan in (
        "- [x] Open settings\n- [x] Confirm\n",
        "- [ ] Open settings\n  - [x] Already done subtask\n",
        "- [/] In progress milestone\n",
        "- [!] Blocked milestone\n",
    ):
        ok, msg = validate_plan_format(bad_plan, require_all_pending=True)
        assert ok is False
        assert "- [ ]" in msg


@pytest.mark.asyncio
async def test_planner_stops_after_first_valid_task_plan_save(mock_context, tmp_path):
    """Planner exits immediately once a valid all-pending task_plan is saved,
    preventing a gratuitous second LLM turn that could overwrite items with [x]."""
    from langchain_core.messages import AIMessage

    mock_context.data_engine = Mock()
    mock_context.data_engine.base_dir = tmp_path
    state = DummyState(initial_goal="Open Settings")
    node = PlannerNode(mock_context)

    calls = 0

    async def fake_acomplete(_llm, _messages):
        nonlocal calls
        calls += 1
        if calls == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_note",
                        "args": {
                            "key": "task_plan",
                            "content": "- [ ] Open Settings\n- [ ] Verify screen\n",
                        },
                        "id": "call_1",
                    }
                ],
            )
        # Should never reach a second turn after a valid task_plan save!
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "save_note",
                    "args": {
                        "key": "task_plan",
                        "content": "- [x] Open Settings\n- [x] Verify screen\n",
                    },
                    "id": "call_2",
                }
            ],
        )

    mock_llm = Mock()
    mock_llm.bind_tools = Mock(return_value=mock_llm)

    with (
        patch("artemis.agents.planner.planner.get_llm", return_value=mock_llm),
        patch("artemis.agents.planner.planner.acomplete", side_effect=fake_acomplete),
    ):
        await node(state)

    assert calls == 1
    saved = (tmp_path / "notes" / "task_plan.md").read_text(encoding="utf-8")
    assert "- [ ] Open Settings" in saved
    assert "- [x]" not in saved


@pytest.mark.asyncio
async def test_planner_rejects_checked_initial_plan_and_retries(mock_context, tmp_path):
    """If the Planner writes '- [x]' during initial planning, post-validation
    rejects it and prompts a retry until all items are unchecked '- [ ]'."""
    from langchain_core.messages import AIMessage

    mock_context.data_engine = Mock()
    mock_context.data_engine.base_dir = tmp_path
    state = DummyState(initial_goal="Open Settings")
    node = PlannerNode(mock_context)

    seen_messages = []

    async def fake_acomplete(_llm, messages):
        seen_messages.append(list(messages))
        if len(seen_messages) == 1:
            return AIMessage(
                content="",
                tool_calls=[
                    {
                        "name": "save_note",
                        "args": {
                            "key": "task_plan",
                            "content": "- [x] Open Settings\n",
                        },
                        "id": "call_bad",
                    }
                ],
            )
        return AIMessage(
            content="",
            tool_calls=[
                {
                    "name": "save_note",
                    "args": {
                        "key": "task_plan",
                        "content": "- [ ] Open Settings\n",
                    },
                    "id": "call_good",
                }
            ],
        )

    mock_llm = Mock()
    mock_llm.bind_tools = Mock(return_value=mock_llm)

    with (
        patch("artemis.agents.planner.planner.get_llm", return_value=mock_llm),
        patch("artemis.agents.planner.planner.acomplete", side_effect=fake_acomplete),
    ):
        await node(state)

    assert len(seen_messages) == 2
    system_prompt = seen_messages[0][0].content
    assert "STRICT Initial Plan Status Rule" in system_prompt
    assert "MUST NEVER" in system_prompt
    assert "end your turn immediately" in system_prompt
    # Second attempt received the validation failure feedback
    assert any("strictly unchecked" in str(getattr(m, "content", "")) for m in seen_messages[1])
    saved = (tmp_path / "notes" / "task_plan.md").read_text(encoding="utf-8")
    assert saved.strip() == "- [ ] Open Settings"
