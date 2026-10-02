"""#12 -- the check that runs on a built release tree before anything is pushed.

`.github/scripts/check_release_tree.py` encodes the Anthropic directory's "Files in
the plugin folder" rules (every non-image, non-font file under 256 KiB; at most 512
files; only text plus PNG/JPEG/GIF/WebP and fonts; no `.gitattributes` with
export-ignore/export-subst/filter) plus this repo's own total budget and a refusal
of `.DS_Store`/`Thumbs.db`.

Ported from claude-remember's test_release_branch_check_851.py (pytest) to stdlib
unittest. Every failure class has a passing twin built the same way: a check that
failed on everything would pass every "must fail" test here, and the twins are what
catch it.
"""

from __future__ import annotations

import importlib.util
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parent.parent
SCRIPT = REPO_ROOT / ".github" / "scripts" / "check_release_tree.py"

KIB = 1024
LIMIT = 256 * KIB

PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 32
JPEG = b"\xff\xd8\xff\xe0" + b"\x00" * 32
GIF = b"GIF89a" + b"\x00" * 32
WEBP = b"RIFF\x00\x00\x00\x00WEBPVP8 " + b"\x00" * 32
WOFF2 = b"wOF2" + b"\x00" * 32
TTF = b"\x00\x01\x00\x00" + b"\x00" * 32
ELF = b"\x7fELF\x02\x01\x01" + b"\x00" * 32


def _load():
    assert SCRIPT.exists(), f"{SCRIPT} does not exist (#12)"
    spec = importlib.util.spec_from_file_location("check_release_tree", SCRIPT)
    mod = importlib.util.module_from_spec(spec)
    sys.modules["check_release_tree"] = mod
    spec.loader.exec_module(mod)
    return mod


BUDGET = {"max_file_bytes": LIMIT, "max_files": 512, "max_total_bytes": 3 * KIB * KIB}


def _tree(tmp_path: Path, files: dict) -> Path:
    root = tmp_path / "tree"
    root.mkdir()
    base = {
        ".claude-plugin/plugin.json": (b'{"name": "x", "description": "d", '
                                       b'"version": "1.0.0", "author": {"name": "a"}}\n'),
        "README.md": ("# x\n\n" + " ".join(["word"] * 40) + "\n").encode(),
        "LICENSE": b"license\n",
        "scripts/run.sh": b"#!/bin/sh\necho hi\n",
    }
    base.update(files)
    for rel, data in base.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_bytes(data)
    return root


def _check(root: Path, **budget):
    mod = _load()
    b = dict(BUDGET)
    b.update(budget)
    return mod.check_tree(root, b)


class _Base(unittest.TestCase):
    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(self.tmp, ignore_errors=True))

    def _fresh_tmp(self) -> Path:
        """A new empty temp directory, for a subTest loop that calls `_tree`
        more than once -- `_tree` always creates `root / "tree"`, so reusing
        `self.tmp` across iterations collides on the second call."""
        p = Path(tempfile.mkdtemp())
        self.addCleanup(lambda: shutil.rmtree(p, ignore_errors=True))
        return p


class CleanTreeTest(_Base):
    def test_a_clean_tree_passes(self):
        result = _check(_tree(self.tmp, {}))
        self.assertEqual(result.offenders, [])
        self.assertEqual(result.files, 4)
        self.assertGreater(result.total_bytes, 0)


class PerFileSizeTest(_Base):
    def test_a_text_file_at_256_kib_fails_and_is_named(self):
        root = _tree(self.tmp, {"CHANGELOG.md": b"a" * LIMIT})
        offenders = _check(root).offenders
        self.assertTrue(any("CHANGELOG.md" in o and "256" in o for o in offenders), offenders)

    def test_a_text_file_one_byte_under_256_kib_passes(self):
        root = _tree(self.tmp, {"CHANGELOG.md": b"a" * (LIMIT - 1)})
        self.assertEqual(_check(root).offenders, [])

    def test_a_large_real_image_is_exempt_from_the_size_limit(self):
        root = _tree(self.tmp, {"img/big.png": PNG + b"\x00" * LIMIT})
        self.assertEqual(_check(root).offenders, [])

    def test_a_large_text_file_named_png_is_not_exempt(self):
        """The exemption follows the bytes, not the extension."""
        root = _tree(self.tmp, {"img/fake.png": b"a" * LIMIT})
        offenders = _check(root).offenders
        self.assertTrue(any("img/fake.png" in o for o in offenders), offenders)


