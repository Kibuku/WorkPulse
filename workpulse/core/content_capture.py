"""Consent-based, ephemeral active-window content capture.

Screenshots exist only in a private temporary directory and are deleted in a
finally block. Only redacted OCR text and derived meaning may be persisted.
"""
from __future__ import annotations

import json
import os
import re
import shutil
import subprocess
import sys
import tempfile
import time
import urllib.error
import urllib.request
import uuid
from datetime import datetime, timezone
from pathlib import Path

from workpulse.core import semantics
from workpulse.core.atoms import new_id

DEFAULT_DENY_APPS = ("1password", "bitwarden", "keychain", "password", "wallet")
DEFAULT_DENY_TERMS = ("bank", "medical", "health", "private browsing", "incognito")

# LlamaParse document-parsing API (whole-file OCR, distinct from the local
# Tesseract screen pipeline below). base URL is cfg-overridable so an endpoint
# drift is a config fix, not a code change.
# ponytail: wire shape follows LlamaParse's documented upload/poll/result flow
# from memory, not a live key. The three HTTP helpers are the calibration knob
# to tune against a real key; the orchestration + degradation around them is
# fully tested with those helpers mocked.
_DEFAULT_LLAMAPARSE_BASE = "https://api.cloud.llamaindex.ai/api/v1/parsing"
_LLAMAPARSE_POLL_TRIES = 30
_LLAMAPARSE_POLL_INTERVAL_S = 2.0

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


def file_denied(path: str, cfg: dict) -> tuple[bool, str]:
    """Opt-out deny-gate for LlamaParse file parsing (plan R3/R4, KTD2).

    Mirrors allowed()'s substring/casefold style but is file-scoped. A path
    matching a configured denied extension or path substring is never sent to
    LlamaParse. Empty config -> nothing denied (opt-out default).
    """
    content = cfg.get("content_capture") or {}
    p = (path or "").casefold()
    for ext in content.get("deny_extensions", []):
        if ext and p.endswith(str(ext).casefold()):
            return True, f"extension {ext} is denied"
    for sub in content.get("deny_paths", []):
        if sub and str(sub).casefold() in p:
            return True, f"path matches denied '{sub}'"
    return False, "allowed"


def _llamaparse_key(cfg: dict | None = None) -> str | None:
    key = os.environ.get("LLAMAPARSE_API_KEY")
    if key:
        return key
    try:
        from workpulse.wp_secrets import get as get_secret  # type: ignore
        return get_secret("llamaparse_key") or None
    except Exception:
        return None


def _llamaparse_base(cfg: dict | None) -> str:
    return (((cfg or {}).get("content_capture") or {}).get("llamaparse_base")
            or _DEFAULT_LLAMAPARSE_BASE)


