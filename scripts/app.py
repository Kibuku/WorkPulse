"""
app.py — WorkPulse local web server + dashboard.

Serves the dashboard at http://localhost:5700
API endpoints used by the dashboard and tray app.

Run standalone:
  python scripts\\app.py
"""

from __future__ import annotations

import io
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional

# Paths/filenames to suppress from the dashboard display
_NOISE_PATTERNS = [
    re.compile(r"\.claude"),              # Claude Code internal files
    re.compile(r"[0-9a-f]{32,}"),        # Hash-named files (browser/system cache)
    re.compile(r"\.tmp\.\d+"),            # VS Code atomic save temps
    re.compile(r"TokenBroker"),
    re.compile(r"CryptnetUrlCache"),
    re.compile(r"workspaceStorage"),
    re.compile(r"globalStorage"),
    re.compile(r"Network Persistent State"),
    re.compile(r"Spelling[/\\]"),
    re.compile(r"SyncEngine"),            # OneDrive sync internals
    re.compile(r"ClientPolicy"),
    re.compile(r"FileSyncShell"),
    re.compile(r"activitywatch", re.IGNORECASE),
    re.compile(r"peewee-sqlite"),
    re.compile(r"OneAuth"),
    re.compile(r"IdentityCache"),
    re.compile(r"ConnectedDevicesPlatform"),
    re.compile(r"Office[/\\]Recent"),
    re.compile(r"FontCache"),
    re.compile(r"ShaderCache"),
    re.compile(r"\\Protect\\"),
    re.compile(r"SystemCertificates"),
    re.compile(r"\.db-journal$"),
    re.compile(r"\.db-wal$"),
    re.compile(r"\.db-shm$"),
]

def _is_noise(path_str: str) -> bool:
    return any(p.search(path_str) for p in _NOISE_PATTERNS)

import psutil
import uvicorn
from fastapi import FastAPI
from fastapi.responses import HTMLResponse, JSONResponse

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import load_config, resolve

app = FastAPI(title="WorkPulse", docs_url=None, redoc_url=None)

PORT = 5700

STREAM_COLORS = {
    "verst-carbon": "#22c55e",
    "majicom":      "#3b82f6",
    "masters":      "#8b5cf6",
    "dissertation": "#06b6d4",
    "consulting":   "#f59e0b",
    "personal-dev": "#a855f7",
    "personal-comms": "#ec4899",
}

# ── watcher process management ────────────────────────────────────────────────

_watcher_proc: Optional[subprocess.Popen] = None
_activity_proc: Optional[subprocess.Popen] = None


def _python() -> str:
    return str(Path(sys.executable))


def _watcher_script() -> str:
    return str(Path(__file__).resolve().parent / "watcher.py")


