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
import binascii
import io
import os
from typing import Any

from PIL import Image, UnidentifiedImageError

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


def _log_image_resolution(
    *,
    caller: str | None,
    orig_size: tuple[int, int],
    final_size: tuple[int, int],
    max_dim: int,
    resized: bool,
    payload_bytes: int,
) -> None:
    """Emit a concise INFO log showing the effective screenshot resolution sent to the LLM."""
    caller_tag = f" caller={caller}" if caller else ""
    orig_w, orig_h = orig_size
    new_w, new_h = final_size
    kb = payload_bytes / 1024.0
    if max_dim <= 0:
        mode_note = "image_max_dim=0 [original]"
    elif resized:
        mode_note = f"image_max_dim={max_dim} [scaled]"
    else:
        mode_note = f"image_max_dim={max_dim} [unchanged]"
    logger.info(
        f"[ImageScaling]{caller_tag} {orig_w}x{orig_h} -> {new_w}x{new_h} "
        f"({mode_note}, {kb:.1f} KB)"
    )


def _process_pil_image(
    im: Image.Image,
    *,
    raw_bytes: bytes | None,
    orig_format: str | None,
    max_dim: int,
    quality: int,
    caller: str | None,
    log_resolution: bool,
) -> bytes:
    """Resize if needed, normalize to JPEG bytes, and optionally log effective resolution."""
    orig_w, orig_h = im.size
    needs_resize = bool(max_dim and max_dim > 0 and max(orig_w, orig_h) > max_dim)

    if needs_resize:
        scale = max_dim / max(orig_w, orig_h)
        new_w = max(1, int(round(orig_w * scale)))
        new_h = max(1, int(round(orig_h * scale)))
        target_im = im.resize((new_w, new_h), Image.Resampling.LANCZOS)
    else:
        new_w, new_h = orig_w, orig_h
        target_im = im

    if (
        not needs_resize
        and raw_bytes is not None
        and orig_format == "JPEG"
        and target_im.mode in ("RGB", "L")
    ):
        out_bytes = raw_bytes
    else:
        if target_im.mode not in ("RGB", "L"):
            target_im = target_im.convert("RGB")
        buf = io.BytesIO()
        target_im.save(buf, format="JPEG", quality=quality, optimize=True)
        out_bytes = buf.getvalue()

    if log_resolution:
        _log_image_resolution(
            caller=caller,
            orig_size=(orig_w, orig_h),
            final_size=(new_w, new_h),
            max_dim=max_dim,
            resized=needs_resize,
            payload_bytes=len(out_bytes),
        )
    return out_bytes


def prepare_image_bytes_for_llm(
    image_input: bytes | str | Image.Image | None,
    max_dim: int | None = None,
    quality: int = 80,
    caller: str | None = None,
    log_resolution: bool = True,
) -> bytes:
    """Prepare and downscale an image to JPEG bytes for LLM vision payloads.

    Preserves the exact aspect ratio so 0-1000 normalized coordinates match
    the original device screen without distortion. Already-compliant JPEG bytes
    pass through unchanged to avoid unnecessary lossy re-compression.
    """
    if not image_input:
        return b""

    resolved_max_dim = resolve_image_max_dim() if max_dim is None else max_dim

    if isinstance(image_input, Image.Image):
        try:
            return _process_pil_image(
                image_input,
                raw_bytes=None,
                orig_format=None,
                max_dim=resolved_max_dim,
                quality=quality,
                caller=caller,
                log_resolution=log_resolution,
            )
        except (OSError, ValueError) as exc:
            logger.debug(f"Failed to encode PIL image for LLM: {exc}")
            return b""

    raw_bytes: bytes
    if isinstance(image_input, str):
        cleaned = image_input.split(",", 1)[1] if "," in image_input else image_input
        try:
            raw_bytes = base64.b64decode(cleaned)
        except (ValueError, binascii.Error):
            return b""
    elif isinstance(image_input, bytes):
        raw_bytes = image_input
    else:
        logger.warning(f"Unsupported image type for LLM encoding: {type(image_input)}")
        return b""

    try:
        with Image.open(io.BytesIO(raw_bytes)) as im:
            return _process_pil_image(
                im,
                raw_bytes=raw_bytes,
                orig_format=im.format,
                max_dim=resolved_max_dim,
                quality=quality,
                caller=caller,
                log_resolution=log_resolution,
            )
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        logger.debug(f"Failed to downscale image for LLM, using fallback: {exc}")
        return raw_bytes


def encode_image_for_llm(
    image_input: bytes | str | Image.Image | None,
    max_dim: int | None = None,
    quality: int = 80,
    caller: str | None = None,
    log_resolution: bool = True,
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
        caller: Optional agent/tool identifier included in resolution logs.
        log_resolution: Whether to log the effective image dimensions sent to the LLM.

    Returns:
        Base64-encoded string of the optimized JPEG image.
    """
    if not image_input:
        return ""

    if isinstance(image_input, str):
        cleaned = image_input.split(",", 1)[1] if "," in image_input else image_input
        try:
            raw_bytes = base64.b64decode(cleaned)
        except (ValueError, binascii.Error):
            return cleaned
        out_bytes = prepare_image_bytes_for_llm(
            raw_bytes,
            max_dim=max_dim,
            quality=quality,
            caller=caller,
            log_resolution=log_resolution,
        )
        return base64.b64encode(out_bytes).decode("utf-8") if out_bytes else ""

    out_bytes = prepare_image_bytes_for_llm(
        image_input,
        max_dim=max_dim,
        quality=quality,
        caller=caller,
        log_resolution=log_resolution,
    )
    return base64.b64encode(out_bytes).decode("utf-8") if out_bytes else ""
