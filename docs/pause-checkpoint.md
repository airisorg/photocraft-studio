# PhotoCraft Studio pause checkpoint — 2026-10-07

The owner requested that the current work be committed, merged, deployed, and then paused.
This checkpoint preserves the accepted implementation and the remaining work. It is not
a claim of complete Figma/Canva parity or exhaustive verification of every native feature.

## Accepted implementation

The original PhotoCraft Rust editor, document model, commands, native file format and
renderers remain the foundation. Cloud and browser behavior lives in the existing adapters;
there is no replacement editor or additional frontend framework.

The current release includes the workspace/header/dialog refinements, project menus and
incomplete-save recovery, scoped invitation feedback, transaction-pool compatibility,
uncertain-save reconciliation, document/request identity protection, and the one-entry
latest-visited browser recovery policy. Old recovery rows are physically evicted within
each account/guest scope; cloud projects and saved versions are separate.

Session expiration now pauses authenticated requests without closing native documents or
discarding their local edits. Sign in again opens a separate sign-in tab; Check sign-in
resumes only the original account and refreshes current project permissions. The server
checks the expected-account header before authenticated operations. Switching the shared
cookie cannot silently save the original account's work under another identity. Confirmed
merged-save acknowledgments remain available across expiration, and rejected logout
preserves the original editor state.

## Release evidence

- The complete 50-case browser suite passed in 675.501 seconds on the frozen
  runtime and test files. Focused cases 48–50 also passed together in 52.195 seconds.
- Before the email-confirmation correction below, the service passed 41 API cases,
  12 auth cases, 4 startup cases, 8 Rust unit tests,
  and strict all-targets Clippy. The same 41 API cases passed through the rotating-backend
  fixture, with 2,427 idle backend swaps and no SQLSTATE errors. That fixture is a local
  protocol fault model, not actual production Supavisor.
- The frontend passed strict WASM Clippy and an optimized release build. The seven
  release-provenance cases passed on the unchanged packaging implementation.
- The accepted WASM SHA-256 is
  `32a4df5f348bc0f1f7bae5baccdd488a62ce382b442012c80f67c8db2485779a`.
  The local test-service hash is
  `7019cc21bb90565f8081039fd0df18216ae6938400b4e9774ddc57c6dde7189a`.
  Production builds the service for Linux from the package's tracked Rust source.
- `release-source.json` inside the final ZIP names its clean source commit and exact WASM.
  Deployment selection, public reachability, hosted fingerprint and journey results are
  recorded separately after publication; local passes do not establish hosted behavior.

On this Mac the evidence is under
`$WORKSPACE/outputs/verification/`:
`2026-10-07-spacing/session-recovery/` contains full50 logs, screenshots and before/after
hashes; `2026-10-07-session-recovery/` contains service checks; and
`2026-10-07-final-release/` contains the final package/deployment/hosted acceptance record.
These generated artifacts and credentials are excluded from the source deployment.

Browser coverage includes rendered header alignment, hover/pressed/focus/reduced-motion
states, narrow layouts, native dialog cancellation, DPR changes, renderer choices, actual
native file/PNG/PSD round trips, physical IndexedDB eviction, request supersession, failure
retries and account/permission boundaries. WebKit automation is not physical Safari or
iPhone acceptance. Local simulated identities do not prove real invitation delivery or
two-person hosted collaboration. Full cross-platform native CI is not established here.

## Email confirmation correction before pause

The owner reported an origin error after clicking Continue from an invitation link.
Both Chromium and WebKit reproduced a browser-generated `Origin: null` and HTTP 403
against the frozen old service. The confirmation page's `no-referrer` policy caused it;
the earlier HTTP test supplied `Origin` manually and missed the actual browser transition.

This correction uses `strict-origin` only on `/auth/confirm`, in the HTML and both header
layers. The token path/query stay out of `Referer`; the exact-origin guard is unchanged.
The actual rendered-form journey now runs in both engines as a mandatory part of
`python tests/web/test_auth.py`, including session identity, scanner-safe GET, single-use
replay rejection and missing/null/foreign-origin denial.

The corrected candidate passed all 50 browser cases in 683.564 seconds, 13 auth cases,
41 API cases, 4 startup cases, 8 Rust unit cases, 7 source-provenance cases, formatting
and strict all-targets cloud Clippy. Before/after runtime/source/test hashes matched.
The local corrected service SHA-256 is
`a0730644fe554733fae9d255c9d8eed079d69826ac699dea6bc8dfae6f87c106`.
The previously accepted WASM is unchanged. Production compiles Linux service code from
the release source; this Mac binary hash does not identify that Linux executable.

Evidence is under `outputs/verification/2026-10-07-auth-confirm-origin/` in the workspace.
Its final release record separately names the published source/deployment and hosted
confirmation-policy check. That check deliberately submits an invalid synthetic token
to exercise origin validation without redeeming an invitation or contacting the provider.
Real inbox delivery, Google return and independent hosted collaboration remain distinct
acceptance boundaries. The retrospective now maps these gaps and requires UI transitions
to be tested directly rather than replaced by headers, cookies or delivery fixtures.

## Remaining work when development resumes

1. Verify the real invitation email journey and two independent controlled hosted identities:
   receipt, sign-in, open, edit, reload, comments, revocation, reconnect and conflict recovery.
   Mike's exact approved address and a second real identity are still missing. No inbox
   delivery claim follows from provider acceptance or local fixture tests.
2. Rebase and validate the unapplied comments submission draft before use. It is preserved at
   `outputs/paused-work/comment-submission.patch` and
   `outputs/paused-work/comment-submission-regression-plan.md` in the workspace. It is not
   compiled or deployed. Duplicate submits, successful POST plus failed refresh, and
   ambiguous acknowledgment require distinct tests before shipping.
3. Continue the remaining original-feature and UI-state inventory from
   [verification retrospective](web-verification-retrospective.md),
   [native scorecard](scorecard.md), and [workspace design contract](workspace-design-contract.md).
   Check actual reference states and complete journeys; consistency or a passing geometry
   assertion alone does not prove optimal UX.
4. Develop operation-level collaboration only after its semantics and measurement are defined.
   Current collaboration saves native snapshots after 3.5 seconds idle and polls every
   1.5 seconds. It supports roles, presence names, comments, versions, public view links and
   conservative manifest merging. It does not provide live cursors/selections, incremental
   operation sync, collaborative undo or Figma-level same-object concurrency.
   See [architecture](collaboration-architecture.md) and
   [local performance evidence](collaboration-performance.md). No hosted p50/p95/p99
   edit-to-remote-render latency or capacity ceiling has been established.
5. Activate upstream releases through the existing guarded workflow rather than bypassing it.
   GitHub Actions runners are blocked by account billing/spending restrictions. After a
   passing workflow creates `tofu-release`, the owner must grant GitHub access through the
   existing Tofu app's Update code flow and enable updates for that branch.
   The pipeline is configured, awaiting activation. See [upstream updates](upstream-updates.md).
6. Complete physical-device and broader accessibility acceptance. Local GPU rendering follows
   the original rendering policy; CPU composition still needs a graphics context for the
   browser UI. No separate native-compute bridge has been added.

Resume with reliability and UI defects first, then the real two-account hosted journey.
Keep the accepted release serving while the next candidate is developed and tested in isolation.

Local evidence paths use `$WORKSPACE` for the local evidence workspace and
`$CHECKOUT` for the source checkout; neither names a contributor’s home directory.
