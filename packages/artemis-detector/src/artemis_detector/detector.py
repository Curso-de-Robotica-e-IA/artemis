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

"""Lightweight visual icon and UI-element detector isolated from ARTEMIS."""

from __future__ import annotations

import asyncio
import io
import subprocess
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from langchain_core.messages import HumanMessage
from PIL import Image, ImageDraw

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.llm.structured import parse_structured
from artemis.services.llm import get_llm
from artemis.utils.image_scaling import encode_image_for_llm

COLOR_PALETTE = [
    "#00FFCC",  # Ciano Neon
    "#FF007F",  # Magenta
    "#FFD700",  # Dourado
    "#00BFFF",  # Azul Celeste
    "#32CD32",  # Verde Lima
    "#FF8C00",  # Laranja
    "#9D00FF",  # Roxo Eletrico
    "#FF1493",  # Rosa Choque
]


def normalized_to_pixel_coords(
    normalized_coords: tuple[int, int],
    screen_size: tuple[int, int],
) -> tuple[int, int]:
    """Convert 0-1000 normalized (x, y) coordinates to native screen pixels."""
    x_norm, y_norm = normalized_coords
    width, height = screen_size
    return (
        int(round(x_norm * width / 1000.0)),
        int(round(y_norm * height / 1000.0)),
    )


@dataclass
class DetectedItem:
    """Represents a single UI element or icon detected on the screen."""

    label: str
    normalized_coords: tuple[int, int]
    pixel_coords: tuple[int, int]
    index: int = 1

    def to_dict(self) -> dict[str, Any]:
        return {
            "index": self.index,
            "label": self.label,
            "normalized_coords": list(self.normalized_coords),
            "pixel_coords": list(self.pixel_coords),
        }


@dataclass
class DetectionResult:
    """Consolidated result of multi-icon visual detection."""

    query: str | list[str]
    found: bool
    total_found: int
    items: list[DetectedItem]
    screen_size: tuple[int, int]
    annotated_image_path: str | None
    elapsed_time: float
    raw_response: Any = None

    @property
    def normalized_coords(self) -> tuple[int, int] | None:
        """Compatibility helper returning the first detected item's normalized coords."""
        return self.items[0].normalized_coords if self.items else None

    @property
    def pixel_coords(self) -> tuple[int, int] | None:
        """Compatibility helper returning the first detected item's pixel coords."""
        return self.items[0].pixel_coords if self.items else None

    def to_dict(self) -> dict[str, Any]:
        return {
            "query": self.query,
            "found": self.found,
            "total_found": self.total_found,
            "items": [item.to_dict() for item in self.items],
            "screen_size": list(self.screen_size),
            "annotated_image_path": self.annotated_image_path,
            "elapsed_time": round(self.elapsed_time, 2),
        }


