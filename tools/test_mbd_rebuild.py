#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""mbd_rebuild 离线测试：签名、配置映射、路径匹配、删库重建主流程。"""
import json
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "src", "xieyin", "app", "xieyin", "scripts")
sys.path.insert(0, os.path.abspath(APP))
import mbd_rebuild as m  # noqa: E402


class SignTest(unittest.TestCase):
    def test_pu_sorted_and_pct20(self):
        self.assertEqual(m._pu({"b": "x y", "a": 1}), "a=1&b=x%20y")

    def test_authx_shape(self):
        with mock.patch.object(m.random, "randint", return_value=123456), \
             mock.patch.object(m.time, "time", return_value=1000.0):
            ax = m._authx("/v/api/v1/mdb/list", {"a": 1}, True)
        self.assertIn("nonce=123456", ax)
        self.assertIn("timestamp=1000000", ax)
        self.assertTrue(ax.startswith("nonce=") and "&sign=" in ax)
        self.assertEqual(len(ax.split("&sign=")[1]), 32)   # md5 hex

    def test_post_sign_uses_compact_json(self):
        """非 GET 的签名输入必须是实际发送的紧凑 JSON（DELETE 空 body = {}）"""
        with mock.patch.object(m.random, "randint", return_value=111111), \
             mock.patch.object(m.time, "time", return_value=2000.0):
            ax = m._authx("/v/api/v1/mdb/x", {}, False)
        expect = m._md5("_".join([m.SALT, "/v/api/v1/mdb/x", "111111",
                                  "2000000", m._md5("{}"), m.APIKEY]))
        self.assertIn("sign=" + expect, ax)


class PayloadTest(unittest.TestCase):
    def test_fields_and_bool_include_adult(self):
        d = {"guid": "G", "name": "AV", "category": "Movie",
             "dir_list": ["/vol1/1000/Media/"], "lan": "zh-CN",
             "include_adult": 1, "prefer_local_nfo": 1,
             "last_update_time": 123, "sort_num": 9,
             "refresh_disabled": False, "posters": ["/a.webp"]}
        p = m._create_payload(d)
        self.assertEqual(p["name"], "AV")
        self.assertEqual(p["dir_list"], ["/vol1/1000/Media/"])
        self.assertEqual(p["prefer_local_nfo"], 1)
        self.assertIs(p["include_adult"], True)      # 数字会 Internal Error
        self.assertNotIn("guid", p)
        self.assertNotIn("last_update_time", p)
        self.assertNotIn("sort_num", p)
        self.assertNotIn("refresh_disabled", p)

    def test_missing_include_adult_becomes_false(self):
        self.assertIs(m._create_payload({"name": "x"})["include_adult"], False)


class MatchTest(unittest.TestCase):
    def test_equal_and_child(self):
        self.assertTrue(m._matches("/vol1/1000/Media", ["/vol1/1000/Media/"]))
        self.assertTrue(m._matches("/vol1/1000/Media/sub", ["/vol1/1000/Media/"]))

    def test_unrelated(self):
        self.assertFalse(m._matches("/vol2/1000/OTHER", ["/vol1/1000/Media/"]))

    def test_lib_inside_root_is_not_a_match(self):
        """库目录只是 root 的子目录时不能删整个库（防误伤）"""
        self.assertFalse(m._matches("/vol2/1000", ["/vol1/1000/Media/"]))


class FakeClient:
    def __init__(self, libs, details, fail_create=0, fail_delete=False,
                 fail_scan=False):
        self.libs, self.cfg = libs, details
        self.fail_create, self.fail_delete, self.fail_scan = fail_create, fail_delete, fail_scan
        self.calls = []

    def list(self):
        self.calls.append(("list",)); return self.libs

    def details(self, g):
        self.calls.append(("details", g)); return self.cfg

    def delete(self, g):
        self.calls.append(("delete", g))
        if self.fail_delete: raise RuntimeError("del boom")

    def create(self, p):
        self.calls.append(("create",))
        if self.fail_create > 0:
            self.fail_create -= 1
            raise RuntimeError("create boom")
        return "NEWGUID"

    def scan(self, g):
        self.calls.append(("scan", g))
        if self.fail_scan: raise RuntimeError("scan boom")


