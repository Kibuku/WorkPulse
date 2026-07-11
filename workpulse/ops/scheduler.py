"""
scheduler.py — wire the dream cycle and reports to the OS scheduler.

PLAN.md §7 step 8b. macOS via launchd; Windows via Task Scheduler. Linux
is not on the roadmap.

Three jobs land on every install:
  consolidate     daily   at 23:00 local — the dream cycle
  report-daily    daily   at 23:15 local — yesterday-night summary
  report-weekly   Sunday  at 20:00 local — week summary

Each job runs the existing CLI verb (`python -m workpulse.core.consolidate`,
`python -m workpulse.core.report daily`, `python -m workpulse.core.report weekly`) so
schedule changes never have to know about anything inside the script.

Public API (testable without touching launchctl / schtasks):
    render_launchd_plist(job) -> str
    render_windows_task_xml(job) -> str
    jobs() -> list[dict]

CLI:
    python -m workpulse.ops.scheduler install              # platform-aware install
    python -m workpulse.ops.scheduler uninstall            # remove the three jobs
    python -m workpulse.ops.scheduler status               # which jobs are loaded
    python -m workpulse.ops.scheduler render <job> [plist|xml]   # dry-print
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

from workpulse.common import ROOT


_LABEL_PREFIX = "com.workpulse"


def jobs() -> list[dict]:
    """All autonomous agents, expressed once. Times are local.

    Design note: there are NO StartCalendarInterval jobs anymore. A
    calendar job only fires if the Mac is awake at that exact minute;
    asleep at 23:00 = the whole day's rollup silently skipped, no
    catch-up. Everything is now interval-based:
      - nightly runs hourly and guards in code (once-a-day rollup, catches
        up on next wake if the evening was missed)
      - dream-refresh runs every 30 min (cluster + name + categorize)
      - calendar-sync hourly, browser-tracker every 30s
    """
    return [
        # Long-running sensors. Must be their OWN launchd Aqua agents, NOT
        # Popen children of the tray — otherwise NSWorkspace frontmost falls
        # back to "Finder" for everything and FSEvents never delivers.
        {
            "slug":    "activity",
            "label":   f"{_LABEL_PREFIX}.activity",
            "module":  "workpulse.signals.activity",
            "args":    [],
            "hour":    None, "minute": None, "weekday": None,
            "daemon":  True,
            "description": "Foreground-app sensor (frontmost window + browser tab).",
        },
        {
            "slug":    "watcher",
            "label":   f"{_LABEL_PREFIX}.watcher",
            "module":  "workpulse.signals.watcher",
            "args":    [],
            "hour":    None, "minute": None, "weekday": None,
            "daemon":  True,
            "description": "File-change sensor (FSEvents over the vault).",
        },
        # Sleep-proof once-a-day (and once-a-week) rollups. Runs hourly,
        # does real work only when due. Replaces the old consolidate /
        # report-daily / report-weekly / about-george calendar jobs.
        {
            "slug":    "nightly",
            "label":   f"{_LABEL_PREFIX}.nightly",
            "module":  "workpulse.ops.nightly",
            "args":    ["run"],
            "hour":    None, "minute": None, "weekday": None,
            "interval_seconds": 3600,            # hourly; guarded in code
            "description": "Sleep-proof daily + weekly rollups (guarded hourly).",
        },
        # Daytime fast refresh: keep job_view + cluster_assignment current
        # so the dashboard doesn't show stale data. Also folds in the
        # Categorizer pass, so there's no separate categorize agent.
        {
            "slug":    "dream-refresh",
            "label":   f"{_LABEL_PREFIX}.dream-refresh",
            "module":  "workpulse.ops.dream_refresh",
            "args":    ["--quiet"],
            "hour":    None, "minute": None, "weekday": None,
            "interval_seconds": 1800,            # every 30 minutes
            "description": "30-min cluster refresh + categorize for the live dashboard.",
        },
        {
            "slug":    "calendar-sync",
            "label":   f"{_LABEL_PREFIX}.calendar-sync",
            "module":  "workpulse.signals.calendar_sync",
            "args":    ["sync"],
            "hour":    None, "minute": None, "weekday": None,
            "interval_seconds": 3600,            # hourly
            "description": "Pull calendar events from ICS URL so meetings become signal.",
        },
        # The autonomous CTO check-in. Runs every 3h and fires a macOS
        # notification if anything's wrong (sensors down, frozen frontmost,
        # agents exiting 78, stale data). WorkPulse fails silently; this is
        # how George finds out within hours instead of days.
        {
            "slug":    "doctor",
            "label":   f"{_LABEL_PREFIX}.doctor",
            "module":  "workpulse.ops.doctor",
            "args":    ["check", "--notify"],
            "hour":    None, "minute": None, "weekday": None,
            "interval_seconds": 10800,           # every 3 hours
            "description": "Self-health check; macOS-notifies on any problem.",
        },
        # NOTE: browser capture is no longer a standalone agent. It's bound
        # into activity.py's frontmost-app sample loop (see
        # _maybe_capture_browser) so a browser_visit is recorded at the exact
        # moment its session starts, sharing the session's clock. The old
        # 30-second standalone poll drifted from the sessions it should align
        # with; this removes that drift and one moving part.
    ]


# ── macOS (launchd) ─────────────────────────────────────────────────────────

def _python_path() -> Path:
    """Prefer the venv python; fall back to whatever's running this code."""
    venv = ROOT / ".venv" / "bin" / "python"
    return venv if venv.exists() else Path(sys.executable)


