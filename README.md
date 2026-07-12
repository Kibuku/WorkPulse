# WorkPulse

A local-first personal attention brain. It passively tracks what you work on —
app focus, file changes, calendar, browser — then synthesizes it into
project-attributed intelligence: what you did, on which project, and what the
brain still doesn't know.

Everything runs on your machine. Nothing leaves it unless you choose to sync.

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

## Install (one command)

**macOS**
```bash
./setup.sh
```

**Windows**
```powershell
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

## Updating

New versions ship on GitHub. To move to the latest, from the WorkPulse folder:

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

If you don't have `git` set up, re-clone the repo and run the setup script again.

**Calendar (optional).** Put your published calendar's ICS URL in
`config/config.yaml` under `calendar.ics_url` — configuring it in the file (rather
than a shell env var) is what lets the scheduled `calendar-sync` agent find it.
Verify with: `python -m workpulse.signals.calendar_sync diagnose`.

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,mac]"    # drop 'mac' on Windows
pytest -q
```
