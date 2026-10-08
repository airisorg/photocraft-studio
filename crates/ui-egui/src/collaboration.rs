//! Optional collaboration views, rendered by the original native paint and move implementations.
//! The host authenticates peers and maps their committed project base to a local document. This
//! module knows nothing about accounts or transport and never changes the real Session/history.

use std::{collections::HashMap, io::Write, sync::Arc};

use photocraft_doc::{DocId, Document, LayerId};
use photocraft_engine::{Session, brush_cmds::LiveStroke};
use photocraft_geom::Rect;
use serde::{Deserialize, Serialize};
use serde_json::{Value, json};

use crate::{PhotocraftApp, canvas::Drag, state::Tool};

pub const MAX_POINTS: usize = 1024;
pub const MAX_EVENTS: usize = 256;
pub const MAX_BYTES: usize = 64 * 1024;
const MAX_PEERS: usize = 32;
const PREVIEW_TTL_MS: f64 = 15_000.;
const CURSOR_TTL_MS: f64 = 5_000.;
const KEY_BASE: u64 = 1 << 48;
const MAX_DAB_PIXEL_WORK: f64 = 16_000_000.;

/// Cumulative transport snapshots may replay these events. Sequence numbers make that harmless.
#[derive(Clone, Debug, Serialize, Deserialize)]
pub struct PreviewEvent {
    pub gesture: u64,
    pub sequence: u64,
    #[serde(flatten)]
    pub kind: PreviewKind,
}

#[derive(Clone, Debug, Serialize, Deserialize)]
#[serde(tag = "kind", rename_all = "camelCase")]
pub enum PreviewKind {
    Cursor { position: Option<[f64; 2]> },
    StrokeStart { command: String, params: Value, brush: Value, foreground: [f32; 4], background: [f32; 4] },
    Points { points: Vec<Vec<f64>> },
    MoveStart { layers: Vec<u64> },
    Offset { dx: i32, dy: i32 },
    End,
    Cancel,
    Unavailable { reason: String },
}

/// `base` is a cheap COW identity, never a wire payload. It is captured before Auto-Select can
/// advance the view revision. The host must still admit it against its last committed base.
#[derive(Clone)]
pub struct LocalEvent {
    pub document: DocId,
    pub local_revision: u64,
    pub base: Arc<Document>,
    pub event: PreviewEvent,
}

struct Local {
    doc: DocId,
    revision: u64,
    base: Arc<Document>,
    gesture: u64,
    sequence: u64,
    fed: usize,
    bytes: usize,
    offset: Option<(i32, i32)>,
    unavailable: bool,
}

enum NativePreview {
    Stroke(Box<LiveStroke>),
    Move { base: Arc<Document>, layers: Vec<LayerId> },
}

struct Remote {
    doc: DocId,
    revision: u64,
    base: Arc<Document>,
    peer: String,
    gesture: u64,
    sequence: u64,
    points: usize,
    last_point: Option<[f64; 2]>,
    travel: f64,
    work_per_dab: f64,
    work_used: f64,
    bytes: usize,
    ended: bool,
    seen_ms: f64,
    key: u64,
    damage: Vec<Rect>,
    native: NativePreview,
    shown: Arc<Document>,
}

struct Cursor {
    doc: DocId,
    position: [f64; 2],
    seen_ms: f64,
}

#[derive(Default)]
pub struct State {
    enabled: bool,
    outgoing: Vec<LocalEvent>,
    candidate: Option<(DocId, u64, Arc<Document>)>,
    local: Option<Local>,
    next_gesture: u64,
    next_preview: u64,
    local_cursor: Option<(DocId, Option<[f64; 2]>)>,
    remote: Option<Remote>,
    cursors: HashMap<String, Cursor>,
    labels: HashMap<String, String>,
    retired: HashMap<String, u64>,
}

pub fn set_enabled(app: &mut PhotocraftApp, enabled: bool) {
    if app.collaboration.enabled != enabled {
        app.collaboration = State { enabled, next_gesture: app.collaboration.next_gesture, next_preview: app.collaboration.next_preview, ..Default::default() };
    }
}

pub fn drain(app: &mut PhotocraftApp) -> Vec<LocalEvent> {
    std::mem::take(&mut app.collaboration.outgoing)
}

pub fn clear_peer(app: &mut PhotocraftApp, peer: &str) {
    app.collaboration.cursors.remove(peer);
    app.collaboration.labels.remove(peer);
    if app.collaboration.remote.as_ref().is_some_and(|r| r.peer == peer) {
        retire(&mut app.collaboration);
    }
}

pub fn clear_all(app: &mut PhotocraftApp) {
    retire(&mut app.collaboration);
    app.collaboration.cursors.clear();
    app.collaboration.labels.clear();
}

/// Keep the authenticated peer key separate from a possibly non-unique display name.
pub fn set_peer_label(app: &mut PhotocraftApp, peer: &str, label: &str) {
    if peer.is_empty() || peer.len() > 96 {
        return;
    }
    if app.collaboration.labels.len() < MAX_PEERS || app.collaboration.labels.contains_key(peer) {
        app.collaboration.labels.insert(peer.into(), label.chars().filter(|c| !c.is_control()).take(32).collect());
    }
}

fn retire(state: &mut State) {
    if let Some(r) = state.remote.take() {
        // Bound peer identities even when the host receives long-lived membership churn.
        if state.retired.len() >= MAX_PEERS && !state.retired.contains_key(&r.peer) {
            state.retired.clear();
        }
        state.retired.entry(r.peer).and_modify(|n| *n = (*n).max(r.gesture)).or_insert(r.gesture);
    }
}

