# Security and public-release review

The original 2026-10-07 review covers the independent PhotoCraft Studio fork, based on `1d264da`, and the remediation changes committed with this document. The imported upstream reference is `3a3984075a1fd06d1af3e660aa376ee5368c4f73`. It includes source review, locally reachable Git history, synthetic local PostgreSQL/API/browser tests, packaging checks and bounded local backend benchmarks. Production attacks, production load, repository visibility changes and Git history rewrites are outside this run. The dated candidate addendum below records subsequent exchange changes; the original release evidence remains historical.

## Fixed findings

| Priority | Defect and impact | Remediation and evidence |
|---|---|---|
| P1 | Project access was checked before lock waits; a removed collaborator could still commit or mutate after removal completed. | Durable project writes acquire the common project barrier (exclusive for durable writes), then query current access. Controlled revocation-wait regressions cover saves, thumbnails, upload reservation/chunks, duplication and comments. |
| P1 | Cached authenticated identity survived queued logout/expiry, including secondary account/upload/quota locks. | The private session digest is retained only internally. Current session, email and membership are checked after waits with wall-clock expiry; account-only writes refresh their session too. Eight controlled expiry/logout scenarios reproduced before the fix. A serialization unit verifies that responses never include the digest. |
| P1 | Cloud commit accepted a native archive with missing referenced data; wire size did not bound expanded or decoded data. Parse/merge work also held database locks. | Reuse the original native loader with cloud-only ZIP/manifest/blob/layer/canvas and decoded-byte limits. Hash/parse/merge runs in one owned blocking lane outside durable locks, followed by fresh authorization, immutable declaration, revision and quota checks. Five archive HTTP regressions and native corruption/round-trip tests pass. |
| P1 | Sign-in held database transactions across two potentially slow provider calls and could starve the five-connection pool. | Readiness is checked first; provider HTTP holds no database connection. Account/session writes begin only after verified identity. Both stalled-provider phases leave ordinary APIs available in the regression. |
| P2 | Concurrent link rotation could retain multiple valid links; member/comment caps and invitation cooldowns were inconsistent or raceable. | Project/account/recipient barriers serialize the relevant changes. Existing members can be reinvited at capacity; unauthorized invitation error behavior is preserved. |
| P2 | Request futures and parser work lacked coherent overload bounds; returning response headers could release admission before the body finished. | Separate 256 ordinary, four authentication and two commit permits, bounded pool acquisition, absolute head/body deadlines, and body-owned permits. Blocking validation retains its permit after HTTP cancellation. Health checks bypass API saturation. These controls do not bound every TCP connection or total fleet memory. |
| P2 | Browser/native package notice trees omitted required attribution and accessible asset notices. | Preserve project and asset notices in web, generic, macOS and Windows packaging. Exact-byte notice fixtures and package-provenance checks cover the locally testable paths. Windows installer execution remains an external release gate. |
| P2 | A software WebGPU device could initialize and then fail before the first native frame, leaving a black workspace while the command bridge appeared ready. Startup errors were also inserted as HTML. | The browser reuses the existing WebGL2 renderer for software WebGPU adapters before canvas binding, preserves hardware WebGPU, and displays errors as text with a recovery action. Real-pixel startup regressions retain the original black-canvas failure; later hardware device loss remains a separate limitation. |
| P2 | Release stripping retained operator home paths in compiled WASM despite a clean source scan. | Reuse Trunk with standard rustc path remapping for repository, Cargo, Rustup, target and operator-home roots. The Tofu packager checks raw bounded WASM before creating or replacing an archive; synthetic and actual-old-artifact rejection tests preserve existing output. Remapping is best effort, so the artifact check remains required. |

A 128-request ordinary admission trial shed 131 requests at 100 simulated clients. The limit was adjusted to 256 using measured concurrency; the rerun completed 36,770 active requests with zero errors. This measured adjustment preserves a finite bound. It does not justify arbitrary pool or room-cap increases.

## Earlier RPC transport security snapshot — 2026-10-08

