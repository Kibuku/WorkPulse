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
    from Foundation import NSRunLoop, NSDate
    from Quartz import (
        CGWindowListCopyWindowInfo,
        kCGWindowListOptionOnScreenOnly,
        kCGWindowListExcludeDesktopElements,
        kCGNullWindowID,
        CGEventSourceSecondsSinceLastEventType,
        kCGEventSourceStateHIDSystemState,
        CGSessionCopyCurrentDictionary,
    )
    _IMPORTS_OK = True
except ImportError as e:
    log.warning("PyObjC imports failed (%s); activity_mac will return None.", e)
    _IMPORTS_OK = False


# ── lock-screen / loginwindow detection (PLAN.md §7 step 7c) ─────────────────
# When the Mac is locked, the frontmost app is `loginwindow` (Apple's lock-
# screen process). Step 8's dream cycle surfaced 73 hours of "loginwindow"
# sessions on the real DB — the sensor was logging the lock screen as active
# work. The fix is two-fold: detect the state at sample time (`is_screen_locked`
# via CGSessionCopyCurrentDictionary), and recognize the well-known lockscreen
# app names as a fallback (`is_lockscreen_app`). `activity.py` skips writes
# entirely when either check fires.

# App names that mean "user is not actually working." Lowercased for compare.
_LOCKSCREEN_APP_NAMES = frozenset({
    "loginwindow",       # primary lock screen on macOS
    "lock screen",
    "screensaverengine", # the screensaver process
    "screensaver",
})


def is_lockscreen_app(app_name: str | None) -> bool:
    """True if the given app name is a known lock-screen / screensaver process.
    Pure function — testable without macOS APIs."""
    if not app_name:
        return False
    return app_name.strip().lower() in _LOCKSCREEN_APP_NAMES


def is_screen_locked() -> bool:
    """True if the macOS screen is locked. Uses CGSessionCopyCurrentDictionary
    which returns a dict containing 'CGSSessionScreenIsLocked' when locked.
    Returns False on any failure — better to log a session than to silently
    drop legitimate work."""
    if not _IMPORTS_OK:
        return False
    try:
        d = CGSessionCopyCurrentDictionary()
        if not d:
            return False
        return bool(d.get("CGSSessionScreenIsLocked", False))
    except Exception as e:
        log.debug("CGSessionCopyCurrentDictionary failed: %s", e)
        return False


# kCGAnyInputEventType is documented as ~0 (UINT32_MAX) — "all event types".
# PyObjC sometimes exposes this constant, sometimes not, depending on version.
# Hardcode the value (it's stable in the Apple headers).
_K_CG_ANY_INPUT_EVENT_TYPE = ~0 & 0xFFFFFFFF


def imports_ok() -> bool:
    """True iff PyObjC was importable. Useful for setup-script sanity checks."""
    return _IMPORTS_OK


def foreground_window() -> tuple[str, int, str] | None:
    """Return (title, pid, app_name) of the frontmost window, or None.

    THE frontmost app comes from NSWorkspace.frontmostApplication() AFTER
    pumping the Cocoa run loop. This is the culmination of a long debugging
    saga — three approaches, only the third works:

      1. NSWorkspace WITHOUT a run loop: its frontmost value updates via
         Cocoa workspace notifications, and those are only delivered when a
         run loop is pumped. activity.py is a plain `while True: sleep()`
         loop with no run loop, so the value FREEZES on whatever app was
         active at process start (logged "Claude" for two days, "Safari"
         for a morning, regardless of actual use).
      2. CGWindowList "first on-screen layer-0 window": tracks changes but
         is UNRELIABLE — it returns the wrong app whenever the frontmost app
         has no normal window (Finder-on-desktop, Teams, Excel between
         sheets). Measured mismatches against ground truth.
      3. NSWorkspace + a brief run-loop pump each call: matched System
         Events (the ground truth) on every read in testing. Native, no
         subprocess per sample, no extra Automation permission. This is it.

    CGWindowList is still used, but ONLY to look up the window TITLE for the
    frontmost pid — not to decide which app is frontmost.
    """
    if not _IMPORTS_OK:
        return None
    try:
        # Pump the run loop briefly so pending workspace notifications land,
        # unfreezing NSWorkspace.frontmostApplication().
        NSRunLoop.currentRunLoop().runUntilDate_(
            NSDate.dateWithTimeIntervalSinceNow_(0.15))
        app = NSWorkspace.sharedWorkspace().frontmostApplication()
        if app is None:
            return None
        pid = int(app.processIdentifier())
        app_name = app.localizedName() or ""

        # Title (best-effort) from CGWindowList for THIS pid. Empty without
        # Screen-Recording / Accessibility permission; app name is the
        # fallback so the log always has signal.
        title = ""
        try:
            opts = kCGWindowListOptionOnScreenOnly | kCGWindowListExcludeDesktopElements
            for w in (CGWindowListCopyWindowInfo(opts, kCGNullWindowID) or []):
                if int(w.get("kCGWindowOwnerPID", 0) or 0) != pid:
                    continue
                if int(w.get("kCGWindowLayer", 0) or 0) != 0:
                    continue
                name = w.get("kCGWindowName")
                if name:
                    title = name
                    break
        except Exception:
            pass
        if not title:
            title = app_name
        return title, pid, app_name
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
