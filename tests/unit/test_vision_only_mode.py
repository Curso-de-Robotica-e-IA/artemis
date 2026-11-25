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

"""Unit tests for VISION_ONLY_MODE ablation feature."""

import json
import os
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch
import pytest

from langchain_core.messages import AIMessage

from artemis.config.constants import ENV_VISION_ONLY_MODE, VISION_ONLY_NOTICE
from artemis.config.settings import is_vision_only_mode, settings
from artemis.agents.operator.prompts import (
    ObservationPromptComponent,
    PromptBuilder,
    render_execution_incident,
)
from artemis.agents.flash.runner import FlashRunner
from artemis.memory.transcript import TranscriptLedger
from artemis.graph.state import State


def test_is_vision_only_mode_toggle(monkeypatch):
    """Verify is_vision_only_mode respects environment variables and default."""
    monkeypatch.delenv(ENV_VISION_ONLY_MODE, raising=False)
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", False)
    assert not is_vision_only_mode()

    for val in ("true", "True", "1", "yes", "YES", "on"):
        monkeypatch.setenv(ENV_VISION_ONLY_MODE, val)
        assert is_vision_only_mode() is True

    for val in ("false", "False", "0", "no", ""):
        monkeypatch.setenv(ENV_VISION_ONLY_MODE, val)
        assert is_vision_only_mode() is False


@pytest.mark.asyncio
async def test_observation_prompt_component_vision_only(monkeypatch):
    """Verify ObservationPromptComponent omits XML and shows neutral text in vision-only mode."""
    component = ObservationPromptComponent()
    state = MagicMock()
    ctx = MagicMock()
    minimal_list = "[1] button 'Submit' [100, 200][300, 250]"

    # 1. Normal mode (VISION_ONLY_MODE = false)
    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "false")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", False)
    builder_normal = PromptBuilder()
    await component(
        builder_normal,
        state,
        ctx,
        latest_screenshot_b64="fake_b64",
        minimal_list=minimal_list,
    )
    normal_parts = builder_normal.human_parts
    normal_text = " ".join([p if isinstance(p, str) else str(p) for p in normal_parts])
    assert minimal_list in normal_text
    assert "Vision-only mode enabled" not in normal_text

    # 2. Vision-only mode via ENV (VISION_ONLY_MODE = true)
    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    builder_vision = PromptBuilder()
    await component(
        builder_vision,
        state,
        ctx,
        latest_screenshot_b64="fake_b64",
        minimal_list=minimal_list,
    )
    vision_parts = builder_vision.human_parts
    vision_text = " ".join([p if isinstance(p, str) else str(p) for p in vision_parts])
    assert minimal_list not in vision_text
    assert "--- Visible UI Elements ---" not in vision_text
    assert "Vision-only mode enabled: operate using visual coordinates [0-1000] only." in vision_text

    # 3. Vision-only mode via settings.VISION_ONLY_MODE directly
    monkeypatch.delenv(ENV_VISION_ONLY_MODE, raising=False)
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", True)
    builder_setting = PromptBuilder()
    await component(
        builder_setting,
        state,
        ctx,
        latest_screenshot_b64="fake_b64",
        minimal_list=minimal_list,
    )
    setting_parts = builder_setting.human_parts
    setting_text = " ".join([p if isinstance(p, str) else str(p) for p in setting_parts])
    assert minimal_list not in setting_text
    assert "--- Visible UI Elements ---" not in setting_text
    assert "Vision-only mode enabled: operate using visual coordinates [0-1000] only." in setting_text


@pytest.mark.asyncio
async def test_observation_prompt_component_fallbacks(monkeypatch):
    """Verify fallback to state.latest_ui_hierarchy and operator_raw_data['xml_hierarchy'] when minimal_list is None."""
    monkeypatch.delenv(ENV_VISION_ONLY_MODE, raising=False)
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", False)

    component = ObservationPromptComponent()
    ctx = MagicMock()

    sample_fused = [
        {
            "class": "android.widget.Button",
            "text": "Save",
            "bounds": "[10,20][100,80]",
        }
    ]

    # Fallback 1: state.latest_ui_hierarchy
    state1 = MagicMock(spec=State)
    state1.latest_ui_hierarchy = sample_fused
    state1.operator_raw_data = {"width": 1080, "height": 2400}
    builder1 = PromptBuilder()
    await component(builder1, state1, ctx, latest_screenshot_b64="fake_b64", minimal_list=None)
    text1 = " ".join([p if isinstance(p, str) else str(p) for p in builder1.human_parts])
    assert "Save" in text1
    assert "Vision-only mode enabled" not in text1

    # Fallback 2: state.operator_raw_data["xml_hierarchy"]
    state2 = MagicMock(spec=State)
    state2.latest_ui_hierarchy = None
    state2.operator_raw_data = {
        "xml_hierarchy": sample_fused,
        "width": 1080,
        "height": 2400,
    }
    builder2 = PromptBuilder()
    await component(builder2, state2, ctx, latest_screenshot_b64="fake_b64", minimal_list=None)
    text2 = " ".join([p if isinstance(p, str) else str(p) for p in builder2.human_parts])
    assert "Save" in text2
    assert "Vision-only mode enabled" not in text2


