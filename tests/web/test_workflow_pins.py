"""Every action a workflow uses is pinned to a full commit SHA.

A tag is a moving target: `@v4` means "whatever v4 points at today", so a
compromised or retagged upstream runs inside our CI — and for any job that
reaches a self-hosted runner, inside our infrastructure. This repository
publishes publicly, so the supply chain is part of the release surface.

Local actions (`./…`) and container references (`docker://…`) are exempt:
they do not resolve through a mutable tag.
"""

import re
import unittest
from pathlib import Path

WORKFLOWS = Path(__file__).resolve().parents[2] / ".github" / "workflows"
FULL_SHA = re.compile(r"^[0-9a-f]{40}$")
USES = re.compile(r"\s*(?:- )?uses:\s*(\S+)")


def unpinned_actions() -> list[str]:
    findings = []
    for workflow in sorted(WORKFLOWS.glob("*.yml")):
        for number, line in enumerate(workflow.read_text().splitlines(), 1):
            match = USES.match(line)
            if match is None:
                continue
            reference = match.group(1)
            if reference.startswith(("./", "docker://")):
                continue
            _, _, version = reference.partition("@")
            if not FULL_SHA.match(version):
                findings.append(f"{workflow.name}:{number}: {reference}")
    return findings


class ActionPinning(unittest.TestCase):
    def test_every_action_is_pinned_to_a_commit(self):
        findings = unpinned_actions()
        self.assertEqual(
            [],
            findings,
            "unpinned actions let a mutable tag run inside CI:\n  " + "\n  ".join(findings),
        )

    def test_the_check_itself_catches_an_unpinned_action(self):
        # Negative control: the rule must fail on the thing it forbids.
        sample = "      - uses: actions/checkout@v4"
        reference = USES.match(sample).group(1)
        _, _, version = reference.partition("@")
        self.assertFalse(FULL_SHA.match(version))
        self.assertTrue(FULL_SHA.match("11d5960a326750d5838078e36cf38b85af677262"))


if __name__ == "__main__":
    unittest.main()
