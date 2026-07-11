-- 0006_calendar.sql
--
-- Calendar events as a new atom type. Same institutional/private split as
-- session: the public table holds only the title_hash + auto-resolved
-- stream + time bounds; raw title / body / location / attendees live in
-- the private table that never leaves the machine.
--
-- Events come from an external feed (.ics URL today; Microsoft Graph later
-- if needed). The `source` column tracks which feed delivered the event so
-- we can dedup if the same event comes from two feeds.

CREATE TABLE IF NOT EXISTS calendar_event (
  id           TEXT PRIMARY KEY,
  source       TEXT NOT NULL,              -- 'ics' | 'graph' | 'manual'
  started_at   TEXT NOT NULL,              -- ISO8601 UTC
  ended_at     TEXT NOT NULL,
  title_hash   TEXT NOT NULL,              -- sha256 of normalized title
  stream       TEXT REFERENCES stream(key),-- auto-resolved via projects.yaml
  is_organizer INTEGER NOT NULL DEFAULT 0,
  fetched_at   TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS calendar_event_started_idx ON calendar_event(started_at);
CREATE INDEX IF NOT EXISTS calendar_event_ended_idx   ON calendar_event(ended_at);
CREATE INDEX IF NOT EXISTS calendar_event_stream_idx  ON calendar_event(stream);
CREATE INDEX IF NOT EXISTS calendar_event_source_idx  ON calendar_event(source);


CREATE TABLE IF NOT EXISTS calendar_event_local (
  event_id      TEXT PRIMARY KEY REFERENCES calendar_event(id) ON DELETE CASCADE,
  raw_title     TEXT,
  raw_body      TEXT,
  raw_location  TEXT,
  attendees     TEXT                       -- JSON array of email strings
);
