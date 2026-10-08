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
5. A separate write-permission job checks that main has not changed while tests ran and
   atomically advances main and `tofu-release`. No force push is used. The generated branch
   holds prebuilt `public/` files for the existing Dockerfile, avoiding a second WASM build
   at Tofu. Its history contains build artifacts; main remains source-only.
6. After owner activation, configure Tofu to follow **tofu-release**. Its safety check,
   deployment and visitor check remain additional gates. Failed preparation or deployment
   keeps the prior live app.
7. An unchanged scheduled check does not rebuild or redeploy. Successful candidates are
   removed after promotion; failed candidates remain for investigation.

Reusable workflows are called explicitly because a push made with `GITHUB_TOKEN` must not
be assumed to trigger another workflow. Acceptance jobs have read permission and do not
persist checkout credentials. Promotion uses the caller revision's script, not candidate
scripts. See GitHub's [token behavior](https://docs.github.com/en/actions/concepts/security/github_token)
and [workflow reuse](https://docs.github.com/en/actions/how-tos/reuse-automations/reuse-workflows).

## One-time activation and current limits

As of the 2026-10-07 setup, GitHub Actions could not start because the account's payment or
spending limit blocked runners. Local tests of the pipeline do not establish a successful
hosted Actions run. Resolve the account restriction and run **Upstream and tested web
release**; the first passing run creates `tofu-release`. Never bypass failing gates to
make the update appear enabled.

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