def test_flash_runner_observation_tail_vision_only(monkeypatch):
    """Verify FlashRunner._build_tail omits XML and displays neutral notice."""
    ledger = MagicMock(spec=TranscriptLedger)
    ledger.elapsed_label.return_value = "T+00:10"

    xml_list = "[1] button 'Play' [50, 50][150, 150]"
    fake_img = b"fake_screenshot_bytes"
    runner = FlashRunner.__new__(FlashRunner)

    # 1. Normal mode
    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "false")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", False)
    msg_normal = runner._build_tail(
        ledger=ledger,
        turns=1,
        img_bytes=fake_img,
        xml_list=xml_list,
    )
    normal_content = msg_normal.content
    texts_normal = [b["text"] for b in normal_content if b.get("type") == "text"]
    full_normal = "\n".join(texts_normal)
    assert xml_list in full_normal
    assert "Vision-only mode enabled" not in full_normal

    # 2. Vision-only mode via ENV
    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    msg_vision = runner._build_tail(
        ledger=ledger,
        turns=1,
        img_bytes=fake_img,
        xml_list=xml_list,
    )
    vision_content = msg_vision.content
    texts_vision = [b["text"] for b in vision_content if b.get("type") == "text"]
    full_vision = "\n".join(texts_vision)
    assert xml_list not in full_vision
    assert "--- Visible UI Elements ---" not in full_vision
    assert "Vision-only mode enabled: operate using visual coordinates [0-1000] only." in full_vision

    # 3. Vision-only mode via settings.VISION_ONLY_MODE directly
    monkeypatch.delenv(ENV_VISION_ONLY_MODE, raising=False)
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", True)
    msg_setting = runner._build_tail(
        ledger=ledger,
        turns=1,
        img_bytes=fake_img,
        xml_list=xml_list,
    )
    setting_content = msg_setting.content
    texts_setting = [b["text"] for b in setting_content if b.get("type") == "text"]
    full_setting = "\n".join(texts_setting)
    assert xml_list not in full_setting
    assert "--- Visible UI Elements ---" not in full_setting
    assert "Vision-only mode enabled: operate using visual coordinates [0-1000] only." in full_setting


def test_flash_runner_observation_tail_fallbacks(monkeypatch):
    """Verify FlashRunner._build_tail fallback to state.latest_ui_hierarchy and operator_raw_data."""
    monkeypatch.delenv(ENV_VISION_ONLY_MODE, raising=False)
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", False)

    ledger = MagicMock(spec=TranscriptLedger)
    ledger.elapsed_label.return_value = "T+00:10"
    runner = FlashRunner.__new__(FlashRunner)

    sample_fused = [
        {
            "class": "android.widget.Button",
            "text": "Confirm",
            "bounds": "[10,20][100,80]",
        }
    ]

    # Fallback 1: state.latest_ui_hierarchy
    state1 = MagicMock(spec=State)
    state1.latest_ui_hierarchy = sample_fused
    state1.operator_raw_data = {"width": 1080, "height": 2400}

    msg1 = runner._build_tail(ledger, 1, None, None, state=state1)
    texts1 = "\n".join([b["text"] for b in msg1.content if b.get("type") == "text"])
    assert "Confirm" in texts1

    # Fallback 2: state.operator_raw_data["xml_hierarchy"]
    state2 = MagicMock(spec=State)
    state2.latest_ui_hierarchy = None
    state2.operator_raw_data = {
        "xml_hierarchy": sample_fused,
        "width": 1080,
        "height": 2400,
    }

    msg2 = runner._build_tail(ledger, 1, None, None, state=state2)
    texts2 = "\n".join([b["text"] for b in msg2.content if b.get("type") == "text"])
    assert "Confirm" in texts2


