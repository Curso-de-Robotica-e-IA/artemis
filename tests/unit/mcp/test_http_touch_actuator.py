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

"""Tests for HttpTouchActuator and the HTTP Touch API server."""

from unittest.mock import AsyncMock, patch

import httpx
import pytest

from artemis.config.settings import settings
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.mcp.action_manifest import DEVICE_ACTIONS, validate_actuator
from artemis.mcp.actuators import (
    Actuator,
    AdbActuator,
    HttpTouchActuator,
    MockActuator,
    create_actuator,
)
from artemis.interfaces.touch_api.server import create_touch_api_app
from artemis.platform import platform


@pytest.fixture
def mock_underlying_actuator() -> MockActuator:
    return MockActuator(width=1080, height=2400)


@pytest.fixture
def touch_app(mock_underlying_actuator: MockActuator):
    return create_touch_api_app(actuator=mock_underlying_actuator)


@pytest.fixture
def test_context() -> ArtemisContext:
    return ArtemisContext(
        device=DeviceContext(
            host_platform=platform.os_type.name,
            mobile_platform=DevicePlatform.ANDROID,
            device_id="test-device-123",
            device_width=1080,
            device_height=2400,
        ),
    )


def _make_client(app) -> httpx.AsyncClient:
    transport = httpx.ASGITransport(app=app)
    return httpx.AsyncClient(transport=transport, base_url="http://testserver")


def test_actuator_protocol_and_capabilities(test_context: ArtemisContext, touch_app):
    client = _make_client(touch_app)
    actuator = HttpTouchActuator(test_context, client=client)

    assert isinstance(actuator, Actuator)
    assert isinstance(actuator, AdbActuator)
    assert isinstance(actuator, HttpTouchActuator)
    assert actuator.capabilities() == DEVICE_ACTIONS
    assert "input_text" in actuator.capabilities()
    assert "focus_and_clear_text" in actuator.capabilities()
    assert "erase_one_char" in actuator.capabilities()
    assert "press_key" in actuator.capabilities()
    # Contract validation must pass with zero violations
    validate_actuator(actuator)


@pytest.mark.asyncio
async def test_device_configuration_route(touch_app):
    async with _make_client(touch_app) as http_client:
        with patch(
            "artemis.interfaces.touch_api.server._query_adb_screen_size",
            new=AsyncMock(return_value=(1440, 3200)),
        ) as mock_query:
            resp = await http_client.post("/device", json={"device_id": "192.168.1.50:5555"})
            assert resp.status_code == 200
            data = resp.json()
            assert data["ok"] is True
            assert data["device_id"] == "192.168.1.50:5555"
            assert data["screen_width"] == 1440
            assert data["screen_height"] == 3200
            mock_query.assert_awaited_once_with("192.168.1.50:5555")

            # Health endpoint should reflect the updated active device
            health_resp = await http_client.get("/health")
            assert health_resp.status_code == 200
            health_data = health_resp.json()
            assert health_data["status"] == "ok"
            assert health_data["device"]["device_id"] == "192.168.1.50:5555"
            assert health_data["device"]["screen_width"] == 1440
            assert health_data["device"]["screen_height"] == 3200


@pytest.mark.asyncio
async def test_auto_sync_device_configuration_on_first_action(
    test_context: ArtemisContext,
    touch_app,
):
    async with _make_client(touch_app) as http_client:
        with patch(
            "artemis.interfaces.touch_api.server._query_adb_screen_size",
            new=AsyncMock(return_value=(1080, 2400)),
        ) as mock_query:
            actuator = HttpTouchActuator(test_context, client=http_client)
            assert actuator._synced_device_id is None

            # First action should auto-sync device
            res1 = await actuator.click(500, 500)
            assert res1.ok
            assert actuator._synced_device_id == "test-device-123"
            assert mock_query.await_count == 1

            # Second action with same device should not trigger another sync
            res2 = await actuator.click(600, 600)
            assert res2.ok
            assert mock_query.await_count == 1


@pytest.mark.asyncio
async def test_touch_routes_delegation(
    test_context: ArtemisContext,
    touch_app,
    mock_underlying_actuator: MockActuator,
):
    async with _make_client(touch_app) as http_client:
        with patch(
            "artemis.interfaces.touch_api.server._query_adb_screen_size",
            new=AsyncMock(return_value=(1080, 2400)),
        ):
            actuator = HttpTouchActuator(test_context, client=http_client)

            # 1. Tap (/tap)
            res_tap = await actuator.click(500, 500)
            assert res_tap.ok
            assert "500, 500" in res_tap.message
            assert mock_underlying_actuator.action_history[-1]["action"] == "tap"

            # 2. Tap sequence (/tap-sequence)
            res_seq = await actuator.click_sequence([(100, 200), (800, 900)], delay_ms=20)
            assert res_seq.ok
            assert "[100, 200]" in res_seq.message

            # 3. Long press (/long-press)
            res_lp = await actuator.long_press(300, 400, duration_ms=1200)
            assert res_lp.ok
            assert "300, 400" in res_lp.message

            # 4. Swipe (/swipe)
            res_sw = await actuator.swipe((500, 800), (500, 200), duration_ms=350)
            assert res_sw.ok
            assert "[500, 800] to [500, 200]" in res_sw.message


@pytest.mark.asyncio
async def test_text_and_key_routes(
    test_context: ArtemisContext,
    touch_app,
):
    async with _make_client(touch_app) as http_client:
        with patch(
            "artemis.interfaces.touch_api.server._query_adb_screen_size",
            new=AsyncMock(return_value=(1080, 2400)),
        ):
            actuator = HttpTouchActuator(test_context, client=http_client)

            # 1. Input text (/input-text)
            res_text = await actuator.input_text("Test Input")
            assert res_text.ok

            # 2. Focus and clear text (/focus-and-clear)
            res_fc = await actuator.focus_and_clear_text(400, 300)
            assert res_fc.ok

            # 3. Erase character (/erase-char)
            res_ec = await actuator.erase_one_char()
            assert res_ec.ok

            # 4. Press key (/press-key)
            res_pk = await actuator.press_key("BACK")
            assert res_pk.ok


def test_create_actuator_factory(test_context: ArtemisContext, monkeypatch):
    monkeypatch.setattr(settings, "ARTEMIS_ACTUATOR_BACKEND", "http")
    act_http = create_actuator(test_context)
    assert isinstance(act_http, HttpTouchActuator)

    monkeypatch.setattr(settings, "ARTEMIS_ACTUATOR_BACKEND", "adb")
    act_adb = create_actuator(test_context)
    assert isinstance(act_adb, AdbActuator)
    assert not isinstance(act_adb, HttpTouchActuator)


@pytest.mark.asyncio
async def test_touch_api_validation_and_error_handling(touch_app):
    async with _make_client(touch_app) as http_client:
        # Invalid coordinate > 1000 should return 422
        resp_invalid = await http_client.post("/tap", json={"nx": 1500, "ny": 500})
        assert resp_invalid.status_code == 422
        data_invalid = resp_invalid.json()
        assert data_invalid["ok"] is False

        # Failure querying screen size in /device should return 500
        with patch(
            "artemis.interfaces.touch_api.server._query_adb_screen_size",
            new=AsyncMock(side_effect=RuntimeError("Device offline")),
        ):
            resp_err = await http_client.post("/device", json={"device_id": "offline-device"})
            assert resp_err.status_code == 500
            data_err = resp_err.json()
            assert data_err["ok"] is False
            assert "Device offline" in (data_err["error"] or "")
