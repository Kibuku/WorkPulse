"""
smoke.py — cross-platform install smoke test for WorkPulse v2.

Run this right after `pip install -e ".[<platform>,dev]"` to confirm the core
works on THIS machine before wider rollout. It's the fast, no-network check
that de-risks a new platform (especially Windows): imports, the OS activity
sensor, the DB + migrations, the web app object, and the autostart renderer.

Usage:
    python tools/smoke.py

Exit code 0 = all checks passed. Non-zero = at least one FAIL (see the report).
Paste the whole output back if anything fails.
"""

from __future__ import annotations

import platform
import sys
import tempfile
import traceback
from pathlib import Path

# Make the repo importable when run as `python tools/smoke.py` from the root.
sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

_results: list[tuple[str, str, str]] = []  # (status, name, detail)


def check(name: str):
    """Decorator: run a check fn, capture PASS/FAIL/WARN + detail, never crash."""
    def wrap(fn):
        try:
            detail = fn() or ""
            status = "PASS"
            if isinstance(detail, tuple):  # (status, detail)
                status, detail = detail
        except Exception as e:  # noqa: BLE001 — smoke test must never abort early
            status, detail = "FAIL", f"{type(e).__name__}: {e}"
            detail += "\n" + "".join(traceback.format_exc(limit=3))
        _results.append((status, name, str(detail).rstrip()))
        return fn
    return wrap


# ── checks ────────────────────────────────────────────────────────────────────

@check("Python >= 3.11")
def _py():
    v = sys.version_info
    if v < (3, 11):
        return ("FAIL", f"found {v.major}.{v.minor}; need >= 3.11")
    return f"{v.major}.{v.minor}.{v.micro}"


@check("Platform")
def _plat():
    return f"{platform.system()} {platform.release()} ({sys.platform})"


@check("Core imports")
def _imports():
    import workpulse.core.db          # noqa: F401
    import workpulse.core.tree        # noqa: F401
    import workpulse.core.llm         # noqa: F401
    import workpulse.core.learning    # noqa: F401
    import workpulse.web.app          # noqa: F401
    import workpulse.signals.activity  # noqa: F401
    return "db, tree, llm, learning, web.app, signals.activity all import"


@check("OS activity sensor")
def _sensor():
    from workpulse.signals import activity
    fw = activity._foreground_window()
    idle = activity._idle_seconds()
    if not isinstance(idle, (int, float)) or idle < 0:
        return ("FAIL", f"idle_seconds() returned {idle!r}")
    if fw is None:
        return ("WARN", f"foreground_window() returned None (no focused window?); idle={idle:.1f}s")
    title, pid, app = fw
    return f"foreground title={title!r} pid={pid} app={app!r}; idle={idle:.1f}s"


@check("Lockscreen filter")
def _lock():
    from workpulse.signals import activity
    fn = activity._is_lockscreen
    # Must be callable and return a bool for an arbitrary app name.
    r = fn("SomeApp.exe")
    if not isinstance(r, bool):
        return ("FAIL", f"_is_lockscreen returned {r!r}")
    return f"_is_lockscreen callable (sample -> {r})"


@check("DB connect + migrate (temp file)")
def _db():
    import workpulse.core.db as db
    tmp = Path(tempfile.mkdtemp()) / "smoke.db"
    db.db_path = lambda cfg=None, _f=tmp: _f          # point at a throwaway DB
    con = db.connect(cfg={"paths": {}})
    try:
        have = db.current_version(con)
        latest = max(v for v, _, _ in db._discover_migrations())
        if have != latest:
            return ("FAIL", f"migrated to v{have}, expected v{latest}")
        return f"migrated to schema v{have} at {tmp}"
    finally:
        con.close()


@check("Web app + static assets")
def _web():
    from workpulse.web import app as webapp
    static = webapp.STATIC_DIR
    missing = [f for f in ("index.html", "dashboard.css", "dashboard.js")
               if not (static / f).exists() or (static / f).stat().st_size == 0]
    if missing:
        return ("FAIL", f"missing/empty static assets: {missing}")
    routes = {getattr(r, "path", None) for r in webapp.app.routes}
    for want in ("/", "/api/system"):
        if want not in routes:
            return ("FAIL", f"route {want} not registered")
    # Exercise the endpoint that crashed under load before the db fixes.
    sysinfo = webapp.api_system()
    if "ai_backend" not in sysinfo or "streams" not in sysinfo:
        return ("FAIL", f"api_system() shape unexpected: keys={list(sysinfo)[:6]}")
    return f"static OK; api_system ai_backend={sysinfo['ai_backend']!r}, {len(sysinfo['streams'])} streams"


@check("Autostart renderer")
def _sched():
    from workpulse.ops import scheduler
    all_jobs = scheduler.jobs()
    if not all_jobs:
        return ("WARN", "scheduler.jobs() returned no jobs")
    job = all_jobs[0]                                  # a real, fully-shaped job
    if sys.platform == "win32":
        xml = scheduler.render_windows_task_xml(job)
        if "<Task" not in xml:
            return ("FAIL", "render_windows_task_xml produced no <Task> XML")
        return f"Task Scheduler XML rendered for {job['label']!r} ({len(xml)} chars)"
    if sys.platform == "darwin":
        plist = scheduler.render_launchd_plist(job)
        if "<plist" not in plist:
            return ("FAIL", "render_launchd_plist produced no <plist>")
        return f"launchd plist rendered for {job['label']!r} ({len(plist)} chars)"
    return ("SKIP", f"no autostart backend for {sys.platform}")


# ── report ─────────────────────────────────────────────────────────────────────

def main() -> int:
    print("=" * 64)
    print(f"WorkPulse v2 smoke test — {platform.system()} / Python {sys.version.split()[0]}")
    print("=" * 64)
    icon = {"PASS": "[PASS]", "FAIL": "[FAIL]", "WARN": "[WARN]", "SKIP": "[SKIP]"}
    for status, name, detail in _results:
        print(f"{icon.get(status, status)}  {name}")
        if detail:
            for line in detail.splitlines():
                print(f"         {line}")
    fails = sum(1 for s, _, _ in _results if s == "FAIL")
    warns = sum(1 for s, _, _ in _results if s == "WARN")
    print("-" * 64)
    print(f"{len(_results)} checks: {len(_results) - fails - warns} pass, {warns} warn, {fails} fail")
    if fails == 0:
        print("\nCore is healthy on this machine. Next: run the sensor for a")
        print("minute and open the dashboard (see tools/SMOKE_WINDOWS.md).")
    return 1 if fails else 0


if __name__ == "__main__":
    raise SystemExit(main())
