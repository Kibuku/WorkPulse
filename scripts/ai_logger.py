"""
ai_logger.py — Phase 3: AI session logger.

Two entry points:

  1. Python wrapper (import and use in code):
       from scripts.ai_logger import WorkPulseAI
       wp = WorkPulseAI(stream="dissertation")
       response = wp.message("Summarise this paragraph: ...")

  2. Manual CLI (for sessions on Claude.ai / Cowork you run by hand):
       python scripts\\ai_logger.py log
       python scripts\\ai_logger.py show --last 10
"""

from __future__ import annotations

import argparse
import io
import json
import os
import sys
import uuid
from datetime import datetime, timezone
from pathlib import Path

if hasattr(sys.stdout, "buffer"):
    sys.stdout = io.TextIOWrapper(sys.stdout.buffer, encoding="utf-8", errors="replace")
    sys.stderr = io.TextIOWrapper(sys.stderr.buffer, encoding="utf-8", errors="replace")

import anthropic

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import ensure_dir, get_env, load_config, resolve

# ── config ────────────────────────────────────────────────────────────────────

def _cfg():
    return load_config()


def _log_path(cfg: dict) -> Path:
    return resolve(cfg["paths"]["logs"]) / "ai_sessions.jsonl"


def _pricing(cfg: dict):
    p = cfg["llm"]["pricing_per_million_tokens"]
    return p["input_usd"], p["output_usd"]


def _cost(input_tokens: int, output_tokens: int, cfg: dict) -> float:
    inp_rate, out_rate = _pricing(cfg)
    return round(
        (input_tokens / 1_000_000) * inp_rate + (output_tokens / 1_000_000) * out_rate,
        6,
    )


# ── session record ────────────────────────────────────────────────────────────

def _build_record(
    *,
    stream: str | None,
    task_summary: str,
    model: str,
    input_tokens: int,
    output_tokens: int,
    duration_minutes: float | None,
    tool_used: str,
    cfg: dict,
) -> dict:
    return {
        "timestamp": datetime.now(timezone.utc).isoformat(),
        "session_id": str(uuid.uuid4()),
        "stream": stream,
        "task_summary": task_summary,
        "tool_used": tool_used,
        "model": model,
        "input_tokens": input_tokens,
        "output_tokens": output_tokens,
        "estimated_cost_usd": _cost(input_tokens, output_tokens, cfg),
        "duration_minutes": duration_minutes,
    }


def _append(record: dict, cfg: dict) -> Path:
    log_path = _log_path(cfg)
    ensure_dir(log_path.parent)
    with log_path.open("a", encoding="utf-8") as f:
        f.write(json.dumps(record, ensure_ascii=False) + "\n")
    # v2 dual-write: best-effort SQLite atom alongside the JSONL.
    try:
        from scripts.dual_write import dual_write_ai_call
        dual_write_ai_call(record, cfg)
    except Exception:
        pass
    return log_path


# ── public helper for other WorkPulse modules ────────────────────────────────

def log_session(
    *,
    stream: str | None,
    task_summary: str,
    input_tokens: int,
    output_tokens: int,
    tool_used: str,
    duration_minutes: float | None = None,
    cfg: dict | None = None,
) -> None:
    """Append an AI-session record to logs/ai_sessions.jsonl.

    Public entry point for any WorkPulse module that calls the Anthropic SDK
    directly (Loop B classifier, doctag, future modules). Silent on any error
    so a logging failure never breaks the caller's hot path.
    """
    try:
        if cfg is None:
            cfg = _cfg()
        record = _build_record(
            stream=stream,
            task_summary=task_summary[:200],  # cap
            model=cfg["llm"]["model"],
            input_tokens=int(input_tokens or 0),
            output_tokens=int(output_tokens or 0),
            duration_minutes=duration_minutes,
            tool_used=tool_used,
            cfg=cfg,
        )
        _append(record, cfg)
    except Exception:
        # Never let a logging failure kill the classifier
        pass


# ── Python wrapper ─────────────────────────────────────────────────────────────

