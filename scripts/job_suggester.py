"""
job_suggester.py — Loop B for Jobs (Vision §12.2, v1.2 build slice).

Watches recent activity per stream. When a stream has accumulated meaningful
work AND has no active job, asks the LLM to name the underlying job. The user
sees the suggestion in the dashboard as a banner with Accept / Rename / Dismiss.

Eligibility (a stream qualifies for a suggestion when ALL hold):
  1. ≥30 min of non-idle activity in the last 4 hours
  2. ≥3 distinct windows (so we're not naming a single trivial app open)
  3. No active job in that stream right now
  4. No dismissal in the cooldown window (4h) UNLESS the activity fingerprint
     has changed materially since the dismissal

Caching: the LLM call is fingerprinted on the eligible sessions. If the
fingerprint hasn't changed since the last proposal, the cached name is
reused — no new LLM call. So a dashboard refresh every 30 s costs nothing
extra until the underlying activity actually shifts.

State storage: logs/job_suggestions.jsonl — append-only events:
    {"event":"proposed", "stream":..., "name":..., "confidence":..., "fingerprint":..., "ts":...}
    {"event":"dismissed", "stream":..., "fingerprint":..., "ts":...}
    {"event":"accepted",  "stream":..., "name":..., "job_id":..., "ts":...}
"""

from __future__ import annotations

import hashlib
import json
import re
import sys
import threading
from collections import defaultdict
from datetime import date, datetime, timedelta
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import ensure_dir, load_config, resolve


# ── tunables ─────────────────────────────────────────────────────────────────

LOOKBACK_HOURS         = 4
MIN_ACTIVITY_SECONDS   = 30 * 60         # ≥30 min of non-idle activity per stream
MIN_DISTINCT_WINDOWS   = 3
DISMISSAL_COOLDOWN_S   = 4 * 3600        # honour a dismissal for 4 hours
ACCEPTANCE_SUPPRESS_S  = 24 * 3600       # after acceptance, suppress for 24h (job is active anyway)
MAX_SAMPLE_SESSIONS    = 12              # don't blow the LLM context with hundreds of rows
MAX_SUGGESTIONS_SHOWN  = 3               # cap the banner count


# ── storage ──────────────────────────────────────────────────────────────────

_WRITE_LOCK = threading.RLock()


def _log_path(cfg: dict) -> Path:
    p = resolve(cfg["paths"]["logs"]) / "job_suggestions.jsonl"
    ensure_dir(p.parent)
    return p


def _now() -> str:
    return datetime.now().astimezone().isoformat(timespec="seconds")


def _append_event(event: dict, cfg: dict) -> None:
    p = _log_path(cfg)
    with _WRITE_LOCK:
        with p.open("a", encoding="utf-8") as f:
            f.write(json.dumps(event, ensure_ascii=False) + "\n")


def _read_events(cfg: dict) -> list[dict]:
    p = _log_path(cfg)
    if not p.exists():
        return []
    out = []
    for line in p.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


# ── recent-activity scan ─────────────────────────────────────────────────────

def _load_recent_sessions(cfg: dict, hours: int = LOOKBACK_HOURS) -> list[dict]:
    """Load all non-idle sessions whose end-time is within the last N hours.
    Re-tags streams on the fly so config edits take effect immediately."""
    log_dir = resolve(cfg["paths"]["logs"])
    cutoff = datetime.now().astimezone() - timedelta(hours=hours)
    today = date.today()
    out: list[dict] = []
    for offset in (1, 0):  # yesterday then today (chronological)
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
            try:
                end_dt = datetime.fromisoformat(rec.get("end") or rec.get("start") or "")
            except (ValueError, TypeError):
                continue
            if end_dt < cutoff:
                continue
            out.append(rec)
    # Re-tag at read time (matches the rest of WorkPulse)
    try:
        from scripts.activity import _tag_stream
        for s in out:
            t = _tag_stream(s.get("title") or "", s.get("exe_path") or "", cfg)
            if t:
                s["stream"] = t
    except Exception:
        pass
    return out


