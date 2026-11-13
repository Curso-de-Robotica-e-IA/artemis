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

"""Service helpers for discovering models on Ollama / OpenAI-compatible endpoints
and updating default model configuration in artemis.jsonc / .env.
"""

from __future__ import annotations

import copy
import io
import json
import os
from pathlib import Path
import shutil
from typing import Any, get_args
from urllib.parse import urlsplit

import httpx

from artemis.config.agent import AgentGlobalConfig
from artemis.config.constants import ARTEMIS_CONFIG_FILENAME, ENV_OPENAI_BASE_URL, LLMProvider
from artemis.config.llm import LLMConfig, LLMWithFallback, _expand_default_into_nodes
from artemis.config.paths import (
    CONFIG_DIR,
    get_app_dir,
    get_bundled_config_path,
    get_config_path,
    get_env_file,
    is_source_checkout,
)
from artemis.config.settings import is_placeholder_key, settings
from artemis.core.diagnostics import readiness_engine
from third_party.mobile_use.utils.file import load_jsonc
from third_party.mobile_use.utils.logger import get_logger

try:
    from apps.admin_console.services.model_service import ModelService
except ImportError:
    from admin_console.services.model_service import ModelService

logger = get_logger(__name__)

VALID_PROVIDERS: frozenset[str] = frozenset(get_args(LLMProvider))
LOCAL_COMPAT_PROVIDERS: frozenset[str] = frozenset({"ollama", "vllm", "custom"})

SUPPORTED_PROVIDER_OPTIONS: list[dict[str, Any]] = [
    {
        "id": "ollama",
        "label": "Ollama (Local)",
        "uses_base_url": True,
        "env_key": "OPENAI_BASE_URL",
    },
    {
        "id": "google",
        "label": "Google Gemini",
        "uses_base_url": False,
        "env_key": "GEMINI_API_KEY",
    },
    {
        "id": "openai",
        "label": "OpenAI",
        "uses_base_url": False,
        "env_key": "OPENAI_API_KEY",
    },
    {
        "id": "openrouter",
        "label": "OpenRouter",
        "uses_base_url": False,
        "env_key": "OPEN_ROUTER_API_KEY",
    },
    {
        "id": "anthropic",
        "label": "Anthropic",
        "uses_base_url": False,
        "env_key": "ANTHROPIC_API_KEY",
    },
    {
        "id": "xai",
        "label": "xAI (Grok)",
        "uses_base_url": False,
        "env_key": "XAI_API_KEY",
    },
    {
        "id": "vllm",
        "label": "vLLM (Local)",
        "uses_base_url": True,
        "env_key": "OPENAI_BASE_URL",
    },
    {
        "id": "custom",
        "label": "Custom OpenAI-Compatible",
        "uses_base_url": True,
        "env_key": "OPENAI_BASE_URL",
    },
    {
        "id": "vertexai",
        "label": "Google Vertex AI",
        "uses_base_url": False,
        "env_key": None,
    },
]

PRO_MODE_TARGETS: dict[str, dict[str, Any]] = {
    "object_detector": {
        "key": "object_detector",
        "path": ("nodes", "object_detector"),
        "config_path_label": "nodes.object_detector",
        "label": "Object Detector",
        "supports_fallback": True,
        "requires_vision_note": (
            "Spatial grounding requires a multimodal vision or Embodied Reasoning (ER) capable model."
        ),
        "description": "Locates UI element coordinates and bounding boxes on screen.",
    },
    "hopper": {
        "key": "hopper",
        "path": ("nodes", "hopper"),
        "config_path_label": "nodes.hopper",
        "label": "Hopper",
        "supports_fallback": True,
        "requires_vision_note": None,
        "description": "Utility extraction model used for structured inspection helpers.",
    },
    "step_summarizer": {
        "key": "step_summarizer",
        "path": ("agent", "flash", "step_summarizer"),
        "config_path_label": "agent.flash.step_summarizer",
        "label": "Flash Step Summarizer",
        "supports_fallback": False,
        "requires_vision_note": None,
        "description": "Lightweight model that summarizes Flash visual step transitions.",
    },
    "chunking": {
        "key": "chunking",
        "path": ("agent", "memory", "chunking"),
        "config_path_label": "agent.memory.chunking",
        "label": "Memory Chunking",
        "supports_fallback": False,
        "requires_vision_note": None,
        "description": "Model used for chunk-level StepCapsuleLens history compression.",
    },
}

_PROVIDER_ENV_KEYS: dict[str, tuple[str, ...]] = {
    "google": ("GEMINI_API_KEY", "GOOGLE_API_KEY", "GCP_API_KEY"),
    "openai": ("OPENAI_API_KEY",),
    "anthropic": ("ANTHROPIC_API_KEY",),
    "openrouter": ("OPEN_ROUTER_API_KEY", "OPENROUTER_API_KEY"),
    "xai": ("XAI_API_KEY",),
}


def normalize_custom_base_url(raw_url: str | None) -> str:
    """Validate and normalize an HTTP(S) custom OpenAI / Ollama base URL."""
    cleaned = (raw_url or "").strip().rstrip("/")
    if not cleaned or is_placeholder_key(cleaned):
        raise ValueError(
            "Custom endpoint URL is empty. Set OPENAI_BASE_URL or enter an HTTP(S) URL "
            "(for example http://localhost:11434/v1)."
        )

    parsed = urlsplit(cleaned)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        raise ValueError(
            f"Invalid custom endpoint URL '{raw_url}'. Expected an http:// or https:// URL."
        )
    return cleaned


