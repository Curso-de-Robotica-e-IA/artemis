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

"""Intelligent image downscaling for LLM vision models while preserving aspect ratio."""

import base64
import io
import os
from typing import Any

from PIL import Image

from third_party.mobile_use.utils.file import load_jsonc
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_IMAGE_MAX_DIM = 1280
_ENV_IMAGE_MAX_DIM_KEYS = (
    "ARTEMIS_IMAGE_MAX_DIM",
    "IMAGE_MAX_DIM",
    "ARTEMIS_SCREENSHOT_MAX_DIM",
)


def parse_image_max_dim(value: object) -> int | None:
    """Parse a non-negative integer pixel limit, rejecting booleans and invalid values."""
    if value is None or isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value >= 0 else None
    if isinstance(value, str):
        stripped = value.strip()
        if not stripped:
            return None
        try:
            parsed = int(stripped)
        except ValueError:
            return None
        return parsed if parsed >= 0 else None
    return None


def extract_image_max_dim_from_config(config_data: Any) -> int | None:
    """Extract ``image_max_dim`` from parsed ``artemis.jsonc`` data if explicitly set."""
    if not isinstance(config_data, dict):
        return None

    agent_block = config_data.get("agent")
    if isinstance(agent_block, dict) and "image_max_dim" in agent_block:
        parsed = parse_image_max_dim(agent_block.get("image_max_dim"))
        if parsed is not None:
            return parsed

    if "image_max_dim" in config_data:
        parsed = parse_image_max_dim(config_data.get("image_max_dim"))
        if parsed is not None:
            return parsed

    return None


def resolve_image_max_dim() -> int:
    """Resolve active ``image_max_dim`` dynamically from ``artemis.jsonc``.

    Reads the active configuration file on every call so changes saved via the
    UI take effect immediately without restarting the server. Falls back to
    ``os.getenv("ARTEMIS_IMAGE_MAX_DIM")`` (and legacy aliases) only when
    ``image_max_dim`` is omitted from ``artemis.jsonc``, then defaults to 1280.
    """
    from artemis.config import paths as artemis_paths

    for candidate in ("artemis.jsonc", "artemis.json"):
        try:
            config_path = artemis_paths.get_config_path(candidate)
        except FileNotFoundError:
            continue
        try:
            with open(config_path, encoding="utf-8") as f:
                data = load_jsonc(f)
        except (OSError, ValueError):
            break
        configured = extract_image_max_dim_from_config(data)
        if configured is not None:
            return configured
        break

    for env_key in _ENV_IMAGE_MAX_DIM_KEYS:
        env_parsed = parse_image_max_dim(os.getenv(env_key))
        if env_parsed is not None:
            return env_parsed

    return DEFAULT_IMAGE_MAX_DIM


def encode_image_for_llm(
    image_input: bytes | str | Image.Image | None,
    max_dim: int | None = None,
    quality: int = 80,
) -> str:
    """Prepares and downscales an image for LLM vision consumption.

    Preserves the exact aspect ratio so 0-1000 normalized coordinates match
    the original device screen without distortion.

    Args:
        image_input: Raw image bytes, base64 string, or PIL Image.
        max_dim: Maximum dimension (width or height) in pixels. Defaults to
            ``agent.image_max_dim`` read dynamically from ``artemis.jsonc``,
            with ``ARTEMIS_IMAGE_MAX_DIM`` env var as secondary fallback
            (default 1280). If <= 0, downscaling is bypassed.
        quality: JPEG compression quality (default 80).

    Returns:
        Base64-encoded string of the optimized JPEG image.
    """
    if not image_input:
        return ""

    if max_dim is None:
        max_dim = resolve_image_max_dim()

    # Extract raw bytes from input
    raw_bytes: bytes
    if isinstance(image_input, str):
        cleaned = image_input.split(",", 1)[1] if "," in image_input else image_input
        try:
            raw_bytes = base64.b64decode(cleaned)
        except Exception:
            return cleaned
    elif isinstance(image_input, bytes):
        raw_bytes = image_input
    elif isinstance(image_input, Image.Image):
        buf = io.BytesIO()
        image_input.save(buf, format="JPEG")
        raw_bytes = buf.getvalue()
    else:
        logger.warning(f"Unsupported image type for LLM encoding: {type(image_input)}")
        return ""

    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            orig_w, orig_h = im.size
            if max_dim and max_dim > 0 and max(orig_w, orig_h) > max_dim:
                scale = max_dim / max(orig_w, orig_h)
                new_w = max(1, int(round(orig_w * scale)))
                new_h = max(1, int(round(orig_h * scale)))
                im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)

            if im.mode in ("RGBA", "P"):
                im = im.convert("RGB")

            buf = io.BytesIO()
            im.save(buf, format="JPEG", quality=quality, optimize=True)
            return base64.b64encode(buf.getvalue()).decode("utf-8")
    except Exception as exc:
        logger.debug(f"Failed to downscale image for LLM, using fallback: {exc}")
        return base64.b64encode(raw_bytes).decode("utf-8")
