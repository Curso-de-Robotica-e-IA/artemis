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

import base64
from datetime import datetime
import hashlib
import json
import logging
import os
from pathlib import Path
import sqlite3
import subprocess
import threading
import time
from typing import Any
import urllib.parse
from fastapi import HTTPException

from artemis.config import IMAGES_DIR, TRACES_PATH, WORKSPACE_ROOT

logger = logging.getLogger(__name__)

RESERVED_TRACE_SUBDIRS = ("replay", "images", "data", "temp_traces", "outputs")
_SESSION_ARTIFACT_MARKERS = ("notes", "check_ledger.jsonl", "images", "steps.json", "replay")


def _extract_video_prefixes(
    db_path: Path, session_id: str, traces_path: Path
) -> tuple[Path | None, list[str]]:
    """Return an existing direct parent dir from video metadata and fallback folder prefixes."""
    if not db_path.is_file():
        return None, []

    prefixes: list[str] = []
    direct_match: Path | None = None
    try:
        traces_root = traces_path.resolve()
    except OSError:
        traces_root = traces_path

    conn: sqlite3.Connection | None = None
    try:
        conn = sqlite3.connect(db_path)
        cursor = conn.cursor()
        raw_paths: list[str] = []
        for query in (
            "SELECT video_filepath FROM sessions WHERE session_id = ? AND video_filepath IS NOT NULL",
            "SELECT local_video_path FROM video_recordings WHERE session_id = ? AND local_video_path IS NOT NULL",
        ):
            try:
                cursor.execute(query, (session_id,))
                raw_paths.extend(str(row[0]) for row in cursor.fetchall() if row and row[0])
            except sqlite3.OperationalError:
                continue
    except (sqlite3.Error, OSError):
        raw_paths = []
    finally:
        if conn is not None:
            conn.close()

    for raw in raw_paths:
        if raw.startswith("/videos/"):
            rel = raw.removeprefix("/videos/").split("?")[0]
            ws_candidate = (WORKSPACE_ROOT / rel).parent
            candidate_parent = ws_candidate if ws_candidate.is_dir() else (traces_path / rel).parent
        else:
            candidate_parent = Path(raw).parent

        parent_name = candidate_parent.name
        if parent_name and parent_name not in RESERVED_TRACE_SUBDIRS:
            base_name = MediaService.strip_trace_status_suffix(parent_name)
            if base_name and base_name not in prefixes:
                prefixes.append(base_name)
            if candidate_parent.is_dir():
                try:
                    resolved_parent = candidate_parent.resolve()
                    if resolved_parent.parent == traces_root:
                        direct_match = candidate_parent
                except OSError:
                    pass
    return direct_match, prefixes


def resolve_session_trace_dir(
    traces_path: Path,
    session_id: str,
    db_path: Path | None = None,
) -> Path:
    """Resolve the execution trace directory for ``session_id`` under ``traces_path``.

    Supports active un-suffixed folders (``web_<id>_<short_uuid>``), finalized
    folders (``web_<id>_<short_uuid>_<STATUS>_<timestamp>``), direct UUID
    folders, and custom ``--test-name`` folders recorded alongside video paths.
    Prefer finalized or artifact-bearing execution directories over transient
    ``traces/<session_id>`` staging folders that only contain process logs.
    """
    s_id = str(session_id).strip()
    if not s_id:
        return traces_path

    direct = traces_path / s_id
    s_prefix = s_id[:8] if len(s_id) >= 8 else s_id
    resolved_db = db_path if db_path is not None else (traces_path / "data_engine.db")
    video_dir, video_prefixes = _extract_video_prefixes(resolved_db, s_id, traces_path)
    if video_dir is not None and any(
        marker in video_dir.name for marker in MediaService.TRACE_STATUS_SUFFIXES
    ):
        return video_dir

    if not traces_path.is_dir():
        return direct

    best_score: tuple[int, int, int, int, int, int, str] | None = None
    best_folder: Path | None = None
    try:
        candidates = list(traces_path.iterdir())
    except OSError:
        return video_dir if video_dir is not None else direct

    for folder in candidates:
        if not folder.is_dir():
            continue
        name = folder.name
        if name.startswith(".") or name in RESERVED_TRACE_SUBDIRS:
            continue
        matches_sid = s_id in name
        matches_prefix = (len(s_prefix) >= 8 and s_prefix in name) or (
            len(s_id) < 8 and s_id in name
        )
        matches_video = any(vp and name.startswith(vp) for vp in video_prefixes)
        if not (matches_sid or matches_prefix or matches_video):
            continue

        has_terminal = any(marker in name for marker in MediaService.TRACE_STATUS_SUFFIXES)
        has_artifacts = any((folder / sub).exists() for sub in _SESSION_ARTIFACT_MARKERS)
        is_web = name.startswith("web_")
        is_direct = folder == direct
        score = (
            int(has_terminal and has_artifacts),
            int(has_terminal),
            int(has_artifacts),
            int(is_web),
            int(matches_sid),
            int(not is_direct),
            name,
        )
        if best_score is None or score > best_score:
            best_score = score
            best_folder = folder

    if best_folder is not None:
        return best_folder
    if video_dir is not None:
        return video_dir
    return direct


