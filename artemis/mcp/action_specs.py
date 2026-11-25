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

"""The canonical device-action manifest: every dialect of every action, side by side.

Historically each device action was declared independently at three model-facing
sites -- the Operator's inline LangChain shells, the Validator/Flash
``ToolDeclaration`` constants, and the action MCP server's function signatures -- and
the declarations drifted apart (``click.target`` took an element index in one place
and only coordinates in another). This module is now the single place all three are
*defined*; the historical sites import their surface from here, generated.

An action has one agent-facing dialect and one wire dialect:

* ``operator`` -- the agent dialect. Both profiles receive the same indexed
  "Visible UI Elements" list, so target parameters accept ``int`` element indices
  as well as normalized coordinates. The Pro Operator binds it as a LangChain shell
  (declaration-only, "Action Recorded": the Operator's translate step lowers the
  call into structured decisions); the FlashRunner binds the very same description
  and parameters projected onto a JSON ``ToolDeclaration`` (:func:`tool_declaration`),
  and ``McpActionExecutor`` resolves an index against the current element list.
  One definition, two bindings: the two profiles can no longer drift apart.
* ``declaration`` -- a JSON-only agent dialect, used solely for actions that have
  no operator shell (``click_sequence`` is declared to Flash alone).
* ``wire`` -- what the action MCP server serves, mirroring the actuator protocol
  one-to-one: normalized coordinates, millisecond durations, no addressing sugar.
  The client-side executor (``McpActionExecutor``) lowers the agent dialect onto it.

A coordinate target names nothing by itself, so the agent dialect pairs it with
``target_description`` (``target_descriptions`` for a ``click_sequence``): the
model's own statement of what it is aiming at. It is required whenever the target
is a coordinate, recorded verbatim on the action, and never sent to the wire. An
element index needs none -- its text, bounds and id are read from the indexed
list -- and the recorded fields stay separate (``target_text``/``target_bounds``/...
for observed elements, ``target_description`` for described coordinates) so no
reader can mistake the model's belief for an observation.

``tests/unit/mcp/test_action_specs.py`` pins the generated schema of every model
surface and checks that the Flash declaration of each shared action is exactly the
operator shell's schema, so a divergence between what one profile sees and what the
other sees -- the historical definition of drift -- fails CI instead of shipping.

The classification of actions (required/optional/internal) stays in
``action_manifest``; this module holds their schemas and teaching text.
"""

from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from functools import cache
import inspect
import types
from typing import Annotated, Any, Literal, Union, get_args, get_origin

from langchain_core.tools import StructuredTool
from mcp.types import CallToolResult
from pydantic import Field, create_model

from artemis.core.tool_declaration import ToolDeclaration
from artemis.mcp.action_types import ActionResult

__all__ = [
    "ACTION_SPECS",
    "ActionSpec",
    "DeclarationDialect",
    "EXCEPTION_PREFIXES",
    "OPERATOR_SHELL_ORDER",
    "OperatorDialect",
    "ParamSpec",
    "WireDialect",
    "exception_prefix",
    "make_wire_handler",
    "operator_shell_tool",
    "tool_declaration",
    "wire_dialects",
]


# --- Spec structure ------------------------------------------------------------------


@dataclass(frozen=True)
class ParamSpec:
    """One parameter of a Python-typed dialect (operator shell or wire)."""

    name: str
    annotation: Any
    description: str | None = None
    required: bool = True
    default: Any = None


@dataclass(frozen=True)
class OperatorDialect:
    """The agent dialect: index-capable targets, one wording for both profiles.

    Bound by the Pro Operator as a declaration-only LangChain shell and by the
    FlashRunner as the JSON projection of the same parameters.
    """

    description: str
    params: tuple[ParamSpec, ...]


@dataclass(frozen=True)
class DeclarationDialect:
    """A JSON-only agent dialect, for actions without an operator shell."""

    description: str
    parameters: dict[str, Any]


@dataclass(frozen=True)
class WireDialect:
    """The action-server tool mirroring one actuator method.

    ``bind`` receives the actuator and the validated, default-filled argument dict
    and performs the (tiny) call-site conversion the historical hand-written wrapper
    performed -- typically ``int()`` coercion of coordinate pairs.
    """

    description: str
    params: tuple[ParamSpec, ...]
    bind: Callable[[Any, dict[str, Any]], Awaitable[ActionResult]]


@dataclass(frozen=True)
class ActionSpec:
    """All dialects of one canonical device action, plus their declared differences.

    Attributes:
        name: The canonical tool name every dialect shares.
        operator: The agent dialect (Operator shell and, projected, the Flash
            declaration), or ``None`` when neither profile binds this action as a
            shell.
        declaration: A JSON-only agent dialect for actions without an operator
            shell, or ``None`` (an action can be wire-only, reachable through
            executors but never declared to a model directly).
        wire: Action-server dialect; ``None`` only for purely virtual actions.
        differences: Human-readable record of every deliberate divergence between
            the agent dialect and the wire (semantics, not just spelling). Empty
            means the dialects align modulo the documented dialect philosophy above.
    """

    name: str
    operator: OperatorDialect | None = None
    declaration: DeclarationDialect | None = None
    wire: WireDialect | None = None
    differences: str = ""


