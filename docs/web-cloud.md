# PhotoCraft on Tofu

This private adaptation retains the PhotoCraft Rust document model, command registry, codecs,
file format, egui interface and wgpu renderer. The browser selects WebGPU, with WebGL2 as a
fallback. No alternative canvas engine or document format is introduced.

Upstream baseline: `storytold/photocraft@47f9306fd06d5dee11acb84b108606f4c867222a` (PhotoCraft 0.3.0, 2026-10-07).
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
atomically. Incomplete uploads never replace a working version. Concurrent saves use a conservative three-way merge over the existing native manifest.
Independent layer properties can merge; simultaneous changes to the same property, conflicting
layer order/identities, raster tiles or global document settings return a conflict. The browser
keeps the local document and offers Save a copy. Unchanged, idle tabs receive committed updates
automatically; applying a remote version resets local undo history, with earlier committed work
available in version history. This is committed-revision collaboration, not stroke streaming or
a general CRDT. Previews are cleared after a merge and regenerated on the next ordinary save.

Members have view or edit access. Only an owner manages membership, trash and public view
links. Links are unguessable, stored as hashes, revocable, and expose only the latest version.
Owners can send invitation emails through Tofu-managed Supabase authentication. The email is
a sign-in email; membership is granted before delivery and delivery failures are explicit.
Confirmation requires a user POST so email-link scanners do not consume a token. Invitation
requests are limited to 20 per owner per hour and one per recipient per minute. Polling supplies
presence, current roles and committed updates every 1.5 seconds.
Version history is retained; the initial quota is 1 GB per owner including versions.

## Deployment

Build the editor using the existing Trunk pipeline. Package its output alongside the HTTP
service's Dockerfile. The container serves `/` and `/healthz` on `PORT`. Set `APP_ORIGIN` to the
Tofu-returned HTTPS origin. Tofu-managed `DATABASE_URL`, `SUPABASE_CA_CERT`, `SUPABASE_URL`, and
`SUPABASE_ANON_KEY` stay in its environment. PostgreSQL uses verified TLS. Startup applies the
idempotent schema migration under an advisory lock before accepting traffic.

The PhotoCraft managed database was provisioned on 2026-10-07 without changing Chat. Google
and email sign-in are configured by Tofu. Live account return, invitation delivery and
independent real-user collaboration require separate hosted evidence. Without a database, the
editor remains usable for local file editing and reports unavailable cloud functions clearly.

The Tofu container package caps the WASM at 26 MiB. Upstream 0.3 with HEIF and the cloud adapter
is about 24.6 MiB raw; HTTP compression reduces transfer size. The separate upstream Cloudflare
package retains its 24 MiB guard. No codec is removed to meet an unrelated hosting limit.

## Validation

Retain the upstream Rust suite and real-file corpora. Add API integration tests against a
disposable PostgreSQL instance for account isolation, each role, CSRF, chunk sizes, corruption,
partial uploads, race/conflict behavior, history, trash, sharing and comments. Browser checks
must cover WebGPU and WebGL2, actual editing, downloads and re-imports, keyboard navigation,
responsive layouts, recovery, network failures and multiple independent sessions. Run core
flows against the final Tofu URL; a local suite or HTTP 200 alone is not release acceptance.

Upstream remains early-alpha software. Its Photoshop compatibility and performance limitations
still apply; a web adaptation does not make every upstream feature complete.

## Feature coverage versus the original editor

| Area | What is reused or added | Acceptance boundary |
| --- | --- | --- |
| Canvas and tools | Original Rust/egui editor, layers, brushes, type, shapes, adjustments, filters, masks and command registry | Upstream suite plus browser editing; upstream alpha limitations remain |
| Rendering | Original wgpu WebGPU compositor, WebGL2 and CPU fallbacks | Browser edit/undo and pixel exports on each path |
| Files | Original `.pcraft`, PSD and raster codecs, including upstream HEIF support | Native/PSD/PNG browser round trips and upstream corpora |
| Workspace | New responsive home, six editable native templates, search, stars, folders, shared projects and trash | Home/template browser actions and API isolation tests |
| Account | Avatar, Google sign-in, local recovery before redirect, sign-out | Simulated provider tests; real hosted Google return needs user completion |
| Saves | Chunked cloud saves, autosave after initial save, checksums, version history and recovery | Interrupted upload, reload, corrupt input, quota and concurrent save tests |
| Collaboration | View/edit membership, presence, comments, committed-update sync and conservative merges | Two independent local accounts; same-pixel conflicts preserve a copy |
| Invitations | Tofu-managed sign-in emails, scanner-safe confirmation, delivery failure state and throttling | Simulated mail provider; real delivery requires an approved recipient |
| Sharing | Private project URLs and revocable public view/download links | Independent anonymous browser and API revocation checks |
| Figma/Canva product features | Not implemented: prototyping, component libraries, shared brand kits, asset marketplace, live cursors, arbitrary simultaneous stroke merging | These are additional products/features, not capabilities supplied by a database or by the upstream editor |

This adapter preserves the original Photoshop-style editor. The surrounding workspace uses
Figma/Canva-like account, navigation, template and sharing patterns. Menu coverage is not a
claim of complete behavioral parity with any of those products.

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
- Recovery stores the current document in IndexedDB after an idle interval. Before sign-in, all
  guest tabs are saved there and remain recoverable after authentication. It is not a
  service-worker cache of the application and does not promise an offline first visit.
- Collaboration uses committed document revisions, member roles, comments and presence.
  Independent manifest changes merge; conflicting edits are preserved as copies. It does not
  merge simultaneous strokes or display other users' live cursors.
- Public view links allow recipients to open and download the shared document. Revoking a link
  prevents further requests but cannot erase copies recipients have already downloaded.
- The same-origin `photocraftCommand` automation bridge reuses the native command registry.
  It exposes no server credential or filesystem authority and is inaccessible cross-origin.

## Starter designs and home

The light workspace home is a small Rust/egui presentation layer in `apps/photocraft-web/src/home.rs`.
It uses the same editor, native file format, GPU compositor and command system as upstream.
Six original starter designs ship as `.pcraft` documents, with editable type and shape layers;
their PNG previews are rendered by the existing PhotoCraft CLI. Documents load only when selected.
Regenerate them with `python3 packaging/web/generate-templates.py target/debug/photocraft-cli`
after building that binary. Previews and documents are ordinary static assets, copied by Trunk
and included recursively in the Tofu package, rather than added to the WASM binary.

The home collapses its sidebar below 1100 px and its gallery from six to three or two columns.
Home uses a local light palette; the editor retains the user's existing theme preference.
Template category filters, project filters, folders, trash, local recovery and cloud permissions
remain separate operations. Opening a starter creates a local document without binding it to
another user's project.
