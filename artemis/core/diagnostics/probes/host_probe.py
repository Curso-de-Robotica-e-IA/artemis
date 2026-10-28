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

"""Integration-host probe: the process environment an IDE / MCP client launched us in.

The standard readiness probes answer "can Artemis drive a phone on this
machine?". This probe answers the question that precedes it when Artemis is
embedded in an AI coding assistant (Claude Code, Cursor, Codex, Antigravity,
...): "was the server launched the way the installer expects, and can it
spawn a background runner?". The failure modes it covers are the ones users
hit mostly through MCP:

* the MCP client started the server with a different interpreter than the
  project virtualenv, so the spawned runner imports a different package set;
* the ``.env`` file the settings loader reads is not where the user put the key;
* the traces directory is not writable from the client's sandbox.

The probe is not registered in the default :class:`ReadinessEngine` probe set.
The ``mobile_diagnose`` MCP tool and ``artemis doctor`` run it explicitly.

Only the unwritable-traces case blocks task execution; the result carries
``is_blocker`` accordingly, so the WARN findings degrade the verdict without
blocking it.
"""

from __future__ import annotations

import os
from pathlib import Path
import sys
import tempfile
from typing import Any

from artemis.core.diagnostics.probes.base import BaseProbe
from artemis.core.diagnostics.schema import (
    ProbeAction,
    ProbeCategory,
    ProbeResult,
    ProbeStatus,
)


def _same_file(a: str | os.PathLike[str], b: str | os.PathLike[str]) -> bool:
    """Compare two interpreter paths tolerantly (symlinks, case on Windows)."""
    try:
        return os.path.samefile(a, b)
    except OSError:
        return os.path.normcase(os.path.abspath(a)) == os.path.normcase(os.path.abspath(b))


#: Client names accepted by ``artemis mcp --install`` (see interfaces/cli/commands/mcp.py).
MCP_CLIENT_NAMES: tuple[str, ...] = (
    "antigravity",
    "claude",
    "cursor",
    "windsurf",
    "vscode",
    "cline",
    "roo",
    "openclaw",
    "codex",
)


def _env_has_prefix(prefix: str, *, exclude: tuple[str, ...] = ()) -> bool:
    return any(key.startswith(prefix) and key not in exclude for key in os.environ)


def detect_mcp_client() -> str | None:
    """Best-effort guess of which MCP client launched this process, from ``os.environ``.

    Returns one of :data:`MCP_CLIENT_NAMES` or ``None`` when unsure. MCP hosts
    spawn the server with their own process environment, so the variables the
    IDE exports to its child processes leak through. Specific hosts are checked
    before the generic VS Code markers because Cursor, Windsurf, Cline and Roo
    are (or run inside) VS Code and inherit ``VSCODE_*`` / ``TERM_PROGRAM``.
    """
    if _env_has_prefix("ANTIGRAVITY_"):
        return "antigravity"
    if os.environ.get("CLAUDECODE") == "1" or _env_has_prefix("CLAUDE_CODE_"):
        return "claude"
    if "CURSOR_TRACE_ID" in os.environ or _env_has_prefix("CURSOR_"):
        return "cursor"
    if _env_has_prefix("WINDSURF_"):
        return "windsurf"
    if _env_has_prefix("CODEX_", exclude=("CODEX_HOME",)):
        return "codex"
    if _env_has_prefix("OPENCLAW_", exclude=("OPENCLAW_WEBHOOK_URL",)):
        return "openclaw"
    if _env_has_prefix("CLINE_"):
        return "cline"
    if _env_has_prefix("ROO_"):
        return "roo"
    if (
        "VSCODE_PID" in os.environ
        or "VSCODE_IPC_HOOK" in os.environ
        or os.environ.get("TERM_PROGRAM") == "vscode"
    ):
        return "vscode"
    return None


def mcp_install_actions(client: str | None) -> list[ProbeAction]:
    """Actions that regenerate the MCP client config for ``client`` (or ask which one)."""
    if client:
        return [
            ProbeAction(
                action_type="command",
                label="Regenerate MCP Config",
                payload=f"uv run artemis mcp --install {client}",
            )
        ]
    return [
        ProbeAction(
            action_type="hint",
            label="Regenerate MCP Config",
            payload=(
                "Could not tell which MCP client launched this server. Ask the user which IDE"
                " they are using, then run `uv run artemis mcp --install <client>` with"
                " <client> being one of: " + ", ".join(MCP_CLIENT_NAMES) + "."
            ),
        )
    ]


def project_venv_python(project_root: Path) -> Path:
    """Return the interpreter path the installer writes into MCP client configs."""
    if sys.platform == "win32":
        return project_root / ".venv" / "Scripts" / "python.exe"
    return project_root / ".venv" / "bin" / "python"


def directory_is_writable(path: Path) -> tuple[bool, str | None]:
    """Try to create and delete a scratch file; return (ok, error)."""
    try:
        path.mkdir(parents=True, exist_ok=True)
        with tempfile.NamedTemporaryFile(dir=path, prefix=".artemis-diag-", delete=True):
            pass
        return True, None
    except OSError as exc:
        return False, str(exc)


