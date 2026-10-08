# PhotoCraft web verification retrospective and release plan

2026-10-07. This is an open acceptance plan, not a declaration of complete Figma, Canva,
Photoshop, or native PhotoCraft parity. The web adapter must preserve the original editor.

## Why the defects escaped

The release evidence was too narrow for the claims being made. Build success, an HTTP 200,
registered commands, and a growing test count were treated as if they proved the complete
user experience. They do not. Verification must start from the user's contract and follow
it through the UI, persisted state, runtime, and deployment.

| Escaped defect | What the old check proved | Missing contract | Required regression |
|---|---|---|---|
| Uneven header buttons | Buttons could be clicked | Common height, center, gaps, typography and action hierarchy | Measure rendered pixels across themes, widths, density and long titles; review screenshots |
| CPU preference ignored on the web | Forced `?cpu` and `?webgl` modes edited and undid correctly | A saved preference must select the next launch's actual renderer | Set preference, verify persistence, restart, inspect actual renderer, edit and undo |
| Software WebGPU starts with a black workspace | Hardware WebGPU worked and the command bridge answered | The selected adapter must paint the native workspace; document CPU fallback cannot repair a lost UI device | Mandatory bundled-browser default and explicit WebGL pixel/click checks; safe startup error and recovery action; separate positive hardware evidence |
| Share window painted behind itself | Window opened and Escape dismissed it | Modal pointer and keyboard ownership | Outside drag leaves document history unchanged; closing restores canvas input |
| Member menu failed after Escape | Membership API worked; dropdown could be drawn | Nested menu closes before its parent and remains usable | Open, Escape, reopen, change role through UI; assert API write and resulting permissions |
| Dialog actions clipped | Dialog bounding box was inside the viewport | Fields and final actions must remain reachable and visible | Scroll to all fields, use real footer actions, test keyboard focus and resize with the window open |
| Text felt unlike browser applications | Editor rendered at one tested resolution | Raster scale, font weight, readability, frame pacing and actual screen conditions | Compare canvas backing size with CSS size × DPR; measure reference styles and actual interaction timing |
| Collaboration confidence too broad | Synthetic local accounts and provider simulation passed | Real identity return, email delivery and independent hosted users | Approved recipient, real callback, two isolated accounts, persistence, revoke and reconnect evidence |
| Empty project after failed first save; cold-worker Open/Trash fails | API fixtures warmed the same process through `/api/config`, then completed uploads | Any authenticated or public-share request can be the first request on a fresh hosting worker; metadata creation is not a saved document | Start a fresh process without configuration polling; restart between upload stages; inject initial-upload failure and exercise actual retry/trash controls |
| Send invitation appears inert; granted access hidden after mail failure | Provider simulation checked status and database rows | The sharing window must show pending, accepted and failed outcomes, including partial success | Hold the actual Send request; prevent duplicate submission; fail delivery after granting access; require inline feedback, refreshed members and a retained retry address |
| Previous project's members/link can appear in another sharing window | Dialogs tested one project at a time | Every asynchronous response belongs to its project and request generation | Delay/reorder project responses, switch projects, then prove old members, links and errors cannot replace the new project's state |
| A loaded workspace retains a connection-error banner | Fast local boot and anonymous hosted smoke passed | Startup must have one complete configuration/session request in flight; a failed account lookup is not a guest session | Hold both stages across polling intervals, count requests, fail each stage, then verify automatic recovery and a settled guest 401 separately |
| Retry reports a conflict after a save actually committed | A rejected chunk leaves an incomplete upload | A missing response cannot establish whether the server committed the save | Let commit succeed, discard its response, then retry unchanged and newly edited local snapshots; prove the original project and collaborator edits survive |
| Recovered copy can inherit a closed document's cloud binding | Recovery after a page reload starts with empty adapter state | Same-session recovery must also create an independent local copy | Save, close without reloading, recover, edit, wait past autosave and prove the original cloud revision/bytes stay unchanged |
| Delayed Open or project list can override a later action | Cards were opened and modified one at a time | The latest navigation/refresh owns its result; stale success and failure must be ignored | Hold and reorder real card requests, duplicate-click Open, and complete an old list after a Trash/Restore refresh |
| Email confirmation rejects a normal Continue click | HTTP tests manually supplied the correct Origin header | The delivered page policy determines the browser-generated POST Origin, cookie acceptance and redirect | Render the served form in Chromium and WebKit, click Continue without injected headers, verify identity and token privacy, reject replay and foreign/null/missing origins |
| Browser recovery accumulates old documents | Individual snapshots could be written and recovered | The requested policy is one latest-visited recovery copy, including real eviction | Visit clean A/B/A documents, reload, migrate old rows, preserve account isolation, reject stale writes and prevent multi-tab work loss on sign-in |
| Collaboration takes several seconds | Native state eventually converged and HTTP requests were fast | Trusted input must produce matching pixels on another user's visible canvas during the gesture | Calibrate input/frame clocks; reject stale or frozen pixels; measure cursors and held native strokes under declared network profiles; verify committed bytes and reload separately |
| Release-image execution was untested | Debug-service/browser suites and package identity checks passed | The exact ZIP's Dockerfile must build/start the release service, serve tested bytes and connect with verified database TLS | Fresh Linux exact-package image job, trusted API/live/lock suites and a hash-bound receipt required before promotion |
| Demo barely showed an edit | Still frames, file provenance and seven-second duration were checked | A useful recording must show legible, continuous actions and their visible result | Review complete playback, progressive motion and scene timing; keep PNG validation warnings outside the recorded interval; verify the served video decodes and pauses |
| Recording shows a cloud-startup notice | Native editing and the downloaded file passed against a static capture server | The recording fixture must also model a configured guest workspace | Use the real isolated backend and database, await actual configuration and unauthenticated account responses, verify the settled footer, then record; do not hide the notice or fabricate sign-in availability |
| Dragging an edge-clipped vector leaves a trail | Pixels, Undo/Redo and the native file matched after release | Every held preview must erase the previous painted region, including vector pixels revealed by moving inward | Apply each native damage-region composite to the previous frame and compare all pixels with a full composition at successive and skipped offsets on all four canvas edges |
| Move shows the previously selected layer's outline | Auto-Select chose the new layer and the final translation succeeded | Snapping bounds and targets must belong to the layer selected by this pointer press | Select A, press B with native Auto-Select, then check the held outline, bounds and excluded snapping targets |
| A clipped vector's box crosses inside its revealed shape | The outline followed the picked layer's cached pixels | Native transform bounds must include its full fill/stroke geometry, including pixels beyond the canvas edge | Compare full geometry and independently rendered pixels on all four edges; preserve raster/interior/mask behavior and verify the held box in the browser |
| Collaborator preview briefly snaps back on release | Held-preview and final saved pixels separately matched | Authorized preview pixels must remain visible while a newer native archive downloads | Delay actual receiver version bytes, sample rendered pixels throughout release and installation, then test cancellation, revocation, local edits and original lease expiry separately |
| Move briefly returns to old pixels at pointer release | Paint handoff tests kept a finished stroke visible until its saved version arrived | Move with Auto-Select has a distinct ordering: view revision, held native transform, release and durable save must be tested together | Hold an auto-selected Move across an autosave interval, inspect the first saved archive, delay canonical installation and require uninterrupted remote release frames plus native-file convergence |

