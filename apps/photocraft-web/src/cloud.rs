//! Cloud workspace around the unmodified editor. All document operations use the Rust engine.
use super::home;
use eframe::App as _;
use egui::{ColorImage, RichText, TextureHandle, Vec2};
use gloo_net::http::{Request, RequestBuilder};
use photocraft_doc::DocId;
use photocraft_ui_egui::{
    PhotocraftApp,
    theme::{ThemeKind, Tokens},
};
use serde_json::{Value, json};
use sha2::{Digest, Sha256};
use std::{
    cell::{Cell, RefCell},
    collections::{HashMap, HashSet},
    rc::Rc,
};
use wasm_bindgen::JsValue;

#[path = "live.rs"]
mod live;

const CHUNK: usize = 524_288;
type Queue = Rc<RefCell<Vec<(u64, Message)>>>;
enum Message {
    CloudResult(u64, Box<Message>),
    Quiet,
    SessionExpired(u64),
    SessionChecked(u64, Result<(Option<Value>, Option<SessionAccess>), String>),
    Connection(Result<(Value, Option<Value>), String>),
    List(u64, Result<Value, String>, Option<(u64, String)>),
    ProjectChanged(Result<(), String>, u64, String),
    RecoverySaved(u64, (DocId, u64), Result<bool, String>),
    Invited { request: ProjectRequest, email: String, outcome: Result<(), String>, members: Result<Value, String> },
    ProjectData(ProjectRequest, Result<Value, String>),
    Opened(u64, Result<(Value, Vec<u8>), String>),
    Saved(DocumentRequest, u64, String, i64, bool),
    Synced(DocumentRequest, u64, Value, Vec<u8>),
    Presence(String, Value),
    Created(DocumentRequest, String),
    CommitAttempt(DocumentRequest, UncertainCommit),
    CommitReconciled(DocumentRequest, String, u64, i64),
    DocumentError(DocumentRequest, String),
    SignedOut(Result<(), String>),
    SignInReady(u64, Option<(DocId, u64)>, Result<(), String>),
    Preview(String, Vec<u8>),
    Error(String),
    Drafts(u64, String, Result<Vec<(String, Value)>, String>),
    Recovered(String, Vec<u8>),
    Template(String, Vec<u8>),
}
struct SessionAccess {
    projects: Value,
    roles: HashMap<String, String>,
}
#[derive(Clone)]
struct ProjectRequest {
    project: String,
    kind: &'static str,
    generation: u64,
}
impl ProjectRequest {
    fn channel(&self) -> &'static str {
        match self.kind {
            "comment_posted" => "comments",
            "share_revoked" => "share",
            kind => kind,
        }
    }
}
#[derive(Clone)]
struct UncertainCommit {
    project: String,
    base: i64,
    local: u64,
    sha256: String,
}
#[derive(Clone, Copy, PartialEq, Eq)]
struct DocumentRequest {
    document: DocId,
    generation: u64,
    auth_generation: u64,
}
struct RecoveryWrite {
    generation: u64,
    current: Rc<Cell<u64>>,
    allowed: Rc<Cell<bool>>,
    saved_at: f64,
}
impl RecoveryWrite {
    fn is_current(&self) -> bool {
        self.allowed.get() && self.generation == self.current.get()
    }
}
#[derive(Clone)]
struct Binding {
    id: String,
    revision: i64,
    saved_local: u64,
    role: String,
}
impl Binding {
    fn can_edit(&self) -> bool {
        matches!(self.role.as_str(), "owner" | "edit")
    }
}
pub struct Cloud {
    live: live::Live,
    queue: Queue,
    epoch: u64,
    auth_generation: Rc<Cell<u64>>,
    auth_allowed: Rc<Cell<bool>>,
    session_warning: Option<String>,
    session_check_pending: bool,
    pub home: bool,
    configured: bool,
    booted: bool,
    initial_navigation_done: bool,
    connection_pending: bool,
    connection_error_sequence: Option<u64>,
    sign_in: bool,
    user: Option<Value>,
    projects: Vec<Value>,
    list_generation: u64,
    bindings: HashMap<DocId, Binding>,
    uncertain_commits: HashMap<DocId, Vec<UncertainCommit>>,
    next_document_request: u64,
    pending_document: Option<DocumentRequest>,
    deferred_save: Option<Message>,
    next_open_request: u64,
    pending_open: Option<(u64, String, Option<i64>)>,
    textures: HashMap<String, TextureHandle>,
    search: String,
    filter: String,
    template_category: String,
    status: String,
    error: bool,
    error_sequence: u64,
    busy: bool,
    last_change: f64,
    observed: Option<(DocId, u64)>,
    last_poll: f64,
    last_draft: Option<(DocId, u64)>,
    draft_scope: String,
    draft_generation: Rc<Cell<u64>>,
    draft_pending: Option<u64>,
    draft_visit: bool,
    draft_activity: f64,
    draft_list_generation: u64,
    draft_retry_at: f64,
    draft_write_warning: Option<String>,
    draft_list_warning: Option<String>,
    drafts: Vec<(String, Value)>,
    show_share: bool,
    show_history: bool,
    show_comments: bool,
    member_email: String,
    member_role: String,
    invitation_pending: Option<String>,
    invitation_result: Option<(String, String, bool)>,
    dialog_project: Option<String>,
    next_project_request: u64,
    project_requests: HashMap<&'static str, u64>,
    project_pending: HashMap<&'static str, u64>,
    member_writes: HashMap<String, u64>,
    project_feedback: HashMap<&'static str, (String, bool)>,
    share_url: String,
    members: Vec<Value>,
    history: Vec<Value>,
    comments: Vec<Value>,
    comment: String,
    people: Vec<String>,
    rename: String,
    folder: String,
    show_details: bool,
    details_id: String,
    newer: bool,
    show_logout: bool,
    logout_pending: bool,
    draft_allowed: Rc<Cell<bool>>,
    compact_panels: bool,
}
fn now() -> f64 {
    js_sys::Date::now()
}
fn js_error(e: impl std::fmt::Display) -> String {
    e.to_string()
}
fn field<'a>(v: &'a Value, k: &str) -> &'a str {
    v.get(k).and_then(Value::as_str).unwrap_or("")
}
fn arr(v: Value) -> Vec<Value> {
    v.as_array().cloned().unwrap_or_default()
}
fn request(method: &str, path: &str) -> RequestBuilder {
    match method {
        "POST" => Request::post(path),
        "PUT" => Request::put(path),
        "PATCH" => Request::patch(path),
        "DELETE" => Request::delete(path),
        _ => Request::get(path),
    }
}
fn account_request(method: &str, path: &str, account: Option<&str>) -> RequestBuilder {
    let request = request(method, path);
    // The server checks this against the cookie on every private request, including bytes.
    // Another browser tab changing the shared cookie cannot upload this account's work.
    if let Some(account) = account { request.header("X-Photocraft-Account", account) } else { request }
}
// Keep authentication status typed until the scoped HTTP client has handled it.
// Error copy is presentation only, never an authentication signal.
#[derive(Debug)]
enum HttpFailure {
    Unauthorized,
    Http(u16, String),
    Other(String),
}
impl std::fmt::Display for HttpFailure {
    fn fmt(&self, formatter: &mut std::fmt::Formatter<'_>) -> std::fmt::Result {
        match self {
            Self::Unauthorized => formatter.write_str("Sign in again to resume cloud saving and sharing."),
            Self::Http(_, message) | Self::Other(message) => formatter.write_str(message),
        }
    }
}
impl From<String> for HttpFailure {
    fn from(message: String) -> Self {
        Self::Other(message)
    }
}
#[derive(Clone)]
struct CloudHttp {
    queue: Queue,
    epoch: u64,
    ctx: egui::Context,
    generation: u64,
    current: Rc<Cell<u64>>,
    allowed: Rc<Cell<bool>>,
    account: Option<String>,
}
impl CloudHttp {
    fn ready(&self) -> Result<(), String> {
        if self.generation != self.current.get() || (self.account.is_some() && !self.allowed.get()) {
            return Err("Cloud request paused until your account is verified.".into());
        }
        Ok(())
    }
    fn finish<T>(&self, result: Result<T, HttpFailure>) -> Result<T, String> {
        if matches!(&result, Err(HttpFailure::Unauthorized)) && self.account.is_some() && self.generation == self.current.get() && self.allowed.replace(false) {
            self.queue.borrow_mut().push((self.epoch, Message::SessionExpired(self.generation)));
            self.ctx.request_repaint();
        }
        result.map_err(|error| error.to_string())
    }
    async fn api(&self, method: &str, path: &str, body: Option<Value>) -> Result<Value, String> {
        self.ready()?;
        self.finish(api(method, path, body, self.account.as_deref()).await)
    }
    async fn binary(&self, method: &str, path: &str, body: Option<&[u8]>) -> Result<Vec<u8>, String> {
        self.ready()?;
        self.finish(binary(method, path, body, self.account.as_deref()).await)
    }
}
fn cloud_task<F: std::future::Future<Output = Result<Message, String>> + 'static>(http: CloudHttp, run: impl FnOnce(CloudHttp) -> F + 'static) {
    let generation = http.generation;
    task(&http.queue.clone(), http.epoch, &http.ctx.clone(), async move {
        Ok(Message::CloudResult(generation, Box::new(run(http).await.unwrap_or_else(Message::Error))))
    });
}
async fn api(method: &str, path: &str, body: Option<Value>, account: Option<&str>) -> Result<Value, HttpFailure> {
    let req = account_request(method, path, account);
    let res = if let Some(v) = body { req.json(&v).map_err(js_error)?.send().await } else { req.send().await }
        .map_err(|_| "Network unavailable. Your document stays open; retry when connected.".to_string())?;
    let status = res.status();
    if status == 401 {
        let _ = res.binary().await;
        return Err(HttpFailure::Unauthorized);
    }
    let json = res.json::<Value>().await.map_err(|_| "The server returned an invalid response".to_string())?;
    if !(200..300).contains(&status) {
        return Err(HttpFailure::Http(status, json.get("error").and_then(Value::as_str).unwrap_or("Cloud request failed").to_string()));
    }
    Ok(json)
}
async fn current_user() -> Result<Option<Value>, String> {
    let response = request("GET", "/api/me").send().await.map_err(|_| "Network unavailable. Retry when connected.".to_string())?;
    // Only an explicit unauthorized response establishes a guest session.
    if response.status() == 401 {
        // Finish the Fetch stream even when the guest response has no JSON body.
        let _ = response.binary().await;
        return Ok(None);
    }
    let status = response.status();
    let value = response.json::<Value>().await.map_err(|_| "The server returned an invalid response".to_string())?;
    if !(200..300).contains(&status) {
        return Err(value.get("error").and_then(Value::as_str).unwrap_or("Could not check your workspace session").to_string());
    }
    Ok(Some(value))
}
async fn binary(method: &str, path: &str, body: Option<&[u8]>, account: Option<&str>) -> Result<Vec<u8>, HttpFailure> {
    let req = account_request(method, path, account);
    let res = if let Some(b) = body {
        req.header("Content-Type", "application/octet-stream").body(js_sys::Uint8Array::from(b)).map_err(js_error)?.send().await
    } else {
        req.send().await
    }
    .map_err(|_| "Network unavailable; retry when connected".to_string())?;
    if res.status() == 401 {
        let _ = res.binary().await;
        return Err(HttpFailure::Unauthorized);
    }
    if !res.ok() {
        let v = res.json::<Value>().await.unwrap_or(Value::Null);
        return Err(HttpFailure::Http(res.status(), field(&v, "error").to_string()));
    }
    res.binary().await.map_err(|error| HttpFailure::Other(js_error(error)))
}
fn task(q: &Queue, epoch: u64, ctx: &egui::Context, f: impl std::future::Future<Output = Result<Message, String>> + 'static) {
    let q = q.clone();
    let ctx = ctx.clone();
    wasm_bindgen_futures::spawn_local(async move {
        let m = f.await.unwrap_or_else(Message::Error);
        q.borrow_mut().push((epoch, m));
        ctx.request_repaint();
    });
}
fn document_task<F: std::future::Future<Output = Result<Message, String>> + 'static>(
    http: CloudHttp,
    request: DocumentRequest,
    run: impl FnOnce(CloudHttp) -> F + 'static,
) {
    cloud_task(http, move |http| async move { Ok(run(http).await.unwrap_or_else(|error| Message::DocumentError(request, error))) });
}
async fn download_project(http: &CloudHttp, path: String, revision: Option<i64>) -> Result<(Value, Vec<u8>), String> {
    let mut meta = http.api("GET", &path, None).await?;
    let rev = revision.unwrap_or_else(|| meta.get("revision").and_then(Value::as_i64).unwrap_or(0));
    if let Some(r) = revision {
        let all = http.api("GET", &format!("{path}/versions"), None).await?;
        let version =
            all.as_array().and_then(|rows| rows.iter().find(|v| v.get("revision").and_then(Value::as_i64) == Some(r))).ok_or("Version is unavailable")?;
        meta["content"] = version.clone();
        // Restoring opens the old bytes but uses the latest base, so the next save is a new version.
    }
    let size = meta.pointer("/content/bytes").and_then(Value::as_u64).ok_or("This project has no saved document yet")? as usize;
    if size == 0 || size > 100 * 1024 * 1024 {
        return Err("Saved document exceeds browser download limit".into());
    }
    let mut bytes = Vec::with_capacity(size);
    for part in 0..size.div_ceil(CHUNK) {
        let b = http.binary("GET", &format!("{path}/content?revision={rev}&part={part}"), None).await?;
        if b.len() > CHUNK {
            return Err("Invalid download chunk".into());
        }
        bytes.extend(b);
    }
    if bytes.len() != size || hex::encode(Sha256::digest(&bytes)) != meta.pointer("/content/sha256").and_then(Value::as_str).unwrap_or("") {
        return Err("Download checksum failed. No document was opened.".into());
    }
    Ok((meta, bytes))
}
impl Cloud {
    pub fn new(ctx: &egui::Context) -> Self {
        let mut s = Self {
            live: live::Live::default(),
            queue: Rc::default(),
            epoch: 0,
            auth_generation: Rc::new(Cell::new(0)),
            auth_allowed: Rc::new(Cell::new(true)),
            session_warning: None,
            session_check_pending: false,
            home: true,
            configured: false,
            booted: false,
            initial_navigation_done: false,
            connection_pending: false,
            connection_error_sequence: None,
            sign_in: false,
            user: None,
            projects: vec![],
            list_generation: 0,
            bindings: HashMap::new(),
            uncertain_commits: HashMap::new(),
            next_document_request: 0,
            pending_document: None,
            deferred_save: None,
            next_open_request: 0,
            pending_open: None,
            textures: HashMap::new(),
            search: String::new(),
            filter: "Home".into(),
            template_category: "For you".into(),
            status: "Connecting to your workspace…".into(),
            error: false,
            error_sequence: 0,
            busy: false,
            last_change: now(),
            observed: None,
            last_poll: 0.,
            last_draft: None,
            draft_scope: String::new(),
            draft_generation: Rc::new(Cell::new(0)),
            draft_pending: None,
            draft_visit: false,
            draft_activity: now(),
            draft_list_generation: 0,
            draft_retry_at: 0.,
            draft_write_warning: None,
            draft_list_warning: None,
            drafts: vec![],
            show_share: false,
            show_history: false,
            show_comments: false,
            member_email: String::new(),
            member_role: "edit".into(),
            invitation_pending: None,
            invitation_result: None,
            dialog_project: None,
            next_project_request: 0,
            project_requests: HashMap::new(),
            project_pending: HashMap::new(),
            member_writes: HashMap::new(),
            project_feedback: HashMap::new(),
            share_url: String::new(),
            members: vec![],
            history: vec![],
            comments: vec![],
            comment: String::new(),
            people: vec![],
            rename: String::new(),
            folder: String::new(),
            show_details: false,
            details_id: String::new(),
            newer: false,
            show_logout: false,
            logout_pending: false,
            draft_allowed: Rc::new(Cell::new(true)),
            compact_panels: false,
        };
        s.refresh_config(ctx);
        for starter in &home::STARTERS {
            let slug = starter.slug;
            task(&s.queue, 0, ctx, async move {
                Ok(Message::Preview(format!("starter/{slug}"), binary("GET", &format!("/templates/{slug}.png"), None, None).await.map_err(js_error)?))
            });
        }
        s
    }
    fn http(&self, ctx: &egui::Context) -> CloudHttp {
        CloudHttp {
            queue: self.queue.clone(),
            epoch: self.epoch,
            ctx: ctx.clone(),
            generation: self.auth_generation.get(),
            current: self.auth_generation.clone(),
            allowed: self.auth_allowed.clone(),
            account: self.user.as_ref().map(|user| field(user, "id").to_string()),
        }
    }
    fn session_paused(&self) -> bool {
        self.user.is_some() && !self.auth_allowed.get()
    }
    fn pause_session(&mut self) {
        self.auth_allowed.set(false);
        self.session_warning = Some("Your session expired. Sign in again to resume cloud saving and sharing.".into());
        // Keep account scope, native documents and commit evidence. Only remote UI work stops.
        self.cancel_open();
        self.project_requests.clear();
        self.project_pending.clear();
        self.member_writes.clear();
        if let Some(project) = self.invitation_pending.take() {
            self.invitation_result =
                Some((project, "Sign-in expired before the invitation could be confirmed. Check member access before sending again.".into(), true));
        }
        self.show_share = false;
        self.show_history = false;
        self.show_comments = false;
        self.show_details = false;
        self.people.clear();
        if !self.error {
            self.status = "Cloud saving paused. Your documents remain open.".into();
        }
    }
    fn check_session(&mut self, ctx: &egui::Context) {
        if self.session_check_pending || self.logout_pending || self.user.is_none() {
            return;
        }
        self.session_check_pending = true;
        let generation = self.auth_generation.get();
        let expected = self.scope();
        let bound_projects = self.bindings.values().map(|binding| binding.id.clone()).collect::<HashSet<_>>();
        // This one explicit identity check bypasses the paused cloud-request gate.
        // A different account is never permitted to fetch or replace this workspace's data.
        task(&self.queue, self.epoch, ctx, async move {
            let result = async {
                let user = current_user().await?;
                let access = if user.as_ref().is_some_and(|user| !expected.is_empty() && field(user, "id") == expected) {
                    let projects = api("GET", "/api/projects", None, Some(&expected)).await.map_err(js_error)?;
                    let rows = projects.as_array().ok_or("The server returned an invalid project list")?;
                    let mut roles =
                        rows.iter().map(|project| (field(project, "id").to_string(), field(project, "role").to_string())).collect::<HashMap<_, _>>();
                    // The workspace list is capped. Absence there is not evidence of revocation.
                    for project in bound_projects {
                        if let std::collections::hash_map::Entry::Vacant(entry) = roles.entry(project) {
                            let role = match api("GET", &format!("/api/projects/{}", entry.key()), None, Some(&expected)).await {
                                Ok(metadata) => field(&metadata, "role").to_string(),
                                Err(HttpFailure::Http(403 | 404, _)) => "unavailable".into(),
                                Err(error) => return Err(error.to_string()),
                            };
                            entry.insert(role);
                        }
                    }
                    Some(SessionAccess { projects, roles })
                } else {
                    None
                };
                Ok((user, access))
            }
            .await;
            Ok(Message::SessionChecked(generation, result))
        });
    }
    fn sign_in_again(&self, ctx: &egui::Context) {
        // The normal browser link mechanism preserves user activation and this editor tab.
        // Do not infer popup blocking from window.open's nullable return with noopener.
        ctx.open_url(egui::OpenUrl::new_tab("/auth/login"));
    }
    fn list(&mut self, ctx: &egui::Context) {
        self.refresh_projects(ctx, None);
    }
    fn refresh_projects(&mut self, ctx: &egui::Context, mutation: Option<(u64, String)>) {
        if self.session_paused() {
            return;
        }
        self.list_generation = self.list_generation.wrapping_add(1);
        let generation = self.list_generation;
        cloud_task(self.http(ctx), move |http| async move { Ok(Message::List(generation, http.api("GET", "/api/projects", None).await, mutation)) });
    }
    fn refresh_config(&mut self, ctx: &egui::Context) {
        if self.user.is_some() {
            self.check_session(ctx);
            return;
        }
        if self.connection_pending {
            return;
        }
        self.connection_pending = true;
        cloud_task(self.http(ctx), move |http| async move {
            let result = async {
                let config = http.api("GET", "/api/config", None).await?;
                let user = current_user().await?;
                Ok((config, user))
            }
            .await;
            // A dedicated result releases the connection guard on both success and failure.
            Ok(Message::Connection(result))
        });
    }
    fn project_request(&mut self, project: &str, kind: &'static str) -> ProjectRequest {
        self.next_project_request = self.next_project_request.wrapping_add(1);
        let request = ProjectRequest { project: project.into(), kind, generation: self.next_project_request };
        self.project_requests.insert(request.channel(), request.generation);
        self.project_pending.insert(request.channel(), request.generation);
        self.project_feedback.remove(request.channel());
        request
    }
    fn project_data(&mut self, ctx: &egui::Context, project: &str, kind: &'static str, path: String) {
        // Polling cannot supersede a slow read or an in-flight mutation's refresh.
        if self.session_paused() || self.project_pending.contains_key(kind) {
            return;
        }
        let request = self.project_request(project, kind);
        cloud_task(self.http(ctx), move |http| async move { Ok(Message::ProjectData(request, http.api("GET", &path, None).await)) });
    }
    fn accepts_project_response(&self, app: &PhotocraftApp, request: &ProjectRequest) -> bool {
        self.dialog_project.as_deref() == Some(request.project.as_str())
            && self.binding(app).is_some_and(|b| b.id == request.project)
            && self.project_requests.get(request.channel()) == Some(&request.generation)
    }
    fn finish_member_write(&mut self, app: &PhotocraftApp, ctx: &egui::Context, request: &ProjectRequest) {
        if self.member_writes.get(&request.project) != Some(&request.generation) {
            return;
        }
        self.member_writes.remove(&request.project);
        if !self.accepts_project_response(app, request) && self.binding(app).is_some_and(|b| b.id == request.project) {
            // A switch away and back may have loaded members before this write finished.
            let path = format!("/api/projects/{}/members", request.project);
            let refresh = self.project_request(&request.project, "members");
            cloud_task(self.http(ctx), move |http| async move { Ok(Message::ProjectData(refresh, http.api("GET", &path, None).await)) });
        }
    }
    fn sync_dialog_project(&mut self, app: &PhotocraftApp) {
        let project = self.binding(app).map(|b| b.id);
        if self.dialog_project != project {
            self.dialog_project = project;
            self.project_requests.clear();
            self.project_pending.clear();
            self.project_feedback.clear();
            self.members.clear();
            self.history.clear();
            self.comments.clear();
            self.share_url.clear();
            self.people.clear();
            self.invitation_result = None;
            self.show_share = false;
            self.show_history = false;
            self.show_comments = false;
        }
    }
    fn project_feedback_ui(&self, ui: &mut egui::Ui, kind: &'static str) {
        if let Some((message, error)) = self.project_feedback.get(kind) {
            let t = Tokens::get(ui.ctx());
            ui.add(egui::Label::new(RichText::new(message).color(if *error { t.warning } else { t.text })).wrap());
        }
    }
    fn mutate(&mut self, ctx: &egui::Context, method: &str, path: String, v: Value) {
        if self.session_paused() {
            return;
        }
        let method = method.to_string();
        let error_sequence = self.error_sequence;
        let previous_status = self.status.clone();
        cloud_task(self.http(ctx), move |http| async move {
            Ok(Message::ProjectChanged(http.api(&method, &path, Some(v)).await.map(|_| ()), error_sequence, previous_status))
        });
    }
    fn receive_projects(&mut self, ctx: &egui::Context, v: Value) {
        self.projects = arr(v);
        for p in &self.projects {
            let id = field(p, "id").to_string();
            if self.textures.contains_key(&id) || p.get("revision").and_then(Value::as_i64).unwrap_or(0) == 0 {
                continue;
            }
            let key = id.clone();
            cloud_task(self.http(ctx), move |http| async move {
                let b = http.binary("GET", &format!("/api/projects/{id}/thumbnail"), None).await.unwrap_or_default();
                Ok(Message::Preview(key, b))
            });
        }
    }
    fn open(&mut self, ctx: &egui::Context, id: String, revision: Option<i64>) {
        self.open_path(ctx, format!("/api/projects/{id}"), revision);
    }
    fn open_path(&mut self, ctx: &egui::Context, path: String, revision: Option<i64>) {
        if self.session_paused() {
            return;
        }
        if self.pending_open.as_ref().is_some_and(|(_, pending, version)| pending == &path && *version == revision) {
            return;
        }
        self.next_open_request = self.next_open_request.wrapping_add(1);
        let request = self.next_open_request;
        self.pending_open = Some((request, path.clone(), revision));
        self.status = "Opening your document…".into();
        cloud_task(self.http(ctx), move |http| async move { Ok(Message::Opened(request, download_project(&http, path, revision).await)) });
    }
    fn cancel_open(&mut self) {
        if self.pending_open.take().is_some() && self.status == "Opening your document…" {
            self.status = "Cloud open canceled".into();
        }
    }
    fn binding(&self, app: &PhotocraftApp) -> Option<Binding> {
        app.session.active().and_then(|d| self.bindings.get(&d.doc.id)).cloned()
    }
    fn document_request(&mut self, document: DocId) -> DocumentRequest {
        self.next_document_request = self.next_document_request.wrapping_add(1);
        let request = DocumentRequest { document, generation: self.next_document_request, auth_generation: self.auth_generation.get() };
        self.pending_document = Some(request);
        request
    }
    fn accepts_document_response(&self, app: &PhotocraftApp, request: DocumentRequest) -> bool {
        self.pending_document == Some(request) && app.session.documents().iter().any(|document| document.doc.id == request.document)
    }
    fn finish_document_request(&mut self, request: DocumentRequest) {
        if self.pending_document == Some(request) {
            self.pending_document = None;
            self.busy = false;
        }
    }
    fn prune_closed_documents(&mut self, app: &PhotocraftApp) {
        let open = app.session.documents().iter().map(|document| document.doc.id).collect::<HashSet<_>>();
        self.bindings.retain(|id, _| open.contains(id));
        self.uncertain_commits.retain(|id, _| open.contains(id));
        if let Some(request) = self.pending_document.filter(|request| !open.contains(&request.document)) {
            self.finish_document_request(request);
            self.deferred_save = None;
        }
    }
    fn is_trashed(&self, id: &str) -> bool {
        self.projects.iter().any(|p| field(p, "id") == id && p.get("trashed").and_then(Value::as_bool) == Some(true))
    }
    pub fn update(&mut self, app: &mut PhotocraftApp, ctx: &egui::Context) {
        self.prune_closed_documents(app);
        self.sync_dialog_project(app);
        let mut messages = std::mem::take(&mut *self.queue.borrow_mut());
        if !self.session_paused()
            && let Some(saved) = self.deferred_save.take()
        {
            // A confirmed commit, including a server merge, is stronger evidence than
            // digest lookup. Apply it through the original save/safe-sync path only
            // after the same account and current permissions have been verified.
            messages.push((self.epoch, saved));
        }
        for (epoch, m) in messages {
            self.sync_dialog_project(app);
            if epoch != self.epoch {
                continue;
            }
            let m = if let Message::CloudResult(generation, message) = m {
                if generation != self.auth_generation.get() || self.session_paused() {
                    // Settle only the matching operation. Its identity/commit evidence was
                    // queued separately before this result and remains available for retry.
                    match *message {
                        saved @ Message::Saved(request, ..) if self.accepts_document_response(app, request) => self.deferred_save = Some(saved),
                        Message::DocumentError(request, _) | Message::Synced(request, ..) => self.finish_document_request(request),
                        Message::Opened(request, _) if self.pending_open.as_ref().is_some_and(|(pending, _, _)| *pending == request) => self.cancel_open(),
                        _ => {}
                    }
                    continue;
                }
                *message
            } else {
                m
            };
            let document_request = match &m {
                Message::Created(request, _)
                | Message::CommitAttempt(request, _)
                | Message::CommitReconciled(request, ..)
                | Message::Saved(request, ..)
                | Message::Synced(request, ..)
                | Message::DocumentError(request, _) => Some(*request),
                _ => None,
            };
            if document_request.is_some_and(|request| !self.accepts_document_response(app, request)) {
                continue;
            }
            match m {
                Message::Quiet => {}
                Message::CloudResult(..) => continue, // Only one scoped envelope is accepted.
                Message::SessionExpired(generation) => {
                    if generation == self.auth_generation.get() && self.user.is_some() {
                        self.pause_session();
                    }
                }
                Message::SessionChecked(generation, result) => {
                    if generation != self.auth_generation.get() {
                        continue;
                    }
                    self.session_check_pending = false;
                    match result {
                        Ok((Some(user), Some(access))) if field(&user, "id") == self.scope() => {
                            let mut restricted = false;
                            for binding in self.bindings.values_mut() {
                                binding.role = access.roles.get(&binding.id).cloned().unwrap_or_else(|| "unavailable".into());
                                restricted |= !binding.can_edit();
                            }
                            self.user = Some(user);
                            self.auth_generation.set(generation.wrapping_add(1));
                            self.auth_allowed.set(true);
                            self.session_warning = None;
                            self.last_poll = now();
                            // Discard any list that started before the verified permission snapshot.
                            self.list_generation = self.list_generation.wrapping_add(1);
                            self.receive_projects(ctx, access.projects);
                            if !self.error && !self.busy {
                                self.status = if restricted {
                                    "Signed in again. Some projects are read-only or unavailable; your local edits are intact. Use Save a copy."
                                } else {
                                    "Signed in again. Cloud saving resumed."
                                }
                                .into();
                            }
                        }
                        Ok((Some(user), _)) => {
                            self.auth_allowed.set(false);
                            self.session_warning = Some(format!(
                                "Signed in as {}. This workspace belongs to {}. Sign in with the original account to resume.",
                                field(&user, "email"),
                                self.user.as_ref().map(|user| field(user, "email")).unwrap_or("the original account")
                            ));
                        }
                        Ok((None, _)) => {
                            self.auth_allowed.set(false);
                            self.session_warning = Some("Sign-in is not complete. Finish signing in in the new tab, then check again.".into());
                        }
                        Err(error) => {
                            self.auth_allowed.set(false);
                            self.session_warning =
                                Some(format!("Could not verify your account and project access: {error}. Check sign-in again when connected."));
                        }
                    }
                }
                Message::Connection(result) => {
                    self.connection_pending = false;
                    self.last_poll = now();
                    self.booted = true;
                    let (c, u) = match result {
                        Ok(connected) => connected,
                        Err(error) => {
                            self.error_sequence = self.error_sequence.wrapping_add(1);
                            self.connection_error_sequence = Some(self.error_sequence);
                            self.error = true;
                            self.status = format!("Could not connect to your workspace: {error}");
                            continue;
                        }
                    };
                    self.configured = c.get("cloud").and_then(Value::as_bool) == Some(true);
                    self.sign_in = c.get("signIn").and_then(Value::as_bool) == Some(true);
                    self.user = u;
                    if self.connection_error_sequence.take() == Some(self.error_sequence) {
                        self.error = false;
                    }
                    if !self.error && !self.busy {
                        self.status = if self.user.is_some() {
                            "Your workspace is ready"
                        } else if self.configured {
                            "Sign in to save and share. Local editing is always available."
                        } else {
                            "Local editing is ready. Cloud storage is awaiting setup."
                        }
                        .into();
                    }
                    if self.user.is_some() {
                        self.list(ctx);
                    }
                    self.refresh_drafts(ctx);
                    // Retry setup/auth until this URL is eligible, then dispatch it only once.
                    if !self.initial_navigation_done {
                        if self.configured
                            && let Some(key) = url_param("share").filter(|s| s.len() == 64)
                        {
                            self.initial_navigation_done = true;
                            self.open_path(ctx, format!("/api/share/{key}"), None);
                        } else if self.user.is_some()
                            && let Some(id) = url_param("project").filter(|v| v.len() == 36)
                        {
                            self.initial_navigation_done = true;
                            self.open(ctx, id, None);
                        }
                    }
                }
                Message::List(generation, result, mutation) => {
                    if generation != self.list_generation {
                        continue;
                    }
                    match result {
                        Ok(projects) => {
                            self.receive_projects(ctx, projects);
                            // A card action may dismiss its old error, never a newer save failure.
                            if let Some((error_sequence, previous_status)) = mutation
                                && !self.busy
                                && self.error_sequence == error_sequence
                                && self.status == previous_status
                            {
                                self.status = "Workspace updated".into();
                                self.error = false;
                            }
                        }
                        Err(error) => {
                            self.error_sequence = self.error_sequence.wrapping_add(1);
                            self.status = error;
                            self.error = true;
                        }
                    }
                }
                Message::ProjectChanged(result, error_sequence, previous_status) => match result {
                    // Assign the read generation after the write finishes, so a slower write
                    // still gets a fresh list that includes every earlier completed mutation.
                    Ok(()) => self.refresh_projects(ctx, Some((error_sequence, previous_status))),
                    Err(error) => {
                        self.error_sequence = self.error_sequence.wrapping_add(1);
                        self.status = error;
                        self.error = true;
                    }
                },
                Message::RecoverySaved(generation, document, result) => {
                    if self.draft_pending == Some(generation) {
                        self.draft_pending = None;
                    }
                    if generation != self.draft_generation.get() {
                        continue;
                    }
                    match result {
                        Ok(written) => {
                            self.last_draft = Some(document);
                            self.draft_visit = false;
                            if written {
                                self.draft_write_warning = None;
                            }
                            // Browser recovery does not mean the cloud upload succeeded.
                            if written && !self.error && !self.busy {
                                self.status = "Recovery copy saved in this browser".into();
                            }
                            self.refresh_drafts(ctx);
                        }
                        Err(error) => {
                            self.draft_retry_at = now() + 1800.;
                            self.draft_visit = false;
                            self.draft_write_warning = Some(format!("Could not save browser recovery: {error}"));
                        }
                    }
                }
                Message::Preview(id, b) => {
                    if let Ok(img) = photocraft_codecs::decode(&b)
                        && img.width() <= 512
                        && img.height() <= 512
                    {
                        self.textures.insert(
                            id.clone(),
                            ctx.load_texture(
                                id,
                                ColorImage::from_rgba_unmultiplied([img.width() as usize, img.height() as usize], &img.to_rgba8()),
                                Default::default(),
                            ),
                        );
                    }
                }
                Message::Opened(request, result) => {
                    if self.pending_open.as_ref().map(|(pending, _, _)| *pending) != Some(request) {
                        continue;
                    }
                    self.pending_open = None;
                    let (meta, bytes) = match result {
                        Ok(document) => document,
                        Err(error) => {
                            self.error_sequence = self.error_sequence.wrapping_add(1);
                            self.status = error;
                            self.error = true;
                            continue;
                        }
                    };
                    let name = format!("{}.pcraft", field(&meta, "title"));
                    match app.open_bytes(&name, &bytes) {
                        Ok(_) => {
                            if let Some(d) = app.session.active() {
                                self.bindings.insert(
                                    d.doc.id,
                                    Binding {
                                        id: field(&meta, "id").into(),
                                        revision: meta.get("revision").and_then(Value::as_i64).unwrap_or(0),
                                        saved_local: d.revision,
                                        role: field(&meta, "role").into(),
                                    },
                                );
                            }
                            self.home = false;
                            self.error = false;
                            self.newer = false;
                            self.status = "Opened from your workspace".into();
                        }
                        Err(e) => {
                            self.status = e;
                            self.error = true;
                        }
                    }
                }
                Message::Created(request, pid) => {
                    // A delayed reservation still belongs to this native document, but
                    // cannot restore owner controls past a verified permission refresh.
                    let role = if request.auth_generation == self.auth_generation.get() {
                        "owner".into()
                    } else {
                        self.projects
                            .iter()
                            .find(|project| field(project, "id") == pid)
                            .map(|project| field(project, "role").to_string())
                            .unwrap_or_else(|| "unavailable".into())
                    };
                    self.bindings.insert(request.document, Binding { id: pid, revision: 0, saved_local: 0, role });
                }
                Message::CommitAttempt(request, attempt) => {
                    let attempts = self.uncertain_commits.entry(request.document).or_default();
                    if !attempts.iter().any(|previous| {
                        previous.project == attempt.project
                            && previous.base == attempt.base
                            && previous.local == attempt.local
                            && previous.sha256 == attempt.sha256
                    }) {
                        attempts.push(attempt);
                    }
                }
                Message::CommitReconciled(request, project, local, revision) => {
                    if let Some(binding) = self.bindings.get_mut(&request.document).filter(|binding| binding.id == project && binding.revision <= revision) {
                        binding.revision = revision;
                        binding.saved_local = local;
                    }
                }
                Message::SignedOut(result) => {
                    self.logout_pending = false;
                    if let Err(error) = result {
                        self.busy = false;
                        self.error = true;
                        self.error_sequence = self.error_sequence.wrapping_add(1);
                        self.status = error;
                        continue;
                    }
                    self.epoch += 1;
                    self.auth_generation.set(self.auth_generation.get().wrapping_add(1));
                    self.auth_allowed.set(true);
                    self.session_warning = None;
                    self.session_check_pending = false;
                    self.pending_open = None;
                    self.connection_pending = false;
                    self.connection_error_sequence = None;
                    self.bindings.clear();
                    self.uncertain_commits.clear();
                    self.pending_document = None;
                    self.deferred_save = None;
                    self.projects.clear();
                    self.textures.retain(|key, _| key.starts_with("starter/"));
                    self.drafts.clear();
                    // Old async writers keep their revoked permit; new guest work gets a fresh one.
                    self.draft_allowed.set(false);
                    self.draft_generation.set(self.draft_generation.get().wrapping_add(1));
                    self.draft_allowed = Rc::new(Cell::new(true));
                    self.draft_generation = Rc::new(Cell::new(0));
                    self.draft_pending = None;
                    self.last_draft = None;
                    self.draft_write_warning = None;
                    self.draft_list_warning = None;
                    self.user = None;
                    self.home = true;
                    self.filter = "Home".into();
                    self.show_logout = false;
                    self.invitation_pending = None;
                    self.invitation_result = None;
                    self.member_writes.clear();
                    let _ = app.session.execute("file.closeAll", json!({}));
                    self.status = "Signed out. Private browser recovery data cleared.".into();
                    self.busy = false;
                }
                Message::SignInReady(generation, document, result) => {
                    if self.draft_pending == Some(generation) {
                        self.draft_pending = None;
                    }
                    if let Err(error) = result {
                        self.busy = false;
                        self.error = true;
                        self.status = error;
                        continue;
                    }
                    if generation != self.draft_generation.get()
                        || document != app.session.active().map(|d| (d.doc.id, d.revision))
                        || self.has_other_unsaved_documents(app)
                    {
                        self.busy = false;
                        self.error = true;
                        self.status = "Your document changed before sign-in. Try signing in again.".into();
                        continue;
                    }
                    super::web::set_unsaved(false);
                    if let Some(w) = web_sys::window() {
                        let _ = w.location().set_href("/auth/login");
                    }
                    return;
                }
                Message::Synced(request, expected, meta, bytes) => {
                    self.finish_document_request(request);
                    let id = request.document;
                    // Never replace an edited tab, a switched document, or an active gesture.
                    if app.session.active().is_some_and(|d| d.doc.id == id && d.revision == expected)
                        && !ctx.input(|i| i.pointer.any_down())
                        && !app.has_active_canvas_gesture()
                        && !ctx.egui_wants_keyboard_input()
                    {
                        match photocraft_format::load_from_bytes(&bytes) {
                            Ok(mut doc) => {
                                doc.id = id;
                                let active = app.session.active().and_then(|d| d.active_layer).filter(|layer| doc.layer(*layer).is_some());
                                let mut state = photocraft_engine::DocState::new(doc, None);
                                state.revision = expected.saturating_add(1);
                                state.saved_revision = state.revision;
                                if let Some(layer) = active {
                                    state.active_layer = Some(layer);
                                    state.selected_layers = vec![layer];
                                }
                                if let Some(d) = app.session.active_mut() {
                                    *d = state;
                                }
                                if let Some(binding) = self.bindings.get_mut(&id) {
                                    binding.revision = meta.get("revision").and_then(Value::as_i64).unwrap_or(binding.revision);
                                    binding.saved_local = expected.saturating_add(1);
                                    binding.role = field(&meta, "role").into();
                                }
                                app.sync_views();
                                self.status = "Up to date with your collaborators. Earlier work is in version history.".into();
                                self.newer = false;
                                self.error = false;
                            }
                            Err(_) => {
                                self.newer = true;
                                self.status = "Could not open the shared update. Your document is unchanged.".into();
                            }
                        }
                    } else {
                        self.newer = true;
                    }
                }
                Message::Presence(pid, v) => {
                    if let Some(b) = self.binding(app).filter(|b| b.id == pid) {
                        if let Some(id) = app.session.active().map(|d| d.doc.id)
                            && let Some(binding) = self.bindings.get_mut(&id)
                            && let Some(role) = v.get("role").and_then(Value::as_str)
                        {
                            binding.role = role.into();
                        }
                        self.people = v
                            .get("people")
                            .and_then(Value::as_array)
                            .map(|p| p.iter().filter_map(Value::as_str).map(String::from).collect())
                            .unwrap_or_default();
                        self.newer = v.get("revision").and_then(Value::as_i64).is_some_and(|r| r > b.revision);
                        if self.newer
                            && !self.busy
                            && !ctx.egui_wants_keyboard_input()
                            && !ctx.input(|i| i.pointer.any_down())
                            && !app.has_active_canvas_gesture()
                            && now() - self.last_change > 800.
                            && let Some(d) = app.session.active().filter(|d| d.revision == b.saved_local)
                        {
                            self.sync(ctx, d.doc.id, d.revision, b.id);
                        }
                    }
                }
                Message::Saved(request, local, pid, revision, merged) => {
                    self.finish_document_request(request);
                    let id = request.document;
                    self.uncertain_commits.remove(&id);
                    if merged {
                        self.sync(ctx, id, local, pid);
                        continue;
                    }
                    self.textures.remove(&pid);
                    if let Some(d) = app.session.active_mut().filter(|d| d.doc.id == id) {
                        d.saved_revision = local;
                    }
                    self.busy = false;
                    self.error = false;
                    self.status = "All changes saved".into();
                    let role = self.bindings.get(&id).map(|b| b.role.clone()).unwrap_or_else(|| "owner".into());
                    self.bindings.insert(id, Binding { id: pid, revision, saved_local: local, role });
                    self.list(ctx);
                }
                Message::ProjectData(request, result) => {
                    self.finish_member_write(app, ctx, &request);
                    if !self.accepts_project_response(app, &request) {
                        continue;
                    }
                    self.project_pending.remove(request.channel());
                    match result {
                        Ok(v) => {
                            self.project_feedback.remove(request.channel());
                            match request.kind {
                                "members" => self.members = arr(v),
                                "history" => self.history = arr(v),
                                "comments" => self.comments = arr(v),
                                "comment_posted" => {
                                    self.comments = arr(v.get("comments").cloned().unwrap_or(Value::Null));
                                    if self.comment == field(&v, "submitted") {
                                        self.comment.clear();
                                    }
                                }
                                "share" => self.share_url = field(&v, "url").into(),
                                "share_revoked" => {
                                    self.share_url.clear();
                                    self.project_feedback.insert("share", ("View links revoked".into(), false));
                                }
                                _ => {}
                            }
                        }
                        Err(error) => {
                            self.project_feedback.insert(request.channel(), (error, true));
                        }
                    }
                }
                Message::Invited { request, email, outcome, members } => {
                    let current_members = self.accepts_project_response(app, &request);
                    self.finish_member_write(app, ctx, &request);
                    if self.project_pending.get(request.channel()) == Some(&request.generation) {
                        self.project_pending.remove(request.channel());
                    }
                    let project = request.project;
                    if self.invitation_pending.as_deref() == Some(&project) {
                        self.invitation_pending = None;
                    }
                    let accepted = outcome.is_ok();
                    let mut message = match outcome {
                        Ok(()) => format!("Email service accepted the invitation for {email}. They can sign in with this address to open the project."),
                        Err(error) => error,
                    };
                    let mut failed = !accepted;
                    match members {
                        Ok(members) if current_members => self.members = arr(members),
                        Err(_) => {
                            message.push_str(" Could not refresh the people list. Close and reopen sharing to refresh it.");
                            failed = true;
                        }
                        _ => {}
                    }
                    if accepted && current_members && self.member_email.trim() == email {
                        self.member_email.clear();
                    }
                    if current_members {
                        self.invitation_result = Some((project, message, failed));
                    }
                }
                Message::Error(e) => {
                    self.error_sequence = self.error_sequence.wrapping_add(1);
                    self.busy = false;
                    self.status = e;
                    self.error = true;
                    self.draft_allowed.set(true);
                }
                Message::DocumentError(request, error) => {
                    self.finish_document_request(request);
                    self.error_sequence = self.error_sequence.wrapping_add(1);
                    self.status = error;
                    self.error = true;
                }
                Message::Drafts(generation, scope, result) => {
                    if generation != self.draft_list_generation || scope != self.scope() {
                        continue;
                    }
                    match result {
                        Ok(drafts) => {
                            self.drafts = drafts;
                            self.draft_list_warning = None;
                        }
                        Err(error) => self.draft_list_warning = Some(format!("Could not load browser recovery: {error}")),
                    }
                }
                Message::Template(name, bytes) => {
                    self.busy = false;
                    match app.open_bytes(&name, &bytes) {
                        Ok(_) => {
                            self.detach_local_copy(app);
                            self.home = false;
                            self.error = false;
                            self.status = "Your own copy. Every layer is ready to edit.".into();
                        }
                        Err(e) => {
                            self.error = true;
                            self.status = e;
                        }
                    }
                }
                Message::Recovered(name, bytes) => match app.open_bytes(&name, &bytes) {
                    Ok(_) => {
                        self.detach_local_copy(app);
                        self.home = false;
                        self.status = "Recovered as a separate local document. Save a new cloud copy when ready.".into();
                    }
                    Err(e) => {
                        self.error = true;
                        self.status = e;
                    }
                },
            }
        }
        // Template and unrelated request results also use the workspace busy flag.
        // A document operation remains exclusive until its own response or invalidation.
        self.busy |= self.pending_document.is_some();
        self.sync_dialog_project(app);
        super::web::set_unsaved(
            app.session
                .documents()
                .iter()
                .any(|d| self.bindings.get(&d.doc.id).map(|b| b.revision == 0 || b.saved_local != d.revision).unwrap_or(d.is_dirty())),
        );
        let current = app.session.active().map(|d| (d.doc.id, d.revision));
        let scope = self.scope();
        if current != self.observed || scope != self.draft_scope {
            if scope != self.draft_scope {
                self.draft_write_warning = None;
                self.draft_list_warning = None;
            }
            self.draft_retry_at = 0.;
            self.draft_visit |= current.map(|d| d.0) != self.observed.map(|d| d.0) || scope != self.draft_scope;
            self.draft_generation.set(self.draft_generation.get().wrapping_add(1));
            self.last_draft = None;
            self.draft_scope = scope;
            self.observed = current;
            self.draft_activity = now();
            self.last_change = self.draft_activity;
            if current.is_some() {
                self.home = false;
            }
        }
        if let Some(d) = app.session.active() {
            let changed = self.binding(app).map(|b| b.revision == 0 || b.saved_local != d.revision).unwrap_or(d.is_dirty());
            if self.booted
                && self.connection_error_sequence.is_none()
                && self.draft_allowed.get()
                && self.draft_pending.is_none()
                && now() >= self.draft_retry_at
                && (self.draft_visit || now() - self.last_change > 1800.)
                && self.last_draft != current
            {
                let name = d.doc.name.clone();
                let scope = self.scope();
                let key = format!("{scope}:{}", d.doc.id.0);
                let generation = self.draft_generation.get();
                let document = (d.doc.id, d.revision);
                let write = self.recovery_write(self.draft_activity);
                match photocraft_format::save_to_bytes(&d.doc, &Default::default()) {
                    Ok(bytes) => {
                        self.draft_pending = Some(generation);
                        task(&self.queue, self.epoch, ctx, async move {
                            Ok(Message::RecoverySaved(generation, document, draft_put(&scope, &key, &name, &bytes, write).await))
                        });
                    }
                    Err(e) => {
                        self.draft_retry_at = now() + 1800.;
                        self.draft_visit = false;
                        self.draft_write_warning = Some(format!("Could not create recovery copy: {e}"));
                    }
                }
            }
            if changed
                && self.binding(app).is_some_and(|b| b.can_edit() && !self.is_trashed(&b.id))
                && self.user.is_some()
                && !self.session_paused()
                && !self.busy
                && !self.error
                && !ctx.input(|i| i.pointer.any_down())
                // Raw Release reaches logic before the native canvas commits its
                // gesture in ui. Auto-Select's view revision must not save old pixels.
                && !app.has_active_canvas_gesture()
                && now() - self.last_change > 150.
            {
                self.save(app, ctx, false);
            }
        }
        if now() - self.last_poll > 1500. {
            self.last_poll = now();
            if let Some(b) = self.binding(app).filter(|_| self.user.is_some() && !self.session_paused()) {
                if self.show_comments {
                    self.project_data(ctx, &b.id, "comments", format!("/api/projects/{}/comments", b.id));
                }
                cloud_task(self.http(ctx), move |http| async move {
                    match http.api("POST", &format!("/api/projects/{}/presence", b.id), Some(json!({}))).await {
                        Ok(value) => Ok(Message::Presence(b.id, value)),
                        Err(_) => Ok(Message::Quiet),
                    }
                });
            }
            if !self.sign_in && self.user.is_none() {
                self.refresh_config(ctx);
            }
        }
        let http = self.http(ctx);
        let binding = self.binding(app).filter(|b| !self.home && b.revision > 0 && !self.is_trashed(&b.id) && self.user.is_some() && !self.session_paused());
        if let Some(update) = self.live.update(app, ctx, binding.clone(), http) {
            if let Some(id) = app.session.active().map(|d| d.doc.id)
                && let Some(current) = self.bindings.get_mut(&id)
            {
                current.role = update.role;
            }
            if let Some(b) = binding
                && update.revision > b.revision
                && !self.busy
                && !ctx.egui_wants_keyboard_input()
                && !ctx.input(|i| i.pointer.any_down())
                && !app.has_active_canvas_gesture()
                && let Some(d) = app.session.active().filter(|d| d.revision == b.saved_local)
            {
                self.newer = true;
                self.sync(ctx, d.doc.id, d.revision, b.id);
            }
        }
        ctx.request_repaint_after(std::time::Duration::from_millis(500));
    }
    fn sync(&mut self, ctx: &egui::Context, id: DocId, revision: u64, pid: String) {
        if self.session_paused() || self.pending_document.is_some() {
            return;
        }
        self.busy = true;
        self.status = "Bringing in your collaborators’ changes…".into();
        let request = self.document_request(id);
        document_task(self.http(ctx), request, move |http| async move {
            let (meta, bytes) = download_project(&http, format!("/api/projects/{pid}"), None).await?;
            Ok(Message::Synced(request, revision, meta, bytes))
        });
    }
    pub(crate) fn open_local(&mut self, app: &mut PhotocraftApp, name: &str, bytes: &[u8]) {
        let documents = app.session.documents().len();
        match app.open_bytes(name, bytes) {
            Ok(_) if app.session.documents().len() > documents => {
                self.cancel_open();
                self.detach_local_copy(app);
                self.home = false;
                self.error = false;
                self.status = app.ui.status.clone();
            }
            Ok(_) => {} // Preset imports do not create a document or detach the active one.
            Err(error) => app.open_failed(name, &error),
        }
    }
    fn detach_local_copy(&mut self, app: &PhotocraftApp) {
        if let Some(document) = app.session.active() {
            // The original engine preserves imported IDs when the old tab is closed.
            // A recovery/template copy must never inherit that tab's cloud destination.
            self.bindings.remove(&document.doc.id);
            self.uncertain_commits.remove(&document.doc.id);
            if let Some(request) = self.pending_document.filter(|request| request.document == document.doc.id) {
                self.finish_document_request(request);
            }
            // A close/reopen can admit the same native ID and revision between frames.
            // Treat admission as a fresh visit and revoke a pending snapshot of the old tab.
            self.draft_generation.set(self.draft_generation.get().wrapping_add(1));
            self.observed = None;
            self.last_draft = None;
        }
    }
    fn scope(&self) -> String {
        self.user.as_ref().map(|u| field(u, "id").into()).unwrap_or_else(|| "guest".into())
    }
    fn refresh_drafts(&mut self, ctx: &egui::Context) {
        self.draft_list_generation = self.draft_list_generation.wrapping_add(1);
        let generation = self.draft_list_generation;
        let scope = self.scope();
        task(&self.queue, self.epoch, ctx, async move {
            let drafts = draft_list(&scope).await;
            Ok(Message::Drafts(generation, scope, drafts))
        });
    }
    fn recovery_write(&self, activity: f64) -> RecoveryWrite {
        RecoveryWrite {
            generation: self.draft_generation.get(),
            current: self.draft_generation.clone(),
            allowed: self.draft_allowed.clone(),
            saved_at: activity,
        }
    }
    fn has_other_unsaved_documents(&self, app: &PhotocraftApp) -> bool {
        let active = app.session.active().map(|d| d.doc.id);
        app.session
            .documents()
            .iter()
            .any(|d| Some(d.doc.id) != active && self.bindings.get(&d.doc.id).map(|b| b.revision == 0 || b.saved_local != d.revision).unwrap_or(d.is_dirty()))
    }
    fn begin_sign_in(&mut self, app: &PhotocraftApp, ctx: &egui::Context) {
        if self.user.is_some() {
            self.sign_in_again(ctx);
            return;
        }
        if self.has_other_unsaved_documents(app) {
            self.error = true;
            self.status = "Download your other unsaved documents before signing in. Browser recovery keeps only the last visited document.".into();
            return;
        }
        let draft = if let Some(d) = app.session.active() {
            match photocraft_format::save_to_bytes(&d.doc, &Default::default()) {
                Ok(bytes) => Some((format!("guest:{}", d.doc.id.0), d.doc.name.clone(), bytes)),
                Err(e) => {
                    self.error = true;
                    self.status = format!("Could not preserve your work before sign-in: {e}. Download your document first.");
                    return;
                }
            }
        } else {
            None
        };
        self.busy = true;
        let document = app.session.active().map(|d| (d.doc.id, d.revision));
        self.draft_generation.set(self.draft_generation.get().wrapping_add(1));
        let write = self.recovery_write(now());
        let generation = write.generation;
        self.draft_pending = Some(generation);
        task(&self.queue, self.epoch, ctx, async move {
            let result = async {
                if let Some((key, name, bytes)) = draft
                    && !draft_put("guest", &key, &name, &bytes, write).await?
                {
                    return Err("Your browser recovery changed before sign-in. Try signing in again.".into());
                }
                Ok(())
            }
            .await;
            Ok(Message::SignInReady(generation, document, result))
        });
    }
    fn save(&mut self, app: &PhotocraftApp, ctx: &egui::Context, copy: bool) {
        if self.session_paused() || self.busy || self.pending_document.is_some() {
            return;
        }
        let Some(d) = app.session.active() else {
            return;
        };
        if self.user.is_none() {
            self.status = "Sign in first, or use File > Save to download your document.".into();
            self.error = true;
            return;
        }
        let binding = if copy { None } else { self.binding(app) };
        if binding.as_ref().is_some_and(|b| !b.can_edit()) {
            self.status = "This project is read-only or unavailable. Use Save a copy to create your own project.".into();
            self.error = true;
            return;
        }
        if binding.as_ref().is_some_and(|b| self.is_trashed(&b.id)) {
            self.status = "Restore this project from Trash before saving, or use Save a copy.".into();
            self.error = true;
            return;
        }
        let thumb = photocraft_compose::thumbnail(&d.doc, 320);
        let options = photocraft_format::SaveOptions { thumbnail: Some(thumb.clone()), composite: None };
        let bytes = match photocraft_format::save_to_bytes(&d.doc, &options) {
            Ok(b) => b,
            Err(e) => {
                self.status = e.to_string();
                self.error = true;
                return;
            }
        };
        if bytes.len() > 100 * 1024 * 1024 {
            self.error = true;
            self.status = "This document exceeds the 100 MB cloud limit. Use File > Save to download it.".into();
            return;
        }
        let preview = photocraft_codecs::Image::from_u8(thumb.width, thumb.height, photocraft_codecs::ChannelLayout::Rgba, thumb.pixels)
            .and_then(|img| photocraft_codecs::encode(&img, photocraft_codecs::Format::Png, &Default::default()))
            .ok();
        let docid = d.doc.id;
        let local = d.revision;
        let name = d.doc.name.clone();
        let width = d.doc.size.width;
        let height = d.doc.size.height;
        self.busy = true;
        self.error = false;
        self.status = "Saving a complete version…".into();
        let queue = self.queue.clone();
        let epoch = self.epoch;
        let uncertain = self.uncertain_commits.get(&docid).cloned().unwrap_or_default();
        let request = self.document_request(docid);
        document_task(self.http(ctx), request, move |http| async move {
            let (pid, mut base) = if let Some(b) = binding {
                (b.id, b.revision)
            } else {
                let p = http.api("POST", "/api/projects", Some(json!({"title":name}))).await?;
                let pid = field(&p, "id").to_string();
                queue.borrow_mut().push((epoch, Message::Created(request, pid.clone())));
                (pid, 0)
            };
            // A lost response does not prove that the preceding commit failed. Only the
            // exact uploaded snapshot can advance our base; the latest revision cannot.
            let mut acknowledged = None;
            for attempt in uncertain.iter().filter(|attempt| attempt.project == pid) {
                let versions =
                    http.api("GET", &format!("/api/projects/{pid}/versions?sha256={}&after_revision={}", attempt.sha256, attempt.base), None).await?;
                if let Some(revision) = versions.as_array().and_then(|versions| {
                    versions
                        .iter()
                        .filter(|version| field(version, "sha256") == attempt.sha256)
                        .filter_map(|version| version.get("revision").and_then(Value::as_i64))
                        .filter(|revision| *revision > attempt.base)
                        .max()
                }) && revision >= base
                    && acknowledged.as_ref().is_none_or(|(_, previous)| revision > *previous)
                {
                    acknowledged = Some((attempt.local, revision));
                }
            }
            if let Some((saved_local, revision)) = acknowledged {
                base = revision;
                queue.borrow_mut().push((epoch, Message::CommitReconciled(request, pid.clone(), saved_local, revision)));
                if saved_local == local {
                    return Ok(Message::Saved(request, local, pid, revision, false));
                }
            }
            let sha256 = hex::encode(Sha256::digest(&bytes));
            let init=http.api("POST",&format!("/api/projects/{pid}/uploads"),Some(json!({"base_revision":base,"bytes":bytes.len(),"parts":bytes.len().div_ceil(CHUNK),"sha256":sha256,"title":"Saved from editor","width":width,"height":height}))).await?;
            let uid = field(&init, "id");
            let uploaded = async {
                for (i, b) in bytes.chunks(CHUNK).enumerate() {
                    http.binary("PUT", &format!("/api/uploads/{uid}/{i}"), Some(b)).await?;
                }
                queue.borrow_mut().push((epoch, Message::CommitAttempt(request, UncertainCommit { project: pid.clone(), base, local, sha256 })));
                http.api("POST", &format!("/api/uploads/{uid}/commit"), Some(json!({}))).await
            }
            .await;
            let done = match uploaded {
                Ok(v) => v,
                Err(e) => {
                    let _ = http.api("DELETE", &format!("/api/uploads/{uid}"), None).await;
                    return Err(e);
                }
            };
            if let Some(png) = preview.filter(|_| done.get("merged").and_then(Value::as_bool) != Some(true)) {
                let _ = http.binary("PUT", &format!("/api/projects/{pid}/thumbnail"), Some(&png)).await;
            }
            Ok(Message::Saved(
                request,
                local,
                pid,
                done.get("revision").and_then(Value::as_i64).unwrap_or(base + 1),
                done.get("merged").and_then(Value::as_bool).unwrap_or(false),
            ))
        });
    }
    pub fn handle_input(&mut self, ctx: &egui::Context) {
        // Handle the workspace modal before the native editor can consume Escape.
        if (self.show_logout || self.show_share || self.show_history || self.show_comments || self.show_details)
            && ctx.input_mut(|input| input.consume_key(egui::Modifiers::NONE, egui::Key::Escape))
        {
            if egui::Popup::is_any_open(ctx) {
                egui::Popup::close_all(ctx);
            } else {
                self.show_logout = false;
                self.show_share = false;
                self.show_history = false;
                self.show_comments = false;
                self.show_details = false;
            }
        }
    }
    pub fn ui(&mut self, app: &mut PhotocraftApp, ui: &mut egui::Ui, frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        if self.home {
            *ui.visuals_mut() = egui::Visuals::light();
            home::workspace_style(ui);
            ui.visuals_mut().selection.bg_fill = egui::Color32::from_rgb(238, 232, 252);
            ui.visuals_mut().selection.stroke = egui::Stroke::new(1., home::PURPLE);
        }
        let t = if self.home { Tokens::for_kind(ThemeKind::StudioLight) } else { Tokens::get(&ctx) };
        let binding = self.binding(app);
        let compact = ui.available_width() < 760.;
        egui::Panel::top("cloud_header").exact_size(64.).frame(egui::Frame::NONE.fill(t.card).inner_margin(egui::Margin::symmetric(20, 12))).show(ui, |ui| {
            // Header actions share one density; the native editor keeps its compact controls.
            ui.spacing_mut().interact_size.y = 36.;
            ui.spacing_mut().button_padding = Vec2::new(12., 8.);
            ui.spacing_mut().item_spacing.x = 8.;
            ui.style_mut().text_styles.insert(egui::TextStyle::Button, egui::FontId::proportional(14.));
            ui.horizontal_centered(|ui| {
                if ui.add(egui::Button::new(RichText::new("PhotoCraft").size(18.).strong()).frame(false)).on_hover_text("Open your workspace").clicked() {
                    self.cancel_open();
                    self.home = !self.home;
                    if self.home && self.user.is_some() {
                        self.list(&ctx);
                    }
                }
                if ui.available_width() > 500. {
                    ui.label(RichText::new("STUDIO").small().color(t.accent));
                }
                ui.separator();
                if !self.home {
                    if !compact && ui.button("Projects").clicked() {
                        self.cancel_open();
                        self.home = true;
                        self.list(&ctx);
                    }
                    if ui.available_width() > 700. {
                        let title = app.session.active().map(|d| d.doc.name.as_str()).unwrap_or("Untitled");
                        // Reserve room for actions so long names cannot push them off-screen.
                        ui.add_sized([ui.available_width() - 440., 36.], egui::Label::new(RichText::new(title).strong()).halign(egui::Align::Min).truncate())
                            .on_hover_text(title);
                    }
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    let avatar = home::avatar(ui, self.user.as_ref().map(|u| field(u, "name")));
                    egui::Popup::menu(&avatar)
                        .frame(
                            egui::Frame::popup(ui.style())
                                .fill(egui::Color32::WHITE)
                                .stroke(egui::Stroke::new(1., home::BORDER))
                                .inner_margin(18)
                                .corner_radius(14),
                        )
                        .show(|ui| {
                            *ui.visuals_mut() = egui::Visuals::light();
                            home::workspace_style(ui);
                            ui.set_width(260.);
                            ui.spacing_mut().item_spacing.y = 12.;
                            if let Some(user) = &self.user {
                                ui.label(RichText::new(field(user, "name")).size(16.).strong());
                                ui.label(field(user, "email"));
                                ui.separator();
                                if self.session_paused() && ui.button("Sign in again").clicked() {
                                    self.sign_in_again(&ctx);
                                    ui.close();
                                }
                                if ui.button("Sign out").clicked() {
                                    self.show_logout = true;
                                    ui.close();
                                }
                            } else {
                                ui.label(RichText::new("A home for your ideas").size(17.).strong());
                                ui.label("Save your designs, share them, and create together.");
                                if self.sign_in {
                                    if ui.add_enabled_ui(!self.busy, |ui| ui.add_sized([260., 42.], home::primary("Continue with Google"))).inner.clicked() {
                                        self.begin_sign_in(app, &ctx);
                                    }
                                } else if !self.booted && !self.error {
                                    ui.horizontal(|ui| {
                                        ui.spinner();
                                        ui.label("Connecting…");
                                    });
                                } else {
                                    ui.label("Cloud sign-in is unavailable. Guest editing still works.");
                                    if ui.button("Retry connection").clicked() {
                                        self.refresh_config(&ctx);
                                    }
                                }
                                ui.label(RichText::new("You can keep editing without an account.").small().color(home::MUTED));
                            }
                        });
                    if !self.home {
                        if let Some(b) = binding.as_ref().filter(|b| b.role == "owner")
                            && ui
                                .add_enabled_ui(b.revision > 0 && !self.session_paused(), |ui| {
                                    ui.add_sized([88., 36.], egui::Button::new(RichText::new("Share").strong().color(t.primary_text)).fill(t.primary_bg))
                                })
                                .inner
                                .on_disabled_hover_text("Save a complete version before sharing")
                                .clicked()
                        {
                            self.show_share = true;
                            self.share_url.clear();
                            self.project_data(&ctx, &b.id, "members", format!("/api/projects/{}/members", b.id));
                        }
                        let can_save = !self.session_paused() && !self.busy && self.pending_document.is_none() && app.session.active().is_some();
                        let retry_save = self.user.is_some()
                            && binding.as_ref().is_none_or(|b| b.can_edit())
                            && (self.error || binding.as_ref().is_some_and(|b| b.revision == 0));
                        let save = ui
                            .add_enabled_ui(can_save, |ui| {
                                if retry_save {
                                    ui.add_sized([88., 36.], egui::Button::new(RichText::new("Retry save").strong().color(t.primary_text)).fill(t.primary_bg))
                                        .on_hover_text("Retry saving this document to the same cloud project")
                                } else if binding.as_ref().is_some_and(|b| b.can_edit()) {
                                    header_icon(ui, "cloud", "Save now · edits also save automatically")
                                } else {
                                    ui.add_sized(
                                        [if compact { 76. } else { 104. }, 36.],
                                        egui::Button::new(
                                            RichText::new(if binding.is_some() { "Save a copy" } else { "Save design" }).strong().color(t.primary_text),
                                        )
                                        .fill(t.primary_bg),
                                    )
                                    .on_hover_text("Save this design to your cloud workspace")
                                }
                            })
                            .inner;
                        if save.clicked() {
                            self.save(app, &ctx, binding.as_ref().is_some_and(|b| !b.can_edit()));
                        }
                        if let Some(b) = binding.as_ref()
                            && !compact
                            && ui.add_enabled_ui(!self.session_paused(), |ui| header_icon(ui, "message-square", "Comments")).inner.clicked()
                        {
                            self.show_comments = true;
                            self.project_data(&ctx, &b.id, "comments", format!("/api/projects/{}/comments", b.id));
                        }
                        let more = header_icon(ui, "ellipsis", "More actions");
                        egui::Popup::menu(&more).show(|ui| {
                            if compact {
                                ui.checkbox(&mut self.compact_panels, "Show editing panels");
                                if ui.button("Fit canvas").clicked() {
                                    let _ = photocraft_ui_egui::menus::invoke(app, &ctx, "view.fitOnScreen", json!({}));
                                    ui.close();
                                }
                            }
                            if ui.add_enabled(can_save, egui::Button::new("Save a copy")).clicked() {
                                self.save(app, &ctx, true);
                                ui.close();
                            }
                            if ui.button("Download .pcraft").clicked() {
                                if let Some(d) = app.session.active() {
                                    match photocraft_format::save_to_bytes(&d.doc, &Default::default()) {
                                        Ok(b) => {
                                            let _ = super::web::download(&format!("{}.pcraft", d.doc.name), &b);
                                        }
                                        Err(e) => self.status = e.to_string(),
                                    }
                                }
                                ui.close();
                            }
                            if ui.button("Export PNG").clicked() {
                                export(app, "png", &mut self.status);
                                ui.close();
                            }
                            if ui.button("Export PSD").clicked() {
                                export(app, "psd", &mut self.status);
                                ui.close();
                            }
                            if let Some(b) = binding.as_ref() {
                                if ui.add_enabled(!self.session_paused(), egui::Button::new("Version history")).clicked() {
                                    self.show_history = true;
                                    self.project_data(&ctx, &b.id, "history", format!("/api/projects/{}/versions", b.id));
                                    ui.close();
                                }
                                if ui.add_enabled(!self.session_paused(), egui::Button::new("Comments")).clicked() {
                                    self.show_comments = true;
                                    self.project_data(&ctx, &b.id, "comments", format!("/api/projects/{}/comments", b.id));
                                    ui.close();
                                }
                                if b.role == "owner"
                                    && ui
                                        .add_enabled(b.revision > 0 && !self.session_paused(), egui::Button::new("Share & permissions"))
                                        .on_disabled_hover_text("Save a complete version before sharing")
                                        .clicked()
                                {
                                    self.show_share = true;
                                    self.share_url.clear();
                                    self.project_data(&ctx, &b.id, "members", format!("/api/projects/{}/members", b.id));
                                    ui.close();
                                }
                            }
                        });
                    }
                    if !compact && let Some(b) = binding.as_ref() {
                        let saved = b.revision > 0 && app.session.active().is_some_and(|d| d.revision == b.saved_local);
                        ui.add_sized(
                            [100., 36.],
                            egui::Label::new(
                                RichText::new(if self.session_paused() {
                                    "Sign-in needed"
                                } else if saved {
                                    "Saved"
                                } else if self.busy {
                                    "Saving…"
                                } else {
                                    "Unsaved changes"
                                })
                                .small()
                                .color(t.text_dim),
                            ),
                        )
                        .on_hover_text(&self.status);
                    }
                });
            });
        });
        if self.session_paused() {
            egui::Panel::top("session_warning").frame(egui::Frame::NONE.fill(t.card).inner_margin(egui::Margin::symmetric(16, 10))).show(ui, |ui| {
                ui.spacing_mut().interact_size.y = 36.;
                ui.spacing_mut().button_padding = Vec2::new(12., 8.);
                ui.spacing_mut().item_spacing = Vec2::new(8., 8.);
                ui.style_mut().text_styles.insert(egui::TextStyle::Button, egui::FontId::proportional(14.));
                ui.add(
                    egui::Label::new(
                        RichText::new(self.session_warning.as_deref().unwrap_or("Your session expired. Sign in again to resume cloud saving and sharing."))
                            .color(t.warning),
                    )
                    .wrap(),
                );
                ui.label("Keep this editor open. Your documents stay here while you sign in in a new tab.");
                ui.horizontal_wrapped(|ui| {
                    if ui.add(egui::Button::new(RichText::new("Sign in again").color(t.primary_text)).fill(t.primary_bg)).clicked() {
                        self.sign_in_again(&ctx);
                    }
                    if ui
                        .add_enabled(
                            !self.session_check_pending && !self.logout_pending,
                            egui::Button::new(if self.session_check_pending { "Checking sign-in…" } else { "Check sign-in" }),
                        )
                        .clicked()
                    {
                        self.check_session(&ctx);
                    }
                    if ui.button("Copy sign-in link").on_hover_text("If no tab opens, paste this link into a new browser tab.").clicked()
                        && let Some(origin) = web_sys::window().and_then(|window| window.location().origin().ok())
                    {
                        ctx.copy_text(format!("{origin}/auth/login"));
                    }
                });
            });
        }
        egui::Panel::bottom("cloud_status").exact_size(27.).frame(egui::Frame::NONE.fill(t.card).inner_margin(egui::Margin::symmetric(16, 3))).show(ui, |ui| {
            // The workspace's 40px controls must not force this compact status row past
            // the viewport edge, especially on phones.
            ui.spacing_mut().interact_size.y = 16.;
            ui.spacing_mut().button_padding = Vec2::new(6., 2.);
            ui.horizontal(|ui| {
                ui.set_max_width((ui.available_width() - 110.).max(100.));
                let status = if self.error { &self.status } else { self.live.notice.as_ref().unwrap_or(&self.status) };
                ui.add(egui::Label::new(RichText::new(status).small().color(if self.error { t.warning } else { t.text_dim })).truncate()).on_hover_text(status);
                if self.error && ui.small_button("Dismiss").clicked() {
                    self.error = false;
                }
                if self.newer
                    && ui.small_button("Open latest in a new tab").clicked()
                    && let Some(b) = &binding
                {
                    self.open(&ctx, b.id.clone(), None);
                }
            });
        });
        if let Some(warning) = self.draft_write_warning.as_ref().or(self.draft_list_warning.as_ref()).cloned() {
            egui::Panel::bottom("recovery_warning").frame(egui::Frame::NONE.fill(t.card).inner_margin(egui::Margin::symmetric(16, 6))).show(ui, |ui| {
                ui.spacing_mut().interact_size.y = 28.;
                ui.spacing_mut().button_padding = Vec2::new(6., 2.);
                ui.horizontal_wrapped(|ui| {
                    ui.label(RichText::new("Browser recovery unavailable. Keep this tab open or download your work.").small().color(t.warning))
                        .on_hover_text(&warning);
                    if ui.add(egui::Button::new("Retry recovery").min_size(Vec2::new(0., 28.))).clicked() {
                        self.last_draft = None;
                        self.draft_retry_at = 0.;
                        self.draft_activity = now();
                        self.draft_visit = true;
                        self.refresh_drafts(&ctx);
                    }
                });
            });
        }
        if self.home {
            self.home_ui(app, ui);
        } else {
            let panels = app.ui.panels.clone();
            if compact {
                app.ui.panels.options_bar = false;
                if !self.compact_panels {
                    app.ui.panels.layers = false;
                    app.ui.panels.color = false;
                    app.ui.panels.history = false;
                    app.ui.panels.properties = false;
                    app.ui.panels.navigator = false;
                    app.ui.panels.character = false;
                } else {
                    app.ui.panels.toolbar = false;
                }
            }
            app.ui(ui, frame);
            if compact {
                app.ui.panels = panels;
            }
        }
        self.dialogs(app, &ctx);
    }
    fn home_action(&mut self, app: &mut PhotocraftApp, ctx: &egui::Context, action: home::Action) {
        self.cancel_open();
        match action {
            home::Action::New(w, h) => {
                new_document(app, w, h, "Untitled canvas");
                self.home = false;
            }
            home::Action::Open => app.open_dialog_file(),
            home::Action::Custom => {
                let _ = photocraft_ui_egui::menus::invoke(app, ctx, "file.new", json!({}));
                self.home = false;
            }
            home::Action::Template(index) => {
                if let Some(starter) = home::STARTERS.get(index) {
                    let slug = starter.slug;
                    self.busy = true;
                    self.status = "Opening your editable template…".into();
                    task(&self.queue, self.epoch, ctx, async move {
                        Ok(Message::Template(
                            format!("{slug}.pcraft"),
                            binary("GET", &format!("/templates/{slug}.pcraft"), None, None).await.map_err(js_error)?,
                        ))
                    });
                }
            }
        }
    }
    fn home_ui(&mut self, app: &mut PhotocraftApp, ui: &mut egui::Ui) {
        let ctx = ui.ctx().clone();
        let t = Tokens::for_kind(ThemeKind::StudioLight);
        let narrow = ui.available_width() < 900.;
        let nav = [("⌂", "Home"), ("▦", "Templates"), ("▤", "All projects"), ("☆", "Starred"), ("♧", "Shared with me"), ("♲", "Trash")];
        if !narrow {
            egui::Panel::left("workspace_nav").exact_size(214.).frame(egui::Frame::NONE.fill(egui::Color32::WHITE).inner_margin(home::NARROW_GUTTER)).show(
                ui,
                |ui| {
                    ui.add_space(home::ROW_GAP);
                    ui.label(RichText::new("YOUR WORKSPACE").font(photocraft_ui_egui::theme::medium(12.)).color(home::MUTED));
                    home::vertical_gap(ui, home::ROW_GAP);
                    for (index, (_, name)) in nav.iter().enumerate() {
                        if home::nav_button(ui, name, self.filter == *name, index).clicked() {
                            self.cancel_open();
                            self.filter = (*name).into();
                            self.search.clear();
                        }
                    }
                    home::vertical_gap(ui, home::SECTION_GAP);
                    ui.separator();
                    home::vertical_gap(ui, home::SECTION_GAP);
                    ui.label(RichText::new("Made for your ideas.").size(14.).strong().color(home::INK));
                    home::vertical_gap(ui, home::RELATED_GAP);
                    ui.label(RichText::new("Layers, brushes, type, and real PSD files. A little room to make something yours.").size(12.).color(home::MUTED));
                    home::vertical_gap(ui, home::ROW_GAP);
                    ui.hyperlink_to("Meet PhotoCraft ↗", "https://github.com/storytold/photocraft");
                    if app.session.active().is_some() {
                        home::vertical_gap(ui, home::SECTION_GAP);
                        if ui.add_sized([178., home::CONTROL_HEIGHT], home::primary("Return to editor →")).clicked() {
                            self.cancel_open();
                            self.home = false;
                        }
                    }
                },
            );
        }
        let gutter = if narrow { home::NARROW_GUTTER } else { home::OUTER_GUTTER };
        egui::CentralPanel::default().frame(egui::Frame::NONE.fill(home::PAPER).inner_margin(gutter)).show(ui, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                if narrow {
                    ui.horizontal_wrapped(|ui| {
                        for (_, name) in nav {
                            if home::compact_nav(ui, name, self.filter == name).clicked() {
                                self.cancel_open();
                                self.filter = name.into();
                                self.search.clear();
                            }
                        }
                    });
                    home::vertical_gap(ui, home::SECTION_GAP);
                }
                if narrow {
                    ui.label(RichText::new(&self.filter).size(24.).strong().color(home::INK));
                    home::vertical_gap(ui, home::ROW_GAP);
                }
                ui.horizontal(|ui| {
                    if !narrow {
                        ui.label(RichText::new(&self.filter).size(24.).strong().color(home::INK));
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        ui.spacing_mut().item_spacing.x = home::RELATED_GAP;
                        if ui.add_sized([132., home::CONTROL_HEIGHT], home::primary("Create a design")).clicked() {
                            self.cancel_open();
                            let _ = photocraft_ui_egui::menus::invoke(app, &ctx, "file.new", json!({}));
                            self.home = false;
                        }
                        if ui.add_sized([88., home::CONTROL_HEIGHT], home::secondary("Open file")).clicked() {
                            self.cancel_open();
                            app.open_dialog_file();
                        }
                    });
                });
                home::vertical_gap(ui, home::ROW_GAP);
                ui.horizontal(|ui| {
                    let hint = if self.filter == "Templates" {
                        "Search templates"
                    } else if self.filter == "Home" && self.user.is_none() {
                        "Search templates and projects"
                    } else {
                        "Search projects or folders"
                    };
                    let reserve = if self.search.is_empty() { 0. } else { 66. };
                    let width = (ui.available_width() - reserve).clamp(100., 420.);
                    ui.add_sized(
                        [width, home::CONTROL_HEIGHT],
                        egui::TextEdit::singleline(&mut self.search).hint_text(hint).margin(egui::Margin::symmetric(12, 10)),
                    );
                    if !self.search.is_empty() && ui.button("Clear").clicked() {
                        self.search.clear();
                    }
                });
                home::vertical_gap(ui, home::SECTION_GAP);
                if self.filter == "Home" && self.user.is_none() && self.search.is_empty() {
                    if !narrow {
                        home::hero(ui, &self.textures);
                        home::vertical_gap(ui, home::SECTION_GAP);
                    }
                    if let Some(a) = home::quick_sizes(ui) {
                        self.home_action(app, &ctx, a);
                    }
                    home::vertical_gap(ui, home::SECTION_GAP);
                }
                if self.filter == "Templates" || (self.filter == "Home" && self.user.is_none()) {
                    if let Some(a) = home::gallery(ui, &self.textures, &mut self.template_category, &self.search) {
                        self.home_action(app, &ctx, a);
                    }
                    home::vertical_gap(ui, home::SECTION_GAP);
                }
                if self.filter != "Templates" {
                    if self.filter == "Home" {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new("Recent projects").size(20.).strong().color(home::INK));
                            if ui.button("All projects →").clicked() {
                                self.cancel_open();
                                self.filter = "All projects".into();
                            }
                        });
                        home::vertical_gap(ui, home::ROW_GAP);
                    }
                    let limit = if self.filter == "Home" { 12 } else { usize::MAX };
                    let items = self.projects.iter().filter(|p| home::project_matches(p, &self.filter, &self.search)).take(limit).cloned().collect::<Vec<_>>();
                    if items.is_empty() {
                        egui::Frame::new().fill(t.card).corner_radius(t.radius_lg).inner_margin(24).show(ui, |ui| {
                            ui.set_min_width((ui.available_width() - 48.).max(100.));
                            let (title, help) = home::empty_message(&self.filter, !self.search.trim().is_empty(), self.user.is_some());
                            ui.label(RichText::new(title).size(18.).strong().color(home::INK));
                            home::vertical_gap(ui, home::RELATED_GAP);
                            ui.label(RichText::new(help).color(home::MUTED));
                        });
                    }
                    let cols = home::grid_columns(ui.available_width(), 240., 6);
                    for (row_index, row) in items.chunks(cols).enumerate() {
                        if row_index > 0 {
                            home::vertical_gap(ui, home::ROW_GAP);
                        }
                        ui.columns(cols, |uis| {
                            for (i, p) in row.iter().enumerate() {
                                if let Some(ui) = uis.get_mut(i) {
                                    self.project_card(app, ui, p, &ctx);
                                }
                            }
                        });
                    }
                    if !self.drafts.is_empty() && matches!(self.filter.as_str(), "Home" | "All projects") {
                        home::vertical_gap(ui, home::SECTION_GAP);
                        ui.label(RichText::new("Browser recovery").size(18.).strong());
                        ui.label("Your most recently visited document is kept here. Visiting another replaces this copy.");
                        home::vertical_gap(ui, home::RELATED_GAP);
                        for (key, v) in self.drafts.clone() {
                            ui.horizontal(|ui| {
                                ui.label(field(&v, "name"));
                                if ui.button("Recover").clicked() {
                                    self.cancel_open();
                                    let key = key.clone();
                                    task(&self.queue, self.epoch, &ctx, async move {
                                        let (name, b) = draft_get(&key).await?;
                                        Ok(Message::Recovered(name, b))
                                    });
                                }
                            });
                        }
                    }
                }
                home::vertical_gap(ui, f32::from(home::OUTER_GUTTER));
            });
        });
    }
    fn project_card(&mut self, app: &mut PhotocraftApp, ui: &mut egui::Ui, p: &Value, ctx: &egui::Context) {
        if self.session_paused() {
            ui.disable();
        }
        let t = Tokens::for_kind(ThemeKind::StudioLight);
        let id = field(p, "id").to_string();
        let title = field(p, "title");
        let incomplete = p.get("revision").and_then(Value::as_i64).unwrap_or(0) == 0;
        let owner = field(p, "role") == "owner";
        let trashed = p.get("trashed").and_then(Value::as_bool) == Some(true);
        let local_document = app.session.documents().iter().position(|d| self.bindings.get(&d.doc.id).is_some_and(|b| b.id == id));
        egui::Frame::new().fill(t.card).stroke(egui::Stroke::new(1., t.card_border)).corner_radius(t.radius_lg).inner_margin(12).show(ui, |ui| {
            // Image/title separation and title/metadata spacing are local to the card.
            ui.spacing_mut().item_spacing.y = home::RELATED_GAP / 2.;
            let width = ui.available_width();
            let (rect, response) = ui.allocate_exact_size(Vec2::new(width, 168.), if incomplete { egui::Sense::hover() } else { egui::Sense::click() });
            ui.painter().rect_filled(rect, t.radius_sm, t.accent_soft);
            if let Some(texture) = self.textures.get(&id) {
                let size = texture.size_vec2();
                let scale = ((width - 24.) / size.x).min(144. / size.y);
                ui.painter().image(
                    texture.id(),
                    egui::Rect::from_center_size(rect.center(), size * scale),
                    egui::Rect::from_min_max(egui::Pos2::ZERO, egui::pos2(1., 1.)),
                    egui::Color32::WHITE,
                );
            } else {
                ui.painter().text(
                    rect.center(),
                    egui::Align2::CENTER_CENTER,
                    if incomplete {
                        "First save incomplete".into()
                    } else {
                        format!("{} × {}", p.get("width").and_then(Value::as_i64).unwrap_or(0), p.get("height").and_then(Value::as_i64).unwrap_or(0))
                    },
                    egui::FontId::proportional(14.),
                    t.text_dim,
                );
            }
            response.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::Button, !incomplete, format!("Open {title}")));
            if !incomplete && (response.hovered() || response.has_focus()) {
                ui.painter().rect_stroke(rect, t.radius_sm, egui::Stroke::new(2., t.accent), egui::StrokeKind::Inside);
            }
            if !incomplete && response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                self.open(ctx, id.clone(), None);
            }
            home::vertical_gap(ui, home::RELATED_GAP);
            ui.horizontal(|ui| {
                let title_width = (ui.available_width() - 52.).max(40.);
                if ui
                    .add_sized(
                        [title_width, 40.],
                        egui::Label::new(RichText::new(title).size(14.).strong()).halign(egui::Align::Min).truncate().sense(if incomplete {
                            egui::Sense::hover()
                        } else {
                            egui::Sense::click()
                        }),
                    )
                    .on_hover_text(title)
                    .clicked()
                    && !incomplete
                {
                    // Selectable egui labels can report clicks even with Sense::hover().
                    self.open(ctx, id.clone(), None);
                }
                let menu = ui.button("…");
                egui::Popup::menu(&menu)
                    .style(|style: &mut egui::Style| {
                        style.visuals = egui::Visuals::light();
                        egui::containers::menu::menu_style(style);
                        style.spacing.interact_size.y = 32.;
                        style.spacing.button_padding = Vec2::new(12., 8.);
                        style.text_styles.insert(egui::TextStyle::Button, egui::FontId::proportional(14.));
                    })
                    .show(|ui| {
                        if ui.add_enabled(!incomplete, egui::Button::new("Open")).on_disabled_hover_text("The first save has not completed yet").clicked() {
                            self.open(ctx, id.clone(), None);
                            ui.close();
                        }
                        if ui
                            .add_enabled(!incomplete, egui::Button::new("Duplicate"))
                            .on_disabled_hover_text("Save the document before making a cloud copy")
                            .clicked()
                        {
                            self.mutate(ctx, "POST", format!("/api/projects/{id}/duplicate"), json!({}));
                            ui.close();
                        }
                        if field(p, "role") == "owner" {
                            if ui.button("Rename / folder").clicked() {
                                self.details_id = id.clone();
                                self.rename = title.into();
                                self.folder = field(p, "folder").into();
                                self.show_details = true;
                                ui.close();
                            }
                            let starred = p.get("starred").and_then(Value::as_bool) == Some(true);
                            if ui.button(if starred { "Remove star" } else { "Star project" }).clicked() {
                                self.mutate(ctx, "PATCH", format!("/api/projects/{id}"), json!({"starred":!starred}));
                                ui.close();
                            }
                            let trash = p.get("trashed").and_then(Value::as_bool) == Some(true);
                            if ui.button(if trash { "Restore project" } else { "Move to Trash" }).clicked() {
                                self.mutate(ctx, "PATCH", format!("/api/projects/{id}"), json!({"trashed":!trash}));
                                ui.close();
                            }
                        }
                    });
            });
            if incomplete {
                ui.label(RichText::new("Not saved to cloud").size(12.).color(t.text_dim));
                if let Some(index) = local_document {
                    ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                        ui.label("Your document is still open in this browser.");
                    });
                    if ui.add_enabled(!self.busy && !trashed, home::primary("Retry save")).clicked() && app.session.set_active(index) {
                        self.cancel_open();
                        app.sync_views();
                        self.home = false;
                        self.save(app, ctx, false);
                    }
                    if trashed {
                        ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                            ui.label("Restore the project before retrying its save.");
                        });
                    }
                } else {
                    ui.with_layout(egui::Layout::top_down(egui::Align::Min), |ui| {
                        ui.label("Open the original document or a browser recovery copy to save it again.");
                    });
                }
                if owner && ui.button(if trashed { "Restore project" } else { "Move to Trash" }).clicked() {
                    self.mutate(ctx, "PATCH", format!("/api/projects/{id}"), json!({"trashed":!trashed}));
                }
            } else {
                ui.label(
                    RichText::new(format!(
                        "{}  ·  Version {}",
                        if owner { "Your project" } else { "Shared project" },
                        p.get("revision").and_then(Value::as_i64).unwrap_or(0)
                    ))
                    .small()
                    .color(t.text_dim),
                );
            }
            if !field(p, "folder").is_empty() {
                ui.add(egui::Label::new(RichText::new(field(p, "folder")).size(12.).color(t.accent)).truncate()).on_hover_text(field(p, "folder"));
            }
        });
    }
    fn dialogs(&mut self, app: &mut PhotocraftApp, ctx: &egui::Context) {
        self.sync_dialog_project(app);
        if self.show_logout || self.show_share || self.show_history || self.show_comments || self.show_details {
            egui::Area::new(egui::Id::new("workspace-dialog-backdrop")).order(egui::Order::Background).fixed_pos(ctx.content_rect().min).show(ctx, |ui| {
                ui.allocate_rect(ctx.content_rect(), egui::Sense::click_and_drag());
            });
        }
        if self.show_logout {
            let mut open = true;
            workspace_window("Sign out of this workspace?", ctx, 440.).default_height(280.).open(&mut open).show(ctx, |ui| {
                ui.style_mut().text_styles.insert(egui::TextStyle::Body, egui::FontId::proportional(14.));
                ui.style_mut().text_styles.insert(egui::TextStyle::Button, egui::FontId::proportional(14.));
                ui.spacing_mut().item_spacing = Vec2::new(10., 12.);
                ui.spacing_mut().interact_size.y = 36.;
                ui.spacing_mut().button_padding = Vec2::new(12., 8.);
                ui.label("Unsaved tabs and browser recovery copies will be cleared. Download any work you want to keep first.");
                if ui.button("Download current document").clicked()
                    && let Some(d) = app.session.active()
                    && let Ok(bytes) = photocraft_format::save_to_bytes(&d.doc, &Default::default())
                {
                    let _ = super::web::download(&format!("{}.pcraft", d.doc.name), &bytes);
                }
                if ui.add_enabled(!self.logout_pending && (!self.busy || self.session_paused()), egui::Button::new("Sign out and clear this browser")).clicked()
                {
                    self.busy = true;
                    self.logout_pending = true;
                    let http = self.http(ctx);
                    let allowed = self.draft_allowed.clone();
                    let generation = self.draft_generation.clone();
                    task(&self.queue, self.epoch, ctx, async move {
                        let result = async {
                            // Explicit sign-out may bypass the paused request gate, but
                            // still observes typed401 and never clears another account.
                            http.finish(api("POST", "/api/logout", Some(json!({})), http.account.as_deref()).await)?;
                            allowed.set(false);
                            generation.set(generation.get().wrapping_add(1));
                            clear_drafts().await
                        }
                        .await;
                        Ok(Message::SignedOut(result))
                    });
                }
            });
            self.show_logout &= open;
        }
        if self.session_paused() {
            return;
        }
        let Some(binding) = self.binding(app) else {
            self.show_share = false;
            self.show_history = false;
            self.show_comments = false;
            if self.show_details {
                self.details_dialog(ctx);
            }
            return;
        };
        if binding.revision <= 0 {
            self.show_share = false;
        }
        let pid = binding.id.clone();
        if self.show_share {
            let mut open = true;
            let content_width = 440_f32.min((ctx.content_rect().width() - 64.).max(240.)) - 40.;
            let feedback_height = |message: &str| {
                ctx.fonts_mut(|fonts| fonts.layout(message.into(), egui::FontId::proportional(14.), egui::Color32::WHITE, content_width).size().y) + 12.
            };
            let invitation_height = if self.invitation_pending.as_deref() == Some(pid.as_str()) {
                32.
            } else {
                self.invitation_result.as_ref().filter(|(project, _, _)| project == &pid).map_or(0., |(_, message, _)| feedback_height(message))
            };
            let project_feedback_height: f32 =
                ["members", "share"].iter().filter_map(|kind| self.project_feedback.get(kind)).map(|(message, _)| feedback_height(message)).sum();
            workspace_window("Share & permissions", ctx, 440.)
                .min_height(
                    (480.
                        + if ctx.content_rect().width() < 520. { 48. } else { 0. }
                        + 76. * self.members.len().min(3) as f32
                        + invitation_height
                        + project_feedback_height
                        + if self.share_url.is_empty() { 0. } else { 84. })
                    .min((ctx.content_rect().height() - 100.).max(200.)),
                )
                .open(&mut open)
                .show(ctx, |ui| {
                    ui.style_mut().text_styles.insert(egui::TextStyle::Body, egui::FontId::proportional(14.));
                    ui.style_mut().text_styles.insert(egui::TextStyle::Button, egui::FontId::proportional(14.));
                    ui.spacing_mut().item_spacing = Vec2::new(10., 12.);
                    ui.spacing_mut().interact_size.y = 36.;
                    ui.spacing_mut().button_padding = Vec2::new(12., 8.);
                    ui.label("Invite collaborators by email");
                    ui.label("Send a sign-in email and choose what they can do.");
                    ui.add_space(10.);
                    ui.horizontal_wrapped(|ui| {
                        let width = (ui.available_width() - 130.).max(140.);
                        ui.add(
                            egui::TextEdit::singleline(&mut self.member_email)
                                .hint_text("name@example.com")
                                .desired_width(width)
                                .margin(egui::Margin::symmetric(10, 9)),
                        );
                        egui::ComboBox::from_id_salt("member_role").selected_text(if self.member_role == "edit" { "Can edit" } else { "Can view" }).show_ui(
                            ui,
                            |ui| {
                                ui.selectable_value(&mut self.member_role, "view".into(), "Can view");
                                ui.selectable_value(&mut self.member_role, "edit".into(), "Can edit");
                            },
                        );
                    });
                    let sending_invitation = self.invitation_pending.as_deref() == Some(pid.as_str());
                    if ui
                        .add_enabled(
                            !self.busy && self.invitation_pending.is_none() && !self.member_writes.contains_key(&pid) && !self.member_email.trim().is_empty(),
                            egui::Button::new(
                                RichText::new(if sending_invitation { "Sending invitation…" } else { "Send invitation" }).color(Tokens::get(ctx).primary_text),
                            )
                            .fill(Tokens::get(ctx).primary_bg),
                        )
                        .clicked()
                    {
                        self.invitation_pending = Some(pid.clone());
                        self.invitation_result = None;
                        ctx.request_repaint();
                        let request = self.project_request(&pid, "members");
                        self.member_writes.insert(pid.clone(), request.generation);
                        let path = format!("/api/projects/{pid}/members");
                        let email = self.member_email.trim().to_string();
                        let v = json!({"email":email,"role":self.member_role});
                        cloud_task(self.http(ctx), move |http| async move {
                            let outcome = http.api("POST", &path.replace("/members", "/invite"), Some(v)).await.map(|_| ());
                            // Access may already be granted when the mail service reports failure.
                            let members = http.api("GET", &path, None).await;
                            Ok(Message::Invited { request, email, outcome, members })
                        });
                    }
                    if sending_invitation {
                        ui.horizontal(|ui| {
                            ui.add(egui::Spinner::new().size(16.));
                            ui.label("Waiting for the email service…");
                        });
                    } else if let Some((project, message, failed)) = &self.invitation_result
                        && project == &pid
                    {
                        ui.add(egui::Label::new(RichText::new(message).color(if *failed { Tokens::get(ctx).warning } else { Tokens::get(ctx).text })).wrap());
                    }
                    self.project_feedback_ui(ui, "members");
                    for m in self.members.clone() {
                        let email = field(&m, "email");
                        let mut role = field(&m, "role").to_string();
                        ui.horizontal(|ui| {
                            ui.add_sized([(ui.available_width() - 130.).max(80.), 36.], egui::Label::new(email).halign(egui::Align::Min).truncate())
                                .on_hover_text(email);
                            ui.add_enabled_ui(!self.member_writes.contains_key(&pid), |ui| {
                                egui::ComboBox::from_id_salt(("access", email)).selected_text(if role == "edit" { "Can edit" } else { "Can view" }).show_ui(
                                    ui,
                                    |ui| {
                                        ui.selectable_value(&mut role, "view".into(), "Can view");
                                        ui.selectable_value(&mut role, "edit".into(), "Can edit");
                                        ui.separator();
                                        ui.selectable_value(&mut role, "remove".into(), "Remove access");
                                    },
                                );
                            });
                        });
                        ui.add(
                            egui::Label::new(
                                RichText::new(match field(&m, "delivery") {
                                    "failed" => "Email not confirmed · access granted",
                                    "sent" => "Email accepted · access granted",
                                    _ => "Access granted",
                                })
                                .small()
                                .color(Tokens::get(ctx).text_dim),
                            )
                            .wrap(),
                        );
                        if role != field(&m, "role") {
                            let path = format!("/api/projects/{pid}/members");
                            let email = email.to_string();
                            let request = self.project_request(&pid, "members");
                            self.member_writes.insert(pid.clone(), request.generation);
                            cloud_task(self.http(ctx), move |http| async move {
                                let result = async {
                                    http.api("PUT", &path, Some(json!({"email":email,"role":role}))).await?;
                                    http.api("GET", &path, None).await
                                }
                                .await;
                                Ok(Message::ProjectData(request, result))
                            });
                        }
                    }
                    ui.add_space(14.);
                    ui.separator();
                    ui.add_space(10.);
                    ui.label("View link");
                    ui.label("Anyone with the link can view and download the latest saved version.");
                    if ui.add_enabled(!self.project_pending.contains_key("share"), egui::Button::new("Create a new view link")).clicked() {
                        self.share_url.clear();
                        let path = format!("/api/projects/{pid}/share");
                        let request = self.project_request(&pid, "share");
                        cloud_task(
                            self.http(ctx),
                            move |http| async move { Ok(Message::ProjectData(request, http.api("POST", &path, Some(json!({}))).await)) },
                        );
                    }
                    self.project_feedback_ui(ui, "share");
                    if !self.share_url.is_empty() {
                        ui.add(egui::TextEdit::singleline(&mut self.share_url).desired_width(ui.available_width()));
                        if ui.button("Copy link").clicked() {
                            ctx.copy_text(self.share_url.clone());
                            self.status = "View link copied".into();
                        }
                    }
                    if ui.add_enabled(!self.project_pending.contains_key("share"), egui::Button::new("Revoke all view links")).clicked() {
                        let path = format!("/api/projects/{pid}/share");
                        let request = self.project_request(&pid, "share_revoked");
                        cloud_task(self.http(ctx), move |http| async move { Ok(Message::ProjectData(request, http.api("DELETE", &path, None).await)) });
                        self.share_url.clear();
                    }
                });
            self.show_share = open;
        }
        if self.show_history {
            let mut open = true;
            workspace_window("Version history", ctx, 460.).open(&mut open).show(ctx, |ui| {
                ui.style_mut().text_styles.insert(egui::TextStyle::Body, egui::FontId::proportional(14.));
                ui.style_mut().text_styles.insert(egui::TextStyle::Button, egui::FontId::proportional(14.));
                ui.spacing_mut().item_spacing = Vec2::new(10., 12.);
                ui.spacing_mut().interact_size.y = 36.;
                ui.spacing_mut().button_padding = Vec2::new(12., 8.);
                ui.label("Open any saved version as another tab. Your current edits stay open.");
                self.project_feedback_ui(ui, "history");
                egui::ScrollArea::vertical().max_height(450.).show(ui, |ui| {
                    for v in self.history.clone() {
                        ui.group(|ui| {
                            let rev = v.get("revision").and_then(Value::as_i64).unwrap_or(0);
                            ui.label(RichText::new(format!("Version {rev} · {}", field(&v, "title"))).strong());
                            ui.label(format!("{} · {}", field(&v, "author"), field(&v, "createdAt")));
                            if ui.button("Open this version").clicked() {
                                self.open(ctx, pid.clone(), Some(rev));
                            }
                        });
                    }
                });
            });
            self.show_history = open;
        }
        if self.show_comments {
            let mut open = true;
            workspace_window("Comments", ctx, 430.).open(&mut open).show(ctx, |ui| {
                ui.style_mut().text_styles.insert(egui::TextStyle::Body, egui::FontId::proportional(14.));
                ui.style_mut().text_styles.insert(egui::TextStyle::Button, egui::FontId::proportional(14.));
                ui.spacing_mut().item_spacing = Vec2::new(10., 12.);
                ui.spacing_mut().interact_size.y = 36.;
                ui.spacing_mut().button_padding = Vec2::new(12., 8.);
                if !self.people.is_empty() {
                    ui.label(format!("Here now: {}", self.people.join(", ")));
                }
                ui.add(
                    egui::TextEdit::multiline(&mut self.comment).hint_text("Leave feedback for your team…").desired_width(ui.available_width()).desired_rows(3),
                );
                if ui.add_enabled(!self.comment.trim().is_empty(), egui::Button::new("Post comment")).clicked() {
                    let path = format!("/api/projects/{pid}/comments");
                    let body = self.comment.clone();
                    let request = self.project_request(&pid, "comment_posted");
                    cloud_task(self.http(ctx), move |http| async move {
                        let result = async {
                            http.api("POST", &path, Some(json!({"body":body}))).await?;
                            let comments = http.api("GET", &path, None).await?;
                            Ok(json!({"comments":comments,"submitted":body}))
                        }
                        .await;
                        Ok(Message::ProjectData(request, result))
                    });
                }
                if ui.button("Refresh comments").clicked() {
                    self.project_data(ctx, &pid, "comments", format!("/api/projects/{pid}/comments"));
                }
                self.project_feedback_ui(ui, "comments");
                egui::ScrollArea::vertical().max_height(400.).show(ui, |ui| {
                    for c in self.comments.clone() {
                        ui.group(|ui| {
                            ui.label(RichText::new(field(&c, "author")).strong());
                            ui.label(field(&c, "body"));
                            let resolved = c.get("resolved").and_then(Value::as_bool) == Some(true);
                            if binding.can_edit() && ui.small_button(if resolved { "Reopen" } else { "Resolve" }).clicked() {
                                let path = format!("/api/projects/{pid}/comments");
                                let cid = field(&c, "id").to_string();
                                let request = self.project_request(&pid, "comments");
                                cloud_task(self.http(ctx), move |http| async move {
                                    let result = async {
                                        http.api("PUT", &format!("{path}/{cid}"), Some(json!({"resolved":!resolved}))).await?;
                                        http.api("GET", &path, None).await
                                    }
                                    .await;
                                    Ok(Message::ProjectData(request, result))
                                });
                            } else if resolved {
                                ui.small("Resolved");
                            }
                        });
                    }
                });
            });
            self.show_comments = open;
        }
        if self.show_details {
            self.details_dialog(ctx);
        }
    }
    fn details_dialog(&mut self, ctx: &egui::Context) {
        let mut open = true;
        workspace_window("Project details", ctx, 400.).default_height(280.).open(&mut open).show(ctx, |ui| {
            ui.style_mut().text_styles.insert(egui::TextStyle::Body, egui::FontId::proportional(14.));
            ui.style_mut().text_styles.insert(egui::TextStyle::Button, egui::FontId::proportional(14.));
            ui.spacing_mut().item_spacing = Vec2::new(10., 12.);
            ui.spacing_mut().interact_size.y = 36.;
            ui.spacing_mut().button_padding = Vec2::new(12., 8.);
            ui.label("Name");
            ui.add(egui::TextEdit::singleline(&mut self.rename).desired_width(ui.available_width()).margin(egui::Margin::symmetric(10, 9)));
            ui.label("Folder");
            ui.add(egui::TextEdit::singleline(&mut self.folder).desired_width(ui.available_width()).margin(egui::Margin::symmetric(10, 9)));
            if ui
                .add_enabled(
                    !self.rename.trim().is_empty(),
                    egui::Button::new(RichText::new("Save details").color(Tokens::get(ctx).primary_text)).fill(Tokens::get(ctx).primary_bg),
                )
                .clicked()
            {
                self.mutate(ctx, "PATCH", format!("/api/projects/{}", self.details_id), json!({"title":self.rename.trim(),"folder":self.folder.trim()}));
                self.show_details = false;
            }
        });
        self.show_details &= open;
    }
}

