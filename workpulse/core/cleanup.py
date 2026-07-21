"""
cleanup.py — one-shot data hygiene passes for the v2 SQLite store.

PLAN.md §7 step 7c — companion to the sensor-side lock-screen filter in
scripts/activity.py + scripts/activity_mac.py. Removes already-contaminated
data the dream cycle surfaced (73h of "loginwindow" sessions on the real DB).

Idempotent. Safe to re-run. Reports counts.

Public API:
    prune_lockscreen(con, *, dry_run=False) -> dict
    rebuild_search_and_clusters(con) -> None   # convenience: reindex+recluster

CLI:
    python -m workpulse.core.cleanup prune-lockscreen [--dry-run]
    python -m workpulse.core.cleanup rebuild
    python -m workpulse.core.cleanup full              # prune + rebuild + reconsolidate
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

from workpulse.core import cluster, db, search
from workpulse.common import load_config, resolve


# App names treated as "not real work" — matches activity_mac._LOCKSCREEN_APP_NAMES
# but case-insensitive at the SQL level via LOWER().
_LOCKSCREEN_APPS = (
    "loginwindow",
    "lock screen",
    "screensaverengine",
    "screensaver",
)

_DATED_SENSOR_LOG = re.compile(
    r"^(?:activity|file_events)_(\d{4}-\d{2}-\d{2})\.jsonl$"
)


def _count(con: sqlite3.Connection, sql: str, params: tuple = ()) -> int:
    return int(con.execute(sql, params).fetchone()[0])


def _prune_dated_sensor_logs(logs_dir: Path, cutoff: date, *,
                             dry_run: bool) -> tuple[int, int]:
    """Remove only date-named WorkPulse sensor logs older than ``cutoff``.

    Learned rules, health state, scheduler logs, and unknown files are never
    touched. Returns (files, bytes)."""
    files = bytes_removed = 0
    if not logs_dir.exists():
        return files, bytes_removed
    for path in logs_dir.iterdir():
        if not path.is_file():
            continue
        match = _DATED_SENSOR_LOG.match(path.name)
        if not match:
            continue
        try:
            log_date = date.fromisoformat(match.group(1))
        except ValueError:
            continue
        if log_date >= cutoff:
            continue
        files += 1
        try:
            bytes_removed += path.stat().st_size
        except OSError:
            pass
        if not dry_run:
            path.unlink(missing_ok=True)
    return files, bytes_removed


def _compact_ai_session_log(log_path: Path, cutoff: date, *,
                            dry_run: bool) -> tuple[int, int]:
    """Drop old JSONL AI-session records while retaining recent/unknown rows.

    The rewrite is atomic. Malformed rows are retained because retention must
    never turn uncertainty into deletion. Returns (rows, bytes saved)."""
    if not log_path.exists() or not log_path.is_file():
        return 0, 0
    original = log_path.read_bytes()
    kept: list[bytes] = []
    removed = 0
    for raw in original.splitlines(keepends=True):
        try:
            row = json.loads(raw.decode("utf-8"))
            stamp = str(row.get("ts") or row.get("timestamp") or "")[:10]
            old = bool(stamp) and date.fromisoformat(stamp) < cutoff
        except (UnicodeDecodeError, json.JSONDecodeError, ValueError, TypeError):
            old = False
        if old:
            removed += 1
        else:
            kept.append(raw)
    new = b"".join(kept)
    saved = max(0, len(original) - len(new))
    if removed and not dry_run:
        tmp = log_path.with_suffix(log_path.suffix + ".tmp")
        tmp.write_bytes(new)
        tmp.replace(log_path)
    return removed, saved


def apply_retention(con: sqlite3.Connection, *, cfg: dict | None = None,
                    now: datetime | None = None,
                    dry_run: bool = False) -> dict:
    """Apply the bounded raw-evidence policy without deleting durable memory.

    Old file events are discarded entirely. For sessions, browser visits, and
    calendar events, only the private raw projection is removed; their compact
    timestamp/app/domain/project markers remain. Old skill runs retain audit
    metadata and cost but lose potentially large input/output payloads.
    Captures, corrections, learned rules, reports, consolidations, and profile
    memory are deliberately outside this function.
    """
    cfg = cfg or load_config()
    days = int((cfg.get("retention") or {}).get("raw_days", 100))
    if days < 1:
        return {"error": "retention.raw_days must be at least 1",
                "raw_days": days, "dry_run": dry_run}
    now = now or datetime.now(timezone.utc)
    cutoff = (now.date() - timedelta(days=days)).isoformat()
    params = (cutoff,)
    counts = {
        "raw_days": days,
        "cutoff": cutoff,
        "file_events": _count(con, "SELECT COUNT(*) FROM file_event WHERE substr(ts,1,10) < ?", params),
        "session_details": _count(con, "SELECT COUNT(*) FROM session_local sl JOIN session s ON s.id=sl.session_id WHERE substr(s.started_at,1,10) < ?", params),
        "browser_details": _count(con, "SELECT COUNT(*) FROM browser_visit_local bl JOIN browser_visit b ON b.id=bl.visit_id WHERE substr(b.ts,1,10) < ?", params),
        "calendar_details": _count(con, "SELECT COUNT(*) FROM calendar_event_local cl JOIN calendar_event c ON c.id=cl.event_id WHERE substr(c.ended_at,1,10) < ?", params),
        "skill_payloads": _count(con, "SELECT COUNT(*) FROM skill_run WHERE substr(ts,1,10) < ? AND (input IS NOT NULL OR output IS NOT NULL)", params),
        "dry_run": dry_run,
    }

    logs_dir = resolve((cfg.get("paths") or {}).get("logs", "logs"))
    # Inventory the filesystem first, but do not mutate it until the database
    # transaction succeeds. This avoids half-applied retention on DB failure.
    cutoff_date = date.fromisoformat(cutoff)
    log_files, log_bytes = _prune_dated_sensor_logs(
        logs_dir, cutoff_date, dry_run=True)
    ai_rows, ai_bytes = _compact_ai_session_log(
        logs_dir / "ai_sessions.jsonl", cutoff_date, dry_run=True)
    counts.update({"log_files": log_files, "log_bytes": log_bytes,
                   "ai_log_rows": ai_rows, "ai_log_bytes": ai_bytes})

    if dry_run:
        return counts

    con.execute("BEGIN")
    try:
        con.execute("DELETE FROM file_event_local WHERE file_event_id IN (SELECT id FROM file_event WHERE substr(ts,1,10) < ?)", params)
        con.execute("DELETE FROM file_event WHERE substr(ts,1,10) < ?", params)
        con.execute("DELETE FROM session_local WHERE session_id IN (SELECT id FROM session WHERE substr(started_at,1,10) < ?)", params)
        con.execute("DELETE FROM browser_visit_local WHERE visit_id IN (SELECT id FROM browser_visit WHERE substr(ts,1,10) < ?)", params)
        con.execute("DELETE FROM calendar_event_local WHERE event_id IN (SELECT id FROM calendar_event WHERE substr(ended_at,1,10) < ?)", params)
        con.execute("UPDATE skill_run SET input=NULL, output=NULL WHERE substr(ts,1,10) < ?", params)
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    _prune_dated_sensor_logs(logs_dir, cutoff_date, dry_run=False)
    _compact_ai_session_log(logs_dir / "ai_sessions.jsonl", cutoff_date,
                            dry_run=False)
    return counts


def prune_lockscreen(con: sqlite3.Connection, *, dry_run: bool = False) -> dict:
    """DELETE every session (and its session_local, edges, FTS row, vec map
    entry) whose app is a known lock-screen process. Counts what would be
    removed under --dry-run."""
    placeholders = ",".join("?" for _ in _LOCKSCREEN_APPS)
    ids_q = (f"SELECT id FROM session WHERE LOWER(app) IN ({placeholders})")
    rows = con.execute(ids_q, _LOCKSCREEN_APPS).fetchall()
    session_ids = [r["id"] for r in rows]
    counts = {"sessions": len(session_ids), "edges": 0, "fts_rows": 0,
              "vec_rows": 0, "session_local": 0, "dry_run": dry_run}

    if dry_run or not session_ids:
        # Still report what cascades would have hit
        if session_ids:
            qmarks = ",".join("?" * len(session_ids))
            counts["edges"] = con.execute(
                f"SELECT COUNT(*) AS n FROM edge "
                f"WHERE src_kind='session' AND src_id IN ({qmarks})",
                session_ids,
            ).fetchone()["n"]
            counts["session_local"] = con.execute(
                f"SELECT COUNT(*) AS n FROM session_local "
                f"WHERE session_id IN ({qmarks})",
                session_ids,
            ).fetchone()["n"]
            counts["fts_rows"] = con.execute(
                f"SELECT COUNT(*) AS n FROM search_fts "
                f"WHERE atom_kind='session' AND atom_id IN ({qmarks})",
                session_ids,
            ).fetchone()["n"]
        return counts

    con.execute("BEGIN")
    try:
        qmarks = ",".join("?" * len(session_ids))
        # Edges first (no FK cascade since edge is polymorphic)
        cur = con.execute(
            f"DELETE FROM edge "
            f"WHERE src_kind='session' AND src_id IN ({qmarks})",
            session_ids,
        )
        counts["edges"] = cur.rowcount
        # FTS rows
        cur = con.execute(
            f"DELETE FROM search_fts "
            f"WHERE atom_kind='session' AND atom_id IN ({qmarks})",
            session_ids,
        )
        counts["fts_rows"] = cur.rowcount
        # session_local cascades from session, but be explicit for clarity
        cur = con.execute(
            f"DELETE FROM session_local WHERE session_id IN ({qmarks})",
            session_ids,
        )
        counts["session_local"] = cur.rowcount
        # And the sessions themselves
        cur = con.execute(
            f"DELETE FROM session WHERE id IN ({qmarks})",
            session_ids,
        )
        counts["sessions"] = cur.rowcount
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return counts


def prune_untracked_file_events(con: sqlite3.Connection, *,
                                cfg: dict | None = None,
                                dry_run: bool = False) -> dict:
    """DELETE file_events whose raw_path is not under any configured
    watcher.watch_roots. file_event_local cascades via ON DELETE CASCADE; no
    edges or FTS rows reference file_events.

    This clears the churn a v1 migration drags in: v1 watched all of ~/Library
    (caches, app state, containers), which v2's watch_roots deliberately exclude.
    On the pilot Mac that was 2.72M of 2.74M rows. Keeps every event under a
    root WorkPulse actually watches. Idempotent; --dry-run reports only.

    Refuses to run if watch_roots is empty (would delete everything)."""
    cfg = cfg or load_config()
    roots = (cfg.get("watcher") or {}).get("watch_roots") or []
    prefixes = [os.path.expanduser(r).rstrip("/").lower() + "/" for r in roots if r]
    counts = {"deleted": 0, "kept": 0, "dry_run": dry_run}
    if not prefixes:
        counts["error"] = "no watch_roots configured; refusing to prune everything"
        return counts

    keep = " OR ".join("LOWER(fel.raw_path) LIKE ?" for _ in prefixes)
    params = [p + "%" for p in prefixes]
    doomed = (f"SELECT fe.id FROM file_event fe "
              f"JOIN file_event_local fel ON fel.file_event_id = fe.id "
              f"WHERE NOT ({keep})")

    total = con.execute("SELECT COUNT(*) FROM file_event").fetchone()[0]
    counts["deleted"] = con.execute(
        f"SELECT COUNT(*) FROM ({doomed})", params).fetchone()[0]
    counts["kept"] = total - counts["deleted"]
    if dry_run or not counts["deleted"]:
        return counts

    con.execute("BEGIN")
    try:
        con.execute("CREATE TEMP TABLE IF NOT EXISTS _doomed_fe (id TEXT PRIMARY KEY)")
        con.execute("DELETE FROM _doomed_fe")
        con.execute(f"INSERT INTO _doomed_fe(id) {doomed}", params)
        # Delete local rows explicitly (fast), then the events. CASCADE would
        # also clear local, but being explicit avoids relying on the pragma.
        con.execute("DELETE FROM file_event_local "
                    "WHERE file_event_id IN (SELECT id FROM _doomed_fe)")
        con.execute("DELETE FROM file_event "
                    "WHERE id IN (SELECT id FROM _doomed_fe)")
        con.execute("DROP TABLE _doomed_fe")
        con.execute("COMMIT")
    except Exception:
        _safe_exec(con, "ROLLBACK")
        raise
    return counts


def _safe_exec(con: sqlite3.Connection, sql: str) -> None:
    try:
        con.execute(sql)
    except Exception:
        pass


def rebuild_search_and_clusters(con: sqlite3.Connection) -> dict:
    """After pruning, the FTS index is consistent but clusters reference
    stale cluster_ids. Re-run search.reindex() (cheap; FTS is small) and
    cluster.refresh() (re-derives job_view + cluster_name carries forward
    via stable content-hash ids)."""
    s_counts = search.reindex(con)
    c_counts = cluster.refresh(con)
    return {"search_indexed": sum(s_counts.values()),
            "clusters": c_counts.get("clusters", 0),
            "sessions_assigned": c_counts.get("sessions_assigned", 0)}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_prune(args: argparse.Namespace) -> int:
    con = db.connect(load_config())
    counts = prune_lockscreen(con, dry_run=args.dry_run)
    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}prune-lockscreen:")
    for k, v in counts.items():
        if k == "dry_run":
            continue
        print(f"  {k:18s} {v}")
    return 0


def _cli_prune_files(args: argparse.Namespace) -> int:
    con = db.connect(load_config())
    counts = prune_untracked_file_events(con, dry_run=args.dry_run)
    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}prune-untracked file events:")
    for k, v in counts.items():
        if k == "dry_run":
            continue
        print(f"  {k:18s} {v:,}" if isinstance(v, int) else f"  {k:18s} {v}")
    return 0


def _cli_rebuild() -> int:
    con = db.connect(load_config())
    counts = rebuild_search_and_clusters(con)
    print("rebuild:")
    for k, v in counts.items():
        print(f"  {k:20s} {v}")
    return 0


def _cli_retention(args: argparse.Namespace) -> int:
    con = db.connect(load_config())
    counts = apply_retention(con, dry_run=args.dry_run)
    tag = "DRY-RUN " if args.dry_run else ""
    print(f"{tag}retention:")
    for k, v in counts.items():
        if k == "dry_run":
            continue
        print(f"  {k:20s} {v:,}" if isinstance(v, int) else f"  {k:20s} {v}")
    return 1 if "error" in counts else 0


def _cli_full() -> int:
    con = db.connect(load_config())
    prune = prune_lockscreen(con)
    print(f"prune: deleted {prune['sessions']} sessions, "
          f"{prune['edges']} edges, {prune['fts_rows']} FTS rows")
    rebuild = rebuild_search_and_clusters(con)
    print(f"rebuild: indexed {rebuild['search_indexed']} atoms, "
          f"{rebuild['clusters']} clusters from "
          f"{rebuild['sessions_assigned']} sessions")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp cleanup",
                                     description="Data hygiene passes.")
    sub = parser.add_subparsers(dest="cmd")
    p = sub.add_parser("prune-lockscreen")
    p.add_argument("--dry-run", action="store_true")
    pf = sub.add_parser("prune-untracked",
                        help="drop file events outside the configured watch_roots")
    pf.add_argument("--dry-run", action="store_true")
    pr = sub.add_parser("retention",
                        help="remove raw evidence older than retention.raw_days")
    pr.add_argument("--dry-run", action="store_true")
    sub.add_parser("rebuild")
    sub.add_parser("full",
                   help="prune-lockscreen + rebuild search + recluster")
    args = parser.parse_args(argv[1:])
    if args.cmd == "prune-lockscreen":
        return _cli_prune(args)
    if args.cmd == "prune-untracked":
        return _cli_prune_files(args)
    if args.cmd == "retention":
        return _cli_retention(args)
    if args.cmd == "rebuild":
        return _cli_rebuild()
    if args.cmd == "full" or args.cmd is None:
        return _cli_full()
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
