"""
coach.py — the policy probe. PLAN.md §7 step 10.

The coach is the only surface where the brain pushes instead of pulls. The
whole step 10 principle ("popup last") is enforced HERE: until the user has
earned the right by pulling `wp think` on enough distinct days, the coach
stays silent. This module computes the gate and the next message; surface
delivery (tray, banner, email) is intentionally deferred to step 10b after
the gate has opened at least once.

The judgment lives in skills/coach.md (markdown is code, principle #5).
Parameters in the YAML frontmatter, voice rules and refuse-cases in prose.

Public API:
    load_params() -> dict
    is_gated_on(con, *, as_of=None) -> tuple[bool, dict]
        returns (gate_open, {distinct_days, window_days, threshold, ...})
    next_message(con, *, as_of=None, cfg=None) -> dict | None
        the single message the coach would say today, or None
    status(con, *, as_of=None, cfg=None) -> dict
        diagnostic — params + gate + would-say + reasoning

CLI:
    python -m workpulse.core.coach status            # diagnostic dump
    python -m workpulse.core.coach next              # the would-say message, or "(silent)"
    python -m workpulse.core.coach gate              # just the gate state
"""

from __future__ import annotations

import argparse
import json
import re
import sqlite3
import sys
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import yaml

from workpulse.core import consolidate, db
from workpulse.common import ROOT, load_config, PKG


_SKILL_PATH = PKG / "skills" / "coach.md"
_FRONTMATTER_RE = re.compile(r"^---\n(.*?\n)---\n", re.DOTALL)

_DEFAULTS = {
    "gate_window_days":        14,
    "gate_distinct_days":       5,
    "max_messages_per_day":     1,
    "quiet_hours_start":       20,
    "quiet_hours_end":          8,
    "plan_overrun_floor_min":  30,
    "untagged_bucket_floor_min": 60,
    "stale_tag_floor_days":    90,
    "dedup_jaccard_floor":      0.6,
    "enabled":                 True,
}


# ── params ──────────────────────────────────────────────────────────────────

def load_params(skill_path: Path | None = None) -> dict:
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


# ── gate ────────────────────────────────────────────────────────────────────

def is_gated_on(con: sqlite3.Connection, *,
                as_of: date | None = None,
                params: dict | None = None) -> tuple[bool, dict]:
    """Has the user earned the right to be coached?

    Count distinct dates with a human-initiated `wp think` invocation in
    the last `gate_window_days`. A `wp think` invocation writes a
    `skill_run` row with `skill_slug = 'think'`. consolidate and report
    runners use `_call_anthropic` directly under their own slugs, so they
    don't count.
    """
    p = params or load_params()
    today = as_of or datetime.now(timezone.utc).date()
    if not p.get("enabled", True):
        return False, {"reason": "disabled in skills/coach.md", "params": p}
    since = (today - timedelta(days=int(p["gate_window_days"]))).isoformat()
    rows = con.execute(
        """
        SELECT COUNT(DISTINCT substr(ts, 1, 10)) AS n
        FROM skill_run
        WHERE skill_slug = 'think'
          AND parent_run_id IS NULL
          AND ts >= ?
        """,
        (since,),
    ).fetchone()
    distinct_days = int(rows["n"] or 0)
    threshold = int(p["gate_distinct_days"])
    info = {
        "distinct_days":  distinct_days,
        "window_days":    int(p["gate_window_days"]),
        "threshold":      threshold,
        "since":          since,
        "as_of":          today.isoformat(),
    }
    return distinct_days >= threshold, info


# ── budget: already spoken today? ───────────────────────────────────────────

def _already_spoken_today(con: sqlite3.Connection, today: date) -> bool:
    """The coach records every successful message as a skill_run with
    skill_slug='coach' and status='ok'. One per day, by policy."""
    n = con.execute(
        """
        SELECT COUNT(*) AS n FROM skill_run
        WHERE skill_slug = 'coach'
          AND status = 'ok'
          AND substr(ts, 1, 10) = ?
        """,
        (today.isoformat(),),
    ).fetchone()["n"]
    return int(n) > 0


# ── content selection (priority cascade, deterministic) ─────────────────────

