"""Safe, consent-based desktop updates for frozen WorkPulse installations."""

from __future__ import annotations

import hashlib
import json
import os
import platform
import subprocess
import sys
import urllib.request
from pathlib import Path
from urllib.parse import urlparse

from workpulse import __version__
from workpulse.common import ROOT
from workpulse import product as product_identity

DEFAULT_MANIFEST_URL = (
    "https://njiani-flame.vercel.app/update.json"
)


def _version_tuple(value: str) -> tuple[int, ...]:
    parts = []
    for token in value.strip().lstrip("v").split("."):
        digits = "".join(ch for ch in token if ch.isdigit())
        parts.append(int(digits or 0))
    return tuple(parts)


def platform_key() -> str | None:
    if sys.platform == "darwin":
        return "macos"
    if sys.platform == "win32":
        return "windows"
    return None


def check(*, manifest_url: str = DEFAULT_MANIFEST_URL,
          timeout: float = 8.0) -> dict:
    """Fetch release metadata without downloading or installing anything."""
    if urlparse(manifest_url).scheme != "https":
        raise ValueError("Update manifest must use HTTPS")
    req = urllib.request.Request(
        manifest_url, headers={"User-Agent": f"WorkPulse/{__version__}"})
    with urllib.request.urlopen(req, timeout=timeout) as response:
        manifest = json.load(response)

    key = platform_key()
    channel = product_identity.release_channel()
    products = manifest.get("products") or {}
    selected = products.get(channel) if products else manifest
    # Product-aware manifests prevent Personal, Institution, and Learning
    # installations from ever crossing release channels during an update.
    compatible = bool(products)
    artifact = ((selected or {}).get("platforms") or {}).get(key) \
        if key and compatible else None
    latest = str(manifest.get("version") or "")
    available = bool(
        artifact and latest
        and _version_tuple(latest) > _version_tuple(__version__)
    )
    return {
        "current_version": __version__,
        "latest_version": latest or None,
        "update_available": available,
        "supported": key is not None,
        "platform": key,
        "channel": channel,
        "product": product_identity.current().public_dict(),
        "notes": manifest.get("notes") or "",
        "published_at": manifest.get("published_at"),
        "artifact": artifact if available else None,
        "manifest_supports_product": bool(products),
    }


def _safe_artifact(artifact: dict) -> tuple[str, str, str]:
    url = str(artifact.get("url") or "")
    sha256 = str(artifact.get("sha256") or "").lower()
    filename = str(artifact.get("filename") or Path(urlparse(url).path).name)
    if urlparse(url).scheme != "https":
        raise ValueError("Installer URL must use HTTPS")
    if len(sha256) != 64 or any(c not in "0123456789abcdef" for c in sha256):
        raise ValueError("Release manifest has no valid SHA-256 checksum")
    if not filename or Path(filename).name != filename:
        raise ValueError("Release manifest has an unsafe filename")
    expected = ".pkg" if sys.platform == "darwin" else ".exe"
    if not filename.lower().endswith(expected):
        raise ValueError(f"Expected a {expected} installer")
    return url, sha256, filename


def download(update: dict, *, timeout: float = 180.0) -> dict:
    """Download and checksum an offered update. Never launches it."""
    url, expected_hash, filename = _safe_artifact(update.get("artifact") or {})
    target_dir = ROOT / "updates"
    target_dir.mkdir(parents=True, exist_ok=True)
    partial = target_dir / f"{filename}.part"
    target = target_dir / filename

    digest = hashlib.sha256()
    req = urllib.request.Request(
        url, headers={"User-Agent": f"WorkPulse/{__version__}"})
    try:
        with urllib.request.urlopen(req, timeout=timeout) as response, \
                partial.open("wb") as out:
            while chunk := response.read(1024 * 1024):
                out.write(chunk)
                digest.update(chunk)
        actual = digest.hexdigest()
        if actual != expected_hash:
            partial.unlink(missing_ok=True)
            raise ValueError("Installer checksum did not match the release manifest")
        partial.replace(target)
    except Exception:
        partial.unlink(missing_ok=True)
        raise
    return {"path": str(target), "filename": filename, "sha256": expected_hash}


def launch_installer(path: str) -> None:
    """Open the native installer. The operating system asks for final consent."""
    installer = Path(path).resolve()
    updates_dir = (ROOT / "updates").resolve()
    if updates_dir not in installer.parents or not installer.is_file():
        raise ValueError("Installer is outside WorkPulse's verified update folder")
    if sys.platform == "darwin":
        subprocess.Popen(["open", str(installer)])
    elif sys.platform == "win32":
        os.startfile(str(installer))  # type: ignore[attr-defined]
    else:
        raise RuntimeError(f"Updates are unsupported on {platform.system()}")


def download_and_launch(*, manifest_url: str = DEFAULT_MANIFEST_URL) -> dict:
    update = check(manifest_url=manifest_url)
    if not update["update_available"]:
        return {**update, "launched": False}
    downloaded = download(update)
    launch_installer(downloaded["path"])
    return {**update, **downloaded, "launched": True}
