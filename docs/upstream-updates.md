# Staying on upstream PhotoCraft

This private repository preserves the Git history of
[storytold/photocraft](https://github.com/storytold/photocraft). It is a private derivative,
not a separate editor implementation. GitHub currently reports it as a private mirror,
not a repository with GitHub's fork metadata. All native engine, document, codec, tool,
command and egui changes continue to arrive through Git merges.

## Boundaries that keep updates manageable

- `crates/` remains the original editor. Keep browser portability fixes small and useful
  upstream. Do not create a duplicate document model, compositor, toolset or command list.
- `apps/photocraft-web` adapts native services and adds the workspace/cloud shell.
- `apps/photocraft-cloud` provides HTTP persistence, identity, sharing and collaboration.
- `tests/web` exercises the adapter and user journeys in addition to the original suites.
- `packaging/web` and the release workflow handle deployment; no Tofu credential belongs
  in the source tree or GitHub Actions secrets for this pipeline.

## Automatic path

`.github/workflows/update-and-release.yml` is configured to run on main changes, manual
dispatch, and a six-hour schedule. Scheduled jobs can be delayed by GitHub; updates are
not instantaneous.

1. Fetch `storytold/photocraft` main from its fixed upstream URL. Merge into an isolated
   candidate descended from our current main. Never replace our tree with upstream files.
2. A conflict stops the run without changing main or the deployed branch. A candidate is
   retained under `automation/upstream-*` when acceptance fails so it can be inspected.
3. Call the existing CI and browser/cloud workflows at the candidate's full commit SHA:
   native tests on Linux/macOS/Windows, corpora, layering, WASM, lint, generated scorecard,
   service/auth/startup/API tests, and browser functionality/visual regressions. The manual
   fuzz job remains manual; do not describe a routine update as an exhaustive fuzz run.
4. Package the tested browser build with its Rust server source. `release-source.json`
   identifies the source commit and WASM checksum; dirty or mismatched packages are refused.
5. A fresh Linux runner downloads that exact ZIP and native fixture. The container gate
   builds its Dockerfile, verifies the served index/WASM bytes, and runs the trusted API,
   live-state and live-lock suites against the release image using verified database TLS.
   A failed build, contract, receipt or cleanup stops the reusable browser workflow.
6. A separate write-permission job verifies the exact-package container receipt before
   calling promotion. It then checks that main has not changed while tests ran and
   atomically advances main and `tofu-release`. No force push is used. The generated branch
   holds prebuilt `public/` files for the existing Dockerfile, avoiding a second WASM build
   at Tofu. Its history contains build artifacts; main remains source-only.
7. After owner activation, configure Tofu to follow **tofu-release**. Its safety check,
   deployment and visitor check remain additional gates. Failed preparation or deployment
   keeps the prior live app.
8. An unchanged scheduled check does not rebuild or redeploy. Successful candidates are
   removed after promotion; failed candidates remain for investigation.

Reusable workflows are called explicitly because a push made with `GITHUB_TOKEN` must not
be assumed to trigger another workflow. Acceptance jobs have read permission and do not
persist checkout credentials. Promotion uses the caller revision's script, not candidate
scripts. See GitHub's [token behavior](https://docs.github.com/en/actions/concepts/security/github_token)
and [workflow reuse](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

## Exact-package container gate

The earlier workflow tested a locally built debug service with `CLOUD_LOCAL_DEV=1`, then
packaged source and prebuilt WASM. It did not prove that the ZIP's Dockerfile built and
started the release service, retained its runtime libraries, served the tested browser
bytes or connected with production-style database TLS. Successful source/browser checks
and package checksums did not cover that transition.

`web-cloud.yml` now has `container-package`, dependent on `acceptance`, on a fresh
`ubuntu-latest` runner with read-only repository permission and no persisted checkout
credentials. It takes the gate helper and tests from the PR base SHA, or the caller's
workflow SHA for a reusable/manual run. Candidate Python helpers and test copies in the
ZIP are not executed on the QA host. The downloaded candidate source is built inside its
Docker image; the existing Rust compiler identity checks and locked release build apply.

`packaging/web/check-container-package.py --execute` is restricted to Linux GitHub CI and
the local Docker socket. Its default mode only prints a plan. It validates bounded archive
paths/provenance, creates an owned temporary build context, builds the image and starts
an owned loopback PostgreSQL cluster, CA and UUID database. The image receives the test CA
and explicit local database/origin settings, without `CLOUD_LOCAL_DEV` or provider secrets.
Database clients use TLS `verify-full`. The gate waits for `/api/config` schema readiness
and checks the exact served index and WASM lengths/hashes before running trusted
`test_api.py`, `test_live.py` and `test_live_scale.py` against that image. Browser, auth,
archive and other suites remain separate acceptance checks; this container job does not
rerun the complete browser/paint suite or a scale benchmark.

The `container-package-evidence` artifact includes logs and `container-package.json`.
The receipt binds the candidate SHA, ZIP SHA-256, WASM/index bytes and hashes, native fixture
bytes/hash, trusted suite/helper hashes and built image ID. It requires successful Linux
execution, all three suites, verified TLS, matching served bytes and completed cleanup of
the owned container, image, database, cluster and context. The promotion job downloads
both evidence artifacts and runs `--verify-receipt` from its trusted checkout **before**
`upstream-release.py promote`. A missing or changed receipt/package/fixture/test helper,
failed suite or incomplete cleanup is a failure, not permission to skip this gate.

This validates one built image, not bit-for-bit reproducibility of a later Tofu image:
Tofu still rebuilds the server from the tested source/Dockerfile, and base-image tags or
system package repositories can change. The prebuilt browser bytes remain hash-bound.
Actual provider startup, database configuration and hosted interaction need their own
post-deployment evidence.

## One-time activation and current limits

At the 2026-10-08 gate review, GitHub Actions remained blocked by the account's payment or
spending restriction. Local tests of the pipeline do not establish a successful
hosted Actions run. The new actual Linux Docker/TLS execution is **unverified**; mocked
container-guard tests and local macOS backend/browser passes are not a container pass.

The first gate introduction needs a reviewed trusted-branch bootstrap: install the helper,
required suites and workflow in the trusted base/caller revision before expecting its
container job to run. A PR base without the helper cannot validate its own newly proposed
gate; do not work around that by executing candidate QA helpers or manufacturing a receipt.
After the reviewed bootstrap and account restriction are resolved, run **Upstream and
tested web release** against a fresh candidate. The first fully passing run creates
`tofu-release`. Never bypass failing gates to make the update appear enabled.

The existing Tofu app was uploaded as a ZIP; its GitHub source is null and automatic
updates are disabled. After a passing release exists, the owner must use **Update code**
on this app's **Overview** or **Versions** page, connect GitHub with their own grant,
choose `FZ2000/photocraft`, branch `tofu-release`, repository root, and enable
**Automatic updates** on the **Source** card in **Versions**. Keep the existing app,
database and identity settings. This cannot be enabled by the coding-agent API.

Read Tofu's GitHub status after connection: repository, branch and last accepted commit
must match the prepared release. Then verify one actual update from candidate through CI,
Tofu readiness and a scoped hosted browser journey. Until that evidence exists, call this
pipeline **configured, awaiting activation**, not active automatic deployment.

## Failure and recovery

Resolve merge conflicts on the candidate using the native implementation wherever possible,
then validate the resolved source through the same workflow. A stale-main error means
rerun against current main. Tests may expose an upstream regression: keep the prior release
and fix or report it; do not lower assertions. Turning off Tofu automatic updates leaves the
current app serving. Restore a specific earlier version through Tofu when authorized; a
restore still needs a fresh visitor check.

`python -m unittest discover -s tests/web -p test_upstream_release.py` uses disposable Git
remotes to check merge preservation, conflicts, stale main, artifact identity/checksum,
unsafe paths, consecutive releases and no-op schedules. It never pushes to GitHub or Tofu.
