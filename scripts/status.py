"""
status.py — view what WorkPulse has logged.

Usage:
  python scripts\\status.py                  # today's activity
  python scripts\\status.py --date 2026-04-19
  python scripts\\status.py --days 3         # last 3 days
  python scripts\\status.py --stream dissertation
  python scripts\\status.py --live           # tail the log in real time
"""

from __future__ import annotations

import argparse
import io
import json
import sys
import time
from collections import defaultdict
from datetime import date, timedelta
from pathlib import Path

# Force UTF-8 output on Windows
if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import load_config, resolve

# ── ANSI colours (work in Windows Terminal / VS Code terminal) ────────────────
RESET  = "\033[0m"
BOLD   = "\033[1m"
DIM    = "\033[2m"
CYAN   = "\033[36m"
GREEN  = "\033[32m"
YELLOW = "\033[33m"
RED    = "\033[31m"
BLUE   = "\033[34m"

STREAM_COLOURS = {
    "verst-carbon": GREEN,
    "majicom":      BLUE,
    "dissertation": CYAN,
    "consulting":   YELLOW,
    "personal-dev": "\033[35m",  # magenta
}

EVENT_SYMBOLS = {
    "created":    "+",
    "modified":   "~",
    "deleted":    "-",
    "moved_from": ">",
    "moved_to":   "<",
}

def _colour(stream: str | None) -> str:
    return STREAM_COLOURS.get(stream or "", DIM)

def _sym(event_type: str) -> str:
    return EVENT_SYMBOLS.get(event_type, "?")


# ── log loading ───────────────────────────────────────────────────────────────

def _log_path(logs_dir: Path, d: date) -> Path:
    return logs_dir / f"file_events_{d.isoformat()}.jsonl"


def load_events(logs_dir: Path, d: date, stream_filter: str | None = None) -> list[dict]:
    path = _log_path(logs_dir, d)
    if not path.exists():
        return []
    events = []
    with path.open(encoding="utf-8") as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            r = json.loads(line)
            if stream_filter and r.get("stream") != stream_filter:
                continue
            # Skip internal smoke-test files
            if r["path"].endswith(("test_wk.txt", "test_energy_ppa.txt",
                                   "test_vault.txt", "test_desktop.txt",
                                   "smoke_test.txt")):
                continue
            events.append(r)
    return events


# ── display ───────────────────────────────────────────────────────────────────

def _fmt_size(b: int | None) -> str:
    if b is None:
        return ""
    if b < 1024:
        return f"{b}B"
    if b < 1024 ** 2:
        return f"{b/1024:.1f}KB"
    return f"{b/1024**2:.1f}MB"


def print_events(events: list[dict], title: str) -> None:
    if not events:
        print(f"  {DIM}No events.{RESET}")
        return

    print(f"\n{BOLD}{title}{RESET}  {DIM}({len(events)} events){RESET}")
    print(DIM + "-" * 72 + RESET)

    for r in events:
        ts    = r["timestamp"][11:19]
        sym   = _sym(r["event_type"])
        stream = r.get("stream")
        col   = _colour(stream)
        name  = Path(r["path"]).name
        sz    = _fmt_size(r.get("size_bytes"))
        stream_label = f"[{stream}]" if stream else "[--]"

        print(
            f"  {DIM}{ts}{RESET}  "
            f"{col}{sym} {stream_label:<20}{RESET}  "
            f"{name}  {DIM}{sz}{RESET}"
        )


def print_summary(events: list[dict], title: str) -> None:
    if not events:
        return

    by_stream: dict[str, int] = defaultdict(int)
    by_type:   dict[str, int] = defaultdict(int)
    for r in events:
        by_stream[r.get("stream") or "(untagged)"] += 1
        by_type[r["event_type"]] += 1

    print(f"\n{BOLD}{title} — Summary{RESET}")
    print(DIM + "-" * 40 + RESET)
    for stream, count in sorted(by_stream.items(), key=lambda x: -x[1]):
        col = _colour(stream if stream != "(untagged)" else None)
        bar = "#" * min(count, 30)
        print(f"  {col}{stream:<22}{RESET}  {bar} {count}")
    print(f"\n  Event types: " + "  ".join(f"{k}:{v}" for k, v in by_type.items()))


# ── live tail ─────────────────────────────────────────────────────────────────

def live_tail(logs_dir: Path, stream_filter: str | None = None) -> None:
    today = date.today()
    path = _log_path(logs_dir, today)
    print(f"{BOLD}Live tail{RESET} — {path.name}  {DIM}(Ctrl-C to stop){RESET}\n")

    seen = 0
    # Fast-forward past existing lines
    if path.exists():
        with path.open(encoding="utf-8") as f:
            seen = sum(1 for _ in f)

    try:
        while True:
            # Handle day rollover
            new_today = date.today()
            if new_today != today:
                today = new_today
                path = _log_path(logs_dir, today)
                seen = 0

            if path.exists():
                with path.open(encoding="utf-8") as f:
                    lines = f.readlines()
                for line in lines[seen:]:
                    line = line.strip()
                    if not line:
                        continue
                    r = json.loads(line)
                    if stream_filter and r.get("stream") != stream_filter:
                        continue
                    ts     = r["timestamp"][11:19]
                    sym    = _sym(r["event_type"])
                    stream = r.get("stream")
                    col    = _colour(stream)
                    name   = Path(r["path"]).name
                    label  = f"[{stream}]" if stream else "[--]"
                    print(f"  {DIM}{ts}{RESET}  {col}{sym} {label:<20}{RESET}  {name}")
                seen = len(lines)
            time.sleep(0.5)
    except KeyboardInterrupt:
        print(f"\n{DIM}Stopped.{RESET}")


# ── main ──────────────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="WorkPulse activity viewer")
    parser.add_argument("--date",   default="today",  help="Date to view (YYYY-MM-DD or 'today')")
    parser.add_argument("--days",   type=int,          help="Show last N days instead of one day")
    parser.add_argument("--stream", default=None,      help="Filter by stream name")
    parser.add_argument("--live",   action="store_true", help="Tail the log in real time")
    parser.add_argument("--summary", action="store_true", help="Show summary only, no event list")
    args = parser.parse_args()

    cfg = load_config()
    logs_dir = resolve(cfg["paths"]["logs"])

    if args.live:
        live_tail(logs_dir, stream_filter=args.stream)
        return

    # Resolve date range
    if args.days:
        start = date.today() - timedelta(days=args.days - 1)
        dates = [start + timedelta(days=i) for i in range(args.days)]
    else:
        d = date.today() if args.date == "today" else date.fromisoformat(args.date)
        dates = [d]

    all_events = []
    for d in dates:
        evs = load_events(logs_dir, d, stream_filter=args.stream)
        if not args.summary and len(dates) == 1:
            print_events(evs, f"File activity — {d.isoformat()}")
        all_events.extend(evs)

    if len(dates) > 1 and not args.summary:
        print_events(all_events, f"File activity — last {args.days} days")

    print_summary(all_events, dates[0].isoformat() if len(dates) == 1 else f"Last {args.days} days")
    print()


if __name__ == "__main__":
    main()
