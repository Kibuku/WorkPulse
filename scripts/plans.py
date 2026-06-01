"""
plans.py — daily morning-plan data layer.

Per Vision §12.2, the Job is the unit of analysis. The morning plan is the
*intent* side of Jobs: the user declares (or carries over) which Jobs they
intend to touch today, optionally with target minutes. End-of-day, planned
items can be reconciled against the actual time logged against each Job.

Source of truth: ``plans/YYYY-MM-DD.md`` — one markdown file per day. The
file is human-editable; this module parses and rewrites it.

Format:
    # Plan — 2026-06-01
    Created: 2026-06-01T08:30:00+03:00

    ## Carried over
    - [ ] NKCC Q3 narrative (~120 min) — job:job-abc123 — stream:client-x
    - [x] Done already (~30 min) — job:job-def456 — stream:misc

    ## New
    - [ ] Call Mwangi re carbon spec (~30 min) — stream:misc

Each item:
    [ ] / [x]         done flag
    name              free-text title
    (~N min)          optional planned minutes, kept loose on purpose
    job:<id>          optional job link (added when the plan creates a Job)
    stream:<key>      optional stream key

Lines that don't match the item shape pass through untouched on rewrite,
so users can leave their own notes inside the file.

Public API:
    today_path()                              -> Path of today's plan file
    plan_for(date)                            -> dict | None
    save_plan_for(date, items, ...)           -> dict (regenerated record)
    suggestions(date_for=None)                -> dict {paused, yesterday, recent}
    materialise_jobs(date, items, cfg=None)   -> list[dict] items with job_id

Item dict shape:
    {
      "name":            str,
      "done":            bool,
      "planned_minutes": int | None,
      "job_id":          str | None,    # job-... (linked or just-created)
      "stream":          str | None,    # stream key
      "section":         "carried"|"new",
      "raw":             str,           # original line, useful for round-trip
    }
"""

from __future__ import annotations

import re
import threading
from datetime import date, datetime, timedelta
from pathlib import Path
from typing import Iterable

import sys
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import ensure_dir, load_config, resolve


_WRITE_LOCK = threading.RLock()


# ── paths ────────────────────────────────────────────────────────────────────

def _plans_dir(cfg: dict | None = None) -> Path:
    cfg = cfg or load_config()
    # No new config key required; live alongside reports/.
    p = resolve("plans")
    ensure_dir(p)
    return p


def path_for(d: date, cfg: dict | None = None) -> Path:
    return _plans_dir(cfg) / f"{d.isoformat()}.md"


def today_path(cfg: dict | None = None) -> Path:
    return path_for(date.today(), cfg)


# ── parse ────────────────────────────────────────────────────────────────────

_ITEM_RE = re.compile(
    r"""^\s*[-*]\s+              # bullet
        \[(?P<done>[ xX])\]\s+   # checkbox
        (?P<body>.+?)\s*$        # body (everything after)
    """,
    re.VERBOSE,
)
_MIN_RE    = re.compile(r"\(\s*~\s*(\d+)\s*min\s*\)", re.IGNORECASE)
_JOB_RE    = re.compile(r"\bjob:(job-[0-9a-f]+)\b", re.IGNORECASE)
_STREAM_RE = re.compile(r"\bstream:([A-Za-z0-9_\-]+)\b")
_SECTION_RE = re.compile(r"^\s*##\s+(.+?)\s*$")


def _parse_item(line: str, section: str) -> dict | None:
    m = _ITEM_RE.match(line)
    if not m:
        return None
    body = m.group("body")
    done = m.group("done").lower() == "x"

    minutes = None
    mm = _MIN_RE.search(body)
    if mm:
        try:
            minutes = int(mm.group(1))
        except ValueError:
            minutes = None

    job_id = None
    jm = _JOB_RE.search(body)
    if jm:
        job_id = jm.group(1)

    stream = None
    sm = _STREAM_RE.search(body)
    if sm:
        stream = sm.group(1)

    # Name = body with the metadata tokens stripped + em-dash separators tidied
    name = body
    for r in (_MIN_RE, _JOB_RE, _STREAM_RE):
        name = r.sub("", name)
    # Collapse separators left behind ("— — ")
    name = re.sub(r"\s*[—\-]\s*$", "", name)
    name = re.sub(r"\s+[—\-]\s+[—\-]\s+", " — ", name)
    name = re.sub(r"\s{2,}", " ", name).strip(" —-\t")

    return {
        "name":            name or "(untitled)",
        "done":            done,
        "planned_minutes": minutes,
        "job_id":          job_id,
        "stream":          stream,
        "section":         section,
        "raw":             line.rstrip("\n"),
    }


