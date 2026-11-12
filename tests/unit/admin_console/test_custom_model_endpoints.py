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
