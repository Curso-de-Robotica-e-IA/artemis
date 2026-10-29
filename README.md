<p align="center">
  <img src="./docs/assets/artemis-banner.png?v=7" alt="ARTEMIS Banner" width="100%" />
</p>

<p align="center">
  <strong>Autonomous Multimodal Mobile AI Engine — Direct Python Library & In-Process CLI</strong>
</p>

<p align="center">
  <a href="./README.md"><b>English</b></a> •
  <a href="./docs/README_PT.md">Português</a> •
  <a href="#key-highlights">Highlights</a> •
  <a href="#quick-start">Quick Start</a> •
  <a href="#python-sdk">Python SDK</a> •
  <a href="#cli-usage">CLI Usage</a> •
  <a href="#execution-profiles">Flash vs. Pro</a> •
  <a href="#benchmarks">Benchmarks</a>
</p>

<p align="center">
  <a href="https://www.python.org/downloads/"><img src="https://img.shields.io/badge/Python-3.12+-3776AB.svg?logo=python&logoColor=white" alt="Python 3.12+"></a>
  <a href="LICENSE"><img src="https://img.shields.io/badge/License-Apache%202.0-blue.svg" alt="License: Apache-2.0"></a>
  <a href="https://ai.google.dev/"><img src="https://img.shields.io/badge/Multimodal-Gemini%20%7C%20Claude%20%7C%20GPT--4o%20%7C%20Qwen--VL-4285F4.svg" alt="Multi-Model"></a>
  <a href="https://github.com/google-research/android_world"><img src="https://img.shields.io/badge/AndroidWorld-99%25%2B%20SOTA-success.svg" alt="AndroidWorld SOTA"></a>
  <img src="https://img.shields.io/badge/Architecture-Direct%20In--Process-00C853.svg" alt="Direct In-Process">
</p>

## Key Highlights

* **Direct Python-First Architecture**: Pure Python library with zero background daemons, zero open network ports, and zero JavaScript/Node dependencies. Runs directly in-process in your scripts, test runners (`pytest`), or terminal.
* **Cross-App Mobile Automation**: Executes end-to-end testing scenarios and user journeys across real Android devices and emulators using natural language goals.
* **First-Class Python SDK**: Synchronous context manager (`ArtemisRunner`) for rapid automation scripts and async `Agent` for full pipeline control with structured Pydantic assertion support.
* **Multimodal Visual Grounding**: Combines element hierarchy analysis (XML-first) with OCR and coordinate fallbacks for custom Canvas, Compose, and Flutter interfaces.
* **Dual Execution Modes**: **Flash** profile (ultra-fast observe-and-act loop at **3–5s per step**) or **Pro** profile (dynamic planning graph with milestones, safety nets, and checkpoint self-recovery).
* **AndroidWorld SOTA**: **99%+ task completion rate** on Google Research's **AndroidWorld** benchmark (100+ multi-step tasks across 20+ apps).

---

<a id="quick-start"></a>
## Quick Start

Ensure an Android device (with **USB Debugging** enabled) or emulator is connected:

```bash
# 1. Sync Python dependencies
uv sync

# 2. Configure API keys & device (interactive quickstart wizard)
uv run artemis init

# 3. Run an autonomous task directly in-process
uv run artemis run "Open Settings, find Battery and tell me current level" --profile flash
```

---

<a id="python-sdk"></a>
## Python SDK Integration

ARTEMIS can be imported directly into any Python codebase, testing suite (e.g. `pytest`), or CI/CD workflow.

### 1. Synchronous Automation (`ArtemisRunner`)

The recommended entrypoint for scripts and automation suites is `ArtemisRunner`:

```python
from artemis import ArtemisRunner

# Target a specific device or omit device_serial to use the first attached device
with ArtemisRunner(device_serial="emulator-5554", profile="flash") as bot:
    # Basic task execution
    result = bot.run("Open Settings and check Wi-Fi connection status")
    print(result)

    # Task with full step history (commands, coordinates, and results)
    response = bot.run("Turn on Airplane Mode", include_steps=True)
    print("Outcome:", response["output"])
    for step in response["steps"]:
        print(f"Step {step['step_number']}: {step['command']} at {step['pixel_coordinates']}")

    # Or inspect steps after the run
    steps = bot.get_steps()
```

### 2. Structured Pydantic Output Extraction

Extract strongly typed data directly from mobile screens using Pydantic models:

