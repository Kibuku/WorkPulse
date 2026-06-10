# WorkPulse — operating principles for Claude

Read this at the start of any WorkPulse session. It encodes the posture I
should hold when collaborating with George on WorkPulse, lifted from the
first-principles framework on branch `from-first-principles` and the design
doc at `docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md`.

The point of this file is to keep me from drifting back to "fit Garry's ideas
into WorkPulse's existing code." The frame is the opposite: WorkPulse is
becoming a brain that has a tracker as one source.

---

## The frame

- WorkPulse is **a personal attention brain**, not a tracker. The tracker is
  one ingestion source.
- Vision §12.2 says "Job is the unit of analysis." Storage-wise the atom is
  the **session**; Jobs are a materialized view. Do not collapse these.
- The institutional fingerprint (Vision v3/v4) is a v0 schema decision, not a
  later build. Design atoms to project safely upward without leaking raw
  titles or paths.

## The ten principles (load-bearing)

1. **Decide the atom.** Four atom types: `session`, `file_event`, `ai_call`,
   `capture`. Everything else is a view.
2. **SQLite + sqlite-vec is the substrate** (Python-native expression of the
   "PGLite shape": one file, no server, vector-capable). Migration 0001 lives
   at `migrations/0001_initial_atoms.sql`; helpers at `scripts/db.py` and
   `scripts/atoms.py`. Don't propose new features that fight this.
3. **Latent vs deterministic, drawn explicitly.** SUMs, totals, rule matching
   never go to the LLM. Synthesis, classification of novel input, judgment
   never go to SQL. Pay the LLM once on borderline cases, cache deterministic
   forever (the `learning.py` pattern).
4. **Three verbs: `capture`, `search`, `think`.** Every UI is a derivation.
   `wp think` always ships with a mandatory gap section.
5. **Markdown is code.** Judgment lives in `skills/*.md`. The Python harness
   does I/O, SQL, sensors, web — never policy.
6. **Self-wiring graph from day 1.** Typed edges extracted at write time, zero
   LLM. The graph is what makes queries cheap later.
7. **Capture before classify.** The tracker never blocks on the LLM. Untagged
   is a first-class state, not an error.
8. **Popup last.** The user pulls; the system does not push. Coach surfaces
   are gated behind "is the user already coming back to `wp think`?"
9. **Design the fingerprint at v0.** Two tables: institutional-safe `session`,
   private `session_local`. Never merge them.
10. **Dream cycle, not reports.** Nightly consolidation produces one markdown
    file. Email reports are a thin wrapper, not the primary artifact.

## Voice (when generating user-facing strings)

Lifted from GBrain DESIGN.md and applied to WorkPulse surfaces:
- Second person, contractions allowed.
- Grounded in concrete data the user can verify ("2 of 3 missed" beats
  "Brier 0.31").
- Never preachy. Never "we recommend." Never "according to your data."
- Short. Under 25 words for narrative; under one line for status.

Apply this to: report copy, coach copy, plan card copy, any LLM-generated
string the user sees. When LLM-generated, gate it through a quick rubric and
have a hand-written fallback template if the model misses.

## When in doubt

- "Should I add this surface?" → Can it be expressed as `wp search` or
  `wp think` plus UI? If yes, no new surface.
- "Should I put this in Python?" → Is it policy or judgment? If yes, it goes
  in `skills/*.md`. If it's I/O, SQL, or sensors, Python.
- "Should this call the LLM?" → Could a SQL query answer it exactly? If yes,
  SQL. If no, LLM — but cache the result deterministically when possible.
- "Should I add a popup or push notification?" → No. Not until the user is
  pulling `wp think` unprompted.

## Tensions still open

I should flag, not assume, the answer to any of these:

1. Brain-first vs. tracker-first identity.
2. Session-as-atom vs. Job-as-atom (Vision §12.2 disagreement).
3. PGLite migration timing.
4. Whether the daily email survives once `wp think` is good.
5. Whether markdown skills + zero-key fallback is worth the doubled surface.

These live in `docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md` §"Open tensions" and
`PLAN.md` §8. When George resolves one, update both files **and** this memory.

## Provenance

Distilled 2026-06-09 from a dialogue between George and Claude, after reading
Garry Tan's GBrain repo (https://github.com/garrytan/gbrain) and WorkPulse's
v1.7 code. The branch `from-first-principles` carries the framework; this file
carries the posture.
