"""
db.py — WorkPulse v2 substrate.

A single SQLite file with sqlite-vec available for vector search (loaded
lazily, only when `wp search` needs it). One DB file per installation.

Why SQLite + sqlite-vec instead of PGLite:
    The framework principle was "embedded, single-file, zero-server,
    vector-capable" — PGLite is the JS-native expression of that. WorkPulse
    is Python; the Python-native expression of the same shape is SQLite +
    sqlite-vec. Same properties, same one-file-on-disk story, no Node
    sidecar. The atom schema and every higher-level principle are unchanged.

Public API:
    db_path(cfg=None)              -> Path of the SQLite file
    connect(cfg=None, *, vec=False) -> sqlite3.Connection (migrations applied)
    migrate(con)                    -> int (count of migrations applied)
    current_version(con)            -> int

CLI:
    python -m scripts.db init       # create DB, apply all migrations
    python -m scripts.db status     # show current schema version + counts
    python -m scripts.db migrate    # apply any pending migrations
"""

from __future__ import annotations

import re
import sqlite3
import sys
from datetime import datetime, timezone
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from scripts.common import ROOT, ensure_dir, load_config, resolve


_MIGRATIONS_DIR = ROOT / "migrations"
_MIGRATION_RE = re.compile(r"^(\d{4})_([a-z0-9_]+)\.sql$")


# ── paths ────────────────────────────────────────────────────────────────────

def db_path(cfg: dict | None = None) -> Path:
    """Where the SQLite file lives.

    Config key: ``paths.db`` (relative or absolute). Defaults to
    ``workpulse.db`` at the project root if unset.
    """
    cfg = cfg or load_config()
    rel = cfg.get("paths", {}).get("db", "workpulse.db")
    p = resolve(rel)
    ensure_dir(p.parent)
    return p


# ── connection ───────────────────────────────────────────────────────────────

def connect(cfg: dict | None = None, *, vec: bool = False) -> sqlite3.Connection:
    """Open the DB, apply any pending migrations, return the connection.

    If ``vec`` is True, attempt to load the sqlite-vec extension. We don't
    fail hard if it's unavailable — `wp search` will tell the user what's
    missing in a clearer way than a stack trace here.
    """
    p = db_path(cfg)
    con = sqlite3.connect(p, isolation_level=None)  # autocommit; we manage txns explicitly
    con.row_factory = sqlite3.Row
    con.execute("PRAGMA foreign_keys = ON")
    con.execute("PRAGMA journal_mode = WAL")
    if vec:
        _try_load_vec(con)
    migrate(con)
    return con


def _try_load_vec(con: sqlite3.Connection) -> bool:
    try:
        con.enable_load_extension(True)
        import sqlite_vec  # type: ignore
        sqlite_vec.load(con)
        return True
    except Exception:
        return False
    finally:
        try:
            con.enable_load_extension(False)
        except Exception:
            pass


# ── migrations ───────────────────────────────────────────────────────────────

def _ensure_migrations_table(con: sqlite3.Connection) -> None:
    con.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_migration (
          version    INTEGER PRIMARY KEY,
          applied_at TEXT NOT NULL,
          name       TEXT NOT NULL
        )
        """
    )


def current_version(con: sqlite3.Connection) -> int:
    _ensure_migrations_table(con)
    row = con.execute("SELECT COALESCE(MAX(version), 0) AS v FROM schema_migration").fetchone()
    return int(row["v"])


def _discover_migrations() -> list[tuple[int, str, Path]]:
    """Return list of (version, name, path), sorted ascending by version."""
    if not _MIGRATIONS_DIR.exists():
        return []
    found: list[tuple[int, str, Path]] = []
    for p in sorted(_MIGRATIONS_DIR.iterdir()):
        m = _MIGRATION_RE.match(p.name)
        if not m:
            continue
        found.append((int(m.group(1)), m.group(2), p))
    return found


def migrate(con: sqlite3.Connection) -> int:
    """Apply any migrations newer than ``current_version``. Returns the count
    of migrations applied this call."""
    _ensure_migrations_table(con)
    have = current_version(con)
    applied = 0
    for version, name, path in _discover_migrations():
        if version <= have:
            continue
        sql = path.read_text(encoding="utf-8")
        # ``executescript`` manages its own transaction and implicitly
        # COMMITs any pending one. We let it run, then record the version
        # immediately after. On failure mid-script SQLite rolls back the
        # script's own writes; the schema_migration row never lands, so the
        # next run retries from the same version.
        con.executescript(sql)
        con.execute(
            "INSERT INTO schema_migration(version, applied_at, name) VALUES (?, ?, ?)",
            (version, datetime.now(timezone.utc).isoformat(), name),
        )
        applied += 1
    return applied


# ── CLI ──────────────────────────────────────────────────────────────────────

def _cli_init() -> int:
    con = connect()
    v = current_version(con)
    print(f"OK  db={db_path()}  schema_version={v}")
    return 0


def _cli_status() -> int:
    con = connect()
    v = current_version(con)
    print(f"db:              {db_path()}")
    print(f"schema_version:  {v}")
    print("counts:")
    for table in ("session", "file_event", "ai_call", "capture", "edge",
                  "plan_item", "skill_run", "app", "stream"):
        try:
            row = con.execute(f"SELECT COUNT(*) AS n FROM {table}").fetchone()
            print(f"  {table:14s} {row['n']}")
        except sqlite3.OperationalError:
            print(f"  {table:14s} (table not present yet)")
    return 0


def _cli_migrate() -> int:
    con = connect()
    n = migrate(con)
    print(f"applied {n} migration(s); schema_version now {current_version(con)}")
    return 0


def main(argv: list[str]) -> int:
    cmd = argv[1] if len(argv) > 1 else "status"
    if cmd == "init":
        return _cli_init()
    if cmd == "status":
        return _cli_status()
    if cmd == "migrate":
        return _cli_migrate()
    print(f"unknown command: {cmd!r}; expected one of init|status|migrate", file=sys.stderr)
    return 2


if __name__ == "__main__":
    raise SystemExit(main(sys.argv))
