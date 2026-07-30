"""
doctor.py — autonomous health check. The "CTO end-of-day check-in".

WorkPulse fails SILENTLY. Across development we hit: agents exiting 78 for
two weeks, the file watcher catching nothing, the activity sensor frozen on
one app for days. Every time, the user only discovered it days later by
happening to open a stale dashboard. This module is the antidote: it runs on
a schedule, checks the things that silently break, and — crucially — ALERTS
via an OS notification (users won't read logs).

Checks (each returns ok / warn / fail + a plain-language message):
  - sensors_running   : activity + watcher processes alive
  - sensor_liveness   : a session written recently (tracker not dead)
  - sensor_not_stuck  : more than one app seen in the recent window
                        (catches the frozen-frontmost-read bug)
  - agents_healthy    : every launchd agent loaded, last exit 0 (not 78)
  - data_fresh        : last session / file_event within tolerance
  - nightly_ran       : consolidate ran today (only checked after evening)

Overall verdict = worst individual result. On warn/fail, fires a macOS
notification (and, if email is configured + --notify-email, an email).

CLI:
    python -m workpulse.ops.doctor check           # run + print report
    python -m workpulse.ops.doctor check --notify  # + macOS notification if not ok
    python -m workpulse.ops.doctor check --json
"""

from __future__ import annotations

import argparse
import json
import subprocess
import sys
from datetime import datetime, timedelta, timezone
from pathlib import Path

from workpulse.core import db
from workpulse import platform_util
from workpulse.common import ROOT, load_config

# Agents expected to be loaded (kept in sync with scheduler.jobs()).
_EXPECTED_AGENTS = ["activity", "watcher", "nightly", "dream-refresh",
                    "calendar-sync"]

OK, WARN, FAIL = "ok", "warn", "fail"
_RANK = {OK: 0, WARN: 1, FAIL: 2}


def _local_now() -> datetime:
    return datetime.now().astimezone()


# ── individual checks ───────────────────────────────────────────────────────

def check_sensors_running() -> dict:
    # Cross-platform process count (psutil), not Unix pgrep.
    act = platform_util.count_processes("activity")
    wat = platform_util.count_processes("watcher")
    if act >= 1 and wat >= 1:
        status = OK
        msg = "activity + watcher both running"
    elif act >= 1 or wat >= 1:
        status = FAIL
        msg = f"a sensor is down (activity={act}, watcher={wat})"
    else:
        status = FAIL
        msg = "both sensors are down"
    if act > 1 or wat > 1:
        status = WARN if status == OK else status
        msg += f"  (duplicates: activity={act}, watcher={wat})"
    return {"check": "sensors_running", "status": status, "message": msg}


def check_sensor_liveness(con) -> dict:
    row = con.execute("SELECT MAX(started_at) AS m FROM session").fetchone()
    last = row["m"]
    if not last:
        return {"check": "sensor_liveness", "status": FAIL,
                "message": "no sessions ever recorded"}
    try:
        t = datetime.fromisoformat(last)
    except ValueError:
        t = None
    now = _local_now()
    if t is None:
        return {"check": "sensor_liveness", "status": WARN,
                "message": f"last session timestamp unparseable: {last}"}
    if t.tzinfo is None:
        t = t.replace(tzinfo=now.tzinfo)
    age_min = (now - t).total_seconds() / 60
    # Tracker checkpoints at least every 60s while awake. >20 min stale
    # during the day is suspicious; overnight it's fine.
    if age_min <= 20:
        return {"check": "sensor_liveness", "status": OK,
                "message": f"last session {age_min:.0f} min ago"}
    if 8 <= now.hour <= 20:  # daytime
        return {"check": "sensor_liveness", "status": WARN,
                "message": f"last session {age_min:.0f} min ago (daytime, tracker may be stalled)"}
    return {"check": "sensor_liveness", "status": OK,
            "message": f"last session {age_min:.0f} min ago (off-hours)"}


