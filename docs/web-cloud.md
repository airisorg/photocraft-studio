# PhotoCraft on Tofu

This independent adaptation retains the PhotoCraft Rust document model, command registry, codecs,
file format, egui interface and wgpu renderer. The browser prefers hardware WebGPU. Software
WebGPU adapters use the existing WebGL2 path before the canvas or document is initialized;
`?webgl` still selects that path explicitly. No alternative canvas engine or document format
is introduced. This startup policy does not replace an active renderer after a later
hardware device loss.

Imported upstream reference: `storytold/photocraft@3a3984075a1fd06d1af3e660aa376ee5368c4f73`.
Repository: [airisorg/photocraft-studio](https://github.com/airisorg/photocraft-studio), with upstream history and an `upstream` remote.
See [the fork code map](fork-code-map.md) for original and added modules and the public-release boundary.

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
The commit validates the complete archive through the original native loader outside database
locks, then locks the project, refreshes authorization, checks the revision and inserts a complete
version atomically. Invalid referenced blobs, oversized expansion and changed revisions preserve
the saved document. See [the security policy](../SECURITY.md) for cloud-specific resource bounds. Incomplete uploads never replace a working version. Concurrent saves use a conservative three-way merge over the existing native manifest.
Independent layer properties can merge; simultaneous changes to the same property, conflicting
layer order/identities, raster tiles or global document settings return a conflict. The browser
keeps the local document and offers Save a copy. Unchanged, idle tabs receive committed updates
automatically; applying a remote version resets local undo history, with earlier committed work
available in version history. Saved revisions are the durable authority. A separate bounded live path shows authenticated
cursors and supported native gesture previews; it is not a general CRDT. Previews are cleared after a merge and regenerated on the next ordinary save.

Members have view or edit access. Only an owner manages membership, trash and public view
links. Links are unguessable, stored as hashes, revocable, and expose only the latest version.
Owners can send invitation emails through Tofu-managed Supabase authentication. The email is
a sign-in email; membership is granted before delivery and delivery failures are explicit.
Confirmation requires a user POST so email-link scanners do not consume a token. Invitation
requests are limited to 20 per owner per hour and one per recipient per minute. Legacy presence polls every 1.5 seconds. Visible editors coalesce changed cursor/gesture writes at 80 ms; a successful write
also returns authorized live state and revision. Quiet editors poll every 80 ms. The adapter
keeps one GET or PUT pending per document/account generation and heartbeats unchanged active
state every 500 ms. Initial synchronization and old-server ACKs require a GET; stale-base and
role-change failures force a fresh read before further writes. Room, session, native preview and expiry bounds are documented in
[collaboration architecture](collaboration-architecture.md). These timers are not a latency guarantee.
Version history is retained; the initial quota is 1 GB per owner including versions.

Browser recovery retains **one most recently visited document** for the current account
(or guest), including clean documents opened from the cloud. A successful replacement
atomically evicts older eligible snapshots. Legacy lists are compacted to their newest
timestamp; a guest handoff is adopted into the signed-in account, while unrelated account
records remain isolated. Delayed older writes cannot evict newer activity. Recovery keys
retain document identity, so an obsolete Recover control cannot open a different document.
Recovery opens an independent local copy, without inheriting a closed document's cloud
binding. Sign-in does not redirect while other unsaved tabs would be lost: those documents
must be downloaded first. The sign-out choice still clears private browser recovery.

Browser storage warnings are independent of cloud-save state: a failed local snapshot
retries on its own timer and does not postpone cloud autosave. The warning remains visible
until the affected recovery operation succeeds. A cloud failure retains its own Retry
action when browser storage recovers.

Opening a downloaded native file, recovering a snapshot or selecting a template creates a
local document. Persisted native document IDs do not confer a cloud destination. The web
shell passes file-picker and dropped bytes to PhotoCraft's original importer, then detaches
the newly admitted document from old cloud bindings. Pending save/sync replies are checked
against their original document request before they can change bindings or local content.

## Deployment

Build the editor using the existing Trunk pipeline. Package its output alongside the HTTP
service's Dockerfile. The container serves `/` and `/healthz` on `PORT`. Set `APP_ORIGIN` to the
Tofu-returned HTTPS origin. Tofu-managed `DATABASE_URL`, `SUPABASE_CA_CERT`, `SUPABASE_URL`, and
`SUPABASE_ANON_KEY` stay in its environment. PostgreSQL uses verified TLS and unnamed
parameterized queries. Disabling SQLx's statement cache alone does not disable statement
names. Unnamed queries alone also do not establish transaction-pool compatibility: the
`tests/web/probe_transaction_pool.py` regression exposed a failure when the backend changed
between prepare and execute. Each parameterized query now runs inside an explicit short
transaction; every write commits before its successful HTTP response. Existing multi-query
transactions remain intact, and no added transaction spans an email-provider request.
The acceptance workflow runs the API suite through a fixture that rotates database backends
at idle transaction boundaries. This proves the adapter's tested protocol behavior, not the
exact pooler version or cause of the earlier hosted 503.
The HTTP editor starts immediately. Cloud routes become available only after the idempotent
schema migration commits under an advisory lock. Setup runs inside bounded cloud requests, so a serverless host cannot suspend it after an
unrelated response. Concurrent setup is serialized and migration lock waits are bounded; the
browser retries configuration while storage is unavailable. The public configuration reports
readiness for that worker only; every authenticated request, public-share lookup and logout
also initializes storage when needed. A completed configuration request on another worker
cannot establish readiness for the worker handling a save or project action. The configuration exposes
only fixed diagnostic categories, never connection strings or provider error payloads. `/healthz` distinguishes `starting`, `ready` and `disabled`
cloud storage while reporting HTTP availability. Sign-in waits briefly for readiness and
checks database readiness before consuming a one-time authentication token. Provider HTTP
verification holds no database connection; the account/session transaction begins after verification.
A fresh authorization check after project locking protects every durable project mutation.

Each worker admits at most 256 ordinary API requests, four authentication exchanges and two
commits, with separate bounded deadlines. Saturation returns 503 with Retry-After; timeout
returns 504 and requires checking saved state before retrying. Health checks remain available.
The database pool defaults to five connections; PHOTOCRAFT_DB_POOL_SIZE may select 1–32.
Increasing it requires measured database and worker-fleet capacity, not an unlimited-storage assumption.

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
| Collaboration | View/edit membership, comments, named cursors, bounded native gesture previews, committed-update sync and conservative merges | Independent local accounts, revocation races and rendered latency profiles; hosted two-user latency remains unverified |
| Invitations | Tofu-managed sign-in emails, scanner-safe confirmation, delivery failure state and throttling | Simulated mail provider; real delivery requires an approved recipient |
| Sharing | Private project URLs and revocable public view/download links | Independent anonymous browser and API revocation checks |
| Figma/Canva product features | Not implemented: prototyping, component libraries, shared brand kits, asset marketplace, live selections, arbitrary simultaneous stroke merging | These are additional products/features, not capabilities supplied by a database or by the upstream editor |

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
python packaging/web/build-release.py
# Start the service with CLOUD_LOCAL_DEV=1, DATABASE_URL pointing to disposable PostgreSQL,
# APP_ORIGIN=http://127.0.0.1:8876, PORT=8876 and PUBLIC_DIR=dist/web.
PHOTOCRAFT_FIXTURE=/tmp/fixture.pcraft python tests/web/test_api.py
python tests/web/test_live.py
python tests/web/test_live_scale.py
python tests/web/test_security_auth.py
python tests/web/test_archive_security.py
python tests/web/test_auth.py
PHOTOCRAFT_FIXTURE=/tmp/fixture.pcraft python tests/web/test_startup.py
python tests/web/test_browser.py
python tests/web/test_renderer_startup.py
python packaging/web/tofu-package.py dist/photocraft-tofu.zip
```

The auth suite simulates GoTrue on loopback and verifies redirect state, single-use token
handling, verified email ownership, session cookies and server-side identity verification.
It also renders and submits the actual confirmation form in Chromium and WebKit, with
no injected Origin header, then verifies identity, token privacy and replay rejection.
Both engines are mandatory dependencies; a missing browser must fail, not skip, the gate.
It does not substitute for inbox receipt or a real hosted Google sign-in. Tests never add an authentication
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
- Collaboration combines durable native revisions with bounded named cursors and supported
  gesture previews. Independent manifest changes merge; conflicting edits are preserved as
  copies. It does not merge arbitrary simultaneous strokes, provide collaborative undo or
  establish full Figma/Canva multiplayer parity.
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

The home collapses its sidebar below 900 px. Template and project grids choose one to six
columns from the available width, reserving space for titles and project menus. Primary buttons
and cards reuse Studio Light theme tokens; editor dialogs retain the current PhotoCraft theme.
Template category filters, project filters, folders, trash, local recovery and cloud permissions
remain separate operations. Opening a starter creates a local document without binding it to
another user's project.


## Workspace component review (2026-10-07)

The reference review covered Figma's workspace, account menu, search, filters, project menu,
list/grid views, editor and sharing dialog, plus Canva's template gallery and preview. Reference
screenshots remain in local QA output, outside this repository and deployment package.
The native PhotoCraft application was built and driven through its existing control channel:
editable template, five themes, native New Document and Image Size dialogs, brush, undo/redo,
and native save. The editor is retained instead of reproduced in a second frontend.

The web workspace now has a single persistent creation action that invokes the original New
Document dialog, a separate search row with Clear, contextual empty states, adaptive cards,
truncated titles with reserved menu space, light workspace menus, and centered cloud dialogs.
Guest onboarding is shorter; signed-in Home goes straight to recent projects. Escape dismisses
cloud dialogs without changing the document. These are presentation changes in the existing
Rust adapter; no frontend library, editor command or dependency was added.

The browser suite includes actual UI interaction for creation, template opening, recovery,
long-title search and clear, dialog dismissal, project star/trash/restore, sharing, comments and
version history. Native file, PSD and PNG round trips, renderer fallbacks, WebKit, interrupted
network requests, and independent local account collaboration remain in the same suite.
Four Rust web tests cover startup wiring, project filtering, empty states and grid sizing.

The [workspace design contract](workspace-design-contract.md) records measured references,
native typography/radius reuse, purposeful spacing, hover/focus/press states and reduced
motion. Browser cases 32–34 check rendered action geometry, native quick actions, intermediate
widths, phone content visibility and template search. Case 35 interrupts the first upload,
preserves the local document and recovery copy, retries the same project, reopens the saved
pixels, and removes projects through the actual card controls. Incomplete saves are labelled
explicitly; Retry save is primary, Trash stays available, and Open/Share require saved content.

Invitation feedback is local to the sharing window: sending, provider acceptance, delivery
failure, and granted membership are separate states. Project dialogs scope responses by
project and request generation; stale people, comments, history, links and errors cannot
replace another project's state. Membership writes serialize per project, while a newer
write may supersede an older read. Browser cases 36–38 exercise held invitation requests,
partial mail failure, delayed cross-project reads, same-project stale reads, and switching
away and back during a membership write. No real email is sent by these local fixtures.

Measured local collaboration timings and their limitations are recorded in
[collaboration performance](collaboration-performance.md); the existing protocol and remaining
product gaps are in [collaboration architecture](collaboration-architecture.md).

Native gaps must remain distinct from web regressions. The upstream scorecard documents
incomplete tool interactions, dead preferences, type coverage, file compatibility and expensive
large-document operations. Those are not fixed by workspace styling or additional database
capacity. See `docs/scorecard.md` and its dated performance baseline; no new performance
measurement or comprehensive native parity claim is made by this UI review.

### Header and dialog regression coverage

The editor header gives Share the primary position next to the account avatar. Existing
cloud documents show a quiet save status and a cloud icon for manual saving; new local
documents retain an explicit Save design action. Secondary actions use PhotoCraft's existing
Lucide icons and tooltips. Controls have 36 px surfaces, 8 px gaps and a common vertical
center; the avatar is 40 px. Long document names truncate before the actions.

`tests/web/visual_assertions.py` measures actual screenshot pixels because the egui canvas
does not expose its controls as DOM boxes. Browser tests 23–24 check control heights,
centers, gaps and clipping across five native themes and desktop/tablet/phone widths, plus
long guest titles. The original uneven header fails this check. These measurements detect
alignment regressions; they do not establish complete visual or accessibility parity.

Sharing keeps long member addresses within the window, exposes Can view / Can edit /
Remove access through the existing membership API, and separates invitation delivery from
access status. A modal input boundary prevents a drag behind a cloud window from painting
the document. Escape closes a member dropdown before closing its parent window.
Tests 25–28 cover long addresses, blocked background painting, permission changes, and
11 original native dialog types at three widths with unchanged document history on cancel.

Native dialogs retain their original fields and engine commands. A constrained scroll area
keeps their title and action footer available; New Document wraps its existing preset grid
and details on narrow screens. No editor model, file format, renderer or command was
reimplemented, and this change adds no dependency.

The saved rendering preference now controls the web GPU compositor on restart, through the
original `effective_rendering_mode` policy. Test 30 selects CPU/Automatic, checks local
persistence, restarts, verifies the actual compositor, then paints and undoes. CPU mode
refers to image composition; the egui browser interface still requires a graphics context.
Test 29 measures backing pixels and rendered controls at DPR 1/1.25/1.5/2 with matching
Chromium process and context scales, both before and after viewport resizing.

Upstream synchronization and tested deployment packaging are documented in
[upstream-updates.md](upstream-updates.md). The full acceptance gaps and release gates are
tracked in [web-verification-retrospective.md](web-verification-retrospective.md).