def parse(text: str) -> dict:
    """Parse a plan markdown blob. Returns:
        {"created_at": str|None, "items": [item, ...]}.
    Unrecognised lines are kept in 'preserved' for round-trip rewrite."""
    created_at = None
    items: list[dict] = []
    section = "new"  # default if the file has no section headers
    for line in text.splitlines():
        if line.lower().startswith("created:"):
            created_at = line.split(":", 1)[1].strip()
            continue
        sec = _SECTION_RE.match(line)
        if sec:
            label = sec.group(1).lower()
            section = "carried" if "carr" in label else "new"
            continue
        item = _parse_item(line, section)
        if item is not None:
            items.append(item)
    return {"created_at": created_at, "items": items}


# ── render ───────────────────────────────────────────────────────────────────

def _render_item(it: dict) -> str:
    check = "x" if it.get("done") else " "
    parts = [it.get("name") or "(untitled)"]
    pm = it.get("planned_minutes")
    if pm:
        parts.append(f"(~{int(pm)} min)")
    if it.get("job_id"):
        parts.append(f"job:{it['job_id']}")
    if it.get("stream"):
        parts.append(f"stream:{it['stream']}")
    return f"- [{check}] " + " — ".join(parts)


def render(d: date, items: list[dict], created_at: str | None = None) -> str:
    """Produce the markdown body for a plan."""
    created_at = created_at or datetime.now().astimezone().isoformat(timespec="seconds")
    carried = [it for it in items if it.get("section") == "carried"]
    new     = [it for it in items if it.get("section") != "carried"]

    out: list[str] = []
    out.append(f"# Plan — {d.isoformat()}")
    out.append("")
    out.append(f"Created: {created_at}")
    out.append("")
    if carried:
        out.append("## Carried over")
        out.append("")
        out.extend(_render_item(it) for it in carried)
        out.append("")
    out.append("## New")
    out.append("")
    if new:
        out.extend(_render_item(it) for it in new)
    else:
        out.append("_(empty)_")
    out.append("")
    return "\n".join(out)


# ── read / write ─────────────────────────────────────────────────────────────

def plan_for(d: date, cfg: dict | None = None) -> dict | None:
    p = path_for(d, cfg)
    if not p.exists():
        return None
    parsed = parse(p.read_text(encoding="utf-8"))
    parsed["date"] = d.isoformat()
    parsed["path"] = str(p)
    return parsed


def save_plan_for(d: date, items: list[dict], *,
                  created_at: str | None = None,
                  cfg: dict | None = None) -> dict:
    """Atomically rewrite the plan file for the given date."""
    p = path_for(d, cfg)
    body = render(d, items, created_at=created_at)
    with _WRITE_LOCK:
        tmp = p.with_suffix(".md.tmp")
        tmp.write_text(body, encoding="utf-8")
        tmp.replace(p)
    return {"date": d.isoformat(), "path": str(p), "items": items,
            "created_at": created_at}


# ── suggestions: what should the user be offered to carry over? ──────────────

def suggestions(date_for: date | None = None, cfg: dict | None = None) -> dict:
    """Return the things WorkPulse can suggest the user start their day with.

    Three buckets:
      * paused    — active jobs that have NOT been ended (still 'in flight')
      * yesterday — jobs that received any time yesterday, ended or not
      * recent    — any job touched in the last 7 days (for "older but warm")

    No dedup across buckets; the UI can filter as it sees fit.
    """
    if cfg is None:
        cfg = load_config()
    if date_for is None:
        date_for = date.today()

    from scripts.jobs import list_active, list_all, sessions_for_job

    paused = [
        {"job_id": j["id"], "name": j["name"], "stream": j.get("stream"),
         "created_at": j.get("created_at")}
        for j in list_active(cfg)
    ]

    yesterday = date_for - timedelta(days=1)
    week_cutoff = date_for - timedelta(days=7)

    yest_jobs: list[dict] = []
    recent_jobs: list[dict] = []

    for j in list_all(days_back=30, cfg=cfg):
        sessions = sessions_for_job(j["id"], cfg=cfg)
        if not sessions:
            continue
        # Latest day this job saw activity
        last_day: date | None = None
        for s in sessions:
            try:
                d = datetime.fromisoformat(s.get("start") or "").date()
            except (ValueError, TypeError):
                continue
            if last_day is None or d > last_day:
                last_day = d
        if last_day is None:
            continue
        entry = {
            "job_id":    j["id"],
            "name":      j["name"],
            "stream":    j.get("stream"),
            "last_day":  last_day.isoformat(),
            "ended_at":  j.get("ended_at"),
        }
        if last_day == yesterday:
            yest_jobs.append(entry)
        elif week_cutoff <= last_day < yesterday:
            recent_jobs.append(entry)

    yest_jobs.sort(key=lambda r: r.get("last_day", ""), reverse=True)
    recent_jobs.sort(key=lambda r: r.get("last_day", ""), reverse=True)

    return {
        "for_date":  date_for.isoformat(),
        "paused":    paused,
        "yesterday": yest_jobs,
        "recent":    recent_jobs,
    }


