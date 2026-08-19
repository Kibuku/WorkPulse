"""Offline semantic observations derived from local activity evidence.

This module intentionally does not read document bodies, take screenshots, or
call an LLM. It turns existing window, file and Chrome metadata into cautious
workflow-stage and friction hypotheses. Raw evidence stays in the private local
projection; the observation table stores generic summaries plus source IDs.
"""

from __future__ import annotations

import hashlib
import json
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path


_STAGES: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("finalization", ("final", "signed", "submit", "submission", "sent")),
    ("review", ("review", "revised", "revision", "feedback", "comments", "track changes")),
    ("analysis", ("analysis", "model", "dashboard", "calculation", "dataset", "data ")),
    ("drafting", ("draft", "proposal", "report", "concept note", "memo", "document")),
    ("planning", ("plan", "brief", "requirements", "terms of reference", "roadmap", "agenda")),
    ("research", ("research", "search", "reference", "literature", "reading")),
    ("communication", ("mail", "inbox", "meeting", "teams", "zoom", "calendar", "slack")),
    ("administration", ("invoice", "timesheet", "expense", "filing", "form", "procurement")),
)

_STAGE_LABELS = {
    "finalization": "Finalising or submitting outputs",
    "review": "Reviewing and revising work",
    "analysis": "Analysing information",
    "drafting": "Drafting working outputs",
    "planning": "Planning and reviewing requirements",
    "research": "Researching and gathering context",
    "communication": "Communicating and coordinating",
    "administration": "Handling administrative work",
}


def classify_stage(text: str, app: str = "") -> str | None:
    """Return a conservative stage hypothesis from metadata text."""
    low = " ".join(f"{text} {app}".casefold().split())
    for stage, terms in _STAGES:
        if any(term in low for term in terms):
            return stage
    suffix = Path(text).suffix.casefold()
    if suffix in {".doc", ".docx", ".odt", ".pages"}:
        return "drafting"
    if suffix in {".xls", ".xlsx", ".csv"}:
        return "analysis"
    if suffix in {".ppt", ".pptx", ".key"}:
        return "drafting"
    return None


def _stable_id(*parts: str) -> str:
    digest = hashlib.sha256("\x1f".join(parts).encode("utf-8")).hexdigest()[:24]
    return f"sem-{digest}"


def _minutes(start: str, end: str | None) -> float:
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat((end or start).replace("Z", "+00:00"))
        return max(0.0, (b - a).total_seconds() / 60)
    except (TypeError, ValueError):
        return 0.0


def _session_evidence(con: sqlite3.Connection, since: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT s.id,s.started_at,s.ended_at,s.app,s.stream,sl.raw_title,
               COALESCE(st.is_private,0) AS is_private
        FROM session s
        LEFT JOIN session_local sl ON sl.session_id=s.id
        LEFT JOIN stream st ON st.key=s.stream
        WHERE s.started_at >= ?
        ORDER BY s.started_at
        """, (since,)
    ).fetchall()
    return [dict(row) for row in rows]


def _file_evidence(con: sqlite3.Connection, since: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT f.id,f.ts,f.kind,fl.raw_path
        FROM file_event f JOIN file_event_local fl ON fl.file_event_id=f.id
        WHERE f.ts >= ? ORDER BY f.ts
        """, (since,)
    ).fetchall()
    return [dict(row) for row in rows]


