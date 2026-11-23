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

"""Targets are either observed (an element index) or described (coordinates).

Flash binds the Pro Operator's agent dialect: a click, long press or focused
input names an element index from the indexed UI list -- resolved here to the
element's center with its observed text/bounds/id recorded -- or a coordinate
pair, which the model must describe itself. A coordinate swipe or click
sequence must be described too. Nothing is inferred, the description never
reaches the wire, and the wire only ever sees coordinates.
"""

from unittest.mock import Mock

import pytest

from artemis.mcp.action_executor import McpActionExecutor, _ArgError


def _make_executor(width=1080, height=2400):
    ctx = Mock()
    ctx.device.device_width = width
    ctx.device.device_height = height
    actuator = Mock()
    actuator.controller = Mock()
    return McpActionExecutor(ctx, actuator=actuator)


def _state():
    state = Mock()
    state.indexed_elements = []
    state.indexed_points = [[540, 1440], [745, 1440]]
    state.latest_ui_hierarchy = None
    return state


def _indexed_state():
    """Two observed elements (pixel centers, the shape ``observe()`` writes)."""
    state = _state()
    state.indexed_elements = [
        {
            "index": 1,
            "center": [540, 1248],
            "text": "Wi-Fi",
            "bounds": [40, 1152, 1040, 1344],
            "class": "android.widget.TextView",
            "resource_id": "android:id/title",
            "is_ocr": False,
        },
        {
            "index": 2,
            "center": [540, 720],
            "text": "Search settings",
            "bounds": [80, 660, 1000, 780],
            "class": "android.widget.EditText",
            "resource_id": "com.android.settings:id/search",
            "is_ocr": False,
        },
    ]
    state.indexed_points = [el["center"] for el in state.indexed_elements]
    return state


# --- Index targets: resolved against the element list, observed semantics recorded ---


@pytest.mark.parametrize("index", [1, "1", 1.0, [1]])
def test_click_index_resolves_to_the_elements_center_and_records_observed_fields(index):
    executor = _make_executor()
    wire_name, wire_args, _, recorded = executor._translate(
        "click", {"target": index}, _indexed_state()
    )
    assert wire_name == "click"
    # 540/1080 -> 500, 1248/2400 -> 520 (normalized 0-1000)
    assert wire_args == {"target": [500, 520], "times": 1, "delay_ms": 100}
    assert recorded == {
        "target_text": "Wi-Fi",
        "target_bounds": [37, 480, 963, 560],
        "target_resource_id": "android:id/title",
        "target_class": "android.widget.TextView",
    }
    assert "target_description" not in recorded


def test_index_target_needs_no_description_and_ignores_a_stray_one():
    """An index already names its element; the model's belief never mixes with
    the observation on the record."""
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "click", {"target": 2, "target_description": "the search box"}, _indexed_state()
    )
    assert wire_args["target"] == [500, 300]
    assert recorded["target_text"] == "Search settings"
    assert "target_description" not in recorded


def test_long_press_and_input_text_resolve_indices_too():
    executor = _make_executor()
    _, press_args, _, recorded = executor._translate(
        "long_press", {"target": 1, "duration": 1500}, _indexed_state()
    )
    assert press_args == {"target": [500, 520], "duration_ms": 1500}
    assert recorded["target_text"] == "Wi-Fi"

    _, type_args, _, recorded = executor._translate(
        "input_text", {"text": "wifi", "target": 2}, _indexed_state()
    )
    assert type_args == {"text": "wifi", "target": [500, 300], "clear_exist": True}
    assert recorded["target_resource_id"] == "com.android.settings:id/search"


@pytest.mark.parametrize("name, label", [("click", "click"), ("long_press", "long press")])
def test_out_of_range_index_is_refused_with_the_active_range(name, label):
    executor = _make_executor()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate(name, {"target": 7}, _indexed_state())
    message = str(excinfo.value)
    assert message.startswith(f"Error during {label}: Invalid target index 7.")
    assert "1 to 2" in message
    assert "ask_explorer" in message


def test_index_against_an_empty_list_is_refused():
    executor = _make_executor()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click", {"target": 1}, _state())
    message = str(excinfo.value)
    assert "Invalid target index 1" in message
    assert "empty" in message


def test_unusable_target_is_refused_with_both_syntaxes_named():
    executor = _make_executor()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click", {"target": "the button"}, _indexed_state())
    message = str(excinfo.value)
    assert message.startswith("Error during click: Invalid target format")
    assert "element index" in message and "[x, y]" in message


def test_index_resolution_uses_the_live_device_resolution():
    """The element list is built against the device size ``observe()`` wrote
    on the context; a different resolution maps the same pixels differently."""
    executor = _make_executor(width=720, height=1600)
    state = _indexed_state()
    state.indexed_elements[0]["center"] = [360, 800]
    _, wire_args, _, _ = executor._translate("click", {"target": 1}, state)
    assert wire_args["target"] == [500, 500]


# --- Coordinate targets: the model describes what it aims at -------------------------


