"""
atoms.py — write-side helpers for the four atom types.

This is the only module that should know how to insert into ``session``,
``file_event``, ``ai_call``, and ``capture`` (plus their private projections
and the typed edges that fall out at write time).

Every write extracts typed edges synchronously, zero LLM calls. The graph is
self-wiring per principle #6.

Public API:
    new_id()                            -> str (ULID-shaped)
    sha256_short(text)                  -> str
    write_session(con, *, app, title, stream=None, raw_path=None,
                  raw_files=None, started_at=None) -> str (session id)
    close_session(con, session_id, *, ended_at=None) -> None
    write_file_event(con, *, raw_path, kind, ts=None) -> str (file_event id)
    write_ai_call(con, *, provider, model, in_tokens, out_tokens,
                  cost_usd, prompt_slug=None, ts=None) -> str (ai_call id)
    write_capture(con, *, body, author='human', pinned_kind=None,
                  pinned_id=None, ts=None) -> str (capture id)
    add_edge(con, *, src_kind, src_id, rel, dst_kind, dst_id) -> None
"""

from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from datetime import datetime, timezone


# ── identity & hashing ───────────────────────────────────────────────────────

_ULID_ALPHABET = "0123456789ABCDEFGHJKMNPQRSTVWXYZ"  # Crockford


def new_id() -> str:
    """Compact, sortable, opaque id. Not a strict ULID — close enough.
    First 10 chars encode milliseconds since epoch, remaining 16 random."""
    ms = int(time.time() * 1000)
    time_part = ""
    for _ in range(10):
        time_part = _ULID_ALPHABET[ms & 0x1F] + time_part
        ms >>= 5
    rand_part = "".join(secrets.choice(_ULID_ALPHABET) for _ in range(16))
    return time_part + rand_part


def sha256_short(text: str) -> str:
    """Short, stable hash for de-identifying titles/paths."""
    return hashlib.sha256(text.encode("utf-8")).hexdigest()[:16]


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── title normalization (mirrors learning.normalize_title) ───────────────────
# Kept local to avoid a circular import. Cheap and stable.

_NORM_PATTERNS = [
    re.compile(r"^\s*\(\d+\)\s*"),
    re.compile(r"\s*and \d+ more pages?\s*", re.I),
    re.compile(r"\s*\[(protected view|read-only|compatibility mode)\]\s*", re.I),
    re.compile(r"\s*-\s*(brave|google chrome|microsoft\W*edge|mozilla firefox|firefox|opera|vivaldi)\s*$", re.I),
    re.compile(r"\s*-\s*personal(\s*-\s*microsoft.*)?$", re.I),
    re.compile(r"\s*—\s*personal(\s*—\s*microsoft.*)?$", re.I),
]


def _normalize_title(raw: str) -> str:
    if not raw:
        return ""
    t = raw
    for p in _NORM_PATTERNS:
        t = p.sub(" ", t)
    return re.sub(r"\s+", " ", t).strip().lower()


# ── reference-table upserts (idempotent, cheap) ──────────────────────────────

def _upsert_app(con: sqlite3.Connection, name: str, ts: str) -> None:
    con.execute(
        """
        INSERT INTO app(name, category, first_seen, last_seen) VALUES (?, NULL, ?, ?)
        ON CONFLICT(name) DO UPDATE SET last_seen = excluded.last_seen
        """,
        (name, ts, ts),
    )


def _ensure_stream(con: sqlite3.Connection, key: str | None) -> None:
    if key is None:
        return
    con.execute(
        "INSERT OR IGNORE INTO stream(key, label, parent_key) VALUES (?, ?, NULL)",
        (key, key),
    )


# ── edges ────────────────────────────────────────────────────────────────────

