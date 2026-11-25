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


@pytest.mark.asyncio
async def test_device_timeout_and_default_device_id_sync(touch_app):
    """POST /device must always be called (even for 'default' device_id) and use device_timeout_s (300s default)."""
    default_ctx = ArtemisContext(
        device=DeviceContext(
            host_platform=platform.os_type.name,
            mobile_platform=DevicePlatform.ANDROID,
            device_id="default",
            device_width=1080,
            device_height=2400,
        ),
    )
    async with _make_client(touch_app) as http_client:
        with patch(
            "artemis.interfaces.touch_api.server._query_adb_screen_size",
            new=AsyncMock(return_value=(1080, 2400)),
        ) as mock_query:
            actuator = HttpTouchActuator(default_ctx, client=http_client)
            assert actuator.timeout_s == 30.0
            assert actuator.device_timeout_s == 300.0

            with patch.object(http_client, "post", wraps=http_client.post) as spy_post:
                res = await actuator.click(500, 500)
                assert res.ok
                assert actuator._synced_device_id == "default"
                mock_query.assert_awaited_once_with("default")

                # First call is POST /device with 300s timeout; second call is POST /tap with 30s timeout
                assert spy_post.await_count == 2
                device_call = spy_post.await_args_list[0]
                tap_call = spy_post.await_args_list[1]
                assert device_call.args[0].endswith("/device")
                assert device_call.kwargs["timeout"] == 300.0
                assert tap_call.args[0].endswith("/tap")
                assert tap_call.kwargs["timeout"] == 30.0


@pytest.mark.asyncio
async def test_agent_startup_mandatorily_calls_touch_api_device(monkeypatch):
    """When ARTEMIS_ACTUATOR_BACKEND='http', Agent initialization must call POST /device and wait before completing."""
    from artemis.mcp.action_types import ActionResult
    from artemis.sdk.agent import Agent
    from third_party.mobile_use.sdk.types.exceptions import AgentError

    HttpTouchActuator.reset_synced_devices()
    monkeypatch.setattr(settings, "ARTEMIS_ACTUATOR_BACKEND", "http")
    monkeypatch.setattr(settings, "ARTEMIS_TOUCH_API_URL", "http://127.0.0.1:8001")
    monkeypatch.setattr(settings, "ARTEMIS_TOUCH_API_DEVICE_TIMEOUT_S", 240.0)
    monkeypatch.setenv("ARTEMIS_FAKE_LLM", "1")

    agent = Agent(device_id="robot-device-01")
    monkeypatch.setattr("third_party.mobile_use.sdk.agent.which", lambda _cmd: "/usr/bin/adb")
    monkeypatch.setattr(agent, "_init_clients", lambda **_kwargs: None)
    monkeypatch.setattr(
        agent,
        "_get_device_context",
        AsyncMock(
            return_value=DeviceContext(
                host_platform=platform.os_type.name,
                mobile_platform=DevicePlatform.ANDROID,
                device_id="robot-device-01",
                device_width=1080,
                device_height=2400,
            )
        ),
    )

    recorded_timeouts: list[float | None] = []

    async def _fake_post(self, path, payload, action=None, *, timeout_s=None):
        recorded_timeouts.append(timeout_s)
        assert path == "/device"
        assert payload == {"device_id": "robot-device-01"}
        return ActionResult.success(
            action=action or "configure_device",
            message="Device configured: robot-device-01 (1080x2400).",
        )

    with patch.object(HttpTouchActuator, "_post", new=_fake_post):
        ok = await agent.init()
        assert ok is True
        assert agent._initialized is True
        assert recorded_timeouts == [240.0]

        # Subsequent HttpTouchActuator created during task execution reuses the startup sync
        task_ctx = ArtemisContext(device=agent._device_context)
        task_actuator = HttpTouchActuator(task_ctx)
        assert task_actuator._synced_device_id == "robot-device-01"

    await agent.clean()
    assert HttpTouchActuator._synced_devices_by_url == {}

    # Verify startup fails with AgentError if POST /device fails
    agent_fail = Agent(device_id="robot-device-01")
    monkeypatch.setattr(agent_fail, "_init_clients", lambda **_kwargs: None)
    monkeypatch.setattr(
        agent_fail,
        "_get_device_context",
        AsyncMock(
            return_value=DeviceContext(
                host_platform=platform.os_type.name,
                mobile_platform=DevicePlatform.ANDROID,
                device_id="robot-device-01",
                device_width=1080,
                device_height=2400,
            )
        ),
    )
    with patch.object(
        HttpTouchActuator,
        "configure_device",
        new=AsyncMock(
            return_value=ActionResult.failure(
                action="configure_device",
                message="Robot calibration failed",
                detail="Robot arm timeout",
            )
        ),
    ):
        with pytest.raises(AgentError, match="Failed to configure device 'robot-device-01'"):
            await agent_fail.init()
