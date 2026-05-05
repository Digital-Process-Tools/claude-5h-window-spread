"""Unit tests for window-spread script. Stdlib unittest, no pytest dep."""

from __future__ import annotations

import importlib.util
import json
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
        with patch.object(ws, "_read_crontab", return_value=existing):
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
        with patch.object(ws, "_read_crontab", return_value=existing):
            with patch.object(ws, "_write_crontab") as mock_write:
                mock_write.return_value = type("R", (), {"returncode": 0, "stderr": ""})()
                results = ws._uninstall_linux(dry_run=False)
                self.assertEqual(len(results), 2)
                final = mock_write.call_args[0][0]
                # user's job preserved
                self.assertEqual(final, ["0 9 * * 1 /home/user/backup.sh"])

    def test_dry_run_does_not_write(self):
        existing = ["30 6 * * 1-5 claude -p hi # com.dpt.window-spread.0630"]
        with patch.object(ws, "_read_crontab", return_value=existing):
            with patch.object(ws, "_write_crontab") as mock_write:
                ws._uninstall_linux(dry_run=True)
                mock_write.assert_not_called()


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
        with patch.object(ws.subprocess, "run") as mock_run:
            mock_run.return_value = MagicMock(returncode=1, stdout="", stderr="no crontab")
            self.assertEqual(ws._read_crontab(), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
