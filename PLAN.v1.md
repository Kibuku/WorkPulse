# WorkPulse — Implementation Plan

Living document. Source of truth for what we're building, in what order, and why. Authoritative spec is `WorkPulse_PDD_v1.docx` (v1.0, April 2026). This plan captures everything in the PDD **plus** the decisions made after review, **plus** the Windows-11 adaptation.

---

## 1. Goal

A local-first personal productivity system that passively tracks laptop usage, file activity, and AI interactions, then synthesises them into **daily** and **weekly** intelligence reports with actionable recommendations. Runs entirely on the user's Windows 11 machine; no cloud storage.

---

## 2. Resolved decisions (from PDD §10 review)

| Question | Decision |
|---|---|
| File watcher scope | Track **all files** in vault (only standard ignores) |
| AI logger token counts | **Yes** — capture input/output tokens + derived cost |
| Daily micro-reports | **Yes** — in addition to weekly |
| Cross-stream comparison in reports | **Yes** — per-stream totals + WoW deltas |
| Report delivery | Markdown file **and** email |
| Target OS | **Windows 11** |

Confirmed:
- Email: Gmail SMTP (`smtp.gmail.com:587`), from/to `kibuku.eoi.agent@gmail.com`, auth via Google App Password (env var `WORKPULSE_SMTP_PASSWORD`)
- Daily report: **23:00 EAT**
- Weekly report: **Sunday 20:00 EAT** (per PDD)
- LLM: Anthropic Claude (`claude-sonnet-4-6`) via paid API — cost expected <$1/month

Still to confirm:
- [ ] Anthropic API key obtained from console.anthropic.com and set as `ANTHROPIC_API_KEY` env var
- [ ] Google App Password generated and set as `WORKPULSE_SMTP_PASSWORD` env var

Vault population:
- Real project folders are currently scattered across the machine. Phase 1 creates **empty vault subdirs**; symlinks/moves happen progressively as files are organized.

---

## 3. Windows adaptation (overrides PDD where it assumes Linux/macOS)

| PDD assumption | Windows replacement |
|---|---|
| `~/workpulse/` | `D:\WorkPulse\` |
| systemd user service | **Windows Task Scheduler** task "at logon", restart on failure |
| cron | **Windows Task Scheduler** triggers (daily + weekly) |
| `ln -s` symlinks | Native Windows symlinks (Developer Mode **ON**, confirmed) |
| Shell scripts (`.sh`) | PowerShell (`.ps1`) for setup; Python entry points for everything else |
| launchd | N/A |

**Python portability rule:** all code uses `pathlib.Path` and `Path.home()`, never hardcoded POSIX paths.

---

## 4. Directory layout (target state)

```
D:\WorkPulse\
├── vault\                          # symlinks to real project folders
│   ├── verst-carbon\   -> ...\Documents\VerstCarbon
│   ├── majicom\        -> ...\Documents\Majicom
│   ├── dissertation\   -> ...
│   ├── consulting\     -> ...
│   └── personal-dev\   -> ...
├── logs\
│   ├── file_events_YYYY-MM-DD.jsonl
│   └── ai_sessions.jsonl
├── reports\
│   ├── daily\YYYY-MM-DD.md
│   └── weekly\YYYY-WW.md
├── config\
│   ├── config.yaml                 # paths, ignores, SMTP, API key ref
│   ├── categories.yaml             # app → category mappings
│   ├── report_prompt_daily.md      # daily micro-report prompt template
│   └── report_prompt_weekly.md     # weekly prompt template
├── scripts\
│   ├── watcher.py                  # file watcher daemon
│   ├── ai_logger.py                # Anthropic SDK wrapper + CLI
│   ├── report.py                   # report generator (daily | weekly mode)
│   ├── export_aw.py                # ActivityWatch data exporter
│   └── install_tasks.ps1           # installs Task Scheduler entries
├── SKILL.md                        # Cowork skill file
├── PLAN.md                         # this file
└── README.md                       # user-facing overview (later)
```

---

## 5. Component specs (delta from PDD only)

### 5.1 File watcher (`scripts/watcher.py`)
- Library: `watchdog`
- Scope: **all files** under `vault\`
- Ignores: `.git\`, `__pycache__\`, `.DS_Store`, `~$*.docx`, `Thumbs.db`, `desktop.ini`
- Debounce: 500ms per-path
- Rotation: daily JSONL files
- Runtime: Windows Scheduled Task, trigger = "At log on of current user", "Restart task if fails" every 1 min

### 5.2 AI logger (`scripts/ai_logger.py`)
Schema additions over PDD:
- `input_tokens` (int)
- `output_tokens` (int)
- `model` (string, e.g. `claude-sonnet-4-6`)
- `estimated_cost_usd` (float, derived at log time from current model pricing)

Two entry points:
- `workpulse_ai.wrap(...)` — Python wrapper for programmatic Anthropic SDK calls
- `python scripts\ai_logger.py log` — interactive CLI for manual Claude.ai / Cowork sessions

### 5.3 Report engine (`scripts/report.py`)
One script, two modes:
- `python scripts\report.py --mode daily` → writes `reports\daily\YYYY-MM-DD.md`, uses short prompt
- `python scripts\report.py --mode weekly` → writes `reports\weekly\YYYY-WW.md`, uses full prompt with WoW comparison

Both modes:
1. Pull ActivityWatch events via localhost:5600 REST API for the window
2. Load file_events JSONL for the window
3. Load ai_sessions JSONL for the window
4. Tag every event by **stream** (which `vault\<stream>\` subtree it touched, or via app/window-title heuristics for ActivityWatch)
5. Compute per-stream totals + (weekly only) WoW deltas
6. Build prompt → call `claude-sonnet-4-6` via Anthropic SDK
7. Write markdown file
8. Send email (SMTP from `config.yaml`) with the markdown as body/attachment

Schedules:
- Daily: 23:00 EAT (TBC)
- Weekly: Sunday 20:00 EAT

### 5.4 Email delivery
New in `config.yaml`:
```yaml
email:
  enabled: true
  smtp_host: ...
  smtp_port: 587
  smtp_user: ...
  smtp_password_env: WORKPULSE_SMTP_PASSWORD   # read from env, never stored
  from_addr: ...
  to_addr: ...
  send_daily: true
  send_weekly: true
