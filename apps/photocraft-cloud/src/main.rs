#![forbid(unsafe_code)]
#![deny(clippy::unwrap_used, clippy::expect_used, clippy::panic)]

#[tokio::main]
async fn main() -> Result<(), Box<dyn std::error::Error>> {
    let app = photocraft_cloud::application().await?;
    let port = std::env::var("PORT").unwrap_or_else(|_| "8080".into()).parse::<u16>()?;
    let listener = tokio::net::TcpListener::bind((std::net::Ipv4Addr::UNSPECIFIED, port)).await?;
    println!("PhotoCraft HTTP service listening on port {port}");
    axum::serve(listener, app).await?;
    Ok(())
}
