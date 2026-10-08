//! Cloud-only native bundle limits. Desktop loading retains its own limits.
//!
//! Validation uses the real format loader, including referenced payload hashes,
//! and is run in one blocking lane before the commit takes any database locks.
use photocraft_format::{LoadOptions, Manifest, manifest::ContentM, zip::ZipReader};
use std::{
    collections::BTreeSet,
    sync::{Arc, OnceLock},
};
use tokio::sync::{OwnedSemaphorePermit, Semaphore};

pub(crate) const DECODED_LIMIT: u64 = 256 << 20;

#[derive(Clone, Copy)]
struct Limits {
    manifest: usize,
    archive: usize,
    blob: usize,
    entries: usize,
}

const CLOUD: Limits = Limits { manifest: 4 << 20, archive: 128 << 20, blob: 64 << 20, entries: 16_384 };

/// The permit travels into spawn_blocking: dropping/timing out the HTTP future
/// must not admit another large decode while its old worker is still running.
pub(crate) async fn acquire() -> Option<OwnedSemaphorePermit> {
    static LANE: OnceLock<Arc<Semaphore>> = OnceLock::new();
    let lane = LANE.get_or_init(|| Arc::new(Semaphore::new(1))).clone();
    // The HTTP commit lane admits at most two requests. Wait briefly before
    // snapshot allocation so ordinary tiny simultaneous saves can serialize.
    tokio::time::timeout(std::time::Duration::from_secs(1), lane.acquire_owned()).await.ok()?.ok()
}

fn preflight(bytes: &[u8], limits: Limits) -> Result<Manifest, String> {
    let archive = ZipReader::new(bytes).map_err(|_| "The native archive is corrupt")?;
    if archive.entries.len() > limits.entries {
        return Err("The cloud archive has too many entries".into());
    }
    let mut names = BTreeSet::new();
    let mut expanded = 0usize;
    for entry in &archive.entries {
        if !names.insert(&entry.name) {
            return Err("The native archive contains duplicate entries".into());
        }
        // Even entries unused by the current document have bounded expansion and
        // valid CRCs. In-memory reads never extract archive paths to a filesystem.
        let cap = if entry.name == "manifest.json" { limits.manifest } else { limits.blob };
        let data = archive
            .read(entry, cap.min(limits.archive.saturating_sub(expanded)))
            .map_err(|_| "The native archive is corrupt or exceeds cloud archive limits")?;
        expanded = expanded.checked_add(data.len()).ok_or("The native archive is too large")?;
        if expanded > limits.archive {
            return Err("The native archive exceeds the cloud expansion limit".into());
        }
    }
    // Preflight established the smaller manifest bound before this shared reader
    // uses its desktop default. This preserves the existing migration behavior.
    let manifest = photocraft_format::read_manifest(bytes).map_err(|_| "The native manifest is invalid")?;
    let size = manifest.document.size;
    if size.width == 0 || size.height == 0 || size.width > 16_384 || size.height > 16_384 || u64::from(size.width) * u64::from(size.height) > 64_000_000 {
        return Err("The document exceeds cloud canvas limits".into());
    }
    let mut layers = manifest.document.layers.iter().collect::<Vec<_>>();
    let mut ids = BTreeSet::new();
    while let Some(layer) = layers.pop() {
        if !ids.insert(layer.id) {
            return Err("The native document has duplicate layer identifiers".into());
        }
        if ids.len() > 4096 {
            return Err("The document exceeds the cloud layer limit (4096)".into());
        }
        if let ContentM::Group { children, .. } = &layer.content {
            layers.extend(children);
        }
    }
    Ok(manifest)
}

pub(crate) fn manifest(bytes: &[u8]) -> Result<Manifest, String> {
    preflight(bytes, CLOUD)
}

pub(crate) fn document(bytes: &[u8], remaining: &mut u64) -> Result<Manifest, String> {
    document_with(bytes, remaining, CLOUD)
}

