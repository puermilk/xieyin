#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""build_fpk.py 的纯函数行为测试（不打包、不联网）。

用法：
    python3 tools/test_build_fpk.py
"""
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import build_fpk as b  # noqa: E402


SAMPLE = (
    "appname               = xieyin\n"
    "version               = 1.0.0\n"
    "platform              = x86\n"
    "checksum              = deadbeef\n"
)


class ManifestTest(unittest.TestCase):
    def test_platform_substituted(self):
        out = b.render_manifest(SAMPLE, version="1.1.0", platform="arm", checksum="cafe")
        self.assertIn("platform              = arm", out)

    def test_version_substituted(self):
        out = b.render_manifest(SAMPLE, version="1.1.0", platform="x86", checksum="cafe")
        self.assertIn("version               = 1.1.0", out)

    def test_checksum_single_line(self):
        out = b.render_manifest(SAMPLE, version="1.1.0", platform="x86", checksum="cafe")
        self.assertEqual(out.count("checksum"), 1)
        self.assertIn("checksum              = cafe", out)

    def test_rejects_unknown_platform(self):
        with self.assertRaises(ValueError):
            b.render_manifest(SAMPLE, version="1.1.0", platform="riscv", checksum="x")


class OutputNameTest(unittest.TestCase):
    def test_x86_name(self):
        self.assertEqual(b.output_name("1.1.0", "x86"), "xieyin-1.1.0-x86.fpk")

    def test_arm_name(self):
        self.assertEqual(b.output_name("1.1.0", "arm"), "xieyin-1.1.0-arm.fpk")

    def test_sha_name(self):
        self.assertEqual(b.output_name("1.1.0", "arm", ext=".fpk.sha256"),
                         "xieyin-1.1.0-arm.fpk.sha256")


class PlatformTest(unittest.TestCase):
    def test_supported_platforms(self):
        self.assertEqual(b.PLATFORMS, ("x86", "arm"))

    def test_validate(self):
        self.assertEqual(b.normalize_platform("ARM"), "arm")
        with self.assertRaises(ValueError):
            b.normalize_platform("mips")


if __name__ == "__main__":
    unittest.main(verbosity=2)
