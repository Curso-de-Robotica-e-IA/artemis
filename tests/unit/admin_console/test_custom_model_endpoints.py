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

"""Unit tests for custom/Ollama model discovery and default model selection endpoints."""

from __future__ import annotations

import io
from pathlib import Path
from unittest.mock import patch

import httpx
import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.server import app
from apps.admin_console.services import custom_model_service
from apps.admin_console.services.model_service import ModelService
from artemis.config import settings
from third_party.mobile_use.utils.file import load_jsonc


def _make_fake_async_client(routes: dict[str, tuple[int, object]]):
    """Create a minimal AsyncClient replacement returning canned status/JSON responses."""

    class _FakeAsyncClient:
        def __init__(self, *args, **kwargs):
            self.calls: list[tuple[str, dict[str, str]]] = []

        async def __aenter__(self):
            return self

        async def __aexit__(self, exc_type, exc, tb):
            return False

        async def get(self, url: str, headers: dict[str, str] | None = None):
            self.calls.append((url, headers or {}))
            if url not in routes:
                return httpx.Response(
                    404, json={"error": "not found"}, request=httpx.Request("GET", url)
                )
            status, payload = routes[url]
            return httpx.Response(status, json=payload, request=httpx.Request("GET", url))

    return _FakeAsyncClient


