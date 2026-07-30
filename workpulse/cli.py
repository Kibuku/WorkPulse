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
import copy
import shutil
import subprocess
import sys
from pathlib import Path

from workpulse import __version__
from workpulse.common import BUNDLE_ROOT, ROOT, enable_utf8_console


def _deep_merge_defaults(user: dict, template: dict, prefix: str = "") -> list[str]:
    """Add keys present in `template` but missing in `user` (recursively),
    never overwriting an existing user value. Returns the added key paths.
    This is what makes updates seamless: new settings a release introduces get
    added to the user's config.yaml while their own choices are preserved."""
    added: list[str] = []
    for k, tv in (template or {}).items():
        path = f"{prefix}{k}"
        if k not in user:
            user[k] = copy.deepcopy(tv)
            added.append(path)
        elif isinstance(user.get(k), dict) and isinstance(tv, dict):
            added += _deep_merge_defaults(user[k], tv, path + ".")
    return added


def _ensure_config() -> Path:
    import yaml
    cfg_path = ROOT / "config" / "config.yaml"
    resource_root = BUNDLE_ROOT if getattr(sys, "frozen", False) else ROOT
    example  = resource_root / "config" / "config.example.yaml"

    if not cfg_path.exists():
        if example.exists():
            cfg_path.parent.mkdir(parents=True, exist_ok=True)
            shutil.copyfile(example, cfg_path)
            print(f"  config    created {cfg_path} from template")
        else:
            print("  config    WARNING: no template found — create config/config.yaml by hand",
                  file=sys.stderr)
        return cfg_path

    if not example.exists():
        print(f"  config    using existing {cfg_path}")
        return cfg_path

    # Exists → merge in any new keys the template gained, preserving user values.
    user = yaml.safe_load(cfg_path.read_text(encoding="utf-8")) or {}
    tmpl = yaml.safe_load(example.read_text(encoding="utf-8")) or {}
    added = _deep_merge_defaults(user, tmpl)
    if added:
        cfg_path.write_text(
            yaml.safe_dump(user, sort_keys=False, allow_unicode=True), encoding="utf-8")
        shown = ", ".join(added[:6]) + (" …" if len(added) > 6 else "")
        print(f"  config    added {len(added)} new setting(s) to {cfg_path}: {shown}")
    else:
        print(f"  config    using existing {cfg_path} (up to date)")
    return cfg_path


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
    print("  1. Start the dashboard:   workpulse web    -> http://127.0.0.1:5700")
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
    # Source-based Windows test installs commonly launch `workpulse web`
    # directly rather than the tray application. Keep that path functional:
    # the dashboard must not be alive while both sensors remain down.
    if sys.platform == "win32":
        app.start_watcher()
        app.start_activity()
    app.run(host=args.host, port=args.port,
            start_watcher_on_launch=args.start_watcher)
    return 0


def cmd_version(args: argparse.Namespace) -> int:
    print(f"WorkPulse {__version__}")
    return 0


def cmd_classroom_agent(args: argparse.Namespace) -> int:
    from workpulse import classroom_agent
    return classroom_agent.main(args.agent_args)


def cmd_classroom_gateway(args: argparse.Namespace) -> int:
    from workpulse.classroom_gateway import run
    run(host=args.host, port=args.port)
    return 0


def _run(cmd: list[str]) -> tuple[int, str]:
    r = subprocess.run(cmd, capture_output=True, text=True)
    return r.returncode, (r.stdout + r.stderr).strip()


def cmd_update(args: argparse.Namespace) -> int:
    """Pull the latest version from GitHub and apply it in place.

    Seamless by construction: config.yaml is gitignored (so `git pull` never
    conflicts on it), DB migrations are forward-only (applied on connect), and
    the apply step runs in a fresh process so it uses the just-pulled code."""
    print(f"WorkPulse — update (currently {__version__})")
    print("=" * 52)
    if not (ROOT / ".git").exists():
        print("  Not a git checkout — nothing to pull. Re-clone from GitHub, or run")
        print("  `workpulse install` to re-apply config + agents.")
        return 1

    print("  git       pulling latest…")
    rc, out = _run(["git", "-C", str(ROOT), "pull", "--ff-only"])
    print("    " + out.replace("\n", "\n    "))
    if rc != 0:
        print("  git pull failed (local changes or diverged history). Resolve, then retry.",
              file=sys.stderr)
        return rc

    print("  deps      syncing (fast when unchanged)…")
    extra = "mac" if sys.platform == "darwin" else "win" if sys.platform == "win32" else ""
    spec = f".[{extra}]" if extra else "."
    rc, out = _run([sys.executable, "-m", "pip", "install", "-e", spec])
    if rc != 0:
        print("    " + out.replace("\n", "\n    "), file=sys.stderr)
        return rc

    # Apply config merge + migrations + agent refresh with the NEW code.
    print("  apply     migrating config, database, and agents…")
    rc, out = _run([sys.executable, "-m", "workpulse.cli", "install"])
    print("    " + out.replace("\n", "\n    "))
    print("\nUpdate complete. If the dashboard was running, restart it: workpulse web")
    return rc


_COMMANDS = {
    "install":   cmd_install,
    "uninstall": cmd_uninstall,
    "update":    cmd_update,
    "status":    cmd_status,
    "doctor":    cmd_doctor,
    "web":       cmd_web,
    "version":   cmd_version,
    "classroom-agent": cmd_classroom_agent,
    "classroom-gateway": cmd_classroom_gateway,
}


def main(argv: list[str] | None = None) -> int:
    enable_utf8_console()  # never crash on Windows cp1252 consoles
    p = argparse.ArgumentParser(prog="workpulse",
                                description="WorkPulse — a local-first attention brain.")
    sub = p.add_subparsers(dest="cmd")
    sub.add_parser("install",   help="set up config, database, and background agents")
    sub.add_parser("uninstall", help="remove the background agents")
    sub.add_parser("update",    help="pull the latest version from GitHub and apply it")
    sub.add_parser("status",    help="show agents + a health summary")
    sub.add_parser("doctor",    help="run the health checks")
    sub.add_parser("version",   help="print the installed version")
    ca = sub.add_parser("classroom-agent",
                        help="enrol or run this computer as a Classroom device")
    ca.add_argument("agent_args", nargs=argparse.REMAINDER)
    cg = sub.add_parser("classroom-gateway",
                        help="run the restricted Classroom device gateway")
    cg.add_argument("--host", default="0.0.0.0")
    cg.add_argument("--port", type=int, default=5722)
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
