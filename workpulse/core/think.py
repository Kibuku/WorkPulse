"""
think.py — `wp think`, the v2 synthesis verb.

The brain layer that GBrain calls `think`. Takes a question, runs `wp
search` internally, calls the LLM with skills/think.md as the procedure,
returns Answer + mandatory Gap section with citations to atom IDs.

Zero-key fallback: if no Anthropic key is configured, returns the top-N
atoms with a templated framing that still respects the contract (Answer
+ Gap headings, atom IDs as "citations"). The fallback is intentionally
less useful than the LLM path — that's the price of staying offline-
capable, and the framework doc records it as a load-bearing tension.

Public API:
    think(con, question, *, limit=10, since=None, stream=None,
          with_vector=False, model=None, force_fallback=False,
          cfg=None) -> dict

    Result shape:
      {
        "question":   str,
        "answer":     str,         # markdown body of the Answer section
        "gap":        str,         # markdown body of the Gap section
        "raw":        str,         # full LLM (or fallback) markdown response
        "atoms":      list[dict],  # the retrieved atoms used as evidence
        "model":      str | None,
        "fallback":   bool,
        "skill_run":  str,         # id of the skill_run row
      }

CLI:
    python -m workpulse.core.think "what's the state of the Acme project?"
    python -m workpulse.core.think "..." --stream dev --limit 15 --vector
    python -m workpulse.core.think "..." --no-llm    # force fallback
    python -m workpulse.core.think "..." --json      # emit the result dict
"""

from __future__ import annotations

import argparse
import json
import os
import re
import sqlite3
import sys
import time
from datetime import datetime, timezone
from pathlib import Path

from workpulse.core import atoms, db, search
from workpulse.common import ROOT, load_config, PKG


_SKILL_PATH = PKG / "skills" / "think.md"
_DEFAULT_MODEL = "claude-sonnet-4-5-20250929"  # current default; cfg may override


# ── skill loading ────────────────────────────────────────────────────────────

def _load_skill() -> str:
    """Read skills/think.md. Markdown is code — the file is the procedure."""
    try:
        return _SKILL_PATH.read_text(encoding="utf-8")
    except FileNotFoundError:
        # Last-ditch inline fallback so think() never crashes on a missing skill.
        return (
            "Produce two markdown sections in order: '## Answer' (synthesized, "
            "with [ID] citations) and '## Gap' (what the brain doesn't know yet)."
        )


# ── atom rendering for the LLM context ───────────────────────────────────────

def _atom_block(a: dict) -> str:
    """Compact rendering of one atom for the LLM. ID first so citations are
    easy to copy back."""
    ts = (a.get("ts") or "")[:19]
    stream = a.get("stream") or "—"
    kind = a.get("atom_kind") or "?"
    aid = a.get("atom_id") or "?"
    content = (a.get("content") or "").strip()
    if len(content) > 400:
        content = content[:400] + "…"
    return f"- [{aid}] kind={kind} ts={ts} stream={stream}\n  {content}"


def _build_prompt(question: str, atoms_list: list[dict], skill: str) -> str:
    if atoms_list:
        evidence = "\n".join(_atom_block(a) for a in atoms_list)
    else:
        evidence = "(no atoms matched — the brain has nothing relevant.)"
    return (
        f"{skill}\n\n"
        f"---\n\n"
        f"USER QUESTION:\n{question}\n\n"
        f"ATOMS (most relevant first):\n{evidence}\n"
    )


# ── Anthropic call ──────────────────────────────────────────────────────────

def _api_key(cfg: dict | None) -> str | None:
    key = os.environ.get("ANTHROPIC_API_KEY")
    if key:
        return key
    try:
        from workpulse.wp_secrets import get as get_secret  # type: ignore
        return get_secret("anthropic_key") or None
    except Exception:
        return None