The receipts here identify the earlier `c0a89fbd` browser artifact. The later combined
Preferences/cursor-label build's [UI and local performance evidence](collaboration-performance.md#combined-browser-build-and-focused-profiles)
is recorded separately; neither set of local checks closes the publication, CI or hosted
acceptance boundaries below.

This candidate combines a live PUT acknowledgment with the authorized peer snapshot,
reducing separate browser read requests. The native editor, durable document format,
undo behavior, provider configuration and credentials are unchanged. Candidate
artifact identities are:

| Artifact | SHA-256 |
|---|---|
| Debug backend | `85de649cfcafb160eef07cd9de772f19b68566642735c4ba57e63d9784eeb0a1` |
| Optimized backend | `90908c3f4eedd41373a8916fddfe65e43b9710dac9439dab437d0042b5ddc090` |
| `photocraft-web-d3fc6e503acae4d9_bg.wasm` | `c0a89fbd41e0b9d00bebf4065e440a754083739e5a84acda93cf581a48abf1a1` |

The private `photocraft.exchange_live_v1` function is generated from the existing
authorization, write and peer SQL fragments in [live.rs](../apps/photocraft-cloud/src/live.rs).
It is `VOLATILE`, uses `SECURITY INVOKER` with `search_path = pg_catalog`, and revokes
execution from `PUBLIC`. All request values remain bound parameters. It rejects
unauthorized callers before acquiring room locks, then checks session, account,
membership, revision and lease eligibility again in a fresh statement after lock
acquisition. Lease eligibility uses a materialized wall-clock cutoff after the wait; session
expiry and returned TTLs also use wall-clock time, not the outer call's start time. The outer transaction commits
before an acknowledgment is returned; shared-lock admission failure still rolls
back before the exclusive retry. GET retains its explicit transaction, including
the SQLx unnamed Parse/Bind protocol boundary.

PUT returns peers from that same authorized post-lock statement, excluding its own
session/tab. Duplicate or older sequences do not renew leases. Existing origin,
expected-account, role, base-revision, room/session and payload bounds remain in
force. In the [browser adapter](../apps/photocraft-web/src/live.rs), project/account
generation changes discard stale replies, role downgrade stops gesture publishing,
and a stale-revision response triggers reconciliation without discarding a newer
gesture. Received leases subtract request time and remain capped at two seconds.
This does not make previously received data retractable after access is revoked,
or turn app sessions into a provider-account revocation feed.

Local candidate evidence is retained in the `2026-10-08-release-continuation`
artifact bundle:

- `rpc-candidate-binaries.json` identifies both backend binaries;
  `focus/receipt.json` records the unchanged browser hash. That earlier browser
  receipt used the preceding exchange backend and is not RPC browser acceptance.
- `api/receipt.json` and its logs record 41 API, 17 live-protocol and 15 lock/concurrency
  tests passing on the debug candidate. The regressions include queued revocation,
  logout and expiry, permission downgrade, post-lock lease/cap admission, private
  function privileges, replay without lease renewal, and a held row-write barrier
  proving the acknowledgment waits for commit and does not mix peer snapshots.
- `api/pool.json` records both API and live suites passing across 3,262 idle backend
  switches, with zero named parses and no SQLSTATE errors. This is a local protocol
  fixture, not hosted Supavisor acceptance.
- `security/receipt.json` records 41 additional passing checks: 19 security/auth
  mutation tests, five archive tests, 13 authentication-flow tests including the
  browser email-confirmation origin regression, and four cold-start tests. They
  used the exact debug candidate above; the browser hash was unchanged before
  and after. The owned server and disposable database were removed, with no
  shared rows touched. Provider and account fixtures remained local; this does
  not establish real inbox delivery or production sign-in acceptance.
- `rpc-rust-gates.json` records passing formatting, cloud units, strict Clippy and
  optimized build checks.
- `browser/receipt.json` records four renderer-startup, 50 full browser-journey and
  six focused live-exchange checks passing against the debug RPC backend and exact
  c0 browser artifact. This includes geometry, native editing/export/recovery, sharing,
  stale account/project responses, PUT-only peer rendering, legacy ACK fallback,
  queued stale-base errors and aged lease rejection.
