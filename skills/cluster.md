---
# Parameters parsed at runtime by scripts/cluster.py.
# Edit these to change clustering behavior — markdown is code (principle #5).
max_gap_minutes:      30      # gap larger than this breaks the cluster
min_cluster_seconds:  60      # clusters shorter than this are dropped from job_view
respect_stream:       true    # sessions in different streams never cluster together
cluster_untagged:     true    # stream=NULL sessions still get clusters (principle #7)
ignore_idle:          false   # reserved for step 7b; idle marker isn't on the v2 atom yet
---

# skill: cluster

A Job in WorkPulse v2 is not a unit of storage. It is **a cluster of
sessions**, computed deterministically from `(stream, time-gap)` and
materialized into `job_view` by `scripts/cluster.py`. The session is the
atom (principle #1); the Job is a view (principle #1, second sentence).

This file holds the **parameters** for that view and the **rationale**
for the defaults. Edit the YAML frontmatter to change behavior.

## What we compute

For each stream (including the implicit "untagged" stream when
`cluster_untagged: true`):

1. Walk sessions in `started_at` order.
2. If the gap from the previous session's `ended_at` to the next
   session's `started_at` is greater than `max_gap_minutes`, start a new
   cluster.
3. Otherwise, extend the current cluster.
4. Assign the cluster's id deterministically: a content-hash of
   `(stream, first_session_id)`. Stable across reruns.
5. Drop clusters whose total active time is below
   `min_cluster_seconds` — these are mostly window-glances, not work.

The cluster_id lands on `session.cluster_id`. The `job_view` table
holds one row per cluster with totals, stream, time bounds, and the
distinct apps seen.

## Why these defaults

**30-minute gap.** Empirically: stepping away to make tea or read a
notification rarely takes more than 30 minutes. Anything longer and it's
genuinely a context shift. This number is the single most important
parameter; if your work pattern is bursty (many short sessions in
clumps), lower it to 10–15. If your work pattern is meeting-heavy with
long pauses between focus blocks, raise it to 45.

**Respect stream.** A session tagged `dev` and the next tagged `work`
are not the same Job, even if they're a minute apart. If you context-
switched streams, that's a different piece of work.

**Cluster untagged.** Untagged is a first-class state (principle #7).
The brain still needs to summarize "what were those 90 minutes of
untagged stuff?" — clustering surfaces the chunk; the consolidation
skill (step 8) names it.

**Drop sub-minute clusters.** A 12-second window-focus blip is not a
Job. It's noise from alt-tabbing to glance at Slack. Surfacing it as a
Job would clutter every view.

## Refresh semantics

`scripts/cluster.py refresh` is idempotent. It re-clusters from scratch
each time — clustering is cheap, and incremental updates are tricky to
get right (one new session can merge two existing clusters, can extend
one cluster's `ended_at`, or can start a new one entirely). Re-running
the whole thing every time keeps the algorithm simple and the result
provably correct.

Typical use:
- After `wp backfill`: run once to populate job_view.
- Nightly (step 8 dream cycle): run as part of consolidate.
- After any large new batch of sessions: run again.

## What this skill does NOT do

- **It does not name clusters.** Naming is judgment — that's the
  `skills/name-cluster.md` skill in step 7b, an LLM call that reads the
  cluster's window titles and proposes a Job name.
- **It does not break clusters on title shift.** Title-based splitting
  ("you stopped touching `narrative.docx` and started touching
  `model.xlsx` — different job") wants tokenization in SQL or a small
  LLM pass; deferred to step 7b.
- **It does not respect idle.** The v2 session atom doesn't carry an
  `idle` bool yet (it lives only in the v1 JSONL). When idle lands on
  the atom (likely as a `kind='idle'` derived edge), set
  `ignore_idle: true` and the algorithm will skip idle gaps when
  measuring continuity.
