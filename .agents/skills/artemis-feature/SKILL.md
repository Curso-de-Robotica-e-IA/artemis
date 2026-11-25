---
name: artemis-feature
description: >-
  Use this skill when designing or implementing new features in ARTEMIS, such as adding new MCP or mobile device tools, creating or modifying LangGraph nodes and sub-agents, extending FlashRunner or Pro execution profiles, adding LLM providers, or building new SDK and CLI commands.
---

# ARTEMIS Feature Agent (`artemis-feature`)

Follow this step-by-step engineering runbook when implementing new features or extending capabilities in the **ARTEMIS** repository.

---

## 1. Adding a New Device Action or Agent Tool (5-Point Pipeline)

When adding a new tool or device action for the Operator, FlashRunner, or Explorer:

1. **Implementation**:
   - For logical/sub-agent/system tools: implement in `artemis/tools/` (and expose via `artemis/tools/index.py` or `artemis/tools/tool_wrapper.py`).
   - For physical/device actions: implement actuator methods in `artemis/mcp/actuators/adb.py` and the hermetic test double in `artemis/mcp/actuators/mock.py` (inheriting from `artemis/mcp/actuators/base.py`).
2. **Manifest Classification (`artemis/mcp/action_manifest.py`)**:
   - Classify the tool in `OPTIONAL_ACTIONS` (for device actions) or `BACKEND_INDEPENDENT_TOOLS` (for notes, sub-agents, diagnostics, or ADB shell helpers).
   - Only place a tool in `REQUIRED_ACTIONS` if a prompt structurally cannot be assembled without it.
3. **MCP Specs & Executor (`artemis/mcp/`)**:
   - Register the action name in `artemis/mcp/action_names.py`, parameter schema in `artemis/mcp/action_specs.py` / `artemis/mcp/action_types.py`, and dispatch logic in `artemis/mcp/action_executor.py`.
4. **LLM Tool Declarations**:
   - Add the `ToolDeclaration` to `artemis/agents/validator/tool_declarations.py` (and `artemis/agents/explorer/tool_declarations.py` if exposed to Explorer).
5. **Conditional Prompt Assembly (`artemis/agents/prompt_assembly.py`)**:
   - Any mention of the tool inside `artemis/agents/operator/prompts.py`, `artemis/agents/flash/flash_runner.md`, or sub-agent `.md`/`.json` files **must** be gated through `artemis/agents/prompt_assembly.py` so that backends omitting optional actions do not leak dead tool references into prompts.
   - If action surface fixtures are affected, check `tests/fixtures/action_surfaces/`.

---

## 2. Extending the Pro Profile Graph (`artemis/graph/`)

When modifying the LangGraph state machine or adding/updating nodes:

1. **Explicit State Schema (`artemis/graph/state.py`)**:
   - Add new fields to `State` (`extra="forbid"`) using explicit typing and reducers (`Annotated[T, "description", take_last]` or `sticky_or`).
   - Ensure `State.initial(goal)` continues to round-trip cleanly via `State(**state.model_dump())`.
2. **Visibility Manifest (`artemis/graph/visibility.py`)**:
   - Update `NODE_VISIBILITY` for every node that reads (`reads`) or mutates/returns (`writes`) the field.
   - In the node function, wrap the incoming state with `state = strict_state(state, "<node_name>")`.
3. **Task Plan Guardrails (`artemis/utils/plan_grammar.py` & `artemis/graph/graph.py`)**:
   - If touching `task_plan.md` handling or checkpoints (`artemis/graph/checkpoints.py`), preserve the machine-channel protections:
     - Incomplete nested subgoals cannot be bypassed when top-level milestones are marked `[x]`.
     - `[Loop:continuous]` milestones cannot be deleted or marked `[x]` unless `state.user_stop_requested` is `True`.
     - `checker-` / `checker:` note keys remain read-only for model-side writes.

---

## 3. Extending the Flash Profile & Memory (`artemis/agents/flash/` & `artemis/memory/`)

When modifying `FlashRunner` (`artemis/agents/flash/runner.py`) or `TranscriptLedger` (`artemis/memory/transcript.py`):

1. **Preserve the 4-Region Memory Layout (`S / F / A / T`)**:
   - **Region S (Stable Prefix)**: Must remain static across turns so provider prompt caching (`Gemini` / `Anthropic`) is never invalidated.
   - **Region F (Frozen History)**, **Region A (Active Window)**, and **Region T (Current Tail)**: Ensure L1 (text scrubbing), L2 (image summarization via `artemis/agents/flash/summarizer.py`), and L3 (`artemis/memory/chunking.py`) never block the critical execution path if background compression fails.
2. **Coordinate & Image Scaling Invariants**:
   - Use `artemis/utils/image_scaling.py` and `artemis/utils/coordinates.py`.
   - Keep model-facing coordinates in the `0..1000` normalized space and preserve native device `width` and `height` in `DeviceContext`.

---

## 4. Configuration, LLM Routing & CLI Extensions

- **Configuration (`artemis/config/` & `config/artemis.jsonc`)**:
  - Define strongly typed Pydantic schemas in `artemis/config/agent.py`, `artemis/config/llm.py`, or `artemis/config/settings.py`.
  - Mirror new default keys in both `config/artemis.jsonc` and `artemis/resources/config/artemis.jsonc`.
- **LLM Providers (`artemis/llm/router.py` & `artemis/services/llm.py`)**:
  - Route all LLM calls through `RobustChatModelWrapper` so circuit breakers, transient failure classification, fallback models, token metering (`artemis/services/token_meter.py`), and `ARTEMIS_FAKE_LLM=1` work uniformly.
- **CLI Commands (`artemis/interfaces/cli/commands/`)**:
  - Register new Typer subcommands in `artemis/interfaces/cli/main.py` and verify that `artemis/main.py` (`normalize_argv`) automatically recognizes the new root command token.

---

## 5. Feature Verification Checklist

Before handing off to Refactor, Test, or QA, run:

```powershell
uv run ruff check .
uv run python scripts/quality_ratchet.py
uv run pyright --project pyright-core.json
uv run pytest tests/unit/graph/test_state_contract.py tests/unit/mcp/test_action_manifest.py tests/unit/test_third_party_boundary.py
```

---

## 6. Subagent Invocation Template (For Orchestrators)

When invoking a dedicated Feature subagent via `define_subagent` / `invoke_subagent`:
- **Name**: `artemis-feature-agent`
- **Role**: `Artemis Feature Engineer`
- **EnableWriteTools**: `true`
- **EnableMCPTools**: `true`
- **Prompt Instruction**: *"Read `.agents/skills/artemis-feature/SKILL.md` and `AGENTS.md` before making changes. Implement the requested feature adhering strictly to State visibility, MCP action manifest contracts, and quality ratchet baselines."*
