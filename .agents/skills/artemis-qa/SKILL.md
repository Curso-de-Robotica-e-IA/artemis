---
name: artemis-qa
description: >-
  Use this skill when performing quality assurance (QA), pre-PR or release readiness verification, CI pipeline validation, wheel packaging smoke tests, environment diagnostics via artemis doctor, or auditing live Android execution traces in ARTEMIS.
---

# ARTEMIS QA & Release Gatekeeper Agent (`artemis-qa`)

Follow this end-to-end verification protocol to audit code quality, architectural invariants, packaging integrity, and live multimodal execution traces in **ARTEMIS**.

---

## Gate 1: Static Analysis, Supply Chain & Quality Ratchet

Run every check enforced by `.github/workflows/ci.yml` in sequence. All commands must exit with code `0`:

```powershell
# 1. Verify all locked dependencies come strictly from public PyPI
python scripts/check_dependency_sources.py

# 2. Check formatting (line-length 100, double quotes)
uv run ruff format --check .

# 3. Check linting & absolute import rules (E, F, TID, UP)
uv run ruff check .

# 4. Enforce exception-handler and type-ignore debt ratchet
uv run python scripts/quality_ratchet.py

# 5. Type-check protected core modules
uv run pyright --project pyright-core.json
```

---

## Gate 2: Hermetic Test Suite, Contracts & Coverage Floor

Run the deterministic test suite and verify that branch/line coverage across `artemis/` and `third_party/` meets the `60%` minimum requirement:

```powershell
# 1. Full deterministic test suite with coverage gate
uv run pytest tests/unit tests/tools -q --cov=artemis --cov=third_party --cov-fail-under=60

# 2. Verify safe collection of all integration/e2e/device test files
uv run pytest tests --collect-only -q
```

---

## Gate 3: Package Build & Bundled Resource Verification

Verify that the distribution builds cleanly and includes all required runtime assets (`artemis.jsonc`, helper APK manifest) without leaking development artifacts:

```powershell
# 1. Run packaging resource unit tests
uv run pytest tests/unit/test_packaging_resources.py tests/unit/test_install_paths.py

# 2. Verify CLI entrypoints respond properly
uv run artemis --version
uv run artemis --help
```

---

## Gate 4: Environment Diagnostics & Live Device Trace Audit

When verifying runtime behavior or diagnosing execution issues on an Android device/emulator:

### 4.1 Toolchain & Device Readiness
```powershell
uv run artemis doctor
uv run artemis helper status
```
- Confirm ADB server connectivity, RSA key status, `scrcpy` / `ffmpeg` availability, and whether `ArtemisAccessibilityHelper` APK is installed and active on the target device.

### 4.2 Live Smoke Execution (If Unlocked Android Device/Emulator is Connected)
```powershell
uv run artemis run "Open Settings and check Battery level" --profile flash --max-steps 5
```

### 4.3 Multimodal Trace Inspection (`traces/<session_id>/`)
Inspect the generated trace artifacts using `uv run artemis trace list` / `uv run artemis trace view <session_id>` or by reading the session files directly:
1. **`status.json`**:
   - Check `status` (`completed` vs `failed` / `cancelled`).
   - Check `hierarchy_backend` (`helper` vs fallback `uiautomator`) and `hierarchy_backend_note` to detect mid-run accessibility service drops.
   - Check token usage and cache hit ratios from `token_meter`.
2. **`steps.json`**:
   - Verify each step's `coordinates` (`0..1000` normalized) vs `pixel_coordinates` (denormalized against native screen `width` and `height`).
   - Check `last_execution_result` and any `open_incident` / `last_closed_incident` entries to spot false-positive Safety Net blocks (`precondition_pixel.py` / `precondition_xml.py`).
3. **Pro Profile Checkpoints (`run_outcome.json` / `task_plan.md`)**:
   - For `--profile pro` runs, verify that `Checker` verdicts (`passed`, `failed`, `inconclusive`, `unchecked`, `retired`) settled cleanly before `exit_settlement_node` exited.

---

## QA Sign-Off Report Template

Always conclude a QA audit with a structured summary table:

| Gate | Check | Status (`PASS`/`FAIL`/`SKIP`) | Details / Metrics |
| :--- | :--- | :---: | :--- |
| **Gate 1** | Supply Chain (`check_dependency_sources.py`) | | |
| **Gate 1** | Ruff Format & Lint (`TID` absolute imports) | | |
| **Gate 1** | Quality Ratchet (`.quality-baseline.json`) | | `broad_exception_handlers`, `type_ignore_comments` |
| **Gate 1** | Pyright Core (`pyright-core.json`) | | |
| **Gate 2** | Hermetic Tests & Coverage (`>= 60%`) | | Total passed, coverage `%` |
| **Gate 3** | Packaging & CLI Entrypoint Smoke | | |
| **Gate 4** | `artemis doctor` & Live Trace Audit | | Device status / Session ID |

---

## Subagent Invocation Template (For Orchestrators)

When invoking a dedicated QA subagent via `define_subagent` / `invoke_subagent`:
- **Name**: `artemis-qa-agent`
- **Role**: `Artemis QA & Release Gatekeeper`
- **EnableWriteTools**: `false` (or `true` only if authorized to fix trivial formatting)
- **EnableMCPTools**: `true`
- **Prompt Instruction**: *"Read `.agents/skills/artemis-qa/SKILL.md` and `AGENTS.md`. Execute Gates 1 through 4, audit any recent traces in `traces/` if applicable, and return the completed QA Sign-Off Report."*