def intended_traces_path() -> Path:
    """Return the traces directory the settings loader *would* pick, without creating it.

    Mirrors the selection order of :func:`artemis.config.paths.get_default_traces_path`
    (``ARTEMIS_TRACES_DIR`` env, else the user app dir when the install mode
    keeps mutable state there, else ``ROOT_DIR/traces``) minus its ``mkdir``.
    Used only when that function raised, so the probe can still name the
    offending directory in its FAIL result instead of crashing.
    """
    from artemis.config import paths as artemis_paths

    env_traces = os.getenv(artemis_paths.ENV_ARTEMIS_TRACES_DIR)
    if env_traces:
        return Path(env_traces)
    if artemis_paths._use_user_app_dir():
        return artemis_paths.get_app_dir() / "traces"
    return Path(artemis_paths.ROOT_DIR) / "traces"


def resolve_traces_dir() -> tuple[Path, str | None]:
    """Return ``(traces_dir, error)``; ``error`` is set when the directory could not be created.

    ``get_default_traces_path`` creates the directory as a side effect, so on a
    read-only parent it raises before :func:`directory_is_writable` ever runs.
    Surface that as data rather than letting the probe itself crash.
    """
    from artemis.config.paths import get_default_traces_path

    try:
        return Path(get_default_traces_path()), None
    except OSError as exc:
        return intended_traces_path(), str(exc)


class IntegrationHostProbe(BaseProbe):
    """Probe verifying the MCP / IDE host process can actually run Artemis tasks."""

    @property
    def probe_id(self) -> str:
        return "integration_host"

    @property
    def category(self) -> ProbeCategory:
        return ProbeCategory.RUNTIME

    @property
    def is_blocker(self) -> bool:
        return True

    async def probe(self) -> ProbeResult:
        from artemis.config.paths import ROOT_DIR, get_env_file, is_source_checkout

        project_root = Path(ROOT_DIR)
        source_checkout = is_source_checkout()
        server_python = Path(sys.executable)
        venv_python: Path | None = project_venv_python(project_root) if source_checkout else None
        venv_exists = venv_python is not None and venv_python.is_file()
        runner_python = venv_python if venv_exists else server_python
        interpreter_matches_venv = venv_exists and _same_file(server_python, venv_python)
        mcp_client = detect_mcp_client()

        env_file = Path(get_env_file())
        traces_dir, traces_error = resolve_traces_dir()
        if traces_error is None:
            traces_writable, traces_error = directory_is_writable(traces_dir)
        else:
            traces_writable = False

        metadata: dict[str, Any] = {
            "os": sys.platform,
            "mcp_client": mcp_client,
            "project_root": str(project_root),
            "is_source_checkout": source_checkout,
            "server_python": str(server_python),
            "venv_python": str(venv_python) if venv_python is not None else None,
            "venv_exists": venv_exists,
            "interpreter_matches_venv": interpreter_matches_venv,
            "runner_python": str(runner_python),
            "env_file": str(env_file),
            "env_file_exists": env_file.is_file(),
            "traces_dir": str(traces_dir),
            "traces_dir_writable": traces_writable,
            "traces_dir_error": traces_error,
        }

        problems: list[str] = []
        actions: list[ProbeAction] = []
        status = ProbeStatus.PASS

        if not traces_writable:
            status = ProbeStatus.FAIL
            problems.append(f"traces directory {traces_dir} is not writable ({traces_error})")
            actions.append(
                ProbeAction(
                    action_type="hint",
                    label="Fix Traces Directory",
                    payload=(
                        f"Make {traces_dir} writable for the user running the MCP server, or point "
                        "ARTEMIS_TRACES_DIR at a writable directory in the MCP server's env block, "
                        "then restart the MCP server."
                    ),
                )
            )

        if not source_checkout:
            # Wheel / frozen install: no virtualenv expectation, nothing to warn about.
            pass
        elif venv_exists and not interpreter_matches_venv:
            status = ProbeStatus.WARN if status is ProbeStatus.PASS else status
            problems.append(
                f"MCP server runs on {server_python}, not the project virtualenv {venv_python}"
            )
            actions.extend(mcp_install_actions(mcp_client))
            actions.append(
                ProbeAction(
                    action_type="hint",
                    label="Interpreter Mismatch",
                    payload=(
                        "The MCP client config should point `command` at the .venv interpreter so the"
                        " server and the background runner share one package set. Restart the MCP"
                        " server after changing the config."
                    ),
                )
            )
        elif not venv_exists:
            status = ProbeStatus.WARN if status is ProbeStatus.PASS else status
            problems.append(
                "no project virtualenv (.venv) found; runner falls back to the server interpreter"
            )
            actions.append(
                ProbeAction(
                    action_type="command",
                    label="Create Virtualenv",
                    payload="uv sync",
                )
            )

        if not env_file.is_file():
            actions.append(
                ProbeAction(
                    action_type="hint",
                    label="Env File Location",
                    payload=(
                        f"No .env file at {env_file}. API keys are read from that file or from the MCP"
                        " server's env block; the file is created by `artemis init`."
                    ),
                )
            )

        if status is ProbeStatus.PASS:
            summary = "Host Ready"
            description = (
                f"MCP server runs on {server_python}; runner uses {runner_python}. "
                f"Traces at {traces_dir}."
            )
            actions.insert(
                0,
                ProbeAction(
                    action_type="hint",
                    label="Host OK",
                    payload="Interpreter and traces directory look consistent.",
                ),
            )
        else:
            summary = "Host Misconfigured" if status is ProbeStatus.FAIL else "Host Warning"
            joined = "; ".join(problems)
            description = joined[:1].upper() + joined[1:] + "."

        return ProbeResult(
            id=self.probe_id,
            category=self.category,
            title="MCP / IDE Integration Host",
            status=status,
            is_blocker=status is not ProbeStatus.WARN,
            summary=summary,
            description=description,
            metadata=metadata,
            actions=actions,
        )