/// Serializing a bitmap brush must not allocate an unbounded network message first.
fn bounded_bytes(value: &impl Serialize) -> Result<Vec<u8>, String> {
    struct Bounded(Vec<u8>);
    impl Write for Bounded {
        fn write(&mut self, bytes: &[u8]) -> std::io::Result<usize> {
            if self.0.len().saturating_add(bytes.len()) > MAX_BYTES {
                return Err(std::io::Error::other("preview exceeds 64 KiB"));
            }
            self.0.extend_from_slice(bytes);
            Ok(bytes.len())
        }
        fn flush(&mut self) -> std::io::Result<()> {
            Ok(())
        }
    }
    let mut out = Bounded(Vec::new());
    serde_json::to_writer(&mut out, value).map_err(|e| e.to_string())?;
    Ok(out.0)
}

fn emit(state: &mut State, kind: PreviewKind) {
    let Some(local) = state.local.as_mut().filter(|l| !l.unavailable) else { return };
    let mut event = PreviewEvent { gesture: local.gesture, sequence: local.sequence, kind };
    let size = bounded_bytes(&event).map(|b| b.len());
    if size.as_ref().map_or(true, |n| local.bytes.saturating_add(*n) > MAX_BYTES - 1024)
        || local.sequence >= (MAX_EVENTS - 1) as u64
        || state.outgoing.len() >= MAX_EVENTS - 1
    {
        event.kind = PreviewKind::Unavailable { reason: "Gesture preview limit reached; the saved version will follow.".into() };
        local.unavailable = true;
    } else if let Ok(n) = size {
        local.bytes += n;
    }
    local.sequence = local.sequence.saturating_add(1);
    state.outgoing.push(LocalEvent { document: local.doc, local_revision: local.revision, base: local.base.clone(), event });
}

pub(crate) fn prepare_down(app: &mut PhotocraftApp) {
    if !app.collaboration.enabled {
        return;
    }
    if app.collaboration.local.is_some() {
        emit(&mut app.collaboration, PreviewKind::Cancel);
    }
    app.collaboration.local = None;
    app.collaboration.candidate = app.session.active().map(|st| (st.doc.id, st.revision, st.doc.clone()));
}

pub(crate) fn begin(app: &mut PhotocraftApp, params: Option<Value>) {
    if !app.collaboration.enabled {
        return;
    }
    let (Some(st), Some(d), Some((doc, revision, base))) = (app.session.active(), app.drag.as_ref(), app.collaboration.candidate.take()) else { return };
    if doc != st.doc.id || !Arc::ptr_eq(&base, &st.doc) {
        return;
    }
    app.collaboration.next_gesture = app.collaboration.next_gesture.saturating_add(1);
    app.collaboration.local = Some(Local {
        doc,
        revision,
        base,
        gesture: app.collaboration.next_gesture,
        sequence: 0,
        fed: d.points.len(),
        bytes: 0,
        offset: None,
        unavailable: false,
    });
    let kind = if crate::canvas::strokes_live(d.tool) {
        if st.symmetry_path.is_some() || matches!(st.channel_view.target, photocraft_engine::channel_cmds::ChannelTarget::Color(_)) {
            PreviewKind::Unavailable { reason: "Symmetry and individual-channel painting use saved-version updates.".into() }
        } else if let Some(mut params) = params {
            let target = params.get("target").and_then(Value::as_str);
            if !matches!(target, Some("pixels" | "mask")) {
                PreviewKind::Unavailable { reason: "Channel previews use saved-version updates.".into() }
            } else if let Some(layer) = st.active_layer {
                params["layer"] = json!(layer.0);
                let brush = bounded_bytes(&app.session.tools.brush).and_then(|v| serde_json::from_slice(&v).map_err(|e| e.to_string()));
                match brush {
                    Ok(brush) => PreviewKind::StrokeStart {
                        command: crate::canvas::stroke_command(d.tool).into(),
                        params,
                        brush,
                        foreground: app.session.tools.foreground,
                        background: app.session.tools.background,
                    },
                    Err(_) => PreviewKind::Unavailable { reason: "This brush is too large for a live preview; the saved version will follow.".into() },
                }
            } else {
                PreviewKind::Unavailable { reason: "Choose a paintable layer for a live preview.".into() }
            }
        } else {
            PreviewKind::Unavailable { reason: "This gesture uses saved-version updates.".into() }
        }
    } else if d.tool == Tool::Move {
        let layers = photocraft_engine::layer_multi_cmds::move_targets(&st.doc, &st.selected_layers());
        if d.modifiers.alt {
            PreviewKind::Unavailable { reason: "Duplicate-and-move uses saved-version updates.".into() }
        } else if layers.is_empty() || layers.len() > 64 {
            PreviewKind::Unavailable { reason: "This layer selection uses saved-version updates.".into() }
        } else {
            PreviewKind::MoveStart { layers: layers.into_iter().map(|l| l.0).collect() }
        }
    } else {
        PreviewKind::Unavailable { reason: "This tool uses saved-version updates.".into() }
    };
    let unavailable = matches!(kind, PreviewKind::Unavailable { .. });
    emit(&mut app.collaboration, kind);
    if unavailable && let Some(local) = &mut app.collaboration.local {
        local.unavailable = true;
    }
}

pub(crate) fn progress(app: &mut PhotocraftApp, drag: Option<&Drag>) {
    if !app.collaboration.enabled {
        return;
    }
    let Some(d) = drag.or(app.drag.as_ref()) else { return };
    let Some(local) = app.collaboration.local.as_ref().filter(|l| !l.unavailable) else { return };
    if d.reposition || app.session.active().is_none_or(|st| st.doc.id != local.doc || !Arc::ptr_eq(&st.doc, &local.base)) {
        emit(&mut app.collaboration, PreviewKind::Unavailable { reason: "The gesture base changed; the saved version will follow.".into() });
        if let Some(local) = &mut app.collaboration.local {
            local.unavailable = true;
        }
        return;
    }
    if d.tool == Tool::Move {
        let end = d.points.last().map_or(d.start, |p| [p[0], p[1]]);
        let offset = ((end[0] - d.start[0]).round().clamp(-1e7, 1e7) as i32, (end[1] - d.start[1]).round().clamp(-1e7, 1e7) as i32);
        if local.offset != Some(offset) {
            emit(&mut app.collaboration, PreviewKind::Offset { dx: offset.0, dy: offset.1 });
            if let Some(local) = &mut app.collaboration.local {
                local.offset = Some(offset);
            }
        }
    } else if crate::canvas::strokes_live(d.tool) && d.points.len() > local.fed {
        if d.points.len() > MAX_POINTS {
            emit(&mut app.collaboration, PreviewKind::Unavailable { reason: "Long strokes use saved-version updates after the preview limit.".into() });
            if let Some(local) = &mut app.collaboration.local {
                local.unavailable = true;
            }
        } else {
            let all = app.stylus.stroke_points(&d.points);
            let points = all.get(local.fed..).unwrap_or_default().to_vec();
            emit(&mut app.collaboration, PreviewKind::Points { points });
            if let Some(local) = &mut app.collaboration.local {
                local.fed = d.points.len();
            }
        }
    }
}