@pytest.mark.parametrize(
    "name, args, label",
    [
        ("click", {"target": [500, 600]}, "click"),
        ("long_press", {"target": [500, 600]}, "long press"),
        ("input_text", {"text": "hi", "target": [500, 600]}, "input text"),
        ("swipe", {"start": [200, 600], "end": [800, 600]}, "swipe"),
        ("drag", {"start": [200, 600], "end": [800, 600]}, "drag"),
        ("drag_and_hold", {"start": [200, 600], "end": [200, 500]}, "drag and hold"),
        ("drop", {"target": [500, 100]}, "drop"),
    ],
)
def test_coordinate_targets_without_description_are_refused(name, args, label):
    executor = _make_executor()
    with pytest.raises(_ArgError) as excinfo:
        executor._translate(name, args, _state())
    message = str(excinfo.value)
    assert message.startswith(f"Error during {label}")
    assert "target_description" in message


@pytest.mark.parametrize("blank", ["", "   ", None, 7])
def test_blank_or_non_string_description_is_refused(blank):
    executor = _make_executor()
    with pytest.raises(_ArgError):
        executor._translate("click", {"target": [500, 600], "target_description": blank}, _state())


def test_description_is_accepted_and_kept_off_the_wire():
    executor = _make_executor()
    wire_name, wire_args, _, _ = executor._translate(
        "click", {"target": [500, 600], "target_description": "play button"}, _state()
    )
    assert wire_name == "click"
    assert wire_args == {"target": [500, 600], "times": 1, "delay_ms": 100}
    assert "target_description" not in wire_args

    _, swipe_args, _, _ = executor._translate(
        "swipe",
        {"start": [200, 600], "end": [800, 600], "target_description": "brightness knob"},
        _state(),
    )
    assert swipe_args == {"start": [200, 600], "end": [800, 600], "duration_ms": 400}


def test_recorded_target_is_the_cleaned_description_in_pro_shape():
    """The recorded semantics come from the same check that requires them."""
    executor = _make_executor()
    _, _, _, recorded = executor._translate(
        "click", {"target": [1, 2], "target_description": " ok "}, _state()
    )
    assert recorded == {"target_description": "ok"}

    _, _, _, recorded = executor._translate(
        "long_press", {"target": [1, 2], "target_description": "thumbnail"}, _state()
    )
    assert recorded == {"target_description": "thumbnail"}

    _, _, _, recorded = executor._translate(
        "click_sequence", {"sequence": [[1, 2]], "target_descriptions": ["a "]}, _state()
    )
    assert recorded == {"target_descriptions": ["a"]}

    # No coordinate target: nothing is recorded, nothing inferred.
    _, _, _, recorded = executor._translate("press_key", {"key": "BACK"}, _state())
    assert recorded == {}


def test_coordinate_swipe_records_its_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "swipe",
        {"start": [200, 600], "end": [800, 600], "target_description": " brightness knob "},
        _state(),
    )
    assert recorded == {"target_description": "brightness knob"}
    assert "target_description" not in wire_args


def test_targeted_input_records_its_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "input_text",
        {"text": "hi", "target": [500, 600], "target_description": "search input"},
        _state(),
    )
    assert wire_args["target"] == [500, 600]
    assert recorded == {"target_description": "search input"}
    assert "target_description" not in wire_args


def test_focused_typing_without_a_target_needs_no_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "input_text", {"text": "hi", "target": None}, _state()
    )
    assert wire_args["target"] is None
    assert recorded == {}


def test_focused_typing_ignores_a_stray_description():
    """Typing into the focused field has no coordinate target: a description the
    model tacked on anyway is not recorded, so no self-described target enters
    the history for an action that aimed at nothing."""
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "input_text",
        {"text": "hi", "target": None, "target_description": "search input"},
        _state(),
    )
    assert wire_args["target"] is None
    assert recorded == {}


def test_directional_swipe_needs_no_description():
    executor = _make_executor()
    wire_name, wire_args, _, recorded = executor._translate("swipe", {"direction": "up"}, _state())
    assert wire_name == "swipe"
    assert "start" in wire_args and "end" in wire_args
    assert recorded == {}


def test_directional_swipe_ignores_a_stray_description():
    executor = _make_executor()
    _, wire_args, _, recorded = executor._translate(
        "swipe", {"direction": "up", "target_description": "the feed"}, _state()
    )
    assert "target_description" not in wire_args
    assert recorded == {}


def test_click_sequence_requires_one_description_per_entry():
    executor = _make_executor()
    sequence = [[500, 600], [690, 600], [10, 10]]

    with pytest.raises(_ArgError) as excinfo:
        executor._translate("click_sequence", {"sequence": sequence}, _state())
    assert "target_descriptions" in str(excinfo.value)
    assert "3 expected" in str(excinfo.value)

    # Wrong length is refused too.
    with pytest.raises(_ArgError):
        executor._translate(
            "click_sequence",
            {"sequence": sequence, "target_descriptions": ["video body", "skip"]},
            _state(),
        )

    _, wire_args, _, recorded = executor._translate(
        "click_sequence",
        {"sequence": sequence, "target_descriptions": ["video body", "digit 2", "corner"]},
        _state(),
    )
    assert wire_args["sequence"] == [[500, 600], [690, 600], [10, 10]]
    assert "target_descriptions" not in wire_args
    assert recorded == {"target_descriptions": ["video body", "digit 2", "corner"]}