The test for the saved CPU preference failed before the adapter fix: it observed GPU still
active after restart. The original header failed the pixel geometry check. These are useful
regressions because they distinguish the broken behavior from the intended behavior.

The 2026-10-08 demo review exposed the motion gaps above. The first GIF had only four
distinct states, moved its artwork farther out of view and included export notices.
A continuous browser recording then exposed defects that final-state equality had missed.
For these transitions, a passing saved file is necessary but insufficient: keep the pointer
held, inspect intermediate rendered frames, and follow the preview through the save handoff.
Presentation checks use native media controls and opt-in loading; a playable MP4 alone does
not establish editing correctness or internet collaboration latency.

A later full-speed Move recording exposed a separate seven-frame recoil after the paint
handoff checks passed. Auto-Select advanced the native view revision while the pixel
document was unchanged. At release, the cloud adapter saw raw pointer-up before the
canvas processed its native Move commit, so autosave uploaded the old pixels. The
receiver correctly installed that intermediate archive; the native End event was not
the cause. A read-only predicate now exposes the existing pending native drag, and
automatic save waits for both pointer input and that lifecycle to be idle. The same
predicate aligns the three existing automatic sync/install guards defensively.

The rendered Move regression failed on WASM `189bb7db` in 6.740 s and passed on
`38853028d21943a51f2488d7300daa7166134c72b1d020962dcaa04e830216d0`
in 9.676 s, with backend `73eada2a` unchanged. It checks the first installed saved
archive, intermediate moved pixels, reload and Undo/Redo. The queued-sync and
same-document pending-save-acknowledgment journeys also passed the new artifact
(8.113 s and 9.068 s), but already passed the old one: those are positive regression
coverage, not additional reproduced data-loss fixes. The local `release-r6-focused3`
receipt records all three passes and owned worker/database cleanup; the failed
`move-release-old-red-r3` receipt is retained separately. These are test durations,
not collaboration latency measurements. Broader browser, recording and hosted
acceptance remain separate gates; this focused evidence does not establish their results.

Full-shape bounds inspection also introduced a route from cached malformed metadata
to the original vector compiler. Targeted tests reproduced excessive tiny-dash work
and a curved-stroke integer overflow. Admission now bounds compilation work, the
original rasterizer uses saturating pixel endpoints, and the inspector falls back to
the existing cache for extreme bounds. Ordinary stroke bounds retain the original
compiler's coarse extents, including nondefault RGB under zero alpha; tightening
them to mathematical fill bounds would discard existing native cache semantics.

The security-release browser run caught the software WebGPU defect before deployment.
Bundled Chromium selected a software adapter, lost its device, and stayed black while
native inspection still answered. Hardware Chrome and explicit WebGL painted correctly.
The adapter now selects the existing WebGL path for software WebGPU before canvas binding.
`test_renderer_startup.py` checks actual workspace pixels and Create-design input, including
after the safe recovery link. Its mandatory cases cover bundled default, explicit WebGL and
escaped initialization errors; hardware coverage is separately reported when available.
This does not establish recovery from device loss during an already-open editing session.