pub(crate) fn finish(app: &mut PhotocraftApp, committed: bool) {
    emit(&mut app.collaboration, if committed { PreviewKind::End } else { PreviewKind::Cancel });
    app.collaboration.local = None;
}

fn valid_points(points: &[Vec<f64>]) -> bool {
    !points.is_empty()
        && points.len() <= MAX_POINTS
        && points
            .iter()
            .all(|p| (2..=6).contains(&p.len()) && p.iter().all(|v| v.is_finite() && v.abs() <= 1_000_000.) && p.get(2).is_none_or(|v| (0.0..=1.0).contains(v)))
}

// A malicious two-point segment must not ask the native brush engine to generate millions of
// interpolated dabs. Large/complex gestures still arrive through the authoritative file path.
fn path_work(points: &[Vec<f64>], mut previous: Option<[f64; 2]>, doc: &Document) -> Result<(f64, Option<[f64; 2]>), String> {
    let mut travel = 0.;
    for p in points {
        let xy = [*p.first().ok_or("Preview point missing")?, *p.get(1).ok_or("Preview point missing")?];
        if xy[0] < -2048. || xy[1] < -2048. || xy[0] > doc.size.width as f64 + 2048. || xy[1] > doc.size.height as f64 + 2048. {
            return Err("Preview point is outside the bounded canvas area".into());
        }
        if let Some(last) = previous {
            travel += (xy[0] - last[0]).hypot(xy[1] - last[1]);
        }
        previous = Some(xy);
    }
    if travel > 16_384. {
        return Err("Long gestures use saved-version updates".into());
    }
    Ok((travel, previous))
}

fn points(params: &Value) -> Result<Vec<Vec<f64>>, String> {
    let points: Vec<Vec<f64>> = serde_json::from_value(params.get("points").cloned().unwrap_or(Value::Null)).map_err(|e| e.to_string())?;
    if !valid_points(&points) {
        return Err("Invalid preview stroke points".into());
    }
    Ok(points)
}

struct Prepared {
    native: NativePreview,
    shown: Arc<Document>,
    damage: Rect,
    point_count: usize,
    work_per_dab: f64,
    work_used: f64,
}

fn brush_work(settings: &photocraft_engine::paint::BrushSettings, travel: f64, points: usize) -> Result<(f64, f64), String> {
    let scatter = &settings.scattering;
    let dual = &settings.dual_brush;
    if !settings.size.is_finite()
        || !(0.5..=2048.).contains(&settings.size)
        || !settings.spacing.is_finite()
        || settings.spacing < 0.01
        || (scatter.enabled && (!scatter.scatter.jitter.is_finite() || !(0.0..=4.0).contains(&scatter.scatter.jitter)))
        || (dual.enabled
            && (!dual.size.is_finite() || !(0.5..=2048.).contains(&dual.size) || !dual.scatter.is_finite() || !(0.0..=4.0).contains(&dual.scatter)))
    {
        return Err("Brush exceeds live preview limits; the saved version will follow".into());
    }
    let diameter = f64::from(settings.size) * (1. + if scatter.enabled { f64::from(scatter.scatter.jitter) } else { 0. });
    let count = if scatter.enabled { f64::from(scatter.count.clamp(1, 16)) } else { 1. };
    let dual_area = if dual.enabled { f64::from(dual.size).powi(2) * (1. + f64::from(dual.scatter)).powi(2) * f64::from(dual.count.clamp(1, 16)) } else { 0. };
    // Four covers rotated tip bounds. Native interpolated dab spacing is never below0.5px.
    let per_dab = 4. * (diameter.powi(2) + dual_area) * count;
    let work = per_dab * (points as f64 + (travel / 0.5).ceil());
    if !work.is_finite() || work > MAX_DAB_PIXEL_WORK {
        return Err("Gesture exceeds live preview work budget; the saved version will follow".into());
    }
    Ok((per_dab, work))
}