@pytest.mark.asyncio
async def test_explorer_prompts_and_setup_omit_xml_in_vision_only_mode(monkeypatch, tmp_path):
    """Verify Explorer omits minimal_list/XML across universal, native, and run_setup."""
    from google.genai import types

    from artemis.agents.explorer.explorer import Explorer
    from artemis.context import ArtemisContext

    shot = tmp_path / "screen.jpg"
    shot.write_bytes(b"fake_jpeg_bytes")

    ctx = MagicMock(spec=ArtemisContext)
    ctx.device = MagicMock(device_width=1080, device_height=2400)
    ctx.data_engine = MagicMock(base_dir=tmp_path)
    ctx.llm_config = MagicMock()
    ctx.llm_config.explorer = MagicMock(model="claude-3-7-sonnet", temperature=0.1)
    ctx.agent_config = MagicMock(denylisted_tools={})

    state = MagicMock(spec=State)
    state.latest_screenshot = str(shot)
    state.latest_ui_hierarchy = [
        {"text": "Settings", "bounds": "[100,200][300,300]", "class": "android.widget.Button"}
    ]
    state.operator_raw_data = {}

    minimal_list = "[1] Text: 'Settings' | Bounds: [100, 200][300, 300]"

    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", True)

    with patch.object(settings, "GOOGLE_API_KEY", None):
        explorer = Explorer(ctx)

        # 1. Universal message assembly omits minimal_list
        univ_msgs = explorer._build_universal_messages(
            query="Settings",
            context_feedback="",
            screenshot_path=str(shot),
            minimal_list=minimal_list,
            prompt_template="Explorer system prompt",
        )
        univ_text = " ".join(
            b["text"] for b in univ_msgs[1].content if isinstance(b, dict) and b.get("type") == "text"
        )
        assert minimal_list not in univ_text
        assert "Initial marked UI elements list" not in univ_text
        assert VISION_ONLY_NOTICE in univ_text

        # 2. Native message assembly omits minimal_list
        native_contents = explorer._build_initial_contents(
            query="Settings",
            context_feedback="",
            minimal_list=minimal_list,
            initial_part=types.Part.from_bytes(data=b"img", mime_type="image/jpeg"),
        )
        native_text = native_contents[0].parts[0].text
        assert minimal_list not in native_text
        assert "Initial marked UI elements list" not in native_text
        assert VISION_ONLY_NOTICE in native_text

        # 3. Screen setup skips XML hierarchy & dot annotation
        fused = await explorer._build_fused_hierarchy(None, state, str(shot))
        assert fused == []
        out_list, out_img = explorer._prepare_initial_annotation(
            [{"text": "Settings", "bounds": "[100,200][300,300]"}],
            str(shot),
            minimal_list,
            "img_hash",
        )
        assert out_list == ""
        assert out_img == str(shot)

        # 4. Full Explorer.run skips XML prepass and executes universal VLM call cleanly
        mock_llm = MagicMock()
        mock_bound = MagicMock()
        captured_messages = []

        async def fake_ainvoke(msgs):
            captured_messages.extend(msgs)
            return AIMessage(
                content="Located visually.",
                tool_calls=[
                    {
                        "name": "submit_answer",
                        "args": {
                            "candidates": [
                                {"label": "V1", "coords": [200, 250], "description": "Settings"}
                            ],
                            "fallback_message": "",
                        },
                        "id": "call_1",
                    }
                ],
            )

        mock_bound.ainvoke = AsyncMock(side_effect=fake_ainvoke)
        mock_llm.bind_tools.return_value = mock_bound

        with (
            patch("artemis.agents.explorer.universal_runner.get_llm", return_value=mock_llm),
            patch("artemis.agents.explorer.run_setup.StorageManager", side_effect=RuntimeError("skip")),
        ):
            raw_res = await explorer.run(
                query="Settings",
                context_feedback="",
                screenshot_path=str(shot),
                state=state,
                minimal_list=minimal_list,
                version="pro",
            )

        parsed = json.loads(raw_res)
        assert parsed["candidates"][0]["coords"] == [200, 250]
        assert len(captured_messages) == 2
        sent_text = " ".join(
            b["text"]
            for b in captured_messages[1].content
            if isinstance(b, dict) and b.get("type") == "text"
        )
        assert "Settings" in sent_text  # query is preserved
        assert "[1] Text: 'Settings'" not in sent_text
        assert "[100, 200][300, 300]" not in sent_text
        assert VISION_ONLY_NOTICE in sent_text


@pytest.mark.asyncio
async def test_diagnoser_and_get_ui_hierarchy_omit_xml_in_vision_only_mode(monkeypatch):
    """Verify Diagnoser prompt and get_ui_hierarchy tool blindfold XML in vision-only mode."""
    from artemis.agents.diagnoser.diagnoser import Diagnoser
    from artemis.context import ArtemisContext
    from artemis.tools.mobile.read_hierarchy import get_ui_hierarchy

    ctx = MagicMock(spec=ArtemisContext)
    ctx.llm_config = MagicMock()
    ctx.data_engine = MagicMock(base_dir="/tmp")
    ctx.data_engine.get_agent_friendly_steps.return_value = []
    ctx.execution_setup = MagicMock(video_recording_tools_enabled=False)
    ctx.adb_client = None
    ctx.ui_adb_client = MagicMock()
    ctx.ui_adb_client.get_hierarchy.return_value = "<hierarchy><node text='SecretXML'/></hierarchy>"

    state = MagicMock(spec=State)
    state.initial_goal = "Open Settings"
    state.latest_screenshot = None
    state.latest_ui_hierarchy = [
        {"text": "SecretButton", "bounds": "[10,20][100,80]", "class": "android.widget.Button"}
    ]

    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", True)

    # 1. get_ui_hierarchy tool stays callable and returns VISION_ONLY_NOTICE without touching XML
    hier_out = await get_ui_hierarchy.execute(ctx=ctx)
    assert hier_out == VISION_ONLY_NOTICE
    ctx.ui_adb_client.get_hierarchy.assert_not_called()

    # 2. Diagnoser.run omits Visible UI Elements and passes VISION_ONLY_NOTICE to the LLM
    mock_llm = MagicMock()
    mock_llm.bind_tools.return_value = mock_llm
    captured_msgs = []

    async def mock_astream(msgs, *args, **kwargs):
        captured_msgs.extend(msgs)
        yield AIMessage(content="Visual diagnosis only.")

    mock_llm.astream.side_effect = mock_astream

    with (
        patch("artemis.agents.diagnoser.diagnoser.get_llm", return_value=mock_llm),
        patch("artemis.agents.diagnoser.diagnoser.get_analyze_logs_tool", return_value=MagicMock(name="analyze_logs")),
    ):
        agent = Diagnoser(ctx)
        result = await agent.run("Why did it fail?", state)

    assert result == "Visual diagnosis only."
    human_blocks = captured_msgs[1].content
    human_text = " ".join(b["text"] for b in human_blocks if isinstance(b, dict) and b.get("type") == "text")
    assert "SecretButton" not in human_text
    assert "--- Visible UI Elements ---" not in human_text
    assert VISION_ONLY_NOTICE in human_text


