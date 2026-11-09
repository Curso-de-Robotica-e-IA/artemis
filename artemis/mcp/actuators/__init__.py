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

"""Actuator backends: pluggable implementations of the device action contract."""

from artemis.config.settings import settings
from artemis.context import ArtemisContext
from artemis.controllers.unified_controller import UnifiedMobileController
from artemis.mcp.actuators.base import Actuator
from artemis.mcp.actuators.adb import AdbActuator
from artemis.mcp.actuators.http_touch import HttpTouchActuator
from artemis.mcp.actuators.mock import MockActuator


def create_actuator(
    ctx: ArtemisContext,
    controller: UnifiedMobileController | None = None,
) -> Actuator:
    """Creates the configured Actuator backend (ADB or HTTP Touch API)."""
    backend = getattr(settings, "ARTEMIS_ACTUATOR_BACKEND", "adb").lower()
    if backend == "http":
        return HttpTouchActuator(ctx, controller=controller)
    return AdbActuator(ctx, controller=controller)


__all__ = [
    "Actuator",
    "AdbActuator",
    "HttpTouchActuator",
    "MockActuator",
    "create_actuator",
]
