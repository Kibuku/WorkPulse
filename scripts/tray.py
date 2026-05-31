"""
tray.py — WorkPulse system tray entry point.

Launches at logon via Task Scheduler. Starts the dashboard server
and watcher, then sits in the system tray with a right-click menu.

Run:
  python scripts\\tray.py
"""

from __future__ import annotations

import asyncio
import logging
import os
import sys
import threading
import traceback
import webbrowser
from pathlib import Path

import pystray
import uvicorn
from PIL import Image, ImageDraw

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))


# ── load user-scope env vars before importing anything that needs them ────────
# Windows User-scope env vars (set via System Properties / setx) propagate
# into a process only if its parent inherited them. When the tray is launched
# from a non-explorer parent (PowerShell from Bash, Task Scheduler with no
# user env block, etc.) ANTHROPIC_API_KEY and WORKPULSE_SMTP_PASSWORD go
# missing — and the AI classifier silently no-ops. We pull them ourselves so
# the tray works regardless of who launched it.

def _load_user_env_vars() -> None:
    needed = ("ANTHROPIC_API_KEY", "WORKPULSE_SMTP_PASSWORD")
    if all(os.environ.get(n) for n in needed):
        return
    try:
        import winreg
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, r"Environment") as k:
            for name in needed:
                if os.environ.get(name):
                    continue
                try:
                    val, _ = winreg.QueryValueEx(k, name)
                    if val:
                        os.environ[name] = val
                except FileNotFoundError:
                    pass
    except Exception:
        pass

_load_user_env_vars()


from scripts.app import app as fastapi_app, PORT
from scripts.app import is_watcher_running, start_watcher, stop_watcher
from scripts.app import is_activity_running, start_activity, stop_activity

# ── crash-proof logging ───────────────────────────────────────────────────────
# pythonw.exe has no console; without a log we never see startup exceptions.

_LOG_PATH = Path(__file__).resolve().parent.parent / "logs" / "tray.log"
_LOG_PATH.parent.mkdir(parents=True, exist_ok=True)
logging.basicConfig(
    filename=str(_LOG_PATH),
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(message)s",
)
log = logging.getLogger("tray")

# ── icon drawing ──────────────────────────────────────────────────────────────

def _make_icon(running: bool) -> Image.Image:
    """Draw a 64x64 circle — green if running, grey if stopped."""
    img = Image.new("RGBA", (64, 64), (0, 0, 0, 0))
    draw = ImageDraw.Draw(img)
    color = "#22c55e" if running else "#6b7280"
    # Outer circle
    draw.ellipse([4, 4, 60, 60], fill=color)
    # Inner 'W' mark — white dot in centre
    draw.ellipse([26, 26, 38, 38], fill="#ffffff")
    return img


# ── server thread ─────────────────────────────────────────────────────────────

def _start_server():
    """Run uvicorn inside a daemon thread.

    `uvicorn.run()` tries to install signal handlers on the current event loop —
    that only works on the main thread, so it crashes silently when called from
    a worker thread. Use Config + Server.run() with install_signal_handlers=False
    instead.
    """
    try:
        config = uvicorn.Config(
            app=fastapi_app,
            host="127.0.0.1",
            port=PORT,
            log_level="warning",
            # log_config=None: skip uvicorn's default formatter, which calls
            # sys.stdout.isatty() and crashes under pythonw.exe (stdout is None).
            log_config=None,
            access_log=False,
        )
        server = uvicorn.Server(config)
        server.config.install_signal_handlers = False  # safe in non-main thread
        # On Windows, give the thread its own event loop.
        loop = asyncio.new_event_loop()
        asyncio.set_event_loop(loop)
        log.info("starting FastAPI server on http://127.0.0.1:%d", PORT)
        loop.run_until_complete(server.serve())
    except Exception:
        log.error("FastAPI server crashed:\n%s", traceback.format_exc())


# ── tray menu actions ─────────────────────────────────────────────────────────

def _open_dashboard(icon, item):
    webbrowser.open(f"http://127.0.0.1:{PORT}")


def _toggle_watcher(icon, item):
    if is_watcher_running():
        stop_watcher()
    else:
        start_watcher()
    _update_icon(icon)


def _update_icon(icon):
    running = is_watcher_running()
    icon.icon = _make_icon(running)
    icon.title = "WorkPulse — Running" if running else "WorkPulse — Stopped"
    icon.menu = _build_menu()


def _build_menu():
    running = is_watcher_running()
    toggle_label = "Stop Watcher" if running else "Start Watcher"
    return pystray.Menu(
        pystray.MenuItem("Open Dashboard", _open_dashboard, default=True),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem(toggle_label, _toggle_watcher),
        pystray.Menu.SEPARATOR,
        pystray.MenuItem("Quit WorkPulse", _quit),
    )


def _quit(icon, item):
    stop_watcher()
    stop_activity()
    icon.stop()


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    # Start FastAPI server in background thread
    server_thread = threading.Thread(target=_start_server, daemon=True)
    server_thread.start()

    # Start watcher + activity tracker
    start_watcher()
    start_activity()

    # Build tray icon
    running = is_watcher_running()
    icon = pystray.Icon(
        name="WorkPulse",
        icon=_make_icon(running),
        title="WorkPulse — Running" if running else "WorkPulse — Stopped",
        menu=_build_menu(),
    )

    # Open dashboard on first launch.
    # At Windows logon the tray fires within seconds — the default browser
    # and explorer aren't ready yet, so a 1-2s delay loses the tab silently.
    # Wait until the FastAPI server is actually accepting connections, then
    # open. Cap the wait so a broken server doesn't block forever.
    def _open_when_ready():
        import socket, time
        url = f"http://127.0.0.1:{PORT}"
        deadline = time.monotonic() + 30  # up to 30s for server + browser
        while time.monotonic() < deadline:
            try:
                with socket.create_connection(("127.0.0.1", PORT), timeout=0.5):
                    break
            except OSError:
                time.sleep(0.5)
        else:
            log.warning("dashboard server never came up within 30s; not opening browser")
            return
        # Server is alive — give the user's browser a moment to finish booting
        # if this is logon, then open.
        time.sleep(5)
        try:
            opened = webbrowser.open(url)
            log.info("dashboard auto-open requested (%s) -> %s", url, opened)
        except Exception:
            log.error("webbrowser.open failed:\n%s", traceback.format_exc())

    threading.Thread(target=_open_when_ready, daemon=True).start()

    icon.run()


if __name__ == "__main__":
    try:
        log.info("=== WorkPulse tray starting ===")
        main()
    except Exception:
        log.error("tray crashed:\n%s", traceback.format_exc())
        raise
