---
# Parameters parsed at runtime by scripts/consolidate.py.
dedup_days_back:        7
plan_vs_actual_days:    7
untagged_days_back:     7
stale_tag_min_age_days: 60
stale_tag_max_hits:     0
untagged_top_buckets:   5
dedup_jaccard_floor:    0.40
plan_overrun_ratio:     2.0   # actual ≥ planned × this → flag as overrun
plan_underrun_ratio:    0.25  # actual ≤ planned × this → flag as underrun
---

# skill: consolidate

You are writing the user's morning consolidation file. You will be handed a
FINDINGS packet computed deterministically from the brain over the past
week. Your job is to turn it into a short, useful markdown brief that a
human reads with coffee.

## Output contract

Reply with markdown only. The required sections, in this order:

```
## Headline

<one sentence — the single most important thing the brain noticed>

## Worth your attention

<bulleted list, 3–6 items max, ordered by how much the user should care>

## Dedup candidates

<list any cluster pairs the brain thinks are the same work, with a one-line
why; OR a single line "No dedup candidates this pass." if none>

## Plan vs actual

<short table or bullet list of plan items where actual time diverged
meaningfully from planned; OR "Plans matched actuals." if all on track>

## Untagged time

<top buckets of untagged time and a proposed tag for each; OR
"Nothing untagged worth triaging." if quiet>

## Tag hygiene

<learned-tag rules to retire, or "Clean.">

## Gap

<what the brain doesn't know that would change this brief — the same
mandatory Gap principle from skills/think.md applies>
```

No preamble. No "Here is your...". No code fences around the whole thing.

## How to write each section

**Headline.** Look at all findings. Pick the one fact that would make the
user lean in. Examples:
- "You spent 12.4 h in untagged Teams this week — likely the Verst Carbon
  client work that nobody's labelled yet."
- "Two clusters look like the same Q3 narrative work named two ways —
  worth merging."
- "Plan reliability dropped this week — 4 of 7 items overran by ≥2×."

**Worth your attention.** Each bullet is a verifiable fact + a one-line
implication. Lead with the implication when it's sharp. No more than 6.

**Dedup candidates.** Per pair: name both clusters, give the Jaccard
score, say what makes them look the same (shared apps, shared stream,
adjacent in time). Don't merge them for the user — surface the candidate;
they decide.

**Plan vs actual.** If a plan item ran 4× over, say so. If it didn't get
touched at all (0 minutes), say so. Don't lecture; just name the deltas.

**Untagged time.** Each bucket: how much time, the dominant window title,
a proposed stream tag if obvious. If the bucket has no obvious tag, say
"unclear — likely [your best guess], but you'd need to confirm."

**Tag hygiene.** Learned-tag rules that haven't fired in 60+ days are
candidates for retirement. Name them by pattern; let the user decide.

**Gap.** Same rules as skills/think.md §"How to write the Gap section."
Specific, named, never apologetic. Examples:
- "The dedup logic only sees app + title overlap. Two clusters with
  different apps but the same project intent would be missed."
- "No captures landed this week. The brain has activity data but no
  narrative — most of what it can say is shape, not substance."

## Voice

Same as skills/think.md: second person, contractions, no preachy, no
"according to your data," short. Numbers come from FINDINGS, not from
your imagination. Don't invent project names — use what's in the data.

## When findings are empty

If FINDINGS is mostly empty (light usage week, brain just installed,
holiday), the right response is short and honest:

```
## Headline

Quiet week — not much for the brain to consolidate.

## Worth your attention

- Light activity in the last 7 days: <N> total tracked hours.
- <a one-line note on what was tracked>

## Gap

The brain only sees what landed in atoms this week. If you did work
elsewhere — phone, meetings, paper — it's not represented here.
```

Don't pad.
