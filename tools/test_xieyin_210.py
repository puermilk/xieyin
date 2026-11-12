#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
"""2.1.0 新行为测试：不生成备份、先清理、演员头像兜底、自动任务开关。"""
from __future__ import annotations

import io
import json
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.abspath(os.path.join(HERE, "..", "src", "xieyin", "app", "xieyin"))
SCRIPTS = os.path.join(APP, "scripts")
sys.path.insert(0, APP)
sys.path.insert(0, SCRIPTS)
sys.path.insert(0, HERE)

import actor_names as an  # noqa: E402
import actor_photo_sources as aps  # noqa: E402
import auto_watch as aw  # noqa: E402
import retranslate_nfo as rt  # noqa: E402
import translate_zh as t  # noqa: E402


def _jpeg(w: int, h: int, color=(120, 90, 70)) -> bytes:
    from PIL import Image
    out = io.BytesIO()
    Image.new("RGB", (w, h), color).save(out, "JPEG", quality=90)
    return out.getvalue()


class ActorNameTest(unittest.TestCase):
    def test_chinese_to_japanese_reverse(self):
        pairs = an.load_map()
        if not pairs:
            self.skipTest("actor map is empty")
        ja, cn = next(iter(pairs.items()))
        self.assertEqual(an.to_japanese(cn), ja)

    def test_placeholder(self):
        self.assertTrue(aps.is_placeholder("https://pics.dmm.co.jp/mono/actjpgs/nowprinting.gif"))
        self.assertFalse(aps.is_placeholder("https://www.av-league.com/image/act/1/1-0.jpg"))


class CoverAvatarTest(unittest.TestCase):
    def test_horizontal_composite_takes_right_half(self):
        out = aps.avatar_from_cover(_jpeg(800, 538, (10, 20, 30)))
        self.assertTrue(out.startswith(b"\xff\xd8\xff"))
        from PIL import Image
        im = Image.open(io.BytesIO(out))
        self.assertLess(im.width, im.height)
        self.assertGreaterEqual(im.width, 300)

    def test_vertical_cover_stays_portrait(self):
        out = aps.avatar_from_cover(_jpeg(379, 538, (30, 40, 50)))
        from PIL import Image
        im = Image.open(io.BytesIO(out))
        self.assertLess(im.width, im.height)


class NoBackupTest(unittest.TestCase):
    def test_retranslate_does_not_create_bak(self):
        with tempfile.TemporaryDirectory() as td:
            p = os.path.join(td, "TEST-001.nfo")
            Path(p).write_text(
                "<?xml version='1.0'?><movie><num>TEST-001</num>"
                "<title>中文标题</title><originaltitle>日本のタイトル</originaltitle>"
                "<plot>短简介</plot><sorttitle>中文标题</sorttitle></movie>",
                encoding="utf-8")
            old_title, old_plot = t.translate_title_zh, t.build_plot_zh
            t.translate_title_zh = lambda d: "新的中文标题"
            t.build_plot_zh = lambda d, title_zh="": "新" * 150
            try:
                r = rt.retranslate(p, True, False, True)
            finally:
                t.translate_title_zh, t.build_plot_zh = old_title, old_plot
            self.assertTrue(r.startswith("OK"))
            self.assertFalse(os.path.exists(p + ".bak-retranslate"))


class CleanBeforeTest(unittest.TestCase):
    def test_clean_deletes_junk_and_backup_but_skips_part_dir(self):
        script = os.path.join(SCRIPTS, "clean_qh.sh")
        with tempfile.TemporaryDirectory() as td:
            a = Path(td) / "AAA-001"
            b = Path(td) / "BBB-002"
            a.mkdir(); b.mkdir()
            (a / "MAIN-001.mp4").touch()
            os.truncate(a / "MAIN-001.mp4", 600 * 1024 * 1024)
            (a / "ad.mp4").touch()
            os.truncate(a / "ad.mp4", 10 * 1024 * 1024)
            (a / "MAIN-001.nfo").write_text("<movie/>", encoding="utf-8")
            (a / "MAIN-001.nfo.bak-actor").write_text("old", encoding="utf-8")
            (b / "OTHER.mp4.part").touch()
            os.truncate(b / "OTHER.mp4.part", 10 * 1024 * 1024)
            (b / "ad.mp4").touch()
            os.truncate(b / "ad.mp4", 10 * 1024 * 1024)
            env = dict(os.environ, XIEYIN_BASE=td)
            p = subprocess.run(["bash", script, "run"], env=env,
                               capture_output=True, text=True, timeout=60)
            self.assertEqual(p.returncode, 0, p.stderr)
            self.assertTrue((a / "MAIN-001.mp4").exists())
            self.assertFalse((a / "ad.mp4").exists())
            self.assertFalse((a / "MAIN-001.nfo.bak-actor").exists())
            self.assertTrue((a / "MAIN-001.nfo").exists())
            self.assertTrue((b / "OTHER.mp4.part").exists())
            self.assertTrue((b / "ad.mp4").exists(), "下载中目录必须整目录跳过")



class WriteArtifactsTest(unittest.TestCase):
    def test_valid_cover_writes_full_set(self):
        import argparse
        import race_scrape as rs
        with tempfile.TemporaryDirectory() as td:
            folder = Path(td)
            video = folder / "AAA-001.mp4"
            video.touch()
            args = argparse.Namespace(no_probe=True, no_images=False,
                                      force_images=False, image_policy="replace")
            rs.write_artifacts({"code": "AAA-001", "title": "t", "actors": []},
                               folder, video, args, _jpeg(400, 538), "vertical")
            for n in ("AAA-001-poster.jpg", "AAA-001-fanart.jpg",
                      "AAA-001-thumb.jpg", "poster.jpg", "folder.jpg"):
                self.assertTrue((folder / n).exists(), n)

class AutoFlagsTest(unittest.TestCase):
    def test_done_states_include_seeding(self):
        for st in ("completed", "uploading", "stalledUP"):
            self.assertIn(st.lower(), aw.DONE_STATES)

    def test_auto_opts_carries_new_flags(self):
        full = {"auto": {"plot_zh": True, "actor_sync": True,
                         "fnos_sync": True, "clean_enabled": True},
                "jav": {"plot_zh": False, "actor_sync": False}}
        w = aw.AutoWatcher(lambda: full, "/tmp/xieyin-test-state", lambda *a: None)
        opts = w._auto_opts(full, ["/tmp/x"])
        self.assertTrue(opts["plot_zh"])
        self.assertTrue(opts["actor_sync"])
        self.assertTrue(opts["fnos_sync"])
        self.assertTrue(opts["clean_before"])


class AppFlagsTest(unittest.TestCase):
    def test_build_job_clean_before_and_flags(self):
        import app_server as a
        with tempfile.TemporaryDirectory() as td:
            _, _, _, env, meta = a.build_job({
                "kind": "jav", "root": td, "dirs": [td], "dry": False,
                "plot_zh": True, "actor_sync": True, "fnos_sync": False,
                "clean_before": True,
            })
            self.assertEqual(env.get("XIEYIN_CLEAN_BEFORE"), "1")
            self.assertEqual(env.get("XIEYIN_PLOT_ZH"), "1")
            self.assertEqual(env.get("XIEYIN_ACTOR_SYNC"), "1")
            self.assertEqual(env.get("XIEYIN_FNOS_SYNC"), "0")
            self.assertEqual(meta.get("dirs"), [td])


if __name__ == "__main__":
    unittest.main(verbosity=2)
