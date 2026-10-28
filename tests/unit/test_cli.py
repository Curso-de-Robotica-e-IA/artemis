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

"""Unit tests for ARTEMIS Unified CLI application."""

from typer.testing import CliRunner

from artemis.interfaces.cli.main import app

runner = CliRunner()


def test_cli_help():
    """Verify top-level CLI help returns status 0 and lists core subcommands."""
    result = runner.invoke(app, ["--help"])
    assert result.exit_code == 0
    assert "Artemis: Autonomous Multimodal Android Agent" in result.output
    assert "run" in result.output
    assert "batch" in result.output
    assert "doctor" in result.output
    assert "trace" in result.output


def test_cli_version():
    """Verify --version returns version banner."""
    result = runner.invoke(app, ["--version"])
    assert result.exit_code == 0
    assert "Artemis Agent Platform" in result.output


def test_cli_run_help():
    """Verify 'artemis run --help' displays execution options."""
    result = runner.invoke(app, ["run", "--help"])
    assert result.exit_code == 0
    assert "--profile" in result.output
    assert "--locked-app" in result.output
    assert "--traces-path" in result.output
    assert "--verification-level" in result.output
    assert "--explorer-pro-mode" in result.output


def test_cli_batch_help():
    """Verify 'artemis batch --help' displays batch options."""
    result = runner.invoke(app, ["batch", "--help"])
    assert result.exit_code == 0
    assert "--file" in result.output
    assert "--delay" in result.output
    assert "--verification-level" in result.output
    assert "--explorer-pro-mode" in result.output


def test_cli_batch_forwards_pro_tuning_in_standalone_mode(monkeypatch):
    """`artemis batch` threads both knobs into run_batch_tasks."""
    import artemis.interfaces.cli.commands.batch as batch_module

    captured: dict = {}

    async def fake_run_batch_tasks(tasks, **kwargs):
        captured["tasks"] = tasks
        captured.update(kwargs)

    monkeypatch.setattr(batch_module, "run_batch_tasks", fake_run_batch_tasks)
    result = runner.invoke(
        app,
        [
            "batch",
            "--profile",
            "pro",
            "--verification-level",
            "strict",
            "--explorer-pro-mode",
            "ultra",
            "Open Settings",
        ],
    )
    assert result.exit_code == 0, result.output
    assert captured["tasks"] == ["Open Settings"]
    assert captured["profile_name"] == "pro"
    assert captured["verification_level"] == "strict"
    assert captured["explorer_pro_mode"] == "ultra"


def test_run_batch_tasks_applies_pro_tuning_to_agent_config(monkeypatch):
    """The standalone batch runner applies the knobs on the AgentConfig builder."""
    import asyncio
    from unittest.mock import AsyncMock, MagicMock

    import artemis.interfaces.cli.commands.batch as batch_module

    fake_builder = MagicMock()
    fake_builders = MagicMock()
    fake_builders.AgentConfig.with_default_profile.return_value = fake_builder
    fake_agent = MagicMock()
    fake_agent.init = AsyncMock()
    fake_agent.run_task = AsyncMock(return_value="ok")
    fake_agent.clean = AsyncMock()

    monkeypatch.setattr(batch_module, "initialize_llm_config", lambda: MagicMock())
    monkeypatch.setattr(batch_module, "AgentProfile", MagicMock())
    monkeypatch.setattr(batch_module, "Builders", fake_builders)
    monkeypatch.setattr(batch_module, "Agent", MagicMock(return_value=fake_agent))

    asyncio.run(
        batch_module.run_batch_tasks(
            ["Goal A"],
            profile_name="pro",
            delay_seconds=0,
            verification_level="strict",
            explorer_pro_mode="ultra",
        )
    )
    fake_builder.with_verification_level.assert_called_once_with("strict")
    fake_builder.with_explorer.assert_called_once_with(pro_mode="ultra")
    fake_agent.run_task.assert_awaited_once_with(goal="Goal A", profile="pro")


