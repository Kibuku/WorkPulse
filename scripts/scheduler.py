"""
scheduler.py — wire the dream cycle and reports to the OS scheduler.

PLAN.md §7 step 8b. macOS via launchd; Windows via Task Scheduler. Linux
is not on the roadmap.

Three jobs land on every install:
  consolidate     daily   at 23:00 local — the dream cycle
  report-daily    daily   at 23:15 local — yesterday-night summary
  report-weekly   Sunday  at 20:00 local — week summary

Each job runs the existing CLI verb (`python -m scripts.consolidate`,
`python -m scripts.report daily`, `python -m scripts.report weekly`) so
schedule changes never have to know about anything inside the script.

Public API (testable without touching launchctl / schtasks):
    render_launchd_plist(job) -> str
    render_windows_task_xml(job) -> str
    jobs() -> list[dict]

CLI:
    python -m scripts.scheduler install              # platform-aware install
    python -m scripts.scheduler uninstall            # remove the three jobs
    python -m scripts.scheduler status               # which jobs are loaded
    python -m scripts.scheduler render <job> [plist|xml]   # dry-print
"""

from __future__ import annotations

import argparse
import json
import os
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import ROOT


_LABEL_PREFIX = "com.workpulse"


def jobs() -> list[dict]:
    """Three jobs, expressed once. Times are local."""
    return [
        {
            "slug":    "consolidate",
            "label":   f"{_LABEL_PREFIX}.consolidate",
            "module":  "scripts.consolidate",
            "args":    [],
            "hour":    23, "minute": 0, "weekday": None,
            "description": "Dream cycle — nightly consolidation.",
        },
        {
            "slug":    "report-daily",
            "label":   f"{_LABEL_PREFIX}.report-daily",
            "module":  "scripts.report",
            "args":    ["daily"],
            "hour":    23, "minute": 15, "weekday": None,
            "description": "Daily report — markdown + optional email.",
        },
        {
            "slug":    "report-weekly",
            "label":   f"{_LABEL_PREFIX}.report-weekly",
            "module":  "scripts.report",
            "args":    ["weekly"],
            "hour":    20, "minute": 0, "weekday": 0,   # 0 = Sunday in launchd
            "description": "Weekly report — markdown + optional email.",
        },
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
    """Render a launchd .plist for one job. Pure function — testable."""
    python = python or _python_path()
    root = root or ROOT
    args_xml = "\n        ".join(
        f"<string>{a}</string>" for a in ["-m", job["module"], *job["args"]]
    )
    interval = [f"<key>Hour</key><integer>{job['hour']}</integer>",
                f"<key>Minute</key><integer>{job['minute']}</integer>"]
    if job["weekday"] is not None:
        interval.append(f"<key>Weekday</key><integer>{job['weekday']}</integer>")
    interval_xml = "\n        ".join(interval)
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

    <key>StartCalendarInterval</key>
    <dict>
        {interval_xml}
    </dict>

    <!-- Catch up if the Mac was asleep at the scheduled time -->
    <key>StartCalendarIntervalRespectsRunStatus</key>
    <false/>

    <key>StandardOutPath</key>
    <string>{root / 'logs' / (job['slug'] + '.out.log')}</string>
    <key>StandardErrorPath</key>
    <string>{root / 'logs' / (job['slug'] + '.err.log')}</string>
</dict>
</plist>
"""


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
    if job["weekday"] is None:
        sched = """<ScheduleByDay><DaysInterval>1</DaysInterval></ScheduleByDay>"""
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
