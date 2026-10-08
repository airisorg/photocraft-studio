# Collaboration latency measurements

This report separates HTTP/state measurements from calibrated input-to-remote-paint
measurements. The 2026-10-08 private RPC candidate passed three local paint observations
during a mixed workload of 998 modeled HTTP actors and two actual native browser clients;
the largest uncertainty-inclusive upper bound was 235.656 ms. A separate matched HTTP-only
comparison and the earlier 19-observation browser profile remain distinct evidence below.
The preferred 150 ms target was not met. These are bounded local observations, not hosted
capacity, 1,000 browsers, physical-device latency or a production service-level objective. See
[collaboration architecture](collaboration-architecture.md) for the transport and limits.

## Private RPC exchange measurements — 2026-10-08 UTC

The server consolidates its authorized PUT exchange in the private
`photocraft.exchange_live_v1` function. It is `VOLATILE`, `SECURITY INVOKER`, with fresh
post-lock authorization and a post-lock wall clock for lease/admission checks. The service
still awaits an explicit transaction commit; this is not one database round trip or a new
browser-accessible provider RPC. The web protocol remains the 80 ms GET-or-PUT exchange
described below. See the [architecture](collaboration-architecture.md) for its authority,
expiry, native-preview and database-pool boundaries.

The measured optimized backend SHA-256 was
`90908c3f4eedd41373a8916fddfe65e43b9710dac9439dab437d0042b5ddc090`.
The mixed run used `photocraft-web-d3fc6e503acae4d9_bg.wasm`, SHA-256
`c0a89fbd41e0b9d00bebf4065e440a754083739e5a84acda93cf581a48abf1a1`.
These are separate from the earlier debug-backend browser profile below. All new runs used
macOS 26.5.1 ARM64, 12 logical CPUs, loopback HTTP, and PostgreSQL TLS with `verify-full`.
Eight application workers each had a four-connection pool, an aggregate worker budget of
32, plus the harness monitor connection. No hosted provider was exercised.

### Matched HTTP-only comparison

Both runs used the same hardened semantic harness: 1,000 synthetic cursor actors in 100
rooms of ten, sticky routing across eight workers, nominal PUT every 80 ms, three seconds
of warmup and ten active seconds. Each successful response had to acknowledge the exact
submitted sequence and return valid authorized peer state. Every actor had to observe every
other HTTP actor in its room with a usable lease during the active interval. Both runs had
zero HTTP/semantic errors, including warmup, complete 1,000-actor coverage, and no native
document mutation. These HTTP fixtures omit browser rendering, ordinary presence polling,
and durable saves/uploads.

| Optimized backend | Active HTTP 200s / 125,000 nominal opportunities | Completion ratio | PUT p50 / p95 / p99 / maximum | Scheduled-completion p50 / p95 / p99 / maximum | Coalesced nominal ticks |
|---|---:|---:|---|---|---:|
| Pre-RPC exchange, `3c4427e6` | 70,796 | 56.6368% | 119.416 / 180.863 / 228.105 / 359.621 ms | 141.666 / 201.467 / 237.358 / 361.704 ms | 7,281 |
| Private RPC exchange, `90908c3f` | 117,147 | 93.7176% | 4.964 / 20.259 / 26.599 / 52.099 ms | 7.542 / 40.365 / 51.238 / 65.735 ms | 0 |

Scheduled completion includes delay from the actor's nominal scheduling time; it is not
input-to-paint latency. The RPC run preceded the retained pre-RPC comparison: 09:15:20–
09:15:43 UTC versus 09:17:23–09:17:46 UTC. This is a matched workload comparison, not a
randomized repeated experiment. Host load was not identical: respective start/end 1/5/15-minute
load averages were 2.86/3.44/4.83 → 6.45/4.31/5.11 and 4.38/4.36/5.05 → 7.45/5.17/5.33.
The full pre-RPC binary SHA-256 is
`3c4427e6f5331c28c36604308ebff507e8eeccf3561c74f68b72c88dd391d385`.
Neither result certifies a sustained offered-rate or production capacity SLO.

### Native paint during the mixed 1,000-actor workload