- `rpc-profiles/paint.json` records 19 calibrated cursor/Pencil/reload observations
  passing the unchanged 500 ms / 15 ms gate, with maximum upper bound 316.380 ms.
  The optimized mixed-load evidence is scoped in the [scale review](scale-release-review.md);
  neither local lane certifies production capacity or universal operation latency.

These receipts establish the stated local source, security, browser and bounded
performance contracts only. Production 1,000-user capacity, hosted two-account
behavior, publication and CI readiness remain separate pending release gates.
Prior checks below are not automatically carried forward to these hashes;
local evidence alone does not establish deployment or public-release readiness.

## Historical local checks — 2026-10-07

The hashes and receipts in this section belong to the 2026-10-07 release. They are
retained unchanged and do not verify the 2026-10-08 candidate.

The 2026-10-07 debug backend SHA-256 is `53c0126e1b770778a8c81b2096679cfb5fb45a242ff15bdbc1c2fc1846efff91`; the optimized backend is `d46b760c283140d8bb7aee7c9939a04eb8fad7f4943caf528922c3c7637e3779`.
The browser artifact is `photocraft-web-3e8018e55ab85877_bg.wasm`, 25,982,620 bytes,
SHA-256 `6ecd9aaeebb452aae091f360a79963bedea98c673adc7c544e81d1c9bf8432cb`.

- 21 cloud units and strict cloud/format Clippy pass; workspace formatting passes.
- All 80 native-format tests pass, including atomic saves, autosave, corruption, hash validation, all-mode round trips and decoded-byte accounting.
- The native corpus gate passes 1,698 tests in 59 groups, with zero failures and 14 existing ignored tests. Layering passes for 29 crates; all 23 WASM package/feature checks and the generated scorecard check pass.
- 19 security, 41 existing API, five archive, 13 authentication and four cold-worker tests pass on the final debug backend.
- Chromium and WebKit submit the actual email-confirmation form without an injected Origin header; verified identity, token privacy and replay rejection pass. The provider is simulated on loopback; no inbox delivery is claimed.
- Packaging provenance, publication fixtures and benchmark safety guards have separate evidence. A missing PowerShell runtime is an explicit skip, not Windows execution evidence.
- The transaction-pool protocol fixture passes all 41 API and 12 live tests across 2,917 idle backend switches, with no named prepared statements or SQLSTATE errors. This fixture is not a Supavisor emulator or hosted-cause diagnosis.
- Ten build-privacy fixtures pass, covering Cargo flag semantics, path remapping and package rejection before an existing ZIP can be overwritten.
- Four focused renderer-startup journeys pass on the final browser artifact: bundled-browser default, explicit WebGL, hardware WebGPU, and safe initialization-error recovery with real pixels, keyboard focus and narrow-screen geometry.

The final 20 live/concurrency tests pass. All 50 bundled-browser journeys pass in
647.780 seconds on the frozen artifacts above, including two-account merge,
permissions, invitation response handling, save/reopen, lost acknowledgments,
bounded recovery and session renewal. Earlier failed attempts are retained:
successful shared-project setups now wait for the actual native dimensions,
layer identities/names and active layer before injecting later faults. Intentionally
delayed or denied opening tests keep their original boundary. No runtime or timing
threshold was changed for this fixture correction. The optimized TLS load results
are in [the scale review](scale-release-review.md); the 1,000-client target is not
met. No code-coverage percentage or production security certification is inferred
from test counts.

The final hardware-browser collaboration lane passes three regressions and all 19
input-to-observed-pixel samples using two independent synthetic accounts on loopback.
Installed Chrome 154.0.8037.98 uses the native WebGPU backend, with two visible
1440 × 960 clients on macOS 26.5.1 / arm64. The six ordinary samples per configured
HTTP-delay profile have the following nearest-rank statistics, including clock
uncertainty in the upper bound:

| Minimum request delay | Upper-bound p50 | Largest upper bound |
|---|---:|---:|
| 0 ms | 104.581 ms | 147.038 ms |
| 50 ms | 186.276 ms | 205.562 ms |
| 100 ms | 200.515 ms | 274.060 ms |

