#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""app_server 模式矩阵与配置守卫的离线测试（1.7.0：以刮削为主，无 PT 保护）。"""
import os
import sys
import tempfile
import unittest
from unittest import mock

HERE = os.path.dirname(os.path.abspath(__file__))
APP = os.path.join(HERE, "..", "src", "xieyin", "app", "xieyin")
sys.path.insert(0, os.path.abspath(APP))
os.environ.setdefault("XIEYIN_DATA", tempfile.mkdtemp(prefix="xieyin-as-test-"))

import app_server as a  # noqa: E402


def fake_conf(en=True, url="http://127.0.0.1:1", exp=False, meta="/tmp/meta"):
    return {"media_root": tempfile.gettempdir(),
            "fnos": {"enabled": en, "export_nfo": exp,
                     "url": url, "token": "T", "meta_dir": meta}}


class SinkMatrixTest(unittest.TestCase):
    def _job(self, conf, kind="jav"):
        orig = a.load_conf
        a.load_conf = lambda: conf
        try:
            return a.build_job({"kind": kind, "root": tempfile.gettempdir(),
                                "dirs": [tempfile.gettempdir()]})
        finally:
            a.load_conf = orig

    def test_matrix(self):
        """1.7.0：inplace 恒在；push/export 仅在配置齐全时叠加，缺失即跳过。"""
        cases = [
            (fake_conf(), "push+inplace"),
            (fake_conf(en=False), "inplace"),
            (fake_conf(en=False, exp=True), "export+inplace"),
            (fake_conf(en=True, exp=True), "push+export+inplace"),
        ]
        for conf, want in cases:
            k, t_, cmd, env, meta = self._job(conf)
            sink = cmd[cmd.index("--sink") + 1]
            self.assertEqual(sink, want)
            self.assertEqual("--fnos-url" in cmd, "push" in want)
            self.assertEqual("--meta-dir" in cmd, "export" in want)

    def test_push_without_url_skips_not_raises(self):
        """目标未配置不再 E004 阻塞：跳过推送，刮削照常（以刮削为主）。"""
        conf = fake_conf(url="")
        k, t_, cmd, env, meta = self._job(conf)
        self.assertEqual(cmd[cmd.index("--sink") + 1], "inplace")
        self.assertNotIn("--fnos-url", cmd)

    def test_export_without_meta_dir_skips(self):
        conf = fake_conf(en=False, exp=True, meta="")
        k, t_, cmd, env, meta = self._job(conf)
        self.assertEqual(cmd[cmd.index("--sink") + 1], "inplace")
        self.assertNotIn("--meta-dir", cmd)

    def test_tmdb_branch_same_sink(self):
        k, t_, cmd, env, meta = self._job(fake_conf(en=True, exp=True), kind="tmdb")
        self.assertEqual(cmd[cmd.index("--sink") + 1], "push+export+inplace")


if __name__ == "__main__":
    unittest.main(verbosity=2)


class CoverSyncHookTest(unittest.TestCase):
    """1.8.1：刮削成功后自动同步封面缓存（不删库）；删库重建不再自动触发。"""

    def _job(self, kind="jav", meta=None, status="success"):
        j = a.Job(kind, "t", ["true"],
                  meta=meta if meta is not None else {"root": "/vol1/1000/Media"})
        j.status = status
        return j

    def test_success_calls_rebuild_and_logs(self):
        j = self._job()
        import img_cache_sync, mbd_rebuild
        with mock.patch.object(img_cache_sync, "sync_covers",
                               return_value=(True, ["[封面缓存] INFO 已同步"])) as sc, \
             mock.patch.object(mbd_rebuild, "rebuild") as rb:
            j._after_success()
        self.assertEqual(sc.call_count, 1)
        self.assertEqual(sc.call_args[0][0], "/vol1/1000/Media")
        rb.assert_not_called()          # 删库不再是自动行为
        self.assertIn("[封面缓存] INFO 已同步", j.lines)

    def test_dry_run_skips(self):
        j = self._job(meta={"root": "/vol1/1000/Media", "dry": True})
        import img_cache_sync
        with mock.patch.object(img_cache_sync, "sync_covers") as sc:
            j._after_success()
        sc.assert_not_called()

    def test_failed_or_other_kind_skips(self):
        import img_cache_sync
        for kind, status in (("jav", "failed"), ("jav", "stopped"),
                             ("net", "success"), ("clean", "success"),
                             ("check", "success")):
            j = self._job(kind=kind, status=status)
            with mock.patch.object(img_cache_sync, "sync_covers") as sc:
                j._after_success()
            self.assertEqual(sc.call_count, 0, "%s/%s" % (kind, status))

    def test_empty_root_skips(self):
        j = self._job(meta={"root": ""})
        import img_cache_sync
        with mock.patch.object(img_cache_sync, "sync_covers") as sc:
            j._after_success()
        sc.assert_not_called()

    def test_rebuild_exception_is_warn_only(self):
        j = self._job()
        import img_cache_sync
        with mock.patch.object(img_cache_sync, "sync_covers",
                               side_effect=RuntimeError("boom")):
            j._after_success()          # 不得冒泡
        self.assertTrue(any("WARN" in x and "boom" in x for x in j.lines))
