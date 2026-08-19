"""Evidence-backed personal workflow learning.

The first vertical slice learns a proposal-production method from local file
and foreground evidence. It anonymizes examples before returning them to the UI:
the sequence is real, while client names and filenames remain private.
"""

from __future__ import annotations

import json
import hashlib
import re
import sqlite3
from collections import Counter, defaultdict
from datetime import datetime, timedelta, timezone
from pathlib import Path
from uuid import uuid4


PROPOSAL_METHOD_ID = "learned-proposal-method"

_GENERIC_STEP_NAMES = {
    "research": "Gather context and source material",
    "planning": "Clarify requirements and plan the work",
    "drafting": "Develop the working output",
    "analysis": "Analyse evidence and refine the output",
    "communication": "Coordinate with the people involved",
    "review": "Review and revise the output",
    "finalization": "Finalise or submit the output",
    "administration": "Complete the administrative follow-through",
}

_RELEVANT = (
    "proposal", "concept note", "terms of reference", "tender", "rfeoi",
    "methodology", "technical approach", "budget",
)

_STEP_NAMES = {
    "requirements": "Review the brief and requirements",
    "drafting": "Develop the proposal or concept-note draft",
    "budgeting": "Build the financial proposal or cost model",
    "review": "Revise and version the working draft",
    "finalization": "Package the final or signed output",
}

_LIFECYCLE_ORDER = {
    "requirements": 1,
    "drafting": 2,
    "budgeting": 3,
    "review": 4,
    "finalization": 5,
}

_EXPECTED = {
    "requirements": "Terms of reference, brief or requirements document",
    "drafting": "Proposal or concept-note working document",
    "budgeting": "Financial workbook, budget or cost model",
    "review": "Revised or versioned draft",
    "finalization": "Final, signed or submission-ready file",
}


def _stage(text: str) -> str | None:
    low = text.casefold()
    suffix = Path(text).suffix.casefold()
    if any(k in low for k in ("final", "signed", "submission")):
        return "finalization"
    if re.search(r"\b(revised|revision|rev[\s_-]?\d+|v\d+|r\d+)\b", low):
        return "review"
    if any(k in low for k in ("terms of reference", "tor", "guideline",
                              "request for proposal", "rfeoi", "tender")):
        return "requirements"
    if any(k in low for k in ("budget", "cost model", "pricing")):
        return "budgeting"
    if "financial proposal" in low:
        return "budgeting" if suffix in {".xls", ".xlsx", ".csv"} else "drafting"
    if any(k in low for k in ("proposal", "concept note")):
        return "drafting"
    return None


def _family(text: str) -> str:
    low = text.casefold()
    if any(k in low for k in ("dissertation", "capstone", "scholar")):
        return "academic"
    if any(k in low for k in ("financial", "budget", "pricing")):
        return "financial"
    if "concept note" in low:
        return "concept"
    if any(k in low for k in ("terms of reference", "tender", "rfeoi")):
        return "requirements"
    return "general"


def _events(con: sqlite3.Connection, days: int) -> list[dict]:
    since = (datetime.now(timezone.utc) - timedelta(days=days)).date().isoformat()
    file_like = " OR ".join(["lower(fl.raw_path) LIKE ?" for _ in _RELEVANT])
    session_like = " OR ".join(
        ["lower(sl.raw_title) LIKE ?" for _ in _RELEVANT])
    params = [f"%{term}%" for term in _RELEVANT]
    rows = con.execute(
        f"""
        SELECT f.ts AS ts, fl.raw_path AS value, 'file' AS source
        FROM file_event f
        JOIN file_event_local fl ON fl.file_event_id=f.id
        WHERE substr(f.ts,1,10) >= ? AND ({file_like})
        UNION ALL
        SELECT s.started_at AS ts, sl.raw_title AS value, 'session' AS source
        FROM session s
        JOIN session_local sl ON sl.session_id=s.id
        WHERE substr(s.started_at,1,10) >= ? AND ({session_like})
        ORDER BY ts
        """,
        [since, *params, since, *params],
    ).fetchall()
    out = []
    seen = set()
    for row in rows:
        name = Path(row["value"]).name
        stage = _stage(name)
        if not stage:
            continue
        key = (row["ts"][:10], name.casefold(), stage)
        if key in seen:
            continue
        seen.add(key)
        out.append({
            "ts": row["ts"],
            "stage": stage,
            "family": _family(name),
            "source": row["source"],
        })
    return out