def _pick_message(findings: dict, p: dict) -> dict | None:
    """Walk the priority order from skills/coach.md and return the first
    finding that clears its floor. Returns None on silence."""
    pva = findings.get("plan_vs_actual") or []
    # 1. Plan overrun
    for it in pva:
        if it["flag"] == "overrun" and it["actual_min"] - it["planned_min"] \
                >= int(p["plan_overrun_floor_min"]):
            extra = round(it["actual_min"] - it["planned_min"], 0)
            return {
                "kind":   "plan_overrun",
                "text":   (f"You logged {it['actual_min']:.0f}m on "
                           f"'{it['name']}' — planned {it['planned_min']}m. "
                           f"Same project, or did this turn into something else?"),
                "atom":   None,
                "facts":  it,
            }
    # 2. Plan no-show
    for it in pva:
        if it["flag"] == "underrun" and not it["done"] and it["actual_min"] == 0:
            return {
                "kind":  "plan_noshow",
                "text":  (f"'{it['name']}' was on today's plan and didn't get "
                          f"touched. Carry it over, or drop it?"),
                "atom":  None,
                "facts": it,
            }
    # 3. Big untagged bucket
    for b in findings.get("untagged_buckets") or []:
        if b["minutes"] >= float(p["untagged_bucket_floor_min"]):
            sample = (b.get("sample_titles") or ["(no titles)"])[0]
            return {
                "kind":  "untagged_bucket",
                "text":  (f"{b['minutes']:.0f}m of untagged time on "
                          f"'{sample}' — want to tag it?"),
                "atom":  None,
                "facts": b,
            }
    # 4. High-confidence dedup pair
    for d in findings.get("dedup_candidates") or []:
        if d["jaccard"] >= float(p["dedup_jaccard_floor"]) and d["same_stream"]:
            return {
                "kind":  "dedup",
                "text":  (f"'{d['a']['name']}' and '{d['b']['name']}' look "
                          f"like the same work (Jaccard {d['jaccard']}). Merge?"),
                "atom":  None,
                "facts": d,
            }
    # 5. Stale tag (lowest priority, only if nothing else fires)
    for t in findings.get("stale_learned_tags") or []:
        if t["age_days"] >= int(p["stale_tag_floor_days"]):
            return {
                "kind":  "stale_tag",
                "text":  (f"Tag rule `{t['pattern']}` → {t['stream']} hasn't "
                          f"fired in {t['age_days']} days. Retire it?"),
                "atom":  None,
                "facts": t,
            }
    return None


def _content_message(con: sqlite3.Connection, today: date) -> dict | None:
    """A cautious coaching prompt from the owner's latest redacted capture."""
    row = con.execute(
        "SELECT id,stage,redacted_text FROM content_capture WHERE substr(ts,1,10)=? "
        "ORDER BY ts DESC LIMIT 1", (today.isoformat(),)
    ).fetchone()
    if not row:
        return None
    text = row["redacted_text"] or ""
    low = text.casefold()
    if any(term in low for term in ("todo", "tbd", "to be confirmed", "missing", "placeholder")):
        prompt = "The active draft still contains an unresolved placeholder. Would you like to turn it into a concrete next action before moving on?"
        kind = "content-placeholder"
    elif row["stage"] == "review":
        prompt = "You appear to be reviewing an output. Is the next decision to revise it, approve it, or request clarification?"
        kind = "content-review"
    elif row["stage"] == "drafting":
        prompt = "You appear to be drafting an output. What requirement should this section satisfy before you consider it complete?"
        kind = "content-drafting"
    else:
        return None
    return {"kind": kind, "text": prompt, "atom": row["id"],
            "facts": {"stage": row["stage"], "source": "redacted-local-content"}}


# ── public: next_message + status ───────────────────────────────────────────

