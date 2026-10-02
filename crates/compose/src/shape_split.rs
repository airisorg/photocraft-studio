//! A stroked shape layer split into its fill and its vector stroke (rasterised over the canvas),
//! for shapes with clipped layers: Photoshop draws the stroke above the clipped layers. Cached
//! per shape state, so tiles (and the GPU's textures) reuse one rasterisation.

use std::collections::HashMap;
use std::hash::{Hash, Hasher};
use std::sync::{Mutex, OnceLock};

use photocraft_color::PixelFormat;
use photocraft_doc::vector::ShapeLayer;
use photocraft_geom::Rect;
use photocraft_raster::Surface;

type Entry = ((Surface, Surface), u64);

fn cache() -> &'static Mutex<(HashMap<u64, Entry>, u64)> {
    static C: OnceLock<Mutex<(HashMap<u64, Entry>, u64)>> = OnceLock::new();
    C.get_or_init(|| Mutex::new((HashMap::new(), 0)))
}

const CAPACITY: usize = 32;

/// (fill only, stroke only) of a stroked shape, rendered over `canvas` (RGBA8, as the CPU
/// compositor's split always was). `None` without a stroke.
pub fn split(sh: &ShapeLayer, canvas: Rect) -> Option<(Surface, Surface)> {
    sh.stroke.as_ref()?;
    let mut h = std::collections::hash_map::DefaultHasher::new();
    (canvas.x0, canvas.y0, canvas.x1, canvas.y1).hash(&mut h);
    format!("{:?}{:?}{:?}{:?}", sh.path, sh.fill, sh.stroke, sh.live).hash(&mut h);
    let key = h.finish();
    {
        let mut c = cache().lock().unwrap_or_else(|e| e.into_inner());
        c.1 += 1;
        let tick = c.1;
        if let Some(e) = c.0.get_mut(&key) {
            e.1 = tick;
            return Some(e.0.clone());
        }
    }
    let fmt = PixelFormat::RGBA8;
    let fill_only = ShapeLayer { stroke: None, cache: None, ..sh.clone() };
    let stroke_only = ShapeLayer { fill: None, cache: None, ..sh.clone() };
    let parts = (photocraft_vector::render_shape(&fill_only, fmt, canvas), photocraft_vector::render_shape(&stroke_only, fmt, canvas));
    let mut c = cache().lock().unwrap_or_else(|e| e.into_inner());
    if c.0.len() >= CAPACITY
        && let Some(old) = c.0.iter().min_by_key(|e| (e.1).1).map(|e| *e.0)
    {
        c.0.remove(&old);
    }
    let tick = c.1;
    c.0.insert(key, (parts.clone(), tick));
    Some(parts)
}