@pytest.mark.asyncio
async def test_checker_final_check_omits_xml_in_vision_only_mode(monkeypatch):
    """Verify CheckerNode.run_final_check omits Final Screen Elements in vision-only mode."""
    from artemis.agents.checker.checker import CheckReport, _capture_final_screen, run_final_check
    from artemis.context import ArtemisContext, ExecutionSetup
    from artemis.utils.plan_grammar import CheckItem

    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = ExecutionSetup(disable_device_probes=True)
    ctx.data_engine = MagicMock(base_dir="unused")
    ctx.data_engine.get_agent_friendly_steps.return_value = []

    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", True)

    # 1. _capture_final_screen still captures screenshot but skips XML formatting
    fake_device_data = SimpleNamespace(
        base64="b64shot",
        elements=[{"text": "HiddenNode", "bounds": "[0,0][100,100]"}],
        width=1080,
        height=2400,
    )
    mock_controller = MagicMock()
    mock_controller.get_screen_data = AsyncMock(return_value=fake_device_data)
    with patch(
        "artemis.controllers.unified_controller.UnifiedMobileController",
        return_value=mock_controller,
    ):
        b64_out, elements_out = await _capture_final_screen(ctx)
    assert b64_out == "b64shot"
    assert elements_out == VISION_ONLY_NOTICE

    # 2. run_final_check omits # Final Screen Elements even if minimal_list was returned
    structured = MagicMock()
    structured.ainvoke = AsyncMock(return_value=CheckReport(verdicts=[]))
    llm = MagicMock()
    llm.with_structured_output.return_value = structured
    done_resp = AIMessage(content="All good.")

    captured_calls = []

    async def fake_acomplete(active_llm, msgs):
        captured_calls.append(msgs)
        return done_resp

    with (
        patch("artemis.agents.checker.checker.get_llm", return_value=llm),
        patch("artemis.agents.checker.checker.acomplete", side_effect=fake_acomplete),
        patch(
            "artemis.agents.checker.checker._capture_final_screen",
            new=AsyncMock(return_value=("b64shot", "[1] Text: 'HiddenElement'")),
        ),
    ):
        await run_final_check(
            ctx,
            goal="Verify setting",
            plan_text="- [x] Done",
            ledger=[],
            check_items=[CheckItem(kind="assert", when="at_end", text="no crash", parent_key=None)],
        )

    human_msg = captured_calls[0][1]
    human_text = " ".join(
        b["text"] for b in human_msg.content if isinstance(b, dict) and b.get("type") == "text"
    )
    assert "HiddenElement" not in human_text
    assert "# Final Screen Elements" not in human_text
    assert VISION_ONLY_NOTICE in human_text


def test_search_history_omits_ui_tree_xml_in_vision_only_mode(monkeypatch, tmp_path):
    """Verify search_history ignores ui_tree XML/bounds while remaining functional."""
    import io
    from PIL import Image
    from artemis.context import ArtemisContext
    from artemis.data_engine.engine import DataEngine
    from artemis.tools.history.search import search_history_text

    buf = io.BytesIO()
    Image.new("RGB", (64, 128), "red").save(buf, format="JPEG")
    jpeg_bytes = buf.getvalue()

    ctx = MagicMock(spec=ArtemisContext)
    ctx.execution_setup = SimpleNamespace(traces_path=str(tmp_path))
    ctx.device = None
    engine = DataEngine(ctx)
    engine.start_session("vision-only search test")
    engine.record_step(
        pre_screenshot_bytes=jpeg_bytes,
        summary="Opened network settings.",
        action_taken={
            "action": "click",
            "coordinates": [500, 500],
            "target_description": "Wi-Fi row",
            "target_bounds": [10, 20, 300, 400],
            "target_resource_id": "com.android.settings:id/xmlonly_id",
        },
        ui_tree=[
            {
                "text": "XMLSecretLabel",
                "content-desc": "XMLContentDesc",
                "resource-id": "com.android.settings:id/secret_toolbar",
                "bounds": "[0,0][100,100]",
                "class": "android.widget.ImageButton",
            }
        ],
    )
    for t in list(engine._pending_threads):
        t.join()

    cfg = SimpleNamespace(enabled=True, max_results=5, max_text_tokens=2000, screen_scan_steps=150)

    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", True)

    # Querying XML-only text/resource-id/class must NOT match ui_tree
    xml_out = search_history_text(engine, query="XMLSecretLabel", recall_config=cfg)
    assert "No matches" in xml_out
    assert "XMLSecretLabel" not in xml_out
    assert "secret_toolbar" not in xml_out
    assert VISION_ONLY_NOTICE in xml_out

    res_out = search_history_text(engine, query="xmlonly_id", recall_config=cfg)
    assert "No matches" in res_out
    assert VISION_ONLY_NOTICE in res_out

    # Querying summary / visual action description still succeeds cleanly
    summary_out = search_history_text(engine, query="network settings", recall_config=cfg)
    assert "Step 1" in summary_out
    assert "Opened network settings." in summary_out
    assert VISION_ONLY_NOTICE in summary_out


