"""Build a local, privacy-aware chronology for one day.

Timeline is not a raw surveillance log. It combines meaningful work blocks and
calendar commitments, then keeps app transitions as supporting evidence behind
each block.
"""

from __future__ import annotations

import json
import sqlite3
from collections import defaultdict
from datetime import date, datetime

from workpulse.core import cluster_context


def _assignment_explanation(source: str, evidence_raw: str | None) -> tuple[str, str]:
    try:
        evidence = json.loads(evidence_raw or "[]")
    except (TypeError, ValueError):
        evidence = []
    if source == "user":
        return "Confirmed by you", "Your correction is the source of truth."
    if source == "fallback":
        return "Needs review", "Not enough evidence to assign this work block."
    ai = next((e for e in evidence if e.get("signal") == "local_ai"), None)
    if ai:
        return (f"Ollama · {ai.get('model') or 'local model'}",
                ai.get("detail") or "Local AI classification")
    names = {
        "calendar_event": "Calendar match",
        "browser_visit": "Browser match",
        "file_path": "File-path match",
        "capture": "Capture match",
        "title_keyword": "Title keyword match",
        "plan_item": "Plan match",
        "existing_stream": "Existing project rule",
    }
    first = evidence[0] if evidence else {}
    return (names.get(first.get("signal"), "WorkPulse rules"),
            first.get("detail") or "Deterministic local signals")


def _seconds(started_at: str, ended_at: str) -> float:
    try:
        return max(0.0, (datetime.fromisoformat(ended_at) -
                         datetime.fromisoformat(started_at)).total_seconds())
    except (TypeError, ValueError):
        return 0.0