class FileCountAndTotalTest(_Base):
    def test_more_files_than_the_budget_fails(self):
        root = _tree(self.tmp, {f"f/{i}.txt": b"x" for i in range(3)})  # 7 files
        offenders = _check(root, max_files=6).offenders
        self.assertTrue(any("7 files" in o and "6" in o for o in offenders), offenders)

    def test_exactly_the_file_budget_passes(self):
        root = _tree(self.tmp, {f"f/{i}.txt": b"x" for i in range(3)})  # 7 files
        self.assertEqual(_check(root, max_files=7).offenders, [])

    def test_a_total_over_the_budget_fails(self):
        root = _tree(self.tmp, {"a.txt": b"a" * 1000, "b.txt": b"b" * 1000})
        total = _check(root).total_bytes
        offenders = _check(root, max_total_bytes=total - 1).offenders
        self.assertTrue(any("total" in o for o in offenders), offenders)
        # Positive control: exactly at the budget is fine.
        self.assertEqual(_check(root, max_total_bytes=total).offenders, [])


class JunkFilesTest(_Base):
    def test_os_junk_files_fail(self):
        for name in (".DS_Store", "sub/.DS_Store", "Thumbs.db", "sub/thumbs.db"):
            with self.subTest(name=name):
                root = _tree(self._fresh_tmp(), {name: b"x"})
                offenders = _check(root).offenders
                self.assertTrue(any(name in o for o in offenders), offenders)

    def test_a_file_merely_containing_ds_store_in_its_name_passes(self):
        root = _tree(self.tmp, {"docs/about-.DS_Store-files.md": b"# x\n"})
        self.assertEqual(_check(root).offenders, [])


class GitattributesTest(_Base):
    CASES = [
        ("tests/ export-ignore", "export-ignore"),
        ("VERSION export-subst", "export-subst"),
        ("*.psd filter=lfs diff=lfs merge=lfs -text", "filter"),
        ("*.bin -filter", "filter"),
    ]

    def test_a_forbidden_gitattributes_fails(self):
        for line, word in self.CASES:
            with self.subTest(line=line):
                root = _tree(self._fresh_tmp(), {"sub/.gitattributes": f"# comment\n{line}\n".encode()})
                offenders = _check(root).offenders
                self.assertTrue(any("sub/.gitattributes" in o and word in o for o in offenders),
                                offenders)

    def test_a_benign_gitattributes_passes(self):
        root = _tree(self.tmp, {
            ".gitattributes": b"*.sh text eol=lf\n# export-ignore in a comment is fine\n",
        })
        self.assertEqual(_check(root).offenders, [])


class FileTypesTest(_Base):
    ALLOWED = [
        ("a.png", PNG), ("a.jpg", JPEG), ("a.gif", GIF), ("a.webp", WEBP),
        ("a.woff2", WOFF2), ("a.ttf", TTF), ("a.svg", b"<svg xmlns='x'/>\n"),
        ("a.md", "café — utf-8\n".encode()), ("empty.txt", b""),
    ]
    BINARY = [
        ("tool", ELF),
        ("x.pyc", b"\x61\x0d\x0d\x0a" + b"\x00" * 12),
        ("notes.txt", b"text with a NUL\x00 in it\n"),
        ("bad.txt", b"\xff\xfe\xfa not utf-8\n"),
    ]

    def test_allowed_types_pass(self):
        for name, data in self.ALLOWED:
            with self.subTest(name=name):
                root = _tree(self._fresh_tmp(), {name: data})
                self.assertEqual(_check(root).offenders, [])

    def test_binary_files_fail_and_are_named(self):
        for name, data in self.BINARY:
            with self.subTest(name=name):
                root = _tree(self._fresh_tmp(), {name: data})
                offenders = _check(root).offenders
                self.assertTrue(any(name in o and "binary" in o for o in offenders), offenders)

    def test_every_offender_is_named_not_just_the_first(self):
        root = _tree(self.tmp, {
            "big.md": b"a" * LIMIT, ".DS_Store": b"x", "tool": ELF,
            ".gitattributes": b"x export-ignore\n",
        })
        offenders = _check(root).offenders
        for name in ("big.md", ".DS_Store", "tool", ".gitattributes"):
            self.assertTrue(any(o.startswith(name + ":") for o in offenders), (name, offenders))


