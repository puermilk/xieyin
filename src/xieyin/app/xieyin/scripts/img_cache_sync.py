#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""刮削完成后，把封面直接同步进飞牛影视（不删库、不刷新、不重扫）。

为什么需要它（实测结论）：
  飞牛把每个条目的封面缓存成 /volX/trim.media/img/<xx>/<yy>/xxx.webp，
  后端只在 FirstScan（首次建 item）时读本地 NFO/封面；已存在 item 的任何
  mdb/refresh / item/refresh / 重扫都不会替换封面，而且图片响应带一年强缓存。

做法（全部走飞牛官方接口，飞牛全权接管图片）：
  1. 只读 trimmedia.db：item_media.path -> item.posters，拿到视频与 item 的映射。
  2. 扫描刮削 root 下的 <stem>-poster.jpg。
  3. POST /image/temp/upload（multipart: file + image_type=poster）上传封面——
     飞牛自行校验、转 webp、生成多尺寸缩略图，返回 hash_path。
  4. POST /item/getEditDetail 取完整编辑数据，只把 posters 改成该 hash_path，
     POST /item/saveEditDetail 回写。飞牛随即：
        processImageField: moved image from cache/img/temp/... to img/...
        processImageField: deleted old image ...      （旧截图自动清理）
     约 5 秒生效，客户端缓存因 URL 变化自然失效。
  5. 原 posters 备份到 <data>/img_cache_backups/，可随时回滚。

安全：只读飞牛 DB；图片只经官方上传接口；不动库配置 / 观看记录 /
      其它元数据字段；任何失败只写 WARN，不影响已成功的刮削任务。
