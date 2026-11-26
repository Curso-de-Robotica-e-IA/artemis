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

"""Unit tests for intelligent image downscaling for LLM vision models."""

import base64
import io
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import MagicMock, patch

import pytest
from PIL import Image

from artemis.utils.image_scaling import (
    encode_image_for_llm,
    prepare_image_bytes_for_llm,
)


def _create_test_image_bytes(width: int, height: int, color: tuple = (255, 0, 0)) -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_encode_image_downscaling():
    # Large 1200 x 2400 image (aspect ratio 1:2)
    raw = _create_test_image_bytes(1200, 2400)
    b64 = encode_image_for_llm(raw, max_dim=1200)

    # Decode and check dimensions
    decoded = base64.b64decode(b64)
    with Image.open(io.BytesIO(decoded)) as im:
        assert im.height == 1200
        assert im.width == 600
        # Aspect ratio is exactly 1:2 preserved
        assert abs((im.width / im.height) - (1200 / 2400)) < 0.01


def test_encode_image_no_upscale():
    # Small 400 x 800 image
    raw = _create_test_image_bytes(400, 800)
    b64 = encode_image_for_llm(raw, max_dim=1280)

    decoded = base64.b64decode(b64)
    with Image.open(io.BytesIO(decoded)) as im:
        assert im.width == 400
        assert im.height == 800


def test_encode_image_disabled_downscaling():
    # Downscaling disabled with max_dim=0
    raw = _create_test_image_bytes(1080, 2400)
    b64 = encode_image_for_llm(raw, max_dim=0)

    decoded = base64.b64decode(b64)
    with Image.open(io.BytesIO(decoded)) as im:
        assert im.width == 1080
        assert im.height == 2400


def test_encode_image_from_base64_and_pil():
    raw = _create_test_image_bytes(1000, 2000)
    raw_b64 = base64.b64encode(raw).decode("utf-8")

    # From base64 string
    res_b64 = encode_image_for_llm(raw_b64, max_dim=1000)
    decoded = base64.b64decode(res_b64)
    with Image.open(io.BytesIO(decoded)) as im:
        assert im.height == 1000
        assert im.width == 500

    # From PIL Image instance
    pil_im = Image.new("RGB", (1000, 2000), (0, 255, 0))
    res_pil_b64 = encode_image_for_llm(pil_im, max_dim=1000)
    decoded_pil = base64.b64decode(res_pil_b64)
    with Image.open(io.BytesIO(decoded_pil)) as im:
        assert im.height == 1000
        assert im.width == 500


def test_encode_image_empty_and_fallback():
    assert encode_image_for_llm(None) == ""
    assert encode_image_for_llm(b"") == ""
    assert encode_image_for_llm("") == ""


