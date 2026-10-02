"""#12 -- the smoke test run on a built release tree before it is published.

This plugin ships no hooks (no `hooks/hooks.json`), so `.github/scripts/
smoke_release_tree.py` here is a rewrite of claude-remember's version: it runs
`claude plugin validate --strict` and then exercises `scripts/window-spread.py`
only in ways that cannot touch the real OS scheduler -- `--help` for every
subcommand and a `compute` call, never `install`/`uninstall`/`list`.

The fixtures build a tiny stand-in tree so the harness itself is tested: a
harness that never ran the script would pass "a broken script fails the smoke"
only if that test did not also exist next to "a working script passes".
"""

from __future__ import annotations

import importlib.util
import json
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "smoke_release_tree.py"
BUILD = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
CONFIG = REPO_ROOT / ".github" / "release-branch.json"


def _load(path: Path, name: str):
    assert path.exists(), f"{path} does not exist (#12)"
    spec = importlib.util.spec_from_file_location(name, path)
    mod = importlib.util.module_from_spec(spec)
    sys.modules[name] = mod
    spec.loader.exec_module(mod)
    return mod


WORKING_SCRIPT = '''#!/usr/bin/env python3
import argparse, json, sys

p = argparse.ArgumentParser()
sub = p.add_subparsers(dest="cmd", required=True)
pc = sub.add_parser("compute")
pc.add_argument("--blocks", required=True)
sub.add_parser("install")
sub.add_parser("uninstall")
sub.add_parser("list")
args = p.parse_args()
if args.cmd == "compute":
    json.dump({"blocks": [], "spread": {"pings": ["06:00"]},
               "natural": {}, "improvement": {}}, sys.stdout)
    sys.stdout.write("\\n")
sys.exit(0)
'''

BROKEN_SCRIPT = '''#!/usr/bin/env python3
import sys
sys.stderr.write("boom\\n")
sys.exit(3)
'''


def _tree(tmp_path: Path, script_body: str) -> Path:
    root = tmp_path / "tree"
    (root / "scripts").mkdir(parents=True)
    (root / "scripts" / "window-spread.py").write_text(script_body, encoding="utf-8")
    return root


class ScriptSmokeTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: subprocess.run(["rm", "-rf", str(self.tmp)]))

    def test_a_working_script_passes_every_call(self):
        mod = _load(SCRIPT, "smoke_release_tree")
        tree = _tree(self.tmp, WORKING_SCRIPT)
        result = mod.run_smoke(tree, validate="skip")
        self.assertTrue(result.ok, result.report())
        labels = [r.label for r in result.runs]
        self.assertIn("window-spread.py --help", labels)
        self.assertIn("window-spread.py compute", labels)
        self.assertTrue(all(r.returncode == 0 for r in result.runs))

    def test_a_failing_script_fails_the_smoke_and_is_named(self):
        mod = _load(SCRIPT, "smoke_release_tree")
        tree = _tree(self.tmp, BROKEN_SCRIPT)
        result = mod.run_smoke(tree, validate="skip")
        self.assertFalse(result.ok)
        bad = [r for r in result.runs if r.returncode != 0]
        self.assertTrue(bad)
        self.assertIn("boom", result.report())

    def test_compute_output_is_checked_not_just_its_exit_code(self):
        """A script that exits 0 but prints garbage for `compute` must still
        fail the smoke -- a passing exit code alone proves nothing about the
        JSON a caller (the skill) actually reads."""
        mod = _load(SCRIPT, "smoke_release_tree")
        garbage = '''#!/usr/bin/env python3
import sys
if "compute" in sys.argv:
    sys.stdout.write("not json\\n")
    sys.exit(0)
sys.exit(0)
'''
        tree = _tree(self.tmp, garbage)
        result = mod.run_smoke(tree, validate="skip")
        self.assertFalse(result.ok)
        self.assertTrue(any("JSON" in e for e in result.errors), result.errors)

    def test_no_script_is_a_note_not_a_failure(self):
        mod = _load(SCRIPT, "smoke_release_tree")
        root = self.tmp / "tree"
        root.mkdir()
        result = mod.run_smoke(root, validate="skip")
        self.assertTrue(result.ok, result.report())
        self.assertTrue(any("no scripts/window-spread.py" in n for n in result.notes))

    def test_never_calls_install_uninstall_or_list_without_help(self):
        """These subcommands touch the real OS scheduler; the smoke test must
        only ever reach them through `--help`, which never runs their code."""
        mod = _load(SCRIPT, "smoke_release_tree")
        result = mod.run_smoke(_tree(self.tmp, WORKING_SCRIPT), validate="skip")
        for r in result.runs:
            for sub in ("install", "uninstall", "list"):
                if f" {sub} " in f" {r.label} " or r.label.endswith(f" {sub}"):
                    self.assertIn("--help", r.label, r.label)

    def test_validate_required_without_a_claude_binary_fails_loudly(self):
        mod = _load(SCRIPT, "smoke_release_tree")
        tree = _tree(self.tmp, WORKING_SCRIPT)
        result = mod.run_smoke(tree, validate="require", claude_bin=str(self.tmp / "no-claude"))
        self.assertFalse(result.ok)
        self.assertIn("claude", result.report())
        self.assertIn("not found", result.report())

    def test_validate_skipped_says_so_out_loud(self):
        mod = _load(SCRIPT, "smoke_release_tree")
        result = mod.run_smoke(_tree(self.tmp, WORKING_SCRIPT), validate="skip")
        self.assertTrue(result.ok)
        self.assertIn("SKIPPED", result.report())
        self.assertIn("validate", result.report())

    def test_this_repository_built_tree_passes_the_smoke(self):
        """Integration: the real build, the real script, in isolation."""
        build = _load(BUILD, "build_release_tree")
        out = self.tmp / "out"
        build.build(REPO_ROOT, "HEAD", out, build.load_config(CONFIG))
        mod = _load(SCRIPT, "smoke_release_tree")
        result = mod.run_smoke(out, validate="skip")
        self.assertTrue(result.ok, result.report())
        labels = {r.label for r in result.runs}
        self.assertIn("window-spread.py compute", labels)


if __name__ == "__main__":
    unittest.main()
