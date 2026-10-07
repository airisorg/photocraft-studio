//! Cloud workspace around the unmodified editor. All document operations use the Rust engine.
use eframe::App as _;
use egui::{ColorImage, RichText, TextureHandle, Vec2};
use gloo_net::http::{Request, RequestBuilder};
use photocraft_doc::DocId;
use photocraft_ui_egui::{PhotocraftApp, theme::Tokens};
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
    Saved(DocId, u64, String, i64),
    Created(DocId, String),
    SignedOut,
    Data(&'static str, Value),
    Preview(String, Vec<u8>),
    Error(String),
    Notice(String),
    Drafts(Vec<(String, Value)>),
    Recovered(String, Vec<u8>),
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
    sign_in: bool,
    user: Option<Value>,
    projects: Vec<Value>,
    bindings: HashMap<DocId, Binding>,
    textures: HashMap<String, TextureHandle>,
    search: String,
    filter: String,
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
            sign_in: false,
            user: None,
            projects: vec![],
            bindings: HashMap::new(),
            textures: HashMap::new(),
            search: String::new(),
            filter: "All projects".into(),
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
                    if let Ok(img) = photocraft_codecs::decode(&b) {
                        if img.width() <= 512 && img.height() <= 512 {
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
                    self.textures.clear();
                    self.drafts.clear();
                    self.user = None;
                    self.home = true;
                    self.show_logout = false;
                    let _ = app.session.execute("file.closeAll", json!({}));
                    self.status = "Signed out. Private browser recovery data cleared.".into();
                    self.busy = false;
                }
                Message::Saved(id, local, pid, revision) => {
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
                    "members" => self.members = arr(v),
                    "history" => self.history = arr(v),
                    "comments" => self.comments = arr(v),
                    "comment_posted" => {
                        self.comments = arr(v);
                        self.comment.clear();
                    }
                    "share" => self.share_url = field(&v, "url").into(),
                    "presence" => {
                        self.people = v
                            .get("people")
                            .and_then(Value::as_array)
                            .map(|v| v.iter().filter_map(Value::as_str).map(String::from).collect())
                            .unwrap_or_default();
                        if let Some(b) = self.binding(app) {
                            self.newer = v.get("revision").and_then(Value::as_i64).is_some_and(|r| r > b.revision);
                            if self.newer {
                                self.status = "A newer version is available. Your local edits have been kept.".into();
                            }
                        }
                    }
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
        if now() - self.last_poll > 10000. {
            self.last_poll = now();
            if let Some(b) = self.binding(app).filter(|_| self.user.is_some()) {
                let q = self.queue.clone();
                let epoch = self.epoch;
                let ctx = ctx.clone();
                wasm_bindgen_futures::spawn_local(async move {
                    if let Ok(v) = api("POST", &format!("/api/projects/{}/presence", b.id), Some(json!({}))).await {
                        q.borrow_mut().push((epoch, Message::Data("presence", v)));
                        ctx.request_repaint();
                    }
                });
            }
        }
        ctx.request_repaint_after(std::time::Duration::from_millis(500));
    }
    fn scope(&self) -> String {
        self.user.as_ref().map(|u| field(u, "id").into()).unwrap_or_else(|| "guest".into())
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
            if let Some(png) = preview {
                let _ = binary("PUT", &format!("/api/projects/{pid}/thumbnail"), Some(&png)).await;
            }
            Ok(Message::Saved(docid, local, pid, done.get("revision").and_then(Value::as_i64).unwrap_or(base + 1)))
        });
    }
    pub fn ui(&mut self, app: &mut PhotocraftApp, ui: &mut egui::Ui, frame: &mut eframe::Frame) {
        let ctx = ui.ctx().clone();
        let t = Tokens::get(&ctx);
        let binding = self.binding(app);
        let compact = ui.available_width() < 760.;
        egui::Panel::top("cloud_header").exact_size(55.).frame(egui::Frame::NONE.fill(t.card).inner_margin(egui::Margin::symmetric(18, 8))).show(ui, |ui| {
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
                        ui.label(RichText::new(app.session.active().map(|d| d.doc.name.as_str()).unwrap_or("Untitled")).strong());
                    }
                }
                ui.with_layout(egui::Layout::right_to_left(egui::Align::Center), |ui| {
                    if self.user.is_some() {
                        ui.menu_button("Account", |ui| {
                            ui.label(self.user.as_ref().map(|u| field(u, "email")).unwrap_or(""));
                            if ui.button("Sign out").clicked() {
                                self.show_logout = true;
                                ui.close();
                            }
                        });
                    } else if ui
                        .add_enabled(self.sign_in, egui::Button::new(if compact { "Sign in" } else { "Continue with Google" }))
                        .on_hover_text(if self.sign_in { "Sign in to your cloud workspace" } else { "Cloud sign-in is awaiting setup" })
                        .clicked()
                    {
                        if let Some(w) = web_sys::window() {
                            let _ = w.location().set_href("/auth/login");
                        }
                    }
                    if !self.home {
                        if ui
                            .add_enabled(
                                !self.busy && app.session.active().is_some(),
                                egui::Button::new(RichText::new(if compact { "Save" } else { "Save to cloud" }).color(t.primary_text)).fill(t.primary_bg),
                            )
                            .clicked()
                        {
                            self.save(app, &ctx, false);
                        }
                        ui.menu_button("More", |ui| {
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
                    if self.busy {
                        ui.spinner();
                    } else if ui.available_width() > 350. {
                        ui.label(RichText::new(if self.user.is_some() { "Cloud workspace" } else { "Local workspace" }).small().color(t.text_dim));
                    }
                });
            });
        });
        egui::Panel::bottom("cloud_status").exact_size(27.).frame(egui::Frame::NONE.fill(t.card).inner_margin(egui::Margin::symmetric(16, 3))).show(ui, |ui| {
            ui.horizontal(|ui| {
                ui.label(RichText::new(&self.status).small().color(if self.error { t.warning } else { t.text_dim }));
                if self.error && ui.small_button("Dismiss").clicked() {
                    self.error = false;
                }
                if self.newer && ui.small_button("Open latest in a new tab").clicked() {
                    if let Some(b) = &binding {
                        self.open(&ctx, b.id.clone(), None);
                    }
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
    fn home_ui(&mut self, app: &mut PhotocraftApp, ui: &mut egui::Ui) {
        let ctx = ui.ctx().clone();
        let t = Tokens::get(&ctx);
        let narrow = ui.available_width() < 760.;
        if !narrow {
            egui::Panel::left("workspace_nav").exact_size(210.).frame(egui::Frame::NONE.fill(t.card).inner_margin(20)).show(ui, |ui| {
                ui.add_space(24.);
                ui.label(RichText::new("YOUR WORKSPACE").small().color(t.text_dim));
                ui.add_space(18.);
                for name in ["All projects", "Starred", "Shared with me", "Trash"] {
                    if ui.add_sized([170., 38.], egui::Button::new(name).selected(self.filter == name)).clicked() {
                        self.filter = name.into();
                    }
                }
                ui.add_space(28.);
                ui.separator();
                ui.add_space(12.);
                ui.label(RichText::new("Made for your ideas").strong());
                ui.add_space(8.);
                ui.label(RichText::new("Layers, masks, brushes, type, and real PSD files. All in your browser.").color(t.text_dim));
                ui.add_space(16.);
                ui.hyperlink_to("Built on PhotoCraft ↗", "https://github.com/storytold/photocraft");
                ui.add_space(12.);
                ui.label(RichText::new("WebGPU · WebGL2 fallback").small().color(t.text_dim));
                if app.session.active().is_some() {
                    ui.add_space(20.);
                    if ui.button("Return to editor").clicked() {
                        self.home = false;
                    }
                }
            });
        }
        egui::CentralPanel::default().frame(egui::Frame::NONE.fill(t.canvas).inner_margin(if narrow { 18 } else { 36 })).show(ui, |ui| {
            egui::ScrollArea::vertical().show(ui, |ui| {
                ui.add_space(12.);
                ui.label(RichText::new("SPACE TO CREATE").size(11.).strong().color(t.accent));
                ui.add_space(10.);
                ui.label(RichText::new("Your next idea starts here.").size(if narrow { 28. } else { 34. }).strong());
                ui.add_space(10.);
                ui.label(RichText::new("Make a new canvas, open a layered file, or pick up where you left off.").size(15.).color(t.text_dim));
                ui.add_space(22.);
                ui.horizontal_wrapped(|ui| {
                    if ui.add_sized([145., 38.], egui::Button::new(RichText::new("+  New canvas").color(t.primary_text)).fill(t.primary_bg)).clicked() {
                        new_document(app, 1200, 900, "Untitled canvas");
                        self.home = false;
                    }
                    if ui.add_sized([145., 38.], egui::Button::new("Open a file…")).clicked() {
                        app.open_dialog_file();
                    }
                    ui.label(RichText::new("PSD, PNG, JPG, .pcraft and more").small().color(t.text_dim));
                });
                ui.add_space(30.);
                ui.label(RichText::new("Start with a size").size(17.).strong());
                ui.add_space(12.);
                let presets =
                    [("Social post", 1080, 1080, "1:1"), ("Story", 1080, 1920, "9:16"), ("Presentation", 1920, 1080, "16:9"), ("Photo", 2400, 1600, "3:2")];
                let cols = if narrow { 2 } else { 4 };
                for chunk in presets.chunks(cols) {
                    ui.columns(cols, |uis| {
                        for (i, (name, w, h, ratio)) in chunk.iter().enumerate() {
                            let Some(ui) = uis.get_mut(i) else {
                                continue;
                            };
                            egui::Frame::new().fill(t.card).corner_radius(t.radius_lg).inner_margin(14).show(ui, |ui| {
                                let width = ui.available_width();
                                let (r, res) = ui.allocate_exact_size(Vec2::new(width, 86.), egui::Sense::click());
                                ui.painter().rect_filled(r, 6., t.accent_soft);
                                let scale = (r.width() * 0.55 / (*w as f32)).min(58. / (*h as f32));
                                let paper = egui::Rect::from_center_size(r.center(), Vec2::new(*w as f32 * scale, *h as f32 * scale));
                                ui.painter().rect_filled(paper, 3., t.text);
                                ui.painter().circle_filled(paper.center(), paper.height().min(paper.width()) * 0.22, t.accent);
                                ui.painter().line_segment(
                                    [paper.left_bottom() + Vec2::new(4., -5.), paper.right_bottom() + Vec2::new(-4., -5.)],
                                    egui::Stroke::new(3., t.accent_border),
                                );
                                ui.add_space(10.);
                                if ui.add_sized([width, 24.], egui::Button::new(*name).frame(false)).clicked() || res.clicked() {
                                    new_document(app, *w, *h, name);
                                    self.home = false;
                                }
                                ui.label(RichText::new(format!("{w} × {h}  ·  {ratio}")).small().color(t.text_dim));
                            });
                        }
                    });
                    ui.add_space(12.);
                }
                ui.add_space(20.);
                ui.separator();
                ui.add_space(24.);
                ui.horizontal_wrapped(|ui| {
                    ui.label(RichText::new(&self.filter).size(20.).strong());
                    ui.add_space(12.);
                    ui.add(egui::TextEdit::singleline(&mut self.search).hint_text("Search projects or folders").desired_width(250.));
                    if self.user.is_some() && ui.button("Refresh").clicked() {
                        self.list(&ctx);
                    }
                });
                if narrow {
                    ui.horizontal_wrapped(|ui| {
                        for name in ["All projects", "Starred", "Shared with me", "Trash"] {
                            ui.selectable_value(&mut self.filter, name.into(), name);
                        }
                    });
                }
                ui.add_space(18.);
                let search = self.search.to_lowercase();
                let items = self
                    .projects
                    .iter()
                    .filter(|p| {
                        let trashed = p.get("trashed").and_then(Value::as_bool) == Some(true);
                        let matches = match self.filter.as_str() {
                            "Trash" => trashed,
                            "Starred" => !trashed && p.get("starred").and_then(Value::as_bool) == Some(true),
                            "Shared with me" => !trashed && field(p, "role") != "owner",
                            _ => !trashed,
                        };
                        matches && (format!("{} {}", field(p, "title"), field(p, "folder")).to_lowercase().contains(&search))
                    })
                    .cloned()
                    .collect::<Vec<_>>();
                if items.is_empty() {
                    egui::Frame::new().fill(t.card).corner_radius(t.radius_lg).inner_margin(24).show(ui, |ui| {
                        ui.set_min_width((ui.available_width() - 48.).max(100.));
                        ui.label(
                            RichText::new(if self.user.is_none() { "Your ideas deserve a home." } else { "A fresh canvas for your projects." })
                                .size(18.)
                                .strong(),
                        );
                        ui.add_space(8.);
                        ui.label(if self.user.is_none() {
                            "Sign in for cloud saves, version history, sharing, and comments. You can start editing right now."
                        } else {
                            "Create a canvas above, then choose Save to cloud. Your saved projects will appear here."
                        });
                    });
                }
                let cols = if narrow { 1 } else { 3 };
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
                if !self.drafts.is_empty() {
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
                ui.add_space(32.);
            });
        });
    }
    fn project_card(&mut self, ui: &mut egui::Ui, p: &Value, ctx: &egui::Context) {
        let t = Tokens::get(ctx);
        let id = field(p, "id").to_string();
        let title = field(p, "title");
        egui::Frame::new().fill(t.card).corner_radius(t.radius_lg).inner_margin(12).show(ui, |ui| {
            let width = ui.available_width();
            if let Some(texture) = self.textures.get(&id).cloned() {
                let size = texture.size_vec2();
                let scale = (width / size.x).min(130. / size.y);
                ui.vertical_centered(|ui| {
                    if ui.add(egui::Image::new(&texture).fit_to_exact_size(size * scale).sense(egui::Sense::click())).clicked() {
                        self.open(ctx, id.clone(), None);
                    }
                });
            } else {
                let (r, res) = ui.allocate_exact_size(Vec2::new(width, 130.), egui::Sense::click());
                ui.painter().rect_filled(r, 6., t.accent_soft);
                ui.painter().text(
                    r.center(),
                    egui::Align2::CENTER_CENTER,
                    format!("{} × {}", p.get("width").and_then(Value::as_i64).unwrap_or(0), p.get("height").and_then(Value::as_i64).unwrap_or(0)),
                    egui::FontId::proportional(16.),
                    t.text_dim,
                );
                if res.clicked() {
                    self.open(ctx, id.clone(), None);
                }
            }
            ui.add_space(8.);
            ui.horizontal(|ui| {
                if ui.button(RichText::new(title).strong()).clicked() {
                    self.open(ctx, id.clone(), None);
                }
                ui.menu_button("…", |ui| {
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
                ui.label(RichText::new(field(p, "folder")).small().color(t.accent));
            }
        });
    }
    fn dialogs(&mut self, app: &mut PhotocraftApp, ctx: &egui::Context) {
        if self.show_logout {
            let mut open = true;
            egui::Window::new("Sign out of this workspace?").open(&mut open).show(ctx, |ui| {
                ui.label("Unsaved tabs and browser recovery copies will be cleared. Download any work you want to keep first.");
                if ui.button("Download current document").clicked() {
                    if let Some(d) = app.session.active() {
                        if let Ok(bytes) = photocraft_format::save_to_bytes(&d.doc, &Default::default()) {
                            let _ = super::web::download(&format!("{}.pcraft", d.doc.name), &bytes);
                        }
                    }
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
            egui::Window::new("Share & permissions").open(&mut open).default_width(440.).show(ctx, |ui| {
                ui.label("Invite collaborators by email");
                ui.label("They sign in with Google. No invitation email is sent.");
                ui.add_space(10.);
                ui.horizontal(|ui| {
                    ui.add(egui::TextEdit::singleline(&mut self.member_email).hint_text("name@example.com"));
                    egui::ComboBox::from_id_salt("member_role").selected_text(&self.member_role).show_ui(ui, |ui| {
                        ui.selectable_value(&mut self.member_role, "view".into(), "Can view");
                        ui.selectable_value(&mut self.member_role, "edit".into(), "Can edit");
                    });
                });
                if ui.button("Grant access").clicked() {
                    let path = format!("/api/projects/{pid}/members");
                    let v = json!({"email":self.member_email,"role":self.member_role});
                    task(&self.queue, self.epoch, ctx, async move {
                        api("PUT", &path, Some(v)).await?;
                        Ok(Message::Data("members", api("GET", &path, None).await?))
                    });
                    self.member_email.clear();
                }
                for m in self.members.clone() {
                    ui.horizontal(|ui| {
                        ui.label(format!("{} · {}", field(&m, "email"), field(&m, "role")));
                        if ui.small_button("Remove").clicked() {
                            let path = format!("/api/projects/{pid}/members");
                            task(&self.queue, self.epoch, ctx, async move {
                                api("PUT", &path, Some(json!({"email":field(&m,"email"),"role":"remove"}))).await?;
                                Ok(Message::Data("members", api("GET", &path, None).await?))
                            });
                        }
                    });
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
                    ui.add(egui::TextEdit::singleline(&mut self.share_url).desired_width(400.));
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
            egui::Window::new("Version history").open(&mut open).default_width(460.).show(ctx, |ui| {
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
            egui::Window::new("Comments").open(&mut open).default_width(430.).show(ctx, |ui| {
                if !self.people.is_empty() {
                    ui.label(format!("Here now: {}", self.people.join(", ")));
                }
                ui.add(egui::TextEdit::multiline(&mut self.comment).hint_text("Leave feedback for your team…").desired_width(400.).desired_rows(3));
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
        egui::Window::new("Project details").open(&mut open).show(ctx, |ui| {
            ui.label("Name");
            ui.text_edit_singleline(&mut self.rename);
            ui.label("Folder");
            ui.text_edit_singleline(&mut self.folder);
            if ui.button("Save details").clicked() {
                self.mutate(ctx, "PATCH", format!("/api/projects/{}", self.details_id), json!({"title":self.rename,"folder":self.folder}));
                self.show_details = false;
            }
        });
        self.show_details &= open;
    }
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
        if let Some(k) = key.as_string().filter(|k| k.starts_with(&format!("{scope}:"))) {
            if let Some(v) = store.get(key).await.map_err(js_error)? {
                let name = js_sys::Reflect::get(&v, &JsValue::from_str("name")).ok().and_then(|v| v.as_string()).unwrap_or_else(|| "Recovered document".into());
                out.push((k, json!({"name":name})));
            }
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
