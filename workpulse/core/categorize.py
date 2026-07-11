"""
categorize.py — Fix 1.7: the Categorizer agent.

First specialist agent in the team. Given a cluster, scores every candidate
project against the signals visible in the cluster's time window and writes
an assignment. The Teacher reads the corrections trail when (later) it
exists. The user reads/corrects via the dashboard dropdown.

Signal types and weights — tuned by feel; revisit after first 20 corrections:

    file_path_match     +5 per matching folder pattern in raw_path
                          (strongest signal — paths usually don't lie)
    capture_about       +5 per capture --about_stream--> X edge in window
                          (user-declared intent = ground truth)
    plan_item_in_window +4 per plan item w/ stream X on cluster's date
                          (declared morning intent, scoped to date)
    daily_candidate     +3 if X is in today's candidate set
                          (smaller bonus — just narrows the field)
    title_keyword       +2 per matching keyword in raw_title
                          (window titles are often app chrome)
    existing_stream     +1 if cluster's session.stream is already X
                          (preserves earlier inference, very small weight)

Public API:
    assign_cluster(con, cluster_id, *, cfg=None) -> dict
    assign_all(con, *, since=None, force=False, cfg=None) -> dict
    correct_assignment(con, cluster_id, to_stream, *, by='user') -> None
    daily_candidates_for(con, date_str) -> list[str]
    extract_daily_candidates(con) -> int

CLI:
    python -m workpulse.core.categorize assign-all
    python -m workpulse.core.categorize assign-all --since 2026-06-20
    python -m workpulse.core.categorize assign-all --force      # re-do agent assignments
    python -m workpulse.core.categorize candidates              # extract daily candidates from captures
    python -m workpulse.core.categorize show <cluster_id>       # diagnose one cluster
"""

from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path

from workpulse.core import atoms, db
from workpulse.core import projects as wp_projects
from workpulse.common import load_config


_WEIGHTS = {
    "calendar_event":      6.0,   # meetings know what they're about
    "file_path_match":     5.0,
    "capture_about":       5.0,
    "browser_visit":       5.0,   # SharePoint URLs / ChatGPT / Office web
    "plan_item_in_window": 4.0,
    "daily_candidate":     3.0,
    "title_keyword":       2.0,
    "existing_stream":     1.0,
}


# ── daily candidates ────────────────────────────────────────────────────────

def extract_daily_candidates(con: sqlite3.Connection) -> int:
    """Walk every human capture's about_stream edges. For each, write a
    daily_candidate row keyed by (capture date, stream). Idempotent."""
    rows = con.execute(
        """
        SELECT c.id AS capture_id, substr(c.ts, 1, 10) AS d,
               e.dst_id AS stream
        FROM capture c
        JOIN edge e ON e.src_kind = 'capture' AND e.src_id = c.id
                   AND e.rel = 'about_stream'
        WHERE c.author = 'human'
        """
    ).fetchall()
    inserted = 0
    for r in rows:
        cur = con.execute(
            """
            INSERT OR IGNORE INTO daily_candidate
              (date, stream, source_capture_id, declared_at)
            VALUES (?, ?, ?, ?)
            """,
            (r["d"], r["stream"], r["capture_id"],
             datetime.now(timezone.utc).isoformat()),
        )
        if cur.rowcount:
            inserted += 1
    return inserted


def daily_candidates_for(con: sqlite3.Connection, date_str: str) -> list[str]:
    rows = con.execute(
        "SELECT stream FROM daily_candidate WHERE date = ?",
        (date_str,),
    ).fetchall()
    return [r["stream"] for r in rows]


# ── signal gathering ────────────────────────────────────────────────────────

def _cluster_meta(con: sqlite3.Connection, cluster_id: str) -> dict | None:
    row = con.execute(
        "SELECT * FROM job_view WHERE cluster_id = ?", (cluster_id,)
    ).fetchone()
    return dict(row) if row else None


