//! HTTP-only cloud adapter. Editing, rendering and file formats stay in PhotoCraft's engine.
#![forbid(unsafe_code)]
#![deny(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

mod invitations;
mod merge;

use axum::{
    Json, Router,
    body::Bytes,
    extract::{DefaultBodyLimit, Path, Query, State},
    http::{HeaderMap, HeaderValue, Method, StatusCode, header},
    middleware::{self, Next},
    response::{IntoResponse, Redirect, Response},
    routing::{get, post, put},
};
use serde::Deserialize;
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use sqlx::{
    PgPool, Row,
    postgres::{PgConnectOptions, PgPoolOptions, PgSslMode},
};
use std::{
    str::FromStr,
    sync::{
        Arc,
        atomic::{AtomicBool, Ordering},
    },
};
use tower_http::{
    compression::CompressionLayer,
    services::{ServeDir, ServeFile},
};
use uuid::Uuid;

pub const CHUNK: usize = 524_288;
pub const MAX_FILE: usize = 100 * 1024 * 1024;
const ACCOUNT_QUOTA: i64 = 1024 * 1024 * 1024;
#[derive(Clone)]
pub struct AppState {
    pub db: Option<PgPool>,
    pub ready: Arc<AtomicBool>,
    pub origin: String,
    pub supabase: String,
    pub anon: String,
    pub http: reqwest::Client,
}
type App = Arc<AppState>;
#[derive(Debug)]
pub struct ApiError(StatusCode, String);
impl IntoResponse for ApiError {
    fn into_response(self) -> Response {
        (self.0, Json(json!({"error": self.1}))).into_response()
    }
}
impl From<sqlx::Error> for ApiError {
    fn from(_: sqlx::Error) -> Self {
        Self(StatusCode::SERVICE_UNAVAILABLE, "Cloud storage is temporarily unavailable. Your local document is unchanged.".into())
    }
}
type Result<T> = std::result::Result<T, ApiError>;
fn bad(s: &str) -> ApiError {
    ApiError(StatusCode::BAD_REQUEST, s.into())
}
fn forbidden() -> ApiError {
    ApiError(StatusCode::NOT_FOUND, "Project not found or access removed".into())
}
fn hash(s: impl AsRef<[u8]>) -> String {
    hex::encode(Sha256::digest(s.as_ref()))
}
fn token() -> String {
    format!("{}{}", Uuid::new_v4().simple(), Uuid::new_v4().simple())
}
fn db(s: &App) -> Result<&PgPool> {
    let pool =
        s.db.as_ref()
            .ok_or(ApiError(StatusCode::SERVICE_UNAVAILABLE, "Cloud storage is not configured yet. You can edit and download files locally.".into()))?;
    if !s.ready.load(Ordering::Acquire) {
        return Err(ApiError(StatusCode::SERVICE_UNAVAILABLE, "Cloud storage is starting. Your local work is safe; retry shortly.".into()));
    }
    Ok(pool)
}
async fn ready_db(s: &App) -> Result<&PgPool> {
    for _ in 0..50 {
        if s.db.is_none() || s.ready.load(Ordering::Acquire) {
            break;
        }
        tokio::time::sleep(std::time::Duration::from_millis(100)).await;
    }
    db(s)
}
fn text(value: &str, max: usize) -> Result<String> {
    let v = value.trim();
    if v.is_empty() || v.chars().count() > max || v.chars().any(|c| c.is_control() && c != '\n') {
        return Err(bad("Text is empty, too long, or contains control characters"));
    }
    Ok(v.to_string())
}
fn cookie(headers: &HeaderMap, name: &str) -> Option<String> {
    headers.get(header::COOKIE)?.to_str().ok()?.split(';').find_map(|c| {
        let (k, v) = c.trim().split_once('=')?;
        (k == name).then(|| v.into())
    })
}
fn session_cookie(s: &App, name: &str, value: &str, age: u32) -> Result<HeaderValue> {
    let secure = if s.origin.starts_with("https://") { "; Secure" } else { "" };
    HeaderValue::from_str(&format!("{name}={value}; Path=/; HttpOnly; SameSite=Lax; Max-Age={age}{secure}")).map_err(|_| bad("Invalid session"))
}
#[derive(Clone)]
struct Account {
    id: Uuid,
    email: String,
    name: String,
}
async fn account(s: &App, h: &HeaderMap) -> Result<Account> {
    let t = cookie(h, "pc_session").ok_or(ApiError(StatusCode::UNAUTHORIZED, "Sign in to use your cloud workspace".into()))?;
    if t.len() != 64 {
        return Err(ApiError(StatusCode::UNAUTHORIZED, "Session expired".into()));
    }
    let r = sqlx::query(
        "SELECT a.id,a.email,a.name FROM photocraft.sessions s JOIN photocraft.accounts a ON a.id=s.account_id WHERE s.hash=$1 AND s.expires_at>now()",
    )
    .bind(hash(t))
    .fetch_optional(db(s)?)
    .await?
    .ok_or(ApiError(StatusCode::UNAUTHORIZED, "Session expired. Sign in again.".into()))?;
    Ok(Account { id: r.get("id"), email: r.get("email"), name: r.get("name") })
}
async fn role(s: &App, a: &Account, id: Uuid, write: bool, owner: bool) -> Result<String> {
    let r=sqlx::query("SELECT CASE WHEN p.owner_id=$2 THEN 'owner' ELSE m.role END AS role FROM photocraft.projects p LEFT JOIN photocraft.members m ON m.project_id=p.id AND m.email=$3 WHERE p.id=$1")
        .bind(id).bind(a.id).bind(&a.email).fetch_optional(db(s)?).await?.ok_or_else(forbidden)?;
    let r = r.get::<Option<String>, _>("role").ok_or_else(forbidden)?;
    if (write && r == "view") || (owner && r != "owner") {
        return Err(forbidden());
    }
    Ok(r)
}