The passing rerun at 09:20:59–09:21:21 UTC used **998 modeled HTTP cursor actors and two
independent native browser clients**, totaling 1,000 synthetic accounts in 100 rooms. Both
actual browsers used worker 0; the HTTP actors crossed workers, and eight shared the native
pair's room. This does not prove cross-worker browser delivery. Headless installed Chrome
154.0.8037.98 used
1440×960 viewports at DPR1 with a 320×240 native document. Background HTTP fixtures were
64×48 and 1,586 bytes. Warmup lasted three seconds, active load twelve seconds. All 998
HTTP actors had successful active requests before the first timed input; each observation
started and finished inside the active interval with background request progress. The three
observation intervals contained 6,787 / 7,073 / 11,212 HTTP requests respectively; this is
aggregate progress, not proof that all 998 actors published in each subsecond interval.
The report does not identify the browser GPU backend.

| Trusted native input | Uncertainty-inclusive paint upper bound | Clock uncertainty | Result |
|---|---:|---:|---|
| Cursor movement 1 | 235.656 ms | 5.959 ms | Passed |
| Cursor movement 2 | 220.709 ms | 14.034 ms | Passed |
| Held native Pencil prefix | 213.264 ms | 13.115 ms | Passed |

The unchanged gate requires an upper bound **strictly below 500 ms** and rejects clock
uncertainty above **15 ms**. The endpoint is the first matching CDP-swapped PNG, not a
physical display. There were no invalid, missing or timed-out samples or capture errors.
The three-sample descriptive p50 is 220.709 ms and p95/p99/maximum is 235.656 ms; this is
too small a sample to establish tail reliability. None met the preferred 150 ms threshold.

The background completed 115,553 active HTTP 200s out of 149,700 nominal opportunities
(77.1897%), with zero errors including warmup and usable peer coverage for all 998 HTTP
actors over the full twelve-second active period, not within each paint interval. Every
HTTP actor completed at least 114 active successful requests. Two nominal ticks were
coalesced. PUT p50/p95/p99/maximum was
26.370/56.893/73.748/184.302 ms; scheduled completion was
49.959/93.666/111.950/187.984 ms. The ratio is below the HTTP-only RPC result and must not
be presented as full nominal delivery. Start/end host load was 3.11/4.01/4.78 →
8.31/5.09/5.15. Browser work and load generation shared this host.

While the pointer remained held, receiver native pixel/history/revision checks passed;
after HTTP load and before release, every project still had canonical revision 1. After
load ended, pointer release committed revision 2; receiver native pixel convergence and
reload passed, and the downloaded 5,089-byte native archive had SHA-256
`50ec0246f89c71d8b5c7289c2c5675b150f4e3faf8925ad008e14eefdffca945` with 320×240 dimensions.
Those durable checks were untimed and outside active load. They establish correctness for
this fixture, not a sub-500 ms durable-save SLO or concurrent-save scale result.

The first mixed attempt remains failed evidence. Its three paint upper bounds were
141.705/154.226/144.386 ms and its HTTP checks passed, but the untimed durable phase raised
an `AssertionError`. The retained peer screenshot showed the saved stroke after reload.
Source inspection identified a likely fixture readiness race: the command bridge can exist
before asynchronous project opening finishes. The rerun added the existing native-document
readiness helper before querying reloaded pixels, plus safe named checkpoints around each
durable assertion/download. No runtime code or timing gate changed. All durable checkpoints
passed in the rerun; the earlier broad-stage error alone does not prove its exact cause.

Evidence under `outputs/verification/2026-10-08-release-continuation/`:

- `exchange-v1-hardened-tls-1000.json` and `rpc-tls-1000.json`: matched HTTP samples,
  complete semantic coverage, exact binaries, environment and cleanup.
- `rpc-mixed-1000-r2.json` and `rpc-mixed-1000-r2-screenshots/`: passing input/frame
  observations, active-load brackets, HTTP coverage, native invariants and durable checks.
- `rpc-mixed-1000.json`, its screenshots and `mixed-fixture-diagnosis.md`: retained initial
  failure and the readiness correction.

