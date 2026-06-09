# WorkPulse — Implementation Plan (v2, from first principles)

This is the plan for the next shape of WorkPulse, written against the
framework in [`docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md`](docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md).
The previous plan (everything built through v1.7) is preserved verbatim in
[`PLAN.v1.md`](PLAN.v1.md) — read it for the full history of what shipped and
why. Nothing in v1 is being deleted; this plan describes the shape we're
building **toward**, not a deprecation.

The single sentence summary: **stop building a tracker; start building a
brain that happens to have a tracker as one of its ingestion sources.**

---

## 0. Status

- Branch: `from-first-principles`
- Main: untouched. v1 continues to work and ship.
- This plan is open for edit. Tensions in §7 are unresolved by design.

---

## 1. Goal (revised)

A local-first personal **attention brain**. The tracker is one of its sources.
The user interacts through three verbs (`capture`, `search`, `think`). Every
surface — dashboard, reports, coach, plan reconciliation — is a derivation of
those three verbs over a shared atom store.

Local-first is non-negotiable. Zero-key operation must remain possible on every
judgment surface via a deterministic fallback path.

---

## 2. Architecture (target state)

```
Skills (markdown, judgment)
        │
        ▼
Sensors → Harness (thin Python) → LLM (Anthropic + zero-key fallback)
                │
                ▼
        PGLite (atoms + typed edges + private projection)
                │
                ▼
        Three verbs: capture / search / think
                │
        ┌───────┼────────┐
        ▼       ▼        ▼
   Dashboard  Dream   Reports +
              cycle   coach
```

Full diagram and explanation in
[`docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md`](docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md).

---

## 3. The atom schema

Four atom types. Everything else is a view over these.

| Atom | Key fields | Written by |
|---|---|---|
| `session` | id, started_at, ended_at, app, title_hash, stream, cluster_id | sensors |
| `file_event` | id, ts, path_hash, kind (created/modified/deleted) | watcher |
| `ai_call` | id, ts, provider, model, in_tokens, out_tokens, cost, prompt_slug | ai_logger |
| `capture` | id, ts, author (human/system), body, pinned_to (job/session/null) | CLI + tray + dashboard |

Typed edges, extracted at write time, zero LLM:

- `session --in_app--> app`
- `session --in_stream--> stream`
- `session --touched_file--> file_event`
- `session --during_plan_item--> plan_item`
- `session --in_window_of--> ai_call`
- `capture --about--> {job | session | plan_item}`

Private projection (never leaves the machine, separate table):

- `session_local`: `session_id, raw_title, raw_path, raw_files[]`

The public `session` table is institutional-safe by construction. The Institution
Brain (Vision v4) queries it as a normal SQL surface with an auth predicate. No
schema migration required at v4.

---

## 4. The three verbs

### 4.1 `wp capture`
- Passive: sensors write session / file_event / ai_call atoms continuously.
- Active: `wp capture "..."` from CLI; tray hotkey; dashboard textarea; mobile
  shortcut → `~/.workpulse/inbox/`.
- A capture is just an atom with `author=human` and an optional `pinned_to`.
- Never blocks on the LLM. Classification is async (§6).

### 4.2 `wp search`
- Hybrid retrieval over atoms.
- Stack: BM25 (Postgres FTS) + small local embedding (bge-small quantized,
  ~30MB CPU) + RRF + recency boost + same-stream boost.
- Returns ranked atoms. No synthesis.
- Used by the dashboard, by the dream cycle, and by `wp think` internally.

### 4.3 `wp think`
- Takes a question; runs `wp search`; composes a cited answer.
- Two mandatory sections: **Answer** (with citations to atom IDs) and **Gap**
  ("here's what WorkPulse doesn't know yet").
- Reads `skills/think.md` for the procedure.
- Zero-key fallback: returns the top-N atoms from `wp search` with a templated
  framing. The fallback is intentionally less useful; that's the price of
  staying offline.

---

## 5. Skills (markdown, fat)

