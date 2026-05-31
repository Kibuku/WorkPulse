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
                "note":       e.get("note", "") or "",
            }
        elif ev == "end":
            if jid in state:
                state[jid]["ended_at"] = e.get("ts")
        elif ev == "rename":
            if jid in state:
                state[jid]["name"] = e.get("name", state[jid]["name"])
        elif ev == "note":
            if jid in state:
                state[jid]["note"] = e.get("text", "")
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


def end_job(job_id: str, cfg: dict | None = None) -> dict:
    if cfg is None:
        cfg = load_config()
    rec = get_job(job_id, cfg)
    if rec is None:
        raise KeyError(f"no such job: {job_id}")
    if rec.get("ended_at"):
        return rec  # already ended
    ts = _now()
    _append_event({"event": "end", "id": job_id, "ts": ts}, cfg)
    rec["ended_at"] = ts
    return rec


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
