"""Frozen desktop entry point for consumer WorkPulse installers.

One bundled executable serves three roles:
  * normal launch: Windows tray or macOS menu-bar companion
  * --cli ...: installation/health commands used by the native installer
  * --agent module ...: background jobs registered with launchd/schtasks

The environment is established before importing the rest of WorkPulse so all
runtime data lands in a writable per-user directory, never inside the app.
"""

from __future__ import annotations

import os
import runpy
import sys
from pathlib import Path


def _default_home() -> Path:
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "WorkPulse"
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "WorkPulse"
    return Path.home() / ".workpulse"


def _prepare_home() -> Path:
    home = Path(os.environ.setdefault("WORKPULSE_HOME", str(_default_home())))
    home.mkdir(parents=True, exist_ok=True)
    os.chdir(home)
    return home


def _run_agent(module: str, args: list[str]) -> int:
    sys.argv = [module, *args]
    runpy.run_module(module, run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    _prepare_home()
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv[:1] == ["--agent"]:
        if len(argv) < 2:
            raise SystemExit("--agent requires a module name")
        return _run_agent(argv[1], argv[2:])
    if argv[:1] == ["--cli"]:
        from workpulse.cli import main as cli_main
        return cli_main(argv[1:])
    if sys.platform == "darwin":
        from workpulse.menubar import run
        run()
        return 0
    if sys.platform == "win32":
        from workpulse.ops.tray import main as tray_main
        tray_main()
        return 0
    raise SystemExit(f"WorkPulse desktop is unsupported on {sys.platform}")


if __name__ == "__main__":
    raise SystemExit(main())