def derive_ollama_tags_url(base_url: str) -> str:
    """Derive Ollama's native /api/tags URL from an OpenAI-style or root base URL."""
    clean = base_url.rstrip("/")
    if clean.lower().endswith("/v1"):
        root = clean[:-3].rstrip("/")
    else:
        root = clean
    return f"{root}/api/tags"


def resolve_effective_base_url(override_base_url: str | None = None) -> str:
    """Resolve the active custom endpoint URL from an explicit override or settings/.env."""
    raw = (
        override_base_url
        if override_base_url is not None and override_base_url.strip()
        else (settings.OPENAI_BASE_URL or os.environ.get(ENV_OPENAI_BASE_URL))
    )
    return normalize_custom_base_url(raw)


def _resolve_auth_headers() -> dict[str, str]:
    """Return Bearer header when a non-placeholder OPENAI_API_KEY is configured."""
    secret = settings.get_api_key("openai")
    raw_key = secret.get_secret_value() if secret else os.environ.get("OPENAI_API_KEY", "")
    clean_key = raw_key.strip()
    if not clean_key or is_placeholder_key(clean_key) or clean_key.upper() == "EMPTY":
        return {}
    return {"Authorization": f"Bearer {clean_key}"}


def _parse_openai_models_payload(payload: Any) -> tuple[list[dict[str, Any]], bool]:
    """Parse OpenAI `/v1/models` response into normalized model records."""
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return [], False

    models: list[dict[str, Any]] = []
    looks_like_ollama = False
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            continue
        owned_by_raw = entry.get("owned_by")
        owned_by = str(owned_by_raw).strip() if owned_by_raw else None
        if owned_by == "library":
            looks_like_ollama = True
        models.append(
            {
                "id": model_id,
                "name": model_id,
                "owned_by": owned_by,
                "parameter_size": None,
                "quantization_level": None,
                "size_bytes": None,
            }
        )
    return models, looks_like_ollama


def _parse_ollama_tags_payload(payload: Any) -> list[dict[str, Any]]:
    """Parse native Ollama `/api/tags` response into normalized model records."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return []

    models: list[dict[str, Any]] = []
    for entry in payload["models"]:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("model") or entry.get("name") or "").strip()
        if not model_id:
            continue
        details = entry.get("details") if isinstance(entry.get("details"), dict) else {}
        param_size = details.get("parameter_size")
        quant = details.get("quantization_level")
        size_val = entry.get("size")
        models.append(
            {
                "id": model_id,
                "name": str(entry.get("name") or model_id).strip(),
                "owned_by": "ollama",
                "parameter_size": str(param_size).strip() if param_size else None,
                "quantization_level": str(quant).strip() if quant else None,
                "size_bytes": int(size_val) if isinstance(size_val, int) else None,
            }
        )
    return models


def _merge_model_records(
    primary: list[dict[str, Any]],
    enrichment: list[dict[str, Any]],
) -> list[dict[str, Any]]:
    """Merge enrichment metadata (e.g. parameter size from /api/tags) by model ID."""
    by_id: dict[str, dict[str, Any]] = {}
    for item in primary:
        by_id[item["id"]] = dict(item)

    for extra in enrichment:
        existing = by_id.get(extra["id"])
        if existing is None:
            by_id[extra["id"]] = dict(extra)
        else:
            for field in ("parameter_size", "quantization_level", "size_bytes", "owned_by"):
                if existing.get(field) is None and extra.get(field) is not None:
                    existing[field] = extra[field]

    return sorted(by_id.values(), key=lambda item: item["id"].lower())


async def list_custom_models(
    base_url: str | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Fetch available models from an OpenAI-compatible or Ollama endpoint."""
    clean_base = resolve_effective_base_url(base_url)
    headers = _resolve_auth_headers()
    tags_url = derive_ollama_tags_url(clean_base)

    candidate_openai_urls = [f"{clean_base}/models"]
    if not clean_base.lower().endswith("/v1"):
        candidate_openai_urls.append(f"{clean_base}/v1/models")

    models: list[dict[str, Any]] = []
    endpoint_used: str | None = None
    source: str | None = None
    last_error: str | None = None

    async with httpx.AsyncClient(timeout=timeout) as client:
        for url in candidate_openai_urls:
            try:
                resp = await client.get(url, headers=headers)
            except httpx.TimeoutException as exc:
                raise TimeoutError(
                    f"Timed out connecting to model endpoint at {url} after {timeout:.0f}s."
                ) from exc
            except httpx.RequestError as exc:
                raise ConnectionError(
                    f"Could not connect to model endpoint at {url}: {exc}"
                ) from exc

            if resp.status_code == 200:
                try:
                    parsed_models, looks_like_ollama = _parse_openai_models_payload(resp.json())
                except ValueError as exc:
                    last_error = f"Endpoint {url} returned invalid JSON: {exc}"
                    continue

                if parsed_models:
                    models = parsed_models
                    endpoint_used = url
                    source = "ollama" if looks_like_ollama else "openai"
                    if looks_like_ollama:
                        try:
                            tags_resp = await client.get(tags_url, headers=headers)
                            if tags_resp.status_code == 200:
                                tag_models = _parse_ollama_tags_payload(tags_resp.json())
                                if tag_models:
                                    models = _merge_model_records(models, tag_models)
                        except (httpx.HTTPError, ValueError):
                            # Enrichment from /api/tags is best-effort; /models already succeeded.
                            pass
                    break
                last_error = f"Endpoint {url} returned an empty model list."
            else:
                last_error = f"HTTP {resp.status_code} from {url}"

        if not models:
            try:
                tags_resp = await client.get(tags_url, headers=headers)
                if tags_resp.status_code == 200:
                    tag_models = _parse_ollama_tags_payload(tags_resp.json())
                    if tag_models:
                        models = _merge_model_records(tag_models, [])
                        endpoint_used = tags_url
                        source = "ollama"
                    elif last_error is None:
                        last_error = f"No models found at {tags_url}."
                elif last_error is None:
                    last_error = f"HTTP {tags_resp.status_code} from {tags_url}"
            except httpx.TimeoutException as exc:
                raise TimeoutError(
                    f"Timed out connecting to Ollama endpoint at {tags_url} after {timeout:.0f}s."
                ) from exc
            except httpx.RequestError as exc:
                if last_error is None:
                    raise ConnectionError(
                        f"Could not connect to Ollama endpoint at {tags_url}: {exc}"
                    ) from exc
            except ValueError as exc:
                last_error = f"Endpoint {tags_url} returned invalid JSON: {exc}"

    if not models:
        if last_error and "empty model list" in last_error.lower():
            return {
                "base_url": clean_base,
                "endpoint_used": candidate_openai_urls[0],
                "source": source or "openai",
                "models": [],
            }
        raise ValueError(
            last_error
            or f"No models could be listed from {clean_base}. "
            "Check that the server is running and has pulled models."
        )

    return {
        "base_url": clean_base,
        "endpoint_used": endpoint_used or candidate_openai_urls[0],
        "source": source or "openai",
        "models": sorted(models, key=lambda item: item["id"].lower()),
    }