@pytest.mark.parametrize("index_entry", [2, "2", 2.0, [2]])
def test_click_sequence_refuses_element_indices(index_entry):
    """click_sequence takes coordinate pairs only. An index would be resolved
    against the element list and then recorded under the model's own
    description, reading back as a self-described coordinate target."""
    executor = _make_executor()
    sequence = [[500, 600], index_entry, [10, 10]]

    with pytest.raises(_ArgError) as excinfo:
        executor._translate(
            "click_sequence",
            {"sequence": sequence, "target_descriptions": ["video body", "digit 2", "corner"]},
            _state(),
        )
    message = str(excinfo.value)
    assert message.startswith("Error during click sequence")
    assert "entry 2" in message
    assert "coordinate" in message
    assert "ask_explorer" in message


def test_click_sequence_accepts_a_serialized_pair_list():
    executor = _make_executor()
    _, wire_args, _, _ = executor._translate(
        "click_sequence",
        {"sequence": "[[500, 600], [10, 10]]", "target_descriptions": ["video body", "corner"]},
        _state(),
    )
    assert wire_args["sequence"] == [[500, 600], [10, 10]]


def test_drag_resolves_indices_and_coordinates():
    executor = _make_executor()
    # Index start -> coordinate end (no target_description needed)
    wire_name, wire_args, _, recorded = executor._translate(
        "drag",
        {"start": 1, "end": [800, 200], "hold_ms": 1800, "duration_ms": 600},
        _indexed_state(),
    )
    assert wire_name == "drag"
    assert wire_args == {
        "start": [500, 520],
        "end": [800, 200],
        "hold_ms": 1800,
        "duration_ms": 600,
    }
    assert recorded["target_text"] == "Wi-Fi"
    assert "target_description" not in recorded

    # Index start -> index end
    _, wire_args2, _, _ = executor._translate(
        "drag",
        {"start": 1, "end": 2},
        _indexed_state(),
    )
    assert wire_args2 == {
        "start": [500, 520],
        "end": [500, 300],
        "hold_ms": 1000,
        "duration_ms": 400,
    }

    # Coordinate start with target_description -> coordinate end
    _, wire_args3, _, recorded3 = executor._translate(
        "drag",
        {
            "start": [200, 400],
            "end": [700, 400],
            "target_description": "Chrome icon",
            "hold_ms": 2000,
            "duration_ms": 500,
        },
        _state(),
    )
    assert wire_args3 == {
        "start": [200, 400],
        "end": [700, 400],
        "hold_ms": 2000,
        "duration_ms": 500,
    }
    assert recorded3 == {"target_description": "Chrome icon"}

    # Invalid hold_ms / duration_ms refused
    with pytest.raises(_ArgError, match="hold_ms"):
        executor._translate("drag", {"start": 1, "end": [800, 200], "hold_ms": -1}, _indexed_state())
    with pytest.raises(_ArgError, match="duration_ms"):
        executor._translate(
            "drag", {"start": 1, "end": [800, 200], "duration_ms": 0}, _indexed_state()
        )


def test_drag_and_hold_and_drop_translation():
    executor = _make_executor()
    wire_name, wire_args, _, recorded = executor._translate(
        "drag_and_hold",
        {"start": 1, "end": [500, 400], "hold_ms": 1200, "duration_ms": 300},
        _indexed_state(),
    )
    assert wire_name == "drag_and_hold"
    assert wire_args == {
        "start": [500, 520],
        "end": [500, 400],
        "hold_ms": 1200,
        "duration_ms": 300,
    }
    assert recorded["target_text"] == "Wi-Fi"

    # Drop onto coordinate target with description
    drop_name, drop_args, _, drop_rec = executor._translate(
        "drop",
        {"target": [500, 90], "target_description": "Remove zone", "duration_ms": 350},
        _state(),
    )
    assert drop_name == "drop"
    assert drop_args == {"target": [500, 90], "duration_ms": 350}
    assert drop_rec == {"target_description": "Remove zone"}

    # Drop onto indexed element
    _, drop_idx_args, _, drop_idx_rec = executor._translate(
        "drop",
        {"target": 2},
        _indexed_state(),
    )
    assert drop_idx_args == {"target": [500, 300], "duration_ms": 400}
    assert drop_idx_rec["target_text"] == "Search settings"

    # Drop in place (no target -> no description required)
    _, drop_in_place, _, drop_in_place_rec = executor._translate("drop", {}, _state())
    assert drop_in_place == {"target": None, "duration_ms": 400}
    assert drop_in_place_rec == {}

    # Invalid duration_ms refused
    with pytest.raises(_ArgError, match="duration_ms"):
        executor._translate("drop", {"duration_ms": 0}, _state())