"""
from __future__ import annotations

import glob
import hashlib
import json
import os
import sqlite3
import time

import mbd_rebuild as mdb

IMG_GLOBS = ("/vol*/trim.media/img", "/vol*/trim.media/cache/img")


def _db_path() -> str:
    d = mdb._db_dir()
    return os.path.join(d, "trimmedia.db") if d else ""


def _img_root_for(cache_file: str) -> str:
    """给定已存在的缓存文件，返回它所属的 img 根目录（保证新文件写在同卷）。"""
    if not cache_file:
        return ""
    for pat in IMG_GLOBS:
        for root in glob.glob(pat):
            if cache_file.startswith(root + "/"):
                return root
    return ""


def _any_img_root() -> str:
    for pat in IMG_GLOBS:
        roots = sorted(glob.glob(pat))
        if roots:
            return roots[0]
    return ""


def _mapping() -> dict:
    """只读 DB：video_stem -> {item_guid, posters, cache_file, img_root}"""
    p = _db_path()
    if not p or not os.path.exists(p):
        return {}
    try:
        con = sqlite3.connect("file:" + p + "?mode=ro", uri=True, timeout=1)
        con.row_factory = sqlite3.Row
        rows = con.execute(
            "SELECT im.path AS vpath, i.guid AS iguid, i.posters AS posters "
            "FROM item_media im JOIN item i ON i.guid = im.item_guid").fetchall()
        con.close()
    except Exception:
        return {}
    out = {}
    for r in rows:
        vp = str(r["vpath"] or "")
        posters = str(r["posters"] or "").split(",")[0].strip()
        stem = os.path.splitext(os.path.basename(vp))[0]
        if not stem:
            continue
        # posters 为空 = 该条目从未设置过封面（如首次入库时只有视频截图、
        # 刮削后也未补上）。必须纳入映射，否则本函数会把它永久跳过。
        cache = ""
        imgroot = ""
        for pat in IMG_GLOBS:
            for root in glob.glob(pat):
                cand = root + posters
                if os.path.isfile(cand):
                    cache, imgroot = cand, root
                    break
            if cache:
                break
        if not imgroot:
            imgroot = _any_img_root()
        out[stem] = {"item_guid": str(r["iguid"] or ""),
                     "posters": posters, "cache_file": cache,
                     "img_root": imgroot}
    return out


def _covers(root: str) -> dict:
    """扫描刮削目录：<stem>-poster.jpg -> 视频 stem"""
    out = {}
    for dirpath, dirs, files in os.walk(root):
        dirs[:] = [d for d in dirs if d != "#recycle"]
        for f in files:
            if not f.lower().endswith("-poster.jpg"):
                continue
            stem = f[:-len("-poster.jpg")]
            if stem:
                out[stem] = os.path.join(dirpath, f)
    return out


def _target_rel(old_posters: str, new_hash: str) -> str:
    """（保留给旧数据/外部调用）复用原两级目录，只换文件名。"""
    parts = [x for x in str(old_posters or "").split("/") if x]
    if len(parts) >= 3 and parts[0] and parts[1]:
        sub = "/%s/%s" % (parts[0], parts[1])
    else:
        sub = "/%s/%s" % (new_hash[:2], new_hash[2:4])
    return "%s/local_%s.jpg" % (sub, new_hash)


def sync_covers(root: str, data_dir: str = "", log=None) -> tuple[bool, list[str]]:
    """刮削完成后调用。返回 (是否全部成功, 日志行)。"""
    lines: list[str] = []

    def emit(s: str) -> None:
        lines.append(s)
        if log:
            try:
                log(s)
            except Exception:
                pass

    root = str(root or "").strip()
    if not root or not os.path.isdir(root):
        emit("[封面缓存] INFO 无有效刮削目录，跳过")
        return True, lines
    mp = _mapping()
    covers = _covers(root)
    matched = [s for s in covers if s in mp]
    if not mp or not covers:
        emit("[封面缓存] INFO 未找到飞牛影视条目映射或本地封面，跳过（不影响刮削）")
        return True, lines
    emit("[封面缓存] INFO 本地封面 %d 张，命中飞牛条目 %d 个" %
         (len(covers), len(matched)))

    c = mdb._pick_client()
    if c is None:
        emit("[封面缓存] WARN 未取得飞牛影视管理员令牌，跳过（不影响刮削结果）")
        return True, lines

    data_dir = data_dir or os.path.join(
        os.path.dirname(os.path.realpath(__file__)), "..", "data")
    # 上传状态：记录每个条目「上次上传的是哪张本地封面（md5）」
    # 重刮后本地封面若变了（例如从 ps 小图换成 pl 大图），md5 不同就重传。
    state_path = os.path.join(data_dir, "cover_sync.json")
    try:
        with open(state_path, encoding="utf-8") as f:
            state = json.load(f) or {}
    except Exception:
        state = {}
    backups = []
    ok_all, changed, skipped = True, 0, 0
    for stem in matched:
        info = mp[stem]
        cover = covers[stem]
        try:
            old = str(info["posters"] or "")
            try:
                with open(cover, "rb") as f:
                    md5_local = hashlib.md5(f.read()).hexdigest()
            except OSError as e:
                raise RuntimeError("读取封面失败: %s" % str(e)[:60])
            rec = state.get(info["item_guid"]) or {}
            if ("upload_poster_" in old and rec.get("md5") == md5_local
                    and rec.get("posters") == old):
                # 本地封面没变、飞牛仍指向上次上传的结果 → 无需重复上传
                skipped += 1
                continue
            # 批量连续 saveEditDetail 时飞牛偶发「返回成功但 posters 未落库」，
            # 因此每次提交后回读校验，未生效则重试（最多 2 轮）。
            hp = ""
            for attempt in range(2):
                hp = c.upload_image(cover, "poster")
                data = c.get_edit_detail(info["item_guid"])
                old = str(data.get("posters") or "")
                data["item_guid"] = info["item_guid"]
                data["posters"] = hp
                c.save_edit_detail(data)
                time.sleep(1.2)
                chk = c.get_edit_detail(info["item_guid"])
                if str(chk.get("posters") or "") == hp:
                    break
                if attempt == 0:
                    emit("[封面缓存] WARN %s 未生效，重试" % stem[:34])
                    time.sleep(1.5)
            else:
                raise RuntimeError("posters 提交 2 次仍未生效")
            backups.append({"item_guid": info["item_guid"], "stem": stem,
                            "old_posters": old, "new_posters": hp})
            state[info["item_guid"]] = {"stem": stem, "md5": md5_local,
                                        "posters": hp,
                                        "updated": time.strftime("%Y-%m-%d %H:%M:%S")}
            changed += 1
            emit("[封面缓存] INFO %s -> %s" % (stem[:34], hp))
        except Exception as e:
            ok_all = False
            emit("[封面缓存] WARN %s 同步失败：%s" % (stem[:34], str(e)[:90]))

    if changed:
        try:
            os.makedirs(data_dir, exist_ok=True)
            tmp = state_path + ".tmp"
            with open(tmp, "w", encoding="utf-8") as f:
                json.dump(state, f, ensure_ascii=False, indent=2)
            os.replace(tmp, state_path)
        except Exception as e:
            emit("[封面缓存] WARN 状态写入失败：%s" % str(e)[:80])

    if backups:
        try:
            bd = os.path.join(data_dir, "img_cache_backups")
            os.makedirs(bd, exist_ok=True)
            bp = os.path.join(bd, "%s.json" % time.strftime("%Y%m%d-%H%M%S"))
            with open(bp, "w", encoding="utf-8") as f:
                json.dump({"root": root, "items": backups}, f,
                          ensure_ascii=False, indent=2)
            emit("[封面缓存] INFO 原 posters 已备份：%s" % bp)
        except Exception as e:
            emit("[封面缓存] WARN 备份失败：%s" % str(e)[:80])
    emit("[封面缓存] INFO 完成：更新 %d / 已一致 %d / 失败 %d" %
         (changed, skipped, len(matched) - changed - skipped))
    return ok_all, lines


if __name__ == "__main__":
    import sys
    ok, out = sync_covers(sys.argv[1] if len(sys.argv) > 1 else "")
    for line in out:
        print(line)
    sys.exit(0 if ok else 1)
