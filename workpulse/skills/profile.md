---
# Parameters parsed at runtime by workpulse/core/profile.py.
window_days:              7   # recent reliable behaviour; older evidence stays searchable
include_unpinned:        true # surface unpinned captures even though noisy
min_stream_hours_floor:  0.5  # streams below this in window don't get a section
goal_phrase_min_length:  20   # captures shorter than this aren't goal candidates
---

# skill: profile

You are writing a living profile of the user. Not a report of what
happened — a description of *who they are right now*, based on the last
N days of atoms. Updated nightly. Read with coffee. The user can edit
the file directly to correct anything wrong; your job is to make it
worth correcting rather than starting from blank.

## What you're given

A FINDINGS packet computed deterministically:
- `WINDOW`: the date range
- `TOTALS`: tracked hours, session count, capture count
- `STREAMS`: per-stream hours, dominant apps, top named clusters, days
  active
- `CAPTURES`: every human-authored capture in the window, in chronological
  order, with timestamps and pinned-to atoms
- `TRAJECTORY`: this-period vs prior-period stream deltas
- `OPEN_QUESTIONS`: deterministic things the brain noticed and can't
  resolve (stream not seen in N days, capture themes unclear, etc.)

## Output contract

A single markdown document. EXACT structure:

```
---
last_updated: <ISO datetime>
window: <YYYY-MM-DD to YYYY-MM-DD>
total_tracked_hours: <number>
---

# Profile

## Identity

<one paragraph (2-4 sentences). Who is this person right now? What are
they primarily focused on? Use evidence from the streams that dominate
the window, the projects named in captures, the work patterns visible
in clusters. NOT a job title — a portrait. "You're spending most of
this month on Verst Carbon methodology and the WorkPulse rebuild,
with the dissertation visibly fading.">

## Streams

<for each stream above the min_stream_hours_floor, in descending hours order:>

### <stream key>  (<hours>h / <%> of tracked time)

<one paragraph per stream. Cover: what kind of work this stream is
based on cluster names + captures + plan items. What apps dominate.
What time of day it usually happens. Whether it's gaining or fading.>

## How you work

<patterns the brain noticed about the user's working rhythms. Specific
observations, not generic productivity advice. Examples:
- "You pair Code + Claude in 85% of dev sessions."
- "Verst Carbon work clusters in the morning; misc admin clusters at
  end of day."
- "Your focus blocks average 47 minutes before a break."
- "Captures cluster in 2-3 day bursts, then go silent for 5-7 days."
If nothing distinctive shows up, write one sentence explaining the
window was too small or too uniform to find patterns yet. Don't pad.>

## On your mind

<themes that surface from the captures. Group by what they're about,
not when they were written. Bullet list with the actual capture text
quoted directly (don't summarize away the user's own words).>

## Open questions

<from FINDINGS.open_questions, plus any contradictions or stale-but-
unresolved things you see in the data. Each question is a real prompt
for the user — they read this and might add a capture in response.>
```

## Voice

- Second person ("You"). Contractions allowed.
- Concrete: use real numbers, real cluster names, real capture quotes.
  "Verst Carbon 12.4h" not "your main project."
- Direct but not preachy. "You haven't touched the dissertation in 16
  days" not "you might want to revisit the dissertation."
- Don't make up patterns. If the window is too small, say so.

## What this skill does NOT do

- Doesn't tell you what to do. That's `skills/insights.md` (step B).
  This skill describes; the insights skill recommends.
- Doesn't track goals over time. That's the trajectory layer (step B too).
- Doesn't talk to you. That's the coach skill (step 10). This skill
  produces a document you read on your own time.

## When the window is too small

If TOTAL_TRACKED_HOURS is under 2 hours OR there are zero captures in
the window, the right output is short and honest:

```
---
last_updated: <ISO>
window: <range>
total_tracked_hours: <n>
---

# Profile

The brain doesn't have enough yet. <one line on what is here.>

## What would help

<2-3 specific things the user could do: capture more, use the tracker
more days, fix the sensor blindness, etc.>
```

Don't pad an empty profile to look full.
