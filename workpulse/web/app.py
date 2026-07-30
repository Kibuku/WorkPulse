"""
app.py — WorkPulse local web server + dashboard.

Serves the dashboard at http://localhost:5700
API endpoints used by the dashboard and tray app. The dashboard's static
assets (HTML/CSS/JS) live in web/static/ and are served verbatim; all dynamic
data reaches the page through the /api/* endpoints.

Run standalone:
  python -m workpulse.web.app
"""

from __future__ import annotations

import asyncio
import ipaddress
import io
import json
import re
import subprocess
import sys
from collections import defaultdict
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from typing import Optional
from urllib.parse import urlparse

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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from workpulse.common import load_config, resolve
from workpulse.core import classroom as classroom_core

app = FastAPI(title="WorkPulse", docs_url=None, redoc_url=None)

# Dashboard assets (HTML/CSS/JS) live in web/static/, served verbatim; all
# dynamic data reaches the page through the /api/* endpoints below.
STATIC_DIR = Path(__file__).resolve().parent / "static"
app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

PORT = 5700

# Optional curated colours, keyed by stream slug. Empty by default — every
# stream gets a stable auto-colour from the hash below, so there are no personal
# stream names baked into the code. Add entries here only to pin a specific hue.
STREAM_COLORS: dict[str, str] = {}

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
    return (f"{module}.py" in cmd) or (f"workpulse.signals.{module}" in cmd) \
        or (f"workpulse/signals/{module}.py" in cmd)


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
                                                   "meeting_notes", "chapter3_notes"]):
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
    UI can show 'Acme Web' instead of just 'acme-web'."""
    from workpulse.core import db as wp_db
    cfg = load_config()
    con = wp_db.connect(cfg)
    rows = con.execute(
        "SELECT key, label FROM stream ORDER BY label"
    ).fetchall()
    return {"streams": [{"key": r["key"], "label": r["label"]} for r in rows]}


@app.get("/api/v2/review")
def api_v2_review(request: Request, days: int = 7):
    """Small, privacy-aware inbox of project decisions worth teaching."""
    from workpulse.core import db as wp_db, review as wp_review
    cfg = load_config()
    con = wp_db.connect(cfg)
    return wp_review.inbox(
        con,
        days=max(1, min(days, 30)),
        cfg=cfg,
        include_private=_personal_unlocked(request),
    )


@app.get("/api/v2/workflows/proposal")
def api_v2_workflow_proposal():
    """Learn a proposal method from real local evidence; examples anonymized."""
    from workpulse.core import db as wp_db, workflows as wp_workflows
    cfg = load_config()
    return wp_workflows.learn_proposal_method(wp_db.connect(cfg))


@app.post("/api/v2/workflows/proposal/confirm")
def api_v2_workflow_proposal_confirm():
    """Promote the observed candidate to confirmed personal method memory."""
    from workpulse.core import db as wp_db, workflows as wp_workflows
    cfg = load_config()
    try:
        learned = wp_workflows.confirm_proposal_method(wp_db.connect(cfg))
        return {"ok": True, "workflow": learned}
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


# ── Streams: create / edit / delete from the UI (writes config.yaml) ──────────
# The taxonomy wizard is the pilot's onboarding surface — users build their
# stream tree here instead of hand-editing YAML. config.yaml is the source of
# truth for the taxonomy (workpulse.core.tree reads it); these endpoints keep
# it consistent and human-editable.

def _load_streams_config() -> tuple[dict, "Path"]:
    """Load the config we'll mutate and the path to write it to. On a fresh
    install config.yaml may not exist yet — seed from the effective config
    (which falls back to the bundled example) so the first stream a user adds
    materialises a complete, working config.yaml rather than erroring."""
    import yaml as _yaml
    cfg_path = resolve("config/config.yaml")
    if cfg_path.exists():
        cfg = _yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    else:
        cfg = load_config()  # effective config (falls back to config.example.yaml)
    return cfg, cfg_path


def _write_config(cfg: dict, cfg_path: "Path") -> None:
    import yaml as _yaml
    cfg_path.parent.mkdir(parents=True, exist_ok=True)
    cfg_path.write_text(_yaml.safe_dump(cfg, sort_keys=False, allow_unicode=True),
                        encoding="utf-8")


# ── "recognize by" hints — how a stream is auto-tagged ───────────────────────
# A hint is either a folder (matched against a process's open files, the most
# authoritative signal → watcher.stream_folder_roots) or a keyword (substring-
# matched against window titles / paths → watcher.stream_path_patterns). We
# route by shape so the wizard can offer a single "recognize by" field.

def _is_folder_hint(v: str) -> bool:
    return ("/" in v) or ("\\" in v) or v.startswith("~")


def _recognize_for(key: str, cfg: dict) -> str:
    """First recognize-by hint for a stream, or '' — folder roots first."""
    w = cfg.get("watcher") or {}
    roots = w.get("stream_folder_roots") or {}
    if isinstance(roots, dict):
        lst = roots.get(key) or []
        if lst:
            return str(lst[0])
    for p in (w.get("stream_path_patterns") or []):
        if isinstance(p, dict) and p.get("stream") == key:
            return str(p.get("path") or "")
    return ""


def _clear_recognize(key: str, cfg: dict) -> None:
    """Drop every recognize-by entry for a stream from both structures."""
    w = cfg.get("watcher") or {}
    roots = w.get("stream_folder_roots")
    if isinstance(roots, dict):
        roots.pop(key, None)
    pats = w.get("stream_path_patterns")
    if isinstance(pats, list):
        w["stream_path_patterns"] = [
            p for p in pats if not (isinstance(p, dict) and p.get("stream") == key)
        ]


def _set_recognize(key: str, value: str, cfg: dict) -> None:
    """Replace a stream's recognize-by hint. Empty value just clears it."""
    value = (value or "").strip()
    _clear_recognize(key, cfg)
    if not value:
        return
    w = cfg.setdefault("watcher", {}) or {}
    if not isinstance(w, dict):
        w = cfg["watcher"] = {}
    if _is_folder_hint(value):
        roots = w.get("stream_folder_roots")
        if not isinstance(roots, dict):
            roots = w["stream_folder_roots"] = {}
        roots.setdefault(key, [])
        if value not in roots[key]:
            roots[key].append(value)
    else:
        pats = w.get("stream_path_patterns")
        if not isinstance(pats, list):
            pats = w["stream_path_patterns"] = []
        pats.append({"path": value, "stream": key})


