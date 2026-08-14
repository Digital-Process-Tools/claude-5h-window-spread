"""Unit tests for window-spread script. Stdlib unittest, no pytest dep."""

from __future__ import annotations

import importlib.util
import json
import plistlib
import sys
import unittest
from io import StringIO
from pathlib import Path
from unittest.mock import patch

# load script as module without packaging it
# (must register in sys.modules before exec_module — @dataclass needs it on Py 3.13)
SCRIPT_PATH = Path(__file__).parent.parent / "scripts" / "window-spread.py"
spec = importlib.util.spec_from_file_location("window_spread", SCRIPT_PATH)
ws = importlib.util.module_from_spec(spec)
sys.modules["window_spread"] = ws
spec.loader.exec_module(ws)


class TimeHelpersTest(unittest.TestCase):
    def test_parse_time_hh_mm(self):
        self.assertEqual(ws.parse_time("8:30"), 510)

    def test_parse_time_zero_padded(self):
        self.assertEqual(ws.parse_time("08:30"), 510)

    def test_parse_time_french_h_separator(self):
        self.assertEqual(ws.parse_time("8h30"), 510)

    def test_parse_time_hour_only(self):
        self.assertEqual(ws.parse_time("14"), 14 * 60)

    def test_parse_time_invalid(self):
        with self.assertRaises(ValueError):
            ws.parse_time("not-a-time")

    def test_format_time(self):
        self.assertEqual(ws.format_time(510), "08:30")
        self.assertEqual(ws.format_time(0), "00:00")
        self.assertEqual(ws.format_time(23 * 60 + 59), "23:59")

    def test_format_time_wraps_24h(self):
        self.assertEqual(ws.format_time(25 * 60), "01:00")  # next day

    def test_format_duration(self):
        self.assertEqual(ws.format_duration(0), "0")
        self.assertEqual(ws.format_duration(45), "45min")
        self.assertEqual(ws.format_duration(60), "1h")
        self.assertEqual(ws.format_duration(200), "3h20")
        self.assertEqual(ws.format_duration(240), "4h")


class ParseBlocksTest(unittest.TestCase):
    def test_simple(self):
        blocks = ws.parse_blocks("8:30-12:20,14:00-18:00")
        self.assertEqual(blocks, [(510, 740), (840, 1080)])

    def test_three_blocks(self):
        blocks = ws.parse_blocks("8:30-12:20,14:00-18:00,20:00-23:00")
        self.assertEqual(blocks, [(510, 740), (840, 1080), (1200, 1380)])

    def test_handles_whitespace(self):
        blocks = ws.parse_blocks(" 8:30-12:20 , 14:00-18:00 ")
        self.assertEqual(blocks, [(510, 740), (840, 1080)])

    def test_block_crossing_midnight(self):
        # 22:00-02:00 should become (1320, 1320 + 4*60) = (1320, 1560)
        blocks = ws.parse_blocks("22:00-02:00")
        self.assertEqual(blocks, [(1320, 1560)])

    def test_empty_input_raises(self):
        with self.assertRaises(ValueError):
            ws.parse_blocks("")

    def test_invalid_format_raises(self):
        with self.assertRaises(ValueError):
            ws.parse_blocks("invalid")


class SimulateTest(unittest.TestCase):
    def test_single_block_inside_one_window(self):
        # 8:30-12:20 (3h50) inside ping at 8:00 → window 8:00-13:00
        sim = ws.simulate(ping_start=8 * 60, blocks=[(510, 740)])
        self.assertEqual(sim.windows, [(480, 780)])
        self.assertEqual(sim.work_per_window, [230])

    def test_block_split_across_two_windows(self):
        # block 8:30-12:20 with ping at 6:00 → W1 6-11, W2 11-16
        # block_in_W1 = 11:00 - 8:30 = 150min, block_in_W2 = 12:20 - 11:00 = 80min
        sim = ws.simulate(ping_start=6 * 60, blocks=[(510, 740)])
        self.assertEqual(sim.windows[0], (360, 660))
        self.assertEqual(sim.windows[1], (660, 960))
        self.assertEqual(sim.work_per_window[0], 150)
        self.assertEqual(sim.work_per_window[1], 80)

    def test_florian_blocks_with_6am_ping(self):
        # full Florian schedule with 6:00 ping
        blocks = [(510, 740), (840, 1080), (1200, 1380)]
        sim = ws.simulate(ping_start=6 * 60, blocks=blocks)
        # W1 6-11: block1 8:30-11:00 = 150min
        # W2 11-16: block1 11:00-12:20 (80) + block2 14:00-16:00 (120) = 200
        # W3 16-21: block2 16:00-18:00 (120) + block3 20:00-21:00 (60) = 180
        # W4 21-02: block3 21:00-23:00 = 120
        self.assertEqual(sim.work_per_window, [150, 200, 180, 120])
        self.assertEqual(sim.max_work_min, 200)


class FindOptimalTest(unittest.TestCase):
    def test_florian_picks_latest_optimal_ping(self):
        # Florian's day: best ping_start is 06:30 (= 390min) — latest first ping
        # among schedules with max=200 (3h20). 06:00 also valid but has 30min
        # more idle in W1 before work starts.
        blocks = [(510, 740), (840, 1080), (1200, 1380)]
        spread = ws.find_optimal(blocks)
        self.assertEqual(spread.ping_start_min, 390)
        self.assertEqual(spread.max_work_min, 200)
        self.assertEqual(len(spread.windows), 4)

    def test_single_short_block_covers_all_work(self):
        # Single 1h block — algorithm may split for cap balance, but
        # invariants must hold: all work covered, max never exceeds block size
        blocks = [(600, 660)]  # 10:00-11:00
        spread = ws.find_optimal(blocks)
        self.assertEqual(sum(spread.work_per_window), 60)
        self.assertLessEqual(spread.max_work_min, 60)
        self.assertLessEqual(spread.ping_start_min, 600)
        self.assertGreaterEqual(spread.windows[-1][1], 660)


class NaturalBaselineTest(unittest.TestCase):
    def test_florian_three_blocks(self):
        blocks = [(510, 740), (840, 1080), (1200, 1380)]
        natural = ws.natural_baseline(blocks)
        # Each block triggers its own window — 3 windows, max_work = 4h (block 2 = 240)
        self.assertEqual(len(natural.windows), 3)
        self.assertEqual(natural.max_work_min, 240)


class ComputeOutputTest(unittest.TestCase):
    def test_compute_florian_emits_expected_pings(self):
        argv = ["compute", "--blocks", "8:30-12:20,14:00-18:00,20:00-23:00"]
        buf = StringIO()
        with patch("sys.stdout", buf):
            ws.main(argv)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["spread"]["pings"], ["06:30", "11:30", "16:30", "21:30"])
        self.assertEqual(out["spread"]["max_work"], "3h20")
        self.assertEqual(out["natural"]["max_work"], "4h")
        self.assertEqual(out["improvement"]["extra_windows"], 1)


# ---------- edge cases ------------------------------------------------------


class WeirdTimeFormatsTest(unittest.TestCase):
    def test_french_h_no_minutes(self):
        self.assertEqual(ws.parse_time("14h"), 14 * 60)

    def test_french_h_zero_padded(self):
        self.assertEqual(ws.parse_time("08h00"), 8 * 60)

    def test_french_h_with_minutes(self):
        self.assertEqual(ws.parse_time("12h20"), 12 * 60 + 20)

    def test_uppercase_H_separator(self):
        self.assertEqual(ws.parse_time("8H30"), 510)

    def test_midnight(self):
        self.assertEqual(ws.parse_time("0:00"), 0)
        self.assertEqual(ws.parse_time("00:00"), 0)

    def test_just_before_midnight(self):
        self.assertEqual(ws.parse_time("23:59"), 23 * 60 + 59)