The final full run passed 47 journeys before the expired-session fixture exposed another
readiness assumption: the command bridge was ready while asynchronous project Open still
reported no document. Expiring the session at that point tested interruption during Open,
not preservation of an already-open unsaved document. The affected setups now wait, with
a deadline, for the expected native dimensions, exact layers and active selection before
injecting expiry or merge changes. Focused journeys 48–50 pass with every original
unsaved-work, account, revision and error assertion retained. The complete run and the
initial failure are recorded separately; an extra fixed sleep is not a readiness contract.

## Inventory: scope is larger than a menu count

The Version 0 incident exposed an environment-model gap: one warm local server does not
represent independently starting hosted workers. The user's open editor displayed
“Cloud storage is starting” while its project card showed 0 × 0 and Version 0. Initial
project metadata is committed before upload completion; interrupted uploads must remain
explicitly incomplete and removable. A background browser-recovery success must not erase
a cloud failure, and only a positive committed cloud revision can establish cloud-saved
state. Preserve users' open documents while repairing this path; never reload them as a
diagnostic shortcut.

The regression scope now includes the first request to a fresh worker, a worker restart
between upload stages, a failed initial upload, retry using the same project ID, reopening
committed bytes, and owner Trash/Restore for both complete and incomplete projects.

The server fix reuses `ready_db` at the three database entry boundaries: authenticated
account lookup, anonymous share lookup, and logout. Invalid or missing session cookies
still fail before database initialization; initialization does not grant authorization.
The same cold-worker tests produced ten failed assertions against the old binary and
passed after the fix. `tests/web/test_startup.py::ColdWorkerReadiness` deliberately avoids
HTTP health/configuration probes that could warm the worker. API cases 35–36 cover failed
first-save retry and owner removal of an incomplete reservation. Browser case 35 injects
an upload failure and exercises the actual card controls and preserved local document.
These are regression gates for this incident, not a promise that future failures cannot
occur or that all hosted collaboration journeys have been accepted.

The new browser journey exposed a second defect before this release: egui's selectable
`Label` adds click handling even when supplied `Sense::hover()`. An incomplete card title
therefore still attempted a cloud open. The adapter now explicitly gates the resulting
click on a completed revision; the test clicks both preview and title and rejects an open
request. Widget configuration alone is not evidence of the resulting interaction behavior.

The sharing regression also reproduced a delayed response from project A replacing project
B's members and exposing A's view link. Dialog state now carries both project identity and
a per-channel request generation. The invitation result and input clearing follow the same
rule. Membership writes serialize per project, including a switch away and back; a late
write triggers a fresh read for the current project instead of applying obsolete feedback.
Visible pending/error states also need geometry checks: feedback initially pushed Revoke
below the desktop window even though scrolling technically kept it reachable. The window
now budgets for measured wrapped feedback and a bounded spinner row, retaining scrolling
for small viewports and long member lists.

The current menu source has 627 placements and 626 distinct command IDs. The tool enum has
45 tools. The upstream scorecards contain 150 individually described acceptance items.
The source hides 61 unimplemented preferences. The generated scorecard reports 60 unread
settings using its documented heuristic lower bound; `performance.effectCacheMb` accounts
for the difference. `cargo xtask scorecard --check` passes. These are different measures,
not proof that every other setting has correct end-to-end behavior.

The full menu and tool inventory is retained in local QA output as
`native-menu-inventory.csv` and `feature-inventory.json`. The sources remain
`crates/ui-egui/src/menu_catalog.rs`, `crates/ui-egui/src/state.rs`,
`crates/engine/src/prefs.rs` and `scorecard/*.toml`. A registered command is not proof of
complete behavior or browser suitability. Do not replace the native scorecards with a
blanket "100% supported" statement.

| Surface | Inventory to audit | Important states |
|---|---|---|
| Workspace | Home, templates, all projects, starred, shared, trash, search, folders, cards, project menus, details, account menu | Guest/owner/editor/viewer, empty/loading/error, long titles, many items, narrow/wide screens |
| Native menus | Every distinct File, Edit, Image, Layer, Type, Select, Filter, View, Window and Help command | Enabled/disabled, required selection/layer type, dialog vs direct execution, browser service availability |
| Tools | All 45 entries in `Tool::ALL`, flyouts and tool options | Down/move/up/cancel, modifiers, stylus/touch, lock/mask/selection, undo/redo, preview vs final pixels |
| Layers and panels | Layer tree, channels, paths, properties, masks, adjustments, effects, type, colors, history and docks | Empty/multiple/long items, scrolling, reorder, collapse, active selection, keyboard operation |
| Native windows | Every `DialogKind` and each specialized command form, preferences section, export/import form | Open/close/cancel/confirm, nested popup, invalid input, viewport resize, reachable footer, focus restoration |
| Files and recovery | Native, PSD/PSB and each advertised image/preset format; drag/drop; local downloads; recovery | Round trip, malformed/large input, unsupported features reported, cancellation, reload and interrupted storage |
| Display and compute | WebGPU, WebGL2, CPU image rendering; density and UI scale; color/depth; saved performance settings | Real backend, fallback, restart, device/context loss, high density, browser zoom, bounded memory and long operations |

## Sharing and collaboration contract

Each row needs a UI journey and server authorization evidence; API coverage alone is not enough.