# --- Shared type aliases -------------------------------------------------------------

#: Agent-dialect target: element index into the indexed UI list, or a normalized
#: [x, y] pair.
IndexOrCoords = int | list[int]

Direction = Literal["up", "down", "left", "right"]

#: Wording for the description that a coordinate target must carry. An element
#: index already names its element (its text, id and bounds are recorded from the
#: indexed list); a bare coordinate names nothing, so the model states what it is
#: aiming at and that statement is recorded as ``target_description``.
_TARGET_DESCRIPTION = (
    "What the target is, in a few words (e.g. 'play button', 'search input',"
    " 'video body'). REQUIRED when target is a coordinate pair; ignored for an"
    " element index."
)


# --- Wire bindings -------------------------------------------------------------------
# Each binding reproduces the exact conversion of the historical hand-written FastMCP
# wrapper it replaces. Arguments arrive validated and default-filled.


async def _wire_click(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.click(
        int(a["target"][0]), int(a["target"][1]), times=a["times"], delay_ms=a["delay_ms"]
    )


async def _wire_click_sequence(actuator: Any, a: dict[str, Any]) -> ActionResult:
    points = [(int(p[0]), int(p[1])) for p in a["sequence"]]
    return await actuator.click_sequence(points, delay_ms=a["delay_ms"])


async def _wire_long_press(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.long_press(
        int(a["target"][0]), int(a["target"][1]), duration_ms=a["duration_ms"]
    )


async def _wire_input_text(actuator: Any, a: dict[str, Any]) -> ActionResult:
    target = a["target"]
    norm = (int(target[0]), int(target[1])) if target else None
    return await actuator.input_text(a["text"], norm, clear_exist=a["clear_exist"])


async def _wire_swipe(actuator: Any, a: dict[str, Any]) -> ActionResult:
    start, end = a["start"], a["end"]
    return await actuator.swipe(
        (int(start[0]), int(start[1])), (int(end[0]), int(end[1])), a["duration_ms"]
    )


async def _wire_drag_and_drop(actuator: Any, a: dict[str, Any]) -> ActionResult:
    start, end = a["start"], a["end"]
    return await actuator.drag_and_drop(
        (int(start[0]), int(start[1])),
        (int(end[0]), int(end[1])),
        hold_ms=a["hold_ms"],
        duration_ms=a["duration_ms"],
    )


async def _wire_drag_and_hold(actuator: Any, a: dict[str, Any]) -> ActionResult:
    start, end = a["start"], a["end"]
    return await actuator.drag_and_hold(
        (int(start[0]), int(start[1])),
        (int(end[0]), int(end[1])),
        hold_ms=a["hold_ms"],
        duration_ms=a["duration_ms"],
    )


async def _wire_drop_after_hold(actuator: Any, a: dict[str, Any]) -> ActionResult:
    target = a["target"]
    norm = (int(target[0]), int(target[1])) if target else None
    return await actuator.drop_after_hold(norm, duration_ms=a["duration_ms"])


async def _wire_press_key(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.press_key(a["key"])


async def _wire_manage_app(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.manage_app(a["action"], a["app_name"])


async def _wire_wait_for_delay(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.wait_for_delay(a["time_in_ms"])


async def _wire_wait_for_text(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.wait_for_text(a["text"], a["wait_state"], a["timeout_ms"])


async def _wire_open_link(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.open_link(a["url"])


async def _wire_erase_one_char(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.erase_one_char()


async def _wire_focus_and_clear_text(actuator: Any, a: dict[str, Any]) -> ActionResult:
    return await actuator.focus_and_clear_text(int(a["target"][0]), int(a["target"][1]))


# --- The manifest --------------------------------------------------------------------

_SPECS: tuple[ActionSpec, ...] = (
    ActionSpec(
        name="click",
        operator=OperatorDialect(
            description=(
                "[ACTION] Tap once (or repeatedly on the SAME point via times=2, etc.) and"
                " release immediately.\n"
                "\n"
                "• Use for: buttons, links, checkboxes, tabs, icons, or double-tapping a"
                " single spot.\n"
                "• Do NOT use for: focusing a field before typing (call input_text directly"
                " — it focuses the field itself), pressing and holding (use long_press),"
                " dragging (use drag_and_drop or drag_and_hold), or tapping multiple"
                " DIFFERENT points rapidly in one burst."
            ),
            params=(
                ParamSpec(
                    "target",
                    IndexOrCoords,
                    "Tap target: an element index integer (e.g. 3) OR a 2-integer normalized"
                    " coordinate pair [x, y] in 0-1000 scale (e.g. [500, 600]). Never pass a"
                    " single coordinate number.",
                ),
                ParamSpec(
                    "target_description",
                    str | None,
                    _TARGET_DESCRIPTION,
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "times",
                    int,
                    "Number of consecutive taps on this SAME target (e.g. 2 for double-tap,"
                    " 7 for developer mode). Default is 1.",
                    required=False,
                    default=1,
                ),
                ParamSpec(
                    "delay_ms",
                    int,
                    "Delay in milliseconds between consecutive taps when times > 1. Default"
                    " is 100.",
                    required=False,
                    default=100,
                ),
            ),
        ),
        wire=WireDialect(
            description="Tap at a 0-1000 normalized [x, y] coordinate.",
            params=(
                ParamSpec("target", list[int]),
                ParamSpec("times", int, required=False, default=1),
                ParamSpec("delay_ms", int, required=False, default=100),
            ),
            bind=_wire_click,
        ),
        differences=(
            "target: the agent dialect accepts an element index or a coordinate pair"
            " (an index is resolved client-side against the indexed element list); the"
            " wire takes coordinates only. target_description: recorded, never sent"
            " to the wire; required only for coordinate targets."
        ),
    ),
    ActionSpec(
        name="click_sequence",
        declaration=DeclarationDialect(
            description=(
                "[ACTION] Tap multiple DIFFERENT [x, y] coordinate points in order as one"
                " atomic burst (the screen is captured ONLY after all taps finish).\n"
                "\n"
                "• Use for: transient / auto-dismissing overlays or control bars that would"
                " vanish during turn latency if tapped across separate turns.\n"
                "• Do NOT use for: repeated taps on the same spot (use click with times),"
                " normal multi-screen navigation (intermediate screens will not have loaded"
                " yet), or element indices — sequence ONLY accepts [x, y] coordinate pairs,"
                " NEVER integer indices."
            ),
            parameters={
                "type": "object",
                "properties": {
                    "sequence": {
                        "type": "array",
                        "items": {
                            "type": "array",
                            "items": {"type": "integer"},
                            "description": (
                                "Normalized coordinate pair [x, y] in 0-1000 scale"
                                " (e.g., [500, 280]). Element indices are NOT accepted."
                            ),
                        },
                        "description": (
                            "Ordered list of [x, y] coordinate pairs to tap, e.g."
                            " [[500, 280], [885, 362]]."
                        ),
                    },
                    "target_descriptions": {
                        "type": "array",
                        "items": {"type": "string"},
                        "description": (
                            "What each target is, in a few words, one entry per sequence"
                            " entry in the same order (e.g. ['video body', 'skip button'])."
                            " Required: a coordinate names nothing by itself."
                        ),
                    },
                    "delay_ms": {
                        "type": "integer",
                        "description": (
                            "Delay between consecutive taps in milliseconds (default 50ms)."
                        ),
                    },
                },
                "required": ["sequence", "target_descriptions"],
            },
        ),
        wire=WireDialect(
            description=("Tap a series of 0-1000 normalized [x, y] points in one atomic burst."),
            params=(
                ParamSpec("sequence", list[list[int]]),
                ParamSpec("delay_ms", int, required=False, default=50),
            ),
            bind=_wire_click_sequence,
        ),
        differences=(
            "Declared to Flash only (the Pro Operator chains actions as a fast-action"
            " burst instead); sequence entries are normalized coordinate pairs in both"
            " dialects (an element index is refused, never resolved client-side)."
            " target_descriptions is recorded per entry and never sent to the wire."
            " Unlike click, a burst is not resolved against the element list: its"
            " whole point is to outrun a transient UI, and the list was captured"
            " before the trigger tap."
        ),
    ),
    ActionSpec(
        name="long_press",
        operator=OperatorDialect(
            description=(
                "[ACTION] Press and hold in place for duration ms, then RELEASE the finger"
                " (DOWN -> WAIT -> UP).\n"
                "\n"
                "• Use for: opening context menus, entering multi-select mode, showing"
                " app-icon shortcuts, or opening the home-screen customization menu (on an"
                " empty wallpaper area).\n"
                "• Do NOT use for:\n"
                "  - Starting a drag, moving/resizing/removing icons or widgets, or holding"
                " before drop_after_hold: long_press ALWAYS releases the finger at the end!"
                " Call drag_and_drop or drag_and_hold + drop_after_hold directly instead"
                " (they already include the initial press delay hold_ms).\n"
                "  - Opening a transient control (such as a camera zoom dial) that"
                " auto-dismisses before the next turn: use a continuous drag_and_hold +"
                " drop_after_hold (or drag_and_drop) from the trigger instead.\n"
                "  - Never repeat long_press on the same target just changing duration if"
                " 1-2 attempts show no progress — switch to a different tool or target."
            ),
            params=(
                ParamSpec(
                    "target",
                    IndexOrCoords,
                    "Long press target: an element index integer (e.g. 3) OR a 2-integer"
                    " normalized coordinate pair [x, y] in 0-1000 scale (e.g. [500, 600]).",
                ),
                ParamSpec(
                    "target_description",
                    str | None,
                    _TARGET_DESCRIPTION,
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "duration",
                    int,
                    "Press duration in milliseconds before releasing (default 1000).",
                    required=False,
                    default=1000,
                ),
            ),
        ),
        wire=WireDialect(
            description="Long-press at a 0-1000 normalized [x, y] coordinate.",
            params=(
                ParamSpec("target", list[int]),
                ParamSpec("duration_ms", int, required=False, default=1000),
            ),
            bind=_wire_long_press,
        ),
        differences=(
            "target: the agent dialect accepts an element index or a coordinate pair;"
            " the wire takes coordinates only. target_description: recorded, never"
            " sent to the wire; required only for coordinate targets."
            " duration: the agent dialect spells the duration parameter `duration`"
            " (structured decisions and recorded traces carry that key); the wire"
            " spells it `duration_ms`. Executors accept both spellings."
        ),
    ),
    ActionSpec(
        name="input_text",
        operator=OperatorDialect(
            description=(
                "[ACTION] Focus (tap) the target input field, optionally clear existing"
                " text, and type text into it (supports multi-line strings with '\\n').\n"
                "\n"
                "• Use for: entering text into search bars, forms, or chat boxes in ONE"
                " step — do NOT call click on the input field first.\n"
                "• Note: input_text does NOT press Enter/Submit automatically; follow with"
                " press_key(key='ENTER') or click the search/submit button if needed."
            ),
            params=(
                ParamSpec(
                    "text",
                    str,
                    "The text content to type. Supports multi-line content with '\\n'.",
                ),
                ParamSpec(
                    "target",
                    IndexOrCoords,
                    "Input field target: an element index integer (e.g. 3) OR a 2-integer"
                    " normalized coordinate pair [x, y] in 0-1000 scale (e.g. [500, 600]).",
                ),
                ParamSpec(
                    "target_description",
                    str | None,
                    _TARGET_DESCRIPTION,
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "clear_exist",
                    bool,
                    "Whether to clear existing text before typing. True (default):"
                    " clear/replace entire text. False: append at the end of"
                    " existing content.",
                    required=False,
                    default=True,
                ),
            ),
        ),
        wire=WireDialect(
            description=("Type text, optionally focusing a 0-1000 normalized [x, y] target first."),
            params=(
                ParamSpec("text", str),
                ParamSpec("target", list[int] | None, required=False, default=None),
                ParamSpec("clear_exist", bool, required=False, default=True),
            ),
            bind=_wire_input_text,
        ),
        differences=(
            "target: the agent dialect accepts an element index or a coordinate pair,"
            " and requires a target; the wire allows omitting the target to type into"
            " the already-focused field. target_description: recorded, never sent to"
            " the wire; required only for coordinate targets."
        ),
    ),
    ActionSpec(
        name="swipe",
        operator=OperatorDialect(
            description=(
                "[ACTION] Perform an immediate single-finger swipe gesture"
                " (DOWN -> MOVE -> UP, with NO initial press delay).\n"
                "\n"
                "• Mode 1 — Directional Scroll (pass 'direction', optional 'target'):"
                " Standard list/page scrolling ('up' reveals content below, 'down' reveals"
                " content above, 'left'/'right' switch pages/tabs). Automatically computes"
                " safe vectors with ~40% visual overlap.\n"
                "• Mode 2 — Coordinate Swipe (pass 'start', 'end', and 'target_description'):"
                " Immediate local motions with NO press delay — adjusting an already-active"
                " slider/SeekBar (volume, brightness, progress bar), pull-to-refresh, or"
                " scrolling a custom container. Swipe slightly PAST the desired slider"
                " value (or to the extreme edge for 0%/100%). Do NOT mix Mode 1 and Mode 2.\n"
                "• Do NOT use swipe for:\n"
                "  - Pinch-to-zoom or two-finger rotation: strictly single-touch! For zoom,"
                " use single-touch UI controls (zoom presets like '1x'/'2x', holding or"
                " dragging a zoom control, '+'/'-' buttons, or double-tap).\n"
                "  - Drag-and-drop, icon/list reordering, or controls requiring an initial"
                " press delay: if swipe has no effect, switch to drag_and_drop (known"
                " visible destination), drag_and_hold + drop_after_hold (control/drop zone"
                " stays open only while pressed), or long_press."
            ),
            params=(
                ParamSpec(
                    "direction",
                    Direction | None,
                    "Mode 1 (scroll/page switch): 'up' (reveal content below), 'down'"
                    " (reveal content above), 'left' (scroll right), 'right' (scroll left)."
                    " Omit when using start/end.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "start",
                    list[int] | None,
                    "Mode 2 start [x, y] in 0-1000 scale for immediate local motions"
                    " without press delay (already-active slider, SeekBar, custom"
                    " scrollable container). Omit when using direction.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "end",
                    list[int] | None,
                    "Mode 2 end [x, y] in 0-1000 scale. Required with start; omit when"
                    " using direction.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "target",
                    int | list[int] | str | None,
                    "Optional Mode 1 container scope: element index (e.g. 2) or bounds"
                    " [left, top, right, bottom] to scroll inside.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "gesture",
                    Direction | list[int] | None,
                    "Legacy alias (prefer 'direction' or 'start'+'end'): direction string"
                    " OR 4-integer list [start_x, start_y, end_x, end_y].",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "target_description",
                    str | None,
                    "What is being swiped, in a few words (e.g. 'brightness slider"
                    " knob'). REQUIRED for Mode 2 coordinate swipes; ignored for Mode 1"
                    " directional scrolling.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "duration",
                    int | None,
                    "Optional swipe duration in milliseconds (default 800; computed"
                    " automatically for directional swipes).",
                    required=False,
                    default=None,
                ),
            ),
        ),
        wire=WireDialect(
            description="Swipe between two 0-1000 normalized [x, y] points.",
            params=(
                ParamSpec("start", list[int]),
                ParamSpec("end", list[int]),
                ParamSpec("duration_ms", int, required=False, default=800),
            ),
            bind=_wire_swipe,
        ),
        differences=(
            "target_description: recorded, never sent to the wire; required for"
            " coordinate gestures only. gesture: the legacy combined"
            " direction-or-coordinates parameter (executors also accept the older"
            " `action` spelling). Smart directional swipes exist only in the agent"
            " dialect; the wire takes a resolved start/end pair, and direction"
            " resolution happens client-side against the live UI tree."
        ),
    ),
    ActionSpec(
        name="drag_and_drop",
        operator=OperatorDialect(
            description=(
                "[ACTION] Press (initial press delay hold_ms), move an item from start to"
                " end, and RELEASE in one continuous turn (DOWN -> DELAY -> MOVE -> UP).\n"
                "\n"
                "• Use for: moving icons/widgets, drag-and-drop between containers, or"
                " reordering list items when the final drop destination is ALREADY visible."
                " Do NOT call long_press before this tool — hold_ms is already built in.\n"
                "• Do NOT use for:\n"
                "  - Drop zones or continuous controls ('Remove'/'Uninstall' bar, folder"
                " creation, edge page switching, continuous camera zoom slider) that ONLY"
                " appear or need live visual adjustment while pressed: use drag_and_hold +"
                " drop_after_hold instead (drag_and_drop releases at the end and dismisses"
                " transient controls).\n"
                "  - Immediate scrolling or sliders without press delay: if hold_ms opens"
                " a context/selection menu instead of moving, switch to swipe (or retry"
                " with hold_ms=0)."
            ),
            params=(
                ParamSpec(
                    "start",
                    IndexOrCoords,
                    "Start location to press and pick up from: element index integer (e.g."
                    " 3) OR normalized [x, y] pair (e.g. [500, 600]).",
                ),
                ParamSpec(
                    "end",
                    IndexOrCoords,
                    "Final drop destination where the finger releases: element index"
                    " integer (e.g. 7) OR normalized [x, y] pair (e.g. [200, 400]).",
                ),
                ParamSpec(
                    "target_description",
                    str | None,
                    "What is being picked up and dragged, in a few words (e.g. 'Chrome app"
                    " icon', 'playlist item'). REQUIRED when start is a coordinate pair;"
                    " ignored when start is an element index.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "hold_ms",
                    int,
                    "Initial press delay in milliseconds at start before moving (e.g. 1000"
                    " for 1s, 0 for immediate start). Default is 1000.",
                    required=False,
                    default=1000,
                ),
                ParamSpec(
                    "duration_ms",
                    int,
                    "Duration of the movement from start to end in milliseconds after the"
                    " initial press delay. Default is 400.",
                    required=False,
                    default=400,
                ),
            ),
        ),
        wire=WireDialect(
            description=(
                "Press at a 0-1000 normalized start [x, y] coordinate (with initial press"
                " delay), drag continuously to end [x, y], and release."
            ),
            params=(
                ParamSpec("start", list[int]),
                ParamSpec("end", list[int]),
                ParamSpec("hold_ms", int, required=False, default=1000),
                ParamSpec("duration_ms", int, required=False, default=400),
            ),
            bind=_wire_drag_and_drop,
        ),
        differences=(
            "start/end: the agent dialect accepts an element index or a coordinate pair"
            " (an index is resolved client-side against the indexed element list); the"
            " wire takes coordinates only. target_description: recorded, never sent"
            " to the wire; required only when start is a coordinate pair."
        ),
    ),
    ActionSpec(
        name="drag_and_hold",
        operator=OperatorDialect(
            description=(
                "[ACTION] Press (initial press delay hold_ms) and drag from start to end"
                " WITHOUT releasing the finger (DOWN -> DELAY -> MOVE; finger stays"
                " pressed across turns until drop_after_hold is called).\n"
                "\n"
                "• Use for: targets or continuous controls that ONLY appear or need live"
                " visual feedback while the finger stays pressed — 'Remove'/'Uninstall'"
                " bars, edge page switching, folder creation, or pressing and sliding a"
                " camera '1x'/'2x' zoom control to adjust a continuous zoom slider. Do NOT"
                " call long_press first — hold_ms is already built in.\n"
                "• Multi-turn rule: the next screenshot is captured while pressed; call"
                " drag_and_hold again to keep adjusting without lifting, and ALWAYS finish"
                " with drop_after_hold.\n"
                "• Do NOT use for: destinations already visible that can finish in 1 turn"
                " (use drag_and_drop), or immediate slides/scrolls (if hold_ms opens a"
                " context menu instead of moving, switch to swipe or retry with hold_ms=0)."
            ),
            params=(
                ParamSpec(
                    "start",
                    IndexOrCoords,
                    "Start location to press and begin dragging from: element index integer"
                    " (e.g. 3) OR normalized [x, y] pair (e.g. [500, 600]).",
                ),
                ParamSpec(
                    "end",
                    IndexOrCoords,
                    "Intermediate location to move to while keeping the finger pressed"
                    " (often offset 50-100 units from start to enter drag mode or open a"
                    " continuous control): element index (int) OR normalized [x, y] pair.",
                ),
                ParamSpec(
                    "target_description",
                    str | None,
                    "What is being picked up and held, in a few words (e.g. 'Chrome app"
                    " icon', '1x zoom button'). REQUIRED when start is a coordinate pair;"
                    " ignored when start is an element index.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "hold_ms",
                    int,
                    "Initial press delay in milliseconds at start before moving (e.g. 1000"
                    " for 1s, 0 for immediate drag). Default is 1000.",
                    required=False,
                    default=1000,
                ),
                ParamSpec(
                    "duration_ms",
                    int,
                    "Duration of the movement from start to end in milliseconds after the"
                    " initial press delay. Default is 400.",
                    required=False,
                    default=400,
                ),
            ),
        ),
        wire=WireDialect(
            description=(
                "Press at a 0-1000 normalized start [x, y] coordinate (with initial press"
                " delay) and drag to end [x, y] without releasing the touch."
            ),
            params=(
                ParamSpec("start", list[int]),
                ParamSpec("end", list[int]),
                ParamSpec("hold_ms", int, required=False, default=1000),
                ParamSpec("duration_ms", int, required=False, default=400),
            ),
            bind=_wire_drag_and_hold,
        ),
        differences=(
            "start/end: the agent dialect accepts an element index or a coordinate pair"
            " (an index is resolved client-side against the indexed element list); the"
            " wire takes coordinates only. target_description: recorded, never sent"
            " to the wire; required only when start is a coordinate pair."
        ),
    ),
    ActionSpec(
        name="drop_after_hold",
        operator=OperatorDialect(
            description=(
                "[ACTION] Move an actively held drag_and_hold touch to an optional target"
                " and RELEASE the finger (MOVE -> UP).\n"
                "\n"
                "• Use ONLY after drag_and_hold, once the revealed drop zone ('Remove',"
                " 'Uninstall', folder) is visible or the held slider has reached the target"
                " value. Omit target to release in place.\n"
                "• Do NOT call after long_press, drag_and_drop, or swipe — those already"
                " released the finger and drop_after_hold will fail."
            ),
            params=(
                ParamSpec(
                    "target",
                    IndexOrCoords | None,
                    "Optional drop destination: element index integer (e.g. 2) OR"
                    " normalized [x, y] pair (e.g. [500, 100]). Omit to release in place"
                    " at the currently held position.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "target_description",
                    str | None,
                    "What the drop destination is, in a few words (e.g. 'Remove zone',"
                    " 'Uninstall button'). REQUIRED when target is a coordinate pair;"
                    " ignored for an element index or in-place release.",
                    required=False,
                    default=None,
                ),
                ParamSpec(
                    "duration_ms",
                    int,
                    "Duration in milliseconds to move from the currently held position"
                    " to target before releasing. Default is 400.",
                    required=False,
                    default=400,
                ),
            ),
        ),
        wire=WireDialect(
            description=(
                "Move an actively held touch to an optional 0-1000 normalized [x, y]"
                " target and release it."
            ),
            params=(
                ParamSpec("target", list[int] | None, required=False, default=None),
                ParamSpec("duration_ms", int, required=False, default=400),
            ),
            bind=_wire_drop_after_hold,
        ),
        differences=(
            "target: the agent dialect accepts an element index, a coordinate pair, or"
            " omitted/None for in-place release; the wire takes coordinates or None."
            " target_description: recorded, never sent to the wire; required only when"
            " target is a coordinate pair."
        ),
    ),
    ActionSpec(
        name="press_key",
        operator=OperatorDialect(
            description=(
                "[ACTION] Press one of the 4 supported Android system keys: ONLY 'ENTER',"
                " 'BACK', 'HOME', or 'APP_SWITCH'.\n"
                "\n"
                "• Use for: submitting search/text ('ENTER'), navigating back ('BACK'),"
                " returning to the home screen ('HOME'), or opening recent apps"
                " ('APP_SWITCH').\n"
                "• Do NOT pass unsupported keys (such as VOLUME_UP, VOLUME_DOWN, POWER,"
                " MENU, or DELETE) — only the 4 enum values above are accepted."
            ),
            params=(
                ParamSpec(
                    "key",
                    Literal["ENTER", "BACK", "HOME", "APP_SWITCH"],
                    "Exact system key name — ONLY 'ENTER', 'BACK', 'HOME', or 'APP_SWITCH'.",
                ),
            ),
        ),
        wire=WireDialect(
            description=(
                "Press a device key (home, back, enter, delete, tab, search, menu, app_switch)."
            ),
            params=(ParamSpec("key", str),),
            bind=_wire_press_key,
        ),
        differences=(
            "key vocabulary: the agent dialect is a closed uppercase enum (the"
            " Operator's translate step emits Android KEYCODE_* names); the wire"
            " takes the actuator's bare lowercase key words. `to_canonical_call` and"
            " the executors normalize between them."
        ),
    ),
    ActionSpec(
        name="manage_app",
        operator=OperatorDialect(
            description=(
                "[ACTION] Launch or force-stop an application directly by display name or"
                " package name.\n"
                "\n"
                "• Use for: opening, switching to, or resetting an app (prefer manage_app"
                " over manually hunting for icons on the home screen or app drawer)."
            ),
            params=(
                ParamSpec("action", Literal["launch", "stop"], "Either 'launch' or 'stop'."),
                ParamSpec(
                    "app_name",
                    str,
                    "Display name (e.g. 'Camera', 'Settings') or Android package name"
                    " (e.g. 'com.android.settings').",
                ),
            ),
        ),
        wire=WireDialect(
            description="Launch or stop an app by human-readable name or package.",
            params=(
                ParamSpec("action", str),
                ParamSpec("app_name", str),
            ),
            bind=_wire_manage_app,
        ),
    ),
    ActionSpec(
        name="wait_for_delay",
        operator=OperatorDialect(
            description=(
                "[ACTION] Pause execution for time_in_ms milliseconds without touching the"
                " screen.\n"
                "\n"
                "• Use for: active loading/splash screens, animations, transitions, or"
                " explicit wait intervals required by the task.\n"
                "• Do NOT loop wait_for_delay on a static screen — if nothing changes after"
                " 1-2 waits, take a different action instead."
            ),
            params=(
                ParamSpec(
                    "time_in_ms",
                    int,
                    "Wait duration in milliseconds (e.g. 2000 for 2s, 5000 for 5s, 60000"
                    " for 1 minute).",
                ),
            ),
        ),
        wire=WireDialect(
            description="Wait for a fixed number of milliseconds.",
            params=(ParamSpec("time_in_ms", int),),
            bind=_wire_wait_for_delay,
        ),
    ),
    ActionSpec(
        name="wait_for_text",
        declaration=None,  # Listed in the legacy tool-name sets but never declared to
        # any LLM; kept wire-reachable for executors and external MCP clients.
        wire=WireDialect(
            description="Wait for text to appear on or disappear from the screen.",
            params=(
                ParamSpec("text", str),
                ParamSpec("wait_state", str, required=False, default="appear"),
                ParamSpec("timeout_ms", int, required=False, default=5000),
            ),
            bind=_wire_wait_for_text,
        ),
    ),
    ActionSpec(
        name="open_link",
        wire=WireDialect(
            description="Open a URL on the device.",
            params=(ParamSpec("url", str),),
            bind=_wire_open_link,
        ),
    ),
    ActionSpec(
        name="erase_one_char",
        wire=WireDialect(
            description="Erase a single character in the focused field.",
            params=(),
            bind=_wire_erase_one_char,
        ),
    ),
    ActionSpec(
        name="focus_and_clear_text",
        wire=WireDialect(
            description=("Focus the field at a 0-1000 normalized [x, y] and clear its text."),
            params=(ParamSpec("target", list[int]),),
            bind=_wire_focus_and_clear_text,
        ),
    ),
)

#: Canonical manifest, keyed by action name, in declaration order.
ACTION_SPECS: dict[str, ActionSpec] = {spec.name: spec for spec in _SPECS}

#: The order the Operator binds its shells (historically the factory-dict order,
#: identical to the prompt's "Physical device actions" enumeration order).
OPERATOR_SHELL_ORDER: tuple[str, ...] = (
    "click",
    "input_text",
    "swipe",
    "drag_and_drop",
    "drag_and_hold",
    "drop_after_hold",
    "press_key",
    "manage_app",
    "wait_for_delay",
    "long_press",
)

#: Exception wording per action; matches the historical executor `except` arms.
EXCEPTION_PREFIXES: dict[str, str] = {
    "click": "Error during click",
    "click_sequence": "Error executing click sequence",
    "long_press": "Error during long press",
    "input_text": "Error during input text",
    "swipe": "Error during swipe",
    "drag_and_drop": "Error during drag and drop",
    "drag_and_hold": "Error during drag and hold",
    "drop_after_hold": "Error during drop after hold",
    "press_key": "Error during press_key",
    "manage_app": "Error during manage_app",
    "wait_for_delay": "Error during wait_for_delay",
    "wait_for_text": "Error during wait_for_text",
}


def exception_prefix(action: str) -> str:
    """Returns the historical human-readable exception prefix for an action."""
    return EXCEPTION_PREFIXES.get(action, f"Error during {action}")


# --- Projections ---------------------------------------------------------------------


@cache
def _operator_args_model(name: str):
    """Builds (once) the pydantic argument model for an operator shell."""
    dialect = ACTION_SPECS[name].operator
    if dialect is None:
        raise ValueError(f"Action '{name}' has no operator dialect.")
    fields: dict[str, Any] = {}
    for p in dialect.params:
        if p.required:
            fields[p.name] = (p.annotation, Field(description=p.description))
        else:
            fields[p.name] = (
                p.annotation,
                Field(default=p.default, description=p.description),
            )
    return create_model(name, **fields)


def operator_shell_tool(name: str) -> StructuredTool:
    """Builds the Operator's declaration-only shell for one action.

    The body is never executed by the Operator loop -- action calls are translated
    into structured decisions instead -- but returns the historical marker string for
    any caller that does invoke it.
    """
    dialect = ACTION_SPECS[name].operator
    if dialect is None:
        raise ValueError(f"Action '{name}' has no operator dialect.")
    return StructuredTool(
        name=name,
        description=dialect.description,
        args_schema=_operator_args_model(name),
        func=lambda **kwargs: "Action Recorded",
    )


_SCALAR_JSON: dict[Any, dict[str, str]] = {
    bool: {"type": "boolean"},
    int: {"type": "integer"},
    float: {"type": "number"},
    str: {"type": "string"},
}


def _json_schema(annotation: Any) -> dict[str, Any]:
    """Projects one Python-typed parameter annotation onto JSON schema.

    ``None`` members of a union only make the parameter optional (its
    ``required`` flag says so); they never appear in the projected schema, so
    the declaration carries no ``null`` alternatives or ``default`` noise.
    """
    origin = get_origin(annotation)
    if origin in (Union, types.UnionType):
        members = [a for a in get_args(annotation) if a is not type(None)]
        schemas = [_json_schema(m) for m in members]
        return schemas[0] if len(schemas) == 1 else {"anyOf": schemas}
    if origin is Literal:
        return {"type": "string", "enum": list(get_args(annotation))}
    if origin is list:
        (item,) = get_args(annotation) or (str,)
        return {"type": "array", "items": _json_schema(item)}
    try:
        return dict(_SCALAR_JSON[annotation])
    except KeyError:
        raise TypeError(f"No JSON projection for parameter annotation {annotation!r}.") from None


def _projected_parameters(dialect: OperatorDialect) -> dict[str, Any]:
    """The operator shell's parameters as a JSON-schema ``parameters`` object."""
    properties: dict[str, Any] = {}
    required: list[str] = []
    for p in dialect.params:
        schema = _json_schema(p.annotation)
        if p.description:
            schema["description"] = p.description
        properties[p.name] = schema
        if p.required:
            required.append(p.name)
    return {"type": "object", "properties": properties, "required": required}


@cache
def tool_declaration(name: str) -> ToolDeclaration:
    """Builds the Flash ``ToolDeclaration`` for one action.

    A shared action is the operator shell projected onto JSON (same description,
    same parameters, same targeting semantics); a JSON-only action uses its own
    ``DeclarationDialect``.
    """
    spec = ACTION_SPECS[name]
    if spec.declaration is not None:
        return ToolDeclaration(
            name=name,
            description=spec.declaration.description,
            parameters=spec.declaration.parameters,
        )
    if spec.operator is not None:
        return ToolDeclaration(
            name=name,
            description=spec.operator.description,
            parameters=_projected_parameters(spec.operator),
        )
    raise ValueError(f"Action '{name}' has no agent dialect to declare.")


def wire_dialects() -> tuple[ActionSpec, ...]:
    """Returns every spec that has a wire dialect, in manifest order."""
    return tuple(spec for spec in _SPECS if spec.wire is not None)


def make_wire_handler(
    spec: ActionSpec,
    actuator: Any,
    wrap: Callable[[ActionResult], CallToolResult],
    wrap_exception: Callable[[str, Exception], CallToolResult],
) -> Callable[..., Awaitable[CallToolResult]]:
    """Builds the FastMCP tool function for one wire dialect.

    The returned coroutine carries an explicit ``__signature__`` so FastMCP derives
    the same argument model a literal ``async def`` produced historically, and the
    same ``Annotated[CallToolResult, ActionResult]`` return annotation so the
    structured-output schema is unchanged.
    """
    wire = spec.wire
    if wire is None:
        raise ValueError(f"Action '{spec.name}' has no wire dialect.")

    async def handler(**kwargs: Any) -> Any:
        try:
            return wrap(await wire.bind(actuator, kwargs))
        except Exception as e:  # pylint: disable=broad-exception-caught
            return wrap_exception(spec.name, e)

    return_annotation = Annotated[CallToolResult, ActionResult]
    parameters = [
        inspect.Parameter(
            p.name,
            inspect.Parameter.POSITIONAL_OR_KEYWORD,
            annotation=p.annotation,
            default=inspect.Parameter.empty if p.required else p.default,
        )
        for p in wire.params
    ]
    handler.__name__ = spec.name
    handler.__qualname__ = spec.name
    handler.__doc__ = wire.description
    handler.__signature__ = inspect.Signature(  # type: ignore[attr-defined]
        parameters, return_annotation=return_annotation
    )
    handler.__annotations__ = {p.name: p.annotation for p in wire.params}
    handler.__annotations__["return"] = return_annotation
    return handler