def check_sensor_not_stuck(con) -> dict:
    """The frozen-frontmost-read signature: many sessions in the recent
    window but all the same app. Deep focus on one app is real, so this
    is a WARN not a FAIL — but the combination (lots of records, one app,
    daytime) is exactly how the NSWorkspace-freeze looked."""
    # 4-hour window: genuine 4h of a single app is uncommon, but the
    # frozen-frontmost bug trivially trips this (it logged one app for
    # DAYS). Widening from 2h to 4h cuts false positives from real focus
    # sessions while still catching the freeze.
    now = _local_now()
    since = (now - timedelta(hours=4)).isoformat()
    rows = con.execute(
        "SELECT app, COUNT(*) AS n FROM session WHERE started_at >= ? GROUP BY app",
        (since,),
    ).fetchall()
    total = sum(r["n"] for r in rows)
    distinct = len(rows)
    if total < 20:
        return {"check": "sensor_not_stuck", "status": OK,
                "message": f"only {total} sessions in 4h, too few to judge"}
    if distinct == 1:
        app = rows[0]["app"]
        return {"check": "sensor_not_stuck", "status": WARN,
                "message": f"{total} sessions in 4h ALL on '{app}'. "
                           f"likely a frozen frontmost read (this is how the "
                           f"NSWorkspace-freeze bug looked)"}
    return {"check": "sensor_not_stuck", "status": OK,
            "message": f"{distinct} distinct apps in 4h, tracking live"}


# Windows Task Scheduler "Last Result" codes in the SCHED_S_* range are STATUS,
# not failures: ready (0x41300=267008), running (267009), not-yet-run
# (0x41303=267011), queued, terminated. A scheduled/daemon task that hasn't
# completed a run yet reports these, so they must not read as errors.
_WIN_SCHED_BENIGN = set(range(267008, 267016))  # 0x41300 .. 0x41307


def check_agents_healthy() -> dict:
    # Cross-platform: launchctl on macOS, schtasks on Windows, via
    # platform_util.agent_last_exit(). Returns (loaded, last_exit_code).
    if not (platform_util.is_mac() or platform_util.is_windows()):
        return {"check": "agents_healthy", "status": OK,
                "message": "unsupported platform, skipped"}
    hard, soft = [], []
    for slug in _EXPECTED_AGENTS:
        loaded, code = platform_util.agent_last_exit(slug)
        if not loaded:
            hard.append(f"{slug}: NOT loaded")
        elif code is not None and code != 0 and code not in _WIN_SCHED_BENIGN:
            # calendar-sync is optional: it exits non-zero when no calendar ICS
            # URL is configured, which is not a system fault.
            if slug == "calendar-sync":
                soft.append(f"{slug}: exit {code} (no calendar configured?)")
            else:
                hard.append(f"{slug}: last exit {code}")
    if not hard and not soft:
        return {"check": "agents_healthy", "status": OK,
                "message": f"all {len(_EXPECTED_AGENTS)} scheduled tasks are registered"}
    if not hard:
        return {"check": "agents_healthy", "status": WARN, "message": "; ".join(soft)}
    return {"check": "agents_healthy", "status": FAIL,
            "message": "; ".join(hard + soft)}


def check_data_fresh(con) -> dict:
    # Compute age in UTC on both sides — file_event.ts is stored UTC. Mixing
    # a UTC timestamp with local `now` (the previous bug) produced a bogus
    # "27h stale" when the data was 12 minutes old.
    now_utc = datetime.now(timezone.utc)
    fe = con.execute("SELECT MAX(ts) AS m FROM file_event").fetchone()["m"]
    status = OK
    if not fe:
        return {"check": "data_fresh", "status": WARN, "message": "no file events"}
    try:
        t = datetime.fromisoformat(fe.replace("Z", "+00:00"))
        if t.tzinfo is None:
            t = t.replace(tzinfo=timezone.utc)
        age_h = (now_utc - t).total_seconds() / 3600
    except ValueError:
        return {"check": "data_fresh", "status": WARN,
                "message": f"file_event timestamp unparseable: {fe}"}
    # File edits are bursty — you can genuinely go a work-day without saving a
    # watched file. Only warn past ~30h (more than a full day + evening).
    if age_h > 30:
        status = WARN
        msg = f"file events {age_h:.0f}h stale"
    else:
        msg = f"file events {age_h:.1f}h ago"
    return {"check": "data_fresh", "status": status, "message": msg}


