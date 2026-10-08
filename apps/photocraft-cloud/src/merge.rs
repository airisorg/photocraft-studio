//! Conservative three-way merges over the existing native manifest and content-addressed blobs.
//! A conflicting field or layer order is never silently resolved by last-writer-wins.
use photocraft_format::{
    Manifest,
    zip::{ZipReader, ZipWriter},
};
use serde_json::Value;
use std::collections::{BTreeMap, BTreeSet};

fn value(base: &Value, ours: &Value, theirs: &Value, depth: usize) -> Option<Value> {
    if depth > 96 {
        return None;
    }
    if ours == theirs || theirs == base {
        return Some(ours.clone());
    }
    if ours == base {
        return Some(theirs.clone());
    }
    if let (Value::Object(b), Value::Object(o), Value::Object(t)) = (base, ours, theirs) {
        let keys: BTreeSet<_> = b.keys().chain(o.keys()).chain(t.keys()).collect();
        let mut out = serde_json::Map::new();
        for key in keys {
            match (b.get(key), o.get(key), t.get(key)) {
                (Some(b), Some(o), Some(t)) => {
                    out.insert(key.clone(), value(b, o, t, depth + 1)?);
                }
                (b, o, t) if o == t || t == b => {
                    if let Some(o) = o {
                        out.insert(key.clone(), o.clone());
                    }
                }
                (b, o, t) if o == b => {
                    if let Some(t) = t {
                        out.insert(key.clone(), t.clone());
                    }
                }
                _ => return None,
            }
        }
        return Some(Value::Object(out));
    }
    // ID-bearing arrays are layer/group trees. Other arrays (text runs, paths, transforms,
    // tiles) stay atomic rather than guessing their semantic ordering or alignment.
    if let (Value::Array(b), Value::Array(o), Value::Array(t)) = (base, ours, theirs) {
        fn indexed(items: &[Value]) -> Option<(Vec<u64>, BTreeMap<u64, &Value>)> {
            let mut ids = Vec::new();
            let mut map = BTreeMap::new();
            for item in items {
                let id = item.get("id")?.as_u64()?;
                if map.insert(id, item).is_some() {
                    return None;
                }
                ids.push(id);
            }
            Some((ids, map))
        }
        let (bi, bm) = indexed(b)?;
        let (oi, om) = indexed(o)?;
        let (ti, tm) = indexed(t)?;
        let order = if oi == ti || ti == bi {
            oi
        } else if oi == bi {
            ti
        } else {
            return None;
        };
        let mut out = Vec::new();
        for id in order {
            match (bm.get(&id), om.get(&id), tm.get(&id)) {
                (Some(b), Some(o), Some(t)) => out.push(value(b, o, t, depth + 1)?),
                (None, Some(o), None) => out.push((*o).clone()),
                (None, None, Some(t)) => out.push((*t).clone()),
                (None, Some(o), Some(t)) if o == t => out.push((*o).clone()),
                _ => return None,
            }
        }
        // Removing a layer that another person edited must conflict, not erase their edit.
        for (id, b) in bm {
            if !om.contains_key(&id) && tm.get(&id).is_some_and(|t| *t != b) {
                return None;
            }
            if !tm.contains_key(&id) && om.get(&id).is_some_and(|o| *o != b) {
                return None;
            }
        }
        return Some(Value::Array(out));
    }
    None
}

