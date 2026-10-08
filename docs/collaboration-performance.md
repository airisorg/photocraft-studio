# Collaboration latency measurements

This benchmark measures the local HTTP adapter and PostgreSQL path. It does not establish
hosted capacity, maximum collaborator counts, browser interaction latency, or a production
service-level objective. See [collaboration architecture](collaboration-architecture.md)
for the transport, current limits, and optimization decisions.

## Reproducible bounded benchmark

`tests/web/benchmark_collaboration.py` starts its own local service on an ephemeral port;
it does not share the browser acceptance server. Both the database and HTTP destination
are restricted to loopback. Database URL query overrides and HTTP redirects are rejected,
and the HTTP client ignores proxy environment variables. Provider configuration is disabled;
no invitation or external email request is made.

The run creates 20 unique synthetic accounts, sessions, and their own projects. Its cleanup
deletes only projects owned by those account UUIDs, those sessions, and those accounts,
including after a failed measurement. A completed report confirms the accounts were removed.
Use only the disposable test database; a loopback address is not proof that an unrelated
database is disposable.

Presence measurements use 1, 5, and 20 distinct signed-in accounts with access to one saved
native document. Each case uses two unmeasured warmup rounds followed by 10 measured rounds
(the CLI permits 10–20). Requests start together within each round, with 1.5 seconds between
round starts. This is a short, bounded polling scenario, not saturation or a throughput test.
The response must retain all active benchmark accounts and the committed revision.

Save measurements use the unchanged Rust-generated native fixture and the real bundled
“Make some noise” template. One warmup save precedes 10 measured sequential saves per file.
The report includes dimensions, byte count, chunk count, and SHA-256. It separates upload
creation, chunk transfer, commit, total save, and a second account's metadata-plus-content
open. Every reopened file must exactly match the original native bytes. These durations
exclude native document serialization, browser decoding/rendering, autosave delay, and the
peer's polling delay.

```sh
cargo build --locked -p photocraft-cloud
cargo run --locked -p photocraft-cloud --example fixture -- /tmp/photocraft-benchmark.pcraft
python tests/web/benchmark_collaboration.py \
  --binary target/debug/photocraft-cloud \
  --fixture /tmp/photocraft-benchmark.pcraft \
  --rounds 10 \
  --output test-results/collaboration-benchmark.json \
  --context 'Browser acceptance and builds stopped; other host activity recorded separately'
```

Use the Python environment from `tests/web/requirements.txt`. The default database is the
existing disposable `photocraft_test` PostgreSQL service on port 55438. Override it only
with another disposable loopback database via `PHOTOCRAFT_TEST_DATABASE_URL` or `--database`.
The benchmark itself starts and stops the service process; it does not start PostgreSQL.

The JSON artifact retains every timing sample, environment, CPU count, load averages,
binary hash, start/end timestamps, request count, and cleanup result. Percentiles use the
nearest-rank method. With 10 samples, p95 and p99 are the maximum; these are observations,
not reliable estimates of a long-running tail. Run-to-run comparisons need matching binaries,
fixtures, build profiles, machines, cache conditions, and background work.

## Observed local HTTP sample — 2026-10-07

This **contended local sample** ran from 21:35:34 to 21:36:24 UTC on macOS 26.5.1 ARM64,
12 logical CPUs, Python 3.14.3, the debug service binary (workspace dev opt-level 1,
dependencies opt-level 2), and loopback PostgreSQL. Full browser acceptance ran concurrently;
the coordinating task reported no current Rust build, and other user activity was unknown.
The 1/5/15-minute host load averages were 4.02/9.36/14.41 at start and 4.50/8.65/13.86 at end.
This is not an uncontended baseline, hosted benchmark, capacity finding, or latency SLO.

| Presence clients | Measured requests | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| 1 | 10 | 2.978 ms | 5.031 ms | 5.031 ms |
| 5 | 50 | 3.707 ms | 6.196 ms | 7.926 ms |
| 20 | 200 | 8.957 ms | 12.020 ms | 13.160 ms |

Each file used one chunk, one excluded warmup save, and 10 measured sequential saves.
The synthetic fixture was 64×48, 1,586 bytes. The bundled “Make some noise” template was
1080×1350, 245,446 bytes. HTTP peer-open includes metadata and all file chunks, with
byte-for-byte verification; it does not include native decoding or browser display.

