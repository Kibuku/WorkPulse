# Ask WorkPulse — port + implementation spec

Status: design note (not yet built). Supersedes `docs/ask-workpulse-phase1.md`.
Njiani is the studio; **WorkPulse** is the tool, so this in-product feature is
**"Ask WorkPulse"**.

## Why this is the whole game

A tracker that only shows you logs is a tracker. The thing that makes WorkPulse a
*companion* is being able to turn to it and ask:

- **"Where is my client Z proposal?"** (and, later, "open it")
- **"How did I work on the NKCC report last week?"** → and "make that an SOP"

If a user cannot do at least these two, they downloaded a tracker. Everything below
is in service of shipping those two, keyless-first, then widening.

The good news from the v1 study (`memory: workpulse-v1-bones`): most of the engine
already exists. `think()` is a working retrieval-augmented Q&A over the user's atoms;
the file paths we need are already captured; and v1's `scripts/llm.py`,
`scripts/jobs.py`, and `scripts/lift.py` are clean, dependency-light code we can port
almost verbatim.

---

## What already exists in v2 (reuse, don't rebuild)

| Piece | Where | Use |
|---|---|---|
| RAG Q&A engine | `core/think.py` — `think(con, question, *, limit, since, stream, with_vector, model, force_fallback, cfg)` returns `{answer, gap, raw, atoms, fallback, ...}` | The general-question path. Already has a keyless templated fallback. |
| Keyword + vector search | `core/search.py` — `search(con, query, *, limit, ...)` | Retrieval inside `think`. |
| Captured file paths | `file_event` (public projection) + **`file_event_local.raw_path`** (private) | The source of truth for "where is this file". |
| Project/stream tagging | `core/projects.py` (`resolve_match(path, projects, kind='path')`), `core/categorize.py`, `job_view`, `cluster_name` | Map a path/session to a project; `job_view` is v2's "Job" rollup. |
| Tag-the-untagged loop | dashboard `POST /api/learn`, `POST /api/v2/cluster/{id}/correct` | The soul is already wired; Ask sits alongside it. |
| Backend status surface | `POST /api/system` (currently anthropic-only + stubbed ollama) | Un-stub when the façade lands. |
| Secrets | `wp_secrets.py` (`anthropic_key`) | Add `prefer_backend`. |
| Personal lock | `POST /api/v2/personal/unlock|lock`, `/api/v2/personal/status` | **Ask must respect this** — `raw_path` is private data behind the password. |
| Web app | `web/app.py` (FastAPI; `con = wp_db.connect(load_config())` per route) | Add the `/api/ask` route + dashboard panel here. |

## What to port from v1 (`D:\WorkPulse\scripts\` on the Windows box)

| v1 file | Ports to | Notes |
|---|---|---|
| `llm.py` | `core/llm.py` (replace the stub) | The entire 3-backend façade. Near-verbatim. |
| `jobs.py` `export_job_markdown` + `_narrative_summary` | `core/retrospective.py` | The "how did I work on X → SOP" output. |
| `lift.py` | `core/lift.py` (Phase 2) | 6 deterministic AI-lift detectors. |
| `job_suggester.py` fingerprint-cache pattern | reuse the idea in `/api/ask` | Cache LLM calls on an activity fingerprint. |
| `doctag.py` | already have `categorize.py`; fold in if needed | Content classify a `.docx`. |

---

## Architecture

```
                    POST /api/ask { question, history[] }
                                  │
                        ┌─────────▼──────────┐
                        │  ask.route(q)      │  deterministic intent first,
                        │  intent classifier │  LLM only to disambiguate
                        └───┬─────┬──────┬───┘
              "where is X"  │     │      │  everything else
            ┌───────────────┘     │      └───────────────┐
            ▼                     ▼                       ▼
     files.locate()      retrospective.summarize()     think()
   (file_event_local)   (job_view + session + files)  (search + skill)
            │                     │                       │
            └──────── format via core/llm.ask_text (or template if no backend) ────────┘
                                  │
              { answer, kind, evidence[], backend, fallback }
```

Two hard rules:
1. **Keyless-first.** `locate` and the retrospective *rollup* are deterministic SQL
   and must return a useful answer with **no API key and no Ollama** — the LLM only
   makes the phrasing conversational. "Where is X" working offline is non-negotiable.
2. **Respect the personal lock.** Any path that reads `*_local` tables checks
   `personal.is_unlocked()` first and returns a clear "unlock to search your files"
   message otherwise (never leak `raw_path` past the lock).

