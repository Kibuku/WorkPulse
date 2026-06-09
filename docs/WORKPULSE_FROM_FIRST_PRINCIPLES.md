# WorkPulse, From First Principles

A design doc written in the voice of "what would Garry Tan tell me if I started
WorkPulse from scratch tomorrow, knowing what GBrain taught us about brains as
software." This is not a description of what WorkPulse is today. It is the
shape I would build toward if I could choose freely.

Status: draft for review. Lives on branch `from-first-principles`. Main is
untouched. Read it, edit it, decide what to merge.

---

## Frame

WorkPulse today is **a tracker that hopes to grow into a brain**. The
reordering this doc proposes is: **build the brain first; the tracker is one
ingestion source**. Every other recommendation in this file falls out of that
single move.

A secondary reordering: the v3 Fingerprint Engine and v4 Institution Brain in
the Vision doc are scheduled as future builds. The institutional *surface* can
be deferred. The institutional *atom shape* cannot. If you don't design the
atom now to compose upward without leaking, v4 becomes a rewrite.

---

## The ten principles

### 1. Decide the atom; everything else is a derivation

The smallest persistable unit in WorkPulse is one of four things:

- `session` — a window-focus interval (app, title, start, end, files touched)
- `file_event` — a single file modification
- `ai_call` — one LLM interaction (provider, tokens, cost, prompt slug)
- `capture` — a human-authored thought, tied to whatever was focused at the time

Jobs, plans, lifts, learned tags, reports, the dashboard, the coach surfaces —
all of them are SQL views or markdown-skill outputs over these four atoms. If
you cannot reconstruct a feature by re-running a query, the feature is in the
wrong place.

The Vision doc says "Job is the unit of analysis." This doc says: Job is the
unit of analysis; it is not the unit of storage. Storage is the session.
Analysis is a materialized view. Collapsing those two ideas is what makes
schema migrations painful.

### 2. Embedded SQL + vector is the substrate

The principle: one file on disk, no server, no Docker, real indexes, real
foreign keys, real vector search when needed. Two-second setup.

PGLite (Postgres+pgvector compiled to WASM) is the JS-native expression of
this shape. WorkPulse is Python; the Python-native expression of the same
shape is **SQLite + sqlite-vec** — pure-C extension, `pip install`-able,
single-file DB. We use sqlite-vec. The atom schema, the graph, the three
verbs, and every higher-level principle are unchanged.

JSONL was the right v0 choice for shipping. It is the wrong choice for the
brain. Every brain-shaped feature you want next — hybrid retrieval, contradiction
detection, time-travel queries, institutional aggregation — wants a database.

### 3. Draw the latent/deterministic line on paper, before any code

Two columns. Every feature goes in one.

**Deterministic** (SQL/code, never LLM):
- Time totals, stream rollups, file change counts
- Plan-vs-actual minutes, cost tracking
- Learned-tag rule matching
- Session clustering by time + stream proximity

**Latent** (LLM, never SQL):
- "What was the thread of work across Tuesday afternoon?"
- "Why is the NKCC narrative stalled?"
- "What should I prepare for tomorrow's Mwangi call?"
- First-time classification of an unfamiliar window

The rule for borderline cases: **pay the LLM once, deterministic forever.**
`learning.py`'s "classify once, cache rule" pattern is the correct shape for
every borderline case. The work-type emoji keying (📝📊🔬💻) is currently a
regex; it should be one LLM call cached forever.

### 4. Three verbs, not seven surfaces

The entire user-facing surface is three verbs.

- **`wp capture`** — passive (sensors write session atoms) and active (CLI,
  tray hotkey, dashboard textarea). One verb, two modes. A captured thought
  is just an atom with a human author.
- **`wp search`** — hybrid retrieval over atoms: BM25 + small local embedding
  + RRF + recency boost + stream boost. Returns ranked atoms. No synthesis.
- **`wp think`** — takes a question, runs `search`, composes a cited answer
  **plus a mandatory gap section**. No `think` answer ships without "here's
  what WorkPulse doesn't know yet." The gap section is the whole differentiator
  between a tracker and a brain.