def _group_by_stream(sessions: list[dict]) -> dict[str, list[dict]]:
    by: dict[str, list[dict]] = defaultdict(list)
    for s in sessions:
        st = s.get("stream")
        if not st:
            continue
        by[st].append(s)
    return dict(by)


def _activity_fingerprint(sessions: list[dict]) -> str:
    """Stable hash of the activity shape. Re-prompts when sessions are added
    or grow significantly; ignores noise like exact-second timings."""
    # Bucket each session's duration into 5-min increments so tiny ticks don't
    # invalidate the fingerprint on every dashboard refresh.
    parts = []
    for s in sorted(sessions, key=lambda x: x.get("start") or ""):
        title = (s.get("title") or "").strip().lower()[:120]
        app   = (s.get("app") or "").strip().lower()
        bucket = int(float(s.get("duration_s") or 0) / 300)  # 5-min buckets
        parts.append(f"{app}|{title}|{bucket}")
    blob = "\n".join(parts).encode("utf-8")
    return hashlib.sha256(blob).hexdigest()[:16]


# ── state queries ────────────────────────────────────────────────────────────

def _latest_event_per_stream(events: list[dict], kind: str) -> dict[str, dict]:
    out: dict[str, dict] = {}
    for e in events:
        if e.get("event") != kind:
            continue
        st = e.get("stream")
        if not st:
            continue
        out[st] = e  # later events overwrite earlier — we want the latest
    return out


def _is_active_job_in_stream(stream: str, cfg: dict) -> bool:
    try:
        from scripts.jobs import list_active
        return any(j.get("stream") == stream for j in list_active(cfg))
    except Exception:
        return False


# ── LLM proposer ─────────────────────────────────────────────────────────────

def _format_sessions_for_prompt(sessions: list[dict]) -> str:
    """Compact session list for the LLM. Top sessions by duration, capped."""
    sorted_s = sorted(sessions, key=lambda s: -float(s.get("duration_s") or 0))
    lines = []
    for s in sorted_s[:MAX_SAMPLE_SESSIONS]:
        dur_min = int(float(s.get("duration_s") or 0) / 60)
        if dur_min < 1:
            continue
        title = (s.get("title") or "").strip()
        if len(title) > 110:
            title = title[:107] + "..."
        app = (s.get("app") or "").strip() or "(unknown)"
        lines.append(f"  - [{dur_min:>3} min] {app:<14} {title}")
    return "\n".join(lines) if lines else "  (no sessions over 1 min)"


