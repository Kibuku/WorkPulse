-- 0013_output_forms.sql
--
-- Output form registry (plan U1, KTD1). A form is an ADOPTED output definition
-- (sections + expected evidence + fill mode), distinct from the LEARNED
-- workflow_method tables -- so it gets its own home rather than overloading
-- those. Shape mirrors workflow_method/workflow_step (parent + ordered children
-- with expected_evidence) for familiarity.

CREATE TABLE IF NOT EXISTS output_form (
  id           TEXT PRIMARY KEY,
  name         TEXT NOT NULL,
  source_skill TEXT,                              -- skill it was seeded from, if any
  fill_mode    TEXT NOT NULL DEFAULT 'strict'
               CHECK (fill_mode IN ('strict', 'full-draft')),
  status       TEXT NOT NULL DEFAULT 'candidate'
               CHECK (status IN ('candidate', 'confirmed', 'dismissed')),
  version      INTEGER NOT NULL DEFAULT 1,
  created_at   TEXT NOT NULL,
  confirmed_at TEXT
);
CREATE INDEX IF NOT EXISTS output_form_status_idx ON output_form(status);

CREATE TABLE IF NOT EXISTS output_form_section (
  id                TEXT PRIMARY KEY,
  form_id           TEXT NOT NULL REFERENCES output_form(id) ON DELETE CASCADE,
  position          INTEGER NOT NULL,
  name              TEXT NOT NULL,
  expected_evidence TEXT,                         -- what the corpus must supply
  required          INTEGER NOT NULL DEFAULT 1,
  UNIQUE(form_id, position)
);
