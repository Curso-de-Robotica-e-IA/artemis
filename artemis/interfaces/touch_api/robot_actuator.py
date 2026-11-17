import asyncio
from collections.abc import Sequence
import os
from typing import Any

import httpx

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.action_types import ActionResult
from artemis.mcp.actuators.adb import AdbActuator
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)


def _env_bool(name: str, default: bool = False) -> bool:
    val = os.environ.get(name, "").strip().lower()
    if not val:
        return default
    return val in {"1", "true", "yes", "on"}


class RobotTouchActuator(AdbActuator):
    """Actuator that delegates physical screen touches to the Aether RDK API."""

    def __init__(
        self,
        ctx: ArtemisContext,
        controller: UnifiedMobileController | None = None,
        robot_api_url: str | None = None,
        robot_ip: str | None = None,
        mirror_touch_to_adb: bool | None = None,
        timeout_s: float = 60.0,
        setup_timeout_s: float = 300.0,
        http_client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(ctx, controller=controller)
        self.robot_api_url = (
            robot_api_url
            or os.environ.get("ROBOT_API_URL", "http://127.0.0.1:8010")
        ).rstrip("/")
        self.robot_ip = robot_ip or os.environ.get("ROBOT_IP") or None
        self.mirror_touch_to_adb = (
            mirror_touch_to_adb
            if mirror_touch_to_adb is not None
            else _env_bool("MIRROR_TOUCH_TO_ADB", default=False)
        )
        self.timeout_s = timeout_s
        self.setup_timeout_s = max(setup_timeout_s, timeout_s)
        self._http_client = http_client
        self._token: str | None = None
        self._configured_device_id: str | None = None

    def _headers(self) -> dict[str, str]:
        if not self._token:
            return {}
        return {"Authorization": f"Bearer {self._token}"}

    def _norm_to_px(self, nx: int, ny: int) -> tuple[float, float]:
        width = int(self.ctx.device.device_width or 1080)
        height = int(self.ctx.device.device_height or 2400)
        px = round((max(0, min(1000, int(nx))) / 1000.0) * (width - 1), 2)
        py = round((max(0, min(1000, int(ny))) / 1000.0) * (height - 1), 2)
        return px, py

    async def _request(
        self,
        method: str,
        path: str,
        json_body: dict[str, Any] | None = None,
        *,
        timeout_s: float | None = None,
    ) -> httpx.Response:
        url = f"{self.robot_api_url}{path}"
        effective_timeout = timeout_s if timeout_s is not None else self.timeout_s
        headers = self._headers()
        if self._http_client is not None:
            return await self._http_client.request(
                method,
                url,
                json=json_body,
                headers=headers,
                timeout=effective_timeout,
            )
        async with httpx.AsyncClient(timeout=effective_timeout) as client:
            return await client.request(
                method,
                url,
                json=json_body,
                headers=headers,
            )

    async def setup_device(
        self,
        device_id: str | None = None,
        width: int | None = None,
        height: int | None = None,
    ) -> ActionResult:
        """Registers the connected device on the Robot API (/calibration/device).

        The Robot API checks if a calibration file already exists for ``device_id``
        (``calibrations/<SERIAL_NUMBER>.json``) or executes RTA to generate one.
        """
        serial = str(device_id or self.ctx.device.device_id or "default")
        screen_w = int(width or self.ctx.device.device_width or 1080)
        screen_h = int(height or self.ctx.device.device_height or 2400)

        payload: dict[str, Any] = {
            "serial_number": serial,
            "screen_width": screen_w,
            "screen_height": screen_h,
        }
        if self.robot_ip:
            payload["robot_ip"] = self.robot_ip

        try:
            resp = await self._request(
                "POST",
                "/calibration/device",
                json_body=payload,
                timeout_s=self.setup_timeout_s,
            )
            if resp.status_code != 200:
                detail = resp.text
                return ActionResult.failure(
                    action="setup_device",
                    message=f"Robot API /calibration/device failed ({resp.status_code}): {detail}",
                    detail=detail,
                )
            data = resp.json()
            self._token = data.get("token")
            self._configured_device_id = serial
            source = data.get("calibration_source", "unknown")
            return ActionResult.success(
                action="setup_device",
                message=f"Device '{serial}' ready on Robot API (calibration_source={source}).",
            )
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            return ActionResult.failure(
                action="setup_device",
                message=f"Failed to configure device '{serial}' on Robot API: {exc}",
                detail=str(exc),
            )

    async def _ensure_ready(self) -> ActionResult | None:
        current_serial = str(self.ctx.device.device_id or "default")
        if self._token and self._configured_device_id == current_serial:
            return None
        res = await self.setup_device(
            device_id=current_serial,
            width=self.ctx.device.device_width,
            height=self.ctx.device.device_height,
        )
        if res.ok:
            return None
        return res

    async def click(
        self,
        nx: int,
        ny: int,
        times: int = 1,
        delay_ms: int = 100,
    ) -> ActionResult:
        if err := await self._ensure_ready():
            return err

        px, py = self._norm_to_px(nx, ny)
        for i in range(max(1, times)):
            resp = await self._request(
                "POST",
                "/calibration/tap",
                json_body={"px": px, "py": py, "hold_time_ms": 50},
            )
            if resp.status_code == 401:
                self._token = None
                if err := await self._ensure_ready():
                    return err
                resp = await self._request(
                    "POST",
                    "/calibration/tap",
                    json_body={"px": px, "py": py, "hold_time_ms": 50},
                )
            if resp.status_code != 200:
                return ActionResult.failure(
                    action="click",
                    message=f"Robot tap failed at ({px}, {py}): {resp.text}",
                    detail=resp.text,
                )
            if i < times - 1 and delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)

        if self.mirror_touch_to_adb:
            await super().click(nx, ny, times=times, delay_ms=delay_ms)

        res = ActionResult.success(
            action="click",
            message=f"Robot tapped at pixel ({px}, {py}) [norm=({nx}, {ny})] x{times}.",
        )
        object.__setattr__(res, "normalized_coordinates", [int(nx), int(ny)])
        return res

    async def click_sequence(
        self,
        points: Sequence[tuple[int, int]],
        delay_ms: int = 50,
    ) -> ActionResult:
        if err := await self._ensure_ready():
            return err

        for idx, (nx, ny) in enumerate(points):
            px, py = self._norm_to_px(nx, ny)
            resp = await self._request(
                "POST",
                "/calibration/tap",
                json_body={"px": px, "py": py, "hold_time_ms": 50},
            )
            if resp.status_code != 200:
                return ActionResult.failure(
                    action="click_sequence",
                    message=f"Robot tap_sequence failed at point {idx} ({px}, {py}): {resp.text}",
                    detail=resp.text,
                )
            if idx < len(points) - 1 and delay_ms > 0:
                await asyncio.sleep(delay_ms / 1000.0)

        if self.mirror_touch_to_adb:
            await super().click_sequence(points, delay_ms=delay_ms)

        return ActionResult.success(
            action="click_sequence",
            message=f"Robot tapped sequence of {len(points)} points.",
        )

    async def long_press(
        self,
        nx: int,
        ny: int,
        duration_ms: int = 1000,
    ) -> ActionResult:
        if err := await self._ensure_ready():
            return err

        px, py = self._norm_to_px(nx, ny)
        resp = await self._request(
            "POST",
            "/calibration/tap",
            json_body={"px": px, "py": py, "hold_time_ms": max(1, int(duration_ms))},
        )
        if resp.status_code != 200:
            return ActionResult.failure(
                action="long_press",
                message=f"Robot long_press failed at ({px}, {py}): {resp.text}",
                detail=resp.text,
            )

        if self.mirror_touch_to_adb:
            await super().long_press(nx, ny, duration_ms=duration_ms)

        res = ActionResult.success(
            action="long_press",
            message=f"Robot long-pressed at ({px}, {py}) for {duration_ms}ms.",
        )
        object.__setattr__(res, "normalized_coordinates", [int(nx), int(ny)])
        object.__setattr__(res, "duration_ms", duration_ms)
        return res

    async def swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int = 400,
    ) -> ActionResult:
        if err := await self._ensure_ready():
            return err

        from_px, from_py = self._norm_to_px(start[0], start[1])
        to_px, to_py = self._norm_to_px(end[0], end[1])
        resp = await self._request(
            "POST",
            "/calibration/swipe",
            json_body={
                "from_px": from_px,
                "from_py": from_py,
                "to_px": to_px,
                "to_py": to_py,
            },
        )
        if resp.status_code != 200:
            return ActionResult.failure(
                action="swipe",
                message=f"Robot swipe failed from ({from_px}, {from_py}) to ({to_px}, {to_py}): {resp.text}",
                detail=resp.text,
            )

        if self.mirror_touch_to_adb:
            await super().swipe(start=start, end=end, duration_ms=duration_ms)

        res = ActionResult.success(
            action="swipe",
            message=f"Robot swiped from ({from_px}, {from_py}) to ({to_px}, {to_py}).",
        )
        object.__setattr__(
            res,
            "normalized_coordinates",
            [int(start[0]), int(start[1]), int(end[0]), int(end[1])],
        )
        object.__setattr__(res, "duration_ms", duration_ms)
        return res

    async def focus_and_clear_text(self, nx: int, ny: int) -> ActionResult:
        tap_res = await self.click(nx, ny, times=1)
        if not tap_res.ok:
            return ActionResult.failure(
                action="focus_and_clear_text",
                message=tap_res.message,
                detail=tap_res.detail,
            )
        return await super().focus_and_clear_text(nx, ny)