class WorkPulseAI:
    """
    Thin wrapper around the Anthropic SDK that auto-logs each call.

    Usage:
        wp = WorkPulseAI(stream="dissertation")
        text = wp.message("Summarise this paragraph: ...", task="Summarise section 3")
    """

    def __init__(self, stream: str | None = None):
        self._cfg = _cfg()
        self._stream = stream
        self._model = self._cfg["llm"]["model"]
        from scripts.wp_secrets import get as get_secret
        api_key = get_secret("anthropic_key")
        if not api_key:
            raise RuntimeError(
                "No Anthropic API key configured. Set it in the WorkPulse dashboard "
                "Settings page, or via the ANTHROPIC_API_KEY env var."
            )
        self._client = anthropic.Anthropic(api_key=api_key)

    def message(
        self,
        prompt: str,
        *,
        task: str = "",
        max_tokens: int = 2048,
        system: str = "",
    ) -> str:
        """Send a single user message, return the text response, and log the session."""
        import time

        start = time.monotonic()
        kwargs = dict(
            model=self._model,
            max_tokens=max_tokens,
            messages=[{"role": "user", "content": prompt}],
        )
        if system:
            kwargs["system"] = system

        response = self._client.messages.create(**kwargs)
        duration = round((time.monotonic() - start) / 60, 3)

        record = _build_record(
            stream=self._stream,
            task_summary=task or prompt[:120],
            model=self._model,
            input_tokens=response.usage.input_tokens,
            output_tokens=response.usage.output_tokens,
            duration_minutes=duration,
            tool_used="anthropic-sdk",
            cfg=self._cfg,
        )
        _append(record, self._cfg)
        return response.content[0].text


# ── CLI: log ──────────────────────────────────────────────────────────────────

def _streams_list(cfg: dict) -> list[str]:
    return list(cfg["streams"].keys())


def cmd_log(cfg: dict) -> None:
    """Interactive prompt for manually logging a Claude.ai / Cowork session."""
    streams = _streams_list(cfg)
    print("\n-- WorkPulse AI Session Logger -------------------------")
    print(f"Streams: {', '.join(streams)}")
    print("(Press Ctrl-C to cancel)\n")

    # stream
    stream = input(f"Stream [{'/'.join(streams)}]: ").strip().lower()
    if stream not in streams:
        print(f"Unknown stream '{stream}'. Using None.")
        stream = None

    # task summary
    task_summary = input("Task summary (what did you ask / accomplish?): ").strip()
    if not task_summary:
        print("Task summary is required.")
        sys.exit(1)

    # token counts
    try:
        input_tokens = int(input("Input tokens (approx): ").strip() or "0")
        output_tokens = int(input("Output tokens (approx): ").strip() or "0")
    except ValueError:
        input_tokens = output_tokens = 0

    # duration
    try:
        duration_raw = input("Duration in minutes (optional): ").strip()
        duration = float(duration_raw) if duration_raw else None
    except ValueError:
        duration = None

    # tool
    tool = input("Tool used [claude.ai / cowork / api / other]: ").strip() or "claude.ai"

    model = cfg["llm"]["model"]
    record = _build_record(
        stream=stream,
        task_summary=task_summary,
        model=model,
        input_tokens=input_tokens,
        output_tokens=output_tokens,
        duration_minutes=duration,
        tool_used=tool,
        cfg=cfg,
    )

    log_path = _append(record, cfg)
    cost = record["estimated_cost_usd"]
    print(f"\n✓ Logged to {log_path}")
    print(f"  Cost estimate: ${cost:.4f}  |  tokens: {input_tokens} in / {output_tokens} out")


# ── CLI: show ─────────────────────────────────────────────────────────────────

def cmd_show(cfg: dict, last: int = 10) -> None:
    log_path = _log_path(cfg)
    if not log_path.exists():
        print("No sessions logged yet.")
        return

    with log_path.open(encoding="utf-8") as f:
        lines = f.readlines()

    recent = lines[-last:]
    total_cost = 0.0
    print(f"\n── Last {len(recent)} AI sessions ──────────────────────────")
    for line in recent:
        r = json.loads(line)
        ts = r["timestamp"][:16].replace("T", " ")
        cost = r.get("estimated_cost_usd", 0)
        total_cost += cost
        print(
            f"  {ts}  [{r.get('stream') or '—':18s}]  "
            f"${cost:.4f}  {r.get('task_summary', '')[:60]}"
        )
    print(f"\n  Subtotal (shown): ${total_cost:.4f}")


# ── entry point ───────────────────────────────────────────────────────────────

def main() -> None:
    parser = argparse.ArgumentParser(description="WorkPulse AI session logger")
    sub = parser.add_subparsers(dest="cmd")

    sub.add_parser("log", help="Manually log a Claude.ai / Cowork session")

    show_p = sub.add_parser("show", help="Display recent logged sessions")
    show_p.add_argument("--last", type=int, default=10, metavar="N", help="Show last N sessions")

    args = parser.parse_args()
    cfg = _cfg()

    if args.cmd == "log":
        try:
            cmd_log(cfg)
        except KeyboardInterrupt:
            print("\nCancelled.")
    elif args.cmd == "show":
        cmd_show(cfg, last=args.last)
    else:
        parser.print_help()


if __name__ == "__main__":
    main()
