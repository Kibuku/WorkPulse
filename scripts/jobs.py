"""
jobs.py — Job data model + JSONL event store + roll-up logic.

A Job is a coherent unit of work spanning multiple sessions, sometimes
multiple days. The user declares jobs explicitly in v1.1a ("Start a job:
NKCC Q3 report"). v1.2 will add LLM-inferred jobs.

Per Vision §12.2, the Job is the unit the coaching surface anchors on —
not the Session, not the Stream.

Storage: logs/jobs.jsonl — one event per line. Append-only, like every other
WorkPulse log. The current state of jobs is folded from the events.

Event shapes:
    {"event":"start","id":"job-...","name":"...","stream":"...","actor_id":"...","ts":"..."}
    {"event":"end",  "id":"job-...","ts":"..."}
    {"event":"rename","id":"job-...","name":"...","ts":"..."}   # v1.2+
    {"event":"note", "id":"job-...","text":"...","ts":"..."}    # v1.2+

Public API:
    start_job(name, stream, note="") -> dict     # auto-ends any existing job in same stream
    end_job(job_id)                  -> dict
    list_active()                    -> list[dict]
    list_all(days_back=30)           -> list[dict]
    get_job(job_id)                  -> dict | None
    sessions_for_job(job_id)         -> list[dict]
    rollup(job_id, include_sessions=False) -> dict
"""

from __future__ import annotations

import json
import secrets
import sys
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import ensure_dir, load_config, resolve


_WRITE_LOCK = threading.RLock()


# ── storage ──────────────────────────────────────────────────────────────────

def _log_path(cfg: dict) -> Path:
    p = resolve(cfg["paths"]["logs"]) / "jobs.jsonl"
    ensure_dir(p.parent)
    return p


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _new_id() -> str:
    return "job-" + secrets.token_hex(6)


def _append_event(event: dict, cfg: dict) -> None:
    p = _log_path(cfg)
    with _WRITE_LOCK:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _read_events(cfg: dict) -> list[dict]:
    p = _log_path(cfg)
    if not p.exists():
        return []
    out: list[dict] = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _actor_id(cfg: dict) -> str:
    """Stable actor_id from config/identity.yaml. Falls back to empty string
    if identity hasn't been bootstrapped — the dashboard still works, just
    without attribution."""
    try:
        import yaml
        id_path = resolve("config/identity.yaml")
        if id_path.exists():
            data = yaml.safe_load(id_path.read_text(encoding="utf-8")) or {}
            return str(data.get("actor_id", "") or "")
    except Exception:
        pass
    return ""


# ── state folding ────────────────────────────────────────────────────────────

def _fold(events: list[dict]) -> dict[str, dict]:
    """Reduce the event log into the current state, keyed by job_id."""
    state: dict[str, dict] = {}
    for e in events:
        ev = e.get("event")
        jid = e.get("id")
        if not jid:
            continue
        if ev == "start":
            state[jid] = {
                "id":         jid,
                "name":       e.get("name", "(untitled)"),
                "stream":     e.get("stream"),
                "actor_id":   e.get("actor_id", ""),
                "created_at": e.get("ts"),
                "ended_at":   None,
                "ended_by":   None,   # 'user' | 'auto' | None
                "note":       e.get("note", "") or "",
            }
        elif ev in ("end", "auto-end"):
            if jid in state:
                state[jid]["ended_at"] = e.get("ts")
                state[jid]["ended_by"] = "auto" if ev == "auto-end" else "user"
        elif ev == "rename":
            if jid in state:
                state[jid]["name"] = e.get("name", state[jid]["name"])
        elif ev == "note":
            if jid in state:
                state[jid]["note"] = e.get("text", "")
        elif ev == "move":
            # v1.7 stream-hierarchy migration: re-home a Job under a different
            # stream. Append-only — the original start event is preserved, the
            # move event simply overrides the effective stream in the folded
            # state. Repeated moves are allowed (last wins).
            if jid in state:
                state[jid]["stream"] = e.get("stream")
    return state