def test_cli_trace_help():
    """Verify 'artemis trace --help' lists trace subcommands."""
    result = runner.invoke(app, ["trace", "--help"])
    assert result.exit_code == 0
    assert "list" in result.output
    assert "view" in result.output


# ---------------------------------------------------------------------------
# artemis doctor (readiness-engine backed)
# ---------------------------------------------------------------------------

_RAW_SECRET = "sk-RAWSECRET0123456789abcdef"


def _probe(
    probe_id: str,
    *,
    status="pass",
    blocker: bool = True,
    summary: str = "OK",
    description: str = "fine",
    actions=(),
    metadata=None,
):
    from artemis.core.diagnostics.schema import (
        ProbeAction,
        ProbeCategory,
        ProbeResult,
        ProbeStatus,
    )

    return ProbeResult(
        id=probe_id,
        category=ProbeCategory.RUNTIME,
        title=f"Title {probe_id}",
        status=ProbeStatus(status),
        is_blocker=blocker,
        summary=summary,
        description=description,
        metadata=metadata or {},
        actions=[ProbeAction(action_type=t, label=lbl, payload=p) for t, lbl, p in actions],
    )


def _all_pass_probes():
    return [
        _probe("vision_ocr_key", blocker=False, summary="Not Configured (Optional)"),
        _probe("toolchain", blocker=False),
        _probe("android_adb", metadata={"installed": True, "adb_keys": {"is_corrupted": False}}),
        _probe(
            "gemini_api_key",
            summary="Active (Gemini)",
            description="Gemini credential is active (sk-RAW...cdef).",
            metadata={
                "current_key": _RAW_SECRET,
                "api_keys": {"google": _RAW_SECRET},
                "providers": [{"raw_key": _RAW_SECRET, "key": _RAW_SECRET}],
            },
        ),
        _probe("system_config"),
        _probe("python_runtime"),
    ]


def _install_doctor_fakes(monkeypatch, probes, host=None, *, heal_result=None):
    """Patch the engine, the host probe, and the CLI-only extra rows."""
    import time
    from unittest.mock import AsyncMock

    from artemis.core.diagnostics.engine import readiness_engine
    from artemis.core.diagnostics.probes.host_probe import IntegrationHostProbe
    from artemis.core.diagnostics.schema import ProbeStatus, SystemReadinessReport

    monkeypatch.setenv("COLUMNS", "200")
    blockers = [p for p in probes if p.is_blocker]
    report = SystemReadinessReport(
        overall_ready=all(p.status is ProbeStatus.PASS for p in blockers),
        blocker_count=len(blockers),
        passed_blocker_count=sum(p.status is ProbeStatus.PASS for p in blockers),
        probes=list(probes),
        os_type="windows",
        timestamp=time.time(),
    )
    run_all = AsyncMock(return_value=report)
    monkeypatch.setattr(readiness_engine, "run_all", run_all)
    heal = AsyncMock(return_value=heal_result or {"success": True, "message": "Keys regenerated."})
    monkeypatch.setattr(readiness_engine, "heal_adb_keys", heal)
    host_probe = AsyncMock(
        return_value=host or _probe("integration_host", summary="Host Ready", description="host ok")
    )
    monkeypatch.setattr(IntegrationHostProbe, "probe", host_probe)
    return {"run_all": run_all, "heal": heal, "host": host_probe}


def test_cli_doctor_all_pass_renders_table_in_fix_order(monkeypatch):
    """All-green run: engine rows in fix order, extras last, exit 0, ready footer."""
    _install_doctor_fakes(monkeypatch, _all_pass_probes())

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    out = result.output
    assert "Artemis System & Environment Doctor" in out
    assert "Details & Recommendations" in out
    assert out.count("✔ OK") == 7
    assert "All system checks passed" in out
    assert 'artemis run "Open Settings and check Battery level"' in out

    order = [
        "Title python_runtime",
        "Title system_config",
        "Title integration_host",
        "Title gemini_api_key",
        "Title android_adb",
        "Title toolchain",
        "Title vision_ocr_key",
    ]
    positions = [out.index(name) for name in order]
    assert positions == sorted(positions)