def _ensure_parent_in_config(parent, streams: dict) -> bool:
    """A chosen parent may live only in the DB (migrated from v1, when
    config.yaml's `streams:` was null). If so, pull it into the config tree as a
    top-level stream so a new child has a real parent — instead of rejecting a
    stream the user can plainly see in their list. Returns True when `parent` is
    None or is now a known config stream; False when it exists nowhere.

    Safe against duplication: once the parent is in config, normalise() owns it
    and _streams_payload's DB pass skips it (config wins on key)."""
    if not parent or parent in streams:
        return True
    try:
        from workpulse.core import db as wp_db
        row = wp_db.connect(load_config()).execute(
            "SELECT label FROM stream WHERE key = ?", (parent,)).fetchone()
    except Exception:
        row = None
    if not row:
        return False
    label = row["label"] if row["label"] else parent
    if label == parent:                       # raw key -> a friendlier label
        label = parent.replace("-", " ").replace("_", " ").title()
    streams[parent] = {"label": label}
    return True


@app.post("/api/streams")
async def api_streams_add(payload: dict):
    """Body: {key, label, parent?}. Append a new stream to config.yaml.
      • key must be lowercase letters/digits/hyphens, 2–30 chars, not used.
      • parent (optional) must reference an existing stream — builds the
        stream hierarchy. Omit / null → top-level node.
    Written in the hierarchical {label, parent} shape so the tree primitive
    stays the source of truth."""
    import re as _re
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

    cfg, cfg_path = _load_streams_config()
    streams = cfg.setdefault("streams", {}) or {}
    if not isinstance(streams, dict):
        return JSONResponse({"error": "streams block is malformed in config.yaml"},
                            status_code=500)
    if key in streams:
        return JSONResponse({"error": f"stream '{key}' already exists"}, status_code=409)
    # Accept a parent that lives in config OR only in the DB (migrated v1
    # streams), materializing the latter into config so the child has a home.
    if parent and not _ensure_parent_in_config(parent, streams):
        return JSONResponse({"error": f"parent '{parent}' is not a known stream"},
                            status_code=400)
    streams[key] = {"label": label}
    if parent:
        streams[key]["parent"] = parent
    cfg["streams"] = streams
    recognize = (payload.get("recognize") or "").strip()
    if recognize:
        _set_recognize(key, recognize, cfg)
    _write_config(cfg, cfg_path)
    from workpulse.core.tree import breadcrumb
    return {
        "ok":         True,
        "key":        key,
        "label":      label,
        "parent":     parent,
        "recognize":  recognize,
        "color":      stream_color(key),
        "breadcrumb": breadcrumb(key, cfg),
    }


@app.patch("/api/streams/{key}")
async def api_streams_patch(key: str, payload: dict):
    """Body: {label?, parent?}. Update a stream's display label and/or parent.
    Refuses moves that would create a cycle (key cannot become a descendant
    of itself)."""
    cfg, cfg_path = _load_streams_config()
    streams = cfg.get("streams") or {}
    if not isinstance(streams, dict):
        streams = {}
    # The stream being edited may live only in the DB (migrated from v1). Pull
    # it into config so it can be renamed or reparented, instead of 404ing a
    # stream the user can plainly see and reorganise.
    if key not in streams and not _ensure_parent_in_config(key, streams):
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
        if new_parent and not _ensure_parent_in_config(new_parent, streams):
            return JSONResponse({"error": f"parent '{new_parent}' is not a known stream"},
                                status_code=400)
        # Cycle guard: walk up from new_parent — if we encounter `key`, abort.
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
    if "recognize" in payload:
        _set_recognize(key, payload.get("recognize") or "", cfg)
    _write_config(cfg, cfg_path)
    from workpulse.core.tree import breadcrumb
    return {"ok": True, "key": key, "label": cur.get("label"),
            "parent": cur.get("parent"), "recognize": _recognize_for(key, cfg),
            "breadcrumb": breadcrumb(key, cfg)}


