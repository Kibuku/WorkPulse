"""Consent-based product feedback without work telemetry.

Feedback is always written locally first.  A configured HTTPS endpoint may
receive the same small envelope only after the user presses Send; raw work
history, titles, URLs, paths, captures, and project names are never attached.
"""

from __future__ import annotations

import json
import platform
import urllib.request
from datetime import datetime, timezone
from pathlib import Path

from workpulse import __version__
from workpulse.common import ROOT, ensure_dir, load_config


def _path() -> Path:
    p = ROOT / "logs" / "feedback.jsonl"
    ensure_dir(p.parent)
    return p


def _records() -> list[dict]:
    p = _path()
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except (ValueError, TypeError):
            continue
    return out


def prompt_status(con, *, now: datetime | None = None) -> dict:
    """Prompt after 3 active days or 5 tracked hours, then wait 30 days."""
    now = now or datetime.now(timezone.utc)
    row = con.execute(
        """SELECT COUNT(DISTINCT substr(started_at, 1, 10)) AS active_days,
                  COALESCE(SUM((julianday(ended_at)-julianday(started_at))*24),0) AS hours
             FROM session WHERE ended_at IS NOT NULL"""
    ).fetchone()
    active_days = int(row["active_days"] or 0)
    active_hours = round(float(row["hours"] or 0), 1)
    records = _records()
    opted_out = any(r.get("kind") == "opt_out" for r in records)
    last = next((r for r in reversed(records) if r.get("kind") == "feedback"), None)
    cooldown = False
    if last and last.get("submitted_at"):
        try:
            sent = datetime.fromisoformat(last["submitted_at"])
            cooldown = (now - sent).days < 30
        except (ValueError, TypeError):
            pass
    due = not opted_out and not cooldown and (active_days >= 3 or active_hours >= 5)
    return {"due": due, "active_days": active_days, "active_hours": active_hours,
            "opted_out": opted_out, "cooldown": cooldown}


def opt_out() -> None:
    _append({"kind": "opt_out", "submitted_at": _now()})


def submit(answers: dict, *, cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    allowed = {"rating", "useful", "confusing", "missing", "expected",
               "recommend", "contact_email", "contact_ok"}
    clean = {k: answers.get(k) for k in allowed if k in answers}
    record = {
        "kind": "feedback", "submitted_at": _now(),
        "workpulse_version": __version__, "os": platform.system(),
        "answers": clean,
    }
    _append(record)
    delivered = False
    error = None
    endpoint = ((cfg.get("feedback") or {}).get("endpoint_url") or "").strip()
    if endpoint:
        if not endpoint.lower().startswith("https://"):
            error = "feedback endpoint must use HTTPS"
        else:
            try:
                body = json.dumps(record).encode("utf-8")
                req = urllib.request.Request(endpoint, data=body, method="POST", headers={
                    "Content-Type": "application/json", "User-Agent": f"WorkPulse/{__version__}",
                })
                with urllib.request.urlopen(req, timeout=10) as response:
                    delivered = 200 <= response.status < 300
            except Exception as exc:  # local copy remains the source of truth
                error = f"delivery failed: {type(exc).__name__}"
    return {"saved": True, "delivered": delivered, "delivery_configured": bool(endpoint),
            "error": error}


def _append(record: dict) -> None:
    with _path().open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()