class MediaService:
    """Service handling media indexing, local file security, payload unwrapping and notes/plans."""

    # Conversion results keyed by source path -> (mtime, size, resolved path).
    # Failed conversions are cached too (resolved path == source path) so a broken
    # file doesn't re-run ffmpeg (up to 45s) on every /api/sessions poll.
    _playable_cache: dict[str, tuple[float, int, str]] = {}
    _playable_cache_lock = threading.Lock()

    @classmethod
    def ensure_browser_playable_video(cls, p: Path) -> Path:
        """Ensure the video file is in a browser-supported container (MP4).

        If given an MKV, check if a converted MP4 already exists or convert it.
        """
        try:
            if not p.exists():
                return p
            suffix = p.suffix.lower()
            if suffix == ".mp4":
                return p
            if suffix in (".mkv", ".webm"):
                if cls.is_live_recording(p):
                    # scrcpy is still appending to this file: converting now
                    # would freeze a few-seconds partial as recording.mp4.
                    return p
                src_stat = p.stat()
                cache_key = str(p)
                cached = cls._playable_cache.get(cache_key)
                if cached and cached[0] == src_stat.st_mtime and cached[1] == src_stat.st_size:
                    return Path(cached[2])
                mp4_cand = p.with_suffix(".mp4")
                if (
                    mp4_cand.exists()
                    and mp4_cand.stat().st_size > 0
                    and mp4_cand.stat().st_mtime >= src_stat.st_mtime
                ):
                    # An MP4 older than its source is a stale partial left by a
                    # mid-run scan; fall through and convert again.
                    cls._playable_cache[cache_key] = (
                        src_stat.st_mtime,
                        src_stat.st_size,
                        str(mp4_cand),
                    )
                    return mp4_cand
                with cls._playable_cache_lock:
                    # Another thread may have converted this file while we waited.
                    cached = cls._playable_cache.get(cache_key)
                    if cached and cached[0] == src_stat.st_mtime and cached[1] == src_stat.st_size:
                        return Path(cached[2])
                    try:
                        result = cls._convert_to_mp4(p, mp4_cand)
                    except Exception:
                        result = p
                    cls._playable_cache[cache_key] = (
                        src_stat.st_mtime,
                        src_stat.st_size,
                        str(result),
                    )
                    return result
        except OSError:
            # Filesystem probe failed (missing/locked file): serve the
            # original path and let the media endpoint report the error.
            pass
        return p

    @staticmethod
    def _convert_to_mp4(p: Path, mp4_cand: Path) -> Path:
        from artemis.utils.video import get_ffmpeg_path

        ffmpeg = get_ffmpeg_path()
        # 1. Attempt fast copy remux
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-fflags",
                "+genpts",
                "-i",
                str(p),
                "-c",
                "copy",
                "-movflags",
                "+faststart",
                str(mp4_cand),
            ],
            capture_output=True,
            timeout=15,
        )
        if mp4_cand.exists() and mp4_cand.stat().st_size > 0:
            return mp4_cand
        # 2. If copy remux failed, fallback to ultrafast re-encode
        subprocess.run(
            [
                ffmpeg,
                "-y",
                "-fflags",
                "+genpts+discardcorrupt",
                "-i",
                str(p),
                "-c:v",
                "libx264",
                "-preset",
                "ultrafast",
                "-pix_fmt",
                "yuv420p",
                "-movflags",
                "+faststart",
                str(mp4_cand),
            ],
            capture_output=True,
            timeout=30,
        )
        if mp4_cand.exists() and mp4_cand.stat().st_size > 0:
            return mp4_cand
        return p

    LIVE_RECORDING_GRACE_SECONDS = 10.0
    TRACE_STATUS_SUFFIXES = ("_PASS_", "_FAIL_", "_TESTFAIL_")

    @classmethod
    def is_live_recording(cls, p: Path) -> bool:
        """Whether ``p`` is a raw .mkv that scrcpy is still appending to.

        scrcpy touches the file continuously while recording; a raw file that
        has not changed for a few seconds belongs to a finished (or killed)
        recording and is safe to convert.
        """
        if p.suffix.lower() != ".mkv":
            return False
        try:
            return (time.time() - p.stat().st_mtime) < cls.LIVE_RECORDING_GRACE_SECONDS
        except OSError:
            return False

    @staticmethod
    def is_browser_playable(p: Path) -> bool:
        return p.suffix.lower() in (".mp4", ".webm")

    @classmethod
    def strip_trace_status_suffix(cls, folder_name: str) -> str:
        for marker in cls.TRACE_STATUS_SUFFIXES:
            folder_name = folder_name.split(marker)[0]
        return folder_name

    @classmethod
    def _finalize_trace_folder_name(cls, folder: Path, session_start_time: float | None) -> Path:
        """Give an unfinished temp trace folder the terminal name the worker uses."""
        try:
            if folder.resolve().parent != Path(TRACES_PATH).resolve():
                return folder
        except OSError:
            return folder
        if any(marker in folder.name for marker in cls.TRACE_STATUS_SUFFIXES):
            return folder
        try:
            stamp = datetime.fromtimestamp(float(session_start_time or time.time())).strftime(
                "%Y-%m-%dT%H-%M-%S"
            )
        except (TypeError, ValueError, OSError, OverflowError):
            stamp = datetime.now().strftime("%Y-%m-%dT%H-%M-%S")
        target = folder.with_name(f"{folder.name}_FAIL_{stamp}")
        if target.exists():
            return folder
        try:
            folder.rename(target)
        except OSError as exc:
            logger.warning("Could not rename orphaned trace folder %s: %s", folder, exc)
            return folder
        return target

    @classmethod
    def recover_orphaned_recording(
        cls,
        local_video_path: str | Path | None,
        session_start_time: float | None = None,
    ) -> Path | None:
        """Finalize the recording of a worker that died before its own stop path ran.

        A hard-killed worker leaves the raw scrcpy ``recording.mkv`` behind
        (playable, but never remuxed) plus, possibly, a few-seconds partial
        ``recording.mp4`` that a session-list scan converted mid-run. The raw
        file is remuxed over that partial and removed on success, and the temp
        trace folder is renamed the way the worker would have done it.

        Returns the final browser-playable path, or None when nothing usable
        exists (the caller then records a terminal recording failure).
        """
        if not local_video_path:
            return None
        source = Path(str(local_video_path))
        folder = source.parent
        if not folder.is_dir():
            return None
        raw = source if source.suffix.lower() == ".mkv" else folder / "recording.mkv"
        mp4 = folder / "recording.mp4"

        def _non_empty(path: Path) -> bool:
            try:
                return path.is_file() and path.stat().st_size > 0
            except OSError:
                return False

        if _non_empty(raw):
            if cls.is_live_recording(raw):
                return None
            with cls._playable_cache_lock:
                converted = cls._convert_to_mp4(raw, mp4)
                cls._playable_cache.pop(str(raw), None)
            if converted != mp4 or not _non_empty(mp4):
                return None
            try:
                raw.unlink()
            except OSError:
                pass
            final = mp4
        elif _non_empty(mp4):
            final = mp4
        elif _non_empty(source) and cls.is_browser_playable(source):
            final = source
        else:
            return None

        folder = cls._finalize_trace_folder_name(folder, session_start_time)
        return folder / final.name

    @staticmethod
    def path_to_video_url(p: Path) -> str:
        resolved = p.resolve()
        try:
            rel = resolved.relative_to(WORKSPACE_ROOT)
            return f"/videos/{urllib.parse.quote(rel.as_posix(), safe='/')}"
        except ValueError:
            return f"/videos/{urllib.parse.quote(resolved.as_posix().lstrip('/'), safe='/:')}"

    @classmethod
    def build_video_index(cls) -> dict[str, str]:
        idx = {}
        dirs = [
            TRACES_PATH,
            WORKSPACE_ROOT / "artemis-traces",
            WORKSPACE_ROOT / ".benchmarks" / "diagnoser" / "outputs" / "artifacts",
        ]
        for d in dirs:
            if not d.exists():
                continue
            try:
                entries = list(d.iterdir())
            except OSError as exc:
                logger.warning("Video index scan failed under %s: %s", d, exc)
                continue
            for item in entries:
                # One unreadable entry must not abort the rest of the directory.
                try:
                    if item.is_dir():
                        for ext in [".mp4", ".mkv", ".webm"]:
                            vfile = item / f"recording{ext}"
                            if vfile.exists():
                                vfile = cls.ensure_browser_playable_video(vfile)
                                if not cls.is_browser_playable(vfile):
                                    # Live (still recording) or unconvertible raw file.
                                    break
                                url = cls.path_to_video_url(vfile)
                                idx[item.name] = url
                                prefix = cls.strip_trace_status_suffix(item.name)
                                idx[prefix] = url
                                break
                    elif item.suffix in [".mp4", ".mkv", ".webm"]:
                        item = cls.ensure_browser_playable_video(item)
                        if not cls.is_browser_playable(item):
                            continue
                        url = cls.path_to_video_url(item)
                        idx[item.stem] = url
                        idx[item.name] = url
                except OSError as exc:
                    logger.warning("Skipping unindexable video entry %s: %s", item, exc)
        return idx

    @classmethod
    def resolve_video_url(
        cls,
        row_dict: dict[str, Any],
        video_rec_map: dict[str, str],
        video_idx: dict[str, str],
    ) -> str | None:
        raw_sid = row_dict.get("session_id")
        s_id = (
            str(raw_sid).strip()
            if raw_sid is not None and str(raw_sid).strip().lower() != "none"
            else ""
        )
        v_url = None
        v_fp = row_dict.get("video_filepath")
        if v_fp and os.path.exists(v_fp):
            p = cls.ensure_browser_playable_video(Path(v_fp))
            if cls.is_browser_playable(p):
                v_url = cls.path_to_video_url(p)

        orig_rec = video_rec_map.get(s_id)
        if not v_url and orig_rec:
            p = Path(orig_rec)
            if p.exists():
                p = cls.ensure_browser_playable_video(p)
                if cls.is_browser_playable(p):
                    v_url = cls.path_to_video_url(p)
            if not v_url:
                folder_name = p.parent.name
                v_url = video_idx.get(folder_name) or video_idx.get(folder_name.split("_")[0])

        if not v_url:
            v_url = video_idx.get(s_id)

        if not v_url and s_id:
            sess_dir = TRACES_PATH / s_id
            if sess_dir.is_dir():
                for ext in [".mp4", ".webm", ".mkv"]:
                    vfile = sess_dir / f"recording{ext}"
                    if vfile.exists():
                        vfile = cls.ensure_browser_playable_video(vfile)
                        if cls.is_browser_playable(vfile):
                            v_url = cls.path_to_video_url(vfile)
                        break
                if not v_url:
                    try:
                        for item in sess_dir.iterdir():
                            if item.is_file() and item.suffix.lower() in [".mp4", ".webm", ".mkv"]:
                                item = cls.ensure_browser_playable_video(item)
                                if not cls.is_browser_playable(item):
                                    continue
                                v_url = cls.path_to_video_url(item)
                                break
                    except OSError:
                        # Unreadable session dir: no recording to resolve.
                        pass

        if not v_url and row_dict.get("initial_goal"):
            goal_str = row_dict["initial_goal"]
            task_hint = (
                goal_str.split("Task: ")[1].split()[0].strip()
                if "Task: " in goal_str
                else goal_str.split("\n")[0].strip()
            )
            v_url = video_idx.get(task_hint)

        return v_url

    @classmethod
    def resolve_video_segments(cls, video_url: str | None) -> list[dict[str, Any]]:
        """Resolve a recording sidecar manifest into browser-safe segment URLs."""
        if not video_url or not video_url.startswith("/videos/"):
            return []
        relative = urllib.parse.unquote(video_url.removeprefix("/videos/"))
        video_path = (WORKSPACE_ROOT / relative).resolve()
        manifest_path = video_path.parent / "recording.json"
        if not manifest_path.is_file():
            return []
        try:
            payload = json.loads(manifest_path.read_text(encoding="utf-8"))
            segments = []
            for item in payload.get("segments", []):
                segment_path = (manifest_path.parent / str(item["file"])).resolve()
                if (
                    segment_path.parent != manifest_path.parent.resolve()
                    or not segment_path.is_file()
                ):
                    continue
                segment = {
                    "url": cls.path_to_video_url(segment_path),
                    "start": float(item.get("start", 0)),
                    "duration": float(item.get("duration", 0)),
                    "width": int(item.get("width", 0)),
                    "height": int(item.get("height", 0)),
                }
                # Manifest v2 carries the real session-relative timeline so the
                # UI can seek by step time across scrcpy restart gaps.
                if item.get("offset_ms") is not None and item.get("duration_ms") is not None:
                    segment["offset_ms"] = int(item["offset_ms"])
                    segment["duration_ms"] = int(item["duration_ms"])
                segments.append(segment)
            return segments
        except (OSError, KeyError, TypeError, ValueError, json.JSONDecodeError):
            return []

    @staticmethod
    def unwrap_payload(obj: Any) -> Any:
        """Unwraps trace payloads, storing base64 images as disk files and
        truncating large dumps.
        """
        if isinstance(obj, str):
            try:
                if (obj.startswith("{") and obj.endswith("}")) or (
                    obj.startswith("[") and obj.endswith("]")
                ):
                    return MediaService.unwrap_payload(json.loads(obj))
            except ValueError:
                # Looks like JSON but is not: treat it as a plain string.
                pass

            # Check if the string is a Base64 image
            is_base64_img = False
            base64_data = None
            if obj.startswith("data:image/") and ";base64," in obj:
                try:
                    _, base64_data = obj.split(";base64,", 1)
                    is_base64_img = True
                except ValueError:
                    # Malformed data URI: fall through to plain-string handling.
                    pass
            elif obj.startswith("iVBORw0KGgo") or obj.startswith("/9j/"):
                is_base64_img = True
                base64_data = obj

            if is_base64_img and base64_data:
                try:
                    image_bytes = base64.b64decode(base64_data)
                    image_hash = hashlib.sha256(image_bytes).hexdigest()

                    IMAGES_DIR.mkdir(parents=True, exist_ok=True)
                    image_path = IMAGES_DIR / f"{image_hash}.jpg"
                    if not image_path.exists():
                        image_path.write_bytes(image_bytes)

                    return f"image://{image_hash}"
                except (ValueError, OSError) as exc:
                    # Invalid base64 or image cache write failure: fall back
                    # to the truncated raw string below.
                    logger.debug("Could not persist inline image: %s", exc)

            if len(obj) > 2000:
                preview_start = obj[:100].replace("\n", " ")
                preview_end = obj[-100:].replace("\n", " ")
                return (
                    f"<Massive String: {preview_start}...[truncated"
                    f" {len(obj) - 200} characters for UI performance]...{preview_end}"
                    f" (length={len(obj)})>"
                )
            return obj
        elif isinstance(obj, dict):
            return {k: MediaService.unwrap_payload(v) for k, v in obj.items()}
        elif isinstance(obj, list):
            if len(obj) > 20 and any(
                isinstance(x, dict) and ("bounds" in x or "resource-id" in x) for x in obj
            ):
                return [f"<XML UI List with {len(obj)} elements truncated for UI performance>"]
            return [MediaService.unwrap_payload(x) for x in obj]
        return obj

    _IMAGE_MEDIA_TYPES = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
    }
    _image_path_cache: dict[str, Path] = {}
    _image_path_cache_lock = threading.Lock()

    @classmethod
    def resolve_image_path(cls, raw_name: str, session_id: str | None = None) -> Path | None:
        """Resolve an image filename, hash, or relative path to a verified safe Path on disk.

        Handles:
        - Images organized in execution directories:
          traces/<execution_folder>/images/<image_hash>.jpg
          traces/<execution_folder>/<image_hash>.jpg
        - Direct relative or nested paths:
          traces/<path>
        - Legacy root images directory:
          traces/images/<image_hash>.jpg
        - Prioritized search when session_id is provided.
        """
        if not raw_name:
            return None
        decoded = urllib.parse.unquote(str(raw_name)).strip().replace("\\", "/")
        if decoded.startswith("file://"):
            decoded = decoded[len("file://") :]
        while decoded.startswith("/"):
            decoded = decoded[1:]
        if decoded.startswith("api/images/"):
            decoded = decoded[len("api/images/") :]
        elif decoded.startswith("images/"):
            decoded = decoded[len("images/") :]

        if not decoded:
            return None

        cache_key = f"{session_id or ''}:{decoded}"
        with cls._image_path_cache_lock:
            cached = cls._image_path_cache.get(cache_key)
            if cached and cached.is_file():
                return cached

        allowed_roots: list[Path] = []
        for root in (TRACES_PATH, WORKSPACE_ROOT):
            try:
                allowed_roots.append(Path(root).resolve())
            except OSError:
                continue

        def _is_safe_image(p: Path) -> bool:
            try:
                resolved = p.resolve(strict=True)
                if not resolved.is_file():
                    return False
                if not any(resolved.is_relative_to(r) for r in allowed_roots):
                    return False
                return resolved.suffix.lower() in cls._IMAGE_MEDIA_TYPES
            except (OSError, ValueError):
                return False

        suffix = Path(decoded).suffix.lower()
        if suffix in cls._IMAGE_MEDIA_TYPES:
            filenames = [decoded]
        else:
            filenames = [f"{decoded}.jpg", f"{decoded}.png", decoded]

        found: Path | None = None

        # 1. Direct path check if decoded has directory components
        if "/" in decoded:
            for root in allowed_roots:
                for fn in filenames:
                    cand = root / fn
                    if _is_safe_image(cand):
                        found = cand.resolve()
                        break
                if found:
                    break

        # 2. Targeted search if session_id is provided
        if not found and session_id:
            s_id = str(session_id).strip()
            s_prefix = s_id[:8] if len(s_id) >= 8 else s_id
            # 2a. Direct session folder
            for fn in filenames:
                for sub in (
                    TRACES_PATH / s_id / "images" / fn,
                    TRACES_PATH / s_id / fn,
                ):
                    if _is_safe_image(sub):
                        found = sub.resolve()
                        break
                if found:
                    break
            # 2b. Execution folder matching session_id or session prefix
            if not found:
                try:
                    for folder in TRACES_PATH.iterdir():
                        if (
                            folder.is_dir()
                            and not folder.name.startswith(".")
                            and folder.name not in RESERVED_TRACE_SUBDIRS
                            and (s_id in folder.name or s_prefix in folder.name)
                        ):
                            for fn in filenames:
                                for sub in (folder / "images" / fn, folder / fn):
                                    if _is_safe_image(sub):
                                        found = sub.resolve()
                                        break
                                if found:
                                    break
                        if found:
                            break
                except OSError:
                    pass

        # 3. Legacy root images folder
        if not found:
            for fn in filenames:
                cand = IMAGES_DIR / fn
                if _is_safe_image(cand):
                    found = cand.resolve()
                    break

        # 4. Search across all execution folders under TRACES_PATH
        if not found:
            try:
                for folder in TRACES_PATH.iterdir():
                    if (
                        folder.is_dir()
                        and not folder.name.startswith(".")
                        and folder.name not in RESERVED_TRACE_SUBDIRS
                    ):
                        for fn in filenames:
                            for sub in (folder / "images" / fn, folder / fn):
                                if _is_safe_image(sub):
                                    found = sub.resolve()
                                    break
                            if found:
                                break
                    if found:
                        break
            except OSError:
                pass

        if found:
            with cls._image_path_cache_lock:
                if len(cls._image_path_cache) > 2000:
                    cls._image_path_cache.clear()
                cls._image_path_cache[cache_key] = found
                cls._image_path_cache[f":{decoded}"] = found
            return found

        return None

    _LOCAL_FILE_MEDIA_TYPES = {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".gif": "image/gif",
        ".webp": "image/webp",
        ".mp4": "video/mp4",
        ".webm": "video/webm",
        ".mkv": "video/x-matroska",
    }

    @classmethod
    def get_safe_local_file(cls, path_str: str) -> tuple[Path, str]:
        """Serve trace media only: resolved path must sit inside the workspace
        or traces tree AND carry a media extension, so this endpoint can never
        hand out .env, databases, or source files."""
        decoded_path = urllib.parse.unquote(path_str)
        if decoded_path.startswith("file://"):
            decoded_path = decoded_path[len("file://") :]
        try:
            p = Path(decoded_path).resolve(strict=True)
        except (OSError, ValueError):
            raise HTTPException(status_code=404, detail="File not found")

        if not p.is_file():
            raise HTTPException(status_code=404, detail="File not found")

        allowed_roots = []
        for root in (WORKSPACE_ROOT, TRACES_PATH):
            try:
                allowed_roots.append(Path(root).resolve())
            except OSError:
                continue
        if not any(p.is_relative_to(root) for root in allowed_roots):
            raise HTTPException(
                status_code=403,
                detail="Access denied. Path is outside workspace root.",
            )

        media_type = cls._LOCAL_FILE_MEDIA_TYPES.get(p.suffix.lower())
        if media_type is None:
            raise HTTPException(
                status_code=403,
                detail="Access denied. Only media files are served.",
            )

        return p, media_type

    @classmethod
    def resolve_session_dir(cls, session_id: str) -> Path:
        """Find the trace directory for a session_id, matching direct, suffixed, or web-prefixed folders."""
        return resolve_session_trace_dir(TRACES_PATH, session_id)

    @classmethod
    def get_task_plan_content(cls, session_id: str) -> str:
        session_dir = cls.resolve_session_dir(session_id)
        plan_path = session_dir / "notes" / "task_plan.md"
        if not plan_path.exists():
            plan_path = TRACES_PATH / "notes" / "task_plan.md"

        if plan_path.exists():
            try:
                return plan_path.read_text(encoding="utf-8")
            except OSError as e:
                return f"Error reading task plan: {e}"
        return "No task plan created yet."

    @staticmethod
    def _read_jsonl_records(path: Path, label: str, session_id: str) -> list[dict[str, Any]]:
        records: list[dict[str, Any]] = []
        if not path.is_file():
            return records
        try:
            for line in path.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except ValueError:
                    continue
                if isinstance(rec, dict):
                    records.append(rec)
        except (OSError, ValueError) as e:
            logger.warning(f"Failed to read {label} for {session_id}: {e}")
        return records

    @classmethod
    def get_session_checks(cls, session_id: str) -> dict[str, Any]:
        """Checker material of one session for UI backfill: the append-only
        verdict ledger (``check_ledger.jsonl``), the per-attempt transcripts of
        the Checker's streamed reasoning (``check_streams.jsonl``: one record
        per attempt with timestamped ``segments``) and the machine-readable run
        outcome (``run_outcome.json``), all written by the Checker /
        ``artemis.graph.checkpoints``. Missing files mean "no checks ran"
        (sessions recorded before transcripts existed simply have no streams).
        """
        session_dir = cls.resolve_session_dir(session_id)
        records = MediaService._read_jsonl_records(
            session_dir / "check_ledger.jsonl", "check ledger", session_id
        )
        streams = MediaService._read_jsonl_records(
            session_dir / "check_streams.jsonl", "check streams", session_id
        )

        run_outcome: dict[str, Any] | None = None
        outcome_path = session_dir / "run_outcome.json"
        if outcome_path.is_file():
            try:
                loaded = json.loads(outcome_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    run_outcome = loaded
            except (OSError, ValueError) as e:
                logger.warning(f"Failed to read run outcome for {session_id}: {e}")

        return {"records": records, "streams": streams, "run_outcome": run_outcome}

    @classmethod
    def get_session_notes_content(cls, session_id: str) -> dict[str, str]:
        session_dir = cls.resolve_session_dir(session_id)
        notes_dir = session_dir / "notes"
        notes_content = {}

        if notes_dir.exists() and notes_dir.is_dir():
            for file in notes_dir.iterdir():
                if file.is_file() and file.suffix in [".md", ".txt", ".json", ".yaml"]:
                    try:
                        notes_content[file.name] = file.read_text(encoding="utf-8")
                    except OSError as e:
                        notes_content[file.name] = f"Error reading file: {e}"

        if not notes_content:
            global_notes_dir = TRACES_PATH / "notes"
            if global_notes_dir.exists() and global_notes_dir.is_dir():
                for file in global_notes_dir.iterdir():
                    if file.is_file() and file.suffix in [
                        ".md",
                        ".txt",
                        ".json",
                        ".yaml",
                    ]:
                        try:
                            notes_content[file.name] = file.read_text(encoding="utf-8")
                        except Exception as e:
                            notes_content[file.name] = f"Error reading file: {e}"

        return notes_content


media_service = MediaService()