---

## Phase 0 — Backend façade (foundation, ~½ day)

Port v1 `scripts/llm.py` → `workpulse/core/llm.py`, replacing the current stub.

- `active_backend(cfg) -> "anthropic" | "ollama" | "none"`: Anthropic key → anthropic;
  else Ollama daemon up **and** configured model pulled → ollama; else none. Honor a
  `prefer_backend` secret ("always local").
- `_probe_ollama()`: `GET {url}/api/tags`, cached 60s, 1.5s timeout.
- `ask_json(prompt, *, max_tokens, cfg) -> (dict|None, meta)` and
  `ask_text(prompt, *, max_tokens, cfg) -> (str|None, meta)`; `meta =
  {backend, input_tokens, output_tokens, duration_s}`. `_parse_json_loose` strips
  ```json fences for local models.
- Refactor `think.py`: replace the direct `_call_anthropic` with `llm.ask_text`, so
  `think` transparently gains the Ollama and none paths (keep the existing templated
  fallback as the floor when backend == none).
- Un-stub `backend_status()` and wire it into `/api/system` so the Settings page shows
  Local (Ollama) vs Hosted (Claude) vs none, with what's missing.

**Config** (`config/config.example.yaml`, merged into user config by the existing
`_ensure_config` deep-merge):
```yaml
llm:
  model: "claude-sonnet-4-6"      # hosted (Anthropic)
  backend: "auto"                  # auto | anthropic | ollama
  ollama:
    url: "http://127.0.0.1:11434"
    model: "llama3.2:3b"
```

Tests: selector across (key present / absent) × (ollama up+model / down); `ask_json`
loose-parse; `/api/system` shape. Extend `tests/test_think.py`.

---

## Phase 1 — Read-only Ask (the flagship, ~2-3 days)

Ships the two flagship queries and a general fallback. No file mutations, no OS actions.

### 1a. `core/files.py` — "where is this file?"

```python
def locate(con, query: str, *, limit: int = 5, since: str | None = None,
           cfg: dict | None = None) -> list[FileHit]
# FileHit: {path, basename, project, last_touched, touch_count, score}
```

- Reads `file_event_local.raw_path` joined to `file_event.ts` (for recency). **Gated on
  the personal lock.**
- Parse the query for: name tokens ("proposal"), extension hint ("docx/xlsx/pdf"),
  project/client ("client z" → match a stream/project), recency ("last week" → `since`).
- Rank each distinct path by: basename token overlap (strong) + project match
  (`projects.resolve_match`) + recency + touch frequency. Return top `limit`.
- **Deterministic; no LLM required.** Formatting: if a backend is available,
  `llm.ask_text` turns the top hits into one warm sentence; otherwise a template:
  > Your client Z proposal is most likely `…/ClientZ/Proposal_v3.docx`, last edited
  > Tuesday at 3:12pm. Two other files matched — say "show the others".

Performance: a full scan of `file_event_local` is fine at pilot data sizes. If it gets
slow, add an FTS5 virtual table over basenames in a new forward-only migration
(`00XX_file_name_fts.sql`) and query that first. Note the cap in logs if results are
truncated.

### 1b. `core/retrospective.py` — "how did I work on X last week?"

```python
def summarize(con, *, stream: str | None, query: str | None,
              since: str, until: str | None = None, cfg=None) -> dict
# rollup: {stream, window, total_seconds, apps[], day_breakdown[],
#          files_touched[], clusters[], sessions[]}

def to_sop_markdown(rollup: dict, *, cfg=None) -> str   # narrative + steps
```

- Resolve the target: a stream/project (from `query` via `projects`) and a window
  (`since`/`until`, default last 7 days).
- Roll up from `job_view` (clusters in window for the stream) + `session` + the file
  paths touched + relevant `capture`s. Reuse `profile.py`/`report.py` query patterns;
  this mirrors v1 `jobs.rollup`.
- `to_sop_markdown` = port of v1 `jobs._narrative_summary` + `export_job_markdown`,
  driven by a new **`skills/sop.md`** procedure: title, "when to use", numbered steps
  reconstructed from the observed sequence, apps/files used, and a **Gaps** section for
  what the data doesn't show. The narrative prompt must stay **descriptive, never
  evaluative** (Vision §12.2 — no "you worked well/badly").
- Keyless: the rollup renders fully without an LLM (day-by-day + files + apps). The LLM
  upgrades it to a flowing SOP when a backend exists.

### 1c. `POST /api/ask` (in `web/app.py`)

Request `{ question: str, history: [{role, content}] }` →
```json
{ "answer": "…markdown…", "kind": "locate|retrospective|general",
  "evidence": [ {"type":"file","path":"…","last_touched":"…"}, … ],
  "backend": "anthropic|ollama|none", "fallback": false }
