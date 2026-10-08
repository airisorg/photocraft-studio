//! Request-bounded collaboration previews. PostgreSQL holds shared latest state;
//! native paint semantics, durable documents, and undo remain outside this module.

use super::{ApiError, App, Result, account_changed, bad, cookie, expected_account, forbidden, hash, query, ready_db};
use axum::{
    Json,
    body::Bytes,
    extract::{Path, Query, State},
    http::{HeaderMap, StatusCode, header},
};
use serde::Deserialize;
use serde_json::{Value, json};
use sqlx::Row;
use uuid::Uuid;

const MAX_BYTES: usize = 64 * 1024;
const MAX_EVENTS: usize = 256;
const MAX_POINTS: usize = 1024;

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
pub(crate) struct LiveQuery {
    tab: Uuid,
}

#[derive(Deserialize)]
#[serde(rename_all = "camelCase", deny_unknown_fields)]
struct Update {
    tab: Uuid,
    seq: u64,
    base_revision: u64,
    cursor: Option<Cursor>,
    gesture: Option<Gesture>,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Cursor {
    x: f32,
    y: f32,
}

#[derive(Deserialize)]
#[serde(deny_unknown_fields)]
struct Gesture {
    events: Vec<Value>,
}

// Each data operation evaluates identity, current account, project state and role in
// its own statement snapshot. In particular, no cached account()/role() result admits
// later preview reads, and conditional/stale responses do not bypass authorization.
const AUTH: &str = r#"
WITH identity AS MATERIALIZED (
 SELECT a.id,a.email,a.name FROM photocraft.sessions s
 JOIN photocraft.accounts a ON a.id=s.account_id
 WHERE s.hash=$1 AND s.expires_at>clock_timestamp()
), access AS MATERIALIZED (
 SELECT p.id,p.revision,p.trashed,p.owner_id,
        CASE WHEN p.owner_id=a.id THEN 'owner' ELSE m.role END AS role
 FROM photocraft.projects p CROSS JOIN identity a
 LEFT JOIN photocraft.members m ON m.project_id=p.id AND m.email=a.email
 WHERE p.id=$3
), gate AS MATERIALIZED (
 SELECT CASE
 WHEN NOT EXISTS(SELECT 1 FROM identity) THEN 401
 WHEN $2::uuid IS NOT NULL AND NOT EXISTS(SELECT 1 FROM identity WHERE id=$2) THEN 401
 WHEN NOT EXISTS(SELECT 1 FROM access WHERE role IS NOT NULL AND NOT trashed) THEN 404
 WHEN (SELECT revision FROM access)<=0 THEN 409
 ELSE 200 END AS status
)
"#;

// A PL/pgSQL call's statement_timestamp precedes its lock wait. Evaluate this
// materialized clock in the separate post-lock data statement instead, so an
// expired renewal cannot bypass admission and expired peers cannot be returned.
const CLOCK: &str = r#"
, live_clock AS MATERIALIZED (SELECT clock_timestamp() AS observed_at)
"#;

// GET and successful PUT expose the same authorized peer snapshot. A PUT reads
// peers in its post-lock write statement, not in another transaction after it.
// Its own row is excluded, so the data-modifying CTE's pre-write snapshot is enough.
const PEERS: &str = r#"
 COALESCE((SELECT jsonb_agg(peer ORDER BY actor,tab) FROM (
  SELECT a.id AS actor,l.tab_id AS tab,jsonb_build_object(
   'actor',a.id,'name',a.name,'tab',l.tab_id,'seq',l.seq,
   'baseRevision',l.base_revision,'cursor',l.cursor,
   'gesture',CASE WHEN l.base_revision=p.revision AND (p.owner_id=a.id OR m.role='edit') THEN l.gesture ELSE NULL END,
   'ttlMs',GREATEST(0,LEAST(2000,ceil(extract(epoch FROM (l.seen_at+interval '2 seconds'-clock_timestamp()))*1000)::integer))
  ) AS peer
  FROM photocraft.live_previews l
  JOIN photocraft.sessions s ON s.hash=l.session_hash AND s.expires_at>clock_timestamp()
  JOIN photocraft.accounts a ON a.id=s.account_id
  JOIN access p ON p.id=l.project_id
  LEFT JOIN photocraft.members m ON m.project_id=p.id AND m.email=a.email
  WHERE gate.status=200
   AND l.seen_at>(SELECT observed_at FROM live_clock)-interval '2 seconds'
   AND (p.owner_id=a.id OR m.role IN ('view','edit'))
   AND (l.cursor IS NOT NULL OR (l.gesture IS NOT NULL AND l.base_revision=p.revision AND (p.owner_id=a.id OR m.role='edit')))
   AND NOT(l.session_hash=$1 AND l.tab_id=$4)
 ) peers),'[]'::jsonb)
"#;

const WRITE: &str = r#"
, previous AS MATERIALIZED (
 SELECT seq,project_id,seen_at,cursor,gesture FROM photocraft.live_previews WHERE session_hash=$1 AND tab_id=$4
), decision AS MATERIALIZED (
 SELECT CASE
 WHEN gate.status<>200 THEN gate.status
 WHEN $8::jsonb IS NOT NULL AND (SELECT revision FROM access)<>$6 THEN 409
 WHEN $8::jsonb IS NOT NULL AND (SELECT role FROM access)='view' THEN 403
 WHEN EXISTS(SELECT 1 FROM previous WHERE seq>=$5) THEN 200
 -- An existing clear cannot add occupancy. A renewal must still be active in
 -- this project after shared room/session locks were obtained. Admissions and
 -- revocations need the exclusive room lock and cannot race this transaction.
 WHEN $9::boolean AND NOT EXISTS(SELECT 1 FROM previous
  WHERE ($7::jsonb IS NULL AND $8::jsonb IS NULL)
     OR (project_id=$3 AND seen_at>(SELECT observed_at FROM live_clock)-interval '2 seconds'
         AND (cursor IS NOT NULL OR gesture IS NOT NULL))) THEN 431
 WHEN $9::boolean THEN 200
 WHEN NOT EXISTS(SELECT 1 FROM previous)
  AND (SELECT count(*) FROM photocraft.live_previews WHERE session_hash=$1)>=256 THEN 430
 WHEN ($7::jsonb IS NOT NULL OR $8::jsonb IS NOT NULL)
  AND (SELECT count(*) FROM photocraft.live_previews
       WHERE session_hash=$1 AND tab_id<>$4 AND seen_at>(SELECT observed_at FROM live_clock)-interval '2 seconds'
       AND (cursor IS NOT NULL OR gesture IS NOT NULL))>=8 THEN 429
 WHEN ($7::jsonb IS NOT NULL OR $8::jsonb IS NOT NULL)
  AND (SELECT count(*) FROM photocraft.live_previews l
       JOIN photocraft.sessions s ON s.hash=l.session_hash AND s.expires_at>clock_timestamp()
       WHERE l.project_id=$3 AND l.seen_at>(SELECT observed_at FROM live_clock)-interval '2 seconds'
       AND (l.cursor IS NOT NULL OR l.gesture IS NOT NULL)
       AND NOT(l.session_hash=$1 AND l.tab_id=$4))>=64 THEN 429
 ELSE 200 END AS status FROM gate
), written AS (
 INSERT INTO photocraft.live_previews(session_hash,tab_id,project_id,seq,base_revision,cursor,gesture,seen_at)
 SELECT $1,$4,$3,$5,CASE WHEN $8::jsonb IS NULL THEN (SELECT revision FROM access) ELSE $6 END,$7,$8,clock_timestamp()
 FROM decision WHERE status=200
 ON CONFLICT(session_hash,tab_id) DO UPDATE SET
  project_id=EXCLUDED.project_id,seq=EXCLUDED.seq,base_revision=EXCLUDED.base_revision,
  cursor=EXCLUDED.cursor,gesture=EXCLUDED.gesture,seen_at=EXCLUDED.seen_at
 WHERE live_previews.seq<EXCLUDED.seq
 RETURNING seq
)
"#;

// Install under ready_db's existing setup transaction/lock. Only compile-time SQL
// fragments enter this DDL; all request values remain bound function arguments.
// Keeping the definition here lets GET and the function share AUTH/PEERS exactly.
pub(crate) fn migration() -> String {
    format!(
        "CREATE OR REPLACE FUNCTION photocraft.exchange_live_v1(text,uuid,uuid,uuid,bigint,bigint,jsonb,jsonb,boolean)
         RETURNS TABLE(result_status integer,result_revision bigint,result_role text,result_accepted boolean,result_seq bigint,result_peers jsonb)
         LANGUAGE plpgsql VOLATILE SECURITY INVOKER SET search_path=pg_catalog AS $photocraft_live$
         DECLARE admitted integer;
         BEGIN
          {AUTH} SELECT gate.status INTO admitted FROM gate;
          IF admitted<>200 THEN
           RETURN QUERY SELECT admitted,NULL::bigint,NULL::text,false,0::bigint,'[]'::jsonb;
           RETURN;
          END IF;
          PERFORM photocraft.lock_live($1,$3,$9);
          -- VOLATILE gives this internal query a new snapshot after the lock wait.
          -- The clock CTE is evaluated here, not at the outer function call's start.
          RETURN QUERY {AUTH}{CLOCK}{WRITE}
           SELECT decision.status,(SELECT revision FROM access),(SELECT role FROM access),
            EXISTS(SELECT 1 FROM written),COALESCE((SELECT seq FROM written),(SELECT seq FROM previous),0),
            CASE WHEN decision.status=200 THEN {PEERS} ELSE '[]'::jsonb END
           FROM decision CROSS JOIN gate;
         END;
         $photocraft_live$;
         REVOKE ALL ON FUNCTION photocraft.exchange_live_v1(text,uuid,uuid,uuid,bigint,bigint,jsonb,jsonb,boolean) FROM PUBLIC;"
    )
}

fn identity(h: &HeaderMap) -> Result<(String, Option<Uuid>)> {
    let session =
        cookie(h, "pc_session").filter(|token| token.len() == 64).ok_or(ApiError(StatusCode::UNAUTHORIZED, "Session expired. Sign in again.".into()))?;
    Ok((hash(session), expected_account(h)?))
}

fn checked_status(status: i32) -> Result<()> {
    match status {
        200 => Ok(()),
        401 => Err(account_changed()),
        403 => Err(ApiError(StatusCode::FORBIDDEN, "Editing access is required for a live gesture.".into())),
        404 => Err(forbidden()),
        409 => Err(ApiError(StatusCode::CONFLICT, "Live preview base changed. Wait for the saved document to synchronize.".into())),
        429 => Err(ApiError(StatusCode::TOO_MANY_REQUESTS, "Live preview slots are full. Close another collaboration tab and retry.".into())),
        430 => Err(ApiError(StatusCode::TOO_MANY_REQUESTS, "This session's live tab history is full. Sign in again to open another collaboration tab.".into())),
        _ => Err(ApiError(StatusCode::SERVICE_UNAVAILABLE, "Live preview is temporarily unavailable.".into())),
    }
}

pub(crate) async fn get(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>, Query(v): Query<LiveQuery>) -> Result<Json<Value>> {
    let (session, expected) = identity(&h)?;
    // Even this single data statement needs BEGIN: SQLx's unnamed Parse/Bind
    // exchange otherwise crosses an idle boundary in transaction-mode poolers.
    let mut tx = ready_db(&s).await?.begin().await?;
    let sql =
        format!("{AUTH}{CLOCK} SELECT gate.status,(SELECT revision FROM access) AS revision,(SELECT role FROM access) AS role,{PEERS} AS peers FROM gate");
    let row = query(&sql).bind(session).bind(expected).bind(id).bind(v.tab).fetch_one(&mut *tx).await?;
    checked_status(row.get("status"))?;
    tx.commit().await?;
    Ok(Json(json!({"revision":row.get::<i64,_>("revision"),"role":row.get::<String,_>("role"),"peers":row.get::<Value,_>("peers")})))
}

pub(crate) async fn put(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>, body: Bytes) -> Result<Json<Value>> {
    if h.get(header::ORIGIN).and_then(|value| value.to_str().ok()) != Some(s.origin.as_str()) {
        return Err(ApiError(StatusCode::FORBIDDEN, "Request origin does not match this workspace".into()));
    }
    let (session, expected) = identity(&h)?;
    let update = parse(&body)?;
    let cursor = update.cursor.map(|p| json!({"x":p.x,"y":p.y}));
    let gesture = update.gesture.map(|g| json!({"events":g.events}));
    let seq = i64::try_from(update.seq).map_err(|_| bad("Invalid live sequence"))?;
    let base = i64::try_from(update.base_revision).map_err(|_| bad("Invalid live base revision"))?;
    let pool = ready_db(&s).await?;
    for shared in [true, false] {
        let mut tx = pool.begin().await?;
        let row = query(
            "SELECT result_status AS status,result_revision AS revision,result_role AS role,
                    result_accepted AS accepted,result_seq AS seq,result_peers AS peers
             FROM photocraft.exchange_live_v1($1,$2,$3,$4,$5,$6,$7,$8,$9)",
        )
        .bind(&session)
        .bind(expected)
        .bind(id)
        .bind(update.tab)
        .bind(seq)
        .bind(base)
        .bind(&cursor)
        .bind(&gesture)
        .bind(shared)
        .fetch_one(&mut *tx)
        .await?;
        let status = row.get("status");
        if shared && status == 431 {
            // Never upgrade a held shared lock: concurrent expired renewals could
            // deadlock. A new transaction obtains exclusive locks and fresh AUTH.
            tx.rollback().await?;
            continue;
        }
        checked_status(status)?;
        tx.commit().await?;
        return Ok(Json(json!({"accepted":row.get::<bool,_>("accepted"),"seq":row.get::<i64,_>("seq"),"revision":row.get::<i64,_>("revision"),
            "role":row.get::<String,_>("role"),"peers":row.get::<Value,_>("peers")})));
    }
    Err(ApiError(StatusCode::SERVICE_UNAVAILABLE, "Live preview admission could not complete.".into()))
}

fn parse(body: &[u8]) -> Result<Update> {
    if body.len() > MAX_BYTES {
        return Err(ApiError(StatusCode::PAYLOAD_TOO_LARGE, "Live preview exceeds 64 KiB.".into()));
    }
    let value: Value = serde_json::from_slice(body).map_err(|_| bad("Invalid live preview JSON"))?;
    let mut nodes = 0;
    let mut points = 0;
    validate_json(&value, 0, &mut nodes, &mut points)?;
    let update: Update = serde_json::from_value(value).map_err(|_| bad("Invalid live preview fields"))?;
    if update.tab.is_nil() || update.seq > i64::MAX as u64 || update.base_revision == 0 || update.base_revision > i64::MAX as u64 {
        return Err(bad("Invalid live tab, sequence or base revision"));
    }
    if update.cursor.as_ref().is_some_and(|p| !coordinate(f64::from(p.x)) || !coordinate(f64::from(p.y))) {
        return Err(bad("Invalid live cursor coordinates"));
    }
    if let Some(gesture) = &update.gesture {
        if gesture.events.is_empty() || gesture.events.len() > MAX_EVENTS {
            return Err(bad("Live gesture must contain 1 to 256 events"));
        }
        for event in &gesture.events {
            validate_event(event)?;
        }
    }
    Ok(update)
}

fn coordinate(value: f64) -> bool {
    value.is_finite() && value.abs() <= 1_000_000.
}

fn validate_json(value: &Value, depth: usize, nodes: &mut usize, points: &mut usize) -> Result<()> {
    *nodes += 1;
    if depth > 16 || *nodes > 16_384 {
        return Err(bad("Live preview JSON is too complex"));
    }
    match value {
        Value::Number(number) if number.as_f64().is_none_or(|n| !n.is_finite()) => return Err(bad("Live preview numbers must be finite")),
        Value::String(text) if text.len() > 4096 => return Err(bad("Live preview string is too long")),
        Value::Array(values) => {
            if values.len() > MAX_POINTS {
                return Err(bad("Live preview array is too long"));
            }
            for value in values {
                validate_json(value, depth + 1, nodes, points)?;
            }
        }
        Value::Object(fields) => {
            if fields.len() > 128 {
                return Err(bad("Live preview object has too many fields"));
            }
            for (key, value) in fields {
                if key.len() > 128 {
                    return Err(bad("Live preview field name is too long"));
                }
                if key == "points" {
                    let values = value.as_array().ok_or(bad("Invalid stroke points"))?;
                    *points += values.len();
                    if *points > MAX_POINTS {
                        return Err(bad("Live gesture exceeds 1024 stroke points"));
                    }
                    for point in values {
                        let coords = point.as_array().ok_or(bad("Invalid stroke point"))?;
                        if !(2..=6).contains(&coords.len())
                            || coords.iter().any(|n| n.as_f64().is_none_or(|n| !coordinate(n)))
                            || coords.get(2).is_some_and(|n| n.as_f64().is_none_or(|n| !(0.0..=1.0).contains(&n)))
                        {
                            return Err(bad("Invalid stroke point coordinates"));
                        }
                    }
                }
                validate_json(value, depth + 1, nodes, points)?;
            }
        }
        _ => {}
    }
    Ok(())
}

fn validate_event(event: &Value) -> Result<()> {
    let fields = event.as_object().ok_or(bad("Invalid native preview event"))?;
    if event.get("gesture").and_then(Value::as_u64).is_none() || event.get("sequence").and_then(Value::as_u64).is_none() {
        return Err(bad("Invalid native gesture identity or sequence"));
    }
    let allowed: &[&str] = match event.get("kind").and_then(Value::as_str) {
        Some("strokeStart") => &["command", "params", "brush", "foreground", "background"],
        Some("points") => &["points"],
        Some("moveStart") => &["layers"],
        Some("offset") => &["dx", "dy"],
        Some("end" | "cancel") => &[],
        Some("unavailable") => &["reason"],
        _ => return Err(bad("Invalid native preview event kind")),
    };
    if fields.keys().any(|key| !["gesture", "sequence", "kind"].contains(&key.as_str()) && !allowed.contains(&key.as_str())) {
        return Err(bad("Unknown native preview event field"));
    }
    let valid = match event.get("kind").and_then(Value::as_str) {
        Some("strokeStart") => {
            event.get("command").and_then(Value::as_str).is_some_and(|s| matches!(s, "paint.stroke" | "paint.pencil"))
                && event.get("params").is_some_and(Value::is_object)
                && event.get("brush").is_some_and(Value::is_object)
                && ["foreground", "background"]
                    .iter()
                    .all(|key| event.get(key).and_then(Value::as_array).is_some_and(|v| v.len() == 4 && v.iter().all(Value::is_number)))
        }
        Some("points") => event.get("points").and_then(Value::as_array).is_some_and(|v| !v.is_empty()),
        Some("moveStart") => {
            event.get("layers").and_then(Value::as_array).is_some_and(|v| !v.is_empty() && v.len() <= 64 && v.iter().all(|n| n.as_u64().is_some()))
        }
        Some("offset") => ["dx", "dy"].iter().all(|key| event.get(key).and_then(Value::as_i64).is_some_and(|n| (-1_000_000..=1_000_000).contains(&n))),
        Some("end" | "cancel") => true,
        Some("unavailable") => event.get("reason").and_then(Value::as_str).is_some_and(|s| s.len() <= 1024),
        _ => false,
    };
    if valid { Ok(()) } else { Err(bad("Invalid native preview event")) }
}

#[cfg(test)]
mod tests {
    use super::*;