class FrontMatterTest(_Base):
    """Skill front matter is parsed by a real YAML parser when PyYAML is
    installed, and by a stdlib fallback (`key: value` lines only) when it is
    not -- the workflow installs PyYAML, but a contributor running the check
    locally without it must still get a usable answer (#12)."""

    SKILL_OK = (
        "---\n"
        "name: s\n"
        "description: a skill that does a thing\n"
        "---\n\n"
        "body\n"
    ).encode()
    SKILL_MISSING_DESC = "---\nname: s\n---\n\nbody\n".encode()

    def test_a_skill_with_a_text_description_passes(self):
        root = _tree(self.tmp, {"skills/s/SKILL.md": self.SKILL_OK})
        self.assertEqual(_check(root).offenders, [])

    def test_a_skill_with_no_description_fails_and_is_named(self):
        root = _tree(self.tmp, {"skills/s/SKILL.md": self.SKILL_MISSING_DESC})
        offenders = _check(root).offenders
        self.assertTrue(any("skills/s/SKILL.md" in o and "description" in o for o in offenders),
                        offenders)

    def test_the_stdlib_fallback_parses_the_same_description(self):
        """Force the `import yaml` inside front_matter() to fail, the way it
        would on a machine with no PyYAML installed, and confirm the fallback
        line-parser still finds the description."""
        mod = _load()
        import sys as _sys
        import builtins as _builtins
        real_import = _builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced: simulating no PyYAML on PATH")
            return real_import(name, *args, **kwargs)

        _builtins.__import__ = fake_import
        try:
            data, problem = mod.front_matter(self.SKILL_OK.decode("utf-8"))
        finally:
            _builtins.__import__ = real_import
        self.assertIsNone(problem)
        self.assertEqual(data.get("description"), "a skill that does a thing")

    def test_the_stdlib_fallback_also_reports_a_missing_description(self):
        mod = _load()
        import builtins as _builtins
        real_import = _builtins.__import__

        def fake_import(name, *args, **kwargs):
            if name == "yaml":
                raise ImportError("forced: simulating no PyYAML on PATH")
            return real_import(name, *args, **kwargs)

        _builtins.__import__ = fake_import
        try:
            data, problem = mod.front_matter(self.SKILL_MISSING_DESC.decode("utf-8"))
        finally:
            _builtins.__import__ = real_import
        self.assertIsNone(problem)
        self.assertNotIn("description", data)


class CLITest(_Base):
    def _cli(self, root: Path, *extra: str):
        return subprocess.run(
            [sys.executable, str(SCRIPT), str(root), *extra],
            capture_output=True, text=True, check=False,
        )

    def test_cli_exits_non_zero_and_names_offenders(self):
        root = _tree(self.tmp, {"big.md": b"a" * LIMIT, "Thumbs.db": b"x"})
        r = self._cli(root)
        self.assertEqual(r.returncode, 1, (r.stdout, r.stderr))
        self.assertIn("big.md", r.stdout)
        self.assertIn("Thumbs.db", r.stdout)

    def test_cli_exits_zero_on_a_clean_tree(self):
        r = self._cli(_tree(self.tmp, {}))
        self.assertEqual(r.returncode, 0, (r.stdout, r.stderr))
        self.assertIn("4 files", r.stdout)

    def test_cli_budget_comes_from_the_config(self):
        root = _tree(self.tmp, {})
        cfg = self.tmp / "cfg.json"
        cfg.write_text('{"budget": {"max_file_bytes": 262144, "max_files": 3, '
                       '"max_total_bytes": 3145728}}', encoding="utf-8")
        self.assertEqual(self._cli(root, "--config", str(cfg)).returncode, 1)
        cfg.write_text('{"budget": {"max_file_bytes": 262144, "max_files": 4, '
                       '"max_total_bytes": 3145728}}', encoding="utf-8")
        self.assertEqual(self._cli(root, "--config", str(cfg)).returncode, 0)


if __name__ == "__main__":
    unittest.main()
