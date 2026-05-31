"""
wp_secrets.py — secret storage with two backends.

(Named wp_secrets, not secrets, because the latter shadows Python's stdlib
`secrets` module — which Starlette imports for token generation.)

WorkPulse never asks the user to touch environment variables. Secrets like
the Anthropic API key and Gmail SMTP password live in config/secrets.json,
written by the in-dashboard Settings page.

Power users (and existing installs) can still set the canonical env vars —
those take priority over the file, so nothing breaks.

Canonical secret names (lowercase, no prefix):
    anthropic_key    -> ANTHROPIC_API_KEY
    smtp_password    -> WORKPULSE_SMTP_PASSWORD
    smtp_user        -> WORKPULSE_SMTP_USER     (optional override)
    smtp_to          -> WORKPULSE_SMTP_TO       (optional override)
"""

from __future__ import annotations

import json
import os
import sys
from pathlib import Path

_ROOT = Path(__file__).resolve().parent.parent
_PATH = _ROOT / "config" / "secrets.json"

# secret name -> env var name
_ENV_MAP = {
    "anthropic_key":  "ANTHROPIC_API_KEY",
    "smtp_password":  "WORKPULSE_SMTP_PASSWORD",
    "smtp_user":      "WORKPULSE_SMTP_USER",
    "smtp_to":        "WORKPULSE_SMTP_TO",
}


def _load_file() -> dict:
    if not _PATH.exists():
        return {}
    try:
        return json.loads(_PATH.read_text(encoding="utf-8"))
    except (json.JSONDecodeError, OSError):
        return {}


def get(name: str) -> str | None:
    """Read a secret by canonical name. Env var wins; falls back to secrets.json.
    Returns None if neither is set (or value is empty)."""
    env_name = _ENV_MAP.get(name)
    if env_name:
        v = os.environ.get(env_name)
        if v:
            return v
    v = _load_file().get(name)
    return v if v else None


def set_(name: str, value: str | None) -> None:
    """Write a secret to secrets.json. Pass None or '' to delete the key.
    Env vars are NEVER modified by this function — only the file.
    Trailing/leading whitespace is stripped from value (common paste artefact)."""
    data = _load_file()
    if value is not None:
        value = value.strip()
    if value:
        data[name] = value
    else:
        data.pop(name, None)
    _PATH.parent.mkdir(parents=True, exist_ok=True)
    tmp = _PATH.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(data, indent=2), encoding="utf-8")
    tmp.replace(_PATH)
    # Best-effort: lock file to current user on Windows. Falls back to default ACL
    # if icacls is unavailable. The file lives in the project tree, which is
    # already user-owned on a normal install.
    try:
        if sys.platform.startswith("win"):
            import subprocess
            user = os.environ.get("USERNAME", "")
            if user:
                subprocess.run(
                    ["icacls", str(_PATH), "/inheritance:r", "/grant:r", f"{user}:F"],
                    capture_output=True, check=False, timeout=5,
                )
    except Exception:
        pass


def has(name: str) -> bool:
    """True iff a non-empty value is configured for this secret."""
    return bool(get(name))


def known_names() -> list[str]:
    return list(_ENV_MAP.keys())


def status() -> dict[str, dict]:
    """Diagnostic snapshot — which secrets are configured and from where.
    Never returns the actual values."""
    out = {}
    file_data = _load_file()
    for name, env in _ENV_MAP.items():
        if os.environ.get(env):
            src = "env"
        elif file_data.get(name):
            src = "file"
        else:
            src = None
        out[name] = {"configured": src is not None, "source": src}
    return out


if __name__ == "__main__":
    # CLI: python scripts/secrets.py status | get <name> | set <name> <value>
    if len(sys.argv) < 2:
        print("usage: secrets.py {status | get NAME | set NAME VALUE | unset NAME}")
        sys.exit(1)
    cmd = sys.argv[1]
    if cmd == "status":
        for k, v in status().items():
            mark = "[x]" if v["configured"] else "[ ]"
            src  = f"({v['source']})" if v["source"] else ""
            print(f"  {mark} {k:<16} {src}")
    elif cmd == "get" and len(sys.argv) >= 3:
        v = get(sys.argv[2])
        print(f"(unset)" if v is None else f"({len(v)} chars)")
    elif cmd == "set" and len(sys.argv) >= 4:
        set_(sys.argv[2], sys.argv[3])
        print("ok")
    elif cmd == "unset" and len(sys.argv) >= 3:
        set_(sys.argv[2], None)
        print("ok")
    else:
        print("bad command", file=sys.stderr)
        sys.exit(2)
