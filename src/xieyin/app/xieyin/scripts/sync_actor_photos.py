#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""给飞牛 person 表补演员头像（JavBus 女优图，走官方图片上传接口）。

默认 dry-run：只打印「谁 -> 哪个番号 -> 哪个图 URL」，不下载、不上传。
--write 才真正下图 + upload + saveEditDetail。

注意：这是访问 JavBus 页面拿演员图，按番号去重、串行、默认每条间隔 2s；
403/429/HTTP 0 立即停止，避免批量把源站打急。
"""
from __future__ import annotations

import argparse
import os
import re
import sqlite3
import sys
import tempfile
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import actor_names as an
import actor_photo_sources as aps
import jav_scrape as j
import mbd_rebuild as m


def db_path() -> str:
    for d in ("/usr/local/apps/@appdata/trim.media/database",
              "/vol1/@appdata/trim.media/database"):
        p = os.path.join(d, "trimmedia.db")
        if os.path.exists(p):
            return p
    return ""


def nfo_actor_names(root: str) -> set:
    """只认 NFO <actor> 里真实出现的演员，过滤飞牛 DB 里的脏人物/男演员。"""
    out = set()
    if not root:
        return out
    for dp, dns, fns in os.walk(root):
        dns[:] = [d for d in dns if d != "#recycle"]
        for f in fns:
            if not f.lower().endswith(".nfo"):
                continue
            try:
                text = open(os.path.join(dp, f), encoding="utf-8-sig", errors="replace").read()
            except OSError:
                continue
            for m in re.finditer(r"<actor>.*?</actor>", text, re.S):
                nm = re.search(r"<name>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</name>", m.group(0), re.S)
                if not nm:
                    continue
                raw = (nm.group(1) if nm.group(1) is not None else nm.group(2) or "").strip()
                n = an.normalize(raw)
                if n:
                    out.add(n)
    return out


def missing_persons(db: str, root: str = "", only_names=None) -> list:
    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=8)
    con.row_factory = sqlite3.Row
    rows = [dict(r) for r in con.execute(
        "select p.guid, p.name, i.guid item_guid, i.path item_path, im.path media_path "
        "from person p "
        "join item_person ip on ip.person_guid=p.guid and ip.job='Actor' "
        "join item i on i.guid=ip.item_guid "
        "left join item_media im on im.item_guid=i.guid "
        "where p.profile_path is null or p.profile_path='' "
        "order by p.name")]
    con.close()
    # 每个 person 选一个最合适的番号：目录名里包含演员名的优先
    root_n = os.path.normpath(root).rstrip("/") if root else ""
    by_person = {}
    for r in rows:
        name = r["name"]
        path = r.get("media_path") or r.get("item_path") or ""
        if root_n:
            pn = os.path.normpath(path)
            if not (pn == root_n or pn.startswith(root_n + "/")):
                continue
        if only_names is not None and an.normalize(name) not in only_names:
            continue
        folder = os.path.basename(os.path.dirname(path))
        code = j.code_of(folder) or j.code_of(os.path.splitext(os.path.basename(path))[0])
        if not code:
            continue
        score = 2 if name and name in folder else 1
        cur = by_person.get(r["guid"])
        if not cur or score > cur["score"]:
            by_person[r["guid"]] = {"guid": r["guid"], "name": name, "code": code,
                                    "folder": folder, "score": score}
    return list(by_person.values())


def fetch_page(sess, code: str):
    st, body = sess.request("/" + code, referer="https://www.javbus.com/")
    if st == 404 or b"404 Page Not Found" in (body or b"")[:2000]:
        st, body = sess.request("/search/" + code, referer="https://www.javbus.com/")
        m = re.search(rb'<a class="movie-box" href="([^"]+)"', body or b"")
        if not m:
            return 0, b""
        path = m.group(1).decode("utf-8", "ignore")
        st, body = sess.request(path, referer="https://www.javbus.com/")
    return st, body


def safe_name(name: str) -> str:
    n = re.sub(r'[\\/:*?"<>|]', "_", name or "actor").strip()
    return n[:60] or "actor"


def find_poster(item_path: str) -> str:
    """同目录里优先找本视频封面，其次通用 poster/folder。"""
    if not item_path:
        return ""
    folder = os.path.dirname(item_path)
    stem = os.path.splitext(os.path.basename(item_path))[0]
    cands = [os.path.join(folder, stem + "-poster.jpg"),
             os.path.join(folder, stem + "-fanart.jpg"),
             os.path.join(folder, "poster.jpg"),
             os.path.join(folder, "folder.jpg"),
             os.path.join(folder, "cover.jpg")]
    for p in cands:
        if os.path.isfile(p) and os.path.getsize(p) > 1000:
            return p
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--limit", type=int, default=5, help="最多处理几个番号（不是人数）")
    ap.add_argument("--only", default="", help="只处理名字包含该关键词的演员")
    ap.add_argument("--root", default="", help="只处理该媒体目录下的演员（避免误改其它媒体库）")
    ap.add_argument("--sleep", type=float, default=2.0)
    ap.add_argument("--write", action="store_true")
    args = ap.parse_args()

    db = db_path()
    if not db:
        print("!! 找不到 trimmedia.db")
        return 1
    names = nfo_actor_names(args.root) if args.root else set()
    persons = missing_persons(db, args.root, names if args.root else None)
    if args.only:
        persons = [p for p in persons if args.only.lower() in (p.get("name") or "").lower()]
    if not persons:
        print("所有 Actor 都已有头像")
        return 0

    # 按番号分组，一个页面只抓一次
    groups = {}
    for p in persons:
        groups.setdefault(p["code"], []).append(p)
    codes = sorted(groups)[:max(1, args.limit)]
    print("缺头像 Actor %d 人，待抓番号 %d 个（本次 %d）" %
          (len(persons), len(groups), len(codes)))

    ts = m._admin_tokens()
    client = m.Client(ts[0]) if ts and args.write else None
    if args.write and client is None:
        print("!! 没有飞牛管理员 token")
        return 1

    sess = j.Session(validate=False)
    sess.ips = ["104.25.112.111", "172.67.80.44", "104.25.111.111"]
    outdir = os.path.join(tempfile.gettempdir(), "xieyin_actor_photos")
    os.makedirs(outdir, exist_ok=True)

    ok = fail = skip = 0
    for code in codes:
        try:
            st, body = fetch_page(sess, code)
            if st in (403, 429):
                print("!! %s HTTP %s，停止（疑似限流/封禁）" % (code, st))
                break
            if st != 200 or not body:
                print("  SKIP", code, "HTTP", st)
                time.sleep(max(0, args.sleep))
                continue
            page_url = "https://www.javbus.com/" + code
            d = j.parse_page(body.decode("utf-8", "ignore"))
            imgs = {x["name"]: x["url"] for x in d.get("actor_images", []) if x.get("url")}
            print("PAGE", code, "演员图", list(imgs.keys()))
            for p in groups[code]:
                url = imgs.get(p["name"]) or ""
                if url and aps.is_placeholder(url):
                    print("  FALLBACK", p["name"], "DMM 无头像占位图 -> AV-League")
                    url = ""
                raw, used, source = aps.resolve(
                    p["name"], url,
                    primary_fetch=lambda u: sess.fetch_url(u, referer=page_url),
                    code=code)
                if not raw:
                    # 最后兜底：用影片封面裁一张头像（DMM pl 取右半张正封面）
                    cover = find_poster(p.get("media_path") or p.get("item_path") or "")
                    cbytes = open(cover, "rb").read() if cover else b""
                    if not cbytes and d.get("cover"):
                        cover = d["cover"]
                        try:
                            cbytes = sess.fetch_url(cover, referer=page_url)
                        except Exception:
                            cbytes = b""
                        if not cbytes:
                            cbytes = aps.fetch(cover, referer=page_url)
                    raw = aps.avatar_from_cover(cbytes)
                    used, source = cover or "", "poster"
                if not raw:
                    print("  SKIP", p["name"], "主源、AV-League、Wikidata、百度百科、影片封面都没有可用头像")
                    skip += 1
                    continue
                print("  %s -> %s (%s)" % (p["name"], used, source))
                if not args.write:
                    ok += 1
                    continue
                fp = os.path.join(outdir, "%s-%s.jpg" % (code, safe_name(p["name"])))
                open(fp, "wb").write(raw)
                hp = client.upload_image(fp, "profile")
                r = m._request("POST", m.API + "/person/getEditDetail", client.token,
                               body={"guid": p["guid"]})
                if r.get("code") != 0:
                    raise RuntimeError("person/getEditDetail: %s" % r.get("msg"))
                data = dict(r.get("data") or {})
                data["guid"] = p["guid"]
                data["profile_path"] = hp
                data["profile_path_locked"] = (source != "poster")
                rr = m._request("POST", m.API + "/person/saveEditDetail", client.token,
                                body=data)
                if rr.get("code") != 0:
                    raise RuntimeError("person/saveEditDetail: %s" % rr.get("msg"))
                ok += 1
                print("  OK", p["name"], hp)
            time.sleep(max(0, args.sleep))
        except Exception as e:
            fail += 1
            print("!!", code, repr(e)[:160])
            break

    print("完成：成功 %d / 跳过 %d / 失败 %d%s" %
          (ok, skip, fail, "" if args.write else "（dry-run）"))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