fn start(st: &photocraft_engine::DocState, event: &PreviewEvent) -> Result<Prepared, String> {
    match &event.kind {
        PreviewKind::StrokeStart { command, params, brush, foreground, background } => {
            if !matches!(command.as_str(), "paint.stroke" | "paint.pencil") || !params.is_object() {
                return Err("Unsupported preview command".into());
            }
            let layer = params.get("layer").and_then(Value::as_u64).map(LayerId).ok_or("Preview layer missing")?;
            if st.doc.layer(layer).is_none() || !matches!(params.get("target").and_then(Value::as_str), Some("pixels" | "mask")) {
                return Err("Unsupported preview target".into());
            }
            if !foreground.iter().chain(background).all(|v| v.is_finite()) {
                return Err("Invalid preview colour".into());
            }
            let p = points(params)?;
            let (travel, _) = path_work(&p, None, &st.doc)?;
            if params
                .as_object()
                .is_none_or(|p| p.keys().any(|k| !matches!(k.as_str(), "points" | "erase" | "zoom" | "target" | "layer" | "seed" | "autoErase")))
                || params.get("zoom").and_then(Value::as_f64).is_none_or(|z| !z.is_finite() || !(0.01..=64.).contains(&z))
                || params.get("seed").and_then(Value::as_u64).is_none()
            {
                return Err("Unsupported preview paint parameters".into());
            }
            let settings: photocraft_engine::paint::BrushSettings = serde_json::from_value(brush.clone()).map_err(|e| e.to_string())?;
            let (work_per_dab, work_used) = brush_work(&settings, travel, p.len())?;
            let mut session = Session::new();
            session.add_document((*st.doc).clone(), None);
            session.tools.brush = settings;
            session.tools.foreground = *foreground;
            session.tools.background = *background;
            if let Some(active) = session.active_mut() {
                active.active_layer = Some(layer);
                active.selected_layers = vec![layer];
            }
            let live = LiveStroke::begin_with(&session, command, params).map_err(|e| e.to_string())?;
            let shown = live.doc.clone();
            let damage = live.bounds();
            Ok(Prepared { native: NativePreview::Stroke(Box::new(live)), shown, damage, point_count: p.len(), work_per_dab, work_used })
        }
        PreviewKind::MoveStart { layers } => {
            if layers.is_empty() || layers.len() > 64 {
                return Err("Invalid preview layer selection".into());
            }
            let ids: Vec<_> = layers.iter().copied().map(LayerId).collect();
            let expected = photocraft_engine::layer_multi_cmds::move_targets(&st.doc, &ids);
            if expected != ids {
                return Err("Preview layers are missing or overlap".into());
            }
            photocraft_engine::layer_multi_cmds::moved(&st.doc, &ids, 0, 0).map_err(|e| e.to_string())?;
            Ok(Prepared {
                native: NativePreview::Move { base: st.doc.clone(), layers: ids },
                shown: st.doc.clone(),
                damage: Rect::EMPTY,
                point_count: 0,
                work_per_dab: 0.,
                work_used: 0.,
            })
        }
        _ => Err("Preview must begin with a supported gesture".into()),
    }
}

/// The host supplies an authenticated display label and an exact admitted local base. Replayed
/// events are ignored, but a gap, changed base or competing gesture never modifies local work.
pub fn receive(app: &mut PhotocraftApp, local_doc: DocId, expected_local_revision: u64, peer: &str, event: PreviewEvent, now_ms: f64) -> Result<(), String> {
    let result = receive_inner(app, local_doc, expected_local_revision, peer, event, now_ms);
    if result.is_err() && app.collaboration.remote.as_ref().is_some_and(|r| r.peer == peer) {
        retire(&mut app.collaboration);
    }
    result
}

fn receive_inner(app: &mut PhotocraftApp, local_doc: DocId, expected_local_revision: u64, peer: &str, event: PreviewEvent, now_ms: f64) -> Result<(), String> {
    if !app.collaboration.enabled || peer.is_empty() || peer.len() > 96 || !now_ms.is_finite() {
        return Err("Collaboration preview unavailable".into());
    }
    let st = app.session.active().filter(|st| st.doc.id == local_doc).ok_or("Preview document is not active")?;
    if let PreviewKind::Cursor { position } = event.kind {
        if let Some(position) = position {
            if !position.iter().all(|v| v.is_finite() && v.abs() <= 1_000_000.) {
                return Err("Invalid cursor position".into());
            }
            if app.collaboration.cursors.len() < MAX_PEERS || app.collaboration.cursors.contains_key(peer) {
                app.collaboration.cursors.insert(peer.into(), Cursor { doc: local_doc, position, seen_ms: now_ms });
            }
        } else {
            app.collaboration.cursors.remove(peer);
        }
        return Ok(());
    }
    if st.revision != expected_local_revision || st.is_dirty() || app.drag.is_some() {
        retire(&mut app.collaboration);
        return Err("Live preview waits until your local work is saved".into());
    }
    if app.collaboration.retired.get(peer).is_some_and(|g| event.gesture <= *g) {
        return Ok(());
    }
    if let Some(r) = app.collaboration.remote.as_ref()
        && (r.doc != local_doc || r.revision != st.revision || !Arc::ptr_eq(&r.base, &st.doc))
    {
        retire(&mut app.collaboration);
    }
    if let Some(r) = app.collaboration.remote.as_mut()
        && r.peer == peer
        && r.gesture == event.gesture
        && event.sequence <= r.sequence
    {
        // The host calls us only for fresh, authorized transport state. A stationary held
        // gesture's heartbeat extends its lease without painting the same points twice.
        r.seen_ms = now_ms;
        return Ok(());
    }
    if matches!(event.kind, PreviewKind::Cancel | PreviewKind::Unavailable { .. }) {
        if app.collaboration.remote.as_ref().is_some_and(|r| r.peer == peer && r.gesture == event.gesture) {
            retire(&mut app.collaboration);
        }
        return Ok(());
    }
    let size = bounded_bytes(&event)?.len();
    if app.collaboration.remote.is_none() {
        if event.sequence != 0 || event.gesture == 0 {
            return Err("Preview start is missing".into());
        }
        let Prepared { native, shown, damage, point_count, work_per_dab, work_used } = start(st, &event)?;
        let (travel, last_point) = match &event.kind {
            PreviewKind::StrokeStart { params, .. } => path_work(&points(params)?, None, &st.doc)?,
            _ => (0., None),
        };
        app.collaboration.next_preview = app.collaboration.next_preview.wrapping_add(1) & 0xfffff;
        let key = KEY_BASE | (app.collaboration.next_preview << 12);
        app.collaboration.remote = Some(Remote {
            doc: local_doc,
            revision: st.revision,
            base: st.doc.clone(),
            peer: peer.into(),
            gesture: event.gesture,
            sequence: 0,
            points: point_count,
            last_point,
            travel,
            work_per_dab,
            work_used,
            bytes: size,
            ended: false,
            seen_ms: now_ms,
            key,
            damage: vec![damage],
            native,
            shown,
        });
        return Ok(());
    }
    let Some(r) = app.collaboration.remote.as_mut() else { return Ok(()) };
    if r.peer != peer || r.gesture != event.gesture {
        return Err("Another collaborator's gesture is being previewed; saved versions will follow".into());
    }
    if event.sequence != r.sequence + 1 || event.sequence >= MAX_EVENTS as u64 || r.bytes.saturating_add(size) > MAX_BYTES || r.ended {
        retire(&mut app.collaboration);
        return Err("Incomplete or oversized preview; waiting for the saved version".into());
    }
    let damage = match (&mut r.native, event.kind) {
        (NativePreview::Stroke(live), PreviewKind::Points { points }) => {
            if !valid_points(&points) || r.points.saturating_add(points.len()) > MAX_POINTS {
                retire(&mut app.collaboration);
                return Err("Invalid or too many preview points".into());
            }
            let (travel, last_point) = path_work(&points, r.last_point, &r.base)?;
            if r.travel + travel > 16_384. {
                retire(&mut app.collaboration);
                return Err("Long gestures use saved-version updates".into());
            }
            let work = r.work_per_dab * (points.len() as f64 + (travel / 0.5).ceil());
            if !work.is_finite() || r.work_used + work > MAX_DAB_PIXEL_WORK {
                return Err("Gesture exceeds live preview work budget; the saved version will follow".into());
            }
            let pts: Vec<_> = points
                .iter()
                .filter_map(|p| {
                    let mut point = photocraft_engine::paint::StrokePoint::new(*p.first()?, *p.get(1)?, p.get(2).copied().unwrap_or(1.) as f32);
                    point.tilt_x = p.get(3).copied().unwrap_or(0.) as f32;
                    point.tilt_y = p.get(4).copied().unwrap_or(0.) as f32;
                    point.rotation = p.get(5).copied().unwrap_or(0.) as f32;
                    Some(point)
                })
                .collect();
            let damage = live.push(&pts).map_err(|e| e.to_string())?;
            r.points += points.len();
            r.travel += travel;
            r.work_used += work;
            r.last_point = last_point;
            r.shown = live.doc.clone();
            damage
        }
        (NativePreview::Move { base, layers }, PreviewKind::Offset { dx, dy }) => {
            if dx.unsigned_abs() > 1_000_000 || dy.unsigned_abs() > 1_000_000 {
                return Err("Preview offset is out of bounds".into());
            }
            r.shown = Arc::new(photocraft_engine::layer_multi_cmds::moved(base, layers, dx, dy).map_err(|e| e.to_string())?);
            // Unlike brush dabs, moving arbitrary effects/groups can affect the entire canvas.
            base.bounds()
        }
        (_, PreviewKind::End) => {
            r.ended = true;
            Rect::EMPTY
        }
        _ => {
            retire(&mut app.collaboration);
            return Err("Preview gesture changed type".into());
        }
    };
    r.sequence = event.sequence;
    r.bytes += size;
    r.seen_ms = now_ms;
    r.damage.push(damage);
    Ok(())
}

