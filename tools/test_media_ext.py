#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""媒体扩展名判定的纯函数行为测试（含 STRM）。

用法：
    python3 tools/test_media_ext.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "src", "xieyin", "app", "xieyin", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import media_ext as m  # noqa: E402


class VideoTest(unittest.TestCase):
    def test_real_video(self):
        for n in ("a.mp4", "a.MKV", "a.avi", "a.ts"):
            self.assertTrue(m.is_video(n), n)

    def test_strm_is_video_for_scraping(self):
        self.assertTrue(m.is_video("a.strm"))
        self.assertTrue(m.is_video("ABC-123-UC.STRM"))

    def test_not_video(self):
        for n in ("a.nfo", "a.jpg", "a.torrent", "a.txt", "a"):
            self.assertFalse(m.is_video(n), n)


class StrmTest(unittest.TestCase):
    def test_is_strm(self):
        self.assertTrue(m.is_strm("a.strm"))
        self.assertTrue(m.is_strm("x/y/z.STRM"))

    def test_not_strm(self):
        self.assertFalse(m.is_strm("a.mkv"))

    def test_strm_not_probeable(self):
        self.assertFalse(m.is_probeable("a.strm"))
        self.assertFalse(m.is_probeable("/m/a.strm"))

    def test_real_video_probeable(self):
        self.assertTrue(m.is_probeable("a.mkv"))


class ProtectedTest(unittest.TestCase):
    def test_strm_protected_from_cleanup(self):
        self.assertTrue(m.is_protected("a.strm"))

    def test_nfo_image_protected(self):
        self.assertTrue(m.is_protected("a.nfo"))
        self.assertTrue(m.is_protected("poster.jpg"))

    def test_real_video_not_protected(self):
        self.assertFalse(m.is_protected("a.mkv"))


class EndToEndTest(unittest.TestCase):
    """auto_watch 的找目录逻辑也要认 .strm。"""

    def test_strm_folder_needs_scrape(self):
        import auto_watch as a
        f = {"path": "/m/a", "names": ["a.strm"], "mtimes": [1.0]}
        self.assertEqual(a.pick_targets([f], 1e9, 0, True), ["/m/a"])

    def test_strm_folder_with_nfo_skipped(self):
        import auto_watch as a
        f = {"path": "/m/a", "names": ["a.strm", "a.nfo"]}
        f["mtimes"] = [1.0, 1.0]
        self.assertEqual(a.pick_targets([f], 1e9, 0, True), [])


if __name__ == "__main__":
    unittest.main(verbosity=2)
