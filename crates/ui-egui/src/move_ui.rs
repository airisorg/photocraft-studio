//! Move tool drags shown live (#128): while the pointer drags, the canvas shows the document with
//! the moving layers already at the pointer, exactly as `layer.translate` will leave them, and
//! recomposites only where they were and where they are. Releasing commits one `layer.translate`
//! (one history step) whose damage rect refreshes the same area.
// PhotoCraft Studio modification (2026-10-08): retain each preview's actual painted bounds
// so edge-clipped vector shapes do not leave stale pixels as they move into the canvas.

use std::sync::Arc;

use photocraft_doc::{DocId, Document, LayerId};
use photocraft_geom::Rect;

use crate::PhotocraftApp;
use crate::state::Tool;

/// Preview keys of move drags: `BASE + n`, one per offset shown (see `canvas::display_doc`).
const BASE: u64 = 1 << 39;

pub(crate) struct MovePreview {
    doc: DocId,
    revision: u64,
    /// Layers that move ([`photocraft_engine::layer_multi_cmds::move_targets`]).
    ids: Vec<LayerId>,
    /// What the original moving layers can change (`None` = anything).
    original_area: Option<Rect>,
    /// Actual offsets and painted areas, by preview key (`BASE + index`).
    frames: Vec<MoveFrame>,
    /// The document at the latest offset.
    shown: Option<Arc<Document>>,
}

struct MoveFrame {
    offset: (i32, i32),
    area: Option<Rect>,
}

impl MovePreview {
    fn key(&self) -> u64 {
        BASE + self.frames.len() as u64
    }
    fn area_of(&self, key: u64) -> Option<Rect> {
        if key == 0 {
            return self.original_area;
        }
        let i = usize::try_from(key.checked_sub(BASE + 1)?).ok()?;
        self.frames.get(i)?.area
    }
}

fn painted_area(doc: &Document, ids: &[LayerId]) -> Option<Rect> {
    let canvas = doc.bounds();
    let b = ids.iter().try_fold(Rect::EMPTY, |acc, id| Some(acc.union(&photocraft_compose::change_bounds(doc.layer(*id)?, canvas)?)))?;
    Some(if b.is_empty() { b } else { b.inflate(1).intersect(&canvas) })
}

/// The whole-pixel offset of the current Move drag on document `idx`, if one is under way.
fn drag_offset(app: &PhotocraftApp) -> Option<(i32, i32)> {
    let d = app.drag.as_ref().filter(|d| d.tool == Tool::Move)?;
    let end = d.points.last().map_or(d.start, |p| [p[0], p[1]]);
    let dx = (end[0] - d.start[0]).round().clamp(-1e7, 1e7) as i32;
    let dy = (end[1] - d.start[1]).round().clamp(-1e7, 1e7) as i32;
    Some((dx, dy))
}

/// The document to show while a Move drag is under way on document `idx`: the moving layers at
/// the pointer. `None` without a drag (or when the layers can't move: locked, say; the drag then
/// shows its arrow and the release reports why).
pub(crate) fn display_doc(app: &mut PhotocraftApp, idx: usize) -> Option<(Arc<Document>, u64)> {
    // Preferences › Interface › Show bounding box when dragging layer: outline and arrow only.
    if app.session.active_index() != Some(idx) || app.session.prefs().interface.show_bounding_box_when_dragging_layer {
        return None;
    }
    let Some(offset) = drag_offset(app) else {
        app.move_preview = None;
        return None;
    };
    let st = app.session.documents().get(idx)?;
    let (doc_id, revision, doc) = (st.doc.id, st.revision, st.doc.clone());
    let fresh = app.move_preview.as_ref().is_some_and(|p| p.doc == doc_id && p.revision == revision);
    if !fresh {
        let ids = photocraft_engine::layer_multi_cmds::move_targets(&doc, &st.selected_layers());
        if ids.is_empty() {
            return None;
        }
        let original_area = painted_area(&doc, &ids);
        app.move_preview = Some(MovePreview { doc: doc_id, revision, ids, original_area, frames: Vec::new(), shown: None });
    }
    let p = app.move_preview.as_mut()?;
    if offset == (0, 0) && p.frames.is_empty() {
        return None;
    }
    if p.frames.last().is_none_or(|f| f.offset != offset) {
        let t0 = crate::gpu_canvas::now_ms();
        match photocraft_engine::layer_multi_cmds::moved(&doc, &p.ids, offset.0, offset.1) {
            Ok(d) => {
                // Moving inward can reveal vector pixels absent from the original clipped
                // cache. Translating its original bounds would miss those pixels next frame.
                let area = painted_area(&d, &p.ids);
                // Duotone documents display through their inks.
                let d = photocraft_engine::mode_cmds::display_document(&d).unwrap_or(d);
                p.shown = Some(Arc::new(d));
                p.frames.push(MoveFrame { offset, area });
            }
            Err(_) => {
                p.shown = None;
                return None;
            }
        }
        app.perf.span("move preview", crate::gpu_canvas::now_ms() - t0);
    }
    let p = app.move_preview.as_ref()?;
    Some((p.shown.clone()?, p.key()))
}