fn header_icon(ui: &mut egui::Ui, icon: &str, label: &str) -> egui::Response {
    let t = Tokens::get(ui.ctx());
    let response = ui.add(egui::Button::image(photocraft_ui_egui::icons::image(icon, 18., t.icon)).small().min_size(Vec2::splat(36.)));
    response.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::Button, ui.is_enabled(), label));
    response.on_hover_text(label)
}

fn workspace_window<'a>(title: &'a str, ctx: &egui::Context, width: f32) -> egui::Window<'a> {
    let t = Tokens::get(ctx);
    let width = width.min((ctx.content_rect().width() - 64.).max(240.));
    let id = egui::Id::new(("workspace-dialog", title));
    // Reuse egui's modal focus boundary so sharing never paints the canvas behind it.
    ctx.memory_mut(|memory| memory.set_modal_layer(egui::LayerId::new(egui::Order::Middle, id)));
    if !egui::Popup::is_any_open(ctx) {
        ctx.move_to_top(egui::LayerId::new(egui::Order::Middle, id));
    }
    egui::Window::new(title)
        .id(id)
        .order(egui::Order::Middle)
        .anchor(egui::Align2::CENTER_CENTER, Vec2::ZERO)
        .collapsible(false)
        .resizable(false)
        .default_width(width)
        .default_height(480.)
        .max_width(width)
        .max_height((ctx.content_rect().height() - 100.).max(200.))
        .vscroll(true)
        .frame(
            egui::Frame::window(&ctx.style_of(ctx.theme()))
                .fill(t.card)
                .stroke(egui::Stroke::new(1., t.card_border))
                .inner_margin(20)
                .corner_radius(t.radius_lg),
        )
}

