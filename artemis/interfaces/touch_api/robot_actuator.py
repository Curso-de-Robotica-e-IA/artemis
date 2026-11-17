import asyncio
from collections.abc import Sequence
import os
from typing import Any

import httpx

from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.interfaces.touch_api.keyboard_layout import (
    key_to_pixels,
    text_to_key_sequence,
    wait_for_keyboard_visible,
)
from artemis.mcp.action_types import ActionResult
from artemis.mcp.actuators.adb import AdbActuator, find_element_at_coords
from third_party.mobile_use.utils.logger import get_logger

logger = get_logger(__name__)

DEFAULT_ARM_SPEED: tuple[float, float, float] = (20.0, 4.0, 4.0)
KEYBOARD_ARM_SPEED: tuple[float, float, float] = (50.0, 25.0, 25.0)


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
        self.key_delay_ms = int(os.environ.get("ROBOT_KEY_DELAY_MS", "50"))
        self._http_client = http_client
        self._token: str | None = None
        self._configured_device_id: str | None = None
        self._current_speed: tuple[float, float, float] | None = None

    async def _ensure_speed(
        self,
        speed: float,
        accel: float,
        decel: float,
    ) -> None:
        """Sets the robot arm speed via POST /speed only when it changes."""
        target = (float(speed), float(accel), float(decel))
        if self._current_speed == target:
            return
        try:
            resp = await self._request(
                "POST",
                "/speed",
                json_body={"speed": target[0], "accel": target[1], "decel": target[2]},
            )
            if resp.status_code == 401:
                self._token = None
                if await self._ensure_ready() is None:
                    resp = await self._request(
                        "POST",
                        "/speed",
                        json_body={
                            "speed": target[0],
                            "accel": target[1],
                            "decel": target[2],
                        },
                    )
            if resp.status_code == 200:
                self._current_speed = target
            else:
                logger.warning(
                    f"POST /speed ({target}) returned {resp.status_code}: {resp.text}"
                )
        except (httpx.HTTPError, OSError, RuntimeError, ValueError) as exc:
            logger.warning(f"Failed to update robot speed to {target}: {exc}")

    async def _tap_robot_pixel(
        self,
        px: float,
        py: float,
        hold_time_ms: int = 50,
        action: str = "click",
    ) -> ActionResult | None:
        """Sends POST /calibration/tap with automatic 401 token refresh.

        Returns ``None`` on success, or a failure ``ActionResult`` on error.
        """
        resp = await self._request(
            "POST",
            "/calibration/tap",
            json_body={"px": px, "py": py, "hold_time_ms": hold_time_ms},
        )
        if resp.status_code == 401:
            self._token = None
            if err := await self._ensure_ready():
                return err
            resp = await self._request(
                "POST",
                "/calibration/tap",
                json_body={"px": px, "py": py, "hold_time_ms": hold_time_ms},
            )
        if resp.status_code != 200:
            return ActionResult.failure(
                action=action,
                message=f"Robot tap failed at ({px}, {py}): {resp.text}",
                detail=resp.text,
            )
        return None

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
            self._current_speed = None
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
        await self._ensure_speed(*DEFAULT_ARM_SPEED)

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
        await self._ensure_speed(*DEFAULT_ARM_SPEED)

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
        await self._ensure_speed(*DEFAULT_ARM_SPEED)

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
        await self._ensure_speed(*DEFAULT_ARM_SPEED)

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

    async def _tap_keyboard_key(
        self,
        key: str,
        action: str = "input_text",
    ) -> ActionResult | None:
        """Taps a named virtual keyboard key using the robot arm."""
        width = int(self.ctx.device.device_width or 1080)
        height = int(self.ctx.device.device_height or 2400)
        px, py = key_to_pixels(key, width, height)
        if err := await self._tap_robot_pixel(px, py, hold_time_ms=40, action=action):
            return err
        if self.mirror_touch_to_adb and hasattr(self.controller, "tap_at"):
            await self.controller.tap_at(int(round(px)), int(round(py)))
        return None

    async def focus_and_clear_text(self, nx: int, ny: int) -> ActionResult:
        tap_res = await self.click(nx, ny, times=1)
        if not tap_res.ok:
            return ActionResult.failure(
                action="focus_and_clear_text",
                message=tap_res.message,
                detail=tap_res.detail,
            )

        device_id = str(self.ctx.device.device_id or "default")
        await wait_for_keyboard_visible(device_id, timeout_s=1.0)

        existing_len = 0
        try:
            if hasattr(self.controller, "get_ui_elements"):
                elements = await self.controller.get_ui_elements()
                px, py = self._norm_to_px(nx, ny)
                elem = find_element_at_coords(elements, int(px), int(py))
                if elem:
                    txt = str(elem.get("text") or "")
                    if txt and elem.get("focused") == "true":
                        existing_len = min(len(txt), 40)
        except Exception as exc:
            logger.debug(f"Skipping existing-text check before clear: {exc}")

        if existing_len > 0:
            if err := await self._ensure_ready():
                return err
            await self._ensure_speed(*KEYBOARD_ARM_SPEED)
            try:
                for _ in range(existing_len):
                    if err := await self._tap_keyboard_key("backspace", action="focus_and_clear_text"):
                        return err
                    if self.key_delay_ms > 0:
                        await asyncio.sleep(self.key_delay_ms / 1000.0)
            finally:
                await self._ensure_speed(*DEFAULT_ARM_SPEED)

        res = ActionResult.success(
            action="focus_and_clear_text",
            message=f"Focused at [{nx}, {ny}] with robot arm.",
        )
        object.__setattr__(res, "normalized_coordinates", [int(nx), int(ny)])
        return res

    async def input_text(
        self,
        text: str,
        target: tuple[int, int] | None = None,
        clear_exist: bool = True,
    ) -> ActionResult:
        if err := await self._ensure_ready():
            return err

        coords = None
        if target:
            nx, ny = target
            coords = [int(nx), int(ny)]
            focus_res = (
                await self.focus_and_clear_text(nx, ny)
                if clear_exist
                else await self.click(nx, ny)
            )
            if not focus_res.ok:
                return focus_res

        device_id = str(self.ctx.device.device_id or "default")
        await wait_for_keyboard_visible(device_id, timeout_s=1.2)

        keys = text_to_key_sequence(text)
        if not keys:
            return ActionResult.failure(
                action="input_text",
                message=f"No typeable keyboard keys mapped for '{text}'.",
            )

        await self._ensure_speed(*KEYBOARD_ARM_SPEED)
        try:
            for idx, key in enumerate(keys):
                if err := await self._tap_keyboard_key(key, action="input_text"):
                    return err
                if idx < len(keys) - 1 and self.key_delay_ms > 0:
                    await asyncio.sleep(self.key_delay_ms / 1000.0)
        finally:
            await self._ensure_speed(*DEFAULT_ARM_SPEED)

        res = ActionResult.success(
            action="input_text",
            message=f"Robot typed '{text}' via {len(keys)} keyboard taps.",
        )
        if coords:
            object.__setattr__(res, "normalized_coordinates", coords)
        return res

    async def erase_one_char(self) -> ActionResult:
        if err := await self._ensure_ready():
            return err
        await self._ensure_speed(*KEYBOARD_ARM_SPEED)
        try:
            if err := await self._tap_keyboard_key("backspace", action="erase_one_char"):
                return err
        finally:
            await self._ensure_speed(*DEFAULT_ARM_SPEED)
        return ActionResult.success(
            action="erase_one_char",
            message="Robot tapped Backspace on virtual keyboard.",
        )

    async def press_key(self, key: str) -> ActionResult:
        normalized = str(key).strip().upper()
        keyboard_aliases = {
            "ENTER": "enter",
            "SEARCH": "enter",
            "66": "enter",
            "KEYCODE_ENTER": "enter",
            "DEL": "backspace",
            "BACKSPACE": "backspace",
            "67": "backspace",
            "KEYCODE_DEL": "backspace",
            "SPACE": "space",
            "62": "space",
            "KEYCODE_SPACE": "space",
        }
        mapped = keyboard_aliases.get(normalized)
        if mapped:
            if err := await self._ensure_ready():
                return err
            await self._ensure_speed(*KEYBOARD_ARM_SPEED)
            try:
                if err := await self._tap_keyboard_key(mapped, action="press_key"):
                    return err
            finally:
                await self._ensure_speed(*DEFAULT_ARM_SPEED)
            return ActionResult.success(
                action="press_key",
                message=f"Robot pressed '{mapped}' on virtual keyboard for key '{key}'.",
            )
        return await super().press_key(key)