The dashboard is a UI over those verbs. Reports are `wp think` with a fixed
question on a cron. The coach is `wp think` with a different fixed question.
The plan reconciliation is `wp search` over today's sessions joined with
today's plan items. The lift detector is a markdown skill that runs `wp think`
nightly.

### 5. Markdown is code; judgment lives in skill files

The harness does file I/O, model loops, SQL, sensors, web. **It does not
encode policy.** Judgment lives in markdown:

```
skills/
  classify.md       # when to tag, what to tag as, when to ask the user
  remember.md       # when to surface prior work, on what evidence floor
  coach.md          # when to nudge — and the rule "popup last, always"
  cluster.md        # how sessions become Jobs
  report-daily.md   # what a daily report contains, in what voice
  report-weekly.md
  consolidate.md    # what the dream cycle looks for
  think.md          # how to answer a question with citations + gap section
```

Each is a procedure the LLM reads at runtime. Same skill, different parameters,
different result. When policy changes, you edit English, not Python.

This costs LLM tokens at runtime. The deterministic floor (Jaccard, rule-based
classification) must survive as a zero-key fallback path on every judgment
surface. Local-first is non-negotiable.

### 6. The graph is self-wiring from session #1

Every session, on write, extracts typed edges with zero LLM calls:

- `session --in_app--> app`
- `session --in_stream--> stream`
- `session --touched_file--> file`
- `session --during_plan_item--> plan_item`
- `session --in_window_of--> ai_call`
- `capture --about_job--> job`

This is the move that gave GBrain +31.4 P@5 over its graph-disabled variant.
Inferring relationships at query time via token overlap is expensive and lossy.
Type the edges at write time and the queries that are currently impossible
become one SQL hop.

### 7. Capture before classify; "untagged" is a first-class state

The tracker never blocks on the LLM. Every session is captured raw, immediately.
Classification is opportunistic and async. "Untagged" is not an error state to
clean up — it is a normal state that the nightly consolidation triages.

