"""
dual_write.py — sensor hot-path → v2 SQLite atoms.

Per PLAN.md §7 step 3. Each v1 sensor (activity, watcher, ai_logger) keeps
writing its JSONL exactly as before; right after the JSONL append it calls
into this module, which best-effort writes the same data as a v2 atom.

Three guarantees this module must keep:
  1. Never raise into the sensor hot path. A failed DB write is logged and
     swallowed — the JSONL is still the source of truth this release.
  2. ID semantics match backfill.py so re-running backfill against today's
     logs after dual-write produces zero duplicates. Both paths use the
     same `_stable_id` shape.
  3. Atoms land in the institutional/private split — raw titles and paths
     go to *_local tables, the public tables only see hashes.

Connection lifecycle:
    A single module-level connection in WAL mode, lazily opened on first
    use, guarded by a threading lock. SQLite WAL handles concurrent
    sensors (each is a separate Python process); within a process the lock
    serializes our writers.
"""

from __future__ import annotations

import hashlib
import json
import logging
import os
import sqlite3
import sys
import threading
from datetime import datetime, timezone
from pathlib import Path

from workpulse.core import atoms, db

log = logging.getLogger("dual_write")

_LOCK = threading.RLock()
_CON: sqlite3.Connection | None = None
_DISABLED = False  # if True, skip silently (set on first hard failure)


def _stable_id(*parts: str) -> str:
    h = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()
    return h[:20].upper()


def _con(cfg: dict | None = None) -> sqlite3.Connection | None:
    """Return a process-wide writer connection, or None if disabled."""
    global _CON, _DISABLED
    if _DISABLED:
        return None
    with _LOCK:
        if _CON is not None:
            return _CON
        try:
            _CON = db.connect(cfg)
            # Sensors run hot; make sqlite a touch more forgiving.
            _CON.execute("PRAGMA busy_timeout = 2000")
            return _CON
        except Exception as e:
            log.warning("dual_write: disabling, can't open DB: %r", e)
            _DISABLED = True
            return None


def disable() -> None:
    """Tests or operators can force-disable dual-write."""
    global _DISABLED, _CON
    with _LOCK:
        _DISABLED = True
        if _CON is not None:
            try:
                _CON.close()
            except Exception:
                pass
            _CON = None


def _safe_rollback(con: sqlite3.Connection) -> None:
    try:
        con.rollback()
    except Exception:
        pass


def _begin(con: sqlite3.Connection) -> None:
    """Start a transaction, first clearing any left dangling by a prior failed
    commit/rollback. Without this, one wedged transaction makes every later
    BEGIN throw 'cannot start a transaction within a transaction'."""
    if con.in_transaction:
        _safe_rollback(con)
    con.execute("BEGIN")


_FAILS = 0


def _note_failure(what: str, e: Exception) -> None:
    """Surface write failures instead of swallowing them at debug. Loud on the
    first and every 100th, so a persistent problem shows up in the sensor log
    (and gets caught by the doctor) rather than freezing a sensor unnoticed."""
    global _FAILS
    _FAILS += 1
    if _FAILS == 1 or _FAILS % 100 == 0:
        log.warning("dual_write %s failed (#%d): %r", what, _FAILS, e)
    else:
        log.debug("dual_write %s failed: %r", what, e)


def _reset_con() -> None:
    """Drop the cached connection so the next write reopens a fresh one. A single
    lock during a COMMIT/ROLLBACK could otherwise leave this process-wide
    connection stuck in an open transaction, wedging every later BEGIN for the
    life of the process. That silently froze the file watcher's writes for ~31h
    while it kept detecting events. Reopening next call makes it self-healing."""
    global _CON
    with _LOCK:
        if _CON is not None:
            try:
                _CON.close()
            except Exception:
                pass
            _CON = None


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── session (called from activity.py after JSONL append) ────────────────────

