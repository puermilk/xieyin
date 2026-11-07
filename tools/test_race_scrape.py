#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""1.4.0 多源竞速刮削的离线测试（不联网）。

用法：python3 tools/test_race_scrape.py
覆盖：标题清洗回归、合规白名单、cid 构造、两个解析器、番号一致性、字段合并。
"""
import json
import os
import shutil
import sys
import tempfile
import time
import unittest
from pathlib import Path

HERE = os.path.dirname(os.path.abspath(__file__))
SCRIPTS = os.path.join(HERE, "..", "src", "xieyin", "app", "xieyin", "scripts")
sys.path.insert(0, os.path.abspath(SCRIPTS))

import race_common as rc  # noqa: E402
import race_scrape as rs  # noqa: E402
from javdb_parse import parse_javdb  # noqa: E402
from jav321_parse import parse_jav321, make_cid  # noqa: E402


# 真实抓回页面的结构片段（已去掉无关内容）
JAVDB_SAMPLE = """
<html><head>
<title> ADN-451 義父に10秒だけの約束で挿入を許したら… 三宮つばき | JavDB 成人影片數據庫 </title>
<meta name="keywords" content="磁力下載,magnet,torrent" />
</head><body>
<div class="panel-block"><strong>日期:</strong> &nbsp;<span class="value">2023-03-07</span></div>
<div class="panel-block"><strong>時長:</strong> &nbsp;<span class="value">120 分鍾</span></div>
<div class="panel-block"><strong>導演:</strong> &nbsp;<span class="value"><a href="/directors/rqnA">絵地メコン</a></span></div>
<div class="panel-block"><strong>片商:</strong> &nbsp;<span class="value"><a href="/makers/x">アタッカーズ</a></span></div>
<div class="panel-block"><strong>演員:</strong> &nbsp;<span class="value"><a href="/actors/a">三宮つばき</a> <a href="/actors/b">小花暖</a></span></div>
<div class="panel-block"><strong>類別:</strong> &nbsp;<span class="value"><a href="/tags/a">已婚婦女</a> <a href="/tags/b">亂倫</a></span></div>
<div class="panel-block"><strong>系列:</strong> &nbsp;<span class="value"><a href="/series/KJZA">義父に…</a></span></div>
<img src="https://c0.jdbstatic.com/covers/1b/1BgmOn.jpg" />
<a href="magnet:?xt=urn:btih:deadbeef">磁力</a>
</body></html>
""".encode("utf-8")

JAV321_SAMPLE = """
<html><body>
<h3>義父に10秒だけの約束で挿入を許したら… 三宮つばき <small>adn-451 三宮つばき</small></h3>
<div class="col-md-9"><b>出演者</b>: <a href="/star/1062909/1">三宮つばき</a> &nbsp; <br>
<b>メーカー</b>: <a href="/company/x">アタッカーズ</a><br>
<b>品番</b>: adn-451<br><b>配信開始日</b>: 2023-03-07<br>
<b>収録時間</b>: 117 minutes<br><b>平均評価</b>: 4<br></div>
<img src="http://pics.dmm.co.jp/digital/video/adn00451/adn00451ps.jpg" />
<a href="magnet:?xt=urn:btih:cafebabe">磁力</a>
</body></html>
""".encode("utf-8")


def fake_jpeg(tag=b"", n=11000):
    """构造结构合法的假 JPEG（SOI 开头 + EOI 结尾），大小可控。"""
    return b"\xff\xd8\xff" + tag * n + b"\xff\xd9"


class CleanTitleTest(unittest.TestCase):
    def test_javdb_opening_code(self):
        """回归：旧正则会把开头番号连同标题一起吃掉"""
        self.assertEqual(
            rc.clean_title(" ADN-451 義父に10秒だけの約束で… 三宮つばき | JavDB 成人影片數據庫"),
            "義父に10秒だけの約束で… 三宮つばき")

    def test_jav321_tail_seo(self):
        self.assertEqual(
            rc.clean_title("義父に… 三宮つばき adn-451 三宮つばき bittorrent Download dmm"),
            "義父に… 三宮つばき")

    def test_empty(self):
        self.assertEqual(rc.clean_title(""), "")


class JpegOkTest(unittest.TestCase):
    def test_complete(self):
        self.assertTrue(rc.jpeg_ok(b"\xff\xd8\xff" + b"x" * 11000 + b"\xff\xd9"))

    def test_truncated(self):
        self.assertFalse(rc.jpeg_ok(b"\xff\xd8\xff" + b"x" * 11000 + b"\x00\x00"))

    def test_too_small(self):
        self.assertFalse(rc.jpeg_ok(b"\xff\xd8\xff\xff\xd9"))

    def test_not_jpeg(self):
        self.assertFalse(rc.jpeg_ok(b"x" * 20000))


class SanitizeTest(unittest.TestCase):
    def test_magnet_and_play_url_dropped(self):
        out = rc.sanitize({"title": "ok", "cover": "magnet:?xt=urn:btih:abc",
                           "desc": "http://x/watch/123", "play_url": "http://x"})
        self.assertEqual(out, {"title": "ok"})

    def test_list_items_filtered(self):
        out = rc.sanitize({"genres": ["亂倫", "magnet:xxx", "單體作品"]})
        self.assertEqual(out["genres"], ["亂倫", "單體作品"])

    def test_non_whitelist_key(self):
        self.assertNotIn("samples", rc.sanitize({"samples": ["http://x/1.jpg"], "title": "t"}))


class MakeCidTest(unittest.TestCase):
    def test_dash(self):
        self.assertEqual(make_cid("ADN-451"), "adn00451")

    def test_no_dash(self):
        self.assertEqual(make_cid("SSNI984"), "ssni00984")

    def test_invalid(self):
        self.assertEqual(make_cid("演员名目录"), "")


class ParseJavdbTest(unittest.TestCase):
    def test_fields(self):
        d = parse_javdb(JAVDB_SAMPLE, "ADN-451")
        self.assertEqual(d["code"], "ADN-451")
        self.assertEqual(d["title"], "義父に10秒だけの約束で挿入を許したら… 三宮つばき")
        self.assertEqual(d["director"], "絵地メコン")
        self.assertEqual(d["releasedate"], "2023-03-07")
        self.assertEqual(d["runtime"], "120 分鍾")
        self.assertEqual(d["studio"], "アタッカーズ")
        self.assertEqual(d["actors"], ["三宮つばき", "小花暖"])
        self.assertEqual(d["genres"], ["已婚婦女", "亂倫"])
        self.assertEqual(d["cover"], "https://c0.jdbstatic.com/covers/1b/1BgmOn.jpg")

    def test_no_magnet_leaks(self):
        self.assertNotIn("magnet", repr(parse_javdb(JAVDB_SAMPLE, "ADN-451")))
        self.assertNotIn("torrent", repr(parse_javdb(JAVDB_SAMPLE, "ADN-451")))


class ParseJav321Test(unittest.TestCase):
    def test_fields(self):
        d = parse_jav321(JAV321_SAMPLE, "ADN-451")
        self.assertEqual(d["code"], "ADN-451")
        self.assertEqual(d["title"], "義父に10秒だけの約束で挿入を許したら… 三宮つばき")
        self.assertEqual(d["studio"], "アタッカーズ")
        self.assertEqual(d["releasedate"], "2023-03-07")
        self.assertEqual(d["runtime"], "117 minutes")
        self.assertEqual(d["actors"], ["三宮つばき"])
        self.assertEqual(d["cover"],
                         "https://pics.dmm.co.jp/digital/video/adn00451/adn00451ps.jpg")

    def test_no_magnet_leaks(self):
        self.assertNotIn("magnet", repr(parse_jav321(JAV321_SAMPLE, "ADN-451")))


class CheckCodeTest(unittest.TestCase):
    def test_match(self):
        self.assertTrue(rs.check_code(b"<title>ADN-451 foo</title>", "ADN-451"))

    def test_fuzzy_rejected(self):
        """JavDB 会把 ZZZ-999 模糊匹配成 ZZZM-999，必须拒绝"""
        self.assertFalse(rs.check_code(b"<title>ZZZM-999 foo</title>", "ZZZ-999"))


class MergeTest(unittest.TestCase):
    def test_only_empty_fields_added(self):
        fast = {"code": "x", "title": "T", "studio": "A", "cover": "small.jpg",
                "source": "jav321"}
        slow = {"title": "OTHER", "director": "D", "genres": ["G"],
                "cover": "big.jpg", "source": "javdb580"}
        out, added = rs.merge(fast, slow)
        self.assertEqual(out["title"], "T")          # 不覆盖已有非空字段
        self.assertEqual(out["studio"], "A")
        self.assertEqual(out["director"], "D")       # 补空字段
        self.assertEqual(out["genres"], ["G"])
        self.assertEqual(out["cover"], "big.jpg")    # cover 例外，用慢源大图
        self.assertEqual(out["source"], "jav321")
        self.assertIn("director", added)
        self.assertIn("genres", added)
        self.assertIn("cover", added)

    def test_slow_failure_keeps_fast(self):
        fast = {"code": "x", "title": "T"}
        self.assertEqual(rs.merge(fast, {})[0], fast)


class BuildNfoCompatTest(unittest.TestCase):
    def test_parsers_feed_build_nfo(self):
        import jav_scrape as j
        for fn, sample in ((parse_javdb, JAVDB_SAMPLE), (parse_jav321, JAV321_SAMPLE)):
            d = fn(sample, "ADN-451")
            xml = j.build_nfo(d, {}, "ADN-451")
            self.assertIn("<movie>", xml)
            self.assertIn("三宮つばき", xml)
            self.assertNotIn("magnet", xml)
            self.assertNotIn("torrent", xml)


class BuildNfoRuntimeTest(unittest.TestCase):
    """回归：<runtime> 必须写入 nfo（1.3.0 起 mins 是死变量，时长从不落盘）。"""

    def test_runtime_written(self):
        import jav_scrape as j
        xml = j.build_nfo({"code": "ADN-451", "title": "t", "runtime": "117 minutes",
                           "actors": []}, {}, "ADN-451")
        self.assertIn("<runtime>117</runtime>", xml)

    def test_runtime_cjk_units(self):
        import jav_scrape as j
        xml = j.build_nfo({"code": "X-1", "title": "t", "runtime": "120 分鍾",
                           "actors": []}, {}, "X-1")
        self.assertIn("<runtime>120</runtime>", xml)

    def test_no_runtime_tag_when_missing(self):
        import jav_scrape as j
        xml = j.build_nfo({"code": "X-1", "title": "t", "actors": []}, {}, "X-1")
        self.assertNotIn("<runtime>", xml)


class PreflightTtlTest(unittest.TestCase):
    """审查 C1：preflight 缓存 TTL 必须真起作用（配置改了要生效）。"""

    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xieyin-ttl-")
        os.environ["XIEYIN_PREFLIGHT_CACHE"] = os.path.join(self.tmp, "cache.json")
        self.calls = []
        self._orig = rc._preflight_one

        def fake(c):
            self.calls.append(c["name"])
            return dict(name=c["name"], host=c["host"], role=c["role"],
                        ips=["1.2.3.4"], dns="200/1B", resolve=[], mode="dns",
                        enabled=True, reason="fake",
                        resource=(c["kind"] == "resource"))
        rc._preflight_one = fake

    def tearDown(self):
        rc._preflight_one = self._orig
        os.environ.pop("XIEYIN_PREFLIGHT_CACHE", None)
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_cache_hit_then_expiry_reprobes(self):
        rc.preflight(force=True, ttl=600, verbose=False)
        self.assertEqual(len(self.calls), len(rc.SOURCES))      # 首跑真探测
        rc.preflight(force=False, ttl=600, verbose=False)
        self.assertEqual(len(self.calls), len(rc.SOURCES))      # 缓存命中不再探
        rc.preflight(force=False, ttl=0, verbose=False)
        self.assertEqual(len(self.calls), 2 * len(rc.SOURCES))  # 过期重探

    def test_cli_preflight_ttl_reaches_preflight(self):
        """--preflight-ttl 必须真正传进 preflight()，否则配置项是假开关。"""
        captured = {}
        orig = rc.preflight

        def fake_preflight(force=False, ttl=None, verbose=True):
            captured["ttl"] = ttl
            return [], []
        rc.preflight = fake_preflight
        old = sys.argv
        try:
            sys.argv = ["race_scrape.py", "--root", self.tmp,
                        "--preflight-ttl", "77", "--no-cache"]
            rs.main()
        finally:
            sys.argv = old
            rc.preflight = orig
        self.assertEqual(captured.get("ttl"), 77)


class WriteArtifactsProtectionTest(unittest.TestCase):
    """审查 S1/T1：PT 保护契约 —— 已有合法图片绝不覆盖（回归钉子）。"""

    class Args:
        no_probe = True
        no_images = False

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="xieyin-pt-"))
        self.video = self.folder / "ADN-451.mp4"
        self.video.write_bytes(b"")
        self.d = {"code": "ADN-451", "title": "t"}

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def _paths(self):
        s = self.video.stem
        return (self.folder / (s + "-fanart.jpg"),
                self.folder / (s + "-thumb.jpg"),
                self.folder / (s + "-poster.jpg"))

    def test_existing_valid_images_never_overwritten(self):
        seed = fake_jpeg(b"SEED")
        for p in self._paths():
            p.write_bytes(seed)
        rs.write_artifacts(self.d, self.folder, self.video, self.Args(),
                           fake_jpeg(b"NEW", 20000))
        for p in self._paths():
            self.assertEqual(p.read_bytes(), seed,
                             "%s 被覆盖 —— 违反 PT 保护契约" % p.name)

    def test_existing_thumb_survives_when_fanart_missing(self):
        """回归 S1：PT 只有 thumb 没 fanart 时，旧代码把 thumb 冲成新封面。"""
        fan, thumb, poster = self._paths()
        seed = fake_jpeg(b"SEED")
        thumb.write_bytes(seed)
        rs.write_artifacts(self.d, self.folder, self.video, self.Args(),
                           fake_jpeg(b"NEW", 20000))
        self.assertEqual(thumb.read_bytes(), seed)

    def test_half_written_fanart_is_healed(self):
        fan, thumb, poster = self._paths()
        fan.write_bytes(b"\xff\xd8\xff" + b"x" * 100)   # 半截图（无 EOI）
        rs.write_artifacts(self.d, self.folder, self.video, self.Args(),
                           fake_jpeg(b"NEW", 20000))
        self.assertEqual(fan.read_bytes(), fake_jpeg(b"NEW", 20000))

    def test_half_written_thumb_is_healed(self):
        fan, thumb, poster = self._paths()
        thumb.write_bytes(b"\xff\xd8\xff" + b"x" * 100)
        rs.write_artifacts(self.d, self.folder, self.video, self.Args(),
                           fake_jpeg(b"NEW", 20000))
        self.assertEqual(thumb.read_bytes(), fake_jpeg(b"NEW", 20000))


class FetchMultiFailTest(unittest.TestCase):
    """审查 T2：全通道失败必须返回可判定的失败值。"""

    def test_all_channels_fail(self):
        orig = rc.curl
        rc.curl = lambda *a, **k: (0, 0, b"")
        try:
            st, sz, body, via = rc.fetch_multi("h", "/p", ["1.2.3.4"])
        finally:
            rc.curl = orig
        self.assertEqual((st, body, via), (0, b"", "none"))


if __name__ == "__main__":
    unittest.main(verbosity=2)
