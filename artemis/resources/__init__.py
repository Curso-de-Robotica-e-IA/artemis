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

"""Access to immutable defaults and resources shipped with Artemis."""

from importlib.resources import files
from pathlib import Path


def _resource_path(*parts: str) -> Path:
    """Return a filesystem path inside the installed resource package."""
    resource = files(__name__).joinpath(*parts)
    return Path(str(resource))


def get_bundled_config_path(filename: str) -> Path | None:
    """Return a bundled configuration template when it exists."""
    candidate = _resource_path("config", filename)
    return candidate if candidate.is_file() else None


def get_bundled_showcase_dist() -> Path | None:
    """Legacy helper maintained for backward compatibility (returns None)."""
    return None


__all__ = ["get_bundled_config_path", "get_bundled_showcase_dist"]