pub(crate) fn documents(base: &[u8], ours: &[u8], theirs: &[u8], limit: usize) -> Option<Vec<u8>> {
    // All three inputs share one decoded budget. Each native document is dropped
    // before the next is loaded; malformed historical versions fail closed too.
    let mut remaining = crate::validation::DECODED_LIMIT;
    let mut parse = |bytes| -> Option<Value> {
        let mut m = crate::validation::document(bytes, &mut remaining).ok()?;
        m.thumbnail = None;
        m.composite = None;
        // Per-tab identities can differ when a file is open twice. They are not content edits.
        m.document.id = 0;
        serde_json::to_value(m).ok()
    };
    let b = parse(base)?;
    let o = parse(ours)?;
    let t = parse(theirs)?;
    for field in ["size", "mode", "depth", "icc_profile"] {
        let bfield = b.get("document")?.get(field)?;
        let ofield = o.get("document")?.get(field)?;
        let tfield = t.get("document")?.get(field)?;
        if (ofield != bfield && t != b && o != t) || (tfield != bfield && o != b && o != t) {
            return None;
        }
    }
    let merged = value(&b, &o, &t, 0)?;
    let mut manifest: Manifest = serde_json::from_value(merged).ok()?;
    manifest.document.id = crate::validation::manifest(theirs).ok()?.document.id;
    let mut writer = ZipWriter::new();
    let manifest = serde_json::to_vec(&manifest).ok()?;
    writer.add("manifest.json", &manifest).ok()?;
    let mut total = manifest.len();
    let mut seen = BTreeSet::new();
    for bytes in [theirs, ours, base] {
        let archive = ZipReader::new(bytes).ok()?;
        for entry in &archive.entries {
            if !(entry.name.starts_with("tiles/") || entry.name.starts_with("blobs/")) || !seen.insert(entry.name.clone()) {
                continue;
            }
            let data = archive.read(entry, limit.saturating_sub(total)).ok()?;
            total = total.checked_add(data.len().checked_add(entry.name.len() * 2 + 128)?)?;
            if total > limit {
                return None;
            }
            writer.add(&entry.name, &data).ok()?;
        }
    }
    let bytes = writer.finish().ok()?;
    if bytes.len() > limit {
        return None;
    }
    // The merged references must form a valid native document, independently of
    // the per-input accounting above. No input Documents are retained here.
    let mut output_budget = crate::validation::DECODED_LIMIT;
    crate::validation::document(&bytes, &mut output_budget).ok()?;
    Some(bytes)
}

#[cfg(test)]
mod tests {
    use super::*;
    use serde_json::json;
    #[test]
    fn independent_layers_and_properties_merge() {
        let b = json!({"layers":[{"id":1,"x":0,"name":"A"},{"id":2,"x":0,"name":"B"}]});
        let mut a = b.clone();
        a["layers"][0]["x"] = json!(5);
        let mut t = b.clone();
        t["layers"][1]["name"] = json!("Changed");
        let got = value(&b, &a, &t, 0).unwrap();
        assert_eq!(got["layers"][0]["x"], 5);
        assert_eq!(got["layers"][1]["name"], "Changed");
        t = b.clone();
        t["layers"][0]["name"] = json!("Same layer, other property");
        assert!(value(&b, &a, &t, 0).is_some());
    }
    #[test]
    fn same_property_deletion_and_order_conflicts_preserve_both() {
        let b = json!([{"id":1,"x":0},{"id":2,"x":0}]);
        let mut a = b.clone();
        a[0]["x"] = json!(5);
        let mut t = b.clone();
        t[0]["x"] = json!(6);
        assert!(value(&b, &a, &t, 0).is_none());
        assert!(value(&b, &a, &json!([{"id":2,"x":0}]), 0).is_none());
        assert!(value(&b, &json!([{"id":1,"x":0},{"id":2,"x":0},{"id":3,"x":1}]), &json!([{"id":1,"x":0},{"id":2,"x":0},{"id":3,"x":2}]), 0).is_none());
    }
    #[test]
    fn corruption_and_size_fail_closed() {
        assert!(documents(b"bad", b"bad", b"bad", 10).is_none());
        let doc = photocraft_doc::Document::new("Test", photocraft_doc::Size::new(16, 16), photocraft_doc::ColorMode::Rgb, photocraft_doc::SampleType::U8);
        let b = photocraft_format::save_to_bytes(&doc, &Default::default()).unwrap();
        let merged = documents(&b, &b, &b, 1_000_000).unwrap();
        assert_eq!(photocraft_format::load_from_bytes(&merged).unwrap().name, "Test");
        assert!(documents(&b, &b, &b, 10).is_none());
    }
}