Consequence: the brain surfaces untagged-time patterns as *insight* ("you have
4h/week consistently untagged at 3pm — want a stream for it?") rather than as
garbage.

### 8. Popup last; the coach earns the right to interrupt

The user pulls, the system does not push. Until `wp think` answers questions
well enough that the user comes back unprompted, no popup, no proactive email,
no interruption. The daily email report should be reconsidered once `wp think`
is good — it may be load-bearing emotionally but not structurally.

### 9. Design the fingerprint at v0, even if you ship the institution at v4

The session atom is shaped so that an institutional projection is a query, not
a rewrite. Two tables:

- `session` (institutional-safe): `id, stream, app_category, cluster_id,
  duration, title_hash, started_at`
- `session_local` (never leaves the machine): `session_id, raw_title, raw_path,
  raw_files[]`

The Institution Brain queries `session` and joins on `stream` and
`cluster_id`. It never reads `session_local`. Designed now, the v4
institutional layer is an auth predicate plus a query. Skipped now, it is a
schema migration across the whole user base.

### 10. Dream cycle, not reports

Nightly cron runs a consolidation pass (a single `wp think` invocation against
`skills/consolidate.md`):

- Clusters loose sessions into Jobs (Jobs are a view, computed nightly)
- Dedups Jobs with high overlap (same work, two names)
- Surfaces contradictions (planned 30 min, logged 4h, three days running)
- Retires learned-tag rules that haven't fired in 60 days
- Triages the week's untagged minutes into proposals
- Writes one markdown file: `consolidation/YYYY-MM-DD.md`

You read it with coffee. That is the "wake up smarter" loop. Email reports are
a derivation of this; they are not the primary artifact.

---

## Architecture in one diagram

```
                        ┌───────────────────────────┐
                        │   Skills (markdown)       │
                        │   classify, remember,     │
                        │   cluster, think,         │
                        │   consolidate, coach      │
                        └─────────────┬─────────────┘
                                      │ read at runtime
                                      ▼
┌──────────────┐    ┌──────────────────────────────────┐    ┌────────────┐
│  Sensors     │───▶│   Harness (thin Python, ~500 LoC) │───▶│  LLM (any) │
│  win / mac   │    │   file I/O, web, model loop,      │    │  Anthropic │
│  watcher     │    │   SQL, safety, sensors            │    │  Local FB  │
│  ai_logger   │    └──────────────┬───────────────────┘    └────────────┘
└──────────────┘                   │
                                   │ writes atoms + edges
                                   ▼
                        ┌───────────────────────────┐
                        │   PGLite (one file)       │
                        │   session, file_event,    │
                        │   ai_call, capture,       │
                        │   edges (typed)           │
                        │   session_local (private) │
                        └─────────────┬─────────────┘
                                      │ queries
                                      ▼
                  ┌────────────────────────────────────────┐
                  │  Three verbs                            │
                  │   wp capture    wp search    wp think   │
                  └─────────────────┬──────────────────────┘
                                    │
                ┌───────────────────┼──────────────────────┐
                ▼                   ▼                      ▼
         Dashboard UI         Dream cycle cron       Reports / coach
         (thin shell)         (consolidate.md)       (think + cron)
```

---

## Build order if starting Monday

1. **Substrate.** PGLite. Schema for four atoms + edges + a `skill_runs` audit
   table. Migration script from existing JSONL → PGLite, run once.
2. **Sensors → atoms.** Port `activity.py`, `watcher.py`, `ai_logger.py` to
   write into PGLite. No surfaces yet. Live with raw atoms for 3–5 days.
3. **`wp capture`** as CLI + tray hotkey. Use it. Notice friction.
4. **`wp search`.** Hybrid retrieval. Live with it for a week. Notice what you
   ask it.
5. **`skills/think.md` + `wp think`.** First synthesis verb. Citations
   mandatory. Gap section mandatory.
6. **Materialize Jobs as a view.** Now, not before. The clustering rules live
   in `skills/cluster.md`.
7. **Dashboard.** Thin UI over the three verbs. No new logic.
8. **Dream cycle.** Nightly `wp think` against `skills/consolidate.md`. One
   markdown file out.
9. **Reports.** If still wanted, they are `wp think` + an email sender. ~30
   lines.
10. **Coach surfaces.** Only after the user comes back to `wp think`
    unprompted. Then design what the system says first.

What is missing from steps 1–5: plans, lifts, remembrance, learned tags,
taxonomy wizard. All of them fall out of the three verbs once the substrate is
right. They shipped as bespoke surfaces because the substrate was not ready to
give them away for free.

---

## Open tensions (push back here)

These are assertions, not conclusions. Disagreement reshapes the framework.

1. **"Brain first, tracker is one source."** If false, the whole framework
   collapses. The tracker is currently the entire identity of WorkPulse;
   demoting it to "one ingestion source" is a real psychological move.
2. **"Session is the atom, Job is a view."** Possibly just a vocabulary
   disagreement with the Vision doc. Worth confirming.
3. **"PGLite over JSONL, now."** Non-trivial migration cost on working
   software. The bet is that the payoff (graph, hybrid retrieval, contradictions,
   fingerprint) is worth the rewrite. False if JSONL is load-bearing for any
   external integration.
4. **"Three verbs, not seven surfaces."** Most aggressive simplification.
   Specific surfaces (plan card, remembrance card) survive as UI; their
   bespoke logic does not.
5. **"Design fingerprint at v0."** Cost now: a slightly more careful schema.
   Cost later if skipped: a rewrite across the user base.
6. **"Popup last; reports may not survive."** Questions whether the daily
   email is structurally useful or just emotionally useful. Honest answer
   either way is fine; the question is the point.
7. **"Markdown skills for judgment."** Doubles the surface (LLM path + zero-key
   fallback). Worth it if local-first is sacred. Painful if not.

---

## What this doc is not

- Not a deprecation of v1. WorkPulse today works. This is the shape of v2 or
  v3, not a demand to scrap what exists.
- Not a copy of GBrain. GBrain is a personal/company brain over notes; WorkPulse
  is a brain over *attention*. Same software shape, different domain atom.
- Not final. The tensions above are real. Edit this file, push back, and the
  framework adapts.

---

## Provenance

Written in dialogue, 2026-06-09. Source material: `/tmp/gbrain` (Garry Tan's
GBrain repo, README, DESIGN.md, ethos essays "Thin Harness, Fat Skills" and
"Homebrew for Personal AI"), plus a read of WorkPulse's `scripts/remembrance.py`,
`scripts/learning.py`, `scripts/plans.py`, and the v1.7 commit message.
