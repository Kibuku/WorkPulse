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

The entry model is also fixed: download and install first, with no account
required for the complete personal product. Sign-in belongs only to optional
remote capabilities such as encrypted sync, backup, or organisation enrolment.
See `docs/PRODUCT_DECISIONS_AND_ROADMAP.md`.

## Demo navigation

1. **Today** — meaningful outputs, priorities, next action and confidence.
2. **Timeline** — calendar, work blocks, meetings and app-switch evidence.
3. **Brain** — ask, search, reusable methods, skills and remembered context.
4. **Privacy** — local data, permissions, retention, access history and
   personal lock.
5. **Optional connections** — an explicitly opted-in organisation layer,
   separated from the complete personal product.

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
- Complete the consent-based product-feedback loop: prompt only after meaningful
  use, disclose the exact payload, and send only when the user presses Send.

Acceptance:

- A user can identify what they worked on without reading raw window logs.
- A user can inspect why WorkPulse made an attribution.
- A user can correct an attribution.
- Ask WorkPulse returns evidence and a stated gap.
- A user can send experience feedback, defer it, or opt out permanently without
  attaching work history, titles, URLs, paths, captures, or project names.

## Day 3 — Optional organisational and workflow layer

- Keep personal WorkPulse complete without an organisation connection.
- Demonstrate the personal Workflow Learner with a candidate method mined from
  George's real local proposal-production journeys:
  - anonymized output journeys and evidence counts;
  - one candidate proposal-production workflow;
  - local user confirmation or correction;
  - one workflow-aware nudge tested against a held-out journey.
- Present organisation as a disconnected, explicitly opted-in layer.
- Add local-demo output briefs, expected outcomes and manager feedback.
- Show exactly which fields would be shared and which are excluded.
- Add AI-use aggregate reporting without prompt content.
- Add one school-focused SOP engine and contextual nudges.
- Add the 24-hour meeting-retention policy and marker preview.

Acceptance:

- The organisation view never exposes raw titles, URLs, paths or private work.
- Workflow evidence states how many local journeys support each step, exposes
  uncertainty, and cannot be promoted to a personal method without confirmation.
- A candidate method can be inspected and confirmed locally.
- The contextual nudge names the missing evidence rather than scoring the user.
- Inferred and user-confirmed statuses are visibly different.
- The SOP demonstrates stages, evidence and a next nudge.
- Meeting raw material and retained markers are visibly separated.
- Disconnecting an organisation does not remove or damage personal memory.

## Day 4 — Demo journey, reliability and package

- Add seeded school demonstration data that is clearly labelled demo data.
- Validate live-data and demo-data modes independently.
- Validate feedback delivery to the developer receiver, local fallback when
  offline, 30-day cooldown, defer, and permanent opt-out.
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
| Workflow Learner | One evidence-backed proposal-production slice | Local preview |
| Private/sensitive personal area | Working | Live |
| Consent-based product feedback | Local-first, explicit Send, no work telemetry | Live |
| Organisation connection | Disconnected and opt-in by default | Local demo |
| Organisation share preview | Working and raw-data excluding | Local demo |
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
- Product feedback never silently attaches usage history or work telemetry.
- Feedback prompts never block work and always offer Not now and Don't ask again.
- Operational health is visible but visually quiet when healthy.
- Detailed tracker data remains available through progressive disclosure.
- Installation and personal use never require sign-in.
- Sign-in names the optional remote capability it enables.
- Signing out or disconnecting an organisation never deletes personal memory.