fn new_document(app: &mut PhotocraftApp, w: u32, h: u32, name: &str) {
    match app.session.execute("file.new", json!({"width":w,"height":h,"name":name,"background":"white"})) {
        Ok(_) => app.sync_views(),
        Err(e) => {
            app.ui.status = e.to_string();
            app.ui.status_error = true;
        }
    }
}

async fn recovery_db() -> Result<rexie::Rexie, String> {
    rexie::Rexie::builder("photocraft-studio-recovery").version(1).add_object_store(rexie::ObjectStore::new("drafts")).build().await.map_err(js_error)
}
fn recovery_time(value: &JsValue) -> f64 {
    js_sys::Reflect::get(value, &JsValue::from_str("savedAt")).ok().and_then(|v| v.as_f64()).filter(|v| v.is_finite()).unwrap_or(0.)
}
async fn recovery_rows(store: &rexie::Store, scope: &str) -> Result<Vec<(String, f64)>, String> {
    let keys = store.get_all_keys(None, None).await.map_err(js_error)?;
    let mut rows = Vec::new();
    let prefix = format!("{scope}:");
    for key in keys {
        if let Some(k) = key.as_string().filter(|k| k.starts_with(&prefix) || k.starts_with("guest:"))
            && let Some(value) = store.get(key).await.map_err(js_error)?
        {
            // Legacy stores may contain many large native files. Do not retain their byte arrays.
            rows.push((k, recovery_time(&value)));
        }
    }
    Ok(rows)
}
async fn draft_put(scope: &str, key: &str, name: &str, bytes: &[u8], write: RecoveryWrite) -> Result<bool, String> {
    let db = recovery_db().await?;
    if !write.is_current() {
        return Ok(false);
    }
    let obj = js_sys::Object::new();
    for (k, v) in [("name", JsValue::from_str(name)), ("data", js_sys::Uint8Array::from(bytes).into()), ("savedAt", JsValue::from_f64(write.saved_at))] {
        js_sys::Reflect::set(&obj, &JsValue::from_str(k), &v).map_err(|_| "Recovery data could not be encoded")?;
    }
    let tx = db.transaction(&["drafts"], rexie::TransactionMode::ReadWrite).map_err(js_error)?;
    let store = tx.store("drafts").map_err(js_error)?;
    let result = async {
        let rows = recovery_rows(&store, scope).await?;
        // Transactions serialize tabs too. A delayed older snapshot cannot evict a newer one.
        if !write.is_current() || rows.iter().any(|(_, saved_at)| *saved_at > write.saved_at) {
            return Ok(false);
        }
        store.put(&obj, Some(&JsValue::from_str(key))).await.map_err(js_error)?;
        for (old, _) in rows {
            if old != key {
                store.delete(JsValue::from_str(&old)).await.map_err(js_error)?;
            }
        }
        Ok(true)
    }
    .await;
    match result {
        Ok(written) => {
            if written && !write.is_current() {
                tx.abort().await.map_err(js_error)?;
                return Ok(false);
            }
            tx.done().await.map_err(js_error)?;
            Ok(written)
        }
        Err(error) => {
            let _ = tx.abort().await;
            Err(error)
        }
    }
}
async fn draft_list(scope: &str) -> Result<Vec<(String, Value)>, String> {
    let db = recovery_db().await?;
    let tx = db.transaction(&["drafts"], rexie::TransactionMode::ReadWrite).map_err(js_error)?;
    let store = tx.store("drafts").map_err(js_error)?;
    let result = async {
        let rows = recovery_rows(&store, scope).await?;
        let Some((key, _)) = rows.iter().max_by(|a, b| a.1.total_cmp(&b.1).then_with(|| a.0.cmp(&b.0))) else {
            return Ok(Vec::new());
        };
        let value = store.get(JsValue::from_str(key)).await.map_err(js_error)?.ok_or("Recovery copy is unavailable")?;
        // Adopt the guest handoff into this account; other accounts remain isolated.
        let retained = key.strip_prefix("guest:").map(|document| format!("{scope}:{document}")).unwrap_or_else(|| key.clone());
        if retained != *key {
            store.put(&value, Some(&JsValue::from_str(&retained))).await.map_err(js_error)?;
        }
        for (old, _) in &rows {
            if *old != retained {
                store.delete(JsValue::from_str(old)).await.map_err(js_error)?;
            }
        }
        let name = js_sys::Reflect::get(&value, &JsValue::from_str("name")).ok().and_then(|v| v.as_string()).unwrap_or_else(|| "Recovered document".into());
        Ok(vec![(retained, json!({"name":name}))])
    }
    .await;
    match result {
        Ok(drafts) => {
            tx.done().await.map_err(js_error)?;
            Ok(drafts)
        }
        Err(error) => {
            let _ = tx.abort().await;
            Err(error)
        }
    }
}
async fn draft_get(key: &str) -> Result<(String, Vec<u8>), String> {
    let db = recovery_db().await?;
    let tx = db.transaction(&["drafts"], rexie::TransactionMode::ReadOnly).map_err(js_error)?;
    let v = tx
        .store("drafts")
        .map_err(js_error)?
        .get(JsValue::from_str(key))
        .await
        .map_err(js_error)?
        .ok_or("This recovery copy was replaced by a more recently visited document. Reload the workspace to see the latest copy.")?;
    let name = js_sys::Reflect::get(&v, &JsValue::from_str("name")).ok().and_then(|v| v.as_string()).unwrap_or_else(|| "Recovered.pcraft".into());
    let data = js_sys::Reflect::get(&v, &JsValue::from_str("data")).map_err(|_| "Recovery copy is invalid")?;
    Ok((format!("{name}.pcraft"), js_sys::Uint8Array::new(&data).to_vec()))
}
async fn clear_drafts() -> Result<(), String> {
    let db = recovery_db().await?;
    let tx = db.transaction(&["drafts"], rexie::TransactionMode::ReadWrite).map_err(js_error)?;
    tx.store("drafts").map_err(js_error)?.clear().await.map_err(js_error)?;
    tx.done().await.map_err(js_error)?;
    Ok(())
}

fn export(app: &PhotocraftApp, extension: &str, status: &mut String) {
    if let Some(d) = app.session.active() {
        let name = format!("{}.{extension}", d.doc.name);
        match photocraft_io::export(&d.doc, &name, &Default::default()) {
            Ok(v) => {
                if let Err(e) = super::web::download(&name, &v.bytes) {
                    *status = e;
                }
            }
            Err(e) => *status = e.to_string(),
        }
    }
}
fn url_param(name: &str) -> Option<String> {
    let search = web_sys::window()?.location().search().ok()?;
    search.trim_start_matches('?').split('&').find_map(|p| {
        let (k, v) = p.split_once('=')?;
        (k == name).then(|| v.to_string())
    })
}
