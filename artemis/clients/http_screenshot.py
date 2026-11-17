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

"""Shared HTTP client helpers for retrieving device screenshots from the Touch API."""

from __future__ import annotations

import base64
import binascii
from dataclasses import dataclass
from io import BytesIO
import json
from typing import Any

import httpx
from PIL import Image, UnidentifiedImageError

from artemis.config.settings import settings
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "HttpScreenshotPayload",
    "decode_screenshot_image",
    "fetch_http_screenshot",
    "fetch_http_screenshot_image",
    "fetch_http_screenshot_image_sync",
    "fetch_http_screenshot_sync",
    "get_screenshot_api_url",
]


@dataclass(frozen=True)
class HttpScreenshotPayload:
    """Decoded screenshot payload returned by ``GET /screenshot``."""

    image_base64: str
    image_bytes: bytes
    width: int | None = None
    height: int | None = None


def get_screenshot_api_url(base_url: str | None = None) -> str:
    """Returns the ``/screenshot`` URL resolved from ``base_url`` or settings."""
    resolved = base_url or getattr(settings, "ARTEMIS_TOUCH_API_URL", "http://127.0.0.1:8001")
    return f"{str(resolved).rstrip('/')}/screenshot"


def _resolve_timeout(timeout_s: float | None) -> tuple[float, httpx.Timeout]:
    effective = (
        float(timeout_s)
        if timeout_s is not None
        else float(getattr(settings, "ARTEMIS_TOUCH_API_TIMEOUT_S", 30.0))
    )
    return effective, httpx.Timeout(effective, connect=min(effective, 5.0))


def _parse_dimension(value: Any) -> int | None:
    if isinstance(value, bool) or value is None:
        return None
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return None
    return parsed if parsed > 0 else None


def _parse_screenshot_response(response: Any, url: str) -> HttpScreenshotPayload | None:
    status_code = getattr(response, "status_code", 200)
    is_success = getattr(response, "is_success", 200 <= status_code < 300)

    try:
        data: Any = response.json()
    except (ValueError, json.JSONDecodeError) as exc:
        if not is_success:
            raw_text = getattr(response, "text", "")
            logger.error(
                f"HTTP Touch API GET {url} returned HTTP {status_code}: {str(raw_text)[:200]}"
            )
        else:
            logger.error(f"HTTP Touch API GET {url} returned invalid JSON: {exc}")
        return None

    if not is_success:
        detail = (data.get("error") or data.get("message")) if isinstance(data, dict) else str(data)
        logger.error(
            f"HTTP Touch API GET {url} returned HTTP {status_code}: {detail or 'request failed'}"
        )
        return None

    if not isinstance(data, dict):
        logger.error(
            f"HTTP Touch API GET {url} returned unexpected JSON type: {type(data).__name__}"
        )
        return None

    if data.get("ok") is False:
        err_detail = data.get("error") or data.get("message") or "unknown error"
        logger.error(f"HTTP Touch API GET {url} reported failure: {err_detail}")
        return None

    raw_b64 = (
        data.get("image_base64")
        or data.get("screenshot_base64")
        or data.get("image_b64")
        or data.get("base64")
        or data.get("screenshot")
        or data.get("image")
        or ""
    )
    if not isinstance(raw_b64, str) or not raw_b64.strip():
        logger.error(f"HTTP Touch API GET {url} response missing base64 image data.")
        return None

    clean_b64 = raw_b64.strip()
    if clean_b64.startswith("data:") and "," in clean_b64:
        clean_b64 = clean_b64.split(",", 1)[1].strip()
    clean_b64 = "".join(clean_b64.split())

    try:
        image_bytes = base64.b64decode(clean_b64, validate=True)
    except (binascii.Error, ValueError) as exc:
        logger.error(f"HTTP Touch API GET {url} returned invalid base64 payload: {exc}")
        return None

    raw_w = data.get("screen_width") if data.get("screen_width") is not None else data.get("width")
    raw_h = (
        data.get("screen_height") if data.get("screen_height") is not None else data.get("height")
    )
    return HttpScreenshotPayload(
        image_base64=clean_b64,
        image_bytes=image_bytes,
        width=_parse_dimension(raw_w),
        height=_parse_dimension(raw_h),
    )


