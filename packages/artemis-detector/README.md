# Artemis Detector

`artemis-detector` is an isolated, lightweight Python package for visual icon and
UI-element detection extracted from ARTEMIS. It runs multimodal icon grounding
and draws annotated coordinate markers without pulling in `artemis.drivers`,
`artemis.interfaces`, `artemis.graph`, or `artemis.agents`.

## Public API

- `IconDetector`: Async visual detector (`capture_screen(...)`, `detect(...)`).
- `DetectionResult`: Consolidated detection response with `found`, `total_found`,
  `items`, `screen_size`, `annotated_image_path`, `elapsed_time`, `normalized_coords`,
  `pixel_coords`, and `to_dict()`.
- `DetectedItem`: Individual detected element (`label`, `normalized_coords`,
  `pixel_coords`, `index`, `to_dict()`).
- `detect_icon(...)`: Synchronous helper to detect icons in an image file.
- `capture_and_detect(...)`: Synchronous helper to capture an Android screen via
  ADB and detect icons.
- `normalized_to_pixel_coords(...)`: Converts `0..1000` normalized coordinates to
  native screen pixels.
- `COLOR_PALETTE`, `__version__`.

## Minimal Usage

Async usage with `IconDetector`:

```python
import asyncio
from artemis_detector import IconDetector


async def main() -> None:
    detector = IconDetector(device_serial="emulator-5554")
    image = detector.capture_screen()
    result = await detector.detect(
        image=image,
        query=["camera icon", "settings icon"],
        output_path="detected_result.png",
    )
    print(result.to_dict())


asyncio.run(main())
```

Synchronous convenience helpers:

```python
from artemis_detector import capture_and_detect, detect_icon

result = detect_icon("screen.png", query="camera icon")
print(result.pixel_coords)

live_result = capture_and_detect("camera icon", device_serial="emulator-5554")
print(live_result.items)
```

## Runtime Dependencies & `artemis` Contract

Direct PyPI runtime dependencies declared in `pyproject.toml`:

- `langchain-core>=1.0.0`
- `pillow>=10.0.0`

At runtime, `artemis_detector.detector` also imports 6 symbols from the core
`artemis` package:

- `artemis.context`: `ArtemisContext`, `DeviceContext`, `DevicePlatform`
- `artemis.services.llm`: `get_llm`
- `artemis.llm.structured`: `parse_structured`
- `artemis.utils.image_scaling`: `encode_image_for_llm`

Because `artemis` is not published on PyPI—and embedding a direct `git+https://...`
URL or workspace back-reference in PEP 508 metadata breaks standalone wheel,
Poetry, and PyPI resolution—`artemis` is intentionally omitted from `dependencies`
and `optional-dependencies`.

Inside the `artemis` workspace, `artemis-detector` is registered in
`[tool.uv.workspace]` and `[tool.uv.sources]` while `artemis` is already present
in the environment. In external projects (such as `camera-mapping`), supply
`artemis` explicitly alongside `artemis-detector` and ensure your LLM provider
credentials are configured (e.g., via `.env` / environment variables consumed by
`artemis`).

## Installation

### `uv` / `pip`

Editable install inside a local checkout (without reinstalling dependencies):

```powershell
uv pip install --no-deps -e packages/artemis-detector
```

Standard editable install with `pip`:

```powershell
pip install -e /path/to/artemis/packages/artemis-detector
```

Install from the Git repository subdirectory:

```powershell
pip install "artemis-detector @ git+https://github.com/Curso-de-Robotica-e-IA/artemis.git@feat/artemis-detector#subdirectory=packages/artemis-detector"
```

Install from a built wheel:

```powershell
pip install /path/to/artemis_detector-0.1.0-py3-none-any.whl
```

### Poetry (e.g., `camera-mapping`)

Add directly from the Git repository subdirectory:

```bash
poetry add "git+https://github.com/Curso-de-Robotica-e-IA/artemis.git@feat/artemis-detector#subdirectory=packages/artemis-detector"
```

Or declare it in `pyproject.toml` under `[tool.poetry.dependencies]`:

```toml
[tool.poetry.dependencies]
artemis-detector = { git = "https://github.com/Curso-de-Robotica-e-IA/artemis.git", branch = "feat/artemis-detector", subdirectory = "packages/artemis-detector" }
```

Or install from a local wheel:

```bash
poetry add /path/to/artemis_detector-0.1.0-py3-none-any.whl
```

> **Note:** External consumers must also ensure the `artemis` package (for the 6
> runtime symbols listed above) and valid LLM credentials (`.env`) are available
> in the target environment.
