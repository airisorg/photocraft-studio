# Collaboration: current behavior, gaps and acceptance

Source audit, 2026-10-07; live exchange update, 2026-10-08. Collaboration is a priority
immediately after reliability and UI defects. This document separates implemented behavior, measured evidence and proposed work.
PhotoCraft's document model, command registry, compositor, native format and editor remain
the foundation; a second editor or a replacement document model is unnecessary.

The single-exchange HTTP protocol below describes the locally built source candidate reviewed
on 2026-10-08. The private RPC candidate has bounded local HTTP-only and mixed-workload
measurements: 998 modeled HTTP actors plus two actual native browser clients, with three
passing paint observations. See [the measured evidence](collaboration-performance.md) for
exact artifacts, completion ratios and retained failed attempts. This does not establish
1,000-browser or production capacity; deployment and hosted acceptance remain separate gates.
Earlier pre-RPC measurements retain their own backend/browser artifact and workload scope.

## What exists today

The browser edits locally through the original Rust engine. After the first cloud save,
the adapter waits for 150 ms of idle time, with the pointer released, before autosaving. It serializes a complete
native `.pcraft` document, uploads sequential 512 KiB chunks, and commits a version under a
project row lock. A checksum and expected revision protect the commit. Another browser
receives authorized live state/revision through the candidate's 80 ms GET-or-PUT exchange
while visible, and downloads the committed document when its
own document is unchanged and it is not actively interacting. Applying that remote version
replaces the local document state and resets its undo history; older work remains in cloud
version history. Continuous editing can defer autosave. Supported cursor and gesture previews
use the separate transient path described below; they are not durable operation sync.

The backend compares native manifests and content-addressed blobs when the base is stale.
Compatible independent changes can merge. Same-property, raster-tile, ordering and document
conflicts preserve the local work and require a copy or comparison. This conservative rule
is preferable to claiming that simultaneous painting has been resolved when it has not.

Membership supports owner, edit and view roles. Comments, saved versions, public view links,
email invitations and presence exist. Live state includes authenticated named cursors and
bounded native Brush/Pencil/Eraser/Move previews, but not selection ownership. Invitations grant membership before attempting a sign-in
email, so a mail failure can coexist with granted access. Provider acceptance does not prove
arrival in an inbox. Shared links and invitation delivery are separate acceptance journeys.

Implementation pointers: `apps/photocraft-web/src/cloud.rs` (`update`, `save`, `sync`),
`apps/photocraft-cloud/src/lib.rs` (`presence`, upload/commit and role handlers),
`apps/photocraft-cloud/src/merge.rs`, and `apps/photocraft-cloud/src/invitations.rs`.

## Comparison with mature collaborative editors

