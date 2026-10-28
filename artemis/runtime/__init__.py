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

"""Artemis cross-platform runtime and process supervisor subsystem."""

from artemis.runtime.adb_endpoint import (
    ADB_ENDPOINT_ID_ENV,
    AdbEndpoint,
    AdbSession,
    AdbTarget,
    InvalidAdbEndpoint,
    current_adb_endpoint,
)
from artemis.runtime.awake_service import (
    ensure_device_awake,
    sanitize_device_state,
    screen_awake_service,
    shutdown_awake_service,
    start_awake_service,
)
from artemis.runtime.cancel_requests import (
    clear_cancel_request,
    is_cancel_requested,
    request_cancel,
    watch_for_cancel_request,
)
from artemis.runtime.device_lock import (
    ConcurrencyMode,
    DeviceBusyError,
    DeviceExecutionLock,
)
from artemis.runtime.device_pool import DevicePool, DeviceStatus, device_pool
from artemis.runtime.process_probe import pid_is_alive
from artemis.runtime.supervisor import ProcessSupervisor, process_supervisor

__all__ = [
    "ADB_ENDPOINT_ID_ENV",
    "AdbEndpoint",
    "AdbSession",
    "AdbTarget",
    "ConcurrencyMode",
    "DeviceBusyError",
    "DeviceExecutionLock",
    "DevicePool",
    "DeviceStatus",
    "InvalidAdbEndpoint",
    "ProcessSupervisor",
    "clear_cancel_request",
    "current_adb_endpoint",
    "device_pool",
    "ensure_device_awake",
    "is_cancel_requested",
    "pid_is_alive",
    "process_supervisor",
    "request_cancel",
    "sanitize_device_state",
    "screen_awake_service",
    "shutdown_awake_service",
    "start_awake_service",
    "watch_for_cancel_request",
]
