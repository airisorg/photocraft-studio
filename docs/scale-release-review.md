# Scale release review

## Earlier RPC transport snapshot — 2026-10-08

This section retains the `c0a89fbd` artifact's measurements. The later combined
Preferences/cursor-label artifact has [separate current evidence](collaboration-performance.md#combined-artifact-cross-worker-mixed-workload),
including the retained first run and one unchanged-artifact diagnostic; its results do
not replace this snapshot or certify production capacity.

The optimized candidate passes a short **local** 1,000-actor mixed workload:
998 modeled HTTP collaborators plus two real native browser clients. The three
observed cursor/Pencil input-to-remote-pixel upper bounds were **235.656,
220.709 and 213.264 ms** with both browsers on one worker. A later direct worker0→worker1
run observed **197.587, 209.369 and 143.335 ms**, also below the unchanged 500 ms gate.
The deployed `a17cea25` guest editing/file check separately passed with matching WASM.
This does not certify
1,000 production browsers, geographical latency, every editor command, a
sustained service SLO or Figma/Canva collaboration parity. Hosted two-account
acceptance and production capacity remain separate release gates.

The original PhotoCraft engine, painting commands, document format, renderer and
undo paths are reused. The thin web adapter now permits one GET **or** PUT per
binding generation at a nominal 80 ms cadence; PUT also returns the authorized
peer snapshot. A private PostgreSQL `exchange_live_v1` function consolidates
application-to-database calls and allows internal query-plan reuse while retaining
preauthorization, ordered locks, fresh post-lock authorization, wall-clock expiry
and commit-before-ACK behavior. PUT still uses BEGIN/function/COMMIT; it is not a
single round trip. No database allowance, provider credential, room/session quota
or purchased service was changed. See the [collaboration contract](collaboration-architecture.md).

### Exact artifacts and workloads

The optimized runs in this snapshot use backend SHA-256
`90908c3f4eedd41373a8916fddfe65e43b9710dac9439dab437d0042b5ddc090`.
The mixed browser run uses `photocraft-web-d3fc6e503acae4d9_bg.wasm`, SHA-256
`c0a89fbd41e0b9d00bebf4065e440a754083739e5a84acda93cf581a48abf1a1`.
Both hashes were unchanged before/after execution. The preserved pre-function
exchange binary is `3c4427e6f5331c28c36604308ebff507e8eeccf3561c74f68b72c88dd391d385`.

The host was macOS 26.5.1/arm64, 12 logical CPUs. An owned temporary UTF-8
PostgreSQL cluster enforced TLS with certificate/IP verification; recorded
connections used TLS 1.3. HTTP was loopback plaintext. Generator, workers, database
and browsers shared this Mac; unrelated host activity was not controlled. No owned
build or other browser workload ran during these measurements. These are short
observations rather than isolated-host or production benchmarks.

HTTP-only stages used three seconds warmup, ten active seconds, ten cooldown,
rooms of ten, cursor payloads and one PUT lane per actor at nominal 80 ms. Eight
workers used four pool slots each (32 total), plus one monitor. Sticky HTTP actors
cross worker boundaries. The harness caps concurrent HTTP at 1,000 and validates
exact accepted sequence ACKs, current revision/role and typed peer state. Every
actor must observe every other HTTP actor in its room **during the active window**
with a remaining lease exceeding the full request duration. Warmup errors fail
acceptance too. Empty or skeletal HTTP 200 responses cannot pass. Bodies above the
64 KiB semantic-validation bound fail rather than bypassing validation.

### HTTP-only observations

| Candidate / actors / workers × pool | Active HTTP 200 | Successful nominal schedule | PUT p50 / p95 / p99 / max (ms) | Scheduled completion p50 / p95 / p99 / max (ms) | Coalesced ticks |
|---|---:|---:|---:|---:|---:|
| Private function / 100 / 1 × 5 | 12,200 | 97.600% | 4.977 / 11.252 / 12.314 / 13.312 | 7.097 / 13.412 / 14.614 / 15.530 | 0 |
| Private function / 1,000 / 8 × 4 | 117,147 | 93.718% | 4.964 / 20.259 / 26.599 / 52.099 | 7.542 / 40.365 / 51.238 / 65.735 | 0 |
| Preserved pre-function exchange / 1,000 / 8 × 4 | 70,796 | 56.637% | See raw report | 141.666 / 201.467 / 237.358 / 361.704 | 7,281 |

All three stages had zero HTTP/transport/semantic errors, and full usable room-peer
coverage. The matched pre-function comparison uses the **same hardened harness**.
Earlier exchange attempts measured different host conditions and an earlier
validator; they remain separate evidence, including the 31.55%/678.711 ms stage.
These sequential comparisons show an observed improvement, without isolating all
host effects. Nominal opportunities are actor-count × active-seconds × 12.5,
not requests actually transmitted. Ratios below 100% must not be described as a
perfect sustained schedule. HTTP measurements still exclude browser rendering,
ordinary presence, durable uploads and saves. `capacity_slo_certified` remains
false in every HTTP report.

The 1,000-actor private-function HTTP stage recorded 151,528 requests including
warmup, 74,110,020 request bytes and 322,013,747 response bytes including headers.
Maximum sampled connections were 33 including the monitor; aggregate worker RSS
was 173.61 MiB. No sampled database lock waits were observed. One-second samples
can miss short waits or peaks; PostgreSQL/browser/generator memory is not included
in worker RSS. These byte totals are uncompressed local traffic, not production
cost forecasts. Full peer snapshots remain a bandwidth cost.

### Actual pixels under mixed load — same-worker run

[benchmark_scale_paint.py](../tests/web/benchmark_scale_paint.py) creates exactly
1,000 accounts and 100 projects: 998 HTTP actors and two separate browser contexts
in installed headless Chrome 154.0.8037.98. Both native clients use worker0;
eight HTTP actors share their room and the other HTTP rooms cross workers. This
is not a cross-worker two-browser test or a 1,000-browser test. Native viewport is
1440 × 960 at DPR1, with a 320 × 240 document and the original Pencil tool. The
renderer backend was not independently recorded, so no hardware-GPU claim is made.

All 998 HTTP actors had succeeded during active load before timed input. Each
complete paint observation stayed inside the active 12-second interval while
6,787 / 7,073 / 11,212 more HTTP requests progressed. Aggregate usable peer coverage
was complete over that interval, with at least 114 successful active requests per
HTTP actor; this is not proof that every actor delivered in every subsecond paint
bracket. The HTTP stage recorded 115,553 active successful PUTs, zero errors,
77.190% of nominal opportunities and two coalesced ticks. Scheduled completion was
p50 49.959 / p95 93.666 / p99 111.950 / max 187.984 ms.

| Trusted pointer input | Remote-pixel upper bound | Clock uncertainty |
|---|---:|---:|
| Cursor 1 | 235.656 ms | 5.959 ms |
| Cursor 2 | 220.709 ms | 14.034 ms |
| Held native Pencil | 213.264 ms | 13.115 ms |

The endpoint is the first matching CDP-swapped PNG, not a physical display.
All three samples passed the unchanged **500 ms / 15 ms uncertainty** limits with
no missing/invalid/capture-error samples. Three observations cannot establish
p99 tail reliability. No artificial network delay was added in this mixed run.

During the held preview, receiver native pixels/history/revision and all cloud
project revisions remained unchanged. After load drained, mouseup committed
revision 2. The peer native pixel, actual document readiness after reload, and
downloaded original `.pcraft` SHA-256 were verified. The resulting 5,089-byte
archive SHA-256 was
`50ec0246f89c71d8b5c7289c2c5675b150f4e3faf8925ad008e14eefdffca945`.
Save/reload correctness was checked after load, not timed as a durable-save SLO.
Every owned worker, browser, background thread, UUID database and temporary TLS
cluster was stopped/removed; shared application rows touched were zero.

### Direct worker0→worker1 confirmation

The additive `rpc-cross-worker-1000.json` run at 10:09:46–10:10:09 UTC used the exact same
optimized `90908c3f` backend and `c0a89fbd` WASM. Its two independent native contexts
connected directly to different loopback worker origins, without a request proxy; the
receiver worker's index/WASM hashes were checked before input. The 998 background HTTP
actors retained sticky distribution across eight workers, with eight sharing the native
pair's room. Counts, native/HTTP fixtures, three-second warmup, twelve-second active period,
8×4 worker pool, local verified database TLS and strict 500 ms / 15 ms paint gates remained
the same. This closes the earlier local cross-worker browser evidence gap, not a hosted
routing or 1,000-browser acceptance gap.

| Trusted pointer input | Remote-pixel upper bound | Clock uncertainty |
|---|---:|---:|
| Cursor 1 | 197.587 ms | 6.415 ms |
| Cursor 2 | 209.369 ms | 3.970 ms |
| Held native Pencil | 143.335 ms | 12.839 ms |

All three observations passed without invalid/missing/capture-error samples. Descriptive
p50 was 197.587 ms and p95/p99/maximum 209.369 ms; this small sample does not establish tail
reliability. Installed headless Chrome 154.0.8037.98 used 1440×960/DPR1. The GPU backend was
not recorded, and no physical display or WAN latency claim is made.

The HTTP stage completed 138,718 active PUT 200s, **92.6640%** of 149,700 nominal
opportunities, with zero errors including warmup and no coalesced ticks. PUT p50/p95/p99/max
was 7.195/23.454/31.326/51.840 ms; scheduled completion was
11.885/42.255/55.023/82.057 ms. Each HTTP actor completed at least 138 successful active
requests, and all 998 observed their room's HTTP peers over the full active window.
The three timed brackets contained 5,705 / 6,237 / 9,638 further HTTP requests. Neither
aggregate coverage nor those counts prove every actor's delivery within every bracket.
The nominal ratio remains below 100%, and `capacity_slo_certified` remains false.

Receiver native preview invariants passed. After load, canonical revision 2, peer native
pixels/reload and the 5,089-byte archive were verified against the same `50ec0246` SHA-256
above. These persistence checks were untimed. Owned cleanup passed with zero shared rows
touched, and artifact hashes stayed unchanged. Host load was 2.79/2.37/2.64 →
9.63/3.91/3.18; the lower observed values do not isolate a cross-worker performance benefit.

### Current deployment and guest evidence

Tofu deployment `dpl_5crMQxccD2ZaNAMi951jJ2jVQ43d` is recorded ready/serving source
`a17cea25d9dfcc84f68313b3e2d1e6e6beaa3cfb`. All 1,855 expected ZIP files matched stored
source in the retained round-trip comparison. The ZIP SHA-256
`2bbaef0b5b88c46b3bc3288cf964c508b8cb401a290d157d8f1fcdbf2e5a10a6` and Tofu source
snapshot identifier `9e9f67ba869e30965daac3a087c8825da7beabdaddb5486d4b300cd788b02f2b`
cover different representations; they must not be called identical hashes.

`hosted-guest-a17cea25/report.json` passed a fresh guest workspace/Create check, local
320×240 document, trusted Pencil gesture, complete exported-PNG pixel checks through native
undo/redo, and actual `.pcraft` picker reopen with preserved dimensions/layers/pixels.
Served WASM matched `c0a89fbd`; the fixture closed its own browser. Dialog fields/confirmation
and menu operations used the original command bridge. There was no sign-in, cloud write,
invitation or existing project access; mutation requests were blocked and none occurred.
This is hosted guest-functionality evidence, not hosted two-person latency, production
capacity, all-command parity or hardware-GPU acceptance. Exact receipt names and scope are
also recorded in [collaboration performance](collaboration-performance.md#deployed-guest-scope--source-a17cea25).

### Failures retained and reproduction

Evidence resides in the local `2026-10-08-release-continuation` bundle:
`rpc-tls-100.json`, `rpc-tls-1000.json`,
`exchange-v1-hardened-tls-1000.json`, `rpc-mixed-1000-r2.json`,
`rpc-cross-worker-1000.json`, their screenshots,
`rpc-candidate-binaries.json`, and the owned temporary TLS wrappers.

The initial `rpc-mixed-1000.json` retains three passing paint observations and a
later `durable_after_load` assertion failure. Source/screenshot review identified
a likely bridge-before-document readiness race in the fixture. The rerun reuses
the existing successful-open barrier, asserts saved dimensions/layer identities,
and records each safe durable checkpoint. No runtime or pixel/latency threshold
was weakened. The initial HTTP/load/pixel receipts are not discarded or attributed
to the later completed persistence check.

The 23 pure benchmark guard tests verify no-I/O dry plans, bounded local-only
execution, semantic response/coverage failures, active paint windows and cleanup.
The current API/security suites separately verify private function privileges,
fresh queued authorization, post-lock expiry and transaction-pool behavior.
Source-grounded tests and passing samples cannot guarantee absence of all bugs.

For a safe dry plan, run the mixed harness with `--output` and `--context` only.
It performs no filesystem, network, process, browser or database I/O. Execution
requires `--execute`, an explicit installed `--chrome`, a built WASM directory,
and an owned password-free literal loopback maintenance `/postgres` URL. Use the
same temporary TLS recipe below with its existing public CA. Do not point the
harness at Tofu, Supabase or real user accounts. Complete hosted two-account,
real invitation receipt, network/geography, larger-room, longer-duration and
fleet quota acceptance before claiming the production 1,000-user target.

## Historical stages — 2026-10-07

The remaining observations retain their original binaries and validator semantics.
They do not describe the 2026-10-08 candidate above.

**The 1,000-active-user, sub-500 ms input-to-remote-paint target is not met.**
Eight local workers completed every HTTP request they actually issued successfully,
but completed only 21.82% of the nominal request schedule. Scheduled request
completion reached 616.188 ms, before any receiver rendering. This is useful
backend evidence, not a whole-application capacity or production latency guarantee.

PhotoCraft Studio retains the original [PhotoCraft](https://github.com/storytold/photocraft)
Rust engine, native painting, document format and renderer. This work changes the
cloud adapter's admission and database coordination; it does not replace native
editing semantics. See the [fork code map](fork-code-map.md),
[security review](security-release-review.md) and
[collaboration architecture](collaboration-architecture.md).

## What was measured

The [bounded harness](../tests/web/benchmark_scale.py) used synthetic accounts and
native 64 × 48 `.pcraft` fixtures, each 1,586 bytes. It created a separate UUID
database for every run, started its own loopback workers and removed both afterward.
No production account, provider authentication, email or hosted load was involved.
The fixture SHA-256 was
`2dbc937653efa89c85872cb6424593013a62a3545ff1f0e5a4f58b5512498946`.

Full stages used three seconds of warmup, ten seconds of active measurement and ten
seconds of cooldown. All stages used the current 80 ms GET / 40 ms PUT schedule
and cursor payloads. Each logical client allowed one request in flight per
direction, coalesced missed ticks, and backed off failed reads/writes by 250/500 ms
before the next interval. At 1,000 clients the generator also imposed a global
1,000-request concurrency cap; up to 2,000 logical directions competed for it.
This bounded generator materially limits offered throughput under contention.

The nominal schedule is 37.5 requests per client per second: 12.5 reads and 25
writes. Thus the ten-second denominator is 37,500 opportunities for 100 clients,
or 375,000 for 1,000 clients. These are schedule opportunities, not a claim that
the harness transmitted every request. Reported success ratio is HTTP 200
completions divided by that denominator. Including overload responses in the
numerator would incorrectly make the single-worker 1,000-client result look like
28.56% instead of 18.61%.

- **HTTP service time** starts after generator admission and includes the local
  request/response exchange. It excludes the generator's admission queue.
- **Generator admission wait** measures that separate queue.
- **Scheduled completion lag** measures completion relative to the request's due
  time, including admission and scheduling delay. It also includes failed
  requests; it does not track an edit through subsequent retry backoff to delivery.
- **Remote paint latency** requires a trusted input event and matching receiver
  pixels. This HTTP harness does not measure it. The separate
  [paint observer](../tests/web/paint_latency.py) and
  [live browser benchmark](../tests/web/benchmark_live_collaboration.py) serve that
  purpose; their small-client results cannot be extrapolated to 1,000 clients.

The host was macOS 26.5.1 on arm64 with 12 logical CPUs. Generator, HTTP workers
and PostgreSQL shared this Mac. Owned builds, browser tests and other heavy tests
were paused during the release stages; unrelated host activity was unmeasured.
There was no artificial network RTT. These are short local observations, not an
isolated-machine capacity study or sustained-load soak.

## Optimized release results

Every release row used the same optimized binary, SHA-256
`d46b760c283140d8bb7aee7c9939a04eb8fad7f4943caf528922c3c7637e3779`.
An independently created temporary PostgreSQL cluster enforced TLS. Both the
service and harness verified its certificate and IP identity; production
`VerifyFull` was preserved. Recorded connections used TLS 1.3. HTTP itself was
loopback plaintext.

The 1,000-client scenarios distributed clients across **100 rooms of ten**, with
sticky, evenly assigned local workers and peers crossing worker boundaries.
This simulates a worker fleet; it does not reproduce Vercel/Tofu routing,
autoscaling, worker startup or geographical placement.

| Clients / workers × DB pool | Active HTTP 200 | Active HTTP 503 | Successful nominal schedule | Successful GET p99 / max (ms) | Successful PUT p99 / max (ms) | Scheduled completion p99 / max (ms) |
|---|---:|---:|---:|---:|---:|---:|
| 100 / 1 × 5 | 37,032 | 0 | 98.752% | 21.283 / 27.054 | 24.252 / 28.332 | 24.539 / 28.861 |
| 1,000 / 1 × 5 | 69,791 | 37,291 | 18.611% | 40.181 / 45.763 | 72.678 / 79.302 | 42.200 / 79.434 |
| 1,000 / 4 × 5 | 52,251 | 2,729 | 13.934% | 786.490 / 1,020.597 | 782.532 / 1,161.592 | 1,176.493 / 1,787.806 |
| 1,000 / 8 × 4 | 81,826 | 0 | 21.820% | 296.823 / 477.998 | 297.649 / 483.295 | 482.159 / 616.188 |

The eight-worker successful GET distribution was p50 98.999 / p95 206.141 ms;
PUT was p50 99.192 / p95 210.304 ms. Its generator queue was p50 116.110,
p95 218.240, p99 325.508, maximum 357.497 ms. Its 231,335 coalesced nominal ticks
and low completion ratio rule out treating the sub-500 ms HTTP service maxima
as a successful 1,000-user test. Four workers had a 777.412 ms queue p99 and
246,846 coalesced ticks.

All recorded 503 bodies in these release runs matched the bounded ordinary
admission response: 49,106 including warmup for one worker and 3,426 for four.
There were no recorded transport exceptions/timeouts or request/byte-budget
aborts in these stages. Fast rejection is still failure: the single-worker 503
p99 was approximately 0.355 ms and is excluded from the successful-service
columns above. The eight-worker report's `status: passed` means its observed
active HTTP gate passed; `capacity_slo_certified` remains false.

### Resource and cleanup evidence

| Stage | Requests including warmup | Response bytes including headers | Maximum sampled DB connections, including monitor | Maximum sampled aggregate worker RSS |
|---|---:|---:|---:|---:|
| 100 / 1 × 5 | 47,836 | 42,704,379 | 6 | 28.98 MiB |
| 1,000 / 1 × 5 | 139,316 | 115,728,916 | 6 | 80.69 MiB |
| 1,000 / 4 × 5 | 85,412 | 98,675,106 | 21 | 159.52 MiB |
| 1,000 / 8 × 4 | 106,740 | 126,565,667 | 33 | 222.28 MiB |

These are uncompressed HTTP byte totals over warmup plus active work, not
production egress or billable-message estimates. The temporary cluster allowed
40 connections. Every sampled worker remained within its configured pool
of five or four, and aggregate usage stayed within the worker pool sum plus
one monitor. No sampled PostgreSQL lock waiters or database deadlocks were
recorded. One-second samples can miss short waits and transient resource peaks;
they do not establish that locks or CPU were irrelevant.

The four/eight-worker maximum sampled aggregate process CPU was 193.6% / 177.0%.
One-minute host load rose from 4.58 to 14.04 / 8.32 to 12.62 respectively.
PostgreSQL CPU/RSS and unrelated processes were not attributed. The generator's
child `ru_maxrss` is not a sum of worker memory; the table uses summed per-worker
samples instead. These results do not isolate a causal reason why the
four-worker stage was slower.

The recorded 1,000-client process descriptor limit was 1,048,575 soft; the harness
did not change it. All stages stopped every worker they created, dropped only
their own UUID database, and reported zero shared application rows touched.
The temporary TLS clusters and their certificate directories were also removed.
Native project revisions remained unchanged. These runs performed no durable
save, archive round trip or browser reload.

## Debug comparison and implemented bounds

The earlier matched debug/plaintext runs retained failures as well as successes.
They used the same cursor workload and phase lengths. These binaries also differ
in security/admission changes, so this is not an isolated optimization experiment.

| Debug candidate | Clients | Active 200 / 503 | Successful nominal schedule | GET / PUT p99 (ms) |
|---|---:|---:|---:|---:|
| Baseline `e393cdfe` | 10 | 3,680 / 0 | 98.133% | 6.709 / 9.216 |
| Candidate `0944f4e3`, 128 ordinary permits | 10 | 3,680 / 0 | 98.133% | 11.787 / 10.659 |
| Baseline `e393cdfe` | 100 | 37,293 / 0 | 99.448% | 38.909 / 40.655 |
| Candidate `0944f4e3`, 128 ordinary permits | 100 | 36,116 / 131 | 96.309% | 22.284 / 23.670 * |
| Candidate `bacbeee0`, 256 ordinary permits | 100 | 36,770 / 0 | 98.053% | 25.245 / 32.262 |

\* The failed candidate's original percentile aggregates include 503 responses;
they must not be described as improved successful-request performance. The
256-permit run's maximum scheduled completion was 41.854 ms. Subsequent security
fixes were included in the final release binary, so `bacbeee0` is not that binary.

The [live handlers](../apps/photocraft-cloud/src/live.rs) and
[lock migration](../apps/photocraft-cloud/migrations/003_scale.sql) now allow
eligible active-tab renewals under shared project locks while keeping exclusive
session locks. New, expired or rebound admissions roll back before taking the
exclusive project path; they never upgrade a held shared lock. Identity,
membership, revision and lease eligibility are evaluated again after waiting.
Duplicate sequence numbers do not extend TTL. Durable authorization changes
and logout use cooperating barriers, preserving fresh revocation semantics.

This reduces unnecessary room-wide serialization without removing database work.
A successful GET still opens a transaction, runs an authorization-and-peer query
and commits. A steady-state PUT opens a transaction, authorizes/takes locks,
reauthorizes/writes and commits; admission fallback adds another transaction.
At the nominal 1,000-client schedule that is 37,500 HTTP transactions per second
before fallback, with 12,500 peer-list reads and 25,000 state writes. This is
source-derived demand, not achieved throughput. Peer fan-out, SQL work and
database RTT remain central transport costs.

[Admission controls](../apps/photocraft-cloud/src/security.rs) bound each worker
to 256 ordinary, four authentication and two commit operations, with body-lifetime
permits and deadlines. The default database pool stays at five; the service
supports explicit configuration and bounded acquisition. Increasing worker count
multiplies pool demand. No project/session quota was raised for the benchmark:
64 active previews per project, eight per session, 256 retained session tab
watermarks, two-second leases, 64 KiB payloads, 256 events and 1,024 points remain.

The final [12 live contract tests](../tests/web/test_live.py) plus
[eight concurrency tests](../tests/web/test_live_scale.py) passed together in
2.812 seconds. They exercise admission races, expiry while queued, replay TTL,
membership revocation, logout and rebinding. The
[six harness safety tests](../tests/web/test_benchmark_scale.py) passed, including
dry-run/no-I/O, unsafe target rejection, fleet pool limits, and cleanup after
partial worker startup. These checks establish specific invariants, not complete
security or performance coverage.

## Evidence and safe reproduction

The local verification bundle is named `2026-10-07-security-scale`. It contains:

- `baseline-10.json`, `baseline-100.json`, `candidate-debug-10.json`,
  `candidate-debug-100.json`, `candidate-debug-100-admission256.json`, and
  `candidate-debug-comparison.json`.
- `release-tls-100.json`, `release-tls-1000-rooms10.json`,
  `release-tls-1000-workers4-pool5.json`,
  `release-tls-1000-workers8-pool4.json`, and `fleet-comparison.json`.
- Matching worker/wrapper logs and fleet `-resources.csv`, `.svg` and `.png`
  artifacts with per-worker CPU/RSS and database activity/lock samples.
- `final-live-api.log`, `benchmark-fleet-guards-final.log` and
  `tls-cluster-wrapper.py`, the exact local temporary-cluster setup helper.

The initial TLS smoke failed while serializing a PostgreSQL version value from
an SQL_ASCII cluster; its log remains `release-tls-smoke-wrapper.log`. The helper
was corrected to initialize UTF-8 and decode that diagnostic. The separate
`release-tls-smoke-r2.json` records the successful two-client smoke. No failed
stage was replaced with a later pass under the same artifact name. The bundle
is local evidence, not an assertion that a public CI artifact was uploaded.

Use Python with the existing `requests` and `psycopg` test dependencies. Build and
generate the original native fixture as described in [web/cloud development](web-cloud.md):

```sh
CARGO_TARGET_DIR=target/cloud cargo build --locked --release -p photocraft-cloud
mkdir -p test-results/scale
CARGO_TARGET_DIR=target/cloud cargo run --locked -p photocraft-cloud --example fixture -- test-results/scale/fixture.pcraft
python tests/web/benchmark_scale.py --help
python tests/web/benchmark_scale.py --clients 1000 --workers 8 --db-pool-size 4 \
  --scenario rooms10 --pacing current --payload cursor \
  --warmup 3 --duration 10 --cooldown 10 --max-inflight 1000 \
  --max-requests 600000 --max-response-mib 512 \
  --output test-results/scale/plan.json --context 'Dry plan only'
```

The last command is a dry run: it creates no database, process, network traffic
or output file. `--execute` is deliberately required for load.

For release execution, first prepare an **owned temporary loopback PostgreSQL
13+ cluster**, separate from any working database. The recorded setup used
UTF-8, 40 maximum connections, a temporary CA and server certificate with
`IP:127.0.0.1` SAN, `hostssl` access only, and rejection of non-TLS connections.
Use its assigned port and public CA PEM below. The harness accepts only a
password-free literal `127.0.0.1` maintenance `/postgres` URL. Do not substitute
a hosted URL, production credentials, an existing application database or a
TLS-verification bypass.

```sh
: "${SCALE_TLS_PORT:?Set the port of your own temporary TLS cluster}"
: "${SCALE_TLS_CA:?Set its public CA PEM path}"
python tests/web/benchmark_scale.py --execute \
  --binary target/cloud/release/photocraft-cloud \
  --database "postgresql://photocraft_test@127.0.0.1:${SCALE_TLS_PORT}/postgres" \
  --tls-ca "$SCALE_TLS_CA" --fixture test-results/scale/fixture.pcraft \
  --clients 1000 --workers 4 --db-pool-size 5 \
  --scenario rooms10 --pacing current --payload cursor \
  --warmup 3 --duration 10 --cooldown 10 --max-inflight 1000 \
  --max-requests 600000 --max-response-mib 512 \
  --output test-results/scale/release-1000-workers4-pool5.json \
  --context 'Disposable local TLS fleet; record host contention here'
```

Start with `--clients 100 --workers 1 --db-pool-size 5 --max-inflight 200
--max-requests 100000 --max-response-mib 100` and a distinct output name. Only
after reviewing that result, run the bounded 1,000-client stage. The eight-worker
comparison changes `--workers 8 --db-pool-size 4` and the output name; eight
workers with pools of five are rejected by the harness's 32-connection budget.
Keep failed JSON/logs. Stop and remove only the temporary cluster created for
this exercise after the harness has stopped its workers and dropped its UUID
database. The harness does not own or stop the maintenance cluster itself.

## Remaining release gates and next checkpoint

A 1,000-person hot room was **not run and is not supported by these results**.
It exceeds ordinary membership and active-preview limits; direct fixture seeding
would bypass normal admission. Native rendering also bounds peer handling and
active gesture previews. Raising these caps would require a separate UX,
authorization, memory and fan-out design.

These stages omit legacy presence polling, durable saves/uploads, native brush
payloads, rendering, browser lifecycle, packet loss, WAN RTT, cold workers and
long-duration fairness. They use directly seeded local identities, not real
hosted sign-in. There is no verified provider entitlement for 1,000 connections,
the required message volume, database connections, egress or function duration.
Local pool bounds do not establish purchased Tofu/Supabase/Vercel capacity.

The next transport checkpoint is a small authenticated notification/preview
adapter that reduces per-client polling and per-pointer database transactions,
while retaining native commands, renderer and durable `.pcraft` commits. First
verify the actual provider's supported transport, regional placement, connection
and message budgets, and revocation behavior. The
[sub-500 ms plan](sub500-collaboration-plan.md) documents the managed-transport
questions; source availability is not hosted readiness. Do not solve this by
caching authorization indefinitely, silently dropping overload errors or blindly
increasing every worker's database pool.

The next acceptance run must include trusted input-to-matching-remote-pixel upper
bounds, every error and maximum, at controlled 0/50/100 ms RTT; mixed cursor and
native gesture traffic; current membership/session revocation; reconnect,
expiry, duplicates and out-of-order delivery; and committed native archive,
reload and undo correctness. Separate or explicitly account for load-generator
limits, include saves/presence, and inspect per-client progress as well as
aggregate throughput. Only then is a bounded, separately authorized hosted
capacity trial meaningful. This release can ship the tested safety and
coordination improvements without claiming the unmet 1,000-user latency target.