def test_cli_doctor_blocker_failure_shows_run_lines_and_splits_chains(monkeypatch):
    """A failing blocker prints each action on its own line; && chains are split."""
    probes = [p for p in _all_pass_probes() if p.id != "android_adb"]
    probes.append(
        _probe(
            "android_adb",
            status="fail",
            summary="ADB Missing",
            description="adb was not found.",
            actions=[
                ("command", "Install", "winget install Google.PlatformTools && adb start-server"),
                ("hint", "Enable USB", "Enable USB debugging on the phone."),
                ("link", "Docs", "https://developer.android.com/tools/adb"),
            ],
            metadata={"installed": False},
        )
    )
    _install_doctor_fakes(monkeypatch, probes)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    out = result.output
    assert "✖ ADB Missing" in out
    assert "Run: winget install Google.PlatformTools" in out
    assert "Run: adb start-server" in out
    assert "&&" not in out
    assert "Enable USB debugging on the phone." in out
    assert "https://developer.android.com/tools/adb" in out
    assert "Action Required" in out
    assert "artemis init" in out
    assert "mobile_diagnose" in out


def test_cli_doctor_optional_failure_is_degraded_not_blocked(monkeypatch):
    """Non-blocker FAIL renders as Optional, keeps exit 0, and yields 'degraded'."""
    import json

    probes = [p for p in _all_pass_probes() if p.id != "toolchain"]
    probes.append(
        _probe(
            "toolchain",
            status="fail",
            blocker=False,
            summary="Missing FFmpeg",
            actions=[("command", "Install", "winget install Gyan.FFmpeg")],
        )
    )
    _install_doctor_fakes(monkeypatch, probes)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "⚪ Optional" in result.output
    assert "Run: winget install Gyan.FFmpeg" in result.output
    assert "optional gaps" in result.output

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    assert json.loads(result.output)["verdict"] == "degraded"


def test_cli_doctor_never_prints_raw_keys_from_metadata(monkeypatch):
    """Probe metadata (which carries raw credentials) must not reach the terminal or JSON."""
    _install_doctor_fakes(monkeypatch, _all_pass_probes())

    table = runner.invoke(app, ["doctor"])
    assert table.exit_code == 0, table.output
    assert "RAWSECRET" not in table.output
    assert "sk-RAW...cdef" in table.output  # the masked description is still shown

    as_json = runner.invoke(app, ["doctor", "--json"])
    assert as_json.exit_code == 0, as_json.output
    assert "RAWSECRET" not in as_json.output
    assert "metadata" not in as_json.output


def test_cli_doctor_json_shape_and_ready_verdict(monkeypatch):
    """--json emits the documented document shape and nothing else."""
    import json

    _install_doctor_fakes(monkeypatch, _all_pass_probes())

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 0, result.output
    doc = json.loads(result.output)
    assert set(doc) == {"verdict", "checks", "extras", "fixes"}
    assert doc["verdict"] == "ready"
    assert doc["fixes"] == []
    assert [c["id"] for c in doc["checks"]] == [
        "python_runtime",
        "system_config",
        "integration_host",
        "gemini_api_key",
        "android_adb",
        "toolchain",
        "vision_ocr_key",
    ]
    for check in doc["checks"]:
        assert set(check) == {"id", "title", "status", "required", "summary", "detail", "fix"}
        assert check["status"] == "pass"
        assert check["fix"] == []
    assert doc["extras"] == {}