def _job_records(cfg: dict | None = None) -> dict[str, dict]:
    if cfg is None:
        cfg = load_config()
    return _fold(_read_events(cfg))


# ── public API ───────────────────────────────────────────────────────────────

def list_active(cfg: dict | None = None) -> list[dict]:
    """Active jobs (no end event). Sorted by created_at desc."""
    recs = _job_records(cfg)
    out = [r for r in recs.values() if r.get("ended_at") is None]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def list_all(days_back: int = 30, cfg: dict | None = None) -> list[dict]:
    """Every job (active + ended) created in the last N days, newest first."""
    recs = _job_records(cfg)
    cutoff = (datetime.now().astimezone() - timedelta(days=days_back)).isoformat()
    out = [r for r in recs.values() if (r.get("created_at") or "") >= cutoff]
    out.sort(key=lambda r: r.get("created_at") or "", reverse=True)
    return out


def get_job(job_id: str, cfg: dict | None = None) -> dict | None:
    recs = _job_records(cfg)
    return recs.get(job_id)


def start_job(name: str, stream: str | None, note: str = "",
              cfg: dict | None = None) -> dict:
    """Create a new job. If an active job already exists in this stream,
    auto-end it first — one active job per stream is the rule."""
    if cfg is None:
        cfg = load_config()
    name = (name or "").strip()
    if not name:
        raise ValueError("job name is required")
    stream = (stream or "").strip() or None
    if stream and stream not in (cfg.get("streams") or {}):
        raise ValueError(f"unknown stream: {stream}")

    # Auto-end any open job in this stream
    if stream:
        for active in list_active(cfg):
            if active.get("stream") == stream:
                end_job(active["id"], cfg=cfg)

    jid = _new_id()
    ts = _now()
    _append_event({
        "event":    "start",
        "id":       jid,
        "name":     name,
        "stream":   stream,
        "note":     note or "",
        "actor_id": _actor_id(cfg),
        "ts":       ts,
    }, cfg)
    return {
        "id":         jid,
        "name":       name,
        "stream":     stream,
        "created_at": ts,
        "ended_at":   None,
    }


def end_job(job_id: str, cfg: dict | None = None, auto: bool = False) -> dict:
    """End an active job. Pass auto=True from the auto-close pass so the event
    log distinguishes 'I'm done with this' from 'system noticed I moved on'."""
    if cfg is None:
        cfg = load_config()
    rec = get_job(job_id, cfg)
    if rec is None:
        raise KeyError(f"no such job: {job_id}")
    if rec.get("ended_at"):
        return rec  # already ended
    ts = _now()
    ev = "auto-end" if auto else "end"
    _append_event({"event": ev, "id": job_id, "ts": ts}, cfg)
    rec["ended_at"] = ts
    rec["ended_by"] = "auto" if auto else "user"
    return rec


# ── auto-close: detect when the user has clearly moved on ────────────────────

# A job auto-closes when its stream has been silent this long AND...
AUTOCLOSE_AFTER_S = 30 * 60
# ...the user has spent at least this much time in OTHER streams since.
# The second condition prevents auto-closing during long idle / lunch breaks.
AUTOCLOSE_OTHER_STREAM_S = 5 * 60
# Fresh jobs with zero sessions get this much grace before being auto-closed.
AUTOCLOSE_FRESH_GRACE_S = 4 * 3600


def _last_session_end_for_job(job: dict, cfg: dict) -> datetime | None:
    """When did the last session attributed to this job actually end?
    None if the job has no sessions yet."""
    sessions = sessions_for_job(job["id"], cfg)
    if not sessions:
        return None
    latest = max(
        (s.get("end") or s.get("start") or "") for s in sessions
    )
    if not latest:
        return None
    try:
        return datetime.fromisoformat(latest)
    except ValueError:
        return None


