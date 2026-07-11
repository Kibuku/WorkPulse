-- 0007_browser_and_privacy.sql
--
-- Three things in one migration because they're load-bearing together:
--   1. browser_visit atom    — what the user is doing in the browser
--   2. stream.is_private     — the personal tier flag
--   3. personal_auth         — password hash + unlock tokens
--
-- The privacy model:
--   - Streams default to is_private=0. Project streams (uganda, dev) stay
--     public on the dashboard.
--   - `personal` stream gets is_private=1 by upsert on this migration's
--     first run, so any browser visit routed there is hidden behind the
--     password gate.
--   - The Categorizer never auto-tags a cluster as personal; only the
--     browser_tracker writes to streams flagged as personal.
--
-- The auth model:
--   - One password (PIN-style), bcrypt-style hash via stdlib hashlib.scrypt.
--   - Unlock writes a signed token to a HttpOnly cookie + this table.
--   - Tokens expire after personal_unlock_minutes (default 30, from config).

CREATE TABLE IF NOT EXISTS browser_visit (
  id             TEXT PRIMARY KEY,
  ts             TEXT NOT NULL,           -- ISO8601 UTC
  app            TEXT NOT NULL,           -- 'Safari' | 'Chrome' | 'Brave' | 'Microsoft Edge'
  domain         TEXT NOT NULL,           -- e.g. 'verst.sharepoint.com'
  url_hash       TEXT NOT NULL,           -- sha256(url)[:16]
  title_hash     TEXT NOT NULL,
  stream         TEXT REFERENCES stream(key),  -- resolved via projects.yaml
  is_private     INTEGER NOT NULL DEFAULT 0    -- denormalized for fast filtering
);
CREATE INDEX IF NOT EXISTS browser_visit_ts_idx     ON browser_visit(ts);
CREATE INDEX IF NOT EXISTS browser_visit_stream_idx ON browser_visit(stream);
CREATE INDEX IF NOT EXISTS browser_visit_domain_idx ON browser_visit(domain);


CREATE TABLE IF NOT EXISTS browser_visit_local (
  visit_id  TEXT PRIMARY KEY REFERENCES browser_visit(id) ON DELETE CASCADE,
  raw_url   TEXT NOT NULL,
  raw_title TEXT NOT NULL
);


-- Extend stream with the privacy flag. ALTER TABLE ADD COLUMN is safe in
-- SQLite and idempotent because the migration runner only applies once.
ALTER TABLE stream ADD COLUMN is_private INTEGER NOT NULL DEFAULT 0;
CREATE INDEX IF NOT EXISTS stream_private_idx ON stream(is_private);

-- Mark the `personal` stream as private on first run. The user can later
-- add other streams to the private tier by editing this column.
INSERT INTO stream(key, label, parent_key, is_private)
  VALUES ('personal', 'Personal', NULL, 1)
ON CONFLICT(key) DO UPDATE SET is_private = 1;


CREATE TABLE IF NOT EXISTS personal_auth (
  id             INTEGER PRIMARY KEY CHECK (id = 1),  -- singleton row
  password_hash  TEXT NOT NULL,
  password_salt  TEXT NOT NULL,
  server_secret  TEXT NOT NULL,                       -- HMAC key for tokens
  created_at     TEXT NOT NULL,
  updated_at     TEXT NOT NULL
);


CREATE TABLE IF NOT EXISTS personal_unlock_token (
  token       TEXT PRIMARY KEY,
  expires_at  TEXT NOT NULL,
  created_at  TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS personal_unlock_expires_idx
  ON personal_unlock_token(expires_at);
