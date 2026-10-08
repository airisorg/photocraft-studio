//! Accounting is exposed without changing the loader's validation or defaults.
use photocraft_doc::{ColorMode, Document, Layer, SampleType, Size};
use photocraft_format::{
    LoadOptions, load_from_bytes_with_stats, save_to_bytes,
    zip::{ZipReader, ZipWriter},
};
use std::sync::Arc;

#[test]
fn accounting_counts_materialized_tiles_and_deduplicates_blobs() {
    let mut doc = Document::new("Accounting", Size::new(64, 48), ColorMode::Rgb, SampleType::U8);
    let blob = Arc::new(vec![7; 16]);
    doc.icc_profile = Some(blob.clone());
    doc.metadata.exif = Some(blob);
    let mut layer = Layer::raster("Repeated content", doc.pixel_format());
    let surface = layer.surface_mut().unwrap();
    surface.write_pixel(0, 0, &[1., 0., 0., 1.]);
    surface.write_pixel(256, 0, &[1., 0., 0., 1.]);
    doc.layers.push(layer);
    let bytes = save_to_bytes(&doc, &Default::default()).unwrap();
    let (loaded, stats) = load_from_bytes_with_stats(&bytes, &LoadOptions::default()).unwrap();
    assert_eq!(loaded, doc);
    assert_eq!(stats.decoded_bytes, 2 * 256 * 256 * 4 + 16);
    let exact = LoadOptions { max_total_bytes: stats.decoded_bytes, ..Default::default() };
    assert!(load_from_bytes_with_stats(&bytes, &exact).is_ok());
    assert!(load_from_bytes_with_stats(&bytes, &LoadOptions { max_total_bytes: stats.decoded_bytes - 1, ..exact }).is_err());
    let archive = ZipReader::new(&bytes).unwrap();
    let mut missing = ZipWriter::new();
    for entry in &archive.entries {
        if !entry.name.starts_with("tiles/") {
            missing.add(&entry.name, &archive.read(entry, bytes.len()).unwrap()).unwrap();
        }
    }
    assert!(load_from_bytes_with_stats(&missing.finish().unwrap(), &LoadOptions::default()).is_err());
}
