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
from fastapi import FastAPI, Request, Response
from fastapi.responses import HTMLResponse, JSONResponse

from workpulse.common import load_config, resolve

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

def stream_color(key: str) -> str:
    """Pick a colour for any stream key. Curated entries win; everything else
    gets a deterministic hue derived from a stable hash of the key, so the
    same user-created stream always renders in the same colour across reloads
    and across machines without storing the colour in config."""
    if not key:
        return "#6b7280"
    if key in STREAM_COLORS:
        return STREAM_COLORS[key]
    import hashlib
    h = int(hashlib.sha1(key.encode("utf-8")).hexdigest()[:6], 16)
    hue = h % 360
    # HSL → hex via a fixed-S/L pairing tuned to match the curated palette weight.
    sat, light = 62, 52
    # Convert HSL to RGB then hex (pure math, no deps)
    c = (1 - abs(2 * light / 100 - 1)) * (sat / 100)
    x = c * (1 - abs((hue / 60) % 2 - 1))
    m = light / 100 - c / 2
    if   0   <= hue < 60:  r, g, b = c, x, 0
    elif 60  <= hue < 120: r, g, b = x, c, 0
    elif 120 <= hue < 180: r, g, b = 0, c, x
    elif 180 <= hue < 240: r, g, b = 0, x, c
    elif 240 <= hue < 300: r, g, b = x, 0, c
    else:                  r, g, b = c, 0, x
    rr, gg, bb = int((r + m) * 255), int((g + m) * 255), int((b + m) * 255)
    return f"#{rr:02x}{gg:02x}{bb:02x}"

# ── watcher process management ────────────────────────────────────────────────

_watcher_proc: Optional[subprocess.Popen] = None
_activity_proc: Optional[subprocess.Popen] = None


def _python() -> str:
    return str(Path(sys.executable))


def _watcher_script() -> str:
    return str(Path(__file__).resolve().parent / "watcher.py")


def _proc_is(cmd: str, module: str) -> bool:
    """Match a sensor process whether it was launched as `.../watcher.py`
    (tray Popen fallback) OR `python -m workpulse.signals.watcher` (launchd daemon).
    The old check only matched the .py form, so it never saw the launchd
    daemon and the tray spawned a DUPLICATE. `module` is e.g. 'watcher'."""
    if "status" in cmd:
        return False
    return (f"{module}.py" in cmd) or (f"scripts.{module}" in cmd) \
        or (f"scripts/{module}.py" in cmd)


def is_watcher_running() -> bool:
    global _watcher_proc
    if _watcher_proc and _watcher_proc.poll() is None:
        return True
    for proc in psutil.process_iter(["pid", "cmdline"]):
        try:
            if _proc_is(" ".join(proc.info["cmdline"] or []), "watcher"):
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
            if _proc_is(" ".join(proc.info["cmdline"] or []), "watcher"):
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
            if _proc_is(" ".join(proc.info["cmdline"] or []), "activity"):
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
            if _proc_is(" ".join(proc.info["cmdline"] or []), "activity"):
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
                "color": stream_color(k),
                "pct": round(v / tagged_total * 100) if tagged_total else 0,
            }
            for k, v in sorted(by_stream.items(), key=lambda x: -x[1])
        ],
    }


# ── v2 brain view (PLAN.md §7 — surfaces the from-first-principles brain) ───

# ── Personal tier: password gate + token-checked privacy filter ─────────────

def _personal_unlocked(request) -> bool:
    """True if the request carries a valid personal-unlock cookie."""
    try:
        from workpulse.core import db as wp_db, personal as wp_personal
        token = request.cookies.get("wp_personal_unlock")
        if not token:
            return False
        con = wp_db.connect(load_config())
        return wp_personal.validate_token(con, token)
    except Exception:
        return False


@app.get("/api/v2/personal/status")
def api_v2_personal_status(request: Request):
    """Tell the dashboard whether a password is set and whether
    the current cookie is unlocked."""
    from workpulse.core import db as wp_db, personal as wp_personal
    con = wp_db.connect(load_config())
    return {
        "password_set": wp_personal.is_password_set(con),
        "unlocked":     _personal_unlocked(request),
    }


@app.post("/api/v2/personal/set-password")
async def api_v2_set_personal_password(payload: dict):
    """First-time setup or password change. Body: {password: str}.
    Rotating the password invalidates any outstanding unlock tokens."""
    from workpulse.core import db as wp_db, personal as wp_personal
    con = wp_db.connect(load_config())
    password = (payload.get("password") or "").strip()
    if not password:
        return JSONResponse({"error": "missing password"}, status_code=400)
    try:
        wp_personal.set_password(con, password)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True}


@app.post("/api/v2/personal/unlock")
async def api_v2_personal_unlock(payload: dict, response: Response):
    """Verify password + issue a 30-minute unlock cookie. Body: {password}."""
    from workpulse.core import db as wp_db, personal as wp_personal
    con = wp_db.connect(load_config())
    password = (payload.get("password") or "")
    if not wp_personal.verify_password(con, password):
        return JSONResponse({"error": "wrong password"}, status_code=401)
    token = wp_personal.create_unlock_token(con, ttl_minutes=30)
    response.set_cookie(
        key="wp_personal_unlock",
        value=token,
        max_age=30 * 60,
        httponly=True,
        samesite="lax",
    )
    return {"ok": True, "expires_in_seconds": 30 * 60}


@app.post("/api/v2/personal/lock")
async def api_v2_personal_lock(response: Response):
    """Immediate lock — drops the cookie."""
    response.delete_cookie("wp_personal_unlock")
    return {"ok": True}


@app.get("/api/v2/personal/data")
def api_v2_personal_data(request: Request):
    """Return the private-tier data the dashboard hides by default.
    Requires a valid unlock cookie."""
    if not _personal_unlocked(request):
        return JSONResponse({"error": "locked"}, status_code=401)
    from workpulse.core import db as wp_db
    con = wp_db.connect(load_config())
    today = datetime.now().date().isoformat()
    # browser visits today, by stream + domain
    rows = con.execute(
        """
        SELECT domain, COUNT(*) AS n
        FROM browser_visit
        WHERE is_private = 1 AND substr(ts, 1, 10) = ?
        GROUP BY domain ORDER BY n DESC
        """,
        (today,),
    ).fetchall()
    return {
        "date":           today,
        "domains_today":  [{"domain": r["domain"], "visits": r["n"]} for r in rows],
    }


@app.post("/api/v2/cluster/{cluster_id}/correct")
async def api_v2_correct_cluster(cluster_id: str, payload: dict):
    """Teach-correct: user picks the right project for a cluster.
    Body: {to_stream: str}. Writes cluster_correction + flips
    cluster_assignment to source='user'."""
    to_stream = (payload.get("to_stream") or "").strip()
    if not to_stream:
        return JSONResponse({"error": "missing to_stream"}, status_code=400)
    from workpulse.core import categorize as cat, db as wp_db
    cfg = load_config()
    con = wp_db.connect(cfg)
    try:
        res = cat.correct_assignment(con, cluster_id, to_stream)
        return {"ok": True, **res}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/v2/streams")
def api_v2_streams():
    """All streams — for the correction dropdown. Includes label so the
    UI can show 'Uganda MEMD' instead of just 'uganda'."""
    from workpulse.core import db as wp_db
    cfg = load_config()
    con = wp_db.connect(cfg)
    rows = con.execute(
        "SELECT key, label FROM stream ORDER BY label"
    ).fetchall()
    return {"streams": [{"key": r["key"], "label": r["label"]} for r in rows]}


@app.post("/api/v2/capture")
async def api_v2_capture(payload: dict):
    """One-shot capture from the dashboard text box.
    Body: {body: str, pin?: 'auto'|'session:<id>'|null}
    Auto-pin defaults to the latest session — same semantics as
    `python -m workpulse.core.capture --auto-pin`."""
    body = (payload.get("body") or "").strip()
    if not body:
        return JSONResponse({"error": "empty body"}, status_code=400)
    pin = payload.get("pin", "auto")
    pinned_kind = None
    pinned_id = None
    from workpulse.core import capture as wp_cap, db as wp_db
    cfg = load_config()
    con = wp_db.connect(cfg)
    if pin == "auto":
        sid = wp_cap._current_session_id(con)
        if sid:
            pinned_kind, pinned_id = "session", sid
    elif pin and pin.startswith("session:"):
        pinned_kind, pinned_id = "session", pin.split(":", 1)[1]
    try:
        cid = wp_cap.capture(body=body, author="human",
                             pinned_kind=pinned_kind,
                             pinned_id=pinned_id, cfg=cfg)
        return {"ok": True, "id": cid,
                "pinned_kind": pinned_kind, "pinned_id": pinned_id}
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)


@app.get("/api/v2/health")
def api_v2_health():
    """Latest doctor verdict (written to logs/health.json every 3h). The
    dashboard shows a banner when the verdict isn't OK — so a stale/broken
    tracker is visible the moment you open the page, not days later."""
    import json as _json
    from workpulse.common import ROOT as _ROOT
    p = _ROOT / "logs" / "health.json"
    if not p.exists():
        return {"verdict": "unknown", "summary": "No health check has run yet.",
                "checks": []}
    try:
        return _json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        return {"verdict": "unknown", "summary": "Health file unreadable.",
                "checks": []}


@app.get("/api/v2/profile")
def api_v2_profile():
    """Read the current brain/george-profile.md (Step A — the living profile).
    Returns the raw markdown + parsed frontmatter so the dashboard can
    render it. Empty payload if no profile has been generated yet."""
    from workpulse.core.about_george import _PROFILE_PATH
    if not _PROFILE_PATH.exists():
        return {"exists": False, "raw": "",
                "frontmatter": {}, "body": ""}
    text = _PROFILE_PATH.read_text(encoding="utf-8")
    fm = {}
    body = text
    if text.startswith("---"):
        try:
            end = text.index("\n---\n", 4)
            fm_raw = text[4:end].strip()
            import yaml as _yaml
            fm = _yaml.safe_load(fm_raw) or {}
            body = text[end + 5:].lstrip()
        except (ValueError, Exception):
            pass
    return {"exists": True, "raw": text, "frontmatter": fm, "body": body}


@app.get("/api/v2/today")
def api_v2_today(request: Request, date: Optional[str] = None):  # noqa: A002
    """The brain's view of today, read straight from the v2 SQLite atom store.

    Same evidence shape that workpulse.core.report.daily() and consolidate.findings()
    use — structured so the dashboard can render sections (time breakdown,
    top named clusters, captures, plan-vs-actual) without an LLM call.
    Unlike /api/realwork (which reads contaminated JSONL), this reads the
    cleaned, FK-tied SQLite — lock-screen sessions are gone, clusters are
    materialized, names from skills/name-cluster.md are joined in.

    Privacy: streams marked is_private=1 are EXCLUDED from the response by
    default. The dashboard's Personal card surfaces them separately behind
    the password gate. If the request carries a valid unlock cookie,
    private streams are included.
    """
    from datetime import date as _date_cls
    from workpulse.core import db as wp_db, report as wp_report
    from workpulse.core import cluster_context as wp_ctx
    cfg = load_config()
    show_private = _personal_unlocked(request)
    try:
        on = (_date_cls.fromisoformat(date) if date
              else datetime.now().date())
    except (ValueError, TypeError):
        return JSONResponse({"error": "bad date"}, status_code=400)
    con = wp_db.connect(cfg)
    ev = wp_report._evidence(con, "daily", start=on, end=on, cfg=cfg)
    # Build a set of private stream keys to filter against. `ev` shape is
    # {time_breakdown: {by_stream, total_hours}, top_clusters: [...], ...}.
    private_keys = {r["key"] for r in con.execute(
        "SELECT key FROM stream WHERE is_private = 1"
    )} if not show_private else set()
    if private_keys:
        tb = ev.get("time_breakdown") or {}
        tb["by_stream"] = [s for s in (tb.get("by_stream") or [])
                            if s.get("stream") not in private_keys]
        tb["total_hours"] = round(sum(s["hours"] for s in tb["by_stream"]), 1)
        ev["time_breakdown"] = tb
        ev["top_clusters"] = [c for c in (ev.get("top_clusters") or [])
                              if c.get("stream") not in private_keys]
    enriched_clusters = []
    for c in ev["top_clusters"]:
        ctx = wp_ctx.cluster_context(con, cluster_id=c["cluster_id"], cfg=cfg)
        # Fix 1.7: read the Categorizer's assignment for this cluster.
        # If no assignment row exists, fall back to the cluster's old
        # session.stream (job_view.stream).
        assignment = con.execute(
            "SELECT stream AS assigned_stream, confidence, source "
            "FROM cluster_assignment WHERE cluster_id = ?",
            (c["cluster_id"],),
        ).fetchone()
        if assignment:
            assigned_stream = assignment["assigned_stream"]
            assignment_confidence = assignment["confidence"]
            assignment_source = assignment["source"]
        else:
            assigned_stream = c["stream"]
            assignment_confidence = None
            assignment_source = "unassigned"
        # Pretty label for the assigned stream
        label_row = con.execute(
            "SELECT label FROM stream WHERE key = ?", (assigned_stream,),
        ).fetchone() if assigned_stream else None
        assigned_label = label_row["label"] if label_row else (assigned_stream or "")
        enriched_clusters.append({
            "cluster_id": c["cluster_id"],
            "name":       c.get("name") or None,
            "name_source": c.get("name_source") or None,
            "one_liner":  c.get("one_liner") or None,
            "stream":     c["stream"],                # raw session-stream
            "assigned_stream":     assigned_stream,   # Categorizer's pick
            "assigned_label":      assigned_label,
            "assignment_confidence": assignment_confidence,
            "assignment_source":   assignment_source, # 'user' | 'agent' | 'fallback' | 'unassigned'
            "hours":      c["hours"],
            "summary":    ctx["summary"],
            "git_commits_count": len(ctx["git_commits"]),
            "captures_count":    len(ctx["captures"]),
            "skill_runs_count":  sum(s["count"] for s in ctx["skill_runs"]),
            "file_events_count": ctx["file_events"]["total"],
            # Surface the top 3 commit subjects so the user can see them
            "top_commits": [
                {"sha": g["sha"], "subject": g["subject"], "ts": g["ts"]}
                for g in ctx["git_commits"][:3]
            ],
        })
    return {
        "date":           on.isoformat(),
        "total_hours":    ev["time_breakdown"]["total_hours"],
        "by_stream":      ev["time_breakdown"]["by_stream"],
        "top_clusters":   enriched_clusters,
        "captures":       [
            {
                "ts":          c["ts"],
                "time":        c["ts"][11:16] if len(c["ts"]) >= 16 else "",
                "author":      c["author"],
                "body":        c["body"],
                "pinned_kind": c.get("pinned_kind"),
                "pinned_id":   c.get("pinned_id"),
            }
            for c in ev["captures"]
        ],
        "plan_vs_actual": ev["findings"]["plan_vs_actual"],
        "untagged_buckets": ev["findings"]["untagged_buckets"][:3],
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
    from workpulse.signals.activity import _tag_stream
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
            "color": stream_color(k),
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
            "color": stream_color(s.get("stream") or ""),
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
            "color": stream_color(r.get("stream") or ""),
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
                "color": stream_color(k),
            }
            for k, v in sorted(by_stream.items(), key=lambda x: -x[1]["count"])
        ],
        "recent": [
            {
                "time": s["timestamp"][11:16],
                "stream": s.get("stream"),
                "color": stream_color(s.get("stream") or ""),
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
    from scripts.tree import labels as _stream_labels
    streams = _stream_labels(cfg)

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
            "color": stream_color(stream),
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
            from workpulse.signals.activity import _tag_stream
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
                 "color": stream_color(k)}
                for k, v in sorted(by_stream.items(), key=lambda x: -x[1])
            ],
        })
    return {"days": out}


# ── system / status (for dashboard chrome) ───────────────────────────────────

@app.get("/api/system")
def api_system():
    """What the dashboard needs to render its chrome: secret status,
    watcher status, identity, available streams, today's date."""
    from workpulse.wp_secrets import status as secret_status, has as has_secret
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
        # Streams normalised into the hierarchical shape — every entry carries
        # parent + breadcrumb so the UI can render a tree without re-fetching.
        # Back-compat: flat string entries in config.yaml degrade to top-level
        # nodes (parent=None, breadcrumb=label). See scripts/tree.py.
        "streams":            _streams_payload(cfg),
        # Trivial-tree signal — the dashboard uses this to auto-open the
        # taxonomy wizard on first load. "Trivial" = only the legacy 'misc'
        # placeholder, or fewer than 2 nodes, or no parent relationships.
        # Once the user builds anything resembling a real tree, the wizard
        # stops auto-opening (they can still open it from Settings).
        "taxonomy_trivial":   _taxonomy_is_trivial(cfg),
        "today":              date.today().isoformat(),
    }


def _taxonomy_is_trivial(cfg: dict) -> bool:
    from scripts.tree import normalise
    tree = normalise(cfg)
    if len(tree) < 2:
        return True
    # If nothing has a parent, the user hasn't engaged with the hierarchy.
    if not any(r.get("parent") for r in tree.values()):
        # Two top-level streams without parents could still be intentional;
        # only treat as trivial if one of them is the legacy 'misc' default.
        return "misc" in tree
    return False


