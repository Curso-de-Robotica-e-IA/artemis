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

from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


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
            settings.ARTEMIS_IMAGE_MAX_DIM or ARTEMIS_IMAGE_MAX_DIM env var (default 1280).
            If <= 0, downscaling is bypassed.
        quality: JPEG compression quality (default 80).

    Returns:
        Base64-encoded string of the optimized JPEG image.
    """
    if not image_input:
        return ""

    if max_dim is None:
        try:
            from artemis.config import settings

            max_dim = settings.ARTEMIS_IMAGE_MAX_DIM
        except Exception:
            try:
                max_dim = int(os.environ.get("ARTEMIS_IMAGE_MAX_DIM", "1280"))
            except ValueError:
                max_dim = 1280

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
