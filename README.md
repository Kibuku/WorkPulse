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

## Develop

```bash
python -m venv .venv && source .venv/bin/activate
pip install -e ".[dev,mac]"    # drop 'mac' on Windows
pytest -q
```