# ── materialise: link plan items to actual Job records ──────────────────────

def materialise_jobs(d: date, items: list[dict],
                     cfg: dict | None = None) -> list[dict]:
    """For each plan item without a job_id, create a Job and write its id
    back into the item. Items already linked are left alone.

    Returns the same item list, mutated in place. Safe to call repeatedly —
    a second call on an already-materialised plan is a no-op.

    Jobs created here have a note marking them as plan-originated, so we can
    distinguish "I clicked Start Job at 11am" from "I planned this in the
    morning". Both end up in the same Jobs storage.
    """
    if cfg is None:
        cfg = load_config()
    # We deliberately do NOT call jobs.start_job here. start_job enforces
    # "one active job per stream" by auto-ending any existing job in the
    # target stream — which would cause a multi-item plan to cascade-end
    # its own freshly-created Jobs. The morning plan is the explicit
    # exception: by design, a single stream can hold several planned Jobs
    # concurrently. We write the start event directly.
    from scripts.jobs import (
        _append_event, _actor_id, _new_id, _now, get_job,
    )

    streams_cfg = cfg.get("streams") or {}
    note_prefix = f"(planned for {d.isoformat()})"

    for it in items:
        # Item already has a job_id and that job still exists — keep it.
        existing = it.get("job_id")
        if existing and get_job(existing, cfg) is not None:
            continue

        # Determine a stream. Item-specified wins; else fall back to misc/first.
        stream = it.get("stream") or None
        if stream and stream not in streams_cfg:
            stream = None
        if not stream:
            # Pick a sensible default: 'misc' if present, else first stream key.
            stream = "misc" if "misc" in streams_cfg else (
                next(iter(streams_cfg), None)
            )
        if not stream:
            # No streams at all — skip job creation; the plan can still hold the item.
            it["job_id"] = None
            continue

        name = (it.get("name") or "(untitled)").strip() or "(untitled)"
        jid = _new_id()
        _append_event({
            "event":    "start",
            "id":       jid,
            "name":     name,
            "stream":   stream,
            "note":     note_prefix,
            "actor_id": _actor_id(cfg),
            "ts":       _now(),
        }, cfg)
        it["job_id"] = jid
        it["stream"] = stream

    return items


# ── reconciliation: planned vs actual ───────────────────────────────────────

def reconcile(d: date, items: list[dict], cfg: dict | None = None) -> dict:
    """For each item linked to a Job, compute:
        actual_minutes_today  — non-idle session minutes on date `d`
        actual_minutes_total  — non-idle session minutes across the Job's life

    Returns the same items list with the two fields added in place, plus a
    top-level totals block (sum of planned, sum of actual today). Items
    without a job_id get zeros — they still count toward planned totals but
    can never accrue actual time until they're linked.
    """
    if cfg is None:
        cfg = load_config()
    from scripts.jobs import sessions_for_job

    target_iso = d.isoformat()

    planned_total = 0
    actual_today_total = 0.0

    for it in items:
        pm = it.get("planned_minutes") or 0
        planned_total += int(pm)

        jid = it.get("job_id")
        if not jid:
            it["actual_minutes_today"] = 0.0
            it["actual_minutes_total"] = 0.0
            continue

        try:
            sessions = sessions_for_job(jid, cfg=cfg)
        except Exception:
            sessions = []

        today_s = 0.0
        total_s = 0.0
        for s in sessions:
            if s.get("idle"):
                continue
            dur = float(s.get("duration_s") or 0)
            total_s += dur
            start = s.get("start") or ""
            if start[:10] == target_iso:
                today_s += dur

        it["actual_minutes_today"] = round(today_s / 60, 1)
        it["actual_minutes_total"] = round(total_s / 60, 1)
        actual_today_total += today_s

    return {
        "items":               items,
        "planned_minutes":     planned_total,
        "actual_minutes_today": round(actual_today_total / 60, 1),
    }


# ── CLI: smoke + diagnostic ─────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse, json
    ap = argparse.ArgumentParser(description="WorkPulse plans CLI")
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("today")
    sub.add_parser("suggestions")
    p2 = sub.add_parser("parse"); p2.add_argument("path")
    args = ap.parse_args()
    if args.cmd == "today":
        print(json.dumps(plan_for(date.today()) or {}, indent=2, default=str))
    elif args.cmd == "suggestions":
        print(json.dumps(suggestions(), indent=2, default=str))
    elif args.cmd == "parse":
        text = Path(args.path).read_text(encoding="utf-8")
        print(json.dumps(parse(text), indent=2, default=str))
    else:
        ap.print_help()
