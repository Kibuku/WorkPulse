-- 0011_project_attribution.sql
--
-- Machine-managed project taxonomy for auto-attribution (plan U1, KTD2/KTD3).
--
-- Adds:
--   project             — the two-level (client -> project) taxonomy the
--                         attribution engine discovers and the user confirms.
--                         status candidate -> confirmed -> dismissed mirrors the
--                         cluster/workflow_method lifecycle (KTD5).
--   session.project_id           — nullable link; NULL until attributed (R11).
--   semantic_observation.project_id — same, so stages/frictions attribute too.
--   project_correction  — durable record of every user reassignment, modelled on
--                         cluster_correction; the corpus discovery learns from.
--
-- Additive only: existing rows keep NULL project_id. Applied once via the
-- schema_migration version gate, so the non-idempotent ALTERs run exactly once.

CREATE TABLE IF NOT EXISTS project (
  id           TEXT PRIMARY KEY,
  client       TEXT,                              -- coarse owner, e.g. "Verst Carbon"; NULL if flat
  name         TEXT NOT NULL,                     -- project, e.g. "MADDs Kenya/Zambia"
  status       TEXT NOT NULL DEFAULT 'candidate'
               CHECK (status IN ('candidate','confirmed','dismissed')),
  confidence   REAL,
  created_at   TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS project_status_idx ON project(status);
CREATE INDEX IF NOT EXISTS project_client_idx ON project(client);

ALTER TABLE session ADD COLUMN project_id TEXT REFERENCES project(id);
CREATE INDEX IF NOT EXISTS session_project_idx ON session(project_id);

ALTER TABLE semantic_observation ADD COLUMN project_id TEXT REFERENCES project(id);
CREATE INDEX IF NOT EXISTS semantic_observation_project_idx
  ON semantic_observation(project_id);

CREATE TABLE IF NOT EXISTS project_correction (
  id                INTEGER PRIMARY KEY AUTOINCREMENT,
  target_kind       TEXT NOT NULL CHECK (target_kind IN ('session','cluster','observation')),
  target_id         TEXT NOT NULL,
  from_project      TEXT,
  to_project        TEXT NOT NULL,
  confidence_before REAL,
  corrected_at      TEXT NOT NULL,
  signals_snapshot  TEXT             -- JSON: the evidence the engine saw
);
CREATE INDEX IF NOT EXISTS project_correction_target_idx
  ON project_correction(target_kind, target_id);
CREATE INDEX IF NOT EXISTS project_correction_to_idx
  ON project_correction(to_project);
