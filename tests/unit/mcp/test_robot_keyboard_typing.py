"""Unit tests for robot-arm virtual keyboard typing (without OpenCV or ADB text)."""

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.interfaces.touch_api.keyboard_layout import (
    key_to_pixels,
    strip_accents,
    text_to_key_sequence,
)
from artemis.interfaces.touch_api.robot_actuator import RobotTouchActuator
from artemis.interfaces.touch_api.server import create_touch_api_app
from artemis.platform import platform


@pytest.fixture
def test_context() -> ArtemisContext:
    return ArtemisContext(
        device=DeviceContext(
            host_platform=platform.os_type.name,
            mobile_platform=DevicePlatform.ANDROID,
            device_id="0083078983",
            device_width=1080,
            device_height=2400,
        ),
    )


def _make_mock_robot_client(requests_log: list[dict[str, Any]]) -> httpx.AsyncClient:
    def handler(request: httpx.Request) -> httpx.Response:
        body = json.loads(request.content.decode("utf-8")) if request.content else None
        requests_log.append(
            {
                "method": request.method,
                "path": request.url.path,
                "headers": dict(request.headers),
                "json": body,
            }
        )
        if request.url.path == "/calibration/device":
            return httpx.Response(
                200,
                json={
                    "result": True,
                    "serial_number": body["serial_number"],
                    "calibration_source": "cache",
                    "token": "robot-token-kb",
                    "screen_size_px": [body["screen_width"], body["screen_height"]],
                    "z_before_touch": 270.0,
                },
            )
        if request.url.path == "/calibration/tap":
            return httpx.Response(200, json={"result": True, "action": "tap"})
        if request.url.path == "/speed":
            return httpx.Response(200, json={"result": True})
        return httpx.Response(404, json={"detail": "Not found"})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8010",
    )


def test_text_to_key_sequence_and_accents():
    assert strip_accents("Configurações") == "Configuracoes"
    assert text_to_key_sequence("WiFi 50") == [
        "shift",
        "w",
        "i",
        "shift",
        "f",
        "i",
        "space",
        "symbols",
        "5",
        "0",
        "symbols",
    ]
    assert text_to_key_sequence("a@b.com\n") == [
        "a",
        "symbols",
        "a",
        "symbols",
        "b",
        ".",
        "c",
        "o",
        "m",
        "enter",
    ]


@pytest.mark.asyncio
async def test_robot_input_text_taps_each_key_without_adb_typing(
    test_context: ArtemisContext,
):
    requests_log: list[dict[str, Any]] = []
    robot_http_client = _make_mock_robot_client(requests_log)

    mock_controller = MagicMock()
    mock_controller.ctx = test_context
    mock_controller.type_text = AsyncMock(side_effect=AssertionError("ADB type_text must not be called"))
    mock_controller.erase_text = AsyncMock(side_effect=AssertionError("ADB erase_text must not be called"))
    mock_controller.press_key = AsyncMock(side_effect=AssertionError("ADB press_key must not be called"))
    mock_controller.tap_at = AsyncMock(side_effect=AssertionError("ADB tap_at must not be called"))
    mock_controller.get_ui_elements = AsyncMock(return_value=[])

    actuator = RobotTouchActuator(
        test_context,
        controller=mock_controller,
        robot_api_url="http://127.0.0.1:8010",
        http_client=robot_http_client,
    )
    actuator.key_delay_ms = 0
    touch_app = create_touch_api_app(actuator=actuator, controller=mock_controller, ctx=test_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=touch_app),
        base_url="http://touch-api",
    ) as client:
        with (
            patch(
                "artemis.interfaces.touch_api.server._query_adb_screen_size",
                new=AsyncMock(return_value=(1080, 2400)),
            ),
            patch(
                "artemis.interfaces.touch_api.robot_actuator.wait_for_keyboard_visible",
                new=AsyncMock(return_value=True),
            ),
        ):
            # 1. Focus search bar with robot arm
            resp_focus = await client.post("/focus-and-clear", json={"nx": 500, "ny": 120})
            assert resp_focus.status_code == 200

            # 2. Type "WiFi 5" via robot arm taps on virtual keyboard
            resp_type = await client.post("/input-text", json={"text": "WiFi 5"})
            assert resp_type.status_code == 200

            # 3. Backspace and Enter via robot arm taps
            resp_erase = await client.post("/erase-char", json={"times": 1})
            assert resp_erase.status_code == 200

            resp_enter = await client.post("/press-key", json={"key": "ENTER"})
            assert resp_enter.status_code == 200

    tap_calls = [r["json"] for r in requests_log if r["path"] == "/calibration/tap"]
    expected_keys = [
        "shift", "w", "i", "shift", "f", "i", "space", "symbols", "5", "symbols",
        "backspace", "enter",
    ]
    # First tap is focus-and-clear at (500, 120), followed by 12 virtual keyboard taps
    assert len(tap_calls) == 1 + len(expected_keys)
    for call, key_name in zip(tap_calls[1:], expected_keys, strict=True):
        exp_px, exp_py = key_to_pixels(key_name, 1080, 2400)
        assert call["px"] == exp_px
        assert call["py"] == exp_py

    speed_calls = [r["json"] for r in requests_log if r["path"] == "/speed"]
    assert speed_calls[0] == {"speed": 20.0, "accel": 4.0, "decel": 4.0}
    assert {"speed": 50.0, "accel": 25.0, "decel": 25.0} in speed_calls
    assert speed_calls[-1] == {"speed": 20.0, "accel": 4.0, "decel": 4.0}

    await robot_http_client.aclose()