@pytest.mark.asyncio
async def test_list_custom_models_fetches_ollama_openai_compat_and_enriches_tags(monkeypatch):
    monkeypatch.setattr(settings, "OPENAI_BASE_URL", "http://192.168.155.5:11434/v1")
    monkeypatch.setattr(type(settings), "get_api_key", lambda self, provider: None)

    routes = {
        "http://192.168.155.5:11434/v1/models": (
            200,
            {
                "object": "list",
                "data": [
                    {"id": "qwen3.8:27b", "owned_by": "library"},
                    {"id": "llama3.2-vision:11b", "owned_by": "library"},
                ],
            },
        ),
        "http://192.168.155.5:11434/api/tags": (
            200,
            {
                "models": [
                    {
                        "name": "qwen3.8:27b",
                        "model": "qwen3.8:27b",
                        "size": 17_000_000_000,
                        "details": {"parameter_size": "27B", "quantization_level": "Q4_K_M"},
                    }
                ]
            },
        ),
    }

    with patch.object(custom_model_service.httpx, "AsyncClient", _make_fake_async_client(routes)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
            res = await ac.get("/api/system/custom-models")

    assert res.status_code == 200
    body = res.json()
    assert body["base_url"] == "http://192.168.155.5:11434/v1"
    assert body["source"] == "ollama"
    by_id = {item["id"]: item for item in body["models"]}
    assert list(by_id) == ["llama3.2-vision:11b", "qwen3.8:27b"]
    assert by_id["qwen3.8:27b"]["parameter_size"] == "27B"
    assert by_id["qwen3.8:27b"]["quantization_level"] == "Q4_K_M"


@pytest.mark.asyncio
async def test_list_custom_models_falls_back_to_native_ollama_tags(monkeypatch):
    monkeypatch.setattr(type(settings), "get_api_key", lambda self, provider: None)
    routes = {
        "http://192.168.155.5:11434/models": (404, {"error": "not found"}),
        "http://192.168.155.5:11434/v1/models": (404, {"error": "not found"}),
        "http://192.168.155.5:11434/api/tags": (
            200,
            {
                "models": [
                    {
                        "name": "ornith-1.5:35b",
                        "model": "ornith-1.5:35b",
                        "details": {"parameter_size": "35B"},
                    }
                ]
            },
        ),
    }

    with patch.object(custom_model_service.httpx, "AsyncClient", _make_fake_async_client(routes)):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
            res = await ac.get(
                "/api/system/custom-models",
                params={"base_url": "http://192.168.155.5:11434"},
            )

    assert res.status_code == 200
    body = res.json()
    assert body["endpoint_used"] == "http://192.168.155.5:11434/api/tags"
    assert [m["id"] for m in body["models"]] == ["ornith-1.5:35b"]


@pytest.mark.asyncio
async def test_list_custom_models_rejects_invalid_scheme_and_remote_clients():
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res = await ac.get("/api/system/custom-models", params={"base_url": "file:///etc/passwd"})
    assert res.status_code == 400

    async with AsyncClient(
        transport=ASGITransport(app=app, client=("192.168.1.55", 50000)),
        base_url="http://localhost",
    ) as ac:
        remote_res = await ac.get(
            "/api/system/custom-models",
            params={"base_url": "http://127.0.0.1:11434/v1"},
        )
    assert remote_res.status_code == 403


@pytest.mark.asyncio
async def test_update_default_model_preserves_jsonc_comments_and_persists_base_url(
    tmp_path: Path, monkeypatch
):
    cfg_file = tmp_path / "artemis.jsonc"
    env_file = tmp_path / ".env"
    original_jsonc = """{
  // Top comment must stay intact
  "default": {
    "provider": "google",
    "model": "gemini-3.8-flash",
    "thinking_level": "medium",
    "fallback": {
      "provider": "google",
      "model": "gemini-3.7-flash"
    }
  },
  // Presets stay untouched
  "presets": {
    "keep-me": {
      "provider": "google",
      "model": "gemini-2.5-flash"
    }
  }
}
"""
    cfg_file.write_text(original_jsonc, encoding="utf-8")
    env_file.write_text("# OPENAI_BASE_URL=http://localhost:11434/v1\n", encoding="utf-8")

    monkeypatch.setattr(
        custom_model_service, "resolve_writable_artemis_config_path", lambda: cfg_file
    )
    monkeypatch.setattr(custom_model_service, "get_env_file", lambda: env_file)
    monkeypatch.setattr("artemis.config.paths.get_config_path", lambda name, *a, **kw: cfg_file)
    monkeypatch.setattr("artemis.config.paths.get_env_file", lambda: env_file)

    ModelService._llm_info_cache = (9999999999.0, "google", "gemini-3.8-flash")
    orig_base_url = settings.OPENAI_BASE_URL
    try:
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
            res = await ac.post(
                "/api/system/model-config/default",
                json={
                    "provider": "openai",
                    "model": "qwen3.8:27b",
                    "update_fallback": True,
                    "base_url": "http://192.168.155.5:11434/v1",
                    "persist_base_url": True,
                },
            )
    finally:
        monkeypatch.setattr(settings, "OPENAI_BASE_URL", orig_base_url)

    assert res.status_code == 200
    data = res.json()
    assert data["status"] == "success"
    assert data["default_model"]["provider"] == "openai"
    assert data["default_model"]["model"] == "qwen3.8:27b"
    assert data["default_model"]["fallback"]["provider"] == "openai"
    assert data["default_model"]["fallback"]["model"] == "qwen3.8:27b"
    assert ModelService._llm_info_cache is None

    updated_text = cfg_file.read_text(encoding="utf-8")
    assert "// Top comment must stay intact" in updated_text
    assert "// Presets stay untouched" in updated_text
    parsed = load_jsonc(io.StringIO(updated_text))
    assert parsed["default"]["thinking_level"] == "medium"
    assert parsed["presets"]["keep-me"]["model"] == "gemini-2.5-flash"

    env_text = env_file.read_text(encoding="utf-8")
    assert "OPENAI_BASE_URL=http://192.168.155.5:11434/v1" in env_text


@pytest.mark.asyncio
async def test_multi_provider_model_listing_and_config_fallback(tmp_path: Path, monkeypatch):
    cfg_file = tmp_path / "artemis.jsonc"
    cfg_file.write_text(
        """{
  "default": {"provider": "ollama", "model": "qwen3.8:27b"},
  "presets": {
    "google-fast": {"provider": "google", "model": "gemini-3.5-flash"},
    "openai-flagship": {"provider": "openai", "model": "gpt-5.4"}
  },
  "nodes": {
    "object_detector": {"model": "gemini-robotics-er-2-preview"}
  },
  "agent": {
    "flash": {"step_summarizer": {"model": "gemini-3.5-flash-lite"}},
    "memory": {"chunking": {"model": "gemini-3.8-flash"}}
  }
}
""",
        encoding="utf-8",
    )
    monkeypatch.setattr(custom_model_service, "get_config_path", lambda *a, **kw: cfg_file)
    monkeypatch.setattr("artemis.config.paths.get_config_path", lambda *a, **kw: cfg_file)
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GOOGLE_API_KEY", raising=False)
    monkeypatch.delenv("OPENAI_API_KEY", raising=False)
    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)
    monkeypatch.setattr(type(settings), "get_api_key", lambda self, provider: None)

    # 1. Missing API key falls back gracefully to preset & config models.
    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res_missing = await ac.get("/api/system/custom-models", params={"provider": "google"})
    assert res_missing.status_code == 200
    body_missing = res_missing.json()
    assert body_missing["provider"] == "google"
    assert body_missing["fallback_used"] is True
    assert body_missing["warning"]
    fallback_ids = {m["id"] for m in body_missing["models"]}
    assert {"gemini-3.5-flash", "gemini-3.5-flash-lite", "gemini-3.8-flash"} <= fallback_ids

    # 2. Live Google Gemini query strips 'models/' prefix and filters generateContent.
    monkeypatch.setenv("GEMINI_API_KEY", "AIza-live-test-key-123456")
    routes = {
        "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200": (
            200,
            {
                "models": [
                    {
                        "name": "models/gemini-2.5-pro",
                        "displayName": "Gemini 2.5 Pro",
                        "supportedGenerationMethods": ["generateContent"],
                    },
                    {
                        "name": "models/embedding-001",
                        "displayName": "Embedding",
                        "supportedGenerationMethods": ["embedContent"],
                    },
                ]
            },
        ),
        "https://api.openai.com/v1/models": (503, {"error": "unavailable"}),
    }
    monkeypatch.setenv("OPENAI_API_KEY", "sk-test-openai-key-123456")

    fake_cls = _make_fake_async_client(routes)
    with patch.object(custom_model_service.httpx, "AsyncClient", fake_cls):
        async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
            res_google = await ac.get("/api/system/custom-models", params={"provider": "google"})
            res_openai = await ac.get("/api/system/custom-models", params={"provider": "openai"})

    assert res_google.status_code == 200
    google_body = res_google.json()
    assert google_body["fallback_used"] is False
    assert [m["id"] for m in google_body["models"]] == ["gemini-2.5-pro"]

    # 3. Remote 503 on external provider falls back gracefully to preset gpt-5.4.
    assert res_openai.status_code == 200
    openai_body = res_openai.json()
    assert openai_body["fallback_used"] is True
    assert "503" in openai_body["warning"]
    assert [m["id"] for m in openai_body["models"]] == ["gpt-5.4"]