class IconDetector:
    """Visual icon detector decoupled from ARTEMIS action execution."""

    def __init__(
        self,
        device_serial: str = "N0OJ460182",
        role_name: str = "operator",
        timeout: float = 60.0,
    ) -> None:
        self.device_serial = device_serial
        self.role_name = role_name
        self.timeout = timeout
        self.device = DeviceContext(platform=DevicePlatform.ANDROID, device_id=device_serial)
        self.ctx = ArtemisContext(device=self.device)
        self._llm: Any = None

    @property
    def llm(self) -> Any:
        if self._llm is None:
            try:
                self._llm = get_llm(self.ctx, name="object_detector", is_utils=True)
            except (AttributeError, KeyError, RuntimeError, ValueError):
                self._llm = get_llm(self.ctx, name=self.role_name)
        return self._llm

    def capture_screen(self, device_serial: str | None = None) -> Image.Image:
        """Capture a screenshot directly from an Android device via ADB."""
        serial = device_serial or self.device_serial
        cmd = ["adb", "-s", serial, "exec-out", "screencap", "-p"]
        res = subprocess.run(cmd, capture_output=True, check=True)
        # Skip any ADB banner/warning prefix by locating the PNG signature.
        png_idx = res.stdout.find(b"\x89PNG\r\n\x1a\n")
        if png_idx == -1:
            stderr_text = res.stderr.decode(errors="replace")
            raise RuntimeError(f"Error capturing screenshot via adb: {stderr_text}")
        return Image.open(io.BytesIO(res.stdout[png_idx:]))

    async def detect(
        self,
        image: Image.Image | str | Path,
        query: str | list[str],
        output_path: str | Path | None = "detected_result.png",
        max_dim: int = 1024,
    ) -> DetectionResult:
        """Locate one or more icons/UI elements in the supplied image."""
        start_time = time.time()

        if isinstance(query, (list, tuple)):
            labels_str = ", ".join(str(q).strip() for q in query if str(q).strip())
            query_repr: str | list[str] = list(query)
        else:
            labels_str = str(query).strip()
            query_repr = str(query).strip()

        if isinstance(image, (str, Path)):
            pil_img = Image.open(image)
        else:
            pil_img = image

        orig_w, orig_h = pil_img.size

        img_buffer = io.BytesIO()
        pil_img.save(img_buffer, format="PNG")
        raw_bytes = img_buffer.getvalue()

        img_b64 = encode_image_for_llm(raw_bytes, max_dim=max_dim)

        prompt = (
            "Detect the locations of all requested UI components/icons on the screen: "
            f"{labels_str}.\n"
            "Return a JSON list containing all detected objects. Each object must have:\n"
            "- 'point': [x, y] normalized coordinates (0 to 1000), where x is horizontal "
            "distance from left (0=left, 1000=right), and y is vertical distance from top "
            "(0=top, 1000=bottom)\n"
            "- 'label': the specific name or category of the detected object\n\n"
            "Example output:\n"
            "[\n"
            '  {"point": [840, 900], "label": "camera icon"},\n'
            '  {"point": [160, 900], "label": "phone icon"}\n'
            "]\n\n"
            "If no matching objects are found, return an empty list []."
        )

        messages = [
            HumanMessage(
                content=[
                    {
                        "type": "image_url",
                        "image_url": {"url": f"data:image/jpeg;base64,{img_b64}"},
                    },
                    {"type": "text", "text": prompt},
                ]
            )
        ]

        response = await asyncio.wait_for(self.llm.ainvoke(messages), timeout=self.timeout)
        content = response.content if isinstance(response.content, str) else str(response.content)

        parsed = parse_structured(content)
        elapsed = time.time() - start_time

        if not parsed or not isinstance(parsed, list):
            return DetectionResult(
                query=query_repr,
                found=False,
                total_found=0,
                items=[],
                screen_size=(orig_w, orig_h),
                annotated_image_path=None,
                elapsed_time=elapsed,
                raw_response=content,
            )

        detected_items: list[DetectedItem] = []
        for idx, item in enumerate(parsed, start=1):
            if not isinstance(item, dict) or "point" not in item:
                continue
            point = item["point"]
            if not isinstance(point, (list, tuple)) or len(point) != 2:
                continue

            x_norm, y_norm = int(point[0]), int(point[1])
            pixel_x, pixel_y = normalized_to_pixel_coords(
                (x_norm, y_norm),
                (orig_w, orig_h),
            )
            label = str(item.get("label") or f"Element {idx}")

            detected_items.append(
                DetectedItem(
                    label=label,
                    normalized_coords=(x_norm, y_norm),
                    pixel_coords=(pixel_x, pixel_y),
                    index=idx,
                )
            )

        saved_img_path = None
        if output_path and detected_items:
            saved_img_path = str(output_path)
            self._draw_all_markers(pil_img, detected_items, saved_img_path)

        return DetectionResult(
            query=query_repr,
            found=len(detected_items) > 0,
            total_found=len(detected_items),
            items=detected_items,
            screen_size=(orig_w, orig_h),
            annotated_image_path=saved_img_path,
            elapsed_time=elapsed,
            raw_response=parsed,
        )

    @staticmethod
    def _draw_all_markers(
        image: Image.Image,
        items: list[DetectedItem],
        output_path: str,
    ) -> None:
        """Draw crosshairs, index badges, and labels for all detected items."""
        annotated = image.copy()
        draw = ImageDraw.Draw(annotated)
        w, h = annotated.size

        radius = max(22, int(w * 0.024))

        for idx, item in enumerate(items):
            color = COLOR_PALETTE[idx % len(COLOR_PALETTE)]
            px, py = item.pixel_coords

            draw.ellipse(
                [px - radius, py - radius, px + radius, py + radius],
                outline=color,
                width=6,
            )
            draw.line([px - radius - 12, py, px + radius + 12, py], fill=color, width=2)
            draw.line([px, py - radius - 12, px, py + radius + 12], fill=color, width=2)
            draw.ellipse([px - 5, py - 5, px + 5, py + 5], fill="#FF0033", width=6)

            badge_text = f"[{item.index}]"
            draw.rectangle([px - 14, py - radius - 24, px + 14, py - radius - 2], fill=color)
            draw.text(
                (px - 9, py - radius - 20),
                badge_text,
                fill="black",
                stroke_width=4,
                stroke_fill="white",
            )

            text = f"{item.index}. {item.label} ({px}, {py})"
            box_w = min(len(text) * 10 + 16, w - px + radius)
            box_y1 = min(py + radius + 6, h - 35)
            box_y2 = min(box_y1 + 28, h - 2)
            draw.rectangle(
                [px - radius, box_y1, px - radius + box_w, box_y2],
                fill="black",
                outline=color,
                width=6,
            )
            draw.text(
                (px - radius + 8, box_y1 + 6),
                text,
                fill="white",
                stroke_width=4,
                stroke_fill="black",
            )

        annotated.save(output_path)


def detect_icon(
    image_path: str | Path,
    query: str | list[str],
    output_path: str | Path | None = "detected_result.png",
    device_serial: str = "N0OJ460182",
) -> DetectionResult:
    """Locate one or more icons in an existing image without running device actions."""
    detector = IconDetector(device_serial=device_serial)
    return asyncio.run(detector.detect(image=image_path, query=query, output_path=output_path))


def capture_and_detect(
    query: str | list[str],
    device_serial: str = "N0OJ460182",
    output_path: str | Path | None = "detected_result.png",
) -> DetectionResult:
    """Capture a device screenshot via ADB and locate the requested icons."""
    detector = IconDetector(device_serial=device_serial)
    img = detector.capture_screen(device_serial)
    return asyncio.run(detector.detect(image=img, query=query, output_path=output_path))
