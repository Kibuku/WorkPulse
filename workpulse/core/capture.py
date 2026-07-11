"""
capture.py — `wp capture`, the v2 active-capture verb.

The principle: capture before classify, no friction. A capture is just an
atom with author='human' (system writes captures too — they're how the
dream cycle leaves notes for you). Per PLAN.md §7 step 4.

Three modes:
    wp capture "the thought"                  # positional
    wp capture --file ./notes/today.md        # file body
    echo "from a pipe" | wp capture --stdin   # piped

Pinning (optional, links the capture to another atom via a typed edge):
    --pin session:<id>      # pin to a specific session atom
    --pin plan_item:<id>    # pin to a plan item
    --pin job:<id>          # pin to a (legacy) job id
    --auto-pin              # pin to the currently-open session if any

Output mode for shell pipelines:
    --quiet                 # print only the capture id on success

A capture also lands as a file under captures/YYYY-MM-DD.md — a one-line
markdown entry per capture, in the same spirit as plans/. The DB is the
source of truth; the markdown is for the human's eyes and for grep.
"""

from __future__ import annotations

import argparse
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

from workpulse.core import atoms, db
from workpulse.common import ROOT, ensure_dir, load_config


# ── markdown sidecar ────────────────────────────────────────────────────────

def _captures_dir() -> Path:
    p = ROOT / "captures"
    ensure_dir(p)
    return p


def _today_path() -> Path:
    return _captures_dir() / f"{datetime.now().date().isoformat()}.md"


def _append_md(cap_id: str, ts: str, body: str,
               pinned_kind: str | None, pinned_id: str | None) -> Path:
    """One human-readable line per capture, appended to today's file."""
    p = _today_path()
    is_new = not p.exists()
    pin = f" — pin:{pinned_kind}:{pinned_id}" if pinned_kind and pinned_id else ""
    line = f"- {ts}  {body.strip()}  — id:{cap_id}{pin}\n"
    with p.open("a", encoding="utf-8") as f:
        if is_new:
            f.write(f"# Captures — {datetime.now().date().isoformat()}\n\n")
        f.write(line)
    return p


# ── auto-pin: find the currently-open session ───────────────────────────────

def _current_session_id(con: sqlite3.Connection) -> str | None:
    """The latest session with ended_at IS NULL, or None.

    v1 sensors only close a session by starting a new one (write a closing
    record). With dual-write step 3 we don't explicitly close sessions in
    SQLite — we just write each completed window as its own row with both
    started_at and ended_at filled in. So "currently open" in v2 is
    actually "the most recent session row" — we treat the latest one as
    the live one for pinning purposes.
    """
    row = con.execute(
        "SELECT id FROM session ORDER BY started_at DESC LIMIT 1"
    ).fetchone()
    return row["id"] if row else None


# ── body resolution ─────────────────────────────────────────────────────────

def _resolve_body(args: argparse.Namespace) -> str:
    if args.stdin:
        body = sys.stdin.read()
    elif args.file:
        body = Path(args.file).read_text(encoding="utf-8")
    elif args.text:
        body = " ".join(args.text)
    else:
        raise SystemExit(
            "ERROR: no body. Pass text as a positional arg, --file, or pipe with --stdin."
        )
    body = body.strip()
    if not body:
        raise SystemExit("ERROR: empty capture body.")
    return body


def _resolve_pin(args: argparse.Namespace, con: sqlite3.Connection
                 ) -> tuple[str | None, str | None]:
    if args.pin:
        if ":" not in args.pin:
            raise SystemExit("ERROR: --pin expects 'kind:id' (e.g. session:ABC123).")
        kind, pid = args.pin.split(":", 1)
        if kind not in ("session", "job", "plan_item", "capture"):
            raise SystemExit(f"ERROR: unknown pin kind {kind!r}.")
        return kind, pid
    if args.auto_pin:
        sid = _current_session_id(con)
        if sid is None:
            return None, None
        return "session", sid
    return None, None


# ── core API (callable from other modules — e.g. dashboard, tray) ───────────

