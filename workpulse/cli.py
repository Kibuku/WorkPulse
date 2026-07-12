"""
workpulse — the command-line entry point (the `workpulse` console command).

    workpulse install     set up config + database + background agents
    workpulse uninstall   remove the background agents
    workpulse status      agents + a health summary
    workpulse doctor      run the health checks
    workpulse web         run the local dashboard

`install` is what turns a fresh checkout into a running system: it seeds
config.yaml from the template, applies the database schema, and installs the
background agents (launchd on macOS, Task Scheduler on Windows) so the sensors
and the recurring jobs — nightly rollup, dream-refresh, calendar-sync, doctor —
actually run on a schedule instead of only when invoked by hand.
"""

from __future__ import annotations

import argparse
import shutil
import sys
from pathlib import Path

from workpulse.common import ROOT


def _ensure_config() -> Path:
    cfg = ROOT / "config" / "config.yaml"
    example = ROOT / "config" / "config.example.yaml"
    if cfg.exists():
        print(f"  config    using existing {cfg}")
    elif example.exists():
        cfg.parent.mkdir(parents=True, exist_ok=True)
        shutil.copyfile(example, cfg)
        print(f"  config    created {cfg} from template")
    else:
        print("  config    WARNING: no template found — create config/config.yaml by hand",
              file=sys.stderr)
    return cfg


def _init_db() -> None:
    from workpulse.core import db
    con = db.connect()
    try:
        print(f"  database  ready at schema v{db.current_version(con)}")
    finally:
        con.close()


def cmd_install(args: argparse.Namespace) -> int:
    print("WorkPulse — install")
    print("=" * 52)
    _ensure_config()
    _init_db()
    from workpulse.ops import scheduler
    print("  agents    installing background agents:")
    rc = scheduler.install_all()
    print()
    if rc == 0:
        print("Done. Next steps:")
    else:
        print("Finished with some agent errors above. Next steps:")
    print("  1. Start the dashboard:   workpulse web    → http://127.0.0.1:5700")
    print("  2. Set up your streams — the wizard opens on first load.")
    print("  3. (optional) Add a calendar: put its ICS URL in config/config.yaml")
    print("     under `calendar.ics_url`, then: workpulse status")
    return rc


def cmd_uninstall(args: argparse.Namespace) -> int:
    from workpulse.ops import scheduler
    return scheduler.uninstall_all()


def cmd_status(args: argparse.Namespace) -> int:
    from workpulse.ops import scheduler
    rows = scheduler.status_all()
    if not rows:
        print(f"No agent backend for platform {sys.platform!r}.")
    else:
        print("Agents:")
        for r in rows:
            tag = "running" if r.get("loaded") else "STOPPED"
            print(f"  {tag:>8s}  {r['label']}   schedule={r.get('schedule', '?')}")
    from workpulse.ops import doctor
    try:
        res = doctor.run_checks()
        print(f"\nHealth: {res['summary']}")
    except Exception as e:  # noqa: BLE001 — status must never crash
        print(f"\nHealth: (check skipped — {type(e).__name__}: {e})")
    return 0


def cmd_doctor(args: argparse.Namespace) -> int:
    from workpulse.ops import doctor
    res = doctor.run_checks()
    doctor._print_report(res)
    return 1 if res.get("verdict") == doctor.FAIL else 0


def cmd_web(args: argparse.Namespace) -> int:
    from workpulse.web import app
    app.run(host=args.host, port=args.port,
            start_watcher_on_launch=args.start_watcher)
    return 0


_COMMANDS = {
    "install":   cmd_install,
    "uninstall": cmd_uninstall,
    "status":    cmd_status,
    "doctor":    cmd_doctor,
    "web":       cmd_web,
}


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(prog="workpulse",
                                description="WorkPulse — a local-first attention brain.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("install",   help="set up config, database, and background agents")
    sub.add_parser("uninstall", help="remove the background agents")
    sub.add_parser("status",    help="show agents + a health summary")
    sub.add_parser("doctor",    help="run the health checks")
    w = sub.add_parser("web",   help="run the local dashboard")
    w.add_argument("--host", default="127.0.0.1")
    w.add_argument("--port", type=int, default=5700)
    w.add_argument("--start-watcher", action="store_true",
                   help="also start the file watcher as a child process")

    args = p.parse_args(argv)
    if not args.cmd:
        p.print_help()
        return 0
    return _COMMANDS[args.cmd](args)


if __name__ == "__main__":
    raise SystemExit(main())
