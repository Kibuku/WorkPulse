# WorkPulse Classroom Product Design Document

Version: 0.1
Status: Design baseline for build
Updated: 2026-07-28
Working product name: WorkPulse Classroom

## WorkPulse suite boundary

WorkPulse is the shared Signal → Atom → Meaning → Brain platform. Products
built on it are installable suites rather than mandatory screens in one
application:

- **WorkPulse Personal** — personal work memory and reflection;
- **WorkPulse Classroom Session Console** — policy preparation, scheduling,
  device enrolment and exception handling;
- **WorkPulse Classroom Managed Device Agent** — a silent classroom/lab agent
  that receives policies and reports device-attributed exceptions;
- **WorkPulse Organisation** — a separate opt-in organisational projection.

Suites share versioned engine code and policy protocols, but not raw data
stores. A university may deploy Classroom without enabling Personal WorkPulse.
A personal user may run Personal without installing any organisational or
classroom service. Packaging should therefore produce separate launch surfaces
and eventually separate installers while retaining one tested core.

## 1. Executive decision

WorkPulse Classroom is an opt-in child workspace within the WorkPulse
environment. Personal, Organisation, and Classroom use the same sensing,
memory, privacy, health, packaging, and update foundations while applying them
to different purposes and permission boundaries.

Classroom is not a separate application that asks lecturers to rebuild their
work inside another platform. It extends the WorkPulse they already use with
teaching context, AI-aware policy, student transparency, and time-bounded
institutional controls.

### Product promise

> A teacher can define the digital resources permitted for a lesson or exam,
> apply that policy to enrolled school devices for a fixed period, and receive
> proportionate alerts when a device leaves the permitted environment.

### Beachhead and entry wedge

The first target segment is African tertiary institutions, professional
training centres, and examination bodies operating managed Windows computer
labs. The first supported teaching environment is Windows with Chrome or Edge.

The market-entry proposition is AI-aware learning governance:

> WorkPulse helps African learning institutions govern AI and digital resources
> according to the purpose of each class or assessment, without collecting
> students' entire digital lives.

The system should help an institution move beyond a binary "block or allow
ChatGPT" decision. It can express that AI is permitted for exploration in a
particular class, must be attributed in an assignment, is prohibited during an
assessment, or may never receive sensitive institutional data.

AI-aware policy is the entry wedge, not the complete long-term moat. The deeper
product differentiation is local institutional memory, context-specific
preparation, transparent exceptions, offline-tolerant enforcement, minimal
data collection, and evidence-backed learning from approved corrections.

The first pilot is normal learning mode with adult learners on institution-owned
devices. Monitoring minors and live examination use are later stages requiring
separate validation and governance.

### First-principles boundary

The system may establish that a device attempted to open a resource outside an
active policy. It may not establish that a student cheated, intended harm, or
deserves discipline. Those are human judgments requiring context and due
process.

### Relationship to Personal WorkPulse

| Personal WorkPulse | WorkPulse Classroom |
|---|---|
| Owned by the individual | Governed by a disclosed school policy |
| No sign-in required by default | Institutional sign-in and device enrolment |
| Learns how a person works | Enforces a declared class or exam policy |
| Raw work signals stay local | Only policy events and approved summaries sync |
| AI can interpret personal patterns | Deterministic rules make enforcement decisions |
| Personal memory can be long-lived | Student event retention is short and configurable |

The child workspaces share sensor adapters, health checks, packaging, local
memory primitives, and visual language. Their permissions and organisational
projections remain explicitly separated. A Personal WorkPulse user must never
become institutionally visible simply because Classroom exists in the same
environment.

## 2. Problem

Schools increasingly depend on browsers, online resources, AI tools, cloud
documents, and managed computers. Teachers need to:

- allow useful digital resources without opening the entire internet;
- change permitted resources for a particular lesson or assessment;
- know when a device leaves the declared environment;
- apply the same policy consistently across a class;
- avoid reading unrelated personal content;
- understand whether the controls are actually active; and
- retain enough evidence to review an incident without building a permanent
  surveillance archive.

