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

"""Actuator layer tests: coordinate conversion, message parity, driver dispatch."""

import pytest

from artemis.mcp.action_manifest import DEVICE_ACTIONS
from artemis.mcp.action_types import ActionCode
from artemis.mcp.actuators import Actuator, AdbActuator, MockActuator


@pytest.fixture
def actuator() -> MockActuator:
    return MockActuator(width=1000, height=2000)


def test_mock_actuator_satisfies_protocol(actuator):
    assert isinstance(actuator, Actuator)
    assert isinstance(actuator, AdbActuator)
    assert actuator.capabilities() == DEVICE_ACTIONS
    assert actuator.extensions() == []


@pytest.mark.asyncio
async def test_click_converts_normalized_to_pixels(actuator):
    res = await actuator.click(500, 500)
    assert res.ok and res.code is ActionCode.OK
    # Historical wording is part of the contract: transcripts and traces assert on it.
    assert res.message == "Tapped at [500, 500] (normalized)."
    assert res.normalized_coordinates == [500, 500]
    assert actuator.action_history[-1] == {
        "action": "tap",
        "x": 500,
        "y": 1000,
        "times": 1,
        "duration_ms": 100,
    }


@pytest.mark.asyncio
async def test_click_sequence_taps_each_point(actuator):
    res = await actuator.click_sequence([(100, 100), (900, 900)], delay_ms=1)
    assert res.ok
    assert res.message == ("Tapped in sequence at [100, 100]; [900, 900] (normalized).")
    taps = [h for h in actuator.action_history if h["action"] == "tap"]
    assert [(t["x"], t["y"]) for t in taps] == [(100, 200), (900, 1800)]


@pytest.mark.asyncio
async def test_long_press_message_and_duration(actuator):
    res = await actuator.long_press(250, 750, duration_ms=1500)
    assert res.ok
    assert res.message == "Long-pressed at [250, 750] (normalized) for 1500ms."
    assert res.duration_ms == 1500


@pytest.mark.asyncio
async def test_android_driver_long_press_always_passes_duration_ms():
    """AndroidAdbDriver.long_press must always forward duration_ms (even when < 500ms)."""
    from unittest.mock import MagicMock

    from artemis.drivers.android.adb_driver import AndroidAdbDriver

    driver = AndroidAdbDriver.__new__(AndroidAdbDriver)
    driver._held_position = None
    driver._device = MagicMock()
    driver._device.shell = MagicMock(return_value="")

    ok_short = await driver.long_press(120, 340, duration_ms=300)
    assert ok_short is True
    driver._device.shell.assert_called_with("input swipe 120 340 120 340 300")

    ok_long = await driver.long_press(120, 340, duration_ms=2500)
    assert ok_long is True
    driver._device.shell.assert_called_with("input swipe 120 340 120 340 2500")


@pytest.mark.asyncio
async def test_swipe_reports_normalized_endpoints(actuator):
    res = await actuator.swipe((500, 800), (500, 200), 400)
    assert res.ok
    assert res.message == "Swiped from [500, 800] to [500, 200] (normalized)."
    assert res.normalized_coordinates == [500, 800, 500, 200]


@pytest.mark.asyncio
async def test_drag_and_drop_converts_normalized_and_reports_hold_ms(actuator):
    res = await actuator.drag_and_drop((200, 400), (800, 400), hold_ms=1500, duration_ms=500)
    assert res.ok
    assert (
        res.message
        == "Dragged and dropped from [200, 400] to [800, 400] (normalized, initial press 1500ms)."
    )
    assert res.normalized_coordinates == [200, 400, 800, 400]
    assert res.duration_ms == 500
    assert actuator.action_history[-1] == {
        "action": "drag",
        "start": (200, 800),
        "end": (800, 800),
        "hold_ms": 1500,
        "duration_ms": 500,
    }