class WeirdBlocksTest(unittest.TestCase):
    def test_block_ending_at_midnight(self):
        # 22:00-00:00 — 00:00 = 0 < 22:00, so end gets +24h → 22:00-24:00
        blocks = ws.parse_blocks("22:00-00:00")
        self.assertEqual(blocks, [(1320, 1440)])

    def test_late_evening_crossing_midnight(self):
        blocks = ws.parse_blocks("20:00-01:30")
        self.assertEqual(blocks, [(1200, 1530)])  # 1500 + 30 = 25:30

    def test_blocks_out_of_chronological_order_get_sorted(self):
        # input order shouldn't matter, parse_blocks should sort
        blocks = ws.parse_blocks("14:00-18:00,8:30-12:20")
        self.assertEqual(blocks, [(510, 740), (840, 1080)])

    def test_adjacent_blocks_no_gap(self):
        # 8:00-12:00 then 12:00-16:00 — back-to-back, no break
        blocks = ws.parse_blocks("8:00-12:00,12:00-16:00")
        self.assertEqual(blocks, [(480, 720), (720, 960)])

    def test_block_exactly_5h_gets_split_for_lower_cap_pressure(self):
        # 5h block: algorithm prefers splitting to halve cap pressure
        # rather than cramming all 5h into one window (which equals natural)
        blocks = [(8 * 60, 13 * 60)]  # 8:00-13:00 = 300min
        spread = ws.find_optimal(blocks)
        self.assertEqual(len(spread.windows), 2)
        # at midpoint split, each window absorbs ~150min
        self.assertLess(spread.max_work_min, 300)
        self.assertEqual(sum(spread.work_per_window), 300)  # all work covered

    def test_15min_sliver_block(self):
        blocks = [(600, 615)]  # 10:00-10:15
        spread = ws.find_optimal(blocks)
        self.assertGreaterEqual(len(spread.windows), 1)
        self.assertEqual(spread.max_work_min, 15)


class FindOptimalEdgeCasesTest(unittest.TestCase):
    def test_two_back_to_back_blocks_no_lunch(self):
        # 9:00-13:00 then 13:00-17:00 — no break, 8h total
        blocks = [(540, 780), (780, 1020)]
        spread = ws.find_optimal(blocks)
        # Total span 8h needs at least 2 windows
        self.assertGreaterEqual(len(spread.windows), 2)
        # max work should be < 4h ideally (split helps)
        self.assertLessEqual(spread.max_work_min, 240)

    def test_one_long_block_8h_no_breaks(self):
        # 9:00-17:00 single 8h block, no breaks
        blocks = [(540, 1020)]
        spread = ws.find_optimal(blocks)
        # Must split across 2+ windows
        self.assertGreaterEqual(len(spread.windows), 2)
        # No window can absorb the whole 8h block (only 5h capacity)
        self.assertLess(spread.max_work_min, 480)

    def test_evening_session_late_splits_for_lower_cap_pressure(self):
        # 4h evening block — algorithm splits to lower max work
        blocks = ws.parse_blocks("21:00-01:00")
        spread = ws.find_optimal(blocks)
        # split = max work strictly less than full 4h
        self.assertLess(spread.max_work_min, 4 * 60)
        self.assertEqual(sum(spread.work_per_window), 4 * 60)

    def test_minh_full_day_with_morning_meeting(self):
        # Hypothetical: meeting 9:00-10:00, work 10:00-12:30, lunch break, 14:00-19:00
        blocks = ws.parse_blocks("9:00-10:00,10:00-12:30,14:00-19:00")
        spread = ws.find_optimal(blocks)
        natural = ws.natural_baseline(blocks)
        # Spread should reduce or tie max work, never increase
        self.assertLessEqual(spread.max_work_min, natural.max_work_min)


class NaturalBaselineEdgeTest(unittest.TestCase):
    def test_block_longer_than_5h_natural_creates_extra_window(self):
        # 9:00-15:30 = 6h30. Natural opens W at 9:00 → expires 14:00.
        # Block tail 14:00-15:30 spills into a second natural window.
        blocks = [(540, 930)]  # 6h30
        natural = ws.natural_baseline(blocks)
        self.assertEqual(len(natural.windows), 2)
        # First window absorbs 5h, second gets 1h30
        self.assertEqual(natural.work_per_window[0], 300)
        self.assertEqual(natural.work_per_window[1], 90)

    def test_two_blocks_with_long_gap_each_gets_window(self):
        # 8:00-10:00, 17:00-19:00 — gap > 5h, two separate natural windows
        blocks = [(480, 600), (1020, 1140)]
        natural = ws.natural_baseline(blocks)
        self.assertEqual(len(natural.windows), 2)


class ComputeOutputEdgeTest(unittest.TestCase):
    def test_compute_with_french_format_blocks(self):
        argv = ["compute", "--blocks", "8h30-12h20,14h-18h,20h-23h"]
        buf = StringIO()
        with patch("sys.stdout", buf):
            ws.main(argv)
        out = json.loads(buf.getvalue())
        self.assertEqual(out["spread"]["pings"], ["06:30", "11:30", "16:30", "21:30"])

    def test_compute_with_evening_only_splits_5h_block(self):
        argv = ["compute", "--blocks", "20:00-01:00"]
        buf = StringIO()
        with patch("sys.stdout", buf):
            ws.main(argv)
        out = json.loads(buf.getvalue())
        # 5h evening block splits across 2 windows for lower cap pressure
        self.assertEqual(len(out["spread"]["windows"]), 2)
        # combined work covers full 5h block
        total_min = sum(w["work_minutes"] for w in out["spread"]["windows"])
        self.assertEqual(total_min, 5 * 60)

    def test_compute_blocks_must_be_present(self):
        with self.assertRaises(SystemExit):
            ws.main(["compute"])


# ---------- scheduler installer tests ---------------------------------------


class MacOSPlistTest(unittest.TestCase):
    def test_weekdays_only_has_5_intervals(self):
        plist = ws._macos_plist(
            "com.dpt.window-spread.0630",
            "claude -p hi",
            6,
            30,
            weekdays_only=True,
        )
        # 5 entries for Mon-Fri (Weekday 1-5)
        self.assertEqual(plist.count("<key>Weekday</key>"), 5)
        for wd in range(1, 6):
            self.assertIn(f"<integer>{wd}</integer>", plist)

    def test_daily_has_single_calendar_interval(self):
        plist = ws._macos_plist(
            "com.dpt.window-spread.0630",
            "claude -p hi",
            6,
            30,
            weekdays_only=False,
        )
        self.assertEqual(plist.count("<key>Weekday</key>"), 0)
        self.assertEqual(plist.count("<key>Hour</key>"), 1)

    def test_label_in_plist(self):
        plist = ws._macos_plist("com.dpt.window-spread.0630", "echo hi", 6, 30, False)
        self.assertIn("<string>com.dpt.window-spread.0630</string>", plist)

    def test_command_via_login_shell(self):
        plist = ws._macos_plist("com.dpt.window-spread.0630", "claude -p hi", 6, 30, False)
        self.assertIn("/bin/bash", plist)
        self.assertIn("-lc", plist)
        self.assertIn("claude -p hi", plist)