def gather_signals(con: sqlite3.Connection, cluster_id: str,
                   started_at: str, ended_at: str,
                   *, projects: list[dict] | None = None) -> dict:
    """Return a dict of stream -> list[(signal_type, weight, detail)].
    Pure read; no writes."""
    if projects is None:
        projects = wp_projects.load_projects()
    out: dict[str, list[tuple]] = defaultdict(list)

    # 1) Existing session.stream — small weight, preserves prior inference
    rows = con.execute(
        "SELECT stream, COUNT(*) AS n FROM session "
        "WHERE cluster_id = ? AND stream IS NOT NULL GROUP BY stream",
        (cluster_id,),
    ).fetchall()
    for r in rows:
        out[r["stream"]].append(
            ("existing_stream", _WEIGHTS["existing_stream"] * min(1.0, r["n"] / 5.0),
             f"{r['n']} sessions already tagged {r['stream']}")
        )

    # 2) File paths touched during window
    paths = con.execute(
        """
        SELECT fl.raw_path FROM file_event fe
        JOIN file_event_local fl ON fl.file_event_id = fe.id
        WHERE fe.ts BETWEEN ? AND ?
        """,
        (started_at, ended_at),
    ).fetchall()
    for path_row in paths:
        m = wp_projects.resolve_match(path_row["raw_path"] or "", projects, kind="path")
        if m:
            out[m["stream"]].append(
                ("file_path_match", _WEIGHTS["file_path_match"],
                 f"path matched '{m['matched_keyword']}'")
            )

    # 3) Captures with about_stream edges in the window OR pinned to cluster sessions
    cluster_session_ids = [r["id"] for r in con.execute(
        "SELECT id FROM session WHERE cluster_id = ?", (cluster_id,)
    )]
    if cluster_session_ids:
        qmarks = ",".join("?" * len(cluster_session_ids))
        edges = con.execute(
            f"""
            SELECT DISTINCT e.dst_id AS stream FROM edge e
            JOIN capture c ON c.id = e.src_id
            WHERE e.src_kind = 'capture' AND e.rel = 'about_stream'
              AND c.author = 'human'
              AND (
                (c.pinned_kind = 'session' AND c.pinned_id IN ({qmarks}))
                OR (c.ts BETWEEN ? AND ?)
              )
            """,
            [*cluster_session_ids, started_at, ended_at],
        ).fetchall()
    else:
        edges = con.execute(
            """
            SELECT DISTINCT e.dst_id AS stream FROM edge e
            JOIN capture c ON c.id = e.src_id
            WHERE e.src_kind = 'capture' AND e.rel = 'about_stream'
              AND c.author = 'human'
              AND c.ts BETWEEN ? AND ?
            """,
            (started_at, ended_at),
        ).fetchall()
    for e in edges:
        out[e["stream"]].append(
            ("capture_about", _WEIGHTS["capture_about"],
             "human capture declared this project")
        )

    # 4) Plan items in the cluster's date range with their stream
    start_d = (started_at or "")[:10]
    end_d = (ended_at or "")[:10]
    pi = con.execute(
        """
        SELECT stream FROM plan_item
        WHERE plan_date BETWEEN ? AND ? AND stream IS NOT NULL
        """,
        (start_d, end_d),
    ).fetchall()
    for r in pi:
        out[r["stream"]].append(
            ("plan_item_in_window", _WEIGHTS["plan_item_in_window"],
             f"plan item tagged {r['stream']}")
        )

    # 5) Calendar events overlapping the cluster window. Meetings know what
    # they're about — title-resolved stream gets the strongest weight.
    events = con.execute(
        """
        SELECT ce.stream, cel.raw_title FROM calendar_event ce
        LEFT JOIN calendar_event_local cel ON cel.event_id = ce.id
        WHERE ce.stream IS NOT NULL
          AND NOT (ce.ended_at < ? OR ce.started_at > ?)
        """,
        (started_at, ended_at),
    ).fetchall()
    for e in events:
        title = (e["raw_title"] or "")[:60]
        out[e["stream"]].append(
            ("calendar_event", _WEIGHTS["calendar_event"],
             f"calendar meeting '{title}'")
        )

    # 6) Browser visits in the cluster window with a resolved stream.
    # Private visits (is_private=1) are excluded from cluster signal because
    # they belong to the personal tier and should never bleed into work
    # cluster assignments — even if a banking visit overlaps a work cluster.
    visits = con.execute(
        """
        SELECT stream, domain, COUNT(*) AS n FROM browser_visit
        WHERE stream IS NOT NULL AND is_private = 0
          AND ts BETWEEN ? AND ?
        GROUP BY stream, domain
        """,
        (started_at, ended_at),
    ).fetchall()
    for v in visits:
        # Each (stream, domain) hit contributes once; multiple polls of the
        # same tab shouldn't dominate. Cap influence per (stream, domain).
        weight = _WEIGHTS["browser_visit"] * min(1.0, v["n"] / 3.0)
        out[v["stream"]].append(
            ("browser_visit", weight,
             f"browser visits to {v['domain']} ({v['n']} polls)")
        )

    # 7) Title keyword matches in session_local.raw_title
    titles = con.execute(
        """
        SELECT sl.raw_title FROM session s
        JOIN session_local sl ON sl.session_id = s.id
        WHERE s.cluster_id = ? AND sl.raw_title IS NOT NULL
        """,
        (cluster_id,),
    ).fetchall()
    for t in titles:
        m = wp_projects.resolve_match(t["raw_title"] or "", projects, kind="text")
        if m:
            out[m["stream"]].append(
                ("title_keyword", _WEIGHTS["title_keyword"],
                 f"title matched '{m['matched_keyword']}'")
            )

    return dict(out)


