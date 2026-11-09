#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""img_cache_sync 离线测试：路径生成、扫描、幂等、主流程与失败降级。"""
import hashlib
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "src", "xieyin", "app", "xieyin", "scripts")
sys.path.insert(0, os.path.abspath(APP))
import img_cache_sync as s  # noqa: E402


class TargetRelTest(unittest.TestCase):
    def test_reuses_two_level_dir(self):
        self.assertEqual(s._target_rel("/35/12/local_a2f0.jpg", "H" * 32),
                         "/35/12/local_%s.jpg" % ("H" * 32))

    def test_generates_dir_when_missing(self):
        h = "abcdef" + "0" * 26
        self.assertEqual(s._target_rel("", h), "/ab/cd/local_%s.jpg" % h)


class CoversTest(unittest.TestCase):
    def test_scan_poster_files(self):
        d = tempfile.mkdtemp(prefix="xieyin-cov-")
        os.makedirs(os.path.join(d, "A"))
        open(os.path.join(d, "A", "AAA-001-poster.jpg"), "wb").write(b"x")
        open(os.path.join(d, "BBB-002-poster.jpg"), "wb").write(b"y")
        open(os.path.join(d, "A", "AAA-001-poster.nfo"), "w").write("z")
        got = s._covers(d)
        self.assertEqual(sorted(got), ["AAA-001", "BBB-002"])


class FakeClient:
    def __init__(self, detail=None, fail_save=False, fail_upload=False):
        self.detail = detail or {"item_guid": "", "title": "T", "posters": "/35/12/poster-old.webp"}
        self.fail_save = fail_save
        self.fail_upload = fail_upload
        self.calls = []

    def upload_image(self, path, image_type="poster"):
        self.calls.append(("upload", path, image_type))
        if self.fail_upload:
            raise RuntimeError("upload boom")
        return "/7c/10/upload_poster_abc123.webp"

    def get_edit_detail(self, g):
        self.calls.append(("get", g))
        return dict(self.detail)

    def save_edit_detail(self, p):
        self.calls.append(("save", p))
        if self.fail_save:
            raise RuntimeError("save boom")
        self.detail = dict(p)      # 模拟飞牛落库（供回读校验）


class SyncFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xieyin-sync-")
        self.imgroot = os.path.join(self.tmp, "img")
        os.makedirs(self.imgroot)
        self.cover = os.path.join(self.tmp, "AAA-001-poster.jpg")
        open(self.cover, "wb").write(b"\xff\xd8\xff" + b"x" * 500)
        self.mp = {"AAA-001": {"item_guid": "IG", "posters": "/35/12/poster-old.webp",
                               "cache_file": "", "img_root": self.imgroot}}

    def _run(self, client, mp=None, covers=None):
        with mock.patch.object(s, "_mapping", return_value=mp if mp is not None else self.mp), \
             mock.patch.object(s, "_covers", return_value=covers if covers is not None else {"AAA-001": self.cover}), \
             mock.patch.object(s.mdb, "_pick_client", return_value=client):
            return s.sync_covers(self.tmp, data_dir=self.tmp)

    def test_full_flow_uploads_then_updates_posters(self):
        c = FakeClient()
        ok, lines = self._run(c)
        self.assertTrue(ok)
        # upload -> getEdit -> save -> getEdit(回读校验)
        self.assertEqual([x[0] for x in c.calls][:3], ["upload", "get", "save"])
        self.assertEqual(c.calls[0][1], self.cover)
        self.assertEqual(c.calls[0][2], "poster")
        saved = c.calls[2][1]
        self.assertEqual(saved["posters"], "/7c/10/upload_poster_abc123.webp")
        self.assertEqual(saved["item_guid"], "IG")
        self.assertEqual(saved["title"], "T")          # 其它字段原样回传
        bdir = os.path.join(self.tmp, "img_cache_backups")
        self.assertEqual(len(os.listdir(bdir)), 1)
        with open(os.path.join(bdir, os.listdir(bdir)[0]), encoding="utf-8") as f:
            b = json.load(f)
        self.assertEqual(b["items"][0]["old_posters"], "/35/12/poster-old.webp")
        self.assertNotIn("token", json.dumps(b).lower())  # 凭据不落盘

    def test_idempotent_when_same_md5(self):
        import json as js
        h = hashlib.md5(open(self.cover, "rb").read()).hexdigest()
        mp = {"AAA-001": {**self.mp["AAA-001"],
                          "posters": "/7c/10/upload_poster_existing.webp"}}
        with open(os.path.join(self.tmp, "cover_sync.json"), "w", encoding="utf-8") as f:
            js.dump({"IG": {"stem": "AAA-001", "md5": h,
                            "posters": "/7c/10/upload_poster_existing.webp"}}, f)
        c = FakeClient()
        ok, lines = self._run(c, mp=mp)
        self.assertTrue(ok)
        self.assertEqual(c.calls, [])
        self.assertTrue(any("已一致 1" in x for x in lines))

    def test_reupload_when_local_cover_changed(self):
        """重刮换了封面（md5 变）→ 即使 posters 已是 upload_ 也要重传。"""
        import json as js
        mp = {"AAA-001": {**self.mp["AAA-001"],
                          "posters": "/7c/10/upload_poster_existing.webp"}}
        with open(os.path.join(self.tmp, "cover_sync.json"), "w", encoding="utf-8") as f:
            js.dump({"IG": {"stem": "AAA-001", "md5": "0" * 32,
                            "posters": "/7c/10/upload_poster_existing.webp"}}, f)
        c = FakeClient()
        ok, lines = self._run(c, mp=mp)
        self.assertTrue(ok)
        self.assertEqual([x[0] for x in c.calls][:3], ["upload", "get", "save"])
        self.assertEqual(c.calls[2][1]["posters"], "/7c/10/upload_poster_abc123.webp")
        # 状态文件应记录新 md5
        with open(os.path.join(self.tmp, "cover_sync.json"), encoding="utf-8") as f:
            st = json.load(f)
        self.assertEqual(st["IG"]["md5"], hashlib.md5(open(self.cover, "rb").read()).hexdigest())

    def test_no_mapping_skips_safely(self):
        c = FakeClient()
        ok, lines = self._run(c, mp={})
        self.assertTrue(ok)
        self.assertEqual(c.calls, [])
        self.assertTrue(any("跳过" in x for x in lines))

    def test_no_token_skips_safely(self):
        with mock.patch.object(s, "_mapping", return_value=self.mp), \
             mock.patch.object(s, "_covers", return_value={"AAA-001": self.cover}), \
             mock.patch.object(s.mdb, "_pick_client", return_value=None):
            ok, lines = s.sync_covers(self.tmp, data_dir=self.tmp)
        self.assertTrue(ok)
        self.assertTrue(any("跳过" in x for x in lines))

    def test_save_failure_warn_only(self):
        c = FakeClient(fail_save=True)
        ok, lines = self._run(c)
        self.assertFalse(ok)
        self.assertTrue(any("同步失败" in x for x in lines))

    def test_upload_failure_warn_only(self):
        c = FakeClient(fail_upload=True)
        ok, lines = self._run(c)
        self.assertFalse(ok)
        self.assertTrue(any("同步失败" in x for x in lines))
        self.assertNotIn("save", [x[0] for x in c.calls])


if __name__ == "__main__":
    unittest.main()
