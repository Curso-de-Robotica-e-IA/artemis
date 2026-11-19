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

"""Unit tests for device-targeted live screen streaming routes and service."""

import asyncio
from types import SimpleNamespace

import pytest
from httpx import ASGITransport, AsyncClient

from apps.admin_console.core.state import state
from apps.admin_console.server import app
from apps.admin_console.services import device_stream_service as dss_mod
from apps.admin_console.services.device_stream_service import DeviceStreamService


class _DummyProc:
    """Async subprocess stub returning canned stdout per captured invocation."""

    def __init__(self, stdout: bytes, returncode: int = 0) -> None:
        self._stdout = stdout
        self.returncode = returncode
        self.killed = False

    async def communicate(self):
        return self._stdout, b""

    def kill(self) -> None:
        self.killed = True

    async def wait(self) -> int:
        return self.returncode


@pytest.fixture(autouse=True)
def _clean_server_state(monkeypatch):
    orig_runs = dict(state.active_runs)
    orig_queue = list(state.queue_items)
    orig_connections = dict(state.active_connections)
    orig_active_sid = state.active_session_id
    state.active_runs.clear()
    state.queue_items.clear()
    state.active_connections.clear()
    state.active_session_id = None

    monkeypatch.setattr(dss_mod.DeviceExecutionLock, "get_active_owners", lambda: {})
    monkeypatch.setattr(dss_mod.DeviceExecutionLock, "get_queued_tasks", lambda: [])
    monkeypatch.setattr(dss_mod.session_repo, "get_session_by_id", lambda _sid: None)
    monkeypatch.setattr(
        dss_mod.session_repo, "get_video_recording_for_session", lambda _sid: None
    )
    monkeypatch.setattr(dss_mod.readiness_engine, "get_probe_target_serial", lambda: None)

    yield

    state.active_runs.clear()
    state.active_runs.update(orig_runs)
    state.queue_items[:] = orig_queue
    state.active_connections.clear()
    state.active_connections.update(orig_connections)
    state.active_session_id = orig_active_sid


