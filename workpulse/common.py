"""
common.py — shared utilities for the WorkPulse package.

Provides:
  - ROOT: project/data root (the repo directory)
  - PKG:  the installed workpulse/ package directory
  - load_config() -> dict
  - resolve(relative_path) -> Path
  - get_env(key) -> str  (raises if missing)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

# In a source checkout, code/resources and user data share the repo root. In a
# frozen desktop build they must separate: bundled resources are read-only,
# while config, the DB, logs and memory live in a per-user writable directory.
BUNDLE_ROOT = (Path(getattr(sys, "_MEIPASS")) if getattr(sys, "frozen", False)
               else Path(__file__).resolve().parent.parent)
ROOT = Path(os.environ.get("WORKPULSE_HOME", str(BUNDLE_ROOT))).expanduser().resolve()
PKG = Path(__file__).resolve().parent            # bundled skills + migrations


def load_config() -> dict:
    """Load config/config.yaml, falling back to config/config.example.yaml
    when no user config exists yet (fresh install, pre-onboarding). This
    lets a freshly-cloned repo run before the user has personalised anything.
    """
    cfg_dir = ROOT / "config"
    cfg_path = cfg_dir / "config.yaml"
    if not cfg_path.exists():
        cfg_path = BUNDLE_ROOT / "config" / "config.example.yaml"
    with cfg_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f) or {}


def resolve(relative: str) -> Path:
    """Resolve a config path (relative to ROOT) to an absolute Path."""
    p = Path(relative)
    if p.is_absolute():
        return p
    return ROOT / p


def paths(cfg: dict) -> dict[str, Path]:
    """Return a dict of all resolved Path objects from cfg['paths']."""
    return {k: resolve(v) for k, v in cfg.get("paths", {}).items()}


def get_env(key: str) -> str:
    """Return the value of an env var, or exit with a clear message if missing."""
    value = os.environ.get(key)
    if not value:
        print(f"ERROR: environment variable {key!r} is not set.", file=sys.stderr)
        print(f"  Set it with:  setx {key} \"<value>\"  (then restart your terminal)", file=sys.stderr)
        sys.exit(1)
    return value


def ensure_dir(path: Path) -> Path:
    """Create directory (and parents) if it doesn't exist. Returns the path."""
    path.mkdir(parents=True, exist_ok=True)
    return path


def enable_utf8_console() -> None:
    """Make stdout/stderr encode as UTF-8 so non-ASCII output (arrows, em
    dashes, emoji) never crashes on a Windows cp1252 console. Without this,
    printing a character the console codepage lacks raises UnicodeEncodeError
    and aborts the command. Safe no-op where the streams can't be reconfigured
    (already wrapped, or redirected to a pipe that fixes the encoding)."""
    for stream in (sys.stdout, sys.stderr):
        reconfigure = getattr(stream, "reconfigure", None)
        if reconfigure is not None:
            try:
                reconfigure(encoding="utf-8")
            except (ValueError, OSError):
                pass