```

### 5.5 Cowork integration
Point Cowork at `D:\WorkPulse\`. Write `SKILL.md` in Phase 5.

---

## 6. Phased build

Each phase is independently useful. Mark progress here as we go.

- [ ] **Phase 1 — Foundation** (1–2h)
  - [ ] Create `D:\WorkPulse\` tree (logs, reports, config, scripts)
  - [ ] Create **empty** vault subdirs: verst-carbon, majicom, dissertation, consulting, personal-dev
  - [ ] Install ActivityWatch (Windows installer + browser extension)
  - [ ] Confirm `aw-server` reachable on localhost:5600
  - [ ] Write initial `config\config.yaml` skeleton (paths, ignores, SMTP, model)
  - [ ] Verify `ANTHROPIC_API_KEY` env var is set

- [x] **Phase 2 — File watcher** ✓ 2026-04-19
  - [x] `scripts\watcher.py` with debounce + ignore patterns
  - [x] `scripts\install_tasks.ps1` — registers watcher as logon task
  - [x] Smoke test: `vault\dissertation\smoke_test.txt` → appeared in `logs\file_events_2026-04-19.jsonl` with correct stream tag

- [x] **Phase 3 — AI logger** ✓ 2026-04-20
  - [x] `scripts\ai_logger.py` with SDK wrapper + manual CLI
  - [x] Token/cost capture (input/output tokens + estimated_cost_usd)
  - [x] Smoke test: manual session + SDK wrapper both logged to ai_sessions.jsonl
  - [x] AI Sessions panel added to dashboard (count, cost, tokens, per-stream breakdown)

- [ ] **Phase 4 — Report engine** (3–4h)
  - [ ] `scripts\export_aw.py` — ActivityWatch REST client
  - [ ] `scripts\scanner.py` — folder structure scanner (names/paths only, no content)
    - Walks configured scan_roots (Desktop, Documents, vault, D:\)
    - Captures: folder tree depth, file counts per folder, loose files, naming issues
      (Untitled/New Document, duplicates), stale files (>90 days untouched)
    - Output: JSON snapshot fed into the report prompt
  - [ ] `scripts\report.py` with `--mode daily|weekly`
  - [ ] Report includes **Workspace Health** section — Claude reasons over scanner snapshot:
    - Disorganised folders (too many loose files, no structure)
    - Files split across locations that belong together
    - Missing standard subfolders in project dirs
    - Naming hygiene issues
    - Consolidation suggestions
  - [ ] Stream tagging logic (vault-path based + app heuristics)
  - [ ] Prompt templates (daily + weekly) in `config\`
  - [ ] SMTP delivery
  - [ ] Register both schedules in Task Scheduler
  - [ ] Smoke test: run each mode manually against real data

- [ ] **Phase 5 — Cowork integration** (1h)
  - [ ] Write `SKILL.md`
  - [ ] Point Cowork at `D:\WorkPulse\`
  - [ ] Smoke test: ask Cowork about a recent file

- [ ] **Phase 6 — Iterate** (ongoing)
  - [ ] After 2–3 weeks of data, refine report prompts based on output quality
  - [ ] Review success metrics (PDD §9)

- [x] **Phase 7 — System tray app** ✓ (Windows: shipped pre-v1.6; macOS port: v1.6, June 2026)
  - [x] `scripts/tray.py` using `pystray` (Win) / `rumps` (mac) — menu-bar icon replaces invisible daemon
  - [x] Menu items: Open Dashboard, Quit
  - [x] macOS LaunchAgent (`~/Library/LaunchAgents/com.workpulse.tray.plist`) launches tray at login
    - **Gotcha (macOS 15+):** launchd's `StandardOutPath` / `StandardErrorPath` must live outside `~/Documents` (TCC-protected). Use `~/Library/Logs/WorkPulse/` instead. Without this, launchd exits 78 silently before Python starts. Fixed 2026-06-01.
    - **Permissions required on macOS:** Screen Recording (not Accessibility) for the framework Python binary at `/opt/homebrew/Cellar/python@3.13/<ver>/Frameworks/Python.framework/Versions/3.13/Resources/Python.app`. Without it, `kCGWindowName` returns empty and window titles fall back to app names.
    - **TCC / venv quirk:** `python -m venv --copies` produces a relocatable launcher but on macOS framework Python the launcher *still* re-execs into the framework binary, so TCC permissions must be granted to `Python.app`, not to `.venv/bin/python`. Granting the launcher path is a no-op.

- [x] **v1.1a — Jobs (Coach surface, per Vision §12.2)** ✓ (May 2026)
  - [x] `scripts/jobs.py` — JSONL event store, fold-to-state, rollup
  - [x] Manual Start/End Job from dashboard + tray
  - [x] AI-lift detector library (`scripts/lift.py`)
  - [x] Coach card: "Jobs in flight"
  - [x] Per-Job markdown export (v1.2a) — `GET /api/jobs/{id}/export`
  - [x] Auto-close idle Jobs (v1.2a) — `autoclose_stale_jobs` runs on every dashboard refresh
  - [x] LLM-inferred Job-name suggestions (v1.2b) — Loop B for Jobs

- [x] **v1.7 — Morning Plan (intent capture, per Vision §12.2 extension)** ✓ (June 2026)
  - [x] `scripts/plans.py` — markdown source-of-truth (`plans/YYYY-MM-DD.md`), parse / render / suggestions / job-materialisation / reconciliation
  - [x] Endpoints: `GET /api/plans/today`, `POST /api/plans/today`, `GET /api/plans/suggestions`
  - [x] Dashboard "Today's plan" card above Jobs in flight
  - [x] Modal: paused-job suggestions (carry over) + free-form textarea (new items, optional `(~N min)`)
  - [x] Each plan item materialises into a Job at submit time so sessions roll up
  - [x] Per-item reconciliation: `actual_minutes_today` / `actual_minutes_total` rendered next to `planned_minutes` with progress bar (red when over)
  - [x] Top-line summary: "N of M done · X min logged of Y min planned"
  - [x] Bug-fix landed: `plans.materialise_jobs` writes start events directly instead of calling `jobs.start_job`, so the one-active-per-stream rule does NOT cascade-end same-stream plan items. The morning plan is the explicit exception to one-per-stream.
  - [x] Untagged-time alert: prominent amber banner when today's untagged total ≥20 min; one-click Tag-as on the top 3 patterns; auto-hides once threshold drops
  - [x] Override behaviour confirmed: `learning.py:125` — newer verdict wins, so manual Tag-as always supersedes AI auto-classification for the same pattern. No code change needed for overrides.
  - [ ] **Open / follow-ups:**
    - [ ] End-of-day reconciliation fold into daily report (blocked on Phase 4 report engine)
    - [ ] Voice input — Apple Speech framework (on-device, per §9.2 local-first) feeds the same textarea
    - [ ] Trigger automation — currently click-driven; first-wake-of-day or fixed time TBD
    - [ ] Configurable untagged-alert threshold via `config.yaml` (currently hardcoded 20 min in dashboard JS)

- [ ] **Phase 8 — Browser extension + local API** (future)
  - [ ] `scripts\server.py` — lightweight FastAPI server on `localhost:5700`
    - `POST /event` — receive browser tab events (URL, title, duration, stream tag)
    - `GET /summary/today` — JSON summary for quick status checks
  - [ ] Chrome/Firefox extension (Manifest V3):
    - Track active tab URL + title + time-on-page (richer than ActivityWatch's extension)
    - Detect Claude.ai sessions → auto-POST to ai_logger endpoint (no more manual logging)
    - URL-pattern → stream mapping (configurable in `config.yaml`)
    - Posts events to `localhost:5700/event` every 30s or on tab switch
  - [ ] Add `browser_events_YYYY-MM-DD.jsonl` log format; include in report engine
  - [ ] Smoke test: browse across project URLs, confirm events appear in JSONL

- [ ] **Phase 9 — Android / mobile** (future, post-laptop stability)
  - [ ] Expose read-only `GET /summary` on local network (`0.0.0.0:5700`) when at home WiFi
  - [ ] Android browser bookmarks `http://192.168.x.x:5700/summary` for quick dashboard
  - [ ] Full Android app: separate project, depends on stable local API from Phase 8

---

## 7. Privacy rules (from PDD §8, preserved)

- Anthropic API key: environment variable only, never in config files
- SMTP password: environment variable only
- AI logger stores prompt **summaries**, not full prompts
- Vault permissions: owner-only ACL on `D:\WorkPulse\vault\`
- Report engine supports a redaction list for sensitive window-title patterns (e.g. banking URLs) — configured in `config.yaml`

---

## 8. Success metrics (PDD §9, to review after 4 weeks)

- Can identify top 3 time sinks each week without manual tracking
- Context-switching frequency decreasing
- Deep-focus time on dissertation + Verst Carbon increasing
- AI usage patterns clearly surface where Claude saves most time
- Weekly recommendations specific and actionable, not generic
