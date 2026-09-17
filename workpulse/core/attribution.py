"""
attribution.py — assign sessions to projects (plan U3, KTD4).

Scores each session's raw signal against the discovered project taxonomy and
writes session.project_id, or leaves it NULL (the unattributed / needs-signal
bucket, R7) when the signal is too weak to trust.

Scoring is idf-weighted token overlap: a token that belongs to only one project
is decisive; a token shared across many projects barely moves the needle. This
is the deterministic, local-first floor (KTD4) — it attributes clear matches
with no LLM and no network.

# ponytail: idf-weighted token overlap is the floor, not full semantics.
# Upgrade path: add sqlite-vec embedding similarity as an extra scored signal and
# an optional LLM tie-breaker for low-margin cases, layered into `_score` without
# changing callers or the schema (KTD4). Synonym/paraphrase matching lives there.

Public API:
    attribute_session(con, session_id, projects=None, cfg=None, *, floor=...) -> dict | None
    attribute_all(con, cfg=None, *, only_unattributed=True, floor=...) -> dict
"""

from __future__ import annotations

import json
import logging
import os
import sqlite3
from collections import Counter
from datetime import datetime, timedelta, timezone

from workpulse.core import atoms
from workpulse.core.name_clusters import _tokens

log = logging.getLogger(__name__)

# A match needs at least this much idf-weighted overlap. 0.6 accepts one
# project-unique token (weight 1.0) but rejects a single token shared across
# three-plus projects (<=0.34): prefer the unattributed bucket over a guess (R7).
_DEFAULT_FLOOR = 0.6


def _session_tokens(raw_title: str | None, raw_path: str | None) -> set[str]:
    toks = set(_tokens(raw_title or ""))
    if raw_path:
        base = os.path.basename(raw_path.replace("\\", "/"))
        toks |= set(_tokens(base))
    return toks


def _project_token_map(con: sqlite3.Connection) -> list[tuple[str, set[str]]]:
    rows = con.execute(
        "SELECT id, client, name FROM project WHERE status != 'dismissed'"
    ).fetchall()
    out = []
    for r in rows:
        toks = set(_tokens(r["name"])) | set(_tokens(r["client"] or ""))
        if toks:
            out.append((r["id"], toks))
    return out


def _project_df(projects: list[tuple[str, set[str]]]) -> Counter:
    df: Counter = Counter()
    for _, toks in projects:
        for tk in toks:
            df[tk] += 1
    return df


def _score(session_toks: set[str], proj_toks: set[str], df: Counter) -> tuple[float, list[str]]:
    shared = sorted(session_toks & proj_toks)
    score = sum(1.0 / df[tk] for tk in shared if df.get(tk))
    return score, shared


def _best(session_toks: set[str], projects, df, floor):
    best_pid, best_score, best_shared = None, 0.0, []
    for pid, toks in projects:
        score, shared = _score(session_toks, toks, df)
        if score > best_score:
            best_pid, best_score, best_shared = pid, score, shared
    if best_pid is None or best_score < floor or not best_shared:
        return None
    confidence = round(min(1.0, best_score / max(len(best_shared), 1)), 3)
    return {"project_id": best_pid, "confidence": confidence,
            "evidence": best_shared, "score": round(best_score, 3)}


def attribute_session(con: sqlite3.Connection, session_id: str,
                      projects=None, cfg: dict | None = None,
                      *, floor: float = _DEFAULT_FLOOR) -> dict | None:
    row = con.execute(
        "SELECT raw_title, raw_path FROM session_local WHERE session_id = ?",
        (session_id,)).fetchone()
    if row is None:
        return None
    session_toks = _session_tokens(row["raw_title"], row["raw_path"])
    if not session_toks:
        return None
    if projects is None:
        projects = _project_token_map(con)
    df = _project_df(projects)
    return _best(session_toks, projects, df, floor)


