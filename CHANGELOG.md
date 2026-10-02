# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are assembled from `changelog.d/` fragments at release time — see `.oss/README.md`.

This file carries only the latest release. The full history is in [CHANGELOG.md on the default branch](https://github.com/Digital-Process-Tools/claude-5h-window-spread/blob/main/CHANGELOG.md).

## [0.3.1] - 2026-10-02

### Added

- Added a slim `release` branch for the Anthropic plugin directory (#12). A tag push
  now builds a tree that keeps only what the plugin runs -- `README.md`, `LICENSE`,
  `CODE_OF_CONDUCT.md`, `SECURITY.md`, banner.png, `.claude-plugin/plugin.json`,
  `scripts/window-spread.py` and `skills/window-spread/SKILL.md`, cutting CHANGELOG.md
  to its latest release and rewriting any link into a removed path -- checks it
  against the directory's pre-submission checklist and a side-effect-free smoke test
  (`claude plugin validate --strict` plus `window-spread.py --help`/`compute`, never
  `install`/`uninstall`/`list`), and pushes it as one commit on `release` only if every
  step passes. `.claude-plugin/plugin.json` now also carries `documentationUrl` and
  `supportUrl`. See `docs/releasing.md` for the full sequence and the portal steps.

[0.3.1]: https://github.com/Digital-Process-Tools/claude-5h-window-spread/releases/tag/v0.3.1
