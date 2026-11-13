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

"""Unit tests for Pro Mode target inheritance and multi-provider routing."""

from __future__ import annotations

from pathlib import Path
from types import SimpleNamespace
from unittest.mock import Mock, patch

from artemis.agents.flash.summarizer import VisualStepSummarizer
from artemis.config.agent import (
    AgentGlobalConfig,
    MemoryChunkingConfig,
    StepSummarizerConfig,
    load_agent_config,
)
from artemis.config.llm import LLMConfig, _expand_default_into_nodes
from artemis.context import ArtemisContext
from artemis.memory.chunking import HistoryChunkManager, StepCapsuleLens


def test_agent_sub_configs_allow_missing_model_and_inherit_default(tmp_path: Path):
    """Omitting model/provider on step_summarizer or chunking cleanly inherits default."""
    cfg_path = tmp_path / "artemis.jsonc"
    cfg_path.write_text(
        """{
  "default": {
    "provider": "ollama",
    "model": "qwen3.8:27b",
    "fallback": {
      "provider": "ollama",
      "model": "qwen3.8:27b"
    }
  },
  "nodes": {
    "object_detector": {},
    "hopper": {}
  },
  "agent": {
    "flash": {
      "step_summarizer": {
        "enabled": true,
        "prune_history_xml": true
      }
    },
    "memory": {
      "chunking": {
        "max_steps": 12
      }
    }
  }
}
""",
        encoding="utf-8",
    )

    # Standalone models allow omitted model/provider without validation error.
    assert StepSummarizerConfig().model is None
    assert StepSummarizerConfig().provider is None
    assert MemoryChunkingConfig().model is None
    assert MemoryChunkingConfig().provider is None
    standalone_agent = AgentGlobalConfig.model_validate({})
    assert standalone_agent.flash.step_summarizer.model is None
    assert standalone_agent.memory.chunking.model is None

    # Loading with top-level 'default' inherits default provider/model.
    agent_cfg = load_agent_config(cfg_path)
    assert agent_cfg.flash.step_summarizer.provider == "ollama"
    assert agent_cfg.flash.step_summarizer.model == "qwen3.8:27b"
    assert agent_cfg.memory.chunking.provider == "ollama"
    assert agent_cfg.memory.chunking.model == "qwen3.8:27b"

    # Nodes object_detector and hopper fall back to default when empty.
    raw = {
        "default": {"provider": "ollama", "model": "qwen3.8:27b"},
        "nodes": {"object_detector": {}, "hopper": {}},
    }
    expanded = _expand_default_into_nodes(raw)
    llm_cfg = LLMConfig.model_validate(expanded)
    assert llm_cfg.utils.object_detector is not None
    assert llm_cfg.utils.object_detector.provider == "ollama"
    assert llm_cfg.utils.object_detector.model == "qwen3.8:27b"
    assert llm_cfg.utils.hopper.provider == "ollama"
    assert llm_cfg.utils.hopper.model == "qwen3.8:27b"
    assert llm_cfg.default is not None
    assert llm_cfg.default.fallback.model == "qwen3.8:27b"


def test_step_summarizer_passes_explicit_provider_and_inherits_default_when_none():
    """VisualStepSummarizer routes explicit provider/model or falls back to ctx default."""
    ctx = Mock(spec=ArtemisContext)
    ctx.data_engine = None
    ctx.llm_config = SimpleNamespace(
        default=SimpleNamespace(provider="openai", model="gpt-5.4"),
        summarizer=SimpleNamespace(provider="openai", model="gpt-5.4"),
    )

    with patch("artemis.agents.flash.summarizer.get_llm", return_value=Mock()) as mock_get_llm:
        summarizer_explicit = VisualStepSummarizer(
            ctx,
            model_name="claude-haiku",
            provider="anthropic",
        )
        assert summarizer_explicit._model_name == "claude-haiku"
        mock_get_llm.assert_called_once_with(
            ctx,
            name="summarizer",
            is_utils=False,
            temperature=0.0,
            model_name="claude-haiku",
            provider="anthropic",
        )

    with patch("artemis.agents.flash.summarizer.get_llm", return_value=Mock()) as mock_get_llm:
        summarizer_inherited = VisualStepSummarizer(ctx, model_name=None, provider=None)
        assert summarizer_inherited._model_name == "gpt-5.4"
        mock_get_llm.assert_called_once_with(
            ctx,
            name="summarizer",
            is_utils=False,
            temperature=0.0,
            model_name="gpt-5.4",
            provider="openai",
        )


def test_chunking_lens_routes_non_google_provider_and_inherits_default():
    """StepCapsuleLens supports explicit non-google providers and inherits default when omitted."""
    ctx = SimpleNamespace(
        llm_config=SimpleNamespace(
            default=SimpleNamespace(
                provider="ollama",
                model="qwen3.8:27b",
                fallback=SimpleNamespace(provider="openai", model="gpt-5.4"),
            ),
            summarizer=SimpleNamespace(
                provider="ollama",
                model="qwen3.8:27b",
                fallback=SimpleNamespace(provider="openai", model="gpt-5.4"),
            ),
        )
    )

    # 1. Explicit non-google provider uses build_chat_model
    lens_explicit = StepCapsuleLens(model_name="gpt-5.4", provider="openai", ctx=ctx)
    with patch("artemis.services.llm.build_chat_model", return_value=Mock()) as mock_build:
        llm = lens_explicit._get_llm()
        assert llm is not None
        mock_build.assert_called_once_with("openai", "gpt-5.4", temperature=0.0)

    # 2. Omitted chunking model inherits default (provider, model)
    mgr = HistoryChunkManager(
        ctx=ctx,
        chunking_config=SimpleNamespace(max_steps=12, min_steps=3, target_source_tokens=2000),
    )
    assert mgr._provider == "ollama"
    assert mgr._model_name == "qwen3.8:27b"
    assert mgr.capsule_service.lens._fallback_provider == "openai"
    assert mgr.capsule_service.lens._fallback_model_name == "gpt-5.4"