def test_execution_incident_and_tool_result_omit_xml_bounds_in_vision_only_mode(monkeypatch):
    """Verify XML bounds and occupants are hidden in execution incidents and tool messages."""
    from artemis.agents.validator.categories import ValidationErrorCategory
    from artemis.agents.validator.incidents import KIND_SAFETY_NET, open_incident
    from artemis.agents.validator.tool_declarations import ToolExecutionResult

    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", True)

    shifted = open_incident(
        previous=None,
        kind=KIND_SAFETY_NET,
        category=ValidationErrorCategory.TARGET_SHIFTED,
        reason="Target moved",
        action_item={"action": "tap", "coordinates": [100, 200], "normalized_coordinates": [100, 100]},
        action_description="Tapped button",
        step_number=2,
        evidence={"new_location": [120, 130], "new_bounds": [10, 20, 30, 40]},
    )
    shifted_text = render_execution_incident(shifted, [])
    assert "[10, 20, 30, 40]" not in shifted_text
    assert "bounds" not in shifted_text
    assert VISION_ONLY_NOTICE in shifted_text

    occupied = open_incident(
        previous=None,
        kind=KIND_SAFETY_NET,
        category=ValidationErrorCategory.TARGET_OCCUPIED,
        reason="Target covered",
        action_item={"action": "tap", "coordinates": [100, 200], "normalized_coordinates": [100, 100]},
        action_description="Tapped button",
        step_number=3,
        evidence={"occupant": "'SecretDialog' (com.app:id/dlg)"},
    )
    occupied_text = render_execution_incident(occupied, [])
    assert "SecretDialog" not in occupied_text
    assert "com.app:id/dlg" not in occupied_text
    assert VISION_ONLY_NOTICE in occupied_text

    tool_msg = ToolExecutionResult(
        tool_call_id="tc1",
        tool_name="click",
        status="success",
        text_summary="Tapped at [100, 100]",
        ui_elements_text="[1] Text: 'XMLLeak'",
    ).to_langchain_tool_message()
    texts = [b["text"] for b in tool_msg.content if b.get("type") == "text"]
    joined = "\n".join(texts)
    assert "XMLLeak" not in joined
    assert "--- UI Element List ---" not in joined
    assert VISION_ONLY_NOTICE in joined


