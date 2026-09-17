---
title: A frozen workpulse.db is usually a moved data root, not a dead collector
date: 2026-09-16
category: workflow-issues
module: data-store / doctor
problem_type: workflow_issue
component: observability
severity: medium
applies_when:
  - A WorkPulse DB or its logs appear frozen at a past timestamp while the collector processes are running
  - Diagnosing capture/freshness after any rename, rebrand, or install of a new app bundle
  - Any code or human reasons about "the database" by assuming a fixed Application Support path
tags: [data-root, rebrand, diagnosis, lsof, doctor, false-alarm]
---

# A frozen workpulse.db is usually a moved data root, not a dead collector

## Context
On 2026-09-16 a check of "is WorkPulse still capturing?" found every table in
`~/Library/Application Support/WorkPulse/workpulse.db` frozen at 2026-08-19
20:21 — four weeks stale — while `ps` showed the activity, watcher, and web
agents alive. The obvious read is "collectors died silently on Aug 19." That
read is wrong, and acting on it wastes a full investigation.

## Guidance
WorkPulse resolves its DB as `ROOT / paths.db`
([scripts/db.py:44](../../../scripts/db.py), [scripts/common.py:30](../../../scripts/common.py)),
so the *active* database lives wherever the running app's `ROOT` points — which
a rename or a new app bundle can move. After the "Pulse" rebrand the live data
root moved from `~/Library/Application Support/WorkPulse/` to
`~/Library/Application Support/Pulse/Personal/` (the shipping app is
"Personal WorkPulse.app"), and the old directory was left populated and
unmarked. A populated-but-frozen `workpulse.db` in an abandoned root is a
diagnosis landmine: it reads exactly like a dead collector.

**Never guess the data-root path. Ask the running process which file it holds:**

```bash
lsof -p "$(pgrep -f workpulse.signals.activity)" | grep workpulse.db
```

That one command points straight at the live DB
(`.../Pulse/Personal/workpulse.db`, current to now) and would have skipped the
entire false-alarm investigation.

## Why This Matters
The freeze timestamp is not a failure time — it is the *switchover* time. Every
signal (DB rows, `.jsonl` logs, `health.json`) in the old root stops at the
exact moment the app started writing to the new root, which makes the stale
copy look precisely like a clean-cut outage. Trusting the path instead of the
process turns a non-event into hours of tracing.

## When to Apply
- Any freshness/stall investigation, before concluding capture stopped.
- Immediately after any rebrand, rename, data-dir migration, or new-bundle
  install — expect a second, orphaned data root to exist.
- When writing tooling that reads "the" WorkPulse DB: resolve it from config /
  the live process, never a hardcoded `Application Support/<name>` path.

## Examples
Prevention shipped alongside this learning (committed locally on branch
`from-first-principles` as `4989957`, no PR yet):

- **`doctor.check_stray_data_roots()`** ([scripts/doctor.py](../../../scripts/doctor.py))
  now warns, naming any data root whose `workpulse.db` is older than the active
  one — so a stray root is self-detecting instead of a trap. Verified against
  the live filesystem: it flags `~/Library/Application Support/WorkPulse`.
- A **`MOVED.txt` tombstone** was placed in the old root pointing to the live
  one, carrying the `lsof` recipe above, so the next human or agent who opens
  that directory is told immediately.

## Related
- Fix commit: `4989957` on `from-first-principles` (scripts/doctor.py, tests/test_doctor.py).