def learn_proposal_method(con: sqlite3.Connection, *, days: int = 45) -> dict:
    events = _events(con, days)
    grouped: dict[str, list[dict]] = defaultdict(list)
    for event in events:
        grouped[event["family"]].append(event)

    journeys = []
    for family, items in grouped.items():
        sequence = []
        for item in sorted(items, key=lambda x: x["ts"]):
            if item["stage"] not in sequence:
                sequence.append(item["stage"])
        if not sequence:
            continue
        journeys.append({
            "family": family,
            "sequence": sequence,
            "started_at": min(i["ts"] for i in items),
            "ended_at": max(i["ts"] for i in items),
            "evidence_markers": len(items),
        })
    journeys.sort(key=lambda j: j["started_at"])

    # Evidence determines which stages exist and their support. A conservative
    # lifecycle scaffold orders the display because filesystem timestamps alone
    # cannot reliably distinguish opening an old final from producing a new one.
    positions: dict[str, list[int]] = defaultdict(list)
    for journey in journeys:
        for position, stage in enumerate(journey["sequence"]):
            positions[stage].append(position)
    ordered = sorted(positions, key=lambda stage: _LIFECYCLE_ORDER[stage])
    examples = [
        {
            "id": f"observed-journey-{i}",
            "label": f"Observed proposal journey {chr(64 + i)}",
            "period": f"{j['started_at'][:10]} to {j['ended_at'][:10]}",
            "evidence_markers": j["evidence_markers"],
            "stages": j["sequence"],
        }
        for i, j in enumerate(journeys, 1)
    ]
    steps = [
        {
            "position": i,
            "action_type": stage,
            "name": _STEP_NAMES[stage],
            "expected_evidence": _EXPECTED[stage],
            "journeys_observed": len(positions[stage]),
            "confidence": round(len(positions[stage]) / max(len(journeys), 1), 2),
            "optional": len(positions[stage]) / max(len(journeys), 1) < 0.5,
        }
        for i, stage in enumerate(ordered, 1)
    ]
    row = con.execute(
        "SELECT status,confirmed_at FROM workflow_method WHERE id=?",
        (PROPOSAL_METHOD_ID,),
    ).fetchone()
    held_out = examples[-1] if examples else None
    common_after_draft = next(
        (s for s in ("review", "finalization")
         if (
             s in ordered
             and held_out
             and s not in held_out["stages"]
             and len(positions[s]) / max(len(journeys), 1) >= 0.5
         )),
        None,
    )
    nudge = None
    if held_out and common_after_draft:
        nudge = (
            f"The held-out journey contains a working draft, but no "
            f"{_EXPECTED[common_after_draft].casefold()} marker. WorkPulse "
            "cannot tell whether that step happened elsewhere or is still due."
        )
    reopened = sum(
        1 for journey in journeys
        if (
            "finalization" in journey["sequence"]
            and "review" in journey["sequence"]
            and journey["sequence"].index("review")
            > journey["sequence"].index("finalization")
        )
    )
    observations = [
        {
            "finding": "Drafting is the strongest repeated stage",
            "evidence": (
                f"Observed in {len(positions.get('drafting', []))} of "
                f"{len(journeys)} journeys."
            ),
        }
    ] if journeys else []
    if reopened:
        observations.append({
            "finding": "Some outputs were revised after a final marker appeared",
            "evidence": (
                f"Observed in {reopened} journey{'s' if reopened != 1 else ''}; "
                "a filename marked final did not always mean the work was closed."
            ),
        })
    if "budgeting" in positions:
        observations.append({
            "finding": "Budgeting appears output-dependent, not universal",
            "evidence": (
                f"Observed in {len(positions['budgeting'])} of {len(journeys)} "
                "journeys, so it is treated as optional."
            ),
        })
    return {
        "enabled": len(journeys) >= 2 and len(steps) >= 2,
        "is_demo": False,
        "privacy": "Examples are anonymized; the method is learned from real local evidence.",
        "method_id": PROPOSAL_METHOD_ID,
        "name": "Your observed proposal-production method",
        "output_type": "Proposal or concept note",
        "status": row["status"] if row else "candidate",
        "confirmed_at": row["confirmed_at"] if row else None,
        "basis": (
            f"Candidate mined from {len(journeys)} real local output journeys "
            f"and {len(events)} distinct evidence markers."
        ),
        "examples": examples,
        "steps": steps,
        "observations": observations,
        "held_out": held_out,
        "nudge": nudge,
        "gap": (
            "File and window evidence can show sequence and revision behaviour. "
            "It cannot yet see the reasoning used to choose a methodology, and "
            "timestamps alone cannot prove that every opened file was actively edited."
        ),
        "ordering_note": (
            "Observed evidence determines stage inclusion and confidence. A "
            "conservative proposal lifecycle orders the display; exceptions are "
            "reported instead of silently forced into that order."
        ),
    }