def _logs_dir() -> Path:
    p = ROOT / "logs"
    p.mkdir(parents=True, exist_ok=True)
    return p


def render_launchd_plist(job: dict, *, python: Path | None = None,
                         root: Path | None = None) -> str:
    """Render a launchd .plist for one job. Pure function — testable.

    Two trigger styles supported:
      • calendar  — hour + minute (+ optional weekday). Fires once at that
                    time. The default for nightly jobs.
      • interval  — interval_seconds. Fires every N seconds, 24/7. Used
                    for the dream-refresh job that needs to keep the live
                    dashboard fresh through the day.
    """
    python = python or _python_path()
    root = root or ROOT
    args_xml = "\n        ".join(
        f"<string>{a}</string>" for a in ["-m", job["module"], *job["args"]]
    )

    interval_secs = job.get("interval_seconds")
    if job.get("daemon"):
        # Long-running sensor (activity, watcher). launchd keeps it alive
        # and — critically — runs it in the user's Aqua GUI session so it
        # has window-server access (NSWorkspace frontmost) and FSEvents
        # delivery. Running these as Popen children of the tray silently
        # broke both: NSWorkspace fell back to "Finder" and FSEvents never
        # fired. See _maybe_capture_browser / activity frontmost read.
        trigger_xml = (
            "<key>RunAtLoad</key>\n    <true/>\n"
            "    <key>KeepAlive</key>\n    <true/>\n"
            "    <key>SessionCreate</key>\n    <true/>\n"
            "    <key>LimitLoadToSessionType</key>\n    <string>Aqua</string>"
        )
    elif interval_secs:
        # RunAtLoad + StartInterval is the canonical reliable-periodic
        # pattern. StartInterval ALONE waits a full interval before the
        # first run and stalls unreliably across sleep/wake — we observed
        # dream-refresh run once then go silent for 8h after a sleep cycle,
        # leaving the dashboard frozen. RunAtLoad guarantees a run on every
        # login/boot and re-arms the interval.
        trigger_xml = (
            f"<key>RunAtLoad</key>\n    <true/>\n"
            f"    <key>StartInterval</key>\n    "
            f"<integer>{int(interval_secs)}</integer>"
        )
    else:
        interval = [f"<key>Hour</key><integer>{job['hour']}</integer>",
                    f"<key>Minute</key><integer>{job['minute']}</integer>"]
        if job["weekday"] is not None:
            interval.append(f"<key>Weekday</key><integer>{job['weekday']}</integer>")
        interval_xml = "\n        ".join(interval)
        trigger_xml = f"<key>StartCalendarInterval</key>\n    <dict>\n        {interval_xml}\n    </dict>"

    return f"""<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
    "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>{job['label']}</string>

    <key>ProgramArguments</key>
    <array>
        <string>{python}</string>
        {args_xml}
    </array>

    <key>WorkingDirectory</key>
    <string>{root}</string>

    {trigger_xml}

    <key>StandardOutPath</key>
    <string>{_agent_log_dir() / (job['slug'] + '.out.log')}</string>
    <key>StandardErrorPath</key>
    <string>{_agent_log_dir() / (job['slug'] + '.err.log')}</string>
</dict>
</plist>
"""