@pytest.mark.asyncio
async def test_non_invasive_disables_manage_app_and_enforces_human_visual_hierarchy(monkeypatch):
    """Verify manage_app and ADB shortcuts are disabled and human visual hierarchy is taught when VISION_ONLY_MODE=true or ARTEMIS_ACTUATOR_BACKEND='http'."""
    from jinja2 import Template
    from pathlib import Path
    from artemis.agents.operator.operator import OperatorNode
    from artemis.agents.operator.prompts import (
        apply_operator_prompt_contract,
        load_operator_prompts,
        resolve_operator_prompt_tools,
    )
    from artemis.config.settings import is_non_invasive_mode
    from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
    from artemis.mcp.action_manifest import validate_actuator
    from artemis.mcp.actuators import AdbActuator, HttpTouchActuator
    from artemis.platform import platform

    ctx = ArtemisContext(
        device=DeviceContext(
            host_platform=platform.os_type.name,
            mobile_platform=DevicePlatform.ANDROID,
            device_id="test-dev",
            device_width=1080,
            device_height=2400,
        )
    )

    for mode_name, env_vision, backend in (
        ("vision_only", "true", "adb"),
        ("http_touch", "false", "http"),
    ):
        monkeypatch.setenv(ENV_VISION_ONLY_MODE, env_vision)
        monkeypatch.setattr(settings, "VISION_ONLY_MODE", env_vision == "true")
        monkeypatch.setattr(settings, "ARTEMIS_ACTUATOR_BACKEND", backend)

        assert is_non_invasive_mode() is True, f"Failed for {mode_name}"

        actuator = (
            HttpTouchActuator(ctx)
            if backend == "http"
            else AdbActuator(ctx, controller=MagicMock())
        )
        ctx.actuator = actuator

        # 1. validate_actuator passes without ActuatorContractError
        validate_actuator(actuator)
        assert "manage_app" not in actuator.capabilities()
        assert "input_text" not in actuator.capabilities()
        assert "open_link" not in actuator.capabilities()
        assert "wait_for_text" not in actuator.capabilities()
        assert "focus_and_clear_text" not in actuator.capabilities()
        assert "erase_one_char" not in actuator.capabilities()
        if backend == "http":
            assert "press_key" in actuator.capabilities()
        else:
            assert "press_key" not in actuator.capabilities()

        # Calling excluded actions directly returns UNSUPPORTED without invoking ADB
        for res in (
            await actuator.manage_app("launch", "Settings"),
            await actuator.input_text("Settings"),
            await actuator.open_link("https://example.com"),
            await actuator.wait_for_text("Settings"),
            await actuator.focus_and_clear_text(400, 300),
            await actuator.erase_one_char(),
        ):
            assert not res.ok
            assert res.code.value == "UNSUPPORTED"

        if backend != "http":
            res_key = await actuator.press_key("BACK")
            assert not res_key.ok
            assert res_key.code.value == "UNSUPPORTED"

        assert await actuator.get_ui_elements() == []

        # 2. Operator tools & prompt exclude manage_app, input_text, and run_adb_command, and teach physical navigation/typing
        op_tools = resolve_operator_prompt_tools(ctx)
        assert "manage_app" not in op_tools
        assert "input_text" not in op_tools
        assert "run_adb_command" not in op_tools
        assert "manage_task" not in op_tools

        op_node = OperatorNode(ctx)
        assert "manage_app" not in op_node._available_device_actions()
        assert "input_text" not in op_node._available_device_actions()

        op_prompt = apply_operator_prompt_contract(
            load_operator_prompts()["main_template"], available_tools=op_tools
        )
        assert "manage_app" not in op_prompt
        assert "input_text" not in op_prompt
        assert "run_adb_command" not in op_prompt
        assert "Visual App Discovery & Launching (Human Navigation Hierarchy)" in op_prompt
        assert "Search Bar / Magnifying Glass & Virtual Keyboard Typing" in op_prompt
        assert "Physical Virtual Keyboard Typing" in op_prompt

        # 3. FlashRunner tools & prompt exclude manage_app and input_text, and teach physical navigation/typing
        with patch("artemis.controllers.unified_controller.get_driver"):
            runner = FlashRunner(ctx, goal="Open Calculator")
            flash_tools = runner._get_tools()
            flash_tool_names = [t.name for t in flash_tools]
            assert "manage_app" not in flash_tool_names
            assert "input_text" not in flash_tool_names
            flash_prompt = runner._render_system_prompt(flash_tools)
            assert "manage_app" not in flash_prompt
            assert "input_text" not in flash_prompt
            assert "Visual App Discovery & Launching (Human Navigation Hierarchy)" in flash_prompt
            assert "Search Bar / Magnifying Glass & Virtual Keyboard Typing" in flash_prompt
            assert "Physical Virtual Keyboard Typing" in flash_prompt

        # 4. Planner system prompt omits App Management / ADB shell and teaches physical navigation & virtual keyboard typing
        planner_data = json.loads(
            (Path("artemis/agents/planner/planner.json")).read_text(encoding="utf-8")
        )
        cap_block = Template(planner_data["blocks"]["capabilities_operator"]).render(
            non_invasive_mode=is_non_invasive_mode()
        )
        assert "App Management" not in cap_block
        assert "Executing arbitrary ADB shell commands" not in cap_block
        assert "Visual App Discovery & Launching (Human Navigation Hierarchy)" in cap_block
        assert "Physical Virtual Keyboard Typing" in cap_block


