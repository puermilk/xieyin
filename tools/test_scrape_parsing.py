#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""刮削脚本的解析行为测试（不联网）。

用法：
    python3 tools/test_scrape_parsing.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "src", "xieyin", "app", "xieyin", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import jav_scrape as j  # noqa: E402
import tmdb_scrape as t  # noqa: E402


class CodeOfTest(unittest.TestCase):
    def test_basic(self):
        self.assertEqual(j.code_of("ABC-123-UC"), "ABC-123")

    def test_spaced(self):
        self.assertEqual(j.code_of("PRED- 566- UC"), "PRED-566")

    def test_suffix(self):
        self.assertEqual(j.code_of("VENX-282_FHD_CH"), "VENX-282")

    def test_digit_only(self):
        self.assertEqual(j.code_of("071518-714"), "071518-714")

    def test_no_code(self):
        self.assertEqual(j.code_of("演员名目录"), "")


class ParseFolderTest(unittest.TestCase):
    def test_bracket_zh(self):
        zh, en, year = t.parse_folder("[中文名].Example.Movie.2024.2160p.WEB-DL")
        self.assertEqual((zh, en, year), ("中文名", "Example Movie", "2024"))

    def test_zh_dot_en(self):
        zh, en, year = t.parse_folder("蜘蛛侠：崭新之日.Spider-Man.Brand.New.Day.2026.2160p.WEB-DL")
        self.assertEqual((zh, en, year), ("蜘蛛侠：崭新之日", "Spider-Man Brand New Day", "2026"))

    def test_zh_year_only(self):
        zh, en, year = t.parse_folder("中文名.2023.1080p.BluRay")
        self.assertEqual(year, "2023")
        self.assertEqual(zh, "中文名")

    def test_en_year(self):
        zh, en, year = t.parse_folder("Example.Movie.2025.1080p.WEBRip")
        self.assertEqual((zh, en, year), ("", "Example Movie", "2025"))

    def test_paren_year(self):
        zh, en, year = t.parse_folder("Movie (2024)")
        self.assertEqual((zh, en, year), ("", "Movie", "2024"))

    def test_no_code_name(self):
        zh, en, year = t.parse_folder("071518-714")
        self.assertEqual(j.code_of("071518-714"), "071518-714")
        self.assertEqual(year, "")


if __name__ == "__main__":
    unittest.main(verbosity=2)