def _streams_payload(cfg: dict) -> list[dict]:
    from scripts.tree import normalise, breadcrumb, ancestors
    tree = normalise(cfg)
    return [
        {
            "key":        k,
            "label":      rec["label"],
            "parent":     rec.get("parent"),
            "color":      stream_color(k),
            "ancestors":  ancestors(k, cfg),
            "breadcrumb": breadcrumb(k, cfg),
        }
        for k, rec in tree.items()
    ]


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
    from workpulse.wp_secrets import set_ as set_secret
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
    from workpulse.wp_secrets import has as has_secret
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
    """Active jobs with their session rollup + AI-lift opportunities +
    cross-Job remembrance (top 3 prior Jobs with semantic overlap).
    Runs auto-close first so the response is consistent with what the user
    would see if they refreshed twice in a row. Drives the 'Jobs in flight'
    card on the dashboard."""
    from scripts.jobs import list_active, rollup, autoclose_stale_jobs
    from scripts.remembrance import remembrance_for
    auto_closed = autoclose_stale_jobs()   # idempotent; safe on every refresh
    out = []
    for rec in list_active():
        r = rollup(rec["id"])
        if r is None:
            continue
        r["stream_color"] = stream_color(r.get("stream") or "")
        # Cross-Job remembrance — deterministic, local, no LLM in v1.
        try:
            r["remembrance"] = remembrance_for(rec["id"], limit=3)
        except Exception:
            r["remembrance"] = []
        out.append(r)
    return {"jobs": out, "auto_closed_count": len(auto_closed)}


@app.get("/api/jobs/{job_id}/remembrance")
def api_job_remembrance(job_id: str, limit: int = 3):
    """Standalone endpoint for cross-Job remembrance. Useful for the
    per-Job detail view and for diagnostic / debugging callers."""
    from scripts.remembrance import remembrance_for
    try:
        out = remembrance_for(job_id, limit=max(1, min(int(limit), 10)))
    except Exception as e:
        return JSONResponse({"error": str(e)}, status_code=500)
    return {"job_id": job_id, "matches": out}


@app.get("/api/jobs/recent")
def api_jobs_recent(days: int = 14, only_ended: bool = False):
    """Jobs from the last N days, newest first. Light payload — no session
    rollups, no lift detection. Used for the 'Recently ended' mini-list on
    the dashboard, the Resume affordance, and the export picker."""
    from scripts.jobs import list_all
    days = max(1, min(int(days), 365))
    out = []
    for rec in list_all(days_back=days):
        if only_ended and not rec.get("ended_at"):
            continue
        out.append({
            **rec,
            "stream_color": stream_color(rec.get("stream") or ""),
        })
    return {"jobs": out, "days": days}


@app.get("/api/jobs/{job_id}/export")
def api_job_export(job_id: str, include_titles: bool = True):
    """Markdown digest of a Job — for sharing, archiving, or pasting into a
    timesheet. Returns text/markdown with Content-Disposition: attachment so
    browsers offer to save it; the dashboard also previews it inline."""
    from scripts.jobs import export_job_markdown, get_job
    rec = get_job(job_id)
    if rec is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    md = export_job_markdown(job_id, include_titles=bool(include_titles))
    if md is None:
        return JSONResponse({"error": "could not build export"}, status_code=500)
    # Safe filename from the job name
    safe = re.sub(r"[^A-Za-z0-9._-]+", "_", rec["name"]).strip("_") or "job"
    filename = f"{safe}-{job_id}.md"
    from fastapi.responses import PlainTextResponse
    return PlainTextResponse(
        md,
        media_type="text/markdown; charset=utf-8",
        headers={"Content-Disposition": f'attachment; filename="{filename}"'},
    )


@app.post("/api/jobs/{job_id}/resume")
async def api_job_resume(job_id: str):
    """Re-open an ended job as a new active job (same name + stream).
    Used by the 'Resume' button on recently-ended cards."""
    from scripts.jobs import resume_job
    try:
        rec = resume_job(job_id)
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    return {"ok": True, **rec}


# ── v1.2b: LLM-inferred job-name suggestions (Loop B for Jobs) ───────────────

@app.get("/api/jobs/suggestions")
def api_job_suggestions():
    """Pending job-name suggestions per stream. Cached by activity fingerprint;
    only invokes the LLM when activity has materially changed."""
    from scripts.job_suggester import compute_suggestions
    try:
        out = compute_suggestions()
    except Exception as e:
        return JSONResponse({"suggestions": [], "error": str(e)[:200]}, status_code=200)
    # Stamp each with the stream color for the dashboard
    for s in out:
        s["stream_color"] = stream_color(s.get("stream") or "")
    return {"suggestions": out}


@app.post("/api/jobs/suggestions/accept")
async def api_job_suggestion_accept(payload: dict):
    """Body: {stream, name}. Starts the job and records the acceptance.
    The suggestion is naturally suppressed afterwards because there's now
    an active job in the stream."""
    from scripts.job_suggester import accept_suggestion
    name = (payload.get("name") or "").strip()
    stream = (payload.get("stream") or "").strip()
    if not name or not stream:
        return JSONResponse({"error": "name and stream required"}, status_code=400)
    try:
        rec = accept_suggestion(stream, name)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, **rec}


@app.post("/api/jobs/suggestions/dismiss")
async def api_job_suggestion_dismiss(payload: dict):
    """Body: {stream}. Suppresses the same suggestion for 4 hours,
    or until the activity in that stream changes materially."""
    from scripts.job_suggester import dismiss_suggestion
    stream = (payload.get("stream") or "").strip()
    if not stream:
        return JSONResponse({"error": "stream required"}, status_code=400)
    dismiss_suggestion(stream)
    return {"ok": True}


@app.get("/api/jobs/{job_id}")
def api_job_detail(job_id: str):
    """Full detail for one job — rollup + all sessions + lift opportunities."""
    from scripts.jobs import rollup
    r = rollup(job_id, include_sessions=True)
    if r is None:
        return JSONResponse({"error": "not found"}, status_code=404)
    r["stream_color"] = stream_color(r.get("stream") or "")
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


@app.post("/api/jobs/{job_id}/restream")
async def api_job_restream(job_id: str, payload: dict):
    """Body: {stream: str | null}. Move a Job under a different stream.
    Used by the v1.7 taxonomy migration flow. Append-only — preserves the
    Job's original start event and prior sessions; only the effective stream
    going forward (and as folded from the event log) changes."""
    from scripts.jobs import move_job
    new_stream = payload.get("stream")
    if isinstance(new_stream, str):
        new_stream = new_stream.strip() or None
    try:
        rec = move_job(job_id, new_stream)
    except KeyError as e:
        return JSONResponse({"error": str(e)}, status_code=404)
    except ValueError as e:
        return JSONResponse({"error": str(e)}, status_code=400)
    return {"ok": True, **rec}


# ── Streams: add a new stream from the UI without editing YAML ───────────────

