-- 0005_categorizer.sql
--
-- Fix 1.7: the Categorizer agent + the teach-correct loop.
--
-- Three tables, separate from job_view so cluster.refresh() rewrites don't
-- wipe assignments:
--
--   cluster_assignment   — current assigned project per cluster.
--                          source: 'user' > 'agent' > 'fallback'.
--                          user assignments are NEVER overwritten by agent.
--
--   cluster_correction   — every time the user overrides an agent assignment,
--                          we keep the (from, to, signals) tuple. This is
--                          the corpus the Teacher will train on later.
--
--   daily_candidate      — the candidate set the user declared for a day
--                          via the morning list-capture pattern. The
--                          Categorizer prefers these when scoring.

CREATE TABLE IF NOT EXISTS cluster_assignment (
  cluster_id    TEXT PRIMARY KEY,
  stream        TEXT NOT NULL REFERENCES stream(key),
  confidence    REAL NOT NULL DEFAULT 0.0,
  source        TEXT NOT NULL CHECK (source IN ('user','agent','fallback')),
  assigned_at   TEXT NOT NULL,
  evidence      TEXT,                    -- JSON array of signal hits
  agent_run_id  TEXT REFERENCES skill_run(id)
);
CREATE INDEX IF NOT EXISTS cluster_assignment_stream_idx ON cluster_assignment(stream);
CREATE INDEX IF NOT EXISTS cluster_assignment_source_idx ON cluster_assignment(source);


CREATE TABLE IF NOT EXISTS cluster_correction (
  id                  INTEGER PRIMARY KEY AUTOINCREMENT,
  cluster_id          TEXT NOT NULL,
  from_stream         TEXT,
  to_stream           TEXT NOT NULL,
  confidence_before   REAL,
  corrected_at        TEXT NOT NULL,
  signals_snapshot    TEXT             -- JSON: the evidence the agent saw
);
CREATE INDEX IF NOT EXISTS cluster_correction_cluster_idx ON cluster_correction(cluster_id);
CREATE INDEX IF NOT EXISTS cluster_correction_to_idx      ON cluster_correction(to_stream);


CREATE TABLE IF NOT EXISTS daily_candidate (
  date              TEXT NOT NULL,
  stream            TEXT NOT NULL REFERENCES stream(key),
  source_capture_id TEXT REFERENCES capture(id),
  declared_at       TEXT NOT NULL,
  PRIMARY KEY (date, stream)
);
CREATE INDEX IF NOT EXISTS daily_candidate_date_idx ON daily_candidate(date);
