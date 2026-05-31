"""
common.py — shared utilities for all WorkPulse scripts.

Provides:
  - ROOT: project root (D:\\WorkPulse\\)
  - load_config() -> dict
  - resolve(relative_path) -> Path
  - get_env(key) -> str  (raises if missing)
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

import yaml

# Project root is the parent of this file's directory (scripts/ -> root)
ROOT = Path(__file__).resolve().parent.parent


def load_config() -> dict:
    """Load and return config/config.yaml."""
    cfg_path = ROOT / "config" / "config.yaml"
    with cfg_path.open(encoding="utf-8") as f:
        return yaml.safe_load(f)


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