def _agent_log_dir() -> Path:
    """Where launchd writes agent stdout/stderr.

    CRITICAL: this must NOT be inside ~/Documents (or Desktop/Downloads).
    launchd — the daemon, not the spawned process — opens StandardOutPath
    and StandardErrorPath *before* forking the job. The launchd daemon has
    no TCC grant for the user's Documents folder, so if the log path lives
    there, the open() fails and the whole job aborts with EX_CONFIG (78)
    before the program ever runs. The spawned process itself CAN read
    Documents (it inherits the user's grants), which is why manual runs
    always worked and only the scheduled runs silently died.

    ~/Library/Logs is the Apple-sanctioned location for this and is not
    TCC-restricted.
    """
    p = Path.home() / "Library" / "Logs" / "WorkPulse"
    p.mkdir(parents=True, exist_ok=True)
    return p


def _launchd_plist_path(label: str) -> Path:
    return Path.home() / "Library" / "LaunchAgents" / f"{label}.plist"


def _macos_install_one(job: dict) -> str:
    plist_path = _launchd_plist_path(job["label"])
    plist_path.parent.mkdir(parents=True, exist_ok=True)
    plist_path.write_text(render_launchd_plist(job), encoding="utf-8")
    # Unload first so re-installs pick up edits; ignore failures (it may
    # not be loaded yet).
    subprocess.run(["launchctl", "unload", str(plist_path)],
                   capture_output=True)
    r = subprocess.run(["launchctl", "load", str(plist_path)],
                       capture_output=True, text=True)
    if r.returncode != 0:
        return f"FAIL  {job['label']}  {r.stderr.strip() or r.stdout.strip()}"
    return f"OK    {job['label']}"


def _macos_uninstall_one(job: dict) -> str:
    plist_path = _launchd_plist_path(job["label"])
    if not plist_path.exists():
        return f"SKIP  {job['label']}  (not installed)"
    subprocess.run(["launchctl", "unload", str(plist_path)],
                   capture_output=True)
    plist_path.unlink()
    return f"OK    {job['label']}  (removed)"


def _macos_status() -> list[dict]:
    out = []
    for job in jobs():
        plist = _launchd_plist_path(job["label"])
        loaded = subprocess.run(
            ["launchctl", "list", job["label"]], capture_output=True, text=True
        ).returncode == 0
        out.append({
            "label":    job["label"],
            "plist":    str(plist),
            "exists":   plist.exists(),
            "loaded":   loaded,
            "schedule": _human_schedule(job),
        })
    return out


# ── Windows (Task Scheduler XML) ────────────────────────────────────────────

def render_windows_task_xml(job: dict, *, python: Path | None = None,
                            root: Path | None = None) -> str:
    """Render a Task Scheduler task XML for one job. Pure function."""
    python = python or _python_path()
    root = root or ROOT
    args = " ".join(['"-m"', f'"{job["module"]}"', *[f'"{a}"' for a in job["args"]]])
    # Trigger: <CalendarTrigger> with <ScheduleByDay> or <ScheduleByWeek>
    # or interval-based <Repetition><Interval>PT30M</Interval>
    interval_secs = job.get("interval_seconds")
    if job.get("daemon"):
        # Long-running sensor: run at logon, restart on failure. On Windows
        # a LogonTrigger + RestartOnFailure is the analog of launchd's
        # RunAtLoad + KeepAlive.
        return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Description>{job['description']}</Description></RegistrationInfo>
  <Triggers><LogonTrigger><Enabled>true</Enabled></LogonTrigger></Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""
    if interval_secs:
        secs = int(interval_secs)
        if secs % 3600 == 0:
            iso_interval = f"PT{secs // 3600}H"
        elif secs % 60 == 0:
            iso_interval = f"PT{secs // 60}M"
        else:
            iso_interval = f"PT{secs}S"
        sched = (f"<Repetition><Interval>{iso_interval}</Interval>"
                 f"<StopAtDurationEnd>false</StopAtDurationEnd></Repetition>"
                 f"<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>")
        start = "2026-01-01T00:00:00"
    elif job["weekday"] is None:
        sched = """<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"""
        start = f"2026-01-01T{job['hour']:02d}:{job['minute']:02d}:00"
    else:
        # launchd weekday: 0=Sun, 1=Mon. Windows: <Sunday/>, <Monday/>...
        day_name = ["Sunday", "Monday", "Tuesday", "Wednesday",
                    "Thursday", "Friday", "Saturday"][job["weekday"]]
        sched = (f"<ScheduleByWeek><WeeksInterval>1</WeeksInterval>"
                 f"<DaysOfWeek><{day_name}/></DaysOfWeek></ScheduleByWeek>")
        start = f"2026-01-01T{job['hour']:02d}:{job['minute']:02d}:00"
    return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo>
    <Description>{job['description']}</Description>
  </RegistrationInfo>
  <Triggers>
    <CalendarTrigger>
      <StartBoundary>{start}</StartBoundary>
      <Enabled>true</Enabled>
      {sched}
    </CalendarTrigger>
  </Triggers>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <Priority>7</Priority>
    <ExecutionTimeLimit>PT2H</ExecutionTimeLimit>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{python}</Command>
      <Arguments>{args}</Arguments>
      <WorkingDirectory>{root}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""


