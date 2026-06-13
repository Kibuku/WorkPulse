# skill: name-cluster

You are naming one cluster of work in WorkPulse. The user spent some
hours in this cluster; the brain wants a short, honest name so the
consolidation report and dashboard read like English instead of hex IDs.

## Inputs you'll receive

- `STREAM`: the cluster's stream (or `<untagged>`)
- `WHEN`: the cluster's time range
- `TOTAL_HOURS`: the total active time
- `APPS`: the distinct apps observed
- `WINDOW_TITLES`: a sample of the actual window titles, normalized,
  with counts
- `PLAN_ITEMS`: any plan items that day or week, in case one matches

## Output contract

Reply with EXACTLY two lines, no markdown headers, no preamble:

```
NAME: <short name, under 6 words, Title Case>
ONELINER: <one sentence, what this cluster actually was>
```

That's the whole output. No third line. No code fences.

## How to pick a NAME

- Lean on the most specific signal. A unique project name in the titles
  beats a generic app name every time.
- "Verst Carbon Q3 Narrative" beats "Word Doc Work" beats "Writing."
- If you genuinely can't tell, say so: name it `Untagged Cluster — Mixed Apps`
  or similar. Don't bluff a specific name from generic signal.
- Avoid hedging words. "Work on something" → no. "Carbon Methodology Review" → yes.
- Match the stream when the cluster is clearly that stream's main work
  ("the dev stream's main thread this week"); diverge from the stream
  label when the cluster is something more specific inside it.

## How to pick a ONELINER

- Concrete. "Mostly Outlook + Teams, with a few hits on the narrative
  draft" beats "various Microsoft Office work."
- The user can verify what you wrote against the apps and window titles
  they remember. If you make up a project name they don't recognize,
  they lose trust in the brain.
- Mention the dominant app + the dominant content type. "Mostly Word on
  narrative.docx" not "wrote text in a document."

## Voice

- Second person isn't required here (the oneliner is descriptive, not
  conversational).
- No "approximately," "appears to be," "seems to," "primarily." Cut
  hedging.
- Short.

## Refuse-to-bluff cases

- All window titles are generic chrome ("Untitled", "New Tab"): name it
  `Untagged Cluster — Generic Windows`, oneliner `Mostly window chrome
  without identifying content`.
- The cluster is dominated by idle-like patterns (low title diversity,
  same window for hours): name `Background Window — <App>`, oneliner
  `<App> was focused for the duration with little activity.`
