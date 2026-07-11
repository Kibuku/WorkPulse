"""
Regression tests for db.migrate() concurrency.

The dashboard fires several /api/* requests at once on load; FastAPI runs the
sync endpoints on a threadpool, so multiple connections call connect()->migrate()
concurrently against the same fresh DB. Before the migrate lock, two threads
would read the same current_version, both apply the same migration, and the
second bookkeeping INSERT tripped:

    sqlite3.IntegrityError: UNIQUE constraint failed: schema_migration.version

Run:  python -m pytest tests/test_db_migrate.py
"""

from __future__ import annotations

import concurrent.futures as cf

from workpulse.core import db


def test_concurrent_connect_migrates_cleanly(tmp_path, monkeypatch):
    """Eight parallel first-time connects must all succeed and converge on the
    latest schema version — no UNIQUE-constraint race on schema_migration and
    no SQLITE_BUSY ``database is locked`` on the cold WAL/migration writer.

    Run several rounds on fresh DBs: a single 8-worker round only tripped the
    original races intermittently (~20% / ~4%), so one round is an unreliable
    guard. Multiple rounds make a regression fail dependably."""
    latest = max(v for v, _, _ in db._discover_migrations())

    for round_i in range(12):
        db_file = tmp_path / f"race_{round_i}.db"
        monkeypatch.setattr(db, "db_path", lambda cfg=None, _f=db_file: _f)

        def worker(_i: int) -> int:
            con = db.connect(cfg={"paths": {}})
            try:
                return db.current_version(con)
            finally:
                con.close()

        with cf.ThreadPoolExecutor(max_workers=8) as ex:
            versions = list(ex.map(worker, range(8)))

        assert versions == [latest] * 8, f"round {round_i}: {versions}"


def test_migrate_is_idempotent(tmp_path, monkeypatch):
    """A second migrate() on an up-to-date DB applies nothing."""
    db_file = tmp_path / "idem.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)

    con = db.connect(cfg={"paths": {}})
    try:
        assert db.migrate(con) == 0            # already fully migrated by connect()
        latest = max(v for v, _, _ in db._discover_migrations())
        assert db.current_version(con) == latest
    finally:
        con.close()
