"""
activity_mac.py — macOS backend for the activity tracker.

Provides foreground_window() + idle_seconds() against AppKit + Quartz via
PyObjC. Imported only when sys.platform == "darwin"; see activity.py for
the platform switch.

Dependencies (installed via requirements-mac.txt):
    pyobjc-framework-Cocoa     (NSWorkspace)
    pyobjc-framework-Quartz    (CGWindowList*, CGEventSource*)

Critical permission requirement
───────────────────────────────
macOS will return EMPTY window titles to CGWindowListCopyWindowInfo unless
the calling process is granted *Accessibility* permission. This is by
Apple design — the OS treats reading other apps' window titles as a
sensitive capability.

To enable real window-title tracking, the user must, once per install:
    System Settings → Privacy & Security → Accessibility → [+]
    Add the python interpreter that runs WorkPulse:
        <repo>/.venv/bin/python
    (or for the .pkg install, the bundled python inside the .app)
    Toggle it on.

Without the permission, WorkPulse still tracks app-level activity (which
app is frontmost, idle state) — just not the per-window title. The
dashboard will look thin until the permission is granted; once it is,
titles populate immediately on the next sample.
"""

from __future__ import annotations

import logging

log = logging.getLogger("activity_mac")

# Import guard: pyobjc isn't installed everywhere we might import this
# (e.g. the dashboard's read-time _tag_stream re-tagging on a Windows
# dev box that has Mac sessions in its history). Fail soft so the rest
# of WorkPulse stays usable.
try:
    from AppKit import NSWorkspace
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGWindowListOptionOnScreenOnly,
        kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
        CGEventSourceSecondsSinceLastEventType,
        kCGEventSourceStateHIDSystemState,
    )
    _IMPORTS_OK = True
except ImportError as e:
    log.warning("PyObjC imports failed (%s); activity_mac will return None.", e)
    _IMPORTS_OK = False


# kCGAnyInputEventType is documented as ~0 (UINT32_MAX) — "all event types".
# PyObjC sometimes exposes this constant, sometimes not, depending on version.
# Hardcode the value (it's stable in the Apple headers).
_K_CG_ANY_INPUT_EVENT_TYPE = ~0 & 0xFFFFFFFF


def imports_ok() -> bool:
    """True iff PyObjC was importable. Useful for setup-script sanity checks."""
    return _IMPORTS_OK


def foreground_window() -> tuple[str, int] | None:
    """Return (title, pid) of the frontmost window, or None if unavailable.

    Window title will be the empty string if Accessibility permission has
    not been granted; we fall back to the app's localized name in that case
    so the activity log still has SOMETHING useful.
    """
    if not _IMPORTS_OK:
        return None
    try:
        workspace = NSWorkspace.sharedWorkspace()
        app = workspace.frontmostApplication()
        if app is None:
            return None
        pid = int(app.processIdentifier())
        app_name = app.localizedName() or ""

        # Try to get the actual window title via Quartz
        title = ""
        try:
            opts = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
            windows = CGWindowListCopyWindowInfo(opts, kCGNullWindowID) or []
            for w in windows:
                if w.get("kCGWindowOwnerPID") != pid:
                    continue
                # Skip dock / menu bar / overlay layers (we want normal app windows)
                if int(w.get("kCGWindowLayer", 0) or 0) != 0:
                    continue
                name = w.get("kCGWindowName")
                if name:
                    title = name
                    break
        except Exception as e:
            log.debug("CGWindowListCopyWindowInfo failed: %s", e)

        # Fall back to app name when title is unavailable. This is what happens
        # when Accessibility permission hasn't been granted — the user still
        # gets app-level activity tracking, just no window detail.
        if not title:
            title = app_name

        return title, pid
    except Exception as e:
        log.warning("foreground_window failed: %s", e)
        return None


def idle_seconds() -> float:
    """Seconds since the last mouse, keyboard, or trackpad input. 0.0 on failure."""
    if not _IMPORTS_OK:
        return 0.0
    try:
        return float(CGEventSourceSecondsSinceLastEventType(
            kCGEventSourceStateHIDSystemState,
            _K_CG_ANY_INPUT_EVENT_TYPE,
        ))
    except Exception as e:
        log.debug("CGEventSourceSecondsSinceLastEventType failed: %s", e)
        return 0.0


# ── tiny self-check for manual diagnostic on Mac ─────────────────────────────

if __name__ == "__main__":
    import sys, time
    if sys.platform != "darwin":
        print(f"This module is macOS-only (current platform: {sys.platform}).")
        sys.exit(1)
    if not _IMPORTS_OK:
        print("PyObjC imports failed. Install with:")
        print("    pip install pyobjc-framework-Cocoa pyobjc-framework-Quartz")
        sys.exit(1)
    print("Sampling the foreground window every 2s for 10 ticks.")
    print("Switch between apps to see it update. Ctrl-C to stop.")
    print()
    for i in range(10):
        fg = foreground_window()
        idle = idle_seconds()
        if fg:
            title, pid = fg
            print(f"  [{i:>2}]  pid={pid:>6}  idle={idle:>5.1f}s  title={title!r}")
        else:
            print(f"  [{i:>2}]  (no foreground window)")
        time.sleep(2)
    print()
    print("If titles came back empty, grant Accessibility permission to your")
    print("Python interpreter and re-run. See module docstring for details.")