def _llamaparse_upload(path: str, key: str, base: str) -> str | None:
    """POST the file as multipart/form-data; return the job id."""
    boundary = uuid.uuid4().hex
    data = Path(path).read_bytes()
    filename = Path(path).name
    body = b"".join([
        f"--{boundary}\r\n".encode(),
        f'Content-Disposition: form-data; name="file"; filename="{filename}"\r\n'.encode(),
        b"Content-Type: application/octet-stream\r\n\r\n",
        data, b"\r\n", f"--{boundary}--\r\n".encode(),
    ])
    req = urllib.request.Request(
        f"{base}/upload", data=body, method="POST",
        headers={"Authorization": f"Bearer {key}",
                 "Content-Type": f"multipart/form-data; boundary={boundary}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        return (json.loads(resp.read().decode()) or {}).get("id")


def _llamaparse_poll(job: str, key: str, base: str) -> bool:
    """Poll the job until SUCCESS (True) or ERROR/timeout (False). Bounded."""
    req = urllib.request.Request(
        f"{base}/job/{job}", headers={"Authorization": f"Bearer {key}"})
    for _ in range(_LLAMAPARSE_POLL_TRIES):
        with urllib.request.urlopen(req, timeout=30) as resp:
            status = (json.loads(resp.read().decode()) or {}).get("status")
        if status in ("SUCCESS", "COMPLETED"):
            return True
        if status in ("ERROR", "FAILED", "CANCELLED"):
            return False
        time.sleep(_LLAMAPARSE_POLL_INTERVAL_S)
    return False


def _llamaparse_result(job: str, key: str, base: str) -> str | None:
    req = urllib.request.Request(
        f"{base}/job/{job}/result/markdown",
        headers={"Authorization": f"Bearer {key}"})
    with urllib.request.urlopen(req, timeout=60) as resp:
        payload = json.loads(resp.read().decode()) or {}
    return payload.get("markdown") or payload.get("text")


def _llamaparse_parse(path: str, cfg: dict) -> tuple[str | None, dict]:
    """Parse a whole document file via LlamaParse (plan U1, KTD3).

    Returns (markdown_text | None, meta). Any failure -- no key, network,
    unsupported file, job error/timeout -- returns None and never raises (R7).
    """
    meta = {"engine": "llamaparse"}
    key = _llamaparse_key(cfg)
    if not key:
        return None, meta
    base = _llamaparse_base(cfg)
    try:
        job = _llamaparse_upload(path, key, base)
        if not job or not _llamaparse_poll(job, key, base):
            return None, meta
        text = _llamaparse_result(job, key, base)
    except Exception:  # noqa: BLE001 -- any failure degrades to no-parse (R7)
        return None, meta
    return (text or None), meta


_DEFAULT_PARSE_EXTENSIONS = (".pdf", ".docx", ".doc", ".pptx", ".xlsx")


def _parse_extensions(cfg: dict | None) -> tuple[str, ...]:
    content = (cfg or {}).get("content_capture") or {}
    exts = content.get("parse_extensions")
    return tuple(str(e).casefold() for e in exts) if exts else _DEFAULT_PARSE_EXTENSIONS


def find_parse_candidates(con, cfg: dict, *, since: str | None = None) -> list[dict]:
    """Recently created/modified document files eligible for LlamaParse (plan
    U3, R1/R3). Excludes deny-listed paths, non-parseable extensions, files that
    no longer exist, and files already parsed at their current mtime (dedup)."""
    exts = _parse_extensions(cfg)
    where = "WHERE f.kind IN ('created','modified')"
    params: list = []
    if since:
        where += " AND f.ts >= ?"
        params.append(since)
    rows = con.execute(
        f"SELECT fl.raw_path AS raw_path, f.path_hash AS path_hash "
        f"FROM file_event f JOIN file_event_local fl ON fl.file_event_id = f.id "
        f"{where} GROUP BY f.path_hash", params).fetchall()

    out, seen = [], set()
    for r in rows:
        path = r["raw_path"]
        if not path or path in seen:
            continue
        seen.add(path)
        if not path.casefold().endswith(exts):
            continue
        if file_denied(path, cfg)[0]:
            continue
        try:
            mtime = str(os.path.getmtime(path))
        except OSError:
            continue  # file gone -- nothing to parse
        already = con.execute(
            "SELECT 1 FROM content_capture WHERE source_path_hash = ? "
            "AND source_mtime = ? LIMIT 1", (r["path_hash"], mtime)).fetchone()
        if already:
            continue
        out.append({"raw_path": path, "path_hash": r["path_hash"], "mtime": mtime})
    return out


def parse_and_persist(con, cfg: dict, *, since: str | None = None) -> dict:
    """Parse each eligible file via LlamaParse, redact, and persist into
    content_capture with ocr_engine='llamaparse' (plan U3). A per-file failure
    is skipped, never fatal (R7). Returns counts."""
    from workpulse.core import search
    content = cfg.get("content_capture") or {}
    parsed = failed = 0
    for cand in find_parse_candidates(con, cfg, since=since):
        text, meta = _llamaparse_parse(cand["raw_path"], cfg)
        if not text:
            failed += 1
            continue
        redacted = redact(text, content.get("redaction_terms") or [])
        if len(redacted) < 20:
            failed += 1
            continue
        stage = semantics.classify_stage(redacted, "document")
        cap_id = new_id()
        ts = datetime.now(timezone.utc).isoformat()
        con.execute(
            "INSERT INTO content_capture(id, ts, app, stage, redacted_text, "
            "ocr_engine, source_path_hash, source_mtime) VALUES (?,?,?,?,?,?,?,?)",
            (cap_id, ts, "document", stage, redacted,
             meta.get("engine", "llamaparse"), cand["path_hash"], cand["mtime"]))
        con.commit()
        # Index just this row so it is retrievable the same pass (KTD5) --
        # incremental append, never a full reindex that would touch other kinds.
        search.index_atom(con, kind="content_capture", id=cap_id, ts=ts,
                          stream=None, content=redacted)
        parsed += 1
    return {"parsed": parsed, "failed": failed}


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
        return {"ok": False, "reason": reason, "blocked": True, "app": app}
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
