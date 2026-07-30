"""
watcher.py — Phase 2: file watcher daemon.

Watches all configured roots (whole machine by default) for file changes
and appends events to logs\\file_events_YYYY-MM-DD.jsonl.

Run:
  python scripts\\watcher.py            # foreground (Ctrl-C to stop)
  python scripts\\watcher.py --once     # print config and exit (smoke test)

Installed as a Windows Task Scheduler logon task via install_tasks.ps1.
"""

from __future__ import annotations

import argparse
import fnmatch
import json
import logging
import sys
import threading
import time
from datetime import datetime, timezone
from pathlib import Path

from watchdog.events import FileSystemEvent, FileSystemEventHandler
from watchdog.observers import Observer

from workpulse.common import ensure_dir, load_config, resolve, ROOT

import io
import os
# Force UTF-8 output on Windows terminals
if sys.stdout.encoding != "utf-8":
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s  %(levelname)-7s  %(message)s",
    datefmt="%Y-%m-%d %H:%M:%S",
    stream=sys.stdout,
)
log = logging.getLogger("watcher")


# ── path helpers ──────────────────────────────────────────────────────────────

def _expand_root(root_str: str) -> Path:
    """Expand ~ to home dir and return an absolute Path."""
    p = Path(root_str).expanduser()
    return p.resolve()


def _watch_roots(
    configured: list[str],
    *,
    platform: str | None = None,
    environ: dict[str, str] | None = None,
) -> list[Path]:
    """Resolve configured roots and add platform-native cloud folders.

    The example configuration is shared by macOS and Windows. Platform-only
    defaults are ignored on the other OS, while Windows OneDrive locations are
    discovered from the environment set by the OneDrive client.
    """
    platform = platform or sys.platform
    environ = os.environ if environ is None else environ
    roots: list[Path] = []

    for value in configured:
        normalised = value.replace("\\", "/")
        if platform == "win32" and normalised.endswith("/Library/CloudStorage"):
            continue
        roots.append(_expand_root(value))

    if platform == "win32":
        for key in ("OneDriveCommercial", "OneDriveConsumer", "OneDrive"):
            value = environ.get(key)
            if value:
                roots.append(Path(value).expanduser().resolve())

    # Preserve ordering while avoiding duplicate roots (the three OneDrive
    # environment variables often point to the same directory).
    return list(dict.fromkeys(roots))


def _is_ignored_dir(path: Path, ignore_dirs: list[str]) -> bool:
    """Return True if path falls under any ignored directory."""
    path_str = str(path).replace("\\", "/")
    for pattern in ignore_dirs:
        norm = pattern.replace("\\", "/")
        # Absolute path (starts with drive letter or /) — prefix match
        if len(norm) >= 2 and norm[1] == ":" or norm.startswith("/"):
            if path_str.startswith(norm):
                return True
        # Multi-component relative pattern (e.g. AppData/Local/Temp) — substring
        elif "/" in norm:
            if norm in path_str:
                return True
        # Single component — match any path part exactly
        else:
            if norm in path.parts:
                return True
    return False


def _is_ignored_file(path: Path, patterns: list[str]) -> bool:
    name = path.name
    return any(fnmatch.fnmatch(name, pat) for pat in patterns)


def _stream_for(path: Path, vault: Path, streams: dict, stream_patterns: list[dict]) -> str | None:
    """
    Determine which stream a path belongs to.
    1. Check vault subdirs first (explicit stream mapping).
    2. Check stream_path_patterns (substring match on full path).
    3. Return None if untagged.
    """
    path_str = str(path)

    # 1. Vault subdir
    try:
        rel = path.relative_to(vault)
        top = rel.parts[0] if rel.parts else None
        if top and top in streams:
            return top
    except ValueError:
        pass

    # 2. Path pattern matching
    for entry in stream_patterns:
        if entry.get("path", "") in path_str:
            return entry.get("stream")

    return None


def _log_file(logs_dir: Path) -> Path:
    date_str = datetime.now().strftime("%Y-%m-%d")
    return logs_dir / f"file_events_{date_str}.jsonl"


def _append_event(logs_dir: Path, record: dict) -> None:
    log_path = _log_file(logs_dir)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    # v2 dual-write: best-effort SQLite atom alongside the JSONL.
    try:
        from workpulse.core.dual_write import dual_write_file_event
        dual_write_file_event(record)
    except Exception:
        pass


# ── debounce ──────────────────────────────────────────────────────────────────

class Debouncer:
    """Collapse rapid events on the same path into one."""

    def __init__(self, delay_ms: int, callback):
        self._delay = delay_ms / 1000.0
        self._callback = callback
        self._timers: dict[str, threading.Timer] = {}
        self._lock = threading.Lock()

    def trigger(self, path: str, event_type: str) -> None:
        with self._lock:
            existing = self._timers.pop(path, None)
            if existing:
                existing.cancel()
            t = threading.Timer(self._delay, self._callback, args=(path, event_type))
            self._timers[path] = t
            t.start()


