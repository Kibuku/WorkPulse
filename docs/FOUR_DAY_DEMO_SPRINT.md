# WorkPulse Four-Day Demo Sprint

Dates: 23–26 July 2026  
Goal: Produce a polished, trustworthy WorkPulse showcase in which every
presented capability is either working, explicitly identified as a local demo,
or clearly labelled as future vision.

## Product direction

The visual quality reference is Rize: calm dark navigation, a dominant daily
timeline, clear summaries, colour-coded work blocks, and low-friction
correction. WorkPulse must not reproduce Rize's brand, information taxonomy,
productivity scores, or exact layouts.

WorkPulse's own interface principle is:

> Outputs first. Evidence second. Private by default. Useful by permission.

## Demo navigation

1. **Today** — meaningful outputs, priorities, next action and confidence.
2. **Timeline** — calendar, work blocks, meetings and app-switch evidence.
3. **Brain** — ask, search, reusable methods, skills and remembered context.
4. **Organisation** — output briefs, approved updates, AI-use aggregates and
   SOP progress.
5. **Privacy** — local data, permissions, retention, access history and
   personal lock.

## Day 1 — Product contract and information architecture

- Establish the new application shell and design tokens.
- Replace the long card stack with the five product views.
- Make Today the default view.
- Surface tracker health without allowing it to dominate the product.
- Create explicit capability labels: `Live`, `Local demo`, and `Vision`.
- Preserve all existing endpoints and data displays during restructuring.

Acceptance:

- The first viewport communicates what WorkPulse understood about the day.
- Navigation reaches all five product layers.
- Raw application activity is not the homepage headline.
- Empty states explain what signal is missing and what will happen next.

## Day 2 — Personal intelligence

- Build the output-first Today view.
- Build the vertical Timeline view with work blocks and supporting evidence.
- Place calendar and tracked activity in the same chronology.
- Add confidence, correction and attribution interactions.
- Integrate Ask WorkPulse and the personal profile into Brain.
- Make the private lock and sensitive-category treatment coherent.

Acceptance:

- A user can identify what they worked on without reading raw window logs.
- A user can inspect why WorkPulse made an attribution.
- A user can correct an attribution.
- Ask WorkPulse returns evidence and a stated gap.

## Day 3 — Organisational and workflow layer

- Turn the existing organisation preview into its own view.
- Add local-demo output briefs, expected outcomes and manager feedback.
- Show exactly which fields would be shared and which are excluded.
- Add AI-use aggregate reporting without prompt content.
- Add one school-focused SOP engine and contextual nudges.
- Add the 24-hour meeting-retention policy and marker preview.

Acceptance:

- The organisation view never exposes raw titles, URLs, paths or private work.
- Inferred and user-confirmed statuses are visibly different.
- The SOP demonstrates stages, evidence and a next nudge.
- Meeting raw material and retained markers are visibly separated.

## Day 4 — Demo journey, reliability and package

- Add seeded school demonstration data that is clearly labelled demo data.
- Validate live-data and demo-data modes independently.
- Test the complete story on macOS.
- Test responsive states and empty/error states.
- Run the full automated test suite.
- Build the signed-state-appropriate installer artifact.
- Prepare a five-minute and a fifteen-minute demonstration path.

Acceptance:

- The product launches without a dead localhost page.
- Every navigation view loads without console errors.
- The demo still works when no AI backend is configured.
- Real personal data cannot appear in the organisational demo projection.
- The presentation claims match the product behaviour.

## Capability contract

| Capability | Sprint target | Demo label |
|---|---|---|
| Foreground application tracking | Working | Live |
| Browser, files and calendar signals | Working with visible health | Live |
| Output and project attribution | Working with confidence and corrections | Live |
| Daily timeline | Working | Live |
| Ask and search personal work | Working with deterministic fallback | Live |
| Personal profile and reusable methods | Working | Live |
| Private/sensitive personal area | Working | Live |
| Organisation share preview | Working and raw-data excluding | Live |
| Manager output briefs and feedback | Stored only on this device | Local demo |
| AI-use aggregate dashboard | Derived from safe metadata or seeded demo data | Local demo |
| SOP engine | One school workflow | Local demo |
| Meeting recording | Policy and marker simulation only | Local demo |
| 24-hour raw meeting deletion | Implement retention primitive and demo evidence | Local demo |
| Mobile PersonalPulse | Narrative only | Vision |
| Permissioned AI-to-AI mentorship | Narrative only | Vision |

## UX guardrails

- No employee productivity score.
- No leaderboard.
- No screenshots or keystroke language.
- No manager drill-down into raw personal activity.
- No more than one dominant question per view.
- Every inference carries confidence or an uncertainty state.
- Every organisational share surface lists excluded fields.
- Operational health is visible but visually quiet when healthy.
- Detailed tracker data remains available through progressive disclosure.