Existing activity trackers show applications and time. That is insufficient
for a school safeguard. WorkPulse Classroom must combine a policy engine,
browser enforcement, device health, transparent alerts, and minimal audit
records.

## 3. Goals and non-goals

### 3.1 MVP goals

1. A school administrator can create an institution and assign roles.
2. A teacher can create a class and enrol school-owned devices.
3. A teacher can create a timed learning or exam session.
4. A teacher can select permitted websites and applications.
5. Enrolled devices can receive and enforce the active policy.
6. Students can always see when a policy is active and why a resource is
   blocked.
7. Teachers receive useful, low-noise policy alerts.
8. The system records a minimal, tamper-evident audit trail.
9. The school controls retention within safe product limits.
10. The product works without an AI model. AI is optional assistance, never
    the enforcement authority.

### 3.2 Non-goals for the MVP

- general student productivity scoring;
- continuous screenshots, webcam recording, or keystroke capture;
- covert monitoring;
- reading messages, document bodies, prompts, or form responses;
- deciding whether misconduct occurred;
- biometric identification;
- personal-device monitoring outside an explicitly managed school profile;
- network-wide filtering for unmanaged devices;
- full mobile-device management replacement;
- Safari parity in the first enforcement pilot; and
- behavioural prediction or risk scoring.

## 4. Users and roles

### 4.1 Student

- Uses an enrolled school device during a declared session.
- Sees the active mode, end time, permitted-resource summary, and school policy.
- Receives a clear block page when a resource is not allowed.
- Can request access or flag a mistaken block.
- Can view the events associated with their own session when school policy
  permits.

### 4.2 Teacher

- Creates classes and session templates.
- Starts, pauses, extends, and ends a session.
- Chooses allowed resources from approved institutional options.
- Sees device health and policy alerts for the active class.
- Adds context and resolves alerts without making an automatic disciplinary
  finding.

### 4.3 Examination officer

- Creates approved exam templates.
- Uses stricter policies and change control.
- Requires a second approver for material policy changes where configured.
- Exports an exam integrity report containing policy and device events, not a
  declaration of cheating.

### 4.4 Safeguarding or data-protection officer

- Reviews data categories, retention, access, and audit history.
- Receives escalated data-safety events where policy requires.
- Cannot browse unrelated student activity.

### 4.5 Institution administrator

- Manages institution identity, roles, classes, devices, integrations, and
  baseline policies.
- Can disable a lost device and inspect agent health.
- Cannot silently enable undisclosed collection.

### 4.6 WorkPulse operator

- Receives product health telemetry and user-submitted feedback only where the
  institution has opted in.
- Does not receive student browsing records by default.
- Cannot enter an institution tenant without a logged, time-limited support
  grant.

## 5. Operating modes

### 5.1 Normal learning mode

The school baseline applies. The browser extension may enforce prohibited
categories or domains, while ordinary educational browsing remains available.
Only blocked attempts and agent health are reported.

### 5.2 Class mode

A teacher activates a time-bounded policy for a class. The policy can:

- allow all sites except a prohibited list;
- allow only selected domains and exact URLs;
- permit specified applications;
- permit named AI tools for declared uses;
- show warnings for restricted data destinations; and
- offer a student access-request path.

### 5.3 Exam mode

Exam mode is strict, explicit, and pre-approved. It can:

- use an allowlist-only browser policy;
- restrict unapproved applications;
- disable downloads, uploads, clipboard, or printing where the supported
  platform permits;
- report agent termination, extension disablement, policy loss, and clock
  anomalies;
- require a device readiness check before entry; and
- restore the school baseline automatically when the exam ends.

Exam mode must not be described as a secure exam environment until its browser,
operating-system, network, recovery, and tamper controls have been independently
validated.

## 6. Core user journeys

### 6.1 Institution onboarding

1. An authorised administrator creates the institution tenant.
2. The institution accepts the data-processing and acceptable-use terms.
3. The administrator chooses region, retention, roles, and baseline policy.
4. The system generates a short-lived device enrolment code or managed install
   package.
5. The administrator installs WorkPulse Classroom on school devices.
6. Each device reports readiness without uploading browsing history.

### 6.2 Teacher prepares a class

