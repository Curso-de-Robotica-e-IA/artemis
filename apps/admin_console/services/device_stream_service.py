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

"""Device Live Screen Streaming Service.

Provides real-time, low-latency device screen frames over HTTP MJPEG and WebSocket.
Operates concurrently with ADB agent actions with zero interference.
"""

import asyncio
from collections.abc import AsyncGenerator, Mapping
from dataclasses import dataclass
import json
import logging
import sqlite3
import subprocess
import time
from typing import Any

from artemis.core.diagnostics import readiness_engine
from artemis.runtime import AdbEndpoint, AdbSession, DeviceExecutionLock, current_adb_endpoint
from artemis.runtime.adb_endpoint import InvalidAdbEndpoint
from artemis.toolchain import find_adb

try:
    from admin_console.core.state import state
    from admin_console.database.repositories.session_repository import session_repo
except ImportError:
    from apps.admin_console.core.state import state
    from apps.admin_console.database.repositories.session_repository import session_repo

logger = logging.getLogger("artemis.stream_service")

_ADB_COMMAND_TIMEOUT_SECONDS = 5.0


def _clean_serial(value: Any) -> str | None:
    """Normalize a candidate ADB serial, rejecting empty or placeholder values."""
    if not isinstance(value, str):
        return None
    cleaned = value.strip()
    if not cleaned or cleaned.lower() == "pending":
        return None
    return cleaned


def _endpoint_from_mapping(raw: Any) -> AdbEndpoint | None:
    """Build an AdbEndpoint from a serialized run/queue mapping when valid."""
    if not isinstance(raw, Mapping):
        return None
    try:
        return AdbEndpoint.from_mapping(raw)
    except (InvalidAdbEndpoint, TypeError, ValueError):
        return None


def _serial_from_session_row(row: Mapping[str, Any] | None) -> str | None:
    """Extract the bound device serial from a persisted session record."""
    if not row:
        return None
    direct = _clean_serial(row.get("device_serial") or row.get("device_id"))
    if direct:
        return direct
    raw_info = row.get("device_info")
    if isinstance(raw_info, str):
        try:
            raw_info = json.loads(raw_info)
        except (TypeError, ValueError):
            raw_info = None
    if isinstance(raw_info, Mapping):
        return _clean_serial(raw_info.get("device_id") or raw_info.get("device_serial"))
    return None


@dataclass
class _StreamWorker:
    """Per-target screen capture worker with isolated latest frame state."""

    key: str
    serial: str | None
    session_id: str | None
    endpoint: AdbEndpoint
    active_listeners: int = 0
    latest_frame: bytes | None = None
    last_frame_time: float = 0.0
    last_captured_serial: str | None = None
    capture_task: asyncio.Task[None] | None = None