class LinuxCronTest(unittest.TestCase):
    def test_cron_line_weekdays(self):
        line = ws._cron_line("06:30", "claude -p hi", weekdays_only=True)
        self.assertIn("30 6 * * 1-5", line)
        self.assertIn("claude -p hi", line)
        self.assertIn("# com.dpt.window-spread.0630", line)

    def test_cron_line_daily(self):
        line = ws._cron_line("06:30", "claude -p hi", weekdays_only=False)
        self.assertIn("30 6 * * *", line)

    def test_cron_line_minute_padding(self):
        line = ws._cron_line("06:00", "echo", weekdays_only=False)
        # 0-padded minute is OK in cron, "0 6 * * *" is valid
        self.assertTrue(line.startswith("0 6 ") or line.startswith("00 6 "))

    def test_install_linux_preserves_existing_entries(self):
        # crontab has user's own jobs + a stale window-spread entry
        existing = [
            "# user's own job",
            "0 9 * * 1 /home/user/backup.sh",
            "30 10 * * * claude -p hi # com.dpt.window-spread.1030",  # stale
        ]
        read = ws.CrontabRead(ws.CRONTAB_OK, existing)
        with patch.object(ws, "_read_crontab", return_value=read):
            with patch.object(ws, "_write_crontab") as mock_write:
                mock_write.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
                ws._install_linux(["06:30"], "claude -p hi", weekdays_only=True, dry_run=False)
                final = mock_write.call_args[0][0]
                # user's job preserved
                self.assertIn("0 9 * * 1 /home/user/backup.sh", final)
                # stale window-spread removed
                self.assertFalse(any("1030" in l for l in final))
                # new entry added
                self.assertTrue(any("0630" in l for l in final))

    def test_uninstall_linux_only_removes_our_entries(self):
        existing = [
            "0 9 * * 1 /home/user/backup.sh",
            "30 6 * * 1-5 claude -p hi # com.dpt.window-spread.0630",
            "30 11 * * 1-5 claude -p hi # com.dpt.window-spread.1130",
        ]
        read = ws.CrontabRead(ws.CRONTAB_OK, existing)
        with patch.object(ws, "_read_crontab", return_value=read):
            with patch.object(ws, "_write_crontab") as mock_write:
                mock_write.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
                results = ws._uninstall_linux(dry_run=False)
                self.assertEqual(len(results), 2)
                final = mock_write.call_args[0][0]
                # user's job preserved
                self.assertEqual(final, ["0 9 * * 1 /home/user/backup.sh"])

    def test_dry_run_does_not_write(self):
        existing = ["30 6 * * 1-5 claude -p hi # com.dpt.window-spread.0630"]
        read = ws.CrontabRead(ws.CRONTAB_OK, existing)
        with patch.object(ws, "_read_crontab", return_value=read):
            with patch.object(ws, "_write_crontab") as mock_write:
                ws._uninstall_linux(dry_run=True)
                mock_write.assert_not_called()