def _get_provider_api_key(provider: str) -> str | None:
    """Resolve a non-placeholder API key for the requested provider."""
    secret = settings.get_api_key(provider)
    if secret:
        raw = secret.get_secret_value().strip()
        if raw and not is_placeholder_key(raw):
            return raw
    for env_key in _PROVIDER_ENV_KEYS.get(provider, ()):
        val = (os.environ.get(env_key) or "").strip()
        if val and not is_placeholder_key(val):
            return val
    return None


def _load_active_artemis_jsonc() -> dict[str, Any]:
    """Load the active artemis.jsonc dictionary if available."""
    for resolver in (get_config_path, get_bundled_config_path):
        try:
            path = resolver(ARTEMIS_CONFIG_FILENAME)
            if path and path.exists():
                with open(path, encoding="utf-8") as f:
                    parsed = load_jsonc(f)
                if isinstance(parsed, dict):
                    return parsed
        except (FileNotFoundError, OSError, ValueError):
            continue
    return {}


def _infer_target_effective_provider(
    target_key: str,
    explicit_provider: str | None,
    explicit_model: str | None,
    default_provider: str | None,
) -> str | None:
    """Resolve active provider matching runtime loader semantics for a Pro Mode target."""
    if not explicit_model:
        return default_provider
    if explicit_provider:
        return explicit_provider
    if target_key in ("object_detector", "hopper"):
        return default_provider
    if target_key == "chunking":
        return "google"
    if "gemini" in explicit_model.lower():
        return "google"
    return default_provider or "google"


def build_pro_targets_state(parsed_config: dict[str, Any]) -> list[dict[str, Any]]:
    """Build current resolved state for all Pro Mode configurable targets."""
    default_cfg = parsed_config.get("default")
    if not isinstance(default_cfg, dict):
        default_cfg = {}
    raw_def_provider = default_cfg.get("provider")
    raw_def_model = default_cfg.get("model")
    default_provider = str(raw_def_provider).strip() if raw_def_provider else None
    default_model = str(raw_def_model).strip() if raw_def_model else None

    targets_state: list[dict[str, Any]] = []
    for target_key, spec in PRO_MODE_TARGETS.items():
        node_obj: Any = parsed_config
        for seg in spec["path"]:
            node_obj = node_obj.get(seg) if isinstance(node_obj, dict) else None
        target_dict = node_obj if isinstance(node_obj, dict) else {}

        raw_exp_provider = target_dict.get("provider")
        raw_exp_model = target_dict.get("model")
        explicit_provider = (
            str(raw_exp_provider).strip()
            if isinstance(raw_exp_provider, str) and raw_exp_provider.strip()
            else None
        )
        explicit_model = (
            str(raw_exp_model).strip()
            if isinstance(raw_exp_model, str) and raw_exp_model.strip()
            else None
        )
        raw_fb = target_dict.get("fallback")
        explicit_fallback = dict(raw_fb) if isinstance(raw_fb, dict) else None

        is_inheriting_default = not bool(explicit_model)
        effective_model = explicit_model if not is_inheriting_default else default_model
        effective_provider = _infer_target_effective_provider(
            target_key, explicit_provider, explicit_model, default_provider
        )
        active_badge = (
            "Inheriting Default"
            if is_inheriting_default
            else f"{effective_provider or 'default'} / {effective_model}"
        )

        targets_state.append(
            {
                "key": target_key,
                "label": spec["label"],
                "config_path": spec["config_path_label"],
                "description": spec["description"],
                "supports_fallback": spec["supports_fallback"],
                "requires_vision_note": spec["requires_vision_note"],
                "explicit_provider": explicit_provider,
                "explicit_model": explicit_model,
                "explicit_fallback": explicit_fallback,
                "effective_provider": effective_provider,
                "effective_model": effective_model,
                "is_inheriting_default": is_inheriting_default,
                "active_badge": active_badge,
            }
        )

    return targets_state