| Document / operation | p50 | p95 | p99 |
|---|---:|---:|---:|
| Native fixture — upload creation | 1.309 ms | 1.820 ms | 1.820 ms |
| Native fixture — chunk transfer | 1.134 ms | 1.346 ms | 1.346 ms |
| Native fixture — commit | 2.095 ms | 2.458 ms | 2.458 ms |
| Native fixture — total save | 4.506 ms | 5.345 ms | 5.345 ms |
| Native fixture — peer HTTP open | 2.107 ms | 2.362 ms | 2.362 ms |
| Make some noise — upload creation | 1.596 ms | 1.995 ms | 1.995 ms |
| Make some noise — chunk transfer | 4.500 ms | 5.125 ms | 5.125 ms |
| Make some noise — commit | 7.158 ms | 7.753 ms | 7.753 ms |
| Make some noise — total save | 13.200 ms | 14.233 ms | 14.233 ms |
| Make some noise — peer HTTP open | 6.210 ms | 6.630 ms | 6.630 ms |

All 452 HTTP requests, including setup and warmups, succeeded. All 20 synthetic accounts
and their owned data were removed. Raw samples and cleanup evidence are in the local artifact
`outputs/verification/2026-10-07-spacing/collaboration-benchmark-contended.json`.
Service binary SHA-256: `553f18ffebe602319e84503f071dd896e069aab81833e554c73fdf632990758c`.

The result shows this small warm local scenario completed successfully through 20 concurrent
presence clients. It does not determine a maximum collaborator count, behavior on large files,
or hosted autoscaling behavior. No runtime optimization was applied for this measurement.

## Observed two-browser autosave sample — 2026-10-07

`tests/web/benchmark_browser_collaboration.py` reuses the existing browser acceptance helpers
against the already-running loopback service. It seeds two distinct synthetic accounts,
creates a 320×240 document with one background layer, manually saves once, grants the second
account edit access, and opens the saved project in an independent browser context. Ten
sequential layer renames then use only automatic autosave. Each sample verifies the exact
layer ID/name at the peer and the expected committed cloud revision; the final revision is 11.

Timing starts immediately before the owner invokes the native rename command. The commit
metric ends when Playwright delivers the successful commit HTTP response to the observer.
The peer metric ends when a 100 ms `ui.inspect` polling loop first reads the expected native
document state. Driver scheduling and polling add observation delay. This measures neither
remote canvas paint nor a physical user's keystroke-to-pixel latency. Screenshots retain the
final state of both contexts; they are not timing probes.

```sh
python tests/web/benchmark_browser_collaboration.py \
  --origin http://127.0.0.1:8876 \
  --output test-results/browser-collaboration-benchmark.json \
  --context 'Describe concurrent browser/build/user activity here'
```

The observed run was 21:39:21–21:40:16 UTC, with concurrent full browser acceptance and
unknown other user activity on the same macOS 26.5.1 ARM64 host. Chrome was 154.0.8037.98;
the loaded assets were `photocraft-web-19e0f475ee2a540a.js` and
`photocraft-web-19e0f475ee2a540a_bg.wasm`. The initial native file was 4,769 bytes. Host
1/5/15-minute load averages were 5.45/7.71/12.49 at start and 5.75/7.45/12.12 at end.

| Observed interval | Samples | p50 | p95 | p99 |
|---|---:|---:|---:|---:|
| Edit → automatic commit acknowledgement | 10 | 3,636.067 ms | 3,662.827 ms | 3,662.827 ms |
| Edit → peer native document state | 10 | 4,645.618 ms | 4,683.951 ms | 4,683.951 ms |

All ten edits converged to the exact expected layer names without an extra manual save or
duplicate commit. Both synthetic accounts and their owned data were removed. The raw report
is `outputs/verification/2026-10-07-spacing/browser-collaboration-benchmark-contended.json`;
the adjacent `browser-collaboration-benchmark-contended-screenshots` directory contains
`owner-final.png` and `peer-final.png`.

The roughly 3.6-second commit and 4.6-second peer-state observations are consistent with the
current 3.5-second autosave delay followed by 1.5-second revision polling. They describe this
tiny-document, two-account, contended local run. They are not hosted latency, a maximum
collaborator count, evidence of remote paint timing, or a promise for larger files. The ten
samples' p95/p99 are their maximum. No transport or runtime optimization was applied.

## Observed input-to-remote-paint baseline — 2026-10-07

`tests/web/benchmark_paint_collaboration.py` adds a separate, opt-in visual gate while keeping
the historical state benchmark intact. It uses two synthetic accounts in independent
Chromium contexts, a 320×240 native document at 100% zoom/DPR1, and the original Pencil tool.
Setup sets a hard 24-pixel green pencil before timing. Five real browser mouse clicks produce
distinct dots. The endpoint is the first observed remote CDP swapped PNG containing the
expected green pixels in the matching canvas region; each region is white beforehand.
Input is captured passively as a trusted pointer-down event. Neither command completion,
API acknowledgment, document inspection nor `requestAnimationFrame` satisfies the paint gate.