| Capability | Current implementation | Local regression entry points | Remaining acceptance boundary |
|---|---|---|---|
| Google identity and sessions | Tofu-managed sign-in, verified identity, avatar, logout | Browser 17/18/39/44/48–50; simulated-provider auth suite | Real hosted provider callback and same-account reauthentication; local expiration/pending-save preservation is covered |
| Initial save and autosave | Native `.pcraft`, checksums, revision checks, chunked saves | Browser 7/35/40/46/47; API 8–14/35; cold-worker suite | Hosted save/reload and account changes during requests |
| Email invitation | Owner grants view/edit access and requests a sign-in email | Browser 36/38; invitation provider tests; `AuthContract.test_browser_email_confirmation_submits_real_same_origin_form` in Chromium/WebKit | Exact approved recipient, inbox receipt and actual hosted acceptance; real local browser form now crosses the simulated provider boundary |
| Membership | Owner changes roles and removes access | Browser 25/28/37; API 6/7/27/34 | Real second-user stale-tab and reconnect behavior after removal |
| Shared projects | Matching signed-in email sees authorized projects | Browser 16; API 6/7 | New invitee, wrong real account, expired session and removed member |
| Public view link | Anonymous view/download, rotation, revocation | Browser 8/22/45; API 15/22 | Hosted separate-browser flow; downloaded copies cannot be recalled |
| Comments | Authorized comments and resolution | Browser 22/37; API 18 | Duplicate pending submission, partial-success feedback and two-user UI receipt |
| Presence and sync | Names, 80 ms authorized live-state reads and committed native checkpoints | Browser 16/47; API 19; live API suite | Hosted independent accounts, reconnect, stale presence and remote-paint latency |
| Concurrent changes | Conservative manifest merge; conflicts preserve a copy | Browser 13/16/40; API 13/14/32/33 | Complete delete/edit matrix, hosted interruption and recovery |
| Version history | Saved native revisions and opening earlier versions | Browser 22 opens history; API 8 checks versions | Actual restore UI, resulting pixels/layers, undo contract and stale collaborator |
| Sign-out and local work | Download/recovery protection and account-scoped eviction | Browser 6/17/41/44/46/47; API logout | Every hosted sign-out choice and account transition with pending writes |
| Live cursors / native previews | Authenticated cursors and one remote Brush/Pencil/Eraser/Move preview using the original engine; separate from document/history | `test_live.py`, `test_live_browser.py`, native collaboration tests, calibrated `benchmark_live_collaboration.py` | Hosted two-account pixel timing, physical-device GPU behavior, larger-room capacity and unsupported gestures |
| Simultaneous same-object stroke merging | Conservative saved-document conflict handling; local gestures take precedence over a remote preview | Existing merge tests preserve conflicting copies | Shared undo and unrestricted concurrent painting remain explicit product gaps |

Numbers refer to `tests/web/test_browser.py` and `tests/web/test_api.py`. The table maps
contracts to tests; only an exact-artifact passing run establishes the corresponding local
evidence. Synthetic accounts and provider fixtures do not establish real two-person or
inbox delivery, and one successful interaction does not cover all states in its row.

The fast adapter changed autosave from 3,500 ms to 150 ms after pointer release. Browser
case 41 had prepared unsaved recovery by waiting 2,300 ms before the old deadline. Its first
run then correctly observed an automatic save, invalidating the fixture's revision-1
assumption. The fixture now explicitly fails only the original project's upload while
preparing recovery, proves the failed attempt and unchanged original checksum, and removes
the fault before saving the recovered copy. All original pixel, new-project and
closed-original protection assertions remain. Preserve the initial failure and focused
passing rerun; a test's timing assumption must not become a product constraint.

## Ordered execution plan

1. **Freeze claims and record the exact revision.** Every report names source commit, WASM
   hash, test environment, browser, viewport/DPR, identity setup and serving deployment.
   Label evidence as local, simulated-provider, hosted, or physical-device. CI pending or
   blocked is not green. Keep old failures alongside the succeeding regression.
2. **Complete the contract ledger.** Create one row per command, tool, window and cloud
   capability from the inventories above. Required fields: source owner, expected result,
   browser adaptation, visible states, persistence behavior, error behavior, test IDs,
   screenshot/evidence and status. Allowed statuses: verified, partial, unsupported,
   untested, blocked. Unclassified items fail the inventory gate; untested never means pass.
3. **Close current defects first.** Header geometry/hierarchy, long content, modal isolation,
   nested dropdown Escape, reachable native forms, and persisted renderer policy. Each fix
   needs a test that fails before the fix and passes afterwards. Reuse original widgets,
   preferences, commands and rendering policy.
4. **Audit user journeys by role.** Guest create/open/edit/export/recover; owner save/share/
   invite/manage/restore; editor receive/edit/conflict; viewer open/copy/download. Include
   new/expired/wrong identity, network loss, permission revocation and storage failure.
   Use UI input for the transitions under test; protocol commands may prepare fixtures but
   must not bypass the control whose behavior the test claims to verify.
5. **Audit every component family visually.** Extract computed reference values from the
   visible Figma/Canva UI: actual label styles, control bounds, padding, radius and shadow.
   Translate into existing egui tokens/widgets; do not copy proprietary assets or add a
   second editor. Review normal/hover/focus/disabled/loading/error/selected/open states.
   Check baseline, contrast, truncation, hit area and complete action visibility. Screenshot
   bounds and golden diffs supplement human visual review; neither replaces it.