def _collect_config_models_for_provider(
    provider: str, parsed_config: dict[str, Any] | None = None
) -> list[dict[str, Any]]:
    """Collect fallback model IDs for ``provider`` from artemis.jsonc presets and nodes."""
    cfg = parsed_config if isinstance(parsed_config, dict) else _load_active_artemis_jsonc()
    clean_provider = provider.strip().lower()
    seen: dict[str, dict[str, Any]] = {}

    def _add_candidate(model_id: Any, note: str) -> None:
        if not isinstance(model_id, str):
            return
        clean_id = model_id.strip()
        if not clean_id:
            return
        if clean_id not in seen:
            seen[clean_id] = {
                "id": clean_id,
                "name": clean_id,
                "provider": clean_provider,
                "owned_by": note,
                "source": "config_fallback",
            }

    def _inspect_block(block: Any, note: str, implicit_provider: str | None = None) -> None:
        if not isinstance(block, dict):
            return
        block_provider = (str(block.get("provider") or implicit_provider or "")).strip().lower()
        if block_provider == clean_provider:
            _add_candidate(block.get("model"), note)
        fb = block.get("fallback")
        if isinstance(fb, dict):
            _inspect_block(fb, f"{note} fallback", implicit_provider=block_provider or None)

    default_cfg = cfg.get("default")
    _inspect_block(default_cfg, "default")
    default_provider = (
        str(default_cfg.get("provider") or "").strip().lower()
        if isinstance(default_cfg, dict)
        else ""
    )

    presets = cfg.get("presets")
    if isinstance(presets, dict):
        for preset_name, preset_cfg in presets.items():
            _inspect_block(preset_cfg, f"preset:{preset_name}")

    nodes = cfg.get("nodes")
    if isinstance(nodes, dict):
        for node_name, node_cfg in nodes.items():
            _inspect_block(node_cfg, f"node:{node_name}", implicit_provider=default_provider)

    agent = cfg.get("agent")
    if isinstance(agent, dict):
        flash_ss = (agent.get("flash") or {}).get("step_summarizer")
        if isinstance(flash_ss, dict):
            inferred = (
                "google"
                if "gemini" in str(flash_ss.get("model") or "").lower()
                else default_provider
            )
            _inspect_block(flash_ss, "agent.flash.step_summarizer", implicit_provider=inferred)
        chunking = (agent.get("memory") or {}).get("chunking")
        if isinstance(chunking, dict):
            _inspect_block(chunking, "agent.memory.chunking", implicit_provider="google")

    return sorted(seen.values(), key=lambda item: item["id"].lower())


def _parse_google_models_payload(payload: Any) -> list[dict[str, Any]]:
    """Parse Google Gemini `/v1beta/models` response into normalized model records."""
    if not isinstance(payload, dict) or not isinstance(payload.get("models"), list):
        return []
    models: list[dict[str, Any]] = []
    for entry in payload["models"]:
        if not isinstance(entry, dict):
            continue
        raw_name = str(entry.get("name") or "").strip()
        if not raw_name:
            continue
        methods = entry.get("supportedGenerationMethods")
        if isinstance(methods, list) and "generateContent" not in methods:
            continue
        model_id = raw_name.removeprefix("models/")
        display_name = str(entry.get("displayName") or model_id).strip()
        models.append(
            {
                "id": model_id,
                "name": display_name,
                "provider": "google",
                "owned_by": "google",
            }
        )
    return sorted(models, key=lambda item: item["id"].lower())


def _parse_standard_provider_models(provider: str, payload: Any) -> list[dict[str, Any]]:
    """Parse OpenAI, OpenRouter, xAI, or Anthropic model list responses."""
    if provider == "google":
        return _parse_google_models_payload(payload)
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return []
    models: list[dict[str, Any]] = []
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            continue
        model_id = str(entry.get("id") or "").strip()
        if not model_id:
            continue
        display_name = str(entry.get("display_name") or entry.get("name") or model_id).strip()
        models.append(
            {
                "id": model_id,
                "name": display_name,
                "provider": provider,
                "owned_by": str(entry.get("owned_by") or provider),
            }
        )
    return sorted(models, key=lambda item: item["id"].lower())