def answer_proposal_question(con: sqlite3.Connection, *, days: int = 45) -> dict:
    """Answer a method question from learned workflow memory, without asking
    an LLM to reconstruct the method from unrelated activity atoms."""
    learned = learn_proposal_method(con, days=days)
    if not learned["enabled"]:
        return {
            "answer": (
                "WorkPulse hasn't observed enough distinct proposal journeys "
                "to describe your method yet."
            ),
            "gap": (
                "At least two proposal journeys with two or more recognizable "
                "stages need to be observed before a candidate method is shown."
            ),
            "workflow": learned,
        }

    status = "confirmed method" if learned["status"] == "confirmed" else "candidate pattern"
    lines = [
        f"WorkPulse has a **{status}** based on "
        f"{len(learned['examples'])} anonymized proposal journeys:",
        "",
    ]
    for step in learned["steps"]:
        qualifier = " — optional" if step["optional"] else ""
        lines.append(
            f"{step['position']}. **{step['name']}**{qualifier}  \n"
            f"   Seen in {step['journeys_observed']} of "
            f"{len(learned['examples'])} journeys; marker: "
            f"{step['expected_evidence']}."
        )
    if learned["observations"]:
        lines += ["", "**What the evidence specifically shows:**"]
        for observation in learned["observations"]:
            lines.append(
                f"- {observation['finding']}. {observation['evidence']}"
            )
    if learned["status"] != "confirmed":
        lines += [
            "",
            "This is still a candidate. Confirm or correct it in the Workflow "
            "Learner before WorkPulse treats it as how you work.",
        ]
    return {
        "answer": "\n".join(lines),
        "gap": learned["gap"],
        "workflow": learned,
    }


