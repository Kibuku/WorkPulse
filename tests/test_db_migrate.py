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
    latest schema version — no UNIQUE-constraint race on schema_migration."""
    db_file = tmp_path / "race.db"
    monkeypatch.setattr(db, "db_path", lambda cfg=None: db_file)

    latest = max(v for v, _, _ in db._discover_migrations())

    def worker(_i: int) -> int:
        con = db.connect(cfg={"paths": {}})
        try:
            return db.current_version(con)
        finally:
            con.close()

    with cf.ThreadPoolExecutor(max_workers=8) as ex:
        versions = list(ex.map(worker, range(8)))

    assert versions == [latest] * 8


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
