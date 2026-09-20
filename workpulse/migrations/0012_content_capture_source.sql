-- 0012_content_capture_source.sql
--
-- Source-file reference for content_capture rows produced by LlamaParse
-- document parsing (plan U2, KTD1). Lets a parsed-file row record WHICH file it
-- came from, so a re-run can skip a file already parsed at its current mtime.
--
-- Additive + nullable: existing Tesseract screen-OCR rows keep both NULL (they
-- are never sourced from a file). Applied once via the schema_migration gate.

ALTER TABLE content_capture ADD COLUMN source_path_hash TEXT;
ALTER TABLE content_capture ADD COLUMN source_mtime TEXT;

CREATE INDEX IF NOT EXISTS content_capture_source_idx
  ON content_capture(source_path_hash);
