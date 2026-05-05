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
import shutil
import subprocess
import sys
from dataclasses import dataclass

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
      3. min round-hour penalty           — prefer HH:00 over HH:30
      4. min ping_start                   — earlier first ping (deterministic)
    """
    first_start = blocks[0][0]
    last_end = max(end for _, end in blocks)

    earliest = first_start - WINDOW_LEN_MIN
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
            round_penalty = sum(0 if p % 60 == 0 else 1 for p in combo)
            key = (max_w, len(windows), round_penalty, -combo[0])
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


def cmd_install(args: argparse.Namespace) -> int:
    """Read pings JSON from file or stdin, invoke claude-code-scheduler add per ping."""
    if args.path == "-":
        data = json.load(sys.stdin)
    else:
        with open(args.path) as f:
            data = json.load(f)

    pings: list[str] = data["spread"]["pings"] if "spread" in data else data["pings"]
    command = args.command
    weekdays_only = args.weekdays

    scheduler = shutil.which("claude-code-scheduler") or shutil.which("ccs")
    if scheduler is None:
        print(
            "claude-code-scheduler binary not found in PATH. Install via:\n"
            "  /plugin install scheduler@claude-code-scheduler",
            file=sys.stderr,
        )
        return 2

    results = []
    for ping in pings:
        # natural-language schedule string for claude-code-scheduler
        days = "every weekday" if weekdays_only else "every day"
        schedule_str = f"{days} at {ping}"
        cmd = [scheduler, "add", schedule_str, command]
        if args.dry_run:
            results.append({"ping": ping, "cmd": cmd, "dry_run": True})
            continue
        proc = subprocess.run(cmd, capture_output=True, text=True)
        results.append(
            {
                "ping": ping,
                "cmd": cmd,
                "returncode": proc.returncode,
                "stdout": proc.stdout.strip(),
                "stderr": proc.stderr.strip(),
            }
        )

    json.dump({"installed": results}, sys.stdout, indent=2)
    sys.stdout.write("\n")
    return 0 if all(r.get("returncode", 0) == 0 for r in results) else 1


# ---------- entrypoint -------------------------------------------------------


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(description="window-spread: optimal pings for Claude 5h windows")
    sub = p.add_subparsers(dest="cmd", required=True)

    pc = sub.add_parser("compute", help="compute optimal pings from work blocks")
    pc.add_argument("--blocks", required=True, help='e.g. "8:30-12:20,14:00-18:00,20:00-23:00"')
    pc.set_defaults(func=cmd_compute)

    pi = sub.add_parser("install", help="install pings via claude-code-scheduler")
    pi.add_argument("path", help="path to pings JSON, or '-' for stdin")
    pi.add_argument(
        "--command",
        default='claude -p "hi" --output-format json',
        help="command each ping will run (default: claude -p hi)",
    )
    pi.add_argument("--weekdays", action="store_true", help="weekdays only (default: every day)")
    pi.add_argument("--dry-run", action="store_true", help="print commands without running")
    pi.set_defaults(func=cmd_install)

    return p


def main(argv: list[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())