def _ask_llm_for_name(stream: str, stream_label: str, sessions: list[dict],
                     cfg: dict) -> tuple[str | None, str]:
    """Ask the LLM to propose a job name for these sessions. Returns
    (name_or_None, confidence). Logs the call to ai_sessions.jsonl."""
    from scripts.llm import ask_json
    from scripts.ai_logger import log_session

    prompt = (
        "You are naming a specific piece of work the user has been doing.\n\n"
        f"Stream: {stream} ({stream_label})\n"
        f"Recent activity (last {LOOKBACK_HOURS} hours, in this stream):\n"
        f"{_format_sessions_for_prompt(sessions)}\n\n"
        "Look at the document names, search queries, and app patterns. Propose "
        "a SHORT concrete name (2-5 words) that the user would immediately "
        "recognise as the job they've been on.\n\n"
        "GOOD names: 'NKCC Q3 report', 'Tariff research note', 'Chapter 3 lit review', "
        "'Verst Carbon dashboard'.\n"
        "BAD names: 'Consulting work', 'Various tasks', 'Q3 stuff', 'Dissertation' "
        "(too generic — that's the stream, not the job).\n\n"
        "Confidence:\n"
        "  high   = clear single-project signal (a specific document name appears repeatedly)\n"
        "  medium = thematic coherence but ambiguous boundaries\n"
        "  low    = too miscellaneous to name as one job\n\n"
        "If confidence is low, return name=null.\n\n"
        'Reply with ONLY one JSON object:\n'
        '  {"name": "<2-5 word name or null>", "confidence": "high|medium|low"}'
    )

    obj, meta = ask_json(prompt, max_tokens=80, cfg=cfg)

    try:
        log_session(
            stream=stream,
            task_summary=f"Suggest job name: {stream}",
            input_tokens=meta.get("input_tokens", 0),
            output_tokens=meta.get("output_tokens", 0),
            tool_used=f"workpulse-job-suggester-{meta.get('backend','none')}",
            duration_minutes=round((meta.get("duration_s") or 0) / 60, 3),
            cfg=cfg,
        )
    except Exception:
        pass

    if not obj:
        return None, "low"
    raw_name = obj.get("name")
    confidence = (obj.get("confidence") or "low").lower()
    if confidence not in ("high", "medium", "low"):
        confidence = "low"
    if not isinstance(raw_name, str) or raw_name.lower() in ("none", "null", ""):
        return None, confidence
    # Strip surrounding quotes and any stray punctuation; cap length defensively
    name = raw_name.strip().strip('"').strip("'").strip()
    name = re.sub(r"\s+", " ", name)[:60]
    return name or None, confidence


# ── orchestrator: what should the dashboard show right now? ──────────────────

def compute_suggestions(cfg: dict | None = None) -> list[dict]:
    """Return current pending job-name suggestions. Cheap to call on every
    dashboard refresh — only invokes the LLM when activity has materially
    changed since the last proposal for a stream."""
    if cfg is None:
        cfg = load_config()
    streams_cfg = cfg.get("streams") or {}
    if not streams_cfg:
        return []

    sessions = _load_recent_sessions(cfg)
    if not sessions:
        return []
    by_stream = _group_by_stream(sessions)

    events           = _read_events(cfg)
    last_proposed    = _latest_event_per_stream(events, "proposed")
    last_dismissed   = _latest_event_per_stream(events, "dismissed")
    last_accepted    = _latest_event_per_stream(events, "accepted")

    now = datetime.now().astimezone()
    suggestions: list[dict] = []

    for stream, ss in by_stream.items():
        if stream not in streams_cfg:
            continue
        total_s = sum(float(s.get("duration_s") or 0) for s in ss)
        if total_s < MIN_ACTIVITY_SECONDS:
            continue
        distinct = len({(s.get("title") or "") for s in ss})
        if distinct < MIN_DISTINCT_WINDOWS:
            continue
        if _is_active_job_in_stream(stream, cfg):
            continue

        # Honour recent acceptance (job naturally won't be active again until cooldown
        # passes or user re-opens, but be defensive)
        acc = last_accepted.get(stream)
        if acc:
            try:
                ts = datetime.fromisoformat(acc.get("ts") or "")
                if (now - ts).total_seconds() < ACCEPTANCE_SUPPRESS_S:
                    continue
            except (ValueError, TypeError):
                pass

        fingerprint = _activity_fingerprint(ss)

        # Honour recent dismissal UNLESS the fingerprint has changed
        dism = last_dismissed.get(stream)
        if dism:
            try:
                ts = datetime.fromisoformat(dism.get("ts") or "")
                age = (now - ts).total_seconds()
                if age < DISMISSAL_COOLDOWN_S and dism.get("fingerprint") == fingerprint:
                    continue
            except (ValueError, TypeError):
                pass

        # Cache hit? If we proposed for this exact fingerprint and the suggestion
        # is still relevant, reuse it without re-prompting.
        prop = last_proposed.get(stream)
        if prop and prop.get("fingerprint") == fingerprint and prop.get("name"):
            suggestions.append({
                "stream":     stream,
                "label":      streams_cfg[stream],
                "name":       prop["name"],
                "confidence": prop.get("confidence", "medium"),
                "fingerprint": fingerprint,
                "total_minutes": round(total_s / 60, 1),
                "session_count": len(ss),
                "from_cache": True,
                "proposed_at": prop.get("ts"),
            })
            continue

        # Fresh LLM call
        name, conf = _ask_llm_for_name(stream, streams_cfg[stream], ss, cfg)
        if conf == "low" or not name:
            # Record the dud as a proposal with name=null so we don't keep
            # paying for the same low-confidence answer until fingerprint changes
            _append_event({
                "event":       "proposed",
                "stream":      stream,
                "name":        None,
                "confidence":  conf,
                "fingerprint": fingerprint,
                "ts":          _now(),
            }, cfg)
            continue
        _append_event({
            "event":       "proposed",
            "stream":      stream,
            "name":        name,
            "confidence":  conf,
            "fingerprint": fingerprint,
            "ts":          _now(),
        }, cfg)
        suggestions.append({
            "stream":        stream,
            "label":         streams_cfg[stream],
            "name":          name,
            "confidence":    conf,
            "fingerprint":   fingerprint,
            "total_minutes": round(total_s / 60, 1),
            "session_count": len(ss),
            "from_cache":    False,
            "proposed_at":   _now(),
        })

    # Cap and order: high confidence first, then by activity volume
    suggestions.sort(key=lambda x: (-{"high":2,"medium":1,"low":0}.get(x["confidence"],0),
                                    -x["total_minutes"]))
    return suggestions[:MAX_SUGGESTIONS_SHOWN]