def _call_anthropic(prompt: str, *, model: str, cfg: dict | None
                    ) -> tuple[str, int, int, float] | None:
    """Returns (text, in_tokens, out_tokens, duration_s) or None on failure."""
    key = _api_key(cfg)
    if not key:
        return None
    try:
        import anthropic  # type: ignore
    except ImportError:
        return None
    client = anthropic.Anthropic(api_key=key)
    start = time.monotonic()
    try:
        resp = client.messages.create(
            model=model,
            max_tokens=1024,
            messages=[{"role": "user", "content": prompt}],
        )
    except Exception:
        return None
    duration = time.monotonic() - start
    text = ""
    for block in resp.content:
        if getattr(block, "type", None) == "text":
            text += block.text
    return text, int(resp.usage.input_tokens), int(resp.usage.output_tokens), duration


def _cost(in_tok: int, out_tok: int, cfg: dict | None) -> float:
    pricing = (cfg or {}).get("llm", {}).get("pricing") or {}
    in_p  = float(pricing.get("input_per_mtok",  3.0))  # default Sonnet 4.5 rate
    out_p = float(pricing.get("output_per_mtok", 15.0))
    return round((in_tok / 1_000_000) * in_p + (out_tok / 1_000_000) * out_p, 6)


# ── fallback (zero-key) ─────────────────────────────────────────────────────

def _fallback(question: str, atoms_list: list[dict]) -> str:
    """Templated Answer + Gap that still respects the output contract.
    Intentionally less useful than the LLM path."""
    if not atoms_list:
        return (
            "## Answer\n\n"
            "The brain doesn't have what you're asking about yet.\n\n"
            "## Gap\n\n"
            "Zero atoms matched this query. Either the topic hasn't been "
            "captured yet, or the wording is too far from what's indexed. "
            "Try `wp capture` to add the missing context, or rephrase.\n"
        )
    lines = ["## Answer", "", f"Top {len(atoms_list)} matches for "
             f"`{question}`:", ""]
    for a in atoms_list:
        ts = (a.get("ts") or "")[:10]
        stream = a.get("stream") or "—"
        kind = a.get("atom_kind") or "?"
        aid = a.get("atom_id") or "?"
        content = (a.get("content") or "").strip().replace("\n", " ")
        if len(content) > 120:
            content = content[:120] + "…"
        lines.append(f"- [{aid}] {kind} {ts} {stream} — {content}")
    lines += ["", "## Gap", ""]
    # Specific gaps the fallback can derive without an LLM.
    kinds = {a.get("atom_kind") for a in atoms_list}
    gap_bits = []
    if "capture" not in kinds:
        gap_bits.append("No captures in this result set — only passive sensor "
                        "data. Whatever you've decided about this isn't written "
                        "down, only what you've been *doing* near it.")
    untagged = sum(1 for a in atoms_list if not a.get("stream"))
    if untagged:
        gap_bits.append(f"{untagged} of {len(atoms_list)} atoms are untagged. "
                        f"The brain can't tell you their stream yet.")
    if not gap_bits:
        gap_bits.append("No LLM available — this is raw retrieval, not "
                        "synthesis. Set `ANTHROPIC_API_KEY` or configure the "
                        "key via the dashboard to get a real answer.")
    lines.extend(gap_bits)
    return "\n".join(lines) + "\n"


# ── output parsing ──────────────────────────────────────────────────────────

_SECTION_RE = re.compile(r"##\s*(Answer|Gap)\s*\n+(.*?)(?=\n##\s|\Z)",
                         re.DOTALL | re.IGNORECASE)


def _split_sections(text: str) -> tuple[str, str]:
    answer, gap = "", ""
    for m in _SECTION_RE.finditer(text):
        name = m.group(1).lower()
        body = m.group(2).strip()
        if name == "answer":
            answer = body
        elif name == "gap":
            gap = body
    return answer, gap


# ── public API ──────────────────────────────────────────────────────────────

