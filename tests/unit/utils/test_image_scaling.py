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

"""Unit tests for intelligent image downscaling for LLM vision models."""

import base64
import io

import pytest
from PIL import Image

from artemis.utils.image_scaling import encode_image_for_llm


def _create_test_image_bytes(width: int, height: int, color: tuple = (255, 0, 0)) -> bytes:
    img = Image.new("RGB", (width, height), color=color)
    buf = io.BytesIO()
    img.save(buf, format="JPEG")
    return buf.getvalue()


def test_encode_image_downscaling():
    # Large 1200 x 2400 image (aspect ratio 1:2)
    raw = _create_test_image_bytes(1200, 2400)
    b64 = encode_image_for_llm(raw, max_dim=1200)

    # Decode and check dimensions
    decoded = base64.b64decode(b64)
    with Image.open(io.BytesIO(decoded)) as im:
        assert im.height == 1200
        assert im.width == 600
        # Aspect ratio is exactly 1:2 preserved
        assert abs((im.width / im.height) - (1200 / 2400)) < 0.01


def test_encode_image_no_upscale():
    # Small 400 x 800 image
    raw = _create_test_image_bytes(400, 800)
    b64 = encode_image_for_llm(raw, max_dim=1280)

    decoded = base64.b64decode(b64)
    with Image.open(io.BytesIO(decoded)) as im:
        assert im.width == 400
        assert im.height == 800


def test_encode_image_disabled_downscaling():
    # Downscaling disabled with max_dim=0
    raw = _create_test_image_bytes(1080, 2400)
    b64 = encode_image_for_llm(raw, max_dim=0)

    decoded = base64.b64decode(b64)
    with Image.open(io.BytesIO(decoded)) as im:
        assert im.width == 1080
        assert im.height == 2400


def test_encode_image_from_base64_and_pil():
    raw = _create_test_image_bytes(1000, 2000)
    raw_b64 = base64.b64encode(raw).decode("utf-8")

    # From base64 string
    res_b64 = encode_image_for_llm(raw_b64, max_dim=1000)
    decoded = base64.b64decode(res_b64)
    with Image.open(io.BytesIO(decoded)) as im:
        assert im.height == 1000
        assert im.width == 500

    # From PIL Image instance
    pil_im = Image.new("RGB", (1000, 2000), (0, 255, 0))
    res_pil_b64 = encode_image_for_llm(pil_im, max_dim=1000)
    decoded_pil = base64.b64decode(res_pil_b64)
    with Image.open(io.BytesIO(decoded_pil)) as im:
        assert im.height == 1000
        assert im.width == 500


def test_encode_image_empty_and_fallback():
    assert encode_image_for_llm(None) == ""
    assert encode_image_for_llm(b"") == ""
    assert encode_image_for_llm("") == ""