`paint_latency.py` calibrates input and frame clocks to the same host timeline, retains
uncertainty, bounds capture queues, and fails invalid timestamps/captures. This measures a
conservative observed compositor-frame upper bound, not physical display photon latency.
Chrome may omit frames. Test-side observation itself adds work. Native structure/pixels,
committed archive SHA and peer reload are checked separately outside the timed path.

```sh
PHOTOCRAFT_CHROME='/Applications/Google Chrome.app/Contents/MacOS/Google Chrome' \
python tests/web/benchmark_paint_collaboration.py \
  --origin http://127.0.0.1:8876 --samples 5 \
  --output test-results/native-paint-baseline.json \
  --context 'Describe concurrent host activity and the exact runtime/assets'
```

At native source `8309d1f`, cloud binary SHA
`a0730644fe554733fae9d255c9d8eed079d69826ac699dea6bc8dfae6f87c106`, and web assets
`photocraft-web-c5269eac07ccf586`, Chrome 154.0.8037.98 produced:

| Input → observed matching peer pixels | Samples | Median | Maximum |
|---|---:|---:|---:|
| Initial calibrated upper bound including uncertainty | 5 | 4,476.828 ms | 4,505.683 ms |
| Final observer with explicit acknowledged capture readiness | 5 | 4,500.469 ms | 4,957.469 ms |

The five upper bounds were 4,326.423, 4,472.316, 4,476.828, 4,505.683 and 4,496.090 ms.
Clock uncertainty was 2.57–3.30 ms; all captures had zero reported errors. Every sample
failed the strict 500 ms gate, and the benchmark exited 1. All five native pixels converged,
the committed version reached revision 6, and peer reload preserved them. Both synthetic
accounts were removed with zero remaining. Cursor delivery is explicitly unsupported,
because the current presence payload has no coordinates; it is not represented by a zero
latency sample.

This was a bounded loopback test on macOS 26.5.1 ARM64, without configured network delay or
a separately measured RTT. Host start/end load averages were 5.24/6.32/6.04 and 4.96/6.17/6.00.
It does not establish hosted latency, separate-device performance, tail reliability or
capacity. No runtime optimization or deployment occurred. Raw evidence lives at
`outputs/verification/2026-10-07-realtime-latency/native-paint-baseline.json` in the workspace,
with the adjacent `native-paint-baseline-screenshots` directory.

The final independent rerun used an acknowledged CDP baseline before each trusted input,
avoiding a capture-start race found by Chrome calibration. It also failed all five 500 ms
gates: 4,812.301, 4,500.469, 4,957.469, 4,474.091 and 4,445.914 ms, with 2.50–3.08 ms clock
uncertainty and zero capture errors. Native pixels, revision 6, reload and both-account
cleanup passed again. The final native SHA was identical between runs:
`061515bf91217475c0e86a604e19451dd9916092ea8d8216b9ba08aa3972a5e5`.
This rerun had higher uncontrolled host load, starting at 9.04/8.36/7.13 and ending at
7.15/7.94/7.03. Its raw report is
`outputs/verification/2026-10-07-realtime-latency/native-paint-baseline-verified.json`.
The reported 1440×960 capture surface matched the pinned browser viewport. The GPU adapter
was not measured; this headless test is not physical-device GPU evidence.

The observer's unit/browser calibration tests reject preexisting pixels, frozen output,
untrusted/missing input, invalid clocks and capture overflow; an injected 600 ms delayed
paint is over budget. In-memory calibration times are measurement-oracle evidence, not
PhotoCraft collaboration performance. See [the implementation and release plan](sub500-collaboration-plan.md).

Ten final oracle/calibration tests passed on independently selected Chrome 154 in 3.481 s.
The observer waits at most two seconds for initial acknowledged capture, before dispatching
input, and reports unavailable readiness as invalid. Three additional fresh Chrome 154
launches passed the same tests. Small emulated Chrome viewports can produce a different
screencast surface: the 320×240 calibration page produced 500×153. Reports retain actual
PNG dimensions; an out-of-surface oracle or a geometry change is invalid. No timed repaint
is forced to manufacture capture evidence.

## Transaction compatibility fix: matched workload — 2026-10-07

The service now wraps formerly unprotected SQL query groups in short explicit transactions.
This keeps SQLx's separate unnamed Parse/Sync and Bind/Execute exchanges on one PostgreSQL
backend when the connection passes through a transaction pool. Parameters remain bound,
existing atomic writes retain their transaction boundaries, and every successful mutation
awaits commit. None of the added transactions spans invitation delivery or another external
HTTP request; the preexisting sign-in verification transaction retains its original scope.
This is a correctness fix, not an optimization or a confirmed explanation of the hosted 503.

