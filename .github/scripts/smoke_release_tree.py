#!/usr/bin/env python3
"""Smoke-test a built release tree before it is published (#12).

Two parts, both read-only with respect to the host:

1. `claude plugin validate --strict TREE` -- the same validator the Anthropic
   directory documents. `--validate auto` skips it, out loud, when no `claude`
   CLI is on PATH; `--validate require` fails instead; `--validate skip` never
   runs it.
2. This plugin ships no hooks (no `hooks/hooks.json`), so there is nothing for
   the hook-replay half claude-remember's version of this script runs. What
   gets exercised instead is the plugin's own script,
   `scripts/window-spread.py`, called only in ways that cannot install, write
   or touch the real OS scheduler:
   - `--help`, for every subcommand;
   - `compute --blocks "..."`, which is pure computation -- it prints JSON and
     touches nothing outside the process.
   `install`, `uninstall` and `list` are never called here: even `--dry-run`
   on Linux reads the user's real crontab, and `list` queries the real
   scheduler on every platform. A smoke test that ships with the plugin must
   never reach a scheduler entry a developer or CI runner actually has.

Isolation:

- HOME, TMPDIR and XDG_* all point inside one temp directory, so nothing the
  script happens to read or write (it does not, today, but the isolation does
  not depend on that staying true) can reach the real environment;
- inherited CLAUDE_*/GIT_* variables are dropped.

Usage:
    smoke_release_tree.py TREE [--validate auto|require|skip] [--claude-bin PATH]
"""

from __future__ import annotations

import argparse
import json
import os
import shutil
import subprocess
import sys
import tempfile
from dataclasses import dataclass, field
from pathlib import Path

DROP_PREFIXES = ("CLAUDE", "GIT_")


@dataclass
class ScriptRun:
    label: str
    returncode: int
    stdout: str
    stderr: str


@dataclass
class SmokeResult:
    notes: list = field(default_factory=list)
    runs: list = field(default_factory=list)
    errors: list = field(default_factory=list)

    @property
    def ok(self) -> bool:
        return not self.errors and all(r.returncode == 0 for r in self.runs)

    def report(self) -> str:
        lines = list(self.notes)
        for r in self.runs:
            verdict = "PASS" if r.returncode == 0 else "FAIL"
            lines.append(f"{verdict} {r.label}: exit {r.returncode}, "
                         f"stdout {len(r.stdout)} bytes")
            tail = r.stderr.strip().splitlines()[-5:]
            lines.extend(f"    stderr: {t}" for t in tail)
        lines.extend(f"FAIL {e}" for e in self.errors)
        lines.append("smoke: OK" if self.ok else "smoke: FAILED")
        return "\n".join(lines)


def _env(home: Path) -> dict:
    env = {k: v for k, v in os.environ.items() if not k.startswith(DROP_PREFIXES)}
    tmp = home / "tmp"
    tmp.mkdir(exist_ok=True)
    env.update({
        "HOME": str(home),
        "TMPDIR": str(tmp),
        "XDG_CONFIG_HOME": str(home / ".config"),
        "XDG_CACHE_HOME": str(home / ".cache"),
        "XDG_DATA_HOME": str(home / ".local" / "share"),
        "XDG_STATE_HOME": str(home / ".local" / "state"),
        "PYTHONDONTWRITEBYTECODE": "1",
        "GIT_CONFIG_NOSYSTEM": "1",
    })
    return env


def run_validate(tree: Path, mode: str, claude_bin: str | None, home: Path,
                 result: SmokeResult) -> None:
    if mode == "skip":
        result.notes.append("validate: SKIPPED (--validate skip)")
        return
    binary = claude_bin or shutil.which("claude")
    if not binary or not Path(binary).exists():
        msg = f"claude CLI not found ({binary or 'not on PATH'})"
        if mode == "require":
            result.errors.append(f"validate: {msg}")
        else:
            result.notes.append(f"validate: SKIPPED -- {msg}; "
                                "`claude plugin validate --strict` did not run")
        return
    env = _env(home)
    r = subprocess.run([binary, "plugin", "validate", "--strict", str(tree)],
                       capture_output=True, text=True, env=env, check=False, timeout=300)
    output = (r.stdout + r.stderr).strip()
    result.notes.append(f"validate: `claude plugin validate --strict` exit {r.returncode}")
    result.notes.extend(f"    {line}" for line in output.splitlines())
    if r.returncode != 0:
        result.errors.append(f"validate: claude plugin validate --strict exited {r.returncode}")


_HELP_TARGETS = ("--help", "compute --help", "install --help", "uninstall --help", "list --help")


def run_script_smoke(tree: Path, home: Path, result: SmokeResult) -> None:
    script = tree / "scripts" / "window-spread.py"
    if not script.is_file():
        result.notes.append("script: no scripts/window-spread.py in the tree, nothing to run")
        return
    env = _env(home)

    for target in _HELP_TARGETS:
        args = [sys.executable, str(script), *target.split()]
        r = subprocess.run(args, capture_output=True, text=True, env=env, check=False, timeout=30)
        result.runs.append(ScriptRun(f"window-spread.py {target}", r.returncode, r.stdout, r.stderr))

    compute = [sys.executable, str(script), "compute",
              "--blocks", "8:30-12:20,14:00-18:00,20:00-23:00"]
    r = subprocess.run(compute, capture_output=True, text=True, env=env, check=False, timeout=30)
    result.runs.append(ScriptRun("window-spread.py compute", r.returncode, r.stdout, r.stderr))
    if r.returncode == 0:
        try:
            doc = json.loads(r.stdout)
        except ValueError as exc:
            result.errors.append(f"script: compute did not print valid JSON: {exc}")
        else:
            for key in ("blocks", "spread", "natural", "improvement"):
                if key not in doc:
                    result.errors.append(f"script: compute output is missing {key!r}")
            pings = doc.get("spread", {}).get("pings")
            if not pings:
                result.errors.append("script: compute produced no pings")


def run_smoke(tree: Path, validate: str = "auto", claude_bin: str | None = None) -> SmokeResult:
    tree = Path(tree).resolve()
    result = SmokeResult()
    work = Path(tempfile.mkdtemp(prefix="release-smoke-")).resolve()
    try:
        home = work / "home"
        home.mkdir(parents=True, exist_ok=True)
        run_validate(tree, validate, claude_bin, home, result)
        run_script_smoke(tree, home, result)
        return result
    finally:
        shutil.rmtree(work, ignore_errors=True)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("tree")
    ap.add_argument("--validate", choices=("auto", "require", "skip"), default="auto")
    ap.add_argument("--claude-bin", default=None)
    args = ap.parse_args(argv)
    result = run_smoke(Path(args.tree), args.validate, args.claude_bin)
    print(result.report())
    return 0 if result.ok else 1


if __name__ == "__main__":
    sys.exit(main())
