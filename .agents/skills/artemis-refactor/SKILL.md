---
name: artemis-refactor
description: >-
  Use this skill when refactoring code in ARTEMIS, paying down technical debt, reducing broad exception handlers or type-ignore comments in .quality-baseline.json, expanding Pyright typechecking coverage in pyright-core.json, or decoupling modules across the artemis and third_party/mobile_use boundary.
---

# ARTEMIS Refactor Agent (`artemis-refactor`)

Follow this runbook to execute safe, behavior-preserving refactorings while ratcheting down technical debt in **ARTEMIS**.

---

## 1. Paying Down the Quality Debt Ratchet (`.quality-baseline.json`)

`scripts/quality_ratchet.py` scans all `.py` files under `artemis/` and `third_party/` for three metrics tracked in `.quality-baseline.json`:
- `broad_exception_handlers`: `except Exception:`, `except BaseException:`, or bare `except:`
- `silent_broad_exception_handlers`: `except Exception: pass` (must always be `0`)
- `type_ignore_comments`: `# type: ignore`

### Step-by-Step Ratchet Workflow
1. **Measure Current Debt**:
   ```powershell
   uv run python scripts/quality_ratchet.py --print-current
   ```
2. **Narrow Exception Handlers**:
   - Inspect what the `try` block actually calls and replace `except Exception:` with specific exception tuples:
     - File/OS/network/ADB I/O: `(OSError, TimeoutError, RuntimeError, ValueError)`
     - JSON/Pydantic parsing: `(json.JSONDecodeError, ValidationError, ValueError, KeyError, TypeError)`
     - Async cancellation: always let `asyncio.CancelledError` propagate or handle it explicitly before other exceptions.
   - Never leave a broad handler with only `pass`; always log at `logger.debug(...)` or `logger.warning(...)` if recovery is intentional.
3. **Eliminate `# type: ignore` Comments**:
   - Replace `# type: ignore` with proper type narrowing (`isinstance`, `assert x is not None`), `typing.cast`, `Protocol`, or updated Pydantic/LangChain type annotations.
4. **Lock In Improvements**:
   - Re-run `uv run python scripts/quality_ratchet.py --print-current`.
   - Whenever any metric drops below the committed value in `.quality-baseline.json`, **update `.quality-baseline.json` downward** so the debt can never return.

---

## 2. Expanding Protected Typecheck Coverage (`pyright-core.json`)

ARTEMIS uses incremental strict type checking via `pyright-core.json`.

1. **Check Candidate Modules**:
   Run Pyright directly on the refactored module(s):
   ```powershell
   uv run pyright artemis/<path_to_module>.py
   ```
2. **Fix Type Issues**:
   - Use modern Python 3.12+ type syntax (`list[str]`, `dict[str, Any]`, `X | None` instead of `Optional[X]`).
   - Ensure Pydantic models and dataclasses have complete field annotations.
3. **Promote Clean Modules to `pyright-core.json`**:
   - Add the clean module path to the `"include"` array in `pyright-core.json`.
   - Verify the entire core suite passes:
     ```powershell
     uv run pyright --project pyright-core.json
     ```

---

## 3. Managing the `artemis` <-> `third_party/mobile_use` Boundary

Code derived from `mobile-use` lives in `third_party/mobile_use/`. When refactoring across or within these packages:

1. **Public Symbols Only Across the Boundary**:
   - `tests/unit/test_third_party_boundary.py` parses the AST of every file in `artemis/` and `third_party/`.
   - Any symbol imported from `artemis` into `third_party` (or vice versa) **must not** contain a private path segment starting with `_` (e.g., `_helper`). Promote shared helpers to public names and list them in `__all__`.
2. **Preserving Legacy Import Paths (`artemis/_legacy_imports.py`)**:
   - If you move a module from `artemis/` into `third_party/mobile_use/`, add its old dotted path to `LEGACY_MODULES` in `artemis/_legacy_imports.py` so external callers and existing patches receive a `DeprecationWarning` instead of an `ImportError`.
3. **License & Attribution Headers**:
   - First-party files in `artemis/`, `tests/`, and `scripts/` must carry the Google LLC Apache 2.0 header and must **never** include `"derived from"`, `"originally from"`, or `"minitap"` in their leading comment block.

---

## 4. Import & Structural Hygiene

- **Ban Relative Imports (`TID`)**: Convert any `from .module import X` or `from ..sub import Y` to absolute imports (`from artemis.sub import Y`).
- **State Tightening (`artemis/graph/visibility.py`)**:
  - When refactoring a graph node in `artemis/agents/` or `artemis/graph/`, check if any fields in `NODE_VISIBILITY[node].reads` or `writes` are no longer touched, and tighten the manifest accordingly.

---

## 5. Refactor Verification Checklist

Run the full verification suite after every refactoring batch:

```powershell
uv run ruff format --check .
uv run ruff check .
uv run python scripts/quality_ratchet.py
uv run pyright --project pyright-core.json
uv run pytest
```

---

## 6. Subagent Invocation Template (For Orchestrators)

When invoking a dedicated Refactor subagent via `define_subagent` / `invoke_subagent`:
- **Name**: `artemis-refactor-agent`
- **Role**: `Artemis Architecture & Debt Refactorer`
- **EnableWriteTools**: `true`
- **EnableMCPTools**: `false`
- **Prompt Instruction**: *"Read `.agents/skills/artemis-refactor/SKILL.md` and `AGENTS.md`. Refactor the target modules while preserving 100% behavioral parity, narrow broad exception handlers, lower `.quality-baseline.json` if debt is reduced, and verify with ruff, pyright, and pytest."*