6. **Verify displays and responsiveness.** Desktop, tablet, phone portrait/landscape,
   ultrawide, DPR 1/1.25/1.5/2, browser zoom, persisted UI scale and resizing open dialogs.
   Compare CSS bounds with backing pixels; test the real target screen. Include keyboard
   focus, screen-reader limitations and actual touch/stylus devices. Do not claim device
   acceptance from emulated viewport sizes.
7. **Verify local computing behavior.** Automatic/GPU/CPU choices must survive restart and
   report the actual backend. The CPU image path still uses browser graphics for the UI.
   Profile drawing, pan/zoom, layer edits, imports, exports and filters with representative
   documents and record p50/p95/worst frame gaps and memory. The current WASM job path runs
   heavy jobs on the calling thread (`crates/engine/src/jobs.rs`); worker isolation is an
   open performance project, not a capability supplied merely by enabling WebGL.
8. **Verify files and engine invariants.** Run applicable native unit/integration suites,
   panic tests and real-file corpora for any engine/codec/compositor changes. Preserve
   layers, text, masks, color/depth, history and cancellation. Registering an action or
   opening a dialog does not count as validating its resulting document.
9. **Release one tested artifact.** Finish builds before browser tests; do not overlap Trunk
   builds against one output directory. Run required suites on the final source. Upload
   that artifact to the existing Tofu project, inspect scan coverage, wait for its serving
   deployment, then perform a deliberately scoped hosted journey. Match the served WASM
   fingerprint. Preserve users' open work; do not reload their editing tabs.
10. **Close the ledger only with evidence.** No known data-loss, authorization, broken-control,
    unreachable-action or deployment mismatch defect may remain in the advertised scope.
    Every mandatory journey must pass on its required environment. Unsupported product
    features and unverified real-world states remain explicit release limitations. No
    finite suite can establish the absence of every unknown defect; the actionable target
    is zero unclassified items and zero unresolved defects in the declared release scope.

## Immediate blockers and next checkpoints

- Dropdown, display-density, saved-renderer and phone New Document regressions passed
  in the earlier released 31-case local browser suite. Native forms cover 33 viewport states and
  cloud windows cover 20; this does not establish every field or physical-device journey.
