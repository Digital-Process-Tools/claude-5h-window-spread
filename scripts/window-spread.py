#!/usr/bin/env python3
"""window-spread — compute optimal cron pings for Claude Pro/Max 5h windows.

Two subcommands:
  compute --blocks "HH:MM-HH:MM,..."     output JSON of optimal pings
  install <pings.json>                    invoke claude-code-scheduler add for each

Pure stdlib. Output is JSON for skill consumption.
"""

from __future__ import annotations

import argparse
import json
import platform
import plistlib
import re
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

WINDOW_LEN_MIN = 5 * 60  # 5h in minutes
DAY_MIN = 24 * 60
PING_STEP_MIN = 30  # search granularity for ping_start


# ---------- time helpers -----------------------------------------------------


def parse_time(s: str) -> int:
    """'8:30' / '08:30' / '8h30' / '14h' -> minutes from day-start."""
    raw = s.strip().lower().replace("h", ":")
    if ":" not in raw:
        raw += ":00"
    parts = raw.split(":")
    if len(parts) != 2:
        raise ValueError(f"invalid time: {s!r}")
    h_str, m_str = parts
    if not h_str:
        raise ValueError(f"invalid time: {s!r}")
    minute = int(m_str) if m_str else 0
    return int(h_str) * 60 + minute


def format_time(minutes: int) -> str:
    """510 -> '08:30'. Wraps modulo 24h."""
    minutes = minutes % DAY_MIN
    return f"{minutes // 60:02d}:{minutes % 60:02d}"


def format_duration(minutes: int) -> str:
    """200 -> '3h20'. 0 -> '0'. 60 -> '1h'."""
    if minutes <= 0:
        return "0"
    h, m = divmod(minutes, 60)
    if h == 0:
        return f"{m}min"
    if m == 0:
        return f"{h}h"
    return f"{h}h{m:02d}"


def parse_blocks(s: str) -> list[tuple[int, int]]:
    """'8:30-12:20,14:00-18:00' -> [(510, 740), (840, 1080)].

    Handles blocks crossing midnight by adding 24h to end.
    """
    blocks = []
    for chunk in s.split(","):
        chunk = chunk.strip()
        if not chunk:
            continue
        if "-" not in chunk:
            raise ValueError(f"block must be HH:MM-HH:MM, got {chunk!r}")
        start_s, end_s = chunk.split("-", 1)
        start = parse_time(start_s)
        end = parse_time(end_s)
        if end <= start:
            end += DAY_MIN
        blocks.append((start, end))
    if not blocks:
        raise ValueError("no blocks parsed")
    return sorted(blocks)


# ---------- core algorithm ---------------------------------------------------


@dataclass
class Schedule:
    ping_start_min: int
    windows: list[tuple[int, int]]  # (start_min, end_min)
    work_per_window: list[int]  # minutes of work in each window

    @property
    def max_work_min(self) -> int:
        return max(self.work_per_window) if self.work_per_window else 0


def simulate(ping_start: int, blocks: list[tuple[int, int]]) -> Schedule:
    """Build CONTIGUOUS windows starting at ping_start, 5h apart, until they cover all blocks.

    Used by tests and for the natural baseline. find_optimal uses _simulate_pings
    which allows non-contiguous windows.
    """
    last_end = max(end for _, end in blocks)
    windows: list[tuple[int, int]] = []
    cur = ping_start
    while cur < last_end:
        windows.append((cur, cur + WINDOW_LEN_MIN))
        cur += WINDOW_LEN_MIN

    work = [0] * len(windows)
    for b_start, b_end in blocks:
        for i, (w_start, w_end) in enumerate(windows):
            overlap = max(0, min(b_end, w_end) - max(b_start, w_start))
            work[i] += overlap

    return Schedule(ping_start_min=ping_start, windows=windows, work_per_window=work)


def _windows_cover_block(windows: list[tuple[int, int]], block: tuple[int, int]) -> bool:
    """A block must be fully inside one window OR a CONTIGUOUS run of windows.

    If windows are non-contiguous (gaps between them), the block must NOT span
    across a gap — Claude usage outside an active window can't happen.
    """
    b_start, b_end = block
    overlapping = [w for w in windows if w[1] > b_start and w[0] < b_end]
    if not overlapping:
        return False
    if overlapping[0][0] > b_start:
        return False
    if overlapping[-1][1] < b_end:
        return False
    # all overlapping windows must be back-to-back inside the block range
    for i in range(len(overlapping) - 1):
        if overlapping[i][1] != overlapping[i + 1][0]:
            return False
    return True