@pytest.mark.asyncio
async def test_pro_mode_targets_apply_and_reset_preserve_comments_and_mirror_fallback(
    tmp_path: Path, monkeypatch
):
    cfg_file = tmp_path / "artemis.jsonc"
    env_file = tmp_path / ".env"
    cfg_file.write_text(
        """{
  // Global default section
  "default": {
    "provider": "ollama",
    "model": "qwen3.8:27b",
    "fallback": {
      "provider": "ollama",
      "model": "qwen3.8:27b"
    }
  },
  "nodes": {
    // Object Detector sub-agent comment
    "object_detector": {
      "model": "gemini-robotics-er-2-preview",
      "fallback": {
        "provider": "google",
        "model": "gemini-2.5-flash"
      }
    },
    // Hopper utility comment
    "hopper": {
      "model": "gemini-3.5-flash",
      "fallback": {
        "provider": "google",
        "model": "gemini-3.5-flash-lite"
      }
    }
  },
  "agent": {
    "flash": {
      "step_summarizer": {
        "enabled": true, // Keeps running asynchronously
        "model": "gemini-3.5-flash-lite", // Lightweight model comment
        "prune_history_xml": true // Prune old XML trees
      }
    },
    "memory": {
      "chunking": {
        "max_steps": 12,
        "model": "gemini-3.8-flash"
      }
    }
  }
}
""",
        encoding="utf-8",
    )
    env_file.write_text("OPENAI_BASE_URL=http://127.0.0.1:11434/v1\n", encoding="utf-8")

    monkeypatch.setattr(
        custom_model_service, "resolve_writable_artemis_config_path", lambda: cfg_file
    )
    monkeypatch.setattr(custom_model_service, "get_env_file", lambda: env_file)
    monkeypatch.setattr("artemis.config.paths.get_config_path", lambda *a, **kw: cfg_file)
    monkeypatch.setattr("artemis.config.paths.get_env_file", lambda: env_file)

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        # 1. Inspect initial state
        env_res = await ac.get("/api/system/model-config-env")
        assert env_res.status_code == 200
        env_body = env_res.json()
        pro_by_key = {t["key"]: t for t in env_body["pro_targets"]}
        assert list(pro_by_key) == ["object_detector", "hopper", "step_summarizer", "chunking"]
        assert pro_by_key["object_detector"]["requires_vision_note"]
        assert pro_by_key["object_detector"]["supports_fallback"] is True
        assert pro_by_key["step_summarizer"]["supports_fallback"] is False

        # 2. Apply provider + model to all 4 targets (mirrored fallback on nodes.*)
        r_od = await ac.post(
            "/api/system/model-config/target",
            json={
                "target": "object_detector",
                "provider": "google",
                "model": "gemini-2.5-pro",
            },
        )
        assert r_od.status_code == 200
        r_hop = await ac.post(
            "/api/system/model-config/target",
            json={"target": "hopper", "provider": "openai", "model": "gpt-5.4"},
        )
        assert r_hop.status_code == 200
        r_ss = await ac.post(
            "/api/system/model-config/target",
            json={
                "target": "step_summarizer",
                "provider": "google",
                "model": "gemini-2.5-flash-lite",
            },
        )
        assert r_ss.status_code == 200
        r_ch = await ac.post(
            "/api/system/model-config/target",
            json={"target": "chunking", "provider": "anthropic", "model": "claude-sonnet-4"},
        )
        assert r_ch.status_code == 200

        parsed_after_apply = load_jsonc(io.StringIO(cfg_file.read_text(encoding="utf-8")))
        assert parsed_after_apply["nodes"]["object_detector"] == {
            "provider": "google",
            "model": "gemini-2.5-pro",
            "fallback": {"provider": "google", "model": "gemini-2.5-pro"},
        }
        assert parsed_after_apply["nodes"]["hopper"] == {
            "provider": "openai",
            "model": "gpt-5.4",
            "fallback": {"provider": "openai", "model": "gpt-5.4"},
        }
        assert parsed_after_apply["agent"]["flash"]["step_summarizer"] == {
            "enabled": True,
            "provider": "google",
            "model": "gemini-2.5-flash-lite",
            "prune_history_xml": True,
        }
        assert parsed_after_apply["agent"]["memory"]["chunking"] == {
            "max_steps": 12,
            "provider": "anthropic",
            "model": "claude-sonnet-4",
        }

        # 3. Reset all 4 targets via "Use Default" (removes model, provider, fallback)
        for key in ("object_detector", "hopper", "step_summarizer", "chunking"):
            rst = await ac.post("/api/system/model-config/target/reset", json={"target": key})
            assert rst.status_code == 200
            assert rst.json()["target"]["is_inheriting_default"] is True
            assert rst.json()["target"]["active_badge"] == "Inheriting Default"

        text_after_reset = cfg_file.read_text(encoding="utf-8")
        assert "// Global default section" in text_after_reset
        assert "// Object Detector sub-agent comment" in text_after_reset
        assert "// Hopper utility comment" in text_after_reset
        assert "// Keeps running asynchronously" in text_after_reset
        assert "// Prune old XML trees" in text_after_reset

        parsed_after_reset = load_jsonc(io.StringIO(text_after_reset))
        assert parsed_after_reset["nodes"]["object_detector"] == {}
        assert parsed_after_reset["nodes"]["hopper"] == {}
        assert parsed_after_reset["agent"]["flash"]["step_summarizer"] == {
            "enabled": True,
            "prune_history_xml": True,
        }
        assert parsed_after_reset["agent"]["memory"]["chunking"] == {"max_steps": 12}

        # 4. Re-apply onto empty object_detector and step_summarizer after reset
        r_reapply = await ac.post(
            "/api/system/model-config/target",
            json={
                "target": "object_detector",
                "provider": "ollama",
                "model": "qwen3-vl:8b",
            },
        )
        assert r_reapply.status_code == 200
        parsed_reapplied = load_jsonc(io.StringIO(cfg_file.read_text(encoding="utf-8")))
        assert parsed_reapplied["nodes"]["object_detector"] == {
            "provider": "ollama",
            "model": "qwen3-vl:8b",
            "fallback": {"provider": "ollama", "model": "qwen3-vl:8b"},
        }