@pytest.mark.asyncio
async def test_non_invasive_blocks_direct_adb_xml_subagents_and_tools(monkeypatch):
    """Issue #153: Verify zero direct ADB / XML / Accessibility calls leak in non-invasive mode."""
    from PIL import Image

    from artemis.agents.checker.checker import _execute_probe, build_checker_tools, probes_enabled
    from artemis.agents.diagnoser.diagnoser import Diagnoser
    from artemis.agents.validator.categories import ValidationErrorCategory
    from artemis.agents.validator.execution_loop import _run_precondition_gate
    from artemis.config import VISION_ONLY_NOTICE
    from artemis.config.constants import ENV_VISION_ONLY_MODE
    from artemis.config.settings import settings
    from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
    from artemis.core.tool_failure import is_tool_failure
    from artemis.drivers.android.adb_driver import AndroidAdbDriver
    from artemis.graph.state import State
    from artemis.mcp.action_server import build_action_server
    from artemis.mcp.action_session import ActionSession
    from artemis.mcp.actuators.adb import AdbActuator
    from artemis.sdk.agent import Agent
    from artemis.tools.command_tool import (
        ManageTaskTool,
        RunAdbCommandTool,
        RunShortAdbCommandTool,
    )
    from artemis.tools.mobile.launch_app import LaunchAppTool
    from artemis.tools.mobile.read_hierarchy import GetUiHierarchyTool

    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", True)
    monkeypatch.setattr(settings, "ARTEMIS_ACTUATOR_BACKEND", "adb")

    ctx = ArtemisContext(
        device=DeviceContext(
            host_platform="LINUX",
            mobile_platform=DevicePlatform.ANDROID,
            device_id="non-invasive-device",
            device_width=1080,
            device_height=2400,
        )
    )

    # 1. AndroidAdbDriver.get_screen_data skips ui_adb_client and app_current, using screenshot() only
    img = Image.new("RGB", (64, 128), color=(10, 20, 30))
    mock_device = MagicMock()
    mock_device.screenshot.return_value = img
    mock_device.app_current.side_effect = AssertionError("app_current must not be called")
    mock_adb_client = MagicMock()
    mock_adb_client.device.return_value = mock_device
    mock_ui_client = MagicMock()
    mock_ui_client.get_screen_data.side_effect = AssertionError("ui_adb_client must not be called")
    mock_ui_client.get_hierarchy.side_effect = AssertionError("get_hierarchy must not be called")

    driver = AndroidAdbDriver(
        device_id="dev-1",
        adb_client=mock_adb_client,
        ui_adb_client=mock_ui_client,
    )
    screen = await driver.get_screen_data(skip_settling=True)
    assert screen.ui_elements == []
    assert screen.ui_hierarchy_xml is None
    assert screen.package is None
    assert screen.activity is None
    assert screen.width == 64
    assert screen.height == 128
    mock_device.screenshot.assert_called_once()
    mock_device.app_current.assert_not_called()
    mock_ui_client.get_screen_data.assert_not_called()
    mock_ui_client.get_hierarchy.assert_not_called()

    # 2. Checker disables probe_device and blocks _execute_probe
    assert probes_enabled(ctx) is False
    checker_tools = build_checker_tools(ctx, "final")
    assert all(t.name != "probe_device" for t in checker_tools)
    probe_res = await _execute_probe(ctx, ["dumpsys", "battery"])
    assert is_tool_failure(probe_res)

    # 3. Diagnoser strips run_adb_command, analyze_logs, and get_ui_hierarchy
    diag_node = Diagnoser(ctx)
    diag_node.is_device_online = True
    diag_tool_names = {t.name for t in diag_node._build_traced_tools()}
    assert "run_adb_command" not in diag_tool_names
    assert "analyze_logs" not in diag_tool_names
    assert "get_ui_hierarchy" not in diag_tool_names

    # 4. Direct tools refuse execution in non-invasive mode
    assert is_tool_failure(await RunAdbCommandTool().execute(ctx=ctx, CommandLine="id"))
    assert is_tool_failure(await ManageTaskTool().execute(ctx=ctx, Action="list"))
    assert is_tool_failure(await RunShortAdbCommandTool().execute(ctx=ctx, CommandLine="id"))
    launch_out = await LaunchAppTool().execute(ctx=ctx, app_name="Settings")
    assert "disabled in non-invasive mode" in str(launch_out)
    hier_out = await GetUiHierarchyTool().execute(ctx=ctx)
    assert hier_out == VISION_ONLY_NOTICE

    # 5. MCP action_server get_ui_hierarchy returns [] without calling actuator
    mock_controller = MagicMock()
    mock_controller.get_ui_elements = AsyncMock(
        side_effect=AssertionError("controller.get_ui_elements must not be called")
    )
    actuator = AdbActuator(ctx, controller=mock_controller)
    server = build_action_server(actuator)
    session = await ActionSession(server).start()
    try:
        assert await session.ui_hierarchy() == []
    finally:
        await session.aclose()

    # 6. Validator precondition gate skips XML validation and goes straight to pixel check
    fake_node = MagicMock()
    fake_node._validate_action_precondition = AsyncMock(
        side_effect=AssertionError("XML precondition check must be skipped")
    )
    fake_node._validate_action_precondition_pixel = AsyncMock(
        return_value=(True, ValidationErrorCategory.NONE, "")
    )
    ok, cat, _ = await _run_precondition_gate(
        fake_node,
        MagicMock(),
        {"action": "tap", "target_text": "Settings", "target_bounds": [0, 0, 100, 100]},
        State.initial("Open Settings"),
        "",
        [50, 50],
    )
    assert ok is True
    assert cat == ValidationErrorCategory.NONE
    fake_node._validate_action_precondition.assert_not_called()
    fake_node._validate_action_precondition_pixel.assert_awaited_once()

    # 7. Agent hybrid & offline initialization skips Accessibility Helper, dumpsys, and does not crash without ADB
    monkeypatch.delenv("ARTEMIS_DEVICE_ID", raising=False)
    monkeypatch.delenv("ADB_DEVICE_SERIAL", raising=False)
    monkeypatch.delenv("ANDROID_SERIAL", raising=False)
    agent_offline = Agent()
    with (
        patch("third_party.mobile_use.sdk.agent.which", return_value=None),
        patch(
            "third_party.mobile_use.sdk.agent.create_screen_client",
            side_effect=AssertionError("create_screen_client must not be called"),
        ),
        patch.object(agent_offline, "_prewarm_llm_connections", new=AsyncMock()),
    ):
        assert await agent_offline._init_internal() is True
        assert agent_offline._adb_client is None
        assert agent_offline._ui_adb_client is None
        assert agent_offline._device_context.device_id == "non-invasive-device"
        assert agent_offline._device_context.device_width == 1080
        # Unlock & environment prep must be no-ops without raising AgentError
        await agent_offline._ensure_device_unlocked()
        await agent_offline._prepare_device_environment(ctx)

    agent_hybrid = Agent()
    mock_adb_instance = MagicMock()
    mock_adb_instance.device.return_value.window_size.return_value = (1080, 2400)
    with (
        patch("third_party.mobile_use.sdk.agent.which", return_value="/usr/bin/adb"),
        patch(
            "third_party.mobile_use.sdk.agent.get_first_device",
            return_value=("emulator-5554", DevicePlatform.ANDROID, None),
        ),
        patch("third_party.mobile_use.sdk.agent.AdbClient", return_value=mock_adb_instance),
        patch(
            "third_party.mobile_use.sdk.agent.create_screen_client",
            side_effect=AssertionError("create_screen_client must not be called"),
        ),
        patch.object(agent_hybrid, "_prewarm_llm_connections", new=AsyncMock()),
    ):
        assert await agent_hybrid._init_internal() is True
        assert agent_hybrid._adb_client is mock_adb_instance
        assert agent_hybrid._ui_adb_client is None
        assert agent_hybrid._device_context.device_id == "emulator-5554"


