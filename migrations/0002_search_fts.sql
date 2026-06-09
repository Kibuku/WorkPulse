-- 0002_search_fts.sql
--
-- FTS5 index over searchable atom content. One row per searchable atom; the
-- (atom_kind, atom_id) pair identifies the source so search results can
-- route back to the original row in the four atom tables.
--
-- The vec0 vector table is NOT created here — it requires the sqlite-vec
-- extension to be loaded, which we can't assume at migration time. It's
-- created lazily by scripts/search._ensure_vec_schema() the first time
-- vector search is requested. Schema-as-optional, per PLAN.md §7 step 5.
--
-- FTS5 is a stdlib SQLite feature since 3.9; no extension needed.

CREATE VIRTUAL TABLE IF NOT EXISTS search_fts USING fts5(
  atom_kind UNINDEXED,        -- 'session' | 'capture' | 'ai_call' | 'plan_item'
  atom_id   UNINDEXED,
  ts        UNINDEXED,        -- ISO8601 UTC, used for recency boost
  stream    UNINDEXED,        -- nullable; used for stream boost
  content,                    -- the actual searchable text
  tokenize  = 'porter unicode61'
);

-- A small bookkeeping table tracks the last-reindex watermark per atom kind
-- so reindex --incremental is cheap.
CREATE TABLE IF NOT EXISTS search_index_state (
  atom_kind   TEXT PRIMARY KEY,
  last_ts     TEXT NOT NULL,    -- ISO8601, exclusive upper bound for "indexed"
  updated_at  TEXT NOT NULL
);