def _windows_install_one(job: dict) -> str:
    xml = render_windows_task_xml(job)
    xml_path = ROOT / "logs" / f"{job['slug']}.task.xml"
    xml_path.parent.mkdir(parents=True, exist_ok=True)
    xml_path.write_text(xml, encoding="utf-16")
    task_name = job["label"]
    # /F overwrites; UTF-16 required by schtasks /XML
    r = subprocess.run(
        ["schtasks", "/Create", "/TN", task_name, "/XML", str(xml_path), "/F"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return f"FAIL  {task_name}  {r.stderr.strip() or r.stdout.strip()}"
    return f"OK    {task_name}"


def _windows_uninstall_one(job: dict) -> str:
    r = subprocess.run(
        ["schtasks", "/Delete", "/TN", job["label"], "/F"],
        capture_output=True, text=True,
    )
    if r.returncode != 0:
        return f"SKIP  {job['label']}  (not installed)"
    return f"OK    {job['label']}  (removed)"


def _windows_status() -> list[dict]:
    out = []
    for job in jobs():
        r = subprocess.run(["schtasks", "/Query", "/TN", job["label"]],
                           capture_output=True, text=True)
        out.append({
            "label":    job["label"],
            "loaded":   r.returncode == 0,
            "schedule": _human_schedule(job),
        })
    return out


# ── shared ──────────────────────────────────────────────────────────────────

def _human_schedule(job: dict) -> str:
    if job.get("daemon"):
        return "always (daemon)"
    interval = job.get("interval_seconds")
    if interval:
        if interval % 3600 == 0:
            return f"every {interval // 3600}h"
        if interval % 60 == 0:
            return f"every {interval // 60} min"
        return f"every {interval}s"
    if job["weekday"] is None:
        return f"daily {job['hour']:02d}:{job['minute']:02d}"
    day = ["Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat"][job["weekday"]]
    return f"{day} {job['hour']:02d}:{job['minute']:02d}"


def install_all() -> int:
    """Platform-aware install. Returns 0 on success, 1 on any failure."""
    fn = (_macos_install_one if sys.platform == "darwin"
          else _windows_install_one if sys.platform == "win32"
          else None)
    if fn is None:
        print(f"unsupported platform: {sys.platform}", file=sys.stderr)
        return 2
    rc = 0
    for job in jobs():
        line = fn(job)
        print(line)
        if line.startswith("FAIL"):
            rc = 1
    return rc


def uninstall_all() -> int:
    fn = (_macos_uninstall_one if sys.platform == "darwin"
          else _windows_uninstall_one if sys.platform == "win32"
          else None)
    if fn is None:
        print(f"unsupported platform: {sys.platform}", file=sys.stderr)
        return 2
    for job in jobs():
        print(fn(job))
    return 0


def status_all() -> list[dict]:
    if sys.platform == "darwin":
        return _macos_status()
    if sys.platform == "win32":
        return _windows_status()
    return []


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_status() -> int:
    rows = status_all()
    if not rows:
        print(f"unsupported platform: {sys.platform}")
        return 2
    for r in rows:
        tag = "LOADED" if r.get("loaded") else "not loaded"
        print(f"{tag:>10s}  {r['label']:35s}  schedule={r['schedule']}")
    return 0


def _cli_render(args: argparse.Namespace) -> int:
    target = (args.format or
              ("plist" if sys.platform == "darwin" else "xml"))
    for job in jobs():
        if args.job and args.job != job["slug"]:
            continue
        print(f"=== {job['slug']} ({target}) ===")
        print(render_launchd_plist(job) if target == "plist"
              else render_windows_task_xml(job))
        print()
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp scheduler",
                                     description="Install / inspect the "
                                                 "dream cycle scheduler.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("install")
    sub.add_parser("uninstall")
    sub.add_parser("status")
    r = sub.add_parser("render")
    r.add_argument("job", nargs="?", help="filter by job slug")
    r.add_argument("--format", choices=("plist", "xml"))
    args = parser.parse_args(argv[1:])
    if args.cmd == "install":
        return install_all()
    if args.cmd == "uninstall":
        return uninstall_all()
    if args.cmd == "status":
        return _cli_status()
    if args.cmd == "render":
        return _cli_render(args)
    parser.error(f"unknown command {args.cmd!r}")
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
