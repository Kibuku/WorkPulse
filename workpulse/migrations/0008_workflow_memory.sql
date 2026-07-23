CREATE TABLE IF NOT EXISTS workflow_method (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  output_type  TEXT NOT NULL,
  scope        TEXT NOT NULL DEFAULT 'personal',
  status       TEXT NOT NULL DEFAULT 'candidate',
  confidence   REAL,
  version      INTEGER NOT NULL DEFAULT 1,
  is_demo      INTEGER NOT NULL DEFAULT 0,
  created_at   TEXT NOT NULL,
  confirmed_at TEXT
);

CREATE TABLE IF NOT EXISTS workflow_step (
  id                TEXT PRIMARY KEY,
  method_id         TEXT NOT NULL REFERENCES workflow_method(id) ON DELETE CASCADE,
  position          INTEGER NOT NULL,
  action_type       TEXT NOT NULL,
  name              TEXT NOT NULL,
  required          INTEGER NOT NULL DEFAULT 1,
  expected_evidence TEXT,
  UNIQUE(method_id, position)
);

CREATE TABLE IF NOT EXISTS workflow_example (
  id            TEXT PRIMARY KEY,
  method_id     TEXT NOT NULL REFERENCES workflow_method(id) ON DELETE CASCADE,
  title         TEXT NOT NULL,
  occurred_at   TEXT,
  evidence_json TEXT NOT NULL DEFAULT '[]',
  is_demo       INTEGER NOT NULL DEFAULT 0
);

CREATE TABLE IF NOT EXISTS workflow_decision (
  id         TEXT PRIMARY KEY,
  method_id  TEXT NOT NULL REFERENCES workflow_method(id) ON DELETE CASCADE,
  action     TEXT NOT NULL,
  payload    TEXT NOT NULL DEFAULT '{}',
  created_at TEXT NOT NULL
);