1. Teacher signs in.
2. Teacher selects a class and chooses **New session**.
3. Teacher selects **Learning** or **Exam**.
4. Teacher adds permitted domains, URLs, applications, and AI-tool rules.
5. The policy preview shows exactly what will be allowed, blocked, and logged.
6. Teacher saves a reusable template or starts the session.

### 6.3 Student joins

1. Student signs into or selects the assigned school device identity.
2. A persistent but unobtrusive indicator shows the active policy.
3. Student opens **What is active?** to see the policy owner, purpose, end time,
   permitted resources, logged event types, and request-access action.
4. The agent confirms enforcement health to the teacher console.

### 6.4 A blocked resource

1. The browser extension evaluates the URL locally against the signed policy.
2. If blocked, navigation is stopped before content is loaded where technically
   possible.
3. The student sees the applicable rule and permitted alternatives.
4. A minimal event is created: device pseudonym, policy rule, domain or
   privacy-preserving resource identifier, time, and action.
5. The alert is deduplicated and sent according to severity.
6. The teacher may allow once, allow for the session, dismiss, or add context.

### 6.5 Session ends

1. WorkPulse restores the institutional baseline automatically.
2. Teacher receives a concise session report.
3. Events enter the configured retention lifecycle.
4. The report distinguishes facts, unresolved events, and teacher annotations.

## 7. Policy model

Every policy is versioned and immutable after activation. A change creates a new
version with author, approver, reason, and activation time.

### 7.1 Policy fields

- institution, class, and session identifiers;
- mode and purpose;
- start, end, grace period, and timezone;
- device and user scope;
- allowed domains and exact URLs;
- blocked domains and categories;
- allowed and blocked applications;
- upload, download, clipboard, print, and external-device controls;
- AI-tool rules;
- alert thresholds and recipients;
- offline behaviour;
- retention class;
- author, approver, and version; and
- student-facing explanation.

### 7.2 Rule precedence

From highest to lowest:

1. emergency safety and legal hold;
2. active exam policy;
3. active class policy;
4. institution baseline;
5. unrestricted school-device default.

More specific rules override broader rules only when the policy explicitly
permits the override. Conflicts fail closed for exam mode and fail to the
institution baseline for normal learning mode.

### 7.3 AI-use rules

AI services may be:

- prohibited;
- allowed for brainstorming only;
- allowed for research with attribution;
- allowed only through an institution-approved provider;
- prohibited from receiving confidential or student-sensitive data; or
- unrestricted for the session.

The MVP identifies provider/domain and policy context. It does not capture
prompts or responses.

## 8. Alerts and incident handling

Alerts should communicate risk without creating alarm fatigue.

| Level | Examples | Default behaviour |
|---|---|---|
| Information | First blocked navigation, access request | Appears in session feed |
| Attention | Repeated blocked attempts, unapproved app foregrounded | Teacher notification, deduplicated |
| Urgent | Agent stopped, extension disabled, policy invalid, exam device offline | Persistent teacher alert |

### Required language

Use: **policy event**, **blocked attempt**, **device lost enforcement**, or
**requires review**.

Do not use without human review: **cheating**, **misconduct**, **malicious**,
**dishonest**, or **guilty**.

### Review workflow

Each event can be:

- unresolved;
- expected or accidental;
- access granted;
- technical issue;
- escalated for human review; or
- dismissed with reason.

The original event remains immutable; annotations are appended.

## 9. Privacy, safeguarding, and data minimisation

### 9.1 Data collected by default

- enrolled device identifier;
- agent and extension health;
- active policy and version;
- blocked or warned policy events;
- application-policy events during a declared session;
- teacher actions and annotations; and
- session start, end, and enforcement status.

### 9.2 Data not collected by default

- full browser history;
- page content;
- search terms;
- form contents;
- passwords;
- messages;
- document contents;
- screenshots;
- camera, microphone, or keystrokes;
- AI prompts and responses; and
- activity outside school policy scope.

### 9.3 Retention

Recommended defaults:

| Data | Default retention |
|---|---|
| Raw URL or application event | 24 hours |
| Deduplicated policy event | 30 days |
| Exam integrity event | 90 days, institution configurable |
| Aggregate session count | 1 year |
| Policy versions and access audit | 1 year |
| Product health telemetry | 30 days |

