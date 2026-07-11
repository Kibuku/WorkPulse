"""
platform_util.py — one place for OS-specific calls, so nothing stays Mac-only.

WorkPulse ships on macOS AND Windows in parallel. Every cross-cutting OS
operation (process detection, desktop notifications, scheduled-task health)
routes through here with a branch per platform. New features call these
helpers instead of hardcoding osascript/launchctl, so the parallel build
never accumulates Mac-only debt.

Testable: the pure-logic helpers (is_mac/is_windows, count_processes given a
fake iterator) are unit-tested. The OS-touching paths (notify, schtasks) are
written against documented APIs and marked where they need real-Windows
verification.
"""

from __future__ import annotations

import subprocess
import sys

import psutil


def is_mac() -> bool:
    return sys.platform == "darwin"


def is_windows() -> bool:
    return sys.platform.startswith("win")


# ── process detection (cross-platform via psutil, not pgrep) ────────────────

def count_processes(module: str) -> int:
    """Count running processes for a WorkPulse module, matching BOTH launch
    forms regardless of subpackage:
      `python -m workpulse.signals.<module>`  (token endswith ".<module>")
      `.../workpulse/signals/<module>.py`     (token endswith "/<module>.py")

    Package-agnostic so it keeps working after restructures. Uses psutil so
    it works identically on macOS and Windows — the old pgrep path was
    Unix-only and left the Windows doctor unable to see its own sensors.
    """
    n = 0
    for proc in psutil.process_iter(["cmdline"]):
        try:
            tokens = proc.info["cmdline"] or []
            if any("status" == t for t in tokens):
                continue
            hit = any(
                t == module
                or t.endswith(f".{module}")            # -m dotted path
                or t.endswith(f"/{module}.py")          # posix path
                or t.endswith(f"\\{module}.py")         # windows path
                for t in tokens
            )
            if hit:
                n += 1
        except (psutil.NoSuchProcess, psutil.AccessDenied):
            pass
    return n


# ── desktop notifications ───────────────────────────────────────────────────

def notify(title: str, message: str) -> bool:
    """Fire a desktop notification. Returns True on best-effort success.

    macOS  : osascript `display notification`
    Windows: PowerShell toast via the Windows.UI.Notifications API (no extra
             dependency). Written against the documented WinRT toast XML;
             needs verification on a real Windows box.
    """
    t = (title or "").replace('"', "'")[:120]
    m = (message or "").replace('"', "'")[:240]
    try:
        if is_mac():
            subprocess.run(
                ["osascript", "-e",
                 f'display notification "{m}" with title "{t}" sound name "Basso"'],
                capture_output=True, timeout=6,
            )
            return True
        if is_windows():
            # PowerShell toast — no third-party package. Escapes for the
            # single-quoted PowerShell strings below.
            tp = t.replace("'", "''")
            mp = m.replace("'", "''")
            ps = (
                "[Windows.UI.Notifications.ToastNotificationManager, "
                "Windows.UI.Notifications, ContentType = WindowsRuntime] > $null; "
                "$xml = [Windows.UI.Notifications.ToastNotificationManager]::"
                "GetTemplateContent("
                "[Windows.UI.Notifications.ToastTemplateType]::ToastText02); "
                "$t = $xml.GetElementsByTagName('text'); "
                f"$t.Item(0).AppendChild($xml.CreateTextNode('{tp}')) > $null; "
                f"$t.Item(1).AppendChild($xml.CreateTextNode('{mp}')) > $null; "
                "$toast = [Windows.UI.Notifications.ToastNotification]::new($xml); "
                "[Windows.UI.Notifications.ToastNotificationManager]::"
                "CreateToastNotifier('WorkPulse').Show($toast);"
            )
            subprocess.run(
                ["powershell", "-NoProfile", "-NonInteractive", "-Command", ps],
                capture_output=True, timeout=10,
            )
            return True
    except Exception:
        return False
    return False


# ── scheduled-task health ───────────────────────────────────────────────────

def agent_last_exit(slug: str) -> tuple[bool, int | None]:
    """Return (loaded, last_exit_code) for a WorkPulse scheduled agent.

    macOS  : `launchctl print gui/<uid>/com.workpulse.<slug>`
    Windows: `schtasks /Query /TN com.workpulse.<slug> /FO LIST /V`, reading
             the "Last Result" line. Written against schtasks output; needs
             verification on a real Windows box.

    loaded=False means the agent isn't registered at all. last_exit_code is
    None when it's never run (or couldn't be parsed).
    """
    label = f"com.workpulse.{slug}"
    try:
        if is_mac():
            uid = subprocess.run(["id", "-u"], capture_output=True, text=True).stdout.strip()
            r = subprocess.run(["launchctl", "print", f"gui/{uid}/{label}"],
                               capture_output=True, text=True)
            if r.returncode != 0:
                return (False, None)
            for line in r.stdout.splitlines():
                if "last exit code" in line.lower():
                    digits = "".join(c for c in line.split("=")[-1] if c.isdigit())
                    return (True, int(digits) if digits else None)
            return (True, None)
        if is_windows():
            r = subprocess.run(
                ["schtasks", "/Query", "/TN", label, "/FO", "LIST", "/V"],
                capture_output=True, text=True,
            )
            if r.returncode != 0:
                return (False, None)
            for line in r.stdout.splitlines():
                if "last result" in line.lower():
                    # "Last Result:  0" (may be hex like 0x0)
                    val = line.split(":", 1)[-1].strip()
                    try:
                        code = int(val, 0)  # handles 0, 0x0, etc.
                        return (True, code)
                    except ValueError:
                        return (True, None)
            return (True, None)
    except Exception:
        return (False, None)
    return (False, None)