def next_message(con: sqlite3.Connection, *,
                 as_of: date | None = None,
                 cfg: dict | None = None) -> dict | None:
    """Return the single message the coach would say today, or None.

    Records a skill_run row only when a message would actually fire
    (status='ok'). Silent passes do not record — we don't want a daily
    "coach was silent" log polluting the audit table.
    """
    p = load_params()
    today = as_of or datetime.now(timezone.utc).date()
    gate_open, gate_info = is_gated_on(con, as_of=today, params=p)
    if not gate_open:
        return None
    if _already_spoken_today(con, today):
        return None
    findings = consolidate.findings(con, as_of=today, cfg=cfg)
    msg = _content_message(con, today) or _pick_message(findings, p)
    if msg is None:
        return None

    # Record the would-fire (callers can mark it as fired or shown later).
    # Use `today` (the as_of date) for the date portion of ts so the budget
    # check via _already_spoken_today sees this record on subsequent calls
    # even when as_of != real-time today. Time portion stays real so the
    # audit ordering is honest.
    from workpulse.core import atoms
    sr_id = atoms.new_id()
    now = datetime.now(timezone.utc)
    if today != now.date():
        rec_ts = datetime.combine(today, now.time(), tzinfo=timezone.utc).isoformat()
    else:
        rec_ts = now.isoformat()
    con.execute(
        """
        INSERT INTO skill_run(id, ts, skill_slug, parent_run_id, model,
                              in_tokens, out_tokens, cost_usd,
                              input, output, status)
        VALUES (?, ?, 'coach', NULL, NULL, 0, 0, 0, ?, ?, 'ok')
        """,
        (sr_id, rec_ts,
         json.dumps({"kind": msg["kind"]})[:2000], msg["text"][:8000]),
    )
    msg["skill_run"] = sr_id
    msg["gate"] = gate_info
    return msg


def status(con: sqlite3.Connection, *,
           as_of: date | None = None, cfg: dict | None = None) -> dict:
    """Diagnostic: params + gate + would-say + the reasoning trail.
    Does NOT record a skill_run — this is the introspection path."""
    p = load_params()
    today = as_of or datetime.now(timezone.utc).date()
    gate_open, gate_info = is_gated_on(con, as_of=today, params=p)
    findings = consolidate.findings(con, as_of=today, cfg=cfg)
    if not p.get("enabled", True):
        return {"params": p, "gate": gate_info, "would_say": None,
                "reason": "disabled in skills/coach.md"}
    if not gate_open:
        return {
            "params": p, "gate": gate_info, "would_say": None,
            "reason": (f"gate closed: {gate_info['distinct_days']} of "
                       f"{gate_info['threshold']} distinct-day `wp think` "
                       f"invocations in last {gate_info['window_days']} days"),
        }
    if _already_spoken_today(con, today):
        return {"params": p, "gate": gate_info, "would_say": None,
                "reason": "already spoken today (max_messages_per_day=1)"}
    msg = _content_message(con, today) or _pick_message(findings, p)
    if msg is None:
        return {"params": p, "gate": gate_info, "would_say": None,
                "reason": "no finding cleared its floor"}
    return {"params": p, "gate": gate_info, "would_say": msg,
            "reason": f"selected: {msg['kind']}"}


# ── CLI ─────────────────────────────────────────────────────────────────────

def _cli_status() -> int:
    con = db.connect(load_config())
    s = status(con, cfg=load_config())
    print("coach status")
    print(f"  enabled:        {s['params']['enabled']}")
    print(f"  gate window:    {s['gate']['window_days']} days")
    print(f"  threshold:      {s['gate']['threshold']} distinct days")
    print(f"  observed:       {s['gate']['distinct_days']} distinct days "
          f"with `wp think` since {s['gate']['since'][:10]}")
    print(f"  gate open:      "
          f"{s['gate']['distinct_days'] >= s['gate']['threshold']}")
    print(f"  reason:         {s['reason']}")
    if s["would_say"]:
        print()
        print(f"would say ({s['would_say']['kind']}):")
        print(f"  {s['would_say']['text']}")
    else:
        print()
        print("(silent today)")
    return 0


def _cli_next() -> int:
    con = db.connect(load_config())
    msg = next_message(con, cfg=load_config())
    if msg is None:
        print("(silent)")
        return 0
    print(msg["text"])
    return 0


def _cli_gate() -> int:
    con = db.connect(load_config())
    open_, info = is_gated_on(con)
    print(f"gate_open={open_}  "
          f"distinct_days={info['distinct_days']}/{info['threshold']}  "
          f"window={info['window_days']}d")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp coach",
                                     description="Coach gate + would-say.")
    parser.add_argument("command", nargs="?", default="status",
                        choices=("status", "next", "gate"))
    args = parser.parse_args(argv[1:])
    if args.command == "status":
        return _cli_status()
    if args.command == "next":
        return _cli_next()
    return _cli_gate()


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
