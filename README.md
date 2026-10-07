# PhotoCraft Studio — private web adaptation

Based on PhotoCraft by the ArtCraft team. This modified version adds a browser workspace and a Rust cloud service for deployment on Tofu. It is not an official ArtCraft release.

See [web architecture and deployment](docs/web-cloud.md). The editing engine, formats, GPU compositor and desktop UI remain upstream code.

---



PhotoCraft Studio is a browser-based image editor with cloud projects, sharing, and collaboration.

It is an independent fork of [PhotoCraft](https://github.com/storytold/photocraft), created by the ArtCraft team and PhotoCraft contributors. Their Rust editor, painting tools, document model, file formats, and GPU renderer remain its foundation. We thank the upstream contributors. This fork is maintained separately and is not affiliated with, sponsored by, or endorsed by the original project or the ArtCraft team.

<p align="center">
  <img alt="100% Rust" src="https://img.shields.io/badge/100%25-Rust-b7410e?style=flat-square&logo=rust">
  <img alt="macOS · Windows · Linux · FreeBSD · Web" src="https://img.shields.io/badge/macOS%20%C2%B7%20Windows%20%C2%B7%20Linux%20%C2%B7%20FreeBSD%20%C2%B7%20Web-native-2f7bf5?style=flat-square">
  <img alt="License: MIT OR Apache-2.0" src="https://img.shields.io/badge/license-MIT%20%2F%20Apache--2.0-3a3a3a?style=flat-square">
  <img alt="Status: early alpha" src="https://img.shields.io/badge/status-early%20alpha-d69e2e?style=flat-square">
</p>

[![Deployed on Tofu](docs/media/deployed-on-tofu.svg)](https://trytofu.ai/)

[Open the editor](https://photocraft-studio-d42c446ec275.trytofu.app/) · [Explore PhotoCraft Studio](https://photocraft-studio-d42c446ec275.trytofu.app/about.html) · [How we deployed with Tofu](docs/tofu-deployment.md)

![PhotoCraft Studio running in a browser with an editable starter design, native tools and nine layers](docs/media/editor.png)

[Watch the editing walkthrough](https://photocraft-studio-d42c446ec275.trytofu.app/media/editing-walkthrough.mp4) · [Watch two editors collaborate](https://photocraft-studio-d42c446ec275.trytofu.app/media/collaboration-demo.mp4)

The editing video follows a real template through text changes, artwork movement, Undo/Redo and a layered file download/reopen. The collaboration video shows two independent browser sessions, including collaborator cursors and paint previews. Both are recorded locally at original speed using this browser editor. The public editor is deployed on Tofu. The collaboration workspace uses disposable test accounts; it is not a measurement of internet latency.

[![Two native PhotoCraft canvases showing the same drawing from two editors](docs/media/collaboration-preview.png)](https://photocraft-studio-d42c446ec275.trytofu.app/about.html#collaboration-demo)

## What we added

<table>
  <tr>
    <td width="25%" valign="top">
      <h3>🎛️ Familiar by design</h3>
      The menus, shortcuts, panels and tools are where your hands expect them, from ⌘J to ⇧⌘D. If you know Photoshop, you already know PhotoCraft.
    </td>
    <td width="25%" valign="top">
      <h3>⚡ Native and fast</h3>
      A GPU compositor on wgpu (Metal, Vulkan, DX12, WebGPU), copy-on-write tiles and multithreaded filters. No Electron, no web view, no waiting.
    </td>
    <td width="25%" valign="top">
      <h3>🗂️ Real PSD files</h3>
      Open, edit and save layered Photoshop documents. Re-saving keeps the render of 307 of the 309 psd-tools test files.
    </td>
    <td width="25%" valign="top">
      <h3>🤖 Agent-ready</h3>
      Every action is a command, so you can drive the same engine from the UI, the CLI, a JSON control channel or an MCP server.
    </td>
  </tr>
</table>

- A Rust browser workspace for projects, starter designs, search, folders, starred items, Trash, and one bounded browser recovery copy.
- A Rust HTTP backend for sign-in, cloud saves, version history, permissions, invitation emails, revocable view links, comments, and presence.
- Bounded named collaborator cursors and native brush, pencil, eraser, and move previews. Saved documents remain the authoritative state; unrestricted simultaneous editing is not implemented.
- Tofu deployment packaging and guarded upstream-update tooling.
- Browser journeys, authorization and recovery regressions, transaction-pool checks, visual assertions, and rendered collaboration latency tests.

Editing remains the original Rust/egui application compiled to WebAssembly. The cloud adapter reuses the native `.pcraft` document format. Rendering and painting run on the user's machine. The browser prefers hardware WebGPU and selects the existing WebGL2 renderer for software WebGPU adapters before opening the workspace.

The editor and this adaptation remain early-alpha software. A recorded short local workload with 998 HTTP collaborators and two browser clients passed the cursor/Pencil paint and save/reload checks; the [production 1,000-active-client target](docs/scale-release-review.md) remains unverified. See the [collaboration contract](docs/collaboration-architecture.md), [measured performance](docs/collaboration-performance.md), and [upstream roadmap](docs/roadmap.md) for current behavior and limits.

## Try it

Open [PhotoCraft Studio](https://photocraft-studio-d42c446ec275.trytofu.app/) in a supported desktop browser. Local editing and download do not require a cloud account. Cloud saving requires sign-in; collaboration also requires access to a shared project.

## Everything in the box

<table>
  <tr>
    <td width="33%" valign="top">
      <h4>🧰 34 tools</h4>
      Move · Rectangular and Elliptical Marquee · Lasso · Polygonal Lasso · Magic Wand · Quick Selection · Object Selection · Crop · Eyedropper · Brush · Pencil · Mixer Brush · Color Replacement · Eraser · Clone Stamp · Healing Brush · Spot Healing · History Brush · Gradient · Paint Bucket · Blur · Sharpen · Smudge · Dodge · Burn · Sponge · Pen · Path Selection · Type · five Shape tools · Hand · Zoom
    </td>
    <td width="33%" valign="top">
      <h4>🖌️ A real brush engine</h4>
      Shape Dynamics, Scattering, Texture, Dual Brush, Color Dynamics, Transfer, Brush Pose, Wet Edges, Build-up and Smoothing (including Pulled String), driven by pen pressure, tilt, rotation and direction. Brush presets, Define Brush from Selection, and deterministic, replayable strokes.
    </td>
    <td width="33%" valign="top">
      <h4>🗃️ Layers, done properly</h4>
      Groups, clipping masks, pixel and vector masks, fill layers (solid, gradient and pattern), adjustment layers, live smart objects with smart filters and lossless transforms and warps, multi-layer selection with align, distribute and link, alpha channels and Quick Mask, 27 blend modes, opacity and fill, locks, colour labels, layer filters, merge, flatten, rasterize, Layer via Copy/Cut, Paste Into.
    </td>
  </tr>
  <tr>
    <td width="33%" valign="top">
      <h4>🎨 Any colour, any depth</h4>
      RGB, Grayscale, CMYK and Lab documents at 8, 16 and 32 bits per channel. Bit depth and colour model are runtime data, so every tool works at every depth.
      <br><br>
      Real ICC colour management in pure Rust: embedded profiles, Assign and Convert to Profile with all four rendering intents and black point compensation, soft proofing (⌘Y) and Gamut Warning (⇧⌘Y) on the GPU.
    </td>
    <td width="33%" valign="top">
      <h4>🗂️ Formats</h4>
      PSD and PSB, plus PNG, JPEG, TIFF, WebP, GIF, BMP, TGA, ICO, QOI, PNM, OpenEXR, Radiance HDR and AVIF, with symmetric read and write at 8, 16 and 32 bits, HEIC photos from iPhone and Mac (read; in official builds, an optional <code>--features heif</code> build feature), and the native <code>.pcraft</code> format.
    </td>
    <td width="33%" valign="top">
      <h4>🪄 The everyday essentials</h4>
      Auto Tone, Contrast and Color · Equalize · Image and Canvas Size · Crop and Trim · Reveal All · Edit › Fill and Stroke · Copy Merged · Paste in Place · guides, rulers, grid and snapping · Actions record and replay · a command palette (⌘K).
    </td>
  </tr>
</table>

<br>

## PSD without compromise

PhotoCraft's PSD support is a standalone crate written from Adobe's public specification and tested against a corpus of real-world files.

- **Faithful round trips:** opening and re-saving a document renders the same for 307 of the 309 files in the psd-tools test set and 169 of 170 in our mixed ag-psd/psd-tools set (`crates/io/tests/corpus.rs`; fetch the psd-tools set with `cargo xtask corpus --psd-tools`), and anything we don't model yet (raw blocks, descriptors, extras) is carried over instead of being dropped. A re-saved file is not byte-identical to its source: PhotoCraft rewrites image resources, layer records and the composite. Only the standalone `photocraft-psd` crate, parsing and writing a file without the document model, reproduces every parseable corpus file byte for byte (`crates/psd/tests/corpus.rs`).
- **Pixels that match:** a composite oracle compares our render with Photoshop's own merged image, covering gradient interpolation (Classic, Perceptual and Linear), layer effects, shape strokes, clipping and fill opacity.
- **Large documents:** PSB, 16 and 32-bit files, and CMYK and Lab documents open natively.

## Built for agents

Every menu item, tool and dialog runs a command from one registry of 500+ commands. The UI, the CLI, the JSON control channel and the MCP server all call the same commands, so anything you can click, a script or an AI agent can do too.

```sh
# Headless: open, edit, save
photocraft-cli run wave.psd \
  --cmd filter.sharpen.smartSharpen     --params '{"amount":80}' \
  --cmd layer.newAdjustmentLayer.curves --params '{"points":[[0,0],[64,48],[192,212],[255,255]]}' \
  --out wave-final.png

# Apply one action list to a folder of images
photocraft-cli batch --actions grade.json --in ./raw --out ./graded

# Every subcommand explains itself
photocraft-cli batch --help

# Let an agent drive it over MCP (headless, or bridged to the running app)
photocraft-cli mcp
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

Japanese fonts for the UI and Type tool come from [craft-fonts](https://github.com/storytold/craft-fonts), an optional build input (desktop release builds always include it). Without it PhotoCraft uses your system's CJK fonts:

```sh
git clone https://github.com/storytold/craft-fonts ../craft-fonts
CRAFT_FONTS_DIR="$PWD/../craft-fonts" cargo run --release -p photocraft
```

New contributors and AI agents: start with [`AGENTS.md`](AGENTS.md), then [`docs/`](docs/).

Installers for macOS, Windows, Linux, FreeBSD and the web are attached to each [GitHub release](https://github.com/storytold/photocraft/releases). On Linux you can pick an AppImage, a `.deb`, an `.rpm`, a tarball or a Flatpak bundle. The bundle needs the freedesktop runtime from [Flathub](https://flathub.org/setup), which `flatpak` offers to install along with it:

```sh
flatpak install --user photocraft-<version>-linux-x86_64.flatpak   # or -linux-aarch64
flatpak run ai.storyteller.photocraft
```

On macOS, the command-line tool comes as `photocraft-cli-<version>-macos-universal.zip`. The binary is signed with the same Developer ID as the app and notarized by Apple. A bare binary can't carry a stapled notarization ticket the way the DMG does, so the first time you run it macOS checks the notarization online. You can confirm it yourself:

```sh
ditto -x -k photocraft-cli-<version>-macos-universal.zip .
spctl --assess --type install -vv photocraft-cli-<version>-macos-universal/photocraft-cli
# ... accepted, source=Notarized Developer ID
```

On FreeBSD 14 (x86_64), the release has a tarball laid out like `/usr/local`. Install the runtime libraries, then unpack it there:

```sh
pkg install libxkbcommon wayland libX11 libXcursor libXrandr libXi libxcb mesa-libs vulkan-loader gtk3 fontconfig freetype2 alsa-lib
tar -xzf photocraft-<version>-freebsd-x86_64.tar.gz --strip-components 1 -C /usr/local
photocraft
```

Maintainers: [`docs/releasing.md`](docs/releasing.md) explains how releases are built, signed and published.

> [!IMPORTANT]
> **Status:** PhotoCraft is in early alpha, and we want to be straight about where it stands: much of Photoshop's feature surface exists in some form, but **it is not yet a Photoshop replacement for daily professional work**. The biggest gaps are AI/generative features, about twenty missing tools, depth in typography and pro workflows, and plug-in compatibility. Every Photoshop menu item is wired to a command ([`docs/parity.md`](docs/parity.md)), but that measures wiring, not behaviour. The honest, dimension-by-dimension picture and where we're going next are in the [roadmap's parity assessment](docs/roadmap.md#honest-parity-assessment-2026-10-05). Expect rough edges, and please file issues (include your OS, document size, layer count and a screenshot). You can also tell us what broke on [Discord](https://discord.gg/artcraft).

## Documentation

Developer, architecture, automation, format, and security documentation is maintained in the [PhotoCraft documentation book](book/).

## Security

Security architecture, threat modeling, parser hardening, fuzzing, and vulnerability reporting are covered in the [security documentation](book/src/security/) and the repository [security policy](SECURITY.md).

## Test corpora

PhotoCraft is tested against real files: our own Photoshop-authored oracle PSDs in
[photocraft-corpus](https://github.com/storytold/photocraft-corpus) plus the psd-tools, ag-psd and PngSuite sets, pinned and
sha256-verified. Fetch them with `cargo xtask corpus --all` and run the tests with
`cargo xtask test-corpus` (details in [docs/development.md](docs/development.md#test-corpora)).

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

Every artwork shown is in the public domain (Wikimedia Commons, NASA, U.S. National Archives); sources are listed in [`docs/images/SOURCES.md`](docs/images/SOURCES.md).

The ArtCraft name, wordmark and logos in [`docs/brand/`](docs/brand/) are trademarks of the
ArtCraft Team and are not covered by this license. They may be used only unmodified, and only as
part of this repository and PhotoCraft, under [`docs/brand/LICENSE-brand.txt`](docs/brand/LICENSE-brand.txt).
Forks and modified versions must remove them.

<sub>Adobe, Photoshop, Illustrator, Premiere Pro, Lightroom, Acrobat, After Effects and InDesign are trademarks or registered trademarks of Adobe Inc. in the United States and/or other countries. PhotoCraft is an independent, open-source project and is not affiliated with, sponsored by or endorsed by Adobe Inc.; these names are used only to describe the workflows it is compatible with.</sub>

<p align="center">
  <br>
  <sub>Made by the <a href="https://getartcraft.com/">ArtCraft</a> team and community.</sub>
</p>

## Star history

[![Star History Chart](https://api.star-history.com/svg?repos=storytold/photocraft&type=Date&legend=top-left)](https://www.star-history.com/?repos=storytold%2Fphotocraft&type=date&legend=top-left)
