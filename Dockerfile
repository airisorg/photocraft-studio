FROM rust:1.95-bookworm AS build
WORKDIR /source
COPY Cargo.toml Cargo.lock ./
COPY crates ./crates
COPY apps ./apps
COPY xtask ./xtask
COPY assets ./assets
RUN cargo build --locked --release -p photocraft-cloud

FROM debian:bookworm-slim
RUN apt-get update && apt-get install -y --no-install-recommends ca-certificates && rm -rf /var/lib/apt/lists/*
WORKDIR /app
COPY --from=build /source/target/release/photocraft-cloud /app/photocraft-cloud
COPY public /app/public
ENV PORT=8080 PUBLIC_DIR=/app/public
USER 65532:65532
EXPOSE 8080
CMD ["/app/photocraft-cloud"]