def _other_stream_activity_since(since: datetime, exclude_stream: str | None,
                                 cfg: dict) -> float:
    """Total seconds of non-idle activity in streams OTHER than `exclude_stream`
    since the given timestamp. Walks today's + yesterday's activity logs only —
    auto-close decisions don't need deeper history."""
    total = 0.0
    log_dir = resolve(cfg["paths"]["logs"])
    today = date.today()
    for offset in (1, 0):  # yesterday, then today (chronological)
        d = today - timedelta(days=offset)
        p = log_dir / f"activity_{d.isoformat()}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                rec = json.loads(line)
            except json.JSONDecodeError:
                continue
            if rec.get("idle"):
                continue
            if exclude_stream and rec.get("stream") == exclude_stream:
                continue
            try:
                end_dt = datetime.fromisoformat(rec.get("end") or rec.get("start") or "")
            except (ValueError, TypeError):
                continue
            if end_dt <= since:
                continue
            total += float(rec.get("duration_s") or 0)
    return total


def autoclose_stale_jobs(cfg: dict | None = None) -> list[dict]:
    """Walk active jobs and close any where the user has clearly moved on.
    Returns the list of jobs that were closed (each augmented with the reason).
    Cheap enough to call on every dashboard refresh."""
    if cfg is None:
        cfg = load_config()
    now = datetime.now().astimezone()
    closed: list[dict] = []
    for job in list_active(cfg):
        last_end = _last_session_end_for_job(job, cfg)
        if last_end is None:
            # Fresh job with no sessions yet — close it only after a long grace
            try:
                created = datetime.fromisoformat(job["created_at"])
            except (KeyError, ValueError):
                continue
            if (now - created).total_seconds() > AUTOCLOSE_FRESH_GRACE_S:
                end_job(job["id"], cfg=cfg, auto=True)
                closed.append({**job, "auto_reason": "no activity in 4h"})
            continue
        idle_s = (now - last_end).total_seconds()
        if idle_s < AUTOCLOSE_AFTER_S:
            continue
        # Stream has been quiet long enough. Has the user actually moved on,
        # or are they on a lunch break / mid-day pause?
        other_s = _other_stream_activity_since(last_end, job.get("stream"), cfg)
        if other_s >= AUTOCLOSE_OTHER_STREAM_S:
            end_job(job["id"], cfg=cfg, auto=True)
            closed.append({**job, "auto_reason": f"moved to another stream ({round(other_s/60)} min)"})
    return closed


def move_job(job_id: str, new_stream: str | None,
             cfg: dict | None = None) -> dict:
    """Re-home a Job under a different stream. Appends a 'move' event so the
    history is preserved (this is auditable, not a destructive rename).
    Used by the v1.7 taxonomy-migration flow. ``new_stream`` may be None to
    detach a Job from any stream — useful when the original stream is being
    deleted and the Job hasn't been re-homed yet."""
    if cfg is None:
        cfg = load_config()
    rec = get_job(job_id, cfg)
    if rec is None:
        raise KeyError(f"no such job: {job_id}")
    if new_stream and new_stream not in (cfg.get("streams") or {}):
        raise ValueError(f"unknown stream: {new_stream}")
    _append_event({
        "event":  "move",
        "id":     job_id,
        "stream": new_stream,
        "ts":     _now(),
    }, cfg)
    rec["stream"] = new_stream
    return rec


def resume_job(job_id: str, cfg: dict | None = None) -> dict:
    """Re-open a previously-ended job under a NEW job id (same name + stream).
    Useful when auto-close fired too eagerly, or when picking work back up after
    a break. The original record is preserved; a new job starts now."""
    if cfg is None:
        cfg = load_config()
    rec = get_job(job_id, cfg)
    if rec is None:
        raise KeyError(f"no such job: {job_id}")
    return start_job(
        name=rec["name"],
        stream=rec.get("stream"),
        note=f"(resumed from {job_id})",
        cfg=cfg,
    )


# ── session linkage + roll-up ────────────────────────────────────────────────