pub async fn application() -> std::result::Result<Router, Box<dyn std::error::Error>> {
    let origin = std::env::var("APP_ORIGIN").unwrap_or_default();
    let pool = match std::env::var("DATABASE_URL") {
        Ok(url) => {
            let mut opts = PgConnectOptions::from_str(&url)?.statement_cache_capacity(0);
            if cfg!(debug_assertions) && std::env::var("CLOUD_LOCAL_DEV").as_deref() == Ok("1") {
                opts = opts.ssl_mode(PgSslMode::Disable);
            } else {
                let ca = std::env::var("SUPABASE_CA_CERT").map_err(|_| "SUPABASE_CA_CERT is required for verified database TLS")?;
                opts = opts.ssl_mode(PgSslMode::VerifyFull).ssl_root_cert_from_pem(ca.into_bytes());
            }
            Some(PgPoolOptions::new().max_connections(5).acquire_timeout(std::time::Duration::from_secs(10)).connect_lazy_with(opts))
        }
        Err(_) => None,
    };
    let s = Arc::new(AppState {
        db: pool,
        ready: Arc::new(AtomicBool::new(false)),
        origin,
        supabase: std::env::var("SUPABASE_URL").unwrap_or_default(),
        anon: std::env::var("SUPABASE_ANON_KEY").unwrap_or_default(),
        http: reqwest::Client::builder().timeout(std::time::Duration::from_secs(15)).redirect(reqwest::redirect::Policy::none()).build()?,
    });
    if let Some(pool) = s.db.clone() {
        let ready = s.ready.clone();
        // Serve the editor immediately. A database wake-up must not take down static pages
        // or kill the process; cloud routes stay unavailable until migration commits.
        tokio::spawn(async move {
            let mut retry_seconds = 1;
            loop {
                let result: std::result::Result<(), sqlx::Error> = async {
                    let mut tx = pool.begin().await?;
                    sqlx::query("SELECT pg_advisory_xact_lock(735193624)").execute(&mut *tx).await?;
                    sqlx::Executor::execute(&mut *tx, include_str!("../migrations/001_cloud.sql")).await?;
                    tx.commit().await
                }
                .await;
                if result.is_ok() {
                    ready.store(true, Ordering::Release);
                    println!("PhotoCraft cloud storage is ready");
                    break;
                }
                // Do not log connection strings or provider error payloads.
                eprintln!("PhotoCraft cloud setup is delayed; the editor remains available. Retrying in {retry_seconds}s");
                tokio::time::sleep(std::time::Duration::from_secs(retry_seconds)).await;
                retry_seconds = (retry_seconds * 2).min(30);
            }
        });
    }
    let public = std::env::var("PUBLIC_DIR").unwrap_or_else(|_| "public".into());
    Ok(router(s)
        .fallback_service(ServeDir::new(&public).not_found_service(ServeFile::new(format!("{public}/index.html"))))
        .layer(middleware::from_fn(browser_headers))
        .layer(CompressionLayer::new()))
}
async fn browser_headers(req: axum::extract::Request, next: Next) -> Response {
    let path = req.uri().path();
    let immutable = path.starts_with("/photocraft-web-") && (path.ends_with(".wasm") || path.ends_with(".js"));
    let mut response = next.run(req).await;
    let cache = if immutable && response.status().is_success() { "public, max-age=31536000, immutable" } else { "no-cache" };
    if !response.headers().contains_key(header::CACHE_CONTROL) {
        response.headers_mut().insert(header::CACHE_CONTROL, HeaderValue::from_static(cache));
    }
    response.headers_mut().insert("x-content-type-options", HeaderValue::from_static("nosniff"));
    response.headers_mut().insert("referrer-policy", HeaderValue::from_static("no-referrer"));
    response.headers_mut().insert("x-frame-options", HeaderValue::from_static("SAMEORIGIN"));
    response
}
pub fn router(s: App) -> Router {
    Router::new()
        .route("/healthz", get(|State(s): State<App>| async move { Json(json!({"status":"ok","service":"photocraft-cloud","cloud":cloud_state(&s)})) }))
        .route("/api/config", get(config))
        .route("/api/me", get(me))
        .route("/auth/login", get(login))
        .route("/auth/callback", get(callback))
        .route("/auth/confirm", get(invitations::confirm_page).post(invitations::confirm))
        .route("/api/logout", post(logout))
        .route("/api/projects", get(projects).post(create_project))
        .route("/api/projects/{id}", get(project).patch(update_project))
        .route("/api/projects/{id}/versions", get(versions))
        .route("/api/projects/{id}/content", get(content))
        .route("/api/projects/{id}/thumbnail", get(thumbnail).put(put_thumbnail))
        .route("/api/projects/{id}/duplicate", post(duplicate))
        .route("/api/projects/{id}/uploads", post(begin_upload))
        .route("/api/uploads/{id}/{part}", put(upload_chunk))
        .route("/api/uploads/{id}", axum::routing::delete(cancel_upload))
        .route("/api/uploads/{id}/commit", post(commit_upload))
        .route("/api/projects/{id}/members", get(members).put(set_member))
        .route("/api/projects/{id}/invite", post(invitations::invite))
        .route("/api/projects/{id}/share", post(create_share).delete(revoke_shares))
        .route("/api/share/{key}", get(shared_project))
        .route("/api/share/{key}/content", get(shared_content))
        .route("/api/projects/{id}/comments", get(comments).post(add_comment))
        .route("/api/projects/{id}/comments/{comment}", put(resolve_comment))
        .route("/api/projects/{id}/presence", post(presence))
        .layer(DefaultBodyLimit::max(CHUNK + 1024))
        .layer(middleware::from_fn_with_state(s.clone(), guard))
        .with_state(s)
}
async fn guard(State(s): State<App>, req: axum::extract::Request, next: Next) -> Response {
    if !matches!(*req.method(), Method::GET | Method::HEAD | Method::OPTIONS)
        && req.headers().get(header::ORIGIN).and_then(|h| h.to_str().ok()) != Some(s.origin.as_str())
    {
        return (StatusCode::FORBIDDEN, Json(json!({"error":"Request origin does not match this workspace"}))).into_response();
    }
    let mut r = next.run(req).await;
    for (k, v) in [("cache-control", "no-store"), ("x-content-type-options", "nosniff"), ("referrer-policy", "no-referrer"), ("x-frame-options", "SAMEORIGIN")]
    {
        r.headers_mut().insert(k, HeaderValue::from_static(v));
    }
    r
}
async fn config(State(s): State<App>) -> Json<Value> {
    let ready = s.ready.load(Ordering::Acquire);
    Json(
        json!({"cloud":ready,"cloudState":cloud_state(&s),"signIn":ready&&!s.supabase.is_empty()&&!s.anon.is_empty()&&!s.origin.is_empty(),"chunkBytes":CHUNK,"maxFileBytes":MAX_FILE,"version":env!("CARGO_PKG_VERSION")}),
    )
}
fn cloud_state(s: &App) -> &'static str {
    if s.db.is_none() {
        "disabled"
    } else if s.ready.load(Ordering::Acquire) {
        "ready"
    } else {
        "starting"
    }
}
async fn me(State(s): State<App>, h: HeaderMap) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    Ok(Json(json!({"id":a.id,"name":a.name,"email":a.email})))
}
async fn login(State(s): State<App>) -> Result<Response> {
    ready_db(&s).await?;
    if !s.origin.starts_with("https://") || s.supabase.is_empty() || s.anon.is_empty() {
        return Err(bad("Cloud sign-in is not configured"));
    }
    let nonce = token();
    let mut back = url::Url::parse(&format!("{}/auth/callback", s.origin)).map_err(|_| bad("Invalid application origin"))?;
    back.query_pairs_mut().append_pair("state", &nonce);
    let mut broker = url::Url::parse("https://oauth.trytofu.ai/start").map_err(|_| bad("Invalid sign-in origin"))?;
    broker.query_pairs_mut().append_pair("return", back.as_str()).append_pair("flow", "pkce");
    let mut r = Redirect::to(broker.as_str()).into_response();
    r.headers_mut().insert(header::SET_COOKIE, session_cookie(&s, "pc_login", &nonce, 600)?);
    Ok(r)
}
async fn callback(State(s): State<App>, h: HeaderMap, Query(q): Query<std::collections::HashMap<String, String>>) -> Result<Response> {
    let nonce = cookie(&h, "pc_login").ok_or(bad("Sign-in expired. Start again."))?;
    if nonce.len() != 64 || q.get("state") != Some(&nonce) {
        return Err(bad("Sign-in state mismatch. Start again."));
    }
    let t = q.get("token_hash").filter(|v| v.len() <= 512).ok_or(bad("Missing sign-in token"))?;
    let ty = q.get("type").filter(|v| matches!(v.as_str(), "magiclink" | "email" | "signup" | "invite")).ok_or(bad("Unsupported sign-in response"))?;
    finish_sign_in(&s, t, ty).await
}
async fn finish_sign_in(s: &App, t: &str, ty: &str) -> Result<Response> {
    // Establish storage before consuming the provider's single-use sign-in token.
    let mut tx = ready_db(s).await?.begin().await?;
    let resp = s
        .http
        .post(format!("{}/auth/v1/verify", s.supabase))
        .header("apikey", &s.anon)
        .json(&json!({"token_hash":t,"type":ty}))
        .send()
        .await
        .map_err(|_| bad("Could not reach sign-in service"))?;
    if !resp.status().is_success() {
        return Err(bad("Sign-in link expired or already used. Start again."));
    }
    let session: Value = resp.json().await.map_err(|_| bad("Invalid sign-in response"))?;
    let access = session.get("access_token").and_then(Value::as_str).ok_or(bad("No authenticated session returned"))?;
    let user = s
        .http
        .get(format!("{}/auth/v1/user", s.supabase))
        .header("apikey", &s.anon)
        .bearer_auth(access)
        .send()
        .await
        .map_err(|_| bad("Could not verify account"))?;
    if !user.status().is_success() {
        return Err(bad("Account verification failed"));
    }
    let u: Value = user.json().await.map_err(|_| bad("Invalid account response"))?;
    if u.get("email_confirmed_at").and_then(Value::as_str).is_none_or(|v| v.is_empty()) {
        return Err(bad("Verify your email before joining a shared workspace"));
    }
    let id = u.get("id").and_then(Value::as_str).and_then(|v| Uuid::parse_str(v).ok()).ok_or(bad("Missing account identity"))?;
    let email = text(u.get("email").and_then(Value::as_str).ok_or(bad("An email address is required"))?, 320)?.to_lowercase();
    let name = text(u.pointer("/user_metadata/full_name").and_then(Value::as_str).unwrap_or(&email), 160)?;
    sqlx::query("INSERT INTO photocraft.accounts(id,email,name) VALUES($1,$2,$3) ON CONFLICT(id) DO UPDATE SET email=EXCLUDED.email,name=EXCLUDED.name")
        .bind(id)
        .bind(email)
        .bind(name)
        .execute(&mut *tx)
        .await?;
    let t = token();
    sqlx::query("INSERT INTO photocraft.sessions(hash,account_id) VALUES($1,$2)").bind(hash(&t)).bind(id).execute(&mut *tx).await?;
    sqlx::query("DELETE FROM photocraft.sessions WHERE expires_at<now()").execute(&mut *tx).await?;
    tx.commit().await?;
    let mut r = Redirect::to("/").into_response();
    r.headers_mut().append(header::SET_COOKIE, session_cookie(s, "pc_session", &t, 604800)?);
    r.headers_mut().append(header::SET_COOKIE, session_cookie(s, "pc_login", "", 0)?);
    Ok(r)
}
async fn logout(State(s): State<App>, h: HeaderMap) -> Result<Response> {
    if let Some(t) = cookie(&h, "pc_session") {
        sqlx::query("DELETE FROM photocraft.sessions WHERE hash=$1").bind(hash(t)).execute(db(&s)?).await?;
    }
    let mut r = Json(json!({"ok":true})).into_response();
    r.headers_mut().insert(header::SET_COOKIE, session_cookie(&s, "pc_session", "", 0)?);
    Ok(r)
}

