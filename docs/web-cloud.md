# PhotoCraft on Tofu

This private adaptation retains the PhotoCraft Rust document model, command registry, codecs,
file format, egui interface and wgpu renderer. The browser selects WebGPU, with WebGL2 as a
fallback. No alternative canvas engine or document format is introduced.

Upstream baseline: `storytold/photocraft@faaa42db958fe3b49db5c0414ceec109fe2918a7`.
Private repository: `FZ2000/photocraft`. GitHub cannot make a public fork private, so this is
a private repository with upstream history and an `upstream` remote.

## Adapter boundaries

- `apps/photocraft-web/src/cloud.rs`: the browser workspace and asynchronous cloud I/O.
- `apps/photocraft-web/src/web.rs`: minimal integration with the existing editor shell.
- `apps/photocraft-cloud`: a Rust HTTP service for accounts, projects, permissions, saves,
  versions, view links, comments and presence. It never edits pixels.
- Cloud documents use existing lossless `.pcraft` serialization. Images and PSDs continue
  through the existing import/export services. Preview images use the existing compositor.
- Database objects live only in the `photocraft` schema. Session cookies are opaque,
  HttpOnly and Secure in production; only their SHA-256 digests are stored in the database.
  Supabase sign-in is verified server-side; no service role key goes to a browser.
- Tofu supplies hosting, HTTPS, deployment history, health monitoring, database provisioning,
  backups and Google sign-in. No resident queue, cron or WebSocket service is required.

## Save and collaboration contract

Uploads use 512 KiB binary chunks (100 MiB per document) with declared lengths and SHA-256.
The commit locks the project, checks the expected revision and inserts a complete version
atomically. Incomplete uploads never replace a working version. Concurrent saves return a
conflict; the browser keeps its edits and offers a separate copy. This is revision-based
collaboration, not a claim of Figma-style simultaneous object merging.

Members have view or edit access. Only an owner manages membership, trash and public view
links. Links are unguessable, stored as hashes, revocable, and expose only the latest version.
No invitation email is sent automatically. Polling supplies presence and new-version notices.
Version history is retained; the initial quota is 1 GB per owner including versions.

## Deployment

Build the editor using the existing Trunk pipeline. Package its output alongside the HTTP
service's Dockerfile. The container serves `/` and `/healthz` on `PORT`. Set `APP_ORIGIN` to the
Tofu-returned HTTPS origin. Tofu-managed `DATABASE_URL`, `SUPABASE_CA_CERT`, `SUPABASE_URL`, and
`SUPABASE_ANON_KEY` stay in its environment. PostgreSQL uses verified TLS. Startup applies the
idempotent schema migration under an advisory lock before accepting traffic.

Database allowance is a deployment prerequisite for cloud functions. On initial inspection,
the account's single database was assigned to Chat. No Chat data or settings may be changed
to free that allowance. Without a database the editor remains usable for local file editing,
and cloud configuration reports its unavailable state explicitly.

## Validation

Retain the upstream Rust suite and real-file corpora. Add API integration tests against a
disposable PostgreSQL instance for account isolation, each role, CSRF, chunk sizes, corruption,
partial uploads, race/conflict behavior, history, trash, sharing and comments. Browser checks
must cover WebGPU and WebGL2, actual editing, downloads and re-imports, keyboard navigation,
responsive layouts, recovery, network failures and multiple independent sessions. Run core
flows against the final Tofu URL; a local suite or HTTP 200 alone is not release acceptance.

Upstream remains early-alpha software. Its Photoshop compatibility and performance limitations
still apply; a web adaptation does not make every upstream feature complete.

## Reproduce the web checks

Use Rust 1.95.0, the `wasm32-unknown-unknown` target, Trunk 0.21.14, Python 3.13+,
and a disposable PostgreSQL database on loopback. The `Browser and cloud acceptance`
workflow provisions these automatically and retains screenshots and a deployment ZIP.

```sh
python -m pip install -r tests/web/requirements.txt
python -m playwright install chromium webkit
cargo build --locked -p photocraft-cloud
cargo test --locked -p photocraft-cloud
cargo run --locked -p photocraft-cloud --example fixture -- /tmp/fixture.pcraft
(cd apps/photocraft-web && NO_COLOR=true trunk build --release)
# Start the service with CLOUD_LOCAL_DEV=1, DATABASE_URL pointing to disposable PostgreSQL,
# APP_ORIGIN=http://127.0.0.1:8876, PORT=8876 and PUBLIC_DIR=dist/web.
PHOTOCRAFT_FIXTURE=/tmp/fixture.pcraft python tests/web/test_api.py
python tests/web/test_auth.py
python tests/web/test_browser.py
python packaging/web/tofu-package.py dist/photocraft-tofu.zip
```

The auth suite simulates GoTrue on loopback and verifies redirect state, single-use token
handling, verified email ownership, session cookies and server-side identity verification.
It does not substitute for a real hosted Google sign-in. Tests never add an authentication
bypass to the deployed application, and refuse to seed accounts on non-loopback services.

## Deliberate limits

- The existing egui browser renderer currently does not expose its canvas controls to screen
  readers. The page has a labelled canvas, a loading announcement, browser zoom and native
  editor keyboard commands, but a full accessible DOM editor is not claimed.
- Phone and tablet layouts are tested at emulated viewport sizes. Physical-device touch,
  stylus pressure, browser memory pressure and Home Screen installation need device evidence.
- Recovery stores the current document in IndexedDB after an idle interval. It is not a
  service-worker cache of the application and does not promise an offline first visit.
- Collaboration uses committed document revisions, member roles, comments and presence.
  Concurrent edits are preserved as conflicts/copies; it does not merge simultaneous strokes.
- Public view links allow recipients to open and download the shared document. Revoking a link
  prevents further requests but cannot erase copies recipients have already downloaded.
- The same-origin `photocraftCommand` automation bridge reuses the native command registry.
  It exposes no server credential or filesystem authority and is inaccessible cross-origin.
