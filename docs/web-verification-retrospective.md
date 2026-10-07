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
| Share window painted behind itself | Window opened and Escape dismissed it | Modal pointer and keyboard ownership | Outside drag leaves document history unchanged; closing restores canvas input |
| Member menu failed after Escape | Membership API worked; dropdown could be drawn | Nested menu closes before its parent and remains usable | Open, Escape, reopen, change role through UI; assert API write and resulting permissions |
| Dialog actions clipped | Dialog bounding box was inside the viewport | Fields and final actions must remain reachable and visible | Scroll to all fields, use real footer actions, test keyboard focus and resize with the window open |
| Text felt unlike browser applications | Editor rendered at one tested resolution | Raster scale, font weight, readability, frame pacing and actual screen conditions | Compare canvas backing size with CSS size × DPR; measure reference styles and actual interaction timing |
| Collaboration confidence too broad | Synthetic local accounts and provider simulation passed | Real identity return, email delivery and independent hosted users | Approved recipient, real callback, two isolated accounts, persistence, revoke and reconnect evidence |

The test for the saved CPU preference failed before the adapter fix: it observed GPU still
active after restart. The original header failed the pixel geometry check. These are useful
regressions because they distinguish the broken behavior from the intended behavior.

## Inventory: scope is larger than a menu count

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

| Capability | Current implementation | Remaining acceptance boundary |
|---|---|---|
| Google identity and sessions | Tofu-managed sign-in; server-verified identity; account avatar; logout | Real hosted callback and session lifecycle must be recorded, separately from simulated provider tests |
| Initial save and autosave | Original `.pcraft`, chunked uploads, checksums, revision checks and autosave after initial save | Hosted save/reload, interrupted writes, account change during requests |
| Email invitation | Owner invites an email with view/edit access; delivery success/failure and throttling | Real delivery and acceptance by an exact approved recipient; do not guess an address |
| Membership | Owner changes view/edit access and removes membership | Actual dropdown journey, immediate server denial after revoke, stale tab behavior |
| Shared projects | Signed-in matching email sees authorized projects | Existing/new invitee, wrong account, expired sign-in, removed member |
| Public view link | Anonymous view/download; rotation and revocation | Separate browser, read-only enforcement, no stale access after revoke; downloaded copies cannot be recalled |
| Comments | Authorized comments and permissions | Two-user receipt, ordering, error/retry, revoked access, long content |
| Presence and sync | Presence and committed-revision polling | Independent accounts, reconnect, stale presence, no cross-project/account leakage |
| Concurrent changes | Conservative independent-manifest merge; conflicts preserve a copy | Two authors on same/different properties, deletion vs edit, stale base, interruption and recovery |
| Version history | Saved revisions and restore through the original file format | Permissions, actual pixels/layers after restore, undo contract and stale collaborator |
| Sign-out and local work | Recovery/download options before sign-out | Every choice, unsaved documents, pending upload and next account isolation |
| Live cursors / simultaneous stroke merging | Not implemented | Explicit product gap; committed-revision sync must not be described as a CRDT or real-time stroke editor |

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

- Complete and verify the new dropdown, display-density and saved-renderer regressions.
- Review the final phone New Document and Share screenshots; use their actual controls.
- Keep the explicit hidden-preference list distinct from the scorecard's heuristic read count.
- Real invitation delivery needs the exact approved email address. Local synthetic accounts
  must never be inserted into the hosted database.
- Current GitHub Actions jobs were blocked by the account's billing/spending restriction;
  local passes must remain separately reported until an exact-revision CI run executes.
- Full native behavior parity, live cursors, simultaneous-stroke collaboration, accessible
  DOM controls, physical-device acceptance and worker offload are open work, not hidden
  behind this UI patch.

## Continuous upstream updates

See [upstream updates](upstream-updates.md). The scheduled workflow preserves the original
Git history, merges upstream into a candidate, calls the existing native and web suites at
that exact commit, and promotes both main and a prebuilt deployment branch only on success.
Conflicts, unavailable CI, a newer main revision, or a mismatched artifact prevent promotion.
The six-hour schedule is a polling interval, not an instantaneous update promise. GitHub
may delay scheduled jobs. Tofu GitHub authorization and Actions billing must be working.

## Display-test correction

Chromium's context-only `device_scale_factor` emulation changed `devicePixelRatio` without
changing ResizeObserver's `devicePixelContentBoxSize`. PhotoCraft correctly relies on that
physical box through eframe. The test now launches Chromium with the matching process scale
as well as its context scale. DPR 1/1.25/1.5/2 canvas backing dimensions and rendered header
geometry pass. No renderer workaround was added for a test-emulation artifact.
