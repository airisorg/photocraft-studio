# PhotoCraft Studio

PhotoCraft Studio is a browser-based image editor with cloud projects, sharing, and collaboration.

It is an independent fork of [PhotoCraft](https://github.com/storytold/photocraft), created by the ArtCraft team and PhotoCraft contributors. Their Rust editor, painting tools, document model, file formats, and GPU renderer remain its foundation. We thank the upstream contributors. This fork is maintained separately and is not affiliated with, sponsored by, or endorsed by the original project or the ArtCraft team.

PhotoCraft Studio is deployed on [Tofu](https://trytofu.ai/), which helps your coding agent take an existing app online with hosting, a managed database, Google sign-in, and transactional email in one place. To take your own app from local development to the web, [try Tofu](https://trytofu.ai/) or explore its [documentation](https://trytofu.ai/docs).

[![Deployed on Tofu](docs/media/deployed-on-tofu.svg)](https://trytofu.ai/)

[Open the editor](https://photocraft-studio-d42c446ec275.trytofu.app/) · [Explore PhotoCraft Studio](https://photocraft-studio-d42c446ec275.trytofu.app/about.html) · [How we deployed with Tofu](docs/tofu-deployment.md)

![PhotoCraft Studio running in a browser with an editable starter design, native tools and nine layers](docs/media/editor.png)

[Watch the editing walkthrough](https://photocraft-studio-d42c446ec275.trytofu.app/media/editing-walkthrough.mp4) · [Watch two editors collaborate](https://photocraft-studio-d42c446ec275.trytofu.app/media/collaboration-demo.mp4)

The editing video follows a real template through text changes, artwork movement, Undo/Redo and a layered file download/reopen. The collaboration video shows two independent browser sessions, including collaborator cursors and paint previews. Both are recorded locally at original speed using this browser editor. The public editor is deployed on Tofu. The collaboration workspace uses disposable test accounts; it is not a measurement of internet latency.

[![Two native PhotoCraft canvases showing the same drawing from two editors](docs/media/collaboration-preview.png)](https://photocraft-studio-d42c446ec275.trytofu.app/about.html#collaboration-demo)

## What we added

We adapted the existing editor for a hosted, shared browser workspace rather than building a second editing engine:

- A Rust browser workspace for projects, starter designs, search, folders, starred items, Trash, and one bounded browser recovery copy.
- A Rust HTTP backend for sign-in, cloud saves, version history, permissions, invitation emails, revocable view links, comments, and presence.
- Bounded named collaborator cursors and native brush, pencil, eraser, and move previews. Saved documents remain the authoritative state; unrestricted simultaneous editing is not implemented.
- Tofu deployment packaging and guarded upstream-update tooling.
- Browser journeys, authorization and recovery regressions, transaction-pool checks, visual assertions, and rendered collaboration latency tests.

Editing remains the original Rust/egui application compiled to WebAssembly. The cloud adapter reuses the native `.pcraft` document format. Rendering and painting run on the user's machine. The browser prefers hardware WebGPU and selects the existing WebGL2 renderer for software WebGPU adapters before opening the workspace.

The editor and this adaptation remain early-alpha software. A recorded short local workload with 998 HTTP collaborators and two browser clients passed the cursor/Pencil paint and save/reload checks; the [production 1,000-active-client target](docs/scale-release-review.md) remains unverified. See the [collaboration contract](docs/collaboration-architecture.md), [measured performance](docs/collaboration-performance.md), and [upstream roadmap](docs/roadmap.md) for current behavior and limits.

## Try it

Open [PhotoCraft Studio](https://photocraft-studio-d42c446ec275.trytofu.app/) in a supported desktop browser. Local editing and download do not require a cloud account. Cloud saving requires sign-in; collaboration also requires access to a shared project.

To build the native editor from [airisorg/photocraft-studio](https://github.com/airisorg/photocraft-studio):

```sh
git clone https://github.com/airisorg/photocraft-studio.git
cd photocraft-studio
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

Report issues for this fork in [this repository](https://github.com/airisorg/photocraft-studio/issues). Start with [AGENTS.md](AGENTS.md) and the [contribution guide](docs/contributing.md). Keep the UI thin, preserve native file compatibility, and add a regression test for each bug fix.

Installers for macOS, Windows, Linux and the web are attached to each [GitHub release](https://github.com/storytold/photocraft/releases). On Linux you can pick an AppImage, a `.deb`, an `.rpm`, a tarball or a Flatpak bundle. The bundle needs the freedesktop runtime from [Flathub](https://flathub.org/setup), which `flatpak` offers to install along with it:

```sh
flatpak install --user photocraft-<version>-linux-x86_64.flatpak   # or -linux-aarch64
flatpak run ai.storyteller.photocraft
```

Maintainers: [`docs/releasing.md`](docs/releasing.md) explains how releases are built, signed and published.

> [!IMPORTANT]
> **Status:** PhotoCraft is in early alpha. The core editing workflow is here, and we're working toward full Photoshop parity, milestone by milestone (see [`docs/roadmap.md`](docs/roadmap.md)). Progress is measured, not guessed: `cargo xtask parity` checks every item in Photoshop's menu tree against the live command registry and writes [`docs/parity.md`](docs/parity.md). Expect rough edges, and please file issues. You can also tell us what broke on [Discord](https://discord.gg/artcraft).

## Documentation

Developer, architecture, automation, format, and security documentation is maintained in the [PhotoCraft documentation book](book/).

## Security

Security architecture, threat modeling, parser hardening, fuzzing, and vulnerability reporting are covered in the [security documentation](book/src/security/) and the repository [security policy](SECURITY.md).

## The Crafting Apps

PhotoCraft is one of the **Crafting Apps**: free, open-source creative tools from the
[ArtCraft](https://getartcraft.com/) team, each written from scratch in Rust and each able to
stand on its own.

| | App | What it's for | Code | Learn more |
|:-:|---|---|---|---|
| <img src="https://raw.githubusercontent.com/storytold/photocraft/main/assets/app-icon/hicolor/64x64/apps/ai.storyteller.photocraft.png" alt="" width="32" height="32"> | **PhotoCraft** | **Image editing: layers, masks, type and real PSD files · you are here** | [GitHub](https://github.com/storytold/photocraft) | [Website](https://getartcraft.com/apps/photocraft) |
| <img src="https://raw.githubusercontent.com/storytold/vectorcraft/main/assets/app-icon/hicolor/64x64/apps/ai.storyteller.vectorcraft.png" alt="" width="32" height="32"> | **VectorCraft** | Vector illustration | [GitHub](https://github.com/storytold/vectorcraft) | [Website](https://getartcraft.com/apps/vectorcraft) |
| <img src="https://raw.githubusercontent.com/storytold/filmcraft/main/assets/app-icon/hicolor/64x64/apps/ai.storyteller.filmcraft.png" alt="" width="32" height="32"> | **FilmCraft** | Video editing, color and sound | [GitHub](https://github.com/storytold/filmcraft) | [Website](https://getartcraft.com/apps/filmcraft) |
| <img src="https://raw.githubusercontent.com/storytold/lightcraft/main/assets/app-icon/hicolor/64x64/apps/ai.storyteller.lightcraft.png" alt="" width="32" height="32"> | **LightCraft** | Photo library and raw development | [GitHub](https://github.com/storytold/lightcraft) | [Website](https://getartcraft.com/apps/lightcraft) |
| <img src="https://raw.githubusercontent.com/storytold/printcraft/main/assets/app-icon/hicolor/64x64/apps/ai.storyteller.printcraft.png" alt="" width="32" height="32"> | **PrintCraft** | Reading, organizing and protecting PDFs | [GitHub](https://github.com/storytold/printcraft) | [Website](https://getartcraft.com/apps/printcraft) |
| <img src="https://raw.githubusercontent.com/storytold/effectcraft/main/assets/app-icon/hicolor/64x64/apps/ai.storyteller.effectcraft.png" alt="" width="32" height="32"> | **EffectCraft** | Motion graphics and visual effects | [GitHub](https://github.com/storytold/effectcraft) | [Website](https://getartcraft.com/apps/effectcraft) |
| <img src="https://raw.githubusercontent.com/storytold/designcraft/main/assets/app-icon/hicolor/64x64/apps/ai.storyteller.designcraft.png" alt="" width="32" height="32"> | **DesignCraft** | Page layout and publishing | [GitHub](https://github.com/storytold/designcraft) | [Website](https://getartcraft.com/apps/designcraft) |

And [**ArtCraft**](https://getartcraft.com/) itself, our AI image and video studio for artists who want real control.

The Crafting Apps share the same conventions: clean-room and pure Rust, native on macOS, Windows and Linux, in the browser via WebAssembly, and fully drivable by agents.

<br>

<p align="center">
  <a href="https://discord.gg/artcraft"><img alt="Join the ArtCraft community on Discord" src="https://img.shields.io/badge/Join%20us%20on%20Discord-5865F2?style=for-the-badge&logo=discord&logoColor=white" height="40"></a>
</p>

<h3 align="center">Come make things with us</h3>

<p align="center">
  Our Discord is where artists of every kind hang out: people who paint, shoot, draw, cut film,
  set type, and people still figuring out what they like to make. Share what you're working on,
  ask for help, tell us what's broken, or tell us what you wish these tools could do.
  Whatever your medium and however long you've been at it, you're welcome here.
</p>

<p align="center">
  <a href="https://discord.gg/artcraft"><b>discord.gg/artcraft</b></a> ·
  <a href="https://getartcraft.com/">getartcraft.com</a> ·
  <a href="https://getartcraft.com/apps">The Crafting Apps</a> ·
  <a href="https://getartcraft.com/apps/photocraft">PhotoCraft</a>
</p>

---

## License and credits

The code is dual-licensed under [MIT](LICENSE-MIT) or [Apache-2.0](LICENSE-APACHE), at your option. Upstream copyright and required notices are preserved in [NOTICE](NOTICE). Contributions to this fork use the same code licenses.

Fonts, icons, images, and other non-code assets retain their individual licenses and credits in [ATTRIBUTION.md](ATTRIBUTION.md). The ArtCraft brand artwork is excluded from the modified source tree; its original terms are retained in [the brand license](docs/brand/LICENSE-brand.txt). Code licenses do not grant trademark rights.

Adobe and Photoshop are trademarks of Adobe Inc.; Figma and Canva are trademarks of their respective owners. References describe compatible workflows or design comparisons and do not imply affiliation, sponsorship, or endorsement.