After raw deletion, WorkPulse may retain a minimal marker: policy ID, event
category, severity, resolution, and timestamp. It must not retain a reversible
copy of deleted content.

The institution must review these defaults against its jurisdiction, student
age groups, safeguarding obligations, examination rules, and records policy
before deployment.

### 9.4 Student transparency

The device must always provide:

- an active-policy indicator;
- a plain-language description of what is being enforced and logged;
- the session end time;
- the responsible institution;
- a way to request access or report an error; and
- access to the applicable privacy and acceptable-use notices.

## 10. System design

### 10.1 Components

```text
Teacher/Admin Console
        |
        | authenticated policy and event APIs
        v
Institution Control Plane
  - identity and roles
  - classes and devices
  - policy registry
  - alert routing
  - audit and reporting
        |
        | signed, versioned policy
        v
Managed Device Agent <----> Browser Extension
  - device health             - URL enforcement
  - app policy                - student block page
  - local event buffer        - access requests
  - policy verification       - extension health
        |
        | minimal policy events
        v
Institution Event Store
```

### 10.2 Control plane

The institutional service provides:

- tenant isolation;
- authentication and role-based access control;
- class, user, and device inventory;
- policy authoring, approval, signing, and distribution;
- alert delivery;
- immutable audit records;
- retention processing; and
- report generation.

### 10.3 Managed device agent

The agent:

- enrols the device using a one-time code;
- stores a device credential in the operating-system credential store;
- verifies signed policies;
- enforces supported application rules;
- checks extension health;
- buffers events during temporary network loss;
- restores the baseline when a timed policy expires; and
- reports health separately from student activity.

### 10.4 Browser extension

The first browser target is Chrome/Edge using a managed extension. It:

- receives policy from the local agent;
- evaluates navigation locally;
- displays the student block and explanation page;
- sends only policy events, not general history;
- supports teacher-granted temporary exceptions; and
- reports disablement or version mismatch.

Brave may be technically compatible with Chromium extension APIs but is not an
MVP support commitment. Safari requires a separately packaged and signed
extension and follows later.

### 10.5 Enforcement hierarchy

1. Browser extension for URL-level rules.
2. Operating-system controls for application/process restrictions.
3. School DNS, proxy, firewall, or MDM integration for defence in depth.

The browser extension is accurate for browser navigation but cannot by itself
control another browser, a VPN, a native application, a second device, or all
network protocols.

## 11. Authentication and permissions

Personal WorkPulse remains accountless by default. WorkPulse Classroom
requires sign-in because policies, classes, devices, alerts, and audit actions
must have accountable institutional identities.

### 11.1 MVP authentication

- institution administrator invitation;
- teacher and examination-officer accounts;
- passwordless email or institution single sign-on;
- short-lived sessions with multi-factor authentication for privileged roles;
- one-time device enrolment codes; and
- revocable per-device credentials.

### 11.2 Authorisation principles

- deny by default;
- tenant isolation;
- least privilege;
- class-scoped teacher access;
- no administrator access to content that was never collected;
- step-up authentication for exports, retention changes, and support access;
- append-only audit for policy, role, export, and event-review actions; and
- time-limited support access approved by an institution administrator.

## 12. Offline, failure, and recovery behaviour

### Learning mode

- Continue the last valid policy until expiry.
- If no valid policy remains, restore the institution baseline.
- Buffer minimal events and sync on reconnection.
- Show offline state to student and teacher.

### Exam mode

- Refuse entry unless a valid signed policy is present.
- If connectivity drops after entry, continue the cached policy.
- Raise an urgent event when connectivity or enforcement health is lost.
- Never silently relax the policy.
- Preserve local audit markers until acknowledged by the server.

Clock changes, agent termination, extension disablement, policy-signature
failure, and local database corruption are explicit health events.

## 13. Reports

### 13.1 Teacher session report

- class, teacher, policy version, and session duration;
- device readiness and attendance;
- number of devices continuously enforced;
- policy events grouped by rule and resolution;
- access requests and teacher decisions;
- technical failures; and
- teacher notes.

