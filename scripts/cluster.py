"""
cluster.py — deterministic session-clustering, parameters from skills/cluster.md.

Per PLAN.md §7 step 7. A Job in v2 is not a unit of storage — it is a cluster
of sessions, derived. This module computes those clusters and materializes
them into the `job_view` table.

The clustering rules live in `skills/cluster.md` as YAML frontmatter +
English rationale. Editing English changes behavior (markdown is code,
principle #5).

Refresh semantics: idempotent. Each `refresh()` re-clusters from scratch.
Clustering is cheap; incremental updates are subtle (one new session can
merge two existing clusters), so we always do the whole thing.

Public API:
    load_params(skill_path=None) -> dict
    refresh(con, *, params=None, cfg=None) -> dict          # cluster counts
    jobs_for_stream(con, stream, *, since=None) -> list[dict]

CLI:
    python -m scripts.cluster refresh                 # full re-cluster
    python -m scripts.cluster status                  # counts + sample
    python -m scripts.cluster params                  # dump live params
"""

from __future__ import annotations

import argparse
import hashlib
import json
import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

import yaml

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts import db
from scripts.common import ROOT, load_config


_SKILL_PATH = ROOT / "skills" / "cluster.md"

_DEFAULTS = {
    "max_gap_minutes":     30,
    "min_cluster_seconds": 60,
    "respect_stream":      True,
    "cluster_untagged":    True,
    "ignore_idle":         False,
}


# ── skill parsing ────────────────────────────────────────────────────────────

_FRONTMATTER_RE = re.compile(r"^---\n(.*?\n)---\n", re.DOTALL)


def load_params(skill_path: Path | None = None) -> dict:
    """Read parameters from skills/cluster.md's YAML frontmatter. Missing
    keys fall back to _DEFAULTS so an edit error never crashes the run."""
    path = skill_path or _SKILL_PATH
    out = dict(_DEFAULTS)
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return out
    m = _FRONTMATTER_RE.match(text)
    if not m:
        return out
    try:
        loaded = yaml.safe_load(m.group(1)) or {}
    except yaml.YAMLError:
        return out
    if isinstance(loaded, dict):
        for k, v in loaded.items():
            if k in _DEFAULTS:
                out[k] = v
    return out


# ── cluster id ───────────────────────────────────────────────────────────────

def _cluster_id(stream: str | None, first_session_id: str) -> str:
    """Deterministic 20-char id. Stable across re-runs as long as the same
    session leads the same cluster."""
    key = f"{stream or '__untagged__'}\x1f{first_session_id}"
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:20].upper()


# ── time helpers ─────────────────────────────────────────────────────────────

def _parse_iso(ts: str | None) -> datetime | None:
    if not ts:
        return None
    try:
        t = datetime.fromisoformat(ts.replace("Z", "+00:00"))
    except ValueError:
        return None
    if t.tzinfo is None:
        t = t.replace(tzinfo=timezone.utc)
    return t


def _now_iso() -> str:
    return datetime.now(timezone.utc).isoformat()


# ── core algorithm ───────────────────────────────────────────────────────────

