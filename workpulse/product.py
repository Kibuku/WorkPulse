"""Installed Pulse product identity and capability boundaries.

One codebase is shipped in several product flavours.  This file is the single
source of truth for what a particular installation is allowed to present and
which release channel its updater follows.  A URL query string must never be
able to change these capabilities.
"""

from __future__ import annotations

import json
import os
import sys
from dataclasses import asdict, dataclass
from pathlib import Path

from workpulse.common import ROOT

MANIFEST_PATH = ROOT / "config" / "product.json"

_FLAVOURS = {
    ("personal", "individual"): {
        "channel": "personal-workpulse",
        "entry_path": "/personal",
        "capabilities": ("personal.view",),
        "label": "Personal WorkPulse",
    },
    ("institution", "member"): {
        "channel": "workpulse-institution",
        "entry_path": "/institution",
        "capabilities": ("institution.member", "personal.view"),
        "label": "WorkPulse Institution",
    },
    ("developer", "lab"): {
        "channel": "developer-lab",
        "entry_path": "/personal",
        "capabilities": ("developer.lab", "personal.view"),
        "label": "Pulse Developer Lab",
    },
    ("learning", "facilitator"): {
        "channel": "learning-facilitator",
        "entry_path": "/learning",
        "capabilities": ("learning.facilitator", "learning.gateway"),
        "label": "LearningPulse Facilitator",
    },
    ("learning", "device"): {
        "channel": "learning-device",
        "entry_path": "/learning?role=device",
        "capabilities": ("learning.device",),
        "label": "LearningPulse Device",
    },
}


@dataclass(frozen=True)
class Product:
    product: str
    role: str
    channel: str
    entry_path: str
    capabilities: tuple[str, ...]
    label: str
    configured: bool = True

    def has(self, capability: str) -> bool:
        return capability in self.capabilities

    def public_dict(self) -> dict:
        data = asdict(self)
        data["capabilities"] = list(self.capabilities)
        return data


def _normalise(product: str, role: str) -> tuple[str, str]:
    product = str(product or "").strip().lower().replace("_", "-")
    role = str(role or "").strip().lower().replace("_", "-")
    aliases = {
        ("personal", "personal"): ("personal", "individual"),
        ("organization", "member"): ("institution", "member"),
        ("organisation", "member"): ("institution", "member"),
        ("learning", "learning-device"): ("learning", "device"),
        ("classroom", "facilitator"): ("learning", "facilitator"),
        ("classroom", "device"): ("learning", "device"),
    }
    return aliases.get((product, role), (product, role))


def _make(product: str, role: str, *, configured: bool = True) -> Product:
    key = _normalise(product, role)
    if key not in _FLAVOURS:
        valid = ", ".join(f"{p}/{r}" for p, r in _FLAVOURS)
        raise ValueError(f"Unknown Pulse product role {product!r}/{role!r}; valid: {valid}")
    return Product(key[0], key[1], configured=configured, **_FLAVOURS[key])


def current() -> Product:
    """Return the immutable identity selected at installation.

    Source checkouts default to a developer identity with every capability so
    contributors and the test suite can exercise all surfaces. Frozen builds
    without a manifest are treated as the private developer lab. Commercial
    installations always receive an explicit manifest from their installer.
    """
    if MANIFEST_PATH.exists():
        try:
            data = json.loads(MANIFEST_PATH.read_text(encoding="utf-8"))
            return _make(data.get("product", ""), data.get("role", ""))
        except (OSError, ValueError, json.JSONDecodeError):
            pass
    if not getattr(sys, "frozen", False) and os.environ.get(
            "WORKPULSE_PRODUCT", "developer").lower() == "developer":
        return Product(
            "developer", "lab", "developer-lab", "/personal",
            tuple(cap for details in _FLAVOURS.values()
                  for cap in details["capabilities"]),
            "Pulse Developer", configured=False,
        )
    return _make("developer", "lab", configured=False)


def configure(product: str, role: str, *, path: Path | None = None) -> Product:
    selected = _make(product, role)
    target = path or MANIFEST_PATH
    target.parent.mkdir(parents=True, exist_ok=True)
    payload = {
        "schema": 1,
        "product": selected.product,
        "role": selected.role,
        "channel": selected.channel,
    }
    temp = target.with_suffix(".tmp")
    temp.write_text(json.dumps(payload, indent=2) + "\n", encoding="utf-8")
    temp.replace(target)
    return selected


def has(capability: str) -> bool:
    return current().has(capability)


def release_channel() -> str:
    product = current()
    # Developer checkouts use the Personal release only when explicitly testing
    # the production updater; they are normally updated through git.
    return product.channel
