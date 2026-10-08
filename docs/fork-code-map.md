# Fork code map

PhotoCraft Studio is an independent adaptation of [storytold/photocraft](https://github.com/storytold/photocraft). The upstream ArtCraft team and PhotoCraft contributors created the editor. This fork adds hosted workspace and collaboration behavior and does not represent an official upstream release.

The imported upstream reference currently used by this checkout is `3a3984075a1fd06d1af3e660aa376ee5368c4f73`. This is a provenance reference, not a claim that the current upstream repository has stopped changing. `git merge-base HEAD upstream/main` identifies the common ancestor of a checkout; [upstream updates](upstream-updates.md) describes the guarded import and release process.

## Original editor

The original crates keep their names and layer boundaries. `geom`, `cms`, `color`, and `raster` provide the foundations; `doc` and `ops` provide documents and history; `algo`, `paint`, `text`, and `vector` implement editing; `compose`, `gpu`, `format`, `io`, and the codec crates render and load/save files; `engine` exposes commands; `ui-egui` displays the native UI. `apps/photocraft` and `apps/photocraft-cli` remain the native application and CLI. [Architecture](architecture.md) explains their APIs and dependency layers.

These directories are reused source, not immutable vendor copies. Do not assume every line under `crates/` is unchanged upstream. Targeted changes include UI styling, window behavior, native collaboration previews, browser-safe service hooks, and separately tested correctness fixes in several native crates. Compare a specific path with the imported upstream reference when reviewing ownership:

```sh
git diff 3a3984075a1fd06d1af3e660aa376ee5368c4f73 -- crates/ui-egui/src/canvas.rs
git log --oneline 3a3984075a1fd06d1af3e660aa376ee5368c4f73..HEAD -- crates/engine
```

## Added adapters

| Path | Responsibility |
|---|---|
| `apps/photocraft-web/src/home.rs` | Reusable workspace styling, navigation and starter-design widgets |
| `apps/photocraft-web/src/cloud.rs` | Stateful workspace, project/recovery/account/sharing UI, authenticated HTTP calls, native save/load integration, versions and synchronization |
| `apps/photocraft-web/src/live.rs` | Ephemeral cursor and native gesture transport; no separate painting engine |
| `crates/ui-egui/src/collaboration.rs` | Temporary remote view state around the existing native canvas and compositor |
| `crates/ui-egui/src/prefs_ui.rs` | Modified upstream Preferences view: compact native section selector and stacked, width-bounded fields on narrow viewports; reuses the existing widgets, working copy and preference commands |
| `crates/ui-egui/src/dialogs.rs` | Modified upstream dialog shell: bounds the Preferences body to the available viewport while preserving its existing Apply/OK/Cancel path |
| `apps/photocraft-cloud/src/` | HTTP service, authorization, invitations, document merge/validation and bounded live previews |
| `apps/photocraft-cloud/migrations/` | Versioned, re-runnable application-schema migrations |
| `apps/photocraft-cloud/examples/fixture.rs` | Synthetic native-document fixture for tests |
| `tests/web/` | Local API, authentication, browser, recovery, live-preview, visual, security and performance checks |
| `packaging/web/stage-tofu.sh`, `tofu-package.py` | Container source/asset staging and release provenance |
| `packaging/web/build-release.py` | Existing Trunk release build with compiler path remapping; no editor replacement |
| `packaging/web/upstream-release.py` | Candidate import and tested-release promotion support |
| `packaging/web/rust-notices.py`, `runtime-notices.py`, `packaging/licenses/` | Verified dependency/runtime notice collection and pinned upstream supplements |
| `.github/workflows/web-cloud.yml`, `update-and-release.yml` | Fork-specific acceptance and guarded upstream-update jobs |

The web entry point, bootstrap, and Cargo manifests in `apps/photocraft-web` existed upstream and were adapted. `crates/ui-egui/src/canvas.rs`, `lib.rs`, and `theme.rs` contain small integration hooks alongside broader workspace/UI corrections. The backend reuses the original native format loader; cloud-specific limits belong in the adapter. The additive `LoadStats` API in `crates/format/src/lib.rs` and `store.rs` exposes existing decoded-byte accounting for cloud validation; the original loader wrapper, desktop defaults and file bytes remain compatible.

## Documentation and release organization

- `docs/architecture.md`, `development.md`, `roadmap.md`, and the documentation book describe the original editor. Their upstream limitations still apply.
- `docs/web-cloud.md`, `collaboration-architecture.md`, `collaboration-performance.md`, `workspace-design-contract.md`, and `web-verification-retrospective.md` describe this fork's adapter, acceptance scope, and measured evidence.
- `SECURITY.md` is the reporting entry point. [The release review](security-release-review.md) distinguishes fixed defects, unresolved risks, local results, and actual hosted observations.
- `LICENSE-MIT`, `LICENSE-APACHE`, `NOTICE`, and `ATTRIBUTION.md` preserve licensing and asset provenance. Restricted upstream brand artwork is removed from the current modified tree; retaining its license does not authorize using its marks.
- Local `outputs/`, test accounts, database contents, credentials, caches, corpus files, and `log/` entries are not application source or public release artifacts. Inspect tracked files and reachable history separately before changing repository visibility.

## Update boundary

Keep upstream implementation in place. Add hosted behavior in adapter modules and use existing engine commands, native renderers, and format APIs. Avoid a second UI framework or duplicated paint/document implementation. Import upstream updates into a candidate branch, run native and web gates, then promote the tested release. Tests running locally do not prove that GitHub automation or Tofu automatic updates are enabled; see [upstream updates](upstream-updates.md) for the owner-controlled steps.

This map is a guide to responsibilities. Git history and the diff against the pinned upstream reference are the authoritative per-file record.