def test_cli_doctor_json_blocked_verdict_and_exit_code(monkeypatch):
    """A blocker that is not PASS makes the verdict 'blocked' and the exit code 1."""
    import json

    probes = [p for p in _all_pass_probes() if p.id != "gemini_api_key"]
    probes.append(
        _probe(
            "gemini_api_key",
            status="fail",
            summary="Key Missing",
            description="No LLM credential found.",
            actions=[
                ("command", "Run Artemis Init", "artemis init"),
                ("link", "Get Key", "https://aistudio.google.com/app/apikey"),
            ],
        )
    )
    _install_doctor_fakes(monkeypatch, probes)

    result = runner.invoke(app, ["doctor", "--json"])
    assert result.exit_code == 1, result.output
    doc = json.loads(result.output)
    assert doc["verdict"] == "blocked"
    key_check = next(c for c in doc["checks"] if c["id"] == "gemini_api_key")
    assert key_check["status"] == "fail"
    assert key_check["required"] is True
    assert key_check["fix"] == [
        {"type": "command", "label": "Run Artemis Init", "payload": "artemis init"},
        {"type": "link", "label": "Get Key", "payload": "https://aistudio.google.com/app/apikey"},
    ]


def test_cli_doctor_host_warn_degrades_and_keeps_the_fix(monkeypatch):
    """A host WARN is not a blocker (the runner works around it): the doctor
    exits 0 with a degraded verdict and still prints the fix."""
    host = _probe(
        "integration_host",
        status="warn",
        blocker=False,
        summary="Host Warning",
        description="MCP server runs on the wrong interpreter.",
        actions=[("command", "Regenerate", "uv run artemis mcp --install cursor")],
    )
    _install_doctor_fakes(monkeypatch, _all_pass_probes(), host=host)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 0, result.output
    assert "⚠ Host Warning" in result.output
    assert "Run: uv run artemis mcp --install cursor" in result.output


def test_cli_doctor_host_fail_blocks_and_uses_summary(monkeypatch):
    host = _probe(
        "integration_host",
        status="fail",
        summary="Host Misconfigured",
        description="traces directory is not writable.",
    )
    _install_doctor_fakes(monkeypatch, _all_pass_probes(), host=host)

    result = runner.invoke(app, ["doctor"])
    assert result.exit_code == 1, result.output
    assert "✖ Host Misconfigured" in result.output


def test_cli_doctor_fix_heals_corrupted_keys_and_sweeps_locks(monkeypatch):
    """--fix heals ADB keys when the probe reports corruption and sweeps stale locks."""
    from artemis.runtime.device_lock import DeviceExecutionLock

    probes = [p for p in _all_pass_probes() if p.id != "android_adb"]
    probes.append(
        _probe(
            "android_adb",
            status="fail",
            summary="Keys Corrupted",
            metadata={"installed": True, "adb_keys": {"is_corrupted": True}},
        )
    )
    fakes = _install_doctor_fakes(monkeypatch, probes)
    cleanup_calls: list = []
    monkeypatch.setattr(
        DeviceExecutionLock,
        "cleanup_stale_locks",
        classmethod(lambda cls, device_id=None: cleanup_calls.append(device_id) or 3),
    )

    result = runner.invoke(app, ["doctor", "--fix"])
    fakes["heal"].assert_awaited_once()
    assert cleanup_calls == [None]
    assert fakes["run_all"].await_count == 2  # report is re-collected after the repairs
    assert "Repairs (--fix)" in result.output
    assert "Keys regenerated." in result.output
    assert "Removed 3 stale device lock(s)" in result.output


def test_cli_doctor_fix_skips_heal_when_keys_are_healthy(monkeypatch):
    """--fix does not touch healthy ADB keys but still sweeps stale locks."""
    from artemis.runtime.device_lock import DeviceExecutionLock

    fakes = _install_doctor_fakes(monkeypatch, _all_pass_probes())
    monkeypatch.setattr(
        DeviceExecutionLock, "cleanup_stale_locks", classmethod(lambda cls, device_id=None: 0)
    )

    result = runner.invoke(app, ["doctor", "--fix"])
    assert result.exit_code == 0, result.output
    fakes["heal"].assert_not_awaited()
    assert "nothing to repair" in result.output
    assert "No stale device locks" in result.output
