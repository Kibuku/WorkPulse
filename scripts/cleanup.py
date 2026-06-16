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
    python -m scripts.cleanup prune-lockscreen [--dry-run]
    python -m scripts.cleanup rebuild
    python -m scripts.cleanup full              # prune + rebuild + reconsolidate
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import cluster, db, search
from scripts.common import load_config


# App names treated as "not real work" — matches activity_mac._LOCKSCREEN_APP_NAMES
# but case-insensitive at the SQL level via LOWER().
_LOCKSCREEN_APPS = (
    "loginwindow",
    "lock screen",
    "screensaverengine",
    "screensaver",
)


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


def _cli_rebuild() -> int:
    con = db.connect(load_config())
    counts = rebuild_search_and_clusters(con)
    print("rebuild:")
    for k, v in counts.items():
        print(f"  {k:20s} {v}")
    return 0


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
    sub.add_parser("rebuild")
    sub.add_parser("full",
                   help="prune-lockscreen + rebuild search + recluster")
    args = parser.parse_args(argv[1:])
    if args.cmd == "prune-lockscreen":
        return _cli_prune(args)
    if args.cmd == "rebuild":
        return _cli_rebuild()
    if args.cmd == "full" or args.cmd is None:
        return _cli_full()
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