Every run stopped its eight owned workers, dropped its own UUID database and touched zero
shared application rows. The passing mixed run verified unchanged backend/WASM bytes
before and after. This bounded local evidence does not cover 1,000 browsers, large native
documents, arbitrary commands, sustained failure/reconnect workloads, physical devices,
hosted geographic latency or production capacity. Hosted acceptance remains a separate gate.

### RPC debug browser acceptance and latency profiles

A separate local browser gate at 09:22:05–09:33:37 UTC passed four renderer-startup cases
(8.238 s), all 50 browser acceptance journeys (639.390 s), and six focused live-exchange
cases (44.130 s). It used the current RPC **debug** backend SHA-256
`85de649cfcafb160eef07cd9de772f19b68566642735c4ba57e63d9784eeb0a1` and the same `c0a89fbd`
WASM identified above. These correctness runs are separate from the optimized-backend
HTTP and mixed-load measurements.

The subsequent two-account profile ran at 09:33:59–09:34:21 UTC on that debug backend and
installed Chrome 154.0.8037.98, with no concurrent owned build, load or other test browser.
Each delay profile included three trusted cursor movements and three held native Pencil
prefixes at 1440×960/DPR1. CDP configured a minimum HTTP delay on both pages; it did not
simulate packet loss or bandwidth limits. The profile receipt does not identify the GPU
backend. Unrelated host activity was uncontrolled; start/end load averages were
3.21/4.32/4.72 → 3.57/4.33/4.71.

| Configured HTTP delay per page | Observed `/api/me` RTT range | Samples | Upper-bound p50 | Upper-bound p95 / p99 / maximum |
|---|---:|---:|---:|---:|
| 0 ms | 0.50–1.40 ms | 6 | 120.901 ms | 180.950 ms |
| 50 ms | 52.40–58.40 ms | 6 | 187.286 ms | 235.412 ms |
| 100 ms | 101.30–110.20 ms | 6 | 302.941 ms | 316.380 ms |

A further held-pencil observation after a real sender reload passed at 150.608 ms; the
receiver stayed open, tab identity was retained, and the sender sequence advanced 81→82.
All **19/19** samples passed the unchanged strict 500 ms / 15 ms uncertainty gates, with
zero missing/invalid/timed-out samples or capture errors. Uncertainty ranged from 2.368 to
3.322 ms. The preferred 150 ms target still failed. The pooled descriptive p50 was
187.286 ms, with p95/p99/maximum 316.380 ms; six observations per ordinary profile and one
reload do not establish reliable tail latency or a speed advantage over earlier profiles.

Canonical native convergence and receiver reload passed separately at revision 11 with
native SHA-256 `1cfa1cce1107c25f6dd54bffd11fd72435f3f1f2a1354875f3d1ebc7fba120a2`.
The profile recorded 256 live GET 200s and 89 PUT 200s including setup/idle periods, and
removed both synthetic accounts. `browser/receipt.json` plus its three logs, and
`rpc-profiles/paint.json` / `rpc-profiles/receipt.json` under the evidence directory above
retain the exact artifacts, observations and cleanup. Both wrappers stopped their owned
server, dropped their own UUID database, touched zero shared rows and verified unchanged
WASM before/after. These runs did not deploy the candidate or exercise hosted accounts.

## Earlier single-request exchange candidate — 2026-10-08 UTC

This candidate uses one live GET **or** PUT at a nominal 80 ms cadence per active binding,
with at most one request in flight. Active input is coalesced into PUTs whose successful
responses include the freshly authorized role, current revision and peer snapshot. An idle
reader uses GET. A 403/409 or a successful older-server acknowledgement without peer data
requires an authoritative GET before another PUT. Both response paths subtract the entire
request elapsed time from peer leases; delayed responses do not restart expired leases.
Stale project/auth generations are ignored. This changes the transient HTTP exchange;
it does not turn preview acknowledgements into durable document commits or remove the
supported-gesture, saved-base and room limits described below.

The frozen assets were:

- WASM `photocraft-web-d3fc6e503acae4d9_bg.wasm`, SHA-256
  `c0a89fbd41e0b9d00bebf4065e440a754083739e5a84acda93cf581a48abf1a1`.
- Debug backend SHA-256
  `e00870cc2fee7526404348fc6b693affd40bd6845f239aaa7f2e669c79aebe00`.

