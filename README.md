# PhotoCraft Studio

PhotoCraft Studio is an independent web adaptation of [PhotoCraft](https://github.com/storytold/photocraft), created by the ArtCraft team and the PhotoCraft contributors. Their Rust editor, document model, painting tools, file formats, and GPU compositor are the foundation of this project. Thank you to the upstream contributors.

This fork is maintained independently in [FZ2000/photocraft](https://github.com/FZ2000/photocraft). It is not affiliated with, sponsored by, endorsed by, or an official release of the ArtCraft team. Please report issues in this fork to its maintainers.

## What we added

We adapted the existing editor for a hosted, shared browser workspace rather than building a second editing engine:

- A Rust browser workspace for projects, starter designs, search, folders, starred items, Trash, and one bounded browser recovery copy.
- A Rust HTTP backend for sign-in, cloud saves, version history, permissions, invitation emails, revocable view links, comments, and presence.
- Bounded named collaborator cursors and native brush, pencil, eraser, and move previews. Saved documents remain the authoritative state; unrestricted simultaneous editing is not implemented.
- Tofu deployment packaging and guarded upstream-update tooling.
- Browser journeys, authorization and recovery regressions, transaction-pool checks, visual assertions, and rendered collaboration latency tests.

Editing remains the original Rust/egui application compiled to WebAssembly. The cloud adapter reuses the native `.pcraft` document format. Rendering and painting run on the user's machine. The browser prefers hardware WebGPU and selects the existing WebGL2 renderer for software WebGPU adapters before opening the workspace.

The editor and this adaptation remain early-alpha software. A passed local benchmark is not a production latency or capacity guarantee; the [1,000-active-client release target](docs/scale-release-review.md) is not met. See the [collaboration contract](docs/collaboration-architecture.md), [measured performance](docs/collaboration-performance.md), and [upstream roadmap](docs/roadmap.md) for current behavior and limits.

## Try it

Open [PhotoCraft Studio](https://photocraft-studio-d42c446ec275.trytofu.app/) in a supported desktop browser. Local editing and download do not require a cloud account. Saving and collaboration require sign-in and access to a shared project.

To build the native editor:

```sh
git clone https://github.com/FZ2000/photocraft.git
cd photocraft
cargo run --release -p photocraft
```

For the browser build, local backend setup, and disposable-database tests, follow [the web development guide](docs/web-cloud.md). Keep database credentials outside the repository; never put production keys in browser code or test fixtures.

## Where the code lives

| Area | Location | Origin |
|---|---|---|
| Editing engine, document model, algorithms, formats, and compositors | `crates/` | Upstream PhotoCraft, with documented targeted changes |
| Native and command-line applications | `apps/photocraft/`, `apps/photocraft-cli/` | Upstream PhotoCraft |
| Browser application | `apps/photocraft-web/` | Upstream Rust/WASM entry point plus our workspace and cloud adapters |
| HTTP service and migrations | `apps/photocraft-cloud/` | Added by this fork |
| Web acceptance and performance tests | `tests/web/` | Added by this fork |
| Web deployment and upstream-update packaging | `packaging/web/` | Upstream web packaging plus our Tofu and update scripts |

The [fork code map](docs/fork-code-map.md) identifies added modules, modified upstream areas, licenses, and the update boundary. Original and adapter code stay in the same workspace so we can reuse native functionality and review upstream changes without duplicating the editor.

## Documentation and contributing

Start with [AGENTS.md](AGENTS.md) and the [contribution guide](docs/contributing.md). Keep the UI thin, preserve native file compatibility, and add a regression test for each bug fix.

- [Architecture](docs/architecture.md) and [development](docs/development.md)
- [Browser workspace and deployment](docs/web-cloud.md)
- [Upstream update process](docs/upstream-updates.md)
- [Collaboration architecture](docs/collaboration-architecture.md) and [performance evidence](docs/collaboration-performance.md)
- [Security policy](SECURITY.md), [release review](docs/security-release-review.md), [asset attribution](ATTRIBUTION.md), and [required notices](NOTICE)

## Hosted with Tofu

[Tofu](https://trytofu.ai/) takes an existing app from your coding agent to a live website, bringing hosting, a managed database, sign-in, email, domains, and analytics into one workflow. PhotoCraft Studio uses Tofu for its hosted deployment and managed services. If your app works locally and you want to put it online, [try Tofu](https://trytofu.ai/) or read its [documentation](https://trytofu.ai/docs).

This acknowledgment describes the deployment platform; it does not imply sponsorship or endorsement.

## License and credits

The code is dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option. Upstream copyright and required notices are preserved in [NOTICE](NOTICE). Contributions to this fork use the same code licenses.

Fonts, icons, images, and other non-code assets retain their individual licenses and credits in [ATTRIBUTION.md](ATTRIBUTION.md). The ArtCraft brand artwork is excluded from the modified source tree; its original terms are retained in [the brand license](docs/brand/LICENSE-brand.txt). Code licenses do not grant trademark rights.

Adobe and Photoshop are trademarks of Adobe Inc.; Figma and Canva are trademarks of their respective owners. References describe compatible workflows or design comparisons and do not imply affiliation, sponsorship, or endorsement.
