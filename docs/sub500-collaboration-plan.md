# Input-to-collaborator paint below 500 ms

Source and service-documentation audit, 2026-10-07, against PhotoCraft `8309d1f`.
This is an implementation and acceptance plan, not a claim that the deployed application
already provides live multiplayer editing. Keep the original Rust/WASM editor, native
document model, brush engine, GPU/CPU compositor, history and `.pcraft` format.

## The current bottleneck

`apps/photocraft-web/src/cloud.rs:1317` waits for 3,500 ms of idle time before saving.
Peers poll every 1,500 ms. Saving generates a thumbnail and complete native ZIP, uploads
512 KiB chunks sequentially, then commits; peers download and replace the whole document.
Continuous editing can postpone the save indefinitely. Presence currently includes names,
roles and revision, without cursor positions. A historical ten-rename local sample measured
3,636 ms median edit-to-commit and 4,646 ms edit-to-peer state. Those measurements were
neither cursor delivery nor remote paint. See [existing evidence](collaboration-performance.md).

Reducing polling intervals would increase requests and still leave serialization, saving
and native interaction boundaries in the critical path. Realtime revision notifications
alone cannot remove the 3.5-second save delay.

## Recommended transport and native reuse

Use [Supabase Broadcast](https://supabase.com/docs/guides/realtime/broadcast) for ephemeral
cursor and gesture previews. Keep [Presence](https://supabase.com/docs/guides/realtime/presence)
for joining/leaving and online status; its documented five track/untrack calls per client
per 30 seconds make it unsuitable for cursor movement. The cursor path must not perform
document serialization, thumbnail generation or durable document writes per movement.
The first authorized database publisher described below still incurs database work and
Realtime message writes per coalesced batch; measure that cost explicitly.

The first bounded native adapter should support one presenter and an observing collaborator:

1. Transform cursor positions through `ViewXform::active/to_doc/to_screen`, including zoom,
   pan and flip. Coalesce updates in 30–50 ms batches; attach actor/tab ID, project/base,
   sequence, expiry and gesture ID. Drop superseded cursor updates rather than queueing lag.
2. Add a narrow optional gesture boundary for Brush/Pencil/Eraser and Move. Reuse
   `LiveStroke::begin_with/push` and `layer_multi_cmds::moved`; send resolved target,
   brush parameters/seed and ordered points. Do not send generic tool events into the
   receiver's active tool or replay unresolved command-journal entries.
3. Paint peers' cursors and native gesture previews separately from authoritative
   `DocState` and undo history. Request an egui repaint on arrival. Cancel expired,
   mismatched-base and revoked previews; preserve receiver-local work. Replace a preview
   only with its matching acknowledged native checkpoint.
4. Keep the current save/recovery path as the initial durability boundary and distinguish
   visible remote preview from a saved edit. A relay ACK does not prove remote paint or
   durable persistence.

This first adapter proves fast remote feedback for supported gestures. It does not prove
unrestricted two-writer editing, shared undo or fast large filters. A local edit should
continue immediately; network delay must not block the original native input path.

For durable updates, expose a narrow manifest-plus-missing-objects API around
`PcraftWriter`'s existing content-addressed tile cache. Send only changed native objects,
not another complete ZIP after every action. Use authorized, idempotent operation IDs,
project sequence numbers, expected-base validation and transactionally published commit
notifications. Retain full `.pcraft` checkpoints for export and catch-up. Reconnect recovers
from the durable sequence/checkpoint; transient Broadcast replay is not a document journal.

## Authentication and immediate revocation

The existing opaque `pc_session` is not a Supabase JWT. `finish_sign_in` currently discards
the GoTrue access and refresh credentials after verifying the account. Existing cookies
cannot recover discarded credentials. An anonymous key is not a person's identity.

Implement a provider-session bridge on new sign-in: securely retain the refresh credential
server-side, bound to the verified PhotoCraft account, provider session and application
session. Return only the current account's provider-signed expiring access JWT from a
same-origin, authenticated, non-cacheable endpoint. The endpoint cannot independently
shorten the JWT's signed expiry. Define the separate encryption key's custody, serialized
refresh rotation, expiry, logout and replay rules before storing provider credentials.
This standard GoTrue bridge needs no custom JWT signer. An imported signing key is a
separate configuration option, not something the current app can assume it possesses.
Never expose a service-role key.
[Supabase JWT integration](https://supabase.com/docs/guides/auth/jwts).

[Private-channel authorization](https://supabase.com/docs/guides/realtime/authorization)
is cached after join/token renewal. Deleting membership does not immediately disconnect
an already-authorized room subscriber. Start with receive-only destinations per application
session: client SELECT policy binds the topic to the verified account, provider session
claim and corresponding live application session; client publishing is denied. A topic UUID
or `auth.uid()` alone is insufficient to enforce that session boundary.
Authenticated ingress validates each batch and publishes only to current authorized,
unexpired recipients, serialized with membership/revocation changes. Measure this fan-out and
HTTP ingress cost. Do not enable direct project-room publishing until a proven revocation
barrier prevents new private data reaching removed members on existing sockets.

Choose the trusted database publisher using `realtime.send`, subject to verified
managed-database permissions. It writes Realtime messages per preview batch; it does not
write a native document revision for each cursor movement. Commit notifications must be
published transactionally; rollbacks produce no edit notification. Receiving user JWTs
alone do not authorize server publishing to other sessions' private topics. A server-only
publisher credential is a separate alternative if database-backed publishing proves too
slow; none is currently injected, and it must never reach a browser.
Account-generation and expected-account guards in the existing web adapter remain mandatory.

## Vercel and Tofu capabilities

The newer [Vercel WebSocket documentation](https://vercel.com/docs/functions/websockets)
documents native support in public beta, requiring Fluid compute. Connections stay on one
function instance only for their lifetime;
reconnects may land elsewhere. The documented default maximum duration is 300 seconds,
with 800 seconds on Pro/Enterprise and a 1,800-second beta extension for Node.js, Bun and
Python. This is not proof of the Rust container path through Tofu. External room coordination
and durable recovery remain necessary even when the function hosts the socket.

The installed Tofu skill excludes resident/socket-only services. Tofu's dated container
spike establishes request-driven HTTP serving, but explicitly leaves regions, maxDuration
and scaling beyond idle startup unverified. Prefer managed Supabase sockets for this app;
do not introduce a second Node frontend or claim a Tofu-hosted gateway is already supported.

A read of this project's Tofu database metadata returned `ready`, provider `supabase`, broad
region `americas`, and three built-in Realtime tables. It does not establish Realtime join
permissions, service quotas or an exact database/function region. Place authoritative handlers
near the actual primary database once verified. CDN proximity does not remove database RTT.
[Vercel function regions](https://vercel.com/docs/functions/configuring-functions/region),
[Supabase architecture](https://supabase.com/docs/guides/realtime/architecture).

A read-only Tofu source audit pinned `origin/main` at
`e8e9e8e6db8ac557729b94a8aeaace45eed89de7`. The managed path sends a Supabase smart region
group, rather than mapping `americas` to a fixed AWS region. It injects `SUPABASE_URL`,
the publishable/anonymous key, database URL and public CA certificate; it does not inject
a service-role key or JWT signing secret. Its database readiness checks cover database,
REST and auth, optionally storage, without proving Realtime readiness. Private Realtime
policies and provider-session credentials are application work. This source snapshot is
not proof of the deployed Tofu implementation or the project's exact credential categories.

Supabase's documented defaults distinguish storage from Realtime capacity: Free has 200
connections/100 messages per second; Pro has 500/500; Pro without spend cap and Team have
10,000/2,500. Actual Tofu allocations remain unverified. For ten users sending at 20 Hz,
one socket each, excluding the sender:

- One shared project-room publication produces 200 sends plus 1,800 deliveries per second:
  2,000 billable messages. Immediate removal from cached room subscriptions is unresolved.
- Separate recipient topics produce 1,800 sends plus 1,800 deliveries per second:
  3,600 billable messages before additional traffic. This is the proposed first secure path.

These are billing-count estimates, not measured throughput or automatic equivalence to
admission limits. Coalescing, visibility subscriptions, payload bounds and backpressure
are required; verify how the allocated service enforces its limits. Initial two-client
proof and larger-room capacity are separate gates. [Service limits](https://supabase.com/docs/guides/realtime/limits),
[message accounting](https://supabase.com/docs/guides/platform/manage-your-usage/realtime-messages).

| Capability | Use in this app | Boundary to verify |
|---|---|---|
| Supabase Broadcast | Coalesced cursors/native previews and committed revision notifications | Private join/publish authority, revocation, fan-out, queue/backpressure and delivery latency |
| Supabase Presence | Online identities and active-document membership | Join/leave and expiry; no per-pointer `track` calls |
| Postgres Changes | Occasional app records if useful | Per-subscriber authorization and serialized processing make it a poor per-pointer write path |
| Vercel region placement | Authoritative API near primary database | Actual primary region and existing container configuration, before changing placement |
| Vercel Fluid/WebSockets | Possible later cookie-authenticated gateway | Public beta, Rust/container route support, instance lifetime and cross-instance coordination |
| Existing native tile cache | Incremental committed document objects | Format round trips, missing-object recovery, CAS/idempotency and collaborative undo |

[Postgres Changes scaling guidance](https://supabase.com/docs/guides/realtime/postgres-changes)
explains the per-subscriber checks and ordered processing. [Fluid compute](https://vercel.com/docs/fluid-compute)
documents cold-start optimizations and runtime support; it does not prove this Tofu
container's configuration or make its document sync incremental. Neither a CDN nor a
local GPU removes the current network save/polling delays.

## Native semantics that must be settled

- The engine resolves commands using local selected layers, colors, settings and selection;
  the current journal can retain unresolved parameters. It is not a deterministic multiplayer log.
- Layer/document IDs are process-local counters. Independent creation needs coordinated
  allocation/remapping compatible with the loader's existing ID-remapping rule.
- Native undo restores whole snapshots. Collaborative undo needs conditional inverse edits
  or explicit rebasing; keeping old snapshots after remote edits can erase peers' work.
- Current merge conservatively treats tile arrays, text, transforms and ordering as atomic.
  Concurrent same-object painting is not resolved by changing transport.
- Heavy WASM jobs run inline. A blocked browser thread cannot paint within 500 ms even when
  transport is fast. Large operations require explicit progress, worker isolation and a
  declared workload envelope, while preserving the native engine's results.

## Acceptance and release gates

Measure trusted pointer/key input to matching pixels in the remote browser's swapped frame.
Use two distinct authenticated accounts, independent contexts and a unique canvas pattern
per operation. Record input trust/time, frame swap timestamp, calibration uncertainty,
invalid captures, errors, misses and raw samples. CDP captures prove compositor output,
not physical display photon timing; the first observed matching frame is a conservative
upper bound when frames are omitted. State polling, request ACK, `requestAnimationFrame`
and GPU submission are diagnostic stages, not the visual endpoint.

The default gate is every valid observed operation's upper latency bound below 500 ms,
with no correctness failures. Report p50/p95/p99/max separately; do not silently substitute
p95 for the user's threshold. Ten samples' p95/p99 are effectively the maximum, not a stable
tail estimate. Always report network RTT, bandwidth, renderer, viewport/DPR, document size,
native hashes, concurrency and host load. Do not promise 500 ms across every network,
background-tab state or arbitrarily large filter.

| Gate | Required evidence |
|---|---|
| Measurement oracle | Stale/unchanged pixels fail; correct state with frozen canvas fails; 600 ms delayed paint misses deadline; unsupported cursor delivery is reported explicitly |
| Cursor/native preview | Paced trusted pointer input; cursor/stroke prefixes appear during the gesture; zoom/pan/flip/DPR and cancel handled |
| Committed native delta | Shape/text/property/brush changes produce matching pixels and exact native structure/tile hashes; reload persists them |
| Authorization | Two real hosted identities; anonymous/wrong-user denial; logout, expiry and revocation on an already-open socket; race barrier tested |
| Recovery | Duplicates/out-of-order events, disconnects, worker restart and unknown commit ACK preserve acknowledged work and catch up safely |
| Multiwriter | Separate/same layers, creation/order/delete and undo preserve peer work or expose an explicit recoverable conflict |
| Capacity | Bounded local 2/5/20-client tests; measured fan-out, payload bytes and deadline misses; verified provider limits before any hosted load |

Implement and verify in that order: measurement baseline, authenticated revision push,
cursor/native previews, native object deltas, then multiwriter/undo semantics. Release each
scope honestly. A zero-gap claim or an estimated 384 ms transport budget cannot substitute
for input-to-remote-paint evidence.

The first cursor/native-preview experiment does not require native object deltas, region
relocation or a custom JWT signer. Those are later optimizations or different identity
configuration. A bounded HTTP preview experiment could reuse `pc_session` to establish
the native adapter before provider setup, but would establish neither service scale nor
the hosted 500 ms target.
