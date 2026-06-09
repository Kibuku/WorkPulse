"""
activity.py — WorkPulse-native activity tracker.

Replaces ActivityWatch. Samples the foreground window + idle state every
few seconds using the platform-appropriate API — no external daemon.

Backends (auto-selected from sys.platform):
  • Windows: activity_win.py     — ctypes + Win32 (user32 / kernel32)
  • macOS:   activity_mac.py     — PyObjC + AppKit + Quartz

Output: logs/activity_YYYY-MM-DD.jsonl, one record per *contiguous session*
of the same (app, title, stream). Each record has start, end, duration_s.

Record schema:
    {
      "start":       "2026-04-22T14:07:00+03:00",   # local ISO
      "end":         "2026-04-22T14:12:30+03:00",
      "duration_s":  330,
      "app":         "WINWORD.EXE",
      "exe_path":    "C:\\Program Files\\...\\WINWORD.EXE",
      "title":       "CHAPTER ONE - GEORGE.docx - Word",
      "stream":      "dissertation",
      "idle":        false
    }
"""

from __future__ import annotations

import io
import json
import sys
import time
from datetime import datetime
from pathlib import Path

import psutil

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import ensure_dir, load_config, resolve

# ── platform backend ──────────────────────────────────────────────────────────
# Each backend exposes:
#   foreground_window() -> (title, pid) | None
#   idle_seconds()      -> float
# The shim names below (_foreground_window, _idle_seconds) keep the rest of
# this file platform-agnostic.

if sys.platform == "win32":
    from scripts.activity_win import foreground_window as _foreground_window
    from scripts.activity_win import idle_seconds      as _idle_seconds
elif sys.platform == "darwin":
    from scripts.activity_mac import foreground_window as _foreground_window
    from scripts.activity_mac import idle_seconds      as _idle_seconds
else:
    # Linux + others: surface a clear error rather than crash later. Per Vision
    # roadmap, Linux is not planned.
    raise NotImplementedError(
        f"WorkPulse activity tracker doesn't support platform '{sys.platform}'. "
        "Supported: Windows (v1.0), macOS (v1.6)."
    )


def _process_info(pid: int) -> tuple[str, str]:
    """Return (exe_name, exe_path) for a PID, best-effort."""
    try:
        p = psutil.Process(pid)
        return p.name(), p.exe()
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return "", ""


# ── stream tagging ────────────────────────────────────────────────────────────

def _open_file_paths(pid: int) -> list[str]:
    """Return open file paths for a PID, lowercased forward-slash form. Best-effort."""
    if not pid:
        return []
    try:
        p = psutil.Process(pid)
        return [f.path.replace("\\", "/").lower() for f in p.open_files()]
    except (psutil.NoSuchProcess, psutil.AccessDenied, Exception):
        return []


def _tag_by_open_files(pid: int, cfg: dict) -> str | None:
    """If the foreground process has any open file under a configured stream
    root folder, return that stream. Subset folders (e.g. dissertation under
    masters) must be listed in config order — first match wins."""
    roots = cfg["watcher"].get("stream_folder_roots") or {}
    if not roots:
        return None
    files = _open_file_paths(pid)
    if not files:
        return None
    # Iterate streams in dict order; subset (dissertation) must come before parent (masters)
    for stream, folder_list in roots.items():
        for folder in folder_list:
            needle = folder.replace("\\", "/").lower().rstrip("/") + "/"
            for f in files:
                if needle in f:
                    return stream
    return None


def _tag_stream(title: str, exe_path: str, cfg: dict, pid: int = 0) -> str | None:
    """Match stream from (1) open file paths under a known stream root,
    then (2) window title / exe path patterns, then (3) stream-name keywords,
    then (4) learned rules (Loop B), then (5) Word-doc content classification."""
    # 1. Most authoritative: process has a file open under a known project folder
    by_files = _tag_by_open_files(pid, cfg)
    if by_files:
        return by_files

    # 2. Fall back to title / exe substring match
    patterns = cfg["watcher"].get("stream_path_patterns", [])
    haystack = f"{title} {exe_path}".lower()
    for p in patterns:
        needle = p["path"].replace("\\", "/").lower()
        if needle in haystack or needle.replace("/", " ") in haystack:
            return p["stream"]
    # 3. Bare stream keywords (e.g. window title "Water Kiosk.xlsx")
    from scripts.tree import labels as _stream_labels
    for key, label in _stream_labels(cfg).items():
        if key.replace("-", " ") in haystack or label.lower() in haystack:
            return key

    # 4. Learned rules (Loop B) — patterns the system acquired from prior AI
    #    classifications. Substring match on normalized title.
    try:
        from scripts.learning import match_learned
        learned = match_learned(title, cfg)
        if learned:
            return learned
    except Exception:
        pass

    # 5. Content classification for Word — read the open .docx and ask Claude.
    #    Only fires when nothing else matched, and result is cached per file revision.
    if pid and exe_path and "winword" in exe_path.lower():
        for f in _open_file_paths(pid):
            if f.endswith(".docx") and "~$" not in f:
                try:
                    from scripts.doctag import classify_doc
                    tag = classify_doc(f, cfg)
                    if tag:
                        return tag
                except Exception:
                    pass
                break  # only classify the first non-temp .docx
    return None