def _load_sessions_across_range(cfg: dict, start_iso: str, end_iso: str) -> list[dict]:
    """Load activity sessions whose start timestamp falls inside [start, end].
    Reads each day's activity_<date>.jsonl that could intersect the range."""
    try:
        d0 = datetime.fromisoformat(start_iso).date()
    except ValueError:
        d0 = date.today()
    try:
        d1 = datetime.fromisoformat(end_iso).date()
    except ValueError:
        d1 = date.today()
    if d1 < d0:
        d0, d1 = d1, d0
    logs_dir = resolve(cfg["paths"]["logs"])
    out: list[dict] = []
    d = d0
    while d <= d1:
        p = logs_dir / f"activity_{d.isoformat()}.jsonl"
        if p.exists():
            for line in p.read_text(encoding="utf-8").splitlines():
                line = line.strip()
                if not line:
                    continue
                try:
                    out.append(json.loads(line))
                except json.JSONDecodeError:
                    continue
        d = d + timedelta(days=1)
    return out


def sessions_for_job(job_id: str, cfg: dict | None = None) -> list[dict]:
    """All activity sessions belonging to this job. A session belongs if:
       (a) its start ≥ job.created_at,
       (b) its end ≤ job.ended_at (or unbounded for active jobs),
       (c) its stream == job.stream (or job has no stream).
    Idle sessions are excluded."""
    if cfg is None:
        cfg = load_config()
    rec = get_job(job_id, cfg)
    if rec is None:
        return []
    job_start = rec["created_at"]
    job_end = rec.get("ended_at") or _now()
    job_stream = rec.get("stream")

    sessions = _load_sessions_across_range(cfg, job_start, job_end)

    # Re-tag at read-time so config edits affect attribution (mirrors /api/realwork)
    try:
        from scripts.activity import _tag_stream
        for s in sessions:
            t = _tag_stream(s.get("title") or "", s.get("exe_path") or "", cfg)
            if t:
                s["stream"] = t
    except Exception:
        pass

    out: list[dict] = []
    for s in sessions:
        if s.get("idle"):
            continue
        s_start = s.get("start") or ""
        s_end = s.get("end") or s_start
        if s_start < job_start:
            continue
        if s_end > job_end:
            continue
        if job_stream and s.get("stream") != job_stream:
            continue
        out.append(s)
    return out


def rollup(job_id: str, include_sessions: bool = False,
           cfg: dict | None = None) -> dict | None:
    """Full job snapshot for the dashboard: totals + app split + day breakdown
    + last_active + optional session list + AI-lift opportunities."""
    if cfg is None:
        cfg = load_config()
    rec = get_job(job_id, cfg)
    if rec is None:
        return None
    sessions = sessions_for_job(job_id, cfg)

    total_s = sum(float(s.get("duration_s") or 0) for s in sessions)
    apps: dict[str, float] = defaultdict(float)
    days: dict[str, float] = defaultdict(float)
    last_active = None
    for s in sessions:
        dur = float(s.get("duration_s") or 0)
        app = (s.get("app") or "(unknown)").strip() or "(unknown)"
        apps[app] += dur
        try:
            day = datetime.fromisoformat(s.get("start") or "").date().isoformat()
            days[day] += dur
        except (ValueError, TypeError):
            pass
        end = s.get("end") or s.get("start")
        if end and (last_active is None or end > last_active):
            last_active = end

    # Sort + format
    app_list = [
        {"app": k, "minutes": round(v / 60, 1), "seconds": int(v)}
        for k, v in sorted(apps.items(), key=lambda x: -x[1])
    ]
    day_list = [
        {"date": k, "minutes": round(v / 60, 1)}
        for k, v in sorted(days.items())
    ]

    # AI-lift detection lives in its own module so detectors can evolve
    # without touching the job-rollup code path.
    lift: list[dict] = []
    try:
        from scripts.lift import detect_lift
        lift = detect_lift(sessions, job=rec, cfg=cfg)
    except Exception:
        lift = []

    out = {
        "id":            rec["id"],
        "name":          rec["name"],
        "stream":        rec.get("stream"),
        "actor_id":      rec.get("actor_id", ""),
        "created_at":    rec["created_at"],
        "ended_at":      rec.get("ended_at"),
        "note":          rec.get("note", ""),
        "total_seconds": int(total_s),
        "total_minutes": round(total_s / 60, 1),
        "session_count": len(sessions),
        "apps":          app_list,
        "day_breakdown": day_list,
        "last_active":   last_active,
        "lift":          lift,
    }
    if include_sessions:
        out["sessions"] = [
            {
                "start":      s.get("start"),
                "end":        s.get("end"),
                "duration_s": s.get("duration_s"),
                "app":        s.get("app"),
                "title":      s.get("title"),
            }
            for s in sessions
        ]
    return out