- On 2026-10-08, screenshot review confirmed clipped internal Preferences controls on
  phones despite an outer window that fits the viewport and a passing intermediate
  61-check browser run. Focused case 51 then failed on that old artifact in 7.102 seconds.
  The combined native build now passes focused cases 51–52, including actual compact
  section-menu clicks, pending-copy preservation and Cancel. The first selector helper
  failure is retained: a 29px workspace-height assumption excluded the actual 24px native
  control; the corrected oracle still checks the complete frame and painted menu captions.
  The exact combined build also passed four renderer-startup, 52 browser-journey and seven
  live cases; this still does not establish every native field or physical-device workflow.
  See the
  [combined build and scoped evidence](collaboration-performance.md#combined-browser-build-and-focused-profiles).
  Outer bounds or the presence of scrolling do not prove that every field and action is reachable.
- Keep the explicit hidden-preference list distinct from the scorecard's heuristic read count.
- Real invitation delivery needs the exact approved email address. Local synthetic accounts
  must never be inserted into the hosted database.
- Earlier GitHub Actions jobs were blocked by the account's billing/spending restriction.
  The 2026-10-08 jobs executed: the browser transaction-pool fixture failed and the
  FreeBSD host exhausted disk space. Fixture transport and disposable-runner disk
  changes require fresh CI; local passes remain separately reported.
- Bounded named live cursors and native Brush/Pencil/Eraser/Move previews are implemented;
  their [contract and measured scope](collaboration-architecture.md) remain explicit.
  Live selections, unrestricted simultaneous strokes, previews for every command type,
  full native behavior parity, accessible DOM controls, physical-device acceptance and
  worker offload remain open work.
- The candidate separates recovery warnings/retry timing from cloud-save eligibility.
  Its quota-failure journey must prove that cloud revisions still advance automatically,
  and that restoring local recovery cannot dismiss an unrelated cloud-save failure.
- Local browser cases 48–50 now verify explicit same-account reauthentication, wrong-account
  refusal and deferred committed-save acknowledgment with native open work preserved.
  Their login provider is simulated; a real hosted renewal remains an acceptance boundary.
- Comment posting still needs a pending-submission guard and separate feedback for a
  successful POST followed by a failed refresh, without inviting duplicate submissions.

## Earlier release evidence on 2026-10-07

Tofu deployment `dpl_FnLvDT6zhYaFW3j11qG9tJugTF7S` serves source
`43a839fedc9d0561aed859765d972678e94127f0`, including upstream
`3a3984075a1fd06d1af3e660aa376ee5368c4f73`. The hosted guest journey created four
native layers, checked exact pixels in a 960 × 640 PNG export, reimported it, passed the
header geometry assertion and reported no page errors. The downloaded WASM SHA-256 was
`8821000905941dd69b3cad5107e56ced8367f95bff362615aae8ec54cfc2771c`, matching the
tested package. A separate existing signed-in browser session displayed the new workspace;
this is not a fresh OAuth callback, email-delivery or independent-user collaboration test.

Local final-source checks: 3,487 native tests passed, 25 ignored; the opt-in corpus run
passed 1,698 with 14 ignored (overlapping unit tests, not an additive total); 31 browser,
34 API, 11 simulated-auth, one startup and six release-pipeline tests passed. Strict
native/WASM lint, layers, WASM, scorecard, explicit adversarial-command test and workflow
lint also passed. GitHub-hosted CI remains blocked by the account restriction.

One diagnostic 6,000 × 4,000 document run recorded a 1,277 ms main-thread long task and
1,266.6 ms worst frame gap for the original Box Blur at radius 20, despite a 16.7 ms p95
frame gap. This exposes a worker-offload requirement; one sample is neither a benchmark
distribution nor a passed performance budget. Preserve the native engine while moving
heavy jobs behind a worker boundary with cancellation and document-revision validation.

## Recovery, sharing and spacing candidate validation

The subsequent recovery/sharing candidate passed all 38 browser scenarios in 392.096 seconds,
36 API tests, 12 simulated-auth/provider tests, and four cold-worker tests. The browser run
used WASM SHA-256 `b620764b5530e51341b1e9851dfbe0899b3e2b677395e1c2699be43d49dffdd6`.
Strict WASM lint, formatting, four native web tests, seven cloud unit tests, cloud lint,
29-crate dependency-layer validation and six release-pipeline tests also passed during this
change. The original engine was not changed or replaced; the earlier large native/corpus
totals above are historical evidence and were not rerun for this adapter-only candidate.

An additional 390×844 local sharing journey checked pending/error feedback, a four-member
list, the real link controls and successful revocation, with unchanged native document state
and no browser errors. All invitation requests in these tests were intercepted or sent to a
loopback provider fixture; they do not prove real inbox delivery. The separate bounded
[collaboration measurements](collaboration-performance.md) are local observations, not
hosted capacity or Figma/Canva parity. Hosted release evidence must identify the serving
deployment and matching artifact after publication.

That candidate was published as source `9a50d8840269c9daf2ad8595985a11fef55f6127`,
Tofu deployment `dpl_D6UiLew2ewyLCu4qkRcRPdd6G7qg`. The scoped hosted guest journey
matched the WASM hash above and passed desktop/mobile geometry, native four-layer editing,
exact 960 × 640 export pixels and reimport, with no page errors and the actual WebGPU
backend. A subsequent ordinary signed-in startup exposed duplicate config/session/project
requests: one project-list request succeeded and another returned 503, leaving a warning
over the loaded workspace. The guest smoke did not cover this authenticated path.

Browser case 39 reproduced the startup race on that released artifact: holding the initial
configuration response for 2.2 seconds caused three simultaneous configuration requests.
The adapter now guards the entire configuration→session pipeline until its dedicated
success/failure result, and treats only `/api/me` HTTP 401 as a guest response. Connection
failure remains visible and retriable. Clearing a recovered connection warning must not
erase a newer unrelated failure. The backend also logs only a fixed SQL error category and
validated five-character SQLSTATE, never SQL, values, credentials or an error source chain.
This enables diagnosis without widening exposure of request data.

The duplicate request race explains the stale warning but does not establish the cause of
the database 503. A separate local protocol fault fixture reproduces SQLSTATE 26000 when
a transaction pool switches backends between SQLx's prepare and execute phases, even with
statement caching disabled. The inspected released Supavisor implementation passes unnamed
statements through and releases a backend when its transaction is idle. See its
[unnamed-statement handling](https://github.com/supabase/supavisor/blob/v2.9.13/lib/supavisor/protocol/prepared_statements.ex#L79-L109)
and [idle completion handling](https://github.com/supabase/supavisor/blob/v2.9.13/lib/supavisor/db_handler.ex#L380-L400).
The adapter now pins each parameterized operation inside an explicit short transaction,
with awaited commits and no transaction held across email delivery. All 38 API scenarios
passed through the corrected rotating-backend fixture, recording 2,262 idle backend
switches and no SQLSTATE errors. The fixture is a protocol fault model, not Supavisor
itself; this confirms the repaired compatibility boundary without retrospectively proving
the exact cause or pooler version behind the earlier hosted failure. This regression is
now included in the acceptance workflow.

The next adapter candidate adds explicit regressions for missing commit acknowledgments,
same-session recovery/template identity, superseded project navigation and list responses,
and the one-entry browser-recovery policy. Case 40 compares persisted native document bytes
and exported pixels after Retry, including an independently committed collaborator change;
request success alone is insufficient. A merged save legitimately completes through the
document-sync path, so the test observes the saved document rather than requiring an
unrelated project-list refresh. Cases 41–43 hold actual requests and assert that older
responses cannot change the new document or undo a visible Trash/Restore action.

Case 44 checks physical IndexedDB contents, not just the number of visible rows. Clean
A/B/A visits leave only the last visited document, legacy rows are compacted, and unrelated
account records remain isolated. An older debounced write cannot replace newer activity in
another tab. Sign-in protects other unsaved tabs that one recovery slot cannot preserve;
sign-out revokes existing write permits and grants a fresh permit for subsequent guest
work. Recovery copies remain separate from cloud projects and version history.

Final review also found that successful connection refreshes could replay the initial
share/project URL. Startup navigation must dispatch once when its prerequisites are ready,
while initial connection failures remain retriable. Case 45 records the document-open
requests and native tabs across refreshes rather than merely checking that the first open
worked. Release packaging now also treats untracked source as dirty: a ZIP that includes
such a file cannot truthfully claim to represent an unchanged commit.

Two follow-up failure journeys were reproduced before the candidate's final build. A
drafts-only IndexedDB quota fault retried seven times while the cloud document remained at
revision 1 despite preserved native edits. The fix separates both local warning state and
retry timing from cloud autosave; changing only the error flag would leave the shared
activity clock starving autosave. Case 46 also combines a local-storage failure with a
cloud failure and checks that recovering one does not hide the other.
Visual review also rejected the initial 17 px recovery action despite the warning being
visible. It now uses a normal native button with a 28 px minimum height; case 46 measures
the rendered action on desktop and phone. Visibility alone is not a sufficient control test.

Actual File > Open and browser drag-and-drop reproduced the closed-document identity bug
outside recovery/templates: editing an imported `.pcraft` advanced its old cloud project's
revision and changed its checksum. The browser now routes its existing file inbox through
the original `open_bytes`/`open_failed` functions and detaches cloud state only when a new
document is admitted. Brush/preset imports retain their original behavior. Save/sync
responses carry a document request identity, invalidated when that document closes or is
replaced by a local import. The existing single-save pipeline remains serialized; an
unrelated template result cannot release it. Case 47 holds creation, committed-save, sync
and error responses across close/reopen and compares the original project and local copy.

The final local release candidate passed all 47 browser scenarios in 746.378 seconds.
The runtime and test-file hashes were identical before and after the run: WASM
`40b153b034d98c3d3b881968e435cfe58cbcdc45fdf18db7e9890e5474459b26`, and local service
`7ab7cd6b407a0d5098c2181926b80bc590081e8f9f83eec7086f584737d551ea`.
The evidence includes 329 screenshots and 46 workspace state measurements at widths
390, 831 and 1440 with DPR 1 and 2. The new recovery action measures 28 px high on
desktop and phone. Chromium and WebKit recovery checks inspect actual IndexedDB bytes;
WebKit automation is not physical Safari-device acceptance. Seven release-provenance
tests also passed, including untracked-source dirty detection. These are local results;
the deployment and real-account journeys require their own evidence.

## Continuous upstream updates

See [upstream updates](upstream-updates.md). The scheduled workflow preserves the original
Git history, merges upstream into a candidate, calls the existing native and web suites at
that exact commit, and requires an exact-package container receipt before promoting both
main and a prebuilt deployment branch.
Conflicts, unavailable CI, a newer main revision, or a mismatched artifact prevent promotion.
The six-hour schedule is a polling interval, not an instantaneous update promise. GitHub
may delay scheduled jobs. Tofu GitHub authorization and Actions billing must be working.

### Close the package-to-runtime verification gap — 2026-10-08

The earlier web job ran the debug service with `CLOUD_LOCAL_DEV=1` and then packaged source
plus tested WASM. It did not execute the ZIP's Linux release image or prove the image's
TLS trust/configuration path. A passed ZIP checksum or a passing macOS service cannot
replace that deployment transition. This was missing coverage, not evidence that the
container had already failed.

The new `container-package` job follows acceptance on a fresh Linux runner. Its helper and
four contract suites come from the trusted PR base/caller revision, not Python code taken
from the candidate ZIP. The candidate Dockerfile builds its locked Rust release inside the
image. An owned local PostgreSQL cluster, temporary CA and UUID database exercise TLS
`verify-full` without `CLOUD_LOCAL_DEV` or provider credentials. After real schema readiness,
the gate checks served index/WASM bytes and runs `test_api.py`, `test_live.py`,
`test_live_scale.py` and `test_live_handoff.py` against the image. Browser/auth/archive suites and paint measurements
remain separate; this job does not claim a second full browser run or capacity test.

Before the write-permission promotion command, the trusted verifier must accept the
receipt's candidate/package/browser/fixture and QA-script hashes, image identity, Linux
execution, TLS setting, four successful suites and complete owned-resource cleanup.
Missing or mismatched evidence fails closed. Logs and the receipt are retained even on
failure. Later Tofu builds can still differ through base tags/system packages, so this
does not prove bit-for-bit container reproducibility or hosted readiness.

The first introduction needs a reviewed trusted-branch bootstrap because an older PR
base lacks the new helper. Keep the QA checkout trusted, then run a fresh candidate after
bootstrap; do not substitute candidate test code or a fabricated receipt. The actual Linux
Docker/TLS execution remains **unverified**. The historical billing hold has cleared;
the `f707cdce` run started, but its container job was skipped after browser acceptance
failed. A fresh run of this candidate must execute the container gate. Mocked gate/cleanup
tests verify orchestration contracts only.
No container pass or promotion is established by the existing local browser/load evidence.
See [the exact gate and activation boundary](upstream-updates.md#exact-package-container-gate).

## Display-test correction

Chromium's context-only `device_scale_factor` emulation changed `devicePixelRatio` without
changing ResizeObserver's `devicePixelContentBoxSize`. PhotoCraft correctly relies on that
physical box through eframe. The test now launches Chromium with the matching process scale
as well as its context scale. DPR 1/1.25/1.5/2 canvas backing dimensions and rendered header
geometry pass. No renderer workaround was added for a test-emulation artifact.

## Email confirmation incident: verify transitions, not isolated components

The owner reported `/auth/confirm` rejecting a normal button click after the earlier
release had passed 50 editor/browser cases and 12 auth cases. Two independent source
audits and both real browser engines reproduced the cause: `no-referrer` in the page
and middleware made the form POST send `Origin: null`. The strict origin guard correctly
rejected it before contacting the provider. The old auth test supplied `Origin` itself,
so it could not exercise the delivered page policy. More tests of the same isolated
contract would not have caught this missing transition.

The confirmation page now uses `strict-origin` in its meta tag and both header writers.
It retains the real request origin while withholding the path and token query from
`Referer`; the external Tofu credit has `rel="noreferrer"`. Other pages retain
`no-referrer`, and the exact-origin guard remains unchanged.

The mandatory `python tests/web/test_auth.py` command now includes Chromium and WebKit
GET → reload → real Continue click → redirect → browser-accepted session → identity read.
GET never redeems the token; replay fails; missing/null/foreign origins are denied.
Missing browser dependencies fail this gate rather than skipping it. The existing
`web-cloud.yml` command already runs before browser packaging/promotion, and manual
releases must run the same command on the frozen service alongside API, startup and
browser journeys. Preserve before/after evidence, and match the served confirmation
policy after deployment. This remains a simulated provider test, not proof of delivered
email or Google consent.

Before marking any journey verified, classify every transition: UI input, navigation,
provider return, identity, authorization, persisted state, reload and visible feedback.
A fixture may prepare a preceding state, but cannot replace the transition under test.
Use held responses and failure injection for races; reserve actual provider/inbox and
independent-user hosted flows for explicit live acceptance. The contract table above
keeps those boundaries visible instead of converting a total test count into parity.

## Inspect generated release artifacts separately

The final source scan was clean, but a byte-level deployment ZIP inspection found
748 operator-home-prefix matches in its optimized WASM. Release stripping removes
debug information, not every compiler-generated panic or `file!()` location.
Source-only privacy checks therefore missed the distribution boundary. The release
build now uses [standard compiler remapping](https://doc.rust-lang.org/rustc/remap-source-paths.html),
and Tofu packaging rejects retained home paths before opening the archive. Keep
failed artifacts and receipts, rebuild rather than patch binary strings, and run
browser/paint acceptance against the replacement artifact's exact hash. A clean
source tree or earlier binary's passing tests cannot stand in for this check.


## Exchange transport verification — 2026-10-08

The first exchange candidate combined active writes and peer reads at 80 ms. Its
local pixel gates passed, but the 1,000-actor TLS HTTP stage still coalesced much
of its nominal schedule and exceeded 500 ms before rendering. Keep these
results separate; a two-browser latency result cannot close a capacity gate.

Three test weaknesses surfaced during the candidate review and execution:

- A row-lock observer searched SQL text beyond PostgreSQL's normal activity-text
  truncation. It now observes the exact blocking backend and active lock wait in
  the owned database. The real barrier, snapshot and commit assertions remain.
- A long synchronous input burst filled the bounded frame-capture queue, and a
  fresh lease held beyond two seconds tested request cancellation rather than
  elapsed-lease subtraction. Time a short input separately from sustained cadence;
  age a real admitted lease and prove the delayed HTTP response actually finishes.
  Keep the original 500 ms latency and 15 ms uncertainty limits. Invalid captures
  remain failures, rather than acquiring an invented finite latency.
- A load response could report HTTP 200 while omitting usable collaboration state.
  Validate the submitted sequence/acceptance, peer identity, state, lease and
  deterministic fixture coordinates. Require every HTTP actor to observe its
  room's other HTTP actors during the active interval, with enough remaining lease
  for receipt. Reject responses beyond the declared validation byte bound.

The private server-side exchange experiment retains explicit transactions and
ordered locks. Its internal authorization query needs a fresh post-lock snapshot;
lease eligibility also needs a clock captured after waiting. Merely combining the
operations in an ordinary CTE, or retaining the outer call's statement timestamp,
would break those contracts. Catalog, queued-revocation, expiry, replay, capacity
and rotating-pool checks precede any performance claim.

The mixed paint harness uses 998 modeled HTTP actors plus two real native browser
clients, rather than claiming 1,000 browsers. It requires progress from the active
HTTP load during timed input and reports paint and HTTP statistics separately.
Both browser clients use one worker; background actors span the simulated fleet.
Only an executed, exact-artifact passing receipt establishes its local result.
Hosted independent accounts, real inbox delivery and sustained production load
remain separate acceptance boundaries.

The first mixed 998-HTTP/two-browser run passed all three pixel timings but failed
a later reload assertion. A command bridge can precede asynchronous native document
opening. Successful reload fixtures now reuse the existing dimension/layer/active-layer
readiness barrier before checking the original native pixel, and retain safe per-step
checkpoints plus download status/hash checks. The original failed receipt remains.
The rerun passed all pixel, native preview, save/reload and archive assertions without
changing runtime code or the 500 ms / 15 ms gates. Aggregate peer coverage over the
12-second load and HTTP progress during each paint bracket are reported separately;
neither is a per-actor delivery trace for every bracket or production capacity proof.
