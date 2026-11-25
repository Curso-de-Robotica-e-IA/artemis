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

"""Isolation and behavior tests for the lightweight ``artemis_detector`` subpackage."""

from __future__ import annotations

import ast
import asyncio
import base64
import importlib.metadata
import io
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

from PIL import Image

PACKAGE_ROOT = Path(__file__).resolve().parents[1]
PACKAGE_SRC = PACKAGE_ROOT / "src"
if str(PACKAGE_SRC) not in sys.path:
    sys.path.insert(0, str(PACKAGE_SRC))

import artemis_detector  # noqa: E402
from artemis.context import ArtemisContext, DevicePlatform  # noqa: E402
from artemis.utils.image_scaling import encode_image_for_llm  # noqa: E402
from artemis_detector import (  # noqa: E402
    COLOR_PALETTE,
    DetectedItem,
    DetectionResult,
    IconDetector,
    capture_and_detect,
    detect_icon,
    normalized_to_pixel_coords,
)

DETECTOR_PATH = PACKAGE_SRC / "artemis_detector" / "detector.py"
INIT_PATH = PACKAGE_SRC / "artemis_detector" / "__init__.py"

ALLOWED_ARTEMIS_IMPORTS = {
    ("artemis.context", "ArtemisContext"),
    ("artemis.context", "DeviceContext"),
    ("artemis.context", "DevicePlatform"),
    ("artemis.services.llm", "get_llm"),
    ("artemis.llm.structured", "parse_structured"),
    ("artemis.utils.image_scaling", "encode_image_for_llm"),
}

FORBIDDEN_ARTEMIS_PREFIXES = (
    "artemis.drivers",
    "artemis.interfaces",
    "artemis.graph",
    "artemis.agents",
)


def test_public_api_imports_and_instantiation() -> None:
    """Public symbols import cleanly and IconDetector initializes minimal context."""
    assert hasattr(artemis_detector, "__version__")
    assert len(COLOR_PALETTE) >= 1
    assert callable(detect_icon)
    assert callable(capture_and_detect)
    assert DetectedItem is not None
    assert DetectionResult is not None

    detector = IconDetector(device_serial="test-device")
    assert detector.device_serial == "test-device"
    assert detector.device.device_id == "test-device"
    assert detector.device.platform == DevicePlatform.ANDROID
    assert isinstance(detector.ctx, ArtemisContext)
    assert detector._llm is None