def _gen_ping_combos(candidates: list[int], n: int, min_value: int = -10**9):
    """Yield combinations of n candidates, sorted, with each ≥5h apart from previous.

    Recursive generator. Each chosen ping must be >= min_value (5h after prior).
    """
    if n == 0:
        yield []
        return
    for i, c in enumerate(candidates):
        if c < min_value:
            continue
        if n == 1:
            yield [c]
        else:
            for rest in _gen_ping_combos(candidates[i + 1:], n - 1, c + WINDOW_LEN_MIN):
                yield [c] + rest


def find_optimal(blocks: list[tuple[int, int]], max_pings: int = 5) -> Schedule:
    """Find optimal ping schedule allowing NON-CONTIGUOUS windows.

    Each ping starts a 5h window. Pings must be ≥5h apart (Anthropic constraint:
    a new window only opens once the prior one expires). Windows can have GAPS
    between them — the user's machine just sits idle until the next ping fires.

    Each block must be fully covered by either:
      * a single window, OR
      * a CONTIGUOUS run of windows (block cannot span a gap)

    Optimization (lexicographic):
      1. min max(work_per_window)        — balance cap load
      2. min num_pings                    — fewer pings = simpler, less idle cap
      3. max ping_start (-combo[0])       — later first ping = less idle pre-work

    No round-hour preference — optimal time is optimal time. If 06:23 beats
    06:00, we pick 06:23.
    """
    first_start = blocks[0][0]
    last_end = max(end for _, end in blocks)

    # Candidates are CLOCK-ALIGNED (multiples of PING_STEP_MIN), so a 30-min
    # grid always considers round hours regardless of where blocks start.
    raw_earliest = first_start - WINDOW_LEN_MIN
    earliest = (raw_earliest // PING_STEP_MIN) * PING_STEP_MIN
    if earliest > raw_earliest:
        earliest -= PING_STEP_MIN
    latest = last_end
    candidates = list(range(earliest, latest + 1, PING_STEP_MIN))

    best: Schedule | None = None
    best_key: tuple | None = None

    for n in range(1, max_pings + 1):
        for combo in _gen_ping_combos(candidates, n):
            windows = [(p, p + WINDOW_LEN_MIN) for p in combo]
            if not all(_windows_cover_block(windows, b) for b in blocks):
                continue
            work = [0] * len(windows)
            for b_start, b_end in blocks:
                for i, (w_start, w_end) in enumerate(windows):
                    overlap = max(0, min(b_end, w_end) - max(b_start, w_start))
                    work[i] += overlap
            max_w = max(work)
            key = (max_w, len(windows), -combo[0])
            if best_key is None or key < best_key:
                best = Schedule(
                    ping_start_min=combo[0],
                    windows=windows,
                    work_per_window=work,
                )
                best_key = key

    if best is None:
        raise RuntimeError("no valid ping schedule found (blocks too long for 5h windows?)")
    return best


def natural_baseline(blocks: list[tuple[int, int]]) -> Schedule:
    """Simulate the no-plugin scenario: each block triggers its own window at block start."""
    windows: list[tuple[int, int]] = []
    work: list[int] = []
    next_eligible = float("-inf")
    for b_start, b_end in blocks:
        if b_start >= next_eligible:
            w_start = b_start
            w_end = w_start + WINDOW_LEN_MIN
            windows.append((w_start, w_end))
            work.append(0)
            next_eligible = w_end
        # accumulate overlap with the latest window
        latest_w_start, latest_w_end = windows[-1]
        overlap = max(0, min(b_end, latest_w_end) - max(b_start, latest_w_start))
        work[-1] += overlap
        # any remaining bit of the block past the current window opens another
        leftover_start = max(b_start, latest_w_end)
        if leftover_start < b_end:
            new_start = leftover_start
            windows.append((new_start, new_start + WINDOW_LEN_MIN))
            work.append(b_end - new_start)
            next_eligible = new_start + WINDOW_LEN_MIN

    return Schedule(ping_start_min=blocks[0][0], windows=windows, work_per_window=work)


# ---------- subcommands ------------------------------------------------------


def cmd_compute(args: argparse.Namespace) -> int:
    blocks = parse_blocks(args.blocks)
    spread = find_optimal(blocks)
    natural = natural_baseline(blocks)

    out = {
        "blocks": [
            {"start": format_time(s), "end": format_time(e), "duration": format_duration(e - s)}
            for s, e in blocks
        ],
        "spread": {
            "pings": [format_time(w_start) for w_start, _ in spread.windows],
            "windows": [
                {
                    "start": format_time(w_start),
                    "end": format_time(w_end),
                    "work": format_duration(work_min),
                    "work_minutes": work_min,
                }
                for (w_start, w_end), work_min in zip(spread.windows, spread.work_per_window)
            ],
            "max_work": format_duration(spread.max_work_min),
            "max_work_minutes": spread.max_work_min,
            "windows_per_day": len(spread.windows),
        },
        "natural": {
            "windows": [
                {
                    "start": format_time(w_start),
                    "end": format_time(w_end),
                    "work": format_duration(work_min),
                    "work_minutes": work_min,
                }
                for (w_start, w_end), work_min in zip(natural.windows, natural.work_per_window)
            ],
            "max_work": format_duration(natural.max_work_min),
            "max_work_minutes": natural.max_work_min,
            "windows_per_day": len(natural.windows),
        },
        "improvement": {
            "max_work_reduction_minutes": natural.max_work_min - spread.max_work_min,
            "extra_windows": len(spread.windows) - len(natural.windows),
        },
    }
    json.dump(out, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


# ---------- cross-platform scheduler -----------------------------------------

LABEL_PREFIX = "com.dpt.window-spread"
DEFAULT_COMMAND = 'claude -p "Just say hi" --output-format json --model haiku'


def _label(ping: str) -> str:
    """ping '06:00' -> 'com.dpt.window-spread.0600'."""
    return f"{LABEL_PREFIX}.{ping.replace(':', '')}"


# A ping is a zero-padded 24h wall-clock time, which is what `format_time`
# emits and what every scheduler here is handed. The shape is the same on all
# three platforms, so the check is too.
_PING_RE = re.compile(r"^([01][0-9]|2[0-3]):[0-5][0-9]$")


def _validate_ping(ping: str) -> None:
    """Refuse a ping that is not 'HH:MM'.

    Until #7 this check existed only as a side effect of the `int()` calls in
    `_split_hm`, which the Windows branch never called -- so an arbitrary
    string reached a schtasks task name and start time. The check is named and
    called explicitly now so that a new platform branch cannot inherit the
    guarantee by accident, or miss it by accident.
    """
    if not isinstance(ping, str) or not _PING_RE.match(ping):
        raise ValueError(
            f"ping must be a zero-padded 24h time 'HH:MM', got {ping!r}"
        )


def _split_hm(ping: str) -> tuple[int, int]:
    _validate_ping(ping)
    h, m = ping.split(":")
    return int(h), int(m)


# ---- macOS launchd ----------------------------------------------------------


# Everything a plist string may hold. XML 1.0 forbids the C0 controls outright
# except tab, LF and CR; of those three, CR is *legal to write* and then
# normalised to LF by every conforming parser on the way back -- so a command
# containing one would be silently rewritten before launchd ever ran it.
# Refusing it is cheaper than shipping a command the user did not type.
_PLIST_ALLOWED_CONTROLS = frozenset("\t\n")


def _validate_plist_command(command: str) -> None:
    """Refuse a command a launchd plist cannot carry unchanged.

    macOS is not "no rule" -- it is a *different* rule from cron's (#6). A
    newline and a bare `%` are fine here and fatal on a crontab line; a C0
    control character is fine on a crontab line and cannot be represented in
    an XML plist at all. `plistlib.dumps` raises `ValueError` on one and
    `UnicodeEncodeError` on an undecodable argv byte, both from inside the
    installer -- i.e. after `cmd_install` has already removed every existing
    ping. This states the rule where it can be applied before that.
    """
    for ch in command:
        if ch < " " and ch not in _PLIST_ALLOWED_CONTROLS:
            raise ValueError(
                f"--command may not contain the control character "
                f"U+{ord(ch):04X}: a launchd plist is XML and cannot carry it"
            )
    try:
        command.encode("utf-8")
    except UnicodeEncodeError as exc:
        # A POSIX argv that is not valid UTF-8 arrives as a lone surrogate.
        raise ValueError(
            "--command is not valid UTF-8 and cannot be written to a plist"
        ) from exc


def _macos_plist(label: str, command: str, hour: int, minute: int, weekdays_only: bool) -> str:
    """Build a launchd plist XML.

    Weekday in launchd: 1=Mon..7=Sun. Weekdays-only = entries for 1-5.

    Serialised with `plistlib.dumps` rather than interpolated into an XML
    template (#6). The template could not encode a command containing `&` or
    `<` -- both ordinary in a shell command -- so `claude -p 'a && b'`
    produced a document launchd cannot parse, and a command carrying
    `</string></array><key>...` produced one it parses as a *different* job.
    A serialiser has no such category: every value is encoded, so `&` and `<`
    stop being special. What remains is a *narrower* rule, not no rule --
    see `_validate_plist_command`, which is called below.
    """
    # cmd_install validates too; this is the check at the point of use, so a
    # direct caller gets this tool's message rather than a plistlib traceback.
    _validate_plist_command(command)
    interval: object
    if weekdays_only:
        interval = [
            {"Hour": hour, "Minute": minute, "Weekday": wd} for wd in (1, 2, 3, 4, 5)
        ]
    else:
        interval = {"Hour": hour, "Minute": minute}
    log_dir = Path.home() / "Library/Logs/window-spread"
    doc = {
        "Label": label,
        "ProgramArguments": ["/bin/bash", "-lc", command],
        "StartCalendarInterval": interval,
        "StandardOutPath": f"{log_dir}/{label}.out",
        "StandardErrorPath": f"{log_dir}/{label}.err",
    }
    return plistlib.dumps(doc).decode("utf-8")


def _install_macos(pings: list[str], command: str, weekdays_only: bool, dry_run: bool) -> list[dict]:
    plist_dir = Path.home() / "Library/LaunchAgents"
    log_dir = Path.home() / "Library/Logs/window-spread"
    if not dry_run:
        plist_dir.mkdir(parents=True, exist_ok=True)
        log_dir.mkdir(parents=True, exist_ok=True)
    results = []
    for ping in pings:
        hour, minute = _split_hm(ping)
        label = _label(ping)
        plist = _macos_plist(label, command, hour, minute, weekdays_only)
        plist_path = plist_dir / f"{label}.plist"
        if dry_run:
            results.append({"ping": ping, "label": label, "path": str(plist_path), "dry_run": True})
            continue
        # Keep the job that is already there until the new one is known to
        # load (#6). The previous order unloaded and overwrote first, so a
        # plist launchd refused left the user with neither the new ping nor
        # the one they had, and no copy of it anywhere.
        previous = plist_path.read_bytes() if plist_path.exists() else None
        # idempotent: unload existing first (ignore error)
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        # write_bytes, not write_text: the document declares UTF-8 in its own
        # XML header, and write_text would encode with the process locale.
        plist_path.write_bytes(plist.encode("utf-8"))
        proc = subprocess.run(
            ["launchctl", "load", "-w", str(plist_path)], capture_output=True, text=True
        )
        entry = {
            "ping": ping,
            "label": label,
            "path": str(plist_path),
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
        }
        if proc.returncode != 0:
            if previous is None:
                # nothing was there: leave nothing behind either.
                plist_path.unlink(missing_ok=True)
                entry["restored"] = False
            else:
                plist_path.write_bytes(previous)
                restore = subprocess.run(
                    ["launchctl", "load", "-w", str(plist_path)],
                    capture_output=True,
                    text=True,
                )
                entry["restored"] = restore.returncode == 0
        results.append(entry)
    return results


def _uninstall_macos(dry_run: bool) -> list[dict]:
    plist_dir = Path.home() / "Library/LaunchAgents"
    if not plist_dir.exists():
        return []
    results = []
    for plist_path in sorted(plist_dir.glob(f"{LABEL_PREFIX}.*.plist")):
        label = plist_path.stem
        if dry_run:
            results.append({"label": label, "path": str(plist_path), "dry_run": True})
            continue
        subprocess.run(["launchctl", "unload", str(plist_path)], capture_output=True)
        plist_path.unlink(missing_ok=True)
        results.append({"label": label, "path": str(plist_path), "removed": True})
    return results


def _list_macos() -> list[dict]:
    plist_dir = Path.home() / "Library/LaunchAgents"
    if not plist_dir.exists():
        return []
    return [
        {"label": p.stem, "path": str(p)}
        for p in sorted(plist_dir.glob(f"{LABEL_PREFIX}.*.plist"))
    ]


# ---- Linux cron -------------------------------------------------------------


def _validate_cron_command(command: str) -> None:
    """Refuse a command that would not survive being written to a crontab line.

    A crontab entry is one line, so an embedded newline does not produce a
    broken entry -- it produces a second, valid, unmarked one, and the marker
    comment then labels the wrong line (#5). cron also rewrites an unescaped
    '%' into a newline and feeds the remainder to the job on stdin, which
    truncates the command the same way without a newline ever being typed.
    """
    for ch, name in (("\n", "newline"), ("\r", "carriage return")):
        if ch in command:
            raise ValueError(
                f"--command may not contain a {name}: it would split the crontab "
                f"line into a second, unmarked entry"
            )
    # Deliberately conservative: a backslash consumes the character after it,
    # so `\%` is accepted and `\\%` -- where whether the '%' is escaped depends
    # on the cron implementation's own backslash handling -- is refused. A
    # false refusal costs an error message; a false accept costs a truncated
    # command and an orphan entry.
    i = 0
    while i < len(command):
        if command[i] == "\\":
            i += 2
            continue
        if command[i] == "%":
            raise ValueError(
                "--command may not contain a bare '%': cron rewrites it to a "
                "newline and truncates the command. Escape it as \\% if it is "
                "meant literally"
            )
        i += 1


def _cron_line(ping: str, command: str, weekdays_only: bool) -> str:
    """Return a single crontab line with a marker comment.

    The marker is a trailing comment on the same line as the command it
    labels. That is only safe because the command is validated first: the
    orphan entries in #5 came from a command that split the line out from
    under its own marker. Keeping the marker where it has always been means
    entries written by older versions are still found by uninstall.
    """
    _validate_cron_command(command)
    hour, minute = _split_hm(ping)
    dow = "1-5" if weekdays_only else "*"
    return f"{minute} {hour} * * {dow} {command} # {_label(ping)}"


# _read_crontab states. "empty" and "unreadable" are both a non-zero
# `crontab -l`, and collapsing them is what let an install overwrite an
# entire crontab (#4).
CRONTAB_OK = "ok"
CRONTAB_EMPTY = "empty"
CRONTAB_UNREADABLE = "unreadable"

# Markers that mean "the crontab exists and I was not allowed to read it".
# Checked first: a message carrying both shapes must never read as empty.
_CRONTAB_DENIED_MARKERS = (
    "permission denied",
    "operation not permitted",
    "access is denied",
    "not allowed to use",
    "you are not authorized",
)
# Markers that mean "there is no crontab". vixie-cron, cronie and macOS all
# print "no crontab for <user>". busybox reports the missing spool file as
# ENOENT, which on its own is far too broad -- `crontab: error while loading
# shared libraries: ...: No such file or directory` is a crontab that exists
# and could not be read -- so ENOENT only counts when the message also names
# the cron spool. Anything else, silent failures included, is unreadable.
_CRONTAB_ABSENT_MARKERS = ("no crontab for",)
_CRONTAB_ENOENT_MARKER = "no such file or directory"
_CRONTAB_SPOOL_MARKER = "spool/cron"


@dataclass(frozen=True)
class CrontabRead:
    """The outcome of `crontab -l`, in three states rather than two."""

    state: str
    lines: list[str]
    stderr: str = ""

    @property
    def usable(self) -> bool:
        """True when we know what is in the crontab and may replace it."""
        return self.state in (CRONTAB_OK, CRONTAB_EMPTY)


def _read_crontab() -> CrontabRead:
    """Read the user's crontab, distinguishing "none" from "cannot read".

    `crontab -l` exits non-zero both when there is no crontab and when there
    is one it could not read. Only a recognised "no crontab" message is
    treated as empty; every other failure is CRONTAB_UNREADABLE, because an
    install that cannot establish what is already there has not learned that
    nothing is there.
    """
    try:
        proc = subprocess.run(["crontab", "-l"], capture_output=True, text=True)
    except OSError as exc:  # crontab not installed, not executable, ...
        return CrontabRead(CRONTAB_UNREADABLE, [], f"could not run crontab: {exc}")
    if proc.returncode == 0:
        return CrontabRead(CRONTAB_OK, proc.stdout.splitlines(), (proc.stderr or "").strip())
    stderr = (proc.stderr or "").strip()
    low = stderr.lower()
    if any(m in low for m in _CRONTAB_DENIED_MARKERS):
        return CrontabRead(CRONTAB_UNREADABLE, [], stderr)
    if any(m in low for m in _CRONTAB_ABSENT_MARKERS):
        return CrontabRead(CRONTAB_EMPTY, [], stderr)
    if _CRONTAB_ENOENT_MARKER in low and _CRONTAB_SPOOL_MARKER in low:
        return CrontabRead(CRONTAB_EMPTY, [], stderr)
    return CrontabRead(CRONTAB_UNREADABLE, [], stderr)


def _unreadable_error(read: CrontabRead) -> dict:
    return {
        "error": "crontab-unreadable",
        "returncode": 1,
        "stderr": read.stderr or "crontab -l failed without a message",
        "hint": "re-run with --force-replace-crontab to replace it anyway",
    }


def _warn_unreadable(action: str, read: CrontabRead) -> None:
    print(
        f"window-spread: could not read the existing crontab, so {action} was "
        f"refused rather than risk replacing it: "
        f"{read.stderr or 'crontab -l failed without a message'}",
        file=sys.stderr,
    )


def _write_crontab(lines: list[str]) -> subprocess.CompletedProcess:
    content = "\n".join(lines) + "\n" if lines else ""
    try:
        return subprocess.run(["crontab", "-"], input=content, capture_output=True, text=True)
    except OSError as exc:
        return subprocess.CompletedProcess(
            ["crontab", "-"], 1, "", f"could not run crontab: {exc}"
        )


def _install_linux(
    pings: list[str],
    command: str,
    weekdays_only: bool,
    dry_run: bool,
    force: bool = False,
) -> list[dict]:
    # Validate before reading anything, so a bad command cannot reach a write
    # under any code path, dry-run included.
    _validate_cron_command(command)
    read = _read_crontab()
    if not read.usable and not force:
        _warn_unreadable("install", read)
        return [dict(_unreadable_error(read), ping=ping) for ping in pings]
    existing = [l for l in read.lines if LABEL_PREFIX not in l]
    new_lines = [_cron_line(ping, command, weekdays_only) for ping in pings]
    final = existing + new_lines
    if dry_run:
        return [{"ping": ping, "line": line, "dry_run": True} for ping, line in zip(pings, new_lines)]
    proc = _write_crontab(final)
    return [
        {
            "ping": ping,
            "line": line,
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
        }
        for ping, line in zip(pings, new_lines)
    ]


def _uninstall_linux(dry_run: bool, force: bool = False) -> list[dict]:
    read = _read_crontab()
    if not read.usable and not force:
        _warn_unreadable("uninstall", read)
        return [_unreadable_error(read)]
    keep = [l for l in read.lines if LABEL_PREFIX not in l]
    removed = [l for l in read.lines if LABEL_PREFIX in l]
    if dry_run:
        return [{"line": l, "dry_run": True} for l in removed]
    if not removed:
        return []
    proc = _write_crontab(keep)
    # Report the write's own outcome: "removed" used to be True even when the
    # rewrite failed and the entries were all still there.
    return [
        {
            "line": l,
            "removed": proc.returncode == 0,
            "returncode": proc.returncode,
            "stderr": proc.stderr.strip(),
        }
        for l in removed
    ]


def _list_linux() -> list[dict]:
    read = _read_crontab()
    if not read.usable:
        print(
            f"window-spread: could not read the crontab, so this list may be "
            f"incomplete: {read.stderr or 'crontab -l failed without a message'}",
            file=sys.stderr,
        )
        return []
    return [{"line": l} for l in read.lines if LABEL_PREFIX in l]


# ---- Windows Task Scheduler -------------------------------------------------


def _install_windows(pings: list[str], command: str, weekdays_only: bool, dry_run: bool) -> list[dict]:
    results = []
    for ping in pings:
        # The other two branches reach _validate_ping through _split_hm; this
        # one has no hour/minute to split, which is exactly how it ended up
        # putting an arbitrary string into a task name and a /st value (#7).
        # cmd_install validates too -- this is the check at the point of use,
        # for anyone calling the helper directly.
        _validate_ping(ping)
        label = _label(ping)
        cmd = [
            "schtasks",
            "/create",
            "/tn",
            label,
            "/tr",
            f'cmd /c {command}',
            "/sc",
            "WEEKLY" if weekdays_only else "DAILY",
            "/st",
            ping,
            "/f",
        ]
        if weekdays_only:
            cmd[-1:-1] = ["/d", "MON,TUE,WED,THU,FRI"]
        if dry_run:
            results.append({"ping": ping, "label": label, "cmd": cmd, "dry_run": True})
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True)
        results.append(
            {
                "ping": ping,
                "label": label,
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        )
    return results


def _uninstall_windows(dry_run: bool) -> list[dict]:
    proc = subprocess.run(
        ["schtasks", "/query", "/fo", "csv", "/nh"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return []
    labels = []
    for line in proc.stdout.splitlines():
        # CSV: "TaskName","NextRunTime","Status"
        parts = line.split('","')
        if not parts:
            continue
        tn = parts[0].lstrip('"').lstrip("\\")
        if tn.startswith(LABEL_PREFIX):
            labels.append(tn)
    results = []
    for label in labels:
        if dry_run:
            results.append({"label": label, "dry_run": True})
            continue
        proc = subprocess.run(
            ["schtasks", "/delete", "/tn", label, "/f"], capture_output=True, text=True
        )
        results.append({"label": label, "removed": proc.returncode == 0})
    return results


def _list_windows() -> list[dict]:
    proc = subprocess.run(
        ["schtasks", "/query", "/fo", "csv", "/nh"], capture_output=True, text=True
    )
    if proc.returncode != 0:
        return []
    out = []
    for line in proc.stdout.splitlines():
        parts = line.split('","')
        if not parts:
            continue
        tn = parts[0].lstrip('"').lstrip("\\")
        if tn.startswith(LABEL_PREFIX):
            out.append({"label": tn})
    return out


# ---- dispatcher -------------------------------------------------------------


def _dispatch(action: str, *args, **kwargs):
    system = platform.system()
    if system == "Darwin":
        funcs = {
            "install": _install_macos,
            "uninstall": _uninstall_macos,
            "list": _list_macos,
        }
    elif system == "Linux":
        funcs = {
            "install": _install_linux,
            "uninstall": _uninstall_linux,
            "list": _list_linux,
        }
    elif system == "Windows":
        funcs = {
            "install": _install_windows,
            "uninstall": _uninstall_windows,
            "list": _list_windows,
        }
    else:
        raise RuntimeError(f"unsupported OS: {system}")
    return funcs[action](*args, **kwargs)


def _validate_install_input(pings: list[str], command: str, system: str) -> None:
    """Check everything `install` was given, before anything is removed.

    Two rules with two different scopes, which is why they are not one
    validator (#6, #7):

    * **The pings are checked on every platform.** A ping means the same thing
      to launchd, cron and schtasks -- a wall-clock time -- so the shape is not
      platform-dependent. Before #7 this was enforced only by the `int()` calls
      inside `_split_hm`, and `_install_windows` never called it.

    * **The command is checked per platform, because "valid" differs**, and it
      differs in both directions -- which is why hoisting one validator to all
      three would be wrong rather than merely redundant:

      - Linux (`_validate_cron_command`): no newline, carriage return or bare
        `%`. A crontab entry is one line, and cron rewrites an unescaped `%`
        to a newline. A newline and a `%` are both harmless on launchd.
      - macOS (`_validate_plist_command`): no C0 control character other than
        tab and newline, and valid UTF-8. A plist is XML, which cannot carry
        one; a carriage return it *can* carry is normalised to a newline on
        read, so the command launchd runs would differ from the one typed.
        Every one of those is harmless on a crontab line.
      - Windows: no rule. `schtasks /tr` applies its own quoting to
        `cmd /c <command>`, and that has not been measured on Windows; an
        unverified rule would be worse than a stated gap. See #7.
    """
    for ping in pings:
        _validate_ping(ping)
    if system == "Linux":
        _validate_cron_command(command)
    elif system == "Darwin":
        _validate_plist_command(command)


def _crontab_force_kwargs(args: argparse.Namespace) -> dict:
    """--force-replace-crontab is a cron-only override; other schedulers never see it."""
    if platform.system() != "Linux":
        return {}
    return {"force": bool(getattr(args, "force_replace_crontab", False))}


def cmd_install(args: argparse.Namespace) -> int:
    """Install pings via the local OS scheduler (launchd / cron / Task Scheduler).

    Destructive-replace: removes all existing window-spread entries first to
    avoid accumulating stale pings when re-running setup with different blocks.
    """
    if args.path == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.path) as f:
            data = json.load(f)
    pings: list[str] = data["spread"]["pings"] if "spread" in data else data["pings"]
    command = args.command
    weekdays_only = args.weekdays

    # Everything below the uninstall pass is destructive, so every check runs
    # above it: a refusal must not cost the user the entries they already had.
    try:
        _validate_install_input(pings, command, platform.system())
    except ValueError as exc:
        print(f"window-spread: {exc}", file=sys.stderr)
        return 2

    kwargs = _crontab_force_kwargs(args)
    removed = _dispatch("uninstall", args.dry_run, **kwargs)
    installed = _dispatch("install", pings, command, weekdays_only, args.dry_run, **kwargs)
    json.dump(
        {"os": platform.system(), "removed": removed, "installed": installed},
        sys.stdout,
        indent=2,
    )
    sys.stdout.write("\n")
    return 0 if all(r.get("returncode", 0) == 0 for r in installed) else 1


def cmd_uninstall(args: argparse.Namespace) -> int:
    """Remove all entries with our LABEL_PREFIX from the local OS scheduler.

    Returns non-zero when a result reports one: on Linux that covers a crontab
    that could not be read (refused rather than rewritten -- see
    --force-replace-crontab) and a rewrite that failed. The macOS and Windows
    paths do not yet report a returncode, so they still exit 0.
    """
    results = _dispatch("uninstall", args.dry_run, **_crontab_force_kwargs(args))
    json.dump({"os": platform.system(), "removed": results}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if all(r.get("returncode", 0) == 0 for r in results) else 1


def cmd_list(args: argparse.Namespace) -> int:
    """List currently installed window-spread entries."""
    results = _dispatch("list")
    json.dump({"os": platform.system(), "entries": results}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0


# ---------- entrypoint -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="window-spread: optimal pings for Claude 5h windows")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("compute", help="compute optimal pings from work blocks")
    pc.add_argument("--blocks", required=True, help='e.g. "8:30-12:20,14:00-18:00,20:00-23:00"')
    pc.set_defaults(func=cmd_compute)

    pi = sub.add_parser("install", help="install pings via local OS scheduler")
    pi.add_argument("path", help="path to pings JSON, or '-' for stdin")
    pi.add_argument(
        "--command",
        default=DEFAULT_COMMAND,
        help="command each ping will run (default: claude -p 'Just say hi' --model haiku)",
    )
    pi.add_argument("--weekdays", action="store_true", help="weekdays only (default: every day)")
    pi.add_argument("--dry-run", action="store_true", help="print commands without running")
    pi.add_argument(
        "--force-replace-crontab",
        action="store_true",
        help="Linux only: replace the crontab even though it could not be read "
        "(this DISCARDS whatever is in it)",
    )
    pi.set_defaults(func=cmd_install)

    pu = sub.add_parser("uninstall", help="remove all our entries from the OS scheduler")
    pu.add_argument("--dry-run", action="store_true", help="print what would be removed")
    pu.add_argument(
        "--force-replace-crontab",
        action="store_true",
        help="Linux only: rewrite the crontab even though it could not be read "
        "(this DISCARDS whatever is in it)",
    )
    pu.set_defaults(func=cmd_uninstall)

    pl = sub.add_parser("list", help="list installed window-spread entries")
    pl.set_defaults(func=cmd_list)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
