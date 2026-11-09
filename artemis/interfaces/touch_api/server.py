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


async def _query_adb_screen_size(device_id: str) -> tuple[int, int]:
    """Queries ADB for the active screen resolution (Override or Physical size)."""
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
    return int(width_str), int(height_str)


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
        "controller": controller,
        "ctx": ctx,
    }

    async def _get_actuator() -> AdbActuator:
        act = state_holder["actuator"]
        if act is None:
            runtime_ctx = state_holder["ctx"]
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

            act = AdbActuator(runtime_ctx, controller=state_holder["controller"])
            state_holder["actuator"] = act
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

        act: AdbActuator | None = state_holder["actuator"]
        if act is not None:
            act.ctx = new_ctx
            if hasattr(act, "controller") and act.controller is not None:
                act.controller.ctx = new_ctx
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
