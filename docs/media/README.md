# Presentation media

These images show the actual hosted PhotoCraft Studio interface with an original
starter design. They contain no account identifiers, private documents or ArtCraft
brand artwork. Licensing and underlying asset credits are in [ATTRIBUTION.md](../../ATTRIBUTION.md).

| File | Purpose |
|---|---|
| `editor.png` | Unmodified 1440 × 960 screenshot of the native editor and Layers panel |
| `workspace.png` | Unmodified 1440 × 960 screenshot of the signed-out workspace |
| `native-editing-demo.gif` | Seven-second, 1200 × 800 loop: native layer move, Undo, Redo |
| `social-preview.png` | Original 1280 × 640 card with the real editor screenshot and a plain-text Tofu acknowledgment |
| `deployed-on-tofu.svg` | Original deployment badge linked to Tofu; not an official Tofu logo |

Captured on 2026-10-08 in a fresh Chrome 154 guest context at device scale 1.
The browser loaded the deployed WebAssembly artifact with SHA-256
`9f7d4c646abeb1b13f4f502040757c1ef5312c800773713d97b250349fe06c19`.
The capture clicked the existing “What comes next” template and used PhotoCraft's
original command protocol to move “Orbit two” and invoke Undo and Redo. PNG exports
confirmed that Undo restored the original pixels and Redo restored the moved layer.
The browser was restricted to same-origin GET/HEAD requests; no cloud project,
account or invitation was created.

The GIF encodes actual captured frames with their observed timing, sampled at five
frames per second. It is a demonstration of native editing, not a collaboration
or latency benchmark. The README keeps the still image visible and the animation
inside an optional expandable section.

Keep the social card below GitHub's 1 MB limit. Once the repository is eligible,
upload it through **Settings → Social preview**; committing the file does not
configure that setting. Replace captures when the visible product changes.
