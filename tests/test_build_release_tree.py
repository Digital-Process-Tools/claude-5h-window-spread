"""#12 -- the build script that turns a git ref into the slim `release` tree.

The Anthropic plugin directory holds any plugin folder with a non-image file of
256 KiB or more, more than 512 files, or a `.gitattributes` carrying
export-ignore/export-subst/filter. `main` keeps everything; `.github/scripts/
build_release_tree.py` extracts a ref through `git ls-tree` + `git cat-file`
(never `git archive`, which would need export-ignore), drops a deny-list, cuts
CHANGELOG.md to the latest released section, and rewrites links that would
otherwise point at removed paths.

Ported from claude-remember's test_release_branch_build_851.py (pytest) to
stdlib unittest, since this repo's CI runs `python -m unittest discover -s
tests` and installs no pytest. Every "must be removed" assertion is paired
with a "must be kept" one: a build that produced an empty tree would pass
every removal check, and a build that copied everything would pass every
keep check.
"""

from __future__ import annotations

import importlib.util
import json
import os
import stat
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "build_release_tree.py"
CONFIG = REPO_ROOT / ".github" / "release-branch.json"

SLUG = "Example-Org/example-plugin"
RAW = "https://raw.githubusercontent.com/Example-Org/example-plugin/main/"
BLOB = "https://github.com/Example-Org/example-plugin/blob/main/"


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#12)"
    spec = importlib.util.spec_from_file_location("build_release_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["build_release_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


def _git_env() -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith("GIT_")}
    env.update({
        "GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
        "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def _git(repo: Path, *args: str) -> str:
    return subprocess.run(
        ["git", "-C", str(repo), *args], check=True, capture_output=True,
        text=True, env=_git_env(),
    ).stdout


CHANGELOG = """# Changelog

All notable changes to this project will be documented in this file.

## [Unreleased]

### Added

- something not released yet (must not ship)

## [0.2.0] - 2026-01-02 -- second

### Fixed

- the latest released fix

## [0.1.0] - 2026-01-01 -- first

- the oldest entry (must not ship)

[Unreleased]: https://github.com/Example-Org/example-plugin/compare/v0.2.0...HEAD
[0.2.0]: https://github.com/Example-Org/example-plugin/releases/tag/v0.2.0
[0.1.0]: https://github.com/Example-Org/example-plugin/releases/tag/v0.1.0
"""

README = """# Example

![logo](docs/logo.png)

<p align="center"><img src="docs/logo.png" width="200"></p>

[![License](https://img.shields.io/badge/x-y-z)](LICENSE)

Read [the guide](docs/guide.md#install) or [the changelog](CHANGELOG.md).

Absolute stays: [site](https://example.com/docs/guide.md).

Anchor stays: [below](#example).

```
![in a fence](docs/logo.png)
```

Inline code stays: `[x](docs/guide.md)`.

[ref-style]: docs/guide.md
"""

SKILL = """---
name: s
description: d
---

See [the guide](../../docs/guide.md) and [hooks](../../hooks/hooks.json).
"""


def _make_repo(tmp_path: Path, *, changelog: str = CHANGELOG,
               extra: dict | None = None) -> Path:
    repo = tmp_path / "src"
    files = {
        ".claude-plugin/plugin.json": json.dumps({"name": "example", "version": "0.2.0"}),
        "scripts/run.sh": "#!/bin/sh\necho hi\n",
        "scripts/run-tests.sh": "#!/bin/sh\nunittest\n",
        "pipeline/__init__.py": "",
        "skills/s/SKILL.md": SKILL,
        "tests/test_x.py": "def test_x():\n    pass\n",
        "docs/guide.md": "# Guide\n",
        "docs/logo.png": "\x89PNG fake\n",
        ".github/workflows/x.yml": "name: x\n",
        ".claude/jit-context/rule.md": "rule\n",
        "CLAUDE.md": "# dev notes\n",
        "LICENSE": "license\n",
        "README.md": README,
        "CHANGELOG.md": changelog,
    }
    files.update(extra or {})
    for rel, text in files.items():
        p = repo / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    subprocess.run(["git", "init", "-q", str(repo)], check=True, env=_git_env())
    _git(repo, "add", "-A")
    _git(repo, "update-index", "--chmod=+x", "scripts/run.sh")
    _git(repo, "commit", "-q", "-m", "init")
    _git(repo, "tag", "v0.2.0")
    return repo


def _config(**over) -> dict:
    cfg = {
        "repo": SLUG,
        "default_branch": "main",
        "deny": ["tests/", "docs/", ".github/", ".claude/", "CLAUDE.md",
                 "scripts/run-tests.sh"],
        "changelog": "CHANGELOG.md",
        "rewrite_links": True,
    }
    cfg.update(over)
    return cfg


def _build(tmp_path: Path, repo: Path, cfg: dict | None = None, ref: str = "v0.2.0") -> Path:
    mod = _load()
    out = tmp_path / "out"
    mod.build(repo, ref, out, cfg or _config())
    return out


def _files(root: Path) -> set:
    return {p.relative_to(root).as_posix() for p in root.rglob("*") if p.is_file()}


class DenyListTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_denied_paths_are_removed_and_everything_else_is_kept(self):
        out = _build(self.tmp, _make_repo(self.tmp))
        files = _files(out)
        for gone in ("tests/test_x.py", "docs/guide.md", "docs/logo.png",
                     ".github/workflows/x.yml", ".claude/jit-context/rule.md",
                     "CLAUDE.md", "scripts/run-tests.sh"):
            self.assertNotIn(gone, files, f"{gone} is on the deny-list but shipped")
        # Positive control: the build did not just produce an empty tree.
        for kept in (".claude-plugin/plugin.json", "scripts/run.sh",
                     "pipeline/__init__.py", "skills/s/SKILL.md", "LICENSE",
                     "README.md", "CHANGELOG.md"):
            self.assertIn(kept, files, f"{kept} is not on the deny-list but was dropped")

    def test_a_file_entry_is_exact_and_a_directory_entry_needs_its_slash(self):
        """`CLAUDE.md` must not take `docs2/CLAUDE.md` or `CLAUDE.md.bak` with it,
        and `tests/` must not take `tests-helper.sh`."""
        repo = _make_repo(self.tmp, extra={
            "sub/CLAUDE.md": "kept\n", "CLAUDE.md.bak": "kept\n", "tests-helper.sh": "kept\n",
        })
        files = _files(_build(self.tmp, repo))
        self.assertNotIn("CLAUDE.md", files)
        self.assertTrue({"sub/CLAUDE.md", "CLAUDE.md.bak", "tests-helper.sh"} <= files)

    @unittest.skipIf(os.name == "nt", "POSIX mode bits")
    def test_the_executable_bit_survives_extraction(self):
        out = _build(self.tmp, _make_repo(self.tmp))
        self.assertTrue(os.stat(out / "scripts/run.sh").st_mode & stat.S_IXUSR)
        # Positive control: a 100644 blob is not made executable wholesale.
        self.assertFalse(os.stat(out / "LICENSE").st_mode & stat.S_IXUSR)

    def test_the_ref_is_built_not_the_working_tree(self):
        repo = _make_repo(self.tmp)
        (repo / "LICENSE").write_text("uncommitted edit\n", encoding="utf-8")
        (repo / "untracked.txt").write_text("x\n", encoding="utf-8")
        out = _build(self.tmp, repo)
        self.assertEqual((out / "LICENSE").read_text(encoding="utf-8"), "license\n")
        self.assertFalse((out / "untracked.txt").exists())

    def test_a_non_empty_output_directory_is_refused(self):
        mod = _load()
        repo = _make_repo(self.tmp)
        out = self.tmp / "out"
        out.mkdir()
        (out / "precious.txt").write_text("x", encoding="utf-8")
        with self.assertRaisesRegex(mod.BuildError, "not empty"):
            mod.build(repo, "v0.2.0", out, _config())
        self.assertTrue((out / "precious.txt").exists())
        # Positive control: an existing EMPTY directory is accepted.
        out2 = self.tmp / "out2"
        out2.mkdir()
        mod.build(repo, "v0.2.0", out2, _config())
        self.assertTrue((out2 / "LICENSE").exists())

    def test_a_forbidden_gitattributes_stops_the_build(self):
        mod = _load()
        repo = _make_repo(self.tmp, extra={".gitattributes": "tests/ export-ignore\n"})
        with self.assertRaisesRegex(mod.BuildError, "export-ignore"):
            mod.build(repo, "v0.2.0", self.tmp / "out", _config())

    def test_a_benign_gitattributes_ships(self):
        repo = _make_repo(self.tmp, extra={".gitattributes": "*.sh text eol=lf\n"})
        out = _build(self.tmp, repo)
        self.assertEqual((out / ".gitattributes").read_text(encoding="utf-8"),
                         "*.sh text eol=lf\n")


class ChangelogCutTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_changelog_keeps_only_the_latest_released_section(self):
        text = (_build(self.tmp, _make_repo(self.tmp)) / "CHANGELOG.md").read_text(encoding="utf-8")
        self.assertIn("## [0.2.0] - 2026-01-02", text)
        self.assertIn("the latest released fix", text)
        self.assertIn("[0.2.0]: https://github.com/Example-Org/example-plugin/releases/tag/v0.2.0", text)
        self.assertNotIn("## [Unreleased]", text)
        self.assertNotIn("not released yet", text)
        self.assertNotIn("[Unreleased]:", text)
        self.assertNotIn("## [0.1.0]", text)
        self.assertNotIn("the oldest entry", text)
        self.assertNotIn("[0.1.0]:", text)
        self.assertIn(BLOB + "CHANGELOG.md", text)

    def test_changelog_skips_an_empty_unreleased_section_too(self):
        """The real CHANGELOG's first `## [` is an EMPTY [Unreleased]; "keep the
        first section" would then ship nothing."""
        empty = CHANGELOG.replace(
            "### Added\n\n- something not released yet (must not ship)\n\n", "")
        mod = _load()
        out = mod.cut_changelog(empty, BLOB + "CHANGELOG.md")
        self.assertIn("the latest released fix", out)
        self.assertNotIn("## [Unreleased]", out)

    def test_changelog_with_no_released_section_is_an_error_not_an_empty_file(self):
        mod = _load()
        with self.assertRaisesRegex(mod.BuildError, "released"):
            mod.cut_changelog("# Changelog\n\n## [Unreleased]\n\n- x\n", BLOB + "CHANGELOG.md")

    def test_changelog_preamble_is_kept(self):
        mod = _load()
        out = mod.cut_changelog(CHANGELOG, BLOB + "CHANGELOG.md")
        self.assertTrue(out.startswith("# Changelog\n"))


class LinkRewritingTest(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def test_readme_links_into_removed_paths_become_absolute(self):
        text = (_build(self.tmp, _make_repo(self.tmp)) / "README.md").read_text(encoding="utf-8")
        self.assertIn(f"![logo]({RAW}docs/logo.png)", text)
        self.assertIn(f'<img src="{RAW}docs/logo.png" width="200">', text)
        self.assertIn(f"[the guide]({BLOB}docs/guide.md#install)", text)
        self.assertIn(f"[ref-style]: {BLOB}docs/guide.md", text)
        # Only the two inside code (the fence and the inline span) may remain relative.
        self.assertEqual(text.count("](docs/"), 2, text)

    def test_readme_links_that_still_resolve_are_left_alone(self):
        text = (_build(self.tmp, _make_repo(self.tmp)) / "README.md").read_text(encoding="utf-8")
        self.assertIn("](LICENSE)", text)
        self.assertIn("[the changelog](CHANGELOG.md)", text)
        self.assertIn("[site](https://example.com/docs/guide.md)", text)
        self.assertIn("[below](#example)", text)
        self.assertIn("![in a fence](docs/logo.png)", text, "fenced code must not be rewritten")
        self.assertIn("`[x](docs/guide.md)`", text, "inline code must not be rewritten")

    def test_nested_markdown_links_resolve_relative_to_their_own_file(self):
        text = (_build(self.tmp, _make_repo(self.tmp)) / "skills/s/SKILL.md").read_text(encoding="utf-8")
        self.assertIn(f"[the guide]({BLOB}docs/guide.md)", text)
        # Positive control: a relative link to a path that ships is untouched.
        self.assertIn("[hooks](../../hooks/hooks.json)", text)

    def test_the_link_base_comes_from_the_config(self):
        cfg = _config(repo="Other/thing", default_branch="trunk")
        text = (_build(self.tmp, _make_repo(self.tmp), cfg) / "README.md").read_text(encoding="utf-8")
        self.assertIn("https://raw.githubusercontent.com/Other/thing/trunk/docs/logo.png", text)
        self.assertIn("https://github.com/Other/thing/blob/trunk/docs/guide.md#install", text)


class RealRepoTest(unittest.TestCase):
    """Integration: the real deny-list and the real tree of this repository."""

    def test_the_committed_config_parses_and_names_the_brief_deny_list(self):
        mod = _load()
        cfg = mod.load_config(CONFIG)
        for entry in ("tests/", "docs/", ".github/", ".claude/", ".oss/",
                     ".githooks/", "changelog.d/", "trap.d/", "outbound/",
                     ".oss.json", ".supertool.json", ".markdownlint.json",
                     "CLAUDE.md", "CONTRIBUTING.md"):
            self.assertIn(entry, cfg["deny"], entry)
        # Positive control: nothing the plugin runs is denied.
        for runtime in ("skills/",):
            self.assertNotIn(runtime, cfg["deny"], runtime)
        self.assertNotIn("scripts/", cfg["deny"])
        self.assertNotIn("scripts/window-spread.py", cfg["deny"])
        self.assertNotIn(".claude-plugin/", cfg["deny"])

    def test_building_this_repository_head_ships_every_runtime_file(self):
        """Integration: the real deny-list against the real tree. The script the
        skill calls and the manifest it needs must survive the build."""
        mod = _load()
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        out = tmp / "out"
        mod.build(REPO_ROOT, "HEAD", out, mod.load_config(CONFIG))
        self.assertTrue((out / "scripts" / "window-spread.py").is_file())
        self.assertTrue((out / "skills" / "window-spread" / "SKILL.md").is_file())
        self.assertTrue((out / ".claude-plugin" / "plugin.json").is_file())
        self.assertTrue((out / "README.md").is_file())
        self.assertTrue((out / "LICENSE").is_file())
        self.assertFalse((out / "tests").exists())
        self.assertFalse((out / "docs").exists())
        self.assertFalse((out / ".github").exists())
        self.assertFalse((out / "CLAUDE.md").exists())

    def test_cli_builds_and_reports(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        repo = _make_repo(tmp)
        cfg = tmp / "cfg.json"
        cfg.write_text(json.dumps(_config()), encoding="utf-8")
        out = tmp / "out"
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(repo), "--ref", "v0.2.0",
             "--out", str(out), "--config", str(cfg)],
            capture_output=True, text=True, env=_git_env(), check=False,
        )
        self.assertEqual(r.returncode, 0, r.stderr)
        self.assertTrue((out / "LICENSE").exists())
        self.assertIn("removed", r.stdout)

    def test_cli_fails_loudly_on_an_unknown_ref(self):
        tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(tmp, ignore_errors=True))
        repo = _make_repo(tmp)
        cfg = tmp / "cfg.json"
        cfg.write_text(json.dumps(_config()), encoding="utf-8")
        r = subprocess.run(
            [sys.executable, str(SCRIPT), "--repo", str(repo), "--ref", "v9.9.9",
             "--out", str(tmp / "out"), "--config", str(cfg)],
            capture_output=True, text=True, env=_git_env(), check=False,
        )
        self.assertNotEqual(r.returncode, 0)
        self.assertIn("v9.9.9", r.stderr)


if __name__ == "__main__":
    unittest.main()