def dual_write_session(rec: dict, cfg: dict | None = None) -> None:
    """rec is the v1 activity record shape:
       {start, end, duration_s, app, exe_path, title, stream, idle}.
    Idle sessions are still written — they carry information about AFK gaps.
    """
    try:
        con = _con(cfg)
        if con is None:
            return
        start = rec.get("start")
        end = rec.get("end")
        app = rec.get("app") or "unknown"
        title = rec.get("title") or ""
        stream = rec.get("stream")
        exe_path = rec.get("exe_path")
        if not start:
            return
        sid = _stable_id("session", start, app, title)
        ts = start
        norm = atoms._normalize_title(title)
        title_hash = atoms.sha256_short(norm or "__empty__")

        with _LOCK:
            _begin(con)
            try:
                con.execute(
                    "INSERT OR IGNORE INTO app(name, category, first_seen, last_seen) VALUES (?, NULL, ?, ?)",
                    (app, ts, ts),
                )
                con.execute(
                    "UPDATE app SET last_seen = ? WHERE name = ? AND last_seen < ?",
                    (ts, app, ts),
                )
                if stream:
                    con.execute(
                        "INSERT OR IGNORE INTO stream(key, label, parent_key) VALUES (?, ?, NULL)",
                        (stream, stream),
                    )
                cur = con.execute(
                    """
                    INSERT OR IGNORE INTO session
                      (id, started_at, ended_at, app, title_hash, stream, cluster_id)
                    VALUES (?, ?, ?, ?, ?, ?, NULL)
                    """,
                    (sid, start, end, app, title_hash, stream),
                )
                if cur.rowcount:
                    con.execute(
                        """
                        INSERT OR IGNORE INTO session_local
                          (session_id, raw_title, raw_path, raw_files)
                        VALUES (?, ?, ?, ?)
                        """,
                        (sid, title, exe_path, json.dumps([])),
                    )
                    _edge(con, "session", sid, "in_app", "app", app)
                    if stream:
                        _edge(con, "session", sid, "in_stream", "stream", stream)
                con.execute("COMMIT")
            except Exception:
                _safe_rollback(con)
                raise
    except Exception as e:
        _note_failure("session", e)
        _reset_con()


# ── file_event (called from watcher.py after JSONL append) ───────────────────

def dual_write_file_event(rec: dict, cfg: dict | None = None) -> None:
    """rec shape: {timestamp, event_type, path, stream, size_bytes}.
    v1 event_type vocabulary includes moved_from/moved_to which the v2 schema
    collapses to 'moved'."""
    try:
        con = _con(cfg)
        if con is None:
            return
        ts = rec.get("timestamp")
        kind = rec.get("event_type")
        path = rec.get("path")
        if not ts or not kind or not path:
            return
        if kind in ("moved_from", "moved_to"):
            kind = "moved"
        if kind == "created_or_modified":
            kind = "modified"
        if kind not in ("created", "modified", "deleted", "moved"):
            return
        fid = _stable_id("file_event", ts, kind, path)
        path_hash = atoms.sha256_short(os.path.normpath(path))
        with _LOCK:
            _begin(con)
            try:
                cur = con.execute(
                    "INSERT OR IGNORE INTO file_event(id, ts, path_hash, kind) VALUES (?, ?, ?, ?)",
                    (fid, ts, path_hash, kind),
                )
                if cur.rowcount:
                    con.execute(
                        "INSERT OR IGNORE INTO file_event_local(file_event_id, raw_path) VALUES (?, ?)",
                        (fid, path),
                    )
                con.execute("COMMIT")
            except Exception:
                _safe_rollback(con)
                raise
    except Exception as e:
        _note_failure("file_event", e)
        _reset_con()


# ── ai_call (called from ai_logger.py after JSONL append) ────────────────────

def dual_write_ai_call(rec: dict, cfg: dict | None = None) -> None:
    """rec shape: {timestamp, session_id, stream, task_summary, tool_used,
                   model, input_tokens, output_tokens, estimated_cost_usd,
                   duration_minutes}."""
    try:
        con = _con(cfg)
        if con is None:
            return
        ts = rec.get("timestamp")
        if not ts:
            return
        provider = "anthropic"
        model = rec.get("model") or "unknown"
        in_tok = int(rec.get("input_tokens") or 0)
        out_tok = int(rec.get("output_tokens") or 0)
        cost = float(rec.get("estimated_cost_usd") or 0.0)
        slug = rec.get("tool_used") or rec.get("task_summary") or None
        sid = rec.get("session_id") or _stable_id("ai_call_seed", ts, slug or "")
        cid = _stable_id("ai_call", sid, ts, model)
        with _LOCK:
            con.execute(
                """
                INSERT OR IGNORE INTO ai_call
                  (id, ts, provider, model, in_tokens, out_tokens, cost_usd, prompt_slug)
                VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                """,
                (cid, ts, provider, model, in_tok, out_tok, cost, slug),
            )
    except Exception as e:
        _note_failure("ai_call", e)
        _reset_con()


# ── internal ─────────────────────────────────────────────────────────────────

def _edge(con: sqlite3.Connection, src_kind: str, src_id: str, rel: str,
          dst_kind: str, dst_id: str) -> None:
    con.execute(
        """
        INSERT OR IGNORE INTO edge(src_kind, src_id, rel, dst_kind, dst_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (src_kind, src_id, rel, dst_kind, dst_id, _now_iso()),
    )