def think(con: sqlite3.Connection, question: str, *,
          limit: int = 10, since: str | None = None,
          stream: str | None = None, with_vector: bool = False,
          model: str | None = None, force_fallback: bool = False,
          cfg: dict | None = None) -> dict:
    cfg = cfg or {}
    model = model or (cfg.get("llm", {}) or {}).get("model") or _DEFAULT_MODEL
    skill = _load_skill()

    retrieved = search.search(con, question, limit=limit, since=since,
                              stream=stream, with_vector=with_vector)
    prompt = _build_prompt(question, retrieved, skill)

    raw = ""
    used_model: str | None = None
    in_tok = out_tok = 0
    duration_s = 0.0
    fallback = True
    status = "fallback"

    if not force_fallback:
        result = _call_anthropic(prompt, model=model, cfg=cfg)
        if result is not None:
            raw, in_tok, out_tok, duration_s = result
            used_model = model
            fallback = False
            status = "ok"

    if fallback:
        raw = _fallback(question, retrieved)

    answer, gap = _split_sections(raw)
    if not answer:
        # The model didn't follow the contract. Wrap whatever it gave us.
        answer = raw.strip()
    if not gap:
        gap = "(no gap section produced — model failed to follow contract.)"

    # Record the call to skill_run and (if LLM used) ai_call.
    skill_run_id = _record(con, slug="think", question=question,
                           raw=raw, model=used_model,
                           in_tok=in_tok, out_tok=out_tok,
                           cost_usd=_cost(in_tok, out_tok, cfg),
                           status=status, cfg=cfg)

    return {
        "question":  question,
        "answer":    answer,
        "gap":       gap,
        "raw":       raw,
        "atoms":     retrieved,
        "model":     used_model,
        "fallback":  fallback,
        "skill_run": skill_run_id,
    }


def _record(con: sqlite3.Connection, *, slug: str, question: str,
            raw: str, model: str | None, in_tok: int, out_tok: int,
            cost_usd: float, status: str, cfg: dict | None) -> str:
    sr_id = atoms.new_id()
    ts = datetime.now(timezone.utc).isoformat()
    con.execute(
        """
        INSERT INTO skill_run
          (id, ts, skill_slug, parent_run_id, model, in_tokens, out_tokens,
           cost_usd, input, output, status)
        VALUES (?, ?, ?, NULL, ?, ?, ?, ?, ?, ?, ?)
        """,
        (sr_id, ts, slug, model, in_tok, out_tok, cost_usd,
         question[:2000], raw[:8000], status),
    )
    if model and not status == "fallback":
        atoms.write_ai_call(
            con, provider="anthropic", model=model,
            in_tokens=in_tok, out_tokens=out_tok, cost_usd=cost_usd,
            prompt_slug=f"skill:{slug}", ts=ts,
        )
    return sr_id


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli(args: argparse.Namespace) -> int:
    con = db.connect(cfg=load_config(), vec=args.vector)
    result = think(
        con, args.question,
        limit=args.limit, since=args.since, stream=args.stream,
        with_vector=args.vector, force_fallback=args.no_llm,
        cfg=load_config(),
    )
    if args.json:
        print(json.dumps(result, indent=2, default=str))
        return 0
    print(result["raw"])
    tag = " (fallback)" if result["fallback"] else f" (model={result['model']})"
    print(f"\n— wp think · {len(result['atoms'])} atoms · "
          f"skill_run={result['skill_run']}{tag}")
    return 0


def main(argv: list[str]) -> int:
    parser = argparse.ArgumentParser(
        prog="wp think",
        description="Synthesize an answer from your WorkPulse brain.",
    )
    parser.add_argument("question", nargs="+",
                        help="The question to think about.")
    parser.add_argument("--limit", type=int, default=10)
    parser.add_argument("--since", metavar="YYYY-MM-DD")
    parser.add_argument("--stream")
    parser.add_argument("--vector", action="store_true",
                        help="Enable vector half of retrieval (needs fastembed).")
    parser.add_argument("--no-llm", action="store_true",
                        help="Force the zero-key fallback path.")
    parser.add_argument("--json", action="store_true",
                        help="Emit the full result as JSON.")
    args = parser.parse_args(argv[1:])
    args.question = " ".join(args.question).strip()
    if not args.question:
        parser.error("missing question")
    return _cli(args)


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
