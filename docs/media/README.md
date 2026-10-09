# Presentation media

These captures show PhotoCraft’s actual browser editor. The Night Shift / Roller
Club poster is original demo artwork: its skate illustration, type, colors and
fictional event copy were created with the native editor. Credits and licenses
are in [ATTRIBUTION.md](../../ATTRIBUTION.md).

| File | Purpose |
|---|---|
| `editor.png` | Unmodified 1440 × 960 local native editor screenshot with the finished 32-layer poster |
| `workspace.png` | Unmodified 1440 × 960 hosted signed-out workspace screenshot |
| `editing-walkthrough.mp4` | 48-second native Type, Move, Pencil, Undo/Redo and layered file save/reopen journey |
| `collaboration-demo.mp4` | 53 seconds with two local accounts contributing typography, composition and hand-drawn details |
| `collaboration-preview.png` | Settled frame from the collaboration film, after reload |
| `social-preview.png` | Original 1280 × 640 card with the real editor screenshot and a plain-text Tofu acknowledgment |
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

The new editor screenshot and films were captured on 2026-10-09 UTC in fresh
Chrome 154 contexts at device scale 1. The workspace screenshot retains its
2026-10-08 hosted guest provenance. The films use the same Rust/WebAssembly editor
locally. Collaboration uses an owned worker, a disposable database and synthetic
Maya/Leo accounts. It does not demonstrate email delivery, hosted two-person
acceptance, internet latency or unrestricted simultaneous editing.

The editing film starts with a prepared original layered draft. It changes LATE
to NIGHT using Type, moves a coral sparkle to balance the composition, draws two
speed trails and two glints with Pencil, then exercises Undo/Redo and reopens a
real 32-layer `.pcraft` file. This custom draft is not a bundled app template.

In the collaboration film, Maya supplies typography and the recorded Move; Leo
supplies the recorded Pencil strokes. Typography arrives as saved versions.
Supported Pencil and Move previews appear in the other view while its native
base and cloud revision remain unchanged, followed by canonical saved results.
Leo reopens the project and both native PNG exports match byte for byte. Two-way
contribution is shown, but each tool is not demonstrated in both directions.

Playwright records actual browser frames at 25 fps. Recorded Type changes use
trusted keyboard input, and strokes and moves use trusted pointer input. Draft,
view and layer setup and native save/open commands use the editor’s existing
command bridge. Save/reopen is real; the films do not show manual File-dialog
navigation. No imitation canvas, fabricated cursor or replaced artwork pixels
were added. The event and venue are fictional; no third-party art or music is used.

FFmpeg encodes H.264 MP4 at original speed. Presentation labels sit outside the
native artwork. The 1440 × 1080 editing frame reserves space for captions; the
1920 × 960 collaboration frame places two native views side by side with complete
File menus, tool rails, document tabs and poster footers. Capture phase clocks
help align presentation; they are not frame-accurate network measurements.
Genuine loading/reopen transitions remain visible with captions that distinguish
action in progress from completion. The public page uses native Play/Pause
controls, no autoplay, and `preload="none"`.

Use a configured local HTTP service for editor recordings, including guest
captures. Wait for real configuration and the guest account response, then verify
the settled workspace visually before filming. A static fixture declaring
`cloud: false` produces a setup notice rather than a configured workspace.

## Verification

Review the full decoded film as well as the raw recording, especially held drags,
preview-to-save handoffs and reopen transitions. Stills, duration and a passing
saved file alone cannot establish smooth motion. Keep PNG export checks outside
the recorded interval so flattening notices do not cover the interface.

Operator checks verify unchanged receiver native state during held previews,
saved native archive SHA/CRC, receiver reload, exact exported PNG equality and
32 editable layers. Independent review decoded all 1,200 editing and 1,325
collaboration frames and checked the Move handoff without an old-position return.
The editing file also reopens with all 32 layers and matching layer content. Raw footage,
source hashes, diagnosed failed takes and cleanup receipts remain in ignored
verification outputs. Generated accounts and cookies are not presentation assets.
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
held-Move and saved-preview handoff regressions discovered during earlier review.

Keep the social card below GitHub’s 1 MB limit. Upload it through **Settings →
Social preview** when the repository is eligible; committing it does not configure
that setting. Replace captures when the visible product changes.
held-Move and saved-preview handoff regressions discovered during this review.

Keep the social card below GitHub's 1 MB limit. Once the repository is eligible,
upload it through **Settings → Social preview**; committing the file does not
configure that setting. Replace captures when the visible product changes.
