# Presentation media

These captures show PhotoCraft's actual browser editor with original starter art.
They contain no personal account identifiers, private documents or ArtCraft brand
artwork. Credits and licenses are in [ATTRIBUTION.md](../../ATTRIBUTION.md).

| File | Purpose |
|---|---|
| `editor.png` | Unmodified 1440 × 960 screenshot of the native editor and Layers panel |
| `workspace.png` | Unmodified 1440 × 960 screenshot of the signed-out workspace |
| `editing-walkthrough.mp4` | Continuous native template, text, Move, Undo/Redo and layered file download/reopen journey |
| `collaboration-demo.mp4` | Two independent local accounts: real cursors, bidirectional Pencil/Move previews, saved results and reload |
| `collaboration-preview.png` | Still frame from the collaboration video |
| `social-preview.png` | Original 1280 × 640 card with a real editor screenshot and a plain-text Tofu acknowledgment |
| `deployed-on-tofu.svg` | Original deployment badge linked to Tofu; not an official Tofu logo |

## Capture and scope

Recorded on 2026-10-08 in fresh Chrome 154 contexts at device scale 1. The still
screenshots show the hosted guest editor. The videos use the same Rust/WebAssembly
editor locally; the collaboration video uses an owned worker, a disposable database
and synthetic Editor A/B accounts. It does not demonstrate email delivery, hosted
two-person acceptance, internet latency or unrestricted simultaneous editing.

Playwright records actual browser frames at 25 fps. The editing journey uses the
original template, Type and Move tools, keyboard Undo/Redo and original file picker.
The collaboration scene is prepared through the original command bridge; its
recorded strokes and moves use actual pointer events. No synthetic cursor, painted
HTML canvas replacement, accelerated footage or interpolated action is added.

FFmpeg encodes the captured footage as H.264 MP4 with original timing. Presentation
labels sit outside the captured canvas. The collaboration composition crops the
two real canvases side by side and labels its local test scope. Their alignment
uses page-creation clocks as an editing guide, not frame-accurate network timestamps.
The public page uses native Play/Pause controls, no autoplay, and `preload="none"`.

Use a configured local HTTP service for editor recordings, including guest captures.
Wait for real configuration and the guest account response, then verify the settled
workspace visually before filming. A static fixture that declares `cloud: false`
correctly produces a setup notice and does not represent a configured workspace.

## Verification

Review complete playback, including every held drag and preview-to-save handoff.
Stills, file duration and a passing saved document cannot establish smooth motion.
Check the actual decoded MP4 as well as the raw recording. Keep PNG export checks
outside the recorded interval so their flattening notices do not cover the interface.

The operator checks verify unchanged native state while remote previews are held,
saved native archive SHA/CRC, receiver reload and complete RGBA equality. The editing
file reopens with all nine layers and the same pixels. Source hashes, scene times,
failures and cleanup receipts remain in ignored verification outputs; generated
accounts, cookies and raw customer data are never presentation assets.

Browser regressions separately check opted-in decoding, advancing playback time,
Pause, responsive controls and no account/API requests on the public presentation.
The [verification retrospective](../web-verification-retrospective.md) records the
held-Move and saved-preview handoff regressions discovered during this review.

Keep the social card below GitHub's 1 MB limit. Once the repository is eligible,
upload it through **Settings → Social preview**; committing the file does not
configure that setting. Replace captures when the visible product changes.
