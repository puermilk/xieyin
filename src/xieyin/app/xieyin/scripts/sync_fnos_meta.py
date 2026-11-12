#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""把本地 NFO 的标题/简介同步进飞牛影视（官方 authx + saveEditDetail）。

为什么不用 /api/media/meta/update：
  实测 fnOS 当前版本没有这个路由；8005 返回的是前端 HTML。
  可用的是官方前端同款接口：
    POST /v/api/v1/item/getEditDetail   {"item_guid": "..."}
    POST /v/api/v1/item/saveEditDetail  {...编辑详情, title, overview}
  token 从飞牛自己的 user_token 表只读取得（需要 root，xieyin 服务本身是 root）。

流程：
  1. 可选 --scan：触发 root 所属媒体库扫描，更新改名后的 item_media.path
  2. 遍历 root 下的 NFO，找到同名视频
  3. 用 item_media.path 查 item_guid
  4. 读取 NFO 的 title/plot，写入飞牛 item 的 title/overview
  5. 回读校验；失败重试一次

默认 dry-run；--write 才写飞牛数据库。不修改任何本地 NFO/视频。
"""
from __future__ import annotations

import argparse
import html
import json
import os
import re
import sqlite3
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import mbd_rebuild as m  # noqa: E402
import media_ext  # noqa: E402

KANA_RE = re.compile(r"[\u3040-\u30ff]")
AD_NAME_RE = re.compile(
    r"直播|情報|情报|最新|地址|网址|網址|聚合|福利|广告|廣告|获取|獲取|社区|社區|"
    r"下载|下載|永久|备用|備用|发布|發布")
_BACKUPS: list[dict] = []


def tag_text(text: str, tag: str) -> str:
    mm = re.search(r"<%s>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</%s>" % (tag, tag),
                   text, re.S)
    if not mm:
        return ""
    return html.unescape((mm.group(1) if mm.group(1) is not None
                          else mm.group(2) or "")).strip()


def read_nfo(path: str) -> dict:
    with open(path, encoding="utf-8-sig", errors="replace") as f:
        s = f.read()
    return {
        "title": tag_text(s, "title"),
        "plot": tag_text(s, "plot") or tag_text(s, "originalplot"),
        "num": tag_text(s, "num"),
    }


def video_for_nfo(nfo_path: str) -> str:
    d = os.path.dirname(nfo_path)
    stem = os.path.splitext(os.path.basename(nfo_path))[0]
    for name in sorted(os.listdir(d)):
        p = os.path.join(d, name)
        if not os.path.isfile(p):
            continue
        if os.path.splitext(name)[0] == stem and media_ext.is_video(name):
            return p
    return ""


def db_path() -> str:
    d = m._db_dir()
    return os.path.join(d, "trimmedia.db") if d else ""


def item_guid_for_video(vpath: str) -> str:
    """优先按当前视频路径查 item_media；查不到再按目录+文件名回退。"""
    db = db_path()
    if not db or not os.path.exists(vpath):
        return ""
    try:
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=2)
        row = con.execute(
            "select item_guid from item_media where path=? order by update_time desc limit 1",
            (os.path.abspath(vpath),)).fetchone()
        if row:
            con.close()
            return str(row[0])
        # 改名后 scan 还没跑完：用同目录同 stem 的旧路径兜底
        d = os.path.dirname(os.path.abspath(vpath))
        stem = os.path.splitext(os.path.basename(vpath))[0]
        row = con.execute(
            "select item_guid from item_media where dir=? and path like ? "
            "order by update_time desc limit 1", (d, "%" + stem + "%")).fetchone()
        con.close()
        return str(row[0]) if row else ""
    except Exception:
        return ""


def libs_for_root(client, root: str) -> list:
    root = os.path.normpath(root).rstrip("/")
    out = []
    try:
        for lib in client.list():
            for d in (lib.get("dir_list") or []):
                dd = os.path.normpath(str(d)).rstrip("/")
                if root == dd or root.startswith(dd + "/") or dd.startswith(root + "/"):
                    out.append(lib)
                    break
    except Exception:
        pass
    return out


def sync_one(client, nfo_path: str, write: bool) -> str:
    nfo = read_nfo(nfo_path)
    title, plot = nfo["title"], nfo["plot"]
    if not title:
        return "SKIP 无标题"
    vpath = video_for_nfo(nfo_path)
    if not vpath:
        return "SKIP 无同名视频"
    guid = item_guid_for_video(vpath)
    if not guid:
        return "SKIP 飞牛未收录"
    d = client.get_edit_detail(guid)
    old_title = str(d.get("title") or "")
    old_plot = str(d.get("overview") or "")
    if old_title == title and old_plot == plot:
        return "SAME " + os.path.basename(nfo_path)
    if not write:
        return "DIFF %s | %s -> %s" % (os.path.basename(nfo_path),
                                      old_title[:28], title[:45])
    # 只改标题/简介，保留海报、锁定状态等其它字段
    _BACKUPS.append({"item_guid": guid, "nfo": nfo_path,
                     "old_title": old_title, "old_overview": old_plot,
                     "new_title": title, "new_overview": plot})
    d["item_guid"] = guid
    d["title"] = title
    d["overview"] = plot
    d["title_locked"] = False
    d["overview_locked"] = False
    last = ""
    for attempt in range(2):
        try:
            client.save_edit_detail(d)
        except Exception as e:
            last = str(e)
            time.sleep(1.0)
            continue
        time.sleep(0.8)
        try:
            chk = client.get_edit_detail(guid)
        except Exception as e:
            last = str(e)
            continue
        if str(chk.get("title") or "") == title and str(chk.get("overview") or "") == plot:
            return "OK   %s | %s" % (os.path.basename(nfo_path), title[:60])
        last = "写后回读不一致"
    return "FAIL " + os.path.basename(nfo_path) + " " + last


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--scan", action="store_true", help="先触发飞牛媒体库扫描")
    ap.add_argument("--only", default="")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    root = os.path.abspath(os.path.expanduser(args.root))
    if not os.path.isdir(root):
        print("目录不存在:", root)
        return 1
    client = m._pick_client()
    if client is None:
        print("!! 取不到飞牛管理员令牌（需要 root 读 trim.media 数据库）")
        return 2

    if args.scan:
        for lib in libs_for_root(client, root):
            try:
                client.scan(str(lib.get("guid") or ""))
                print("SCAN", lib.get("name"), lib.get("guid"))
            except Exception as e:
                print("WARN scan %s: %s" % (lib.get("name"), e))
        time.sleep(3)

    files = []
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "#recycle"]
        for f in sorted(fns):
            if not f.lower().endswith(".nfo"):
                continue
            if AD_NAME_RE.search(re.sub(r"\s", "", f)):
                continue
            files.append(os.path.join(dp, f))
    if args.only:
        files = [p for p in files if args.only.lower() in p.lower()]
    if args.limit:
        files = files[:args.limit]
    print("待同步 NFO %d 个（%s）" % (len(files), "写入" if args.write else "预览"))

    ok = skip = fail = 0
    for i, p in enumerate(files, 1):
        try:
            r = sync_one(client, p, args.write)
        except Exception as e:
            r = "ERR  %s" % e
        print("[%d/%d] %s  %s" % (i, len(files), os.path.relpath(p, root), r), flush=True)
        if r.startswith("OK"):
            ok += 1
        elif r.startswith(("SKIP", "SAME")):
            skip += 1
        else:
            fail += 1
    print("完成：成功 %d / 跳过 %d / 失败 %d%s" %
          (ok, skip, fail, "" if args.write else "（预览，未写飞牛）"))
    if args.write and _BACKUPS:
        try:
            base = os.environ.get("XIEYIN_DATA") or os.path.join(HERE, "..", "data")
            bd = os.path.join(base, "fnos_meta_backups")
            os.makedirs(bd, exist_ok=True)
            bp = os.path.join(bd, time.strftime("%Y%m%d-%H%M%S") + ".json")
            with open(bp, "w", encoding="utf-8") as f:
                json.dump({"root": root, "items": _BACKUPS}, f,
                          ensure_ascii=False, indent=2)
            print("备份：", bp)
        except Exception as e:
            print("WARN 备份失败：%s" % e)
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