The profile ran at 08:48:42–08:49:05 UTC with two independent synthetic accounts on
macOS 26.5.1 ARM64, 12 logical CPUs and installed Chrome 154.0.8037.98. The native fixture
was 320×240, one layer and 4,769 bytes; viewport 1440×960, DPR1. There was no concurrent
owned build, load test or other test browser. Unrelated host activity was not controlled;
start/end load averages were 5.21/6.71/5.76 and 4.93/6.52/5.72. CDP imposed a minimum HTTP
request delay on both pages, without packet loss or a bandwidth cap. The measured
`/api/me` round trips below are separate identity-checked probes, not a physical network RTT.

Each ordinary profile contains three trusted cursor inputs and three original native Pencil
prefixes observed while the pointer remains held. Timing ends at the first observed matching
CDP-swapped PNG, including clock uncertainty; it does not measure a physical display.

| Configured HTTP delay per page | Observed `/api/me` RTT range | Samples | Upper-bound p50 | Upper-bound p95 / p99 / maximum |
|---|---:|---:|---:|---:|
| 0 ms | 0.80–2.30 ms | 6 | 121.029 ms | 201.179 ms |
| 50 ms | 51.40–59.70 ms | 6 | 234.390 ms | 271.917 ms |
| 100 ms | 101.50–112.10 ms | 6 | 300.224 ms | 339.305 ms |

A separate held-pencil observation after a real sender reload passed at 138.748 ms;
the receiver remained open, the sender retained its tab identity and its sequence advanced
from 81 to 82. All **19/19** observations passed, with zero invalid, missing or timed-out
samples and zero capture errors. Clock uncertainty ranged from 2.538 to 3.187 ms. The
unchanged acceptance gate requires the uncertainty-inclusive upper bound to be strictly
below 500 ms and rejects uncertainty above 15 ms. The preferred 150 ms threshold did not
pass, including across the six samples with no configured delay.

Pooling the different profiles and the reload sample gives a descriptive p50 of 234.390 ms
and p95/p99/maximum of 339.305 ms. With these small samples, each profile's p95 and p99 is
its maximum; the mixed distribution is not a production latency distribution or a reliable
tail estimate. These data do not establish a speed advantage over the historical candidate.

The run recorded 261 successful live GETs and 89 successful live PUTs, including idle/setup
periods. Canonical native convergence and receiver reload passed separately at revision 11,
with exact final native SHA-256
`1cfa1cce1107c25f6dd54bffd11fd72435f3f1f2a1354875f3d1ebc7fba120a2`.
Both synthetic accounts were removed. The wrapper stopped its own server, dropped its UUID
fixture database, touched zero shared rows and verified the same WASM hash before/after.

### Focused exchange correctness

Six rendered browser cases passed in 44.196 s on the same candidate at 08:47:09–08:47:54 UTC:

1. Cursor lease expiry and reconnect preserve the real native document.
2. A delayed previous-base 409 does not clear a newer held gesture; its observed paint
   upper bound was 216.899 ms with 2.943 ms uncertainty.
3. A delayed successful PUT cannot extend an expired peer lease or cross an active-document
   generation. An aged real lease of 780 ms was held for 942.474 ms; the HTTP 200 request
   finished successfully in 947.294 ms, below the 2,000 ms client abort deadline. No ghost
   appeared in the subsequent rendered frames. A separate fresh response was ignored after
   opening a new native document.
4. An older-server PUT acknowledgement forces one GET before another PUT, even during
   continued input. The resulting cursor upper bound was 164.173 ms with 2.845 ms uncertainty.
5. Active peer paint arrives through PUT responses while GET peer data is withheld, with
   unchanged receiver native pixels, history and revision. Its upper bound was 251.922 ms
   with 3.068 ms uncertainty. A separate sustained burst coalesced 48 trusted pointer moves
   into 18 PUTs, 79.562–86.306 ms apart; observed maximum in-flight requests was one.
6. An edit-to-view downgrade clears the drawing preview while preserving cursor access and
   local native pixels.