```
- `ask.route(question)`: deterministic intent match first (regex/keywords:
  where/find/locate/open + file-ish → locate; how did I / what did I do / summarize /
  "make an SOP" / last week/month → retrospective; else general). Use an LLM
  disambiguation call only when deterministic routing is ambiguous **and** a backend
  exists.
- `general` → `think(con, question, since=…)`, folding the last N history turns into the
  prompt (cap N to bound token cost — the v1 fingerprint-cache trick keeps repeat asks
  cheap).
- Respects the personal lock for `locate`/`retrospective`.

### 1d. Dashboard "Ask" panel (`web/static/`)

An input box + rendered-markdown answer + **evidence chips** (file paths that reveal
the hit, atom/cluster citations) + a backend badge ("Local · Ollama" / "Hosted ·
Claude" / "Manual"). A "Save as SOP" button on retrospective answers writes the
markdown to `reports/sop/<slug>.md` (a benign local write; the only P1 write, clearly
user-initiated).

**Definition of done for P1:** with *no API key*, a user can ask "where is my client Z
proposal" and get the path, and "how did I work on <project> last week" and get a
day-by-day rollup. With a key (or Ollama), both answers become conversational and the
retrospective becomes a real SOP.

Tests: `files.locate` ranking (name/project/recency fixtures); `retrospective.summarize`
rollup shape; `ask.route` intent matrix; lock-respected behavior; `/api/ask` contract;
`skills/sop.md` output shape.

---

## Phase 2 — Actions (the leap from "answers" to "helps")

- **Tools / function-calling** the Ask layer may invoke: `reveal_file(path)` (OS reveal:
  `open -R` / `explorer /select,`), `open_file(path)`, `save_sop(md, dest)`,
  `draft_document(md)`. Each action is confirmed in the UI before it fires.
- **Multi-turn resolution**: "open it" / "that one" resolve against the previous
  answer's evidence list (kept in `history`).
- **Jobs + AI-lift**: port v1 `jobs.py` + `lift.py`. The retrospective becomes
  Job-anchored; the Coach card shows "Jobs in flight" with AI-lift opportunities
  (long Excel → transform script, many-tab research → summary prompt, etc.). This is
  the paid value surface.

## Phase 3 — Ladder to the Vision

- **Fingerprints (v3)**: an opted-in Job retrospective becomes a signed knowledge
  fingerprint (`actor_id` from `identity.yaml` is already the seed). Original files
  never leave the machine.
- **Entitlements/paywall**: a single `entitlements.check(feature, cfg)` consulted by
  `/api/ask` gates the paid tier. Free = deterministic locate + rollups + local-Ollama
  ask. Paid = hosted Anthropic + Jobs/AI-lift + the EOI search agent. Build the check
  now (returns "all allowed") so the switch exists before billing does.

---

## Monetization boundary (where the line sits)

- **Free, always, local:** capture + tagging + `locate` + retrospective *rollups* +
  ask via local Ollama. The user brings the compute; nothing leaves the machine.
- **Paid, hosted:** sharper answers via Anthropic, Jobs + AI-lift, outbound agents
  (EOI). Gated by `entitlements.check`, surfaced honestly ("this sends the relevant
  excerpts to answer your question").

## Cross-cutting requirements

- **Personal lock** honored on every `*_local` read.
- **Graceful degradation** to `none` everywhere (keyless answers always work).
- **Every LLM call logged** to the AI-sessions surface (transparency — v1 did this).
- **User-facing copy** follows the house voice: warm, no em dashes, no arrows, no
  guilt/evaluation (Vision §12.2 + `memory: no-robotic-dashes-client-copy`).
- **Forward-only migrations** only (any new table/FTS index is additive).

## Build order

1. Phase 0 (façade) — unblocks everything, restores Ollama.
2. Phase 1a `files.locate` — the single highest-value, keyless win. Ship it alone if
   needed; it is the "where is my file" moment that proves it is not just a tracker.
3. Phase 1b/c/d — retrospective + `/api/ask` + panel.
4. Phase 2 actions, then Phase 3 ladder.
