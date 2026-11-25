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

"""Unit tests verifying per-session ``<execution_dir>/replay/`` storage and resolution."""

import json
from pathlib import Path
from uuid import UUID

from apps.admin_console.replay_manager import ReplayManager
from apps.admin_console.routers import steps as steps_router
from apps.admin_console.services import task_queue_service as tqs_mod
from apps.admin_console.services.media_service import MediaService, resolve_session_trace_dir
from artemis.data_engine.storage import StorageManager
from artemis.runtime import trace_store


def _seed_session_db(
    traces_root: Path,
    session_id: str,
    *,
    exec_dir: Path,
    with_steps: bool = True,
) -> StorageManager:
    """Create a hermetic Data Engine DB populated with a sample session."""
    storage = StorageManager(db_path=traces_root / "data_engine.db", base_trace_dir=traces_root)
    with storage._get_connection() as conn:
        conn.execute(
            """
            INSERT INTO sessions (session_id, initial_goal, start_time, end_time, status)
            VALUES (?, ?, ?, ?, ?)
            """,
            (session_id, "Verify replay storage layout", 1000.0, 1010.0, "PASS"),
        )
        if with_steps:
            images_dir = exec_dir / "images"
            images_dir.mkdir(parents=True, exist_ok=True)
            (images_dir / "img_pre.jpeg").write_bytes(b"\xff\xd8\xff\xe0pre")
            (images_dir / "img_post.jpeg").write_bytes(b"\xff\xd8\xff\xe0post")
            conn.execute(
                """
                INSERT INTO images (image_name, timestamp, ocr_result, ui_tree, extra_metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("img_pre.jpeg", 1001.0, "[]", "[]", "{}"),
            )
            conn.execute(
                """
                INSERT INTO images (image_name, timestamp, ocr_result, ui_tree, extra_metadata)
                VALUES (?, ?, ?, ?, ?)
                """,
                ("img_post.jpeg", 1002.0, "[]", "[]", "{}"),
            )
            conn.execute(
                """
                INSERT INTO steps (
                    step_id, session_id, step_number, timestamp,
                    pre_image_name, post_image_name, summary, action_taken
                )
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (
                    "step-uuid-01",
                    session_id,
                    1,
                    1002.0,
                    "img_pre.jpeg",
                    "img_post.jpeg",
                    "Tapped Settings",
                    json.dumps({"action": "click", "target_text": "Settings"}),
                ),
            )
        conn.commit()
    return storage


def test_resolve_session_trace_dir_prefers_finalized_web_folder(tmp_path: Path):
    session_id = "aaaaaaaa-bbbb-cccc-dddd-eeeeeeeeeeee"
    short_id = session_id[:8]

    # Create reserved subdirectories that must never be mistaken for session folders.
    (tmp_path / "replay").mkdir()
    (tmp_path / "images").mkdir()
    (tmp_path / "outputs").mkdir()

    # Transient full-UUID log dir plus active un-suffixed web dir.
    transient = tmp_path / session_id
    transient.mkdir()
    (transient / "stdout.log").write_text("boot", encoding="utf-8")

    active_web = tmp_path / f"web_1_{short_id}"
    (active_web / "images").mkdir(parents=True)
    assert resolve_session_trace_dir(tmp_path, session_id) == active_web

    # Rename to finalized terminal folder and verify dynamic resolution follows.
    finalized_web = tmp_path / f"web_1_{short_id}_PASS_20261113_080000"
    active_web.rename(finalized_web)
    assert resolve_session_trace_dir(tmp_path, session_id) == finalized_web


def test_chunk_session_traces_writes_inside_execution_replay_dir(tmp_path: Path):
    session_id = "12345678-2222-3333-4444-555555555555"
    exec_dir = tmp_path / f"web_7_{session_id[:8]}_PASS_20261113_081500"
    exec_dir.mkdir(parents=True)
    _seed_session_db(tmp_path, session_id, exec_dir=exec_dir, with_steps=True)

    manager = ReplayManager(workspace_root=tmp_path, traces_path=tmp_path)
    out_dir = manager.chunk_session_traces(session_id)

    expected_replay_dir = exec_dir / "replay"
    assert out_dir == expected_replay_dir
    assert (expected_replay_dir / ".chunked").is_file()
    assert (expected_replay_dir / "session.json").is_file()
    assert (expected_replay_dir / "step_01" / "step.json").is_file()
    assert (expected_replay_dir / "step_01" / "pre.jpg").is_file()
    assert (expected_replay_dir / "step_01" / "post.jpg").is_file()
    assert (expected_replay_dir / "step_01" / "pre_image_meta.json").is_file()
    assert not (tmp_path / "replay").exists()

    steps = manager.get_replay_steps(session_id)
    assert len(steps) == 1
    assert steps[0]["step_number"] == 1
    assert (
        str((expected_replay_dir / "step_01" / "pre.jpg").resolve()) in steps[0]["pre_screenshot"]
    )
    assert (
        str((expected_replay_dir / "step_01" / "post.jpg").resolve()) in steps[0]["post_screenshot"]
    )
    assert steps[0]["is_replayed"] is False
    assert manager.load_steps_metadata(session_id)[1]["step_id"] == "step-uuid-01"


