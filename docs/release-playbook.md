# Release playbook

The release playbook shared by every crafting app lives in craftrules:
`../craftrules/release/playbook.md`
(`storytold/craftrules`). It is the canonical copy; don't duplicate it here.

This fork's verified environment does not include that separate checkout. Do not
infer that the upstream playbook ran: follow the locally available [web guide](web-cloud.md),
[release review](security-release-review.md) and [upstream-update gates](upstream-updates.md)
for this adaptation.

PhotoCraft is its reference implementation (`.github/workflows/release.yml`, `packaging/`,
`cargo xtask version`). PhotoCraft-specific details are in [releasing.md](releasing.md).
