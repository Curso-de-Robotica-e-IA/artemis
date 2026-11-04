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

import os
import pytest
from unittest.mock import MagicMock

from artemis.config.constants import ENV_VISION_ONLY_MODE
from artemis.config.settings import is_vision_only_mode, settings
from artemis.agents.operator.prompts import ObservationPromptComponent, PromptBuilder
from artemis.agents.flash.runner import FlashRunner
from artemis.memory.transcript import TranscriptLedger


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

    # 2. Vision-only mode (VISION_ONLY_MODE = true)
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
    assert "Vision-only mode enabled: operate using visual coordinates [0-1000] only." in vision_text


def test_flash_runner_observation_tail_vision_only(monkeypatch):
    """Verify FlashRunner._build_tail omits XML and displays neutral notice."""
    ledger = MagicMock(spec=TranscriptLedger)
    ledger.elapsed_label.return_value = "T+00:10"

    xml_list = "[1] button 'Play' [50, 50][150, 150]"
    fake_img = b"fake_screenshot_bytes"
    runner = FlashRunner.__new__(FlashRunner)

    # 1. Normal mode
    monkeypatch.setenv(ENV_VISION_ONLY_MODE, "false")
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

    # 2. Vision-only mode
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
    assert "Vision-only mode enabled: operate using visual coordinates [0-1000] only." in full_vision