[Figma's published multiplayer architecture](https://www.figma.com/blog/how-figmas-multiplayer-technology-works/)
uses ongoing WebSocket updates and explicit conflict handling. Its
[2022 reliability report](https://www.figma.com/blog/making-multiplayer-more-reliable/)
describes incremental updates, durable journaling, recovery validation, and 95% persistence
within roughly 600 ms at that time. That historical persistence measurement is not a current
SLA or a remote-render latency measurement, and cannot be compared directly with an HTTP
request benchmark. [Canva describes](https://www.canva.com/newsroom/news/canva-cements-position-collaboration-platform-amidst-rapid-mau-growth/)
visible collaborator selections and contributions as they happen, alongside immediate comments.

PhotoCraft Studio does not currently match that interaction contract. Missing capabilities
include live selections, durable incremental edit delivery, predictable collaborative undo,
same-object/stroke concurrency, reliable catch-up without whole-document downloads, and a
measured hosted multi-user latency/capacity envelope. A working Share dialog is not evidence
that those capabilities exist.

## Bounded native live path

`crates/ui-egui/src/collaboration.rs` is optional view state. Small hooks in the existing
canvas capture resolved native gestures and reuse `LiveStroke::begin_with/push`, `moved`,
document transforms and native damage-region rendering. Remote previews use COW documents
separate from the real Session, document revision and undo. Only one remote drawing gesture
is shown at a time; receiver-local work takes precedence. Unsupported channels, symmetry,
duplicate moves and expensive or oversized strokes explicitly use saved-version updates.

The candidate Rust web adapter in `apps/photocraft-web/src/live.rs` uses one 80 ms exchange
cadence and sends cumulative ordered gesture events. Changed state takes a PUT slot; when
there is no change or due heartbeat it uses GET. An unchanged published cursor/gesture gets
a PUT heartbeat after 500 ms instead of another GET. Request duration and browser scheduling
can slow either path. Only one GET or PUT is pending within the current document/account
generation. Reset rejects old replies but does not immediately abort an old fetch, so a
switch can briefly overlap an old request with the new generation; each fetch has a two-second
abort deadline. This is not a global semaphore across switches.

A new generation starts with GET. A successful PUT supplies the same authorized role and
peer snapshot as GET, so active clients need no independent read loop. A legacy PUT ACK
without that snapshot forces GET before another PUT. A current-base 409 blocks previews
for that base until the saved version advances; a delayed previous-base 409 preserves newer
gesture events and forces prompt reconciliation without the ordinary network-error backoff.
PUT 403 pauses gesture emission and reads the current role before deciding whether view
access remains. These forced reads and retry paths are separate from ordinary cadence.

Cookie and expected-account guards remain unchanged. Persisted tab/transport identities
survive reload; wire gesture IDs continue increasing even when native counters restart.
Hidden, unbound, signed-out and unavailable editors stop live transport. Acknowledged
cancellation clears only its matching gesture payload; an old ACK cannot clear a newer one.
The canonical save, native rendering and undo paths are unchanged by this transport candidate.

`apps/photocraft-cloud/src/live.rs` and the additive `002_live.sql` / `003_scale.sql`
migrations keep latest transient state shared across workers. Startup installs the private
`photocraft.exchange_live_v1` PL/pgSQL function through `live::migration()` under the existing
`ready_db` setup transaction and advisory lock. It is `VOLATILE`, `SECURITY INVOKER`, fixes
`search_path` to `pg_catalog`, and revokes `PUBLIC` execution. The HTTP service invokes it
with bound arguments and its existing database privileges; this is not a public browser or
Supabase PostgREST RPC and requires no new provider token or signing key.

The function first denies unauthorized callers before acquiring private project locks.
It then takes ordered session/project advisory locks and runs a separate authorization,
lease/admission, conditional write and peer-snapshot statement. `VOLATILE` permits fresh
internal query snapshots, so authorization after the lock wait is not the caller's earlier
admission snapshot. This relies on PostgreSQL's normal `READ COMMITTED` behavior; a deployment
changing transaction isolation must revalidate the queued-revocation contract.
See [PostgreSQL function volatility](https://www.postgresql.org/docs/current/xfunc-volatility.html).

Lease eligibility uses a materialized `clock_timestamp()` observed in that post-lock
statement. The outer function call's `statement_timestamp()` would predate its lock wait;
using it could wrongly treat an expired renewal as active. Session expiry and returned TTLs
also use wall-clock time. Shared project locks permit active renewals; session locks remain
exclusive. A new, expired or rebound slot rolls back the shared attempt and retries exclusive
admission in a new transaction; it never upgrades a held shared lock. Durable access/revision
changes and logout retain their coordinating project/session barriers.

GET and successful PUT share the same fresh authorization and peer filter. PUT awaits the
transaction commit before returning `{accepted, seq, revision, role, peers}`. The write
statement's pre-write peer snapshot is sufficient because the sender's own session/tab row
is excluded. Duplicate/stale sequence ACKs can return a fresh authorized snapshot without
updating that sender's sequence or lease. No separate post-commit peer-read transaction is added.

The private function consolidates application-to-database calls; it does not make the whole
exchange one database round trip. PUT still uses explicit `BEGIN`, function invocation and
awaited `COMMIT` (or rollback/retry), and the function executes internal statements. GET also
retains an explicit short transaction: SQLx's unnamed prepare/bind exchange must not cross
an idle transaction-pool boundary. Fresh authorization and awaited write commits remain intact.
Revoked/expired publishers are filtered from both response paths. Viewers may publish cursors
but not drawing gestures. State expires after two seconds; duplicate/stale sequences do not
extend the lease. For both GET and PUT, clients conservatively subtract the full elapsed time
from request start through response handling from each peer's remaining lease. Expired peers
are not admitted, and a network failure does not restart a previous lease. A response admitted
before revocation cannot be recalled from the network.

Bounds are 64 KiB per update, 256 events, 1,024 points, eight active tabs per session,
256 retained session watermarks and 64 active room slots; the native view shows at most
32 peer cursors. Brush previews also have a conservative 16-million estimated dab-pixel
work limit. Watermarks survive clear/expiry and disappear with their session. These are
admission bounds, not demonstrated capacity or an unlimited multiplayer contract.

The first transport uses existing HTTP/PostgreSQL credentials and needs no new provider
secret. Supabase private Broadcast remains a later optimization: the opaque cookie does
not become a Realtime JWT by exposing the anonymous key. The safe provider-session bridge,
authorization/revocation contract and actual quotas in the [sub-500 plan](sub500-collaboration-plan.md)
must be established before enabling that path.

## Known scaling costs

- The 1.5-second presence interval implies about 0.67 requests/second per open bound document
  in an active client. At 1,000 such clients, the arithmetic is about 667 requests/second,
  before saves, comments, loads or reconnects. This is a workload estimate, not measured
  capacity. Browser throttling and request latency can alter the actual rate.
- A warm presence request currently executes five SQL statements: session lookup, role
  lookup, presence upsert, active-name lookup, and revision lookup. The example above would
  imply roughly 3,333 SQL statements/second. Each worker defaults to five database connections;
  `PHOTOCRAFT_DB_POOL_SIZE` accepts 1–32 per worker. There is no application-wide fleet pool
  cap: the local load harness's aggregate 32-connection budget is a test constraint. More
  workers do not make the shared database or provider connection allowance unlimited.
- The candidate's ordinary 80 ms live cadence implies up to 12.5 GET-or-PUT exchanges/second
  per visible bound client, before forced reconciliation/legacy fallback and alongside
  existing presence/save requests. It replaces the previous independent 80 ms read and
  40 ms write schedules. Idle clients still poll; unchanged published state heartbeats
  after 500 ms. Both response types transfer cumulative authorized peer state, while PUT
  also acquires bounded per-session/project transaction locks. Fewer requests do not prove
  reduced database time, bounded hot-room bandwidth or 1,000-user capacity. Measure the
  candidate's exact workload and errors before changing the recorded capacity conclusion;
  provider push/streaming and incremental native persistence remain later transport work.
- Full-document transfers and full version blobs scale with document size and save frequency.
  Commits serialize per project, and the quota check sums retained version sizes. Raster
  uploads and merge work can dominate both latency and memory even when HTTP handlers are fast.
- Current application bounds are 100 MiB per document and 1 GiB per owner including history.
  Invitation creation enforces 100 collaborators. Lists currently cap projects at 500,
  versions/comments at 200, and visible presence names at 50. These are source limits, not
  statements about Tofu's purchased database allowance or demonstrated concurrent capacity.
- The renderer uses the user's browser/GPU, but collaboration transport, persistence and
  mail remain server concerns. Fast WebGL does not reduce the autosave/polling delays.

No production p50/p95/p99 edit-to-remote-render benchmark or capacity ceiling has been
established. Local HTTP results must name fixture size, concurrency, binary profile, machine,
database, warm/cold state, sample count and errors. They must never be reported as Tofu-wide
or production performance. See the separate bounded local benchmark report.

## Preserve the core while improving collaboration

1. Ship the cold-worker and invitation-state fixes first. Keep failed-upload, auth, recovery,
   same-ID retry, exact-content reopen, revoke and Trash/Restore regressions as release gates.
2. Measure the current path before changing timing: local edit, serialization, upload, commit,
   notification receipt, download/apply and remote paint. Track durable acknowledgment
   separately from visible remote updates and email delivery.
3. Reduce needless work: pause/back off inactive presence, combine authorized metadata reads
   where measurements justify it, and avoid redundant downloads. Preserve prompt revocation
   checks and use backoff/jitter; do not lower the polling interval as a substitute for sync.
4. Evaluate Tofu's managed Supabase Realtime for authorized revision notifications and
   ephemeral cursors. [Supabase recommends Broadcast](https://supabase.com/docs/guides/realtime/subscribing-to-database-changes)
   for scalable database notifications. Verify the actual Tofu project's enabled services,
   quotas and authentication bridge before implementation: our opaque application cookie
   is not a Supabase Realtime JWT. An anonymous key must not authorize a private project.
   [Realtime limits](https://supabase.com/docs/guides/realtime/limits) and
   [reports](https://supabase.com/docs/guides/realtime/reports) are service-specific; an
   unlimited-storage assumption does not remove connection or message limits.
5. Add a small collaboration adapter around native command/document changes, with stable
   operation identity, authorization, ordering, idempotency and a durable sequence. Reuse
   native manifest properties and blobs. Keep full `.pcraft` snapshots for checkpoints,
   exports, recovery and compatibility; transmit changed data between checkpoints.
6. Prototype concurrency semantics before wiring every command: independent property edits,
   same-property edits, create/delete/reorder, raster tiles, text and undo need explicit
   outcomes. Applying arbitrary command replay or a generic CRDT to the entire engine is
   not automatically correct. Validate replay against the native result byte/pixel oracle.
7. Separate large immutable content from hot coordination metadata if measured size/history
   costs justify it. Verify managed storage access, cleanup, retention and restore before
   moving existing data. Do not introduce a new database or paid service merely for this audit.

## Verification gates and proposed targets

Targets below are proposed acceptance criteria, not observed performance or product claims.
Use a declared regional network profile (for example RTT at most 100 ms), document class,
browser, device and number of collaborators. Publish p50/p95/p99, maximum, errors and sample
count; do not average away failures or mix warm/cold measurements.

| Journey | Evidence required | Proposed target / invariant |
|---|---|---|
| Cursor/selection | Two independent users; timestamp input and remote paint | p95 at most 150 ms under declared profile |
| Small property edit | Native action to visible remote result; no manual save | p95 at most 250 ms under declared profile |
| Durable save acknowledgment | Commit sequence survives forced worker restart | p95 at most 1 s; no acknowledged edit lost |
| Reconnect | Offline edits, stale revision, duplicate/out-of-order messages | Converge or preserve an explicit conflict; no silent overwrite |
| Large raster change | Representative 2/10/24 MP documents and changed tiles | Report bytes, serialization/apply time and frame stalls separately |
| Permission removal | Existing connection, refresh, reconnect and download | Removed member cannot obtain new private data |
| Invitation | Exact approved recipient, inbox receipt, actual acceptance | Correct account sees project; wrong account denied |
| Load | 1/5/20, then controlled higher concurrency across and within projects | A measured operating envelope, not a maximum-user guess |

Fault tests include worker replacement, timeout after commit, retry after unknown outcome,
offline recovery, account changes, permission revocation, malformed operations and concurrent
delete/edit. Compare final native structure and pixels across clients. UI checks include
saving/saved/offline/conflict labels, inline errors, pending action feedback, focus restoration
and readable collaborator identity. Local synthetic identities, provider simulation, real
hosted identities and physical-device evidence remain separate categories.

Production load testing is not part of this benchmark. The immediate hosted acceptance is
one deliberate normal journey through the serving build, plus approved real invitation and
independent-user checks. Broad probes would interfere with the app and its hosting protections.

## Realtime source check and smallest first adapter

Read-only check, 2026-10-07: Tofu's local `origin/main` snapshot
`b81400aa3612560350dd1a38a943dabbb40e050d`, not a verified hosted release. Its
`packages/orchestrate/src/supabase.ts:352` provisions the Supabase URL, anonymous key and
database URL. `packages/orchestrate/src/app-auth-broker.ts:829` issues a GoTrue handoff for
the application's own Supabase project. The reviewed shared/MCP contracts expose database
status and app-auth configuration, but do not establish a Realtime enablement or quota
contract. The installed Tofu skill also explicitly excludes an app-hosted resident socket
server. A managed Realtime service would therefore carry sockets; PhotoCraft's backend
would remain request-driven. No platform configuration or credentials were accessed here.

PhotoCraft's `finish_sign_in` verifies that provider identity, then replaces the provider
session with an opaque `pc_session`. It currently retains neither an access nor refresh
token for a browser subscription. Its private database schema has no Realtime policies or
notification trigger. Working Google/email login is consequently not proof that private
Realtime channels can be joined. Hosted availability, private-channel configuration,
database function permissions, JWT lifetime and connection/message quotas remain unknown.

The first prototype should deliver **committed revision invalidations only**:

1. Extend the existing GoTrue handoff with an explicit provider-session bridge tied to the
   authenticated application session. Keep refresh credentials server-side, and expose only
   the current user's expiring access token through a same-origin, non-cacheable endpoint.
   Verify that its subject equals the PhotoCraft account UUID; rotate, expire and revoke the
   bridge with that session. An anonymous key or a `pc_session` value cannot replace this
   JWT, and no service-role credential belongs in the browser.
2. Use private, receive-only topics. Supabase evaluates `realtime.messages` RLS on join/token
   renewal and caches the result; membership removal alone leaves an existing connection
   authorized until token expiry or refresh. For the first prototype, consider a separate
   non-secret topic ID per application session: RLS binds it to `auth.uid()`, while publishing
   selects only currently authorized, unexpired sessions. Deleted sessions and revoked
   members then stop being event recipients even if a socket remains open. Serialize
   membership changes with the project commit boundary and test the race explicitly. This
   introduces per-session fan-out that must be measured; it is not a demonstrated scaling
   improvement. [Supabase authorization](https://supabase.com/docs/guides/realtime/authorization).
3. After a successful revision update, insert only `{project_id, revision}` notifications
   through a transaction-scoped database trigger using `realtime.send(..., true)`, subject
   to verified managed permissions. Do not broadcast native file bytes, full project rows,
   email addresses or uncommitted edits. Database Broadcast uses committed database changes;
   it avoids relying on a detached application task surviving the HTTP response. A rolled
   back save must emit no revision. [Database Broadcast](https://supabase.com/docs/guides/realtime/broadcast).
4. A thin browser transport adapter tells the existing Rust cloud adapter to reconcile a
   newer revision through its authorized HTTP endpoints. Keep native serialization, merge,
   conflict preservation and apply rules. Reconnect, duplicate/out-of-order events and
   missed delivery must converge via a revision check; retain a backed-off polling fallback.
   The [Realtime protocol](https://supabase.com/docs/guides/realtime/protocol) supports a
   bounded WebSocket adapter; heartbeat, token renewal and reconnect handling still need
   implementation and tests. Cursor broadcasts and native operation deltas are later work.

Before enabling this adapter, prove two real identities, wrong-user/anonymous denial,
logout and membership revocation on an already-open socket, rollback silence, reconnect
catch-up, worker replacement, and exact native-document recovery. Measure commit-to-peer
state separately from edit-to-peer paint. At the time of this Realtime source check, the
3.5-second autosave delay alone exceeded the proposed 250 ms edit target. The current native
save path described above uses a shorter idle delay, but whole-document transfer and apply
still require separate measurement; revision push alone does not establish that target.
No Realtime implementation, hosted probe or infrastructure change was performed in this
source check.