pub(crate) fn display_doc(app: &PhotocraftApp, idx: usize) -> Option<(Arc<Document>, u64)> {
    let st = app.session.documents().get(idx)?;
    let r = app.collaboration.remote.as_ref()?;
    if app.drag.is_some() || st.doc.id != r.doc || st.revision != r.revision || !Arc::ptr_eq(&st.doc, &r.base) {
        return None;
    }
    let shown = photocraft_engine::mode_cmds::display_document(&r.shown).map(Arc::new).unwrap_or_else(|| r.shown.clone());
    Some((shown, r.key + r.damage.len() as u64))
}

pub(crate) fn damage(app: &PhotocraftApp, doc: DocId, revision: u64, seen: u64, now: u64) -> Option<Rect> {
    let r = app.collaboration.remote.as_ref().filter(|r| r.doc == doc && r.revision == revision && r.key + r.damage.len() as u64 == now)?;
    let n = if seen == 0 { 0 } else { usize::try_from(seen.checked_sub(r.key)?).ok()? };
    Some(r.damage.get(n..)?.iter().fold(Rect::EMPTY, |a, b| a.union(b)))
}

/// Called at the normal canvas paint boundary. Cursors are document-space native egui overlays.
fn expire(app: &mut PhotocraftApp, now_ms: f64) {
    if app.collaboration.remote.as_ref().is_some_and(|r| {
        now_ms - r.seen_ms > PREVIEW_TTL_MS
            || app.session.active().is_none_or(|s| s.doc.id != r.doc || s.revision != r.revision || !Arc::ptr_eq(&s.doc, &r.base))
    }) {
        retire(&mut app.collaboration);
    }
    app.collaboration.cursors.retain(|_, c| now_ms - c.seen_ms <= CURSOR_TTL_MS);
}

