# Scale release review — 2026-10-07

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
