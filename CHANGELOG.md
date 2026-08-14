# Changelog

All notable changes to this project are documented in this file.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/), and this project
adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

Entries are assembled from `changelog.d/` fragments at release time — see `.oss/README.md`.

## [Unreleased]

## [0.3.0] - 2026-08-14

### Fixed

- On Linux, `install` and `uninstall` no longer replace the entire crontab when
  they could not read the existing one (#4). `crontab -l` exits non-zero both
  when there is no crontab and when there is one it cannot read, and the two
  were treated alike -- a permissions problem on the cron spool left a crontab
  containing only this tool's lines, with no copy of what had been there. The
  read now reports three states, and one it does not recognise as "no crontab"
  refuses the write and exits non-zero. `--force-replace-crontab` overrides
  that, and discards the crontab.

- On Linux, a `--command` containing a newline, a carriage return or a bare `%`
  is now refused before anything is written (#5). Either one split the crontab
  line into a second entry that ran on its own schedule and carried no marker
  comment, so `uninstall` reported success and left it firing. Write `\%` for a
  literal percent sign.

- On macOS, the launchd plist is built with `plistlib` instead of being pasted
  together as XML (#6). A `--command` containing `&`, `<` or `>` -- `claude -p
  'a && b'`, any redirect -- produced a document launchd could not parse, and
  one containing `</string></array><key>...` produced a well-formed document
  carrying launchd keys nobody asked for, including a replaced
  `ProgramArguments`. Every value is now encoded, so neither is reachable.
  What replaces the old rule is a narrower one, not none: a plist is XML, so a
  `--command` carrying a C0 control character other than tab or newline, or a
  carriage return (which every parser normalises to a newline on read), or a
  byte that is not valid UTF-8, is refused before anything is removed.
- Independently, a failed `launchctl load` no longer costs you the job you had.
  Install used to unload the existing plist and overwrite it before anything
  checked the new one loaded. The previous file is now restored and reloaded on
  failure, and the result reports `"restored"`.

- Ping times are validated on every platform, in `install`, before the
  destructive uninstall pass runs (#7). The check previously existed only as a
  side effect of the `int()` calls in `_split_hm`, and the Windows branch never
  called it -- so an arbitrary string out of the pings JSON flowed straight
  into a `schtasks` task name and `/st` value. A ping must be `HH:MM`,
  zero-padded, `00:00`-`23:59`.
- The command is still checked per platform rather than universally, because
  the rules differ in both directions: cron's newline and `%` restrictions do
  not apply to launchd, where both round-trip untouched, and launchd's control
  character restriction does not apply to cron. All three arms -- cron's rule,
  the plist rule, and Windows' stated absence of one -- are now written out
  together in `_validate_install_input` instead of being implied by which
  helper each branch happens to call.

## [0.2.1] - 2026-05-07

Released before this file existed. See the
[commit history](https://github.com/Digital-Process-Tools/claude-5h-window-spread/commits/v0.2.1)
for what it carried. Its entries are not reconstructed here, because a changelog written after
the fact from commit subjects records what was committed rather than what changed for anyone
using it.

[Unreleased]: https://github.com/Digital-Process-Tools/claude-5h-window-spread/compare/v0.3.0...HEAD
[0.3.0]: https://github.com/Digital-Process-Tools/claude-5h-window-spread/releases/tag/v0.3.0
[0.2.1]: https://github.com/Digital-Process-Tools/claude-5h-window-spread/releases/tag/v0.2.1