def test_detector_ast_imports_only_allowed_artemis_symbols() -> None:
    """AST inspection confirms strict isolation to the allowed ARTEMIS imports."""
    detector_tree = ast.parse(DETECTOR_PATH.read_text(encoding="utf-8"), filename=str(DETECTOR_PATH))
    init_tree = ast.parse(INIT_PATH.read_text(encoding="utf-8"), filename=str(INIT_PATH))

    found_artemis_from: set[tuple[str, str]] = set()
    all_imported_modules: set[str] = set()

    for node in ast.walk(detector_tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                all_imported_modules.add(alias.name)
                assert not (
                    alias.name == "artemis" or alias.name.startswith("artemis.")
                ), f"Forbidden bare artemis import in detector.py: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "Relative imports are forbidden"
            module = node.module or ""
            all_imported_modules.add(module)
            if module == "artemis" or module.startswith("artemis."):
                for alias in node.names:
                    found_artemis_from.add((module, alias.name))

    assert found_artemis_from == ALLOWED_ARTEMIS_IMPORTS

    for module in all_imported_modules:
        for prefix in FORBIDDEN_ARTEMIS_PREFIXES:
            assert not (
                module == prefix or module.startswith(prefix + ".")
            ), f"Forbidden ARTEMIS subsystem import: {module}"

    for cli_mod in ("argparse", "json", "re"):
        assert cli_mod not in all_imported_modules, f"CLI-only import remained: {cli_mod}"

    for node in ast.walk(init_tree):
        if isinstance(node, ast.Import):
            for alias in node.names:
                assert not (
                    alias.name == "artemis" or alias.name.startswith("artemis.")
                ), f"Unexpected artemis import in __init__.py: {alias.name}"
        elif isinstance(node, ast.ImportFrom):
            assert node.level == 0, "Relative imports are forbidden in __init__.py"
            module = node.module or ""
            assert not (
                module == "artemis" or module.startswith("artemis.")
            ), f"Unexpected artemis import in __init__.py: {module}"


def test_normalized_to_pixel_coords_conversion() -> None:
    """Normalized 0-1000 coordinates convert accurately to native pixel coordinates."""
    assert normalized_to_pixel_coords((0, 0), (1080, 2400)) == (0, 0)
    assert normalized_to_pixel_coords((500, 500), (1080, 2400)) == (540, 1200)
    assert normalized_to_pixel_coords((1000, 1000), (1080, 2400)) == (1080, 2400)
    assert normalized_to_pixel_coords((840, 900), (1080, 2400)) == (907, 2160)
    assert normalized_to_pixel_coords((250, 750), (1440, 3120)) == (360, 2340)


def test_encode_image_for_llm_with_synthetic_pil_image() -> None:
    """encode_image_for_llm preserves aspect ratio and outputs valid base64 JPEG."""
    synthetic = Image.new("RGB", (2160, 1080), color=(30, 144, 255))
    buf = io.BytesIO()
    synthetic.save(buf, format="PNG")

    encoded = encode_image_for_llm(buf.getvalue(), max_dim=1024)
    assert isinstance(encoded, str) and len(encoded) > 0

    decoded_bytes = base64.b64decode(encoded)
    with Image.open(io.BytesIO(decoded_bytes)) as downscaled:
        assert downscaled.format == "JPEG"
        assert downscaled.size == (1024, 512)


class _StubLLM:
    def __init__(self, reply: str) -> None:
        self.reply = reply
        self.recorded_messages: list[Any] = []

    async def ainvoke(self, messages: list[Any]) -> SimpleNamespace:
        self.recorded_messages.append(messages)
        return SimpleNamespace(content=self.reply)


def test_icon_detector_detect_end_to_end_with_stub_llm(tmp_path: Path) -> None:
    """IconDetector.detect parses coordinates, scales to native pixels, and annotates output."""
    synthetic = Image.new("RGB", (1080, 2400), color=(20, 20, 20))
    annotated_file = tmp_path / "annotated.png"

    detector = IconDetector(device_serial="test-device")
    stub = _StubLLM(
        '[{"point": [500, 250], "label": "flash icon"}, '
        '{"point": [840, 900], "label": "camera icon"}]'
    )
    detector._llm = stub

    result = asyncio.run(
        detector.detect(
            image=synthetic,
            query=["flash icon", "camera icon"],
            output_path=annotated_file,
            max_dim=512,
        )
    )

    assert result.found is True
    assert result.total_found == 2
    assert result.screen_size == (1080, 2400)
    assert result.annotated_image_path == str(annotated_file)
    assert annotated_file.exists()

    assert result.items[0].label == "flash icon"
    assert result.items[0].normalized_coords == (500, 250)
    assert result.items[0].pixel_coords == (540, 600)
    assert result.items[1].label == "camera icon"
    assert result.items[1].normalized_coords == (840, 900)
    assert result.items[1].pixel_coords == (907, 2160)
    assert result.normalized_coords == (500, 250)
    assert result.pixel_coords == (540, 600)

    sent_content = stub.recorded_messages[0][0].content
    assert sent_content[0]["image_url"]["url"].startswith("data:image/jpeg;base64,")

    empty_detector = IconDetector(device_serial="test-device")
    empty_detector._llm = _StubLLM("no json payload here")
    empty_result = asyncio.run(
        empty_detector.detect(
            image=synthetic,
            query="missing icon",
            output_path=tmp_path / "should_not_exist.png",
        )
    )
    assert empty_result.found is False
    assert empty_result.total_found == 0
    assert empty_result.items == []
    assert empty_result.annotated_image_path is None
    assert not (tmp_path / "should_not_exist.png").exists()


def test_package_distribution_metadata() -> None:
    """Installed distribution metadata matches version and minimal PyPI dependencies."""
    assert importlib.metadata.version("artemis-detector") == artemis_detector.__version__
    dist = importlib.metadata.distribution("artemis-detector")
    assert dist.metadata["Name"] == "artemis-detector"

    requires = dist.requires or []
    base_requires = [req for req in requires if "; extra ==" not in req]
    assert any(req.startswith("langchain-core") for req in base_requires)
    assert any(req.startswith("pillow") for req in base_requires)
    assert all("git+" not in req for req in requires)