LIB = {"guid": "OLDGUID", "name": "AV", "category": "Movie",
       "dir_list": ["/vol1/1000/Media/"], "prefer_local_nfo": 1}
CFG = dict(LIB, lan="zh-CN", include_adult=0, skip_filesize=0)


class RebuildFlowTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.mkdtemp(prefix="xieyin-mbd-")

    def _run(self, client, root="/vol1/1000/Media"):
        with mock.patch.object(m, "_pick_client", return_value=client):
            return m.rebuild(root, data_dir=self.tmp)

    def test_no_match_skips_destructive_calls(self):
        c = FakeClient([], CFG)
        ok, lines = self._run(c, root="/vol3/nope")
        self.assertTrue(ok)
        self.assertEqual(c.calls, [("list",)])
        self.assertTrue(any("未归属" in x for x in lines))

    def test_full_flow_order_and_backup(self):
        c = FakeClient([LIB], CFG)
        ok, lines = self._run(c)
        self.assertTrue(ok)
        self.assertEqual([x[0] for x in c.calls],
                         ["list", "details", "delete", "create", "scan"])
        self.assertEqual(c.calls[2][1], "OLDGUID")
        self.assertEqual(c.calls[4][1], "NEWGUID")
        backups = os.listdir(os.path.join(self.tmp, "mdb_backups"))
        self.assertEqual(len(backups), 1)
        with open(os.path.join(self.tmp, "mdb_backups", backups[0]),
                  encoding="utf-8") as f:
            b = json.load(f)
        self.assertEqual(b["guid"], "OLDGUID")
        self.assertEqual(b["create_payload"]["name"], "AV")
        self.assertNotIn("token", json.dumps(b).lower())   # 凭据绝不落盘

    def test_create_retry_then_success(self):
        c = FakeClient([LIB], CFG, fail_create=2)
        ok, lines = self._run(c)
        self.assertTrue(ok)
        self.assertEqual(sum(1 for x in c.calls if x[0] == "create"), 3)
        self.assertTrue(any("第 3 次" in x for x in lines))

    def test_create_total_failure_reports_backup(self):
        c = FakeClient([LIB], CFG, fail_create=9)
        ok, lines = self._run(c)
        self.assertFalse(ok)
        self.assertTrue(any("重建彻底失败" in x and "mdb_backups" in x for x in lines))

    def test_delete_failure_does_not_create(self):
        c = FakeClient([LIB], CFG, fail_delete=True)
        ok, lines = self._run(c)
        self.assertFalse(ok)
        self.assertNotIn("create", [x[0] for x in c.calls])

    def test_backup_failure_does_not_delete(self):
        c = FakeClient([LIB], CFG)
        with mock.patch.object(m, "_pick_client", return_value=c), \
             mock.patch.object(m, "_backup_path", side_effect=OSError("disk")):
            ok, lines = m.rebuild("/vol1/1000/Media", data_dir=self.tmp)
        self.assertFalse(ok)
        self.assertNotIn("delete", [x[0] for x in c.calls])
        self.assertTrue(any("备份失败" in x for x in lines))

    def test_no_token_skips_safely(self):
        with mock.patch.object(m, "_pick_client", return_value=None):
            ok, lines = m.rebuild("/vol1/1000/Media", data_dir=self.tmp)
        self.assertTrue(ok)
        self.assertTrue(any("跳过重建" in x for x in lines))

    def test_scan_failure_still_ok(self):
        c = FakeClient([LIB], CFG, fail_scan=True)
        ok, lines = self._run(c)
        self.assertTrue(ok)
        self.assertTrue(any("触发重扫失败" in x for x in lines))


if __name__ == "__main__":
    unittest.main()