The first focused attempt is retained as failed evidence: a long synchronous input burst
overflowed the bounded CDP frame queue. Its timing sample was **invalid**, despite matching
pixels under 500 ms. The fixture now uses a short timed prefix and a separate sustained
cadence burst after capture stops. The observer, 500 ms limit and 15 ms uncertainty cap
were unchanged. The lease fixture also explicitly proves successful body completion, so an
aborted request cannot satisfy its negative-pixel assertion.

Local evidence is under `outputs/verification/2026-10-08-release-continuation/`:

- `profiles/paint.json`, its adjacent screenshots and `profiles/receipt.json` retain the
  19 samples, clock bounds, input/frame evidence, exact artifacts, correctness and cleanup.
- `focus/live.log`, `focus/receipt.json` and `focus/screenshots/*.json` / `*.png` retain all
  six cases, request cadence and rendered output.
- `focus-attempt1-capture-overflow/` retains the initial invalid-capture run.

These local tests do not establish hosted two-person latency, 1,000-client capacity,
all-command preview support or sub-500 ms durable persistence. Capacity and hosted
acceptance remain separate gates; see [the scale review](scale-release-review.md).

## Historical security-remediation sample

The frozen browser artifact `6ecd9aae` and debug backend `53c0126` passed the
hardware Chrome 154 lane with two independent local accounts. Ordinary profiles
contain six samples each: configured minimum HTTP delays of 0 / 50 / 100 ms produced
largest uncertainty-inclusive upper bounds of 147.038 / 205.562 / 274.060 ms.
One additional reload observation was 132.266 ms. The endpoint is a matching
CDP-swapped PNG, not a physical display. See the [release review](security-release-review.md)
for exact hashes, environment, statistics and the separate inconclusive software-renderer
timing runs. The [1,000-client scale gate](scale-release-review.md) is not met;
these two-browser results do not certify hosted scale or all editing operations.

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

## Bounded original-native previews — 2026-10-07 local date

The optional adapter now reuses the original `LiveStroke::begin_with/push`, layer `moved`
operation and damage-region compositor in a separate COW preview. It preserves the real
document, local input and undo. The authenticated HTTP/PostgreSQL transport coalesces writes
at 40 ms, reads at 80 ms with one pending request per direction, and expires peer state
after two seconds. Canonical full-document autosave starts after 150 ms of idle time with
the pointer released. Preview feedback and durable persistence are separate measurements.

`benchmark_live_collaboration.py` used two distinct synthetic accounts and independent
Chromium contexts, the 320×240 one-layer native fixture (4,769 bytes), zoom 100%, viewport
1440×960 and DPR1. Trusted pointer moves drive the original Pencil at eight paced points;
the expected green prefix must appear while the button remains held. Cursor observations
require the peer marker's actual purple pixels. The owner/receiver native revisions and
pixels, saved archive and history must remain unchanged during the preview. After release,
native pixels, committed archive SHA and reload convergence must match.

The final run at 02:22:37–02:22:59 UTC on 2026-10-08 (2026-10-07 locally) used macOS
26.5.1 ARM64, 12 logical CPUs and Chrome 154.0.8037.98. Headless launch enabled unsafe WebGPU
and SwiftShader options; the selected GPU adapter was not measured. No owned build,
other test browser, proxy or native performance run was concurrent. Other host activity
was unmeasured: load averages were 9.27/12.12/15.71 at start and 8.42/11.74/15.49 at end.

CDP added a minimum HTTP request delay on both pages, without packet loss or a bandwidth
cap. This is not a physical packet RTT or geographic-region benchmark. Five `/api/me`
requests per account/profile verified identity and measured the following actual round trips.
All timing values below are conservative observed compositor-frame upper bounds including
clock uncertainty; physical display photon latency was not measured.

| Configured HTTP delay per page | Observed `/api/me` RTT range | Cursor + held-pencil samples | Nearest-rank p50 | Maximum |
|---|---:|---:|---:|---:|
| 0 ms | 0.80–1.90 ms | 6 | 84.552 ms | 117.382 ms |
| 50 ms | 50.90–55.60 ms | 6 | 155.437 ms | 187.766 ms |
| 100 ms | 100.70–105.50 ms | 6 | 236.291 ms | 287.777 ms |

