# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are assembled from `changelog.d/` fragments at release time — see `.oss/README.md`.

This file carries only the latest release. The full history is in [CHANGELOG.md on the default branch](https://github.com/Digital-Process-Tools/claude-5h-window-spread/blob/main/CHANGELOG.md).

## [0.3.2] - 2026-10-03

### Changed

- The Anthropic plugin directory now reads this plugin from the slim `release` branch
  instead of `main`. The plugin itself is unchanged from 0.3.1. The release build lets `claude plugin validate
  --strict`'s reserved-name error for `claude-5h-window-spread` through as a warning,
  and only when it is the only error, while Anthropic is asked whether the listing keeps
  its name (#20).

[0.3.2]: https://github.com/Digital-Process-Tools/claude-5h-window-spread/releases/tag/v0.3.2