def add_edge(
    con: sqlite3.Connection,
    *,
    src_kind: str,
    src_id: str,
    rel: str,
    dst_kind: str,
    dst_id: str,
) -> None:
    """Idempotent (unique index on the 5-tuple)."""
    con.execute(
        """
        INSERT OR IGNORE INTO edge(src_kind, src_id, rel, dst_kind, dst_id, created_at)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (src_kind, src_id, rel, dst_kind, dst_id, _now_iso()),
    )


# ── session ──────────────────────────────────────────────────────────────────

def write_session(
    con: sqlite3.Connection,
    *,
    app: str,
    title: str,
    stream: str | None = None,
    raw_path: str | None = None,
    raw_files: list[str] | None = None,
    started_at: str | None = None,
) -> str:
    """Insert a session atom + its private projection + its typed edges.

    The public ``session`` row holds only the de-identified projection
    (title_hash, stream key, app name). Raw title / path / files land in
    ``session_local`` and never leave the machine.
    """
    sid = new_id()
    ts = started_at or _now_iso()
    norm = _normalize_title(title)
    title_hash = sha256_short(norm) if norm else sha256_short("__empty__")

    con.execute("BEGIN")
    try:
        _upsert_app(con, app, ts)
        _ensure_stream(con, stream)
        con.execute(
            """
            INSERT INTO session(id, started_at, ended_at, app, title_hash, stream, cluster_id)
            VALUES (?, ?, NULL, ?, ?, ?, NULL)
            """,
            (sid, ts, app, title_hash, stream),
        )
        con.execute(
            """
            INSERT INTO session_local(session_id, raw_title, raw_path, raw_files)
            VALUES (?, ?, ?, ?)
            """,
            (sid, title, raw_path, json.dumps(raw_files or [])),
        )
        # Typed edges, zero LLM
        add_edge(con, src_kind="session", src_id=sid, rel="in_app",
                 dst_kind="app", dst_id=app)
        if stream:
            add_edge(con, src_kind="session", src_id=sid, rel="in_stream",
                     dst_kind="stream", dst_id=stream)
        for f in (raw_files or []):
            fh = sha256_short(f)
            add_edge(con, src_kind="session", src_id=sid, rel="touched_file",
                     dst_kind="file", dst_id=fh)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return sid


def close_session(
    con: sqlite3.Connection,
    session_id: str,
    *,
    ended_at: str | None = None,
) -> None:
    con.execute(
        "UPDATE session SET ended_at = ? WHERE id = ? AND ended_at IS NULL",
        (ended_at or _now_iso(), session_id),
    )


# ── file_event ───────────────────────────────────────────────────────────────

def write_file_event(
    con: sqlite3.Connection,
    *,
    raw_path: str,
    kind: str,
    ts: str | None = None,
) -> str:
    fid = new_id()
    ts = ts or _now_iso()
    path_hash = sha256_short(os.path.normpath(raw_path))
    con.execute("BEGIN")
    try:
        con.execute(
            "INSERT INTO file_event(id, ts, path_hash, kind) VALUES (?, ?, ?, ?)",
            (fid, ts, path_hash, kind),
        )
        con.execute(
            "INSERT INTO file_event_local(file_event_id, raw_path) VALUES (?, ?)",
            (fid, raw_path),
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return fid


# ── ai_call ──────────────────────────────────────────────────────────────────

def write_ai_call(
    con: sqlite3.Connection,
    *,
    provider: str,
    model: str,
    in_tokens: int,
    out_tokens: int,
    cost_usd: float,
    prompt_slug: str | None = None,
    ts: str | None = None,
) -> str:
    cid = new_id()
    con.execute(
        """
        INSERT INTO ai_call(id, ts, provider, model, in_tokens, out_tokens, cost_usd, prompt_slug)
        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
        """,
        (cid, ts or _now_iso(), provider, model, in_tokens, out_tokens, cost_usd, prompt_slug),
    )
    return cid


# ── capture ──────────────────────────────────────────────────────────────────

def write_capture(
    con: sqlite3.Connection,
    *,
    body: str,
    author: str = "human",
    pinned_kind: str | None = None,
    pinned_id: str | None = None,
    ts: str | None = None,
) -> str:
    if author not in ("human", "system"):
        raise ValueError(f"author must be 'human' or 'system', got {author!r}")
    if (pinned_kind is None) ^ (pinned_id is None):
        raise ValueError("pinned_kind and pinned_id must be set together or not at all")
    cid = new_id()
    con.execute("BEGIN")
    try:
        con.execute(
            """
            INSERT INTO capture(id, ts, author, body, pinned_kind, pinned_id)
            VALUES (?, ?, ?, ?, ?, ?)
            """,
            (cid, ts or _now_iso(), author, body, pinned_kind, pinned_id),
        )
        if pinned_kind and pinned_id:
            add_edge(con, src_kind="capture", src_id=cid, rel="about",
                     dst_kind=pinned_kind, dst_id=pinned_id)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return cid
