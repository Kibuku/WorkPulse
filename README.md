# WorkPulse

A local-first personal attention brain. It passively tracks what you work on —
app focus, file changes, calendar, browser — then synthesizes it into
project-attributed intelligence: what you did, on which project, and what the
brain still doesn't know.

Everything runs on your machine. Nothing leaves it unless you choose to sync.

> **Status:** v2 in active development. macOS working; Windows parity in progress.

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
workpulse uninstall  # remove the background agents
```

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