def test_ensure_session_chunked_skips_zero_step_sessions(tmp_path: Path):
    session_id = "abcdef12-2222-3333-4444-555555555555"
    exec_dir = tmp_path / f"web_2_{session_id[:8]}_FAIL_20261113_082000"
    exec_dir.mkdir(parents=True)
    _seed_session_db(tmp_path, session_id, exec_dir=exec_dir, with_steps=False)

    manager = ReplayManager(workspace_root=tmp_path, traces_path=tmp_path)
    replay_dir = manager._ensure_session_chunked(session_id)

    assert replay_dir == exec_dir / "replay"
    assert not replay_dir.exists()
    assert not (tmp_path / "replay").exists()
    assert manager.get_replay_steps(session_id) == []


def test_archive_older_replays_and_sandbox_db_use_session_replay_dir(tmp_path: Path, monkeypatch):
    session_id = "fedcba98-2222-3333-4444-555555555555"
    exec_dir = tmp_path / f"web_3_{session_id[:8]}_PASS_20261113_083000"
    sandbox_step_dir = exec_dir / "replay" / "outputs" / "step_01"
    sandbox_db = sandbox_step_dir / "temp_traces" / "data_engine.db"
    sandbox_db.parent.mkdir(parents=True)
    sandbox_db.write_bytes(b"sqlite-placeholder")

    monkeypatch.setattr(
        steps_router.media_service,
        "resolve_session_dir",
        lambda sid: exec_dir if sid == session_id else tmp_path / sid,
    )
    assert steps_router._resolve_trace_db_path(session_id, 1) == sandbox_db

    monkeypatch.setattr(tqs_mod, "TRACES_PATH", tmp_path)
    tqs_mod.TaskQueueService.archive_older_replays_on_launch()

    assert not sandbox_step_dir.exists()
    archived = list((exec_dir / "replay" / "older").glob("step_01_*"))
    assert len(archived) == 1
    assert (archived[0] / "temp_traces" / "data_engine.db").is_file()
    assert not (tmp_path / "replay").exists()


def test_consolidate_and_delete_session_manage_web_replay_dir(tmp_path: Path, monkeypatch):
    session_id = "99998888-2222-3333-4444-555555555555"
    transient_dir = tmp_path / session_id
    (transient_dir / "replay" / "step_01").mkdir(parents=True)
    (transient_dir / "stdout.log").write_text("worker output", encoding="utf-8")
    (transient_dir / "replay" / "step_01" / "step.json").write_text("{}", encoding="utf-8")

    finalized_dir = tmp_path / f"web_9_{session_id[:8]}_PASS_20261113_090000"
    (finalized_dir / "images").mkdir(parents=True)
    (finalized_dir / "images" / "shot.jpeg").write_bytes(b"\xff\xd8\xff")

    monkeypatch.setattr(trace_store, "TRACES_DIR", str(tmp_path))
    tqs_mod.TaskQueueService._consolidate_trace_artifacts(session_id, session_id)

    assert not transient_dir.exists()
    assert (finalized_dir / "stdout.log").is_file()
    assert (finalized_dir / "replay" / "step_01" / "step.json").is_file()

    storage = _seed_session_db(tmp_path, session_id, exec_dir=finalized_dir, with_steps=False)
    storage.delete_session(UUID(session_id))
    assert not finalized_dir.exists()


def test_media_service_resolve_session_dir_delegates_consistently(tmp_path: Path, monkeypatch):
    session_id = "77776666-2222-3333-4444-555555555555"
    exec_dir = tmp_path / f"web_4_{session_id[:8]}_TESTFAIL_20261113_091500"
    (exec_dir / "replay" / "step_01").mkdir(parents=True)

    monkeypatch.setattr("apps.admin_console.services.media_service.TRACES_PATH", tmp_path)
    assert MediaService.resolve_session_dir(session_id) == exec_dir
