//! A synthetic, non-personal native document for API integration tests.
fn main() -> Result<(), Box<dyn std::error::Error>> {
    let path = std::env::args().nth(1).ok_or("pass an output .pcraft path")?;
    let doc =
        photocraft_doc::Document::new("Synthetic fixture", photocraft_doc::Size::new(64, 48), photocraft_doc::ColorMode::Rgb, photocraft_doc::SampleType::U8);
    std::fs::write(path, photocraft_format::save_to_bytes(&doc, &Default::default())?)?;
    Ok(())
}
