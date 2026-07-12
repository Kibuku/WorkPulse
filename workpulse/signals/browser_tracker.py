"""
browser_tracker.py — read the frontmost browser's active tab via AppleScript,
write a browser_visit atom, classify into project / private / deny tier.

Closes the SharePoint / ChatGPT / browser-based work blind spot. The OS
only tells us "Safari is foreground"; this tells us "you're on
example.sharepoint.com/sites/ProjectX/...". That URL routes to the matching
project via projects.yaml's path-mode resolver.

Privacy tier model (config/projects.yaml):
  - deny_domains    → visit DROPPED entirely. No atom, no edge.
                       (For things you don't want stored even encrypted:
                        bank login pages, password manager URLs, etc.)
  - private_domains → visit WRITTEN, stream='personal', is_private=1.
                       Hidden from default dashboard. Unlock with password.
  - project match   → visit written, stream=resolved (acme, dev, ...).
                       Visible on dashboard like any other signal.
  - unknown         → visit written, stream=NULL. Visible.

Public API:
    sample_once(con, *, cfg=None) -> dict | None
        — one AppleScript poll + write. Returns the visit dict or None.
    current_browser_tab() -> dict | None
        — pure read, no DB write. {app, url, title} or None.

CLI:
    python -m workpulse.signals.browser_tracker sample            # one-shot (default)
    python -m workpulse.signals.browser_tracker run --interval 30 # long-running
    python -m workpulse.signals.browser_tracker show              # last 20 visits
    python -m workpulse.signals.browser_tracker diagnose          # is anything readable?
"""

from __future__ import annotations

import argparse
import hashlib
import json
import subprocess
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlparse

from workpulse.core import atoms, db
from workpulse.core import projects as wp_projects
from workpulse.common import load_config


# ── AppleScript-based browser reads ─────────────────────────────────────────
#
# Each entry: app NAME (as macOS sees it) → AppleScript that returns
# "URL\nTITLE\n" of the active tab. Returns "" if the app isn't running
# OR if the user is in incognito (browsers refuse to disclose private tabs).

_BROWSER_SCRIPTS: dict[str, str] = {
    "Safari": 'tell application "Safari" to do JavaScript "" in front document ' + \
              '\nreturn (URL of current tab of front window) & linefeed & ' + \
              '(name of current tab of front window)',
    "Google Chrome": 'tell application "Google Chrome" to return '
                     '(URL of active tab of front window) & linefeed & '
                     '(title of active tab of front window)',
    "Brave Browser": 'tell application "Brave Browser" to return '
                     '(URL of active tab of front window) & linefeed & '
                     '(title of active tab of front window)',
    "Microsoft Edge": 'tell application "Microsoft Edge" to return '
                      '(URL of active tab of front window) & linefeed & '
                      '(title of active tab of front window)',
    "Arc": 'tell application "Arc" to return '
           '(URL of active tab of front window) & linefeed & '
           '(title of active tab of front window)',
}

# Cleaner Safari script — the do-JavaScript trick above sometimes hits
# permission prompts; use the simpler URL-of-current-tab form first.
_BROWSER_SCRIPTS["Safari"] = (
    'tell application "Safari" to return '
    '(URL of current tab of front window) & linefeed & '
    '(name of current tab of front window)'
)


def _frontmost_app_name() -> str | None:
    """Return localized name of the frontmost app via NSWorkspace."""
    try:
        from AppKit import NSWorkspace
        a = NSWorkspace.sharedWorkspace().frontmostApplication()
        return a.localizedName() if a else None
    except Exception:
        return None


def _read_browser(app_name: str, *, timeout: int = 3) -> tuple[str, str] | None:
    """Run the AppleScript for `app_name` and return (url, title)."""
    script = _BROWSER_SCRIPTS.get(app_name)
    if not script:
        return None
    try:
        r = subprocess.run(
            ["osascript", "-e", script],
            capture_output=True, text=True, timeout=timeout,
        )
    except (subprocess.SubprocessError, FileNotFoundError):
        return None
    if r.returncode != 0:
        return None
    parts = r.stdout.split("\n", 1)
    if len(parts) < 2:
        return None
    url = parts[0].strip()
    title = parts[1].strip()
    if not url or url == "missing value":
        return None
    return url, title


