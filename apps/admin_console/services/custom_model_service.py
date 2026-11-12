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

import io
import json
import os
from pathlib import Path
import shutil
from typing import Any, get_args
from urllib.parse import urlsplit

import httpx

from artemis.config.constants import ARTEMIS_CONFIG_FILENAME, ENV_OPENAI_BASE_URL, LLMProvider
from artemis.config.llm import LLMWithFallback
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


def _find_object_entries(text: str, open_idx: int, close_idx: int) -> dict[str, tuple[int, int]]:
    """Map direct keys in a JSONC object to their raw (value_start, value_end) spans."""
    entries: dict[str, tuple[int, int]] = {}
    idx = _skip_ws_and_comments(text, open_idx + 1)

    while idx < close_idx:
        if text[idx] == "}":
            break
        if text[idx] != '"':
            raise ValueError(f"Expected property key at offset {idx} in JSONC.")
        key_end = _scan_string_end(text, idx)
        key = json.loads(text[idx:key_end])

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

        entries[key] = (val_start, val_end)
        idx = _skip_ws_and_comments(text, val_end)
        if idx < close_idx and text[idx] == ",":
            idx = _skip_ws_and_comments(text, idx + 1)

    return entries


def _collect_object_string_updates(
    text: str,
    obj_open: int,
    obj_close: int,
    updates: dict[str, str],
) -> list[tuple[int, int, str]]:
    """Collect text replacement spans to set string fields inside a JSONC object."""
    entries = _find_object_entries(text, obj_open, obj_close)
    edits: list[tuple[int, int, str]] = []
    missing_fields: list[tuple[str, str]] = []

    for key, new_val in updates.items():
        encoded = json.dumps(new_val)
        if key in entries:
            v_start, v_end = entries[key]
            edits.append((v_start, v_end, encoded))
        else:
            missing_fields.append((key, encoded))

    if missing_fields:
        has_existing = bool(entries)
        line_start = text.rfind("\n", 0, obj_open) + 1
        base_indent = ""
        for ch in text[line_start:obj_open]:
            if ch in " \t":
                base_indent += ch
            else:
                break
        child_indent = f"{base_indent}  "
        is_multiline = "\n" in text[obj_open:obj_close]

        if is_multiline:
            insertion = "".join(
                f"\n{child_indent}{json.dumps(k)}: {val}," for k, val in missing_fields
            )
            if not has_existing and insertion.endswith(","):
                insertion = insertion[:-1]
        else:
            pairs = ", ".join(f"{json.dumps(k)}: {val}" for k, val in missing_fields)
            insertion = f" {pairs}," if has_existing else f" {pairs}"

        edits.append((obj_open + 1, obj_open + 1, insertion))

    return edits


def update_default_model_in_jsonc_text(
    raw_text: str,
    provider: str,
    model: str,
    update_fallback: bool = True,
) -> str:
    """Update default.provider/model (and optional fallback) while preserving JSONC comments."""
    root_open = _skip_ws_and_comments(raw_text, 0)
    if root_open >= len(raw_text) or raw_text[root_open] != "{":
        raise ValueError("Expected root JSONC object in artemis.jsonc.")
    root_close = _scan_matching_delimiter(raw_text, root_open, "{", "}")
    root_entries = _find_object_entries(raw_text, root_open, root_close)

    if "default" not in root_entries:
        raise ValueError("artemis.jsonc is missing a top-level 'default' section.")

    default_open, default_end = root_entries["default"]
    default_close = default_end - 1
    if raw_text[default_open] != "{" or raw_text[default_close] != "}":
        raise ValueError("artemis.jsonc 'default' entry must be a JSON object.")

    default_entries = _find_object_entries(raw_text, default_open, default_close)
    edits = _collect_object_string_updates(
        raw_text,
        default_open,
        default_close,
        {"provider": provider, "model": model},
    )

    if update_fallback and "fallback" in default_entries:
        fb_open, fb_end = default_entries["fallback"]
        fb_close = fb_end - 1
        if raw_text[fb_open] == "{" and raw_text[fb_close] == "}":
            edits.extend(
                _collect_object_string_updates(
                    raw_text,
                    fb_open,
                    fb_close,
                    {"provider": provider, "model": model},
                )
            )

    updated = raw_text
    for start, end, replacement in sorted(edits, key=lambda item: (item[0], item[1]), reverse=True):
        updated = f"{updated[:start]}{replacement}{updated[end:]}"
    return updated


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

    # Validate parsed JSONC and schema before writing to disk.
    parsed = load_jsonc(io.StringIO(updated_text))
    if not isinstance(parsed, dict) or not isinstance(parsed.get("default"), dict):
        raise ValueError("Updated artemis.jsonc failed structural validation.")
    LLMWithFallback.model_validate(parsed["default"])

    config_path.write_text(updated_text, encoding="utf-8")
    ModelService.invalidate_cache()
    readiness_engine.invalidate_cache()

    return {
        "config_path": str(config_path),
        "config_content": updated_text,
        "default_model": parsed["default"],
        "base_url": saved_base_url,
    }