@pytest.mark.asyncio
async def test_device_state_selects_explicit_wifi_serial_over_usb_first(monkeypatch):
    monkeypatch.setattr(
        dss_mod.device_stream_service,
        "list_connected_serials",
        lambda _endpoint=None: asyncio.sleep(0, result=["usb-001", "192.168.1.55:5555"]),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res = await ac.get(
            "/api/stream/device-state",
            params={"serial": "192.168.1.55:5555", "session_id": "wifi-sess"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is True
    assert data["serial"] == "192.168.1.55:5555"
    assert "serial=192.168.1.55%3A5555" in data["live_stream_url"]
    assert "session_id=wifi-sess" in data["live_stream_url"]


@pytest.mark.asyncio
async def test_device_state_resolves_session_bound_wifi_serial(monkeypatch):
    state.active_runs["wifi-sess"] = {
        "process": None,
        "device_id": "192.168.1.55:5555",
        "goal": "Open Settings",
        "profile": "flash",
    }
    monkeypatch.setattr(
        dss_mod.device_stream_service,
        "list_connected_serials",
        lambda _endpoint=None: asyncio.sleep(0, result=["usb-001", "192.168.1.55:5555"]),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res = await ac.get("/api/stream/device-state", params={"session_id": "wifi-sess"})

    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is True
    assert data["serial"] == "192.168.1.55:5555"
    assert "serial=192.168.1.55%3A5555" in data["live_stream_url"]


@pytest.mark.asyncio
async def test_device_state_explicit_disconnected_serial_does_not_fallback_to_usb(monkeypatch):
    monkeypatch.setattr(
        dss_mod.device_stream_service,
        "list_connected_serials",
        lambda _endpoint=None: asyncio.sleep(0, result=["usb-001"]),
    )

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res = await ac.get(
            "/api/stream/device-state",
            params={"serial": "192.168.1.55:5555"},
        )

    assert res.status_code == 200
    data = res.json()
    assert data["connected"] is False
    assert data["serial"] is None
    assert data["live_stream_url"] is None


@pytest.mark.asyncio
async def test_stream_service_falls_back_to_active_run_and_probe_target_before_first_adb_device(
    monkeypatch,
):
    service = DeviceStreamService()
    monkeypatch.setattr(
        service,
        "list_connected_serials",
        lambda _endpoint=None: asyncio.sleep(0, result=["usb-001", "192.168.1.55:5555"]),
    )

    # 1. Idle preferred serial selected in UI
    monkeypatch.setattr(dss_mod.readiness_engine, "get_probe_target_serial", lambda: "192.168.1.55:5555")
    assert await service.get_device_serial() == "192.168.1.55:5555"

    # 2. Running task overrides idle preference
    state.active_session_id = "usb-run"
    state.queue_items.append({"session_id": "usb-run", "device_serial": "usb-001", "status": "running"})
    assert await service.get_device_serial() == "usb-001"


@pytest.mark.asyncio
async def test_stream_workers_are_isolated_per_serial_and_capture_with_target_flag(monkeypatch):
    service = DeviceStreamService()
    monkeypatch.setattr(dss_mod, "find_adb", lambda: "adb")
    monkeypatch.setattr(
        service,
        "list_connected_serials",
        lambda _endpoint=None: asyncio.sleep(0, result=["usb-001", "192.168.1.55:5555"]),
    )

    seen_commands: list[tuple[str, ...]] = []

    async def fake_exec(*cmd, **_kwargs):
        cmd_tuple = tuple(str(part) for part in cmd)
        seen_commands.append(cmd_tuple)
        if "-s" in cmd_tuple:
            serial_val = cmd_tuple[cmd_tuple.index("-s") + 1]
            payload = (f"PNG_{serial_val}_" + ("x" * 1100)).encode("utf-8")
            return _DummyProc(payload, returncode=0)
        return _DummyProc(b"", returncode=1)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", fake_exec)

    wifi_gen = service.mjpeg_frame_generator(serial="192.168.1.55:5555", session_id="wifi-sess")
    usb_gen = service.mjpeg_frame_generator(serial="usb-001", session_id="usb-sess")

    wifi_chunk = await asyncio.wait_for(wifi_gen.__anext__(), timeout=2.0)
    usb_chunk = await asyncio.wait_for(usb_gen.__anext__(), timeout=2.0)

    await wifi_gen.aclose()
    await usb_gen.aclose()

    assert b"PNG_192.168.1.55:5555_" in wifi_chunk
    assert b"PNG_usb-001_" not in wifi_chunk
    assert b"PNG_usb-001_" in usb_chunk
    assert b"PNG_192.168.1.55:5555_" not in usb_chunk

    assert any(
        "-s" in cmd and cmd[cmd.index("-s") + 1] == "192.168.1.55:5555" and "screencap" in cmd
        for cmd in seen_commands
    )


@pytest.mark.asyncio
async def test_api_status_includes_top_level_device_serial_for_active_run(monkeypatch):
    from apps.admin_console.routers import tasks as tasks_router

    monkeypatch.setattr(tasks_router.task_queue_service, "ensure_worker_running", lambda: None)
    monkeypatch.setattr(
        tasks_router.DeviceExecutionLock,
        "get_active_owner",
        lambda: SimpleNamespace(
            session_id="wifi-sess",
            device_id="192.168.1.55:5555",
            description="Open Settings",
            pid=999999,
            ingress="frontend",
            acquired_at="2026-11-19T12:00:00Z",
        ),
    )
    monkeypatch.setattr(
        tasks_router.DeviceExecutionLock,
        "get_active_owners",
        lambda: {
            "192.168.1.55:5555": SimpleNamespace(
                session_id="wifi-sess",
                device_id="192.168.1.55:5555",
                description="Open Settings",
                pid=999999,
                ingress="frontend",
                acquired_at="2026-11-19T12:00:00Z",
            )
        },
    )
    monkeypatch.setattr(tasks_router.DeviceExecutionLock, "get_queued_tasks", lambda: [])

    state.active_session_id = "wifi-sess"
    state.active_runs["wifi-sess"] = {
        "process": SimpleNamespace(returncode=None, pid=999999),
        "device_id": "192.168.1.55:5555",
        "goal": "Open Settings",
        "profile": "flash",
    }

    async with AsyncClient(transport=ASGITransport(app=app), base_url="http://localhost") as ac:
        res = await ac.get("/api/status")

    assert res.status_code == 200
    payload = res.json()
    assert payload["session_id"] == "wifi-sess"
    assert payload["device_serial"] == "192.168.1.55:5555"
