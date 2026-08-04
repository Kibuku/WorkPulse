"""WorkPulse Classroom Agent for an enrolled student/lab computer."""

from __future__ import annotations

import argparse
import json
import platform
import sys
import threading
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

import psutil

from workpulse.common import ROOT, ensure_dir


CONFIG_PATH = ROOT / "config" / "classroom-agent.json"
_agent_thread: threading.Thread | None = None
_agent_lock = threading.Lock()
_last_remote_status: dict = {}


def _request(url: str, *, method: str = "GET", body: dict | None = None,
             token: str = "") -> dict:
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"}
    if token:
        headers["Authorization"] = f"Bearer {token}"
    request = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(request, timeout=8) as response:
            return json.loads(response.read().decode("utf-8"))
    except urllib.error.HTTPError as exc:
        detail = exc.read().decode("utf-8", errors="replace")
        raise RuntimeError(f"server returned {exc.code}: {detail}") from exc


def enroll(server: str, code: str, name: str) -> dict:
    result = _request(
        server.rstrip("/") + "/api/v2/classroom/agent/enroll",
        method="POST",
        body={"code": code, "name": name, "platform": platform.system()},
    )
    ensure_dir(CONFIG_PATH.parent)
    CONFIG_PATH.write_text(
        json.dumps({
            "server": server.rstrip("/"),
            "device_id": result["device_id"],
            "device_name": result["device_name"],
            "token": result["token"],
        }, indent=2),
        encoding="utf-8",
    )
    return result


def _foreground() -> dict:
    if sys.platform == "darwin":
        from workpulse.signals.activity_mac import foreground_window
    elif sys.platform == "win32":
        from workpulse.signals.activity_win import foreground_window
    else:
        raise RuntimeError("Classroom Agent supports macOS and Windows")
    current = foreground_window()
    if not current:
        return {"app": "", "title": "", "domain": ""}
    title, pid, app_name = current
    if not app_name:
        try:
            app_name = psutil.Process(pid).name()
        except (psutil.Error, OSError):
            app_name = ""
    domain = ""
    try:
        from workpulse.signals.browser_tracker import read_tab_for
        tab = read_tab_for(app_name)
        if tab:
            domain = (urlparse(tab["url"]).hostname or "").removeprefix("www.")
            title = tab.get("title") or title
    except Exception:
        pass
    return {"app": app_name, "title": title, "domain": domain}


def run(interval: int = 5) -> None:
    global _last_remote_status
    if not CONFIG_PATH.exists():
        raise RuntimeError("this device is not enrolled; run classroom-agent enroll first")
    cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
    print(f"WorkPulse Classroom Agent · {cfg['device_name']} ({cfg['device_id']})")
    print(f"Session Console gateway: {cfg['server']}")
    print("Press Ctrl-C to stop.")
    while True:
        signal = _foreground()
        signal.update({
            "ts": datetime.now(timezone.utc).isoformat(),
            "source_ref": f"agent:{int(time.time())}",
        })
        try:
            result = _request(
                cfg["server"] + "/api/v2/classroom/agent/heartbeat",
                method="POST",
                body={"signal": signal},
                token=cfg["token"],
            )
            _last_remote_status = result.get("status") or {}
            mode = result["status"]["mode"]
            decision = result["decision"]["decision"]
            print(f"{datetime.now():%H:%M:%S}  {mode:<6}  {decision:<12}  "
                  f"{signal['app']}  {signal['domain'] or signal['title'][:50]}")
        except Exception as exc:
            print(f"{datetime.now():%H:%M:%S}  offline  {exc}")
        time.sleep(max(2, interval))


def start_background(interval: int = 5) -> dict:
    """Start this device's enrolled agent once, owned by the local dashboard."""
    global _agent_thread
    with _agent_lock:
        if _agent_thread and _agent_thread.is_alive():
            return {"running": True, "started": False}
        if not CONFIG_PATH.exists():
            raise RuntimeError("this device is not enrolled")
        _agent_thread = threading.Thread(
            target=run,
            kwargs={"interval": interval},
            daemon=True,
            name="workpulse-classroom-agent",
        )
        _agent_thread.start()
        return {"running": True, "started": True}


def local_status() -> dict:
    enrolled = CONFIG_PATH.exists()
    cfg = {}
    if enrolled:
        try:
            cfg = json.loads(CONFIG_PATH.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            enrolled = False
    return {
        "enrolled": enrolled,
        "running": bool(_agent_thread and _agent_thread.is_alive()),
        "device_id": cfg.get("device_id"),
        "device_name": cfg.get("device_name"),
        "server": cfg.get("server"),
        "mode": _last_remote_status.get("mode", "normal"),
        "active_session": _last_remote_status.get("active_session"),
        "scheduled_session": _last_remote_status.get("scheduled_session"),
        "interventions": _last_remote_status.get("interventions", []),
    }


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(prog="workpulse classroom-agent")
    sub = parser.add_subparsers(dest="action", required=True)
    pair = sub.add_parser("enroll")
    pair.add_argument("--server", required=True,
                      help="Classroom gateway on the shared network, e.g. http://192.168.1.5:5722")
    pair.add_argument("--code", required=True)
    pair.add_argument("--name", default=platform.node() or "Lab computer")
    start = sub.add_parser("run")
    start.add_argument("--interval", type=int, default=5)
    args = parser.parse_args(argv)
    if args.action == "enroll":
        try:
            result = enroll(args.server, args.code, args.name)
        except (RuntimeError, OSError) as exc:
            print(f"Could not enrol this device: {exc}", file=sys.stderr)
            return 1
        print(f"Enrolled {result['device_name']} as {result['device_id']}")
        print("Start monitoring with: workpulse classroom-agent run")
        return 0
    run(args.interval)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
