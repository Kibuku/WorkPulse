-- 0004_cluster_names.sql
--
-- Names for clusters, keyed by the content-hash cluster_id. Lives in its own
-- table so cluster.refresh() (which DELETE+INSERTs job_view) never wipes a
-- name. A user-set name overrides anything the LLM proposes; an LLM name
-- overrides the fallback.
--
-- source values: 'user' > 'llm' > 'fallback' in precedence.

CREATE TABLE IF NOT EXISTS cluster_name (
  cluster_id     TEXT PRIMARY KEY,
  name           TEXT NOT NULL,
  one_liner      TEXT,
  confidence     REAL NOT NULL DEFAULT 0.0,
  source         TEXT NOT NULL CHECK (source IN ('llm','fallback','user')),
  named_at       TEXT NOT NULL,
  model          TEXT,
  skill_run_id   TEXT REFERENCES skill_run(id)
);

CREATE INDEX IF NOT EXISTS cluster_name_source_idx ON cluster_name(source);
CREATE INDEX IF NOT EXISTS cluster_name_named_idx  ON cluster_name(named_at);