def attribute_all(con: sqlite3.Connection, cfg: dict | None = None,
                  *, only_unattributed: bool = True,
                  floor: float = _DEFAULT_FLOOR, batch_size: int = 500) -> dict:
    projects = _project_token_map(con)
    df = _project_df(projects)
    # A user-corrected session is never re-touched by an automatic pass, even a
    # full (only_unattributed=False) re-run (KTD5, R8).
    conds = ["s.id NOT IN (SELECT target_id FROM project_correction "
             "WHERE target_kind = 'session')"]
    if only_unattributed:
        conds.append("s.project_id IS NULL")
    where = "WHERE " + " AND ".join(conds)
    rows = con.execute(
        f"SELECT s.id AS id, sl.raw_title AS t, sl.raw_path AS p "
        f"FROM session s JOIN session_local sl ON sl.session_id = s.id {where}"
    ).fetchall()

    # Commit per batch so a mid-pass failure keeps prior progress and the next
    # run resumes from the still-NULL rows (R9/AE5). Each batch is its own txn.
    attributed = unattributed = 0
    for start in range(0, len(rows), batch_size):
        con.execute("BEGIN")
        try:
            for r in rows[start:start + batch_size]:
                toks = _session_tokens(r["t"], r["p"])
                decision = _best(toks, projects, df, floor) if toks else None
                if decision is None:
                    unattributed += 1
                    continue
                con.execute("UPDATE session SET project_id = ? WHERE id = ?",
                            (decision["project_id"], r["id"]))
                attributed += 1
            con.execute("COMMIT")
        except Exception:
            con.execute("ROLLBACK")
            raise
    return {"attributed": attributed, "unattributed": unattributed}


def correct_session(con: sqlite3.Connection, session_id: str, *,
                    project_id: str | None = None, client: str | None = None,
                    name: str | None = None, signals: dict | None = None) -> str:
    """Record a user reassignment of a session to a project (plan R6, KTD5).

    Pass an existing ``project_id``, or a ``name`` (+ optional ``client``) to
    reuse or create the target project. The target is confirmed, the session is
    relinked, and the (from, to) tuple is stored in project_correction so
    discovery/attribution can learn from it and automatic passes never revert it.
    Returns the target project id.
    """
    if project_id is None and not name:
        raise ValueError("correct_session needs project_id or name")
    now = datetime.now(timezone.utc).isoformat()
    prev = con.execute("SELECT project_id FROM session WHERE id = ?",
                       (session_id,)).fetchone()
    from_project = prev["project_id"] if prev else None
    con.execute("BEGIN")
    try:
        if project_id is None:
            row = con.execute(
                "SELECT id FROM project WHERE IFNULL(client,'') = IFNULL(?, '') "
                "AND name = ?", (client, name)).fetchone()
            if row:
                project_id = row["id"]
            else:
                project_id = atoms.new_id()
                con.execute(
                    "INSERT INTO project(id, client, name, status, confidence, "
                    "created_at, confirmed_at) VALUES (?,?,?,?,?,?,?)",
                    (project_id, client, name, "confirmed", 1.0, now, now))
        con.execute(
            "UPDATE project SET status = 'confirmed', "
            "confirmed_at = COALESCE(confirmed_at, ?) WHERE id = ?",
            (now, project_id))
        con.execute("UPDATE session SET project_id = ? WHERE id = ?",
                    (project_id, session_id))
        con.execute(
            "INSERT INTO project_correction(target_kind, target_id, from_project, "
            "to_project, confidence_before, corrected_at, signals_snapshot) "
            "VALUES (?,?,?,?,?,?,?)",
            ("session", session_id, from_project, project_id, None, now,
             json.dumps(signals) if signals else None))
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return project_id