fn document_with(bytes: &[u8], remaining: &mut u64, limits: Limits) -> Result<Manifest, String> {
    let manifest = preflight(bytes, limits)?;
    let opts = LoadOptions {
        max_manifest_bytes: limits.manifest,
        max_blob_bytes: limits.blob.min(*remaining as usize),
        max_total_bytes: *remaining,
        preserve_ids: false,
    };
    // Do not retain pixel allocations when validating the next merge input.
    let (doc, stats) = photocraft_format::load_from_bytes_with_stats(bytes, &opts)
        .map_err(|_| "The native document has missing/corrupt data or exceeds cloud decoded limits")?;
    *remaining = remaining.checked_sub(stats.decoded_bytes).ok_or("The document exceeds cloud decoded limits")?;
    drop(doc);
    Ok(manifest)
}

#[cfg(test)]
mod tests {
    use super::*;
    use photocraft_format::zip::ZipWriter;

    fn fixture() -> Vec<u8> {
        let doc = photocraft_doc::Document::new(
            "Cloud validation",
            photocraft_doc::Size::new(64, 48),
            photocraft_doc::ColorMode::Rgb,
            photocraft_doc::SampleType::U8,
        );
        photocraft_format::save_to_bytes(&doc, &Default::default()).unwrap()
    }

    fn bundle(manifest: &Manifest, payload: Option<(&str, &[u8])>) -> Vec<u8> {
        let mut zip = ZipWriter::new();
        zip.add("manifest.json", &serde_json::to_vec(manifest).unwrap()).unwrap();
        if let Some((name, data)) = payload {
            zip.add(name, data).unwrap();
        }
        zip.finish().unwrap()
    }

    #[test]
    fn native_roundtrip_and_missing_or_corrupt_references() {
        let good = fixture();
        let mut m = document(&good, &mut { DECODED_LIMIT }).unwrap();
        let hash = "0".repeat(64);
        m.document.icc_profile = Some(hash.clone());
        let missing = bundle(&m, None);
        assert!(manifest(&missing).is_ok());
        assert!(document(&missing, &mut { DECODED_LIMIT }).is_err());
        let corrupt = bundle(&m, Some((&format!("blobs/{hash}.zst"), b"not zstd")));
        assert!(document(&corrupt, &mut { DECODED_LIMIT }).is_err());
    }

    #[test]
    fn small_budgets_bound_expansion_entries_and_ambiguous_names() {
        let good = fixture();
        assert!(document_with(&good, &mut { DECODED_LIMIT }, Limits { manifest: 16, ..CLOUD }).is_err());
        assert!(document_with(&good, &mut { DECODED_LIMIT }, Limits { archive: 16, ..CLOUD }).is_err());
        assert!(document_with(&good, &mut { DECODED_LIMIT }, Limits { entries: 0, ..CLOUD }).is_err());
        let raw = ZipReader::new(&good).unwrap().read_by_name("manifest.json", CLOUD.manifest).unwrap();
        let mut zip = ZipWriter::new();
        zip.add("manifest.json", &raw).unwrap();
        zip.add("manifest.json", &raw).unwrap();
        assert!(document(&zip.finish().unwrap(), &mut { DECODED_LIMIT }).is_err());
    }

    #[test]
    fn merge_inputs_share_one_actual_decoded_budget() {
        let mut doc =
            photocraft_doc::Document::new("Budget", photocraft_doc::Size::new(64, 48), photocraft_doc::ColorMode::Rgb, photocraft_doc::SampleType::U8);
        doc.icc_profile = Some(Arc::new(vec![7; 16]));
        let bytes = photocraft_format::save_to_bytes(&doc, &Default::default()).unwrap();
        let mut remaining = 40;
        document(&bytes, &mut remaining).unwrap();
        assert_eq!(remaining, 24);
        document(&bytes, &mut remaining).unwrap();
        assert_eq!(remaining, 8);
        assert!(document(&bytes, &mut remaining).is_err());
        assert_eq!(remaining, 8);
    }
}
