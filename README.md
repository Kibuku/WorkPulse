# WorkPulse

A local-first personal attention brain. It passively tracks what you work on —
app focus, file changes, calendar, browser — then synthesizes it into
project-attributed intelligence: what you did, on which project, and what the
brain still doesn't know.

Everything runs on your machine. Nothing leaves it unless you choose to sync.

Personal WorkPulse is **download-first and account-free**. Installation creates
a local profile and data store; sign-in is reserved for optional capabilities
that genuinely need a remote identity, such as encrypted device sync, backup,
or joining an organisation. See
[`docs/PRODUCT_DECISIONS_AND_ROADMAP.md`](docs/PRODUCT_DECISIONS_AND_ROADMAP.md).

The dashboard explains every tracking mechanism in plain language. It also
includes an employee-controlled organization-update preview that excludes raw
activity, plus consent-based product feedback. In v2.0.3, organization sharing
is preview-and-copy only: no manager is silently connected to local evidence.
Feedback is sent only after the user presses Send, through Njiani's HTTPS
receiver; failed deliveries remain saved locally rather than being discarded.

> **This is WorkPulse v2 — the current version.** macOS is fully working; Windows
> is in validation. Install it with the one command below and keep it current with
> `workpulse update`.
>
> Version 1 is preserved on the **`v1`** branch (tag **`v1.0.0`**) but is no longer
> the default — new installs and updates track v2.

## Layout

```
workpulse/
  core/       the brain — pure Python, cross-platform (db, atoms, the three
              verbs capture/search/think, clustering, categorizer, reports,
              profile, projects, privacy)
  signals/    data sources (activity, watcher, browser, calendar) + OS backends
  ops/        lifecycle (scheduler, nightly rollups, dream-refresh, doctor, tray)
  web/        the dashboard
  migrations/ SQLite schema
  skills/     markdown procedures the LLM follows
```

## Getting access

WorkPulse is a **private** repository — you can't clone it without being granted
access first. To add a pilot user:

1. **Owner:** on GitHub, go to the repo **Settings → Collaborators and teams →
   Add people**, enter the person's GitHub username or email, and set their role
   to **Read**. Read access lets them clone and pull updates but not push changes.
2. **New user:** accept the emailed invitation (or open
   `github.com/Kibuku/WorkPulse/invitations`).
3. **New user:** the first `git clone`/`pull` will ask you to sign in to GitHub.
   Because the account uses 2FA, authenticate with a **Personal Access Token**
   (create one at `github.com/settings/tokens`, scoped to this repo) rather than a
   password. macOS Keychain caches it, so `workpulse update` works silently after.

> Prefer not to hand out GitHub access? See **Install from a shared package**
> below — it lets a pilot user install and update from a folder you send them,
> with no GitHub account required.

## First-time install (one command)

For non-technical users, use the native installer from the Njiani download
page: `WorkPulseSetup-<version>-Windows.exe` on Windows or
`WorkPulse-<version>-macOS.pkg` on macOS. Each carries its own Python runtime;
the user does not install Python or run a terminal command. The source-based
steps below remain available for developers.

This is the entry point for a machine that does **not** have WorkPulse yet. Clone
the repo, then run the setup script from inside the folder:

**macOS**
```bash
git clone https://github.com/Kibuku/WorkPulse.git
cd WorkPulse
./setup.sh
```

**Windows**
```powershell
git clone https://github.com/Kibuku/WorkPulse.git
cd WorkPulse
powershell -ExecutionPolicy Bypass -File .\setup.ps1
```

Either script creates a virtualenv, installs WorkPulse, seeds `config/config.yaml`
from the template, applies the database schema, and installs the background agents
(launchd on macOS, Task Scheduler on Windows) so the sensors and the recurring jobs
— nightly rollup, dream-refresh, calendar-sync, doctor — run on a schedule.

Then open the dashboard and set up your streams:
```bash
workpulse web        # → http://127.0.0.1:5700  (the setup wizard opens on first run)
```

Other commands:
```bash
workpulse status     # background agents + a health summary
workpulse doctor     # run the health checks
workpulse version    # the installed version
workpulse uninstall  # remove the background agents
```

## Install from a shared package (no GitHub account needed)

For pilot users you'd rather not add as GitHub collaborators, ship a **package**
instead — a self-contained folder you send them (AirDrop, Drive, email, a zip).

**You (maintainer), build the package** from a git checkout:
```bash
./installer/build-package.sh          # → dist/WorkPulse-v<version>.zip
```
The zip contains only tracked files (no database, no `config.yaml`, no vault —
user data is gitignored and excluded by construction), plus the installers.

**The user** unzips it and runs the installer for their OS:

- **macOS:** double-click `install.command` (or `bash install.command`)
- **Windows:** right-click `install.ps1` → *Run with PowerShell*

It installs to `~/WorkPulse` (override with `WORKPULSE_HOME`), sets up the venv,
dependencies, config, database, and background agents — the same setup the git
install performs.

**To update a package install,** send a newer package and have them run the
installer again. It targets the same `~/WorkPulse` and applies the new version
in place:

- **New and changed files** are copied in.
- **Files removed in the new version (archived)** are deleted from the install,
  so nothing stale lingers.
- **Your data and settings are preserved** — `config.yaml`, the database, `vault/`,
  `logs/`, reports and the venv are shielded from the sync, then the standard
  config-merge and forward-only migrations run over them.

## Updating a git checkout

If you cloned from GitHub, move to the latest from the WorkPulse folder:

```bash
workpulse update
```

This pulls the latest code, syncs dependencies, and applies it in place — safely,
from any older version:

- **Your settings are preserved.** `config.yaml` is gitignored (so a pull never
  conflicts with it), and `update` *merges* any new settings a release adds into
  your file without touching your existing values.
- **Your data migrates automatically.** Database schema changes are forward-only
  and applied on the next run.
- **Agents refresh** to pick up any new or changed background jobs.

If you installed from a shared package rather than a clone, `workpulse update`
has nothing to pull — update by running the installer from a newer package (see
above).

**Calendar (optional).** Put your published calendar's ICS URL in
`config/config.yaml` under `calendar.ics_url` — configuring it in the file (rather
than a shell env var) is what lets the scheduled `calendar-sync` agent find it.
Verify with: `python -m workpulse.signals.calendar_sync diagnose`.

## Affordable AI options

WorkPulse still records and reports without an AI backend. For AI-assisted
classification and synthesis, start local and upgrade only when the evidence
shows a need:

1. Install [Ollama](https://ollama.com/download) and run
   `ollama pull llama3.2:3b`. WorkPulse detects it automatically when no
   Anthropic key is configured. The model download is about 2 GB and inference
   stays on the machine.
2. If local output is too slow or not accurate enough, add an Anthropic API key.
   The hosted default is Claude Haiku 4.5, the lower-cost Claude tier. Change
   `llm.model` to a Sonnet model later for selected high-reasoning workflows.

Set `llm.backend` to `ollama`, `anthropic`, `auto`, or `none` in
`config/config.yaml`. `auto` prefers Anthropic when a key exists, then Ollama.

## Data retention

`retention.raw_days` defaults to 100. The nightly cycle synthesizes memory
first, then deletes dated raw sensor logs and old file events. It strips old raw
window titles, URLs, calendar details, and large AI payloads while preserving
captures, corrections, reports, consolidations, learned rules, project links,
and lightweight historical markers. Preview the impact without deleting:

```bash
python -m workpulse.core.cleanup retention --dry-run
```

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,mac]"    # drop 'mac' on Windows
pytest -q
```