async def _query_external_provider_models(
    provider: str,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Query an external provider's model listing API with graceful config fallback."""
    fallback_models = _collect_config_models_for_provider(provider)

    def _fallback_response(warning: str, endpoint_used: str | None = None) -> dict[str, Any]:
        return {
            "provider": provider,
            "base_url": None,
            "endpoint_used": endpoint_used or "config:artemis.jsonc",
            "source": "config_fallback",
            "fallback_used": True,
            "warning": warning,
            "models": fallback_models,
        }

    api_key = _get_provider_api_key(provider)
    if not api_key:
        env_hint = ", ".join(_PROVIDER_ENV_KEYS.get(provider, ())) or "provider credentials"
        return _fallback_response(
            f"Missing {env_hint}; showing preset and config models from artemis.jsonc."
        )

    endpoint_specs: dict[str, tuple[str, dict[str, str]]] = {
        "google": (
            "https://generativelanguage.googleapis.com/v1beta/models?pageSize=200",
            {"x-goog-api-key": api_key},
        ),
        "openai": (
            "https://api.openai.com/v1/models",
            {"Authorization": f"Bearer {api_key}"},
        ),
        "openrouter": (
            "https://openrouter.ai/api/v1/models",
            {"Authorization": f"Bearer {api_key}"},
        ),
        "anthropic": (
            "https://api.anthropic.com/v1/models",
            {"x-api-key": api_key, "anthropic-version": "2023-06-01"},
        ),
        "xai": (
            "https://api.x.ai/v1/models",
            {"Authorization": f"Bearer {api_key}"},
        ),
    }
    spec = endpoint_specs.get(provider)
    if spec is None:
        return _fallback_response(
            f"Live model listing is not available for '{provider}'; showing config models."
        )

    url, headers = spec
    try:
        async with httpx.AsyncClient(timeout=timeout) as client:
            resp = await client.get(url, headers=headers)
    except (httpx.HTTPError, TimeoutError, ConnectionError) as exc:
        return _fallback_response(
            f"Could not query {provider} models ({exc}); showing preset and config models.",
            endpoint_used=url,
        )

    if resp.status_code != 200:
        return _fallback_response(
            f"{provider} models endpoint returned HTTP {resp.status_code}; showing config models.",
            endpoint_used=url,
        )

    try:
        parsed_models = _parse_standard_provider_models(provider, resp.json())
    except ValueError as exc:
        return _fallback_response(
            f"{provider} models endpoint returned invalid JSON ({exc}); showing config models.",
            endpoint_used=url,
        )

    if not parsed_models:
        return _fallback_response(
            f"{provider} models endpoint returned no compatible models; showing config models.",
            endpoint_used=url,
        )

    return {
        "provider": provider,
        "base_url": None,
        "endpoint_used": url,
        "source": provider,
        "fallback_used": False,
        "warning": None,
        "models": parsed_models,
    }


async def list_models_for_provider(
    provider: str | None = None,
    base_url: str | None = None,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """List models for the requested provider, routing local vs cloud APIs cleanly."""
    if provider is None or not provider.strip():
        return await list_custom_models(base_url=base_url, timeout=timeout)

    clean_provider = provider.strip().lower()
    if clean_provider not in VALID_PROVIDERS:
        allowed = ", ".join(sorted(VALID_PROVIDERS))
        raise ValueError(f"Unsupported provider '{provider}'. Expected one of: {allowed}.")

    if clean_provider in LOCAL_COMPAT_PROVIDERS or (
        clean_provider == "openai" and base_url is not None and bool(base_url.strip())
    ):
        result = await list_custom_models(base_url=base_url, timeout=timeout)
        return {
            **result,
            "provider": clean_provider,
            "fallback_used": False,
            "warning": None,
        }

    return await _query_external_provider_models(clean_provider, timeout=timeout)


def _skip_ws_and_comments(text: str, idx: int) -> int:
    """Advance index past whitespace and JSONC // or /* */ comments."""
    n = len(text)
    while idx < n:
        ch = text[idx]
        if ch in " \t\r\n":
            idx += 1
            continue
        if ch == "/" and idx + 1 < n:
            nxt = text[idx + 1]
            if nxt == "/":
                idx += 2
                while idx < n and text[idx] != "\n":
                    idx += 1
                continue
            if nxt == "*":
                end = text.find("*/", idx + 2)
                if end == -1:
                    raise ValueError("Unterminated block comment in JSONC.")
                idx = end + 2
                continue
        break
    return idx


def _scan_string_end(text: str, idx: int) -> int:
    """Return index immediately after the closing quote of a JSON string at idx."""
    n = len(text)
    if idx >= n or text[idx] != '"':
        raise ValueError("Expected opening quote for JSON string.")
    idx += 1
    while idx < n:
        ch = text[idx]
        if ch == "\\":
            idx += 2
            continue
        if ch == '"':
            return idx + 1
        idx += 1
    raise ValueError("Unterminated string literal in JSONC.")


def _scan_matching_delimiter(text: str, open_idx: int, open_ch: str, close_ch: str) -> int:
    """Find matching closing brace/bracket index while ignoring strings and comments."""
    n = len(text)
    depth = 0
    idx = open_idx
    while idx < n:
        ch = text[idx]
        if ch == '"':
            idx = _scan_string_end(text, idx)
            continue
        if ch == "/" and idx + 1 < n and text[idx + 1] in ("/", "*"):
            idx = _skip_ws_and_comments(text, idx)
            continue
        if ch == open_ch:
            depth += 1
        elif ch == close_ch:
            depth -= 1
            if depth == 0:
                return idx
        idx += 1
    raise ValueError(f"Unmatched '{open_ch}' in JSONC.")


def _find_object_entry_records(text: str, open_idx: int, close_idx: int) -> list[dict[str, Any]]:
    """Return ordered property span records for direct keys inside a JSONC object."""
    records: list[dict[str, Any]] = []
    idx = _skip_ws_and_comments(text, open_idx + 1)

    while idx < close_idx:
        if text[idx] == "}":
            break
        if text[idx] != '"':
            raise ValueError(f"Expected property key at offset {idx} in JSONC.")
        key_start = idx
        key_end = _scan_string_end(text, key_start)
        key = json.loads(text[key_start:key_end])

        idx = _skip_ws_and_comments(text, key_end)
        if idx >= close_idx or text[idx] != ":":
            raise ValueError(f"Expected ':' after key '{key}' in JSONC.")
        val_start = _skip_ws_and_comments(text, idx + 1)
        if val_start >= close_idx:
            raise ValueError(f"Missing value for key '{key}' in JSONC.")

        first = text[val_start]
        if first == "{":
            val_end = _scan_matching_delimiter(text, val_start, "{", "}") + 1
        elif first == "[":
            val_end = _scan_matching_delimiter(text, val_start, "[", "]") + 1
        elif first == '"':
            val_end = _scan_string_end(text, val_start)
        else:
            val_end = val_start
            while val_end < close_idx and text[val_end] not in ",} \t\r\n/":
                val_end += 1

        idx = _skip_ws_and_comments(text, val_end)
        comma_pos: int | None = None
        if idx < close_idx and text[idx] == ",":
            comma_pos = idx
            idx = _skip_ws_and_comments(text, idx + 1)

        records.append(
            {
                "key": key,
                "key_start": key_start,
                "val_start": val_start,
                "val_end": val_end,
                "comma_pos": comma_pos,
            }
        )

    return records


def _find_object_entries(text: str, open_idx: int, close_idx: int) -> dict[str, tuple[int, int]]:
    """Map direct keys in a JSONC object to their raw (value_start, value_end) spans."""
    return {
        record["key"]: (record["val_start"], record["val_end"])
        for record in _find_object_entry_records(text, open_idx, close_idx)
    }


def _line_indentation_at(text: str, pos: int) -> str:
    """Return leading horizontal whitespace on the line containing ``pos``."""
    line_start = text.rfind("\n", 0, pos) + 1
    indent = ""
    for ch in text[line_start:pos]:
        if ch in " \t":
            indent += ch
        else:
            break
    return indent


def _format_jsonc_value(val: Any, indent: str) -> str:
    """Render a scalar or nested dict cleanly for insertion into formatted JSONC."""
    if isinstance(val, dict):
        if not val:
            return "{}"
        inner_indent = f"{indent}  "
        items = ",\n".join(
            f"{inner_indent}{json.dumps(k)}: {_format_jsonc_value(v, inner_indent)}"
            for k, v in val.items()
        )
        return "{\n" + items + f"\n{indent}" + "}"
    return json.dumps(val)


def _collect_object_field_updates(
    text: str,
    obj_open: int,
    obj_close: int,
    updates: dict[str, Any],
) -> list[tuple[int, int, str]]:
    """Collect text replacement spans to upsert string or dict fields in a JSONC object."""
    entries = _find_object_entries(text, obj_open, obj_close)
    edits: list[tuple[int, int, str]] = []
    missing_fields: list[tuple[str, Any]] = []

    for key, new_val in updates.items():
        if key in entries:
            v_start, v_end = entries[key]
            if (
                isinstance(new_val, dict)
                and v_start < v_end
                and text[v_start] == "{"
                and text[v_end - 1] == "}"
            ):
                edits.extend(_collect_object_field_updates(text, v_start, v_end - 1, new_val))
            else:
                indent = _line_indentation_at(text, v_start)
                edits.append((v_start, v_end, _format_jsonc_value(new_val, indent)))
        else:
            missing_fields.append((key, new_val))

    if missing_fields:
        has_existing = bool(entries)
        base_indent = _line_indentation_at(text, obj_open)
        child_indent = f"{base_indent}  "
        is_multiline = "\n" in text[obj_open:obj_close]

        if is_multiline:
            insertion = "".join(
                f"\n{child_indent}{json.dumps(k)}: {_format_jsonc_value(v, child_indent)},"
                for k, v in missing_fields
            )
            if not has_existing and insertion.endswith(","):
                insertion = insertion[:-1]
        elif has_existing:
            pairs = ", ".join(f"{json.dumps(k)}: {json.dumps(v)}" for k, v in missing_fields)
            insertion = f" {pairs},"
        else:
            formatted = ",\n".join(
                f"{child_indent}{json.dumps(k)}: {_format_jsonc_value(v, child_indent)}"
                for k, v in missing_fields
            )
            insertion = f"\n{formatted}\n{base_indent}"

        edits.append((obj_open + 1, obj_open + 1, insertion))

    return edits


def _collect_object_string_updates(
    text: str,
    obj_open: int,
    obj_close: int,
    updates: dict[str, str],
) -> list[tuple[int, int, str]]:
    """Collect text replacement spans to set string fields inside a JSONC object."""
    return _collect_object_field_updates(text, obj_open, obj_close, updates)


def _apply_text_edits(text: str, edits: list[tuple[int, int, str]]) -> str:
    """Apply non-overlapping ``(start, end, replacement)`` edits from right to left."""
    updated = text
    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        updated = f"{updated[:start]}{replacement}{updated[end:]}"
    return updated


def _navigate_jsonc_object(raw_text: str, path: tuple[str, ...]) -> tuple[int, int]:
    """Locate ``(open_brace_idx, close_brace_idx)`` for a nested JSONC object path."""
    root_open = _skip_ws_and_comments(raw_text, 0)
    if root_open >= len(raw_text) or raw_text[root_open] != "{":
        raise ValueError("Expected root JSONC object in artemis.jsonc.")
    cur_open = root_open
    cur_close = _scan_matching_delimiter(raw_text, cur_open, "{", "}")

    for depth_idx, segment in enumerate(path):
        entries = _find_object_entries(raw_text, cur_open, cur_close)
        if segment not in entries:
            joined = ".".join(path[: depth_idx + 1])
            raise ValueError(f"artemis.jsonc is missing expected section '{joined}'.")
        val_start, val_end = entries[segment]
        if raw_text[val_start] != "{" or raw_text[val_end - 1] != "}":
            joined = ".".join(path[: depth_idx + 1])
            raise ValueError(f"artemis.jsonc section '{joined}' must be a JSON object.")
        cur_open, cur_close = val_start, val_end - 1

    return cur_open, cur_close


def _remove_object_keys_in_jsonc_text(
    text: str,
    obj_open: int,
    obj_close: int,
    keys_to_remove: set[str],
) -> str:
    """Remove direct keys from a JSONC object while preserving comments and valid commas."""
    records = _find_object_entry_records(text, obj_open, obj_close)
    if not records:
        return text

    kept = [rec for rec in records if rec["key"] not in keys_to_remove]
    removed = [rec for rec in records if rec["key"] in keys_to_remove]
    if not removed:
        return text

    edits: list[tuple[int, int, str]] = []
    if kept and kept[-1]["comma_pos"] is not None:
        cpos = kept[-1]["comma_pos"]
        edits.append((cpos, cpos + 1, ""))

    for rec in removed:
        line_start = text.rfind("\n", 0, rec["key_start"])
        before_on_line = text[line_start + 1 : rec["key_start"]]
        tail_start = rec["comma_pos"] + 1 if rec["comma_pos"] is not None else rec["val_end"]
        next_nl = text.find("\n", tail_start)
        owns_line = False
        if next_nl != -1 and before_on_line.strip(" \t") == "":
            after_tail = text[tail_start:next_nl].lstrip(" \t")
            if after_tail == "" or after_tail.startswith("//"):
                owns_line = True

        if owns_line:
            edits.append((line_start + 1, next_nl + 1, ""))
        else:
            del_start = rec["key_start"]
            del_end = tail_start
            if rec["comma_pos"] is not None:
                while del_end < obj_close and text[del_end] in " \t":
                    del_end += 1
            else:
                while del_start > obj_open + 1 and text[del_start - 1] in " \t":
                    del_start -= 1
            edits.append((del_start, del_end, ""))

    updated = _apply_text_edits(text, edits)
    if not kept:
        # Collapse whitespace-only empty objects while leaving any comments intact.
        new_close = _scan_matching_delimiter(updated, obj_open, "{", "}")
        inner = updated[obj_open + 1 : new_close]
        if inner and inner.strip() == "":
            updated = f"{updated[: obj_open + 1]}{updated[new_close:]}"
    return updated


def update_default_model_in_jsonc_text(
    raw_text: str,
    provider: str,
    model: str,
    update_fallback: bool = True,
) -> str:
    """Update default.provider/model (and optional fallback) while preserving JSONC comments."""
    default_open, default_close = _navigate_jsonc_object(raw_text, ("default",))
    updates: dict[str, Any] = {"provider": provider, "model": model}
    if update_fallback:
        updates["fallback"] = {"provider": provider, "model": model}

    edits = _collect_object_field_updates(raw_text, default_open, default_close, updates)
    return _apply_text_edits(raw_text, edits)


def update_target_model_in_jsonc_text(
    raw_text: str,
    target_key: str,
    provider: str,
    model: str,
) -> str:
    """Set explicit provider/model (and mirrored fallback where supported) on a Pro target."""
    if target_key not in PRO_MODE_TARGETS:
        allowed = ", ".join(PRO_MODE_TARGETS)
        raise ValueError(f"Unknown model target '{target_key}'. Expected one of: {allowed}.")
    spec = PRO_MODE_TARGETS[target_key]
    target_open, target_close = _navigate_jsonc_object(raw_text, spec["path"])
    updates: dict[str, Any] = {"provider": provider, "model": model}
    if spec["supports_fallback"]:
        updates["fallback"] = {"provider": provider, "model": model}
    edits = _collect_object_field_updates(raw_text, target_open, target_close, updates)
    return _apply_text_edits(raw_text, edits)


def reset_target_model_in_jsonc_text(raw_text: str, target_key: str) -> str:
    """Clear explicit model/provider/fallback overrides on a Pro Mode target."""
    if target_key not in PRO_MODE_TARGETS:
        allowed = ", ".join(PRO_MODE_TARGETS)
        raise ValueError(f"Unknown model target '{target_key}'. Expected one of: {allowed}.")
    spec = PRO_MODE_TARGETS[target_key]
    target_open, target_close = _navigate_jsonc_object(raw_text, spec["path"])
    return _remove_object_keys_in_jsonc_text(
        raw_text, target_open, target_close, {"model", "provider", "fallback"}
    )


def resolve_writable_artemis_config_path() -> Path:
    """Resolve writable artemis.jsonc path, cloning bundled template if only immutable exists."""
    bundled = get_bundled_config_path(ARTEMIS_CONFIG_FILENAME)
    try:
        resolved = get_config_path(ARTEMIS_CONFIG_FILENAME)
    except FileNotFoundError:
        resolved = None

    if resolved is not None and (bundled is None or resolved.resolve() != bundled.resolve()):
        return resolved

    target = (
        CONFIG_DIR / ARTEMIS_CONFIG_FILENAME
        if is_source_checkout()
        else get_app_dir() / ARTEMIS_CONFIG_FILENAME
    )
    target.parent.mkdir(parents=True, exist_ok=True)
    if not target.exists():
        if bundled is None or not bundled.exists():
            raise FileNotFoundError(
                f"Cannot initialize {ARTEMIS_CONFIG_FILENAME}: no template config found."
            )
        shutil.copy2(bundled, target)
    return target


def persist_openai_base_url(base_url: str, persist_to_env: bool = True) -> str:
    """Set OPENAI_BASE_URL in settings, process env, and optionally the active .env file."""
    clean_url = normalize_custom_base_url(base_url)
    settings.OPENAI_BASE_URL = clean_url
    os.environ[ENV_OPENAI_BASE_URL] = clean_url

    if persist_to_env:
        env_file = get_env_file()
        env_file.parent.mkdir(parents=True, exist_ok=True)
        lines = env_file.read_text(encoding="utf-8").splitlines() if env_file.exists() else []
        new_lines: list[str] = []
        replaced = False
        for line in lines:
            stripped = line.lstrip()
            if (
                stripped.startswith(f"{ENV_OPENAI_BASE_URL}=")
                or stripped.startswith(f"#{ENV_OPENAI_BASE_URL}=")
                or stripped.startswith(f"# {ENV_OPENAI_BASE_URL}=")
            ):
                if not replaced:
                    new_lines.append(f"{ENV_OPENAI_BASE_URL}={clean_url}")
                    replaced = True
            else:
                new_lines.append(line)
        if not replaced:
            new_lines.append(f"{ENV_OPENAI_BASE_URL}={clean_url}")
        env_file.write_text("\n".join(new_lines) + "\n", encoding="utf-8")

    return clean_url


def _validate_and_write_artemis_jsonc(
    config_path: Path,
    updated_text: str,
) -> dict[str, Any]:
    """Validate updated artemis.jsonc structure, write atomically, and clear caches."""
    parsed = load_jsonc(io.StringIO(updated_text))
    if not isinstance(parsed, dict) or not isinstance(parsed.get("default"), dict):
        raise ValueError("Updated artemis.jsonc failed structural validation.")
    LLMConfig.model_validate(_expand_default_into_nodes(copy.deepcopy(parsed)))
    if isinstance(parsed.get("agent"), dict):
        AgentGlobalConfig.model_validate(parsed["agent"])

    config_path.write_text(updated_text, encoding="utf-8")
    ModelService.invalidate_cache()
    readiness_engine.invalidate_cache()
    return parsed


def update_default_model_config(
    provider: str,
    model: str,
    update_fallback: bool = True,
    base_url: str | None = None,
    persist_base_url: bool = False,
) -> dict[str, Any]:
    """Validate and persist default model selection in artemis.jsonc (and optional base URL)."""
    clean_provider = provider.strip().lower()
    if clean_provider not in VALID_PROVIDERS:
        allowed = ", ".join(sorted(VALID_PROVIDERS))
        raise ValueError(f"Unsupported provider '{provider}'. Expected one of: {allowed}.")

    clean_model = model.strip()
    if not clean_model:
        raise ValueError("Model name cannot be empty.")

    saved_base_url: str | None = None
    if base_url is not None and base_url.strip():
        saved_base_url = persist_openai_base_url(base_url, persist_to_env=persist_base_url)

    config_path = resolve_writable_artemis_config_path()
    raw_text = config_path.read_text(encoding="utf-8")
    updated_text = update_default_model_in_jsonc_text(
        raw_text,
        provider=clean_provider,
        model=clean_model,
        update_fallback=update_fallback,
    )
    parsed = _validate_and_write_artemis_jsonc(config_path, updated_text)

    return {
        "config_path": str(config_path),
        "config_content": updated_text,
        "default_model": parsed["default"],
        "base_url": saved_base_url,
    }


def update_pro_target_model_config(
    target: str,
    provider: str,
    model: str,
    base_url: str | None = None,
    persist_base_url: bool = False,
) -> dict[str, Any]:
    """Validate and persist an explicit provider/model override on a Pro Mode target."""
    clean_target = target.strip().lower()
    if clean_target not in PRO_MODE_TARGETS:
        allowed = ", ".join(PRO_MODE_TARGETS)
        raise ValueError(f"Unknown model target '{target}'. Expected one of: {allowed}.")

    clean_provider = provider.strip().lower()
    if clean_provider not in VALID_PROVIDERS:
        allowed = ", ".join(sorted(VALID_PROVIDERS))
        raise ValueError(f"Unsupported provider '{provider}'. Expected one of: {allowed}.")

    clean_model = model.strip()
    if not clean_model:
        raise ValueError("Model name cannot be empty.")

    saved_base_url: str | None = None
    if base_url is not None and base_url.strip():
        saved_base_url = persist_openai_base_url(base_url, persist_to_env=persist_base_url)

    config_path = resolve_writable_artemis_config_path()
    raw_text = config_path.read_text(encoding="utf-8")
    updated_text = update_target_model_in_jsonc_text(
        raw_text,
        target_key=clean_target,
        provider=clean_provider,
        model=clean_model,
    )
    parsed = _validate_and_write_artemis_jsonc(config_path, updated_text)
    pro_targets = build_pro_targets_state(parsed)
    target_state = next(item for item in pro_targets if item["key"] == clean_target)

    return {
        "config_path": str(config_path),
        "config_content": updated_text,
        "target": target_state,
        "pro_targets": pro_targets,
        "base_url": saved_base_url,
    }


def reset_pro_target_model_config(target: str) -> dict[str, Any]:
    """Clear explicit model/provider/fallback on a Pro Mode target to inherit default."""
    clean_target = target.strip().lower()
    if clean_target not in PRO_MODE_TARGETS:
        allowed = ", ".join(PRO_MODE_TARGETS)
        raise ValueError(f"Unknown model target '{target}'. Expected one of: {allowed}.")

    config_path = resolve_writable_artemis_config_path()
    raw_text = config_path.read_text(encoding="utf-8")
    updated_text = reset_target_model_in_jsonc_text(raw_text, target_key=clean_target)
    parsed = _validate_and_write_artemis_jsonc(config_path, updated_text)
    pro_targets = build_pro_targets_state(parsed)
    target_state = next(item for item in pro_targets if item["key"] == clean_target)

    return {
        "config_path": str(config_path),
        "config_content": updated_text,
        "target": target_state,
        "pro_targets": pro_targets,
    }