# ── event handler ─────────────────────────────────────────────────────────────

class MachineHandler(FileSystemEventHandler):
    def __init__(self, vault: Path, logs_dir: Path, cfg: dict):
        super().__init__()
        self._vault = vault
        self._logs_dir = logs_dir
        self._ignore_dirs = cfg["watcher"]["ignore_dirs"]
        self._ignore_files = cfg["watcher"]["ignore_patterns"]
        self._streams = cfg.get("streams") or {}
        self._stream_patterns = cfg["watcher"].get("stream_path_patterns", [])
        self._debouncer = Debouncer(
            delay_ms=cfg["watcher"]["debounce_ms"],
            callback=self._emit,
        )

    def _should_skip(self, path: Path, is_dir: bool = False) -> bool:
        if _is_ignored_dir(path, self._ignore_dirs):
            return True
        if not is_dir and _is_ignored_file(path, self._ignore_files):
            return True
        return False

    def _on_event(self, event: FileSystemEvent, event_type: str) -> None:
        src = event.src_path
        # Strip NTFS \\?\ long-path prefix if watchdog emits it
        if src.startswith("\\\\?\\") or src.startswith("//?/"):
            src = src[4:]
        path = Path(src).resolve()
        if self._should_skip(path, is_dir=event.is_directory):
            return
        if event.is_directory:
            return  # only log file events
        self._debouncer.trigger(str(path), event_type)

    def on_created(self, event):
        self._on_event(event, "created")

    def on_modified(self, event):
        self._on_event(event, "modified")

    def on_deleted(self, event):
        self._on_event(event, "deleted")

    def on_moved(self, event):
        src = Path(event.src_path).resolve()
        dst = Path(event.dest_path).resolve()
        if not event.is_directory:
            if not self._should_skip(src):
                self._debouncer.trigger(str(src), "moved_from")
            if not self._should_skip(dst):
                self._debouncer.trigger(str(dst), "moved_to")

    def _emit(self, path_str: str, event_type: str) -> None:
        path = Path(path_str)
        stream = _stream_for(path, self._vault, self._streams, self._stream_patterns)

        try:
            size = path.stat().st_size if path.exists() else None
        except OSError:
            size = None

        record = {
            "timestamp": datetime.now(timezone.utc).isoformat(),
            "event_type": event_type,
            "path": path_str,
            "stream": stream,
            "size_bytes": size,
        }
        _append_event(self._logs_dir, record)
        log.info("[%s] %s  (stream=%s)", event_type, path.name, stream or "—")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="WorkPulse file watcher daemon")
    parser.add_argument("--once", action="store_true", help="Print config and exit (smoke test)")
    args = parser.parse_args()

    cfg = load_config()
    vault = resolve(cfg["paths"]["vault"])
    logs_dir = resolve(cfg["paths"]["logs"])
    ensure_dir(logs_dir)

    roots = _watch_roots(cfg["watcher"]["watch_roots"])

    log.info("WorkPulse file watcher starting")
    log.info("  watching : %s", [str(r) for r in roots])
    log.info("  vault    : %s", vault)
    log.info("  logs     : %s", logs_dir)
    log.info("  debounce : %dms", cfg["watcher"]["debounce_ms"])
    log.info("  ignore_dirs  : %d patterns", len(cfg["watcher"]["ignore_dirs"]))
    log.info("  ignore_files : %d patterns", len(cfg["watcher"]["ignore_patterns"]))

    if args.once:
        log.info("--once flag set, exiting.")
        return

    missing = [r for r in roots if not r.exists()]
    if missing:
        for m in missing:
            log.warning("Watch root does not exist, skipping: %s", m)
        roots = [r for r in roots if r.exists()]

    if not roots:
        log.error("No valid watch roots found. Exiting.")
        sys.exit(1)

    # Always exclude WorkPulse's own files so it never logs itself: the whole
    # install dir (code, .venv, logs, reports), plus the shared-package
    # extraction folder that the installer unzips next to it.
    workpulse_internal = [
        str(ROOT),            # the entire WorkPulse install directory
        "WorkPulse-pkg",      # installer's package-extraction folder (by name)
    ]
    cfg["watcher"]["ignore_dirs"] = workpulse_internal + cfg["watcher"]["ignore_dirs"]

    handler = MachineHandler(vault, logs_dir, cfg)
    observer = Observer()

    for root in roots:
        observer.schedule(handler, str(root), recursive=True)
        log.info("  scheduled: %s", root)

    observer.start()
    log.info("Watching %d root(s) ... (Ctrl-C to stop)", len(roots))

    try:
        while observer.is_alive():
            time.sleep(1)
    except KeyboardInterrupt:
        log.info("Interrupted — stopping watcher.")
    finally:
        observer.stop()
        observer.join()
        log.info("Watcher stopped.")


if __name__ == "__main__":
    main()
