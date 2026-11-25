---
name: artemis-test
description: >-
  Use this skill when writing, fixing, or expanding unit tests, tool and prompt contract tests, LangGraph state visibility tests, or Android integration and e2e tests in the ARTEMIS repository.
---

# ARTEMIS Test Agent (`artemis-test`)

Follow this runbook to write deterministic, high-signal tests across **ARTEMIS**'s multi-layered test architecture.

---

## 1. Test Suite Architecture & Layers

ARTEMIS partitions tests into distinct layers (configured in `pyproject.toml` and `Makefile`):

| Layer | Directory | Marker | Command | Requirements |
| :--- | :--- | :--- | :--- | :--- |
| **Unit (Default CI)** | `tests/unit/` | *(unmarked)* | `uv run pytest` | 100% hermetic. Zero ADB, zero network, zero API keys. |
| **Tool Contracts (Default CI)** | `tests/tools/<agent>/` | *(unmarked)* | `uv run pytest tests/tools` | Hermetic tool schema, declaration, and wrapper validation. |
| **Integration** | `tests/integration/` | `@pytest.mark.integration` | `make test-integration` | Cross-component tests; may use configured LLM credentials. |
| **Device / E2E** | `tests/integration/`, `tests/e2e/` | `@pytest.mark.android`, `@pytest.mark.e2e` | `make test-device` | Requires an attached, unlocked Android device or emulator. |

> **Critical Rule**: Every test file under `tests/integration/` or `tests/e2e/` must remain **safely collectable** (`uv run pytest --collect-only`) even when no Android device, emulator, or API key is present. Never execute hardware connections or env-var assertions at module import time!

---

## 2. Writing Hermetic Unit Tests (`tests/unit/` & `tests/tools/`)

Default `pytest` runs with `-m "not integration and not e2e and not cloud and not manual and not android"` and enforces `--cov-fail-under=60` across `artemis` and `third_party`.

### Built-in Test Doubles & Fixtures to Use
1. **Fake LLM (`ARTEMIS_FAKE_LLM=1` & `artemis/llm/fake_model.py`)**:
   - Set `monkeypatch.setenv("ARTEMIS_FAKE_LLM", "1")` to bypass real API calls and connection pre-warming.
   - Or inject canned structured responses/tool calls using helpers from `artemis/llm/fake_model.py`.
2. **Mock Device Driver & Actuator**:
   - Use `artemis.drivers.mock.mock_driver.MockDriver` for driver-level tests (`tests/unit/test_drivers.py`).
   - Use `artemis.mcp.actuators.mock.MockActuator` for MCP action execution and session tests (`tests/unit/mcp/`).
3. **Strict Graph State Policing (`ARTEMIS_STRICT_STATE=1`)**:
   - Whenever testing a LangGraph node (`planner`, `perception`, `operator`, `execution_check`, `validator`, `summarizer`, `exit_settlement`), always enable strict state visibility:
     ```python
     def test_my_graph_node(monkeypatch):
         monkeypatch.setenv("ARTEMIS_STRICT_STATE", "1")
         state = State.initial("Open Settings")
         ...
     ```
   - Assert that node return dictionaries pass `check_update("<node_name>", update_dict)` from `artemis.graph.visibility`.
4. **Temporary DataEngine & Trace Isolation (`tmp_path`)**:
   - Always route `traces_dir` / `DataEngine` storage to pytest's `tmp_path` fixture so tests never pollute the workspace `traces/` folder.

---

## 3. Contract Tests You Must Maintain

When features or refactorings touch core contracts, add or update cases in these suites:

- **State & Visibility Contract**: `tests/unit/graph/test_state_contract.py`
  - Verifies `State.initial()` round-trip serialization, rejects unknown fields (`extra="forbid"`), and runs `validate_manifest()`.
- **Plan Mutation & Checkpoint Guardrails**: `tests/unit/agents/test_plan_mutation_guardrails.py` & `tests/unit/graph/test_checkpoints.py`
  - Verifies `[Loop:continuous]` protection, nested subgoal completion checks, and deterministic restoration of deleted check items (`@end` / `@on_complete`).
- **MCP Action Manifest & Prompt Assembly**: `tests/unit/mcp/test_action_manifest.py` & `tests/unit/agents/test_prompt_assembly.py`
  - Scans prompts to ensure every tool mentioned is classified in `artemis/mcp/action_manifest.py` and properly filtered when an actuator omits optional tools.
- **Cross-Boundary Imports**: `tests/unit/test_third_party_boundary.py`
  - Ensures all imports between `artemis/` and `third_party/mobile_use/` resolve to public symbols.
- **Packaging Resources**: `tests/unit/test_packaging_resources.py`
  - Ensures bundled `artemis.jsonc` and helper APK manifests are intact.

---

## 4. Running & Measuring Coverage

```powershell
# Run fast hermetic unit + tool tests
uv run pytest

# Run with coverage report (must stay >= 60%)
uv run pytest tests/unit tests/tools -q --cov=artemis --cov=third_party --cov-fail-under=60

# Verify all test trees collect cleanly without hardware attached
uv run pytest tests --collect-only -q
```

---

## 5. Subagent Invocation Template (For Orchestrators)

When invoking a dedicated Test subagent via `define_subagent` / `invoke_subagent`:
- **Name**: `artemis-test-agent`
- **Role**: `Artemis Test & Contract Engineer`
- **EnableWriteTools**: `true`
- **EnableMCPTools**: `false`
- **Prompt Instruction**: *"Read `.agents/skills/artemis-test/SKILL.md` and `AGENTS.md`. Write comprehensive hermetic unit and contract tests for the target changes using `ARTEMIS_STRICT_STATE=1`, `ARTEMIS_FAKE_LLM=1`, and `tmp_path`. Verify all tests pass and coverage remains >= 60%."*