def capture(*, body: str, author: str = "human",
            pinned_kind: str | None = None,
            pinned_id: str | None = None,
            cfg: dict | None = None) -> str:
    """Write a capture atom + (optional) typed edge + a markdown sidecar.
    Auto-routes the body through workpulse.core.projects.resolve_match — if a
    project keyword matches, creates a `capture --about_stream--> stream`
    edge AND propagates the stream to the pinned session if untagged.
    Returns the capture id."""
    con = db.connect(cfg)
    cid = atoms.write_capture(
        con, body=body, author=author,
        pinned_kind=pinned_kind, pinned_id=pinned_id,
    )
    ts = datetime.now(timezone.utc).isoformat()

    # Fix (i)+: project routing on every human capture. Uses resolve_all
    # so a "list-capture" ("Today: Acme, Contoso, WorkPulse")
    # extracts EVERY project mentioned. Each becomes an about_stream
    # edge — the cluster assignment pass will pick which clusters in
    # the day belong to which candidate. Session-stream propagation
    # only fires when EXACTLY ONE project matched (otherwise there's
    # no single right answer).
    if author == "human":
        try:
            from workpulse.core import projects as wp_projects
            from workpulse.core.atoms import add_edge
            matches = wp_projects.resolve_all(body)
            for m in matches:
                con.execute(
                    "INSERT OR IGNORE INTO stream(key, label, parent_key) "
                    "VALUES (?, ?, NULL)",
                    (m["stream"], m["label"]),
                )
                add_edge(con, src_kind="capture", src_id=cid,
                         rel="about_stream", dst_kind="stream",
                         dst_id=m["stream"])
            # Single-match → propagate to pinned untagged session.
            # Multi-match → leave session alone; cluster assignment later
            # picks which project the work belongs to.
            if len(matches) == 1 and pinned_kind == "session" and pinned_id:
                row = con.execute(
                    "SELECT stream FROM session WHERE id = ?",
                    (pinned_id,),
                ).fetchone()
                if row is not None and row["stream"] is None:
                    con.execute(
                        "UPDATE session SET stream = ? WHERE id = ?",
                        (matches[0]["stream"], pinned_id),
                    )
                    add_edge(con, src_kind="session", src_id=pinned_id,
                             rel="in_stream", dst_kind="stream",
                             dst_id=matches[0]["stream"])
        except Exception:
            pass  # routing is best-effort; never fail the capture

    try:
        _append_md(cid, ts, body, pinned_kind, pinned_id)
    except Exception:
        pass  # markdown sidecar is a nice-to-have; DB is source of truth
    return cid


# ── CLI ─────────────────────────────────────────────────────────────────────

def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="wp capture",
        description="Capture a thought into WorkPulse. Local, instant, "
                    "linkable to whatever you're focused on.",
    )
    parser.add_argument("text", nargs="*",
                        help="The capture body (positional, joined with spaces).")
    parser.add_argument("--file", metavar="PATH",
                        help="Read the body from a file.")
    parser.add_argument("--stdin", action="store_true",
                        help="Read the body from stdin (for shell pipes).")
    parser.add_argument("--pin", metavar="KIND:ID",
                        help="Pin this capture to another atom "
                             "(e.g. session:ABC, plan_item:XYZ).")
    parser.add_argument("--auto-pin", action="store_true",
                        help="Pin to the currently-focused session if any.")
    parser.add_argument("--author", default="human", choices=("human", "system"),
                        help="Who/what is making the capture. Default: human.")
    parser.add_argument("--quiet", action="store_true",
                        help="Print only the capture id, nothing else.")
    args = parser.parse_args(argv[1:])

    body = _resolve_body(args)
    cfg = load_config()
    con = db.connect(cfg)
    pinned_kind, pinned_id = _resolve_pin(args, con)

    cid = capture(body=body, author=args.author,
                  pinned_kind=pinned_kind, pinned_id=pinned_id, cfg=cfg)

    if args.quiet:
        print(cid)
    else:
        pin_note = f" (pinned to {pinned_kind}:{pinned_id})" if pinned_kind else ""
        print(f"captured  id={cid}{pin_note}")
        print(f"           → {_today_path()}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