Correctness and timing used separate paths. The local wire fixture
`tests/web/probe_transaction_pool.py` forces a fresh backend after each completed idle
protocol batch. The frozen previous binary returned HTTP 503 and SQLSTATE `26000` for both
`/api/me` and `/api/projects`. The fixed binary passed all 38 API tests through 2,262 backend
swaps, with zero idle Parse gaps, named statements, or SQLSTATE errors. This covers normal
create, native upload/commit/reopen, Trash/Restore, sharing, authorization, and failure cases;
it is a protocol regression fixture, not a Supavisor emulator or hosted failure diagnosis.
Its timings are excluded from the tables below.

The normal-loopback HTTP harness ran the same bounded workload sequentially against frozen
before/after debug binaries, without that proxy. Both used the same macOS 26.5.1 ARM64 host,
12 logical CPUs, Python 3.14.3, PostgreSQL instance, fixtures, warmups, and 10 measured rounds.
The before run was 22:22:33–22:23:24 UTC; the after run was 22:23:24–22:24:15 UTC.
Browser diagnostics had closed, but frontend build work and other shared-host activity could
contend. Load averages show **strong, changing host contention**:

| Run boundary | 1-minute load | 5-minute load | 15-minute load |
|---|---:|---:|---:|
| Before start | 44.69 | 31.26 | 25.97 |
| Before end / after start | 31.23 | 29.56 | 25.64 |
| After end | 21.28 | 27.16 | 24.98 |

These are matched workload observations, not a controlled estimate of causal overhead.
Some measurements improved and others worsened while load changed; neither direction
establishes a performance effect of the code change. There is no capacity or SLO claim.

| Presence clients | Samples per run | Before p50 / p95 / p99 | After p50 / p95 / p99 |
|---|---:|---:|---:|
| 1 | 10 | 2.544 / 3.597 / 3.597 ms | 3.093 / 6.542 / 6.542 ms |
| 5 | 50 | 11.453 / 47.389 / 54.000 ms | 4.423 / 9.270 / 10.747 ms |
| 20 | 200 | 11.556 / 90.231 / 139.464 ms | 12.448 / 117.202 / 125.737 ms |

Both document cases used one chunk and 10 measured saves/opens per run. The native fixture
remained 64×48 and 1,586 bytes; “Make some noise” remained 1080×1350 and 245,446 bytes.
Each reopened native file matched the original bytes. With 10 samples, each p95 below also
equals p99 and the maximum; this does not estimate a long-running tail.

| Document / operation | Before p50 / p95 | After p50 / p95 |
|---|---:|---:|
| Native fixture — upload creation | 4.212 / 5.854 ms | 3.345 / 7.441 ms |
| Native fixture — chunk transfer | 3.292 / 5.414 ms | 4.367 / 12.832 ms |
| Native fixture — commit | 5.620 / 8.956 ms | 4.235 / 10.691 ms |
| Native fixture — total save | 13.210 / 18.452 ms | 12.239 / 29.106 ms |
| Native fixture — peer HTTP open | 6.193 / 8.036 ms | 5.393 / 15.250 ms |
| Make some noise — upload creation | 5.236 / 13.511 ms | 2.521 / 9.161 ms |
| Make some noise — chunk transfer | 7.551 / 10.981 ms | 6.819 / 8.338 ms |
| Make some noise — commit | 11.825 / 38.606 ms | 10.291 / 17.588 ms |
| Make some noise — total save | 26.498 / 51.690 ms | 20.651 / 31.491 ms |
| Make some noise — peer HTTP open | 11.424 / 20.679 ms | 9.689 / 16.313 ms |

Structurally, each added transaction contributes one awaited `BEGIN` and one awaited
`COMMIT` database round trip. Grouping related statements shares that cost: presence uses
three short transactions (account lookup, project authorization, and its three presence
queries), adding six database round trips to that request path. Remote database latency may
therefore matter more than loopback timing. No provider pool mode or prepared-statement
configuration was changed to avoid that cost, and no commit runs after an HTTP success.

Each run completed all 452 HTTP requests, including setup and warmups: 904 combined. Each
removed its own 20 synthetic accounts and owned data, with zero remaining; no production
requests or real email were sent. Reports retain all raw samples, cleanup, and binary hashes:

- `outputs/verification/2026-10-07-spacing/transaction-overhead-before.json`
- `outputs/verification/2026-10-07-spacing/transaction-overhead-after.json`
- Separate correctness evidence: `transaction-pool-before.json`, `transaction-pool-after.json`,
  and `transaction-pool-after.api.log` in the same artifact directory.

Before binary SHA-256: `70cb5758027ae1de95a37c8f2beac8ae3d5213a4817fda4a08ec8553c54bfde6`.
Fixed/current backend SHA-256: `7ab7cd6b407a0d5098c2181926b80bc590081e8f9f83eec7086f584737d551ea`.
