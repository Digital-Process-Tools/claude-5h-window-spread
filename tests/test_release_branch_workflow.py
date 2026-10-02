"""#12 -- the workflow that publishes the `release` branch.

`.github/workflows/release-branch.yml` is triggered by a version tag or manual
dispatch, builds/checks/smoke-tests the release tree in a read-only job, and
only a second job -- the only one with write permissions -- pushes it, and only
after rebuilding and matching byte-for-byte what the first job verified.

Needs PyYAML to parse the workflow; skipped (not failed) when it is not
installed, the way the rest of this repo's stdlib-unittest test suite treats
an optional dependency. CI installs PyYAML in the workflow itself, and the oss
assemble step installs it too (`gh-workflow: tests.yml` does not, which is
why this test must tolerate its absence rather than require it).
"""

from __future__ import annotations

import re
import unittest
from pathlib import Path

try:
    import yaml
except ImportError:
    yaml = None

REPO_ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = REPO_ROOT / ".github" / "workflows" / "release-branch.yml"

SHA_PINNED = re.compile(r"^[\w.-]+/[\w.-]+@[0-9a-f]{40}$")


@unittest.skipIf(yaml is None, "PyYAML is not installed")
class WorkflowTest(unittest.TestCase):
    def setUp(self):
        self.assertTrue(WORKFLOW.exists(), f"{WORKFLOW} does not exist (#12)")
        self.doc = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        self.text = WORKFLOW.read_text(encoding="utf-8")

    def test_triggers_on_version_tags_and_manual_dispatch_with_a_ref(self):
        on = self.doc.get(True, self.doc.get("on"))
        self.assertEqual(on["push"]["tags"], ["v*"])
        self.assertNotIn("branches", on["push"],
                         "a push to main must not publish the release branch")
        self.assertIn("ref", on["workflow_dispatch"]["inputs"])

    def test_permissions_are_read_only_except_the_publishing_job(self):
        self.assertEqual(self.doc["permissions"], {"contents": "read"})
        writers = [name for name, job in self.doc["jobs"].items()
                  if (job.get("permissions") or {}).get("contents") == "write"]
        self.assertEqual(len(writers), 1, writers)
        for job in self.doc["jobs"].values():
            self.assertLessEqual(set((job.get("permissions") or {}).keys()), {"contents"})

    def test_actions_are_pinned_to_a_commit_sha(self):
        uses = [s["uses"] for j in self.doc["jobs"].values() for s in j["steps"] if "uses" in s]
        self.assertTrue(uses)
        for u in uses:
            self.assertRegex(u, SHA_PINNED, u)

    def test_pushes_only_after_build_check_and_smoke(self):
        steps = [s for j in self.doc["jobs"].values() for s in j["steps"]]
        runs = [s.get("run", "") for s in steps]
        idx = {key: next(i for i, r in enumerate(runs) if key in r)
               for key in ("build_release_tree.py", "check_release_tree.py",
                           "smoke_release_tree.py", "git push")}
        self.assertLess(idx["build_release_tree.py"], idx["check_release_tree.py"])
        self.assertLess(idx["check_release_tree.py"], idx["smoke_release_tree.py"])
        self.assertLess(idx["smoke_release_tree.py"], idx["git push"])
        self.assertIn("refs/heads/release", self.text)
        # Pushed on top of the previous release commit, never forced: a catalogue
        # that pinned an older release sha must still be able to fetch it.
        self.assertIn("git push origin", self.text)
        self.assertNotIn("--force origin", self.text)
        self.assertNotIn("push -f", self.text)
        self.assertIn('-p "$parent"', self.text)
        # No step may be allowed to fail open on the way to the push.
        for s in steps:
            self.assertFalse(s.get("continue-on-error"), s.get("name"))

    def test_claude_cli_version_is_pinned_at_or_above_2_1_281(self):
        """Earlier CLIs warn "Unknown field" on documentationUrl/supportUrl in
        plugin.json, and `--strict` turns that warning into a failure."""
        m = re.search(r'CLAUDE_CLI_VERSION:\s*"([0-9.]+)"', self.text)
        self.assertIsNotNone(m, "CLAUDE_CLI_VERSION not found in the workflow")
        self.assertGreaterEqual(tuple(int(x) for x in m.group(1).split(".")), (2, 1, 281))


if __name__ == "__main__":
    unittest.main()
