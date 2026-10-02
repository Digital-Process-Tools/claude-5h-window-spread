# Releasing

`main` carries everything: the plugin, its tests, this doc, the maintainer tooling and
`.oss/`, `.github/`. The Anthropic plugin directory does not accept that tree as-is. Its
pre-submission checklist holds any version whose plugin folder has more than 512 files,
or a file of 256 KiB or more that is not an image or font, and it previously scanned
`main` directly: v0.3.0 (`de8d3e8`, committed 2026-08-14) was scanned on 2026-09-23 and came back with two warnings --
`CLAUDE.md at the plugin root isn't loaded` and `No icon`. `CLAUDE.md` is for
maintainers and was never meant to ship; the missing icon is a decided gap, out of
scope here (the listing icon is set once, at the first save, and we chose not to ship
one).

So there are two branches people install from:

| Branch | Who reads it | What decides the version they get |
| --- | --- | --- |
| `main` | the DPT marketplace (`dpt-plugins`), manual installs | `version` in `.claude-plugin/plugin.json` on `main` |
| `release` | the Anthropic directory, once the portal's Source field is switched to it (see "Switching the listing to `release`" below) | the latest commit on `release`, which only the release workflow writes |

`release` is built by [`.github/workflows/release-branch.yml`](../.github/workflows/release-branch.yml)
from a tag. It never shares history with `main`: each release is one commit on top of
the previous release commit, and its message names the tag and the `main` commit it
came from. The approach, the three scripts and the workflow are adapted from
claude-remember's own `release` branch
([docs/releasing.md](https://github.com/Digital-Process-Tools/claude-remember/blob/main/docs/releasing.md)),
which hit the same directory holds first.

## The sequence

1. **Fold `changelog.d/` into a `## [x.y.z]` section of CHANGELOG.md and bump every
   version site**: today that is just `.claude-plugin/plugin.json`. The oss plugin's
   per-repo config (`.oss.json`, untracked -- see `CLAUDE.md`'s "Maintenance" note)
   names the full list under `version_sites` when it is present on the machine doing
   the release.
   *Why:* DPT-marketplace installs follow `main`, and the `version` field in
   `plugin.json` is the only thing that tells them there is something new. The tag does
   not matter to them.

2. **Run the full suite (`python3 -m unittest discover -s tests`) and commit the
   release on `main`.**
   *Why:* that commit is what gets tagged; CI across three OSes is the gate.

3. **Tag that commit `vx.y.z` and push the tag with your own credentials**:
   `git tag vx.y.z <release-commit-sha> && git push origin vx.y.z`, then check it
   landed with `git ls-remote --tags origin vx.y.z`.
   *Why:* **pushing the tag is what publishes to the directory**, once the portal
   follows `release` (see below). The tag push starts the `release branch` workflow,
   and that workflow is the only thing that writes `release`.
   *Why your own credentials:* a tag pushed by another workflow with `GITHUB_TOKEN`
   does not start workflows (GitHub suppresses them to prevent loops).

4. **Watch the `release branch` run** (Actions tab, or
   `gh run list --workflow release-branch.yml`). It has two jobs:
   - `verify`, read-only: installs PyYAML, builds the tree from the tag, runs
     [`check_release_tree.py`](../.github/scripts/check_release_tree.py) (the
     directory's pre-submission checklist), installs the pinned claude CLI
     (`CLAUDE_CLI_VERSION` in the workflow, 2.1.287) and runs
     `claude plugin validate --strict`, then exercises `scripts/window-spread.py` in a
     side-effect-free way -- `--help` for every subcommand and a `compute` call, never
     `install`/`uninstall`/`list`, which would touch the real OS scheduler
     ([`smoke_release_tree.py`](../.github/scripts/smoke_release_tree.py)). This
     plugin ships no hooks, so there is nothing for a hook-replay pass to run, unlike
     claude-remember's version of this script.
     **If the npm install of the CLI fails, the run does not fail**: validate is
     SKIPPED and the only trace is a `::warning::` annotation, so read the run rather
     than its status. The pin must be 2.1.281 or later: earlier CLIs warn
     "Unknown field" on the directory listing fields in `plugin.json`
     (`documentationUrl`, `supportUrl`), and `--strict` makes that a failure.
   - `publish`, the only job allowed to write: rebuilds the same tree, refuses to push
     unless it is byte-for-byte the tree `verify` passed, and pushes one commit to
     `release`.
   *Why two jobs:* the smoke test runs the plugin's own script, so it never holds a
   token that can push.

5. **Publish the GitHub release** (`scripts/release_publish.py` from the oss plugin,
   not a file in this repository; it runs `gh release create --verify-tag`).
   *Why it is unaffected:* the release notes are read from `CHANGELOG.md` in the
   maintainer's local `main` checkout, never from the release tree. The release tree's
   `CHANGELOG.md` is cut to the latest section, but nothing reads that copy except
   people browsing `release`.

6. **The directory picks up the new `release` commit** (at once through the push
   webhook, otherwise within about 6 hours) and scans it. A version with a **Policy
   hold** waits for an Anthropic reviewer, and while the listing itself is flagged, no
   newer version goes live, however clean, until a reviewer clears it. Read the
   **Versions** tab in the developer portal rather than assuming a waiting version is
   only in a queue.

## What the release tree contains

[`build_release_tree.py`](../.github/scripts/build_release_tree.py) reads the tag
straight from git (`git ls-tree` and `git cat-file`; never the working tree, and never
`git archive`, which would need `export-ignore`). Then:

- **It drops the deny-list** in [`.github/release-branch.json`](../.github/release-branch.json):
  `tests/`, `.github/`, `.claude/`, `.oss/`, `.githooks/`, `changelog.d/`, `trap.d/`,
  `outbound/`, `.oss.json`, `.supertool.json`, `.markdownlint.json`, `.gitignore`,
  `CLAUDE.md` and `CONTRIBUTING.md`. It is a deny-list on purpose: a file nobody listed
  still ships, and `check_release_tree.py` catches it loudly if it is too big. With an
  allow-list, a forgotten runtime file would vanish from every user's install with no
  error anywhere. A new dev-only top-level file or directory therefore needs adding
  here -- `trap.d/`, `outbound/`, `.markdownlint.json` and `CONTRIBUTING.md` are
  already denied even though none of them exist on `main` yet, because a maintainer
  furniture branch adds them.
- **It cuts CHANGELOG.md** to the latest released `## [x.y.z]` section, skipping
  `[Unreleased]` even when it has entries, plus that section's link and a link to the
  full file on `main`.
- **It rewrites links** in every shipped `.md` file that point at a removed path to
  absolute URLs on `main`: `raw.githubusercontent.com` for images,
  `github.com/.../blob/main` for everything else. Links to files that still ship (the
  README's `banner.png`, its link to `LICENSE`) are left alone.

What ships today: `README.md`, `LICENSE`, `CODE_OF_CONDUCT.md`, `SECURITY.md`,
`banner.png`, `.claude-plugin/plugin.json`, `scripts/window-spread.py` and
`skills/window-spread/SKILL.md` -- 9 files, under 2 MB, dominated by `banner.png`.

## When the workflow fails

Nothing is pushed. `release` stays on the previous release, so the directory keeps
serving that, and people on `main` are not affected at all. Fix the cause on `main`,
then run the workflow again for the same tag: **Actions, `release branch`, Run
workflow, ref = `vx.y.z`**, or

```bash
gh workflow run release-branch.yml -f ref=vx.y.z
```

A manual run takes the workflow, the scripts and `.github/release-branch.json` from the
branch you run it on (normally `main`) and the plugin files from the ref you name. That
is how a tooling fix reaches a tag that is already cut. If the problem is in the plugin
itself, it needs a new patch release instead. Re-running the same tag when `release`
already holds that exact tree pushes nothing.

## Building and checking locally

```bash
python3 .github/scripts/build_release_tree.py --ref vX.Y.Z --out /tmp/release-tree   # the latest tag
python3 .github/scripts/check_release_tree.py /tmp/release-tree
python3 .github/scripts/smoke_release_tree.py /tmp/release-tree --validate skip       # --validate require needs `claude` on PATH
```

The scripts come from your checkout; the plugin files come from the tag.

## Switching the listing to `release`

**Not done yet for this repository.** The portal currently follows the repository's
default branch (`main`), and **v0.3.0 is waiting for review** on that listing right
now. Per the submission docs, *the tracked branch cannot be changed while a version is
with a reviewer, and changing it cancels any publish request that is still waiting* --
so this switch must wait until v0.3.0's review resolves, one way or the other.

Once it is clear to proceed, in this order:

1. Create `release` first: push a tag, or
   `gh workflow run release-branch.yml -f ref=<tag>`.
2. Check on GitHub that `release` holds only the slim tree (the file list above).
3. In the developer portal (claude.ai/directory/manage, this plugin's page,
   **Settings → Source → "Tracked branch or tag"**), type `release` and **Save**. The
   portal's own text on saving: "Saving a change scans the latest commit on the new
   branch or tag as a new version. A version that is already live stays up while that
   happens, and a publish request that is still waiting is cancelled."
4. While on that page, set up the **GitHub push webhook**
   (**Settings → Updates → Set up**). Portal text: "With a push webhook, GitHub tells
   the directory the moment you push. With or without one, a scheduled check looks for
   new commits about every 6 hours." Setting it up needs admin rights on the
   repository, and the portal shows the webhook secret only once.

Until the switch, the listing keeps following `main`, and every push to it lands in the
portal as a new version to scan.

## Reusing this in another plugin repository

Not everything repository-specific lives in `.github/release-branch.json`:

- `build_release_tree.py` reads `repo`, `default_branch`, `deny`, `changelog`,
  `rewrite_links` and the optional `link_ref` from it; `check_release_tree.py` reads
  only `budget`. In `budget`, `max_file_bytes` and `max_files` are the directory's
  rules, but `max_total_bytes` is this repository's own ceiling, not a directory rule
  -- set your own, against the actual size of what you plan to ship.
- `smoke_release_tree.py` here is the no-hooks version: it runs `claude plugin
  validate --strict` and then `--help`/`compute` on `scripts/window-spread.py`. A
  plugin that ships `hooks/hooks.json` needs claude-remember's version instead, which
  replays every hook once in an isolated temp HOME with a fake `claude`/`codex` on
  PATH.
- `release-branch.yml` pins `CLAUDE_CLI_VERSION`; keep it at 2.1.281 or later.
- Rewritten links point at the head of the default branch, not at the tag, so a
  released README's links into removed paths can drift from the version that shipped.

To adopt it elsewhere:

1. Copy `.github/scripts/{build,check,smoke}_release_tree.py`,
   `.github/release-branch.json` and `.github/workflows/release-branch.yml`, and adapt
   the points above.
2. Rewrite the deny-list from that repository's own tree (`git ls-files`). Check every
   candidate against what the plugin loads at runtime before denying it, and keep
   `LICENSE` and `README.md`: the directory blocks without them.
3. Build from the latest tag and run the check and the smoke test locally (section
   above).
4. Make sure `github-actions[bot]` can push `release` (no ruleset or branch protection
   blocking it), never make `release` the default branch, and treat push rights on
   `v*` tags as publish rights: whoever can push such a tag publishes to the directory.
5. Push a tag, confirm `release` exists, then do the portal steps in "Switching the
   listing to `release`" above.