def check_nightly_ran(con) -> dict:
    now = _local_now()
    if now.hour < 21:
        return {"check": "nightly_ran", "status": OK,
                "message": "before nightly window, not expected yet"}
    today = now.date().isoformat()
    row = con.execute(
        """
        SELECT 1 FROM skill_run
        WHERE skill_slug='consolidate' AND status IN ('ok','fallback')
          AND substr(ts,1,10)=? LIMIT 1
        """,
        (today,),
    ).fetchone()
    if row:
        return {"check": "nightly_ran", "status": OK,
                "message": "today's consolidation is done"}
    return {"check": "nightly_ran", "status": WARN,
            "message": "past 21:00 but today's consolidation hasn't run yet"}


def _table_has_rows(con, table) -> bool:
    try:
        con.execute(f"SELECT 1 FROM {table} LIMIT 1")
        return True
    except Exception:
        return False


# ── orchestration ───────────────────────────────────────────────────────────

def run_checks(cfg: dict | None = None) -> dict:
    cfg = cfg or load_config()
    con = db.connect(cfg)
    checks = [
        check_sensors_running(),
        check_sensor_liveness(con),
        check_sensor_not_stuck(con),
        check_agents_healthy(),
        check_data_fresh(con),
        check_nightly_ran(con),
    ]
    verdict = OK
    for c in checks:
        if _RANK[c["status"]] > _RANK[verdict]:
            verdict = c["status"]
    return {
        "ts":       _local_now().isoformat(),
        "verdict":  verdict,
        "checks":   checks,
        "summary":  _summary_line(verdict, checks),
    }


def _summary_line(verdict: str, checks: list[dict]) -> str:
    if verdict == OK:
        return "WorkPulse is healthy."
    bad = [c for c in checks if c["status"] != OK]
    lead = "Something's wrong with WorkPulse:" if verdict == FAIL \
        else "WorkPulse needs a look:"
    return lead + " " + "; ".join(c["message"] for c in bad)


# ── alerting ────────────────────────────────────────────────────────────────

def _notify(title: str, message: str) -> None:
    # Cross-platform: macOS notification or Windows toast.
    platform_util.notify(title, message)


def _write_health_file(result: dict) -> None:
    """Write the latest verdict where the dashboard can read it."""
    try:
        p = ROOT / "logs" / "health.json"
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(json.dumps(result, indent=2), encoding="utf-8")
    except Exception:
        pass


def check_and_alert(*, notify: bool = False, notify_email: bool = False,
                    cfg: dict | None = None) -> dict:
    result = run_checks(cfg)
    _write_health_file(result)
    if notify and result["verdict"] != OK:
        _notify("WorkPulse health", result["summary"])
    if notify_email and result["verdict"] != OK:
        try:
            from workpulse.core.report import send_report_email
            send_report_email("WorkPulse health alert", result["summary"], cfg)
        except Exception:
            pass
    return result


# ── CLI ──────────────────────────────────────────────────────────────────────

def _print_report(result: dict) -> None:
    icon = {OK: "OK  ", WARN: "WARN", FAIL: "FAIL"}
    print(f"verdict: {result['verdict'].upper()}  ({result['ts'][:19]})")
    print()
    for c in result["checks"]:
        print(f"  [{icon[c['status']]}] {c['check']:18s} {c['message']}")
    print()
    print(f"  {result['summary']}")


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp doctor",
                                     description="WorkPulse self-health check.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    c = sub.add_parser("check")
    c.add_argument("--notify", action="store_true",
                   help="fire a macOS notification if not healthy")
    c.add_argument("--notify-email", action="store_true",
                   help="also email the alert if email is configured")
    c.add_argument("--json", action="store_true")
    args = parser.parse_args(argv[1:])
    if args.cmd == "check":
        result = check_and_alert(notify=args.notify,
                                 notify_email=args.notify_email)
        if args.json:
            print(json.dumps(result, indent=2))
        else:
            _print_report(result)
        # Exit code mirrors verdict so callers can react: 0 ok, 1 warn, 2 fail
        return {OK: 0, WARN: 1, FAIL: 2}[result["verdict"]]
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
