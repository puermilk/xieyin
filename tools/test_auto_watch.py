#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""自动刮削（定时扫描 / qBittorrent 联动）的纯函数行为测试，不联网、不做 IO。

用法：
    python3 tools/test_auto_watch.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "src", "xieyin", "app", "xieyin", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import auto_watch as a  # noqa: E402

NOW = 1_800_000_000.0


def folder(path, names, mtime=NOW - 3600):
    return {"path": path, "names": names, "mtimes": [mtime] * len(names)}


class PTDetectTest(unittest.TestCase):
    def test_release_group(self):
        self.assertTrue(a.is_pt_name("Movie.2024.1080p@GROUP.mkv"))

    def test_release_group_dir(self):
        self.assertTrue(a.is_pt_name("Movie.2024.1080p@GROUP"))

    def test_group_with_suffix(self):
        self.assertTrue(a.is_pt_name("Movie.2024@GROUP-10bit.mkv"))

    def test_domain_prefix_not_pt(self):
        self.assertFalse(a.is_pt_name("489155.com@ABC-123.mp4"))

    def test_plain_not_pt(self):
        self.assertFalse(a.is_pt_name("ABC-123.mkv"))

    def test_folder_torrent(self):
        self.assertTrue(a.is_pt_folder("x", ["a.torrent", "b.mkv"]))


class StabilityTest(unittest.TestCase):
    def test_old_file_is_stable(self):
        self.assertTrue(a.is_stable(NOW - 3600, NOW, 10))

    def test_fresh_file_not_stable(self):
        self.assertFalse(a.is_stable(NOW - 60, NOW, 10))

    def test_zero_settle(self):
        self.assertTrue(a.is_stable(NOW, NOW, 0))


class PickTargetsTest(unittest.TestCase):
    def test_new_folder_needs_scrape(self):
        got = a.pick_targets([folder("/m/a", ["a.mkv", "b.jpg"])], NOW, 10, True)
        self.assertEqual(got, ["/m/a"])

    def test_folder_with_nfo_skipped(self):
        got = a.pick_targets([folder("/m/a", ["a.mkv", "a.nfo"])], NOW, 10, True)
        self.assertEqual(got, [])

    def test_no_video_skipped(self):
        got = a.pick_targets([folder("/m/a", ["a.nfo", "b.jpg"])], NOW, 10, True)
        self.assertEqual(got, [])

    def test_unstable_skipped(self):
        f = folder("/m/a", ["a.mkv"])
        f["mtimes"] = [NOW - 30]
        self.assertEqual(a.pick_targets([f], NOW, 10, True), [])

    def test_pt_skipped_when_guard_on(self):
        f = folder("/m/a", ["Movie.2024@GROUP.mkv"])
        self.assertEqual(a.pick_targets([f], NOW, 10, True), [])

    def test_pt_allowed_when_guard_off(self):
        f = folder("/m/a", ["Movie.2024@GROUP.mkv"])
        self.assertEqual(a.pick_targets([f], NOW, 10, False), ["/m/a"])

    def test_recycle_skipped(self):
        f = folder("/m/#recycle/x", ["a.mkv"])
        self.assertEqual(a.pick_targets([f], NOW, 10, False), [])

    def test_mixed_batch(self):
        fs = [
            folder("/m/ok", ["a.mkv"]),
            folder("/m/hasnfo", ["a.mkv", "a.nfo"]),
            folder("/m/pt", ["x@G.mkv"]),
        ]
        self.assertEqual(a.pick_targets(fs, NOW, 10, True), ["/m/ok"])


class QbitTest(unittest.TestCase):
    def t(self, h, left=0, completion=100, path="/d/a"):
        return {"hash": h, "name": "a", "content_path": path, "save_path": "/d",
                "amount_left": left, "completion_on": completion, "state": "pausedUP"}

    def test_new_completed_selected(self):
        got = a.select_new_completed([self.t("h1")], {})
        self.assertEqual(len(got), 1)
        self.assertEqual(got[0]["hash"], "h1")

    def test_incomplete_skipped(self):
        self.assertEqual(a.select_new_completed([self.t("h1", left=5)], {}), [])

    def test_seen_skipped(self):
        self.assertEqual(a.select_new_completed([self.t("h1")], {"h1": 100}), [])

    def test_recheck_reprocessed(self):
        got = a.select_new_completed([self.t("h1", completion=200)], {"h1": 100})
        self.assertEqual(len(got), 1)

    def test_state_update(self):
        seen = {"h0": 100}
        got = a.select_new_completed([self.t("h1"), self.t("h0")], seen)
        self.assertEqual([g["hash"] for g in got], ["h1"])
        a.merge_state(seen, got)
        self.assertEqual(seen["h1"], 100)


class WatcherScanTest(unittest.TestCase):
    """端到端：_run_scan 从顶层 media_root 找目录并触发刮削。"""

    def _run(self):
        import shutil
        import tempfile
        tmp = tempfile.mkdtemp(prefix="awtest-")
        try:
            os.makedirs(os.path.join(tmp, "ABC-123-UC"))
            with open(os.path.join(tmp, "ABC-123-UC", "ABC-123-UC.mkv"), "w") as f:
                f.write("x")
            full = {"media_root": tmp,
                    "auto": {"enabled": True, "scan_enabled": True,
                             "settle_minutes": 0, "skip_pt": True}}
            calls = []
            w = a.AutoWatcher(conf_get=lambda: full,
                              state_path=os.path.join(tmp, "state.json"),
                              trigger=lambda k, t, o: calls.append((k, t, o)))
            w._run_scan(full)
            return calls, tmp
        finally:
            shutil.rmtree(tmp, ignore_errors=True)

    def test_finds_new_folder(self):
        calls, tmp = self._run()
        self.assertEqual(len(calls), 1)
        kind, title, opts = calls[0]
        self.assertEqual(kind, "jav")
        self.assertEqual(opts["dirs"], [os.path.join(tmp, "ABC-123-UC")])

    def test_respects_top_level_media_root(self):
        calls, tmp = self._run()
        self.assertTrue(calls[0][2]["dirs"][0].startswith(tmp))


if __name__ == "__main__":
    unittest.main(verbosity=2)
