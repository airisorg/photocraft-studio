//! Paint helpers: Paint Bucket and Gradient tool.

use photocraft_algo::paint::{GradientShape, bucket_fill, paint_gradient};
use serde_json::{Value, json};

use crate::commands::{CommandSpec, blend_from_str};
use crate::{EngineError, Result, Session};

fn f(p: &Value, k: &str, d: f32) -> f32 {
    p.get(k).and_then(Value::as_f64).map_or(d, |v| v as f32)
}
fn b(p: &Value, k: &str, d: bool) -> bool {
    p.get(k).and_then(Value::as_bool).unwrap_or(d)
}
fn color(v: Option<&Value>, d: [f32; 4]) -> [f32; 4] {
    match v {
        Some(Value::Array(a)) if a.len() >= 3 => {
            let c: Vec<f32> = a.iter().map(|x| x.as_f64().unwrap_or(0.0) as f32).collect();
            [c[0], c[1], c[2], c.get(3).copied().unwrap_or(1.0)]
        }
        Some(Value::String(h)) => {
            let h = h.trim_start_matches('#');
            let c = |i: usize| h.get(i..i + 2).and_then(|x| u8::from_str_radix(x, 16).ok()).map(|v| f32::from(v) / 255.0);
            match (c(0), c(2), c(4)) {
                (Some(r), Some(g), Some(b)) => [r, g, b, c(6).unwrap_or(1.0)],
                _ => d,
            }
        }
        _ => d,
    }
}
fn point(p: &Value, k: &str) -> Option<(f32, f32)> {
    let a = p.get(k)?.as_array()?;
    Some((a.first()?.as_f64()? as f32, a.get(1)?.as_f64()? as f32))
}

fn bucket(s: &mut Session, p: &Value) -> Result<Value> {
    let fg = s.tools.foreground;
    let c = color(p.get("color"), fg);
    let (x, y) = (f(p, "x", 0.0).floor() as i32, f(p, "y", 0.0).floor() as i32);
    let (tol, contiguous, aa, opacity) = (f(p, "tolerance", 32.0), b(p, "contiguous", true), b(p, "antiAlias", true), f(p, "opacity", 100.0) / 100.0);
    let filled = s.edit("Paint Bucket", |doc, active| {
        let area = doc.bounds();
        let sel = doc.selection.clone();
        let (surf, _) = crate::channel_cmds::target_surface(doc, *active, p)?;
        let ok = bucket_fill(surf, area, (x, y), tol, contiguous, aa, c, opacity, sel.as_ref());
        surf.prune();
        Ok(ok)
    })?;
    Ok(json!({ "filled": filled }))
}

fn gradient(s: &mut Session, p: &Value) -> Result<Value> {
    let from = point(p, "from").ok_or_else(|| EngineError::BadParams { cmd: "paint.gradient".into(), msg: "missing from".into() })?;
    let to = point(p, "to").ok_or_else(|| EngineError::BadParams { cmd: "paint.gradient".into(), msg: "missing to".into() })?;
    let shape = match p.get("style").and_then(Value::as_str).unwrap_or("linear") {
        "radial" => GradientShape::Radial,
        "angle" => GradientShape::Angle,
        "reflected" => GradientShape::Reflected,
        "diamond" => GradientShape::Diamond,
        _ => GradientShape::Linear,
    };
    let (fg, bg) = (s.tools.foreground, s.tools.background);
    // A preset (`gradient`), explicit `stops`, or the current gradient (Gradients panel); the
    // legacy `colors` list spaces its colours evenly.
    let stops: Vec<(f32, [f32; 4])> = match crate::presets::gradients::tool_stops(s, p)? {
        Some(st) => st,
        None => {
            let colors: Vec<[f32; 4]> = p.get("colors").and_then(Value::as_array).map(|a| a.iter().map(|v| color(Some(v), fg)).collect()).unwrap_or_else(|| vec![fg, bg]);
            let n = colors.len();
            colors.into_iter().enumerate().map(|(i, c)| (if n > 1 { i as f32 / (n - 1) as f32 } else { 0.0 }, c)).collect()
        }
    };
    let reverse = b(p, "reverse", false);
    let opacity = f(p, "opacity", 100.0) / 100.0;
    let blend = p.get("mode").and_then(Value::as_str).and_then(blend_from_str).unwrap_or(photocraft_color::BlendMode::Normal);
    s.edit("Gradient", |doc, active| {
        let sel = doc.selection.clone();
        let area = sel.as_ref().map(|m| m.content_bounds()).filter(|r| !r.is_empty()).unwrap_or_else(|| doc.bounds()).intersect(&doc.bounds());
        let (surf, _) = crate::channel_cmds::target_surface(doc, *active, p)?;
        paint_gradient(surf, area, from, to, shape, &stops, reverse, opacity, blend, sel.as_ref());
        Ok(())
    })?;
    Ok(Value::Null)
}

