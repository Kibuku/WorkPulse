"""
workpulse.menubar — WorkPulse's home in the macOS menu bar.

v2 runs the dashboard as a headless web server (the com.workpulse.dashboard
agent), so unlike v1 there was nothing to *see* when you logged in. This is the
quiet menu-bar presence that fixes that: a small icon that opens the dashboard
on click and shows at a glance whether the sensors are live.

It deliberately does NOT run the server itself — it only points at it — so there
is never a fight over port 5700. Runs at login via com.workpulse.menubar
(launchd, Aqua session). macOS only (rumps / PyObjC); a Windows tray is a
separate build.

    python -m workpulse.menubar
"""
from __future__ import annotations

import subprocess
import urllib.request

from workpulse.product import current as current_product

try:
    import rumps
except ImportError:  # only importable on a mac with the [mac] extra installed
    rumps = None

DASH_BASE_URL = "http://127.0.0.1:5700"


def dashboard_url() -> str:
    return f"{DASH_BASE_URL}{current_product().entry_path}"


def _server_up(timeout: float = 1.5) -> bool:
    """True when the dashboard answers on 5700 (sensors + web are alive)."""
    try:
        with urllib.request.urlopen(DASH_BASE_URL, timeout=timeout) as r:
            return getattr(r, "status", 200) == 200
    except Exception:
        return False


def _open_dashboard(_=None) -> None:
    # Chrome per house preference; fall back to the default browser.
    try:
        subprocess.Popen(["open", "-a", "Google Chrome", dashboard_url()])
    except Exception:
        subprocess.Popen(["open", dashboard_url()])


if rumps is not None:

    class WorkPulseBar(rumps.App):
        """The menu-bar item. A glyph title (no bundled icon yet), a click-to-open
        action, a live status line, and a quit that stops only the menu bar — not
        the sensors or the server."""

        def __init__(self):
            super().__init__("WorkPulse", title="◉", quit_button=None)
            self.status = rumps.MenuItem("Checking…")
            self.menu = [
                rumps.MenuItem("Open WorkPulse", callback=self._open),
                None,
                self.status,
                None,
                rumps.MenuItem("Quit", callback=self._quit),
            ]
            self._tick(None)  # set the status line straight away, not after 15s

        def _open(self, _):
            _open_dashboard()

        def _quit(self, _):
            rumps.quit_application()

        @rumps.timer(15)
        def _tick(self, _):
            self.status.title = "● Sensors running" if _server_up() else "○ Server offline"


def run() -> None:
    if rumps is None:
        raise SystemExit(
            "The menu-bar app needs rumps. Install it with:\n"
            "    pip install rumps\n"
            "or reinstall WorkPulse with the mac extra:  pip install -e \".[mac]\"")
    WorkPulseBar().run()


if __name__ == "__main__":
    run()