/// What changed between preview (or document) key `seen` and `now` of the current Move drag on
/// document `doc` at `revision`: where the moving layers were in both. `None` when either key
/// isn't one of this drag's (the canvas then recomposites everything).
pub(crate) fn damage(app: &PhotocraftApp, doc: DocId, revision: u64, seen: u64, now: u64) -> Option<Rect> {
    let p = app.move_preview.as_ref().filter(|p| p.doc == doc && p.revision == revision)?;
    if seen == now || (seen < BASE && seen != 0) || (now < BASE && now != 0) {
        return None;
    }
    let (a, b) = (p.area_of(seen)?, p.area_of(now)?);
    Some(if a.is_empty() {
        b
    } else if b.is_empty() {
        a
    } else {
        a.union(&b)
    })
}

/// Whether the canvas shows a Move drag's layers at the pointer.
pub(crate) fn showing(app: &PhotocraftApp) -> bool {
    app.move_preview.as_ref().is_some_and(|p| p.shown.is_some())
}

/// Whether `key` is a Move drag preview key.
pub(crate) fn is_preview_key(key: u64) -> bool {
    key > BASE && key < BASE << 1
}

/// Release: one `layer.translate` by the drag's offset. The canvas already shows the result, so
/// its caches are told it showed the document: the command's damage rect refreshes that area.
pub(crate) fn finish(app: &mut PhotocraftApp, dx: f64, dy: f64) {
    // Only when the canvas shows this very offset (else it recomposites everything once).
    let at = (dx.clamp(-1e7, 1e7) as i32, dy.clamp(-1e7, 1e7) as i32);
    let shown = app.move_preview.take().filter(|p| p.shown.is_some() && p.frames.last().is_some_and(|f| f.offset == at));
    if dx == 0.0 && dy == 0.0 {
        return;
    }
    if app.run("layer.translate", serde_json::json!({"dx": dx, "dy": dy})).is_err() {
        return;
    }
    let Some(p) = shown else { return };
    crate::canvas::shown_as_document(app, p.doc, is_preview_key);
}

#[cfg(test)]
mod tests {
    use photocraft_geom::Rect;
    use serde_json::json;

    use crate::PhotocraftApp;
    use crate::canvas::ToolEvent;
    use crate::state::Tool;

    #[test]
    fn edge_clipped_shape_move_damage_reconstructs_every_held_frame_without_trails() {
        // A cached vector shape is clipped to the original canvas. Moving it inward reveals
        // pixels which translating that clipped cache's bounds cannot describe.
        for (rect, start, direction) in [
            ([48, 16, 32, 32], [56.0, 32.0], [-1.0, 0.25]),
            ([-16, 16, 32, 32], [8.0, 32.0], [1.0, 0.25]),
            ([16, 48, 32, 32], [32.0, 56.0], [0.25, -1.0]),
            ([16, -16, 32, 32], [32.0, 8.0], [0.25, 1.0]),
        ] {
            let mut app = PhotocraftApp::new(photocraft_engine::Session::new(), crate::Services::default());
            app.session.execute("file.new", json!({"width": 64, "height": 64})).unwrap();
            app.session.execute("shape.create", json!({"kind": "ellipse", "rect": rect, "fill": "#fa9974"})).unwrap();
            app.sync_views();
            app.ui.tool = Tool::Move;
            app.ui.extras.snap = false;
            app.ui.view.show.smart_guides = false;
            let modifiers = egui::Modifiers::NONE;
            crate::canvas::tool_event(&mut app, ToolEvent::Down { x: start[0], y: start[1], pressure: 1.0 }, modifiers);
            let st = app.session.active().unwrap();
            let (id, revision, history, original) = (st.doc.id, st.revision, st.history.entries().len(), st.doc.clone());
            let mut screen = photocraft_compose::flatten(&original);
            let mut seen = 0;
            let mut last = None;
            for step in 1..=6 {
                let offset = [direction[0] * f64::from(step * 4), direction[1] * f64::from(step * 4)];
                crate::canvas::tool_event(&mut app, ToolEvent::Move { x: start[0] + offset[0], y: start[1] + offset[1], pressure: 1.0 }, modifiers);
                let (shown, key) = super::display_doc(&mut app, 0).unwrap();
                let st = app.session.active().unwrap();
                assert_eq!(st.revision, revision, "held preview must not edit the document");
                assert_eq!(st.history.entries().len(), history);
                assert!(std::sync::Arc::ptr_eq(&st.doc, &original));
                // A skipped paint must still redraw everything since the last displayed key.
                if step == 3 {
                    continue;
                }
                let damage = super::damage(&app, id, revision, seen, key).unwrap();
                let patch = photocraft_compose::render(&shown, damage);
                for y in damage.y0..damage.y1 {
                    for x in damage.x0..damage.x1 {
                        screen.px[(y * 64 + x) as usize] = patch.get(x, y);
                    }
                }
                let expected = photocraft_compose::flatten(&shown);
                let different = screen.px.iter().zip(&expected.px).filter(|(a, b)| a != b).count();
                assert_eq!(different, 0, "stale pixels at edge {rect:?}, held step {step}");
                seen = key;
                last = Some((shown, offset));
            }
            let (shown, offset) = last.unwrap();
            crate::canvas::tool_event(&mut app, ToolEvent::Up { x: start[0] + offset[0], y: start[1] + offset[1] }, modifiers);
            let st = app.session.active().unwrap();
            assert_eq!(st.history.entries().len(), history + 1, "one release, one undo entry");
            let committed = photocraft_compose::flatten(&st.doc);
            assert_eq!(committed.px, photocraft_compose::flatten(&shown).px);
            app.session.execute("edit.undo", json!({})).unwrap();
            assert_eq!(photocraft_compose::flatten(&app.session.active().unwrap().doc).px, photocraft_compose::flatten(&original).px);
            app.session.execute("edit.redo", json!({})).unwrap();
            assert_eq!(photocraft_compose::flatten(&app.session.active().unwrap().doc).px, committed.px);
        }
    }

