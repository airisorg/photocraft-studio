//! Bounded, authenticated transient views. Saved documents still use the native cloud path.
use super::{Binding, CloudHttp, HttpFailure, account_request, field, now};
use photocraft_doc::{DocId, Document};
use photocraft_ui_egui::{
    PhotocraftApp,
    collaboration::{self, PreviewEvent, PreviewKind},
};
use serde_json::{Value, json};
use std::{
    cell::{Cell, RefCell},
    collections::HashMap,
    rc::Rc,
    sync::Arc,
};

const EXCHANGE_MS: f64 = 80.;
const HEARTBEAT_MS: f64 = 500.;
const EXPIRE_MS: f64 = 2_000.;
const TAB_KEY: &str = "photocraft.live.tab.v1";

struct Failure {
    status: u16,
}
enum Reply {
    Read(u64, f64, Result<Value, Failure>),
    Write(u64, f64, Option<u64>, i64, Result<Value, Failure>),
}
pub(super) struct Update {
    pub revision: i64,
    pub role: String,
}

#[derive(Default)]
pub(super) struct Live {
    tab: Option<String>,
    sequence: u64,
    generation: u64,
    key: Option<(DocId, String, u64)>,
    base: Option<(i64, Arc<Document>)>,
    replies: Rc<RefCell<Vec<Reply>>>,
    read_pending: bool,
    write_pending: bool,
    read_required: bool,
    read_at: f64,
    write_at: f64,
    cursor: Option<[f64; 2]>,
    events: Vec<PreviewEvent>,
    gesture: Option<(u64, u64)>,
    gesture_base: i64,
    changed: bool,
    editing_paused: bool,
    blocked_base: Option<i64>,
    peers: HashMap<String, f64>,
    pub notice: Option<String>,
}

fn tab_id() -> Option<(String, u64)> {
    let window = web_sys::window()?;
    // Reloads retain their watermark. New tabs get a fresh identity even when their
    // opener copied sessionStorage, so two tabs cannot overwrite each other's cursor.
    let reload = window
        .performance()
        .and_then(|p| js_sys::Reflect::get(&p.get_entries_by_type("navigation").get(0), &"type".into()).ok())
        .and_then(|v| v.as_string())
        .as_deref()
        == Some("reload");
    if reload
        && let Some(value) =
            window.session_storage().ok().flatten().and_then(|s| s.get_item(TAB_KEY).ok().flatten()).and_then(|s| serde_json::from_str::<Value>(&s).ok())
    {
        let tab = field(&value, "tab");
        if tab.len() == 36
            && tab.bytes().enumerate().all(|(i, b)| if [8, 13, 18, 23].contains(&i) { b == b'-' } else { b.is_ascii_hexdigit() })
            && let Some(sequence) = value.get("seq").and_then(Value::as_u64).filter(|v| *v < i64::MAX as u64)
        {
            return Some((tab.into(), sequence));
        }
    }
    let mut bytes = [0_u8; 16];
    window.crypto().ok()?.get_random_values_with_u8_array(&mut bytes).ok()?;
    bytes[6] = (bytes[6] & 15) | 64;
    bytes[8] = (bytes[8] & 63) | 128;
    let v = hex::encode(bytes);
    Some((format!("{}-{}-{}-{}-{}", v.get(..8)?, v.get(8..12)?, v.get(12..16)?, v.get(16..20)?, v.get(20..)?), 0))
}

fn persist(tab: &str, sequence: u64) {
    if let Some(storage) = web_sys::window().and_then(|w| w.session_storage().ok().flatten()) {
        let _ = storage.set_item(TAB_KEY, &json!({"tab":tab,"seq":sequence}).to_string());
    }
}

// Abort slow requests so an offline fetch cannot block a direction indefinitely. This also
// bounds the serverless work retained by a tab; authority comes from the normal cookie client.
async fn call(http: &CloudHttp, method: &str, path: &str, body: Option<Value>) -> Result<Value, Failure> {
    http.ready().map_err(|_| Failure { status: 0 })?;
    let controller = web_sys::AbortController::new().map_err(|_| Failure { status: 0 })?;
    let complete = Rc::new(Cell::new(false));
    let done = complete.clone();
    let abort = controller.clone();
    wasm_bindgen_futures::spawn_local(async move {
        gloo_timers::future::TimeoutFuture::new(2_000).await;
        if !done.get() {
            abort.abort();
        }
    });
    let request = account_request(method, path, http.account.as_deref()).abort_signal(Some(&controller.signal()));
    let result = async {
        let response =
            if let Some(body) = body { request.json(&body).map_err(|e| HttpFailure::Other(e.to_string()))?.send().await } else { request.send().await }
                .map_err(|_| HttpFailure::Other("Live connection interrupted".into()))?;
        let status = response.status();
        if status == 401 {
            let _ = response.binary().await;
            return Err(HttpFailure::Unauthorized);
        }
        let value = response.json::<Value>().await.map_err(|_| HttpFailure::Other("Invalid live response".into()))?;
        if !(200..300).contains(&status) {
            return Err(HttpFailure::Http(status, field(&value, "error").into()));
        }
        Ok(value)
    }
    .await;
    complete.set(true);
    let status = match &result {
        Err(HttpFailure::Http(status, _)) => *status,
        Err(HttpFailure::Unauthorized) => 401,
        _ => 0,
    };
    http.finish(result).map_err(|_| Failure { status })
}