# ── Markdown export ──────────────────────────────────────────────────────────

def _human_dur(seconds: float) -> str:
    s = int(seconds or 0)
    if s < 60: return f"{s}s"
    m = s // 60
    if m < 60: return f"{m} min"
    h, mm = divmod(m, 60)
    return f"{h}h {mm}m" if mm else f"{h}h"


def _human_date(iso: str | None) -> str:
    if not iso:
        return "—"
    try:
        return datetime.fromisoformat(iso).strftime("%a, %b %d %Y")
    except (ValueError, TypeError):
        return iso[:10]


def _human_time(iso: str | None) -> str:
    if not iso:
        return ""
    try:
        return datetime.fromisoformat(iso).strftime("%H:%M")
    except (ValueError, TypeError):
        return iso[11:16]


def _narrative_summary(roll: dict, cfg: dict) -> str:
    """Generate a 2-3 sentence narrative of how the job unfolded.
    Uses scripts.llm (any configured backend); falls back to a deterministic
    one-liner if no LLM is available."""
    try:
        from scripts.llm import ask_text
        from scripts.ai_logger import log_session
    except ImportError:
        return ""

    apps_summary = ", ".join(f"{a['app']} {_human_dur(a['seconds'])}"
                             for a in (roll.get("apps") or [])[:5])
    days_summary = ", ".join(f"{d['date']} ({_human_dur(d['minutes']*60)})"
                             for d in (roll.get("day_breakdown") or [])[:7])
    lift_summary = "; ".join(l["title"] for l in (roll.get("lift") or [])[:3]) or "none flagged"

    prompt = (
        "Write a 2-3 sentence factual summary of how this work was executed. "
        "No coaching, no advice, no commentary on whether the user worked 'well' — "
        "just observe the shape of the work.\n\n"
        f"Job:        {roll.get('name')}\n"
        f"Stream:     {roll.get('stream')}\n"
        f"Total time: {_human_dur(roll.get('total_seconds') or 0)}\n"
        f"Days:       {days_summary}\n"
        f"Apps:       {apps_summary}\n"
        f"AI-lift opportunities found: {lift_summary}\n\n"
        "Reply with prose only. No bullet points, no headers, no leading label."
    )
    text, meta = ask_text(prompt, max_tokens=200, cfg=cfg)
    if meta.get("backend") != "none":
        try:
            log_session(
                stream=roll.get("stream"),
                task_summary=f"Job export narrative: {roll.get('name','')}"[:200],
                input_tokens=meta.get("input_tokens", 0),
                output_tokens=meta.get("output_tokens", 0),
                tool_used=f"workpulse-export-{meta.get('backend')}",
                duration_minutes=round((meta.get("duration_s") or 0) / 60, 3),
                cfg=cfg,
            )
        except Exception:
            pass
    return (text or "").strip()


