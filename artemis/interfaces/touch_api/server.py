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

"""HTTP Touch & Input API server backed by ADB."""

import asyncio
import json
import os
import re
import time
from typing import Any

from pydantic import BaseModel, Field, ValidationError
from starlette.applications import Starlette
from starlette.requests import Request
from starlette.responses import JSONResponse
from starlette.routing import Route

from artemis.config.settings import settings
from artemis.context import ArtemisContext, DeviceContext, DevicePlatform
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.action_types import ActionResult
from artemis.mcp.actuators.adb import AdbActuator
from artemis.platform import platform
from artemis.toolchain import find_adb
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

__all__ = [
    "DeviceConfigRequest",
    "DeviceConfigResponse",
    "DragRequest",
    "EraseCharRequest",
    "FocusAndClearRequest",
    "InputTextRequest",
    "LongPressRequest",
    "PressKeyRequest",
    "SwipeRequest",
    "TapPoint",
    "TapRequest",
    "TapSequenceRequest",
    "TouchActionResponse",
    "app",
    "create_touch_api_app",
]

_WM_SIZE_PATTERN = re.compile(r"(\d+)x(\d+)")
_CUR_DISPLAY_SIZE_PATTERN = re.compile(r"\bcur=(\d+)x(\d+)")
_LANDSCAPE_ROTATION_PATTERN = re.compile(
    r"(?:\bmRotation=(?:1|3|ROTATION_90|ROTATION_270)\b"
    r"|\bmCurrentRotation=(?:1|3|ROTATION_90|ROTATION_270)\b"
    r"|\bmDisplayRotation=(?:1|3|ROTATION_90|ROTATION_270)\b"
    r"|\bSurfaceOrientation:\s*[13]\b"
    r"|\borientation=(?:1|3)\b)"
)
_PORTRAIT_ROTATION_PATTERN = re.compile(
    r"(?:\bmRotation=(?:0|2|ROTATION_0|ROTATION_180)\b"
    r"|\bmCurrentRotation=(?:0|2|ROTATION_0|ROTATION_180)\b"
    r"|\bmDisplayRotation=(?:0|2|ROTATION_0|ROTATION_180)\b"
    r"|\bSurfaceOrientation:\s*[02]\b"
    r"|\borientation=(?:0|2)\b)"
)
_ORIENTATION_REFRESH_TTL_S = 1.0


class DeviceConfigRequest(BaseModel):
    """Request payload for ``POST /device``."""

    device_id: str = Field(..., min_length=1, description="ADB serial number or IP:port")


class DeviceConfigResponse(BaseModel):
    """Response payload for ``POST /device``."""

    ok: bool
    device_id: str
    screen_width: int
    screen_height: int
    message: str
    error: str | None = None


class TapRequest(BaseModel):
    """Request payload for ``POST /tap``."""

    nx: int = Field(..., ge=0, le=1000)
    ny: int = Field(..., ge=0, le=1000)
    times: int = Field(default=1, ge=1)
    delay_ms: int = Field(default=100, ge=0)


class TapPoint(BaseModel):
    """Single normalized point in a ``POST /tap-sequence`` request."""

    nx: int = Field(..., ge=0, le=1000)
    ny: int = Field(..., ge=0, le=1000)


class TapSequenceRequest(BaseModel):
    """Request payload for ``POST /tap-sequence``."""

    points: list[TapPoint] = Field(..., min_length=1)
    delay_ms: int = Field(default=50, ge=0)


class LongPressRequest(BaseModel):
    """Request payload for ``POST /long-press``."""

    nx: int = Field(..., ge=0, le=1000)
    ny: int = Field(..., ge=0, le=1000)
    duration_ms: int = Field(default=1000, ge=1)


class SwipeRequest(BaseModel):
    """Request payload for ``POST /swipe``."""

    start_nx: int = Field(..., ge=0, le=1000)
    start_ny: int = Field(..., ge=0, le=1000)
    end_nx: int = Field(..., ge=0, le=1000)
    end_ny: int = Field(..., ge=0, le=1000)
    duration_ms: int = Field(default=400, ge=1)


class DragRequest(BaseModel):
    """Request payload for ``POST /drag``."""

    start_nx: int = Field(..., ge=0, le=1000)
    start_ny: int = Field(..., ge=0, le=1000)
    end_nx: int = Field(..., ge=0, le=1000)
    end_ny: int = Field(..., ge=0, le=1000)
    hold_ms: int = Field(default=1000, ge=0)
    duration_ms: int = Field(default=400, ge=1)