def _browser_evidence(con: sqlite3.Connection, since: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT b.id,b.ts,b.app,b.domain,b.stream,b.is_private,bl.raw_title
        FROM browser_visit b
        JOIN browser_visit_local bl ON bl.visit_id=b.id
        WHERE b.ts >= ? AND lower(b.app) LIKE '%chrome%'
        ORDER BY b.ts
        """, (since,)
    ).fetchall()
    return [dict(row) for row in rows]


def _write_observation(con: sqlite3.Connection, item: dict) -> None:
    now = datetime.now(timezone.utc).isoformat()
    prior = con.execute(
        "SELECT status,created_at FROM semantic_observation WHERE id=?", (item["id"],)
    ).fetchone()
    status = prior["status"] if prior else "proposed"
    created = prior["created_at"] if prior else now
    con.execute(
        """
        INSERT OR REPLACE INTO semantic_observation
          (id,observed_date,kind,semantic_key,stream,summary,confidence,
           evidence_count,source_types,first_seen,last_seen,is_private,status,
           created_at,updated_at)
        VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)
        """,
        (item["id"], item["date"], item["kind"], item["key"], item.get("stream"),
         item["summary"], item["confidence"], len(item["evidence"]),
         json.dumps(sorted({kind for kind, _ in item["evidence"]})),
         item["first_seen"], item["last_seen"], int(item.get("is_private", False)),
         status, created, now),
    )
    con.execute("DELETE FROM semantic_evidence_local WHERE observation_id=?", (item["id"],))
    con.executemany(
        "INSERT INTO semantic_evidence_local(observation_id,source_kind,source_id) VALUES (?,?,?)",
        [(item["id"], kind, source_id) for kind, source_id in item["evidence"]],
    )


def refresh(con: sqlite3.Connection, *, days: int = 14) -> dict:
    """Derive stage and friction observations idempotently."""
    since_dt = datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 90)))
    since = since_dt.isoformat()
    sessions = _session_evidence(con, since)
    files = _file_evidence(con, since)
    browser_visits = _browser_evidence(con, since)
    stage_groups: dict[tuple[str, str, str | None], list[dict]] = defaultdict(list)

    for row in sessions:
        stage = classify_stage(row.get("raw_title") or "", row.get("app") or "")
        if stage:
            stage_groups[(row["started_at"][:10], stage, row.get("stream"))].append({
                "ts": row["started_at"], "kind": "session", "id": row["id"],
                "private": bool(row["is_private"]),
            })
    for row in files:
        stage = classify_stage(row["raw_path"])
        if stage:
            stage_groups[(row["ts"][:10], stage, None)].append({
                "ts": row["ts"], "kind": "file_event", "id": row["id"], "private": False,
            })
    for row in browser_visits:
        stage = classify_stage(f"{row['raw_title']} {row['domain']}", row["app"])
        if stage:
            stage_groups[(row["ts"][:10], stage, row.get("stream"))].append({
                "ts": row["ts"], "kind": "browser_visit", "id": row["id"],
                "private": bool(row["is_private"]),
            })

    written = Counter()
    for (day, stage, stream), evidence in stage_groups.items():
        if len(evidence) < 2:
            continue
        item = {
            "id": _stable_id(day, "stage", stage, stream or ""), "date": day,
            "kind": "stage", "key": stage, "stream": stream,
            "summary": _STAGE_LABELS[stage],
            "confidence": round(min(0.95, 0.52 + len(evidence) * 0.07), 2),
            "evidence": [(e["kind"], e["id"]) for e in evidence],
            "first_seen": min(e["ts"] for e in evidence),
            "last_seen": max(e["ts"] for e in evidence),
            "is_private": any(e["private"] for e in evidence),
        }
        _write_observation(con, item)
        written["stage"] += 1

    # Context switching: five transitions across at least four apps in a
    # rolling 30-minute window. This is a signal to review, never a diagnosis.
    by_day: dict[str, list[dict]] = defaultdict(list)
    for row in sessions:
        by_day[row["started_at"][:10]].append(row)
    for day, rows in by_day.items():
        for i, start in enumerate(rows):
            start_dt = datetime.fromisoformat(start["started_at"].replace("Z", "+00:00"))
            window = []
            for row in rows[i:]:
                ts = datetime.fromisoformat(row["started_at"].replace("Z", "+00:00"))
                if ts - start_dt > timedelta(minutes=30):
                    break
                window.append(row)
            apps = [r["app"] for r in window]
            transitions = sum(a != b for a, b in zip(apps, apps[1:]))
            if len(set(apps)) >= 4 and transitions >= 5:
                evidence = [("session", r["id"]) for r in window]
                _write_observation(con, {
                    "id": _stable_id(day, "friction", "context-switching"), "date": day,
                    "kind": "friction", "key": "context-switching", "stream": None,
                    "summary": "Frequent context switching may have interrupted focus",
                    "confidence": round(min(0.9, 0.55 + transitions * 0.04), 2),
                    "evidence": evidence, "first_seen": window[0]["started_at"],
                    "last_seen": window[-1]["started_at"],
                    "is_private": any(bool(r["is_private"]) for r in window),
                })
                written["friction"] += 1
                break

        # Rework: same normalized title appears at least three times in short
        # sessions. Store no title in the observation.
        title_groups: dict[str, list[dict]] = defaultdict(list)
        for row in rows:
            title = re.sub(r"\s+", " ", (row.get("raw_title") or "").strip().casefold())
            if title:
                title_groups[hashlib.sha256(title.encode()).hexdigest()[:16]].append(row)
        repeats = [g for g in title_groups.values()
                   if len(g) >= 3 and sum(_minutes(r["started_at"], r["ended_at"]) for r in g) <= 45]
        if repeats:
            group = max(repeats, key=len)
            _write_observation(con, {
                "id": _stable_id(day, "friction", "repeated-rework"), "date": day,
                "kind": "friction", "key": "repeated-rework", "stream": group[0].get("stream"),
                "summary": "The same work item was reopened repeatedly in short bursts",
                "confidence": round(min(0.9, 0.55 + len(group) * 0.07), 2),
                "evidence": [("session", r["id"]) for r in group],
                "first_seen": group[0]["started_at"], "last_seen": group[-1]["started_at"],
                "is_private": any(bool(r["is_private"]) for r in group),
            })
            written["friction"] += 1

    con.commit()
    return {"stages": written["stage"], "frictions": written["friction"]}


def observations(con: sqlite3.Connection, *, days: int = 7,
                 include_private: bool = False) -> dict:
    since = (datetime.now(timezone.utc) - timedelta(days=max(1, min(days, 30)))).date().isoformat()
    rows = con.execute(
        """
        SELECT o.*,COALESCE(s.label,o.stream) AS stream_label
        FROM semantic_observation o LEFT JOIN stream s ON s.key=o.stream
        WHERE o.observed_date >= ? AND o.status != 'dismissed'
          AND (? OR o.is_private=0)
        ORDER BY o.observed_date DESC,o.confidence DESC,o.last_seen DESC
        """, (since, int(include_private))
    ).fetchall()
    items = []
    for row in rows:
        item = dict(row)
        item["source_types"] = json.loads(item["source_types"])
        items.append(item)
    return {
        "enabled": True,
        "privacy": "Derived locally from metadata. No document contents, screenshots or keystrokes are captured.",
        "limitations": "These are evidence-backed hypotheses, not proof of intent. Confirm or dismiss them to teach WorkPulse.",
        "items": items,
        "counts": dict(Counter(item["kind"] for item in items)),
    }


def set_status(con: sqlite3.Connection, observation_id: str, status: str) -> dict:
    if status not in {"confirmed", "dismissed", "proposed"}:
        raise ValueError("status must be proposed, confirmed or dismissed")
    cur = con.execute(
        "UPDATE semantic_observation SET status=?,updated_at=? WHERE id=?",
        (status, datetime.now(timezone.utc).isoformat(), observation_id),
    )
    if not cur.rowcount:
        raise ValueError("semantic observation not found")
    con.commit()
    return {"id": observation_id, "status": status}