### 13.2 Examination integrity report

- approved exam policy and change history;
- device readiness evidence;
- enforcement interruptions;
- timestamped policy events;
- annotations and resolutions; and
- an explicit statement that the report records technical events and does not
  determine misconduct.

### 13.3 Institution safeguarding report

- aggregate policy-event trends;
- common mistaken blocks;
- recurring technical failures;
- use of approved versus prohibited AI-tool categories;
- policy exceptions;
- retention and deletion health; and
- access/export audit.

Reports must not rank students by activity volume or infer academic ability.

## 14. MVP data model

Core entities:

- `institution`
- `institution_user`
- `role_assignment`
- `classroom`
- `class_membership`
- `device`
- `device_enrolment`
- `session`
- `policy`
- `policy_version`
- `policy_rule`
- `device_policy_assignment`
- `policy_event`
- `event_annotation`
- `access_request`
- `alert_delivery`
- `audit_event`
- `retention_rule`

Every tenant-owned record includes `institution_id`. Every mutable workflow
record includes creation and update actors. Policy versions and audit events
are append-only.

## 15. MVP API surface

Provisional service endpoints:

```text
POST   /v1/auth/invitations
POST   /v1/devices/enrol
POST   /v1/devices/heartbeat
GET    /v1/devices/{id}/policy
POST   /v1/classes
POST   /v1/sessions
POST   /v1/sessions/{id}/start
POST   /v1/sessions/{id}/pause
POST   /v1/sessions/{id}/end
POST   /v1/policies
POST   /v1/policies/{id}/versions
POST   /v1/events/batch
POST   /v1/events/{id}/annotations
POST   /v1/access-requests
POST   /v1/access-requests/{id}/decision
GET    /v1/reports/sessions/{id}
GET    /v1/audit
```

Device APIs use device credentials and signed requests. Human APIs use
institutional identity and role checks.

## 16. Product surfaces

### Teacher console

- Today and upcoming sessions
- Create session
- Live class
- Alerts and access requests
- Classes and devices
- Session templates
- Reports

### Student device surface

- Active policy indicator
- What is allowed
- Why this was blocked
- Request access
- Technical help
- Privacy notice

### Administrator console

- Institution setup
- People and roles
- Devices and deployment
- Baseline policies
- AI and data-safety rules
- Retention
- Integrations
- Audit and exports
- System health

## 17. Delivery plan

### Phase 0: design and safety baseline

- Approve this PDD.
- Secure one design-partner institution and named policy/data-protection owner.
- Obtain one real lecturer, course, lesson, Windows laboratory, current
  AI/acceptable-use policy, and IT representative.
- Confirm school-owned Windows devices and Chrome/Edge as the first target.
- Complete threat model and data-protection checklist.
- Define the pilot acceptable-use and student transparency language.

Exit criterion: product boundaries, responsible roles, and pilot conditions are
approved. A production Phase 1 does not begin without the design partner. A
local technical proof of concept may be built beforehand to secure and shape
that partnership.

### Phase 1: vertical enforcement slice

- Create a separate Classroom code boundary.
- Implement policy schema and local signature verification.
- Build a Chrome/Edge extension proof of concept.
- Build a Windows agent bridge.
- Add allowlist/blocklist and student block page.
- Demonstrate a locally configured timed session on one device.

Exit criterion: a declared policy reliably blocks and allows test sites,
expires correctly, and leaves a local audit record.

### Phase 2: institutional control plane

- Add institution sign-in and tenant model.
- Add roles, classes, and device enrolment.
- Build policy authoring and distribution.
- Add device health and event ingestion.
- Add teacher live-session view and access requests.

Exit criterion: one teacher can manage a timed session across five test devices.

### Phase 3: school pilot readiness

- Add alert deduplication and severity.
- Add session and integrity reports.
- Add configurable retention and deletion jobs.
- Add policy transparency and audit views.
- Package managed Windows installer and extension deployment.
- Complete security review, recovery drills, and usability testing.

Exit criterion: the system is ready for a supervised, non-exam pilot.

### Phase 4: validated expansion