@pytest.mark.asyncio
async def test_env_precedence_screen_client_factory_and_scrcpy_disabled(monkeypatch, tmp_path):
    """Verify shell VISION_ONLY_MODE overrides .env, ScreenClientFactory returns visual_only,
    and scrcpy/screen recording is automatically disabled in non-invasive mode."""
    from artemis.clients.screen_client_factory import (
        VisualOnlyScreenClient,
        create_screen_client,
        describe_backend,
        hierarchy_backend_sentence,
    )
    from artemis.config.core.engine import CascadingConfigEngine
    from artemis.config.settings import is_non_invasive_mode
    from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
    from artemis.controllers.unified_controller import UnifiedMobileController
    from artemis.drivers.android.adb_driver import AndroidAdbDriver
    from artemis.sdk.builders.agent_config_builder import AgentConfigBuilder

    # 1. Process env VISION_ONLY_MODE=true must NOT be clobbered by workspace .env VISION_ONLY_MODE=false
    ws_env = tmp_path / ".env"
    ws_env.write_text("VISION_ONLY_MODE=false\n", encoding="utf-8")
    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", False)
    monkeypatch.setattr(settings, "ARTEMIS_ACTUATOR_BACKEND", "adb")

    CascadingConfigEngine(workspace_root=tmp_path)
    assert os.environ.get(ENV_VISION_ONLY_MODE) == "true"
    assert is_vision_only_mode() is True
    assert is_non_invasive_mode() is True

    # Reverse precedence: explicit env=false wins even if settings.VISION_ONLY_MODE is True
    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "false")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", True)
    assert is_vision_only_mode() is False
    assert is_non_invasive_mode() is False

    # Re-enable via env only (simulating shell `$env:VISION_ONLY_MODE="true"` with .env=false)
    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "true")
    monkeypatch.setattr(settings, "VISION_ONLY_MODE", False)
    assert is_non_invasive_mode() is True

    # 2. ScreenClientFactory returns VisualOnlyScreenClient and never instantiates Helper/UIAutomator2
    with (
        patch(
            "artemis.clients.screen_client_factory.AccessibilityClient",
            side_effect=AssertionError("AccessibilityClient must not be instantiated"),
        ),
        patch(
            "artemis.clients.screen_client_factory.UIAutomatorClient",
            side_effect=AssertionError("UIAutomatorClient must not be instantiated"),
        ),
    ):
        client = create_screen_client("0083078983")
        assert isinstance(client, VisualOnlyScreenClient)
        assert client.active_backend == "visual_only"
        assert describe_backend(client) == "visual_only"
        assert "visual-only / non-invasive mode" in (hierarchy_backend_sentence(client) or "")
        client.connect()
        assert client.get_hierarchy() == ""
        data = client.get_screen_data()
        assert data.elements == []
        assert data.hierarchy_xml == ""
        client.disconnect()

    # 3. AgentConfigBuilder automatically disables video_recording_tools_enabled in non-invasive mode
    cfg = AgentConfigBuilder().build(validate_profiles=False)
    assert cfg.video_recording_tools_enabled is False

    # 4. UnifiedMobileController & AndroidAdbDriver refuse to spawn scrcpy in non-invasive mode
    ctx = ArtemisContext(
        device=DeviceContext(
            host_platform="WINDOWS",
            mobile_platform=DevicePlatform.ANDROID,
            device_id="0083078983",
            device_width=1080,
            device_height=2400,
        )
    )
    driver = AndroidAdbDriver(device_id="0083078983", adb_client=MagicMock())
    with patch(
        "asyncio.create_subprocess_exec",
        side_effect=AssertionError("scrcpy subprocess must not be spawned"),
    ):
        await driver.start_video_recording(output_dir=tmp_path)

    with (
        patch("artemis.controllers.unified_controller.get_driver", return_value=driver),
        patch(
            "artemis.controllers.unified_controller.get_android_display_state",
            new=AsyncMock(side_effect=AssertionError("display state probe must not run")),
        ),
    ):
        controller = UnifiedMobileController(ctx)
        rec_res = await controller.start_video_recording(output_dir=tmp_path)
        assert rec_res.success is False
        assert "disabled in non-invasive mode" in rec_res.message