def _log_request_error(exc: BaseException, url: str, effective_timeout: float) -> None:
    if isinstance(exc, httpx.TimeoutException):
        logger.error(f"HTTP Touch API timed out on GET {url} after {effective_timeout}s: {exc}")
    elif isinstance(exc, httpx.ConnectError):
        logger.error(f"HTTP Touch API is unreachable at GET {url} (connection error): {exc}")
    elif isinstance(exc, httpx.HTTPError):
        logger.error(f"HTTP Touch API request to GET {url} failed: {exc}")
    else:
        logger.error(f"Unexpected error fetching screenshot from {url}: {exc}")


def decode_screenshot_image(image_bytes: bytes, url: str = "") -> Image.Image | None:
    """Decodes raw PNG/JPEG bytes into a detached PIL ``Image``."""
    if not image_bytes:
        return None
    try:
        with Image.open(BytesIO(image_bytes)) as img:
            img.load()
            return img.copy()
    except (UnidentifiedImageError, OSError, ValueError) as exc:
        target = f" from {url}" if url else ""
        logger.error(f"HTTP Touch API returned unreadable image bytes{target}: {exc}")
        return None


async def fetch_http_screenshot(
    *,
    base_url: str | None = None,
    timeout_s: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> HttpScreenshotPayload | None:
    """Asynchronously fetches a screenshot from the Touch API ``GET /screenshot`` route."""
    url = get_screenshot_api_url(base_url)
    effective, timeout = _resolve_timeout(timeout_s)
    try:
        if client is not None:
            response = await client.get(url, timeout=timeout)
        else:
            async with httpx.AsyncClient(timeout=timeout) as http_client:
                response = await http_client.get(url)
        return _parse_screenshot_response(response, url)
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        OSError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        _log_request_error(exc, url, effective)
        return None


def fetch_http_screenshot_sync(
    *,
    base_url: str | None = None,
    timeout_s: float | None = None,
    client: httpx.Client | None = None,
) -> HttpScreenshotPayload | None:
    """Synchronously fetches a screenshot from the Touch API ``GET /screenshot`` route."""
    url = get_screenshot_api_url(base_url)
    effective, timeout = _resolve_timeout(timeout_s)
    try:
        if client is not None:
            response = client.get(url, timeout=timeout)
        else:
            with httpx.Client(timeout=timeout) as http_client:
                response = http_client.get(url)
        return _parse_screenshot_response(response, url)
    except (
        httpx.HTTPError,
        ValueError,
        TypeError,
        OSError,
        RuntimeError,
        json.JSONDecodeError,
    ) as exc:
        _log_request_error(exc, url, effective)
        return None


async def fetch_http_screenshot_image(
    *,
    base_url: str | None = None,
    timeout_s: float | None = None,
    client: httpx.AsyncClient | None = None,
) -> Image.Image | None:
    """Asynchronously fetches and decodes a PIL ``Image`` from ``GET /screenshot``."""
    payload = await fetch_http_screenshot(base_url=base_url, timeout_s=timeout_s, client=client)
    if payload is None:
        return None
    return decode_screenshot_image(payload.image_bytes, url=get_screenshot_api_url(base_url))


def fetch_http_screenshot_image_sync(
    *,
    base_url: str | None = None,
    timeout_s: float | None = None,
    client: httpx.Client | None = None,
) -> Image.Image | None:
    """Synchronously fetches and decodes a PIL ``Image`` from ``GET /screenshot``."""
    payload = fetch_http_screenshot_sync(base_url=base_url, timeout_s=timeout_s, client=client)
    if payload is None:
        return None
    return decode_screenshot_image(payload.image_bytes, url=get_screenshot_api_url(base_url))
