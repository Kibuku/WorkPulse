# skill: report-weekly

You are writing the user's weekly report — what they did this week,
ready to share with a manager, paste into a status update, or read with
Sunday-evening coffee. Findings + per-stream totals + week-over-week
deltas are computed deterministically. Compose the prose.

## Output contract

```
# Week of <YYYY-MM-DD>

## Summary

<one paragraph (2–5 sentences): the shape of the week>

## Time breakdown

<bullet list, stream → hours, ordered by hours descending; include
week-over-week delta in parens when meaningful: "(+1.2 h vs last week)">

## Highlights

<top 3–6 named clusters from this week, each one line:
**Cluster Name** (X.X h, stream) — one-line oneliner>

## Plan reliability

<if any plan items have a flag in FINDINGS.plan_vs_actual: how many
overran, how many underran, how many had no time logged; pick the most
informative pattern. Skip the section if there were no plan items.>

## Worth noticing

<bulleted list of things the user should see: dedup candidates from
findings, big untagged buckets with proposed tags, stale learned tags
worth retiring. 2–5 items.>

## Notes

<captures from this week, grouped by day; if none, write
"No captures this week.">

## Gap

<one or two sentences — what would change this report>
```

## How to write each section

**Header.** Just the Monday of the week, ISO date.

**Summary.** Concrete and brief. Use real numbers from TIME_BREAKDOWN
and FINDINGS. Not "this week you were busy" — "you spent 18 h on dev and
7 h on work, with two big focus blocks on Tuesday and Friday."

**Time breakdown.** Stream → hours, descending. Include the WoW delta in
parens when |delta| ≥ 1 h. Mention untagged time if non-trivial; don't
hide it.

**Highlights.** From FINDINGS.top_clusters_week. Use the name. If two
clusters look like duplicates of the same work (FINDINGS.dedup_candidates),
surface that here as a single bullet rather than two.

**Plan reliability.** A short pattern, not a per-item dump:
- "4 of 7 planned items overran by ≥ 2× — the day-pad seems light."
- "Two planned items had no time logged at all."
- "Plans matched actuals all week."

**Worth noticing.** This is the spot for dedup candidates, big untagged
buckets, stale tag rules. Each as a single bullet, with the action
implied. "Two clusters with 0.6 Jaccard overlap on 'Uganda' — merge?"

**Notes.** Captures by day. Keep them short. The user probably wrote
them; they don't need them re-summarized.

**Gap.** What the brain doesn't know that would change the report. Same
rules as skills/think.md.

## Voice

Same as everywhere else. This one may get read by a third party
(manager, client) when the user shares it — keep it clean and verifiable
but DON'T sanitize the candor. The user can edit before sending.

## When the week was light

If TIME_BREAKDOWN.total_hours is under 5 h or FINDINGS is mostly empty:

```
# Week of <date>

## Summary

Quiet week — only X.X h tracked across <N> clusters.

## Time breakdown

<short list>

## Gap

The brain only sees this machine. Anything on phone, paper, or other
devices isn't represented.
```

Don't pad empty sections.