/// Paint helper command specs.
pub fn specs() -> Vec<CommandSpec> {
    vec![
        CommandSpec {
            id: "paint.bucket",
            label: "Paint Bucket",
            menu: &[],
            shortcut: None,
            params: r##"{"x":px,"y":px,"tolerance":0..255=32,"contiguous":bool=true,"antiAlias":bool=true,"color":"#rrggbb"=foreground,"opacity":1..100=100,"target":"pixels"|"mask"|"quickMask"|{"channel":i}=Channels panel target}"##,
            enabled: crate::commands::has_paintable,
            run: bucket,
            journal: true,
        },
        CommandSpec {
            id: "paint.gradient",
            label: "Gradient",
            menu: &[],
            shortcut: None,
            params: r##"{"from":[x,y],"to":[x,y],"style":"linear|radial|angle|reflected|diamond"="linear","colors":["#rrggbb",…]? (evenly spaced),"gradient":preset name?,"stops":[[t,"#rrggbb"|"foreground"|"background"],…]?,"transparency":[[t,0..100],…]? (default: the current gradient, see gradient.presets.select),"reverse":bool=false,"opacity":1..100=100,"mode":"normal|multiply|…"="normal","target":"pixels"|"mask"|"quickMask"|{"channel":i}=Channels panel target}"##,
            enabled: crate::commands::has_paintable,
            run: gradient,
            journal: true,
        },
    ]
}

#[cfg(test)]
mod tests {
    use super::*;
    use photocraft_geom::Rect;

    fn session() -> Session {
        let mut s = Session::new();
        s.execute("file.new", json!({"width": 20, "height": 10})).unwrap();
        s
    }

    fn px(s: &Session, x: i32, y: i32) -> Vec<f32> {
        let d = s.active().unwrap();
        d.doc.layer(d.active_layer.unwrap()).unwrap().surface().unwrap().pixel(x, y)
    }

    #[test]
    fn bucket_fill_contiguous() {
        let mut s = session();
        s.edit("wall", |doc, _| {
            doc.layers[0].surface_mut().unwrap().fill_rect(Rect::new(10, 0, 11, 10), &[0.0, 0.0, 0.0, 1.0]);
            Ok(())
        })
        .unwrap();
        s.execute("paint.bucket", json!({"x": 2, "y": 2, "color": "#ff0000", "antiAlias": false})).unwrap();
        assert_eq!(px(&s, 5, 5), vec![1.0, 0.0, 0.0, 1.0]);
        assert_eq!(px(&s, 15, 5), vec![1.0, 1.0, 1.0, 1.0]);
        s.execute("paint.bucket", json!({"x": 2, "y": 2, "color": "#0000ff", "contiguous": false, "antiAlias": false, "tolerance": 0})).unwrap();
        assert_eq!(px(&s, 5, 5), vec![0.0, 0.0, 1.0, 1.0]);
    }

    #[test]
    fn gradient_tool_with_selection_and_reverse() {
        let mut s = session();
        s.execute("paint.gradient", json!({"from": [0, 0], "to": [20, 0], "colors": ["#000000", "#ffffff"]})).unwrap();
        assert!(px(&s, 0, 5)[0] < 0.05 && px(&s, 19, 5)[0] > 0.95);
        s.execute("paint.gradient", json!({"from": [0, 0], "to": [20, 0], "colors": ["#000000", "#ffffff"], "reverse": true})).unwrap();
        assert!(px(&s, 0, 5)[0] > 0.95);
        s.execute("select.rect", json!({"x": 0, "y": 0, "width": 5, "height": 10})).unwrap();
        s.execute("paint.gradient", json!({"from": [0, 0], "to": [0, 10], "colors": ["#ff0000"]})).unwrap();
        assert_eq!(px(&s, 2, 5)[..3], [1.0, 0.0, 0.0]);
        assert!(px(&s, 10, 5)[1] > 0.4, "outside the selection untouched");
        assert!(s.execute("paint.gradient", json!({"from": [0, 0]})).is_err());
    }
}