def attribute_observations(con: sqlite3.Connection,
                           *, only_unattributed: bool = True) -> dict:
    """Attribute semantic_observations from the projects of their evidence
    sessions (plan U4, R11). An observation takes the dominant project across
    its session evidence; if none of that evidence is attributed, it stays in
    the unattributed bucket (R7). only_unattributed leaves already-set rows
    untouched, so a re-run never overwrites a prior attribution or correction.
    """
    where = "WHERE project_id IS NULL" if only_unattributed else ""
    obs = con.execute(f"SELECT id FROM semantic_observation {where}").fetchall()
    attributed = unattributed = 0
    con.execute("BEGIN")
    try:
        for o in obs:
            pids = [r["pid"] for r in con.execute(
                "SELECT s.project_id AS pid FROM semantic_evidence_local e "
                "JOIN session s ON s.id = e.source_id "
                "WHERE e.observation_id = ? AND e.source_kind = 'session' "
                "  AND s.project_id IS NOT NULL",
                (o["id"],)).fetchall()]
            if not pids:
                unattributed += 1
                continue
            best = Counter(pids).most_common(1)[0][0]
            con.execute("UPDATE semantic_observation SET project_id = ? WHERE id = ?",
                        (best, o["id"]))
            attributed += 1
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise
    return {"attributed": attributed, "unattributed": unattributed}