The additional reload sample has a 132.266 ms upper bound. Measurement ends at a
matching CDP-swapped PNG, not a physical display. These small samples cover cursor
input and a supported native Pencil preview, not every editor operation or tail
reliability. The delay is a CDP HTTP simulation, not measured Internet latency.
No owned builds or load tests ran concurrently; ordinary macOS and unrelated user
work remained active. The bundled software-renderer timing run and one bounded
repeat produced matching pixels but exceeded the unchanged 15 ms clock-uncertainty
limit (29.58 ms and 17.283 ms). Both remain invalid timing evidence rather than
passes or demonstrated latency failures. Full functional browser acceptance uses
the bundled browser independently of the hardware timing lane.

The earlier `3c65885b` browser artifact passed its functional checks but was held
when byte-level package inspection found operator home paths in compiled WASM.
The remapped artifact above has zero home-path matches; every browser and hardware
timing gate was rerun against its exact hash. Earlier receipts remain retained as
superseded evidence rather than being attributed to the replacement artifact.

## Publication boundary from the earlier review

Gitleaks inspected 412 reachable commits and found zero secrets before remediation. A separate object scan found eight historical restricted brand paths, three home-path-containing blob revisions across two repository paths, and 53 author email identities requiring manual review. The current restricted artwork was already removed; the current fork checkpoint path is now redacted. History remains unchanged.

The artwork and one historical home-path-containing file originated in already-public upstream commits. The other affected path is a fork-only checkpoint. Preserving exact upstream ancestry therefore also preserves those original objects. An owner-approved selective rewrite can remove fork-only privacy material while retaining upstream ancestry; a fully object-clean history requires a different publication/history strategy and update boundary. Do not silently waive the gate, rewrite upstream identity or publish private fork paths.

`packaging/web/check-publication.py` reports counts and fails closed on retained restricted artwork or home paths. Author consent, unavailable refs, issues and external services need separate review. The repository remains private pending this decision.

The README credits upstream prominently and states the fork is independent. NOTICE preserves original copyright and adds a fork modification notice; modified upstream text files carry change notices. The code map separates original crates from added adapters. Asset license terms remain separate from the MIT OR Apache-2.0 code choice. Tofu is acknowledged as the deployment platform, with no sponsorship implication.

## Dependencies, CI and remaining gates

