"""Build the small correction inbox that teaches WorkPulse.

The inbox is deliberately selective. It does not ask the user to label every
window; it surfaces only decisions where WorkPulse is unsure or where a named
output contradicts the project assignment.
"""

from __future__ import annotations

import sqlite3
from datetime import date, timedelta

from workpulse.core import cluster_context, projects

_NOISE_TITLES = {
    "unclear work block",
    "sign in",
    "sign in - google accounts",
    "new tab",
    "loading",
}


def _kind(*, source: str, confidence: float | None, assigned: str | None,
          suggested: str | None) -> tuple[str, str] | None:
    if suggested and assigned and suggested != assigned:
        return (
            "contradiction",
            "The output name points to a different project than the current filing.",
        )
    if source in {"fallback", "unassigned"} or not assigned:
        return (
            "unassigned",
            "WorkPulse does not have enough evidence to file this work yet.",
        )
    if source == "agent" and confidence is not None and confidence < 0.65:
        return (
            "low_confidence",
            "WorkPulse made this filing with limited evidence.",
        )
    return None


def inbox(con: sqlite3.Connection, *, as_of: date | None = None,
          days: int = 7, cfg: dict | None = None,
          include_private: bool = False, limit: int = 12) -> dict:
    end = as_of or date.today()
    start = end - timedelta(days=max(days, 1) - 1)
    private = set()
    if not include_private:
        private = {r["key"] for r in con.execute(
            "SELECT key FROM stream WHERE is_private = 1"
        ).fetchall()}
    labels = {r["key"]: r["label"] for r in con.execute(
        "SELECT key, label FROM stream"
    ).fetchall()}
    taxonomy = projects.load_projects(cfg)
    project_labels = {p["stream"]: p["label"] for p in taxonomy}

    def label_for(stream: str | None) -> str | None:
        if not stream:
            return None
        label = labels.get(stream)
        if not label or label.casefold() == stream.casefold():
            return project_labels.get(stream, label or stream)
        return label
    rows = con.execute(
        """
        SELECT jv.cluster_id, jv.started_at, jv.ended_at, jv.total_seconds,
               jv.stream AS raw_stream, ca.stream, ca.confidence, ca.source
        FROM job_view jv
        LEFT JOIN cluster_assignment ca ON ca.cluster_id = jv.cluster_id
        WHERE substr(jv.started_at, 1, 10) <= ?
          AND substr(jv.ended_at, 1, 10) >= ?
        ORDER BY jv.ended_at DESC
        """,
        (end.isoformat(), start.isoformat()),
    ).fetchall()

    items = []
    for row in rows:
        source = row["source"] or "unassigned"
        assigned = None if source == "fallback" else (
            row["stream"] or row["raw_stream"])
        if assigned in private:
            continue
        ctx = cluster_context.cluster_context(
            con, cluster_id=row["cluster_id"], cfg=cfg)
        output = cluster_context.infer_output(
            ctx, label_for(assigned) or "Needs review")
        if output["title"].strip().casefold() in _NOISE_TITLES:
            continue
        match = projects.resolve_match(output["title"], taxonomy)
        suggested = match["stream"] if match else None
        review = _kind(
            source=source,
            confidence=row["confidence"],
            assigned=assigned,
            suggested=suggested,
        )
        if review is None:
            continue
        kind, reason = review
        items.append({
            "cluster_id": row["cluster_id"],
            "cluster_ids": [row["cluster_id"]],
            "instances": 1,
            "kind": kind,
            "reason": reason,
            "title": output["title"],
            "evidence_label": output["evidence_label"],
            "started_at": row["started_at"],
            "ended_at": row["ended_at"],
            "minutes": round(float(row["total_seconds"] or 0) / 60.0, 1),
            "assigned_stream": assigned,
            "assigned_label": label_for(assigned) or "Not filed",
            "suggested_stream": suggested,
            "suggested_label": label_for(suggested) if match else None,
            "confidence": row["confidence"],
        })

    # Repeated blocks with the same decision become one question. Confirming
    # "Uganda MEMD work block" should not require answering it three times.
    grouped: dict[tuple, dict] = {}
    for item in items:
        key = (
            item["kind"],
            item["title"].strip().casefold(),
            item["assigned_stream"],
            item["suggested_stream"],
        )
        existing = grouped.get(key)
        if existing is None:
            grouped[key] = item
            continue
        existing["cluster_ids"].append(item["cluster_id"])
        existing["instances"] += 1
        existing["minutes"] = round(existing["minutes"] + item["minutes"], 1)
        if item["started_at"] > existing["started_at"]:
            existing["started_at"] = item["started_at"]
            existing["ended_at"] = item["ended_at"]

    items = list(grouped.values())
    priority = {"contradiction": 0, "unassigned": 1, "low_confidence": 2}
    items.sort(key=lambda item: (
        priority[item["kind"]], -item["minutes"], item["started_at"]))
    items = items[:limit]
    return {
        "window": {"start": start.isoformat(), "end": end.isoformat()},
        "count": len(items),
        "items": items,
    }
