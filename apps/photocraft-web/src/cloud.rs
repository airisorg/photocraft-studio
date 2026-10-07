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
    collections::HashMap,
    rc::Rc,
};
use wasm_bindgen::JsValue;

const CHUNK: usize = 524_288;
type Queue = Rc<RefCell<Vec<(u64, Message)>>>;
enum Message {
    Boot(Value, Option<Value>),
    List(Value),
    Opened(Value, Vec<u8>),
    Saved(DocId, u64, String, i64, bool),
    Synced(DocId, u64, Value, Vec<u8>),
    Presence(String, Value),
    Created(DocId, String),
    SignedOut,
    SignInReady,
    Data(&'static str, Value),
    Preview(String, Vec<u8>),
    Error(String),
    Notice(String),
    Drafts(Vec<(String, Value)>),
    Recovered(String, Vec<u8>),
    Template(String, Vec<u8>),
}
#[derive(Clone)]
struct Binding {
    id: String,
    revision: i64,
    saved_local: u64,
    role: String,
}
pub struct Cloud {
    queue: Queue,
    epoch: u64,
    pub home: bool,
    configured: bool,
    booted: bool,
    sign_in: bool,
    user: Option<Value>,
    projects: Vec<Value>,
    bindings: HashMap<DocId, Binding>,
    textures: HashMap<String, TextureHandle>,
    search: String,
    filter: String,
    template_category: String,
    status: String,
    error: bool,
    busy: bool,
    last_change: f64,
    observed: Option<(DocId, u64)>,
    last_poll: f64,
    last_draft: Option<(DocId, u64)>,
    drafts: Vec<(String, Value)>,
    show_share: bool,
    show_history: bool,
    show_comments: bool,
    member_email: String,
    member_role: String,
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
async fn api(method: &str, path: &str, body: Option<Value>) -> Result<Value, String> {
    let req = request(method, path);
    let res = if let Some(v) = body { req.json(&v).map_err(js_error)?.send().await } else { req.send().await }
        .map_err(|_| "Network unavailable. Your document stays open; retry when connected.".to_string())?;
    let status = res.status();
    let json = res.json::<Value>().await.map_err(|_| "The server returned an invalid response".to_string())?;
    if !(200..300).contains(&status) {
        return Err(json.get("error").and_then(Value::as_str).unwrap_or("Cloud request failed").to_string());
    }
    Ok(json)
}
async fn binary(method: &str, path: &str, body: Option<&[u8]>) -> Result<Vec<u8>, String> {
    let req = request(method, path);
    let res = if let Some(b) = body {
        req.header("Content-Type", "application/octet-stream").body(js_sys::Uint8Array::from(b)).map_err(js_error)?.send().await
    } else {
        req.send().await
    }
    .map_err(|_| "Network unavailable; retry when connected".to_string())?;
    if !res.ok() {
        let v = res.json::<Value>().await.unwrap_or(Value::Null);
        return Err(field(&v, "error").to_string());
    }
    res.binary().await.map_err(js_error)
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
async fn download_project(path: String, revision: Option<i64>) -> Result<Message, String> {
    let mut meta = api("GET", &path, None).await?;
    let rev = revision.unwrap_or_else(|| meta.get("revision").and_then(Value::as_i64).unwrap_or(0));
    if let Some(r) = revision {
        let all = api("GET", &format!("{path}/versions"), None).await?;
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
        let b = binary("GET", &format!("{path}/content?revision={rev}&part={part}"), None).await?;
        if b.len() > CHUNK {
            return Err("Invalid download chunk".into());
        }
        bytes.extend(b);
    }
    if bytes.len() != size || hex::encode(Sha256::digest(&bytes)) != meta.pointer("/content/sha256").and_then(Value::as_str).unwrap_or("") {
        return Err("Download checksum failed. No document was opened.".into());
    }
    Ok(Message::Opened(meta, bytes))
}
impl Cloud {
    pub fn new(ctx: &egui::Context) -> Self {
        let s = Self {
            queue: Rc::default(),
            epoch: 0,
            home: true,
            configured: false,
            booted: false,
            sign_in: false,
            user: None,
            projects: vec![],
            bindings: HashMap::new(),
            textures: HashMap::new(),
            search: String::new(),
            filter: "Home".into(),
            template_category: "For you".into(),
            status: "Connecting to your workspace…".into(),
            error: false,
            busy: false,
            last_change: now(),
            observed: None,
            last_poll: 0.,
            last_draft: None,
            drafts: vec![],
            show_share: false,
            show_history: false,
            show_comments: false,
            member_email: String::new(),
            member_role: "edit".into(),
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
            draft_allowed: Rc::new(Cell::new(true)),
            compact_panels: false,
        };
        task(&s.queue, 0, ctx, async {
            let c = api("GET", "/api/config", None).await?;
            let u = api("GET", "/api/me", None).await.ok();
            Ok(Message::Boot(c, u))
        });
        for starter in &home::STARTERS {
            let slug = starter.slug;
            task(
                &s.queue,
                0,
                ctx,
                async move { Ok(Message::Preview(format!("starter/{slug}"), binary("GET", &format!("/templates/{slug}.png"), None).await?)) },
            );
        }
        s
    }
    fn list(&self, ctx: &egui::Context) {
        task(&self.queue, self.epoch, ctx, async { Ok(Message::List(api("GET", "/api/projects", None).await?)) });
    }
    fn data(&self, ctx: &egui::Context, key: &'static str, path: String) {
        task(&self.queue, self.epoch, ctx, async move { Ok(Message::Data(key, api("GET", &path, None).await?)) });
    }
    fn mutate(&mut self, ctx: &egui::Context, method: &str, path: String, v: Value) {
        let method = method.to_string();
        task(&self.queue, self.epoch, ctx, async move {
            api(&method, &path, Some(v)).await?;
            Ok(Message::List(api("GET", "/api/projects", None).await?))
        });
    }
    fn open(&mut self, ctx: &egui::Context, id: String, revision: Option<i64>) {
        self.busy = true;
        self.status = "Opening your document…".into();
        task(&self.queue, self.epoch, ctx, download_project(format!("/api/projects/{id}"), revision));
    }
    fn binding(&self, app: &PhotocraftApp) -> Option<Binding> {
        app.session.active().and_then(|d| self.bindings.get(&d.doc.id)).cloned()
    }
    pub fn update(&mut self, app: &mut PhotocraftApp, ctx: &egui::Context) {
        let messages = std::mem::take(&mut *self.queue.borrow_mut());
        for (epoch, m) in messages {
            if epoch != self.epoch {
                continue;
            }
            match m {
                Message::Boot(c, u) => {
                    self.booted = true;
                    self.configured = c.get("cloud").and_then(Value::as_bool) == Some(true);
                    self.sign_in = c.get("signIn").and_then(Value::as_bool) == Some(true);
                    self.user = u;
                    self.status = if self.user.is_some() {
                        "Your workspace is ready"
                    } else if self.configured {
                        "Sign in to save and share. Local editing is always available."
                    } else {
                        "Local editing is ready. Cloud storage is awaiting setup."
                    }
                    .into();
                    if self.user.is_some() {
                        self.list(ctx);
                        if let Some(id) = url_param("project").filter(|v| v.len() == 36) {
                            self.open(ctx, id, None);
                        }
                    }
                    let scope = self.scope();
                    task(&self.queue, self.epoch, ctx, async move { Ok(Message::Drafts(draft_list(&scope).await?)) });
                    if let Some(key) = url_param("share").filter(|s| s.len() == 64) {
                        self.busy = true;
                        task(&self.queue, self.epoch, ctx, download_project(format!("/api/share/{key}"), None));
                    }
                }
                Message::List(v) => {
                    self.projects = arr(v);
                    for p in &self.projects {
                        let id = field(p, "id").to_string();
                        if self.textures.contains_key(&id) || p.get("revision").and_then(Value::as_i64).unwrap_or(0) == 0 {
                            continue;
                        }
                        let key = id.clone();
                        task(&self.queue, self.epoch, ctx, async move {
                            let b = binary("GET", &format!("/api/projects/{id}/thumbnail"), None).await.unwrap_or_default();
                            Ok(Message::Preview(key, b))
                        });
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
                Message::Opened(meta, bytes) => {
                    self.busy = false;
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
                Message::Created(id, pid) => {
                    self.bindings.insert(id, Binding { id: pid, revision: 0, saved_local: 0, role: "owner".into() });
                }
                Message::SignedOut => {
                    self.epoch += 1;
                    self.bindings.clear();
                    self.projects.clear();
                    self.textures.retain(|key, _| key.starts_with("starter/"));
                    self.drafts.clear();
                    self.user = None;
                    self.home = true;
                    self.filter = "Home".into();
                    self.show_logout = false;
                    let _ = app.session.execute("file.closeAll", json!({}));
                    self.status = "Signed out. Private browser recovery data cleared.".into();
                    self.busy = false;
                }
                Message::SignInReady => {
                    super::web::set_unsaved(false);
                    if let Some(w) = web_sys::window() {
                        let _ = w.location().set_href("/auth/login");
                    }
                    return;
                }
                Message::Synced(id, expected, meta, bytes) => {
                    self.busy = false;
                    // Never replace an edited tab, a switched document, or an active gesture.
                    if app.session.active().is_some_and(|d| d.doc.id == id && d.revision == expected)
                        && !ctx.input(|i| i.pointer.any_down())
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
                            && now() - self.last_change > 800.
                            && let Some(d) = app.session.active().filter(|d| d.revision == b.saved_local)
                        {
                            self.sync(ctx, d.doc.id, d.revision, b.id);
                        }
                    }
                }
                Message::Saved(id, local, pid, revision, merged) => {
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
                Message::Data(key, v) => match key {
                    "config" => {
                        let was_configured = self.configured;
                        self.booted = true;
                        self.configured = v.get("cloud").and_then(Value::as_bool) == Some(true);
                        self.sign_in = v.get("signIn").and_then(Value::as_bool) == Some(true);
                        self.status = if self.sign_in {
                            "Sign in to save your designs and collaborate."
                        } else {
                            "Cloud connection is unavailable. You can still edit and download your designs."
                        }
                        .into();
                        if !was_configured && self.configured {
                            task(&self.queue, self.epoch, ctx, async move {
                                let user = api("GET", "/api/me", None).await.ok();
                                Ok(Message::Boot(v, user))
                            });
                        }
                    }
                    "members" => self.members = arr(v),
                    "invited" => {
                        self.members = arr(v);
                        self.member_email.clear();
                        self.status = "Invitation sent. Their sign-in email gives them access to this project.".into();
                        self.error = false;
                        self.busy = false;
                    }
                    "history" => self.history = arr(v),
                    "comments" => self.comments = arr(v),
                    "comment_posted" => {
                        self.comments = arr(v);
                        self.comment.clear();
                    }
                    "share" => self.share_url = field(&v, "url").into(),
                    _ => {}
                },
                Message::Notice(v) => {
                    self.status = v;
                    self.error = false;
                }
                Message::Error(e) => {
                    self.busy = false;
                    self.status = e;
                    self.error = true;
                    self.draft_allowed.set(true);
                }
                Message::Drafts(v) => self.drafts = v,
                Message::Template(name, bytes) => {
                    self.busy = false;
                    match app.open_bytes(&name, &bytes) {
                        Ok(_) => {
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
        super::web::set_unsaved(
            app.session.documents().iter().any(|d| self.bindings.get(&d.doc.id).map(|b| b.saved_local != d.revision).unwrap_or(d.is_dirty())),
        );
        let current = app.session.active().map(|d| (d.doc.id, d.revision));
        if current != self.observed {
            self.observed = current;
            self.last_change = now();
            if current.is_some() {
                self.home = false;
            }
        }
        if let Some(d) = app.session.active() {
            let changed = self.binding(app).map(|b| b.saved_local != d.revision).unwrap_or(d.is_dirty());
            if changed && now() - self.last_change > 1800. && self.last_draft != current {
                self.last_draft = current;
                let name = d.doc.name.clone();
                let scope = self.scope();
                let key = format!("{scope}:{}", d.doc.id.0);
                let permit = self.draft_allowed.clone();
                match photocraft_format::save_to_bytes(&d.doc, &Default::default()) {
                    Ok(bytes) => {
                        task(&self.queue, self.epoch, ctx, async move {
                            draft_put(&key, &name, &bytes, permit).await?;
                            Ok(Message::Notice("Recovery copy saved in this browser".into()))
                        });
                    }
                    Err(e) => {
                        self.error = true;
                        self.status = format!("Could not create recovery copy: {e}");
                    }
                }
            }
            if changed
                && self.binding(app).is_some_and(|b| b.role != "view")
                && self.user.is_some()
                && !self.busy
                && !self.error
                && now() - self.last_change > 3500.
            {
                self.save(app, ctx, false);
            }
        }
        if now() - self.last_poll > 1500. {
            self.last_poll = now();
            if let Some(b) = self.binding(app).filter(|_| self.user.is_some()) {
                let q = self.queue.clone();
                let epoch = self.epoch;
                if self.show_comments {
                    self.data(ctx, "comments", format!("/api/projects/{}/comments", b.id));
                }
                let ctx = ctx.clone();
                wasm_bindgen_futures::spawn_local(async move {
                    if let Ok(v) = api("POST", &format!("/api/projects/{}/presence", b.id), Some(json!({}))).await {
                        q.borrow_mut().push((epoch, Message::Presence(b.id.clone(), v)));
                        ctx.request_repaint();
                    }
                });
            }
            if !self.sign_in && self.user.is_none() {
                self.data(ctx, "config", "/api/config".into());
            }
        }
        ctx.request_repaint_after(std::time::Duration::from_millis(500));
    }
    fn sync(&mut self, ctx: &egui::Context, id: DocId, revision: u64, pid: String) {
        self.busy = true;
        self.status = "Bringing in your collaborators’ changes…".into();
        task(&self.queue, self.epoch, ctx, async move {
            match download_project(format!("/api/projects/{pid}"), None).await? {
                Message::Opened(meta, bytes) => Ok(Message::Synced(id, revision, meta, bytes)),
                _ => Err("Could not read the shared update".into()),
            }
        });
    }
    fn scope(&self) -> String {
        self.user.as_ref().map(|u| field(u, "id").into()).unwrap_or_else(|| "guest".into())
    }
    fn begin_sign_in(&mut self, app: &PhotocraftApp, ctx: &egui::Context) {
        let mut drafts = Vec::new();
        for d in app.session.documents() {
            match photocraft_format::save_to_bytes(&d.doc, &Default::default()) {
                Ok(bytes) => drafts.push((format!("guest:{}", d.doc.id.0), d.doc.name.clone(), bytes)),
                Err(e) => {
                    self.error = true;
                    self.status = format!("Could not preserve your work before sign-in: {e}. Download your document first.");
                    return;
                }
            }
        }
        self.busy = true;
        let permit = self.draft_allowed.clone();
        task(&self.queue, self.epoch, ctx, async move {
            for (key, name, bytes) in drafts {
                draft_put(&key, &name, &bytes, permit.clone()).await?;
            }
            Ok(Message::SignInReady)
        });
    }
    fn save(&mut self, app: &PhotocraftApp, ctx: &egui::Context, copy: bool) {
        if self.busy {
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
        if binding.as_ref().is_some_and(|b| b.role == "view") {
            self.status = "View access. Use Save a copy to create your own project.".into();
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
        task(&self.queue, self.epoch, ctx, async move {
            let (pid, base) = if let Some(b) = binding {
                (b.id, b.revision)
            } else {
                let p = api("POST", "/api/projects", Some(json!({"title":name}))).await?;
                let pid = field(&p, "id").to_string();
                queue.borrow_mut().push((epoch, Message::Created(docid, pid.clone())));
                (pid, 0)
            };
            let init=api("POST",&format!("/api/projects/{pid}/uploads"),Some(json!({"base_revision":base,"bytes":bytes.len(),"parts":bytes.len().div_ceil(CHUNK),"sha256":hex::encode(Sha256::digest(&bytes)),"title":"Saved from editor","width":width,"height":height}))).await?;
            let uid = field(&init, "id");
            let uploaded = async {
                for (i, b) in bytes.chunks(CHUNK).enumerate() {
                    binary("PUT", &format!("/api/uploads/{uid}/{i}"), Some(b)).await?;
                }
                api("POST", &format!("/api/uploads/{uid}/commit"), Some(json!({}))).await
            }
            .await;
            let done = match uploaded {
                Ok(v) => v,
                Err(e) => {
                    let _ = api("DELETE", &format!("/api/uploads/{uid}"), None).await;
                    return Err(e);
                }
            };
            if let Some(png) = preview.filter(|_| done.get("merged").and_then(Value::as_bool) != Some(true)) {
                let _ = binary("PUT", &format!("/api/projects/{pid}/thumbnail"), Some(&png)).await;
            }
            Ok(Message::Saved(
                docid,
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
                                        self.data(&ctx, "config", "/api/config".into());
                                    }
                                }
                                ui.label(RichText::new("You can keep editing without an account.").small().color(home::MUTED));
                            }
                        });
                    if !self.home {
                        if let Some(b) = binding.as_ref().filter(|b| b.role == "owner")
                            && ui.add_sized([88., 36.], egui::Button::new(RichText::new("Share").strong().color(t.primary_text)).fill(t.primary_bg)).clicked()
                        {
                            self.show_share = true;
                            self.share_url.clear();
                            self.data(&ctx, "members", format!("/api/projects/{}/members", b.id));
                        }
                        let can_save = !self.busy && app.session.active().is_some();
                        let save = ui
                            .add_enabled_ui(can_save, |ui| {
                                if binding.as_ref().is_some_and(|b| b.role != "view") {
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
                            self.save(app, &ctx, binding.as_ref().is_some_and(|b| b.role == "view"));
                        }
                        if let Some(b) = binding.as_ref()
                            && !compact
                            && header_icon(ui, "message-square", "Comments").clicked()
                        {
                            self.show_comments = true;
                            self.data(&ctx, "comments", format!("/api/projects/{}/comments", b.id));
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
                            if ui.button("Save a copy").clicked() {
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
                                if ui.button("Version history").clicked() {
                                    self.show_history = true;
                                    self.data(&ctx, "history", format!("/api/projects/{}/versions", b.id));
                                    ui.close();
                                }
                                if ui.button("Comments").clicked() {
                                    self.show_comments = true;
                                    self.data(&ctx, "comments", format!("/api/projects/{}/comments", b.id));
                                    ui.close();
                                }
                                if b.role == "owner" && ui.button("Share & permissions").clicked() {
                                    self.show_share = true;
                                    self.share_url.clear();
                                    self.data(&ctx, "members", format!("/api/projects/{}/members", b.id));
                                    ui.close();
                                }
                            }
                        });
                    }
                    if !compact && let Some(b) = binding.as_ref() {
                        let saved = app.session.active().is_some_and(|d| d.revision == b.saved_local);
                        ui.add_sized(
                            [100., 36.],
                            egui::Label::new(
                                RichText::new(if saved {
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
        egui::Panel::bottom("cloud_status").exact_size(27.).frame(egui::Frame::NONE.fill(t.card).inner_margin(egui::Margin::symmetric(16, 3))).show(ui, |ui| {
            // The workspace's 40px controls must not force this compact status row past
            // the viewport edge, especially on phones.
            ui.spacing_mut().interact_size.y = 16.;
            ui.spacing_mut().button_padding = Vec2::new(6., 2.);
            ui.horizontal(|ui| {
                ui.set_max_width((ui.available_width() - 110.).max(100.));
                ui.add(egui::Label::new(RichText::new(&self.status).small().color(if self.error { t.warning } else { t.text_dim })).truncate())
                    .on_hover_text(&self.status);
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
        match action {
            home::Action::New(w, h) => {
                new_document(app, w, h, "Untitled canvas");
                self.home = false;
            }
            home::Action::Template(index) => {
                if let Some(starter) = home::STARTERS.get(index) {
                    let slug = starter.slug;
                    self.busy = true;
                    self.status = "Opening your editable template…".into();
                    task(&self.queue, self.epoch, ctx, async move {
                        Ok(Message::Template(format!("{slug}.pcraft"), binary("GET", &format!("/templates/{slug}.pcraft"), None).await?))
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
            egui::Panel::left("workspace_nav").exact_size(214.).frame(egui::Frame::NONE.fill(egui::Color32::WHITE).inner_margin(18)).show(ui, |ui| {
                ui.add_space(20.);
                ui.label(RichText::new("YOUR WORKSPACE").size(10.).strong().color(home::MUTED));
                ui.add_space(18.);
                for (index, (_, name)) in nav.iter().enumerate() {
                    if home::nav_button(ui, name, self.filter == *name, index).clicked() {
                        self.filter = (*name).into();
                        self.search.clear();
                    }
                    ui.add_space(4.);
                }
                ui.add_space(28.);
                ui.separator();
                ui.add_space(22.);
                ui.label(RichText::new("Made for your ideas.").size(14.).strong().color(home::INK));
                ui.add_space(10.);
                ui.label(RichText::new("Layers, brushes, type, and real PSD files. A little room to make something yours.").size(12.).color(home::MUTED));
                ui.add_space(16.);
                ui.hyperlink_to("Meet PhotoCraft ↗", "https://github.com/storytold/photocraft");
                if app.session.active().is_some() {
                    ui.add_space(24.);
                    if ui.add_sized([178., 36.], home::primary("Return to editor →")).clicked() {
                        self.home = false;
                    }
                }
            });
        }
        egui::CentralPanel::default().frame(egui::Frame::NONE.fill(home::PAPER).inner_margin(if narrow { 18 } else { 32 })).show(ui, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                if narrow {
                    ui.horizontal_wrapped(|ui| {
                        for (_, name) in nav {
                            if home::compact_nav(ui, name, self.filter == name).clicked() {
                                self.filter = name.into();
                                self.search.clear();
                            }
                        }
                    });
                    ui.add_space(20.);
                }
                if narrow {
                    ui.label(RichText::new(&self.filter).size(24.).strong().color(home::INK));
                    ui.add_space(8.);
                }
                ui.horizontal(|ui| {
                    if !narrow {
                        ui.label(RichText::new(&self.filter).size(24.).strong().color(home::INK));
                    }
                    ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                        if ui.add_sized([132., 40.], home::primary("Create a design")).clicked() {
                            let _ = photocraft_ui_egui::menus::invoke(app, &ctx, "file.new", json!({}));
                            self.home = false;
                        }
                        if ui.add_sized([88., 40.], egui::Button::new("Open file")).clicked() {
                            app.open_dialog_file();
                        }
                    });
                });
                ui.add_space(16.);
                ui.horizontal(|ui| {
                    let hint = if self.filter == "Templates" { "Search templates" } else { "Search projects or folders" };
                    let reserve = if self.search.is_empty() { 0. } else { 66. };
                    let width = (ui.available_width() - reserve).clamp(100., 420.);
                    ui.add_sized([width, 40.], egui::TextEdit::singleline(&mut self.search).hint_text(hint).margin(egui::Margin::symmetric(12, 10)));
                    if !self.search.is_empty() && ui.button("Clear").clicked() {
                        self.search.clear();
                    }
                });
                ui.add_space(24.);
                if self.filter == "Home" && self.user.is_none() && self.search.is_empty() {
                    home::hero(ui, &self.textures);
                    ui.add_space(20.);
                    if let Some(a) = home::quick_sizes(ui) {
                        self.home_action(app, &ctx, a);
                    }
                    ui.add_space(24.);
                }
                if self.filter == "Templates" || (self.filter == "Home" && self.user.is_none() && self.search.is_empty()) {
                    if let Some(a) = home::gallery(ui, &self.textures, &mut self.template_category, &self.search) {
                        self.home_action(app, &ctx, a);
                    }
                    ui.add_space(12.);
                }
                if self.filter != "Templates" {
                    if self.filter == "Home" {
                        ui.horizontal(|ui| {
                            ui.label(RichText::new("Recent projects").size(20.).strong().color(home::INK));
                            if ui.button("All projects →").clicked() {
                                self.filter = "All projects".into();
                            }
                        });
                        ui.add_space(18.);
                    }
                    let items = self.projects.iter().filter(|p| home::project_matches(p, &self.filter, &self.search)).cloned().collect::<Vec<_>>();
                    if items.is_empty() {
                        egui::Frame::new().fill(t.card).corner_radius(t.radius_lg).inner_margin(24).show(ui, |ui| {
                            ui.set_min_width((ui.available_width() - 48.).max(100.));
                            let (title, help) = home::empty_message(&self.filter, !self.search.trim().is_empty(), self.user.is_some());
                            ui.label(RichText::new(title).size(18.).strong().color(home::INK));
                            ui.add_space(8.);
                            ui.label(RichText::new(help).color(home::MUTED));
                        });
                    }
                    let cols = home::grid_columns(ui.available_width(), 240., 6);
                    for row in items.chunks(cols) {
                        ui.columns(cols, |uis| {
                            for (i, p) in row.iter().enumerate() {
                                if let Some(ui) = uis.get_mut(i) {
                                    self.project_card(ui, p, &ctx);
                                }
                            }
                        });
                        ui.add_space(16.);
                    }
                    if !self.drafts.is_empty() && matches!(self.filter.as_str(), "Home" | "All projects") {
                        ui.add_space(24.);
                        ui.label(RichText::new("Browser recovery").size(18.).strong());
                        ui.label("Local recovery copies stay on this browser. Open one as a separate document.");
                        ui.add_space(8.);
                        for (key, v) in self.drafts.clone() {
                            ui.horizontal(|ui| {
                                ui.label(field(&v, "name"));
                                if ui.button("Recover").clicked() {
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
                ui.add_space(32.);
            });
        });
    }
    fn project_card(&mut self, ui: &mut egui::Ui, p: &Value, ctx: &egui::Context) {
        let t = Tokens::for_kind(ThemeKind::StudioLight);
        let id = field(p, "id").to_string();
        let title = field(p, "title");
        egui::Frame::new().fill(t.card).stroke(egui::Stroke::new(1., t.card_border)).corner_radius(t.radius_lg).inner_margin(12).show(ui, |ui| {
            let width = ui.available_width();
            let (rect, response) = ui.allocate_exact_size(Vec2::new(width, 168.), egui::Sense::click());
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
                    format!("{} × {}", p.get("width").and_then(Value::as_i64).unwrap_or(0), p.get("height").and_then(Value::as_i64).unwrap_or(0)),
                    egui::FontId::proportional(14.),
                    t.text_dim,
                );
            }
            response.widget_info(|| egui::WidgetInfo::labeled(egui::WidgetType::Button, true, format!("Open {title}")));
            if response.hovered() || response.has_focus() {
                ui.painter().rect_stroke(rect, t.radius_sm, egui::Stroke::new(2., t.accent), egui::StrokeKind::Inside);
            }
            if response.on_hover_cursor(egui::CursorIcon::PointingHand).clicked() {
                self.open(ctx, id.clone(), None);
            }
            ui.add_space(8.);
            ui.horizontal(|ui| {
                let title_width = (ui.available_width() - 52.).max(40.);
                if ui
                    .add_sized(
                        [title_width, 40.],
                        egui::Label::new(RichText::new(title).size(14.).strong()).halign(egui::Align::Min).truncate().sense(egui::Sense::click()),
                    )
                    .on_hover_text(title)
                    .clicked()
                {
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
                        if ui.button("Open").clicked() {
                            self.open(ctx, id.clone(), None);
                            ui.close();
                        }
                        if ui.button("Duplicate").clicked() {
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
            ui.label(
                RichText::new(format!(
                    "{}  ·  Version {}",
                    if field(p, "role") == "owner" { "Your project" } else { "Shared project" },
                    p.get("revision").and_then(Value::as_i64).unwrap_or(0)
                ))
                .small()
                .color(t.text_dim),
            );
            if !field(p, "folder").is_empty() {
                ui.add(egui::Label::new(RichText::new(field(p, "folder")).size(12.).color(t.accent)).truncate()).on_hover_text(field(p, "folder"));
            }
        });
    }
    fn dialogs(&mut self, app: &mut PhotocraftApp, ctx: &egui::Context) {
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
                if ui.add_enabled(!self.busy, egui::Button::new("Sign out and clear this browser")).clicked() {
                    self.busy = true;
                    self.draft_allowed.set(false);
                    task(&self.queue, self.epoch, ctx, async {
                        api("POST", "/api/logout", Some(json!({}))).await?;
                        clear_drafts().await?;
                        Ok(Message::SignedOut)
                    });
                }
            });
            self.show_logout &= open;
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
        let pid = binding.id;
        if self.show_share {
            let mut open = true;
            workspace_window("Share & permissions", ctx, 440.)
                .min_height(
                    (480.
                        + if ctx.content_rect().width() < 520. { 48. } else { 0. }
                        + 64. * self.members.len().min(3) as f32
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
                    if ui
                        .add_enabled(
                            !self.busy && !self.member_email.trim().is_empty(),
                            egui::Button::new(RichText::new("Send invitation").color(Tokens::get(ctx).primary_text)).fill(Tokens::get(ctx).primary_bg),
                        )
                        .clicked()
                    {
                        self.busy = true;
                        let path = format!("/api/projects/{pid}/members");
                        let v = json!({"email":self.member_email,"role":self.member_role});
                        task(&self.queue, self.epoch, ctx, async move {
                            api("POST", &path.replace("/members", "/invite"), Some(v)).await?;
                            Ok(Message::Data("invited", api("GET", &path, None).await?))
                        });
                    }
                    for m in self.members.clone() {
                        let email = field(&m, "email");
                        let mut role = field(&m, "role").to_string();
                        ui.horizontal(|ui| {
                            ui.add_sized([(ui.available_width() - 130.).max(80.), 36.], egui::Label::new(email).halign(egui::Align::Min).truncate())
                                .on_hover_text(email);
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
                        ui.add(
                            egui::Label::new(
                                RichText::new(match field(&m, "delivery") {
                                    "failed" => "Email not sent · access granted",
                                    "sent" => "Invitation sent",
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
                            task(&self.queue, self.epoch, ctx, async move {
                                api("PUT", &path, Some(json!({"email":email,"role":role}))).await?;
                                Ok(Message::Data("members", api("GET", &path, None).await?))
                            });
                        }
                    }
                    ui.add_space(14.);
                    ui.separator();
                    ui.add_space(10.);
                    ui.label("View link");
                    ui.label("Anyone with the link can view and download the latest saved version.");
                    if ui.button("Create a new view link").clicked() {
                        let path = format!("/api/projects/{pid}/share");
                        task(&self.queue, self.epoch, ctx, async move { Ok(Message::Data("share", api("POST", &path, Some(json!({}))).await?)) });
                    }
                    if !self.share_url.is_empty() {
                        ui.add(egui::TextEdit::singleline(&mut self.share_url).desired_width(ui.available_width()));
                        if ui.button("Copy link").clicked() {
                            ctx.copy_text(self.share_url.clone());
                            self.status = "View link copied".into();
                        }
                    }
                    if ui.button("Revoke all view links").clicked() {
                        let path = format!("/api/projects/{pid}/share");
                        task(&self.queue, self.epoch, ctx, async move {
                            api("DELETE", &path, None).await?;
                            Ok(Message::Notice("View links revoked".into()))
                        });
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
                    task(&self.queue, self.epoch, ctx, async move {
                        api("POST", &path, Some(json!({"body":body}))).await?;
                        Ok(Message::Data("comment_posted", api("GET", &path, None).await?))
                    });
                }
                if ui.button("Refresh comments").clicked() {
                    self.data(ctx, "comments", format!("/api/projects/{pid}/comments"));
                }
                egui::ScrollArea::vertical().max_height(400.).show(ui, |ui| {
                    for c in self.comments.clone() {
                        ui.group(|ui| {
                            ui.label(RichText::new(field(&c, "author")).strong());
                            ui.label(field(&c, "body"));
                            let resolved = c.get("resolved").and_then(Value::as_bool) == Some(true);
                            if binding.role != "view" && ui.small_button(if resolved { "Reopen" } else { "Resolve" }).clicked() {
                                let path = format!("/api/projects/{pid}/comments");
                                let cid = field(&c, "id").to_string();
                                task(&self.queue, self.epoch, ctx, async move {
                                    api("PUT", &format!("{path}/{cid}"), Some(json!({"resolved":!resolved}))).await?;
                                    Ok(Message::Data("comments", api("GET", &path, None).await?))
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
async fn draft_put(key: &str, name: &str, bytes: &[u8], permit: Rc<Cell<bool>>) -> Result<(), String> {
    let db = recovery_db().await?;
    if !permit.get() {
        return Ok(());
    }
    let tx = db.transaction(&["drafts"], rexie::TransactionMode::ReadWrite).map_err(js_error)?;
    let store = tx.store("drafts").map_err(js_error)?;
    let obj = js_sys::Object::new();
    for (k, v) in [("name", JsValue::from_str(name)), ("data", js_sys::Uint8Array::from(bytes).into()), ("savedAt", JsValue::from_f64(now()))] {
        js_sys::Reflect::set(&obj, &JsValue::from_str(k), &v).map_err(|_| "Recovery data could not be encoded")?;
    }
    store.put(&obj, Some(&JsValue::from_str(key))).await.map_err(js_error)?;
    tx.done().await.map_err(js_error)?;
    Ok(())
}
async fn draft_list(scope: &str) -> Result<Vec<(String, Value)>, String> {
    let db = recovery_db().await?;
    let tx = db.transaction(&["drafts"], rexie::TransactionMode::ReadOnly).map_err(js_error)?;
    let store = tx.store("drafts").map_err(js_error)?;
    let keys = store.get_all_keys(None, None).await.map_err(js_error)?;
    let mut out = vec![];
    for key in keys {
        if let Some(k) = key.as_string().filter(|k| k.starts_with(&format!("{scope}:")) || k.starts_with("guest:"))
            && let Some(v) = store.get(key).await.map_err(js_error)?
        {
            let name = js_sys::Reflect::get(&v, &JsValue::from_str("name")).ok().and_then(|v| v.as_string()).unwrap_or_else(|| "Recovered document".into());
            out.push((k, json!({"name":name})));
        }
    }
    Ok(out)
}
async fn draft_get(key: &str) -> Result<(String, Vec<u8>), String> {
    let db = recovery_db().await?;
    let tx = db.transaction(&["drafts"], rexie::TransactionMode::ReadOnly).map_err(js_error)?;
    let v = tx.store("drafts").map_err(js_error)?.get(JsValue::from_str(key)).await.map_err(js_error)?.ok_or("Recovery copy is unavailable")?;
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
