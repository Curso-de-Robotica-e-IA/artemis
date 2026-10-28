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

"""☕ ARTEMIS: Autonomous Multimodal Android Agent & Testing Framework."""

from artemis._version import __version__
from artemis import _legacy_imports

# Old import paths of the modules moved to third_party/ (deprecated).
_legacy_imports.install()

from artemis.drivers.base import BaseDeviceDriver
from artemis.interfaces.sdk.task import StreamEvent, StreamEventType, Task, TaskResult
from artemis.runner import ArtemisRunner
from artemis.runtime.device_lock import ConcurrencyMode
from artemis.sdk.agent import Agent
from artemis.sdk.builders import Builders

__all__ = [
    "__version__",
    "Agent",
    "ArtemisRunner",
    "BaseDeviceDriver",
    "Builders",
    "ConcurrencyMode",
    "StreamEvent",
    "StreamEventType",
    "Task",
    "TaskResult",
]