def is_browser(app_name: str | None) -> bool:
    """True if `app_name` is a browser we know how to read via AppleScript.
    activity.py already knows the frontmost app name, so it uses this to
    decide whether to grab a URL in its own sample loop."""
    return bool(app_name) and app_name in _BROWSER_SCRIPTS


def read_tab_for(app_name: str) -> dict | None:
    """Read the active tab of a KNOWN-browser app. Returns {app, url, title}
    or None. The caller has already established this is the frontmost app —
    no re-detection here, so the read is bound to the caller's moment."""
    if not is_browser(app_name):
        return None
    pair = _read_browser(app_name)
    if pair is None:
        return None
    return {"app": app_name, "url": pair[0], "title": pair[1]}


def current_browser_tab() -> dict | None:
    """If the frontmost app is a known browser, read its active tab.
    Returns {app, url, title} or None. Standalone path (CLI / diagnose)."""
    return read_tab_for(_frontmost_app_name())


# ── URL → domain → tier classification ──────────────────────────────────────

def _domain_of(url: str) -> str:
    try:
        p = urlparse(url)
        host = (p.netloc or "").split("@")[-1].split(":")[0].lower()
        if host.startswith("www."):
            host = host[4:]
        return host
    except Exception:
        return ""


def _hash16(s: str) -> str:
    return hashlib.sha256((s or "").encode("utf-8")).hexdigest()[:16]


# ── write path ──────────────────────────────────────────────────────────────

def record_visit(con: sqlite3.Connection, tab: dict, *,
                 ts: str | None = None, cfg: dict | None = None) -> dict | None:
    """Classify + write a browser visit from an already-read tab dict
    ({app, url, title}). `ts` lets the caller bind the visit to its own
    sample moment (activity.py passes the session's timestamp so the visit
    and the session share a clock). Returns the visit dict, or the deny
    marker, or None if the tab is empty."""
    if not tab or not tab.get("url"):
        return None
    classification = wp_projects.classify_url(tab["url"])
    tier = classification["tier"]
    if tier == "deny":
        # Drop entirely. Don't even record that the user visited.
        return {"tier": "deny", **classification}

    stream = None
    is_private = 0
    if tier == "private":
        stream = "personal"
        is_private = 1
    elif tier == "project":
        stream = classification["stream"]
        # Lookup is_private from stream table (defensive — most projects are not private)
        row = con.execute(
            "SELECT is_private FROM stream WHERE key = ?", (stream,)
        ).fetchone()
        is_private = int(row["is_private"]) if row else 0

    visit_id = atoms.new_id()
    ts = ts or datetime.now(timezone.utc).isoformat()
    domain = _domain_of(tab["url"])
    url_hash = _hash16(tab["url"])
    title_hash = _hash16(tab["title"])

    if stream:
        con.execute(
            "INSERT OR IGNORE INTO stream(key, label, parent_key, is_private) "
            "VALUES (?, ?, NULL, ?)",
            (stream, stream, is_private),
        )

    con.execute("BEGIN")
    try:
        con.execute(
            """
            INSERT INTO browser_visit(id, ts, app, domain, url_hash,
                                       title_hash, stream, is_private)
            VALUES (?, ?, ?, ?, ?, ?, ?, ?)
            """,
            (visit_id, ts, tab["app"], domain, url_hash,
             title_hash, stream, is_private),
        )
        con.execute(
            """
            INSERT INTO browser_visit_local(visit_id, raw_url, raw_title)
            VALUES (?, ?, ?)
            """,
            (visit_id, tab["url"], tab["title"]),
        )
        con.execute("COMMIT")
    except Exception:
        con.execute("ROLLBACK")
        raise

    return {
        "tier":       tier,
        "id":         visit_id,
        "ts":         ts,
        "app":        tab["app"],
        "domain":     domain,
        "stream":     stream,
        "is_private": bool(is_private),
    }