    fn payload(events: Value) -> Vec<u8> {
        json!({"tab":Uuid::new_v4(),"seq":1,"baseRevision":1,"cursor":{"x":12.,"y":24.},"gesture":{"events":events}}).to_string().into_bytes()
    }

    #[test]
    fn accepts_native_cumulative_events_without_resolving_paint() {
        let events = json!([
            {"gesture":7,"sequence":0,"kind":"strokeStart","command":"paint.pencil","params":{"points":[[12.,24.,1.]]},"brush":{},"foreground":[0.,1.,0.,1.],"background":[1.,1.,1.,1.]},
            {"gesture":7,"sequence":1,"kind":"points","points":[[14.,25.,0.8,0.2,0.3,0.4]]},
            {"gesture":7,"sequence":2,"kind":"end"}
        ]);
        assert!(parse(&payload(events)).is_ok());
    }

    #[test]
    fn rejects_event_and_total_point_overflow() {
        assert!(parse(&payload(json!(vec![json!({"gesture":1,"sequence":0,"kind":"end"}); 257]))).is_err());
        let points = vec![json!([1., 2., 1.]); 513];
        assert!(
            parse(&payload(json!([
                {"gesture":1,"sequence":0,"kind":"points","points":points},
                {"gesture":1,"sequence":1,"kind":"points","points":points}
            ])))
            .is_err()
        );
    }

    #[test]
    fn rejects_unbounded_or_malformed_inputs() {
        assert!(parse(&vec![b' '; MAX_BYTES + 1]).is_err_and(|error| error.0 == StatusCode::PAYLOAD_TOO_LARGE));
        assert!(parse(&payload(json!([{"gesture":1,"sequence":0,"kind":"command","code":"arbitrary"}]))).is_err());
        assert!(parse(&payload(json!([{"gesture":1,"sequence":0,"kind":"points","points":[[1e12,2.]]}]))).is_err());
        assert!(parse(&payload(json!([{"gesture":1,"sequence":0,"kind":"points","points":[[1.,2.,1.1]]}]))).is_err());
        assert!(parse(&payload(json!([{"gesture":1,"sequence":0,"kind":"end","futureField":true}]))).is_err());
        let mut nested = json!(0);
        for _ in 0..18 {
            nested = json!({"nested":nested});
        }
        assert!(parse(&payload(json!([{"gesture":1,"sequence":0,"kind":"end","nested":nested}]))).is_err());
    }
}