# ── accept / dismiss ─────────────────────────────────────────────────────────

def accept_suggestion(stream: str, name: str, cfg: dict | None = None) -> dict:
    """Start a job with the given name + stream, recording the acceptance."""
    if cfg is None:
        cfg = load_config()
    from scripts.jobs import start_job
    job = start_job(name=name, stream=stream, note="(accepted suggestion)", cfg=cfg)
    _append_event({
        "event":  "accepted",
        "stream": stream,
        "name":   name,
        "job_id": job["id"],
        "ts":     _now(),
    }, cfg)
    return job


def dismiss_suggestion(stream: str, cfg: dict | None = None) -> None:
    """Record a dismissal so the same suggestion doesn't reappear immediately."""
    if cfg is None:
        cfg = load_config()
    # Capture the fingerprint of CURRENT activity so the dismissal only
    # applies to this snapshot — if the user does materially different work
    # in this stream later, we can propose again before the cooldown expires.
    sessions = _load_recent_sessions(cfg)
    stream_sessions = [s for s in sessions if s.get("stream") == stream]
    fp = _activity_fingerprint(stream_sessions)
    _append_event({
        "event":       "dismissed",
        "stream":      stream,
        "fingerprint": fp,
        "ts":          _now(),
    }, cfg)


# ── CLI for diagnostics ──────────────────────────────────────────────────────

if __name__ == "__main__":
    import argparse
    ap = argparse.ArgumentParser()
    sub = ap.add_subparsers(dest="cmd")
    sub.add_parser("check")          # show current suggestions (may invoke LLM)
    p = sub.add_parser("dismiss"); p.add_argument("stream")
    p = sub.add_parser("accept");  p.add_argument("stream"); p.add_argument("name")
    args = ap.parse_args()
    cfg = load_config()
    if args.cmd == "check":
        for s in compute_suggestions(cfg):
            print(f"  [{s['confidence']:>6}]  {s['stream']:<14}  {s['name']}  "
                  f"({s['total_minutes']} min, {s['session_count']} sessions, "
                  f"{'cached' if s['from_cache'] else 'fresh'})")
    elif args.cmd == "dismiss":
        dismiss_suggestion(args.stream, cfg); print("ok")
    elif args.cmd == "accept":
        print(json.dumps(accept_suggestion(args.stream, args.name, cfg), indent=2))
    else:
        ap.print_help()