@app.post("/api/streams")
async def api_streams_add(payload: dict):
    """Body: {key, label, parent?}. Append a new stream to config.yaml.
      • key must be lowercase letters/digits/hyphens, 2–30 chars, not used.
      • parent (optional) must reference an existing stream — used to build
        the v1.7 stream hierarchy. Omit / null → top-level node.
    On success the stream is written in the hierarchical {label, parent}
    shape so the tree primitive remains the source of truth."""
    import re as _re, yaml as _yaml
    key    = (payload.get("key") or "").strip().lower()
    label  = (payload.get("label") or "").strip()
    parent = payload.get("parent")
    parent = parent.strip().lower() if isinstance(parent, str) and parent.strip() else None

    if not _re.fullmatch(r"[a-z0-9][a-z0-9\-]{1,29}", key):
        return JSONResponse(
            {"error": "key must be lowercase letters/digits/hyphens, 2–30 chars"},
            status_code=400)
    if not label:
        return JSONResponse({"error": "label is required"}, status_code=400)
    if len(label) > 60:
        return JSONResponse({"error": "label too long (60 char max)"}, status_code=400)
    if parent and parent == key:
        return JSONResponse({"error": "stream cannot be its own parent"}, status_code=400)

    cfg_path = resolve("config/config.yaml")
    if not cfg_path.exists():
        return JSONResponse({"error": "config.yaml not found"}, status_code=500)
    cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    streams = cfg.setdefault("streams", {}) or {}
    if not isinstance(streams, dict):
        return JSONResponse({"error": "streams block is malformed in config.yaml"},
                            status_code=500)
    if key in streams:
        return JSONResponse({"error": f"stream '{key}' already exists"}, status_code=409)
    if parent and parent not in streams:
        return JSONResponse({"error": f"parent '{parent}' is not a known stream"},
                            status_code=400)
    # Always write the dict shape — keeps the file uniform once any stream
    # acquires a parent. Existing string-shape entries can stay; the
    # normaliser handles them.
    streams[key] = {"label": label}
    if parent:
        streams[key]["parent"] = parent
    cfg["streams"] = streams
    cfg_path.write_text(_yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
    from scripts.tree import breadcrumb
    return {
        "ok":         True,
        "key":        key,
        "label":      label,
        "parent":     parent,
        "color":      stream_color(key),
        "breadcrumb": breadcrumb(key, cfg),
    }


@app.patch("/api/streams/{key}")
async def api_streams_patch(key: str, payload: dict):
    """Body: {label?, parent?}. Update a stream's display label and/or parent.
    Refuses moves that would create a cycle (key cannot become a descendant
    of itself)."""
    import yaml as _yaml
    cfg_path = resolve("config/config.yaml")
    if not cfg_path.exists():
        return JSONResponse({"error": "config.yaml not found"}, status_code=500)
    cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    streams = cfg.get("streams") or {}
    if not isinstance(streams, dict) or key not in streams:
        return JSONResponse({"error": f"stream '{key}' not found"}, status_code=404)

    # Normalise the entry to dict shape so partial updates compose cleanly.
    cur = streams[key]
    if isinstance(cur, str):
        cur = {"label": cur}
    elif not isinstance(cur, dict):
        cur = {"label": key}

    if "label" in payload:
        lbl = (payload["label"] or "").strip()
        if not lbl:
            return JSONResponse({"error": "label cannot be empty"}, status_code=400)
        if len(lbl) > 60:
            return JSONResponse({"error": "label too long (60 char max)"}, status_code=400)
        cur["label"] = lbl

    if "parent" in payload:
        new_parent = payload.get("parent")
        new_parent = (new_parent or "").strip().lower() or None
        if new_parent == key:
            return JSONResponse({"error": "stream cannot be its own parent"}, status_code=400)
        if new_parent and new_parent not in streams:
            return JSONResponse({"error": f"parent '{new_parent}' is not a known stream"},
                                status_code=400)
        # Cycle guard: walk up from new_parent — if we encounter `key`, abort.
        # We use the in-memory `streams` to traverse, treating strings as
        # parent-less.
        cur_p = new_parent
        seen = set()
        while cur_p:
            if cur_p == key:
                return JSONResponse(
                    {"error": f"cannot make '{key}' a descendant of itself"},
                    status_code=400)
            if cur_p in seen:
                break
            seen.add(cur_p)
            up = streams.get(cur_p)
            cur_p = up.get("parent") if isinstance(up, dict) else None
        if new_parent:
            cur["parent"] = new_parent
        else:
            cur.pop("parent", None)

    streams[key] = cur
    cfg["streams"] = streams
    cfg_path.write_text(_yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
    from scripts.tree import breadcrumb
    return {"ok": True, "key": key, "label": cur.get("label"),
            "parent": cur.get("parent"), "breadcrumb": breadcrumb(key, cfg)}


@app.delete("/api/streams/{key}")
def api_streams_delete(key: str):
    """Remove a stream. Refused if it has children OR any Job attached.
    Caller can move/end Jobs first via the migration UI, then retry."""
    import yaml as _yaml
    cfg_path = resolve("config/config.yaml")
    if not cfg_path.exists():
        return JSONResponse({"error": "config.yaml not found"}, status_code=500)
    cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    streams = cfg.get("streams") or {}
    if key not in streams:
        return JSONResponse({"error": f"stream '{key}' not found"}, status_code=404)

    # Children check (depends on parent links in the on-disk shape).
    kids = [k for k, v in streams.items()
            if isinstance(v, dict) and v.get("parent") == key]
    if kids:
        return JSONResponse(
            {"error": f"stream '{key}' has children: {kids}. Re-parent or delete them first."},
            status_code=400)

    # Job attachment check.
    from scripts.jobs import list_all
    attached = [j["id"] for j in list_all(days_back=3650, cfg=cfg)
                if j.get("stream") == key]
    if attached:
        return JSONResponse(
            {"error": f"stream '{key}' has {len(attached)} job(s) attached. Re-home them first."},
            status_code=400)

    del streams[key]
    cfg["streams"] = streams
    cfg_path.write_text(_yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")
    return {"ok": True, "deleted": key}


# ── Plans API (v1.7 Morning Plan — intent capture) ───────────────────────────

@app.get("/api/plans/today")
def api_plans_today():
    """Today's plan, if one has been written. Items are enriched in-place
    with reconciliation fields (actual_minutes_today / actual_minutes_total)
    so the dashboard can show planned-vs-actual without a second request.
    Returns the empty-state shape when no plan exists yet."""
    from datetime import date as _date
    from scripts.plans import plan_for, reconcile
    today = _date.today()
    p = plan_for(today)
    if p is None:
        return {"exists": False, "date": today.isoformat(),
                "items": [], "created_at": None,
                "planned_minutes": 0, "actual_minutes_today": 0.0}
    rec = reconcile(today, p.get("items") or [])
    return {"exists": True, **p,
            "planned_minutes":      rec["planned_minutes"],
            "actual_minutes_today": rec["actual_minutes_today"]}


@app.get("/api/plans/suggestions")
def api_plans_suggestions():
    """Carry-over candidates for the morning plan: in-flight jobs, jobs touched
    yesterday, and jobs touched in the past week. UI decides what to show."""
    from scripts.plans import suggestions
    return suggestions()


@app.post("/api/plans/today")
async def api_plans_save_today(payload: dict):
    """Body: {items: [{name, planned_minutes?, job_id?, stream?, section?, done?}, ...]}.

    Any item without a job_id gets a fresh Job created on the spot (so the
    rest of the day's activity rolls up under it). Returns the written plan
    with all items now job-linked where possible.
    """
    from datetime import date as _date
    from scripts.plans import materialise_jobs, save_plan_for, plan_for
    items = list(payload.get("items") or [])
    # Normalise + drop empties.
    cleaned: list[dict] = []
    for raw in items:
        if not isinstance(raw, dict):
            continue
        name = (raw.get("name") or "").strip()
        if not name:
            continue
        cleaned.append({
            "name":            name,
            "done":            bool(raw.get("done")),
            "planned_minutes": (int(raw["planned_minutes"])
                                if raw.get("planned_minutes") not in (None, "", 0)
                                else None),
            "job_id":          (raw.get("job_id") or None),
            "stream":          (raw.get("stream") or None),
            "section":         ("carried" if raw.get("section") == "carried"
                                else "new"),
        })
    today = _date.today()
    cleaned = materialise_jobs(today, cleaned)
    save_plan_for(today, cleaned)
    out = plan_for(today) or {}
    from scripts.plans import reconcile
    rec = reconcile(today, out.get("items") or [])
    # `exists: True` mirrors the shape of GET /api/plans/today so the dashboard
    # renderer doesn't fall into its empty-state branch right after a save.
    return {"ok": True, "exists": True, **out,
            "planned_minutes":      rec["planned_minutes"],
            "actual_minutes_today": rec["actual_minutes_today"]}


# ── dashboard HTML ────────────────────────────────────────────────────────────

DASHBOARD_HTML = """<!DOCTYPE html>
<html lang="en">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>WorkPulse</title>
<style>
:root {
  --bg:        #fbf8f1;
  --bg-warm:   #fff6e0;
  --surface:   #ffffff;
  --border:    #eee5d2;
  --border-d:  #ddd0b3;
  --text:      #1c1c1a;
  --text-soft: #6b6b62;
  --text-faint:#a8a8a0;
  --muted:     #6b6b62;
  --shadow:    0 1px 3px rgba(28,28,26,0.04), 0 6px 22px rgba(28,28,26,0.04);
  --shadow-lift: 0 4px 12px rgba(28,28,26,0.07), 0 12px 36px rgba(28,28,26,0.06);
  --radius:    16px;
  --radius-sm: 12px;
  --green:     #22c55e;
  --green-d:   #16a34a;
  --amber-bg:  #fef7e6;
  --amber-br:  #f5d77a;
  --amber-tx:  #6b4a0f;
  --accent:    #d97757;
  --accent-soft:#fbe4d4;
  --bg-soft:   #f5f1e6;
}
* { box-sizing: border-box; margin: 0; padding: 0; }
html { background: var(--bg); }
body {
  background:
    radial-gradient(circle at 0% 0%, #fff1d6 0%, transparent 45%),
    radial-gradient(circle at 100% 8%, #f1ebd8 0%, transparent 35%),
    var(--bg);
  color: var(--text);
  font-family: -apple-system, BlinkMacSystemFont, 'Segoe UI Variable', 'Segoe UI', 'Inter', system-ui, sans-serif;
  font-size: 15px; line-height: 1.55;
  -webkit-font-smoothing: antialiased; -moz-osx-font-smoothing: grayscale;
  max-width: 1320px; margin: 0 auto; padding: 48px 40px 96px;
  min-height: 100vh;
}
@media (max-width: 900px) { body { padding: 32px 20px 80px; } }

/* Master grid for the cards region */
.cards-grid {
  display: grid;
  grid-template-columns: repeat(12, 1fr);
  gap: 18px;
  margin-top: 28px;
}
.span-12 { grid-column: span 12; }
.span-8  { grid-column: span 8; }
.span-6  { grid-column: span 6; }
.span-4  { grid-column: span 4; }
@media (max-width: 1100px) {
  .span-8, .span-6, .span-4 { grid-column: span 12; }
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
.hero { margin-bottom: 28px; display: grid; grid-template-columns: 1fr auto; gap: 32px; align-items: end; }
.hero-text { min-width: 0; }
.hero-greeting { font-size: 13px; font-weight: 600; letter-spacing: 1px; text-transform: uppercase;
  color: var(--accent); margin-bottom: 8px; display: flex; align-items: center; gap: 8px; }
.hero-greeting .sun { font-size: 20px; line-height: 1; }
.hero h1 { font-size: 32px; font-weight: 500; letter-spacing: -0.7px; line-height: 1.25;
  max-width: 780px; margin-bottom: 10px; }
.hero h1 .strong { font-weight: 700; }
.hero .sub { color: var(--text-soft); font-size: 15px; }
.hero-cta { display: flex; flex-direction: column; gap: 8px; align-items: flex-end; }
.btn-hero {
  background: var(--text); color: var(--bg);
  border: none; border-radius: 14px;
  padding: 14px 22px; font-size: 14px; font-weight: 600;
  cursor: pointer; font-family: inherit; letter-spacing: -0.1px;
  display: inline-flex; align-items: center; gap: 10px;
  box-shadow: 0 2px 6px rgba(28,28,26,0.16), 0 8px 24px rgba(28,28,26,0.10);
  transition: transform 0.15s ease, box-shadow 0.15s ease;
}
.btn-hero:hover { transform: translateY(-2px);
  box-shadow: 0 4px 10px rgba(28,28,26,0.2), 0 14px 32px rgba(28,28,26,0.14); }
.btn-hero .ic { font-size: 18px; line-height: 1; }
.btn-hero.subtle { background: var(--surface); color: var(--text); border: 1px solid var(--border); box-shadow: var(--shadow); }
.hero-cta-note { font-size: 12px; color: var(--text-faint); }
@media (max-width: 800px) {
  .hero { grid-template-columns: 1fr; gap: 18px; align-items: start; }
  .hero-cta { align-items: flex-start; }
}

/* Card */
.card { background: var(--surface); border: 1px solid var(--border); border-radius: var(--radius);
  padding: 24px 26px; box-shadow: var(--shadow);
  transition: box-shadow 0.18s ease, transform 0.18s ease; }
.card:hover { box-shadow: var(--shadow-lift); }
.card.interactive:hover { transform: translateY(-2px); }
.card-row { display: grid; grid-template-columns: 1.4fr 1fr; gap: 18px; margin-bottom: 18px; }
.eyebrow { font-size: 11px; font-weight: 600; letter-spacing: 1.6px; text-transform: uppercase;
  color: var(--text-soft); margin-bottom: 22px; }

/* Collapsible cards (UI cleanup) — click header to fold/unfold.
   State is persisted per card_id in localStorage (wp_card_states). */
.card.collapsible { padding-top: 18px; }
.card.collapsible .card-header { cursor: pointer; user-select: none;
  display: flex; align-items: center; gap: 8px;
  margin-bottom: 14px; padding-bottom: 0; }
.card.collapsible .card-header:hover .card-toggle { color: var(--text); }
.card.collapsible .card-toggle { display: inline-block; transition: transform 0.2s ease;
  color: var(--text-faint); font-size: 11px; line-height: 1; flex-shrink: 0; width: 12px; text-align: center; }
.card.collapsible.is-collapsed .card-toggle { transform: rotate(-90deg); }
.card.collapsible.is-collapsed .card-body { display: none; }
.card.collapsible.is-collapsed { padding-bottom: 14px; }
.card.collapsible .card-header-text { flex: 1; display: flex; align-items: baseline;
  justify-content: space-between; gap: 12px; }
.card.collapsible .card-header-meta { color: var(--text-soft); font-weight: 400;
  font-size: 12px; text-transform: none; letter-spacing: 0; }
.card.collapsible.hidden { display: none !important; }
.card.collapsible .card-summary { font-size: 13px; color: var(--text-soft);
  margin-top: 4px; line-height: 1.5; }
.card.collapsible.is-collapsed .card-summary { display: block; }
.card.collapsible:not(.is-collapsed) .card-summary { display: none; }

/* View menu (topbar dropdown) */
.view-menu { position: fixed; min-width: 220px; max-height: 60vh; overflow:auto;
  background: var(--surface); border: 1px solid var(--border); border-radius: 8px;
  box-shadow: 0 8px 24px rgba(0,0,0,0.15); padding: 6px 0; z-index: 9000;
  font-size: 13px; }
.view-menu .view-item { display: flex; align-items: center; gap: 8px;
  padding: 7px 14px; cursor: pointer; }
.view-menu .view-item:hover { background: var(--bg-warm, #fff6e0); }
.view-menu .view-item input[type=checkbox] { margin: 0; }
.view-menu .view-sep { height: 1px; background: var(--border); margin: 4px 0; }
.view-menu .view-action { padding: 7px 14px; cursor: pointer; color: var(--text-soft); font-size: 12px; }
.view-menu .view-action:hover { background: var(--bg-warm, #fff6e0); color: var(--text); }

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

/* Untagged-time alert (v1.7) — surfaces when ≥ALERT_THRESHOLD min go untagged */
.alert-card { border: 1px solid #d4a017; background: #fffaeb;
  border-radius: 10px; padding: 12px 16px; margin: 14px 0; }
.alert-head { display: flex; align-items: center; gap: 8px;
  font-weight: 500; font-size: 14px; color: #7a5b0a; margin-bottom: 8px; }
.alert-icon { font-size: 16px; }
.alert-row { display: flex; align-items: center; gap: 10px; padding: 5px 0;
  border-top: 1px dashed #e8d28a; font-size: 13px; }
.alert-row:first-of-type { border-top: none; }
.alert-row .alert-title { flex: 1; min-width: 0; color: #5a4408;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.alert-row .alert-min { color: #7a5b0a; font-size: 12px; }

/* Today's plan (Morning Plan card — v1.7) */
.plan-eyebrow { display: flex; justify-content: space-between; align-items: center; }
.plan-section-label { font-size: 10px; text-transform: uppercase; letter-spacing: 0.12em;
  color: var(--muted); margin: 18px 0 10px; font-weight: 600; }

/* Plan items as a responsive grid of mini-cards */
.plan-grid { display: grid; grid-template-columns: repeat(auto-fill, minmax(240px, 1fr));
  gap: 12px; }
.plan-item {
  position: relative;
  background: var(--surface);
  border: 1px solid var(--border);
  border-radius: var(--radius-sm);
  padding: 14px 16px 16px;
  display: flex; flex-direction: column; gap: 10px;
  transition: transform 0.18s ease, box-shadow 0.18s ease, border-color 0.18s ease;
  overflow: hidden;
}
.plan-item::before {
  content: ''; position: absolute; left: 0; top: 0; bottom: 0; width: 4px;
  background: var(--stream-color, #6b7280);
  border-radius: var(--radius-sm) 0 0 var(--radius-sm);
}
.plan-item::after {
  content: ''; position: absolute; right: -40px; top: -40px;
  width: 140px; height: 140px; border-radius: 50%;
  background: var(--stream-color, #6b7280);
  opacity: 0.05;
  transition: opacity 0.18s ease;
  pointer-events: none;
}
.plan-item:hover { transform: translateY(-2px);
  box-shadow: 0 4px 12px rgba(28,28,26,0.08); border-color: var(--border-d); }
.plan-item:hover::after { opacity: 0.10; }
.plan-item.done { opacity: 0.55; background: var(--bg-soft); }
.plan-item.done .plan-item-name { text-decoration: line-through; color: var(--muted); }

.plan-item-top { display: flex; align-items: flex-start; gap: 10px; }
.plan-item-icon { font-size: 24px; line-height: 1; flex-shrink: 0;
  filter: drop-shadow(0 1px 2px rgba(0,0,0,0.06)); }
.plan-item-body { flex: 1; min-width: 0; }
.plan-item-name { font-size: 14px; font-weight: 600; line-height: 1.35;
  color: var(--text); word-wrap: break-word; }
.plan-item-stream { font-size: 11px; color: var(--stream-color, var(--muted));
  font-weight: 600; letter-spacing: 0.04em; text-transform: lowercase;
  margin-top: 4px; }
.plan-item-bottom { display: flex; align-items: center; justify-content: space-between;
  gap: 10px; }
.plan-item-time { font-size: 12px; color: var(--muted); font-variant-numeric: tabular-nums; }
.plan-item-time .actual { color: var(--text); font-weight: 600; }
.plan-item-check { width: 18px; height: 18px; accent-color: var(--stream-color, var(--text));
  cursor: pointer; flex-shrink: 0; }

.plan-bar-wrap { height: 4px; background: var(--border);
  border-radius: 2px; overflow: hidden; }
.plan-bar { height: 100%; background: var(--stream-color, var(--text));
  border-radius: 2px; transition: width 0.4s ease; }
.plan-bar-over { background: #c44; }
.plan-sg-block { background: var(--bg-soft, #fafafa); border: 1px solid var(--border);
  border-radius: 8px; padding: 10px 12px; margin: 8px 0 14px;
  max-height: 220px; overflow-y: auto; }
.plan-sg-label { font-size: 11px; text-transform: uppercase; letter-spacing: 0.08em;
  color: var(--muted); margin: 6px 0 4px; }
.plan-sg-row { display: flex; align-items: center; gap: 8px; padding: 4px 0; font-size: 13px; }
.plan-sg-row input[type="checkbox"] { width: 14px; height: 14px; accent-color: var(--text);
  cursor: pointer; }
.plan-sg-row .meta { color: var(--muted); font-size: 12px; margin-left: auto; }
.plan-hint { margin-top: 4px; font-size: 11px; }
.plan-hint code { background: var(--bg-soft, #f3f3f3); padding: 1px 5px; border-radius: 4px;
  font-family: ui-monospace, SFMono-Regular, monospace; font-size: 11px; }

/* Jobs in flight (Coach card) */
.jobs-eyebrow { display: flex; justify-content: space-between; align-items: center; }
.btn-start-job { background: var(--text); color: var(--bg); border: none;
  font-size: 12px; font-weight: 500; padding: 7px 14px; border-radius: 8px;
  cursor: pointer; font-family: inherit; text-transform: none; letter-spacing: 0; }
.btn-start-job:hover { opacity: 0.88; }

.job-card { position: relative; padding: 20px 22px; margin-bottom: 14px;
  background: linear-gradient(135deg,
    color-mix(in srgb, var(--stream-color, #6b7280) 7%, var(--surface)),
    var(--surface));
  border: 1px solid var(--border);
  border-left: 4px solid var(--stream-color, #6b7280);
  border-radius: var(--radius-sm);
  transition: transform 0.18s ease, box-shadow 0.18s ease;
}
.job-card:hover { transform: translateY(-2px); box-shadow: var(--shadow); }
.job-card:last-child { margin-bottom: 4px; }
.job-head { display: flex; align-items: center; gap: 12px; margin-bottom: 8px; }
.job-icon { font-size: 22px; line-height: 1; flex-shrink: 0;
  filter: drop-shadow(0 1px 2px rgba(0,0,0,0.06)); }
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

/* Cross-Job remembrance (v1.7 Coach — Slice A) */
.job-rem { background: #f5f6fb; border-left: 3px solid #6b7280;
  border-radius: 8px; padding: 12px 14px; margin-top: 12px; }
.job-rem-head { font-size: 11px; font-weight: 600; letter-spacing: 0.6px;
  text-transform: uppercase; color: var(--muted); margin-bottom: 8px; }
.rem-item { padding: 6px 0; border-top: 1px dashed #d8dae3; cursor: pointer;
  transition: background 0.12s; border-radius: 4px; }
.rem-item:first-of-type { border-top: none; }
.rem-item:hover { background: #ebedf3; padding-left: 6px; padding-right: 6px; }
.rem-name { font-size: 13px; font-weight: 500; color: var(--text); }
.rem-meta { font-size: 11px; color: var(--muted); margin-top: 2px; }

.job-lift { background: var(--bg); border-radius: 10px; padding: 14px 16px;
  border: 1px solid var(--border); }
.job-lift-head { font-size: 11px; font-weight: 600; letter-spacing: 0.6px;
  text-transform: uppercase; color: var(--text-soft); margin-bottom: 10px; }
.lift-item { padding: 8px 0; border-top: 1px dashed var(--border); }
.lift-item:first-of-type { border-top: none; padding-top: 0; }
.lift-title { font-size: 13px; font-weight: 500; color: var(--text); margin-bottom: 2px; }
.lift-suggest { font-size: 12px; color: var(--text-soft); line-height: 1.5; }
.job-lift-empty { font-size: 12px; color: var(--text-faint); font-style: italic; }

/* Job-name suggestions (LLM-inferred, v1.2b) */
.sg-block { margin-bottom: 20px; }
.sg-card { display: flex; align-items: center; gap: 14px; padding: 14px 16px;
  background: linear-gradient(180deg, #fdfbf3 0%, #f9f4e5 100%);
  border: 1px solid var(--amber-br); border-radius: 12px; margin-bottom: 8px; }
.sg-card.medium { background: var(--bg); border-color: var(--border); }
.sg-bulb { font-size: 18px; line-height: 1; flex-shrink: 0; }
.sg-body { flex: 1; min-width: 0; }
.sg-line1 { font-size: 14px; color: var(--text); }
.sg-line1 strong { font-weight: 600; }
.sg-line2 { font-size: 12px; color: var(--text-soft); margin-top: 3px; }
.sg-actions { display: flex; gap: 6px; flex-shrink: 0; }
.sg-actions button { background: transparent; border: 1px solid var(--border);
  color: var(--text-soft); font-size: 12px; padding: 5px 12px; border-radius: 6px;
  cursor: pointer; font-family: inherit; transition: all 0.12s; }
.sg-actions button:hover { background: var(--surface); color: var(--text); border-color: var(--border-d); }
.sg-actions button.primary { background: var(--text); color: var(--bg); border-color: var(--text); }
.sg-actions button.primary:hover { opacity: 0.88; }

/* Recently ended jobs (mini-list under the active list) */
.re-block { margin-top: 28px; padding-top: 18px; border-top: 1px solid var(--border); }
.re-head { font-size: 11px; font-weight: 600; letter-spacing: 0.6px;
  text-transform: uppercase; color: var(--text-soft); margin-bottom: 10px; }
.re-row { display: flex; align-items: center; gap: 12px; padding: 8px 0;
  border-bottom: 1px dashed var(--border); }
.re-row:last-child { border-bottom: none; }
.re-name { flex: 1; font-size: 13px; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.re-meta { font-size: 11px; color: var(--text-faint); font-variant-numeric: tabular-nums; }
.re-actions { display: flex; gap: 4px; }
.re-actions button { background: transparent; border: 1px solid var(--border);
  color: var(--text-soft); font-size: 11px; padding: 3px 9px; border-radius: 5px;
  cursor: pointer; font-family: inherit; transition: all 0.12s; }
.re-actions button:hover { background: var(--bg); color: var(--text); border-color: var(--border-d); }
.re-auto-tag { font-size: 10px; color: var(--text-faint); font-style: italic;
  margin-left: 6px; }

/* Export modal — markdown preview + copy/download */
.export-modal .modal { max-width: 720px; }
.export-meta { font-size: 12px; color: var(--text-soft); margin-bottom: 14px; }
.export-preview { background: var(--bg); border: 1px solid var(--border);
  border-radius: 10px; padding: 18px 22px; max-height: 50vh; overflow-y: auto;
  font-family: ui-monospace, 'SF Mono', Menlo, Consolas, monospace;
  font-size: 12px; line-height: 1.55; color: var(--text);
  white-space: pre-wrap; word-break: break-word; }
.export-loading { padding: 60px 0; text-align: center; color: var(--text-faint);
  font-style: italic; }

/* Taxonomy wizard (v1.7 Slice 2) */
.tx-qs-btn { background: var(--surface); color: var(--text); border: 1px solid var(--border);
  font-size: 13px; font-weight: 500; padding: 8px 14px; border-radius: 8px;
  cursor: pointer; font-family: inherit; transition: all 0.12s; }
.tx-qs-btn:hover { background: var(--bg-soft); border-color: var(--border-d); }
.tx-tree-list { list-style: none; padding: 0; margin: 0; }
.tx-tree-list ul { list-style: none; padding: 0; margin: 4px 0 0 22px; border-left: 1px dashed var(--border); padding-left: 14px; }
.tx-node { display: flex; align-items: center; gap: 8px; padding: 8px 10px; border-radius: 8px;
  transition: background 0.12s; margin-bottom: 4px; }
.tx-node:hover { background: var(--bg-soft); }
.tx-node-color { width: 10px; height: 10px; border-radius: 50%; flex-shrink: 0; }
.tx-node-label { flex: 1; font-size: 14px; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tx-node-key { font-size: 11px; color: var(--text-faint); font-family: ui-monospace, monospace; }
.tx-node-actions { display: flex; gap: 4px; opacity: 0.4; transition: opacity 0.12s; }
.tx-node:hover .tx-node-actions { opacity: 1; }
.tx-icon-btn { background: transparent; border: none; cursor: pointer; padding: 4px 8px;
  border-radius: 5px; color: var(--text-soft); font-family: inherit; font-size: 12px;
  transition: all 0.12s; }
.tx-icon-btn:hover { background: var(--surface); color: var(--text); }
.tx-icon-btn.danger:hover { background: #fde8e6; color: #b13a2b; }
.tx-add-form { display: grid; grid-template-columns: 0.7fr 1.3fr auto auto; gap: 8px;
  padding: 10px 12px; margin: 4px 0 4px 22px;
  background: var(--bg-soft); border-radius: 8px; border: 1px dashed var(--border-d); }
.tx-add-form input { padding: 7px 10px; border: 1px solid var(--border); border-radius: 6px;
  font-family: inherit; font-size: 13px; background: var(--surface); }
.tx-add-form button { padding: 6px 12px; border-radius: 6px; font-family: inherit;
  font-size: 12px; cursor: pointer; border: 1px solid var(--border); background: var(--surface);
  color: var(--text); }
.tx-add-form button.primary { background: var(--text); color: var(--bg); border-color: var(--text); }
.tx-rename-form { display: flex; gap: 6px; flex: 1; }
.tx-rename-form input { flex: 1; padding: 6px 9px; border: 1px solid var(--border-d);
  border-radius: 6px; font-family: inherit; font-size: 14px; }
.tx-mig-row { display: flex; align-items: center; gap: 10px; padding: 8px 0;
  border-bottom: 1px dashed var(--border); }
.tx-mig-row:last-child { border-bottom: none; }
.tx-mig-name { flex: 1; font-size: 13px; min-width: 0;
  white-space: nowrap; overflow: hidden; text-overflow: ellipsis; }
.tx-mig-row select { padding: 6px 10px; border: 1px solid var(--border-d); border-radius: 6px;
  font-family: inherit; font-size: 13px; min-width: 180px; }

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
  <div class="brand" style="display:flex; align-items:center; gap:8px;">
    <button id="personal-lock-btn" onclick="openPersonalModal()" title="Personal (locked)"
            style="background:transparent; border:0; cursor:pointer; padding:2px 4px; font-size:14px; color:var(--text-soft); opacity:0.6;">🔒</button>
    <span class="pulse" id="pulse"></span>WorkPulse <span id="actor-suffix" style="color:var(--text-soft);font-weight:400;margin-left:6px"></span></div>
  <div class="topbar-right">
    <span class="date" id="date"></span>
    <button id="health-indicator" onclick="openHealthModal(event)" title="System health"
            style="background:transparent; border:0; cursor:pointer; padding:2px 6px; font-size:15px; line-height:1; display:inline-flex; align-items:center; gap:4px;">
      <span id="health-dot">●</span>
    </button>
    <button class="icon-btn" onclick="openViewMenu(event)" title="Show / hide cards" style="margin-right:6px;">View</button>
    <button class="icon-btn" onclick="openSettings()" title="Settings">Settings</button>
  </div>
</div>

<!-- Health modal: opens on the health indicator click -->
<div id="health-modal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.4); z-index:10000; align-items:center; justify-content:center;">
  <div style="background:var(--surface, #fff); border-radius:10px; padding:20px; min-width:420px; max-width:560px; box-shadow:0 10px 40px rgba(0,0,0,0.2);">
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:14px;">
      <strong id="health-modal-title" style="font-size:16px;">System health</strong>
      <button onclick="closeHealthModal()" style="background:transparent; border:0; cursor:pointer; font-size:18px; color:var(--text-soft);">×</button>
    </div>
    <div id="health-modal-body"><div class="empty">Loading…</div></div>
  </div>
</div>

<!-- Personal modal: opens on padlock click -->
<div id="personal-modal" style="display:none; position:fixed; inset:0; background:rgba(0,0,0,0.4); z-index:10000; align-items:center; justify-content:center;">
  <div style="background:var(--surface, #fff); border-radius:10px; padding:20px; min-width:380px; max-width:520px; box-shadow:0 10px 40px rgba(0,0,0,0.2);">
    <div style="display:flex; align-items:center; justify-content:space-between; margin-bottom:12px;">
      <strong style="font-size:16px;">🔒 Personal</strong>
      <button onclick="closePersonalModal()" style="background:transparent; border:0; cursor:pointer; font-size:18px; color:var(--text-soft);">×</button>
    </div>
    <div id="personal-modal-body"><div class="empty">Loading…</div></div>
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
  <div class="hero-text">
    <div class="hero-greeting"><span class="sun" id="hero-icon">☀️</span><span id="hero-greeting-text">Good morning</span></div>
    <h1 id="hero-headline">Loading…</h1>
    <div class="sub" id="hero-sub"></div>
  </div>
  <div class="hero-cta">
    <button class="btn-hero" id="hero-primary-btn" onclick="heroPrimaryAction()">
      <span class="ic" id="hero-primary-icon">🌅</span>
      <span id="hero-primary-label">Plan your day</span>
    </button>
    <button class="btn-hero subtle" onclick="openStartJob()">
      <span class="ic">＋</span><span>Start a job</span>
    </button>
  </div>
</div>

<!-- Untagged-time alert (shows only when threshold crossed) -->
<div class="alert-card" id="untagged-alert" style="display:none">
  <div class="alert-head">
    <span class="alert-icon">⚠</span>
    <span id="untagged-alert-text"></span>
  </div>
  <div id="untagged-alert-items"></div>
</div>

<div class="cards-grid">

  <!-- v2 capture bar: frictionless input. The brain needs intent declarations
       to know what work belongs to which project; the CLI verb is too slow. -->
  <div class="card span-12" id="capture-bar" style="background: var(--bg-warm, #fff6e0); border-left: 3px solid #f59e0b;">
    <form id="capture-form" onsubmit="return submitCapture(event)" style="display:flex; gap:8px; align-items:center;">
      <label for="capture-input" style="font-size:13px; color:var(--text-soft); white-space:nowrap;">
        What are you working on?
      </label>
      <input type="text" id="capture-input"
             placeholder="e.g. Uganda MEMD narrative, methodology section"
             style="flex:1; padding:8px 12px; border:1px solid var(--border, #eee5d2); border-radius:6px; font-size:14px; background:var(--surface, #fff);"
             autocomplete="off" />
      <button type="submit"
              style="padding:8px 14px; border:0; border-radius:6px; background:#f59e0b; color:#fff; font-weight:600; cursor:pointer;">
        Capture
      </button>
      <span id="capture-status" style="font-size:12px; color:var(--text-soft); min-width:120px; text-align:right;"></span>
    </form>
  </div>

  <!-- v2 brain view: what you actually worked on today -->
  <div class="card span-12 collapsible" id="today-card" data-card="today" data-card-label="What you worked on today" style="border-left: 3px solid var(--accent, #3b82f6);">
    <div class="eyebrow card-header" onclick="toggleCard('today')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text">
        <span>What you worked on today</span>
        <span id="today-meta" class="card-header-meta"></span>
      </div>
    </div>
    <div class="card-summary" id="today-summary"></div>
    <div class="card-body">
      <div id="today-panel"><div class="empty">Loading…</div></div>
    </div>
  </div>

  <!-- v2 brain layer: "About George" — the living profile (Step A) -->
  <div class="card span-12 collapsible" id="profile-card" data-card="profile" data-card-label="Who you are" style="border-left: 3px solid #8b5cf6;">
    <div class="eyebrow card-header" onclick="toggleCard('profile')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text">
        <span>Who you are</span>
        <span id="profile-meta" class="card-header-meta"></span>
      </div>
    </div>
    <div class="card-summary" id="profile-summary"></div>
    <div class="card-body">
      <div id="profile-panel"><div class="empty">Loading…</div></div>
    </div>
  </div>

  <!-- Today's plan (v1.7 intent capture — the Morning Plan surface) -->
  <div class="card plan-card span-12 collapsible" id="plan-card" data-card="plan" data-card-label="Today's plan">
    <div class="eyebrow card-header plan-eyebrow" onclick="toggleCard('plan', event)">
      <span class="card-toggle">▾</span>
      <div class="card-header-text">
        <span>Today's plan</span>
        <button class="btn-start-job" id="plan-action-btn" onclick="event.stopPropagation(); openPlanModal()">+ Plan your day</button>
      </div>
    </div>
    <div class="card-body">
      <div id="plan-panel"><div class="empty">Loading…</div></div>
    </div>
  </div>

  <!-- Jobs in flight (the Coach surface — v1.1a + v1.2a + v1.2b) -->
  <div class="card jobs-card span-12 collapsible" id="jobs-card" data-card="jobs" data-card-label="Jobs in flight">
    <div class="eyebrow card-header jobs-eyebrow" onclick="toggleCard('jobs')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text">
        <span>Jobs in flight</span>
      </div>
    </div>
    <div class="card-body">
      <div id="suggestions-block" class="sg-block"></div>
      <div id="jobs-panel"><div class="empty">Loading…</div></div>
      <div id="recently-ended-block"></div>
    </div>
  </div>

  <!-- Weekly heatmap -->
  <div class="card span-12 collapsible" data-card="heatmap" data-card-label="Last 14 days">
    <div class="eyebrow card-header" onclick="toggleCard('heatmap')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text"><span>Last 14 days</span></div>
    </div>
    <div class="card-body">
      <div class="heatmap" id="heatmap"><div class="empty">Loading…</div></div>
    </div>
  </div>

  <div class="card span-8 collapsible" data-card="donut" data-card-label="Where the time went">
    <div class="eyebrow card-header" onclick="toggleCard('donut')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text"><span>Where the time went</span></div>
    </div>
    <div class="card-body">
      <div id="donut-panel"><div class="empty">Loading…</div></div>
    </div>
  </div>
  <div class="card span-4 collapsible" data-card="last-active" data-card-label="Last active">
    <div class="eyebrow card-header" onclick="toggleCard('last-active')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text"><span>Last active</span></div>
    </div>
    <div class="card-body">
      <div id="last-active"><div class="empty">Loading…</div></div>
    </div>
  </div>

  <div class="card span-12 collapsible" data-card="timeline" data-card-label="Today's flow">
    <div class="eyebrow card-header" onclick="toggleCard('timeline')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text"><span>Today's flow</span></div>
    </div>
    <div class="card-body">
      <div class="tl-wrap">
        <div class="tl" id="timeline"></div>
        <div class="tl-axis" id="tl-axis"></div>
      </div>
    </div>
  </div>

  <div class="card span-8 collapsible" data-card="attention" data-card-label="Needs your attention">
    <div class="eyebrow card-header" onclick="toggleCard('attention')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text"><span>Needs your attention</span></div>
    </div>
    <div class="card-body">
      <div id="attn-panel"><div class="empty">Nothing untagged today.</div></div>
    </div>
  </div>
  <div class="card span-4 collapsible" data-card="apps" data-card-label="Apps used">
    <div class="eyebrow card-header" onclick="toggleCard('apps')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text"><span>Apps used</span></div>
    </div>
    <div class="card-body">
      <div id="apps-panel"><div class="empty">Loading…</div></div>
    </div>
  </div>

  <div class="card span-12 collapsible" data-card="ai" data-card-label="AI sessions">
    <div class="eyebrow card-header" onclick="toggleCard('ai')">
      <span class="card-toggle">▾</span>
      <div class="card-header-text"><span>AI sessions</span></div>
    </div>
    <div class="card-body">
      <div id="ai-panel"><div class="empty">Loading…</div></div>
    </div>
  </div>

  <div class="span-12">
    <details>
      <summary style="cursor:pointer; color:var(--text-soft); font-size:13px; padding:8px 4px;">Show every window visit today</summary>
      <div class="card" style="margin-top: 8px">
        <div id="sessions-panel"><div class="empty">Loading…</div></div>
      </div>
    </details>
  </div>

</div>

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

    <div class="modal-row" style="background:var(--bg);padding:14px 16px;border-radius:10px;border:1px solid var(--border);margin-bottom:14px">
      <label style="margin-bottom:8px">Taxonomy (stream hierarchy)</label>
      <div style="font-size:13px;color:var(--text-soft);line-height:1.6;margin-bottom:10px">
        Tree of contexts that everything you do gets tagged under.
      </div>
      <button class="btn-hero subtle" style="font-size:13px;padding:8px 14px" onclick="closeSettings(); openTaxonomy('edit');">Edit taxonomy</button>
    </div>

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
        Anthropic API key <span style="color:var(--text-faint);font-weight:400">(cloud, best quality, pay per call)</span>
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

<!-- Job-Export modal -->
<div class="modal-bg export-modal" id="export-modal" onclick="if(event.target===this)closeExport()">
  <div class="modal" role="dialog" aria-labelledby="export-title">
    <h2 id="export-title">Export job</h2>
    <div class="sub" id="export-sub">Building digest…</div>
    <div class="export-meta">
      <label style="display:inline-flex;gap:6px;align-items:center;font-size:12px">
        <input type="checkbox" id="export-include-titles" checked />
        Include window titles in the day-by-day breakdown
      </label>
    </div>
    <div class="export-preview" id="export-preview"><div class="export-loading">Generating…</div></div>
    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeExport()">Close</button>
      <div style="display:flex;gap:8px">
        <button class="btn-link" onclick="copyExport()">Copy</button>
        <button class="btn-primary" onclick="downloadExport()">Download .md</button>
      </div>
    </div>
  </div>
</div>

<!-- Taxonomy / stream-tree wizard (v1.7 Slice 2) -->
<div class="modal-bg" id="tx-modal" onclick="if(event.target===this)closeTaxonomy()">
  <div class="modal" role="dialog" aria-labelledby="tx-title" style="max-width: 720px">
    <h2 id="tx-title">Set up your taxonomy</h2>
    <div class="sub" id="tx-intro">
      WorkPulse organises your work as a tree of contexts. Build the tree once;
      every Job and every window's time rolls up through it. You can edit this any time
      from Settings.
    </div>

    <div id="tx-quickstart" style="margin: 18px 0; display: none;">
      <div class="plan-sg-label">Quick start: tap to add a top-level domain</div>
      <div style="display:flex; flex-wrap:wrap; gap:8px; margin-top:8px;">
        <button class="tx-qs-btn" onclick="txQuickAdd('personal','Personal')">+ Personal</button>
        <button class="tx-qs-btn" onclick="txQuickAdd('masters','Master\\'s')">+ Master's</button>
        <button class="tx-qs-btn" onclick="txQuickAdd('work','Work')">+ Work</button>
      </div>
    </div>

    <div id="tx-tree" style="margin-top: 18px;"></div>

    <div id="tx-migration" style="margin-top: 18px; display: none;"></div>

    <div class="modal-actions">
      <button class="btn-ghost" onclick="closeTaxonomy()">Close</button>
      <button class="btn-primary" onclick="finishTaxonomy()">Done</button>
    </div>
  </div>
</div>

<!-- Plan-the-day modal -->
<div class="modal-bg" id="plan-modal" onclick="if(event.target===this)closePlanModal()">
  <div class="modal" role="dialog" aria-labelledby="plan-title">
    <h2 id="plan-title">Plan today</h2>
    <div class="sub">Declare what you intend to work on. Each item becomes a Job. Sessions roll up to it automatically. End-of-day you can see planned vs actual.</div>

    <div id="plan-suggestions" class="plan-sg-block"></div>

    <div class="sj-row">
      <label for="plan-new-items">New items (one per line)</label>
      <textarea id="plan-new-items" rows="6" placeholder="One item per line, e.g.&#10;NKCC Q3 narrative (~120 min)&#10;Call Mwangi (~30 min)&#10;Review v1.7 brief"></textarea>
      <div class="sub plan-hint">Tip: add <code>(~N min)</code> for an optional time target.</div>
    </div>

    <div class="modal-actions">
      <button class="btn-ghost" onclick="closePlanModal()">Cancel</button>
      <button class="btn-primary" onclick="submitPlan()">Save plan</button>
    </div>
  </div>
</div>

<!-- Start-Job modal -->
<div class="modal-bg" id="sj-modal" onclick="if(event.target===this)closeStartJob()">
  <div class="modal" role="dialog" aria-labelledby="sj-title">
    <h2 id="sj-title">Start a job</h2>
    <div class="sub">A job is a coherent unit of work: "NKCC Q3 report", "Chapter 3 lit review". Sessions inside this stream from now until you end the job will roll up to it.</div>
    <div class="sj-row">
      <label for="sj-name">What are you working on?</label>
      <input type="text" id="sj-name" placeholder="e.g. NKCC Q3 report" autocomplete="off" />
    </div>
    <div class="sj-row">
      <label for="sj-stream">Stream</label>
      <select id="sj-stream" onchange="onStreamSelectChange()"></select>
      <div id="sj-newstream" style="display:none; margin-top:10px; padding:12px; background:var(--bg-soft); border-radius:8px; border:1px dashed var(--border-d)">
        <div style="display:grid; grid-template-columns: 1fr 1.4fr; gap:8px; margin-bottom:8px">
          <input type="text" id="sj-newstream-key" placeholder="key (e.g. client-x)" />
          <input type="text" id="sj-newstream-label" placeholder="label (e.g. Client X / Consulting)" />
        </div>
        <div style="font-size:11px; color:var(--text-faint)">
          Key: lowercase letters / digits / hyphens. The label is what shows on the dashboard.
        </div>
      </div>
    </div>
    <div class="sj-row">
      <label for="sj-note">Note (optional)</label>
      <textarea id="sj-note" placeholder="A line or two about the goal of this job. Used later when reviewing how it went."></textarea>
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
  // Also normalise em/en dashes to a plain hyphen — the dashboard should
  // never show "—". This covers static strings AND LLM-generated content
  // (profile, cluster summaries, reports) since nearly everything rendered
  // passes through here. A surrounding " — " collapses to " - ".
  return (s || '')
    .replace(/\s*[—–]\s*/g, ' - ')
    .replace(/[&<>"']/g, c =>
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
      detail.innerHTML = `Ollama is running but model <code>${llm.ollama.want_model}</code> isn't pulled yet. Run: <code>ollama pull ${llm.ollama.want_model}</code>, or add an Anthropic key in Settings.`;
    } else {
      detail.textContent = 'Add an Anthropic API key in Settings, or install Ollama for free local AI.';
    }
  }

  document.getElementById('anthropic-status').className =
    'status-dot' + (d.secrets.anthropic_key.configured ? '' : ' off');
  document.getElementById('smtp-status').className =
    'status-dot' + (d.secrets.smtp_password.configured ? '' : ' off');

  // Taxonomy wizard auto-open (once per browser session while tree is trivial)
  maybeAutoOpenTaxonomy();
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

  // ── Untagged-time alert (v1.7) ────────────────────────────────────────
  // Fires when today's untagged time crosses the threshold. Reuses the
  // same Tag-as dropdown UX as the lower "Needs your attention" panel but
  // makes the worst offenders impossible to miss.
  const UNTAGGED_ALERT_MIN = 20;
  const alertEl = document.getElementById('untagged-alert');
  const untaggedMin = d.untagged_minutes || 0;
  const topUntagged = (d.untagged_windows || []).filter(w => w.minutes >= 3).slice(0, 3);
  if (untaggedMin >= UNTAGGED_ALERT_MIN && topUntagged.length > 0) {
    document.getElementById('untagged-alert-text').textContent =
      `${fmtMins(untaggedMin)} untagged today — tag the patterns below so future activity rolls up.`;
    document.getElementById('untagged-alert-items').innerHTML = topUntagged.map((w, i) => `
      <div class="alert-row">
        <span class="alert-title" title="${escapeHtml(w.title)}">${escapeHtml(w.title)}</span>
        <span class="alert-min">${fmtMins(w.minutes)}</span>
        <div class="attn-tag">
          <button class="tag-btn" onclick="toggleTagMenu('alert-${i}')">Tag as ▾</button>
          <div class="tag-menu" id="tag-menu-alert-${i}">
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
    alertEl.style.display = '';
  } else {
    alertEl.style.display = 'none';
  }

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
  let active = [], ended = [], suggestions = [];
  try {
    const [aResp, rResp, sResp] = await Promise.all([
      fetch('/api/jobs/active'),
      fetch('/api/jobs/recent?days=14&only_ended=true'),
      fetch('/api/jobs/suggestions'),
    ]);
    active = (await aResp.json()).jobs || [];
    ended  = (await rResp.json()).jobs || [];
    suggestions = (await sResp.json()).suggestions || [];
  } catch (e) {
    document.getElementById('jobs-panel').innerHTML =
      '<div class="empty">Could not load jobs.</div>';
    return;
  }
  renderSuggestions(suggestions);
  const panel = document.getElementById('jobs-panel');
  if (active.length === 0) {
    panel.innerHTML =
      '<div class="empty">No jobs in flight. Hit "+ Start a job" to track a specific piece of work.</div>';
  } else {
    panel.innerHTML = active.map(renderJobCard).join('');
  }
  // Recently-ended mini-list (newest first, cap at 5)
  const reBlock = document.getElementById('recently-ended-block');
  const recent = ended.slice(0, 5);
  if (recent.length === 0) {
    reBlock.innerHTML = '';
  } else {
    reBlock.innerHTML = `
      <div class="re-block">
        <div class="re-head">Recently ended</div>
        ${recent.map(j => `
          <div class="re-row">
            <span class="lg-dot" style="background:${j.stream_color}"></span>
            <div class="re-name">
              ${escapeHtml(j.name)}
              ${j.ended_by === 'auto' ? '<span class="re-auto-tag">auto-ended</span>' : ''}
            </div>
            <div class="re-meta">ended ${fmtRelative(j.ended_at)}</div>
            <div class="re-actions">
              <button onclick="openExport('${escapeHtml(j.id)}', '${escapeHtml(j.name)}')">Export</button>
              <button onclick="resumeJob('${escapeHtml(j.id)}', '${escapeHtml(j.name)}')">Resume</button>
            </div>
          </div>`).join('')}
      </div>`;
  }
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

  // Cross-Job remembrance block — surfaces prior Jobs with semantic overlap.
  // Local-only, deterministic match. Click a row → opens that Job's export
  // (which contains the full session breakdown + apps + LLM narrative if
  // a key was configured at export time).
  let remBlock = '';
  const rem = job.remembrance || [];
  if (rem.length > 0) {
    remBlock = `<div class="job-rem">
      <div class="job-rem-head">Worth knowing from before</div>
      ${rem.map(m => `
        <div class="rem-item" onclick="openExport('${escapeHtml(m.job_id)}', '${escapeHtml(m.name)}')">
          <div class="rem-name">${escapeHtml(m.name)}</div>
          <div class="rem-meta">
            ${m.total_minutes > 0 ? fmtMins(m.total_minutes) + ' · ' : ''}${escapeHtml(m.reason)}
          </div>
        </div>`).join('')}
    </div>`;
  }

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

  const jic = workIcon(job.name);
  return `
    <div class="job-card" data-job="${escapeHtml(job.id)}" style="--stream-color:${job.stream_color}">
      <div class="job-head">
        <span class="job-icon">${jic}</span>
        <div class="job-name">${escapeHtml(job.name)}</div>
        <span class="chip" style="background:${job.stream_color}">${escapeHtml((job.stream||'').replace(/-/g,' '))}</span>
        <div class="job-actions">
          <button class="job-end-btn" onclick="openExport('${escapeHtml(job.id)}', '${escapeHtml(job.name)}')">Export</button>
          <button class="job-end-btn" onclick="endJob('${escapeHtml(job.id)}', '${escapeHtml(job.name)}')">End job</button>
        </div>
      </div>
      <div class="job-meta">
        ${total} total · started ${started} · ${sessions} session${sessions===1?'':'s'}
        ${paused ? `<span class="job-paused">paused, last touch ${fmtRelative(job.last_active)}</span>` : ''}
      </div>
      ${sessions > 0 ? `<div class="job-apps">${appsLine || '<span style="color:var(--text-faint)">(no app data yet)</span>'}</div>` : ''}
      ${remBlock}
      ${liftBlock}
    </div>
  `;
}

function openStartJob() {
  // Populate stream dropdown from systemSnapshot (already fetched).
  // Append a sentinel "+ Create new stream…" option so users aren't locked
  // into the streams currently in config.yaml.
  const sel = document.getElementById('sj-stream');
  const streams = (systemSnapshot && systemSnapshot.streams) || availableStreams || [];
  const opts = streams.map(s =>
    `<option value="${escapeHtml(s.key)}">${escapeHtml(s.label)}</option>`
  );
  opts.push('<option value="__new__">+ Create new stream…</option>');
  sel.innerHTML = opts.join('');
  document.getElementById('sj-newstream').style.display = 'none';
  document.getElementById('sj-newstream-key').value = '';
  document.getElementById('sj-newstream-label').value = '';
  document.getElementById('sj-name').value = '';
  document.getElementById('sj-note').value = '';
  document.getElementById('sj-modal').classList.add('open');
  setTimeout(() => document.getElementById('sj-name').focus(), 60);
}

function onStreamSelectChange() {
  const sel = document.getElementById('sj-stream');
  const block = document.getElementById('sj-newstream');
  if (sel.value === '__new__') {
    block.style.display = '';
    setTimeout(() => document.getElementById('sj-newstream-key').focus(), 40);
  } else {
    block.style.display = 'none';
  }
}

function closeStartJob() {
  document.getElementById('sj-modal').classList.remove('open');
}

async function submitStartJob() {
  const name = document.getElementById('sj-name').value.trim();
  let stream = document.getElementById('sj-stream').value;
  const note = document.getElementById('sj-note').value.trim();
  if (!name) { showToast('Job needs a name.'); return; }

  // If the user picked "Create new stream", create it first.
  if (stream === '__new__') {
    const key   = document.getElementById('sj-newstream-key').value.trim().toLowerCase();
    const label = document.getElementById('sj-newstream-label').value.trim();
    if (!key || !label) {
      showToast('New stream needs both a key and a label.');
      return;
    }
    try {
      const r = await fetch('/api/streams', {
        method: 'POST',
        headers: {'Content-Type': 'application/json'},
        body: JSON.stringify({ key, label })
      });
      const d = await r.json();
      if (!r.ok) {
        showToast('New stream error: ' + (d.error || 'could not create'));
        return;
      }
      stream = key;
      // Refresh systemSnapshot so the new stream is available everywhere
      await fetchSystem();
    } catch (e) {
      showToast('New stream error: ' + e.message);
      return;
    }
  }

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

// ── Taxonomy wizard (v1.7 Slice 2) ──────────────────────────────────────
// Build / edit the stream hierarchy on the dashboard. Auto-opens once per
// browser session while the tree is "trivial" (per /api/system). After
// closing, the user can re-open from Settings.

let taxonomyOpen = false;

function maybeAutoOpenTaxonomy() {
  // Only auto-open once per session, and only if the tree is still trivial.
  if (taxonomyOpen) return;
  if (sessionStorage.getItem('wp.taxWizardSeen')) return;
  if (!systemSnapshot || !systemSnapshot.taxonomy_trivial) return;
  openTaxonomy('first-load');
}

function openTaxonomy(reason) {
  taxonomyOpen = true;
  document.getElementById('tx-modal').classList.add('open');
  document.getElementById('tx-title').textContent =
    reason === 'first-load' ? 'Set up your taxonomy' : 'Edit your taxonomy';
  // Show quick-start chips only on the first-time setup
  document.getElementById('tx-quickstart').style.display =
    (systemSnapshot && systemSnapshot.taxonomy_trivial) ? '' : 'none';
  renderTaxonomyTree();
  document.getElementById('tx-migration').style.display = 'none';
  document.getElementById('tx-migration').innerHTML = '';
}

function closeTaxonomy() {
  taxonomyOpen = false;
  document.getElementById('tx-modal').classList.remove('open');
  sessionStorage.setItem('wp.taxWizardSeen', '1');
}

async function finishTaxonomy() {
  // Check whether any active Jobs sit in trivial / legacy streams and offer
  // to re-home them under the new tree.
  try {
    const ar = await fetch('/api/jobs/active');
    const ad = await ar.json();
    const streams = (systemSnapshot && systemSnapshot.streams) || [];
    const validKeys = new Set(streams.map(s => s.key));
    // Candidates for migration: Jobs whose stream is 'misc' OR whose stream
    // is a top-level domain when descendants exist (i.e. user could go
    // more specific). Conservative: just flag 'misc' for the first pass.
    const candidates = (ad.jobs || []).filter(j => j.stream === 'misc' && streams.length > 1);
    if (candidates.length === 0) {
      closeTaxonomy();
      await fetchSystem();
      await refreshDayPanels();
      showToast('Taxonomy saved.');
      return;
    }
    renderMigration(candidates, streams);
  } catch (e) {
    closeTaxonomy();
    showToast('Taxonomy saved.');
  }
}

function renderMigration(jobs, streams) {
  const block = document.getElementById('tx-migration');
  const opts = streams.filter(s => s.key !== 'misc').map(s =>
    `<option value="${escapeHtml(s.key)}">${escapeHtml(s.breadcrumb || s.label)}</option>`
  ).join('');
  block.innerHTML = `
    <div class="plan-sg-label" style="margin-top:8px">Re-home these Jobs from <code>misc</code></div>
    <div style="margin-top:6px">
      ${jobs.map(j => `
        <div class="tx-mig-row">
          <span class="tx-mig-name" title="${escapeHtml(j.name)}">${escapeHtml(j.name)}</span>
          <select data-jobid="${escapeHtml(j.id)}">
            <option value="">— keep in misc —</option>
            ${opts}
          </select>
        </div>`).join('')}
    </div>
    <div style="display:flex; gap:8px; justify-content:flex-end; margin-top:12px">
      <button class="btn-ghost" onclick="skipMigration()">Skip</button>
      <button class="btn-primary" onclick="applyMigration()">Apply</button>
    </div>`;
  block.style.display = '';
}

async function applyMigration() {
  const rows = document.querySelectorAll('#tx-migration select[data-jobid]');
  let moved = 0, errored = 0;
  for (const sel of rows) {
    const target = sel.value;
    if (!target) continue;
    try {
      const r = await fetch(`/api/jobs/${sel.dataset.jobid}/restream`, {
        method: 'POST',
        headers: {'Content-Type':'application/json'},
        body: JSON.stringify({ stream: target })
      });
      if (r.ok) moved++; else errored++;
    } catch (e) { errored++; }
  }
  closeTaxonomy();
  await fetchSystem();
  await refreshDayPanels();
  showToast(`Migrated ${moved} job${moved===1?'':'s'}${errored?` (${errored} failed)`:''}.`);
}

function skipMigration() { closeTaxonomy(); fetchSystem(); refreshDayPanels(); }

async function txQuickAdd(key, label) {
  try {
    const r = await fetch('/api/streams', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ key, label })
    });
    const d = await r.json();
    if (!r.ok) { showToast(d.error || 'could not add'); return; }
    await fetchSystem();
    renderTaxonomyTree();
  } catch (e) { showToast('Error: ' + e.message); }
}

function renderTaxonomyTree() {
  const streams = (systemSnapshot && systemSnapshot.streams) || [];
  // Build adjacency: parent → [children]
  const byParent = {};
  streams.forEach(s => {
    const p = s.parent || '__root__';
    (byParent[p] = byParent[p] || []).push(s);
  });
  function renderLevel(parentKey) {
    const kids = byParent[parentKey] || [];
    if (kids.length === 0) return '';
    kids.sort((a, b) => (a.label || '').localeCompare(b.label || ''));
    return `<ul class="tx-tree-list">${kids.map(s => `
      <li>
        <div class="tx-node" id="tx-node-${escapeHtml(s.key)}">
          <span class="tx-node-color" style="background:${s.color}"></span>
          <span class="tx-node-label">${escapeHtml(s.label)}</span>
          <span class="tx-node-key">${escapeHtml(s.key)}</span>
          <div class="tx-node-actions">
            <button class="tx-icon-btn" onclick="txBeginRename('${escapeHtml(s.key)}')">rename</button>
            <button class="tx-icon-btn" onclick="txShowAddChild('${escapeHtml(s.key)}')">+ child</button>
            <button class="tx-icon-btn danger" onclick="txDelete('${escapeHtml(s.key)}','${escapeHtml(s.label)}')">×</button>
          </div>
        </div>
        <div id="tx-form-${escapeHtml(s.key)}"></div>
        ${renderLevel(s.key)}
      </li>`).join('')}
    </ul>`;
  }
  const tree = document.getElementById('tx-tree');
  const rootHTML = renderLevel('__root__');
  if (!rootHTML) {
    tree.innerHTML = `
      <div class="empty" style="text-align:left">
        No streams yet — use the quick-start chips above or
        <button class="tx-icon-btn" onclick="txShowAddChild('')" style="text-decoration:underline">+ add a top-level domain</button>.
      </div>
      <div id="tx-form-"></div>`;
    return;
  }
  tree.innerHTML = `
    ${rootHTML}
    <div style="margin-top:12px"><button class="tx-icon-btn" onclick="txShowAddChild('')">+ add another top-level domain</button></div>
    <div id="tx-form-"></div>`;
}

function txShowAddChild(parentKey) {
  // Replace the form slot under this node with an inline form.
  const slot = document.getElementById(`tx-form-${parentKey}`);
  if (!slot) return;
  slot.innerHTML = `
    <div class="tx-add-form">
      <input type="text" placeholder="key (e.g. uganda-memd)" id="tx-new-key-${parentKey}" />
      <input type="text" placeholder="label (e.g. Uganda MEMD Project)" id="tx-new-label-${parentKey}" />
      <button onclick="txAddChild('${parentKey}')" class="primary">Add</button>
      <button onclick="txCancelAdd('${parentKey}')">Cancel</button>
    </div>`;
  setTimeout(() => document.getElementById(`tx-new-key-${parentKey}`).focus(), 30);
}

function txCancelAdd(parentKey) {
  const slot = document.getElementById(`tx-form-${parentKey}`);
  if (slot) slot.innerHTML = '';
}

async function txAddChild(parentKey) {
  const key   = document.getElementById(`tx-new-key-${parentKey}`).value.trim().toLowerCase();
  const label = document.getElementById(`tx-new-label-${parentKey}`).value.trim();
  if (!key || !label) { showToast('Both key and label are required.'); return; }
  try {
    const body = { key, label };
    if (parentKey) body.parent = parentKey;
    const r = await fetch('/api/streams', {
      method: 'POST',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify(body)
    });
    const d = await r.json();
    if (!r.ok) { showToast(d.error || 'could not add'); return; }
    await fetchSystem();
    renderTaxonomyTree();
  } catch (e) { showToast('Error: ' + e.message); }
}

function txBeginRename(key) {
  const node = document.getElementById(`tx-node-${key}`);
  const label = node.querySelector('.tx-node-label').textContent;
  // Replace the label + actions with an inline edit form.
  node.querySelector('.tx-node-label').outerHTML = `
    <div class="tx-rename-form">
      <input type="text" id="tx-rename-${key}" value="${escapeHtml(label)}" />
    </div>`;
  node.querySelector('.tx-node-actions').innerHTML = `
    <button class="tx-icon-btn" onclick="txCommitRename('${key}')">save</button>
    <button class="tx-icon-btn" onclick="renderTaxonomyTree()">cancel</button>`;
  setTimeout(() => {
    const inp = document.getElementById(`tx-rename-${key}`);
    inp.focus(); inp.select();
  }, 30);
}

async function txCommitRename(key) {
  const label = document.getElementById(`tx-rename-${key}`).value.trim();
  if (!label) { showToast('Label cannot be empty.'); return; }
  try {
    const r = await fetch(`/api/streams/${encodeURIComponent(key)}`, {
      method: 'PATCH',
      headers: {'Content-Type':'application/json'},
      body: JSON.stringify({ label })
    });
    const d = await r.json();
    if (!r.ok) { showToast(d.error || 'could not rename'); renderTaxonomyTree(); return; }
    await fetchSystem();
    renderTaxonomyTree();
  } catch (e) { showToast('Error: ' + e.message); renderTaxonomyTree(); }
}

async function txDelete(key, label) {
  if (!confirm(`Delete stream "${label}"?\\n\\nRefused if it has children or any Job attached.`)) return;
  try {
    const r = await fetch(`/api/streams/${encodeURIComponent(key)}`, { method: 'DELETE' });
    const d = await r.json();
    if (!r.ok) { showToast(d.error || 'could not delete'); return; }
    await fetchSystem();
    renderTaxonomyTree();
    showToast(`Deleted "${label}".`);
  } catch (e) { showToast('Error: ' + e.message); }
}


// ── Today's plan (Morning Plan — v1.7 intent capture) ─────────────────────

// ── Health banner: surface the doctor's verdict ────────────────────────────
let healthState = null;

async function fetchHealth() {
  try {
    const r = await fetch('/api/v2/health');
    healthState = await r.json();
  } catch (e) {
    healthState = {verdict: 'unknown', summary: 'Health check unavailable.', checks: []};
  }
  updateHealthDot(healthState);
}

function updateHealthDot(d) {
  const dot = document.getElementById('health-dot');
  const btn = document.getElementById('health-indicator');
  if (!dot || !btn) return;
  const v = (d && d.verdict) || 'unknown';
  if (v === 'ok') {
    dot.textContent = '●'; dot.style.color = '#16a34a';
    btn.title = 'All systems healthy';
  } else if (v === 'fail') {
    dot.textContent = '▲'; dot.style.color = '#dc2626';
    btn.title = "Something's wrong, click for details";
  } else if (v === 'warn') {
    dot.textContent = '▲'; dot.style.color = '#d97706';
    btn.title = 'Needs a look, click for details';
  } else {
    dot.textContent = '●'; dot.style.color = 'var(--text-faint)';
    btn.title = 'Health status unknown';
  }
}

function openHealthModal(ev) {
  if (ev) ev.stopPropagation();
  document.getElementById('health-modal').style.display = 'flex';
  renderHealthModal();
}
function closeHealthModal() {
  document.getElementById('health-modal').style.display = 'none';
}

function renderHealthModal() {
  const body = document.getElementById('health-modal-body');
  const title = document.getElementById('health-modal-title');
  const d = healthState || {verdict: 'unknown', checks: []};
  const verdictLabel = {ok: 'All systems healthy', warn: 'Needs a look',
                        fail: "Something's wrong", unknown: 'Status unknown'}[d.verdict] || 'Status unknown';
  const verdictColor = {ok: '#16a34a', warn: '#d97706', fail: '#dc2626',
                        unknown: 'var(--text-soft)'}[d.verdict] || 'var(--text-soft)';
  if (title) title.innerHTML = `System health <span style="color:${verdictColor}; font-weight:500;">· ${verdictLabel}</span>`;

  if (!d.checks || !d.checks.length) {
    body.innerHTML = '<div class="empty">No health check has run yet. The doctor runs every 3 hours.</div>';
    return;
  }
  const iconFor = (s) => s === 'ok'
    ? '<span style="color:#16a34a;">●</span>'
    : (s === 'fail' ? '<span style="color:#dc2626;">▲</span>'
                    : '<span style="color:#d97706;">▲</span>');
  const nice = (k) => ({
    sensors_running: 'Sensors running',
    sensor_liveness: 'Tracker is live',
    sensor_not_stuck: 'Tracking varied apps',
    agents_healthy: 'Background agents',
    data_fresh: 'Recent file activity',
    nightly_ran: 'Nightly summary',
  })[k] || k;
  let html = '<div style="display:flex; flex-direction:column; gap:2px;">';
  d.checks.forEach(c => {
    html += `<div style="display:flex; gap:10px; padding:8px 4px; border-bottom:1px solid var(--border, #eee5d2);">
      <div style="width:16px; text-align:center; flex-shrink:0;">${iconFor(c.status)}</div>
      <div>
        <div style="font-weight:500; font-size:14px;">${escapeHtml(nice(c.check))}</div>
        <div style="font-size:12px; color:var(--text-soft); margin-top:2px;">${escapeHtml(c.message)}</div>
      </div>
    </div>`;
  });
  html += '</div>';
  if (d.ts) {
    html += `<div style="font-size:11px; color:var(--text-faint); margin-top:10px;">Last checked ${new Date(d.ts).toLocaleString()}</div>`;
  }
  body.innerHTML = html;
}

document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const m = document.getElementById('health-modal');
    if (m && m.style.display === 'flex') closeHealthModal();
  }
});
document.addEventListener('click', function(e) {
  const m = document.getElementById('health-modal');
  if (m && m.style.display === 'flex' && e.target === m) closeHealthModal();
});

// ── UI cleanup: collapsible cards + View menu (show/hide per card) ─────────

// Per-card UX defaults. Cards not listed default to collapsed=false, visible=true.
const CARD_DEFAULTS = {
  today:       { collapsed: false, visible: true },
  profile:     { collapsed: true,  visible: true },   // summary only by default
  plan:        { collapsed: true,  visible: true },
  jobs:        { collapsed: true,  visible: true },
  heatmap:     { collapsed: true,  visible: true },
  donut:       { collapsed: true,  visible: true },
  'last-active': { collapsed: true, visible: true },
  timeline:    { collapsed: true,  visible: false },  // hidden by default
  attention:   { collapsed: true,  visible: true },
  apps:        { collapsed: true,  visible: false },
  ai:          { collapsed: true,  visible: false },
};

function getCardStates() {
  let s;
  try { s = JSON.parse(localStorage.getItem('wp_card_states') || '{}'); }
  catch (e) { s = {}; }
  return s;
}
function saveCardStates(s) {
  localStorage.setItem('wp_card_states', JSON.stringify(s));
}
function cardState(id) {
  const saved = getCardStates()[id];
  const def = CARD_DEFAULTS[id] || { collapsed: false, visible: true };
  return Object.assign({}, def, saved || {});
}

function toggleCard(id, ev) {
  if (ev) ev.stopPropagation();
  const el = document.querySelector(`[data-card="${id}"]`);
  if (!el) return;
  el.classList.toggle('is-collapsed');
  const states = getCardStates();
  states[id] = Object.assign({}, states[id] || {}, {
    collapsed: el.classList.contains('is-collapsed'),
  });
  saveCardStates(states);
}

function setCardVisibility(id, visible) {
  const el = document.querySelector(`[data-card="${id}"]`);
  if (el) el.classList.toggle('hidden', !visible);
  const states = getCardStates();
  states[id] = Object.assign({}, states[id] || {}, { visible: !!visible });
  saveCardStates(states);
}

function applyCardStates() {
  document.querySelectorAll('[data-card]').forEach(el => {
    const id = el.dataset.card;
    const s = cardState(id);
    el.classList.toggle('is-collapsed', !!s.collapsed);
    el.classList.toggle('hidden', s.visible === false);
  });
}

// View menu — dropdown with checkboxes per card
function openViewMenu(ev) {
  ev.stopPropagation();
  closeViewMenu();
  const cards = Array.from(document.querySelectorAll('[data-card]'));
  const menu = document.createElement('div');
  menu.className = 'view-menu';
  const rect = ev.target.getBoundingClientRect();
  menu.style.top  = (rect.bottom + 6) + 'px';
  menu.style.right = (window.innerWidth - rect.right) + 'px';

  let html = '<div style="padding:8px 14px; font-size:11px; color:var(--text-soft); text-transform:uppercase; letter-spacing:1.2px;">Show / hide cards</div>';
  cards.forEach(el => {
    const id = el.dataset.card;
    const label = el.dataset.cardLabel || id;
    const checked = cardState(id).visible !== false;
    html += `<label class="view-item">
      <input type="checkbox" ${checked ? 'checked' : ''}
             onchange="setCardVisibility('${id}', this.checked)" />
      <span>${escapeHtml(label)}</span>
    </label>`;
  });
  html += '<div class="view-sep"></div>';
  html += `<div class="view-action" onclick="expandAllCards()">Expand all</div>`;
  html += `<div class="view-action" onclick="collapseAllCards()">Collapse all</div>`;
  html += `<div class="view-action" onclick="resetCardLayout()">Reset to defaults</div>`;
  menu.innerHTML = html;
  document.body.appendChild(menu);

  setTimeout(() => {
    function onDocClick(e) {
      if (!menu.contains(e.target)) {
        closeViewMenu();
        document.removeEventListener('click', onDocClick);
      }
    }
    document.addEventListener('click', onDocClick);
  }, 50);
}
function closeViewMenu() {
  document.querySelectorAll('.view-menu').forEach(el => el.remove());
}
function expandAllCards() {
  document.querySelectorAll('[data-card]').forEach(el => el.classList.remove('is-collapsed'));
  const states = getCardStates();
  document.querySelectorAll('[data-card]').forEach(el => {
    const id = el.dataset.card;
    states[id] = Object.assign({}, states[id] || {}, { collapsed: false });
  });
  saveCardStates(states);
  closeViewMenu();
}
function collapseAllCards() {
  document.querySelectorAll('[data-card]').forEach(el => el.classList.add('is-collapsed'));
  const states = getCardStates();
  document.querySelectorAll('[data-card]').forEach(el => {
    const id = el.dataset.card;
    states[id] = Object.assign({}, states[id] || {}, { collapsed: true });
  });
  saveCardStates(states);
  closeViewMenu();
}
function resetCardLayout() {
  localStorage.removeItem('wp_card_states');
  applyCardStates();
  closeViewMenu();
}

// Apply card states on initial load
document.addEventListener('DOMContentLoaded', applyCardStates);
// Also apply right now in case DOMContentLoaded already fired
applyCardStates();

// ── v2 personal: small padlock in topbar + modal on click ──────────────────

let personalState = null;

async function fetchPersonal() {
  try {
    const r = await fetch('/api/v2/personal/status');
    personalState = await r.json();
    updatePersonalLockIcon(personalState);
  } catch (e) {
    // Silent — the padlock just stays in its locked state
  }
}

function updatePersonalLockIcon(s) {
  const btn = document.getElementById('personal-lock-btn');
  if (!btn || !s) return;
  if (s.unlocked) {
    btn.textContent = '🔓';
    btn.style.opacity = '1';
    btn.title = 'Personal (unlocked, click to lock)';
  } else if (s.password_set) {
    btn.textContent = '🔒';
    btn.style.opacity = '0.6';
    btn.title = 'Personal (locked, click to unlock)';
  } else {
    btn.textContent = '🔒';
    btn.style.opacity = '0.35';
    btn.title = 'Personal — click to set a password';
  }
}

function openPersonalModal() {
  // If already unlocked, fast-lock instead of opening the modal
  if (personalState && personalState.unlocked) {
    personalLock();
    return;
  }
  document.getElementById('personal-modal').style.display = 'flex';
  renderPersonalModal();
  // Focus the password input after render
  setTimeout(() => {
    const inp = document.getElementById('personal-pwd') ||
                document.getElementById('personal-new-pwd');
    if (inp) inp.focus();
  }, 50);
}

function closePersonalModal() {
  document.getElementById('personal-modal').style.display = 'none';
}

function renderPersonalModal() {
  const body = document.getElementById('personal-modal-body');
  const s = personalState || {password_set: false, unlocked: false};
  if (!s.password_set) {
    body.innerHTML = `
      <p style="font-size:13px; color:var(--text-soft); margin:0 0 12px 0;">
        Set a password to protect your personal browsing (banking, healthcare,
        personal email, social, etc.). Minimum 4 characters.
      </p>
      <form onsubmit="personalSetPassword(event); return false;" style="display:flex; gap:8px;">
        <input type="password" id="personal-new-pwd" placeholder="new password"
               style="flex:1; padding:8px 12px; border:1px solid var(--border, #ddd); border-radius:6px; font-size:14px;" />
        <button type="submit"
                style="padding:8px 14px; border:0; border-radius:6px; background:#6b7280; color:#fff; font-weight:600; cursor:pointer;">
          Set
        </button>
      </form>`;
    return;
  }
  if (!s.unlocked) {
    body.innerHTML = `
      <p style="font-size:13px; color:var(--text-soft); margin:0 0 12px 0;">
        Your personal browsing is hidden from the dashboard. Enter your password
        to view what's been visited today.
      </p>
      <form onsubmit="personalUnlock(event); return false;" style="display:flex; gap:8px;">
        <input type="password" id="personal-pwd" placeholder="password" autocomplete="current-password"
               style="flex:1; padding:8px 12px; border:1px solid var(--border, #ddd); border-radius:6px; font-size:14px;" />
        <button type="submit"
                style="padding:8px 14px; border:0; border-radius:6px; background:#6b7280; color:#fff; font-weight:600; cursor:pointer;">
          Unlock
        </button>
      </form>
      <div id="personal-err" style="margin-top:8px; font-size:12px; color:#dc2626;"></div>`;
    return;
  }
  // Unlocked — fetch the data
  body.innerHTML = '<div class="empty">Loading…</div>';
  fetch('/api/v2/personal/data').then(r => r.json()).then(d => {
    let html = '<div style="font-size:12px; color:var(--text-soft); margin-bottom:10px;">Auto-locks in 30 min · session-only</div>';
    if (d.domains_today && d.domains_today.length) {
      html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:6px;">Private-tier domains today:</div>';
      html += '<ul style="margin:0 0 14px 18px; padding:0;">';
      d.domains_today.forEach(it => {
        html += `<li style="margin-bottom:3px;"><strong>${escapeHtml(it.domain)}</strong> · ${it.visits} visit${it.visits === 1 ? '' : 's'}</li>`;
      });
      html += '</ul>';
    } else {
      html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:14px;">No private-tier browsing recorded today.</div>';
    }
    html += `<button onclick="personalLock()" style="padding:6px 14px; border:0; border-radius:6px; background:var(--border, #ddd); color:var(--text); font-size:13px; cursor:pointer;">Lock now</button>`;
    body.innerHTML = html;
  });
}

async function personalSetPassword(ev) {
  if (ev) ev.preventDefault();
  const pwd = document.getElementById('personal-new-pwd').value;
  if (!pwd) return false;
  const r = await fetch('/api/v2/personal/set-password', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pwd}),
  });
  if (!r.ok) {
    const d = await r.json();
    alert('Failed: ' + (d.error || r.status));
    return false;
  }
  await fetchPersonal();
  renderPersonalModal();
  return false;
}

async function personalUnlock(ev) {
  if (ev) ev.preventDefault();
  const pwd = document.getElementById('personal-pwd').value;
  const err = document.getElementById('personal-err');
  if (!pwd) return false;
  const r = await fetch('/api/v2/personal/unlock', {
    method: 'POST',
    headers: {'Content-Type': 'application/json'},
    body: JSON.stringify({password: pwd}),
  });
  if (!r.ok) {
    if (err) err.textContent = 'wrong password';
    return false;
  }
  await fetchPersonal();
  renderPersonalModal();
  if (typeof fetchToday === 'function') fetchToday();
  return false;
}

async function personalLock() {
  await fetch('/api/v2/personal/lock', {method: 'POST'});
  await fetchPersonal();
  closePersonalModal();
  if (typeof fetchToday === 'function') fetchToday();
}

// Close modal on Escape or click outside
document.addEventListener('keydown', function(e) {
  if (e.key === 'Escape') {
    const m = document.getElementById('personal-modal');
    if (m && m.style.display === 'flex') closePersonalModal();
  }
});
document.addEventListener('click', function(e) {
  const m = document.getElementById('personal-modal');
  if (m && m.style.display === 'flex' && e.target === m) closePersonalModal();
});

// ── v2 capture bar: post → POST /api/v2/capture → refresh ──────────────────

async function submitCapture(ev) {
  if (ev) ev.preventDefault();
  const input = document.getElementById('capture-input');
  const status = document.getElementById('capture-status');
  const body = (input.value || '').trim();
  if (!body) return false;
  status.textContent = 'capturing…';
  status.style.color = 'var(--text-soft)';
  try {
    const r = await fetch('/api/v2/capture', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({body: body, pin: 'auto'}),
    });
    if (!r.ok) throw new Error('http ' + r.status);
    const d = await r.json();
    input.value = '';
    const pinNote = d.pinned_kind ? ` (pinned to ${d.pinned_kind})` : '';
    status.textContent = '✓ captured' + pinNote;
    status.style.color = '#16a34a';
    // Refresh the day panels so the new capture surfaces immediately
    if (typeof fetchToday === 'function') fetchToday();
    setTimeout(() => { status.textContent = ''; }, 4000);
  } catch (e) {
    status.textContent = 'failed';
    status.style.color = '#dc2626';
  }
  return false;
}

// Keyboard shortcut: Cmd/Ctrl+Shift+K focuses the capture box
document.addEventListener('keydown', function(e) {
  if ((e.metaKey || e.ctrlKey) && e.shiftKey && e.key && e.key.toLowerCase() === 'k') {
    e.preventDefault();
    const el = document.getElementById('capture-input');
    if (el) el.focus();
  }
});

// ── v2 brain layer: "Who you are" card (the living profile, Step A) ────────

let profileState = null;

async function fetchProfile() {
  try {
    const r = await fetch('/api/v2/profile');
    if (!r.ok) throw new Error('http ' + r.status);
    profileState = await r.json();
    renderProfile(profileState);
  } catch (e) {
    document.getElementById('profile-panel').innerHTML =
      '<div class="empty">Could not load the profile.</div>';
  }
}

function renderProfile(d) {
  const panel = document.getElementById('profile-panel');
  const meta  = document.getElementById('profile-meta');
  const summary = document.getElementById('profile-summary');
  if (!d || !d.exists) {
    panel.innerHTML =
      `<div class="empty">No profile yet. Run <code>python -m workpulse.core.about_george update</code>, or wait for tonight&rsquo;s dream cycle.</div>`;
    meta.textContent = '';
    if (summary) summary.textContent = 'No profile yet.';
    return;
  }
  const fm = d.frontmatter || {};
  const updated = fm.last_updated ? new Date(fm.last_updated).toLocaleString() : '';
  const window  = fm.window || '';
  const hours   = fm.total_tracked_hours;
  const bits = [];
  if (window) bits.push(window);
  if (hours !== undefined) bits.push(`${hours} h tracked`);
  if (updated) bits.push(`updated ${updated}`);
  meta.textContent = bits.join('  ·  ');

  // One-line summary for collapsed state: pull the first paragraph from
  // the Identity section, truncated.
  if (summary) {
    const body = d.body || '';
    const m = body.match(/##\\s+Identity\\s*\\n+([^\\n#]+)/);
    if (m) {
      let line = m[1].trim().replace(/\*\*/g, '');
      if (line.length > 180) line = line.slice(0, 180) + '…';
      summary.textContent = line;
    } else if (hours !== undefined) {
      summary.textContent = `${hours} h over ${window || 'recent days'}.`;
    } else {
      summary.textContent = '';
    }
  }

  // Render the markdown body. Light renderer: section headers + paragraphs +
  // bullet lists. Avoid pulling in a markdown library; the profile shape is
  // controlled and predictable.
  const body = d.body || '';
  panel.innerHTML = renderProfileMarkdown(body);
}

function renderProfileMarkdown(md) {
  // Split by ## headings into sections, render each
  const sections = [];
  let cur = { heading: '', lines: [] };
  md.split('\\n').forEach(line => {
    const m = line.match(/^##\s+(.+?)\s*$/);
    if (m) {
      if (cur.heading || cur.lines.length) sections.push(cur);
      cur = { heading: m[1].trim(), lines: [] };
    } else if (line.match(/^#\s/)) {
      // ignore the title (already in the eyebrow)
    } else {
      cur.lines.push(line);
    }
  });
  if (cur.heading || cur.lines.length) sections.push(cur);

  let html = '<div style="display:flex; flex-direction:column; gap:14px;">';
  sections.forEach(s => {
    if (!s.heading && !s.lines.some(l => l.trim())) return;
    html += '<div>';
    if (s.heading) {
      html += `<div style="font-size:13px; color:var(--text-soft); font-weight:600; margin-bottom:6px;">${escapeHtml(s.heading)}</div>`;
    }
    // Inline render: bullets → ul; ### → bold; blank → paragraph
    let inUl = false;
    const out = [];
    const flushUl = () => { if (inUl) { out.push('</ul>'); inUl = false; } };
    s.lines.forEach(rawLine => {
      const line = rawLine.replace(/\s+$/, '');
      const sub = line.match(/^###\s+(.+?)$/);
      const bullet = line.match(/^[-*]\s+(.+)$/);
      if (sub) {
        flushUl();
        out.push(`<div style="font-weight:600; margin-top:6px;">${renderInline(sub[1])}</div>`);
      } else if (bullet) {
        if (!inUl) { out.push('<ul style="margin:0 0 0 18px; padding:0;">'); inUl = true; }
        out.push(`<li style="margin-bottom:3px;">${renderInline(bullet[1])}</li>`);
      } else if (line.trim() === '') {
        flushUl();
        out.push('');
      } else {
        flushUl();
        out.push(`<p style="margin:6px 0;">${renderInline(line)}</p>`);
      }
    });
    flushUl();
    html += out.filter(s => s !== undefined).join('\\n');
    html += '</div>';
  });
  html += '</div>';
  return html;
}

function renderInline(text) {
  // **bold**, *italic*, `code`, basic safety
  return escapeHtml(text)
    .replace(/\*\*([^*]+)\*\*/g, '<strong>$1</strong>')
    .replace(/`([^`]+)`/g, '<code style="background:var(--bg-soft, #f5efe2); padding:1px 5px; border-radius:3px; font-size:90%;">$1</code>')
    .replace(/\[([0-9]{4}-[0-9]{2}-[0-9]{2})\]/g, '<span style="color:var(--text-faint); font-family:var(--font-mono,monospace); font-size:90%;">[$1]</span>');
}

// ── v2 brain view: "What you worked on today" card ──────────────────────────

let todayState = null;  // last response from GET /api/v2/today

async function fetchToday() {
  try {
    const r = await fetch('/api/v2/today');
    if (!r.ok) throw new Error('http ' + r.status);
    todayState = await r.json();
    renderToday(todayState);
  } catch (e) {
    document.getElementById('today-panel').innerHTML =
      '<div class="empty">Could not load the brain view. The v2 SQLite store may not be initialized yet. Run <code>python -m workpulse.core.db init</code> from the repo.</div>';
  }
}

function renderToday(d) {
  const panel = document.getElementById('today-panel');
  const meta  = document.getElementById('today-meta');
  const summary = document.getElementById('today-summary');
  if (!d) { panel.innerHTML = '<div class="empty">No data.</div>'; return; }
  meta.textContent = `${d.date} · ${d.total_hours.toFixed(1)} h tracked`;

  // One-line summary used when the card is collapsed
  if (summary) {
    if (d.total_hours < 0.05) {
      summary.textContent = `Nothing tracked yet today.`;
    } else {
      const top = (d.by_stream || []).slice(0, 3)
        .map(s => `${s.stream} ${s.hours.toFixed(1)}h`)
        .join(' · ');
      summary.textContent = top ? `${d.total_hours.toFixed(1)}h · ${top}` :
                                  `${d.total_hours.toFixed(1)}h tracked`;
    }
  }

  if (d.total_hours < 0.05) {
    panel.innerHTML =
      '<div class="empty">Nothing tracked yet today. Open something, or run <code>python -m workpulse.core.capture "thought" --auto-pin</code> to capture a note.</div>';
    return;
  }

  let html = '';

  // Stream bar: a single segmented bar showing per-stream hours
  if (d.by_stream && d.by_stream.length) {
    const total = d.total_hours || 0.001;
    html += '<div style="margin: 10px 0 16px 0;">';
    html += '<div style="display:flex; height:14px; border-radius:6px; overflow:hidden; background:var(--bg-soft, #f5efe2);">';
    d.by_stream.forEach((s, i) => {
      const pct = Math.max(2, Math.round((s.hours / total) * 100));
      const color = todayStreamColor(s.stream, i);
      html += `<div title="${escapeHtml(s.stream)}: ${s.hours.toFixed(1)} h" style="width:${pct}%; background:${color};"></div>`;
    });
    html += '</div>';
    html += '<div style="display:flex; flex-wrap:wrap; gap:14px; margin-top:8px; font-size:12px;">';
    d.by_stream.forEach((s, i) => {
      const color = todayStreamColor(s.stream, i);
      html += `<span style="display:inline-flex; align-items:center; gap:6px;"><span style="display:inline-block; width:10px; height:10px; background:${color}; border-radius:2px;"></span><strong>${escapeHtml(s.stream)}</strong> ${s.hours.toFixed(1)} h</span>`;
    });
    html += '</div></div>';
  }

  // Top named clusters — each with a side-channel context line (git + captures + skill runs)
  if (d.top_clusters && d.top_clusters.length) {
    html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:6px;">Top clusters today</div>';
    html += '<div style="display:flex; flex-direction:column; gap:8px; margin-bottom:14px;">';
    d.top_clusters.slice(0, 5).forEach(c => {
      const name = c.name ? escapeHtml(c.name) : '<em style="color:var(--text-faint);">(unnamed cluster)</em>';
      const oneliner = c.one_liner ? ` — ${escapeHtml(c.one_liner)}` : '';

      // Fix 1.7: project assignment + confidence badge + correction dropdown
      let assignBadge = '';
      const label = c.assigned_label || c.assigned_stream || '—';
      const conf = c.assignment_confidence;
      const src = c.assignment_source;
      let badgeBg = '#e5e7eb';
      let badgeFg = '#374151';
      let confText = '';
      if (src === 'user') {
        badgeBg = '#dcfce7'; badgeFg = '#166534';
        confText = '· user';
      } else if (src === 'agent') {
        badgeBg = conf >= 0.7 ? '#dbeafe' : (conf >= 0.4 ? '#fef3c7' : '#fee2e2');
        badgeFg = conf >= 0.7 ? '#1e40af' : (conf >= 0.4 ? '#92400e' : '#991b1b');
        confText = '· ' + (conf * 100).toFixed(0) + '%';
      } else if (src === 'fallback') {
        confText = '· fallback';
      } else {
        confText = '· not assigned';
      }
      assignBadge = `<span style="display:inline-block; padding:2px 8px; background:${badgeBg}; color:${badgeFg}; border-radius:10px; font-size:11px; font-weight:600; cursor:pointer;" onclick="openCorrection('${escapeHtml(c.cluster_id)}', '${escapeHtml(c.assigned_stream || '')}', this)" title="Click to correct">${escapeHtml(label)} ${escapeHtml(confText)}</span>`;

      // Context line: "what was actually happening" — git + captures + brain calls
      let ctxLine = '';
      if (c.summary) {
        ctxLine = `<div style="margin-top:4px; font-size:12px; color:var(--text-soft);">↳ ${escapeHtml(c.summary)}</div>`;
      }
      // Top commit subjects (one-line each, max 3)
      let commitsLine = '';
      if (c.top_commits && c.top_commits.length) {
        commitsLine = '<ul style="margin:6px 0 0 18px; padding:0; font-size:12px; color:var(--text-soft);">';
        c.top_commits.forEach(g => {
          commitsLine += `<li><span style="font-family:var(--font-mono,monospace); color:var(--text-faint);">${escapeHtml(g.sha)}</span> ${escapeHtml(g.subject)}</li>`;
        });
        commitsLine += '</ul>';
      }

      html += `<div data-cluster="${escapeHtml(c.cluster_id)}" style="padding:10px 12px; background:var(--surface, #fff); border:1px solid var(--border, #eee5d2); border-radius:6px;">
        <div style="display:flex; align-items:baseline; justify-content:space-between; gap:12px;">
          <div><strong>${name}</strong>${oneliner}</div>
          <div style="text-align:right; white-space:nowrap;"><span style="font-variant-numeric:tabular-nums;">${c.hours.toFixed(1)} h</span> &nbsp;${assignBadge}</div>
        </div>
        ${ctxLine}
        ${commitsLine}
      </div>`;
    });
    html += '</div>';
  }

  // Plan vs actual (if any flagged items today)
  if (d.plan_vs_actual && d.plan_vs_actual.length) {
    html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:6px;">Plan vs actual</div><ul style="margin:0 0 14px 18px; padding:0;">';
    d.plan_vs_actual.forEach(it => {
      const flagColor = (it.flag === 'overrun') ? '#d97706'
                       : (it.flag === 'underrun') ? '#dc2626'
                       : 'var(--text-soft)';
      html += `<li style="margin-bottom:4px;"><span style="color:${flagColor}; font-weight:600;">[${escapeHtml(it.flag)}]</span> ${escapeHtml(it.name)} — planned ${it.planned_min}m, actual ${it.actual_min}m</li>`;
    });
    html += '</ul>';
  }

  // Captures
  if (d.captures && d.captures.length) {
    html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:6px;">Captures today</div>';
    html += '<ul style="margin:0 0 14px 18px; padding:0;">';
    d.captures.forEach(c => {
      const t = c.time ? `<span style="color:var(--text-faint); font-variant-numeric:tabular-nums; margin-right:8px;">${escapeHtml(c.time)}</span>` : '';
      const author = c.author === 'system' ? '<span style="color:var(--text-faint); font-size:11px; margin-left:6px;">(system)</span>' : '';
      html += `<li style="margin-bottom:4px;">${t}${escapeHtml(c.body)}${author}</li>`;
    });
    html += '</ul>';
  } else {
    html += '<div style="font-size:13px; color:var(--text-soft); margin-bottom:14px;">No captures today. Run <code>python -m workpulse.core.capture "thought" --auto-pin</code> to add one.</div>';
  }

  // Untagged buckets — surface only the largest one as a one-liner
  if (d.untagged_buckets && d.untagged_buckets.length) {
    const top = d.untagged_buckets[0];
    if (top.minutes >= 5) {
      html += `<div style="font-size:12px; color:var(--text-soft); padding:6px 10px; background:var(--bg-warm, #fff6e0); border-radius:6px;">⚠️ ${top.minutes.toFixed(0)} min of untagged "${escapeHtml(top.sample_titles && top.sample_titles[0] || top.token)}" — want to tag it?</div>`;
    }
  }

  panel.innerHTML = html;
}

// ── Fix 1.7 correction UI: dropdown opens on badge click ────────────────────
let _streamsCache = null;
async function _getStreams() {
  if (_streamsCache) return _streamsCache;
  try {
    const r = await fetch('/api/v2/streams');
    const d = await r.json();
    _streamsCache = d.streams || [];
  } catch (e) {
    _streamsCache = [];
  }
  return _streamsCache;
}

async function openCorrection(clusterId, currentStream, anchor) {
  // Remove any existing popovers
  document.querySelectorAll('.wp-correction-popover').forEach(el => el.remove());
  const streams = await _getStreams();
  const rect = anchor.getBoundingClientRect();
  const pop = document.createElement('div');
  pop.className = 'wp-correction-popover';
  pop.style.cssText = `position:fixed; top:${rect.bottom + 6}px; left:${rect.left}px;
    background:var(--surface, #fff); border:1px solid var(--border, #ddd0b3);
    border-radius:8px; padding:8px; box-shadow:0 4px 20px rgba(0,0,0,0.15);
    z-index:9999; min-width:200px; max-height:300px; overflow:auto;`;
  let html = '<div style="font-size:11px; color:var(--text-soft); margin-bottom:6px; padding:0 4px;">Set this cluster to:</div>';
  streams.forEach(s => {
    const isCurrent = s.key === currentStream;
    html += `<div style="padding:6px 10px; cursor:pointer; border-radius:4px; ${isCurrent ? 'background:var(--bg-soft, #f5efe2); font-weight:600;' : ''}"
                onmouseover="this.style.background='var(--bg-warm, #fff6e0)'"
                onmouseout="this.style.background='${isCurrent ? 'var(--bg-soft, #f5efe2)' : 'transparent'}'"
                onclick="correctCluster('${clusterId}', '${s.key}', this)">
       <span style="font-weight:600;">${escapeHtml(s.label || s.key)}</span>
       <span style="color:var(--text-faint); font-size:11px; margin-left:6px;">${escapeHtml(s.key)}</span>
     </div>`;
  });
  pop.innerHTML = html;
  document.body.appendChild(pop);
  // Click outside to close
  setTimeout(() => {
    function onDocClick(e) {
      if (!pop.contains(e.target)) {
        pop.remove();
        document.removeEventListener('click', onDocClick);
      }
    }
    document.addEventListener('click', onDocClick);
  }, 50);
}

async function correctCluster(clusterId, toStream, anchor) {
  try {
    const r = await fetch(`/api/v2/cluster/${clusterId}/correct`, {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({to_stream: toStream}),
    });
    if (!r.ok) throw new Error('http ' + r.status);
    // Close popover + refresh
    document.querySelectorAll('.wp-correction-popover').forEach(el => el.remove());
    if (typeof fetchToday === 'function') fetchToday();
  } catch (e) {
    alert('Correction failed: ' + e.message);
  }
}

// Stable color per stream (matches v1 tinting where possible)
function todayStreamColor(stream, i) {
  const palette = ['#3866d0', '#bc38d0', '#d08838', '#38d09a', '#d03866', '#7a7a7a'];
  if (stream === '<untagged>' || !stream) return '#bbb';
  // Deterministic hash → palette
  let h = 0;
  for (let k = 0; k < stream.length; k++) h = (h * 31 + stream.charCodeAt(k)) >>> 0;
  return palette[h % palette.length];
}

let planState = null;  // last response from GET /api/plans/today

async function fetchPlan() {
  try {
    const r = await fetch('/api/plans/today');
    const d = await r.json();
    planState = d;
    renderPlan(d);
  } catch (e) {
    document.getElementById('plan-panel').innerHTML =
      '<div class="empty">Could not load plan.</div>';
  }
}

// Work-type emoji — picked from item name keywords with a sensible default.
// Keyword groups intentionally ordered: the FIRST hit wins, so put the more
// specific ones first (e.g. "model" before "data").
const WORK_ICONS = [
  [/\\b(report|memo|narrative|brief|letter|draft)/i, '📝'],
  [/\\b(call|meeting|sync|standup|huddle|interview)/i, '🗣'],
  [/\\b(review|read|skim|notes?)\\b/i, '📖'],
  [/\\b(research|study|baseline|survey|literature)/i, '🔬'],
  [/\\b(code|build|implement|ship|deploy|refactor|debug)/i, '💻'],
  [/\\b(model|forecast|reforecast|projection)/i, '📈'],
  [/\\b(data|analysis|analyse|analyze|crunch|excel|sheet|kpi)/i, '📊'],
  [/\\b(plan|roadmap|strategy|scoping|design)/i, '🗺'],
  [/\\b(email|inbox|reply|respond|follow.?up)/i, '📧'],
  [/\\b(slide|deck|presentation|pitch)/i, '🎤'],
  [/\\b(carbon|climate|sustainab)/i, '🌱'],
  [/\\b(workpulse|feature|coach|jobs?|plan)/i, '🛠'],
  [/\\b(client|customer|stakeholder)/i, '🤝'],
];
function workIcon(name) {
  for (const [re, ic] of WORK_ICONS) { if (re.test(name)) return ic; }
  return '✦';
}

// Time-of-day-aware greeting + matching icon.
function greetingForNow() {
  const h = new Date().getHours();
  if (h < 5)  return { icon: '🌙', text: 'Working late' };
  if (h < 12) return { icon: '☀️', text: 'Good morning' };
  if (h < 17) return { icon: '🌤️', text: 'Good afternoon' };
  if (h < 21) return { icon: '🌇', text: 'Good evening' };
  return { icon: '🌙', text: 'Late shift' };
}

// Primary CTA: if no plan today → "Plan your day". If plan exists → "Edit plan".
function refreshHero() {
  const g = greetingForNow();
  document.getElementById('hero-icon').textContent = g.icon;
  document.getElementById('hero-greeting-text').textContent = g.text;
  const hasPlan = planState && planState.exists && (planState.items || []).length > 0;
  document.getElementById('hero-primary-icon').textContent = hasPlan ? '✎' : '🌅';
  document.getElementById('hero-primary-label').textContent = hasPlan ? 'Edit plan' : 'Plan your day';
}
function heroPrimaryAction() { openPlanModal(); }

function renderPlan(p) {
  const panel = document.getElementById('plan-panel');
  const btn = document.getElementById('plan-action-btn');
  refreshHero();
  if (!p || !p.exists || !p.items || p.items.length === 0) {
    btn.textContent = '+ Plan your day';
    panel.innerHTML = '<div class="empty">No plan yet for today. ' +
      'Take 30 seconds to declare what you\\'re working on — ' +
      'each item becomes a Job that sessions roll up to.</div>';
    return;
  }
  btn.textContent = 'Edit plan';
  const carried = p.items.filter(it => it.section === 'carried');
  const fresh   = p.items.filter(it => it.section !== 'carried');
  const done    = p.items.filter(it => it.done).length;
  const html = [];

  // Top-line summary: actual vs planned for the whole day
  const actual = p.actual_minutes_today || 0;
  const planned = p.planned_minutes || 0;
  let summary = `${done} of ${p.items.length} done`;
  if (actual > 0 || planned > 0) {
    summary += ` · ${fmtMins(actual)} logged` +
      (planned > 0 ? ` of ${fmtMins(planned)} planned` : '');
  }
  html.push(`<div class="sub" style="margin-bottom:14px">${summary}</div>`);

  function gridFor(items, label) {
    if (!items.length) return '';
    return `
      <div class="plan-section-label">${label}</div>
      <div class="plan-grid">
        ${items.map(it => renderPlanItem(it, p.items.indexOf(it))).join('')}
      </div>`;
  }
  html.push(gridFor(carried, 'Carried over'));
  html.push(gridFor(fresh, 'New today'));
  panel.innerHTML = html.join('');
}

function renderPlanItem(it, idx) {
  const checked = it.done ? 'checked' : '';
  const cls = it.done ? 'plan-item done' : 'plan-item';

  const planned = it.planned_minutes || 0;
  const actual  = it.actual_minutes_today || 0;

  // Stream color: pulled from systemSnapshot.streams (already fetched).
  let streamColor = '#6b7280';
  if (it.stream && systemSnapshot && systemSnapshot.streams) {
    const found = systemSnapshot.streams.find(s => s.key === it.stream);
    if (found) streamColor = found.color;
  }

  const ic = workIcon(it.name);

  // Time line: "45m / 2h planned" or "45m planned" or "45m logged".
  let timeLine = '';
  if (planned > 0 && actual > 0) {
    timeLine = `<span class="actual">${fmtMins(actual)}</span> / ${fmtMins(planned)}`;
  } else if (planned > 0) {
    timeLine = `${fmtMins(planned)} planned`;
  } else if (actual > 0) {
    timeLine = `<span class="actual">${fmtMins(actual)}</span> logged`;
  }

  // Progress bar — only when there's a planned target.
  let barHTML = '';
  if (planned > 0) {
    const pct = Math.min(100, Math.round((actual / planned) * 100));
    const over = actual > planned;
    const cls2 = over ? 'plan-bar plan-bar-over' : 'plan-bar';
    barHTML = `<div class="plan-bar-wrap" title="${fmtMins(actual)} of ${fmtMins(planned)} planned">
      <div class="${cls2}" style="width:${pct}%"></div>
    </div>`;
  }

  const streamLabel = it.stream ? `<div class="plan-item-stream">${escapeHtml(it.stream)}</div>` : '';

  return `
    <div class="${cls}" style="--stream-color:${streamColor}">
      <div class="plan-item-top">
        <div class="plan-item-icon">${ic}</div>
        <div class="plan-item-body">
          <div class="plan-item-name">${escapeHtml(it.name)}</div>
          ${streamLabel}
        </div>
        <input class="plan-item-check" type="checkbox" ${checked} onchange="togglePlanItem(${idx})" />
      </div>
      ${barHTML}
      <div class="plan-item-bottom">
        <span class="plan-item-time">${timeLine || '<span style="color:var(--text-faint)">no target set</span>'}</span>
      </div>
    </div>`;
}

async function togglePlanItem(idx) {
  if (!planState || !planState.items || !planState.items[idx]) return;
  // Optimistic UI: flip done locally and re-render immediately so the
  // checkbox + line-through register instantly. We then POST in the
  // background and replace state with the server's authoritative copy
  // (which now includes reconciled actual_minutes_today).
  planState.items[idx].done = !planState.items[idx].done;
  renderPlan(planState);
  try {
    const r = await fetch('/api/plans/today', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ items: planState.items })
    });
    if (r.ok) {
      const fresh = await r.json();
      // Only swap if the server returned a valid plan; otherwise keep the
      // optimistic state and let the next periodic refresh reconcile.
      if (fresh && fresh.items) {
        planState = fresh;
        renderPlan(planState);
      }
    } else {
      // Roll back the optimistic flip on failure.
      planState.items[idx].done = !planState.items[idx].done;
      renderPlan(planState);
      showToast('Could not update plan.');
    }
  } catch (e) {
    planState.items[idx].done = !planState.items[idx].done;
    renderPlan(planState);
    showToast('Could not update plan: ' + e.message);
  }
}

async function openPlanModal() {
  // Load suggestions on each open so paused-jobs list is fresh.
  let sg = { paused: [], yesterday: [], recent: [] };
  try {
    const r = await fetch('/api/plans/suggestions');
    sg = await r.json();
  } catch (e) {}
  const block = document.getElementById('plan-suggestions');
  const sections = [];
  if (sg.paused && sg.paused.length) {
    sections.push('<div class="plan-sg-label">Still in flight, carry over?</div>');
    sg.paused.forEach(j => {
      sections.push(`
        <label class="plan-sg-row">
          <input type="checkbox" data-jobid="${escapeHtml(j.job_id)}"
                 data-name="${escapeHtml(j.name)}"
                 data-stream="${escapeHtml(j.stream || '')}"
                 data-section="carried" />
          <span>${escapeHtml(j.name)}</span>
          <span class="meta">${escapeHtml(j.stream || '')}</span>
        </label>`);
    });
  }
  if (sg.yesterday && sg.yesterday.length) {
    sections.push('<div class="plan-sg-label">Touched yesterday</div>');
    sg.yesterday.forEach(j => {
      sections.push(`
        <label class="plan-sg-row">
          <input type="checkbox" data-jobid="${escapeHtml(j.job_id)}"
                 data-name="${escapeHtml(j.name)}"
                 data-stream="${escapeHtml(j.stream || '')}"
                 data-section="carried" />
          <span>${escapeHtml(j.name)}</span>
          <span class="meta">${escapeHtml(j.stream || '')}</span>
        </label>`);
    });
  }
  if (sg.recent && sg.recent.length) {
    sections.push('<div class="plan-sg-label">Recent (last 7 days)</div>');
    sg.recent.slice(0, 8).forEach(j => {
      sections.push(`
        <label class="plan-sg-row">
          <input type="checkbox" data-jobid="${escapeHtml(j.job_id)}"
                 data-name="${escapeHtml(j.name)}"
                 data-stream="${escapeHtml(j.stream || '')}"
                 data-section="carried" />
          <span>${escapeHtml(j.name)}</span>
          <span class="meta">${escapeHtml(j.last_day || '')}</span>
        </label>`);
    });
  }
  if (sections.length === 0) {
    block.innerHTML = '<div class="sub">No previous jobs to carry over yet. Just add new items below.</div>';
  } else {
    block.innerHTML = sections.join('');
  }
  // Pre-populate the textarea with existing "new" items if a plan already exists today
  const existingNew = (planState && planState.items)
    ? planState.items.filter(it => it.section !== 'carried' && !it.job_id)
                     .map(it => it.planned_minutes
                       ? `${it.name} (~${it.planned_minutes} min)`
                       : it.name)
                     .join('\\n')
    : '';
  document.getElementById('plan-new-items').value = existingNew;
  document.getElementById('plan-modal').classList.add('open');
  setTimeout(() => document.getElementById('plan-new-items').focus(), 60);
}

function closePlanModal() {
  document.getElementById('plan-modal').classList.remove('open');
}

function parsePlanLine(line) {
  // "Foo bar (~30 min)" -> { name: "Foo bar", planned_minutes: 30 }
  const trimmed = line.trim();
  if (!trimmed) return null;
  const m = trimmed.match(/^(.*?)\\s*\\(\\s*~?\\s*(\\d+)\\s*min\\s*\\)\\s*$/i);
  if (m) return { name: m[1].trim(), planned_minutes: parseInt(m[2], 10) };
  return { name: trimmed, planned_minutes: null };
}

async function submitPlan() {
  const items = [];
  // Carried-over (checkbox-selected) suggestions
  document.querySelectorAll('#plan-suggestions input[type="checkbox"]:checked').forEach(cb => {
    items.push({
      name:    cb.dataset.name,
      job_id:  cb.dataset.jobid,
      stream:  cb.dataset.stream || null,
      section: 'carried',
    });
  });
  // New items typed in the textarea
  const raw = document.getElementById('plan-new-items').value || '';
  raw.split('\\n').forEach(line => {
    const it = parsePlanLine(line);
    if (it) items.push({ ...it, section: 'new' });
  });
  if (items.length === 0) {
    showToast('Nothing to save — pick at least one item or type a new one.');
    return;
  }
  try {
    const r = await fetch('/api/plans/today', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ items })
    });
    const d = await r.json();
    if (r.ok) {
      planState = d;
      renderPlan(d);
      closePlanModal();
      showToast('Plan saved — ' + items.length + ' item' + (items.length===1?'':'s'));
      fetchJobs();  // refresh Jobs in flight since new Jobs may have been created
    } else {
      showToast('Error saving plan: ' + (d.error || 'unknown'));
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

// ── Job-name suggestions (v1.2b) ───────────────────────────────────────────

function renderSuggestions(suggestions) {
  const block = document.getElementById('suggestions-block');
  if (!suggestions || suggestions.length === 0) { block.innerHTML = ''; return; }
  block.innerHTML = suggestions.map(s => {
    const cls = s.confidence === 'high' ? '' : 'medium';
    const streamLabel = escapeHtml(s.label || s.stream);
    const name = escapeHtml(s.name);
    const totalMin = fmtMins(s.total_minutes);
    const confLabel = s.confidence === 'high' ? 'strong match' : 'possible';
    return `
      <div class="sg-card ${cls}">
        <div class="sg-bulb">💡</div>
        <div class="sg-body">
          <div class="sg-line1">
            Looks like you've been on a single piece of
            <strong>${streamLabel}</strong> work — call it
            <strong>"${name}"</strong>?
          </div>
          <div class="sg-line2">
            ${totalMin} of activity in the last 4 hours · ${s.session_count} window${s.session_count===1?'':'s'} · ${confLabel}
          </div>
        </div>
        <div class="sg-actions">
          <button class="primary" onclick="acceptSuggestion('${escapeHtml(s.stream)}', '${escapeAttr(name)}')">Accept</button>
          <button onclick="renameSuggestion('${escapeHtml(s.stream)}', '${escapeAttr(name)}')">Rename</button>
          <button onclick="dismissSuggestion('${escapeHtml(s.stream)}', '${escapeAttr(name)}')">Dismiss</button>
        </div>
      </div>`;
  }).join('');
}

function escapeAttr(s) {
  return (s || '').replace(/'/g, "\\'").replace(/"/g, '&quot;');
}

async function acceptSuggestion(stream, name) {
  try {
    const r = await fetch('/api/jobs/suggestions/accept', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ stream, name }),
    });
    const d = await r.json();
    if (r.ok) {
      showToast(`Started: ${name}`);
      await fetchJobs();
    } else {
      showToast('Error: ' + (d.error || 'could not start job'));
    }
  } catch (e) { showToast('Error: ' + e.message); }
}

function renameSuggestion(stream, name) {
  // Open the existing Start-Job modal, pre-filled with the suggested name + stream
  openStartJob();
  setTimeout(() => {
    document.getElementById('sj-name').value = name;
    const sel = document.getElementById('sj-stream');
    if (sel) sel.value = stream;
    document.getElementById('sj-name').focus();
    document.getElementById('sj-name').select();
  }, 80);
}

async function dismissSuggestion(stream, name) {
  try {
    const r = await fetch('/api/jobs/suggestions/dismiss', {
      method: 'POST',
      headers: {'Content-Type': 'application/json'},
      body: JSON.stringify({ stream }),
    });
    if (r.ok) {
      showToast('Suggestion dismissed.');
      await fetchJobs();
    } else {
      showToast('Could not dismiss.');
    }
  } catch (e) { showToast('Error: ' + e.message); }
}

async function resumeJob(id, name) {
  if (!confirm(`Resume "${name}"?\\n\\nStarts a new active job with the same name + stream. The previous job's record is preserved.`)) return;
  try {
    const r = await fetch(`/api/jobs/${id}/resume`, { method: 'POST' });
    const d = await r.json();
    if (r.ok) {
      showToast(`Resumed: ${name}`);
      await fetchJobs();
    } else {
      showToast('Error: ' + (d.error || 'could not resume'));
    }
  } catch (e) {
    showToast('Error: ' + e.message);
  }
}

// ── Job export modal ──────────────────────────────────────────────────────
let _currentExportJobId = null;
let _currentExportName = null;
let _currentExportMarkdown = null;

function openExport(jobId, name) {
  _currentExportJobId = jobId;
  _currentExportName = name;
  _currentExportMarkdown = null;
  document.getElementById('export-title').textContent = 'Export: ' + name;
  document.getElementById('export-sub').textContent =
    'A self-contained markdown digest you can paste into an email, timesheet, or shared note.';
  document.getElementById('export-preview').innerHTML =
    '<div class="export-loading">Generating digest…</div>';
  document.getElementById('export-modal').classList.add('open');
  loadExport();
}

function closeExport() {
  document.getElementById('export-modal').classList.remove('open');
  _currentExportJobId = null;
  _currentExportMarkdown = null;
}

async function loadExport() {
  if (!_currentExportJobId) return;
  const includeTitles = document.getElementById('export-include-titles').checked;
  try {
    const r = await fetch(`/api/jobs/${_currentExportJobId}/export?include_titles=${includeTitles}`);
    if (!r.ok) {
      const err = await r.json().catch(() => ({error:'unknown'}));
      document.getElementById('export-preview').innerHTML =
        '<div class="export-loading">Error: ' + escapeHtml(err.error || 'failed') + '</div>';
      return;
    }
    _currentExportMarkdown = await r.text();
    document.getElementById('export-preview').textContent = _currentExportMarkdown;
  } catch (e) {
    document.getElementById('export-preview').innerHTML =
      '<div class="export-loading">Error: ' + escapeHtml(e.message) + '</div>';
  }
}

document.addEventListener('change', e => {
  if (e.target && e.target.id === 'export-include-titles') loadExport();
});

async function copyExport() {
  if (!_currentExportMarkdown) return;
  try {
    await navigator.clipboard.writeText(_currentExportMarkdown);
    showToast('Copied to clipboard.');
  } catch (e) {
    showToast('Copy failed: ' + e.message);
  }
}

function downloadExport() {
  if (!_currentExportMarkdown) return;
  const safe = (_currentExportName || 'job').replace(/[^A-Za-z0-9._-]+/g, '_').replace(/^_+|_+$/g, '');
  const filename = `${safe}-${_currentExportJobId}.md`;
  const blob = new Blob([_currentExportMarkdown], { type: 'text/markdown;charset=utf-8' });
  const url = URL.createObjectURL(blob);
  const a = document.createElement('a');
  a.href = url; a.download = filename;
  document.body.appendChild(a); a.click(); document.body.removeChild(a);
  URL.revokeObjectURL(url);
  showToast('Downloaded ' + filename);
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
  await Promise.all([fetchHealth(), fetchPersonal(), fetchProfile(), fetchToday(), fetchRealWork(), fetchLastActive(), fetchAI(), fetchJobs(), fetchPlan()]);
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