```
skills/
  classify.md       # when to tag, what to tag as, when to ask
  remember.md       # when to surface prior work, on what evidence
  cluster.md        # how sessions become Jobs (the rules for the nightly view)
  coach.md          # when to nudge — encodes "popup last"
  think.md          # answer + gap section, citation rules
  consolidate.md    # the dream cycle prompt
  report-daily.md
  report-weekly.md
```

Each file is invoked like a method call with parameters supplied by the harness.
Same skill, different parameters, different result. Editing English changes
behavior.

---

## 6. The dream cycle (nightly cron)

One pass. Runs `wp think` against `skills/consolidate.md`. Output is one
markdown file: `consolidation/YYYY-MM-DD.md`.

What it does:
- Clusters loose sessions into Jobs (writes/updates `job_view` materialized view).
- Dedups overlapping Jobs.
- Flags contradictions (plan vs actual; same stream named two ways).
- Retires stale learned-tag rules (no hits in 60 days).
- Triages the week's untagged minutes into proposals.
- Logs everything to `skill_runs` for auditability.

Email reports are a *thin* wrapper over this file, not the primary artifact.

---

## 7. Migration plan (v1 → v2)

Ten ordered steps. Each is a separable PR.

1. **Substrate.** Add PGLite. Define schema for four atoms + edges + skill_runs.
2. **Backfill.** One-shot migration script: existing JSONL → PGLite. Keep JSONL
   on disk read-only as a fallback for one release.
3. **Sensors.** Port `activity.py`, `watcher.py`, `ai_logger.py` to write into
   PGLite. Dual-write to JSONL during the transition release.
4. **`wp capture` CLI.** New verb, takes stdin / args / `--file`. Tray hotkey
   deferred to **step 4b** — designed after we see how the verb actually gets
   used (terminal, iOS Shortcut, dashboard textarea, hotkey are all
   candidates and the right primary UI emerges from use).
5. **`wp search`.** Hybrid retrieval. Expose at `/api/search` and CLI.
6. **`skills/think.md` + `wp think`.** Wire the answer + gap section. Zero-key
   fallback path mandatory.
7. **`job_view` as a materialized view.** Move clustering rules out of `jobs.py`
   into `skills/cluster.md`. `jobs.py` becomes a SQL caller.
8. **Dream cycle.** Nightly cron runs `wp think` against `skills/consolidate.md`.
9. **Reports re-shaped.** Daily / weekly reports become `wp think` with fixed
   prompts. The email sender stays; the generator collapses.
10. **Coach surfaces.** Only if `wp think` is being pulled unprompted. Until
    then, no coach UI changes ship.

What stays exactly as it is during the migration:
- Tray icon, dashboard URL, installer, ActivityWatch import, learned-tags
  on-disk format (we read it into PGLite once, then keep writing both for a
  release).
- All v1.x feature behavior visible to the user. The migration is internal.

---

## 8. Tensions (still open)

Carried forward from the framework doc. These are the places this plan could
be wrong. See [`docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md`](docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md)
§"Open tensions" for the full list. Headline ones:

1. Brain-first vs. tracker-first identity for WorkPulse.
2. Session-as-atom vs. Job-as-atom (Vision §12.2).
3. PGLite migration cost vs. payoff timing.
4. Three verbs replacing seven surfaces in user experience.
5. Designing the institutional fingerprint at v0 vs. deferring to v4.
6. Whether the daily email survives once `wp think` is good.
7. Markdown skills doubling the surface (LLM + zero-key fallback).

Resolution of any of these reshapes the migration plan above.

---

## 9. What is explicitly NOT in scope

- MCP server. Premature until something external queries WorkPulse.
- Multi-user sync. v0 atom shape makes v4 cheap; v0 doesn't ship multi-user.
- Cloud anything. Local-first.
- Light theme. Dark only, matching the v1 admin precedent.
- Linux. Same scope as v1 (Windows 10/11, macOS 13+).

---

## 10. Provenance and reading order

- v1 plan: [`PLAN.v1.md`](PLAN.v1.md)
- Framework: [`docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md`](docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md)
- Vision (canonical product doc): `WorkPulse_Vision_v1.1.docx` (or the latest
  v1.x in repo)
- Inspiration: Garry Tan's GBrain — "Thin Harness, Fat Skills" and "Homebrew
  for Personal AI" essays