def test_real_bundled_artemis_jsonc_roundtrip_preserves_comments(tmp_path: Path, monkeypatch):
    """Apply and Use Default round-trips on real artemis.jsonc keep all structural comments intact."""
    bundled = Path("artemis/resources/config/artemis.jsonc")
    assert bundled.is_file()
    cfg_file = tmp_path / "artemis.jsonc"
    original_text = bundled.read_text(encoding="utf-8")
    cfg_file.write_text(original_text, encoding="utf-8")

    monkeypatch.setattr(
        custom_model_service, "resolve_writable_artemis_config_path", lambda: cfg_file
    )

    for target_key, prov, model in (
        ("object_detector", "google", "gemini-2.5-pro"),
        ("hopper", "openai", "gpt-5.4"),
        ("step_summarizer", "anthropic", "claude-haiku"),
        ("chunking", "ollama", "qwen3.8:27b"),
    ):
        custom_model_service.update_pro_target_model_config(target_key, prov, model)

    for target_key in ("object_detector", "hopper", "step_summarizer", "chunking"):
        res = custom_model_service.reset_pro_target_model_config(target_key)
        assert res["target"]["is_inheriting_default"] is True

    final_text = cfg_file.read_text(encoding="utf-8")
    for line in original_text.splitlines():
        if line.lstrip().startswith("//"):
            assert line in final_text

    for expected_comment in (
        "// 👁️ Object Detector & Explorer (Flash Mode): Visual coordinates grounding and bounding boxes",
        "// 🔽 Hopper: Fast lightweight package name locator",
        "// 🧠 Step Summarizer: Asynchronous visual context compressor",
        "// Whether background step summarization is active",
        "// Prune outdated heavy XML trees from historical steps",
    ):
        assert expected_comment in original_text
        assert expected_comment in final_text

    parsed = load_jsonc(io.StringIO(final_text))
    assert parsed["nodes"]["object_detector"] == {}
    assert parsed["nodes"]["hopper"] == {}
    assert "model" not in parsed["agent"]["flash"]["step_summarizer"]
    assert "model" not in parsed["agent"]["memory"]["chunking"]


