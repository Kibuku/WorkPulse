"""Consent-based, ephemeral active-window content capture.

Screenshots exist only in a private temporary directory and are deleted in a
finally block. Only redacted OCR text and derived meaning may be persisted.
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import sys
import tempfile
from datetime import datetime, timezone
from pathlib import Path

from workpulse.core import semantics
from workpulse.core.atoms import new_id

DEFAULT_DENY_APPS = ("1password", "bitwarden", "keychain", "password", "wallet")
DEFAULT_DENY_TERMS = ("bank", "medical", "health", "private browsing", "incognito")

_REDACTIONS = (
    (re.compile(r"[\w.+-]+@[\w.-]+\.[A-Za-z]{2,}"), "[EMAIL]"),
    (re.compile(r"(?<!\d)(?:\+?\d[\d ()-]{7,}\d)(?!\d)"), "[PHONE]"),
    (re.compile(r"\b(?:\d[ -]*?){13,19}\b"), "[PAYMENT_NUMBER]"),
    (re.compile(r"(?i)\b(?:password|passcode|pin|otp)\s*[:=]\s*\S+"), "[SECRET]"),
    (re.compile(r"\b[A-Za-z0-9_-]{24,}\b"), "[TOKEN]"),
)


def redact(text: str, extra_patterns: list[str] | None = None) -> str:
    out = text or ""
    for pattern, replacement in _REDACTIONS:
        out = pattern.sub(replacement, out)
    for raw in extra_patterns or []:
        if raw:
            out = re.sub(re.escape(raw), "[PRIVATE]", out, flags=re.I)
    return "\n".join(line.strip() for line in out.splitlines() if line.strip())[:12000]


def allowed(app: str, title: str, cfg: dict) -> tuple[bool, str]:
    content = cfg.get("content_capture") or {}
    if not content.get("enabled", False):
        return False, "content capture is off"
    haystack = f"{app} {title}".casefold()
    terms = [*DEFAULT_DENY_APPS, *DEFAULT_DENY_TERMS,
             *(str(x).casefold() for x in content.get("deny_terms", []))]
    match = next((term for term in terms if term and term in haystack), None)
    if match:
        return False, "active window matches a private exclusion"
    allow_apps = [str(x).casefold() for x in content.get("allow_apps", [])]
    if allow_apps and not any(x in app.casefold() for x in allow_apps):
        return False, "active application is not in the content-capture allow list"
    return True, "allowed"


def _foreground() -> tuple[str, int, str] | None:
    if sys.platform == "darwin":
        from workpulse.signals.activity_mac import foreground_window
    elif sys.platform == "win32":
        from workpulse.signals.activity_win import foreground_window
    else:
        return None
    return foreground_window()


def _capture_image(path: Path, pid: int) -> None:
    if sys.platform == "darwin":
        from Quartz import (CGWindowListCopyWindowInfo,
                            kCGWindowListOptionOnScreenOnly, kCGNullWindowID)
        windows = CGWindowListCopyWindowInfo(kCGWindowListOptionOnScreenOnly,
                                             kCGNullWindowID) or []
        window_id = next((int(w.get("kCGWindowNumber")) for w in windows
                          if int(w.get("kCGWindowOwnerPID", 0) or 0) == pid
                          and int(w.get("kCGWindowLayer", 0) or 0) == 0), None)
        if not window_id:
            raise RuntimeError("could not isolate the active window")
        subprocess.run(["screencapture", "-x", "-l", str(window_id), str(path)], check=True,
                       capture_output=True, timeout=15)
        return
    if sys.platform == "win32":
        safe_path = str(path).replace("'", "''")
        script = (
            "Add-Type -AssemblyName System.Windows.Forms;"
            "Add-Type -AssemblyName System.Drawing;"
            "Add-Type -TypeDefinition 'using System;using System.Runtime.InteropServices;"
            "public class W{[DllImport(\"user32.dll\")]public static extern IntPtr GetForegroundWindow();"
            "[DllImport(\"user32.dll\")]public static extern bool GetWindowRect(IntPtr h,out R r);"
            "public struct R{public int L,T,Ri,B;}}';"
            "$r=New-Object W+R;[W]::GetWindowRect([W]::GetForegroundWindow(),[ref]$r)|Out-Null;"
            "$b=New-Object Drawing.Rectangle $r.L,$r.T,($r.Ri-$r.L),($r.B-$r.T);"
            "if($b.Width -le 0 -or $b.Height -le 0){throw 'no active window bounds'};"
            "$i=New-Object Drawing.Bitmap $b.Width,$b.Height;"
            "$g=[Drawing.Graphics]::FromImage($i);"
            "$g.CopyFromScreen($b.Location,[Drawing.Point]::Empty,$b.Size);"
            f"$i.Save('{safe_path}');$g.Dispose();$i.Dispose()"
        )
        subprocess.run(["powershell", "-NoProfile", "-Command", script], check=True,
                       capture_output=True, timeout=20)
        return
    raise RuntimeError("content capture supports macOS and Windows")


def _ocr(path: Path) -> tuple[str, str]:
    # Tesseract is a local process. Installers can bundle/provision it without
    # changing this privacy boundary.
    exe = _tesseract_path()
    if not exe:
        raise RuntimeError("local OCR engine is not installed")
    result = subprocess.run([exe, str(path), "stdout", "--psm", "6"],
                            text=True, capture_output=True, timeout=45)
    if result.returncode:
        raise RuntimeError("local OCR failed")
    return result.stdout, "tesseract-local"


def ocr_ready() -> tuple[bool, str | None]:
    if _tesseract_path():
        return True, "tesseract-local"
    return False, None


def _tesseract_path() -> str | None:
    found = shutil.which("tesseract")
    if found:
        return found
    bundle_root = getattr(sys, "_MEIPASS", None)
    if bundle_root:
        bundled_name = "tesseract.exe" if sys.platform == "win32" else "tesseract"
        bundled = Path(bundle_root) / "tesseract" / bundled_name
        if bundled.is_file() and os.access(bundled, os.X_OK):
            return str(bundled)
    for candidate in ("/opt/homebrew/bin/tesseract", "/usr/local/bin/tesseract"):
        if Path(candidate).is_file() and os.access(candidate, os.X_OK):
            return candidate
    return None


def capture_once(cfg: dict, *, capture_image=_capture_image, ocr=_ocr) -> dict:
    fg = _foreground()
    if not fg:
        return {"ok": False, "reason": "could not identify the active window"}
    title, _pid, app = fg
    ok, reason = allowed(app, title, cfg)
    if not ok:
        return {"ok": False, "reason": reason, "blocked": True}
    temp_dir = Path(tempfile.mkdtemp(prefix="workpulse-content-"))
    try:
        try:
            os.chmod(temp_dir, 0o700)
        except OSError:
            pass
        image = temp_dir / "active-window.png"
        capture_image(image, _pid)
        raw, engine = ocr(image)
        content = cfg.get("content_capture") or {}
        redacted = redact(raw, content.get("redaction_terms") or [])
        if len(redacted) < 20:
            return {"ok": False, "reason": "not enough readable text", "engine": engine}
        stage = semantics.classify_stage(redacted, app)
        return {"ok": True, "engine": engine, "app": app, "stage": stage,
                "redacted_text": redacted,
                "privacy": "Screenshot deleted immediately after local OCR."}
    finally:
        shutil.rmtree(temp_dir, ignore_errors=True)


def persist(con, result: dict) -> dict:
    if not result.get("ok"):
        raise ValueError("only successful content captures can be persisted")
    capture_id = new_id()
    con.execute(
        "INSERT INTO content_capture(id,ts,app,stage,redacted_text,ocr_engine) VALUES (?,?,?,?,?,?)",
        (capture_id, datetime.now(timezone.utc).isoformat(), result["app"],
         result.get("stage"), result["redacted_text"], result["engine"]),
    )
    con.commit()
    return {"id": capture_id, "stage": result.get("stage"), "engine": result["engine"]}