@pytest.mark.asyncio
async def test_drag_and_hold_then_drop_after_hold(actuator):
    # Dropping without an active hold fails cleanly
    res_fail = await actuator.drop_after_hold((500, 100))
    assert not res_fail.ok

    # Drag and hold keeps pointer pressed
    res_hold = await actuator.drag_and_hold((200, 600), (200, 500), hold_ms=1200, duration_ms=300)
    assert res_hold.ok
    assert "not released" in res_hold.message
    assert res_hold.normalized_coordinates == [200, 600, 200, 500]
    assert actuator.action_history[-1]["action"] == "drag_and_hold"

    # Drop onto revealed target
    res_drop = await actuator.drop_after_hold((500, 100), duration_ms=350)
    assert res_drop.ok
    assert res_drop.message == "Dropped at [500, 100] (normalized)."
    assert res_drop.normalized_coordinates == [500, 100]
    assert actuator.action_history[-1] == {
        "action": "drop",
        "from": (200, 1000),
        "end": (500, 200),
        "duration_ms": 350,
    }

    # Drop in place
    res_hold2 = await actuator.drag_and_hold((200, 600), (200, 500), hold_ms=500, duration_ms=200)
    assert res_hold2.ok
    res_drop_in_place = await actuator.drop_after_hold()
    assert res_drop_in_place.ok
    assert res_drop_in_place.message == "Dropped in place at held position."


@pytest.mark.asyncio
async def test_press_key_forwards_unknown_keycode_to_driver(actuator):
    # Arbitrary keycodes (KEYCODE_* names, numeric codes) pass through to the driver,
    # matching the historical adb_server behavior of accepting any Android key event.
    res = await actuator.press_key("KEYCODE_DPAD_DOWN")
    assert res.ok
    assert res.message == "Pressed key 'KEYCODE_DPAD_DOWN'."
    assert actuator.action_history[-1]["action"] == "press_key"


@pytest.mark.asyncio
async def test_press_key_rejects_empty_key(actuator):
    res = await actuator.press_key("")
    assert not res.ok
    assert res.code is ActionCode.INVALID_ARGS


@pytest.mark.asyncio
async def test_input_text_append_moves_cursor_and_preserves_text(actuator, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    res = await actuator.input_text("hello", (500, 300), clear_exist=False)
    assert res.ok
    assert res.message == "Typed 'hello' at [500, 300] (normalized)."
    actions = [h["action"] for h in actuator.action_history]
    # Append semantics: focus tap, cursor-to-end (KEYCODE_MOVE_END), then type without
    # clearing — matching the historical adb_server focus_and_input_text contract.
    assert actions == ["tap", "press_key", "input_text"]
    typed = actuator.action_history[-1]
    assert typed["clear_existing"] is False
    cursor = actuator.action_history[-2]
    assert cursor["key"] == "123"


@pytest.mark.asyncio
async def test_input_text_clear_erases_then_types_without_reclearing(actuator, monkeypatch):
    monkeypatch.setattr("asyncio.sleep", _instant_sleep)
    res = await actuator.input_text("hello", (500, 300), clear_exist=True)
    assert res.ok
    actions = [h["action"] for h in actuator.action_history]
    # The controller's full clear runs as a shell keyevent batch on this driver.
    assert actions == ["tap", "execute_shell", "input_text"]
    assert actuator.action_history[-1]["clear_existing"] is False


async def _instant_sleep(_delay, *args, **kwargs):
    return None


@pytest.mark.asyncio
async def test_wait_for_delay(actuator):
    res = await actuator.wait_for_delay(1)
    assert res.ok
    assert res.message == "Waited 1ms."


def test_partial_capabilities_are_reported():
    partial = MockActuator(capabilities=frozenset({"click_sequence"}))
    assert partial.capabilities() == frozenset({"click_sequence"})


@pytest.mark.asyncio
async def test_observation_primitives(actuator):
    shot = await actuator.take_screenshot()
    assert isinstance(shot, str) and len(shot) > 0
    data = await actuator.get_screen_data()
    assert data.width == 1000 and data.height == 2000
