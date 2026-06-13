-- 0003_job_view.sql
--
-- Materialized job_view: one row per cluster of sessions. Populated by
-- scripts/cluster.py from the parameters in skills/cluster.md. Re-run is
-- safe; cluster_id is a content-hash, so re-clustering is idempotent.
--
-- SQLite does not have materialized views as a first-class object — we use
-- a regular table that the cluster refresh job rewrites. Keeps the SQL
-- engine simple and the result inspectable.

CREATE TABLE IF NOT EXISTS job_view (
  cluster_id     TEXT PRIMARY KEY,
  stream         TEXT,                       -- NULL = untagged cluster
  started_at     TEXT NOT NULL,              -- ISO8601, min(session.started_at)
  ended_at       TEXT NOT NULL,              -- ISO8601, max(session.ended_at)
  session_count  INTEGER NOT NULL,
  total_seconds  REAL NOT NULL DEFAULT 0,    -- sum of session durations
  apps_seen      TEXT NOT NULL DEFAULT '[]', -- JSON array of distinct apps
  computed_at    TEXT NOT NULL               -- when this row was written
);

CREATE INDEX IF NOT EXISTS job_view_stream_idx  ON job_view(stream);
CREATE INDEX IF NOT EXISTS job_view_started_idx ON job_view(started_at);
CREATE INDEX IF NOT EXISTS job_view_ended_idx   ON job_view(ended_at);
