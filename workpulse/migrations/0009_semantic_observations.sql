-- Local, derived meaning over existing Personal WorkPulse evidence.
-- Summaries are deliberately generic: raw titles, paths and URLs remain only
-- in the existing *_local tables and never enter semantic_observation.

CREATE TABLE IF NOT EXISTS semantic_observation (
  id             TEXT PRIMARY KEY,
  observed_date  TEXT NOT NULL,
  kind           TEXT NOT NULL CHECK (kind IN ('stage','friction')),
  semantic_key   TEXT NOT NULL,
  stream         TEXT REFERENCES stream(key),
  summary        TEXT NOT NULL,
  confidence     REAL NOT NULL CHECK (confidence >= 0 AND confidence <= 1),
  evidence_count INTEGER NOT NULL DEFAULT 0,
  source_types   TEXT NOT NULL DEFAULT '[]',
  first_seen     TEXT NOT NULL,
  last_seen      TEXT NOT NULL,
  is_private     INTEGER NOT NULL DEFAULT 0,
  status         TEXT NOT NULL DEFAULT 'proposed'
                 CHECK (status IN ('proposed','confirmed','dismissed')),
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS semantic_observation_date_idx
  ON semantic_observation(observed_date);
CREATE INDEX IF NOT EXISTS semantic_observation_stream_idx
  ON semantic_observation(stream);
CREATE INDEX IF NOT EXISTS semantic_observation_status_idx
  ON semantic_observation(status);

CREATE TABLE IF NOT EXISTS semantic_evidence_local (
  observation_id TEXT NOT NULL REFERENCES semantic_observation(id) ON DELETE CASCADE,
  source_kind    TEXT NOT NULL CHECK (source_kind IN ('session','file_event','browser_visit')),
  source_id      TEXT NOT NULL,
  PRIMARY KEY (observation_id, source_kind, source_id)
);
