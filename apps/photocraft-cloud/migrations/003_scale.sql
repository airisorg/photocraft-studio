-- Existing active previews do not consume another room slot. Shared room locks let
-- those renewals proceed independently while exclusive admissions/revocations wait
-- for them to commit. The handler revalidates eligibility AFTER these locks; it
-- rolls back before retrying admission, never upgrades a held shared lock.
CREATE OR REPLACE FUNCTION photocraft.lock_live(p_session text, p_project uuid, p_shared boolean)
RETURNS void LANGUAGE plpgsql AS $$
DECLARE item record;
BEGIN
 PERFORM set_config('lock_timeout', '750ms', true);
 FOR item IN
  SELECT value,bool_and(shared) AS shared FROM (VALUES
   (hashtextextended('photocraft.live.session:' || p_session, 0),false),
   (hashtextextended('photocraft.live.project:' || p_project::text, 0),p_shared)
  ) AS keys(value,shared) GROUP BY value ORDER BY value
 LOOP
  IF item.shared THEN
   PERFORM pg_advisory_xact_lock_shared(item.value);
  ELSE
   PERFORM pg_advisory_xact_lock(item.value);
  END IF;
 END LOOP;
END;
$$;
REVOKE ALL ON FUNCTION photocraft.lock_live(text, uuid, boolean) FROM PUBLIC;
