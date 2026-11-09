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

"""HTTP-backed Touch and Input Actuator for ARTEMIS."""

from collections.abc import Sequence
import json
from typing import Any

import httpx

from artemis.config.settings import settings
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.action_types import ActionResult
from artemis.mcp.actuators.adb import AdbActuator


class HttpTouchActuator(AdbActuator):
    """Actuator that routes touch, text, and key actions through the HTTP Touch API.

    Screen observation (screenshots, UI hierarchy) and app management remain
    handled directly via ADB through :class:`AdbActuator`.
    """

    def __init__(
        self,
        ctx: ArtemisContext,
        controller: UnifiedMobileController | None = None,
        base_url: str | None = None,
        timeout_s: float | None = None,
        client: httpx.AsyncClient | None = None,
    ) -> None:
        super().__init__(ctx, controller=controller)
        if client is not None and str(client.base_url).rstrip("/"):
            default_base_url = str(client.base_url).rstrip("/")
        else:
            default_base_url = getattr(settings, "ARTEMIS_TOUCH_API_URL", "http://127.0.0.1:8001")
        self.base_url = (base_url or default_base_url).rstrip("/")
        self.timeout_s = timeout_s or float(
            getattr(settings, "ARTEMIS_TOUCH_API_TIMEOUT_S", 30.0)
        )
        self._external_client = client
        self._synced_device_id: str | None = None

    async def _post(
        self, path: str, payload: dict[str, Any], action: str | None = None
    ) -> ActionResult:
        action_name = action or path.lstrip("/").replace("-", "_")
        url = f"{self.base_url}{path}"
        try:
            if self._external_client is not None:
                response = await self._external_client.post(
                    url, json=payload, timeout=self.timeout_s
                )
            else:
                async with httpx.AsyncClient(timeout=self.timeout_s) as client:
                    response = await client.post(url, json=payload)

            data = response.json()
            ok = bool(data.get("ok", response.is_success))
            message = str(data.get("message", ""))
            error = data.get("error")
            if ok:
                return ActionResult.success(
                    action=action_name,
                    message=message or f"Action {action_name} succeeded.",
                    detail=error,
                )
            return ActionResult.failure(
                action=action_name,
                message=message or (error or f"HTTP {response.status_code}"),
                detail=error,
            )
        except (httpx.HTTPError, ValueError, OSError, RuntimeError, json.JSONDecodeError) as exc:
            return ActionResult.failure(
                action=action_name,
                message=f"HTTP Touch API request to {path} failed: {exc}",
                detail=str(exc),
            )

    async def _ensure_device_configured(self) -> ActionResult | None:
        device_id = self.ctx.device.device_id if (self.ctx and self.ctx.device) else None
        if not device_id or device_id == "default":
            return None
        if self._synced_device_id == device_id:
            return None

        res = await self._post("/device", {"device_id": device_id}, action="configure_device")
        if res.ok:
            self._synced_device_id = device_id
            return None
        return res

    async def configure_device(self, device_id: str) -> ActionResult:
        """Explicitly configures the target device on the Touch API server."""
        res = await self._post("/device", {"device_id": device_id}, action="configure_device")
        if res.ok:
            self._synced_device_id = device_id
            if self.ctx and self.ctx.device:
                self.ctx.device.device_id = device_id
        return res

    async def click(
        self,
        nx: int,
        ny: int,
        times: int = 1,
        delay_ms: int = 100,
    ) -> ActionResult:
        if err := await self._ensure_device_configured():
            return err
        res = await self._post(
            "/tap",
            {"nx": nx, "ny": ny, "times": times, "delay_ms": delay_ms},
            action="click",
        )
        if res.ok:
            object.__setattr__(res, "normalized_coordinates", [int(nx), int(ny)])
        return res

    async def click_sequence(
        self,
        points: Sequence[tuple[int, int]],
        delay_ms: int = 50,
    ) -> ActionResult:
        if err := await self._ensure_device_configured():
            return err
        serialized_points = [{"nx": px, "ny": py} for px, py in points]
        return await self._post(
            "/tap-sequence",
            {"points": serialized_points, "delay_ms": delay_ms},
            action="click_sequence",
        )

    async def long_press(
        self,
        nx: int,
        ny: int,
        duration_ms: int = 1000,
    ) -> ActionResult:
        if err := await self._ensure_device_configured():
            return err
        res = await self._post(
            "/long-press",
            {"nx": nx, "ny": ny, "duration_ms": duration_ms},
            action="long_press",
        )
        if res.ok:
            object.__setattr__(res, "normalized_coordinates", [int(nx), int(ny)])
            object.__setattr__(res, "duration_ms", duration_ms)
        return res

    async def swipe(
        self,
        start: tuple[int, int],
        end: tuple[int, int],
        duration_ms: int = 400,
    ) -> ActionResult:
        if err := await self._ensure_device_configured():
            return err
        res = await self._post(
            "/swipe",
            {
                "start_nx": start[0],
                "start_ny": start[1],
                "end_nx": end[0],
                "end_ny": end[1],
                "duration_ms": duration_ms,
            },
            action="swipe",
        )
        if res.ok:
            object.__setattr__(
                res,
                "normalized_coordinates",
                [int(start[0]), int(start[1]), int(end[0]), int(end[1])],
            )
            object.__setattr__(res, "duration_ms", duration_ms)
        return res

    async def input_text(
        self,
        text: str,
        target: tuple[int, int] | None = None,
        clear_exist: bool = True,
    ) -> ActionResult:
        if err := await self._ensure_device_configured():
            return err
        coords = None
        if target:
            nx, ny = target
            coords = [int(nx), int(ny)]
            if clear_exist:
                focus_res = await self.focus_and_clear_text(nx, ny)
            else:
                focus_res = await self.click(nx, ny)
            if not focus_res.ok:
                return focus_res
        res = await self._post(
            "/input-text",
            {"text": text},
            action="input_text",
        )
        if res.ok and coords:
            object.__setattr__(res, "normalized_coordinates", coords)
        return res

    async def focus_and_clear_text(self, nx: int, ny: int) -> ActionResult:
        if err := await self._ensure_device_configured():
            return err
        res = await self._post(
            "/focus-and-clear",
            {"nx": nx, "ny": ny},
            action="focus_and_clear_text",
        )
        if res.ok:
            object.__setattr__(res, "normalized_coordinates", [int(nx), int(ny)])
        return res

    async def erase_one_char(self) -> ActionResult:
        if err := await self._ensure_device_configured():
            return err
        return await self._post("/erase-char", {"times": 1}, action="erase_one_char")

    async def press_key(self, key: str) -> ActionResult:
        if err := await self._ensure_device_configured():
            return err
        return await self._post("/press-key", {"key": key}, action="press_key")