def _score(signals_for_stream: list[tuple]) -> float:
    return round(sum(w for (_, w, _) in signals_for_stream), 2)


# ── main scoring + assignment ───────────────────────────────────────────────

def assign_cluster(con: sqlite3.Connection, cluster_id: str,
                   *, cfg: dict | None = None,
                   force: bool = False) -> dict:
    """Score and assign one cluster. Returns the assignment dict.
    Respects user overrides (source='user' assignments are never touched
    unless force=True). Returns {skipped: True, ...} when no signal fires
    and there are no candidates to fall back to."""
    meta = _cluster_meta(con, cluster_id)
    if meta is None:
        return {"skipped": True, "reason": "no such cluster"}

    existing = con.execute(
        "SELECT * FROM cluster_assignment WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()
    # User assignments are ALWAYS protected — force=True does not override
    # them. force only re-does agent/fallback assignments.
    if existing and existing["source"] == "user":
        return {"skipped": True, "reason": "user assignment present",
                "stream": existing["stream"], "confidence": existing["confidence"]}

    projects = wp_projects.load_projects()
    signals = gather_signals(con, cluster_id, meta["started_at"],
                             meta["ended_at"], projects=projects)

    # Bonus: streams declared by human captures on the cluster's date.
    # Read directly from about_stream edges so this doesn't require
    # daily_candidate rows to have been extracted first — the agent is
    # self-sufficient.
    cluster_date = (meta["started_at"] or "")[:10]
    candidate_rows = con.execute(
        """
        SELECT DISTINCT e.dst_id AS stream FROM edge e
        JOIN capture c ON c.id = e.src_id
        WHERE e.src_kind = 'capture' AND e.rel = 'about_stream'
          AND c.author = 'human'
          AND substr(c.ts, 1, 10) = ?
        """,
        (cluster_date,),
    ).fetchall()
    for r in candidate_rows:
        signals.setdefault(r["stream"], []).append(
            ("daily_candidate", _WEIGHTS["daily_candidate"],
             "declared in today's morning candidates")
        )

    if not signals:
        # Fallback to misc when literally nothing matches
        return _write_assignment(con, cluster_id, stream="misc",
                                 confidence=0.0, source="fallback",
                                 evidence=[], cfg=cfg)

    # Score and pick winner
    scores: list[tuple[str, float, list]] = [
        (s, _score(sigs), sigs) for s, sigs in signals.items()
    ]
    scores.sort(key=lambda x: -x[1])
    best_stream, best_score, best_sigs = scores[0]
    total_score = sum(s for (_, s, _) in scores) or 1.0
    confidence = round(best_score / total_score, 3)

    evidence_payload = [
        {"signal": typ, "weight": w, "detail": detail}
        for (typ, w, detail) in best_sigs
    ]
    return _write_assignment(con, cluster_id, stream=best_stream,
                             confidence=confidence, source="agent",
                             evidence=evidence_payload, cfg=cfg)


def _write_assignment(con: sqlite3.Connection, cluster_id: str, *,
                      stream: str, confidence: float, source: str,
                      evidence: list, cfg: dict | None) -> dict:
    # Ensure stream row exists (lazy upsert)
    con.execute(
        "INSERT OR IGNORE INTO stream(key, label, parent_key) VALUES (?, ?, NULL)",
        (stream, stream),
    )
    # Record the agent run
    sr_id = atoms.new_id()
    ts = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                              in_tokens, out_tokens, cost_usd,
                              input, output, status)
        VALUES (?, ?, 'categorize', NULL, NULL, 0, 0, 0, ?, ?, 'ok')
        """,
        (sr_id, ts, cluster_id[:200],
         json.dumps({"stream": stream, "confidence": confidence,
                     "source": source})[:8000]),
    )
    con.execute(
        """
        INSERT INTO cluster_assignment
          (cluster_id, stream, confidence, source, assigned_at,
           evidence, agent_run_id)
        VALUES (?, ?, ?, ?, ?, ?, ?)
        ON CONFLICT(cluster_id) DO UPDATE SET
          stream = excluded.stream,
          confidence = excluded.confidence,
          source = excluded.source,
          assigned_at = excluded.assigned_at,
          evidence = excluded.evidence,
          agent_run_id = excluded.agent_run_id
        WHERE cluster_assignment.source != 'user'  -- protect user overrides
        """,
        (cluster_id, stream, confidence, source, ts,
         json.dumps(evidence), sr_id),
    )
    return {
        "skipped":    False,
        "cluster_id": cluster_id,
        "stream":     stream,
        "confidence": confidence,
        "source":     source,
        "evidence":   evidence,
        "skill_run":  sr_id,
    }


def assign_all(con: sqlite3.Connection, *, since: str | None = None,
               force: bool = False, cfg: dict | None = None) -> dict:
    """Run the Categorizer over every cluster (or every cluster ending
    on/after `since`). Extracts daily candidates first."""
    candidates_added = extract_daily_candidates(con)
    sql = "SELECT cluster_id FROM job_view"
    params: tuple = ()
    if since:
        sql += " WHERE ended_at >= ?"
        params = (since,)
    sql += " ORDER BY total_seconds DESC"
    rows = con.execute(sql, params).fetchall()
    counts = {"assigned_agent": 0, "kept_user": 0, "fallback": 0, "total": 0,
              "candidates_added": candidates_added}
    for r in rows:
        res = assign_cluster(con, r["cluster_id"], cfg=cfg, force=force)
        counts["total"] += 1
        if res.get("skipped"):
            if res.get("reason") == "user assignment present":
                counts["kept_user"] += 1
            continue
        src = res["source"]
        if src == "fallback":
            counts["fallback"] += 1
        elif src == "agent":
            counts["assigned_agent"] += 1
    return counts


# ── teach-correct: user override ────────────────────────────────────────────

def correct_assignment(con: sqlite3.Connection, cluster_id: str,
                       to_stream: str, *, by: str = "user") -> dict:
    """The teach-correct loop's write path. Records the prior agent
    assignment (if any), updates cluster_assignment with source='user',
    and stores the signals snapshot for later analysis by the Teacher."""
    prior = con.execute(
        "SELECT stream, confidence, evidence FROM cluster_assignment "
        "WHERE cluster_id = ?", (cluster_id,),
    ).fetchone()
    from_stream = prior["stream"] if prior else None
    conf_before = prior["confidence"] if prior else None
    signals = prior["evidence"] if prior else None
    ts = datetime.now(timezone.utc).isoformat()

    con.execute(
        "INSERT OR IGNORE INTO stream(key, label, parent_key) VALUES (?, ?, NULL)",
        (to_stream, to_stream),
    )
    con.execute(
        """
        INSERT INTO cluster_correction
          (cluster_id, from_stream, to_stream, confidence_before,
           corrected_at, signals_snapshot)
        VALUES (?, ?, ?, ?, ?, ?)
        """,
        (cluster_id, from_stream, to_stream, conf_before, ts, signals),
    )
    con.execute(
        """
        INSERT INTO cluster_assignment
          (cluster_id, stream, confidence, source, assigned_at, evidence)
        VALUES (?, ?, 1.0, 'user', ?, ?)
        ON CONFLICT(cluster_id) DO UPDATE SET
          stream      = excluded.stream,
          confidence  = 1.0,
          source      = 'user',
          assigned_at = excluded.assigned_at,
          evidence    = excluded.evidence
        """,
        (cluster_id, to_stream, ts,
         json.dumps([{"signal": "user_override", "weight": 999,
                      "detail": f"corrected by {by}"}])),
    )
    return {"cluster_id": cluster_id, "from_stream": from_stream,
            "to_stream": to_stream, "by": by}


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_assign_all(args: argparse.Namespace) -> int:
    cfg = load_config()
    con = db.connect(cfg)
    counts = assign_all(con, since=args.since, force=args.force, cfg=cfg)
    print("Categorizer pass complete")
    for k, v in counts.items():
        print(f"  {k:20s} {v}")
    return 0


def _cli_candidates() -> int:
    con = db.connect(load_config())
    n = extract_daily_candidates(con)
    print(f"extracted {n} new daily_candidate row(s)")
    by_date = con.execute(
        "SELECT date, GROUP_CONCAT(stream) AS streams FROM daily_candidate "
        "GROUP BY date ORDER BY date DESC LIMIT 14"
    ).fetchall()
    for r in by_date:
        print(f"  {r['date']}: {r['streams']}")
    return 0


def _cli_show(cluster_id: str) -> int:
    con = db.connect(load_config())
    meta = _cluster_meta(con, cluster_id)
    if not meta:
        print(f"no cluster {cluster_id}")
        return 1
    print(f"cluster {cluster_id}")
    print(f"  {meta['started_at']} → {meta['ended_at']}")
    print(f"  total_seconds: {meta['total_seconds']} ({meta['total_seconds']/3600:.1f}h)")
    print(f"  stream:        {meta['stream']}")
    signals = gather_signals(con, cluster_id, meta["started_at"], meta["ended_at"])
    print("\n  signals per stream:")
    for stream in sorted(signals, key=lambda s: -_score(signals[s])):
        print(f"    {stream:16s} score={_score(signals[stream])}")
        for (typ, w, detail) in signals[stream]:
            print(f"      • {typ:24s} (+{w})  {detail}")
    row = con.execute(
        "SELECT * FROM cluster_assignment WHERE cluster_id = ?",
        (cluster_id,),
    ).fetchone()
    if row:
        print(f"\n  current assignment: {row['stream']} "
              f"(conf={row['confidence']}, source={row['source']})")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp categorize",
                                     description="Categorizer + corrections.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    a = sub.add_parser("assign-all")
    a.add_argument("--since", metavar="YYYY-MM-DD")
    a.add_argument("--force", action="store_true",
                   help="re-do agent assignments (user overrides still kept)")
    sub.add_parser("candidates")
    s = sub.add_parser("show")
    s.add_argument("cluster_id")
    args = parser.parse_args(argv[1:])
    if args.cmd == "assign-all":
        return _cli_assign_all(args)
    if args.cmd == "candidates":
        return _cli_candidates()
    if args.cmd == "show":
        return _cli_show(args.cluster_id)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