@app.delete("/api/streams/{key}")
def api_streams_delete(key: str, confirm: bool = False):
    """Remove a stream. Two-phase and non-destructive by default: without
    ?confirm=true it returns the work currently tagged to the stream so the UI
    can ask first. Deleting untags that work (it reverts to unclassified).
    Refused if the stream still has child streams."""
    from workpulse.core import db as wp_db, learning
    cfg, cfg_path = _load_streams_config()
    streams = cfg.get("streams") or {}

    kids = [k for k, v in streams.items()
            if isinstance(v, dict) and v.get("parent") == key]
    if kids:
        return JSONResponse(
            {"error": f"stream '{key}' has children: {kids}. Re-parent or delete them first."},
            status_code=400)

    eff = load_config()
    con = wp_db.connect(eff)
    in_config = key in streams
    in_db = con.execute("SELECT 1 FROM stream WHERE key = ?", (key,)).fetchone() is not None
    if not in_config and not in_db:
        return JSONResponse({"error": f"stream '{key}' not found"}, status_code=404)

    # What deleting will cost: the work currently tagged to this stream, which
    # reverts to unclassified. Surface it and require explicit confirmation so we
    # never quietly throw away someone's tagging.
    row = con.execute(
        """SELECT COUNT(*) AS n,
                  COALESCE(SUM((julianday(ended_at) - julianday(started_at)) * 86400.0), 0) AS secs,
                  COUNT(DISTINCT substr(started_at,1,10)) AS days
           FROM session WHERE stream = ? AND ended_at IS NOT NULL""", (key,)).fetchone()
    impact = {"sessions": int(row["n"] or 0),
              "hours": round((row["secs"] or 0) / 3600.0, 1),
              "active_days": int(row["days"] or 0),
              "reverts_to": "unclassified"}
    if not confirm:
        label = streams.get(key)
        label = label.get("label") if isinstance(label, dict) else (label or key)
        return {"ok": False, "needs_confirm": True, "stream": key,
                "label": label, "impact": impact}

    # Clear EVERY reference to the stream, then delete its row. This is the part
    # that silently blocked before: session / plan_item / browser_visit /
    # calendar_event .stream are nullable FKs (null them); cluster_assignment and
    # daily_candidate .stream are NOT NULL FKs (delete those rows); child streams
    # get orphaned to top level. On a real failure, surface it instead of hiding.
    try:
        con.execute("UPDATE session SET stream = NULL WHERE stream = ?", (key,))
        con.execute("UPDATE plan_item SET stream = NULL WHERE stream = ?", (key,))
        con.execute("UPDATE browser_visit SET stream = NULL WHERE stream = ?", (key,))
        con.execute("UPDATE calendar_event SET stream = NULL WHERE stream = ?", (key,))
        con.execute("UPDATE stream SET parent_key = NULL WHERE parent_key = ?", (key,))
        con.execute("DELETE FROM cluster_assignment WHERE stream = ?", (key,))
        con.execute("DELETE FROM daily_candidate WHERE stream = ?", (key,))
        con.execute("DELETE FROM edge WHERE rel = 'in_stream' AND dst_id = ?", (key,))
        con.execute("DELETE FROM stream WHERE key = ?", (key,))
        con.commit()
    except Exception as e:  # noqa: BLE001
        con.rollback()
        return JSONResponse(
            {"ok": False, "error": f"could not delete '{key}': {type(e).__name__}: {e}"},
            status_code=200)

    # Drop learned rules that assigned to it, so it isn't re-derived (best-effort).
    try:
        rules = [r for r in learning.load_learned_rules(eff) if r.get("stream") != key]
        learning._save_learned_rules(eff, rules)
    except Exception:
        pass

    # Remove from config too, if it was there.
    if in_config:
        del streams[key]
        cfg["streams"] = streams
        _write_config(cfg, cfg_path)

    return {"ok": True, "deleted": key, "untagged_sessions": impact["sessions"]}


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
    cfg = load_config()
    if cfg.get("preview", {}).get("snapshot_mode"):
        return {
            "verdict": "snapshot",
            "summary": "This preview uses a private copy of recent WorkPulse data. "
                       "It does not indicate the live tracker state.",
            "checks": [],
        }
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
    """Read the current brain/profile.md (the living profile).
    Returns the raw markdown + parsed frontmatter so the dashboard can
    render it. Empty payload if no profile has been generated yet."""
    from workpulse.core import db as wp_db
    from workpulse.core.profile import _PROFILE_PATH
    con = wp_db.connect(load_config())
    memory = {
        "active_days": con.execute(
            "SELECT COUNT(DISTINCT substr(started_at,1,10)) FROM session"
        ).fetchone()[0],
        "human_captures": con.execute(
            "SELECT COUNT(*) FROM capture WHERE author='human'"
        ).fetchone()[0],
        "corrections": con.execute(
            "SELECT COUNT(*) FROM cluster_correction"
        ).fetchone()[0],
        "projects_observed": con.execute(
            "SELECT COUNT(DISTINCT stream) FROM cluster_assignment "
            "WHERE source != 'fallback'"
        ).fetchone()[0],
    }
    if not _PROFILE_PATH.exists():
        return {"exists": False, "raw": "",
                "frontmatter": {}, "body": "", "memory": memory}
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
    return {"exists": True, "raw": text, "frontmatter": fm, "body": body,
            "memory": memory}