def sample_once(con: sqlite3.Connection, *, cfg: dict | None = None) -> dict | None:
    """Standalone one-shot: detect frontmost browser, read, classify, write.
    Used by the CLI. The live capture path is now activity.py calling
    read_tab_for() + record_visit() inside its own sample loop."""
    tab = current_browser_tab()
    if tab is None:
        return None
    return record_visit(con, tab, cfg=cfg)


def run_loop(*, interval: int = 30, cfg: dict | None = None) -> None:
    """Long-running mode. Polls every `interval` seconds until SIGINT."""
    cfg = cfg or load_config()
    con = db.connect(cfg)
    print(f"browser_tracker running (interval={interval}s). Ctrl-C to stop.")
    while True:
        try:
            res = sample_once(con, cfg=cfg)
            if res is None:
                pass
            elif res.get("tier") == "deny":
                print(f"  [deny] {res.get('reason')}")
            else:
                tag = "🔒" if res.get("is_private") else ""
                print(f"  [{res['tier']}] {tag} {res['domain']:30s} -> "
                      f"{res['stream'] or '—'}")
        except KeyboardInterrupt:
            return
        except Exception as e:
            print(f"  err: {e!r}")
        time.sleep(interval)


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_sample() -> int:
    con = db.connect(load_config())
    res = sample_once(con)
    if res is None:
        print("(no readable browser tab)")
        return 0
    if res.get("tier") == "deny":
        print(f"(deny: matched '{res.get('reason')}' — visit dropped)")
        return 0
    print(json.dumps({k: v for k, v in res.items() if k != "id"},
                     indent=2, default=str))
    return 0


def _cli_show(args: argparse.Namespace) -> int:
    con = db.connect(load_config())
    rows = con.execute(
        """
        SELECT bv.ts, bv.app, bv.domain, bv.stream, bv.is_private,
               bl.raw_title
        FROM browser_visit bv
        LEFT JOIN browser_visit_local bl ON bl.visit_id = bv.id
        ORDER BY bv.ts DESC LIMIT ?
        """,
        (args.limit,),
    ).fetchall()
    if not rows:
        print("(no visits yet)")
        return 0
    for r in rows:
        lock = "🔒" if r["is_private"] else "  "
        title = (r["raw_title"] or "")[:50]
        print(f"  {r['ts'][11:16]} {lock} {r['app']:14s} {r['domain']:32s} "
              f"→ {r['stream'] or '—':12s}  {title}")
    return 0


def _cli_diagnose() -> int:
    print("frontmost app:", _frontmost_app_name())
    tab = current_browser_tab()
    if tab is None:
        print("(no readable browser tab — frontmost app may not be a supported browser)")
        print("supported:", list(_BROWSER_SCRIPTS.keys()))
        return 0
    print(f"  app:    {tab['app']}")
    print(f"  url:    {tab['url'][:80]}")
    print(f"  title:  {tab['title'][:80]}")
    c = wp_projects.classify_url(tab["url"])
    print(f"\nclassification: {c}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(prog="wp browser-tracker",
                                     description="Browser tab sensor + classifier.")
    sub = parser.add_subparsers(dest="cmd", required=True)
    sub.add_parser("sample")
    r = sub.add_parser("run")
    r.add_argument("--interval", type=int, default=30)
    sh = sub.add_parser("show")
    sh.add_argument("--limit", type=int, default=20)
    sub.add_parser("diagnose")
    args = parser.parse_args(argv[1:])
    if args.cmd == "sample":
        return _cli_sample()
    if args.cmd == "run":
        run_loop(interval=args.interval)
        return 0
    if args.cmd == "show":
        return _cli_show(args)
    if args.cmd == "diagnose":
        return _cli_diagnose()
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