const PROJECT_FIELDS: &str = "json_build_object('id',p.id,'title',p.title,'folder',p.folder,'starred',p.starred,'trashed',p.trashed,'revision',p.revision,'width',p.width,'height',p.height,'updatedAt',p.updated_at,'role',CASE WHEN p.owner_id=$2 THEN 'owner' ELSE m.role END)";
async fn projects(State(s): State<App>, h: HeaderMap) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    let q = format!(
        "SELECT {PROJECT_FIELDS} AS v FROM photocraft.projects p LEFT JOIN photocraft.members m ON m.project_id=p.id AND m.email=$1 WHERE p.owner_id=$2 OR m.email=$1 ORDER BY p.updated_at DESC LIMIT 500"
    );
    let rows = sqlx::query(&q).bind(&a.email).bind(a.id).fetch_all(db(&s)?).await?;
    Ok(Json(json!(rows.iter().map(|r| r.get::<Value, _>("v")).collect::<Vec<_>>())))
}
#[derive(Deserialize)]
struct NewProject {
    title: String,
}
async fn create_project(State(s): State<App>, h: HeaderMap, Json(v): Json<NewProject>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    let title = text(&v.title, 160)?;
    let id = Uuid::new_v4();
    let mut tx = db(&s)?.begin().await?;
    sqlx::query("SELECT id FROM photocraft.accounts WHERE id=$1 FOR UPDATE").bind(a.id).execute(&mut *tx).await?;
    let n: i64 = sqlx::query_scalar("SELECT count(*) FROM photocraft.projects WHERE owner_id=$1").bind(a.id).fetch_one(&mut *tx).await?;
    if n >= 500 {
        return Err(bad("Workspace limit: 500 projects"));
    }
    sqlx::query("INSERT INTO photocraft.projects(id,owner_id,title) VALUES($1,$2,$3)").bind(id).bind(a.id).bind(&title).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Json(json!({"id":id,"title":title,"revision":0,"role":"owner"})))
}
async fn project(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    let r = role(&s, &a, id, false, false).await?;
    let p = sqlx::query("SELECT title,revision,width,height FROM photocraft.projects WHERE id=$1").bind(id).fetch_one(db(&s)?).await?;
    let rev: i64 = p.get("revision");
    let content = version_meta(db(&s)?, id, rev).await?;
    Ok(Json(
        json!({"id":id,"title":p.get::<String,_>("title"),"revision":rev,"width":p.get::<i32,_>("width"),"height":p.get::<i32,_>("height"),"role":r,"content":content}),
    ))
}
#[derive(Deserialize)]
struct UpdateProject {
    title: Option<String>,
    folder: Option<String>,
    starred: Option<bool>,
    trashed: Option<bool>,
}
async fn update_project(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>, Json(v): Json<UpdateProject>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, true, true).await?;
    let title = v.title.as_deref().map(|v| text(v, 160)).transpose()?;
    let folder = v.folder.map(|f| if f.is_empty() { Ok(f) } else { text(&f, 100) }).transpose()?;
    sqlx::query("UPDATE photocraft.projects SET title=coalesce($2,title),folder=coalesce($3,folder),starred=coalesce($4,starred),trashed=coalesce($5,trashed),updated_at=now() WHERE id=$1").bind(id).bind(title).bind(folder).bind(v.starred).bind(v.trashed).execute(db(&s)?).await?;
    Ok(Json(json!({"ok":true})))
}
async fn versions(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, false, false).await?;
    let rows=sqlx::query("SELECT json_build_object('revision',v.revision,'title',v.title,'author',a.name,'createdAt',v.created_at,'bytes',octet_length(v.data),'sha256',v.sha256) AS v FROM photocraft.versions v JOIN photocraft.accounts a ON a.id=v.author_id WHERE v.project_id=$1 ORDER BY v.revision DESC LIMIT 200").bind(id).fetch_all(db(&s)?).await?;
    Ok(Json(json!(rows.iter().map(|r| r.get::<Value, _>("v")).collect::<Vec<_>>())))
}
async fn version_meta(pool: &PgPool, id: Uuid, rev: i64) -> Result<Value> {
    let r = sqlx::query("SELECT octet_length(data) AS bytes,sha256 FROM photocraft.versions WHERE project_id=$1 AND revision=$2")
        .bind(id)
        .bind(rev)
        .fetch_optional(pool)
        .await?;
    Ok(r.map(|r| json!({"bytes":r.get::<i32,_>("bytes"),"sha256":r.get::<String,_>("sha256")})).unwrap_or(Value::Null))
}
#[derive(Deserialize)]
struct ContentQuery {
    revision: Option<i64>,
    part: Option<usize>,
}
async fn file_part(pool: &PgPool, id: Uuid, q: ContentQuery) -> Result<Response> {
    let part = q.part.unwrap_or(0);
    if part >= 200 {
        return Err(bad("Invalid chunk number"));
    }
    let rev =
        if let Some(r) = q.revision { r } else { sqlx::query_scalar("SELECT revision FROM photocraft.projects WHERE id=$1").bind(id).fetch_one(pool).await? };
    let r =
        sqlx::query("SELECT substring(data from $3 for $4) AS data,octet_length(data) AS bytes FROM photocraft.versions WHERE project_id=$1 AND revision=$2")
            .bind(id)
            .bind(rev)
            .bind((part * CHUNK + 1) as i32)
            .bind(CHUNK as i32)
            .fetch_optional(pool)
            .await?
            .ok_or_else(forbidden)?;
    if part * CHUNK >= r.get::<i32, _>("bytes") as usize {
        return Err(bad("Chunk is beyond the file"));
    }
    Ok(([(header::CONTENT_TYPE, "application/octet-stream")], r.get::<Vec<u8>, _>("data")).into_response())
}
async fn content(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>, Query(q): Query<ContentQuery>) -> Result<Response> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, false, false).await?;
    file_part(db(&s)?, id, q).await
}
async fn thumbnail(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Response> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, false, false).await?;
    let v: Option<Vec<u8>> = sqlx::query_scalar("SELECT thumbnail FROM photocraft.projects WHERE id=$1").bind(id).fetch_one(db(&s)?).await?;
    Ok(([(header::CONTENT_TYPE, "image/png")], v.ok_or_else(forbidden)?).into_response())
}
async fn put_thumbnail(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>, b: Bytes) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, true, false).await?;
    let dimensions = b.get(16..24).and_then(|v| Some((u32::from_be_bytes(v.get(..4)?.try_into().ok()?), u32::from_be_bytes(v.get(4..)?.try_into().ok()?))));
    if b.len() > 128 * 1024
        || !b.starts_with(b"\x89PNG\r\n\x1a\n")
        || b.get(12..16) != Some(b"IHDR")
        || !dimensions.is_some_and(|(w, h)| w > 0 && h > 0 && w <= 512 && h <= 512)
    {
        return Err(bad("Invalid PNG preview"));
    }
    sqlx::query("UPDATE photocraft.projects SET thumbnail=$2 WHERE id=$1").bind(id).bind(b.to_vec()).execute(db(&s)?).await?;
    Ok(Json(json!({"ok":true})))
}
async fn duplicate(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, false, false).await?;
    let new = Uuid::new_v4();
    let mut tx = db(&s)?.begin().await?;
    sqlx::query("SELECT id FROM photocraft.accounts WHERE id=$1 FOR UPDATE").bind(a.id).execute(&mut *tx).await?;
    let count: i64 = sqlx::query_scalar("SELECT count(*) FROM photocraft.projects WHERE owner_id=$1").bind(a.id).fetch_one(&mut *tx).await?;
    if count >= 500 {
        return Err(bad("Workspace limit: 500 projects"));
    }
    let used: i64 = sqlx::query_scalar(
        "SELECT coalesce(sum(octet_length(v.data)),0)::bigint FROM photocraft.versions v JOIN photocraft.projects p ON p.id=v.project_id WHERE p.owner_id=$1",
    )
    .bind(a.id)
    .fetch_one(&mut *tx)
    .await?;
    let size: Option<i32> = sqlx::query_scalar(
        "SELECT octet_length(v.data) FROM photocraft.versions v JOIN photocraft.projects p ON p.id=v.project_id AND p.revision=v.revision WHERE p.id=$1",
    )
    .bind(id)
    .fetch_optional(&mut *tx)
    .await?;
    if used + i64::from(size.unwrap_or(0)) > ACCOUNT_QUOTA {
        return Err(bad("Workspace storage limit reached"));
    }
    sqlx::query("INSERT INTO photocraft.projects(id,owner_id,title,revision,width,height,thumbnail) SELECT $2,$3,left(title,153)||' (copy)',CASE WHEN revision>0 THEN 1 ELSE 0 END,width,height,thumbnail FROM photocraft.projects WHERE id=$1").bind(id).bind(new).bind(a.id).execute(&mut *tx).await?;
    sqlx::query("INSERT INTO photocraft.versions(project_id,revision,author_id,title,data,sha256) SELECT $2,1,$3,'Created a copy',v.data,v.sha256 FROM photocraft.versions v JOIN photocraft.projects p ON p.id=v.project_id AND p.revision=v.revision WHERE p.id=$1").bind(id).bind(new).bind(a.id).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Json(json!({"id":new})))
}
#[derive(Deserialize)]
struct Upload {
    base_revision: i64,
    bytes: usize,
    parts: usize,
    sha256: String,
    title: String,
    width: i32,
    height: i32,
}
async fn begin_upload(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>, Json(v): Json<Upload>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, true, false).await?;
    if v.bytes == 0
        || v.bytes > MAX_FILE
        || v.parts != v.bytes.div_ceil(CHUNK)
        || v.sha256.len() != 64
        || !v.sha256.bytes().all(|b| b.is_ascii_hexdigit())
        || v.width < 1
        || v.height < 1
        || v.width > 16384
        || v.height > 16384
        || i64::from(v.width) * i64::from(v.height) > 64_000_000
    {
        return Err(bad("Invalid upload size, digest, or dimensions"));
    }
    let title = text(&v.title, 160)?;
    let p = db(&s)?;
    let mut tx = p.begin().await?;
    // Serialize reservation counts across processes; parallel tabs cannot bypass the limit.
    sqlx::query("SELECT pg_advisory_xact_lock(hashtextextended($1,0))").bind(format!("photocraft.uploads/{}", a.id)).execute(&mut *tx).await?;
    sqlx::query("DELETE FROM photocraft.uploads WHERE author_id=$1 AND created_at<now()-interval '1 hour'").bind(a.id).execute(&mut *tx).await?;
    let n: i64 = sqlx::query_scalar("SELECT count(*) FROM photocraft.uploads WHERE author_id=$1").bind(a.id).fetch_one(&mut *tx).await?;
    if n >= 5 {
        return Err(bad("Too many pending uploads; finish one or retry in an hour"));
    }
    let next = Uuid::new_v4();
    sqlx::query(
        "INSERT INTO photocraft.uploads(id,project_id,author_id,base_revision,bytes,parts,sha256,title,width,height) VALUES($1,$2,$3,$4,$5,$6,$7,$8,$9,$10)",
    )
    .bind(next)
    .bind(id)
    .bind(a.id)
    .bind(v.base_revision)
    .bind(v.bytes as i64)
    .bind(v.parts as i32)
    .bind(v.sha256.to_lowercase())
    .bind(title)
    .bind(v.width)
    .bind(v.height)
    .execute(&mut *tx)
    .await?;
    tx.commit().await?;
    Ok(Json(json!({"id":next})))
}
async fn upload_chunk(State(s): State<App>, h: HeaderMap, Path((id, part)): Path<(Uuid, i32)>, b: Bytes) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    let r = sqlx::query("SELECT project_id,parts,bytes FROM photocraft.uploads WHERE id=$1 AND author_id=$2 AND created_at>now()-interval '1 hour'")
        .bind(id)
        .bind(a.id)
        .fetch_optional(db(&s)?)
        .await?
        .ok_or_else(forbidden)?;
    role(&s, &a, r.get("project_id"), true, false).await?;
    let parts: i32 = r.get("parts");
    let size: i64 = r.get("bytes");
    if part < 0 || part >= parts || b.len() != if part == parts - 1 { size as usize - (part as usize) * CHUNK } else { CHUNK } {
        return Err(bad("Chunk does not match the declared upload"));
    }
    sqlx::query("INSERT INTO photocraft.chunks(upload_id,part,data) VALUES($1,$2,$3) ON CONFLICT(upload_id,part) DO UPDATE SET data=EXCLUDED.data")
        .bind(id)
        .bind(part)
        .bind(b.to_vec())
        .execute(db(&s)?)
        .await?;
    Ok(Json(json!({"ok":true})))
}
async fn commit_upload(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    let p = db(&s)?;
    let mut tx = p.begin().await?;
    let u = sqlx::query("SELECT * FROM photocraft.uploads WHERE id=$1 AND author_id=$2 AND created_at>now()-interval '1 hour' FOR UPDATE")
        .bind(id)
        .bind(a.id)
        .fetch_optional(&mut *tx)
        .await?
        .ok_or_else(forbidden)?;
    let project: Uuid = u.get("project_id");
    // Use this transaction's connection: concurrent commits must not exhaust the pool while
    // each waits for a second connection to authorize its own upload.
    let can_write:bool=sqlx::query_scalar("SELECT EXISTS(SELECT 1 FROM photocraft.projects p LEFT JOIN photocraft.members m ON m.project_id=p.id AND m.email=$3 WHERE p.id=$1 AND (p.owner_id=$2 OR m.role='edit'))").bind(project).bind(a.id).bind(&a.email).fetch_one(&mut *tx).await?;
    if !can_write {
        return Err(forbidden());
    }
    let pr = sqlx::query("SELECT revision,owner_id,trashed FROM photocraft.projects WHERE id=$1 FOR UPDATE").bind(project).fetch_one(&mut *tx).await?;
    sqlx::query("SELECT id FROM photocraft.accounts WHERE id=$1 FOR UPDATE").bind(pr.get::<Uuid, _>("owner_id")).execute(&mut *tx).await?;
    if pr.get::<bool, _>("trashed") {
        return Err(bad("Restore this project from Trash before saving"));
    }
    let revision: i64 = pr.get("revision");
    let rows = sqlx::query("SELECT part,data FROM photocraft.chunks WHERE upload_id=$1 ORDER BY part").bind(id).fetch_all(&mut *tx).await?;
    if rows.len() != u.get::<i32, _>("parts") as usize {
        return Err(bad("Upload is incomplete; retry the missing chunks"));
    }
    let mut bytes = Vec::with_capacity(u.get::<i64, _>("bytes") as usize);
    for r in rows {
        bytes.extend(r.get::<Vec<u8>, _>("data"));
    }
    if bytes.len() != u.get::<i64, _>("bytes") as usize || hash(&bytes) != u.get::<String, _>("sha256") {
        return Err(bad("Upload checksum does not match; retry saving"));
    }
    let manifest = photocraft_format::read_manifest(&bytes).map_err(|_| bad("Cloud saves must be valid PhotoCraft .pcraft documents"))?;
    if manifest.document.size.width as i32 != u.get::<i32, _>("width") || manifest.document.size.height as i32 != u.get::<i32, _>("height") {
        return Err(bad("Saved document dimensions do not match its upload"));
    }
    let merged = revision != u.get::<i64, _>("base_revision");
    if merged {
        let base: Option<Vec<u8>> = sqlx::query_scalar("SELECT data FROM photocraft.versions WHERE project_id=$1 AND revision=$2")
            .bind(project)
            .bind(u.get::<i64, _>("base_revision"))
            .fetch_optional(&mut *tx)
            .await?;
        let latest: Option<Vec<u8>> = sqlx::query_scalar("SELECT data FROM photocraft.versions WHERE project_id=$1 AND revision=$2")
            .bind(project)
            .bind(revision)
            .fetch_optional(&mut *tx)
            .await?;
        bytes = base.zip(latest).and_then(|(base, latest)| merge::documents(&base, &bytes, &latest, MAX_FILE)).ok_or(ApiError(
            StatusCode::CONFLICT,
            "You and a collaborator changed the same content. Your edits are safe here. Save a copy, or open the latest version to compare.".into(),
        ))?;
    }
    let checksum = hash(&bytes);
    let used: i64 = sqlx::query_scalar(
        "SELECT coalesce(sum(octet_length(v.data)),0)::bigint FROM photocraft.versions v JOIN photocraft.projects p ON p.id=v.project_id WHERE p.owner_id=$1",
    )
    .bind(pr.get::<Uuid, _>("owner_id"))
    .fetch_one(&mut *tx)
    .await?;
    if used + bytes.len() as i64 > ACCOUNT_QUOTA {
        return Err(bad("Workspace storage limit (1 GB including history) reached. Download your work."));
    }
    let next = revision + 1;
    sqlx::query("INSERT INTO photocraft.versions(project_id,revision,author_id,title,data,sha256) VALUES($1,$2,$3,$4,$5,$6)")
        .bind(project)
        .bind(next)
        .bind(a.id)
        .bind(u.get::<String, _>("title"))
        .bind(bytes)
        .bind(checksum)
        .execute(&mut *tx)
        .await?;
    sqlx::query(
        "UPDATE photocraft.projects SET revision=$2,width=$3,height=$4,thumbnail=CASE WHEN $5 THEN NULL ELSE thumbnail END,updated_at=now() WHERE id=$1",
    )
    .bind(project)
    .bind(next)
    .bind(u.get::<i32, _>("width"))
    .bind(u.get::<i32, _>("height"))
    .bind(merged)
    .execute(&mut *tx)
    .await?;
    sqlx::query("DELETE FROM photocraft.uploads WHERE id=$1").bind(id).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Json(json!({"revision":next,"merged":merged})))
}
async fn cancel_upload(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    let deleted = sqlx::query("DELETE FROM photocraft.uploads WHERE id=$1 AND author_id=$2").bind(id).bind(a.id).execute(db(&s)?).await?.rows_affected();
    if deleted == 0 {
        return Err(forbidden());
    }
    Ok(Json(json!({"ok":true})))
}
async fn members(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, false, true).await?;
    let rows = sqlx::query("SELECT m.email,m.role,EXISTS(SELECT 1 FROM photocraft.accounts a WHERE a.email=m.email) AS joined,(SELECT status FROM photocraft.invitation_deliveries d WHERE d.project_id=m.project_id AND d.email=m.email ORDER BY created_at DESC LIMIT 1) AS delivery FROM photocraft.members m WHERE m.project_id=$1 ORDER BY m.email").bind(id).fetch_all(db(&s)?).await?;
    Ok(Json(json!(rows.iter().map(|r| json!({"email":r.get::<String,_>("email"),"role":r.get::<String,_>("role"),"joined":r.get::<bool,_>("joined"),"delivery":r.get::<Option<String>,_>("delivery")})).collect::<Vec<_>>())))
}
#[derive(Deserialize)]
struct Member {
    email: String,
    role: String,
}
async fn set_member(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>, Json(v): Json<Member>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, true, true).await?;
    let email = text(&v.email, 320)?.to_lowercase();
    if !email.contains('@') || email.contains(char::is_whitespace) || !matches!(v.role.as_str(), "view" | "edit" | "remove") {
        return Err(bad("Enter a valid email and access role"));
    }
    if v.role == "remove" {
        sqlx::query("DELETE FROM photocraft.members WHERE project_id=$1 AND email=$2").bind(id).bind(email).execute(db(&s)?).await?;
    } else {
        sqlx::query("INSERT INTO photocraft.members(project_id,email,role) VALUES($1,$2,$3) ON CONFLICT(project_id,email) DO UPDATE SET role=EXCLUDED.role")
            .bind(id)
            .bind(email)
            .bind(v.role)
            .execute(db(&s)?)
            .await?;
    }
    Ok(Json(json!({"ok":true})))
}
async fn create_share(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, true, true).await?;
    let t = token();
    let mut tx = db(&s)?.begin().await?;
    sqlx::query("DELETE FROM photocraft.shares WHERE project_id=$1").bind(id).execute(&mut *tx).await?;
    sqlx::query("INSERT INTO photocraft.shares(hash,project_id) VALUES($1,$2)").bind(hash(&t)).bind(id).execute(&mut *tx).await?;
    tx.commit().await?;
    Ok(Json(json!({"url":format!("{}/?share={t}",s.origin)})))
}
async fn revoke_shares(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, true, true).await?;
    sqlx::query("DELETE FROM photocraft.shares WHERE project_id=$1").bind(id).execute(db(&s)?).await?;
    Ok(Json(json!({"ok":true})))
}
async fn shared_id(s: &App, key: &str) -> Result<Uuid> {
    if key.len() != 64 {
        return Err(forbidden());
    }
    sqlx::query_scalar("SELECT p.id FROM photocraft.shares s JOIN photocraft.projects p ON p.id=s.project_id WHERE s.hash=$1 AND NOT p.trashed")
        .bind(hash(key))
        .fetch_optional(db(s)?)
        .await?
        .ok_or_else(forbidden)
}
async fn shared_project(State(s): State<App>, Path(key): Path<String>) -> Result<Json<Value>> {
    let id = shared_id(&s, &key).await?;
    let r = sqlx::query("SELECT title,revision,width,height FROM photocraft.projects WHERE id=$1").bind(id).fetch_one(db(&s)?).await?;
    let revision: i64 = r.get("revision");
    Ok(Json(
        json!({"id":id,"title":r.get::<String,_>("title"),"revision":revision,"width":r.get::<i32,_>("width"),"height":r.get::<i32,_>("height"),"role":"view","content":version_meta(db(&s)?,id,revision).await?}),
    ))
}
async fn shared_content(State(s): State<App>, Path(key): Path<String>, Query(mut q): Query<ContentQuery>) -> Result<Response> {
    let id = shared_id(&s, &key).await?;
    q.revision = None;
    file_part(db(&s)?, id, q).await
}
async fn comments(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, false, false).await?;
    let rows=sqlx::query("SELECT json_build_object('id',c.id,'body',c.body,'resolved',c.resolved,'author',a.name,'createdAt',c.created_at) AS v FROM photocraft.comments c JOIN photocraft.accounts a ON a.id=c.author_id WHERE project_id=$1 ORDER BY c.created_at DESC LIMIT 200").bind(id).fetch_all(db(&s)?).await?;
    Ok(Json(json!(rows.iter().map(|r| r.get::<Value, _>("v")).collect::<Vec<_>>())))
}
#[derive(Deserialize)]
struct Comment {
    body: String,
}
async fn add_comment(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>, Json(v): Json<Comment>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, false, false).await?;
    let body = text(&v.body, 4000)?;
    let n: i64 = sqlx::query_scalar("SELECT count(*) FROM photocraft.comments WHERE project_id=$1").bind(id).fetch_one(db(&s)?).await?;
    if n >= 1000 {
        return Err(bad("This project has reached 1,000 comments"));
    }
    let cid = Uuid::new_v4();
    sqlx::query("INSERT INTO photocraft.comments(id,project_id,author_id,body) VALUES($1,$2,$3,$4)")
        .bind(cid)
        .bind(id)
        .bind(a.id)
        .bind(body)
        .execute(db(&s)?)
        .await?;
    Ok(Json(json!({"id":cid})))
}
#[derive(Deserialize)]
struct Resolve {
    resolved: bool,
}
async fn resolve_comment(State(s): State<App>, h: HeaderMap, Path((id, cid)): Path<(Uuid, Uuid)>, Json(v): Json<Resolve>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    role(&s, &a, id, true, false).await?;
    let n = sqlx::query("UPDATE photocraft.comments SET resolved=$3 WHERE project_id=$1 AND id=$2")
        .bind(id)
        .bind(cid)
        .bind(v.resolved)
        .execute(db(&s)?)
        .await?
        .rows_affected();
    if n != 1 {
        return Err(forbidden());
    }
    Ok(Json(json!({"ok":true})))
}
async fn presence(State(s): State<App>, h: HeaderMap, Path(id): Path<Uuid>) -> Result<Json<Value>> {
    let a = account(&s, &h).await?;
    let access = role(&s, &a, id, false, false).await?;
    sqlx::query("INSERT INTO photocraft.presence(project_id,account_id) VALUES($1,$2) ON CONFLICT(project_id,account_id) DO UPDATE SET seen_at=now()")
        .bind(id)
        .bind(a.id)
        .execute(db(&s)?)
        .await?;
    let rows=sqlx::query("SELECT a.name FROM photocraft.presence p JOIN photocraft.accounts a ON a.id=p.account_id WHERE p.project_id=$1 AND p.seen_at>now()-interval '30 seconds' ORDER BY a.name LIMIT 50").bind(id).fetch_all(db(&s)?).await?;
    let revision: i64 = sqlx::query_scalar("SELECT revision FROM photocraft.projects WHERE id=$1").bind(id).fetch_one(db(&s)?).await?;
    Ok(Json(json!({"people":rows.iter().map(|r|r.get::<String,_>("name")).collect::<Vec<_>>(),"revision":revision,"role":access})))
}

#[cfg(test)]
mod tests {
    use super::*;
    #[test]
    fn bounds_and_unicode() {
        assert_eq!(text("  Café  ", 5).ok().as_deref(), Some("Café"));
        assert!(text("", 100).is_err());
        assert!(text("abcdef", 5).is_err());
        assert!(text("a\0b", 100).is_err());
    }
    #[test]
    fn cookie_exact_name() {
        let mut h = HeaderMap::new();
        h.insert(header::COOKIE, HeaderValue::from_static("other=1; pc_session=abc; pc_session_fake=xyz"));
        assert_eq!(cookie(&h, "pc_session").as_deref(), Some("abc"));
        assert!(cookie(&h, "missing").is_none());
    }
    #[test]
    fn capabilities_have_entropy() {
        let a = token();
        let b = token();
        assert_eq!(a.len(), 64);
        assert_ne!(a, b);
        assert_eq!(hash("abc"), "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad");
    }
}