- Run a normal-learning pilot.
- Review mistaken blocks, alert noise, teacher workload, and student feedback.
- Add approved network/MDM integrations.
- Validate exam-mode threat model before any real assessment.
- Plan macOS and Safari support from evidence gathered in the pilot.

## 18. MVP acceptance criteria

- A teacher can create and start a policy in under three minutes.
- A policy reaches 95% of online enrolled devices within 10 seconds.
- A prohibited site is blocked before page content is shown in supported
  browsers.
- A permitted site remains usable.
- Policy expiry restores the baseline without teacher intervention.
- Students can identify what is active and why a site was blocked.
- Duplicate attempts do not produce more than one teacher alert within the
  configured suppression window.
- Agent or extension loss is visible within 60 seconds during an active session.
- No general browser history, prompt content, screenshots, or keystrokes appear
  in the server store.
- Retention deletion can be demonstrated and audited.
- A report labels events as technical facts requiring human interpretation.

## 19. Success measures

### Product

- session setup time;
- policy delivery success;
- enforcement uptime;
- false-block and false-allow rates;
- alert-to-action ratio;
- access-request response time;
- teacher-reported usefulness; and
- student understanding of active controls.

### Safety and trust

- percentage of sessions with visible policy disclosure;
- unauthorised access attempts to institutional data;
- number of raw-data fields collected;
- deletion-job success;
- policy-event appeal or correction rate;
- support access frequency; and
- survey response to “I understood what WorkPulse could see.”

### Explicit anti-metric

Do not use number of alerts, blocked attempts, time-on-task, or application
switches as a proxy for student quality, diligence, or academic ability.

## 20. Risks and mitigations

| Risk | Mitigation |
|---|---|
| Product is perceived as surveillance | Visible policies, minimal collection, short retention, student explanation, no covert mode |
| Browser-only controls are bypassed | Managed devices, OS controls, network defence in depth, honest product claims |
| Alert fatigue | Severity, deduplication, session context, teacher-tunable thresholds |
| Mistaken blocks disrupt learning | Preview, templates, access requests, temporary exceptions, post-session review |
| Teacher labels an event as cheating | Product language, training, human review workflow, no automated misconduct score |
| Policy unavailable during exam | Signed cache, readiness checks, explicit fail-closed behaviour |
| Device agent is disabled | Health heartbeat, tamper event, managed deployment |
| Sensitive student data is over-retained | Minimal schema, retention defaults, verifiable deletion, no content capture |
| Small-school reports identify individuals | Class-scoped permissions and minimum aggregation thresholds |
| AI introduces inconsistent decisions | Deterministic enforcement; AI limited to drafting and explanation |

## 21. Open decisions before implementation

These decisions should not block the Phase 1 local proof of concept, but they
must be resolved before an institutional pilot:

1. Pilot jurisdiction, age group, and school policy owner.
2. Institution-hosted versus WorkPulse-hosted control plane.
3. Identity provider and student identity model.
4. School-owned devices only, or managed bring-your-own-device profiles.
5. Exact default retention and legal-hold authority.
6. Whether teachers may create policies freely or only from approved templates.
7. Which app controls are required beyond Chrome/Edge.
8. Whether domain names are retained in exam events or converted to protected
   identifiers after review.
9. Who can export reports and how exports expire.
10. Minimum aggregation threshold for institution reports.

## 22. Build decision record

The following decisions are fixed for the first implementation unless this PDD
is revised:

- WorkPulse Classroom is a permissioned child workspace inside WorkPulse.
- Personal, Organisation, and Classroom share the WorkPulse foundation and
  product shell while maintaining purpose-specific data boundaries.
- The institutional layer is opt-in and requires sign-in.
- Personal WorkPulse remains individually owned and local-first.
- The first enforcement platform is Windows with Chrome/Edge.
- Enforcement is deterministic.
- AI may draft policies, explain rules, and summarise approved events; it may
  not decide access, cheating, discipline, or student risk.
- Browser history is not synchronised; policy events are.
- Monitoring is active only under a declared institution baseline or timed
  session and is always visible.
- Exam-mode claims wait for security validation.
- Student monitoring does not enter the Personal WorkPulse data store.
