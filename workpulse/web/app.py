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
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from workpulse.common import load_config, resolve

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
    if parent and parent not in streams:
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
def api_streams_delete(key: str):
    """Remove a stream. Refused if it still has child streams — re-parent or
    delete those first."""
    cfg, cfg_path = _load_streams_config()
    streams = cfg.get("streams") or {}
    if key not in streams:
        return JSONResponse({"error": f"stream '{key}' not found"}, status_code=404)

    kids = [k for k, v in streams.items()
            if isinstance(v, dict) and v.get("parent") == key]
    if kids:
        return JSONResponse(
            {"error": f"stream '{key}' has children: {kids}. Re-parent or delete them first."},
            status_code=400)

    del streams[key]
    cfg["streams"] = streams
    _write_config(cfg, cfg_path)
    return {"ok": True, "deleted": key}


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
    """Read the current brain/profile.md (the living profile).
    Returns the raw markdown + parsed frontmatter so the dashboard can
    render it. Empty payload if no profile has been generated yet."""
    from workpulse.core.profile import _PROFILE_PATH
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


def _taxonomy_is_trivial(cfg: dict) -> bool:
    from workpulse.core.tree import normalise
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
    from workpulse.core.tree import normalise, breadcrumb, ancestors
    tree = normalise(cfg)
    return [
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


# ── Loop A: user correction → permanent learned rule ─────────────────────────

@app.post("/api/learn")
async def api_learn(payload: dict):
    """Body: {pattern: str, stream: str|null, raw_title: str|null}.
    pattern  — substring that the learning module will match on (lowercased,
               normalized). If omitted, derived from raw_title.
    stream   — the stream key to assign, or null to mark this pattern as
               permanently untagged.
    """
    from workpulse.core.learning import normalize_title, _add_rule
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


# ── Ask WorkPulse: conversational retrieval over your own work ────────────────

_ASK_RETRO_RE = re.compile(
    r"\b(how did i|how do i|what did i|what have i been|summari[sz]e|recap|"
    r"walk me through|make (a|an) sop|as an sop|retrospective|"
    r"last week|last month|this week|past week|yesterday)\b", re.I)
_ASK_LOCATE_RE = re.compile(
    r"\b(where('?s| is| are)?|find|locate|open|which file|what file|"
    r"path (to|of)|show me the)\b", re.I)


def _ask_route(question: str) -> str:
    """Deterministic intent routing: retrospective > locate > general."""
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
    if not question:
        return JSONResponse({"error": "missing question"}, status_code=400)

    from workpulse.core import (db as wp_db, files as wp_files,
                                retrospective as wp_retro, think as wp_think,
                                llm as wp_llm)
    cfg = load_config()
    con = wp_db.connect(cfg)
    kind = _ask_route(question)
    backend = wp_llm.active_backend(cfg)

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
            "evidence": [{"type": "file", "path": h["path"],
                          "basename": h["basename"],
                          "last_touched": h["last_touched"]} for h in hits],
        }

    if kind == "retrospective":
        roll = wp_retro.summarize(con, query=question, cfg=cfg)
        return {
            "kind": "retrospective", "backend": backend,
            "fallback": backend == "none",
            "answer": wp_retro.to_sop_markdown(roll, cfg=cfg),
            "window": roll["window"], "total_seconds": roll["total_seconds"],
            "evidence": [{"type": "file", "path": f["path"],
                          "basename": f["basename"]} for f in roll["files"][:10]],
        }

    result = wp_think.think(con, question, cfg=cfg)
    return {
        "kind": "general", "backend": backend, "fallback": result["fallback"],
        "answer": result["answer"], "gap": result["gap"],
        "evidence": [{"type": "atom", "atom_id": a.get("atom_id"),
                      "atom_kind": a.get("atom_kind")}
                     for a in result["atoms"][:10]],
    }


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


# ── dashboard HTML ────────────────────────────────────────────────────────────



@app.get("/")
def dashboard():
    return FileResponse(STATIC_DIR / "index.html", media_type="text/html")


# ── entry point ───────────────────────────────────────────────────────────────

def run(host: str = "127.0.0.1", port: int = PORT, start_watcher_on_launch: bool = False):
    if start_watcher_on_launch:
        start_watcher()
    uvicorn.run(app, host=host, port=port, log_level="warning")


if __name__ == "__main__":
    run()