def confirm_proposal_method(con: sqlite3.Connection, *, days: int = 45) -> dict:
    learned = learn_proposal_method(con, days=days)
    if not learned["enabled"]:
        raise ValueError("not enough observed proposal evidence")
    now = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO workflow_method
          (id,name,output_type,scope,status,confidence,version,is_demo,
           created_at,confirmed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET
          status='confirmed', confidence=excluded.confidence,
          version=workflow_method.version+1, confirmed_at=excluded.confirmed_at
        """,
        (PROPOSAL_METHOD_ID, learned["name"], learned["output_type"],
         "personal", "confirmed",
         round(sum(s["confidence"] for s in learned["steps"]) /
               max(len(learned["steps"]), 1), 2),
         1, 0, now, now),
    )
    con.execute("DELETE FROM workflow_step WHERE method_id=?",
                (PROPOSAL_METHOD_ID,))
    for step in learned["steps"]:
        con.execute(
            """
            INSERT INTO workflow_step
              (id,method_id,position,action_type,name,required,expected_evidence)
            VALUES (?,?,?,?,?,?,?)
            """,
            (f"{PROPOSAL_METHOD_ID}-step-{step['position']}",
             PROPOSAL_METHOD_ID, step["position"], step["action_type"],
             step["name"], 1, step["expected_evidence"]),
        )
    con.execute(
        "INSERT INTO workflow_decision(id,method_id,action,payload,created_at) "
        "VALUES (?,?,?,?,?)",
        (str(uuid4()), PROPOSAL_METHOD_ID, "confirm",
         json.dumps({"basis": learned["basis"], "privacy": learned["privacy"]}),
         now),
    )
    con.commit()
    return learn_proposal_method(con, days=days)


def learn_repeated_methods(con: sqlite3.Connection, *, days: int = 60) -> dict:
    """Induce real methods from repeated semantic journeys, without templates."""
    since = (datetime.now(timezone.utc) - timedelta(days=max(7, min(days, 180)))).date().isoformat()
    rows = con.execute(
        """
        SELECT o.observed_date,o.semantic_key,o.stream,o.first_seen,o.evidence_count,
               COALESCE(s.label,o.stream,'Unfiled work') AS stream_label
        FROM semantic_observation o LEFT JOIN stream s ON s.key=o.stream
        WHERE o.kind='stage' AND o.status!='dismissed' AND o.observed_date>=?
        ORDER BY o.observed_date,o.first_seen
        """, (since,)
    ).fetchall()
    grouped: dict[tuple[str, str], list[dict]] = defaultdict(list)
    labels = {}
    for row in rows:
        stream = row["stream"] or "unfiled"
        grouped[(stream, row["observed_date"])].append(dict(row))
        labels[stream] = row["stream_label"]

    by_stream: dict[str, list[dict]] = defaultdict(list)
    for (stream, day), items in grouped.items():
        sequence = []
        for item in items:
            if item["semantic_key"] not in sequence:
                sequence.append(item["semantic_key"])
        if len(sequence) >= 2:
            by_stream[stream].append({
                "date": day, "sequence": sequence,
                "evidence_markers": sum(int(x["evidence_count"] or 0) for x in items),
            })

    methods = []
    for stream, journeys in by_stream.items():
        if len(journeys) < 2:
            continue
        journeys.sort(key=lambda x: x["date"])
        support = Counter(stage for journey in journeys for stage in set(journey["sequence"]))
        positions: dict[str, list[int]] = defaultdict(list)
        for journey in journeys:
            for pos, stage in enumerate(journey["sequence"]):
                positions[stage].append(pos)
        common = [stage for stage, count in support.items() if count >= 2]
        common.sort(key=lambda stage: sum(positions[stage]) / len(positions[stage]))
        if len(common) < 2:
            continue
        method_id = "learned-" + hashlib.sha256(stream.encode()).hexdigest()[:16]
        saved = con.execute(
            "SELECT status,confirmed_at FROM workflow_method WHERE id=?", (method_id,)
        ).fetchone()
        examples = [{
            "id": f"{method_id}-journey-{i}",
            "label": f"Observed {labels[stream]} journey {i}",
            "period": journey["date"],
            "evidence_markers": journey["evidence_markers"],
            "stages": journey["sequence"],
        } for i, journey in enumerate(journeys, 1)]
        steps = [{
            "position": i,
            "action_type": stage,
            "name": _GENERIC_STEP_NAMES.get(stage, stage.replace("-", " ").title()),
            "expected_evidence": f"Repeated {stage.replace('-', ' ')} activity",
            "journeys_observed": support[stage],
            "confidence": round(support[stage] / len(journeys), 2),
            "optional": support[stage] / len(journeys) < 0.6,
        } for i, stage in enumerate(common, 1)]
        held = examples[-1]
        missing = next((stage for stage in common if stage not in held["stages"]), None)
        methods.append({
            "enabled": True, "is_demo": False, "method_id": method_id,
            "stream": stream, "name": f"Your observed {labels[stream]} method",
            "output_type": labels[stream],
            "status": saved["status"] if saved else "candidate",
            "confirmed_at": saved["confirmed_at"] if saved else None,
            "basis": (f"Learned from {len(journeys)} separate real journeys and "
                      f"{sum(j['evidence_markers'] for j in journeys)} evidence markers."),
            "privacy": "Built from local, redacted observations; raw titles and content are not displayed.",
            "examples": examples, "steps": steps, "held_out": held,
            "nudge": (f"The latest journey has not yet shown the usual {missing} stage. "
                      "It may be incomplete, intentionally different, or recorded elsewhere.") if missing else
                     "The latest journey contains the repeatedly observed stages.",
            "observations": [{
                "finding": f"{_GENERIC_STEP_NAMES.get(stage, stage.title())} repeats",
                "evidence": f"Observed in {support[stage]} of {len(journeys)} journeys.",
            } for stage in common],
            "gap": "WorkPulse learns from observable evidence and corrections; it cannot prove intent or quality.",
            "ordering_note": "Step order is the average observed order across journeys, not a predefined lifecycle.",
        })
    methods.sort(key=lambda m: (-len(m["examples"]), -sum(x["evidence_markers"] for x in m["examples"])))
    return {"enabled": bool(methods), "methods": methods,
            "learning": {"journeys_seen": sum(len(v) for v in by_stream.values()),
                         "minimum_journeys": 2}}


def confirm_repeated_method(con: sqlite3.Connection, method_id: str, *, days: int = 60) -> dict:
    learned = learn_repeated_methods(con, days=days)
    method = next((m for m in learned["methods"] if m["method_id"] == method_id), None)
    if not method:
        raise ValueError("learned method not found or no longer supported by evidence")
    now = datetime.now(timezone.utc).isoformat()
    confidence = round(sum(s["confidence"] for s in method["steps"]) / len(method["steps"]), 2)
    con.execute(
        """INSERT INTO workflow_method
        (id,name,output_type,scope,status,confidence,version,is_demo,created_at,confirmed_at)
        VALUES (?,?,?,?,?,?,?,?,?,?)
        ON CONFLICT(id) DO UPDATE SET status='confirmed',confidence=excluded.confidence,
        version=workflow_method.version+1,confirmed_at=excluded.confirmed_at""",
        (method_id, method["name"], method["output_type"], "personal", "confirmed",
         confidence, 1, 0, now, now))
    con.execute("DELETE FROM workflow_step WHERE method_id=?", (method_id,))
    for step in method["steps"]:
        con.execute("""INSERT INTO workflow_step
        (id,method_id,position,action_type,name,required,expected_evidence)
        VALUES (?,?,?,?,?,?,?)""",
        (f"{method_id}-step-{step['position']}", method_id, step["position"],
         step["action_type"], step["name"], int(not step["optional"]), step["expected_evidence"]))
    con.execute("INSERT INTO workflow_decision(id,method_id,action,payload,created_at) VALUES (?,?,?,?,?)",
                (str(uuid4()), method_id, "confirm", json.dumps({"basis": method["basis"]}), now))
    con.commit()
    return next(m for m in learn_repeated_methods(con, days=days)["methods"]
                if m["method_id"] == method_id)


def answer_repeated_method_question(con: sqlite3.Connection, question: str, *, days: int = 60) -> dict:
    learned = learn_repeated_methods(con, days=days)
    if not learned["methods"]:
        return {"answer": "WorkPulse has not observed the same multi-stage journey often enough to describe a personal method yet.",
                "gap": "At least two separate multi-stage journeys in the same stream are required.",
                "workflow": None, "learning": learned["learning"]}
    low = question.casefold()
    method = next((m for m in learned["methods"]
                   if m["output_type"].casefold() in low or m["stream"].replace("-", " ") in low),
                  learned["methods"][0])
    lines = [f"WorkPulse has observed a **{method['status']} method** from {len(method['examples'])} repeated {method['output_type']} journeys:", ""]
    for step in method["steps"]:
        qualifier = " (optional)" if step["optional"] else ""
        lines.append(f"{step['position']}. **{step['name']}**{qualifier} — seen in {step['journeys_observed']} of {len(method['examples'])} journeys.")
    if method["status"] != "confirmed":
        lines += ["", "This remains a candidate until you confirm or correct it."]
    return {"answer": "\n".join(lines), "gap": method["gap"],
            "workflow": method, "learning": learned["learning"]}