def refresh(con: sqlite3.Connection, *, params: dict | None = None,
            cfg: dict | None = None) -> dict:
    """Re-cluster all sessions from scratch and rewrite job_view.

    Returns a summary dict:
        {
          "params":     dict (the params actually used),
          "clusters":   int (rows written to job_view),
          "sessions_assigned":  int,
          "untagged_clusters":  int (subset of clusters),
          "dropped_short":      int (clusters dropped for being too short),
        }
    """
    p = params or load_params()
    max_gap_s = float(p["max_gap_minutes"]) * 60.0
    min_secs  = float(p["min_cluster_seconds"])
    respect_stream  = bool(p["respect_stream"])
    cluster_untagged = bool(p["cluster_untagged"])

    counts = {
        "clusters": 0,
        "sessions_assigned": 0,
        "untagged_clusters": 0,
        "dropped_short": 0,
    }

    # Stream key set: every distinct stream in `session` + None if enabled.
    streams = [r["s"] for r in con.execute(
        "SELECT DISTINCT stream AS s FROM session ORDER BY s IS NULL, s"
    )]
    if not cluster_untagged:
        streams = [s for s in streams if s is not None]
    # If respect_stream is False, treat everything as one global stream.
    if not respect_stream:
        streams = [None]  # sentinel; we'll fetch sessions without a WHERE on stream

    con.execute("BEGIN")
    try:
        # Wipe both: cluster_id on session, and the job_view table.
        con.execute("UPDATE session SET cluster_id = NULL")
        con.execute("DELETE FROM job_view")

        now = _now_iso()

        for stream_key in streams:
            if respect_stream:
                if stream_key is None:
                    rows = con.execute(
                        "SELECT id, started_at, ended_at, app FROM session "
                        "WHERE stream IS NULL ORDER BY started_at"
                    ).fetchall()
                else:
                    rows = con.execute(
                        "SELECT id, started_at, ended_at, app FROM session "
                        "WHERE stream = ? ORDER BY started_at",
                        (stream_key,),
                    ).fetchall()
            else:
                rows = con.execute(
                    "SELECT id, started_at, ended_at, app, stream "
                    "FROM session ORDER BY started_at"
                ).fetchall()

            # Walk and build clusters
            cluster_first_id: str | None = None
            cluster_started: datetime | None = None
            cluster_ended: datetime | None = None
            cluster_session_ids: list[str] = []
            cluster_apps: set[str] = set()
            cluster_total_s: float = 0.0
            prev_end: datetime | None = None
            current_stream = stream_key

            def _flush():
                nonlocal cluster_first_id, cluster_started, cluster_ended
                nonlocal cluster_session_ids, cluster_apps, cluster_total_s
                if cluster_first_id is None or not cluster_session_ids:
                    return
                cid = _cluster_id(current_stream if respect_stream else None,
                                  cluster_first_id)
                if cluster_total_s < min_secs:
                    counts["dropped_short"] += 1
                else:
                    # Assign to sessions
                    qmarks = ",".join("?" * len(cluster_session_ids))
                    con.execute(
                        f"UPDATE session SET cluster_id = ? WHERE id IN ({qmarks})",
                        [cid, *cluster_session_ids],
                    )
                    con.execute(
                        """
                        INSERT INTO job_view
                          (cluster_id, stream, started_at, ended_at,
                           session_count, total_seconds, apps_seen, computed_at)
                        VALUES (?, ?, ?, ?, ?, ?, ?, ?)
                        """,
                        (cid,
                         current_stream if respect_stream else None,
                         cluster_started.isoformat() if cluster_started else "",
                         cluster_ended.isoformat()   if cluster_ended   else "",
                         len(cluster_session_ids),
                         round(cluster_total_s, 1),
                         json.dumps(sorted(cluster_apps)),
                         now),
                    )
                    counts["clusters"] += 1
                    counts["sessions_assigned"] += len(cluster_session_ids)
                    if current_stream is None:
                        counts["untagged_clusters"] += 1
                # reset
                cluster_first_id = None
                cluster_started = None
                cluster_ended = None
                cluster_session_ids = []
                cluster_apps = set()
                cluster_total_s = 0.0

            for r in rows:
                s = _parse_iso(r["started_at"])
                e = _parse_iso(r["ended_at"]) or s
                if s is None:
                    continue
                if not respect_stream:
                    current_stream = r["stream"]
                gap_s = (s - prev_end).total_seconds() if prev_end else 0.0
                if cluster_first_id is None or gap_s > max_gap_s:
                    _flush()
                    cluster_first_id = r["id"]
                    cluster_started = s
                cluster_session_ids.append(r["id"])
                cluster_apps.add(r["app"])
                cluster_ended = e
                if e and s:
                    cluster_total_s += max(0.0, (e - s).total_seconds())
                prev_end = e
            _flush()

        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    counts["params"] = p
    return counts


# ── read API ─────────────────────────────────────────────────────────────────

def jobs_for_stream(con: sqlite3.Connection, stream: str | None, *,
                    since: str | None = None) -> list[dict]:
    sql = "SELECT * FROM job_view WHERE "
    params: list = []
    if stream is None:
        sql += "stream IS NULL"
    else:
        sql += "stream = ?"
        params.append(stream)
    if since:
        sql += " AND ended_at >= ?"
        params.append(since)
    sql += " ORDER BY started_at DESC"
    return [dict(r) for r in con.execute(sql, params)]


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_refresh() -> int:
    con = db.connect(load_config())
    summary = refresh(con)
    print("cluster refresh complete")
    print(f"  params (from skills/cluster.md):")
    for k, v in summary["params"].items():
        print(f"    {k:22s} {v}")
    print(f"  clusters written       {summary['clusters']:>6d}")
    print(f"  sessions assigned      {summary['sessions_assigned']:>6d}")
    print(f"  untagged clusters      {summary['untagged_clusters']:>6d}")
    print(f"  dropped (too short)    {summary['dropped_short']:>6d}")
    return 0


def _cli_status() -> int:
    con = db.connect(load_config())
    n_jobs = con.execute("SELECT COUNT(*) AS n FROM job_view").fetchone()["n"]
    n_assigned = con.execute(
        "SELECT COUNT(*) AS n FROM session WHERE cluster_id IS NOT NULL"
    ).fetchone()["n"]
    n_total = con.execute("SELECT COUNT(*) AS n FROM session").fetchone()["n"]
    print(f"job_view rows:           {n_jobs}")
    print(f"sessions assigned:       {n_assigned} / {n_total}")
    by_stream = con.execute(
        "SELECT COALESCE(stream, '<untagged>') AS s, COUNT(*) AS n, "
        "ROUND(SUM(total_seconds)/3600.0, 1) AS hours "
        "FROM job_view GROUP BY stream ORDER BY n DESC"
    ).fetchall()
    print("\nby stream:")
    for r in by_stream:
        print(f"  {r['s']:18s} {r['n']:>4d} jobs   {r['hours']:>7.1f} h")
    print("\nlongest 5 jobs:")
    for r in con.execute(
        "SELECT cluster_id, stream, started_at, total_seconds "
        "FROM job_view ORDER BY total_seconds DESC LIMIT 5"
    ):
        hrs = r["total_seconds"] / 3600.0
        print(f"  {r['cluster_id']}  {r['stream'] or '<untagged>':14s}  "
              f"{r['started_at'][:10]}  {hrs:5.1f} h")
    return 0


def _cli_params() -> int:
    p = load_params()
    for k, v in p.items():
        print(f"{k:22s} {v}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp cluster",
                                     description="Materialize job_view from sessions.")
    parser.add_argument("command", nargs="?", default="status",
                        choices=("refresh", "status", "params"))
    args = parser.parse_args(argv[1:])
    if args.command == "refresh":
        return _cli_refresh()
    if args.command == "params":
        return _cli_params()
    return _cli_status()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