    #[test]
    fn dragging_a_layer_larger_than_the_canvas_redraws_what_comes_in_from_beyond_the_edge() {
        let mut app = PhotocraftApp::new(photocraft_engine::Session::new(), crate::Services::default());
        app.session.execute("file.new", json!({"width": 64, "height": 64})).unwrap();
        app.sync_views();
        app.session.execute("layer.new.layer", json!({})).unwrap();
        app.session
            .edit("paint", |doc, a| {
                doc.layer_mut(a.unwrap()).unwrap().surface_mut().unwrap().fill_rect(Rect::new(-32, -32, 96, 96), &[1.0, 0.0, 0.0, 1.0]);
                Ok(())
            })
            .unwrap();
        app.ui.tool = Tool::Move;
        app.ui.extras.snap = false;
        app.ui.view.show.smart_guides = false;
        let m = egui::Modifiers::NONE;
        crate::canvas::tool_event(&mut app, ToolEvent::Down { x: 32.0, y: 32.0, pressure: 1.0 }, m);
        let mut keys = Vec::new();
        for x in [42.0, 52.0] {
            crate::canvas::tool_event(&mut app, ToolEvent::Move { x, y: 32.0, pressure: 1.0 }, m);
            keys.push(super::display_doc(&mut app, 0).unwrap().1);
        }
        let st = app.session.active().unwrap();
        // From +10 to +20 the strip x 0..10 shows pixels that were off the canvas a frame ago.
        let r = super::damage(&app, st.doc.id, st.revision, keys[0], keys[1]).unwrap();
        assert_eq!(r, Rect::new(0, 0, 64, 64));
    }

    /// Mid-drag on a painted layer: is the layer shown at the pointer?
    fn live(outline: bool) -> bool {
        let mut app = PhotocraftApp::new(photocraft_engine::Session::new(), crate::Services::default());
        app.session.execute("file.new", json!({"width": 64, "height": 64})).unwrap();
        app.sync_views();
        app.session.execute("layer.new.layer", json!({})).unwrap();
        app.session.execute("select.rect", json!({"x": 8, "y": 8, "width": 16, "height": 16})).unwrap();
        app.session.execute("edit.fill", json!({"color": "#ff0000"})).unwrap();
        app.session.execute("select.deselect", json!({})).unwrap();
        app.session.edit_prefs(|p| p.interface.show_bounding_box_when_dragging_layer = outline);
        app.ui.tool = Tool::Move;
        let m = egui::Modifiers::NONE;
        crate::canvas::tool_event(&mut app, ToolEvent::Down { x: 16.0, y: 16.0, pressure: 1.0 }, m);
        crate::canvas::tool_event(&mut app, ToolEvent::Move { x: 30.0, y: 20.0, pressure: 1.0 }, m);
        let shown = super::display_doc(&mut app, 0).is_some();
        assert_eq!(shown, super::showing(&app));
        shown
    }

    #[test]
    fn bounding_box_preference_turns_the_live_drag_off() {
        assert!(live(false), "by default the layer follows the pointer");
        assert!(!live(true), "with the preference on, only the outline and arrow move");
    }
}