@pytest.mark.asyncio
async def test_image_resolution_endpoints_update_jsonc_and_apply_dynamically(
    tmp_path: Path, monkeypatch
):
    """Updating image_max_dim preserves all JSONC comments and affects scaling immediately."""
    import base64
    from PIL import Image
    from artemis.utils.image_scaling import encode_image_for_llm

    bundled = Path("artemis/resources/config/artemis.jsonc")
    assert bundled.is_file()
    cfg_file = tmp_path / "artemis.jsonc"
    original_text = bundled.read_text(encoding="utf-8")
    cfg_file.write_text(original_text, encoding="utf-8")

    monkeypatch.setattr(
        custom_model_service, "resolve_writable_artemis_config_path", lambda: cfg_file
    )
    monkeypatch.setattr(custom_model_service, "get_config_path", lambda *a, **kw: cfg_file)
    monkeypatch.setattr("artemis.config.paths.get_config_path", lambda *a, **kw: cfg_file)

    raw_buf = io.BytesIO()
    Image.new("RGB", (1000, 2000), (0, 128, 255)).save(raw_buf, format="JPEG")
    raw_bytes = raw_buf.getvalue()

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res_initial = await ac.get("/api/system/image-resolution")
        assert res_initial.status_code == 200
        assert res_initial.json()["image_max_dim"] == 1280
        assert res_initial.json()["source"] == "artemis.jsonc"

        res_env = await ac.get("/api/system/model-config-env")
        assert res_env.status_code == 200
        assert res_env.json()["image_max_dim"] == 1280

        res_720 = await ac.post("/api/system/image-resolution", json={"image_max_dim": 720})
        assert res_720.status_code == 200
        assert res_720.json()["image_max_dim"] == 720
        assert res_720.json()["model_config_env"]["image_max_dim"] == 720

        encoded_720 = encode_image_for_llm(raw_bytes)
        with Image.open(io.BytesIO(base64.b64decode(encoded_720))) as im:
            assert max(im.width, im.height) == 720

        res_zero = await ac.post("/api/system/image-resolution", json={"image_max_dim": 0})
        assert res_zero.status_code == 200
        assert res_zero.json()["image_max_dim"] == 0
        assert res_zero.json()["model_config_env"]["image_max_dim"] == 0

        encoded_orig = encode_image_for_llm(raw_bytes)
        with Image.open(io.BytesIO(base64.b64decode(encoded_orig))) as im:
            assert (im.width, im.height) == (1000, 2000)

        for bad_payload in (
            {"image_max_dim": -10},
            {"image_max_dim": True},
            {"image_max_dim": 12.5},
        ):
            bad_res = await ac.post("/api/system/image-resolution", json=bad_payload)
            assert bad_res.status_code in (400, 422)

    final_text = cfg_file.read_text(encoding="utf-8")
    for line in original_text.splitlines():
        if line.lstrip().startswith("//"):
            assert line in final_text
    assert '"image_max_dim": 0' in final_text