class DeviceStreamService:
    """Manages real-time screen capture and distribution to web clients."""

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._workers: dict[str, _StreamWorker] = {}

    def _resolve_session_metadata(self, session_id: str) -> tuple[str | None, AdbEndpoint | None]:
        """Look up the device serial and ADB endpoint assigned to a session."""
        clean_sid = session_id.strip()
        if not clean_sid:
            return None, None

        found_endpoint: AdbEndpoint | None = None

        run = state.active_runs.get(clean_sid)
        if isinstance(run, Mapping):
            found_endpoint = _endpoint_from_mapping(run.get("adb_endpoint"))
            serial = _clean_serial(run.get("device_id") or run.get("device_serial"))
            if serial:
                return serial, found_endpoint

        for item in state.queue_items:
            if not isinstance(item, Mapping):
                continue
            if str(item.get("session_id") or "").strip() != clean_sid:
                continue
            found_endpoint = found_endpoint or _endpoint_from_mapping(item.get("adb_endpoint"))
            serial = _clean_serial(item.get("device_serial") or item.get("device_id"))
            if serial:
                return serial, found_endpoint

        try:
            active_owners = DeviceExecutionLock.get_active_owners()
        except (OSError, RuntimeError, ValueError):
            active_owners = {}
        for owner in active_owners.values():
            if str(getattr(owner, "session_id", "") or "").strip() != clean_sid:
                continue
            serial = _clean_serial(getattr(owner, "device_id", None))
            if serial:
                return serial, found_endpoint

        try:
            queued = DeviceExecutionLock.get_queued_tasks()
        except (OSError, RuntimeError, ValueError):
            queued = []
        for q_item in queued:
            if not isinstance(q_item, Mapping):
                continue
            if str(q_item.get("session_id") or "").strip() != clean_sid:
                continue
            serial = _clean_serial(q_item.get("device_serial") or q_item.get("device_id"))
            if serial:
                return serial, found_endpoint

        row = session_repo.get_session_by_id(clean_sid)
        serial = _serial_from_session_row(row)
        if serial:
            return serial, found_endpoint

        recording = session_repo.get_video_recording_for_session(clean_sid)
        if isinstance(recording, Mapping):
            serial = _clean_serial(recording.get("device_id"))
            if serial:
                return serial, found_endpoint

        return None, found_endpoint

    def _resolve_active_run_metadata(self) -> tuple[str | None, AdbEndpoint | None]:
        """Resolve device metadata for the currently active run when no target was passed."""
        state.prune_finished_runs()

        if state.active_session_id:
            serial, endpoint = self._resolve_session_metadata(str(state.active_session_id))
            if serial:
                return serial, endpoint

        for run in state.active_runs.values():
            if not isinstance(run, Mapping):
                continue
            serial = _clean_serial(run.get("device_id") or run.get("device_serial"))
            if serial:
                return serial, _endpoint_from_mapping(run.get("adb_endpoint"))

        try:
            active_owners = DeviceExecutionLock.get_active_owners()
        except (OSError, RuntimeError, ValueError):
            active_owners = {}
        for owner in active_owners.values():
            serial = _clean_serial(getattr(owner, "device_id", None))
            if serial:
                return serial, None

        for item in state.queue_items:
            if isinstance(item, Mapping) and item.get("status") == "running":
                serial = _clean_serial(item.get("device_serial") or item.get("device_id"))
                if serial:
                    return serial, _endpoint_from_mapping(item.get("adb_endpoint"))

        return None, None

    async def list_connected_serials(self, endpoint: AdbEndpoint | None = None) -> list[str]:
        """Return currently connected ADB device serials in 'device' state."""
        try:
            adb_bin = find_adb()
            if not adb_bin:
                return []
            target_endpoint = endpoint or current_adb_endpoint()
            adb_session = AdbSession(target_endpoint, adb_path=adb_bin)
            proc = await asyncio.create_subprocess_exec(
                *adb_session.command(["devices"]),
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                env=adb_session.environment(),
            )
            try:
                stdout, _ = await asyncio.wait_for(
                    proc.communicate(),
                    timeout=_ADB_COMMAND_TIMEOUT_SECONDS,
                )
            except TimeoutError:
                proc.kill()
                await proc.wait()
                return []

            serials: list[str] = []
            lines = stdout.decode(errors="replace").strip().splitlines()
            for line in lines[1:]:
                parts = line.strip().split()
                if len(parts) >= 2 and parts[1] == "device":
                    serials.append(parts[0])
            return serials
        except (OSError, subprocess.SubprocessError, ValueError, RuntimeError) as exc:
            logger.warning("Error checking adb devices: %s", exc)
            return []

    async def resolve_stream_target(
        self,
        serial: str | None = None,
        session_id: str | None = None,
        *,
        require_connected: bool = False,
    ) -> tuple[str | None, AdbEndpoint, bool]:
        """Resolve the target `(serial, endpoint, is_bound_target)` for live streaming."""
        explicit_serial = _clean_serial(serial)
        clean_sid = session_id.strip() if isinstance(session_id, str) and session_id.strip() else None

        endpoint = current_adb_endpoint()
        session_serial: str | None = None
        if clean_sid:
            session_serial, session_endpoint = self._resolve_session_metadata(clean_sid)
            if session_endpoint is not None:
                endpoint = session_endpoint

        bound_serial = explicit_serial or session_serial
        if bound_serial:
            if require_connected:
                connected = await self.list_connected_serials(endpoint)
                return (bound_serial if bound_serial in connected else None), endpoint, True
            return bound_serial, endpoint, True

        active_serial, active_endpoint = self._resolve_active_run_metadata()
        if active_endpoint is not None:
            endpoint = active_endpoint

        connected = await self.list_connected_serials(endpoint)
        if active_serial:
            if not require_connected or active_serial in connected:
                return active_serial, endpoint, False

        preferred_serial = _clean_serial(readiness_engine.get_probe_target_serial())
        if preferred_serial and preferred_serial in connected:
            return preferred_serial, endpoint, False

        if connected:
            return connected[0], endpoint, False
        return None, endpoint, False

    async def get_device_serial(
        self,
        serial: str | None = None,
        session_id: str | None = None,
    ) -> str | None:
        """Resolve the connected target ADB device serial for live screen streaming."""
        resolved_serial, _, _ = await self.resolve_stream_target(
            serial=serial,
            session_id=session_id,
            require_connected=True,
        )
        return resolved_serial

    async def _capture_loop(self, worker: _StreamWorker) -> None:
        """Background frame capture loop dedicated to one target worker."""
        logger.info("[StreamService] Starting live screen capture loop (%s)...", worker.key)
        try:
            while worker.active_listeners > 0:
                try:
                    start_t = time.time()
                    if worker.serial:
                        target_serial = worker.serial
                        endpoint = worker.endpoint
                    else:
                        target_serial, endpoint, _ = await self.resolve_stream_target(
                            session_id=worker.session_id,
                            require_connected=False,
                        )
                        if target_serial and worker.session_id:
                            # Lock session-scoped worker onto its resolved device once known.
                            worker.serial = target_serial
                            worker.endpoint = endpoint

                    if target_serial != worker.last_captured_serial:
                        worker.latest_frame = None
                        worker.last_frame_time = 0.0
                        worker.last_captured_serial = target_serial

                    if not target_serial:
                        await asyncio.sleep(0.2)
                        continue

                    adb_bin = find_adb()
                    if not adb_bin:
                        await asyncio.sleep(0.5)
                        continue

                    adb_session = AdbSession(endpoint, adb_path=adb_bin)
                    cmd = adb_session.command(["-s", target_serial, "exec-out", "screencap", "-p"])
                    proc = await asyncio.create_subprocess_exec(
                        *cmd,
                        stdout=subprocess.PIPE,
                        stderr=subprocess.PIPE,
                        env=adb_session.environment(),
                    )
                    try:
                        stdout, _ = await asyncio.wait_for(
                            proc.communicate(),
                            timeout=_ADB_COMMAND_TIMEOUT_SECONDS,
                        )
                    except TimeoutError:
                        proc.kill()
                        await proc.wait()
                        stdout = b""
                    except asyncio.CancelledError:
                        proc.kill()
                        await proc.wait()
                        raise

                    if proc.returncode == 0 and len(stdout) > 1000:
                        worker.latest_frame = stdout
                        worker.last_frame_time = time.time()

                    elapsed = time.time() - start_t
                    delay = max(0.03, 0.08 - elapsed)
                    await asyncio.sleep(delay)
                except asyncio.CancelledError:
                    break
                except (
                    OSError,
                    subprocess.SubprocessError,
                    ValueError,
                    RuntimeError,
                    sqlite3.Error,
                ) as exc:
                    logger.error("[StreamService] Capture error (%s): %s", worker.key, exc)
                    await asyncio.sleep(0.5)
        finally:
            logger.info("[StreamService] Stopping live screen capture loop (%s).", worker.key)

    async def _acquire_worker(
        self,
        serial: str | None = None,
        session_id: str | None = None,
    ) -> _StreamWorker:
        """Register a listener on the worker matching `(serial, session_id)`."""
        initial_serial, endpoint, _ = await self.resolve_stream_target(
            serial=serial,
            session_id=session_id,
            require_connected=False,
        )
        clean_sid = (
            session_id.strip() if isinstance(session_id, str) and session_id.strip() else None
        )
        if initial_serial:
            key = f"{endpoint.identity}|serial:{initial_serial}"
        elif clean_sid:
            key = f"{endpoint.identity}|session:{clean_sid}"
        else:
            key = f"{endpoint.identity}|default"

        async with self._lock:
            worker = self._workers.get(key)
            if worker is None:
                worker = _StreamWorker(
                    key=key,
                    serial=initial_serial,
                    session_id=clean_sid,
                    endpoint=endpoint,
                )
                self._workers[key] = worker
            worker.active_listeners += 1
            if worker.capture_task is None or worker.capture_task.done():
                worker.capture_task = asyncio.create_task(self._capture_loop(worker))
            return worker

    async def _release_worker(self, worker: _StreamWorker) -> None:
        """Deregister a listener and stop the worker when no listeners remain."""
        async with self._lock:
            worker.active_listeners = max(0, worker.active_listeners - 1)
            if worker.active_listeners == 0:
                if worker.capture_task and not worker.capture_task.done():
                    worker.capture_task.cancel()
                if self._workers.get(worker.key) is worker:
                    self._workers.pop(worker.key, None)

    async def mjpeg_frame_generator(
        self,
        serial: str | None = None,
        session_id: str | None = None,
    ) -> AsyncGenerator[bytes, None]:
        """Async generator streaming MJPEG multipart bytes for the target device."""
        worker = await self._acquire_worker(serial=serial, session_id=session_id)
        try:
            last_sent_time = 0.0
            while True:
                if worker.latest_frame and worker.last_frame_time > last_sent_time:
                    last_sent_time = worker.last_frame_time
                    frame_bytes = worker.latest_frame
                    yield (
                        b"--frame\r\n"
                        b"Content-Type: image/png\r\n"
                        b"Content-Length: "
                        + str(len(frame_bytes)).encode()
                        + b"\r\n\r\n"
                        + frame_bytes
                        + b"\r\n"
                    )
                await asyncio.sleep(0.04)
        finally:
            await self._release_worker(worker)


device_stream_service = DeviceStreamService()