impl Live {
    fn reset(&mut self, app: &mut PhotocraftApp) {
        self.generation = self.generation.wrapping_add(1);
        self.key = None;
        self.base = None;
        self.read_pending = false;
        self.write_pending = false;
        self.read_required = true;
        self.read_at = 0.;
        self.write_at = 0.;
        self.cursor = None;
        self.events.clear();
        self.gesture = None;
        self.changed = false;
        self.editing_paused = false;
        self.blocked_base = None;
        self.peers.clear();
        self.notice = None;
        collaboration::clear_all(app);
        collaboration::set_enabled(app, false);
    }

    pub fn update(&mut self, app: &mut PhotocraftApp, ctx: &egui::Context, binding: Option<Binding>, http: CloudHttp) -> Option<Update> {
        let visible = web_sys::window().and_then(|w| w.document()).is_some_and(|d| !d.hidden());
        let active = app.session.active().map(|d| d.doc.id);
        let Some((document, binding)) = active.zip(binding).filter(|(_, b)| visible && matches!(b.role.as_str(), "owner" | "edit" | "view")) else {
            if self.key.is_some() {
                self.reset(app);
            }
            return None;
        };
        if self.tab.is_none()
            && let Some((tab, sequence)) = tab_id()
        {
            self.tab = Some(tab);
            self.sequence = sequence;
        }
        let Some(tab) = self.tab.clone() else {
            self.notice = Some("Live updates are unavailable in this browser. Saved changes still sync.".into());
            return None;
        };
        let key = (document, binding.id.clone(), http.generation);
        if self.key.as_ref() != Some(&key) {
            self.reset(app);
            self.key = Some(key);
        }
        collaboration::set_enabled(app, true);
        if self.blocked_base.is_some_and(|base| base != binding.revision) {
            self.blocked_base = None;
        }
        if !binding.can_edit() && !self.events.is_empty() {
            self.events.clear();
            self.gesture = None;
            self.changed = true;
        }
        if self.base.as_ref().is_some_and(|(revision, _)| *revision != binding.revision) {
            collaboration::clear_all(app);
            self.peers.clear();
            self.events.clear();
            self.gesture = None;
            self.changed = true;
            self.base = None;
        }
        if self.base.is_none()
            && let Some(d) = app.session.active().filter(|d| d.revision == binding.saved_local)
        {
            self.base = Some((binding.revision, d.doc.clone()));
        }
        for local in collaboration::drain(app).into_iter().filter(|e| e.document == document) {
            if let PreviewKind::Cursor { position } = local.event.kind {
                self.cursor = position;
                self.changed = true;
                continue;
            }
            if !binding.can_edit()
                || self.editing_paused
                || self.blocked_base == Some(binding.revision)
                || !self.base.as_ref().is_some_and(|(_, base)| Arc::ptr_eq(base, &local.base))
            {
                self.notice = Some("Live gestures resume after your current changes are saved.".into());
                continue;
            }
            if self.gesture.map(|(native, _)| native) != Some(local.event.gesture) {
                self.events.clear();
                // Native counters restart after reload. A wire ID derived from the
                // persistent tab watermark cannot replay an already retired gesture.
                self.sequence = self.sequence.saturating_add(1);
                persist(&tab, self.sequence);
                self.gesture = Some((local.event.gesture, self.sequence));
                self.gesture_base = binding.revision;
                self.notice = None;
            }
            if let PreviewKind::Unavailable { ref reason } = local.event.kind {
                self.notice = Some(reason.clone());
            }
            if self.events.len() < collaboration::MAX_EVENTS {
                let mut event = local.event;
                event.gesture = self.gesture.map_or(0, |(_, wire)| wire);
                self.events.push(event);
                self.changed = true;
            }
        }
        let clock = now();
        let native_clock = ctx.input(|i| i.time * 1000.);
        let replies = std::mem::take(&mut *self.replies.borrow_mut());
        let mut update = None;
        for reply in replies {
            let (generation, read, elapsed, terminal, submitted_base, result) = match reply {
                Reply::Read(g, started, r) => (g, true, (clock - started).max(0.), None, 0, r),
                Reply::Write(g, started, terminal, base, r) => (g, false, (clock - started).max(0.), terminal, base, r),
            };
            if generation != self.generation || http.generation != http.current.get() || !http.allowed.get() {
                continue;
            }
            if read {
                self.read_pending = false;
            } else {
                self.write_pending = false;
            }
            let value = match result {
                Ok(v) => v,
                Err(failure) => {
                    if !read && failure.status == 409 {
                        // A canonical save can beat a previous-base preview request.
                        // Reconcile promptly; never impose the network retry delay on
                        // new gestures whose committed base has already advanced.
                        if submitted_base == binding.revision {
                            self.blocked_base = Some(submitted_base);
                            self.events.clear();
                            self.gesture = None;
                            self.notice = Some("Live gestures resume after the latest saved version arrives.".into());
                        }
                        self.read_at = 0.;
                        self.read_required = true;
                        self.write_at = clock - EXCHANGE_MS;
                        self.changed = true;
                        continue;
                    }
                    if failure.status == 404 || (read && failure.status == 403) {
                        self.notice = Some("Live access was removed. Your local document is preserved.".into());
                        self.events.clear();
                        self.gesture = None;
                        self.cursor = None;
                        self.changed = false;
                        self.peers.clear();
                        collaboration::clear_all(app);
                        collaboration::set_enabled(app, false);
                        return Some(Update { revision: binding.revision, role: "unavailable".into() });
                    }
                    if !read && failure.status == 403 {
                        // A write denial can be an edit-to-view downgrade. Read the
                        // authoritative role before deciding whether view access ended.
                        self.events.clear();
                        self.gesture = None;
                        self.editing_paused = true;
                        self.changed = true;
                        self.read_at = 0.;
                        self.read_required = true;
                    }
                    self.notice = Some("Live updates reconnecting. Saved changes still sync.".into());
                    if read {
                        self.read_at = clock + 250.;
                    } else {
                        self.write_at = clock + 500.;
                        self.read_required = true;
                        self.changed = true;
                    }
                    // Existing peers retain only their admitted remaining lease. A
                    // transient request failure must not retire a valid held gesture.
                    continue;
                }
            };
            if !read {
                self.sequence = self.sequence.max(value.get("seq").and_then(Value::as_u64).unwrap_or(0));
                persist(&tab, self.sequence);
                if value.get("accepted").and_then(Value::as_bool) == Some(true) && terminal.is_some() && self.gesture.map(|(_, wire)| wire) == terminal {
                    self.events.clear();
                    self.gesture = None;
                    self.changed = true;
                }
                // Older servers acknowledge writes without returning a peer
                // snapshot. Reconcile with GET before sending another update.
                if !value.get("peers").is_some_and(Value::is_array) || !matches!(field(&value, "role"), "owner" | "edit" | "view") {
                    self.read_required = true;
                    self.read_at = 0.;
                    continue;
                }
            }
            self.read_required = false;
            if self.notice.as_deref().is_some_and(|n| n.starts_with("Live updates reconnecting")) {
                self.notice = None;
            }
            let revision = value.get("revision").and_then(Value::as_i64).unwrap_or(binding.revision);
            let role = field(&value, "role").to_string();
            self.editing_paused = !matches!(role.as_str(), "owner" | "edit");
            update = Some(Update { revision, role: role.clone() });
            if !matches!(role.as_str(), "owner" | "edit" | "view") {
                collaboration::clear_all(app);
                self.peers.clear();
                continue;
            }
            let mut present = HashMap::new();
            if let Some(peers) = value.get("peers").and_then(Value::as_array) {
                for peer in peers.iter().take(32) {
                    // Account + tab establish a stable identity independent of rename/email.
                    let label = format!("{}:{}", field(peer, "actor"), field(peer, "tab"));
                    let lease = peer.get("ttlMs").and_then(Value::as_f64).filter(|n| n.is_finite()).unwrap_or(0.).clamp(0., EXPIRE_MS) - elapsed;
                    if lease <= 0. {
                        continue;
                    }
                    present.insert(label.clone(), clock + lease);
                    if peer.get("gesture").is_none_or(Value::is_null) {
                        collaboration::clear_peer(app, &label);
                    }
                    collaboration::set_peer_label(app, &label, field(peer, "name"));
                    let position = peer.get("cursor").filter(|c| !c.is_null()).and_then(|c| Some([c.get("x")?.as_f64()?, c.get("y")?.as_f64()?]));
                    let _ = collaboration::receive(
                        app,
                        document,
                        0,
                        &label,
                        PreviewEvent { gesture: 0, sequence: 0, kind: PreviewKind::Cursor { position } },
                        native_clock,
                    );
                    let clean_base = app.session.active().is_some_and(|d| self.base.as_ref().is_some_and(|(_, base)| Arc::ptr_eq(base, &d.doc)));
                    if peer.get("baseRevision").and_then(Value::as_i64) != Some(binding.revision) || !clean_base {
                        continue;
                    }
                    let expected = app.session.active().map_or(0, |d| d.revision);
                    if let Some(events) = peer.pointer("/gesture/events").and_then(Value::as_array) {
                        for raw in events.iter().take(collaboration::MAX_EVENTS) {
                            let result = serde_json::from_value::<PreviewEvent>(raw.clone())
                                .map_err(|e| e.to_string())
                                .and_then(|event| collaboration::receive(app, document, expected, &label, event, native_clock));
                            match result {
                                Ok(()) => {}
                                Err(_) => {
                                    self.notice = Some("Some live gestures are waiting for the saved version.".into());
                                    break;
                                }
                            }
                        }
                    }
                }
            }
            for absent in self.peers.keys().filter(|peer| !present.contains_key(*peer)) {
                collaboration::clear_peer(app, absent);
            }
            self.peers = present;
            ctx.request_repaint();
        }
        self.peers.retain(|peer, expires| {
            if *expires <= clock {
                collaboration::clear_peer(app, peer);
                false
            } else {
                true
            }
        });
        let heartbeat = (self.cursor.is_some() || !self.events.is_empty()) && clock - self.write_at >= HEARTBEAT_MS;
        // One request at a time exchanges the whole latest transient state.
        // A publish also returns authorized peers, so an active tab does not
        // need a second independent read loop.
        if !self.read_pending
            && !self.write_pending
            && clock - self.read_at >= EXCHANGE_MS
            && (self.read_required || clock - self.write_at < EXCHANGE_MS || !(self.changed || heartbeat))
        {
            self.read_pending = true;
            self.read_at = clock;
            let queue = self.replies.clone();
            let generation = self.generation;
            let http = http.clone();
            let context = ctx.clone();
            let path = format!("/api/projects/{}/live?tab={tab}", binding.id);
            wasm_bindgen_futures::spawn_local(async move {
                let result = call(&http, "GET", &path, None).await;
                queue.borrow_mut().push(Reply::Read(generation, clock, result));
                context.request_repaint();
            });
        }
        if !self.read_pending
            && !self.write_pending
            && !self.read_required
            && clock - self.read_at >= EXCHANGE_MS
            && clock - self.write_at >= EXCHANGE_MS
            && (self.changed || heartbeat)
        {
            let cursor = self.cursor.map(|p| json!({"x":p[0],"y":p[1]}));
            let gesture = if self.events.is_empty() { Value::Null } else { json!({"events":self.events}) };
            self.sequence = self.sequence.saturating_add(1);
            persist(&tab, self.sequence);
            let body = json!({"tab":tab,"seq":self.sequence,"baseRevision":if self.gesture.is_some() { self.gesture_base } else { binding.revision },"cursor":cursor,"gesture":gesture});
            if serde_json::to_vec(&body).map_or(true, |bytes| bytes.len() > collaboration::MAX_BYTES) {
                self.events.clear();
                self.gesture = None;
                self.changed = true;
                self.notice = Some("This gesture exceeds the live preview limit. The saved version will follow.".into());
            } else {
                self.write_pending = true;
                self.changed = false;
                self.read_at = clock;
                self.write_at = clock;
                let queue = self.replies.clone();
                let generation = self.generation;
                let http = http.clone();
                let context = ctx.clone();
                let path = format!("/api/projects/{}/live", binding.id);
                let terminal = self.events.last().filter(|e| matches!(e.kind, PreviewKind::Cancel | PreviewKind::Unavailable { .. })).map(|e| e.gesture);
                let submitted_base = if self.gesture.is_some() { self.gesture_base } else { binding.revision };
                wasm_bindgen_futures::spawn_local(async move {
                    let result = call(&http, "PUT", &path, Some(body)).await;
                    queue.borrow_mut().push(Reply::Write(generation, clock, terminal, submitted_base, result));
                    context.request_repaint();
                });
            }
        }
        ctx.request_repaint_after(std::time::Duration::from_millis(16));
        update
    }
}
