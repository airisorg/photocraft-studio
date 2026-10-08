-- Transient previews never update a document, version, thumbnail, or undo history.
-- Keep sequence watermarks until the authenticated session is removed, including after
-- payload expiry/clear. A delayed request cannot resurrect a superseded preview.
CREATE TABLE IF NOT EXISTS photocraft.live_previews (
 session_hash text NOT NULL REFERENCES photocraft.sessions(hash) ON DELETE CASCADE,
 tab_id uuid NOT NULL,
 project_id uuid REFERENCES photocraft.projects(id) ON DELETE SET NULL,
 seq bigint NOT NULL CHECK(seq >= 0),
 base_revision bigint NOT NULL CHECK(base_revision > 0),
 cursor jsonb,
 gesture jsonb,
 seen_at timestamptz NOT NULL DEFAULT clock_timestamp(),
 PRIMARY KEY(session_hash, tab_id),
 CHECK(cursor IS NULL OR jsonb_typeof(cursor) = 'object'),
 CHECK(gesture IS NULL OR jsonb_typeof(gesture) = 'object'),
 CHECK(octet_length(COALESCE(cursor::text, '') || COALESCE(gesture::text, '')) <= 131072)
);
CREATE INDEX IF NOT EXISTS live_previews_project ON photocraft.live_previews(project_id, seen_at);
ALTER TABLE photocraft.live_previews ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON photocraft.live_previews FROM PUBLIC;

-- Both limits (one session's tabs and one project's active peers) must hold across
-- workers. Acquire the two transaction locks in a total order before the fresh
-- authorization/upsert statement obtains its snapshot. Do not hold a lock across HTTP.
CREATE OR REPLACE FUNCTION photocraft.lock_live(p_session text, p_project uuid)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE lock_key bigint;
BEGIN
 PERFORM set_config('lock_timeout', '750ms', true);
 FOR lock_key IN
  SELECT DISTINCT value FROM unnest(ARRAY[
   hashtextextended('photocraft.live.session:' || p_session, 0),
   hashtextextended('photocraft.live.project:' || p_project::text, 0)
  ]) AS keys(value) ORDER BY value
 LOOP
  PERFORM pg_advisory_xact_lock(lock_key);
 END LOOP;
END;
$$;
REVOKE ALL ON FUNCTION photocraft.lock_live(text, uuid) FROM PUBLIC;