class InputTextRequest(BaseModel):
    """Request payload for ``POST /input-text``."""

    text: str = Field(..., min_length=1)


class FocusAndClearRequest(BaseModel):
    """Request payload for ``POST /focus-and-clear``."""

    nx: int = Field(..., ge=0, le=1000)
    ny: int = Field(..., ge=0, le=1000)


class EraseCharRequest(BaseModel):
    """Request payload for ``POST /erase-char``."""

    times: int = Field(default=1, ge=1)


class PressKeyRequest(BaseModel):
    """Request payload for ``POST /press-key``."""

    key: str = Field(..., min_length=1)


class TouchActionResponse(BaseModel):
    """Standard JSON response returned by all action endpoints."""

    ok: bool
    message: str
    error: str | None = None


async def _query_adb_display_orientation_size(
    adb_bin: str,
    device_id: str,
    base_width: int,
    base_height: int,
) -> tuple[int, int]:
    """Resolves the active orientation-aware screen size via ``dumpsys window displays``."""
    cmd = [adb_bin]
    if device_id and device_id != "default":
        cmd.extend(["-s", device_id])
    cmd.extend(["shell", "dumpsys", "window", "displays"])

    try:
        proc = await asyncio.create_subprocess_exec(
            *cmd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout, _stderr = await asyncio.wait_for(proc.communicate(), timeout=3.0)
        if proc.returncode != 0:
            return base_width, base_height
        text = stdout.decode("utf-8", errors="replace")
    except (OSError, TimeoutError, ValueError):
        return base_width, base_height

    cur_match = _CUR_DISPLAY_SIZE_PATTERN.search(text)
    if cur_match:
        cur_w, cur_h = int(cur_match.group(1)), int(cur_match.group(2))
        if cur_w > 0 and cur_h > 0:
            return cur_w, cur_h

    if _LANDSCAPE_ROTATION_PATTERN.search(text):
        return max(base_width, base_height), min(base_width, base_height)
    if _PORTRAIT_ROTATION_PATTERN.search(text):
        return min(base_width, base_height), max(base_width, base_height)

    return base_width, base_height


async def _query_adb_screen_size(device_id: str) -> tuple[int, int]:
    """Queries ADB for the active orientation-aware screen resolution."""
    adb_bin = find_adb() or "adb"
    cmd = [adb_bin]
    if device_id and device_id != "default":
        cmd.extend(["-s", device_id])
    cmd.extend(["shell", "wm", "size"])

    proc = await asyncio.create_subprocess_exec(
        *cmd,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    stdout, stderr = await proc.communicate()
    output = stdout.decode("utf-8", errors="replace").strip()

    if proc.returncode != 0:
        err_msg = stderr.decode("utf-8", errors="replace").strip() or output
        raise RuntimeError(f"Failed to query screen size for '{device_id}': {err_msg}")

    matches = _WM_SIZE_PATTERN.findall(output)
    if not matches:
        raise RuntimeError(f"Could not parse 'wm size' output for '{device_id}': {output}")

    width_str, height_str = matches[-1]
    base_width, base_height = int(width_str), int(height_str)
    return await _query_adb_display_orientation_size(
        adb_bin,
        device_id,
        base_width,
        base_height,
    )


def _build_context(
    device_id: str | None = None,
    width: int = 1080,
    height: int = 2400,
) -> ArtemisContext:
    serial = (
        device_id
        or os.environ.get("ADB_DEVICE_SERIAL")
        or getattr(settings, "ADB_DEVICE_SERIAL", None)
        or "default"
    )
    return ArtemisContext(
        device=DeviceContext(
            host_platform=platform.os_type.name,
            mobile_platform=DevicePlatform.ANDROID,
            device_id=str(serial),
            device_width=width,
            device_height=height,
        ),
    )


def _action_result_to_response(res: ActionResult) -> JSONResponse:
    body = TouchActionResponse(
        ok=res.ok,
        message=res.message,
        error=None if res.ok else (res.detail or res.message),
    )
    status_code = 200 if res.ok else 500
    return JSONResponse(body.model_dump(), status_code=status_code)


def _validation_error_response(exc: BaseException) -> JSONResponse:
    return JSONResponse(
        TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
        status_code=422,
    )


def create_touch_api_app(
    actuator: AdbActuator | None = None,
    controller: UnifiedMobileController | None = None,
    ctx: ArtemisContext | None = None,
) -> Starlette:
    """Creates an ASGI application serving the ADB-backed touch and input routes."""
    state_holder: dict[str, Any] = {
        "actuator": actuator,
        "custom_actuator": actuator is not None,
        "controller": controller,
        "ctx": ctx,
        "last_size_check": 0.0,
    }

    def _sync_actuator_dimensions(
        act: AdbActuator,
        runtime_ctx: ArtemisContext,
        width: int,
        height: int,
    ) -> None:
        if runtime_ctx.device is not None:
            runtime_ctx.device.device_width = width
            runtime_ctx.device.device_height = height
        act.ctx = runtime_ctx
        ctrl = getattr(act, "controller", None)
        if ctrl is not None:
            ctrl.ctx = runtime_ctx
            drv = getattr(ctrl, "_driver", None)
            if drv is not None:
                if hasattr(drv, "_width"):
                    drv._width = width
                if hasattr(drv, "_height"):
                    drv._height = height

    async def _get_actuator() -> AdbActuator:
        act: AdbActuator | None = state_holder["actuator"]
        if act is None:
            runtime_ctx: ArtemisContext | None = state_holder["ctx"]
            if runtime_ctx is None:
                default_serial = (
                    os.environ.get("ADB_DEVICE_SERIAL")
                    or getattr(settings, "ADB_DEVICE_SERIAL", None)
                    or "default"
                )
                try:
                    w, h = await _query_adb_screen_size(str(default_serial))
                except (RuntimeError, OSError, ValueError) as exc:
                    logger.warning(f"Falling back to 1080x2400 screen size: {exc}")
                    w, h = 1080, 2400
                runtime_ctx = _build_context(device_id=str(default_serial), width=w, height=h)
                state_holder["ctx"] = runtime_ctx
                state_holder["last_size_check"] = time.monotonic()

            act = AdbActuator(runtime_ctx, controller=state_holder["controller"])
            state_holder["actuator"] = act
            return act

        # Dynamically refresh screen dimensions when device orientation changes mid-session
        if not state_holder["custom_actuator"]:
            now = time.monotonic()
            if now - float(state_holder["last_size_check"]) >= _ORIENTATION_REFRESH_TTL_S:
                runtime_ctx = state_holder["ctx"] or getattr(act, "ctx", None)
                device_id = (
                    runtime_ctx.device.device_id
                    if runtime_ctx and runtime_ctx.device and runtime_ctx.device.device_id
                    else "default"
                )
                try:
                    w, h = await _query_adb_screen_size(str(device_id))
                    if runtime_ctx is not None:
                        _sync_actuator_dimensions(act, runtime_ctx, w, h)
                except (RuntimeError, OSError, ValueError):
                    pass
                state_holder["last_size_check"] = now

        return act

    async def health_endpoint(_request: Request) -> JSONResponse:
        current_ctx: ArtemisContext | None = state_holder.get("ctx")
        device_info = None
        if current_ctx and current_ctx.device:
            device_info = {
                "device_id": current_ctx.device.device_id,
                "screen_width": current_ctx.device.device_width,
                "screen_height": current_ctx.device.device_height,
            }
        return JSONResponse(
            {
                "status": "ok",
                "service": "artemis-touch-api",
                "device": device_info,
            }
        )

    async def set_device_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            req = DeviceConfigRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)

        try:
            width, height = await _query_adb_screen_size(req.device_id)
        except (RuntimeError, OSError, ValueError) as exc:
            body = DeviceConfigResponse(
                ok=False,
                device_id=req.device_id,
                screen_width=0,
                screen_height=0,
                message=f"Failed to query device '{req.device_id}'.",
                error=str(exc),
            )
            return JSONResponse(body.model_dump(), status_code=500)

        new_ctx = _build_context(device_id=req.device_id, width=width, height=height)
        state_holder["ctx"] = new_ctx
        state_holder["last_size_check"] = time.monotonic()

        act: AdbActuator | None = state_holder["actuator"]
        if state_holder["custom_actuator"] and act is not None:
            _sync_actuator_dimensions(act, new_ctx, width, height)
        else:
            state_holder["actuator"] = AdbActuator(
                new_ctx, controller=state_holder["controller"]
            )

        body = DeviceConfigResponse(
            ok=True,
            device_id=req.device_id,
            screen_width=width,
            screen_height=height,
            message=f"Device configured: {req.device_id} ({width}x{height}).",
        )
        return JSONResponse(body.model_dump(), status_code=200)

    async def tap_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            req = TapRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)
        try:
            act = await _get_actuator()
            res = await act.click(req.nx, req.ny, times=req.times, delay_ms=req.delay_ms)
            return _action_result_to_response(res)
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            return JSONResponse(
                TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
                status_code=500,
            )

    async def tap_sequence_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            req = TapSequenceRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)
        try:
            act = await _get_actuator()
            points = [(pt.nx, pt.ny) for pt in req.points]
            res = await act.click_sequence(points, delay_ms=req.delay_ms)
            return _action_result_to_response(res)
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            return JSONResponse(
                TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
                status_code=500,
            )

    async def long_press_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            req = LongPressRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)
        try:
            act = await _get_actuator()
            res = await act.long_press(req.nx, req.ny, duration_ms=req.duration_ms)
            return _action_result_to_response(res)
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            return JSONResponse(
                TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
                status_code=500,
            )

    async def swipe_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            req = SwipeRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)
        try:
            act = await _get_actuator()
            res = await act.swipe(
                start=(req.start_nx, req.start_ny),
                end=(req.end_nx, req.end_ny),
                duration_ms=req.duration_ms,
            )
            time.sleep(1)
            return _action_result_to_response(res)
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            return JSONResponse(
                TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
                status_code=500,
            )

    async def drag_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            req = DragRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)
        try:
            act = await _get_actuator()
            res = await act.drag(
                start=(req.start_nx, req.start_ny),
                end=(req.end_nx, req.end_ny),
                hold_ms=req.hold_ms,
                duration_ms=req.duration_ms,
            )
            return _action_result_to_response(res)
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            return JSONResponse(
                TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
                status_code=500,
            )

    async def input_text_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            req = InputTextRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)
        try:
            act = await _get_actuator()
            res = await act.input_text(req.text)
            return _action_result_to_response(res)
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            return JSONResponse(
                TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
                status_code=500,
            )

    async def focus_and_clear_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            req = FocusAndClearRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)
        try:
            act = await _get_actuator()
            res = await act.focus_and_clear_text(req.nx, req.ny)
            return _action_result_to_response(res)
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            return JSONResponse(
                TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
                status_code=500,
            )

    async def erase_char_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json() if await request.body() else {}
            req = EraseCharRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)
        try:
            act = await _get_actuator()
            res = ActionResult.success(action="erase_one_char", message="No characters erased.")
            for _ in range(req.times):
                res = await act.erase_one_char()
                if not res.ok:
                    break
            return _action_result_to_response(res)
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            return JSONResponse(
                TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
                status_code=500,
            )

    async def press_key_endpoint(request: Request) -> JSONResponse:
        try:
            payload = await request.json()
            req = PressKeyRequest.model_validate(payload)
        except (json.JSONDecodeError, ValidationError, ValueError) as exc:
            return _validation_error_response(exc)
        try:
            act = await _get_actuator()
            res = await act.press_key(req.key)
            return _action_result_to_response(res)
        except (RuntimeError, OSError, ValueError, TimeoutError) as exc:
            return JSONResponse(
                TouchActionResponse(ok=False, message=str(exc), error=str(exc)).model_dump(),
                status_code=500,
            )

    routes = [
        Route("/", health_endpoint, methods=["GET"]),
        Route("/health", health_endpoint, methods=["GET"]),
        Route("/device", set_device_endpoint, methods=["POST"]),
        Route("/tap", tap_endpoint, methods=["POST"]),
        Route("/tap-sequence", tap_sequence_endpoint, methods=["POST"]),
        Route("/long-press", long_press_endpoint, methods=["POST"]),
        Route("/swipe", swipe_endpoint, methods=["POST"]),
        Route("/drag", drag_endpoint, methods=["POST"]),
        Route("/input-text", input_text_endpoint, methods=["POST"]),
        Route("/focus-and-clear", focus_and_clear_endpoint, methods=["POST"]),
        Route("/erase-char", erase_char_endpoint, methods=["POST"]),
        Route("/press-key", press_key_endpoint, methods=["POST"]),
    ]
    return Starlette(routes=routes)


app = create_touch_api_app()


def main() -> None:
    """Runs the ADB Touch HTTP API server using Uvicorn."""
    import uvicorn

    host = os.environ.get("TOUCH_API_HOST", "127.0.0.1")
    port = int(os.environ.get("TOUCH_API_PORT", "8001"))
    uvicorn.run(app, host=host, port=port)


if __name__ == "__main__":
    main()