```python
from pydantic import BaseModel, Field
from artemis import ArtemisRunner

class BatteryInfo(BaseModel):
    percentage: int = Field(description="Battery level as an integer between 0 and 100")
    is_charging: bool = Field(description="True if device is plugged into a charger")

with ArtemisRunner(profile="pro") as bot:
    info = bot.run(
        "Open Settings, navigate to Battery, and extract current status",
        output=BatteryInfo,
    )
    print(f"Battery: {info.percentage}% | Charging: {info.is_charging}")
    assert info.percentage > 10, "Battery too low for test run!"
```

### 3. Asynchronous Pipeline Integration (`Agent`)

For native `asyncio` workflows and custom event streams:

```python
import asyncio
from artemis import Agent

async def main():
    agent = Agent(device_serial="emulator-5554")
    await agent.init()
    try:
        result = await agent.run_task(
            goal="Open YouTube and search for Lo-Fi Hip Hop",
            profile="flash",
        )
        print(f"Status: {result.status} | Steps: {result.steps_taken}")
    finally:
        await agent.clean()

if __name__ == "__main__":
    asyncio.run(main())
```

### 4. Use Inside Another Project

To use ARTEMIS in another project as an editable dependency, add this to your project's `pyproject.toml`:

```toml
[project]
requires-python = ">=3.12"
dependencies = [
    "artemis",
]

[tool.uv.sources]
artemis = { path = "../artemis", editable = true }
```

Then run `uv sync`.

---

<a id="cli-usage"></a>
## CLI Usage

ARTEMIS provides a direct terminal CLI for interactive runs, batch testing, and diagnostics:

| Command | Description | Example |
| :--- | :--- | :--- |
| `artemis run` | Execute an autonomous task in-process on an attached device. | `uv run artemis run "Open Chrome and search Google" --profile flash` |
| `artemis batch` | Execute a sequence of automation tasks sequentially. | `uv run artemis batch --file tasks.txt --profile pro` |
| `artemis doctor` | Verify ADB connectivity, toolchains (scrcpy, FFmpeg), and API keys. | `uv run artemis doctor` (or `uv run artemis doctor --fix`) |
| `artemis trace` | Inspect local session traces, turn logs, and video recordings. | `uv run artemis trace list` / `uv run artemis trace view <session_id>` |
| `artemis helper` | Manage the on-device accessibility helper APK. | `uv run artemis helper install` / `uv run artemis helper status` |
| `artemis init` | Interactive setup wizard to configure API keys and default device. | `uv run artemis init` |

---

<a id="on-device-helper"></a>
## What ARTEMIS Installs on Your Phone

The first task on a device automatically provisions the **Artemis Accessibility Helper**, a lightweight Android accessibility service that reads screen hierarchies instantly without conflicting with `UiAutomation`.

* **Pre-install helper**: `uv run artemis helper install` (avoids first-run setup delay)
* **Check status**: `uv run artemis helper status`
* **Uninstall**: `uv run artemis helper uninstall`
* **Use UIAutomator2 fallback**: Set `ARTEMIS_HIERARCHY_BACKEND=uiautomator` in your `.env`

---

<a id="execution-profiles"></a>
## Execution Profiles: Flash vs. Pro

ARTEMIS supports two execution profiles tailored for different automation requirements:

* **Flash Profile (`--profile flash`)**: Fast and token-efficient reactive loop (~3–5s per step). One model observes the live screen, reasons, and executes actions immediately. Ideal for routine, deterministic UI tasks and automated regression flows.
* **Pro Profile (`--profile pro`)**: Deep planning and verification workflow (~15–40s per step) powered by a LangGraph multi-agent state machine. A **Planner** maintains a living Markdown task plan; the **Operator** executes actions protected by pre-execution **Safety Nets**; and a read-only **Checker** audits checkpoints and validates task completion against the original goal.

---

<a id="benchmarks"></a>
## Benchmarks: AndroidWorld (SOTA 99%+)

Artemis achieved a **99%+ completion rate** on [AndroidWorld](https://github.com/google-research/android_world), Google Research's benchmark spanning 20+ apps and 100+ multi-step tasks.

<p align="center">
  <img src="./docs/assets/androidworld_leaderboard.png?v=2" alt="AndroidWorld Benchmark Comparison" width="100%" />
</p>

---

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for guidelines on code style, tests, and development workflows.

## License

This project is licensed under the [Apache License 2.0](LICENSE).

### Third-party code

Code not owned by Google lives under [`third_party/`](third_party/), one directory per upstream project, each with its own `LICENSE` and `METADATA`.
- [`third_party/mobile_use`](third_party/mobile_use/) – portions of [mobile-use](https://github.com/minitap-ai/mobile-use) by Minitap, Inc., Apache License 2.0.