# ── sampler ───────────────────────────────────────────────────────────────────

SAMPLE_INTERVAL_S = 5
IDLE_THRESHOLD_S = 60       # no input for 60s = AFK
CHECKPOINT_EVERY_S = 60     # flush current session at least every 60s


def _log_path(cfg: dict) -> Path:
    logs = resolve(cfg["paths"]["logs"])
    ensure_dir(logs)
    return logs / f"activity_{datetime.now().date().isoformat()}.jsonl"


def _append(record: dict, cfg: dict) -> None:
    with _log_path(cfg).open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    # v2 dual-write: best-effort SQLite atom alongside the JSONL. Never raises
    # back into the sensor hot path; see scripts/dual_write.py.
    try:
        from scripts.dual_write import dual_write_session
        dual_write_session(record, cfg)
    except Exception:
        pass


def _now_iso() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


class Session:
    """A contiguous run of the same (app, title, idle)."""

    def __init__(self, app: str, exe_path: str, title: str, stream: str | None, idle: bool):
        self.app = app
        self.exe_path = exe_path
        self.title = title
        self.stream = stream
        self.idle = idle
        self.start = _now_iso()
        self._start_mono = time.monotonic()

    def matches(self, app: str, title: str, idle: bool) -> bool:
        # Merge consecutive samples of same window; idle boundary breaks sessions.
        return self.app == app and self.title == title and self.idle == idle

    def to_record(self) -> dict:
        end = _now_iso()
        return {
            "start": self.start,
            "end": end,
            "duration_s": round(time.monotonic() - self._start_mono, 1),
            "app": self.app,
            "exe_path": self.exe_path,
            "title": self.title,
            "stream": self.stream,
            "idle": self.idle,
        }


def run() -> None:
    cfg = load_config()
    print(f"[activity] WorkPulse activity tracker starting (PID {__import__('os').getpid()})")
    print(f"[activity] Sample every {SAMPLE_INTERVAL_S}s, idle threshold {IDLE_THRESHOLD_S}s")
    print(f"[activity] Writing to {_log_path(cfg)}")
    sys.stdout.flush()

    current: Session | None = None
    last_checkpoint = time.monotonic()

    try:
        while True:
            fg = _foreground_window()
            idle_s = _idle_seconds()
            is_idle = idle_s >= IDLE_THRESHOLD_S

            if fg is None:
                title, pid = "(no window)", 0
                app, exe_path = "", ""
            else:
                title, pid = fg
                app, exe_path = _process_info(pid)

            stream = _tag_stream(title, exe_path, cfg, pid)
            now = time.monotonic()

            # New session?
            if current is None:
                current = Session(app, exe_path, title, stream, is_idle)
                last_checkpoint = now
            elif not current.matches(app, title, is_idle):
                # Window changed — close previous, start new
                rec = current.to_record()
                _append(rec, cfg)
                # Loop B: if the closed session was untagged, not idle, and the
                # user spent meaningful time on it, ask Claude to classify it.
                # Result lands in learned_tags.json so the next encounter is
                # tagged locally without an API call.
                if (
                    rec.get("stream") is None
                    and not rec.get("idle")
                    and rec.get("duration_s", 0) >= 30
                    and rec.get("title")
                ):
                    try:
                        from scripts.learning import classify_async
                        classify_async(rec["title"], rec.get("app", ""),
                                       rec["duration_s"], cfg)
                    except Exception:
                        pass
                current = Session(app, exe_path, title, stream, is_idle)
                last_checkpoint = now
            elif now - last_checkpoint >= CHECKPOINT_EVERY_S:
                # Same window, but time to checkpoint so the log stays live
                _append(current.to_record(), cfg)
                current = Session(app, exe_path, title, stream, is_idle)
                last_checkpoint = now

            time.sleep(SAMPLE_INTERVAL_S)
    except KeyboardInterrupt:
        pass
    finally:
        if current is not None:
            _append(current.to_record(), cfg)
        print("[activity] Shutdown complete.")


if __name__ == "__main__":
    run()