One additional held stroke after a real sender reload passed at 82.821 ms while the receiver
remained open. Tab identity was retained and the wire sequence advanced from 102 to 103.
All 19 observations passed the strict 500 ms gate; none was invalid, missing or timed out,
and captures reported zero errors. Each six-sample profile's p95/p99 is its maximum, not a
reliable tail estimate. The preferred 150 ms threshold passed with no configured delay but not across delayed
profiles. The final canonical revision was 11, exact native convergence/reload passed, and
both synthetic accounts were removed with zero remaining.

The report counted 332 successful live GETs, 111 successful PUTs and three PUT 409s; no
401/403/404 responses were counted. The profile report does not retain 409 bodies/timestamps,
so it cannot attribute a particular sample's delay to one of those responses. A separate
rendered race test held the previous-base End request for 536.645 ms, confirmed a real 409,
and then observed the newer held stroke at 165.704 ms. Accepted new-base writes, unchanged
held native state, committed bytes, receiver reload and native undo/redo all passed.

The initial final-profile attempt is retained: it passed 16 observations, then declared
one capture invalid and two planned samples missing. Its observer was left recording during
intervening canonical-save waits, overflowing the bounded frame queue before the next input.
The harness now opens/closes capture per timed sample, outside save/reload waits. The frozen
observer, clocks, pixel oracle and strict 500 ms gate were unchanged. Ten observer calibration
tests passed, including stale/frozen pixels and deliberately delayed over-budget paint.

Local artifacts under `outputs/verification/2026-10-07-realtime-latency/` retain every sample,
capture readiness, clock bounds, frame timestamps, PNG hashes, request counts and cleanup:

- `final-live-profiles-r2.json` and adjacent screenshots: final 19 passing observations.
- `final-live-profiles.json`: initial invalid-capture attempt; do not discard it.
- `final-live-journeys/`: delayed 409, edit-to-view downgrade, cursor lease expiry/reconnect.
- `final-live-calibration/`: frozen-oracle calibration evidence.

The existing five-click native Pencil journey was also repeated with previews enabled.
All five visual upper bounds passed: 166.806, 170.153, 172.863, 101.130 and 101.347 ms.
Native pixel convergence, exact archive/layer checks, receiver reload and both-account
cleanup passed. Its first attempt is retained: fast preview pixels arrived before the peer
adopted the newly committed archive, exposing an immediate-canonical-readiness assertion.
The harness now waits up to ten seconds for exact native convergence outside the unchanged
visual timer. It also limits capture to each sample and removes obsolete runtime-delay
metadata. Final evidence is `final-native-paint-preview-enabled-r2.json`; the initial
`final-native-paint-preview-enabled.json` must not be treated as a passing run.

Final WASM SHA-256:
`c2c847c5bffe1d325c234082283120e9d270f2731e053d9a64d35b0151518c96`.
Cloud debug binary SHA-256:
`e393cdfe5ed5b71137581cbc5ba8392e41f7059b94870926dfa49460c8fee428`.

Regression gates passed 739 native UI tests (three existing benchmark tests ignored), 11
cloud unit tests, 41 API tests, 13 auth tests, four cold-worker tests, seven release-provenance
tests, 12 live API tests and three new rendered live journeys. The live API cases also passed
through 948 forced idle backend switches with zero SQLSTATE errors. The 50 existing browser
journeys passed across the full run and a focused recovery-fixture correction/rerun; the
initial obsolete-autosave-deadline failure is preserved. Layering, all 23 WASM targets,
strict touched-crate Clippy and the quick native performance smoke passed. Quick performance
had no matching baseline and was contended; it is not a no-regression performance claim.

These observations establish a small supported-preview envelope. They do not establish
hosted two-user latency, 500 ms for every command/file/network, unrestricted simultaneous
writers, shared undo, large-room capacity, or sub-500 ms durable persistence. A sender must
start a supported gesture from its last acknowledged native base; rapid subsequent gestures
before canonical acknowledgment and unsupported/oversized operations can use saved-version
sync. Only one remote drawing gesture is displayed; receiver-local work takes precedence.
HTTP read/write costs, native object deltas and the safe provider private-Broadcast bridge
remain explicit scaling work in [the plan](sub500-collaboration-plan.md).
