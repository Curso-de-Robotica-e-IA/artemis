# Use ARTEMIS inside another project

This template lets any Python project run ARTEMIS tasks directly, with no ARTEMIS server to start.

**You need:** [uv](https://docs.astral.sh/uv/), Android platform-tools (`adb`) on your `PATH`,
`git` (uv installs ARTEMIS from GitHub), and a phone or emulator listed in `adb devices`.

## 1. Copy the files

Copy these into your project root:

- `artemis_runner.py`
- `.env.example`, then copy it to `.env` and add your API key

## 2. Install ARTEMIS

Set Python 3.12 (`echo 3.12 > .python-version`) and add this to your `pyproject.toml`:

```toml
[project]
requires-python = ">=3.12"
dependencies = [
    "artemis",
]

[tool.uv.sources]
# ARTEMIS from GitHub. artemis-client lives in a subfolder of the same repository.
artemis = { git = "https://github.com/Curso-de-Robotica-e-IA/artemis", branch = "main" }
artemis-client = { git = "https://github.com/Curso-de-Robotica-e-IA/artemis", branch = "main", subdirectory = "packages/artemis-client" }
```

Then run `uv sync`. The exact commit is saved in `uv.lock`; run `uv lock --upgrade-package artemis`
to pick up newer ARTEMIS changes. To stay on one version, replace `branch = "main"` with
`tag = "<tag>"` or `rev = "<commit>"` in both lines.

If `uv sync` reports a version conflict, one of your packages pins a version ARTEMIS cannot use.
Fix it by limiting `requires-python` (e.g. `">=3.12, <3.13"`) or, after checking the package still
works, by overriding the pin:

```toml
[tool.uv]
override-dependencies = ["pywin32>=310"]
```

## 3. Add to `.gitignore`

```
.env
traces/
```

## 4. Use it

```python
from artemis_runner import ArtemisRunner

with ArtemisRunner(device_serial="emulator-5554") as artemis:
    print(artemis.run("Open Settings and check the Wi-Fi status"))
    artemis.run("Turn on airplane mode", timeout=120)
    artemis.run("Find the battery percentage", profile="pro", output="the percentage as a number")
```

- `device_serial`: from `adb devices`. Leave it out to use the first device.
- `profile`: `"flash"` (fast, default) or `"pro"` (plans and checks its work, slower).
- `timeout`: seconds per task (default 300). The task is stopped and `TimeoutError` raised.
- `output`: optional answer format, either a text description or a Pydantic model class.

Without `with`, call `start()` first (or let the first `run()` start it) and `close()` at the end.

Import `artemis_runner` before anything else from `artemis`. It loads your `.env` first, because
ARTEMIS reads its settings when it is imported.

## Traces

Every task saves a trace (step log, screenshots, action overlays) for debugging. `artemis_runner`
saves them in `traces/` in your project root. To use another folder, set `ARTEMIS_TRACES_DIR` in
`.env` (absolute path, or relative to where you run Python). Without the runner, ARTEMIS falls back
to its user data folder, so set `ARTEMIS_TRACES_DIR` yourself in that case.

Traces include screenshots of the phone, so keep them out of git (see step 3) and delete old ones
when they take too much space.

## Optional: choose the AI models

Copy [`config/artemis.jsonc`](https://github.com/Curso-de-Robotica-e-IA/artemis/blob/main/config/artemis.jsonc)
from the ARTEMIS repository into your project root and edit it. It is used
automatically and **replaces** the ARTEMIS defaults (not merged), so keep the whole file.

## Async projects

Use the agent directly instead of the runner (but still `import artemis_runner` first, for the `.env`):

```python
import artemis_runner  # noqa: F401  (loads .env before artemis)
from artemis import Agent

agent = Agent(device_serial="emulator-5554")
await agent.init()
try:
    result = await agent.run_task(goal="Open Settings", profile="flash")
finally:
    await agent.clean()
```

## Checklist

- [ ] Python 3.12, `artemis` and `artemis-client` from GitHub in `[tool.uv.sources]`, `uv sync`
- [ ] `.env` with an API key, `.env` and `traces/` (or your `ARTEMIS_TRACES_DIR`) in `.gitignore`
- [ ] Phone listed in `adb devices`
