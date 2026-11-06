#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""jav_scrape IP 验证行为测试（不联网）。

用法：
    python3 tools/test_ip_validation.py
"""
import contextlib
import io
import os
import sys
import unittest
from unittest.mock import patch

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "src", "xieyin", "app", "xieyin", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import jav_scrape as j  # noqa: E402


def page(marker=True):
    return (200, b"<html><title>JavBus</title></html>" if marker else b"<html>ok</html>")


class ProbeOkTest(unittest.TestCase):
    def test_accepts_200_with_marker(self):
        with patch.object(j.Session, "probe", return_value=(200, b"<title>JavBus</title>")):
            self.assertTrue(j.Session.probe_ok("1.2.3.4"))

    def test_rejects_200_without_marker(self):
        with patch.object(j.Session, "probe", return_value=(200, b"<html>ok</html>")):
            self.assertFalse(j.Session.probe_ok("1.2.3.4"))

    def test_rejects_redirect(self):
        with patch.object(j.Session, "probe", return_value=(302, b"  ")):
            self.assertFalse(j.Session.probe_ok("1.2.3.4"))

    def test_rejects_network_error(self):
        with patch.object(j.Session, "probe", return_value=(0, b"")):
            self.assertFalse(j.Session.probe_ok("1.2.3.4"))


class InitTest(unittest.TestCase):
    def test_only_valid_ips_are_kept(self):
        answers = {
            "1.1.1.1": (200, b"javbus page"),
            "2.2.2.2": (200, b"not the site"),
            "3.3.3.3": (302, b"  "),
        }
        with patch.object(j, "doh_ips", return_value=list(answers)), \
             patch.object(j, "KNOWN_IPS", []), \
             patch.object(j.Session, "probe", side_effect=lambda ip, timeout=12: answers[ip]):
            s = j.Session()
        self.assertEqual(s.ips, ["1.1.1.1"])
        self.assertTrue(s.validated)

    def test_warns_when_all_ips_fail(self):
        err = io.StringIO()
        with patch.object(j, "doh_ips", return_value=["1.1.1.1"]), \
             patch.object(j, "KNOWN_IPS", ["9.9.9.9"]), \
             patch.object(j.Session, "probe", return_value=(0, b"")), \
             contextlib.redirect_stderr(err):
            s = j.Session()
        self.assertFalse(s.validated)
        self.assertEqual(s.ips, ["9.9.9.9"])
        self.assertIn("硬闯", err.getvalue())


class RotateTest(unittest.TestCase):
    def test_rotate_only_adds_validated_ips(self):
        s = j.Session.__new__(j.Session)
        s.ips = ["1.1.1.1"]
        s.ipidx = 0
        s.cookies = {}
        answers = {
            "2.2.2.2": (200, b"not the site"),
            "3.3.3.3": (200, b"javbus"),
        }
        with patch.object(j, "doh_ips", return_value=list(answers)), \
             patch.object(j.Session, "probe", side_effect=lambda ip, timeout=12: answers[ip]):
            s.rotate()
        self.assertNotIn("2.2.2.2", s.ips)
        self.assertIn("3.3.3.3", s.ips)


if __name__ == "__main__":
    unittest.main(verbosity=2)