def _attention_chapters(con: sqlite3.Connection, day: str,
                        valid_clusters: set[str]) -> list[dict]:
    """Summarise foreground truth into 15-minute attention chapters.

    Every switch remains in the evidence count, but sub-minute flickers do not
    become first-class timeline cards.
    """
    rows = con.execute(
        """
        SELECT id, cluster_id, app, started_at, ended_at
        FROM session
        WHERE substr(started_at, 1, 10) = ? AND ended_at IS NOT NULL
        ORDER BY started_at
        """,
        (day,),
    ).fetchall()
    buckets: dict[datetime, list[dict]] = defaultdict(list)
    for row in rows:
        if row["cluster_id"] not in valid_clusters:
            continue
        seconds = _seconds(row["started_at"], row["ended_at"])
        if seconds <= 0:
            continue
        dt = datetime.fromisoformat(row["started_at"])
        bucket = dt.replace(minute=(dt.minute // 15) * 15,
                            second=0, microsecond=0)
        buckets[bucket].append({**dict(row), "seconds": seconds})

    chapters: list[dict] = []
    for _, items in sorted(buckets.items()):
        by_cluster: dict[str, float] = defaultdict(float)
        app_seconds: dict[str, float] = defaultdict(float)
        for item in items:
            by_cluster[item["cluster_id"]] += item["seconds"]
            app_seconds[item["app"]] += item["seconds"]
        dominant = max(by_cluster, key=by_cluster.get)
        sequence = [item["app"] for item in items]
        switches = sum(sequence[i] != sequence[i - 1]
                       for i in range(1, len(sequence)))
        chapter = {
            "cluster_id": dominant,
            "started_at": min(i["started_at"] for i in items),
            "ended_at": max(i["ended_at"] for i in items),
            "seconds": sum(i["seconds"] for i in items),
            "apps_sequence": sequence,
            "app_seconds": app_seconds,
            "app_switches": switches,
        }
        current = chapters[-1] if chapters else None
        gap = (_seconds(current["ended_at"], chapter["started_at"])
               if current else None)
        if (current and current["cluster_id"] == dominant
                and gap is not None and gap <= 20 * 60):
            if current["apps_sequence"][-1] != sequence[0]:
                current["app_switches"] += 1
            current["app_switches"] += switches
            current["ended_at"] = chapter["ended_at"]
            current["seconds"] += chapter["seconds"]
            current["apps_sequence"].extend(sequence)
            for app, seconds in app_seconds.items():
                current["app_seconds"][app] += seconds
        else:
            chapters.append(chapter)
    return chapters


def _browser_domains(con: sqlite3.Connection, start: str, end: str) -> list[dict]:
    rows = con.execute(
        """
        SELECT domain, COUNT(*) AS visits
        FROM browser_visit
        WHERE ts BETWEEN ? AND ? AND is_private = 0
        GROUP BY domain ORDER BY visits DESC LIMIT 4
        """,
        (start, end),
    ).fetchall()
    return [dict(r) for r in rows]


def build_day(con: sqlite3.Connection, on: date, *, cfg: dict | None = None,
              include_private: bool = False) -> dict:
    day = on.isoformat()
    private = set()
    if not include_private:
        private = {r["key"] for r in con.execute(
            "SELECT key FROM stream WHERE is_private = 1"
        ).fetchall()}

    labels = {r["key"]: r["label"] for r in con.execute(
        "SELECT key, label FROM stream"
    ).fetchall()}
    events: list[dict] = []
    total_seconds = 0.0
    total_switches = 0

    clusters = con.execute(
        """
        SELECT jv.cluster_id, jv.started_at, jv.ended_at, jv.total_seconds,
               jv.stream AS raw_stream, ca.stream, ca.confidence,
               ca.source, ca.evidence
        FROM job_view jv
        LEFT JOIN cluster_assignment ca ON ca.cluster_id = jv.cluster_id
        WHERE substr(jv.started_at, 1, 10) <= ?
          AND substr(jv.ended_at, 1, 10) >= ?
        ORDER BY jv.started_at
        """,
        (day, day),
    ).fetchall()
    cluster_meta: dict[str, dict] = {}
    for row in clusters:
        source = row["source"] or "unassigned"
        stream = None if source == "fallback" else (row["stream"] or row["raw_stream"])
        if stream in private:
            continue
        label = labels.get(stream, stream) if stream else "Needs review"
        ctx = cluster_context.cluster_context(
            con, cluster_id=row["cluster_id"], cfg=cfg)
        output = cluster_context.infer_output(ctx, label)
        method, reason = _assignment_explanation(source, row["evidence"])
        cluster_meta[row["cluster_id"]] = {
            "ctx": ctx,
            "output": output,
            "stream": stream,
            "label": label,
            "confidence": row["confidence"],
            "source": source,
            "method": method,
            "reason": reason,
        }

    episodes = _attention_chapters(con, day, set(cluster_meta))
    previous_app = None
    for episode in episodes:
        meta = cluster_meta.get(episode["cluster_id"])
        if not meta:
            continue
        ctx = meta["ctx"]
        seconds = episode["seconds"]
        total_seconds += seconds
        if previous_app and episode["apps_sequence"][0] != previous_app:
            total_switches += 1
        total_switches += episode["app_switches"]
        previous_app = episode["apps_sequence"][-1]
        apps = [
            {"app": app, "minutes": round(seconds_for_app / 60, 1)}
            for app, seconds_for_app in sorted(
                episode["app_seconds"].items(), key=lambda x: -x[1])[:5]
        ]
        events.append({
            "kind": "work",
            "id": episode["cluster_id"],
            "started_at": episode["started_at"],
            "ended_at": episode["ended_at"],
            "minutes": round(seconds / 60, 1),
            "title": meta["output"]["title"],
            "title_evidence": meta["output"]["evidence_label"],
            "project": meta["stream"],
            "project_label": meta["label"],
            "confidence": meta["confidence"],
            "assignment_source": meta["source"],
            "attribution_method": meta["method"],
            "attribution_reason": meta["reason"],
            "apps": apps,
            "app_switches": episode["app_switches"],
            "browser_domains": _browser_domains(
                con, episode["started_at"], episode["ended_at"]),
            "file_events": ctx["file_events"]["total"],
            "captures": len(ctx["captures"]),
        })

    meetings = con.execute(
        """
        SELECT ce.id, ce.started_at, ce.ended_at, ce.stream,
               cel.raw_title, cel.raw_location
        FROM calendar_event ce
        JOIN calendar_event_local cel ON cel.event_id = ce.id
        WHERE substr(ce.started_at, 1, 10) <= ?
          AND substr(ce.ended_at, 1, 10) >= ?
        ORDER BY ce.started_at
        """,
        (day, day),
    ).fetchall()
    for row in meetings:
        if row["stream"] in private:
            continue
        events.append({
            "kind": "meeting",
            "id": row["id"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "title": row["raw_title"] or "Calendar commitment",
            "location": row["raw_location"],
            "project": row["stream"],
            "project_label": labels.get(row["stream"], row["stream"])
                             if row["stream"] else "Needs review",
        })

    events.sort(key=lambda e: (e["started_at"], e["kind"] != "meeting"))
    return {
        "date": day,
        "events": events,
        "work_blocks": sum(e["kind"] == "work" for e in events),
        "meetings": sum(e["kind"] == "meeting" for e in events),
        "tracked_hours": round(total_seconds / 3600, 1),
        "app_switches": total_switches,
    }