def _since(days: int | None) -> str | None:
    if not days:
        return None
    return (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()


_MINUTES = ("(julianday(s.ended_at) - julianday(s.started_at)) * 1440")


def project_time(con: sqlite3.Connection, *, days: int | None = None) -> list[dict]:
    """Time and activity grouped by project (plan R13). minutes sums session
    durations where an end time is known; sessions without one still count.
    Unattributed sessions (project_id NULL) are excluded by the join.
    """
    since = _since(days)
    where = "WHERE s.started_at >= ?" if since else ""
    params = (since,) if since else ()
    rows = con.execute(
        f"SELECT p.id AS pid, p.client AS client, p.name AS name, p.status AS status, "
        f"COUNT(*) AS sessions, "
        f"COALESCE(SUM(CASE WHEN s.ended_at IS NOT NULL THEN {_MINUTES} END), 0) AS minutes "
        f"FROM session s JOIN project p ON p.id = s.project_id {where} "
        f"GROUP BY p.id ORDER BY minutes DESC, sessions DESC", params).fetchall()
    return [{"project_id": r["pid"], "client": r["client"], "name": r["name"],
             "status": r["status"], "sessions": r["sessions"],
             "minutes": round(r["minutes"], 1)} for r in rows]


def project_detail(con: sqlite3.Connection, project_id: str,
                   *, days: int | None = None, limit: int = 50) -> dict:
    """Drill-down for one project: deliverable-level breakdown (by raw title)
    with session counts and minutes (plan R4). The title is the local deliverable
    signal; it never leaves the machine.
    """
    meta = con.execute(
        "SELECT id, client, name, status, confidence FROM project WHERE id = ?",
        (project_id,)).fetchone()
    since = _since(days)
    conds = ["s.project_id = ?"]
    params: list = [project_id]
    if since:
        conds.append("s.started_at >= ?")
        params.append(since)
    where = "WHERE " + " AND ".join(conds)
    rows = con.execute(
        f"SELECT sl.raw_title AS title, COUNT(*) AS sessions, "
        f"COALESCE(SUM(CASE WHEN s.ended_at IS NOT NULL THEN {_MINUTES} END), 0) AS minutes "
        f"FROM session s JOIN session_local sl ON sl.session_id = s.id {where} "
        f"GROUP BY sl.raw_title ORDER BY sessions DESC LIMIT ?",
        (*params, limit)).fetchall()
    return {
        "project": dict(meta) if meta else None,
        "deliverables": [{"title": r["title"], "sessions": r["sessions"],
                          "minutes": round(r["minutes"], 1)} for r in rows],
    }


def llm_assist_attribution(con: sqlite3.Connection, cfg: dict | None = None,
                           *, max_calls: int | None = None) -> dict:
    """Resolve the sessions the deterministic floor left unattributed, using the
    `attribution` feature's provider (plan U4, KTD4). Titles are redacted before
    they leave (R12/KTD7); a within-run cache keys on the normalized title so
    duplicate titles cost one call, and attributed rows persist so a re-run makes
    no new calls (R8). Corrected and already-attributed sessions are never sent
    (R6, R9). No provider -> no-op (R4); a provider error leaves the session in
    the unattributed bucket (R5/AE5).
    """
    from workpulse.core import llm, content_capture
    backend, _ = llm._resolve_route(cfg, "attribution")
    if backend == "none":
        return {"attributed": 0, "llm_calls": 0}
    projects = con.execute(
        "SELECT id, client, name FROM project WHERE status != 'dismissed'").fetchall()
    if not projects:
        return {"attributed": 0, "llm_calls": 0}
    valid = {p["id"] for p in projects}
    catalog = "\n".join(
        f"{p['id']}: {content_capture.redact((p['client'] or '') + ' | ' + p['name'])}"
        for p in projects)
    rows = con.execute(
        "SELECT s.id AS id, sl.raw_title AS t FROM session s "
        "JOIN session_local sl ON sl.session_id = s.id "
        "WHERE s.project_id IS NULL AND s.id NOT IN "
        "(SELECT target_id FROM project_correction WHERE target_kind = 'session')"
    ).fetchall()

    cache: dict[str, str | None] = {}
    attributed = calls = 0
    for r in rows:
        title = (r["t"] or "").strip()
        if not title:
            continue
        key = title.lower()
        if key not in cache:
            if max_calls is not None and calls >= max_calls:
                break  # per-pass cap (R11); the rest stay deterministic
            red = content_capture.redact(title)
            prompt = ("Projects:\n" + catalog + "\n\nWhich project id does this "
                      'window best belong to? Reply ONLY JSON {"project_id": <id or null>}.'
                      f"\nWindow: {red}")
            try:
                obj, _meta = llm.ask_json(prompt, feature="attribution", cfg=cfg)
            except Exception as e:  # noqa: BLE001 — degrade to unattributed (R5)
                log.warning("attribution LLM call failed: %s", e)
                obj = None
            calls += 1
            pid = obj.get("project_id") if isinstance(obj, dict) else None
            cache[key] = pid if pid in valid else None
        pid = cache[key]
        if pid:
            con.execute("UPDATE session SET project_id = ? WHERE id = ?", (pid, r["id"]))
            attributed += 1
    return {"attributed": attributed, "llm_calls": calls}


def run_attribution_pass(con: sqlite3.Connection, cfg: dict | None = None,
                         *, batch_size: int = 500,
                         min_evidence: int = 2,
                         max_llm_calls: int | None = None) -> dict:
    """One full, idempotent attribution pass (plan U6, R8/R9): refresh the
    discovered taxonomy, attribute unattributed sessions, then attribute
    observations. Decoupled from intake (KTD6) — it only reads and writes the DB
    and never runs in the sensor path. Safe to run over the whole backlog and
    again: attribute_all commits per batch, so a mid-pass failure leaves the DB
    consistent and the next pass resumes.
    """
    # Imported here to avoid a core import cycle (discovery imports nothing heavy).
    from workpulse.core import discovery
    candidates = discovery.discover_projects(con, cfg, min_evidence=min_evidence)
    # LLM taxonomy cleanup between discovery and attribution (U3); no-op without
    # a provider, so the deterministic pass is unchanged (R4).
    refine = discovery.refine_taxonomy(con, cfg)
    sessions = attribute_all(con, cfg, batch_size=batch_size)
    # LLM resolves the residual the floor could not place (U4); no-op without a
    # provider, so the deterministic result is unchanged (R4).
    assist = llm_assist_attribution(con, cfg, max_calls=max_llm_calls)
    observations = attribute_observations(con)
    return {"candidates": len(candidates),
            "refine": refine,
            "sessions": sessions,
            "assist": assist,
            "observations": observations}
