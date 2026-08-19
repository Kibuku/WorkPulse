# WorkPulse Product Decisions and Roadmap

This is the product decision log for choices that must shape implementation.
It distinguishes the current contract from later capabilities so that future
work does not accidentally weaken privacy or make optional infrastructure a
requirement.

## ADR-001: Download first; sign-in only for optional network features

**Status:** Accepted  
**Decision date:** 23 July 2026

### Decision

Personal WorkPulse must work after downloading and installing it without an
account, email address, internet connection, or organisation membership.

The installation creates:

- a local SQLite data store;
- a local WorkPulse identity;
- local tracking permissions selected by the user;
- a complete personal dashboard, memory, reports, corrections, and feedback
  drafts.

A person's name and role may be entered for local personalisation. This is not
an online account and must not imply that data has been uploaded.

### When sign-in is allowed

Sign-in is introduced only at the boundary of a feature that requires a remote
identity:

- encrypted backup and recovery;
- synchronisation between the person's own devices;
- joining or leaving an organisation;
- sending an approved update to a manager;
- transferring preferences to a new computer;
- future mobile PersonalPulse;
- permissioned AI-to-AI interaction.

Product feedback does not require an account. The user reviews the disclosed
payload and explicitly presses Send.

### Organisation enrolment

Organisation membership is a separate opt-in connection, not the default state
of Personal WorkPulse:

1. The organisation sends an invitation.
2. The user authenticates for that invitation.
3. WorkPulse shows the requested fields, purpose, recipient, and retention.
4. The user reviews and approves the connection.
5. Only approved derived outputs are shared.
6. The user can disconnect without losing or damaging personal memory.

Raw titles, URLs, file paths, captures, private categories, and personal memory
remain local unless a later consent contract explicitly says otherwise.

### Product flow

```text
Download
  → Install
  → Choose local permissions
  → Personal WorkPulse works locally
      ├─ optional: send product feedback
      └─ optional sign-in
           ├─ encrypted backup or device sync
           └─ review invitation → approve organisation connection
```

### Implementation constraints

- Core tables cannot depend on a cloud user ID.
- Generate a local, opaque installation/actor ID.
- Authentication code must live behind a sync/organisation boundary.
- Offline use is a tested first-class state, not an error state.
- The installer must not present account creation as a prerequisite.
- “Sign in” must state the capability it enables, such as “Sign in to join
  your organisation,” rather than appearing as a generic product gate.
- Disconnect and sign-out must never delete local personal memory.

## Build order

### Now: individual product

1. Reliable macOS and Windows installation.
2. Reliable foreground, browser, file, and calendar signals.
3. Output-first Today and Timeline.
4. Explainable attribution, correction, and local learning.
5. Local Ollama support with deterministic fallback.
6. Local personal memory, reports, privacy controls, and retention.
7. Consent-based experience feedback.

### Next: release readiness

1. Signed/notarized macOS distribution and trusted Windows installer.
2. First-run permissions and health recovery.
3. Update mechanism that preserves the local database and settings.
4. Installer and upgrade testing on clean macOS and Windows machines.
5. Feedback review loop and beta issue triage.

### Later: optional account services

1. Define encrypted sync and recovery threat model.
2. Add optional account creation at the sync boundary.
3. Add organisation invitations and explicit consent contracts.
4. Add employee-controlled output sharing and revocation.
5. Add manager feedback without raw-activity access.
6. Add mobile PersonalPulse and permissioned AI-to-AI interactions.

## Decision test

Before adding a feature, ask:

1. Can Personal WorkPulse still function without it?
2. Does it require data to leave the device?
3. If yes, is the purpose, payload, recipient, and retention visible before
   consent?
4. Can the user revoke it without losing personal memory?
5. Is sign-in being used for identity at a genuine network boundary, or merely
   because it is conventional?

## ADR-002: First run teaches the trust model before the feature set

**Status:** Accepted  
**Decision date:** 23 July 2026

The first-run experience must teach this sequence:

1. Personal WorkPulse works locally without sign-in.
2. It observes active-app, idle, file-metadata and optional context signals.
3. Observation is evidence, not proof of the user's intent.
4. WorkPulse proposes projects and outputs and exposes uncertainty.
5. Corrections become teaching examples; routine manual capture is optional.
6. Personal memory remains local.
7. Organisation membership is disconnected by default and requires a separate,
   reviewable permission contract.

Personal WorkPulse also derives local semantic observations from those signals:
workflow stages, repeated rework and possible context-switching friction. These
remain hypotheses with confidence and provenance, never proof of intent. Raw
titles, paths and URLs do not enter the semantic observation table, and the
owner can confirm or dismiss each proposal.

The tour must never imply that WorkPulse records keystrokes, continuously
captures screenshots, understands intent from one app name, or grants an
organisation access merely because the software is installed.

The persistent “How it works” contract must disclose:

- which signals are local and automatic;
- which signals require optional permissions;
- what is not collected;
- that the deterministic product continues with less context when an optional
  permission is withheld;
- what an organisation could receive;
- that raw evidence, corrections, private categories and the personal Brain are
  excluded from organisation access.

## ADR-003: Methods are confirmed memory, not LLM stories

**Status:** Accepted  
**Decision date:** 23 July 2026

WorkPulse may propose a workflow only from one or more evidence-backed output
journeys. A proposed method remains a candidate until the user confirms or
corrects it. The model may interpret an ambiguous episode, but it may not
silently promote a generated sequence into “how this person works.”

The sprint demonstration uses George's real local proposal-production evidence,
but exposes only anonymized journeys, stage sequences, evidence counts,
confidence and unknowns. Client names, project names and filenames remain
private. The candidate proves learning only after George inspects, corrects and
confirms it; it must not be presented as a universal method.

Workflow-aware nudges must:

- name the expected step or evidence marker;
- state what was and was not observed;
- allow the user to correct the method or current-output interpretation;
- avoid productivity scores, compliance accusations, or employee comparison.
