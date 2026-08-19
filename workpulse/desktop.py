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


_HOME_NAMES = {
    ("personal", "individual"): "Personal",
    ("institution", "member"): "Institution",
    ("developer", "lab"): "DeveloperLab",
    ("learning", "facilitator"): "LearningFacilitator",
    ("learning", "device"): "LearningDevice",
}


def _runtime_flavour(argv: list[str]) -> tuple[str, str]:
    """Infer flavour before importing common.py (which freezes ROOT).

    Install commands carry explicit values. Normal launches infer them from the
    flavour-specific application/install directory. Unknown/legacy executables
    remain Personal Pulse for backwards compatibility.
    """
    try:
        p = argv.index("--product")
        r = argv.index("--role")
        pair = (argv[p + 1].lower(), argv[r + 1].lower().replace("_", "-"))
        if pair == ("learning", "learning-device"):
            pair = ("learning", "device")
        if pair in _HOME_NAMES:
            return pair
    except (ValueError, IndexError):
        pass
    runtime = str(Path(sys.executable).resolve()).lower()
    if "workpulseinstitution" in runtime or "workpulse institution" in runtime:
        return "institution", "member"
    if "learningpulsefacilitator" in runtime or "learningpulse facilitator" in runtime:
        return "learning", "facilitator"
    if "learningpulsedevice" in runtime or "learningpulse device" in runtime:
        return "learning", "device"
    return "personal", "individual"


def _default_home(flavour: tuple[str, str]) -> Path:
    name = _HOME_NAMES[flavour]
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "Pulse" / name
    if sys.platform == "win32":
        return Path(os.environ.get("LOCALAPPDATA", Path.home())) / "Pulse" / name
    return Path.home() / ".pulse" / name.lower()


def _prepare_home(argv: list[str]) -> Path:
    home = Path(os.environ.setdefault(
        "WORKPULSE_HOME", str(_default_home(_runtime_flavour(argv)))))
    home.mkdir(parents=True, exist_ok=True)
    os.chdir(home)
    return home


def _run_agent(module: str, args: list[str]) -> int:
    sys.argv = [module, *args]
    runpy.run_module(module, run_name="__main__")
    return 0


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    _prepare_home(argv)
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