The locked advisory review found optional `rsa 0.9.10` matching [RUSTSEC-2023-0071](https://rustsec.org/advisories/RUSTSEC-2023-0071.html), with no active cloud dependency path, and the compile-time unmaintained `paste 1.0.15` advisory [RUSTSEC-2024-0436](https://rustsec.org/advisories/RUSTSEC-2024-0436.html). The recorded graph/reachability distinction must be rechecked each release. It is not a blanket vulnerability waiver.

Privileged release/update actions and the web acceptance actions are pinned to verified immutable commit IDs. The two FreeBSD bootstrap commands now download the exact rustup 1.28.2 `x86_64-unknown-freebsd` installer and verify its pinned SHA-256 before execution. Its downloaded bytes matched the [official archive checksum](https://static.rust-lang.org/rustup/archive/1.28.2/x86_64-unknown-freebsd/rustup-init.sha256); no installer was executed during this verification. The downloaded file retains the `rustup-init` basename required by [rustup's installer dispatch](https://github.com/rust-lang/rustup/blob/1.28.2/src/bin/rustup-init.rs#L85-L109), inside an owned temporary directory removed on exit. Synthetic workflow checks require that basename and prove a checksum mismatch or failed download prevents invocation. This pins the bootstrap, while the existing `stable` Rust toolchain selection remains unchanged. Some upstream nonprivileged jobs and version-only build-tool downloads remain hardening work. Earlier FreeBSD jobs had no executed steps because of the account's payment/spending restriction. The later 2026-10-08 run at `f707cdc` executed but exhausted its host disk; the browser acceptance run at that same head executed and failed the transaction-pool fixture. The fixture transport and disposable-runner disk changes require fresh exact-head CI. No billing setting was changed. GitHub CI and real Windows/macOS release artifacts, including their native dependency/runtime notice inventories, must pass before claiming those platforms release-ready.

The new [exact-package container gate](upstream-updates.md#exact-package-container-gate)
requires a fresh Linux image build, verified database TLS, trusted API/live/lock/handoff suites and
a matching receipt before automated promotion. Actual Docker execution remains unverified:
the `f707cdc` container job was skipped after the preceding acceptance failure. Mocked
guard tests and local backend/browser passes must not be reported as a container pass. The first run also requires the reviewed helper
and workflows to exist in the trusted base/caller revision. Hosted readiness and later
Tofu image rebuilds remain separate checks.

The browser/cloud resolved Rust graphs now carry a verified notice bundle: 322 distinct external packages, 633 files, including nested egui fonts and 23 exact-commit public supplements. Repeated generation is byte-identical and missing/tampered text blocks packaging. The bundle records target graphs, Cargo.lock hash, file hashes and authentic copyright excerpts; build dependencies are conservatively included. It is not a linker census.

The separate runtime bundle contains 13 files (501,723 bytes), preserving Rust 1.95.0 standard-library notices, compiler-builtins/libm licenses and pinned LLVM compiler-rt credits. The actual WASM producer and authentic text hashes are checked offline; the Docker build rejects a mismatched Rust release/source commit. Six runtime-notice regressions pass. Full toolchain redistribution, Debian base-image/system-library inventories and native installers remain separate distribution scopes.

Provider-wide abuse controls, actual database/worker quotas, full RSS/slow-consumer limits, CSP compatibility, provider-account revocation mirroring, real invitation receipt and hosted two-user edit-to-render latency remain distinct acceptance items. Existing opaque app sessions last seven days; this review does not add a provider revocation feed. Supported live previews still have room/session/event bounds and do not provide general simultaneous editing or collaborative undo.

## Source publication preparation — 2026-10-08

This preparation changes publication tooling, tests, workflows and documentation.
The Rust editor, HTTP service, renderer, assets and deployed browser build from
`8177c2a` are unchanged. It is preparation for an early-alpha source release,
not a new production-capacity or simultaneous-editing claim.

Independent review found two weaknesses in the publication scan. Candidate-owned
Gitleaks configuration and ignore files could hide a finding, and ordinary Git
patch output omitted content introduced only by a merge resolution. The trusted
checker now scans Git metadata with embedded rules, an owned empty ignore file,
disabled inline exclusions and explicit raw merge diffs. External diff drivers
and text conversion are disabled. Ten actual-scanner regressions exercise these
boundaries, including linked worktrees and exact-commit scope. Preparation and
promotion check the candidate before remote writes; the candidate cannot replace
the preloaded checker.

The scanner installer accepts only checksum-pinned Gitleaks 8.30.0 Linux x64
release bytes, validates the archive and installs one regular binary into a new
owned directory. CI checks full candidate ancestry from a complete checkout.
The nfpm, Trunk and automatically downloaded AppImageTool binaries now have
verified release checksums before installation or execution; cached AppImageTool
bytes are verified too. Explicitly supplied or preinstalled tools remain the
operator's responsibility. Privileged external Actions already use full commit
IDs, verified against their official repositories.

The combined local publication, upstream-update, installer and discovery suite
passed 43 cases, with one PowerShell-only case skipped on macOS. The current
backend passed 41 authentication, authorization, archive and startup checks, and
seven live-handoff checks. Disposable workers and databases were removed without
changing shared rows. These results are local evidence; exact-source GitHub and
Linux container execution remain separate gates.

An isolated history-filtered copy preserves the current source tree and contributor
identities while excluding restricted historical artwork, temporary test output
and personal path prefixes. The proposed publication uses a fresh repository and
keeps the original repository, objects and Actions history private for recovery.
No original repository history or visibility has been changed by this preparation.
The final copy must pass its own full-history gate before publication; the owner
must approve the repository transition. Automatic upstream updates remain disabled
until a sanitized import path is implemented and reviewed, as described in the
[update guide](upstream-updates.md).

A successful local request benchmark is not a 1,000-user whole-application guarantee. Use errors, successful-request latency, scheduling lag, completion ratio, per-client progress, database contention and bytes together. Preserve every failed stage. The hosting deployment and a normal live journey require separate recorded evidence.
