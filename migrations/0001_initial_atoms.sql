-- 0001_initial_atoms.sql
--
-- The four atom types plus typed edges, the private projection, and skill_run
-- bookkeeping. This is the v2 substrate per PLAN.md §3 and
-- docs/WORKPULSE_FROM_FIRST_PRINCIPLES.md.
--
-- Atoms: session, file_event, ai_call, capture. Everything else is a view.
-- Private projection: session_local, file_event_local — raw strings that
-- never leave the machine. The public tables are institutional-safe by
-- construction (no raw titles, no raw paths). Designed at v0 so v4
-- Institution Brain is a query, not a rewrite.

PRAGMA foreign_keys = ON;
PRAGMA journal_mode = WAL;

-- ── reference tables (small, lazily populated) ──────────────────────────────

CREATE TABLE IF NOT EXISTS app (
  name        TEXT PRIMARY KEY,
  category    TEXT,
  first_seen  TEXT NOT NULL,
  last_seen   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS stream (
  key         TEXT PRIMARY KEY,
  label       TEXT NOT NULL,
  parent_key  TEXT REFERENCES stream(key)
);

-- ── atom: session ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS session (
  id          TEXT PRIMARY KEY,
  started_at  TEXT NOT NULL,                   -- ISO8601 UTC
  ended_at    TEXT,                            -- NULL while open
  app         TEXT NOT NULL REFERENCES app(name),
  title_hash  TEXT NOT NULL,                   -- sha256 of normalized title
  stream      TEXT REFERENCES stream(key),     -- NULL = untagged (first-class state)
  cluster_id  TEXT                             -- filled by nightly cluster.md
);
CREATE INDEX IF NOT EXISTS session_started_idx ON session(started_at);
CREATE INDEX IF NOT EXISTS session_stream_idx  ON session(stream);
CREATE INDEX IF NOT EXISTS session_app_idx     ON session(app);
CREATE INDEX IF NOT EXISTS session_cluster_idx ON session(cluster_id);

CREATE TABLE IF NOT EXISTS session_local (
  session_id  TEXT PRIMARY KEY REFERENCES session(id) ON DELETE CASCADE,
  raw_title   TEXT,
  raw_path    TEXT,
  raw_files   TEXT                              -- JSON array
);

-- ── atom: file_event ────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS file_event (
  id          TEXT PRIMARY KEY,
  ts          TEXT NOT NULL,
  path_hash   TEXT NOT NULL,
  kind        TEXT NOT NULL CHECK (kind IN ('created','modified','deleted','moved'))
);
CREATE INDEX IF NOT EXISTS file_event_ts_idx   ON file_event(ts);
CREATE INDEX IF NOT EXISTS file_event_path_idx ON file_event(path_hash);

CREATE TABLE IF NOT EXISTS file_event_local (
  file_event_id  TEXT PRIMARY KEY REFERENCES file_event(id) ON DELETE CASCADE,
  raw_path       TEXT NOT NULL
);

-- ── atom: ai_call ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS ai_call (
  id           TEXT PRIMARY KEY,
  ts           TEXT NOT NULL,
  provider     TEXT NOT NULL,
  model        TEXT NOT NULL,
  in_tokens    INTEGER NOT NULL DEFAULT 0,
  out_tokens   INTEGER NOT NULL DEFAULT 0,
  cost_usd     REAL NOT NULL DEFAULT 0,
  prompt_slug  TEXT
);
CREATE INDEX IF NOT EXISTS ai_call_ts_idx   ON ai_call(ts);
CREATE INDEX IF NOT EXISTS ai_call_slug_idx ON ai_call(prompt_slug);

-- ── atom: capture ───────────────────────────────────────────────────────────

CREATE TABLE IF NOT EXISTS capture (
  id           TEXT PRIMARY KEY,
  ts           TEXT NOT NULL,
  author       TEXT NOT NULL CHECK (author IN ('human','system')),
  body         TEXT NOT NULL,
  pinned_kind  TEXT,                            -- 'session' | 'job' | 'plan_item' | NULL
  pinned_id    TEXT
);
CREATE INDEX IF NOT EXISTS capture_ts_idx     ON capture(ts);
CREATE INDEX IF NOT EXISTS capture_pinned_idx ON capture(pinned_kind, pinned_id);

-- ── typed edges (polymorphic, extracted at write time, zero LLM) ────────────
-- src/dst kinds are strings rather than FKs because the graph spans many
-- atom types and SQLite doesn't do polymorphic FKs well. Cheaper than
-- per-pair tables; queries still hit the indexes.

CREATE TABLE IF NOT EXISTS edge (
  id          INTEGER PRIMARY KEY AUTOINCREMENT,
  src_kind    TEXT NOT NULL,                    -- 'session' | 'capture' | 'plan_item' | ...
  src_id      TEXT NOT NULL,
  rel         TEXT NOT NULL,                    -- 'in_app' | 'in_stream' | 'touched_file' | ...
  dst_kind    TEXT NOT NULL,
  dst_id      TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS edge_src_idx ON edge(src_kind, src_id);
CREATE INDEX IF NOT EXISTS edge_dst_idx ON edge(dst_kind, dst_id);
CREATE INDEX IF NOT EXISTS edge_rel_idx ON edge(rel);
-- Prevent duplicate edges (idempotent write):
CREATE UNIQUE INDEX IF NOT EXISTS edge_unique ON edge(src_kind, src_id, rel, dst_kind, dst_id);

-- ── plan_item — atomic, not derived ─────────────────────────────────────────
-- Plan items pre-date their sessions, so they can't be a session view.
-- They're a small atom of their own.

CREATE TABLE IF NOT EXISTS plan_item (
  id               TEXT PRIMARY KEY,
  plan_date        TEXT NOT NULL,               -- YYYY-MM-DD
  name             TEXT NOT NULL,
  planned_minutes  INTEGER,
  done             INTEGER NOT NULL DEFAULT 0,
  job_id           TEXT,                        -- legacy v1 linkage; nullable
  stream           TEXT REFERENCES stream(key),
  section          TEXT NOT NULL CHECK (section IN ('carried','new')),
  raw              TEXT
);
CREATE INDEX IF NOT EXISTS plan_item_date_idx ON plan_item(plan_date);

-- ── skill_run — every LLM/skill invocation is auditable ─────────────────────

CREATE TABLE IF NOT EXISTS skill_run (
  id             TEXT PRIMARY KEY,
  ts             TEXT NOT NULL,
  skill_slug     TEXT NOT NULL,                 -- e.g. 'classify', 'think', 'consolidate'
  parent_run_id  TEXT REFERENCES skill_run(id),
  model          TEXT,
  in_tokens      INTEGER NOT NULL DEFAULT 0,
  out_tokens     INTEGER NOT NULL DEFAULT 0,
  cost_usd       REAL NOT NULL DEFAULT 0,
  input          TEXT,
  output         TEXT,
  status         TEXT NOT NULL CHECK (status IN ('ok','error','fallback'))
);
CREATE INDEX IF NOT EXISTS skill_run_ts_idx   ON skill_run(ts);
CREATE INDEX IF NOT EXISTS skill_run_slug_idx ON skill_run(skill_slug);