pub(crate) fn canvas(app: &mut PhotocraftApp, ui: &egui::Ui, xf: &crate::canvas::ViewXform, doc: DocId, primary: bool) {
    if !app.collaboration.enabled {
        return;
    }
    let now_ms = ui.input(|i| i.time * 1000.);
    if primary && app.drag.is_none() && app.collaboration.local.is_some() {
        finish(app, false);
    }
    expire(app, now_ms);
    if primary {
        let point = crate::dialogs::free_pointer_over(ui.ctx(), xf.rect).map(|p| xf.to_doc(p));
        if app.collaboration.local_cursor != Some((doc, point)) {
            app.collaboration.local_cursor = Some((doc, point));
            if let Some(st) = app.session.active() {
                app.collaboration.outgoing.retain(|e| !matches!(e.event.kind, PreviewKind::Cursor { .. }));
                if app.collaboration.outgoing.len() < MAX_EVENTS {
                    app.collaboration.outgoing.push(LocalEvent {
                        document: doc,
                        local_revision: st.revision,
                        base: st.doc.clone(),
                        event: PreviewEvent { gesture: 0, sequence: 0, kind: PreviewKind::Cursor { position: point } },
                    });
                }
            }
        }
    }
    let painter = ui.painter_at(xf.rect);
    let color = crate::theme::Tokens::get(ui.ctx()).collaborator_color();
    for (peer, cursor) in &app.collaboration.cursors {
        if cursor.doc != doc {
            continue;
        }
        let p = xf.to_screen(cursor.position[0] as f32, cursor.position[1] as f32);
        painter.circle_filled(p, 3., color);
        painter.add(egui::Shape::convex_polygon(vec![p, p + egui::vec2(4., 13.), p + egui::vec2(8., 8.), p + egui::vec2(13., 4.)], color, egui::Stroke::NONE));
        let label = app.collaboration.labels.get(peer).map(String::as_str).unwrap_or(peer);
        painter.text(p + egui::vec2(10., 14.), egui::Align2::LEFT_TOP, label, egui::FontId::proportional(12.), color);
    }
    if app.collaboration.remote.is_some() || !app.collaboration.cursors.is_empty() {
        ui.ctx().request_repaint_after(std::time::Duration::from_millis(250));
    }
}

#[cfg(test)]
mod tests {
    use super::*;
    use crate::canvas::{ToolEvent, tool_event};

    fn pair(depth: u8, tool: Tool) -> (PhotocraftApp, PhotocraftApp) {
        let mut owner = PhotocraftApp::new(Session::new(), Default::default());
        owner.run("file.new", json!({"width":64,"height":64,"depth":depth,"background":"white"})).unwrap();
        owner.run("layer.new.layer", json!({})).unwrap();
        owner.run("edit.fill", json!({"color":"#254581"})).unwrap();
        owner.run("tools.setColors", json!({"foreground":"#19c563","background":"#ffffff"})).unwrap();
        owner.run("tools.setBrush", json!({"reset":true,"size":12,"pressureSize":false,"opacity":0.7,"hardness":0.4})).unwrap();
        owner.sync_views();
        owner.ui.tool = tool;
        owner.ui.tool_options.move_auto_select = false;
        owner.ui.extras.snap = false;
        owner.ui.view.show.smart_guides = false;
        let st = owner.session.active_mut().unwrap();
        st.saved_revision = st.revision;
        let mut session = Session::new();
        session.add_document((*st.doc).clone(), None);
        let mut peer = PhotocraftApp::new(session, Default::default());
        peer.sync_views();
        set_enabled(&mut owner, true);
        set_enabled(&mut peer, true);
        (owner, peer)
    }

    fn replay(owner: &mut PhotocraftApp, peer: &mut PhotocraftApp, cumulative: &mut Vec<PreviewEvent>) {
        cumulative.extend(drain(owner).into_iter().map(|e| e.event));
        let st = peer.session.active().unwrap();
        let (id, revision) = (st.doc.id, st.revision);
        for e in cumulative.iter() {
            receive(peer, id, revision, "Peer", e.clone(), 0.).unwrap();
        }
    }

    fn pixels(doc: &Document) -> Vec<[f32; 4]> {
        photocraft_compose::render(doc, doc.bounds()).px
    }

    #[test]
    fn native_brush_pencil_eraser_pixels_match_without_touching_receiver_history() {
        for depth in [8, 16, 32] {
            for tool in [Tool::Brush, Tool::Pencil, Tool::Eraser] {
                let (mut owner, mut peer) = pair(depth, tool);
                let original = peer.session.active().unwrap().doc.clone();
                let original_revision = peer.session.active().unwrap().revision;
                let original_pixels = pixels(&original);
                let mut events = Vec::new();
                tool_event(&mut owner, ToolEvent::Down { x: 12., y: 20., pressure: 0.4 }, egui::Modifiers::NONE);
                replay(&mut owner, &mut peer, &mut events);
                assert_ne!(pixels(&display_doc(&peer, 0).unwrap().0), original_pixels, "initial real dab is visible");
                for x in [18., 24., 30., 38., 45.] {
                    tool_event(&mut owner, ToolEvent::Move { x, y: 25., pressure: 0.8 }, egui::Modifiers::NONE);
                    replay(&mut owner, &mut peer, &mut events);
                }
                tool_event(&mut owner, ToolEvent::Up { x: 47., y: 25. }, egui::Modifiers::NONE);
                replay(&mut owner, &mut peer, &mut events);
                let shown = display_doc(&peer, 0).unwrap().0;
                let actual = pixels(&shown);
                let expected = pixels(&owner.session.active().unwrap().doc);
                assert_eq!(actual, expected, "{tool:?} at{depth}bit uses identical native pixels");
                let st = peer.session.active().unwrap();
                assert!(Arc::ptr_eq(&original, &st.doc));
                assert_eq!(st.revision, original_revision);
                assert_eq!(st.history.past_len(), 0);
                assert!(!st.is_dirty());
                assert!(matches!(events.last().unwrap().kind, PreviewKind::End));
                assert!(peer.collaboration.remote.as_ref().unwrap().ended);
            }
        }
    }

    #[test]
    fn move_offsets_reuse_native_layers_and_preserve_owner_and_peer_history() {
        let (mut owner, mut peer) = pair(8, Tool::Move);
        let mut events = Vec::new();
        let before = owner.session.active().unwrap().history.past_len();
        tool_event(&mut owner, ToolEvent::Down { x: 20., y: 20., pressure: 1. }, egui::Modifiers::NONE);
        tool_event(&mut owner, ToolEvent::Move { x: 29., y: 26., pressure: 1. }, egui::Modifiers::NONE);
        replay(&mut owner, &mut peer, &mut events);
        let st = owner.session.active().unwrap();
        let ids = photocraft_engine::layer_multi_cmds::move_targets(&st.doc, &st.selected_layers());
        let expected = photocraft_engine::layer_multi_cmds::moved(&st.doc, &ids, 9, 6).unwrap();
        assert_eq!(pixels(&display_doc(&peer, 0).unwrap().0), pixels(&expected));
        assert_eq!(st.history.past_len(), before);
        tool_event(&mut owner, ToolEvent::Up { x: 29., y: 26. }, egui::Modifiers::NONE);
        replay(&mut owner, &mut peer, &mut events);
        assert_eq!(pixels(&display_doc(&peer, 0).unwrap().0), pixels(&owner.session.active().unwrap().doc));
        assert_eq!(peer.session.active().unwrap().history.past_len(), 0);
    }