def is_watcher_running() -> bool:
    global _watcher_proc
    if _watcher_proc and _watcher_proc.poll() is None:
        return True
    # Also check if a watcher.py is running as a separate process
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
            if "watcher.py" in cmd and "status" not in cmd:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def start_watcher() -> bool:
    global _watcher_proc
    if is_watcher_running():
        return False
    _watcher_proc = subprocess.Popen(
        [_python(), _watcher_script()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def stop_watcher() -> bool:
    global _watcher_proc
    stopped = False
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
            if "watcher.py" in cmd and "status" not in cmd:
                proc.terminate()
                stopped = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _watcher_proc = None
    return stopped


# ── activity tracker process management ──────────────────────────────────────

def _activity_script() -> str:
    return str(Path(__file__).resolve().parent / "activity.py")


def is_activity_running() -> bool:
    global _activity_proc
    if _activity_proc and _activity_proc.poll() is None:
        return True
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
            if "activity.py" in cmd:
                return True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return False


def start_activity() -> bool:
    global _activity_proc
    if is_activity_running():
        return False
    _activity_proc = subprocess.Popen(
        [_python(), _activity_script()],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    return True


def stop_activity() -> bool:
    global _activity_proc
    stopped = False
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            cmd = " ".join(proc.info["cmdline"] or [])
            if "activity.py" in cmd:
                proc.terminate()
                stopped = True
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    _activity_proc = None
    return stopped


# ── data helpers ──────────────────────────────────────────────────────────────

def _parse_date_q(q: Optional[str]) -> date:
    """Parse a YYYY-MM-DD query parameter. Defaults to today on missing/invalid."""
    if not q:
        return date.today()
    try:
        return date.fromisoformat(q)
    except ValueError:
        return date.today()


def _load_activity_sessions(cfg: dict, target: date) -> list[dict]:
    """Load all activity sessions for one local date from logs/activity_<date>.jsonl."""
    log_path = resolve(cfg["paths"]["logs"]) / f"activity_{target.isoformat()}.jsonl"
    if not log_path.exists():
        return []
    out: list[dict] = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            out.append(json.loads(line))
        except json.JSONDecodeError:
            continue
    return out


def _load_file_events(days: int = 1, for_date: Optional[date] = None) -> list[dict]:
    cfg = load_config()
    logs_dir = resolve(cfg["paths"]["logs"])
    events = []
    anchor = for_date or date.today()
    # Include the adjacent day too — UTC log date may differ from local date
    for i in range(days + 1):
        d = anchor - timedelta(days=i)
        p = logs_dir / f"file_events_{d.isoformat()}.jsonl"
        if not p.exists():
            continue
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if not line:
                continue
            try:
                r = json.loads(line)
                if _is_noise(r["path"]):
                    continue
                # Skip smoke-test files
                if any(x in r["path"] for x in ["test_wk", "test_energy", "test_vault",
                                                   "test_desktop", "smoke_test",
                                                   "test_activity", "test_study",
                                                   "meeting_notes", "chapter3_notes",
                                                   "test_majicom", "test_verst"]):
                    continue
                events.append(r)
            except Exception:
                pass
    return events


def _aw_app_usage(hours: int = 8) -> list[dict]:
    """Pull top app usage from ActivityWatch for the last N hours."""
    try:
        import httpx
        cfg = load_config()
        base = cfg["activitywatch"]["base_url"]
        bucket = cfg["activitywatch"]["bucket_window"]
        from datetime import datetime, timezone
        now = datetime.now(timezone.utc)
        start = (now - timedelta(hours=hours)).isoformat()
        r = httpx.get(
            f"{base}/api/0/buckets/{bucket}/events",
            params={"start": start, "limit": -1},
            timeout=3,
            follow_redirects=True,
        )
        r.raise_for_status()
        totals: dict[str, float] = defaultdict(float)
        for ev in r.json():
            app = ev.get("data", {}).get("app", "Unknown")
            totals[app] += ev.get("duration", 0)
        return [
            {"app": app, "minutes": round(secs / 60, 1)}
            for app, secs in sorted(totals.items(), key=lambda x: -x[1])
            if secs > 30
        ][:15]
    except Exception:
        return []


# ── API routes ────────────────────────────────────────────────────────────────

@app.get("/api/status")
def api_status():
    return {"watcher_running": is_watcher_running()}


@app.post("/api/watcher/start")
def api_start():
    start_watcher()
    return {"watcher_running": is_watcher_running()}


@app.post("/api/watcher/stop")
def api_stop():
    stop_watcher()
    return {"watcher_running": is_watcher_running()}


@app.get("/api/activity")
def api_activity():
    events = _load_file_events(days=1)
    by_stream: dict[str, int] = defaultdict(int)
    untagged = 0
    for ev in events:
        stream = ev.get("stream")
        if stream:
            by_stream[stream] += 1
        else:
            untagged += 1
    tagged_total = sum(by_stream.values())
    total = tagged_total + untagged
    return {
        "total": total,
        "tagged": tagged_total,
        "untagged": untagged,
        "by_stream": [
            {
                "stream": k,
                "count": v,
                "color": STREAM_COLORS.get(k, "#6b7280"),
                "pct": round(v / tagged_total * 100) if tagged_total else 0,
            }
            for k, v in sorted(by_stream.items(), key=lambda x: -x[1])
        ],
    }


@app.get("/api/realwork")
def api_realwork(date: Optional[str] = None):  # noqa: A002 — param name is the public API
    """Real work stats from activity.py log — actual window-focus time.

    Query params:
      date  YYYY-MM-DD. Defaults to today. Pass any historical date to view it.
    """
    cfg = load_config()
    target = _parse_date_q(date)
    del date  # don't shadow the imported `date` class below
    sessions = _load_activity_sessions(cfg, target)

    if not sessions:
        return {
            "date": target.isoformat(),
            "total_active_minutes": 0,
            "by_stream": [],
            "by_app": [],
            "untagged_windows": [],
            "recent_sessions": [],
            "no_data": True,
        }

    active = [s for s in sessions if not s.get("idle")]

    # Re-tag at read-time so config edits to stream_path_patterns take effect
    # immediately on existing log entries (not just newly-written sessions).
    from scripts.activity import _tag_stream
    for s in active:
        retagged = _tag_stream(s.get("title") or "", s.get("exe_path") or "", cfg)
        if retagged:
            s["stream"] = retagged

    # Per-stream totals
    by_stream_secs: dict[str, float] = defaultdict(float)
    untagged_secs = 0.0
    # Per-app totals (all apps, tagged or not)
    by_app_secs: dict[str, float] = defaultdict(float)
    # Untagged window titles — what George is doing outside known projects
    by_untagged_title: dict[str, float] = defaultdict(float)

    for s in active:
        stream = s.get("stream")
        dur = s.get("duration_s", 0)
        app = s.get("app") or "(unknown)"
        title = s.get("title") or ""

        if stream:
            by_stream_secs[stream] += dur
        else:
            untagged_secs += dur
            # Group untagged by title (trimmed)
            trimmed = title[:70] if title else f"({app})"
            by_untagged_title[trimmed] += dur

        by_app_secs[app] += dur

    total_active_s = sum(by_stream_secs.values()) + untagged_secs

    by_stream = [
        {
            "stream": k,
            "minutes": round(v / 60, 1),
            "color": STREAM_COLORS.get(k, "#6b7280"),
            "pct": round(v / total_active_s * 100) if total_active_s else 0,
        }
        for k, v in sorted(by_stream_secs.items(), key=lambda x: -x[1])
    ]

    by_app = [
        {
            "app": k,
            "minutes": round(v / 60, 1),
            "pct": round(v / total_active_s * 100) if total_active_s else 0,
        }
        for k, v in sorted(by_app_secs.items(), key=lambda x: -x[1])[:12]
    ]

    # Top 10 untagged windows — what you were doing that's not mapped
    untagged_windows = [
        {
            "title": k,
            "minutes": round(v / 60, 1),
        }
        for k, v in sorted(by_untagged_title.items(), key=lambda x: -x[1])[:10]
        if v >= 10  # at least 10 seconds
    ]

    # Recent sessions: lowered threshold — show anything ≥5s (every real sample)
    meaningful = [s for s in active if s.get("duration_s", 0) >= 5]
    recent = meaningful[-25:][::-1]
    recent_out = [
        {
            "start": s["start"][11:19],
            "duration_min": round(s.get("duration_s", 0) / 60, 1),
            "duration_s": round(s.get("duration_s", 0), 0),
            "app": s.get("app", ""),
            "title": (s.get("title") or "")[:90],
            "stream": s.get("stream"),
            "color": STREAM_COLORS.get(s.get("stream") or "", "#6b7280"),
        }
        for s in recent
    ]

    return {
        "date": target.isoformat(),
        "total_active_minutes": round(total_active_s / 60, 1),
        "untagged_minutes": round(untagged_secs / 60, 1),
        "by_stream": by_stream,
        "by_app": by_app,
        "untagged_windows": untagged_windows,
        "recent_sessions": recent_out,
        "no_data": False,
    }


@app.get("/api/events/recent")
def api_recent(n: int = 20):
    events = _load_file_events(days=1)
    events = [e for e in events if not _is_noise(e.get("path", ""))]
    recent = events[-n:][::-1]
    return [
        {
            "time": r["timestamp"][11:19],
            "type": r["event_type"],
            "stream": r.get("stream"),
            "color": STREAM_COLORS.get(r.get("stream") or "", "#6b7280"),
            "name": Path(r["path"]).name,
            "size": r.get("size_bytes"),
        }
        for r in recent
    ]


@app.get("/api/ai")
def api_ai(date: Optional[str] = None):  # noqa: A002
    """AI session stats from ai_sessions.jsonl, for one local date.

    Query params:
      date  YYYY-MM-DD. Defaults to today.
    """
    cfg = load_config()
    target = _parse_date_q(date)
    del date  # avoid shadowing imported date class
    log_path = resolve(cfg["paths"]["logs"]) / "ai_sessions.jsonl"
    if not log_path.exists():
        return {"date": target.isoformat(), "count": 0, "total_cost": 0,
                "total_tokens": 0, "by_stream": [], "recent": []}

    sessions = []
    for line in log_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line:
            continue
        try:
            r = json.loads(line)
            dt = datetime.fromisoformat(r["timestamp"]).astimezone()
            if dt.date() == target:
                sessions.append(r)
        except Exception:
            pass

    by_stream: dict[str, dict] = defaultdict(lambda: {"count": 0, "cost": 0.0, "tokens": 0})
    for s in sessions:
        key = s.get("stream") or "untagged"
        by_stream[key]["count"] += 1
        by_stream[key]["cost"] += s.get("estimated_cost_usd", 0)
        by_stream[key]["tokens"] += s.get("input_tokens", 0) + s.get("output_tokens", 0)

    total_cost = sum(s.get("estimated_cost_usd", 0) for s in sessions)
    total_tokens = sum(s.get("input_tokens", 0) + s.get("output_tokens", 0) for s in sessions)

    return {
        "date": target.isoformat(),
        "count": len(sessions),
        "total_cost": round(total_cost, 4),
        "total_tokens": total_tokens,
        "by_stream": [
            {
                "stream": k,
                "count": v["count"],
                "cost": round(v["cost"], 4),
                "color": STREAM_COLORS.get(k, "#6b7280"),
            }
            for k, v in sorted(by_stream.items(), key=lambda x: -x[1]["count"])
        ],
        "recent": [
            {
                "time": s["timestamp"][11:16],
                "stream": s.get("stream"),
                "color": STREAM_COLORS.get(s.get("stream") or "", "#6b7280"),
                "task": s.get("task_summary", "")[:60],
                "tool": s.get("tool_used", ""),
                "cost": round(s.get("estimated_cost_usd", 0), 4),
                "tokens_in": s.get("input_tokens", 0),
                "tokens_out": s.get("output_tokens", 0),
            }
            for s in reversed(sessions[-10:])
        ],
    }


@app.get("/api/apps")
def api_apps():
    return _aw_app_usage(hours=8)


@app.get("/api/focus")
def api_focus(date: Optional[str] = None):  # noqa: A002
    """
    Focus data: stream timeline by hour, last-touched per stream,
    and a focus score based on context switching.

    Query params:
      date  YYYY-MM-DD. Defaults to today.
    """
    cfg = load_config()
    target = _parse_date_q(date)
    del date
    events = _load_file_events(days=1, for_date=target)
    streams = cfg["streams"]

    # Last touched per stream
    last_seen: dict[str, str] = {}
    for ev in events:
        s = ev.get("stream")
        if s:
            last_seen[s] = ev["timestamp"]

    now = datetime.now(timezone.utc)
    last_touched = []
    for stream, label in streams.items():
        ts = last_seen.get(stream)
        if ts:
            dt = datetime.fromisoformat(ts)
            minutes_ago = int((now - dt).total_seconds() / 60)
            if minutes_ago < 60:
                ago = f"{minutes_ago}m ago"
            elif minutes_ago < 1440:
                ago = f"{minutes_ago // 60}h ago"
            else:
                ago = f"{minutes_ago // 1440}d ago"
        else:
            ago = None
        last_touched.append({
            "stream": stream,
            "label": label,
            "last_ago": ago,
            "color": STREAM_COLORS.get(stream, "#6b7280"),
        })

    # Timeline: events per hour per stream (last 12 hours)
    hourly: dict[int, dict[str, int]] = {h: {} for h in range(24)}
    for ev in events:
        s = ev.get("stream")
        if not s:
            continue
        try:
            dt = datetime.fromisoformat(ev["timestamp"])
            # Convert UTC to local hour (approximate: use machine offset)
            local_dt = dt.astimezone()
            hour = local_dt.hour
            hourly[hour][s] = hourly[hour].get(s, 0) + 1
        except Exception:
            pass

    # Focus score: penalise context switches between streams
    stream_sequence = [ev.get("stream") for ev in events if ev.get("stream")]
    switches = sum(1 for i in range(1, len(stream_sequence)) if stream_sequence[i] != stream_sequence[i-1])
    total_tagged = len(stream_sequence)
    if total_tagged == 0:
        focus_score = None
        focus_label = "No project activity yet"
    elif switches == 0:
        focus_score = 100
        focus_label = "Deep focus — single stream all day"
    else:
        # Score drops with switch rate; 1 switch per 10 events = 90, etc.
        switch_rate = switches / total_tagged
        focus_score = max(0, round(100 - switch_rate * 200))
        if focus_score >= 80:
            focus_label = "Good focus"
        elif focus_score >= 50:
            focus_label = "Moderate switching between projects"
        else:
            focus_label = "High context switching — consider time-blocking"

    # Only return hours that have activity
    active_hours = [
        {"hour": h, "streams": data}
        for h, data in hourly.items()
        if data
    ]

    return {
        "date": target.isoformat(),
        "last_touched": last_touched,
        "active_hours": sorted(active_hours, key=lambda x: x["hour"]),
        "focus_score": focus_score,
        "focus_label": focus_label,
        "switches": switches,
    }


# ── calendar / historical view ────────────────────────────────────────────────

@app.get("/api/calendar")
def api_calendar(days: int = 14):
    """Per-day summary for the last N days — used by the weekly heatmap.

    Returns one entry per day: total active minutes + per-stream breakdown.
    Days with no log file are returned with total_minutes=0.
    """
    days = max(1, min(int(days), 90))  # cap at 90 days
    cfg = load_config()
    today = date.today()
    out = []
    for i in range(days):
        d = today - timedelta(days=days - 1 - i)  # oldest first
        sessions = _load_activity_sessions(cfg, d)
        active = [s for s in sessions if not s.get("idle")]
        # Re-tag on the fly so config edits affect historical views.
        try:
            from scripts.activity import _tag_stream
            for s in active:
                t = _tag_stream(s.get("title") or "", s.get("exe_path") or "", cfg)
                if t:
                    s["stream"] = t
        except Exception:
            pass
        by_stream: dict[str, float] = defaultdict(float)
        total = 0.0
        for s in active:
            dur = s.get("duration_s", 0)
            total += dur
            if s.get("stream"):
                by_stream[s["stream"]] += dur
        out.append({
            "date": d.isoformat(),
            "weekday": d.strftime("%a"),
            "total_minutes": round(total / 60, 1),
            "by_stream": [
                {"stream": k, "minutes": round(v / 60, 1),
                 "color": STREAM_COLORS.get(k, "#6b7280")}
                for k, v in sorted(by_stream.items(), key=lambda x: -x[1])
            ],
        })
    return {"days": out}


# ── system / status (for dashboard chrome) ───────────────────────────────────

@app.get("/api/system")
def api_system():
    """What the dashboard needs to render its chrome: secret status,
    watcher status, identity, available streams, today's date."""
    from scripts.wp_secrets import status as secret_status, has as has_secret
    from scripts.llm import backend_status, active_backend
    cfg = load_config()
    identity = {}
    id_path = resolve("config/identity.yaml")
    if id_path.exists():
        try:
            import yaml
            identity = yaml.safe_load(id_path.read_text(encoding="utf-8")) or {}
        except Exception:
            identity = {}
    ai_backend = active_backend(cfg)
    return {
        "watcher_running":  is_watcher_running(),
        "ai_enabled":       ai_backend != "none",
        "ai_backend":       ai_backend,              # "anthropic" | "ollama" | "none"
        "llm":              backend_status(cfg),     # full Ollama detection details
        "email_enabled":    bool(cfg.get("email", {}).get("enabled")) and has_secret("smtp_password"),
        "secrets":          secret_status(),
        "identity": {
            "actor_label":  identity.get("actor_label", ""),
            "organization": identity.get("organization", ""),
        },
        "streams": [
            {"key": k, "label": v, "color": STREAM_COLORS.get(k, "#6b7280")}
            for k, v in cfg.get("streams", {}).items()
        ],
        "today": date.today().isoformat(),
    }


# ── Loop A: user correction → permanent learned rule ─────────────────────────

@app.post("/api/learn")
async def api_learn(payload: dict):
    """Body: {pattern: str, stream: str|null, raw_title: str|null}.
    pattern  — substring that the learning module will match on (lowercased,
               normalized). If omitted, derived from raw_title.
    stream   — the stream key to assign, or null to mark this pattern as
               permanently untagged.
    """
    from scripts.learning import normalize_title, _add_rule
    cfg = load_config()
    raw_title = (payload.get("raw_title") or "").strip()
    pattern   = (payload.get("pattern")   or normalize_title(raw_title)).strip().lower()
    stream    = payload.get("stream")
    if stream and stream not in cfg.get("streams", {}):
        return JSONResponse({"error": f"unknown stream: {stream}"}, status_code=400)
    if not pattern or len(pattern) < 2:
        return JSONResponse({"error": "pattern too short"}, status_code=400)
    _add_rule(cfg=cfg, pattern=pattern, stream=stream, raw_title=raw_title, source="user")
    return {"ok": True, "pattern": pattern, "stream": stream}


# ── Settings: in-app secret + email config (no terminal needed) ──────────────

_ALLOWED_SECRETS = {"anthropic_key", "smtp_password", "smtp_user", "smtp_to"}


@app.post("/api/settings/secret")
async def api_set_secret(payload: dict):
    """Body: {name: str, value: str|null}.
    Writes to config/secrets.json. Pass value=null to delete.
    Only known canonical secret names are accepted."""
    from scripts.wp_secrets import set_ as set_secret
    name  = (payload.get("name") or "").strip()
    value = payload.get("value")
    if name not in _ALLOWED_SECRETS:
        return JSONResponse({"error": f"unknown secret name: {name}"}, status_code=400)
    set_secret(name, value)
    return {"ok": True, "name": name, "configured": value is not None and value != ""}


@app.post("/api/settings/email")
async def api_set_email(payload: dict):
    """Body: {enabled?: bool, smtp_user?: str, from_addr?: str, to_addr?: str}.
    Writes flagged fields back into config/config.yaml. enabled requires
    smtp_password to be configured first; otherwise rejected."""
    import yaml
    from scripts.wp_secrets import has as has_secret
    cfg_path = resolve("config/config.yaml")
    if not cfg_path.exists():
        return JSONResponse({"error": "config.yaml not found"}, status_code=500)
    cfg = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    email = cfg.setdefault("email", {})
    if "enabled" in payload:
        enabled = bool(payload["enabled"])
        if enabled and not has_secret("smtp_password"):
            return JSONResponse(
                {"error": "set smtp_password first via /api/settings/secret"},
                status_code=400,
            )
        email["enabled"] = enabled
    for field in ("smtp_user", "from_addr", "to_addr"):
        if field in payload:
            email[field] = (payload[field] or "").strip()
    cfg_path.write_text(yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
    return {"ok": True, "email": email}


# ── Jobs API (v1.1a Coach surface) ───────────────────────────────────────────

@app.get("/api/jobs/active")
def api_jobs_active():
    """Active jobs with their session rollup + AI-lift opportunities.
    Drives the 'Jobs in flight' card on the dashboard."""
    from scripts.jobs import list_active, rollup
    out = []
    for rec in list_active():
        r = rollup(rec["id"])
        if r is None:
            continue
        r["stream_color"] = STREAM_COLORS.get(r.get("stream") or "", "#6b7280")
        out.append(r)
    return {"jobs": out}


@app.get("/api/jobs/recent")
def api_jobs_recent(days: int = 30):
    """Active + ended jobs from the last N days, newest first.
    Light payload — does NOT include session rollups or lift detection."""
    from scripts.jobs import list_all
    days = max(1, min(int(days), 365))
    out = []
    for rec in list_all(days_back=days):
        out.append({
            **rec,
            "stream_color": STREAM_COLORS.get(rec.get("stream") or "", "#6b7280"),
        })
    return {"jobs": out, "days": days}


@app.get("/api/jobs/{job_id}")
def api_job_detail(job_id: str):
    """Full detail for one job — rollup + all sessions + lift opportunities."""
    from scripts.jobs import rollup
    r = rollup(job_id, include_sessions=True)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    r["stream_color"] = STREAM_COLORS.get(r.get("stream") or "", "#6b7280")
    return r


@app.post("/api/jobs/start")
async def api_job_start(payload: dict):
    """Body: {name, stream, note?}. Auto-ends any existing job in the same stream."""
    from scripts.jobs import start_job
    try:
        rec = start_job(
            name=payload.get("name") or "",
            stream=payload.get("stream"),
            note=payload.get("note") or "",
        )
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, **rec}


@app.post("/api/jobs/{job_id}/end")
async def api_job_end(job_id: str):
    from scripts.jobs import end_job
    try:
        rec = end_job(job_id)
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"ok": True, **rec}


# ── dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorkPulse</title>
<style>
:root {
  --bg:        #faf9f6;
  --surface:   #ffffff;
  --border:    #ece9e1;
  --border-d:  #d9d6cd;
  --text:      #1c1c1a;
  --text-soft: #6b6b62;
  --text-faint:#a8a8a0;
  --shadow:    0 1px 3px rgba(28,28,26,0.05), 0 4px 16px rgba(28,28,26,0.03);
  --radius:    16px;
  --green:     #22c55e;
  --green-d:   #16a34a;
  --amber-bg:  #fef7e6;
  --amber-br:  #f5d77a;
  --amber-tx:  #6b4a0f;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { background: var(--bg); }
body {
  background: var(--bg); color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI Variable', 'Segoe UI', 'Inter', system-ui, sans-serif;
  font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
  max-width: 1080px; margin: 0 auto; padding: 56px 40px 96px;
}

/* Top bar */
.topbar { display: flex; justify-content: space-between; align-items: baseline; margin-bottom: 64px; }
.brand  { font-size: 19px; font-weight: 600; letter-spacing: -0.3px; display: flex; align-items: center; gap: 10px; }
.pulse  { width: 8px; height: 8px; border-radius: 50%; background: var(--green); position: relative; }
.pulse::after { content: ''; position: absolute; inset: -4px; border-radius: 50%; background: var(--green);
  opacity: 0.35; animation: ping 2.4s ease-out infinite; }
.pulse.off { background: #c8c5bb; }
.pulse.off::after { display: none; }
@keyframes ping { 0% { transform: scale(0.6); opacity: 0.5; } 70%, 100% { transform: scale(2.2); opacity: 0; } }
.date { color: var(--text-soft); font-size: 14px; }

/* Hero */
.hero { margin-bottom: 56px; }
.hero h1 { font-size: 30px; font-weight: 500; letter-spacing: -0.7px; line-height: 1.3;
  max-width: 780px; margin-bottom: 12px; }
.hero h1 .strong { font-weight: 700; }
.hero .sub { color: var(--text-soft); font-size: 15px; }

/* Card */
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 28px 30px; margin-bottom: 18px; box-shadow: var(--shadow); }
.card-row { display: grid; grid-template-columns: 1.4fr 1fr; gap: 18px; margin-bottom: 18px; }
.eyebrow { font-size: 11px; font-weight: 600; letter-spacing: 1.6px; text-transform: uppercase;
  color: var(--text-soft); margin-bottom: 22px; }

/* Donut */
.donut-wrap { display: flex; align-items: center; gap: 36px; }
.donut { width: 168px; height: 168px; border-radius: 50%;
  background: conic-gradient(var(--border) 0% 100%);
  display: flex; align-items: center; justify-content: center; position: relative; flex-shrink: 0; }
.donut::after { content: ''; position: absolute; inset: 22px; background: var(--surface);
  border-radius: 50%; box-shadow: inset 0 0 0 1px var(--border); }
.donut-c { position: relative; z-index: 1; text-align: center; }
.donut-c .num { font-size: 26px; font-weight: 600; letter-spacing: -0.5px; line-height: 1.1; }
.donut-c .lbl { font-size: 10px; color: var(--text-soft); letter-spacing: 1.2px;
  text-transform: uppercase; margin-top: 4px; }
.legend { flex: 1; min-width: 0; }
.lg-row { display: flex; align-items: center; gap: 12px; padding: 8px 0;
  border-bottom: 1px dashed var(--border); }
.lg-row:last-child { border-bottom: none; }
.lg-dot { width: 9px; height: 9px; border-radius: 50%; flex-shrink: 0; }
.lg-name { flex: 1; font-size: 14px; text-transform: capitalize; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.lg-min { font-size: 13px; color: var(--text-soft); font-variant-numeric: tabular-nums; min-width: 64px; text-align: right; }
.lg-pct { font-size: 12px; color: var(--text-faint); width: 38px; text-align: right; font-variant-numeric: tabular-nums; }

/* Last active */
.la-row { display: flex; align-items: center; gap: 12px; padding: 11px 0;
  border-bottom: 1px dashed var(--border); }
.la-row:last-child { border-bottom: none; }
.la-name { flex: 1; font-size: 14px; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.la-ago  { font-size: 12px; color: var(--text-soft); font-variant-numeric: tabular-nums; }

/* Timeline */
.tl-wrap { padding-bottom: 4px; }
.tl { display: flex; gap: 2px; height: 64px; align-items: flex-end; }
.tl-h { flex: 1; height: 100%; display: flex; flex-direction: column;
  justify-content: flex-end; cursor: default; }
.tl-bar { width: 100%; min-height: 0; border-radius: 3px 3px 0 0; transition: opacity 0.15s; }
.tl-bar:hover { opacity: 0.75; }
.tl-empty { width: 100%; height: 4px; background: var(--border); border-radius: 2px; }
.tl-axis { display: flex; gap: 2px; margin-top: 8px; }
.tl-tick { flex: 1; font-size: 10px; color: var(--text-faint); text-align: center;
  font-variant-numeric: tabular-nums; }

/* Attention list */
.attn-row { display: flex; align-items: center; gap: 16px; padding: 13px 0;
  border-bottom: 1px dashed var(--border); }
.attn-row:last-child { border-bottom: none; }
.attn-app { font-size: 11px; color: var(--text-faint); text-transform: uppercase;
  letter-spacing: 0.6px; min-width: 90px; }
.attn-title { flex: 1; font-size: 14px; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.attn-min { font-size: 13px; color: var(--text-soft); font-variant-numeric: tabular-nums; min-width: 64px; text-align: right; }

/* App bars (small, secondary) */
.app-row { display: flex; align-items: center; gap: 12px; padding: 6px 0; }
.app-name { font-size: 13px; min-width: 130px; color: var(--text-soft);
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.app-track { flex: 1; height: 4px; background: var(--border); border-radius: 2px; overflow: hidden; }
.app-fill { height: 100%; background: var(--text-soft); border-radius: 2px; }
.app-min { font-size: 12px; color: var(--text-faint); font-variant-numeric: tabular-nums; min-width: 48px; text-align: right; }

/* AI strip */
.ai-strip { display: flex; gap: 44px; flex-wrap: wrap; }
.ai-stat .num { font-size: 22px; font-weight: 600; letter-spacing: -0.4px; line-height: 1.2; }
.ai-stat .lbl { font-size: 10px; color: var(--text-soft); letter-spacing: 1.2px;
  text-transform: uppercase; margin-top: 4px; }

/* Details (collapsible power-user table) */
details { margin-top: 8px; }
details > summary { cursor: pointer; font-size: 12px; color: var(--text-soft);
  letter-spacing: 0.4px; padding: 6px 0; user-select: none; list-style: none; }
details > summary::before { content: '▸'; display: inline-block; margin-right: 6px;
  transition: transform 0.15s; }
details[open] > summary::before { transform: rotate(90deg); }
.session-table { width: 100%; border-collapse: collapse; font-size: 12px; margin-top: 12px; }
.session-table th { text-align: left; padding: 8px 10px 8px 0; color: var(--text-faint);
  font-size: 10px; text-transform: uppercase; letter-spacing: 0.8px;
  font-weight: 500; border-bottom: 1px solid var(--border); }
.session-table td { padding: 8px 10px 8px 0; border-bottom: 1px solid var(--border); vertical-align: top; }
.session-table tr:last-child td { border-bottom: none; }
.session-table .mono { font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  font-size: 11px; color: var(--text-soft); white-space: nowrap; }
.session-table .title-cell { color: var(--text); max-width: 360px;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }

/* Stream chip */
.chip { display: inline-block; padding: 2px 9px; border-radius: 999px; font-size: 11px;
  font-weight: 600; color: #fff; text-transform: capitalize; line-height: 1.5; }
.chip.muted { background: var(--border-d); color: var(--text-soft); font-weight: 500; }

/* Footer */
footer { margin-top: 64px; padding-top: 24px; border-top: 1px solid var(--border);
  display: flex; justify-content: space-between; align-items: center;
  font-size: 12px; color: var(--text-faint); }
.foot-status { display: flex; align-items: center; gap: 8px; }
.btn-link { background: none; border: none; color: var(--text-soft); cursor: pointer;
  font-size: 12px; padding: 4px 10px; border-radius: 6px; font-weight: 500;
  transition: background 0.12s; font-family: inherit; }
.btn-link:hover { background: var(--border); color: var(--text); }

.empty { color: var(--text-faint); font-size: 14px; padding: 20px 0; text-align: center; font-style: italic; }

/* Date navigation chips */
.datebar { display: flex; align-items: center; gap: 6px; flex-wrap: wrap;
  margin: 8px 0 32px; }
.date-chip { background: transparent; border: 1px solid var(--border); color: var(--text-soft);
  font-size: 12px; padding: 6px 14px; border-radius: 999px; cursor: pointer;
  font-family: inherit; transition: all 0.12s; }
.date-chip:hover { border-color: var(--border-d); color: var(--text); }
.date-chip.active { background: var(--text); border-color: var(--text); color: #fff; font-weight: 500; }
.date-chip input[type=date] { background: transparent; border: none; color: inherit;
  font: inherit; cursor: pointer; padding: 0; max-width: 130px; }
.date-chip input[type=date]:focus { outline: none; }

/* Settings button (top-right of brand bar) */
.icon-btn { background: transparent; border: 1px solid var(--border); color: var(--text-soft);
  padding: 6px 10px; border-radius: 8px; cursor: pointer; font-size: 13px;
  font-family: inherit; transition: all 0.12s; }
.icon-btn:hover { background: var(--surface); color: var(--text); border-color: var(--border-d); }
.topbar-right { display: flex; align-items: center; gap: 14px; }

/* AI-off banner */
.banner { background: var(--amber-bg); border: 1px solid var(--amber-br);
  color: var(--amber-tx); border-radius: 12px; padding: 14px 20px;
  display: flex; justify-content: space-between; align-items: center;
  font-size: 14px; margin-bottom: 24px; }
.banner button { background: var(--amber-tx); color: var(--amber-bg); border: none;
  padding: 6px 14px; border-radius: 6px; cursor: pointer; font-size: 13px;
  font-weight: 500; font-family: inherit; }
.banner button:hover { opacity: 0.88; }

/* Weekly heatmap */
.heatmap { display: flex; gap: 4px; align-items: flex-end; padding: 4px 0; }
.hm-col { flex: 1; display: flex; flex-direction: column; align-items: center;
  gap: 6px; cursor: pointer; min-width: 30px; }
.hm-bar-wrap { height: 80px; width: 100%; display: flex; flex-direction: column;
  justify-content: flex-end; }
.hm-bar { width: 100%; border-radius: 4px 4px 0 0; transition: opacity 0.15s; min-height: 3px; }
.hm-col.empty .hm-bar { background: var(--border); height: 3px; }
.hm-col:hover .hm-bar { opacity: 0.78; }
.hm-col.selected .hm-bar { box-shadow: 0 0 0 2px var(--text); border-radius: 4px; }
.hm-day { font-size: 10px; color: var(--text-faint); text-transform: uppercase;
  letter-spacing: 0.5px; font-variant-numeric: tabular-nums; }
.hm-col.selected .hm-day { color: var(--text); font-weight: 600; }

/* Loop A: Tag-as dropdown next to untagged windows */
.attn-tag { position: relative; }
.tag-btn { background: var(--bg); border: 1px solid var(--border); color: var(--text-soft);
  font-size: 11px; padding: 4px 10px; border-radius: 6px; cursor: pointer;
  font-family: inherit; font-weight: 500; transition: all 0.12s; }
.tag-btn:hover { border-color: var(--text-soft); color: var(--text); }
.tag-menu { display: none; position: absolute; right: 0; top: calc(100% + 4px);
  background: var(--surface); border: 1px solid var(--border-d); border-radius: 8px;
  box-shadow: 0 4px 16px rgba(28,28,26,0.12); padding: 4px; min-width: 180px; z-index: 10; }
.tag-menu.open { display: block; }
.tag-opt { display: flex; align-items: center; gap: 8px; padding: 7px 10px;
  border-radius: 6px; cursor: pointer; font-size: 13px; }
.tag-opt:hover { background: var(--bg); }
.tag-opt.ignore { color: var(--text-soft); border-top: 1px solid var(--border); margin-top: 4px; padding-top: 10px; }

/* Settings modal */
.modal-bg { display: none; position: fixed; inset: 0; background: rgba(28,28,26,0.42);
  z-index: 50; align-items: center; justify-content: center; padding: 24px; }
.modal-bg.open { display: flex; }
.modal { background: var(--surface); border-radius: 18px; max-width: 540px; width: 100%;
  max-height: calc(100vh - 48px); overflow-y: auto;
  box-shadow: 0 20px 60px rgba(28,28,26,0.25); padding: 36px 36px 28px; }
.modal h2 { font-size: 22px; font-weight: 600; letter-spacing: -0.4px; margin-bottom: 4px; }
.modal .sub { color: var(--text-soft); font-size: 13px; margin-bottom: 28px; }
.modal-row { margin-bottom: 22px; }
.modal-row label { display: block; font-size: 13px; font-weight: 500; margin-bottom: 6px; }
.modal-row .help { font-size: 12px; color: var(--text-soft); margin-top: 4px; }
.modal-row .help a { color: var(--text-soft); }
.modal-row input[type=text],
.modal-row input[type=email],
.modal-row input[type=password] {
  width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px;
  font-family: inherit; font-size: 14px; color: var(--text); background: var(--bg);
}
.modal-row input:focus { outline: none; border-color: var(--text-soft); background: var(--surface); }
.toggle-row { display: flex; justify-content: space-between; align-items: center; }
.modal-actions { display: flex; justify-content: space-between; align-items: center;
  border-top: 1px solid var(--border); padding-top: 20px; margin-top: 8px; }
.btn-primary { background: var(--text); color: var(--bg); border: none;
  padding: 10px 22px; border-radius: 8px; cursor: pointer; font-family: inherit;
  font-size: 14px; font-weight: 500; transition: opacity 0.12s; }
.btn-primary:hover { opacity: 0.88; }
.btn-ghost { background: transparent; border: none; color: var(--text-soft);
  cursor: pointer; font-family: inherit; font-size: 13px; padding: 8px 12px; }
.status-dot { display: inline-block; width: 7px; height: 7px; border-radius: 50%;
  background: var(--green); margin-right: 6px; vertical-align: middle; }
.status-dot.off { background: var(--text-faint); }
.toast { position: fixed; bottom: 24px; right: 24px; background: var(--text); color: var(--bg);
  padding: 12px 20px; border-radius: 10px; font-size: 13px; z-index: 100;
  box-shadow: 0 8px 24px rgba(28,28,26,0.25); opacity: 0; transition: opacity 0.2s;
  pointer-events: none; }
.toast.show { opacity: 1; }

/* Jobs in flight (Coach card) */
.jobs-eyebrow { display: flex; justify-content: space-between; align-items: center; }
.btn-start-job { background: var(--text); color: var(--bg); border: none;
  font-size: 12px; font-weight: 500; padding: 7px 14px; border-radius: 8px;
  cursor: pointer; font-family: inherit; text-transform: none; letter-spacing: 0; }
.btn-start-job:hover { opacity: 0.88; }

.job-card { padding: 18px 0; border-bottom: 1px dashed var(--border); }
.job-card:last-child { border-bottom: none; padding-bottom: 4px; }
.job-card:first-of-type { padding-top: 4px; }
.job-head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.job-name { font-size: 17px; font-weight: 600; letter-spacing: -0.3px; flex: 1;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.job-actions { display: flex; gap: 4px; }
.job-end-btn { background: transparent; border: 1px solid var(--border);
  color: var(--text-soft); font-size: 12px; padding: 5px 12px; border-radius: 6px;
  cursor: pointer; font-family: inherit; transition: all 0.12s; }
.job-end-btn:hover { background: var(--bg); color: var(--text); border-color: var(--border-d); }
.job-meta { font-size: 13px; color: var(--text-soft); margin-bottom: 10px; }
.job-paused { color: var(--text-faint); font-style: italic; margin-left: 8px; }
.job-apps { font-size: 13px; color: var(--text-soft); margin-bottom: 14px; line-height: 1.7; }
.job-apps .app-tag { white-space: nowrap; margin-right: 14px; }
.job-apps .app-tag strong { color: var(--text); font-weight: 500; }

.job-lift { background: var(--bg); border-radius: 10px; padding: 14px 16px;
  border: 1px solid var(--border); }
.job-lift-head { font-size: 11px; font-weight: 600; letter-spacing: 0.6px;
  text-transform: uppercase; color: var(--text-soft); margin-bottom: 10px; }
.lift-item { padding: 8px 0; border-top: 1px dashed var(--border); }
.lift-item:first-of-type { border-top: none; padding-top: 0; }
.lift-title { font-size: 13px; font-weight: 500; color: var(--text); margin-bottom: 2px; }
.lift-suggest { font-size: 12px; color: var(--text-soft); line-height: 1.5; }
.job-lift-empty { font-size: 12px; color: var(--text-faint); font-style: italic; }

/* Start-Job modal — reuses .modal-bg + .modal styles */
.sj-row { margin-bottom: 18px; }
.sj-row label { display: block; font-size: 12px; font-weight: 500;
  color: var(--text-soft); margin-bottom: 6px; }
.sj-row input, .sj-row select, .sj-row textarea {
  width: 100%; padding: 10px 12px; border: 1px solid var(--border); border-radius: 8px;
  font-family: inherit; font-size: 14px; color: var(--text); background: var(--bg); }
.sj-row input:focus, .sj-row select:focus, .sj-row textarea:focus {
  outline: none; border-color: var(--text-soft); background: var(--surface); }
.sj-row textarea { resize: vertical; min-height: 60px; }
</style>
</head>
<body>

<div class="topbar">
  <div class="brand"><span class="pulse" id="pulse"></span>WorkPulse <span id="actor-suffix" style="color:var(--text-soft);font-weight:400;margin-left:6px"></span></div>
  <div class="topbar-right">
    <span class="date" id="date"></span>
    <button class="icon-btn" onclick="openSettings()" title="Settings">Settings</button>
  </div>
</div>

<!-- AI-off banner (only shown when no backend available) -->
<div class="banner" id="ai-banner" style="display:none">
  <div>
    <strong>Smart auto-tagging is off.</strong>
    <span id="ai-banner-detail">Add an Anthropic API key in Settings, or install Ollama for free local AI.</span>
  </div>
  <button onclick="openSettings()">Open Settings</button>
</div>

<!-- Date navigation -->
<div class="datebar" id="datebar"></div>

<div class="hero">
  <h1 id="hero-headline">Loading…</h1>
  <div class="sub" id="hero-sub"></div>
</div>

<!-- Jobs in flight (the v1.1a Coach surface) -->
<div class="card jobs-card">
  <div class="eyebrow jobs-eyebrow">
    <span>Jobs in flight</span>
    <button class="btn-start-job" onclick="openStartJob()">+ Start a job</button>
  </div>
  <div id="jobs-panel"><div class="empty">Loading…</div></div>
</div>

<!-- Weekly heatmap -->
<div class="card">
  <div class="eyebrow">Last 14 days</div>
  <div class="heatmap" id="heatmap"><div class="empty">Loading…</div></div>
</div>

<div class="card-row">
  <div class="card">
    <div class="eyebrow">Where the time went</div>
    <div id="donut-panel"><div class="empty">Loading…</div></div>
  </div>
  <div class="card">
    <div class="eyebrow">Last active</div>
    <div id="last-active"><div class="empty">Loading…</div></div>
  </div>
</div>

<div class="card">
  <div class="eyebrow">Today's flow</div>
  <div class="tl-wrap">
    <div class="tl" id="timeline"></div>
    <div class="tl-axis" id="tl-axis"></div>
  </div>
</div>

<div class="card">
  <div class="eyebrow">Needs your attention</div>
  <div id="attn-panel"><div class="empty">Nothing untagged today.</div></div>
</div>

<div class="card">
  <div class="eyebrow">Apps used</div>
  <div id="apps-panel"><div class="empty">Loading…</div></div>
</div>

<div class="card">
  <div class="eyebrow">AI sessions</div>
  <div id="ai-panel"><div class="empty">Loading…</div></div>
</div>

<details>
  <summary>Show every window visit today</summary>
  <div class="card" style="margin-top: 12px">
    <div id="sessions-panel"><div class="empty">Loading…</div></div>
  </div>
</details>

<footer>
  <div class="foot-status">
    <span id="foot-status">Checking watcher…</span>
    <button class="btn-link" id="toggle-btn" onclick="toggleWatcher()">…</button>
  </div>
  <div id="refresh-label"></div>
</footer>

<!-- Settings modal (hidden until openSettings()) -->
<div class="modal-bg" id="settings-modal" onclick="if(event.target===this)closeSettings()">
  <div class="modal" role="dialog" aria-labelledby="set-title">
    <h2 id="set-title">Settings</h2>
    <div class="sub">All values stay on this machine. Nothing is sent anywhere except direct API calls you make.</div>

    <!-- AI backend status block (read-only summary) -->
    <div class="modal-row" style="background:var(--bg);padding:14px 16px;border-radius:10px;border:1px solid var(--border)">
      <label style="margin-bottom:8px">AI backend currently in use</label>
      <div id="backend-summary" style="font-size:13px;color:var(--text-soft);line-height:1.6">
        Loading…
      </div>
    </div>

    <div class="modal-row">
      <label>
        <span id="anthropic-status" class="status-dot off"></span>
        Anthropic API key <span style="color:var(--text-faint);font-weight:400">(cloud — best quality, pay per call)</span>
      </label>
      <input type="password" id="anthropic-input" placeholder="sk-ant-…" autocomplete="off" />
      <div class="help">
        Unlocks AI auto-tagging of windows and (later) AI-written reports.
        <a href="https://console.anthropic.com/settings/keys" target="_blank" rel="noopener">Get a key →</a>
      </div>
    </div>

    <div class="modal-row">
      <label>
        <span id="smtp-status" class="status-dot off"></span>
        Gmail App Password (for email reports)
      </label>
      <input type="password" id="smtp-input" placeholder="xxxx xxxx xxxx xxxx" autocomplete="off" />
      <div class="help">
        Optional. Used only if you turn on email reports below.
        <a href="https://myaccount.google.com/apppasswords" target="_blank" rel="noopener">Create one →</a>
      </div>
    </div>

    <div class="modal-row">
      <label>Gmail address (used as the From and To)</label>
      <input type="email" id="smtp-user-input" placeholder="you@gmail.com" autocomplete="off" />
    </div>

    <div class="modal-row toggle-row">
      <div>
        <label style="margin:0">Email me daily &amp; weekly reports</label>
        <div class="help">Off by default. Requires App Password above.</div>
      </div>
      <label class="switch">
        <input type="checkbox" id="email-enabled" />
      </label>
    </div>

    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeSettings()">Cancel</button>
      <button class="btn-primary" onclick="saveSettings()">Save</button>
    </div>
  </div>
</div>

<!-- Start-Job modal -->
<div class="modal-bg" id="sj-modal" onclick="if(event.target===this)closeStartJob()">
  <div class="modal" role="dialog" aria-labelledby="sj-title">
    <h2 id="sj-title">Start a job</h2>
    <div class="sub">A job is a coherent unit of work — "NKCC Q3 report", "Chapter 3 lit review". Sessions inside this stream from now until you end the job will roll up to it.</div>
    <div class="sj-row">
      <label for="sj-name">What are you working on?</label>
      <input type="text" id="sj-name" placeholder="e.g. NKCC Q3 report" autocomplete="off" />
    </div>
    <div class="sj-row">
      <label for="sj-stream">Stream</label>
      <select id="sj-stream"></select>
    </div>
    <div class="sj-row">
      <label for="sj-note">Note (optional)</label>
      <textarea id="sj-note" placeholder="A line or two about the goal of this job — used later when reviewing how it went."></textarea>
    </div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeStartJob()">Cancel</button>
      <button class="btn-primary" onclick="submitStartJob()">Start job</button>
    </div>
  </div>
</div>

<div class="toast" id="toast"></div>

<script>
// ── State ──────────────────────────────────────────────────────────────────
let watcherRunning = false;
let aiEnabled = false;
let currentDate = null;       // YYYY-MM-DD; null = today
let availableStreams = [];    // [{key, label, color}]
let todayISO = null;

const TITLE_CASE = s => (s || '').replace(/-/g,' ').replace(/\\b\\w/g, c => c.toUpperCase());

function fmtMins(m) {
  if (m == null) return '—';
  m = Math.round(m);
  if (m < 1) return '<1 min';
  if (m < 60) return m + ' min';
  const h = Math.floor(m / 60), mm = m % 60;
  return mm ? `${h}h ${mm}m` : `${h}h`;
}

function escapeHtml(s) {
  return (s || '').replace(/[&<>"']/g, c =>
    ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
}

function activeDate() { return currentDate || todayISO; }
function isViewingToday() { return !currentDate || currentDate === todayISO; }

function relativeDateLabel(iso) {
  if (!iso || !todayISO) return iso || '';
  const t = new Date(todayISO + 'T12:00:00');
  const d = new Date(iso + 'T12:00:00');
  const diff = Math.round((t - d) / 86400000);
  if (diff === 0) return 'Today';
  if (diff === 1) return 'Yesterday';
  if (diff > 1 && diff < 7) return d.toLocaleDateString(undefined, {weekday:'long'});
  return d.toLocaleDateString(undefined, {weekday:'short', month:'short', day:'numeric'});
}

function showToast(msg) {
  const t = document.getElementById('toast');
  t.textContent = msg;
  t.classList.add('show');
  clearTimeout(t._timer);
  t._timer = setTimeout(() => t.classList.remove('show'), 2400);
}

function setHeaderDate() {
  const iso = activeDate();
  const d = iso ? new Date(iso + 'T12:00:00') : new Date();
  document.getElementById('date').textContent =
    d.toLocaleDateString(undefined, {weekday:'long', month:'long', day:'numeric'});
}

// ── System + identity ──────────────────────────────────────────────────────
let systemSnapshot = null;  // stash for openSettings()

async function fetchSystem() {
  const r = await fetch('/api/system');
  const d = await r.json();
  systemSnapshot = d;
  watcherRunning = d.watcher_running;
  aiEnabled = d.ai_enabled;
  availableStreams = d.streams || [];
  todayISO = d.today;

  document.getElementById('pulse').className = 'pulse' + (watcherRunning ? '' : ' off');
  document.getElementById('foot-status').textContent =
    watcherRunning ? 'Watcher running' : 'Watcher stopped';
  document.getElementById('toggle-btn').textContent =
    watcherRunning ? 'Pause' : 'Start';

  const suffix = d.identity && d.identity.actor_label
    ? '— ' + d.identity.actor_label : '';
  document.getElementById('actor-suffix').textContent = suffix;

  // AI banner — tailored to what's actually missing
  const banner = document.getElementById('ai-banner');
  const detail = document.getElementById('ai-banner-detail');
  if (aiEnabled) {
    banner.style.display = 'none';
  } else {
    banner.style.display = 'flex';
    const llm = d.llm || {};
    if (llm.ollama && llm.ollama.installed && !llm.ollama.model_ready) {
      detail.innerHTML = `Ollama is running but model <code>${llm.ollama.want_model}</code> isn't pulled yet. Run: <code>ollama pull ${llm.ollama.want_model}</code> — or add an Anthropic key in Settings.`;
    } else {
      detail.textContent = 'Add an Anthropic API key in Settings, or install Ollama for free local AI.';
    }
  }

  document.getElementById('anthropic-status').className =
    'status-dot' + (d.secrets.anthropic_key.configured ? '' : ' off');
  document.getElementById('smtp-status').className =
    'status-dot' + (d.secrets.smtp_password.configured ? '' : ' off');
}

async function toggleWatcher() {
  const ep = watcherRunning ? '/api/watcher/stop' : '/api/watcher/start';
  await fetch(ep, { method: 'POST' });
  await fetchSystem();
}

// ── Date navigation ────────────────────────────────────────────────────────
function renderDateBar() {
  const bar = document.getElementById('datebar');
  if (!todayISO) { bar.innerHTML = ''; return; }
  const yesterday = new Date(todayISO + 'T12:00:00');
  yesterday.setDate(yesterday.getDate() - 1);
  const yesterdayISO = yesterday.toISOString().slice(0,10);

  const chip = (iso, label) => {
    const active = (iso === activeDate());
    return `<button class="date-chip ${active?'active':''}" onclick="selectDate('${iso}')">${label}</button>`;
  };
  bar.innerHTML =
    chip(todayISO, 'Today') +
    chip(yesterdayISO, 'Yesterday') +
    `<span class="date-chip ${currentDate && currentDate !== todayISO && currentDate !== yesterdayISO ? 'active' : ''}">
       <input type="date" max="${todayISO}" value="${currentDate || todayISO}"
              onchange="selectDate(this.value)" />
     </span>`;
}

function selectDate(iso) {
  if (!iso) return;
  currentDate = (iso === todayISO) ? null : iso;
  renderDateBar();
  setHeaderDate();
  refreshDayPanels();
}

async function fetchRealWork() {
  const url = '/api/realwork' + (currentDate ? '?date=' + currentDate : '');
  let d;
  try {
    const r = await fetch(url);
    d = await r.json();
  } catch (e) {
    document.getElementById('hero-headline').textContent = 'Could not load activity data.';
    return;
  }

  const whenLabel = isViewingToday() ? 'today' : 'on ' + relativeDateLabel(d.date).toLowerCase();
  const whenStart = isViewingToday() ? 'You did' : 'On ' + relativeDateLabel(d.date) + ', you did';

  // Empty-state hero
  if (d.no_data || !d.total_active_minutes) {
    document.getElementById('hero-headline').textContent =
      isViewingToday() ? 'Quiet day so far.' : `No tracked activity on ${relativeDateLabel(d.date)}.`;
    document.getElementById('hero-sub').textContent =
      isViewingToday() ? 'Once you start working in a tracked app, this view will fill up.'
                       : 'Either WorkPulse wasn\\'t running, or no work happened.';
    document.getElementById('donut-panel').innerHTML = '<div class="empty">No focused activity.</div>';
    document.getElementById('attn-panel').innerHTML = '<div class="empty">Nothing to review.</div>';
    document.getElementById('apps-panel').innerHTML = '<div class="empty">No apps tracked.</div>';
    document.getElementById('sessions-panel').innerHTML = '<div class="empty">No sessions.</div>';
    renderTimeline([]);
    return;
  }

  // ── Hero ──────────────────────────────────────────────────────────────
  const total = fmtMins(d.total_active_minutes);
  const top = d.by_stream && d.by_stream[0];
  let head = isViewingToday()
    ? `You did <span class="strong">${total}</span> of focused work today`
    : `On <span class="strong">${relativeDateLabel(d.date)}</span> you did <span class="strong">${total}</span> of focused work`;
  if (top) head += `, mostly on <span class="strong">${TITLE_CASE(top.stream)}</span> (${fmtMins(top.minutes)}).`;
  else head += '.';
  document.getElementById('hero-headline').innerHTML = head;

  const projCount = (d.by_stream || []).length;
  const subBits = [];
  if (projCount) subBits.push(`${projCount} project${projCount===1?'':'s'} active`);
  if (d.untagged_minutes) subBits.push(`${fmtMins(d.untagged_minutes)} uncategorized`);
  else subBits.push('fully tagged');
  document.getElementById('hero-sub').textContent = subBits.join(' · ');

  // ── Donut + legend ────────────────────────────────────────────────────
  const streams = d.by_stream || [];
  let cum = 0;
  const slices = streams.length
    ? streams.map(s => {
        const a = cum, b = cum + s.pct; cum = b;
        return `${s.color} ${a}% ${b}%`;
      }).join(', ')
    : 'var(--border) 0% 100%';
  const legend = streams.length
    ? streams.map(s => `
        <div class="lg-row">
          <span class="lg-dot" style="background:${s.color}"></span>
          <span class="lg-name">${escapeHtml(s.stream.replace(/-/g,' '))}</span>
          <span class="lg-min">${fmtMins(s.minutes)}</span>
          <span class="lg-pct">${s.pct}%</span>
        </div>`).join('')
    : '<div class="empty">No tagged streams yet.</div>';
  document.getElementById('donut-panel').innerHTML = `
    <div class="donut-wrap">
      <div class="donut" style="background: conic-gradient(${slices})">
        <div class="donut-c">
          <div class="num">${total}</div>
          <div class="lbl">focused</div>
        </div>
      </div>
      <div class="legend">${legend}</div>
    </div>`;

  // ── Needs your attention (with Loop A tag-as dropdown) ───────────────
  const attn = (d.untagged_windows || []).filter(w => w.minutes >= 1).slice(0, 10);
  if (attn.length === 0) {
    const msg = aiEnabled
      ? 'Nothing untagged worth reviewing. Auto-tagging is doing its job.'
      : 'Nothing untagged yet. As windows show up here, tag them once and WorkPulse remembers.';
    document.getElementById('attn-panel').innerHTML = `<div class="empty">${msg}</div>`;
  } else {
    document.getElementById('attn-panel').innerHTML = attn.map((w, i) => `
        <div class="attn-row">
          <div class="attn-title" title="${escapeHtml(w.title)}">${escapeHtml(w.title)}</div>
          <div class="attn-min">${fmtMins(w.minutes)}</div>
          <div class="attn-tag">
            <button class="tag-btn" onclick="toggleTagMenu(${i})">Tag as ▾</button>
            <div class="tag-menu" id="tag-menu-${i}">
              ${availableStreams.map(s => `
                <div class="tag-opt" onclick="learn('${escapeHtml(w.title)}', '${s.key}')">
                  <span class="lg-dot" style="background:${s.color}"></span>
                  <span>${escapeHtml(s.label)}</span>
                </div>`).join('')}
              <div class="tag-opt ignore" onclick="learn('${escapeHtml(w.title)}', null)">
                Never tag this (ignore)
              </div>
            </div>
          </div>
        </div>`).join('');
  }

  // ── Apps used (compact) ───────────────────────────────────────────────
  const apps = (d.by_app || []).slice(0, 8);
  if (apps.length === 0) {
    document.getElementById('apps-panel').innerHTML = '<div class="empty">No apps tracked yet.</div>';
  } else {
    const maxMin = apps[0].minutes || 1;
    document.getElementById('apps-panel').innerHTML = apps.map(a => `
      <div class="app-row">
        <div class="app-name">${escapeHtml(a.app)}</div>
        <div class="app-track"><div class="app-fill" style="width:${Math.round(a.minutes/maxMin*100)}%"></div></div>
        <div class="app-min">${fmtMins(a.minutes)}</div>
      </div>`).join('');
  }

  // ── Hidden detail: every window visit ─────────────────────────────────
  const sessions = d.recent_sessions || [];
  if (sessions.length === 0) {
    document.getElementById('sessions-panel').innerHTML = '<div class="empty">No sessions yet.</div>';
  } else {
    const rows = sessions.map(s => {
      const dur = s.duration_s < 60 ? `${Math.round(s.duration_s)}s` : `${s.duration_min}m`;
      const chip = s.stream
        ? `<span class="chip" style="background:${s.color}">${escapeHtml(s.stream.replace(/-/g,' '))}</span>`
        : '<span class="chip muted">untagged</span>';
      return `<tr>
        <td class="mono">${s.start}</td>
        <td class="mono">${dur}</td>
        <td>${chip}</td>
        <td class="mono">${escapeHtml(s.app)}</td>
        <td class="title-cell" title="${escapeHtml(s.title)}">${escapeHtml(s.title)}</td>
      </tr>`;
    }).join('');
    document.getElementById('sessions-panel').innerHTML = `
      <table class="session-table">
        <thead><tr><th>Start</th><th>Duration</th><th>Project</th><th>App</th><th>Window</th></tr></thead>
        <tbody>${rows}</tbody>
      </table>`;
  }

  // ── Timeline (24-hour strip) ──────────────────────────────────────────
  renderTimeline(sessions);
}

function renderTimeline(sessions) {
  // Bucket by hour-of-day. Color each hour by its dominant stream.
  const buckets = Array.from({length:24}, () => ({total:0, byStream:{}, color:null}));
  sessions.forEach(s => {
    if (!s.start || !s.duration_s) return;
    const hh = parseInt(s.start.slice(0,2), 10);
    if (isNaN(hh) || hh < 0 || hh > 23) return;
    const m = s.duration_s / 60;
    buckets[hh].total += m;
    if (s.stream) {
      const cur = buckets[hh].byStream[s.stream] || {min:0, color:s.color};
      cur.min += m;
      buckets[hh].byStream[s.stream] = cur;
    }
  });
  buckets.forEach(b => {
    const top = Object.values(b.byStream).sort((a,b)=>b.min-a.min)[0];
    b.color = top ? top.color : '#c8c5bb';
  });
  const maxMin = Math.max(1, ...buckets.map(b => b.total));
  const tl = document.getElementById('timeline');
  const ax = document.getElementById('tl-axis');
  tl.innerHTML = ''; ax.innerHTML = '';
  buckets.forEach((b, h) => {
    const hStr = h.toString().padStart(2,'0');
    if (b.total > 0) {
      const heightPct = Math.max(8, Math.round(b.total / maxMin * 100));
      const tip = `${hStr}:00 · ${fmtMins(b.total)}`;
      tl.insertAdjacentHTML('beforeend',
        `<div class="tl-h" title="${tip}"><div class="tl-bar" style="background:${b.color};height:${heightPct}%"></div></div>`);
    } else {
      tl.insertAdjacentHTML('beforeend',
        `<div class="tl-h" title="${hStr}:00"><div class="tl-empty"></div></div>`);
    }
    ax.insertAdjacentHTML('beforeend',
      `<div class="tl-tick">${h % 3 === 0 ? hStr : ''}</div>`);
  });
}

async function fetchLastActive() {
  const url = '/api/focus' + (currentDate ? '?date=' + currentDate : '');
  let d;
  try {
    const r = await fetch(url);
    d = await r.json();
  } catch (e) {
    document.getElementById('last-active').innerHTML = '<div class="empty">Unavailable.</div>';
    return;
  }
  const list = (d.last_touched || []).filter(s => s.last_ago);
  const emptyMsg = isViewingToday()
    ? 'No project files touched yet today.'
    : 'No file activity on this day.';
  document.getElementById('last-active').innerHTML = list.length === 0
    ? `<div class="empty">${emptyMsg}</div>`
    : list.map(s => `
        <div class="la-row">
          <span class="lg-dot" style="background:${s.color}"></span>
          <span class="la-name">${escapeHtml(s.label)}</span>
          <span class="la-ago">${escapeHtml(s.last_ago)}</span>
        </div>`).join('');
}

async function fetchAI() {
  const url = '/api/ai' + (currentDate ? '?date=' + currentDate : '');
  let d;
  try {
    const r = await fetch(url);
    d = await r.json();
  } catch (e) {
    document.getElementById('ai-panel').innerHTML = '<div class="empty">Unavailable.</div>';
    return;
  }
  if (!d.count) {
    document.getElementById('ai-panel').innerHTML =
      '<div class="empty">No AI sessions logged for this day.</div>';
    return;
  }
  document.getElementById('ai-panel').innerHTML = `
    <div class="ai-strip">
      <div class="ai-stat"><div class="num">${d.count}</div><div class="lbl">sessions</div></div>
      <div class="ai-stat"><div class="num">$${d.total_cost.toFixed(2)}</div><div class="lbl">spent</div></div>
      <div class="ai-stat"><div class="num">${(d.total_tokens/1000).toFixed(1)}k</div><div class="lbl">tokens</div></div>
    </div>`;
}

// ── Jobs in flight (v1.1a Coach surface) ───────────────────────────────────

function fmtRelative(iso) {
  if (!iso) return '';
  try {
    const t = new Date(iso);
    const diffMin = Math.floor((Date.now() - t.getTime()) / 60000);
    if (diffMin < 2) return 'just now';
    if (diffMin < 60) return `${diffMin} min ago`;
    const diffH = Math.floor(diffMin / 60);
    if (diffH < 24) return `${diffH}h ago`;
    const diffD = Math.floor(diffH / 24);
    if (diffD === 1) return 'yesterday';
    if (diffD < 7) return `${diffD} days ago`;
    return t.toLocaleDateString(undefined, {month:'short', day:'numeric'});
  } catch (e) { return ''; }
}

function isJobPaused(job) {
  if (!job.last_active) return job.session_count === 0 ? false : true;
  const diffMs = Date.now() - new Date(job.last_active).getTime();
  return diffMs > 30 * 60 * 1000;   // >30 min since last session = paused
}

async function fetchJobs() {
  let d;
  try {
    const r = await fetch('/api/jobs/active');
    d = await r.json();
  } catch (e) {
    document.getElementById('jobs-panel').innerHTML =
      '<div class="empty">Could not load jobs.</div>';
    return;
  }
  const jobs = d.jobs || [];
  if (jobs.length === 0) {
    document.getElementById('jobs-panel').innerHTML =
      '<div class="empty">No jobs in flight. Hit "+ Start a job" to track a specific piece of work.</div>';
    return;
  }
  document.getElementById('jobs-panel').innerHTML = jobs.map(renderJobCard).join('');
}

function renderJobCard(job) {
  const total = fmtMins(job.total_minutes);
  const started = fmtRelative(job.created_at);
  const sessions = job.session_count || 0;
  const paused = isJobPaused(job);

  // Apps line: top 4
  const appsLine = (job.apps || []).slice(0, 4).map(a =>
    `<span class="app-tag"><strong>${escapeHtml(a.app)}</strong> ${fmtMins(a.minutes)}</span>`
  ).join('');

  // Lift block
  let liftBlock = '';
  if (sessions === 0) {
    liftBlock = `<div class="job-lift">
      <div class="job-lift-head">Where AI could lift this job</div>
      <div class="job-lift-empty">No activity logged inside this job yet. Once you start working, AI-lift opportunities appear here.</div>
    </div>`;
  } else if ((job.lift || []).length === 0) {
    liftBlock = `<div class="job-lift">
      <div class="job-lift-head">Where AI could lift this job</div>
      <div class="job-lift-empty">Nothing stands out yet. Lift opportunities surface as the session data builds up.</div>
    </div>`;
  } else {
    liftBlock = `<div class="job-lift">
      <div class="job-lift-head">Where AI could lift this job</div>
      ${job.lift.map(l => `
        <div class="lift-item">
          <div class="lift-title">${escapeHtml(l.title)}</div>
          <div class="lift-suggest">${escapeHtml(l.suggestion)}</div>
        </div>`).join('')}
    </div>`;
  }

  return `
    <div class="job-card" data-job="${escapeHtml(job.id)}">
      <div class="job-head">
        <div class="job-name">${escapeHtml(job.name)}</div>
        <span class="chip" style="background:${job.stream_color}">${escapeHtml((job.stream||'').replace(/-/g,' '))}</span>
        <div class="job-actions">
          <button class="job-end-btn" onclick="endJob('${escapeHtml(job.id)}', '${escapeHtml(job.name)}')">End job</button>
        </div>
      </div>
      <div class="job-meta">
        ${total} total · started ${started} · ${sessions} session${sessions===1?'':'s'}
        ${paused ? `<span class="job-paused">paused — last touch ${fmtRelative(job.last_active)}</span>` : ''}
      </div>
      ${sessions > 0 ? `<div class="job-apps">${appsLine || '<span style="color:var(--text-faint)">(no app data yet)</span>'}</div>` : ''}
      ${liftBlock}
    </div>
  `;
}

function openStartJob() {
  // Populate stream dropdown from systemSnapshot (already fetched)
  const sel = document.getElementById('sj-stream');
  const streams = (systemSnapshot && systemSnapshot.streams) || availableStreams || [];
  sel.innerHTML = streams.map(s =>
    `<option value="${escapeHtml(s.key)}">${escapeHtml(s.label)}</option>`
  ).join('');
  document.getElementById('sj-name').value = '';
  document.getElementById('sj-note').value = '';
  document.getElementById('sj-modal').classList.add('open');
  setTimeout(() => document.getElementById('sj-name').focus(), 60);
}

function closeStartJob() {
  document.getElementById('sj-modal').classList.remove('open');
}

async function submitStartJob() {
  const name = document.getElementById('sj-name').value.trim();
  const stream = document.getElementById('sj-stream').value;
  const note = document.getElementById('sj-note').value.trim();
  if (!name) { showToast('Job needs a name.'); return; }
  try {
    const r = await fetch('/api/jobs/start', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ name, stream, note })
    });
    const d = await r.json();
    if (r.ok) {
      showToast(`Started: ${name}`);
      closeStartJob();
      await fetchJobs();
    } else {
      showToast('Error: ' + (d.error || 'could not start job'));
    }
  } catch (e) {
    showToast('Error: ' + e.message);
  }
}

async function endJob(id, name) {
  if (!confirm(`End job "${name}"?\\n\\nSessions stop rolling up to it. The job's history is preserved.`)) return;
  try {
    const r = await fetch(`/api/jobs/${id}/end`, { method: 'POST' });
    const d = await r.json();
    if (r.ok) {
      showToast(`Ended: ${name}`);
      await fetchJobs();
    } else {
      showToast('Error: ' + (d.error || 'could not end job'));
    }
  } catch (e) {
    showToast('Error: ' + e.message);
  }
}

// ── Weekly heatmap (last 14 days) ──────────────────────────────────────────
async function fetchHeatmap() {
  let d;
  try {
    const r = await fetch('/api/calendar?days=14');
    d = await r.json();
  } catch (e) { return; }
  const days = d.days || [];
  if (days.length === 0) {
    document.getElementById('heatmap').innerHTML = '<div class="empty">No data yet.</div>';
    return;
  }
  const maxMin = Math.max(1, ...days.map(x => x.total_minutes));
  const activeISO = activeDate();
  document.getElementById('heatmap').innerHTML = days.map(day => {
    const top = day.by_stream && day.by_stream[0];
    const color = top ? top.color : 'var(--border)';
    const heightPct = day.total_minutes > 0
      ? Math.max(6, Math.round(day.total_minutes / maxMin * 100)) : 0;
    const dayLabel = (new Date(day.date + 'T12:00:00'))
      .toLocaleDateString(undefined, {day:'numeric'});
    const cls = 'hm-col' + (day.total_minutes === 0 ? ' empty' : '')
              + (day.date === activeISO ? ' selected' : '');
    const tip = `${day.weekday} ${day.date} — ${fmtMins(day.total_minutes)}`;
    return `<div class="${cls}" title="${tip}" onclick="selectDate('${day.date}')">
              <div class="hm-bar-wrap">
                <div class="hm-bar" style="background:${color};height:${heightPct}%"></div>
              </div>
              <div class="hm-day">${dayLabel}</div>
            </div>`;
  }).join('');
}

// ── Loop A: tag-as dropdown ────────────────────────────────────────────────
function toggleTagMenu(i) {
  // close all others first
  document.querySelectorAll('.tag-menu.open').forEach(m => {
    if (m.id !== 'tag-menu-' + i) m.classList.remove('open');
  });
  const m = document.getElementById('tag-menu-' + i);
  if (m) m.classList.toggle('open');
}

document.addEventListener('click', e => {
  if (!e.target.closest('.attn-tag')) {
    document.querySelectorAll('.tag-menu.open').forEach(m => m.classList.remove('open'));
  }
});

async function learn(rawTitle, streamKey) {
  try {
    const r = await fetch('/api/learn', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ raw_title: rawTitle, stream: streamKey })
    });
    const d = await r.json();
    if (d.ok) {
      showToast(streamKey
        ? `Tagged as ${streamKey}. WorkPulse will remember.`
        : `Will ignore this window in future.`);
      await fetchRealWork();
    } else {
      showToast('Error: ' + (d.error || 'could not save rule'));
    }
  } catch (e) {
    showToast('Error: ' + e.message);
  }
}

// ── Settings modal ─────────────────────────────────────────────────────────
function openSettings() {
  document.getElementById('settings-modal').classList.add('open');
  fetch('/api/system').then(r => r.json()).then(d => {
    const cfg = d || {};
    // Backend summary block
    const llm = cfg.llm || {};
    const ol  = llm.ollama || {};
    const anth = (llm.anthropic || {});
    let lines = [];
    const active = cfg.ai_backend || 'none';
    const activeLabel = active === 'anthropic' ? 'Cloud (Anthropic Claude)'
                      : active === 'ollama'    ? `Local (Ollama, ${ol.want_model || ''})`
                      :                          'OFF — no backend configured';
    lines.push(`<div><strong>Active:</strong> ${activeLabel}</div>`);
    lines.push(`<div style="margin-top:6px"><strong>Anthropic:</strong> ${anth.available ? 'key configured' : 'not configured'}</div>`);
    if (ol.installed) {
      const m = (ol.models || []).length;
      lines.push(`<div><strong>Ollama:</strong> running, ${m} model${m===1?'':'s'} pulled${ol.model_ready ? ' (incl. '+ol.want_model+')' : ' (need '+ol.want_model+')'}</div>`);
      if (!ol.model_ready) {
        lines.push(`<div style="margin-top:4px;color:var(--text-faint)">Run <code>ollama pull ${ol.want_model}</code> to enable the local backend.</div>`);
      }
    } else {
      lines.push(`<div><strong>Ollama:</strong> not detected. <a href="https://ollama.com/download" target="_blank" rel="noopener" style="color:var(--text-soft)">Install Ollama →</a></div>`);
    }
    document.getElementById('backend-summary').innerHTML = lines.join('');
    document.getElementById('anthropic-status').className =
      'status-dot' + (cfg.secrets.anthropic_key.configured ? '' : ' off');
    document.getElementById('smtp-status').className =
      'status-dot' + (cfg.secrets.smtp_password.configured ? '' : ' off');
    document.getElementById('email-enabled').checked = !!cfg.email_enabled;
  });
}
function closeSettings() {
  document.getElementById('settings-modal').classList.remove('open');
  // Clear inputs so secrets aren't sitting in DOM
  document.getElementById('anthropic-input').value = '';
  document.getElementById('smtp-input').value = '';
}

async function saveSettings() {
  const anth = document.getElementById('anthropic-input').value.trim();
  const smtp = document.getElementById('smtp-input').value.trim();
  const smtpUser = document.getElementById('smtp-user-input').value.trim();
  const emailOn = document.getElementById('email-enabled').checked;

  const ops = [];
  if (anth) ops.push(fetch('/api/settings/secret', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name:'anthropic_key', value: anth })
  }));
  if (smtp) ops.push(fetch('/api/settings/secret', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify({ name:'smtp_password', value: smtp })
  }));
  const emailPayload = { enabled: emailOn };
  if (smtpUser) { emailPayload.smtp_user = smtpUser; emailPayload.from_addr = smtpUser; emailPayload.to_addr = smtpUser; }
  ops.push(fetch('/api/settings/email', {
    method:'POST', headers:{'Content-Type':'application/json'},
    body: JSON.stringify(emailPayload)
  }));

  try {
    const results = await Promise.all(ops);
    const allOk = results.every(r => r.ok);
    if (allOk) {
      showToast('Settings saved.');
      closeSettings();
      await fetchSystem();
    } else {
      const errs = await Promise.all(results.map(r => r.ok ? null : r.json().catch(() => ({error:'unknown'}))));
      const firstErr = errs.find(e => e && e.error);
      showToast('Save failed: ' + (firstErr ? firstErr.error : 'unknown'));
    }
  } catch (e) {
    showToast('Save failed: ' + e.message);
  }
}

// ── Refresh orchestration ──────────────────────────────────────────────────
async function refreshDayPanels() {
  await Promise.all([fetchRealWork(), fetchLastActive(), fetchAI(), fetchJobs()]);
  await fetchHeatmap();   // re-render so selected day highlights
}

async function refresh() {
  await fetchSystem();
  if (todayISO) renderDateBar();
  setHeaderDate();
  await refreshDayPanels();
  document.getElementById('refresh-label').textContent =
    'Updated ' + new Date().toLocaleTimeString();
}

refresh();
setInterval(refresh, 30000);
</script>
</body>
</html>"""


@app.get("/", response_class=HTMLResponse)
def dashboard():
    return DASHBOARD_HTML


# ── entry point ───────────────────────────────────────────────────────────────

def run(host: str = "127.0.0.1", port: int = PORT, start_watcher_on_launch: bool = False):
    if start_watcher_on_launch:
        start_watcher()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