def test_encode_image_reads_dynamic_artemis_jsonc_without_restart(tmp_path, monkeypatch):
    cfg_file = tmp_path / "artemis.jsonc"
    cfg_file.write_text(
        """{
  // Screenshot dim comment
  "default": {"provider": "google", "model": "gemini-3.8-flash"},
  "agent": {
    "image_max_dim": 1600
  }
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr("artemis.config.paths.get_config_path", lambda name, *a, **kw: cfg_file)
    monkeypatch.setenv("ARTEMIS_IMAGE_MAX_DIM", "640")

    raw = _create_test_image_bytes(1000, 2000)

    # First read picks 1600 from artemis.jsonc (takes precedence over env var).
    b64_1600 = encode_image_for_llm(raw)
    with Image.open(io.BytesIO(base64.b64decode(b64_1600))) as im:
        assert max(im.width, im.height) == 1600

    # Updating artemis.jsonc takes effect immediately on the next call.
    cfg_file.write_text(
        '{"default": {"provider": "google", "model": "gemini-3.8-flash"}, "agent": {"image_max_dim": 720}}',
        encoding="utf-8",
    )
    b64_720 = encode_image_for_llm(raw)
    with Image.open(io.BytesIO(base64.b64decode(b64_720))) as im:
        assert max(im.width, im.height) == 720

    # 0 disables downscaling even if ARTEMIS_IMAGE_MAX_DIM env var is still set.
    cfg_file.write_text(
        '{"default": {"provider": "google", "model": "gemini-3.8-flash"}, "agent": {"image_max_dim": 0}}',
        encoding="utf-8",
    )
    b64_orig = encode_image_for_llm(raw)
    with Image.open(io.BytesIO(base64.b64decode(b64_orig))) as im:
        assert (im.width, im.height) == (1000, 2000)


def test_encode_image_falls_back_to_env_when_omitted_in_jsonc(tmp_path, monkeypatch):
    cfg_file = tmp_path / "artemis.jsonc"
    cfg_file.write_text(
        '{"default": {"provider": "google", "model": "gemini-3.8-flash"}, "agent": {}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("artemis.config.paths.get_config_path", lambda name, *a, **kw: cfg_file)
    monkeypatch.setenv("ARTEMIS_IMAGE_MAX_DIM", "800")

    raw = _create_test_image_bytes(1000, 2000)
    b64_env = encode_image_for_llm(raw)
    with Image.open(io.BytesIO(base64.b64decode(b64_env))) as im:
        assert max(im.width, im.height) == 800

    monkeypatch.delenv("ARTEMIS_IMAGE_MAX_DIM", raising=False)
    monkeypatch.delenv("IMAGE_MAX_DIM", raising=False)
    monkeypatch.delenv("ARTEMIS_SCREENSHOT_MAX_DIM", raising=False)
    b64_default = encode_image_for_llm(raw)
    with Image.open(io.BytesIO(base64.b64decode(b64_default))) as im:
        assert max(im.width, im.height) == 1280


def test_prepare_image_bytes_passthrough_and_rgba_pil():
    # Already-compliant JPEG passes through unchanged (no lossy re-compression).
    small_jpeg = _create_test_image_bytes(400, 800)
    assert prepare_image_bytes_for_llm(small_jpeg, max_dim=1280) == small_jpeg
    assert prepare_image_bytes_for_llm(small_jpeg, max_dim=0) == small_jpeg

    # RGBA PIL image converts cleanly to RGB JPEG and scales to max_dim.
    rgba_im = Image.new("RGBA", (1000, 2000), (10, 20, 30, 128))
    scaled_bytes = prepare_image_bytes_for_llm(rgba_im, max_dim=500)
    with Image.open(io.BytesIO(scaled_bytes)) as im:
        assert im.format == "JPEG"
        assert im.mode == "RGB"
        assert (im.width, im.height) == (250, 500)

    # Empty / unsupported inputs return empty bytes safely.
    assert prepare_image_bytes_for_llm(None) == b""
    assert prepare_image_bytes_for_llm(b"") == b""
    assert prepare_image_bytes_for_llm("") == b""


def test_image_scaling_logs_effective_resolution(monkeypatch):
    logged: list[str] = []
    monkeypatch.setattr("artemis.utils.image_scaling.logger.info", logged.append)

    large = _create_test_image_bytes(1080, 2400)
    encode_image_for_llm(large, max_dim=1200, caller="flash")
    assert len(logged) == 1
    assert "[ImageScaling] caller=flash 1080x2400 -> 540x1200 (image_max_dim=1200 [scaled]," in logged[0]

    logged.clear()
    encode_image_for_llm(large, max_dim=0, caller="operator")
    assert len(logged) == 1
    assert "[ImageScaling] caller=operator 1080x2400 -> 1080x2400 (image_max_dim=0 [original]," in logged[0]

    logged.clear()
    small = _create_test_image_bytes(400, 800)
    encode_image_for_llm(small, max_dim=1280, caller="checker")
    assert len(logged) == 1
    assert "[ImageScaling] caller=checker 400x800 -> 400x800 (image_max_dim=1280 [unchanged]," in logged[0]

    logged.clear()
    encode_image_for_llm(large, max_dim=720, caller="video_analyzer", log_resolution=False)
    assert logged == []


def _decode_image_block_size(block: dict) -> tuple[int, int]:
    url = block["image_url"]["url"]
    b64 = url.split(",", 1)[1]
    with Image.open(io.BytesIO(base64.b64decode(b64))) as im:
        return im.width, im.height


def _set_dynamic_image_max_dim(tmp_path: Path, monkeypatch: pytest.MonkeyPatch, dim: int) -> None:
    cfg_file = tmp_path / "artemis.jsonc"
    cfg_file.write_text(
        f'{{"default": {{"provider": "google", "model": "gemini-2.5-flash"}}, "agent": {{"image_max_dim": {dim}}}}}',
        encoding="utf-8",
    )
    monkeypatch.setattr("artemis.config.paths.get_config_path", lambda name, *a, **kw: cfg_file)


@pytest.mark.asyncio
async def test_subagents_and_tools_respect_dynamic_image_max_dim(tmp_path, monkeypatch):
    """Verify secondary agents/tools scale LLM payloads according to agent.image_max_dim."""
    from artemis.agents.diagnoser.diagnoser import Diagnoser
    from artemis.agents.explorer.native_runner import NativeRunnerMixin
    from artemis.agents.explorer.universal_runner import UniversalRunnerMixin
    from artemis.agents.outputter.outputter import _initial_messages as outputter_initial_messages
    from artemis.agents.validator.precondition_pixel import _build_messages as pixel_build_messages
    from artemis.agents.validator.tool_declarations import ToolExecutionResult
    from artemis.config import OutputConfig
    from artemis.tools.explorer_tool import (
        ExplorerCandidate,
        ExplorerOutcome,
        RegisteredCandidate,
        render_operator_blocks,
    )
    from artemis.tools.history.screenshot import ScreenshotResult

    _set_dynamic_image_max_dim(tmp_path, monkeypatch, 720)
    large_bytes = _create_test_image_bytes(1080, 2400)
    large_b64 = base64.b64encode(large_bytes).decode("utf-8")
    shot_path = tmp_path / "shot.jpg"
    shot_path.write_bytes(large_bytes)

    # 1. get_step_screenshot (ScreenshotResult.to_content_blocks)
    shot = ScreenshotResult(step_number=1, which="pre", description="pre", image_bytes=large_bytes)
    shot_blocks = shot.to_content_blocks()
    assert isinstance(shot_blocks, list)
    assert _decode_image_block_size(shot_blocks[1]) == (324, 720)

    # 2. ToolExecutionResult.to_langchain_tool_message
    tool_msg = ToolExecutionResult(
        tool_call_id="tc-1",
        tool_name="click",
        status="success",
        text_summary="Clicked",
        screenshot_bytes=large_bytes,
    ).to_langchain_tool_message()
    assert _decode_image_block_size(tool_msg.content[-1]) == (324, 720)

    # 3. Explorer universal runner
    uni_msgs = UniversalRunnerMixin()._build_universal_messages(
        query="Find button",
        context_feedback="",
        screenshot_path=str(shot_path),
        minimal_list="",
        prompt_template="sys",
    )
    assert _decode_image_block_size(uni_msgs[1].content[1]) == (324, 720)

    # 4. Explorer native runner (inline Part.from_bytes)
    getter = NativeRunnerMixin()._make_image_part_getter(
        client=MagicMock(), use_file_api=False, uploaded_files=[]
    )
    native_part = await getter(str(shot_path))
    assert native_part.inline_data is not None and native_part.inline_data.data is not None
    with Image.open(io.BytesIO(native_part.inline_data.data)) as im:
        assert (im.width, im.height) == (324, 720)

    # 5. Explorer tool annotated image returned to Operator
    ctx = SimpleNamespace(data_engine=SimpleNamespace(base_dir=tmp_path))
    state = SimpleNamespace(latest_screenshot=str(shot_path))
    outcome = ExplorerOutcome(candidates=[ExplorerCandidate("S1", (500, 500), "Btn")])
    registered = [RegisteredCandidate(1, (540, 1200), (500, 500), "Btn")]
    with patch(
        "artemis.tools.explorer_tool.draw_dots",
        side_effect=lambda _src, _pts, _lbls, out, **_kw: Path(out).write_bytes(large_bytes),
    ):
        op_blocks = render_operator_blocks(ctx, state, "Find btn", outcome, registered)
    assert isinstance(op_blocks, list)
    assert _decode_image_block_size(op_blocks[1]) == (324, 720)

    # 6. Outputter initial messages
    out_ctx = SimpleNamespace(data_engine=None, device=None)
    out_state = SimpleNamespace(
        initial_goal="Test goal",
        operator_raw_data={"screenshot_b64": large_b64},
    )
    out_msgs = outputter_initial_messages(
        out_ctx, OutputConfig(structured_output=None, output_description="desc"), out_state, None
    )
    out_img_block = next(b for b in out_msgs[1].content if b.get("type") == "image_url")
    assert _decode_image_block_size(out_img_block) == (324, 720)

    # 7. Diagnoser initial messages
    diag = Diagnoser(ctx=SimpleNamespace(data_engine=None))
    diag_state = SimpleNamespace(initial_goal="Goal", latest_screenshot=str(shot_path))
    diag_msgs = diag._build_initial_messages(
        prompt="Why stuck?",
        state=diag_state,
        system_prompt="sys",
        checker_feedback_template="",
        plan_and_history="",
        checker_feedback="",
        minimal_list="",
    )
    diag_img_block = next(b for b in diag_msgs[1].content if b.get("type") == "image_url")
    assert _decode_image_block_size(diag_img_block) == (324, 720)

    # 8. Pixel precondition safety net
    pix_msgs = pixel_build_messages("sys", large_bytes, large_bytes, {"action": "click"}, None)
    pix_img_blocks = [b for b in pix_msgs[1].content if b.get("type") == "image_url"]
    assert len(pix_img_blocks) == 2
    assert all(_decode_image_block_size(b) == (324, 720) for b in pix_img_blocks)

    # Switching to 0 (Original) immediately restores full resolution without restart.
    _set_dynamic_image_max_dim(tmp_path, monkeypatch, 0)
    shot_blocks_orig = shot.to_content_blocks()
    assert isinstance(shot_blocks_orig, list)
    assert _decode_image_block_size(shot_blocks_orig[1]) == (1080, 2400)


def test_all_agent_and_tool_image_payloads_use_image_scaling_contract():
    """Contract guard: every agent/tool building LLM image payloads must use image_scaling."""
    repo_root = Path(__file__).resolve().parents[3]
    offenders: list[str] = []
    for folder in (repo_root / "artemis" / "agents", repo_root / "artemis" / "tools"):
        for py_file in sorted(folder.rglob("*.py")):
            src = py_file.read_text(encoding="utf-8")
            sends_image = "data:image/" in src or 'mime_type="image/jpeg"' in src
            if sends_image and "artemis.utils.image_scaling" not in src:
                offenders.append(str(py_file.relative_to(repo_root)))
    assert offenders == [], f"Modules bypassing artemis.utils.image_scaling: {offenders}"