    #[test]
    fn changed_base_competing_gesture_and_sequence_gap_fail_closed() {
        let (mut owner, mut peer) = pair(8, Tool::Pencil);
        tool_event(&mut owner, ToolEvent::Down { x: 10., y: 10., pressure: 1. }, egui::Modifiers::NONE);
        let first = drain(&mut owner).remove(0).event;
        let st = peer.session.active().unwrap();
        let (id, rev) = (st.doc.id, st.revision);
        receive(&mut peer, id, rev, "A", first.clone(), 0.).unwrap();
        assert!(receive(&mut peer, id, rev, "B", first.clone(), 0.).is_err());
        let gap = PreviewEvent { gesture: first.gesture, sequence: 2, kind: PreviewKind::End };
        assert!(receive(&mut peer, id, rev, "A", gap, 0.).is_err());
        assert!(display_doc(&peer, 0).is_none());
        receive(&mut peer, id, rev, "A", first.clone(), 0.).unwrap();
        assert!(display_doc(&peer, 0).is_none(), "retired cumulative events cannot resurrect");
        peer.run("layer.renameLayer", json!({"name":"Local work"})).unwrap();
        assert!(receive(&mut peer, id, rev, "C", first, 0.).is_err());
        assert_eq!(peer.session.active().unwrap().history.past_len(), 1);
    }

    #[test]
    fn gesture_limit_is_explicit_and_cancellation_does_not_erase_local_work() {
        let (mut owner, mut peer) = pair(8, Tool::Pencil);
        tool_event(&mut owner, ToolEvent::Down { x: 10., y: 10., pressure: 1. }, egui::Modifiers::NONE);
        let mut events = Vec::new();
        replay(&mut owner, &mut peer, &mut events);
        clear_peer(&mut peer, "Peer");
        assert!(display_doc(&peer, 0).is_none());
        owner.collaboration.local.as_mut().unwrap().sequence = (MAX_EVENTS - 1) as u64;
        tool_event(&mut owner, ToolEvent::Move { x: 20., y: 20., pressure: 1. }, egui::Modifiers::NONE);
        let last = drain(&mut owner).pop().unwrap();
        assert!(matches!(last.event.kind, PreviewKind::Unavailable { .. }));
        assert!(owner.collaboration.local.as_ref().unwrap().unavailable);
        tool_event(&mut owner, ToolEvent::Up { x: 20., y: 20. }, egui::Modifiers::NONE);
        assert!(owner.session.active().unwrap().is_dirty(), "normal native commit still completes");
        assert!(!peer.session.active().unwrap().is_dirty());
    }

    #[test]
    fn dropped_poll_batches_catch_up_pressure_tilt_and_seeded_brush_then_end_stays_visible() {
        let (mut owner, mut peer) = pair(16, Tool::Brush);
        owner.run("tools.setBrush", json!({"pressureSize":true,"shapeDynamics":{"enabled":true,"brushProjection":true,"angle":{"jitter":0.4}}})).unwrap();
        owner.stylus.feed.set(Some(crate::stylus::PenSample { pressure: 0.3, tilt_x: 35., tilt_y: -20., rotation: 45., eraser: false }));
        tool_event(&mut owner, ToolEvent::Down { x: 10., y: 10., pressure: 0.3 }, egui::Modifiers::NONE);
        let mut events = Vec::new();
        replay(&mut owner, &mut peer, &mut events);
        for (x, pressure) in [(18., 0.4), (25., 0.6), (32., 0.9), (40., 0.7)] {
            owner.stylus.feed.set(Some(crate::stylus::PenSample { pressure, tilt_x: 10., tilt_y: 30., rotation: 100., eraser: false }));
            tool_event(&mut owner, ToolEvent::Move { x, y: 25., pressure }, egui::Modifiers::NONE);
        }
        tool_event(&mut owner, ToolEvent::Up { x: 44., y: 26. }, egui::Modifiers::NONE);
        replay(&mut owner, &mut peer, &mut events);
        assert!(events.iter().any(|e| matches!(&e.kind,PreviewKind::Points{points} if points.iter().any(|p|p.len()==6 && p[3]!=0.))));
        assert_eq!(pixels(&display_doc(&peer, 0).unwrap().0), pixels(&owner.session.active().unwrap().doc));
        let st = peer.session.active().unwrap();
        let (id, revision, base) = (st.doc.id, st.revision, st.doc.clone());
        let damage_len = peer.collaboration.remote.as_ref().unwrap().damage.len();
        for now in [10_000., 20_000., 30_000.] {
            for event in &events {
                receive(&mut peer, id, revision, "Peer", event.clone(), now).unwrap();
            }
            expire(&mut peer, now + 500.);
            assert!(display_doc(&peer, 0).is_some());
            assert_eq!(peer.collaboration.remote.as_ref().unwrap().damage.len(), damage_len, "replay does not paint twice");
        }
        assert!(Arc::ptr_eq(&base, &peer.session.active().unwrap().doc));
        expire(&mut peer, 46_000.);
        assert!(display_doc(&peer, 0).is_none());
        for event in &events {
            receive(&mut peer, id, revision, "Peer", event.clone(), 46_001.).unwrap();
        }
        assert!(display_doc(&peer, 0).is_none(), "expired cumulative snapshots cannot resurrect");
    }

