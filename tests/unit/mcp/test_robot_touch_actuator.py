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

"""Tests for RobotTouchActuator and its integration with the Touch API server."""

import json
from typing import Any
from unittest.mock import AsyncMock, patch

import httpx
import pytest

from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.interfaces.touch_api.robot_actuator import RobotTouchActuator
from artemis.interfaces.touch_api.server import create_touch_api_app
from artemis.mcp.action_manifest import validate_actuator
from artemis.platform import platform


@pytest.fixture
def test_context() -> ArtemisContext:
    return ArtemisContext(
        device=DeviceContext(
            host_platform=platform.os_type.name,
            mobile_platform=DevicePlatform.ANDROID,
            device_id="RXCY501ELYW",
            device_width=1080,
            device_height=2340,
        ),
    )


def _make_mock_robot_client(requests_log: list[dict[str, Any]]) -> httpx.AsyncClient:
    """Builds an httpx.AsyncClient backed by MockTransport simulating the Aether RDK API."""

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
                    "status": "success",
                    "serial_number": body["serial_number"],
                    "calibration_source": "saved",
                    "calibration_file": f"calibrations/{body['serial_number']}.json",
                    "robot_ip": "192.168.155.53",
                    "token": "robot-session-token-123",
                    "mode": "blind_affine_touch_grid",
                    "screen_width": body["screen_width"],
                    "screen_height": body["screen_height"],
                },
            )
        if request.url.path == "/calibration/tap":
            if request.headers.get("authorization") != "Bearer robot-session-token-123":
                return httpx.Response(401, json={"detail": "Unauthorized"})
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "action": "tap",
                    "px": body["px"],
                    "py": body["py"],
                    "target_x": 200.0,
                    "target_y": -100.0,
                    "touch_z": 43.0,
                    "approach_z": 48.0,
                },
            )
        if request.url.path == "/calibration/swipe":
            if request.headers.get("authorization") != "Bearer robot-session-token-123":
                return httpx.Response(401, json={"detail": "Unauthorized"})
            return httpx.Response(
                200,
                json={
                    "status": "success",
                    "action": "swipe",
                    "from_px": body["from_px"],
                    "from_py": body["from_py"],
                    "to_px": body["to_px"],
                    "to_py": body["to_py"],
                    "from_x": 200.0,
                    "from_y": -100.0,
                    "from_touch_z": 43.0,
                    "to_x": 200.0,
                    "to_y": -50.0,
                    "to_touch_z": 43.0,
                    "approach_z": 48.0,
                },
            )
        if request.url.path == "/speed":
            return httpx.Response(200, json={"result": True})
        return httpx.Response(404, json={"detail": "Not found"})

    return httpx.AsyncClient(
        transport=httpx.MockTransport(handler),
        base_url="http://127.0.0.1:8010",
    )


def test_robot_touch_actuator_contract(test_context: ArtemisContext):
    requests_log: list[dict[str, Any]] = []
    client = _make_mock_robot_client(requests_log)
    actuator = RobotTouchActuator(
        test_context,
        robot_api_url="http://127.0.0.1:8010",
        http_client=client,
    )
    validate_actuator(actuator)


@pytest.mark.asyncio
async def test_touch_api_with_robot_actuator_end_to_end(
    test_context: ArtemisContext,
    monkeypatch: pytest.MonkeyPatch,
):
    """Touch API sends device info on /device and forwards tap/swipe to Robot API without touching calibration files."""
    monkeypatch.delenv("ROBOT_IP", raising=False)
    requests_log: list[dict[str, Any]] = []
    robot_http_client = _make_mock_robot_client(requests_log)
    robot_actuator = RobotTouchActuator(
        test_context,
        robot_api_url="http://127.0.0.1:8010",
        http_client=robot_http_client,
    )
    touch_app = create_touch_api_app(actuator=robot_actuator, ctx=test_context)

    async with httpx.AsyncClient(
        transport=httpx.ASGITransport(app=touch_app),
        base_url="http://touch-api",
    ) as client:
        with patch(
            "artemis.interfaces.touch_api.server._query_adb_screen_size",
            new=AsyncMock(return_value=(1080, 2340)),
        ):
            # 1. Configure device -> triggers POST /calibration/device on Robot API
            resp_dev = await client.post("/device", json={"device_id": "RXCY501ELYW"})
            assert resp_dev.status_code == 200
            dev_data = resp_dev.json()
            assert dev_data["ok"] is True
            assert dev_data["device_id"] == "RXCY501ELYW"
            assert dev_data["screen_width"] == 1080
            assert dev_data["screen_height"] == 2340

            assert len(requests_log) == 1
            assert requests_log[0]["path"] == "/calibration/device"
            assert requests_log[0]["json"] == {
                "serial_number": "RXCY501ELYW",
                "screen_width": 1080,
                "screen_height": 2340,
            }

            # 2. Tap -> triggers POST /speed (20, 4, 4) then POST /calibration/tap with Bearer token
            resp_tap = await client.post("/tap", json={"nx": 500, "ny": 500, "times": 1})
            assert resp_tap.status_code == 200
            assert resp_tap.json()["ok"] is True
            tap_req = [r for r in requests_log if r["path"] == "/calibration/tap"][-1]
            assert tap_req["headers"].get("authorization") == "Bearer robot-session-token-123"
            assert tap_req["json"]["px"] == 539.5
            assert tap_req["json"]["py"] == 1169.5

            # 3. Swipe -> triggers POST /calibration/swipe with Bearer token
            resp_swipe = await client.post(
                "/swipe",
                json={
                    "start_nx": 500,
                    "start_ny": 800,
                    "end_nx": 500,
                    "end_ny": 200,
                    "duration_ms": 400,
                },
            )
            assert resp_swipe.status_code == 200
            assert resp_swipe.json()["ok"] is True
            swipe_req = [r for r in requests_log if r["path"] == "/calibration/swipe"][-1]
            assert swipe_req["headers"].get("authorization") == "Bearer robot-session-token-123"
            assert swipe_req["json"] == {
                "from_px": 539.5,
                "from_py": 1871.2,
                "to_px": 539.5,
                "to_py": 467.8,
            }
            speed_reqs = [r["json"] for r in requests_log if r["path"] == "/speed"]
            assert speed_reqs == [{"speed": 20.0, "accel": 4.0, "decel": 4.0}]

    await robot_http_client.aclose()