@app.post("/api/v2/profile/refresh")
async def api_v2_profile_refresh(payload: dict | None = None):
    """Refresh the local living profile using the active backend or fallback."""
    from workpulse.core import db as wp_db, profile as wp_profile
    cfg = load_config()
    use_ai = bool((payload or {}).get("use_ai"))
    result = wp_profile.update_profile(
        wp_db.connect(cfg), cfg=cfg, force_fallback=not use_ai)
    return {"ok": True, "fallback": result["fallback"],
            "model": result["model"]}


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
            "SELECT stream AS assigned_stream, confidence, source, evidence "
            "FROM cluster_assignment WHERE cluster_id = ?",
            (c["cluster_id"],),
        ).fetchone()
        if assignment:
            assigned_stream = (None if assignment["source"] == "fallback"
                               else assignment["assigned_stream"])
            assignment_confidence = assignment["confidence"]
            assignment_source = assignment["source"]
            try:
                assignment_evidence = json.loads(assignment["evidence"] or "[]")
            except (ValueError, TypeError):
                assignment_evidence = []
        else:
            assigned_stream = c["stream"]
            assignment_confidence = None
            assignment_source = "unassigned"
            assignment_evidence = []
        if assignment_source == "user":
            attribution_method = "Confirmed by you"
            attribution_reason = "Your correction is the source of truth."
        elif assignment_source == "fallback":
            attribution_method = "Needs review"
            attribution_reason = "WorkPulse did not find enough evidence to assign this."
        elif any(e.get("signal") == "local_ai" for e in assignment_evidence):
            ai_ev = next(e for e in assignment_evidence
                         if e.get("signal") == "local_ai")
            model = ai_ev.get("model") or "local model"
            attribution_method = f"Ollama · {model}"
            attribution_reason = ai_ev.get("detail") or "Local AI classification"
        elif assignment_source == "agent":
            signal_names = {
                "calendar_event": "Calendar match",
                "browser_visit": "Browser match",
                "file_path": "File-path match",
                "capture": "Capture match",
                "title_keyword": "Title keyword match",
                "plan_item": "Plan match",
                "existing_stream": "Existing project rule",
            }
            first = assignment_evidence[0] if assignment_evidence else {}
            attribution_method = signal_names.get(
                first.get("signal"), "WorkPulse rules")
            attribution_reason = first.get("detail") or \
                "Deterministic local signals"
        else:
            attribution_method = "Not assigned"
            attribution_reason = "No project decision has been made."
        # Pretty label for the assigned stream
        label_row = con.execute(
            "SELECT label FROM stream WHERE key = ?", (assigned_stream,),
        ).fetchone() if assigned_stream else None
        assigned_label = (label_row["label"] if label_row
                          else (assigned_stream or "Needs review"))
        output = wp_ctx.infer_output(ctx, assigned_label)
        enriched_clusters.append({
            "cluster_id": c["cluster_id"],
            "name":       c.get("name") or None,
            "name_source": c.get("name_source") or None,
            "one_liner":  c.get("one_liner") or None,
            "stream":     assigned_stream,            # current source of truth
            "raw_stream": c.get("raw_stream"),        # evidence/debug only
            "assigned_stream":     assigned_stream,   # Categorizer's pick
            "assigned_label":      assigned_label,
            "assignment_confidence": assignment_confidence,
            "assignment_source":   assignment_source, # 'user' | 'agent' | 'fallback' | 'unassigned'
            "attribution_method":  attribution_method,
            "attribution_reason":  attribution_reason,
            "hours":      c["hours"],
            "output_title": output["title"],
            "output_specific": output["specific"],
            "evidence_kind": output["evidence_kind"],
            "evidence_label": output["evidence_label"],
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


@app.get("/api/v2/timeline")
def api_v2_timeline(request: Request, date: Optional[str] = None):  # noqa: A002
    """Meaningful work blocks and calendar commitments in one local chronology."""
    from datetime import date as _date_cls
    from workpulse.core import db as wp_db
    from workpulse.core import timeline as wp_timeline
    cfg = load_config()
    try:
        on = _date_cls.fromisoformat(date) if date else datetime.now().date()
    except (ValueError, TypeError):
        return JSONResponse({"error": "bad date"}, status_code=400)
    con = wp_db.connect(cfg)
    return wp_timeline.build_day(
        con, on, cfg=cfg, include_private=_personal_unlocked(request))


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

    # Re-tag at read-time so config edits to stream_path_patterns AND rules the
    # user just taught via the Tag-as dropdown take effect immediately on
    # existing log entries — not only on newly-written sessions. Without the
    # match_learned() pass, a window tagged from the "Needs your attention" panel
    # would stay in that panel until the next capture, looking like the tag
    # didn't stick.
    from workpulse.signals.activity import _tag_stream
    from workpulse.core.learning import has_negative_rule, match_learned
    for s in active:
        title = s.get("title") or ""
        retagged = _tag_stream(title, s.get("exe_path") or "", cfg)
        if not retagged:
            retagged = match_learned(title, cfg)  # user-taught rules
        if retagged:
            s["stream"] = retagged

    # Per-stream totals
    by_stream_secs: dict[str, float] = defaultdict(float)
    untagged_secs = 0.0
    # Per-app totals (all apps, tagged or not)
    by_app_secs: dict[str, float] = defaultdict(float)
    # Untagged window titles — what the user is doing outside known projects
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
            # A window the user explicitly chose to "Never tag" still counts as
            # untagged time, but must not keep resurfacing in the attention panel.
            if not has_negative_rule(title, cfg):
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
    from workpulse.core.tree import labels as _stream_labels
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
    from workpulse.core.llm import backend_status, active_backend
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


@app.get("/api/update")
def api_update_check():
    """Check for a newer installer. This never downloads or installs it."""
    from workpulse.core import updater
    try:
        return updater.check()
    except Exception as exc:
        return JSONResponse(
            {"error": str(exc), "current_version": updater.__version__,
             "update_available": False},
            status_code=503,
        )


@app.post("/api/update/install")
def api_update_install(request: Request):
    """Download, verify, then open the native installer after a user click."""
    from workpulse.core import updater
    # A custom header cannot be submitted by a cross-origin HTML form. This
    # prevents an arbitrary website from making localhost open an installer.
    if request.headers.get("x-workpulse-action") != "install-update":
        return JSONResponse({"error": "Explicit update confirmation required"},
                            status_code=403)
    try:
        return updater.download_and_launch()
    except Exception as exc:
        return JSONResponse({"error": str(exc)}, status_code=502)


def _taxonomy_is_trivial(cfg: dict) -> bool:
    from workpulse.core.tree import normalise
    tree = normalise(cfg)
    # Real streams in the DATA (e.g. migrated from v1) count as "set up" too,
    # even when config.yaml has none — otherwise a migrated user is told to set
    # up streams that already exist.
    try:
        from workpulse.core import db as wp_db
        n = wp_db.connect(cfg).execute(
            "SELECT COUNT(*) FROM stream WHERE key IS NOT NULL AND key <> 'misc'"
        ).fetchone()[0]
        if n >= 2:
            return False
    except Exception:
        pass
    if len(tree) < 2:
        return True
    # If nothing has a parent, the user hasn't engaged with the hierarchy.
    if not any(r.get("parent") for r in tree.values()):
        # Two top-level streams without parents could still be intentional;
        # only treat as trivial if one of them is the legacy 'misc' default.
        return "misc" in tree
    return False


def _streams_payload(cfg: dict) -> list[dict]:
    from workpulse.core.tree import normalise, breadcrumb, ancestors
    tree = normalise(cfg)
    out = [
        {
            "key":        k,
            "label":      rec["label"],
            "parent":     rec.get("parent"),
            "color":      stream_color(k),
            "recognize":  _recognize_for(k, cfg),
            "ancestors":  ancestors(k, cfg),
            "breadcrumb": breadcrumb(k, cfg),
        }
        for k, rec in tree.items()
    ]
    # Include streams that exist in the data (e.g. migrated from v1) but aren't
    # in config.yaml yet, so they're taggable rather than invisible in the
    # "Tag as" dropdown. Without this a migrated user sees only "ignore".
    try:
        from workpulse.core import db as wp_db
        seen = {e["key"] for e in out}
        con = wp_db.connect(cfg)
        for r in con.execute("SELECT key, label FROM stream ORDER BY label"):
            k = r["key"]
            if not k or k in seen:
                continue
            label = r["label"] or k
            if label == k:                        # raw key like "personal-dev"
                label = k.replace("-", " ").replace("_", " ").title()
            out.append({
                "key": k, "label": label, "parent": None,
                "color": stream_color(k), "recognize": _recognize_for(k, cfg),
                "ancestors": [], "breadcrumb": label,
            })
            seen.add(k)
    except Exception:
        pass
    return out


# ── Loop A: user correction → permanent learned rule ─────────────────────────

@app.post("/api/learn")
async def api_learn(payload: dict):
    """Body: {pattern: str, stream: str|null, raw_title: str|null}.
    pattern  — substring that the learning module will match on (lowercased,
               normalized). If omitted, derived from raw_title.
    stream   — the stream key to assign, or null to mark this pattern as
               permanently untagged.
    """
    from workpulse.core.learning import (
        normalize_title, _add_rule, retag_sessions, retag_calendar_events)
    from workpulse.core import db as wp_db
    cfg = load_config()
    raw_title = (payload.get("raw_title") or "").strip()
    pattern   = (payload.get("pattern")   or normalize_title(raw_title)).strip().lower()
    stream    = payload.get("stream")
    if stream and stream not in (cfg.get("streams") or {}):
        # also accept streams that exist in the data (migrated from v1, etc.)
        if not wp_db.connect(cfg).execute(
                "SELECT 1 FROM stream WHERE key = ?", (stream,)).fetchone():
            return JSONResponse({"error": f"unknown stream: {stream}"}, status_code=400)
    if not pattern or len(pattern) < 2:
        return JSONResponse({"error": "pattern too short"}, status_code=400)
    # Persist the tag onto sessions so the retrospective / Ask see it immediately,
    # not just the read-time-retagged dashboard views. This is the learning loop:
    # tag one window, every matching session (past and future) gets attributed.
    # Never let this raise a 500 — the dashboard can't parse an HTML error page,
    # so on any failure return a JSON error the UI can show.
    retagged = 0
    meetings_retagged = 0
    try:
        _add_rule(cfg=cfg, pattern=pattern, stream=stream, raw_title=raw_title, source="user")
        con = wp_db.connect(cfg)
        retagged = retag_sessions(con, cfg)
        # One loop for work AND meetings: a rule taught here also files matching
        # calendar events, so they leave the "Meetings to file" panel.
        meetings_retagged = retag_calendar_events(con, cfg)
    except Exception as e:  # noqa: BLE001 — the tag action must never 500
        import logging
        logging.getLogger("workpulse.web").warning("learn/retag failed: %r", e)
        return JSONResponse({"ok": False, "error": f"{type(e).__name__}: {e}"},
                            status_code=200)
    return {"ok": True, "pattern": pattern, "stream": stream,
            "retagged": retagged, "meetings_retagged": meetings_retagged}


@app.post("/api/v2/retag")
async def api_v2_retag(payload: dict):
    """Tag the untagged. Body: {ai: bool=true}. Applies learned rules to every
    untagged session (retroactively), and when ai=true and an LLM backend is
    available, first AI-classifies the biggest untagged windows into streams.
    Returns how many sessions got attributed."""
    from workpulse.core import db as wp_db, learning
    cfg = load_config()
    con = wp_db.connect(cfg)
    if payload.get("ai", True) is not False:
        res = learning.classify_untagged(con, cfg=cfg)
        return {"ok": True, "retagged": res["retagged"], "ai": res}
    return {"ok": True, "retagged": learning.retag_sessions(con, cfg)}


@app.get("/api/meetings/untagged")
def api_meetings_untagged():
    """Meetings not yet attributed to a stream — the calendar side of the
    tag-the-untagged loop, so the calendar becomes signal, not a dump. Grouped
    by title (a recurring meeting is tagged once); tagging reuses /api/learn,
    which also retags matching meetings and work windows."""
    cfg = load_config()
    from workpulse.core import db as wp_db
    con = wp_db.connect(cfg)
    try:
        rows = con.execute(
            """SELECT cel.raw_title AS title, COUNT(*) AS n,
                      MIN(ce.started_at) AS first_at, MAX(ce.started_at) AS last_at
               FROM calendar_event ce
               JOIN calendar_event_local cel ON cel.event_id = ce.id
               WHERE ce.stream IS NULL AND cel.raw_title IS NOT NULL
                     AND cel.raw_title <> ''
               GROUP BY cel.raw_title
               ORDER BY n DESC, last_at DESC
               LIMIT 20"""
        ).fetchall()
    except Exception:  # noqa: BLE001 — no calendar data yet -> empty, never 500
        rows = []
    meetings = [
        {"title": r["title"], "count": r["n"],
         "first_at": r["first_at"], "last_at": r["last_at"]}
        for r in rows
    ]
    return {"meetings": meetings, "count": len(meetings)}


# ── Trust, organization preview, and product feedback ───────────────────────

@app.get("/api/v2/organization/preview")
def api_organization_preview(request: Request, date: Optional[str] = None):  # noqa: A002
    """Return only the outcome-level fields a user may choose to share.

    Raw app/window sessions, file paths, browser history, captures, and private
    streams are deliberately absent.  This is a preview, not an upload.
    """
    today = api_v2_today(request, date=date)
    if isinstance(today, Response):
        return today
    projects = [
        {"key": item.get("stream"), "label": item.get("label") or item.get("stream"),
         "hours": item.get("hours", 0)}
        for item in today.get("by_stream", [])
        if item.get("stream") and item.get("stream") != "<untagged>"
    ]
    return {"date": today["date"],
            "total_hours": round(sum(float(p["hours"] or 0) for p in projects), 1),
            "projects": projects,
            "excluded": ["raw window titles", "URLs", "file paths", "personal activity",
                         "screenshots", "keystrokes"]}


def _manager_context_path() -> Path:
    return resolve("config/manager_context.json")


@app.get("/api/v2/organization/context")
def api_organization_context():
    p = _manager_context_path()
    if not p.exists():
        return {"priorities": "", "expected_outcomes": "", "feedback": "",
                "updated_at": None, "local_only": True}
    try:
        data = json.loads(p.read_text(encoding="utf-8"))
    except Exception:
        data = {}
    return {k: data.get(k, "") for k in ("priorities", "expected_outcomes", "feedback")} | {
        "updated_at": data.get("updated_at"), "local_only": True}


@app.post("/api/v2/organization/context")
async def api_set_organization_context(payload: dict):
    """Demo the manager-to-user context channel locally; no manager link yet."""
    data = {k: str(payload.get(k) or "").strip()[:4000]
            for k in ("priorities", "expected_outcomes", "feedback")}
    data["updated_at"] = datetime.now(timezone.utc).isoformat()
    p = _manager_context_path()
    p.parent.mkdir(parents=True, exist_ok=True)
    p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    return {"ok": True, "local_only": True, **data}


@app.get("/api/v2/feedback/status")
def api_feedback_status():
    from workpulse.core import db as wp_db, feedback as wp_feedback
    return wp_feedback.prompt_status(wp_db.connect(load_config()))


@app.post("/api/v2/feedback")
async def api_feedback(payload: dict):
    from workpulse.core import feedback as wp_feedback
    if payload.get("opt_out"):
        wp_feedback.opt_out()
        return {"ok": True, "opted_out": True}
    answers = payload.get("answers") or {}
    if not isinstance(answers, dict) or not any(str(v or "").strip() for v in answers.values()):
        return JSONResponse({"error": "feedback is empty"}, status_code=400)
    return {"ok": True, **wp_feedback.submit(answers, cfg=load_config())}


# ── Ask WorkPulse: conversational retrieval over your own work ────────────────

_ASK_RETRO_RE = re.compile(
    r"\b(how (did|do) i|what (did|have|was) i|what happened|what have i been|"
    r"summari[sz]e|recap|walk me through|make (a|an) sop|as an sop|retrospective|"
    r"last week|last month|this (week|month)|past week|recently|yesterday|"
    r"in (january|february|march|april|may|june|july|august|september|october|"
    r"november|december))\b", re.I)
_ASK_LOCATE_RE = re.compile(
    r"\b(where('?s| is| are)?|find|locate|open|which file|what file|"
    r"path (to|of)|show me the)\b", re.I)
_ASK_METHOD_RE = re.compile(
    r"\b(how did i approach|what approach did i|what method did i|"
    r"how do i usually|how have i been approaching)\b", re.I)
_ASK_PROPOSAL_METHOD_RE = re.compile(
    r"\b(how (do|did|have) i (do|make|write|create|prepare|develop|produce|handle)|"
    r"my (proposal|concept note) (method|process|workflow|approach)|"
    r"how (do|did) i approach)\b.*\b(proposals?|concept notes?)\b|"
    r"\b(proposals?|concept notes?)\b.*\b(method|process|workflow|approach)\b",
    re.I,
)


def _ask_route(question: str) -> str:
    """Deterministic intent routing: retrospective > locate > general."""
    low = question.casefold()
    proposal_topic = bool(re.search(r"\b(proposals?|concept notes?)\b", low))
    method_cue = (
        bool(re.search(r"\bhow\b.*\b(i|my)\b|\b(i|my)\b.*\bhow\b", low))
        or bool(re.search(r"\b(method|process|workflow|approach|way)\b", low))
    )
    time_cue = bool(re.search(
        r"\b(yesterday|today|last (week|month)|this (week|month)|"
        r"past week|in (january|february|march|april|may|june|july|august|"
        r"september|october|november|december))\b", low))
    if _ASK_PROPOSAL_METHOD_RE.search(question) or (
            proposal_topic and method_cue and not time_cue):
        return "workflow"
    # Method questions need evidence synthesis, not a time-led worklog.
    if _ASK_METHOD_RE.search(question):
        return "general"
    if _ASK_RETRO_RE.search(question):
        return "retrospective"
    if _ASK_LOCATE_RE.search(question):
        return "locate"
    return "general"


@app.post("/api/ask")
async def api_ask(payload: dict, request: Request):
    """Ask WorkPulse a question about your own work. Body: {question}.
    Routes to file-locate, a work retrospective, or the general think() brain.
    locate + retrospective read private paths, so they need the personal unlock.
    Keyless: answers work with no API key; a backend only sharpens the phrasing."""
    question = (payload.get("question") or "").strip()
    use_ai = bool(payload.get("use_ai"))
    if not question:
        return JSONResponse({"error": "missing question"}, status_code=400)

    from workpulse.core import (db as wp_db, files as wp_files,
                                retrospective as wp_retro, think as wp_think,
                                llm as wp_llm, projects as wp_projects,
                                workflows as wp_workflows)
    cfg = load_config()
    con = wp_db.connect(cfg)
    kind = _ask_route(question)
    backend = wp_llm.active_backend(cfg)

    if kind == "workflow":
        result = wp_workflows.answer_proposal_question(con)
        learned = result["workflow"]
        return {
            "kind": "workflow",
            "backend": "workflow-memory",
            "model": None,
            "fallback": False,
            "answer": result["answer"],
            "gap": result["gap"],
            "evidence": [
                {
                    "type": "workflow",
                    "name": example["label"],
                    "period": example["period"],
                    "markers": example["evidence_markers"],
                    "stages": example["stages"],
                }
                for example in learned["examples"]
            ],
        }

    if kind in ("locate", "retrospective"):
        # The private tier is protected only if the user set a personal password.
        # No password means no lock, so file search works out of the box.
        from workpulse.core import personal as wp_personal
        if wp_personal.is_password_set(con) and not _personal_unlocked(request):
            return JSONResponse({
                "kind": kind, "backend": backend, "locked": True,
                "answer": "Unlock your private data (Settings) to search your "
                          "files and work history.",
            }, status_code=401)

    if kind == "locate":
        hits = wp_files.locate(con, question, cfg=cfg)
        return {
            "kind": "locate", "backend": backend, "fallback": backend == "none",
            "answer": wp_files.format_hits(hits, question),
            "gap": ("" if hits else
                    "No indexed file matched. WorkPulse may not have observed "
                    "the folder yet, or the file used different words."),
            "evidence": [{"type": "file", "path": h["path"],
                          "basename": h["basename"],
                          "last_touched": h["last_touched"]} for h in hits],
        }

    if kind == "retrospective":
        roll = wp_retro.summarize(con, query=question, cfg=cfg)
        if use_ai:
            try:
                rendered, render_meta = await asyncio.wait_for(
                    asyncio.to_thread(
                        wp_retro.to_sop_markdown,
                        roll,
                        cfg=cfg,
                        with_meta=True,
                        use_backend=True,
                    ),
                    timeout=38,
                )
            except TimeoutError:
                rendered, render_meta = wp_retro.to_sop_markdown(
                    roll, cfg=cfg, with_meta=True, use_backend=False)
        else:
            rendered, render_meta = wp_retro.to_sop_markdown(
                roll, cfg=cfg, with_meta=True, use_backend=False)
        gap_match = re.search(r"\n##\s+Gap\s*\n+(.*)\Z", rendered,
                              flags=re.I | re.S)
        gap = gap_match.group(1).strip() if gap_match else ""
        answer = rendered[:gap_match.start()].rstrip() if gap_match else rendered
        return {
            "kind": "retrospective",
            "backend": render_meta.get("backend") or "none",
            "model": render_meta.get("model"),
            "fallback": render_meta.get("backend") == "none",
            "answer": answer, "gap": gap,
            "window": roll["window"], "total_seconds": roll["total_seconds"],
            "evidence": (
                [{"type": "work", "name": a["label"],
                  "hours": round(a["seconds"] / 3600.0, 1)}
                 for a in roll["areas"] if a["tagged"] and a["seconds"] >= 60][:6]
                + [{"type": "file", "path": f["path"],
                    "basename": f["basename"]} for f in roll["files"][:10]]
            ),
        }

    project_match = wp_projects.resolve_match(
        question, wp_projects.load_projects(cfg))
    retrieval_query = (
        project_match["matched_keyword"] if project_match else question)
    retrieval_stream = project_match["stream"] if project_match else None

    if use_ai:
        def _run_local_think():
            # SQLite connections are thread-bound. Open the AI worker's own
            # connection so the dashboard event loop remains responsive.
            worker_con = wp_db.connect(cfg)
            try:
                return wp_think.think(
                    worker_con, question, cfg=cfg, force_fallback=False,
                    retrieval_query=retrieval_query,
                    stream=retrieval_stream)
            finally:
                worker_con.close()

        try:
            result = await asyncio.wait_for(
                asyncio.to_thread(_run_local_think), timeout=38)
        except TimeoutError:
            result = wp_think.think(
                con, question, cfg=cfg, force_fallback=True,
                retrieval_query=retrieval_query,
                stream=retrieval_stream)
    else:
        result = wp_think.think(
            con, question, cfg=cfg, force_fallback=True,
            retrieval_query=retrieval_query,
            stream=retrieval_stream)
    return {
        "kind": "general",
        "backend": ("none" if result["fallback"] else backend),
        "fallback": result["fallback"],
        "answer": result["answer"], "gap": result["gap"],
        "evidence": [{"type": "atom", "atom_id": a.get("atom_id"),
                      "atom_kind": a.get("atom_kind"),
                      "date": (a.get("ts") or "")[:10],
                      "stream": a.get("stream"),
                      "content": (a.get("content") or "")[:120]}
                     for a in result["atoms"][:10]],
    }


# ── Settings: in-app secret + email config (no terminal needed) ──────────────

_ALLOWED_SECRETS = {"anthropic_key", "smtp_password", "smtp_user", "smtp_to"}


# ── Classroom: Session Console and managed-device policy service ─────────────

@app.get("/api/v2/classroom/status")
def api_classroom_status():
    status = classroom_core.session_status()
    devices = classroom_core.devices()
    device_ids = {device["id"] for device in devices}
    # Classroom surfaces enrolled devices only. Older local-console prototype
    # events used a synthetic "Device 01"; do not let those appear as learners.
    status["events"] = [
        event for event in status.get("events", [])
        if event.get("device_id") in device_ids
    ]
    status["devices"] = devices
    freshest = max(devices, key=lambda item: item.get("last_seen") or "", default=None)
    status["latest_signal"] = ({
        "device_id": freshest["id"],
        "device_name": freshest["name"],
        "ts": freshest.get("last_seen"),
        "app": freshest.get("last_app") or "",
        "title": freshest.get("last_title") or "",
        "domain": freshest.get("last_domain") or "",
        "source": "classroom_agent",
    } if freshest else None)
    return status


@app.get("/api/v2/classroom/policy")
def api_classroom_policy(mode: str = "class"):
    try:
        return classroom_core.get_policy(mode)
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/v2/classroom/policy")
async def api_classroom_policy_save(request: Request):
    payload = await request.json()
    try:
        return classroom_core.save_policy(
            str(payload.get("mode") or "class"), payload.get("policy") or {}
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


@app.post("/api/v2/classroom/pairing")
def api_classroom_pairing(request: Request):
    import ipaddress
    import socket
    import threading
    try:
        probe = socket.create_connection(("127.0.0.1", 5722), timeout=.2)
        probe.close()
    except OSError:
        from workpulse import classroom_gateway
        threading.Thread(
            target=classroom_gateway.run,
            kwargs={"host": "0.0.0.0", "port": 5722},
            daemon=True,
            name="workpulse-classroom-gateway",
        ).start()
    result = classroom_core.create_pairing()
    candidates = []
    for addresses in psutil.net_if_addrs().values():
        for address in addresses:
            if address.family != socket.AF_INET:
                continue
            try:
                ip = ipaddress.ip_address(address.address)
            except ValueError:
                continue
            if ip.is_private and not ip.is_loopback and not ip.is_link_local:
                candidates.append(address.address)
    host = candidates[0] if candidates else "SESSION-CONSOLE-IP"
    result["server"] = f"http://{host}:5722"
    result["invitation"] = (
        "workpulse://classroom/join"
        f"?server={result['server']}&code={result['code']}"
    )
    result["network_addresses"] = candidates
    return result


def _validate_classroom_server(server: str) -> str:
    parsed = urlparse(server.strip())
    if parsed.scheme != "http" or not parsed.hostname or parsed.port != 5722:
        raise ValueError("invitation does not contain a valid Classroom console")
    try:
        address = ipaddress.ip_address(parsed.hostname)
    except ValueError as exc:
        raise ValueError("Classroom console must use its local network address") from exc
    if not (address.is_private or address.is_loopback):
        raise ValueError("Classroom console is not on a private network")
    return f"http://{parsed.hostname}:5722"


@app.get("/api/v2/classroom/local-agent")
def api_classroom_local_agent():
    from workpulse import classroom_agent
    return classroom_agent.local_status()


@app.post("/api/v2/classroom/local-agent/join")
async def api_classroom_local_agent_join(request: Request):
    from workpulse import classroom_agent
    payload = await request.json()
    try:
        server = _validate_classroom_server(str(payload.get("server") or ""))
        result = classroom_agent.enroll(
            server,
            str(payload.get("code") or "").strip().upper(),
            str(payload.get("name") or "").strip() or "Classroom computer",
        )
        background = classroom_agent.start_background()
    except (RuntimeError, OSError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    return {
        "ok": True,
        "device_id": result["device_id"],
        "device_name": result["device_name"],
        **background,
    }


@app.post("/api/v2/classroom/agent/enroll")
async def api_classroom_agent_enroll(request: Request):
    payload = await request.json()
    try:
        return classroom_core.enroll_device(
            str(payload.get("code") or ""),
            str(payload.get("name") or ""),
            str(payload.get("platform") or "unknown"),
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)


def _classroom_bearer(request: Request) -> str:
    header = request.headers.get("authorization", "")
    return header[7:].strip() if header.lower().startswith("bearer ") else ""


@app.get("/api/v2/classroom/agent/policy")
def api_classroom_agent_policy(request: Request):
    if not classroom_core.authenticate_device(_classroom_bearer(request)):
        return JSONResponse({"error": "unauthorized"}, status_code=401)
    return classroom_core.agent_policy()


@app.post("/api/v2/classroom/agent/heartbeat")
async def api_classroom_agent_heartbeat(request: Request):
    payload = await request.json()
    try:
        return classroom_core.device_heartbeat(
            _classroom_bearer(request), payload.get("signal") or {}
        )
    except ValueError as exc:
        return JSONResponse({"error": str(exc)}, status_code=401)


@app.post("/api/v2/classroom/session/start")
async def api_classroom_start(request: Request):
    payload = await request.json()
    try:
        status = classroom_core.start_session(
            mode=str(payload.get("mode") or "class"),
            title=str(payload.get("title") or "Classroom session"),
            duration_minutes=int(payload.get("duration_minutes") or 60),
            policy=payload.get("policy"),
            starts_at=payload.get("starts_at"),
        )
    except (TypeError, ValueError) as exc:
        return JSONResponse({"error": str(exc)}, status_code=400)
    status["devices"] = classroom_core.devices()
    status["latest_signal"] = None
    return status


@app.post("/api/v2/classroom/session/end")
def api_classroom_end():
    status = classroom_core.end_session()
    status["devices"] = classroom_core.devices()
    status["latest_signal"] = None
    return status


@app.post("/api/v2/classroom/evaluate")
async def api_classroom_evaluate(request: Request):
    payload = await request.json()
    signal = payload.get("signal") if isinstance(payload, dict) else None
    if not signal:
        return api_classroom_status()
    result = classroom_core.evaluate_signal(signal=signal)
    status = api_classroom_status()
    status["latest_signal"] = result.get("signal")
    status["latest_decision"] = result
    return status


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


# ── dashboard HTML ────────────────────────────────────────────────────────────



@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


# ── entry point ───────────────────────────────────────────────────────────────

def run(host: str = "127.0.0.1", port: int = PORT, start_watcher_on_launch: bool = False):
    if start_watcher_on_launch:
        start_watcher()
    try:
        from workpulse import classroom_agent
        if classroom_agent.local_status()["enrolled"]:
            classroom_agent.start_background()
    except (RuntimeError, OSError, ValueError):
        pass
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
