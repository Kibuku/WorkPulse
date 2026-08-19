CREATE TABLE IF NOT EXISTS content_capture (
  id            TEXT PRIMARY KEY,
  ts            TEXT NOT NULL,
  app           TEXT NOT NULL,
  stage         TEXT,
  redacted_text TEXT NOT NULL,
  ocr_engine    TEXT NOT NULL,
  confirmed     INTEGER NOT NULL DEFAULT 0
);
CREATE INDEX IF NOT EXISTS content_capture_ts_idx ON content_capture(ts);