def export_job_markdown(job_id: str, *, include_titles: bool = True,
                        cfg: dict | None = None) -> str | None:
    """Build a self-contained markdown digest of a Job — suitable for emailing
    a client, pasting into a timesheet, or filing as a personal record.
    Returns None if the job doesn't exist."""
    if cfg is None:
        cfg = load_config()
    roll = rollup(job_id, include_sessions=True, cfg=cfg)
    if roll is None:
        return None

    from scripts.tree import breadcrumb, labels as _stream_labels
    streams_cfg = _stream_labels(cfg)
    stream_key = roll.get("stream") or ""
    # Use the breadcrumb (Work › Verst Carbon › Uganda MEMD) when the tree
    # has depth; falls back to the bare label for flat configs.
    stream_label = (breadcrumb(stream_key, cfg) if stream_key else None) \
                   or streams_cfg.get(stream_key, stream_key or "(no stream)")

    lines: list[str] = []
    lines.append(f"# {roll.get('name', '(untitled job)')}")
    lines.append("")
    lines.append(f"**Stream:** {stream_label}  ")
    lines.append(f"**Duration:** {_human_dur(roll.get('total_seconds') or 0)} "
                 f"across {len(roll.get('day_breakdown') or [])} day"
                 f"{'s' if len(roll.get('day_breakdown') or []) != 1 else ''}  ")
    lines.append(f"**Started:** {_human_date(roll.get('created_at'))}  ")
    if roll.get("ended_at"):
        lines.append(f"**Ended:** {_human_date(roll['ended_at'])}  ")
    if roll.get("note"):
        lines.append(f"**Note:** {roll['note']}  ")
    lines.append("")

    # Narrative summary (LLM-generated, graceful when no key)
    narrative = _narrative_summary(roll, cfg)
    if narrative:
        lines.append("## Summary")
        lines.append("")
        lines.append(narrative)
        lines.append("")

    # Day-by-day breakdown
    if roll.get("day_breakdown"):
        lines.append("## Day-by-day breakdown")
        lines.append("")
        # Group sessions by date
        sessions_by_day: dict[str, list[dict]] = defaultdict(list)
        for s in (roll.get("sessions") or []):
            try:
                day = datetime.fromisoformat(s.get("start") or "").date().isoformat()
            except (ValueError, TypeError):
                continue
            sessions_by_day[day].append(s)
        for day_block in roll["day_breakdown"]:
            day = day_block["date"]
            lines.append(f"### {_human_date(day)} — {_human_dur(day_block['minutes']*60)}")
            for s in sessions_by_day.get(day, []):
                t = _human_time(s.get("start"))
                dur = _human_dur(s.get("duration_s") or 0)
                app = s.get("app") or "(unknown)"
                if include_titles and s.get("title"):
                    title = (s["title"] or "")[:80]
                    lines.append(f"- `{t}` {app} ({dur}) — {title}")
                else:
                    lines.append(f"- `{t}` {app} ({dur})")
            lines.append("")

    # Apps used
    if roll.get("apps"):
        lines.append("## Apps used")
        lines.append("")
        for a in roll["apps"]:
            lines.append(f"- **{a['app']}** — {_human_dur(a['seconds'])}")
        lines.append("")

    # AI-lift opportunities
    if roll.get("lift"):
        lines.append("## AI-lift opportunities identified")
        lines.append("")
        for l in roll["lift"]:
            lines.append(f"- **{l['title']}** — {l['suggestion']}")
        lines.append("")

    lines.append("---")
    lines.append(f"*Generated by WorkPulse on {datetime.now().astimezone().strftime('%b %d, %Y at %H:%M')}.*")
    return "\n".join(lines)


# ── CLI: smoke + diagnostic ───────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser(description="WorkPulse jobs CLI")
    sub = ap.add_subparsers(dest="cmd")
    p = sub.add_parser("start")
    p.add_argument("name")
    p.add_argument("--stream", required=True)
    p2 = sub.add_parser("end"); p2.add_argument("id")
    sub.add_parser("active")
    sub.add_parser("all")
    p3 = sub.add_parser("show"); p3.add_argument("id")
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "start":
        print(json.dumps(start_job(args.name, args.stream, cfg=cfg), indent=2))
    elif args.cmd == "end":
        print(json.dumps(end_job(args.id, cfg=cfg), indent=2))
    elif args.cmd == "active":
        for j in list_active(cfg):
            print(f"  {j['id']}  {j['stream']:<14}  {j['name']}")
    elif args.cmd == "all":
        for j in list_all(cfg=cfg):
            tag = "active" if not j.get("ended_at") else "ended "
            print(f"  [{tag}] {j['id']}  {j['stream']:<14}  {j['name']}")
    elif args.cmd == "show":
        r = rollup(args.id, include_sessions=True, cfg=cfg)
        print(json.dumps(r, indent=2, default=str))
    else:
        ap.print_help()