    #[test]
    fn mask_eraser_and_selection_reuse_the_original_native_target() {
        for tool in [Tool::Brush, Tool::Eraser] {
            let (mut owner, mut peer) = pair(16, tool);
            owner.run("layer.layerMask.revealAll", json!({})).unwrap();
            owner.run("select.rect", json!({"x":8,"y":8,"width":20,"height":48,"feather":2.})).unwrap();
            owner.run("tools.setColors", json!({"foreground":"#000000","background":"#000000"})).unwrap();
            owner.ui.mask_target = true;
            let st = owner.session.active_mut().unwrap();
            st.saved_revision = st.revision;
            peer.session.active_mut().unwrap().doc = st.doc.clone();
            let mut events = Vec::new();
            tool_event(&mut owner, ToolEvent::Down { x: 12., y: 20., pressure: 1. }, egui::Modifiers::NONE);
            tool_event(&mut owner, ToolEvent::Move { x: 42., y: 25., pressure: 1. }, egui::Modifiers::NONE);
            tool_event(&mut owner, ToolEvent::Up { x: 44., y: 25. }, egui::Modifiers::NONE);
            replay(&mut owner, &mut peer, &mut events);
            assert_eq!(pixels(&display_doc(&peer, 0).unwrap().0), pixels(&owner.session.active().unwrap().doc));
            assert_eq!(peer.session.active().unwrap().history.past_len(), 0);
        }
    }

    #[test]
    fn auto_select_retains_original_base_and_invalid_remote_coordinates_are_rejected() {
        let (mut owner, mut peer) = pair(8, Tool::Move);
        owner.ui.tool_options.move_auto_select = true;
        let st = owner.session.active().unwrap();
        let (revision, base) = (st.revision, st.doc.clone());
        tool_event(&mut owner, ToolEvent::Down { x: 20., y: 20., pressure: 1. }, egui::Modifiers::NONE);
        let event = drain(&mut owner).remove(0);
        assert_eq!(event.local_revision, revision);
        assert!(Arc::ptr_eq(&event.base, &base));
        let st = peer.session.active().unwrap();
        let (id, revision) = (st.doc.id, st.revision);
        assert!(
            receive(
                &mut peer,
                id,
                revision,
                "Peer",
                PreviewEvent { gesture: 0, sequence: 0, kind: PreviewKind::Cursor { position: Some([f64::NAN, 1.]) } },
                0.
            )
            .is_err()
        );
        let (mut owner, _) = pair(8, Tool::Pencil);
        tool_event(&mut owner, ToolEvent::Down { x: 10., y: 10., pressure: 1. }, egui::Modifiers::NONE);
        let mut bad = drain(&mut owner).remove(0).event;
        if let PreviewKind::StrokeStart { params, .. } = &mut bad.kind {
            params["layer"] = json!(peer.session.active().unwrap().active_layer.unwrap().0);
            params["points"] = json!([[1., 1., 1.], [999999., 999999., 1.]]);
        }
        assert!(receive(&mut peer, id, revision, "Peer", bad, 0.).is_err());
        assert!(display_doc(&peer, 0).is_none());
        assert_eq!(peer.session.active().unwrap().history.past_len(), 0);
    }

    #[test]
    fn unsupported_duplicate_move_and_changed_gesture_base_emit_explicit_fallback() {
        let (mut owner, _) = pair(8, Tool::Move);
        tool_event(&mut owner, ToolEvent::Down { x: 20., y: 20., pressure: 1. }, egui::Modifiers::ALT);
        assert!(drain(&mut owner).iter().any(|e| matches!(e.event.kind, PreviewKind::Unavailable { .. })));
        let (mut owner, _) = pair(8, Tool::Pencil);
        tool_event(&mut owner, ToolEvent::Down { x: 20., y: 20., pressure: 1. }, egui::Modifiers::NONE);
        drain(&mut owner);
        owner.run("layer.renameLayer", json!({"name":"Changed during gesture"})).unwrap();
        progress(&mut owner, None);
        assert!(drain(&mut owner).iter().any(|e| matches!(e.event.kind, PreviewKind::Unavailable { .. })));
        assert_eq!(owner.session.active().unwrap().doc.layer(owner.session.active().unwrap().active_layer.unwrap()).unwrap().name, "Changed during gesture");
    }

    #[test]
    fn compact_remote_payload_cannot_request_unbounded_native_brush_work() {
        let (mut owner, mut peer) = pair(8, Tool::Pencil);
        tool_event(&mut owner, ToolEvent::Down { x: 10., y: 10., pressure: 1. }, egui::Modifiers::NONE);
        let mut event = drain(&mut owner).remove(0).event;
        let st = peer.session.active().unwrap();
        let (id, revision, original) = (st.doc.id, st.revision, st.doc.clone());
        if let PreviewKind::StrokeStart { brush, .. } = &mut event.kind {
            brush["size"] = json!(1024.);
            brush["scattering"] = json!({"enabled":true,"count":16});
        }
        assert!(bounded_bytes(&event).unwrap().len() < MAX_BYTES);
        assert!(receive(&mut peer, id, revision, "Expensive", event.clone(), 0.).unwrap_err().contains("work budget"));
        assert!(display_doc(&peer, 0).is_none());
        if let PreviewKind::StrokeStart { brush, .. } = &mut event.kind {
            brush["size"] = json!(128.);
            brush["scattering"] = json!({"enabled":false});
        }
        receive(&mut peer, id, revision, "Expensive", event.clone(), 0.).unwrap();
        let step = PreviewEvent { gesture: event.gesture, sequence: 1, kind: PreviewKind::Points { points: vec![vec![2000., 10., 1.]] } };
        assert!(receive(&mut peer, id, revision, "Expensive", step, 0.).unwrap_err().contains("work budget"));
        assert!(display_doc(&peer, 0).is_none());
        assert!(Arc::ptr_eq(&original, &peer.session.active().unwrap().doc));
        assert_eq!(peer.session.active().unwrap().history.past_len(), 0);
    }
}
