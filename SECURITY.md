# Security reporting and release boundaries

Modified by PhotoCraft Studio on 2026-10-08 to document this independent fork's reporting and release policy; upstream license and attribution notices remain unchanged.

Please report a vulnerability privately to the repository maintainer. Use
[GitHub private vulnerability reporting](https://github.com/airisorg/photocraft-studio/security/advisories/new)
when that feature is enabled; otherwise contact the maintainer through their
GitHub profile to arrange a private channel before sharing exploit details.
Do not put credentials, session cookies, private artwork, customer identifiers,
or a working exploit against a hosted service in a public issue.

Include the affected commit or release, the trust boundary involved, expected
and observed behavior, and a small synthetic reproduction. Local isolated
reproductions are preferred. Testing someone else's account, production load
testing, destructive requests, or accessing private content requires separate
permission. This policy is not a bug-bounty or a promise of response times.

Only the current maintained branch is a remediation target. A passing local
check is not evidence that a fix has reached a hosted deployment. Release notes
should identify the source revision, tested artifact, and deployment evidence.

## Before publishing source or a release

- Run `python3 packaging/web/check-publication.py` from a clean checkout with
  `gitleaks` installed. It checks all locally reachable Git history, rejects
  retained restricted brand artwork and personal home paths, and reports only
  counts and categories. Fetch the refs intended for publication first. It does
  not inspect unavailable refs or prove that all sensitive information is absent.
- Review Git author identities and public contact details separately. Secret
  scanners cannot decide whether a person consented to publishing their identity.
- Keep the upstream copyright, both project licenses, `NOTICE`, asset licenses,
  and `ATTRIBUTION.md`. Modified upstream files must carry change notices when
  distributing under Apache-2.0. The restricted upstream marks are not covered
  by the source-code licenses. A clean current tree does not remove old assets
  or private data from Git history. Do not rewrite history without owner approval.
- Review the current RustSec advisories against the locked dependencies and the
  actual target's dependency graph. An optional lockfile package is not proof of
  runtime exposure; record the reason for each exception rather than silently
  suppressing it. Recheck advisories for each release.
- Pin privileged CI actions to full commit IDs and verify downloaded build tools.
  Tests and artifact provenance do not replace reviewing upstream or workflow
  changes that execute in a release job.

## Cloud native-document limits

Cloud uploads retain a 100 MiB wire limit. Validation additionally bounds the
expanded ZIP to 128 MiB, the manifest to 4 MiB, an individual blob to 64 MiB,
archive entries to 16,384, and layers to 4,096. The native loader verifies every
referenced tile/blob and its content hash under a 256 MiB decoded-payload budget.
All three merge inputs share that budget; the merged output is checked separately
after their decoded documents have been released. These are cloud limits, not a
change to the desktop file format or its loading defaults.

One blocking validation worker is admitted per service process. Waiting is
bounded before upload snapshots are allocated, and cancellation does not release
the worker's permit early. These bounds reduce resource amplification; they are
not a process-RSS guarantee or provider-wide abuse protection. Container memory,
worker counts, database capacity, and ingress controls still need deployment
limits. Oversized or malformed saves are rejected explicitly while preserving
the user's local document and the previous saved version.
