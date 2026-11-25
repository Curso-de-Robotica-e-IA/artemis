# ARTEMIS Workspace Rules (`AGENTS.md`)

You are working in the **ARTEMIS** repository (*Autonomous Multimodal Mobile AI Engine*, Python 3.12+). Follow these invariant architectural and engineering rules across all tasks.

---

## 1. Environment & Tool Execution
- Always run Python tools and scripts via `uv run` (e.g., `uv run pytest`, `uv run ruff`, `uv run pyright`, `uv run artemis`).
- Never introduce private package indexes or non-PyPI URLs in `pyproject.toml` or `uv.lock` (enforced by `scripts/check_dependency_sources.py`).

## 2. Code Style, Imports & Quality Ratchet
- **Absolute Imports Only**: `pyproject.toml` enforces `ban-relative-imports = "all"` (`TID`). Always use full package paths (`from artemis...` or `from third_party.mobile_use...`).
- **Formatting & Target Version**: Line length is `100`, quote style is `"double"`, target Python version is `py312`.
- **Quality Debt Ratchet (`scripts/quality_ratchet.py` & `.quality-baseline.json`)**:
  - **Zero Silent Broad Exceptions**: Never write `except Exception: pass` (`silent_broad_exception_handlers` must remain `0`).
  - **No Debt Regression**: Do not increase `broad_exception_handlers` (`except Exception:`, `except BaseException:`, or bare `except:`) or `type_ignore_comments` (`# type: ignore`) above the counts in `.quality-baseline.json`. Always catch specific exception types (`OSError`, `ValueError`, `ValidationError`, `TimeoutError`, etc.).
  - When removing existing broad exception handlers or `# type: ignore` comments, lower the baseline numbers in `.quality-baseline.json` accordingly.
- **Protected Core Typechecking (`pyright-core.json`)**:
  - Any changes to modules listed in `pyright-core.json` must pass `uv run pyright --project pyright-core.json` with zero errors.

## 3. Core Architectural Contracts
- **LangGraph State & Node Visibility (`artemis/graph/state.py` & `artemis/graph/visibility.py`)**:
  - `State` uses `extra="forbid"`. Construct initial state exclusively via `State.initial(goal)`.
  - Every `State` field read or written by a graph node (`perception`, `planner`, `operator`, `execution_check`, `validator`, `summarizer`, `convergence`, `exit_settlement`) must be explicitly declared in `NODE_VISIBILITY` (`artemis/graph/visibility.py`).
  - Nodes must wrap incoming state with `state = strict_state(state, "<node_name>")` so tests running with `ARTEMIS_STRICT_STATE=1` police undeclared reads and writes.
- **MCP Action Manifest & Prompt Assembly (`artemis/mcp/action_manifest.py` & `artemis/agents/prompt_assembly.py`)**:
  - Never add a tool name to an agent prompt (`artemis/agents/operator/prompts.py`, `artemis/agents/flash/flash_runner.md`, etc.) without classifying it in `artemis/mcp/action_manifest.py` (`REQUIRED_ACTIONS`, `OPTIONAL_ACTIONS`, `INTERNAL_ACTIONS`, or `BACKEND_INDEPENDENT_TOOLS`) and wiring conditional rendering in `artemis/agents/prompt_assembly.py`.
- **`artemis` vs `third_party/mobile_use` Boundary**:
  - Never import private symbols (names starting with `_`, except dunder names) across the boundary between `artemis/` and `third_party/mobile_use/` (enforced by `tests/unit/test_third_party_boundary.py`).
  - First-party files under `artemis/`, `tests/`, and `scripts/` must carry the Google Apache 2.0 header and never contain third-party attribution headers.
  - If moving a module from `artemis/` to `third_party/mobile_use/`, register its old path in `LEGACY_MODULES` inside `artemis/_legacy_imports.py`.
- **Coordinate Space Separation (`artemis/utils/coordinates.py`)**:
  - VLM/LLM prompts and decisions operate in normalized coordinates (`0..1000`) or UI element indices (`[n]`).
  - Never overwrite or scale the native hardware `width` and `height` stored in `DeviceContext` or controller state, even if downscaling screenshot images for prompt payloads.
- **Bundled Runtime Resources (`artemis/resources/config/artemis.jsonc`)**:
  - Structural configuration changes in `config/artemis.jsonc` and `artemis/config/` must stay compatible with the packaged default config at `artemis/resources/config/artemis.jsonc` (verified by `tests/unit/test_packaging_resources.py`).

## 4. Specialized Agent Skills & Subagent Orchestration
This workspace includes 4 specialized skills in `.agents/skills/` that can be activated on demand or delegated to dedicated subagents (`define_subagent` / `invoke_subagent`):
1. **`artemis-feature`** (`.agents/skills/artemis-feature/SKILL.md`): Feature engineering (new tools, graph nodes, sub-agents, SDK/CLI capabilities, LLM providers).
2. **`artemis-refactor`** (`.agents/skills/artemis-refactor/SKILL.md`): Architecture refactoring, paying down `.quality-baseline.json` debt, expanding `pyright-core.json`, and cleaning `third_party` boundaries.
3. **`artemis-test`** (`.agents/skills/artemis-test/SKILL.md`): Writing hermetic unit tests (`ARTEMIS_FAKE_LLM=1`, `ARTEMIS_STRICT_STATE=1`, `MockDriver`, `MockActuator`), tool/prompt contract tests, and marked integration/device tests.
4. **`artemis-qa`** (`.agents/skills/artemis-qa/SKILL.md`): End-to-end quality assurance, CI gate verification, wheel packaging checks, `artemis doctor` diagnostics, and live execution trace auditing.
