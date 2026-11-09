#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""1.4.0 多源竞速刮削的离线测试（不联网）。

用法：python3 tools/test_race_scrape.py
覆盖：标题清洗回归、合规白名单、cid 构造、两个解析器、番号一致性、字段合并。
"""
import io
import inspect
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
import jav_scrape as j  # noqa: E402
import fnos_meta as fm  # noqa: E402
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


class BuildNfoArtScopeTest(unittest.TestCase):
    """回归：<art> 只引用磁盘上真实存在的图片。

    飞牛影视对不存在的引用会报 NFO image relative path not found，并退化成
    扫描整个目录；平铺目录（多个视频同目录）就会把别的条目的封面错配过来
    （实测：5 个 JUR 条目共用了 JUR-794 的封面）。
    """

    def _xml(self, folder, stem="ABC-123-C"):
        import jav_scrape as j
        return j.build_nfo({"code": "ABC-123", "title": "t", "actors": []},
                           {}, "ABC-123", stem, folder)

    def test_only_existing_poster_referenced(self):
        d = tempfile.mkdtemp(prefix="xieyin-art-")
        open(os.path.join(d, "ABC-123-C-poster.jpg"), "wb").write(b"p")
        xml = self._xml(d)
        self.assertIn("<poster>ABC-123-C-poster.jpg</poster>", xml)
        self.assertNotIn("fanart.jpg</fanart>", xml)
        self.assertNotIn("thumb.jpg</thumb>", xml)

    def test_no_art_block_when_no_images(self):
        d = tempfile.mkdtemp(prefix="xieyin-art-")
        xml = self._xml(d)
        self.assertNotIn("<art>", xml)
        self.assertNotIn("-poster.jpg", xml)

    def test_never_writes_bare_poster_alias(self):
        """平铺目录里无前缀 poster.jpg 只可能属于其中一条，绝不引用。"""
        d = tempfile.mkdtemp(prefix="xieyin-art-")
        open(os.path.join(d, "ABC-123-C-poster.jpg"), "wb").write(b"p")
        open(os.path.join(d, "poster.jpg"), "wb").write(b"other")
        xml = self._xml(d)
        self.assertNotIn("<poster>poster.jpg</poster>", xml)

    def test_legacy_call_without_folder_keeps_refs(self):
        import jav_scrape as j
        xml = j.build_nfo({"code": "ABC-123", "title": "t", "actors": []},
                          {}, "ABC-123", "ABC-123-C")
        self.assertIn("<poster>ABC-123-C-poster.jpg</poster>", xml)


class WriteArtifactsOrderTest(unittest.TestCase):
    """nfo 必须在图片之后写，否则按存在性生成的 <art> 会全是空。"""

    def test_nfo_written_after_images(self):
        import inspect
        import race_scrape as r
        src = inspect.getsource(r.write_artifacts)
        img = src.index("cover_bytes")
        nfo = src.rindex("build_nfo")
        self.assertGreater(nfo, img, "build_nfo 必须在图片写入之后调用")


class NfoRespectTest(unittest.TestCase):
    """PT 资源自带发布组 nfo 时：不覆盖别人的 nfo，只补缺失的封面。"""

    def _args(self, **kw):
        import types
        d = dict(no_probe=True, no_images=True, image_policy="keep",
                 force_images=False, force=False)
        d.update(kw)
        return types.SimpleNamespace(**d)

    def _setup(self, nfo_text):
        from pathlib import Path
        tmp = tempfile.mkdtemp(prefix="xieyin-nfo-")
        v = os.path.join(tmp, "ABC-123.mp4")
        open(v, "wb").write(b"x")
        if nfo_text is not None:
            open(os.path.join(tmp, "ABC-123.nfo"), "w", encoding="utf-8").write(nfo_text)
        return Path(tmp), Path(v)

    def test_foreign_nfo_not_overwritten(self):
        import race_scrape as rs
        folder, video = self._setup("<movie><title>发布组刮的</title></movie>")
        rs.write_artifacts({"code": "ABC-123", "title": "我们的标题"}, folder, video,
                           self._args())
        txt = open(os.path.join(folder, "ABC-123.nfo"), encoding="utf-8").read()
        self.assertIn("发布组刮的", txt)
        self.assertNotIn("我们的标题", txt)

    def test_own_nfo_gets_updated(self):
        import race_scrape as rs
        folder, video = self._setup(
            "<!-- xieyin:generated -->\n<movie><num>ABC-123</num><title>旧</title></movie>")
        rs.write_artifacts({"code": "ABC-123", "title": "新标题"}, folder, video,
                           self._args())
        txt = open(os.path.join(folder, "ABC-123.nfo"), encoding="utf-8").read()
        self.assertIn("xieyin:generated", txt)
        self.assertIn("新标题", txt)

    def test_release_group_nfo_with_num_not_overwritten(self):
        """发布组 nfo 常也有 <num>，必须靠专属签名区分，不能误覆盖。"""
        import race_scrape as rs
        folder, video = self._setup(
            "<movie><num>ABC-123</num><title>发布组的</title></movie>")
        rs.write_artifacts({"code": "ABC-123", "title": "我们的"}, folder, video,
                           self._args())
        txt = open(os.path.join(folder, "ABC-123.nfo"), encoding="utf-8").read()
        self.assertIn("发布组的", txt)

    def test_force_overwrites_foreign_nfo(self):
        import race_scrape as rs
        folder, video = self._setup("<movie><title>发布组刮的</title></movie>")
        rs.write_artifacts({"code": "ABC-123", "title": "我们的标题"}, folder, video,
                           self._args(force=True))
        txt = open(os.path.join(folder, "ABC-123.nfo"), encoding="utf-8").read()
        self.assertIn("我们的标题", txt)


class DoneCheckNeedsPosterTest(unittest.TestCase):
    """只有 nfo 没有封面时不算完成（要进去补图）。"""

    def test_done_requires_poster(self):
        import inspect
        import race_scrape as rs
        src = inspect.getsource(rs.main)
        self.assertIn("poster_ok", src)
        self.assertIn("-poster.jpg", src)


class JavdatabaseCidTest(unittest.TestCase):
    """javdatabase 页面 = DMM 真实 cid 解析器（h_/18/1 等内部前缀）。"""

    def test_parses_real_cid(self):
        import race_scrape as rs
        html = b'<meta content="https://pics.dmm.co.jp/digital/video/h_068mxgs01438/h_068mxgs01438pl.jpg">'
        orig = rs.rc.curl
        rs.rc.curl = lambda host, path, ip=None, timeout=None: (200, 0, html)
        try:
            self.assertEqual(rs.rc.javdatabase_cid("MXGS-1438"), "h_068mxgs01438")
        finally:
            rs.rc.curl = orig

    def test_empty_when_page_fails(self):
        import race_scrape as rs
        orig = rs.rc.curl
        rs.rc.curl = lambda *a, **k: (0, 0, b"")
        try:
            self.assertEqual(rs.rc.javdatabase_cid("XXX-1"), "")
            self.assertEqual(rs.rc.javdatabase_cid(""), "")
        finally:
            rs.rc.curl = orig

    def test_fetch_cover_falls_back_to_real_cid(self):
        import race_scrape as rs
        seen = []

        def fake_cov(u, **k):
            seen.append(u)
            return (b"\xff\xd8\xff" + b"X" * 400) if "h_068mxgs01438" in u else None

        orig_cov, orig_cid = rs.rc.download_cover, rs.rc.javdatabase_cid
        rs.rc.download_cover = fake_cov
        rs.rc.javdatabase_cid = lambda c, **k: "h_068mxgs01438"
        try:
            cov, used = rs.fetch_cover(["https://x/a.jpg"], None, attempts=1,
                                       code="MXGS-1438")
        finally:
            rs.rc.download_cover, rs.rc.javdatabase_cid = orig_cov, orig_cid
        self.assertIsNotNone(cov)
        self.assertIn("h_068mxgs01438", used)
        self.assertIn("pics.dmm.com", used)


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
        force_images = False

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


class ForceImagesTest(unittest.TestCase):
    """1.5.1：--force 只管 nfo；--force-images 才动图。"""

    class Args:
        no_probe = True
        no_images = False
        force_images = False
        image_policy = "keep"

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="xieyin-fi-"))
        self.video = self.folder / "ADN-451.mp4"
        self.video.write_bytes(b"")
        self.d = {"code": "ADN-451", "title": "t"}

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_force_alone_keeps_images(self):
        seed = fake_jpeg(b"SEED")
        for n in ("fanart", "thumb"):
            (self.folder / ("ADN-451-" + n + ".jpg")).write_bytes(seed)
        rs.write_artifacts(self.d, self.folder, self.video, self.Args(),
                           fake_jpeg(b"NEW", 20000))
        for n in ("fanart", "thumb"):
            self.assertEqual((self.folder / ("ADN-451-" + n + ".jpg")).read_bytes(),
                             seed, "没传 force_images 就不该动图")

    def test_force_images_overwrites(self):
        seed = fake_jpeg(b"SEED")
        for n in ("fanart", "thumb"):
            (self.folder / ("ADN-451-" + n + ".jpg")).write_bytes(seed)
        args = self.Args()
        args.force_images = True
        new = fake_jpeg(b"NEW", 20000)
        rs.write_artifacts(self.d, self.folder, self.video, args, new)
        for n in ("fanart", "thumb"):
            self.assertEqual((self.folder / ("ADN-451-" + n + ".jpg")).read_bytes(),
                             new, "force_images 应覆盖已有图")


class NamingUnifyTest(unittest.TestCase):
    """1.5.1：AV 同时补写无前缀 poster.jpg / folder.jpg（兼容两种命名）。"""

    class Args:
        no_probe = True
        no_images = False
        force_images = False
        image_policy = "keep"

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="xieyin-nm-"))
        self.video = self.folder / "ADN-451.mp4"
        self.video.write_bytes(b"")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def test_unprefixed_aliases_written(self):
        rs.write_artifacts({"code": "ADN-451", "title": "t"}, self.folder,
                           self.video, self.Args(), fake_jpeg(b"NEW", 20000))
        for n in ("ADN-451-poster.jpg", "ADN-451-fanart.jpg", "ADN-451-thumb.jpg",
                  "poster.jpg", "folder.jpg"):
            self.assertTrue((self.folder / n).exists(), n + " 缺失")

    def test_aliases_protected_like_prefixed(self):
        """无前缀别名同样受保护：已有合法 poster.jpg 不被覆盖。"""
        seed = fake_jpeg(b"SEED")
        (self.folder / "poster.jpg").write_bytes(seed)
        (self.folder / "folder.jpg").write_bytes(seed)
        rs.write_artifacts({"code": "ADN-451", "title": "t"}, self.folder,
                           self.video, self.Args(), fake_jpeg(b"NEW", 20000))
        self.assertEqual((self.folder / "poster.jpg").read_bytes(), seed)
        self.assertEqual((self.folder / "folder.jpg").read_bytes(), seed)


class ImagePolicyTest(unittest.TestCase):
    """1.5.1：image_policy = keep / replace / smart。"""

    class Args:
        no_probe = True
        no_images = False
        force_images = False
        image_policy = "keep"

    def setUp(self):
        self.folder = Path(tempfile.mkdtemp(prefix="xieyin-ip-"))
        self.video = self.folder / "ADN-451.mp4"
        self.video.write_bytes(b"")
        self.seed = fake_jpeg(b"SEED")

    def tearDown(self):
        shutil.rmtree(self.folder, ignore_errors=True)

    def _seed_images(self):
        for n in ("fanart", "thumb"):
            (self.folder / ("ADN-451-" + n + ".jpg")).write_bytes(self.seed)

    def test_keep_never_touches(self):
        self._seed_images()
        args = self.Args(); args.image_policy = "keep"
        rs.write_artifacts({"code": "ADN-451", "title": "t"}, self.folder,
                           self.video, args, fake_jpeg(b"NEW", 20000))
        self.assertEqual((self.folder / "ADN-451-fanart.jpg").read_bytes(), self.seed)

    def test_replace_always(self):
        self._seed_images()
        args = self.Args(); args.image_policy = "replace"
        new = fake_jpeg(b"NEW", 20000)
        rs.write_artifacts({"code": "ADN-451", "title": "t"}, self.folder,
                           self.video, args, new)
        self.assertEqual((self.folder / "ADN-451-fanart.jpg").read_bytes(), new)

    def test_smart_without_our_nfo_keeps(self):
        """smart：目录里没有本工具的 nfo 签名 -> 不覆盖（不是我们写的图）。"""
        self._seed_images()
        args = self.Args(); args.image_policy = "smart"
        rs.write_artifacts({"code": "ADN-451", "title": "t"}, self.folder,
                           self.video, args, fake_jpeg(b"NEW", 20000))
        self.assertEqual((self.folder / "ADN-451-fanart.jpg").read_bytes(), self.seed,
                         "smart 且无签名 nfo 不应覆盖")

    def test_smart_with_our_nfo_replaces(self):
        """smart：nfo 带 <num> 签名（本工具产物）-> 覆盖自己的图。"""
        self._seed_images()
        (self.folder / "ADN-451.nfo").write_text(
            "<!-- xieyin:generated -->\n<movie>\n  <num><![CDATA[ADN-451]]></num>\n</movie>\n",
            encoding="utf-8")
        args = self.Args(); args.image_policy = "smart"
        new = fake_jpeg(b"NEW", 20000)
        rs.write_artifacts({"code": "ADN-451", "title": "t"}, self.folder,
                           self.video, args, new)
        self.assertEqual((self.folder / "ADN-451-fanart.jpg").read_bytes(), new,
                         "smart 且有签名 nfo 应覆盖")


class CollectUnitsTest(unittest.TestCase):
    """问题②：根目录直接放的视频也要刮；番号解析对无番号目录回退视频文件名。"""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xieyin-units-"))

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_root_level_video_uses_video_name_code(self):
        v = self.tmp / "ADN-999-UC.mp4"
        v.write_bytes(b"")
        units = j.collect_units([self.tmp])
        self.assertEqual(units, [(self.tmp, v, "ADN-999")])

    def test_folder_code_wins_for_nested_video(self):
        f = self.tmp / "ADN-888-UC"
        f.mkdir()
        v = f / "whatever@GROUP.mp4"
        v.write_bytes(b"")
        units = j.collect_units([f])
        self.assertEqual(units, [(f, v, "ADN-888")])

    def test_video_name_fallback_when_folder_has_no_code(self):
        f = self.tmp / "新建文件夹"
        f.mkdir()
        v = f / "SSNI-984-10bit.mp4"
        v.write_bytes(b"")
        units = j.collect_units([f])
        self.assertEqual(units, [(f, v, "SSNI-984")])

    def test_no_code_anywhere_is_skipped(self):
        v = self.tmp / "随便什么.mp4"
        v.write_bytes(b"")
        self.assertEqual(j.collect_units([self.tmp]), [])

    def test_each_video_is_own_unit(self):
        v1 = self.tmp / "ADN-111-UC.mp4"
        v2 = self.tmp / "SSNI-222-UC.mp4"
        v1.write_bytes(b"")
        v2.write_bytes(b"")
        units = j.collect_units([self.tmp])
        self.assertEqual([u[1] for u in units], [v1, v2])
        self.assertEqual([u[2] for u in units], ["ADN-111", "SSNI-222"])

    def test_multi_part_folder_gives_unit_per_part(self):
        f = self.tmp / "ADN-888-UC"
        f.mkdir()
        v1 = f / "ADN-888-UC-1.mp4"
        v2 = f / "ADN-888-UC-2.mp4"
        v1.write_bytes(b"")
        v2.write_bytes(b"")
        units = j.collect_units([f])
        self.assertEqual([u[2] for u in units], ["ADN-888", "ADN-888"])
        self.assertEqual([u[1] for u in units], [v1, v2])

    def test_sample_and_trailer_videos_skipped(self):
        f = self.tmp / "ADN-451-UC"
        f.mkdir()
        (f / "ADN-451-UC-sample.mp4").write_bytes(b"")
        (f / "ADN-451-UC-trailer.mp4").write_bytes(b"")
        v = f / "ADN-451-UC.mp4"
        v.write_bytes(b"")
        units = j.collect_units([f])
        self.assertEqual([u[1] for u in units], [v])

    def test_folder_with_code_and_two_videos_same_code(self):
        """文件夹名有码时所有视频共用该码（含无关名视频）。"""
        f = self.tmp / "PRED-566-UC"
        f.mkdir()
        (f / "part1.mp4").write_bytes(b"")
        (f / "part2.mp4").write_bytes(b"")
        units = j.collect_units([f])
        self.assertEqual([u[2] for u in units], ["PRED-566", "PRED-566"])


class FnosMetaPayloadTest(unittest.TestCase):
    """方案A：meta 载荷按文档字段映射；magnet/播放地址绝不进推送体。"""

    def test_movie_fields_mapping(self):
        d = {"title": "肖申克的救赎", "overview": "希望让人自由",
             "year": 1994, "tmdb_id": "278", "douban_id": "1292052",
             "genres": ["剧情", "犯罪"], "cast": ["蒂姆·罗宾斯"],
             "cover": "https://img/poster.jpg", "backdrop_url": "https://img/back.jpg"}
        p = fm.build_payload(d, "/mnt/pt/电影/肖申克的救赎.mkv")
        self.assertTrue(os.path.isabs(p["file_path"]))
        m = p["meta"]
        self.assertEqual(m["title"], "肖申克的救赎")
        self.assertEqual(m["overview"], "希望让人自由")
        self.assertEqual(m["year"], 1994)
        self.assertEqual(m["tmdb_id"], "278")
        self.assertEqual(m["douban_id"], "1292052")
        self.assertEqual(m["genres"], ["剧情", "犯罪"])
        self.assertEqual(m["actors"], [{"name": "蒂姆·罗宾斯"}])
        self.assertEqual(m["poster_url"], "https://img/poster.jpg")
        self.assertEqual(m["backdrop_url"], "https://img/back.jpg")

    def test_av_fields_and_year_from_releasedate(self):
        d = {"code": "ADN-888", "title": "t", "releasedate": "2023-03-07",
             "runtime": "120", "studio": "s", "director": "d",
             "series": "ser", "label": "lab", "actors": ["三宮つばき"],
             "genres": ["亂倫"], "desc": "简介", "cover": "https://c0/x.jpg"}
        p = fm.build_payload(d, "/vol02/pt/ADN-888-UC/a@G.mp4")
        m = p["meta"]
        self.assertEqual(m["year"], 2023)
        self.assertEqual(m["code"], "ADN-888")
        self.assertEqual(m["studio"], "s")
        self.assertEqual(m["director"], "d")
        self.assertEqual(m["series"], "ser")
        self.assertEqual(m["label"], "lab")
        self.assertEqual(m["runtime"], "120")
        self.assertEqual(m["releasedate"], "2023-03-07")
        self.assertEqual(m["overview"], "简介")
        self.assertEqual(m["poster_url"], "https://c0/x.jpg")

    def test_magnet_and_play_url_never_in_payload(self):
        d = {"title": "t", "desc": "有 magnet:?xt=urn:btih:abc 的简介",
             "cover": "http://x/watch/1.jpg", "backdrop_url": "magnet:?xt=1",
             "actors": [{"name": "a", "photo_url": "http://x/stream/2.jpg"}]}
        raw = str(fm.build_payload(d, "/v.mkv"))
        self.assertNotIn("magnet:", raw)
        self.assertNotIn("/watch", raw)
        self.assertNotIn("/stream", raw)

    def test_empty_fields_omitted(self):
        m = fm.build_payload({"title": "t"}, "/v.mkv")["meta"]
        self.assertEqual(m, {"title": "t", "original_title": "t"})
        self.assertNotIn("year", m)
        self.assertNotIn("actors", m)

    def test_original_title_fallback(self):
        m = fm.build_payload({"title": "原名"}, "/v.mkv")["meta"]
        self.assertEqual(m["original_title"], "原名")


class FnosMetaPushTest(unittest.TestCase):
    """方案A：POST /api/media/meta/update + Bearer 令牌 + file_path 主键。"""

    def setUp(self):
        import http.server
        import threading
        self.reqs = []
        outer = self
        class H(http.server.BaseHTTPRequestHandler):
            def do_POST(self):
                n = int(self.headers.get("Content-Length") or 0)
                body = self.rfile.read(n)
                outer.reqs.append({
                    "path": self.path,
                    "auth": self.headers.get("Authorization"),
                    "ctype": self.headers.get("Content-Type"),
                    "body": json.loads(body.decode("utf-8")),
                })
                resp = outer.resp_body
                self.send_response(outer.resp_code)
                self.send_header("Content-Type", "application/json")
                self.send_header("Content-Length", str(len(resp)))
                self.end_headers()
                self.wfile.write(resp)
            def log_message(self, *a):
                pass
        self.H = H
        self.resp_body = b'{"code":0,"msg":"success","data":{}}'
        self.resp_code = 200
        self.srv = http.server.HTTPServer(("127.0.0.1", 0), H)
        self.port = self.srv.server_address[1]
        self.thread = threading.Thread(target=self.srv.serve_forever, daemon=True)
        self.thread.start()

    def tearDown(self):
        self.srv.shutdown()
        self.srv.server_close()

    def test_success_push_shape(self):
        ok, msg = fm.push_meta("http://127.0.0.1:%d" % self.port, "TOKEN123",
                               {"file_path": "/v.mkv", "meta": {"title": "t"}})
        self.assertTrue(ok, msg)
        self.assertEqual(len(self.reqs), 1)
        r = self.reqs[0]
        self.assertEqual(r["path"], "/api/media/meta/update")
        self.assertEqual(r["auth"], "Bearer TOKEN123")
        self.assertEqual(r["ctype"], "application/json")
        self.assertEqual(r["body"], {"file_path": "/v.mkv", "meta": {"title": "t"}})

    def test_base_url_with_and_without_prefix(self):
        fm.push_meta("http://127.0.0.1:%d" % self.port, "", {"file_path": "/v.mkv", "meta": {}})
        fm.push_meta("http://127.0.0.1:%d/api/media/meta" % self.port, "", {"file_path": "/v.mkv", "meta": {}})
        self.assertEqual([r["path"] for r in self.reqs],
                         ["/api/media/meta/update", "/api/media/meta/update"])

    def test_no_token_omits_auth_header(self):
        fm.push_meta("http://127.0.0.1:%d" % self.port, "", {"file_path": "/v.mkv", "meta": {}})
        self.assertIsNone(self.reqs[0]["auth"])

    def test_error_code_returns_failure(self):
        self.resp_body = b'{"code":5000,"msg":"invalid sign","data":null}'
        ok, msg = fm.push_meta("http://127.0.0.1:%d" % self.port, "T", {"file_path": "/v.mkv", "meta": {}})
        self.assertFalse(ok)
        self.assertIn("invalid sign", msg)

    def test_http_error_returns_failure(self):
        self.resp_code = 502
        self.resp_body = b"bad gateway"
        ok, msg = fm.push_meta("http://127.0.0.1:%d" % self.port, "T", {"file_path": "/v.mkv", "meta": {}})
        self.assertFalse(ok)
        self.assertIn("502", msg)

    def test_connection_refused_returns_failure(self):
        ok, msg = fm.push_meta("http://127.0.0.1:1", "T", {"file_path": "/v.mkv", "meta": {}}, timeout=2)
        self.assertFalse(ok)
        self.assertIn("连接失败", msg)

    def test_empty_base_returns_failure(self):
        ok, msg = fm.push_meta("", "T", {"file_path": "/v.mkv", "meta": {}})
        self.assertFalse(ok)
        self.assertIn("未配置", msg)

    def test_non_json_response_returns_failure(self):
        self.resp_body = b"<html>oops</html>"
        ok, msg = fm.push_meta("http://127.0.0.1:%d" % self.port, "T", {"file_path": "/v.mkv", "meta": {}})
        self.assertFalse(ok)




class SinkDispatchTest(unittest.TestCase):
    """方案A 产物分发：push 推飞牛 API / export 落独立元目录 / preview 零产出 / inplace 就地写。"""

    class Args:
        no_probe = True
        no_images = False
        force_images = False
        image_policy = "keep"
        sink = "inplace"
        fnos_url = ""
        fnos_token = ""
        meta_dir = ""
        root = ""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xieyin-sink-"))
        self.pt = self.tmp / "pt"
        (self.pt / "ADN-888-UC").mkdir(parents=True)
        self.video = self.pt / "ADN-888-UC" / "a@GROUP.mp4"
        self.video.write_bytes(b"")
        self.meta = self.tmp / "meta"
        self.d = {"code": "ADN-888", "title": "t", "cover": "https://img/x.jpg"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def _args(self, sink):
        a = self.Args()
        a.sink = sink
        a.root = str(self.pt)
        return a

    def test_push_sink_pushes_and_writes_nothing(self):
        pushed = []
        args = self._args("push")
        args.fnos_url, args.fnos_token = "http://x", "TOK"
        orig = fm.push_meta
        fm.push_meta = lambda base, tok, payload, timeout=15: (
            pushed.append((base, tok, payload)) or (True, "success"))
        try:
            rs.emit_artifact(self.d, self.pt / "ADN-888-UC", self.video, args)
        finally:
            fm.push_meta = orig
        self.assertEqual(len(pushed), 1)
        base, tok, payload = pushed[0]
        self.assertEqual(base, "http://x")
        self.assertEqual(tok, "TOK")
        self.assertEqual(payload["file_path"], str(self.video.resolve()))
        self.assertEqual(sorted(p.name for p in (self.pt / "ADN-888-UC").iterdir()),
                         ["a@GROUP.mp4"])

    def test_export_sink_writes_meta_dir_mirroring_source(self):
        args = self._args("export")
        args.meta_dir = str(self.meta)
        rs.emit_artifact(self.d, self.pt / "ADN-888-UC", self.video, args,
                         cover_bytes=fake_jpeg(b"NEW", 20000))
        out = self.meta / "ADN-888-UC"
        self.assertTrue((out / "a@GROUP.nfo").exists(), "nfo 应落在镜像目录")
        self.assertTrue((out / "a@GROUP-poster.jpg").exists())
        self.assertEqual(sorted(p.name for p in (self.pt / "ADN-888-UC").iterdir()),
                         ["a@GROUP.mp4"])

    def test_export_sink_flat_when_video_outside_root(self):
        args = self._args("export")
        args.meta_dir = str(self.meta)
        args.root = str(self.tmp / "elsewhere")
        rs.emit_artifact(self.d, self.pt / "ADN-888-UC", self.video, args,
                         cover_bytes=fake_jpeg(b"NEW", 20000))
        self.assertTrue((self.meta / "ADN-888-UC" / "a@GROUP.nfo").exists())

    def test_preview_sink_zero_output(self):
        args = self._args("preview")
        args.meta_dir = str(self.meta)
        rs.emit_artifact(self.d, self.pt / "ADN-888-UC", self.video, args,
                         cover_bytes=fake_jpeg(b"NEW", 20000))
        self.assertFalse(self.meta.exists(), "preview 不该产出任何文件")
        self.assertEqual(sorted(p.name for p in (self.pt / "ADN-888-UC").iterdir()),
                         ["a@GROUP.mp4"])

    def test_inplace_sink_writes_beside_video(self):
        args = self._args("inplace")
        rs.emit_artifact(self.d, self.pt / "ADN-888-UC", self.video, args,
                         cover_bytes=fake_jpeg(b"NEW", 20000))
        self.assertTrue((self.pt / "ADN-888-UC" / "a@GROUP.nfo").exists())


class CoverUrlPickTest(unittest.TestCase):
    """问题①：poster/backdrop URL 择优。poster 竖版大图优先；backdrop 从 DMM ps 推导 pl 横版。"""

    def test_poster_prefers_javdb_over_dmm_small(self):
        cands = ["https://pics.dmm.co.jp/digital/video/adn00556/adn00556ps.jpg",
                 "https://c0.jdbstatic.com/covers/1b/1BgmOn.jpg"]
        self.assertEqual(rs.pick_poster_url(cands),
                         "https://c0.jdbstatic.com/covers/1b/1BgmOn.jpg")

    def test_poster_falls_back_to_dmm(self):
        cands = ["https://pics.dmm.co.jp/digital/video/adn00556/adn00556ps.jpg"]
        self.assertEqual(rs.pick_poster_url(cands),
                         "https://pics.dmm.co.jp/digital/video/adn00556/adn00556ps.jpg")

    def test_poster_ignores_magnet_and_empty(self):
        self.assertIsNone(rs.pick_poster_url(["magnet:?xt=1", "", None]))

    def test_backdrop_derived_from_dmm_ps(self):
        cands = ["https://pics.dmm.co.jp/digital/video/adn00556/adn00556ps.jpg"]
        self.assertEqual(rs.pick_backdrop_url(cands),
                         "https://pics.dmm.co.jp/digital/video/adn00556/adn00556pl.jpg")

    def test_backdrop_tmdb_wins(self):
        cands = ["https://image.tmdb.org/t/p/original/xyz.jpg",
                 "https://pics.dmm.co.jp/digital/video/adn00556/adn00556ps.jpg"]
        self.assertEqual(rs.pick_backdrop_url(cands),
                         "https://image.tmdb.org/t/p/original/xyz.jpg")

    def test_backdrop_no_dmm_no_derivation(self):
        self.assertIsNone(rs.pick_backdrop_url(["https://c0.jdbstatic.com/covers/1b/x.jpg"]))




class CompositeSinkTest(unittest.TestCase):
    """兜底导出是「额外」产出：push+export 组合时两者都落；runtime 归一为纯数字。"""

    class Args:
        no_probe = True
        no_images = False
        force_images = False
        image_policy = "keep"
        sink = "push+export"
        fnos_url = ""
        fnos_token = ""
        meta_dir = ""
        root = ""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xieyin-cs-"))
        self.pt = self.tmp / "pt" / "ADN-888-UC"
        self.pt.mkdir(parents=True)
        self.video = self.pt / "a@GROUP.mp4"
        self.video.write_bytes(b"")
        self.meta = self.tmp / "meta"
        self.d = {"code": "ADN-888", "title": "t"}

    def tearDown(self):
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_push_plus_export_both_emit(self):
        pushed = []
        args = self.Args()
        args.root = str(self.tmp / "pt")
        args.meta_dir = str(self.meta)
        args.fnos_url, args.fnos_token = "http://x", "T"
        orig = fm.push_meta
        fm.push_meta = lambda b, t, p, timeout=15: (pushed.append(p) or (True, "ok"))
        try:
            rs.emit_artifact(self.d, self.pt, self.video, args,
                             cover_bytes=fake_jpeg(b"NEW", 20000))
        finally:
            fm.push_meta = orig
        self.assertEqual(len(pushed), 1)
        self.assertTrue((self.meta / "ADN-888-UC" / "a@GROUP.nfo").exists(),
                        "组合模式导出也要落")
        self.assertEqual(sorted(p.name for p in self.pt.iterdir()), ["a@GROUP.mp4"])

    def test_runtime_normalized_to_digits(self):
        m = fm.build_payload({"title": "t", "runtime": "117 minutes"}, "/v.mkv")["meta"]
        self.assertEqual(m["runtime"], "117")
        m2 = fm.build_payload({"title": "t", "runtime": "120"}, "/v.mkv")["meta"]
        self.assertEqual(m2["runtime"], "120")




class PushMarkTest(unittest.TestCase):
    """推送成功才标已推送（失败必须可重试）；emit 返回推送结果。"""

    class Args:
        no_probe = True
        no_images = False
        force_images = False
        image_policy = "keep"
        sink = "push"
        fnos_url = "http://x"
        fnos_token = "T"
        meta_dir = ""
        root = ""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xieyin-pm-"))
        self.pt = self.tmp / "pt" / "ADN-888-UC"
        self.pt.mkdir(parents=True)
        self.video = self.pt / "a@GROUP.mp4"
        self.video.write_bytes(b"")
        self.marks = []
        self.orig_push = fm.push_meta
        self.orig_mark = fm.mark_pushed
        fm.mark_pushed = lambda v: self.marks.append(v)

    def tearDown(self):
        fm.push_meta = self.orig_push
        fm.mark_pushed = self.orig_mark
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_success_marks_and_returns_true(self):
        fm.push_meta = lambda b, t, p, timeout=15: (True, "success")
        r = rs.emit_artifact({"code": "ADN-888", "title": "t"},
                             self.pt, self.video, self.Args())
        self.assertIs(r, True)
        self.assertEqual(self.marks, [self.video])

    def test_failure_no_mark_and_returns_false(self):
        fm.push_meta = lambda b, t, p, timeout=15: (False, "连接失败")
        r = rs.emit_artifact({"code": "ADN-888", "title": "t"},
                             self.pt, self.video, self.Args())
        self.assertIs(r, False)
        self.assertEqual(self.marks, [], "推送失败不得标记已推送")

    def test_inplace_returns_none_and_no_mark(self):
        args = self.Args()
        args.sink = "inplace"
        r = rs.emit_artifact({"code": "ADN-888", "title": "t"}, self.pt, self.video, args)
        self.assertIsNone(r)
        self.assertEqual(self.marks, [])


class RaceOnePushStatusTest(unittest.TestCase):
    """纯 push 模式推送全失败 = 任务失败（元数据未落地不能算成功）。"""

    class Args:
        no_probe = True
        no_images = True
        force_images = False
        image_policy = "keep"
        sink = "push"
        fnos_url = "http://x"
        fnos_token = "T"
        meta_dir = ""
        root = ""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix="xieyin-ro-"))
        self.pt = self.tmp / "pt" / "ADN-888-UC"
        self.pt.mkdir(parents=True)
        self.video = self.pt / "a@GROUP.mp4"
        self.video.write_bytes(b"")
        self.orig_scrape = rs.scrape_source
        self.orig_push = fm.push_meta
        self.orig_mark = fm.mark_pushed
        fm.mark_pushed = lambda v: None
        rs.scrape_source = lambda n, code, cfg: {"code": code, "title": "t"}

    def tearDown(self):
        rs.scrape_source = self.orig_scrape
        fm.push_meta = self.orig_push
        fm.mark_pushed = self.orig_mark
        shutil.rmtree(self.tmp, ignore_errors=True)

    def test_all_pushes_fail_returns_fail(self):
        fm.push_meta = lambda b, t, p, timeout=15: (False, "连接失败")
        log = open(os.devnull, "w")
        try:
            msg = rs.race_one("ADN-888", [{"name": "x", "ips": [], "mode": ""}],
                              self.pt, self.video, self.Args(), log)
        finally:
            log.close()
        self.assertTrue(msg.startswith("FAIL"), msg)

    def test_push_ok_returns_ok(self):
        fm.push_meta = lambda b, t, p, timeout=15: (True, "success")
        log = open(os.devnull, "w")
        try:
            msg = rs.race_one("ADN-888", [{"name": "x", "ips": [], "mode": ""}],
                              self.pt, self.video, self.Args(), log)
        finally:
            log.close()
        self.assertTrue(msg.startswith("OK"), msg)


JAVDBASE_DETAIL = """
<title>SSIS-001 - Sayaka Otoshiro, Tsukasa Aoi - JAV Database</title>
<h1 class="entry-title">SSIS-001 禁欲の果てに彼女のルームメイト2人と</h1>
<p class="mb-1"><b>Content ID: </b>ssis00001</p>
<p class="mb-1"><b>Release Date: </b>2021-02-18</p>
<p class="mb-1"><b>Runtime: </b>147 (HD: 147) min.</p>
<p class="mb-1"><b>Studio: </b><span class="btn btn-primary btn-sm"><a href="https://www.javdatabase.com/studios/s1-no-1-style/" rel="tag">S1 NO.1 STYLE</a></span></p>
<p class="mb-1"><b>Idol(s)/Actress(es): </b> <span class="btn btn-primary btn-sm"><a href="https://www.javdatabase.com/idols/sayaka-otoshiro/">Sayaka Otoshiro</a></span> <span class="btn btn-primary btn-sm"><a href="https://www.javdatabase.com/idols/tsukasa-aoi/">Tsukasa Aoi</a></span></p>
<p class="mb-1"><b>Director: </b><span class="btn btn-primary btn-sm"><a href="https://www.javdatabase.com/directors/ichigohara/" rel="tag">Ichigohara</a></span></p>
<p class="mb-1"><b>Genre(s): </b><span class="btn btn-primary btn-sm"><a href="https://www.javdatabase.com/genres/creampie/" rel="tag">Creampie</a></span> <span class="btn btn-primary btn-sm"><a href="https://www.javdatabase.com/genres/incest/" rel="tag">Incest</a></span></p>
"""

JAVBUS_PAGE = """
<h3>ADN-451 義父に10秒だけの約束で挿入を許したら… 三宮つばき</h3>
<p><span class="header">識別碼:</span> <span style="color:#FF0000;">ADN-451</span></p>
<p><span class="header">發行日期:</span> 2023-03-07</p>
<p><span class="header">長度:</span> 120分鐘</p>
<p><span class="header">導演:</span> <a href="/director/aaa">絵地メコン</a></p>
<p><span class="header">製作商:</span> <a href="/studio/aaa">アタッカーズ</a></p>
<p><span class="header">發行商:</span> <a href="/label/aaa">ATTACKERS</a></p>
<p><span class="header">系列:</span> <a href="/series/aaa">義父に…</a></p>
<p><span class="header">類別:</span> <a href="/genre/aaa">已婚婦女</a> <a href="/genre/bbb">亂倫</a></p>
<div id="star_111"><div class="star-name"><a href="/star/aaa" title="三宮つばき">三宮つばき</a></div></div>
<a class="bigImage" href="https://www.javbus.com/pics/cover/abc_big.jpg"><img src="x"></a>
<meta name="description" content="義父に10秒だけの約束…">
"""


class CollectUnitsStemTest(unittest.TestCase):
    """1.8.x：平铺目录的视频文件名必须去掉扩展名再解析番号
    （KSBJ115C.mp4 带 .mp4 时 code_of 会因尾部非数字而失败）。"""

    def test_no_dash_suffix_video_in_flat_folder(self):
        d = Path(tempfile.mkdtemp(prefix="xieyin-stem-"))
        for n in ("KSBJ115C.mp4", "JUR-819-UC.mp4", "ROE-526-C.mp4"):
            (d / n).touch()
        units = j.collect_units([d])
        codes = sorted(u[2] for u in units)
        self.assertIn("KSBJ-115", codes)
        self.assertEqual(len(units), 3)


class CodeOfWildFormTest(unittest.TestCase):
    """1.7.4：无横线+尾缀形态（KSBJ115C -> KSBJ-115）也要解析出来。"""

    def test_no_dash_trailing_suffix(self):
        self.assertEqual(j.code_of("KSBJ115C"), "KSBJ-115")
        self.assertEqual(j.code_of("ksbj115uc"), "KSBJ-115")

    def test_existing_forms_unchanged(self):
        self.assertEqual(j.code_of("ALDN-586-C"), "ALDN-586")
        self.assertEqual(j.code_of("KSBJ-411_FHD_CH"), "KSBJ-411")
        self.assertEqual(j.code_of("98t.tv"), "")


class AliasScopeTest(unittest.TestCase):
    """1.7.4：无前缀别名（poster.jpg/folder.jpg）只在单视频目录写——
    平铺/多视频目录里别名会互相覆盖（实测：备份/poster.jpg 错成最后一条目的图）。"""

    def _args(self):
        import argparse
        return argparse.Namespace(no_probe=True, no_images=False,
                                  image_policy="replace", force_images=True)

    def _d(self):
        return {"code": "AAA-001", "title": "t", "actors": [], "genres": []}

    def test_multi_video_folder_no_alias(self):
        folder = Path(tempfile.mkdtemp(prefix="xieyin-alias-"))
        v1 = folder / "AAA-001.mp4"; v1.touch()
        v2 = folder / "AAA-002.mp4"; v2.touch()
        cov = b"\xff\xd8\xff" + b"x" * 12000
        rs.write_artifacts(self._d(), folder, v1, self._args(), cov)
        self.assertTrue((folder / "AAA-001-poster.jpg").exists())
        self.assertFalse((folder / "poster.jpg").exists())
        self.assertFalse((folder / "folder.jpg").exists())

    def test_single_video_folder_keeps_alias(self):
        folder = Path(tempfile.mkdtemp(prefix="xieyin-alias-"))
        v1 = folder / "AAA-001.mp4"; v1.touch()
        cov = b"\xff\xd8\xff" + b"x" * 12000
        rs.write_artifacts(self._d(), folder, v1, self._args(), cov)
        self.assertTrue((folder / "poster.jpg").exists())
        self.assertTrue((folder / "folder.jpg").exists())


class FolderCollectDedupeTest(unittest.TestCase):
    """1.7.3：平铺布局（根目录直接放视频）单元不得翻倍——
    scan 的叶子枚举已含根目录时，main 的 [base]+folders 前置会造成每视频刮两遍。"""

    def _mk(self):
        root = Path(tempfile.mkdtemp(prefix="xieyin-flat-"))
        for n in ("AAA-001.mp4", "bbb-002-C.mp4", "CCC-003.mkv"):
            (root / n).touch()
        (root / "sub").mkdir()
        (root / "sub" / "DDD-004.mp4").touch()
        return root

    def test_flat_root_units_not_doubled(self):
        root = self._mk()
        folders = rs.folders_for(root, [], str(root))
        units = j.collect_units(folders)
        codes = sorted(u[2] for u in units)
        self.assertEqual(codes, ["AAA-001", "BBB-002", "CCC-003", "DDD-004"])

    def test_subdir_layout_unchanged(self):
        root = self._mk()
        (root / "AAA-001.mp4").unlink()
        (root / "bbb-002-C.mp4").unlink()
        (root / "CCC-003.mkv").unlink()
        folders = rs.folders_for(root, [], str(root))
        units = j.collect_units(folders)
        self.assertEqual([u[2] for u in units], ["DDD-004"])


class JavDatabaseParseTest(unittest.TestCase):
    """1.7.2：javdatabase（tier-2 备胎源）解析。"""

    def test_fields(self):
        from javdatabase_parse import parse_javdatabase
        d = parse_javdatabase(JAVDBASE_DETAIL.encode(), "SSIS-001")
        self.assertEqual(d["code"], "SSIS-001")
        self.assertIn("禁欲の果て", d["title"])
        self.assertEqual(d["releasedate"], "2021-02-18")
        self.assertEqual(d["runtime"], "147")
        self.assertEqual(d["studio"], "S1 NO.1 STYLE")
        self.assertEqual(d["director"], "Ichigohara")
        self.assertEqual(d["actors"], ["Sayaka Otoshiro", "Tsukasa Aoi"])
        self.assertEqual(d["genres"], ["Creampie", "Incest"])

    def test_compliance_no_links(self):
        from javdatabase_parse import parse_javdatabase
        raw = json.dumps(parse_javdatabase(JAVDBASE_DETAIL.encode(), "SSIS-001"),
                         ensure_ascii=False).lower()
        self.assertEqual(raw.count("magnet"), 0)
        self.assertEqual(raw.count("torrent"), 0)


class SourceDispatchTest(unittest.TestCase):
    """1.7.2：race 集成 javbus（固定IP直连）与 javdatabase（tier-2）。"""

    def test_javbus_branch(self):
        orig = rs.fetch_javbus
        rs.fetch_javbus = lambda code, ips: (200, JAVBUS_PAGE.encode())
        try:
            d = rs.scrape_source("javbus", "ADN-451", {"ips": ["1.2.3.4"]})
        finally:
            rs.fetch_javbus = orig
        self.assertEqual(d["code"], "ADN-451")
        self.assertEqual(d["studio"], "アタッカーズ")
        self.assertEqual(d["cover"], "https://www.javbus.com/pics/cover/abc_big.jpg")
        self.assertEqual(d["actors"], ["三宮つばき"])

    def test_javbus_wrong_code_rejected(self):
        orig = rs.fetch_javbus
        rs.fetch_javbus = lambda code, ips: (200, JAVBUS_PAGE.replace("ADN-451", "ZZZ-999").encode())
        try:
            with self.assertRaises(RuntimeError):
                rs.scrape_source("javbus", "ADN-451", {"ips": []})
        finally:
            rs.fetch_javbus = orig

    def test_javdatabase_branch(self):
        orig = rs.rc.fetch_multi
        rs.rc.fetch_multi = lambda host, path, ips, timeout, **kw: (200, 22981, JAVDBASE_DETAIL.encode(), "dns")
        try:
            d = rs.scrape_source("javdatabase", "SSIS-001", {"ips": ["1.2.3.4"]})
        finally:
            rs.rc.fetch_multi = orig
        self.assertEqual(d["studio"], "S1 NO.1 STYLE")
        self.assertEqual(d["_via"], "tier-2备胎")

    def test_pool_has_three_sources(self):
        names = {s["name"] for s in rc.SOURCES}
        self.assertTrue({"javbus", "javdatabase", "javdb580"} <= names)


class NfoArtTagsTest(unittest.TestCase):
    """1.6.1：nfo <art> 节——媒体服务器靠它把本地图片关联到影片（“无封面”根因之一）。"""

    D = {"code": "ADN-556", "title": "t", "actors": []}

    def test_art_block_with_stem(self):
        xml = j.build_nfo(dict(self.D), {}, "F", stem="ADN-556-C")
        self.assertIn("<art>", xml)
        self.assertIn("<poster>ADN-556-C-poster.jpg</poster>", xml)
        self.assertIn("<fanart>ADN-556-C-fanart.jpg</fanart>", xml)
        self.assertIn("<thumb>ADN-556-C-thumb.jpg</thumb>", xml)
        # 1.8.3：不再声明无前缀 poster.jpg —— 平铺目录里它只可能属于其中一条，
        # 飞牛会据此把别的条目的封面错配过来（实测 5 个 JUR 共用一张）
        self.assertNotIn("<poster>poster.jpg</poster>", xml)

    def test_legacy_thumb_fanart_tags(self):
        xml = j.build_nfo(dict(self.D), {}, "F", stem="ADN-556-C")
        self.assertIn('<thumb aspect="poster"', xml)
        self.assertIn("<fanart>", xml)

    def test_no_art_without_stem(self):
        xml = j.build_nfo(dict(self.D), {}, "F")
        self.assertNotIn("<art>", xml)

    def test_art_names_xml_escaped(self):
        xml = j.build_nfo(dict(self.D), {}, "F", stem="A&B<c>")
        self.assertIn("A&amp;B&lt;c&gt;-poster.jpg", xml)


class CropPosterTest(unittest.TestCase):
    """1.7.1：横版双封面合成图（如 DMM pl 800x538）必须取右半张正封面，
    不再中间裁出『两封面各一半+剧中图』。"""

    def _img(self, w, h, left_color, right_color=None):
        from PIL import Image
        im = Image.new("RGB", (w, h), left_color)
        if right_color:
            im.paste(Image.new("RGB", (w // 2, h), right_color), (w // 2, 0))
        return im

    def test_landscape_composite_takes_right_half(self):
        import tempfile
        from PIL import Image
        src = tempfile.mktemp(suffix=".jpg")
        dst = tempfile.mktemp(suffix=".jpg")
        # 左黑右白的 800x538 合成图
        self._img(800, 538, (0, 0, 0), (255, 255, 255)).save(src, "JPEG")
        self.assertTrue(j.crop_poster(src, dst))
        out = Image.open(dst)
        self.assertEqual(out.size, (379, 538))
        px = out.getpixel((189, 269))   # 中心点应在右半（白）
        self.assertGreater(px[0], 200, "中心应取到右半张（白），实际 %s" % (px,))

    def test_portrait_used_directly(self):
        import tempfile
        from PIL import Image
        src = tempfile.mktemp(suffix=".jpg")
        dst = tempfile.mktemp(suffix=".jpg")
        self._img(500, 538, (120, 120, 120)).save(src, "JPEG")
        self.assertTrue(j.crop_poster(src, dst))
        px = Image.open(dst).getpixel((189, 269))
        self.assertGreater(px[0], 60)

    def test_small_portrait_upscales(self):
        import tempfile
        from PIL import Image
        src = tempfile.mktemp(suffix=".jpg")
        dst = tempfile.mktemp(suffix=".jpg")
        self._img(147, 200, (90, 90, 90)).save(src, "JPEG")
        self.assertTrue(j.crop_poster(src, dst))
        self.assertEqual(Image.open(dst).size, (379, 538))


class CoverFallbackTest(unittest.TestCase):
    """1.6.1：封面下载多候选+重试+失败 WARN(E001)（图床间歇可达时不再静默丢封面）。"""

    def _patch_download(self, fake):
        orig = rs.rc.download_cover
        rs.rc.download_cover = fake
        return orig

    def test_tries_all_candidates_in_order(self):
        calls = []

        def fake(u, timeout=22):
            calls.append(u)
            return b"\xff\xd8\xff" + b"x" * 20000 if "second" in u else None

        orig = self._patch_download(fake)
        try:
            cov, used = rs.fetch_cover(
                ["http://h/first.jpg", "http://h/second.jpg"], None, attempts=1)
        finally:
            rs.rc.download_cover = orig
        self.assertIsNotNone(cov)
        self.assertEqual(used, "http://h/second.jpg")
        self.assertEqual(calls, ["http://h/first.jpg", "http://h/second.jpg"])

    def test_failure_logs_warn_e001(self):
        import io
        log = io.StringIO()
        orig = self._patch_download(lambda u, timeout=22: None)
        try:
            cov, used = rs.fetch_cover(["http://h/a.jpg"], log, attempts=2)
        finally:
            rs.rc.download_cover = orig
        self.assertIsNone(cov)
        out = log.getvalue()
        self.assertIn("E001", out)
        self.assertIn("封面", out)

    def test_candidates_dedupe_pick_first(self):
        urls = rs.cover_candidates(
            ["http://c0.jdbstatic.com/x.jpg", "https://pics.dmm.co.jp/a/adnps.jpg"],
            {"cover": "http://c0.jdbstatic.com/x.jpg"})
        self.assertEqual(urls[0], "http://c0.jdbstatic.com/x.jpg")  # 择优非 DMM 优先
        self.assertEqual(len(urls), 2)                              # merged.cover 去重

    def test_candidates_add_dmm_derived(self):
        urls = rs.cover_candidates(
            ["http://c0.jdbstatic.com/x.jpg"], {"code": "ADN-556"})
        # 1.8.6：DMM 双镜像域名（pics.dmm.com + imgsrc.dmm.com），pl 大图优先
        self.assertIn("https://pics.dmm.com/digital/video/adn00556/adn00556pl.jpg", urls)
        self.assertIn("https://imgsrc.dmm.com/pics/digital/video/adn00556/adn00556pl.jpg", urls)
        self.assertIn("https://pics.dmm.com/digital/video/adn00556/adn00556ps.jpg", urls)
        self.assertFalse(any("pics.dmm.co.jp" in u for u in urls),
                         "不得再生成 .co.jp 候选（SNI 被阻断）")
        # 1.9.3：已知 cid 时 DMM pl 提到首位（实测 167KB/2.7s，
        # jdbstatic 185KB 但需 15s），jdbstatic 降为兜底
        self.assertEqual(urls[0],
                         "https://pics.dmm.com/digital/video/adn00556/adn00556pl.jpg")
        self.assertIn("http://c0.jdbstatic.com/x.jpg", urls)
        # pl（清晰正封面）必须先于 ps（低像素缩略图）
        pl = urls.index("https://pics.dmm.com/digital/video/adn00556/adn00556pl.jpg")
        ps = urls.index("https://pics.dmm.com/digital/video/adn00556/adn00556ps.jpg")
        self.assertLess(pl, ps)


class DmmDomainRemapTest(unittest.TestCase):
    """1.8.4：下载前把 pics.dmm.co.jp 重映射到 pics.dmm.com（同 CDN，SNI 未封）。"""

    def test_download_cover_remaps_domain(self):
        import race_scrape as rs
        seen = []

        def fake_curl(host, path, ip, timeout, referer=None):
            seen.append(host)
            return 0, 0, b""

        orig = rs.rc.curl
        rs.rc.curl = fake_curl
        try:
            rs.rc.download_cover("https://pics.dmm.co.jp/digital/video/x/xps.jpg")
        finally:
            rs.rc.curl = orig
        self.assertTrue(seen)
        self.assertTrue(all(h == "pics.dmm.com" for h in seen), seen)

    def test_pick_helpers_treat_both_domains_as_dmm(self):
        import race_scrape as rs
        # DMM 图排在非 DMM 之后（择优），.com 也算 DMM
        self.assertEqual(rs.pick_poster_url(
            ["https://pics.dmm.com/a/aps.jpg", "https://c0.jdbstatic.com/b.jpg"]),
            "https://c0.jdbstatic.com/b.jpg")
        # backdrop: ps -> pl 推导，两个域名都认
        self.assertEqual(rs.pick_backdrop_url(["https://pics.dmm.com/a/aps.jpg"]),
                         "https://pics.dmm.com/a/apl.jpg")

    def test_candidates_no_crash_without_code(self):
        urls = rs.cover_candidates([], {"title": "t"})
        self.assertEqual(urls, [])

    def test_ok_line_shows_cover_status(self):
        src = inspect.getsource(rs.race_one)
        self.assertIn(" | 封面 ", src)   # OK 行必须能区分有无封面


if __name__ == "__main__":

    unittest.main(verbosity=2)
