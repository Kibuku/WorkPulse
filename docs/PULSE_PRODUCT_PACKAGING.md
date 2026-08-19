# Pulse Product Packaging Decision

Status: Accepted build boundary
Updated: 2026-08-04

## Decision

Pulse Core is shared code. It is not sold or presented as an everything
dashboard. Each buyer installs or enters the product they selected.

## Product availability

### Personal WorkPulse — public

- Entry URL: `/personal`
- Release channel: `personal-workpulse`.
- Public installer cards for macOS and Windows.
- Runs locally without requiring an account or organisation connection.

### WorkPulse Institution

- Entry URL: `/institution`
- Owns institutional work signals, output attribution, workflow learning,
  purpose-bound team projections, and organizational reporting.
- Raw evidence remains separated from institutional projections.
- Installer label: **WorkPulse Institution**.
- Status: building/design-partner phase; no public installer link.

### LearningPulse

- Facilitator entry URL: `/learning`
- Learning-device entry URL: `/learning?role=device`
- Owns declared exercises, AI-use policy, enrolled devices, policy evidence,
  facilitator actions, and factual session reports.
- Uses the isolated learning vault and never reads the Personal vault.
- Installer labels: **LearningPulse Facilitator** and **LearningPulse Device**.
- Status: private development; no public installer link until explicit release
  approval is given.

The first packaged release may contain the same signed executable payload, but
the installer flavour writes a product configuration and creates only the
appropriate shortcut and startup behaviour. Separate branding must not be
implemented by copying the engine into separate repositories.

### Developer Lab (not sold)

- Entry URL: `/personal`
- Preserves George's private local Pulse as the semantic-capture and memory lab.
- Has no public installer card or commercial update channel.
- Existing Personal installations map here and are never silently converted
  into institution-connected installations.

## Installer configuration

Each installation records:

```yaml
product: institution | learning | developer
role: member | facilitator | device | lab
entry_path: /institution | /learning | /learning/device | /personal
```

The tray/menu-bar action opens only `entry_path`. A learning-device install
starts the authenticated device agent after enrolment; it does not start or
expose the facilitator console. A facilitator install may start the restricted
LAN gateway but keeps report and control endpoints on loopback.

## Release channels

Njiani currently offers one public download card:

1. Personal WorkPulse for macOS/Windows (`personal-workpulse`).

WorkPulse Institution may be described as building but has no download.
LearningPulse may be described or demonstrated privately but has no public
installer or update channel until the owner gives explicit release approval.
The Developer Lab is never listed publicly or bundled as an optional product.

## Security boundary

- Facilitator reports and control actions remain loopback-only.
- Learning devices communicate through the restricted gateway using a
  per-device bearer token.
- No raw screen is sent to the facilitator.
- Every support action is persisted and attributable to the active session.
- Future remote coordination requires institution authentication and a proper
  control plane; opening the local dashboard to the LAN is prohibited.

## Current implementation status

Implemented:

- independent Institution, Developer Lab, and Learning entry surfaces;
- separate facilitator and learning-device views;
- isolated learning vault;
- real enrolment and heartbeat attribution;
- declared exercise, learning goal, and AI-use rule;
- persisted facilitator support and acknowledgement;
- factual session reporting.
- product identity persisted at installation;
- storage isolation for Institution, Developer Lab, Facilitator, and Device installations;
- backend capability enforcement independent of browser URLs;
- product-specific startup destinations and native installer names;
- role-aware update channels with protection against cross-product updates;
- one shared Windows/macOS build feeding all three installer flavours;
- learning-evidence retention and durable deletion markers.

Still required before external packaging:

- signed Windows and notarised macOS packages;
- facilitator identity and institution sign-in for any non-local deployment;
- real browser-policy enforcement extension;
- deeper content semantics (optional document-body extraction/OCR) beyond the
  shipped local metadata-based stage and friction observations;
- retention execution and deletion receipts for learning evidence; and
- mixed-device load test in a real course.