class CrontabReadStateTest(unittest.TestCase):
    """_read_crontab must distinguish "no crontab" from "cannot read one" (#4)."""

    def _read_with(self, **proc):
        proc.setdefault("stdout", "")
        proc.setdefault("stderr", "")
        with patch.object(ws.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(**proc)
            return ws._read_crontab()

    def test_success_returns_lines(self):
        read = self._read_with(returncode=0, stdout="0 9 * * 1 backup.sh\n# note\n")
        self.assertEqual(read.state, ws.CRONTAB_OK)
        self.assertEqual(read.lines, ["0 9 * * 1 backup.sh", "# note"])
        self.assertTrue(read.usable)

    def test_no_crontab_for_user_is_empty(self):
        # vixie-cron / cronie / macOS all say this; observed on macOS.
        read = self._read_with(returncode=1, stderr="crontab: no crontab for jane")
        self.assertEqual(read.state, ws.CRONTAB_EMPTY)
        self.assertEqual(read.lines, [])
        self.assertTrue(read.usable)

    def test_enoent_on_the_spool_is_empty(self):
        # busybox reports a missing crontab as ENOENT on the spool path.
        # REASONED, not observed: this wording is from busybox's error format,
        # not from a run against a real busybox crontab.
        read = self._read_with(
            returncode=1,
            stderr="crontab: can't open '/var/spool/cron/crontabs/jane': No such file or directory",
        )
        self.assertEqual(read.state, ws.CRONTAB_EMPTY)

    def test_enoent_away_from_the_spool_is_unreadable(self):
        # A crontab that cannot even start says ENOENT about something else
        # entirely. The crontab it could not read may be full of the user's
        # jobs, so this must not read as "there is nothing there".
        read = self._read_with(
            returncode=1,
            stderr=(
                "crontab: error while loading shared libraries: libpam.so.0: "
                "cannot open shared object file: No such file or directory"
            ),
        )
        self.assertEqual(read.state, ws.CRONTAB_UNREADABLE)

    def test_permission_denied_is_unreadable(self):
        read = self._read_with(
            returncode=1,
            stderr="crontab: cannot read /var/spool/cron/crontabs/jane: Permission denied",
        )
        self.assertEqual(read.state, ws.CRONTAB_UNREADABLE)
        self.assertFalse(read.usable)

    def test_denial_wins_over_an_enoent_substring(self):
        # A message carrying both shapes must never be read as "empty".
        read = self._read_with(
            returncode=1,
            stderr="crontab: no such file or directory for spool; Permission denied",
        )
        self.assertEqual(read.state, ws.CRONTAB_UNREADABLE)

    def test_silent_failure_is_unreadable(self):
        # An unrecognised failure is not evidence that there is nothing there.
        read = self._read_with(returncode=1, stderr="")
        self.assertEqual(read.state, ws.CRONTAB_UNREADABLE)

    def test_missing_crontab_binary_is_unreadable(self):
        with patch.object(ws.subprocess, "run", side_effect=FileNotFoundError("crontab")):
            read = ws._read_crontab()
        self.assertEqual(read.state, ws.CRONTAB_UNREADABLE)
        self.assertIn("crontab", read.stderr)


class CrontabRefusesToOverwriteTest(unittest.TestCase):
    """An install/uninstall that cannot read the crontab must not write one (#4)."""

    def setUp(self):
        self.unreadable = ws.CrontabRead(
            ws.CRONTAB_UNREADABLE, [], "crontab: cannot read spool: Permission denied"
        )
        self.empty = ws.CrontabRead(ws.CRONTAB_EMPTY, [])
        self.populated = ws.CrontabRead(
            ws.CRONTAB_OK,
            [
                "0 9 * * 1 /home/jane/backup.sh",
                "30 6 * * * claude -p hi # com.dpt.window-spread.0630",
            ],
        )

    def _run(self, action, read, **kwargs):
        with patch.object(ws, "_read_crontab", return_value=read):
            with patch.object(ws, "_write_crontab") as mock_write:
                mock_write.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
                with patch("sys.stderr", StringIO()):
                    results = action(**kwargs)
        return results, mock_write

    def test_install_refuses_when_crontab_unreadable(self):
        results, mock_write = self._run(
            ws._install_linux,
            self.unreadable,
            pings=["06:30"],
            command="claude -p hi",
            weekdays_only=False,
            dry_run=False,
        )
        mock_write.assert_not_called()
        self.assertTrue(any(r.get("returncode", 0) != 0 for r in results))
        self.assertTrue(any("Permission denied" in str(r.get("stderr", "")) for r in results))

    def test_install_writes_when_crontab_is_genuinely_empty(self):
        # positive control for the refusal above
        results, mock_write = self._run(
            ws._install_linux,
            self.empty,
            pings=["06:30"],
            command="claude -p hi",
            weekdays_only=False,
            dry_run=False,
        )
        mock_write.assert_called_once()
        self.assertEqual(
            mock_write.call_args[0][0],
            ["30 6 * * * claude -p hi # com.dpt.window-spread.0630"],
        )
        self.assertTrue(all(r.get("returncode", 0) == 0 for r in results))

    def test_install_force_overrides_the_refusal(self):
        _, mock_write = self._run(
            ws._install_linux,
            self.unreadable,
            pings=["06:30"],
            command="claude -p hi",
            weekdays_only=False,
            dry_run=False,
            force=True,
        )
        mock_write.assert_called_once()

    def test_install_dry_run_never_writes_even_when_unreadable(self):
        results, mock_write = self._run(
            ws._install_linux,
            self.unreadable,
            pings=["06:30"],
            command="claude -p hi",
            weekdays_only=False,
            dry_run=True,
        )
        mock_write.assert_not_called()
        self.assertTrue(any(r.get("returncode", 0) != 0 for r in results))

    def test_uninstall_refuses_when_crontab_unreadable(self):
        results, mock_write = self._run(ws._uninstall_linux, self.unreadable, dry_run=False)
        mock_write.assert_not_called()
        self.assertTrue(any(r.get("returncode", 0) != 0 for r in results))

    def test_uninstall_writes_when_crontab_readable(self):
        # positive control for the refusal above
        results, mock_write = self._run(ws._uninstall_linux, self.populated, dry_run=False)
        mock_write.assert_called_once()
        self.assertEqual(mock_write.call_args[0][0], ["0 9 * * 1 /home/jane/backup.sh"])
        self.assertEqual(len(results), 1)

    def test_uninstall_force_reports_nothing_to_remove_rather_than_refusing(self):
        results, mock_write = self._run(
            ws._uninstall_linux, self.unreadable, dry_run=False, force=True
        )
        # Forced, so no refusal entry -- and the read produced no lines
        # carrying our marker, so there is nothing to remove and nothing is
        # written. The empty list is what distinguishes this from the refusal;
        # mock_write is silent on both paths.
        self.assertEqual(results, [])
        mock_write.assert_not_called()

    def test_uninstall_reports_a_failed_rewrite_as_not_removed(self):
        with patch.object(ws, "_read_crontab", return_value=self.populated):
            with patch.object(ws, "_write_crontab") as mock_write:
                mock_write.return_value = type(
                    "R", (), {"returncode": 1, "stderr": "crontab: errors in crontab file"}
                )()
                results = ws._uninstall_linux(dry_run=False)
        self.assertEqual(len(results), 1)
        self.assertFalse(results[0]["removed"])
        self.assertEqual(results[0]["returncode"], 1)

    def test_list_is_empty_but_says_so_on_stderr(self):
        err = StringIO()
        with patch.object(ws, "_read_crontab", return_value=self.unreadable):
            with patch("sys.stderr", err):
                self.assertEqual(ws._list_linux(), [])
        self.assertIn("could not", err.getvalue().lower())

    def test_list_returns_entries_when_readable(self):
        # positive control for the stderr warning above
        err = StringIO()
        with patch.object(ws, "_read_crontab", return_value=self.populated):
            with patch("sys.stderr", err):
                entries = ws._list_linux()
        self.assertEqual(len(entries), 1)
        self.assertEqual(err.getvalue(), "")


class CronCommandValidationTest(unittest.TestCase):
    """A command that would split the cron line is refused before any write (#5)."""

    def test_newline_in_command_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            ws._cron_line("06:30", "claude -p hi\n0 0 * * * /tmp/evil.sh", weekdays_only=False)
        self.assertIn("newline", str(ctx.exception))

    def test_carriage_return_in_command_is_refused(self):
        with self.assertRaises(ValueError):
            ws._cron_line("06:30", "claude -p hi\r0 0 * * * /tmp/evil.sh", weekdays_only=False)

    def test_bare_percent_in_command_is_refused(self):
        with self.assertRaises(ValueError) as ctx:
            ws._cron_line("06:30", 'claude -p "50% done"', weekdays_only=False)
        self.assertIn("%", str(ctx.exception))

    def test_escaped_percent_is_allowed(self):
        line = ws._cron_line("06:30", r'claude -p "50\% done"', weekdays_only=False)
        self.assertTrue(line.endswith("# com.dpt.window-spread.0630"))

    def test_ordinary_command_still_builds(self):
        # positive control for the three refusals above
        line = ws._cron_line("06:30", ws.DEFAULT_COMMAND, weekdays_only=False)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertTrue(line.endswith("# com.dpt.window-spread.0630"))

    def test_marker_labels_the_line_it_is_on(self):
        # the harm in #5 was a marker that ended up on a different line than
        # the ping it labelled; with the command validated it cannot.
        line = ws._cron_line("06:30", ws.DEFAULT_COMMAND, weekdays_only=False)
        self.assertEqual(len(line.splitlines()), 1)
        self.assertIn(ws.LABEL_PREFIX, line)

    def test_uninstall_removes_a_line_this_version_installs(self):
        # round trip: whatever _cron_line emits, _uninstall_linux must find.
        line = ws._cron_line("06:30", ws.DEFAULT_COMMAND, weekdays_only=True)
        read = ws.CrontabRead(ws.CRONTAB_OK, ["0 9 * * 1 backup.sh", line])
        with patch.object(ws, "_read_crontab", return_value=read):
            with patch.object(ws, "_write_crontab") as mock_write:
                mock_write.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
                results = ws._uninstall_linux(dry_run=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(mock_write.call_args[0][0], ["0 9 * * 1 backup.sh"])

    def test_uninstall_still_removes_a_pre_fix_marker_line(self):
        # the on-disk format did not change, so entries written by an older
        # version are still found by uninstall
        legacy = "30 6 * * 1-5 claude -p hi # com.dpt.window-spread.0630"
        read = ws.CrontabRead(ws.CRONTAB_OK, [legacy])
        with patch.object(ws, "_read_crontab", return_value=read):
            with patch.object(ws, "_write_crontab") as mock_write:
                mock_write.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
                results = ws._uninstall_linux(dry_run=False)
        self.assertEqual(len(results), 1)
        self.assertEqual(mock_write.call_args[0][0], [])

    def test_install_linux_refuses_a_newline_command_without_writing(self):
        read = ws.CrontabRead(ws.CRONTAB_OK, ["0 9 * * 1 backup.sh"])
        with patch.object(ws, "_read_crontab", return_value=read):
            with patch.object(ws, "_write_crontab") as mock_write:
                with self.assertRaises(ValueError):
                    ws._install_linux(
                        ["06:30"],
                        "claude -p hi\n0 0 * * * /tmp/evil.sh",
                        weekdays_only=False,
                        dry_run=False,
                    )
                mock_write.assert_not_called()

    def test_install_linux_writes_an_ordinary_command(self):
        # positive control for the refusal above, same fixture
        read = ws.CrontabRead(ws.CRONTAB_OK, ["0 9 * * 1 backup.sh"])
        with patch.object(ws, "_read_crontab", return_value=read):
            with patch.object(ws, "_write_crontab") as mock_write:
                mock_write.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
                ws._install_linux(["06:30"], "claude -p hi", weekdays_only=False, dry_run=False)
                mock_write.assert_called_once()

    def test_dry_run_also_refuses_a_newline_command(self):
        read = ws.CrontabRead(ws.CRONTAB_OK, [])
        with patch.object(ws, "_read_crontab", return_value=read):
            with self.assertRaises(ValueError):
                ws._install_linux(["06:30"], "a\nb", weekdays_only=False, dry_run=True)


class CmdInstallCommandGateTest(unittest.TestCase):
    """cmd_install rejects the command before the uninstall pass runs (#5)."""

    def _main(self, command):
        payload = json.dumps({"pings": ["06:30"]})
        err = StringIO()
        with patch.object(ws.platform, "system", return_value="Linux"):
            with patch("sys.stdin", StringIO(payload)):
                with patch.object(ws, "_dispatch") as mock_dispatch:
                    mock_dispatch.return_value = [{"returncode": 0}]
                    with patch("sys.stdout", StringIO()):
                        with patch("sys.stderr", err):
                            rc = ws.main(["install", "-", "--command", command])
        return rc, mock_dispatch, err.getvalue()

    def test_newline_command_never_reaches_the_scheduler(self):
        rc, mock_dispatch, err = self._main("claude -p hi\n0 0 * * * /tmp/evil.sh")
        self.assertNotEqual(rc, 0)
        mock_dispatch.assert_not_called()
        self.assertIn("newline", err)

    def test_ordinary_command_reaches_the_scheduler(self):
        # positive control: the gate above is not simply blocking everything
        rc, mock_dispatch, err = self._main("claude -p hi")
        self.assertEqual(rc, 0)
        self.assertEqual(mock_dispatch.call_count, 2)


class CmdUninstallExitCodeTest(unittest.TestCase):
    """A refused uninstall must not exit 0 (#4)."""

    def _main(self, results):
        with patch.object(ws.platform, "system", return_value="Linux"):
            with patch.object(ws, "_dispatch", return_value=results):
                with patch("sys.stdout", StringIO()):
                    return ws.main(["uninstall"])

    def test_refusal_exits_non_zero(self):
        self.assertNotEqual(
            self._main([{"error": "crontab-unreadable", "returncode": 1, "stderr": "denied"}]), 0
        )

    def test_successful_removal_exits_zero(self):
        # positive control for the refusal above
        self.assertEqual(self._main([{"line": "x", "removed": True, "returncode": 0}]), 0)

    def test_nothing_to_remove_exits_zero(self):
        self.assertEqual(self._main([]), 0)


class WindowsSchtasksTest(unittest.TestCase):
    def test_install_command_structure_weekdays(self):
        results = ws._install_windows(
            ["06:30"], "claude -p hi", weekdays_only=True, dry_run=True
        )
        self.assertEqual(len(results), 1)
        cmd = results[0]["cmd"]
        self.assertIn("schtasks", cmd[0])
        self.assertIn("/create", cmd)
        self.assertIn("/tn", cmd)
        self.assertIn("com.dpt.window-spread.0630", cmd)
        self.assertIn("/tr", cmd)
        self.assertIn("/sc", cmd)
        self.assertIn("WEEKLY", cmd)
        self.assertIn("/d", cmd)
        self.assertIn("MON,TUE,WED,THU,FRI", cmd)
        self.assertIn("/st", cmd)
        self.assertIn("06:30", cmd)
        self.assertIn("/f", cmd)

    def test_install_command_structure_daily(self):
        results = ws._install_windows(
            ["06:30"], "claude -p hi", weekdays_only=False, dry_run=True
        )
        cmd = results[0]["cmd"]
        self.assertIn("DAILY", cmd)
        self.assertNotIn("MON,TUE,WED,THU,FRI", cmd)

    def test_install_uses_cmd_c_wrapper(self):
        results = ws._install_windows(["06:30"], "claude -p hi", True, dry_run=True)
        cmd = results[0]["cmd"]
        idx = cmd.index("/tr")
        self.assertTrue(cmd[idx + 1].startswith("cmd /c "))

    def test_install_multiple_pings(self):
        results = ws._install_windows(
            ["06:30", "11:30", "16:30", "21:30"], "claude -p hi", True, dry_run=True
        )
        self.assertEqual(len(results), 4)
        labels = [r["label"] for r in results]
        self.assertEqual(
            labels,
            [
                "com.dpt.window-spread.0630",
                "com.dpt.window-spread.1130",
                "com.dpt.window-spread.1630",
                "com.dpt.window-spread.2130",
            ],
        )


class LabelTest(unittest.TestCase):
    def test_label_format(self):
        self.assertEqual(ws._label("06:30"), "com.dpt.window-spread.0630")
        self.assertEqual(ws._label("21:00"), "com.dpt.window-spread.2100")
        self.assertEqual(ws._label("00:00"), "com.dpt.window-spread.0000")


# ---------- glue / dispatcher / cli tests -----------------------------------


import tempfile
import os
from unittest.mock import MagicMock


class MacOSInstallEndToEndTest(unittest.TestCase):
    def test_install_writes_plist_and_calls_launchctl(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ws.Path, "home", return_value=Path(tmpdir)):
                with patch.object(ws.subprocess, "run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stderr="")
                    results = ws._install_macos(
                        ["06:30"], "claude -p hi", weekdays_only=True, dry_run=False
                    )
                # plist file actually created
                plist_path = Path(tmpdir) / "Library/LaunchAgents/com.dpt.window-spread.0630.plist"
                self.assertTrue(plist_path.exists())
                content = plist_path.read_text()
                self.assertIn("com.dpt.window-spread.0630", content)
                # launchctl called twice: unload (idempotent) + load
                self.assertEqual(mock_run.call_count, 2)
                self.assertEqual(mock_run.call_args_list[0][0][0][:2], ["launchctl", "unload"])
                self.assertEqual(mock_run.call_args_list[1][0][0][:3], ["launchctl", "load", "-w"])
                self.assertEqual(len(results), 1)
                self.assertEqual(results[0]["returncode"], 0)

    def test_install_dry_run_writes_nothing(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ws.Path, "home", return_value=Path(tmpdir)):
                with patch.object(ws.subprocess, "run") as mock_run:
                    ws._install_macos(["06:30"], "claude -p hi", True, dry_run=True)
                # no subprocess calls
                mock_run.assert_not_called()
                # no plist file
                plist_dir = Path(tmpdir) / "Library/LaunchAgents"
                self.assertFalse(plist_dir.exists() and any(plist_dir.iterdir()))


class MacOSUninstallTest(unittest.TestCase):
    def test_uninstall_removes_only_our_plists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            la = home / "Library/LaunchAgents"
            la.mkdir(parents=True)
            (la / "com.dpt.window-spread.0630.plist").write_text("ours")
            (la / "com.dpt.window-spread.1130.plist").write_text("ours")
            (la / "com.user.something-else.plist").write_text("user's, do not touch")
            with patch.object(ws.Path, "home", return_value=home):
                with patch.object(ws.subprocess, "run") as mock_run:
                    mock_run.return_value = MagicMock(returncode=0, stderr="")
                    results = ws._uninstall_macos(dry_run=False)
            self.assertEqual(len(results), 2)
            self.assertFalse((la / "com.dpt.window-spread.0630.plist").exists())
            self.assertFalse((la / "com.dpt.window-spread.1130.plist").exists())
            # user's untouched
            self.assertTrue((la / "com.user.something-else.plist").exists())

    def test_uninstall_no_dir_returns_empty(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            with patch.object(ws.Path, "home", return_value=Path(tmpdir)):
                self.assertEqual(ws._uninstall_macos(dry_run=False), [])


class MacOSListTest(unittest.TestCase):
    def test_list_returns_only_our_plists(self):
        with tempfile.TemporaryDirectory() as tmpdir:
            home = Path(tmpdir)
            la = home / "Library/LaunchAgents"
            la.mkdir(parents=True)
            (la / "com.dpt.window-spread.0630.plist").write_text("ours")
            (la / "com.user.thing.plist").write_text("user's")
            with patch.object(ws.Path, "home", return_value=home):
                results = ws._list_macos()
            self.assertEqual(len(results), 1)
            self.assertEqual(results[0]["label"], "com.dpt.window-spread.0630")


class WindowsUninstallTest(unittest.TestCase):
    def test_uninstall_parses_csv_output(self):
        csv_output = (
            '"\\com.dpt.window-spread.0630","6/5/2026 06:30:00","Ready"\n'
            '"\\com.user.something","6/5/2026 09:00:00","Ready"\n'
            '"\\com.dpt.window-spread.1130","6/5/2026 11:30:00","Ready"\n'
        )
        with patch.object(ws.subprocess, "run") as mock_run:
            # first call: query, then 2 deletes
            mock_run.side_effect = [
                MagicMock(returncode=0, stdout=csv_output, stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
                MagicMock(returncode=0, stdout="", stderr=""),
            ]
            results = ws._uninstall_windows(dry_run=False)
        self.assertEqual(len(results), 2)
        labels = [r["label"] for r in results]
        self.assertIn("com.dpt.window-spread.0630", labels)
        self.assertIn("com.dpt.window-spread.1130", labels)
        # com.user.something not touched
        self.assertNotIn("com.user.something", labels)

    def test_uninstall_dry_run_no_delete(self):
        csv_output = '"\\com.dpt.window-spread.0630","6/5/2026 06:30:00","Ready"\n'
        with patch.object(ws.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=csv_output, stderr="")
            results = ws._uninstall_windows(dry_run=True)
        # only 1 call (the query), not the delete
        self.assertEqual(mock_run.call_count, 1)
        self.assertEqual(len(results), 1)
        self.assertTrue(results[0]["dry_run"])


class WindowsListTest(unittest.TestCase):
    def test_list_filters_our_entries(self):
        csv_output = (
            '"\\com.dpt.window-spread.0630","6/5/2026 06:30:00","Ready"\n'
            '"\\com.user.foo","6/5/2026 09:00:00","Ready"\n'
        )
        with patch.object(ws.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=0, stdout=csv_output, stderr="")
            results = ws._list_windows()
        self.assertEqual(len(results), 1)
        self.assertEqual(results[0]["label"], "com.dpt.window-spread.0630")


class DispatcherTest(unittest.TestCase):
    def test_dispatch_macos(self):
        with patch.object(ws.platform, "system", return_value="Darwin"):
            with patch.object(ws, "_install_macos") as mock:
                mock.return_value = []
                ws._dispatch("install", ["06:30"], "cmd", True, False)
                mock.assert_called_once()

    def test_dispatch_linux(self):
        with patch.object(ws.platform, "system", return_value="Linux"):
            with patch.object(ws, "_install_linux") as mock:
                mock.return_value = []
                ws._dispatch("install", ["06:30"], "cmd", True, False)
                mock.assert_called_once()

    def test_dispatch_windows(self):
        with patch.object(ws.platform, "system", return_value="Windows"):
            with patch.object(ws, "_install_windows") as mock:
                mock.return_value = []
                ws._dispatch("install", ["06:30"], "cmd", True, False)
                mock.assert_called_once()

    def test_dispatch_unsupported_os(self):
        with patch.object(ws.platform, "system", return_value="Plan9"):
            with self.assertRaises(RuntimeError):
                ws._dispatch("install", [], "cmd", False, False)


class CmdInstallTest(unittest.TestCase):
    def test_cmd_install_reads_file(self):
        with tempfile.NamedTemporaryFile(mode="w", suffix=".json", delete=False) as f:
            json.dump({"spread": {"pings": ["06:30", "11:30"]}}, f)
            fname = f.name
        try:
            with patch.object(ws, "_dispatch") as mock_dispatch:
                mock_dispatch.return_value = [{"returncode": 0}, {"returncode": 0}]
                buf = StringIO()
                with patch("sys.stdout", buf):
                    ret = ws.main(["install", fname, "--weekdays", "--dry-run"])
                self.assertEqual(ret, 0)
                args = mock_dispatch.call_args[0]
                self.assertEqual(args[0], "install")
                self.assertEqual(args[1], ["06:30", "11:30"])
        finally:
            os.unlink(fname)

    def test_cmd_install_reads_stdin(self):
        payload = json.dumps({"spread": {"pings": ["06:30"]}})
        with patch("sys.stdin", StringIO(payload)):
            with patch.object(ws, "_dispatch") as mock_dispatch:
                mock_dispatch.return_value = [{"returncode": 0}]
                buf = StringIO()
                with patch("sys.stdout", buf):
                    ret = ws.main(["install", "-", "--dry-run"])
                self.assertEqual(ret, 0)
                self.assertEqual(mock_dispatch.call_args[0][1], ["06:30"])

    def test_cmd_install_calls_uninstall_first(self):
        # destructive-replace: install must remove existing entries before installing new
        payload = json.dumps({"spread": {"pings": ["06:30"]}})
        with patch("sys.stdin", StringIO(payload)):
            with patch.object(ws, "_dispatch") as mock_dispatch:
                mock_dispatch.return_value = [{"returncode": 0}]
                buf = StringIO()
                with patch("sys.stdout", buf):
                    ws.main(["install", "-", "--dry-run"])
                # 2 calls: uninstall (cleanup) then install
                self.assertEqual(mock_dispatch.call_count, 2)
                self.assertEqual(mock_dispatch.call_args_list[0][0][0], "uninstall")
                self.assertEqual(mock_dispatch.call_args_list[1][0][0], "install")

    def test_cmd_install_returns_nonzero_on_failure(self):
        payload = json.dumps({"spread": {"pings": ["06:30"]}})
        with patch("sys.stdin", StringIO(payload)):
            with patch.object(ws, "_dispatch") as mock_dispatch:
                mock_dispatch.return_value = [{"returncode": 1, "stderr": "boom"}]
                buf = StringIO()
                with patch("sys.stdout", buf):
                    ret = ws.main(["install", "-"])
                self.assertEqual(ret, 1)


class CmdUninstallTest(unittest.TestCase):
    def test_cmd_uninstall_calls_dispatch(self):
        with patch.object(ws, "_dispatch") as mock_dispatch:
            mock_dispatch.return_value = []
            buf = StringIO()
            with patch("sys.stdout", buf):
                ret = ws.main(["uninstall"])
            self.assertEqual(ret, 0)
            self.assertEqual(mock_dispatch.call_args[0][0], "uninstall")

    def test_cmd_uninstall_dry_run(self):
        with patch.object(ws, "_dispatch") as mock_dispatch:
            mock_dispatch.return_value = []
            buf = StringIO()
            with patch("sys.stdout", buf):
                ws.main(["uninstall", "--dry-run"])
            args = mock_dispatch.call_args[0]
            self.assertTrue(args[1])  # dry_run=True


class CmdListTest(unittest.TestCase):
    def test_cmd_list_emits_json(self):
        with patch.object(ws, "_dispatch") as mock_dispatch:
            mock_dispatch.return_value = [{"label": "com.dpt.window-spread.0630"}]
            buf = StringIO()
            with patch("sys.stdout", buf):
                ret = ws.main(["list"])
            self.assertEqual(ret, 0)
            out = json.loads(buf.getvalue())
            self.assertIn("entries", out)
            self.assertEqual(len(out["entries"]), 1)


class ErrorPathsTest(unittest.TestCase):
    def test_empty_pings_compute_raises(self):
        with self.assertRaises(ValueError):
            ws.parse_blocks("")

    def test_install_with_legacy_pings_format(self):
        # JSON without "spread" key, just top-level "pings"
        payload = json.dumps({"pings": ["06:30"]})
        with patch("sys.stdin", StringIO(payload)):
            with patch.object(ws, "_dispatch") as mock_dispatch:
                mock_dispatch.return_value = [{"returncode": 0}]
                buf = StringIO()
                with patch("sys.stdout", buf):
                    ws.main(["install", "-", "--dry-run"])
                self.assertEqual(mock_dispatch.call_args[0][1], ["06:30"])

    def test_read_crontab_no_existing(self):
        # "no crontab for <user>" is the recognised quiet case: empty, usable,
        # and safe to write over. An unrecognised failure is NOT this case --
        # see CrontabReadStateTest.test_silent_failure_is_unreadable.
        with patch.object(ws.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(
                returncode=1, stdout="", stderr="crontab: no crontab for jane"
            )
            read = ws._read_crontab()
            self.assertEqual(read.state, ws.CRONTAB_EMPTY)
            self.assertEqual(read.lines, [])
            self.assertTrue(read.usable)


# ---------- #6: the plist is a document, not a string ------------------------


class MacOSPlistEncodingTest(unittest.TestCase):
    """A plist must encode whatever the command contains, not interpolate it.

    Every case here asserts on the *parsed* document via plistlib, so a test
    that stopped generating a plist at all would fail on the load rather than
    pass on an absent substring.
    """

    KEYS = {
        "Label",
        "ProgramArguments",
        "StartCalendarInterval",
        "StandardOutPath",
        "StandardErrorPath",
    }

    def _load(self, command="claude -p hi", label="com.dpt.window-spread.0630"):
        return plistlib.loads(
            ws._macos_plist(label, command, 6, 30, False).encode("utf-8")
        )

    def test_ordinary_command_round_trips(self):
        # positive control: the fixture does produce a loadable plist with the
        # exact command in it, so a failure below is about the command's
        # content and not about the harness.
        doc = self._load("claude -p hi")
        self.assertEqual(set(doc.keys()), self.KEYS)
        self.assertEqual(doc["ProgramArguments"], ["/bin/bash", "-lc", "claude -p hi"])

    def test_ampersand_command_round_trips(self):
        cmd = "claude -p 'a & b'"
        doc = self._load(cmd)
        self.assertEqual(doc["ProgramArguments"][2], cmd)

    def test_angle_bracket_redirect_round_trips(self):
        cmd = "claude -p hi > /tmp/out 2>&1 < /dev/null"
        self.assertEqual(self._load(cmd)["ProgramArguments"][2], cmd)

    def test_markup_in_command_injects_no_keys(self):
        # The full injection: it closes <string> *and* the ProgramArguments
        # <array>, so it lands in the top-level dict where launchd reads it.
        inj = (
            "claude</string></array><key>RunAtLoad</key><true/>"
            "<key>ProgramArguments</key><array><string>evil"
        )
        doc = self._load(inj)
        # positive control, same fixture: a document was generated and the
        # command reached it verbatim. Without this line the assertions below
        # would also pass on an empty dict.
        self.assertEqual(doc["ProgramArguments"], ["/bin/bash", "-lc", inj])
        self.assertEqual(set(doc.keys()), self.KEYS)
        self.assertNotIn("RunAtLoad", doc)

    def test_markup_in_label_injects_no_keys(self):
        inj = "L</string><key>RunAtLoad</key><true/><string>"
        doc = self._load(label=inj)
        self.assertEqual(doc["Label"], inj)  # positive control
        self.assertEqual(set(doc.keys()), self.KEYS)
        self.assertNotIn("RunAtLoad", doc)

    def test_newline_and_percent_are_fine_on_launchd(self):
        # Why cron's _validate_cron_command is NOT hoisted to every platform:
        # both characters launchd is indifferent to round-trip exactly. A
        # shared validator would refuse input that works here.
        for cmd in ("a\nb", "pct 100% done", "a\tb"):
            with self.subTest(cmd=cmd):
                self.assertEqual(self._load(cmd)["ProgramArguments"][2], cmd)

    def test_control_character_is_refused_by_name(self):
        # macOS is not "no command rule": an XML plist cannot carry a C0
        # control character other than tab/LF/CR, and a lone surrogate does
        # not encode. Without an explicit check these surface as a raw
        # plistlib ValueError / UnicodeEncodeError from inside the installer,
        # which on the install path is *after* the destructive uninstall pass.
        for cmd in ("a\x00b", "a\x01b", "a\x1bb", "a\x1fb"):
            with self.subTest(cmd=cmd):
                with self.assertRaises(ValueError) as ctx:
                    ws._validate_plist_command(cmd)
                self.assertIn("control character", str(ctx.exception))

    def test_carriage_return_is_refused_because_it_does_not_survive(self):
        # A CR is legal in an XML document and plistlib will happily write
        # one, but every XML parser normalises it to LF on the way back --
        # so launchd would run a command the user did not type. Silent
        # corruption is worse than a refusal.
        with self.assertRaises(ValueError):
            ws._validate_plist_command("a\rb")

    def test_carriage_return_really_does_not_round_trip(self):
        # the measurement the refusal above is built on, pinned so that a
        # future plistlib change is visible rather than assumed.
        doc = plistlib.loads(plistlib.dumps({"k": "a\rb"}))
        self.assertEqual(doc["k"], "a\nb")

    def test_undecodable_argv_byte_is_refused(self):
        # POSIX argv that is not valid UTF-8 reaches us as a lone surrogate.
        with self.assertRaises(ValueError):
            ws._validate_plist_command("claude \udcff")

    def test_plist_command_validator_accepts_ordinary_commands(self):
        # positive control for the two refusals above.
        for cmd in ("claude -p hi", "claude -p 'a && b' > /tmp/o", "100% \t\n", "é"):
            with self.subTest(cmd=cmd):
                ws._validate_plist_command(cmd)

    def test_macos_plist_refuses_control_character_at_point_of_use(self):
        # our message, not plistlib's: the point is that the refusal is a
        # stated rule of this tool and not a serialiser accident.
        with self.assertRaises(ValueError) as ctx:
            ws._macos_plist("L", "a\x01b", 6, 30, False)
        self.assertIn("control character", str(ctx.exception))

    def test_calendar_interval_daily(self):
        doc = plistlib.loads(ws._macos_plist("L", "c", 6, 30, False).encode("utf-8"))
        self.assertEqual(doc["StartCalendarInterval"], {"Hour": 6, "Minute": 30})

    def test_calendar_interval_weekdays(self):
        doc = plistlib.loads(ws._macos_plist("L", "c", 6, 30, True).encode("utf-8"))
        self.assertEqual(
            doc["StartCalendarInterval"],
            [{"Hour": 6, "Minute": 30, "Weekday": wd} for wd in (1, 2, 3, 4, 5)],
        )


class MacOSInstallOrderingTest(unittest.TestCase):
    """A failed load must not cost the user the job they already had (#6)."""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.home = Path(self.tmp.name)
        (self.home / "Library/LaunchAgents").mkdir(parents=True)
        self.plist_path = (
            self.home / "Library/LaunchAgents/com.dpt.window-spread.0630.plist"
        )
        self.previous = b"<?xml version='1.0'?><plist version='1.0'><dict/></plist>\n"
        self.plist_path.write_bytes(self.previous)
        patcher = patch.object(ws.Path, "home", return_value=self.home)
        patcher.start()
        self.addCleanup(patcher.stop)

    def _run(self, load_returncode):
        calls = []

        def fake_run(cmd, **kwargs):
            record = list(cmd)
            rc = 0
            if cmd[1] == "load":
                # snapshot what is on disk at the moment load is attempted
                record.append(("disk", self.plist_path.read_bytes()))
                rc = load_returncode
            calls.append(record)
            return MagicMock(returncode=rc, stdout="", stderr="boom" if rc else "")

        with patch.object(ws.subprocess, "run", side_effect=fake_run):
            results = ws._install_macos(
                ["06:30"], "claude -p hi", weekdays_only=False, dry_run=False
            )
        return results, calls

    def test_failed_load_restores_previous_plist(self):
        results, calls = self._run(load_returncode=1)
        self.assertEqual(results[0]["returncode"], 1)
        self.assertEqual(
            self.plist_path.read_bytes(),
            self.previous,
            "a load failure left the user's previous plist overwritten",
        )
        # the restored job must be handed back to launchd, not just to disk
        loads = [c for c in calls if c[1] == "load"]
        self.assertEqual(len(loads), 2, "the previous job was never reloaded")

    def test_successful_load_keeps_new_plist(self):
        # positive control for the assertion above: on success the new
        # document is on disk, so "previous content survived" is a real
        # statement about the failure path and not about a no-op installer.
        results, calls = self._run(load_returncode=0)
        self.assertEqual(results[0]["returncode"], 0)
        doc = plistlib.loads(self.plist_path.read_bytes())
        self.assertEqual(doc["ProgramArguments"][2], "claude -p hi")
        self.assertEqual(len([c for c in calls if c[1] == "load"]), 1)

    def test_new_plist_is_on_disk_when_load_is_attempted(self):
        _, calls = self._run(load_returncode=0)
        snapshot = [c[-1][1] for c in calls if c[1] == "load"][0]
        self.assertEqual(plistlib.loads(snapshot)["ProgramArguments"][2], "claude -p hi")


# ---------- #7: ping values are validated on every platform ------------------


class ValidatePingTest(unittest.TestCase):
    def test_accepts_ordinary_ping(self):
        for good in ("06:30", "00:00", "23:59"):
            with self.subTest(good=good):
                ws._validate_ping(good)  # positive control: must not raise

    def test_rejects_non_time(self):
        for bad in ("not-a-time", "", "0630", "6:30", "06:3", "06:30:00", "aa:bb"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ws._validate_ping(bad)

    def test_rejects_out_of_range(self):
        for bad in ("24:00", "99:00", "06:60"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ws._validate_ping(bad)

    def test_rejects_path_and_flag_shapes(self):
        for bad in ("../../etc/passwd", "/f", "06:30 /f"):
            with self.subTest(bad=bad):
                with self.assertRaises(ValueError):
                    ws._validate_ping(bad)


class WindowsPingValidationTest(unittest.TestCase):
    def test_install_windows_refuses_unvalidated_ping(self):
        with self.assertRaises(ValueError):
            ws._install_windows(["not-a-time"], "x", weekdays_only=False, dry_run=True)

    def test_install_windows_accepts_a_real_ping(self):
        # positive control: the refusal above is about the ping, not about
        # _install_windows having stopped working.
        cmd = ws._install_windows(["06:30"], "x", weekdays_only=False, dry_run=True)[0][
            "cmd"
        ]
        self.assertEqual(cmd[cmd.index("/st") + 1], "06:30")


class CmdInstallValidationTest(unittest.TestCase):
    """Validation happens in cmd_install, before the destructive uninstall."""

    def _install(self, pings, system, command="claude -p hi"):
        payload = json.dumps({"spread": {"pings": pings}})
        with patch.object(ws.platform, "system", return_value=system):
            with patch("sys.stdin", StringIO(payload)):
                with patch.object(ws, "_dispatch") as mock_dispatch:
                    mock_dispatch.return_value = [{"returncode": 0}] * len(pings)
                    out, err = StringIO(), StringIO()
                    with patch("sys.stdout", out), patch("sys.stderr", err):
                        rc = ws.main(["install", "-", "--dry-run", "--command", command])
                    return rc, mock_dispatch, err.getvalue()

    def test_bad_ping_refused_before_dispatch_on_every_platform(self):
        for system in ("Darwin", "Linux", "Windows"):
            with self.subTest(system=system):
                rc, dispatch, err = self._install(["not-a-time"], system)
                self.assertEqual(rc, 2)
                # the point of the issue: nothing was uninstalled on the way
                # to the refusal.
                dispatch.assert_not_called()
                self.assertIn("not-a-time", err)

    def test_good_ping_dispatches_on_every_platform(self):
        # positive control for the whole class: without it, "dispatch was not
        # called" would also pass if main() had stopped dispatching at all.
        for system in ("Darwin", "Linux", "Windows"):
            with self.subTest(system=system):
                rc, dispatch, _ = self._install(["06:30"], system)
                self.assertEqual(rc, 0)
                self.assertEqual(dispatch.call_count, 2)  # uninstall + install

    def test_newline_command_refused_on_linux_only(self):
        # The command rule is cron's, and stays cron's: the same command is
        # legal on launchd (see MacOSPlistEncodingTest) and is not adjudicated
        # for schtasks. Linux is the positive control for the macOS case.
        rc, dispatch, err = self._install(["06:30"], "Linux", command="a\nb")
        self.assertEqual(rc, 2)
        dispatch.assert_not_called()
        self.assertIn("newline", err)

        rc, dispatch, _ = self._install(["06:30"], "Darwin", command="a\nb")
        self.assertEqual(rc, 0)
        self.assertEqual(dispatch.call_count, 2)

    def test_control_character_command_refused_on_macos_before_dispatch(self):
        # The macOS rule is plist's, not cron's, and it must be applied above
        # the destructive uninstall pass for the same reason cron's is: a
        # plistlib ValueError raised inside _install_macos would land *after*
        # every existing ping had been removed.
        rc, dispatch, err = self._install(["06:30"], "Darwin", command="claude\x01hi")
        self.assertEqual(rc, 2)
        dispatch.assert_not_called()
        self.assertIn("control character", err)

    def test_ordinary_command_still_dispatches_on_macos(self):
        # positive control for the refusal above.
        rc, dispatch, _ = self._install(["06:30"], "Darwin", command="claude -p hi")
        self.assertEqual(rc, 0)
        self.assertEqual(dispatch.call_count, 2)

    def test_control_character_not_refused_on_linux(self):
        # The mirror of the newline case: \x01 is illegal in a plist and
        # legal on a crontab line, so the plist rule is not shared either.
        # Both directions of "valid is not one thing" are pinned.
        rc, dispatch, _ = self._install(["06:30"], "Linux", command="claude\x01hi")
        self.assertEqual(rc, 0, "Linux does not impose the plist rule")
        self.assertEqual(dispatch.call_count, 2)


if __name__ == "__main__":
    unittest.main(verbosity=2)
