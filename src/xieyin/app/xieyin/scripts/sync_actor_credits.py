#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""把 NFO 里的 <actor> 写入飞牛 item_person / person。

飞牛只在首次建 item 时读 NFO 演员段；已存在条目 refresh 不会补演员。
现有 fix_actor_meta / sync_actor_photos 只维护已经存在的 item_person，
因此需要本脚本补上「NFO 有、飞牛 DB 没有」的演员关联。

默认 dry-run；--write 才写 trimmedia.db。写入前自动备份数据库。
头像仍交给 sync_actor_photos.py 走飞牛官方图片接口。
"""
from __future__ import annotations

import argparse
import json
import os
import sqlite3
import sys
import time
import uuid

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
import actor_names as an
import fix_actor_meta as fam
import media_ext


def _data_dir() -> str:
    return os.environ.get("XIEYIN_DATA") or os.path.abspath(os.path.join(HERE, "..", "data"))


def _mapping(con: sqlite3.Connection) -> dict:
    out = {}
    for r in con.execute(
            "select im.path vpath, i.guid iguid from item_media im "
            "join item i on i.guid=im.item_guid"):
        stem = os.path.splitext(os.path.basename(str(r["vpath"] or "")))[0]
        if stem:
            out[stem] = str(r["iguid"] or "")
    return out


def _find_person(con: sqlite3.Connection, name: str):
    return con.execute(
        "select guid,name,profile_path from person where name=? or original_name=? "
        "order by case when profile_path is not null and profile_path!='' then 0 else 1 end, guid "
        "limit 1", (name, name)).fetchone()


def _make_person(con: sqlite3.Connection, name: str) -> str:
    guid = uuid.uuid4().hex
    now = int(time.time())
    con.execute(
        "insert into person(guid,trim_id,imdb_id,tmdb_id,lan,pinyin,name,original_name,"
        "also_know_as,biography,know_for_department,images,profile_path,gender,"
        "create_time,update_time) values(?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
        (guid, "LOCAL_PERSON_" + guid, "", 0, "zh-CN",
         json.dumps({name: [name]}, ensure_ascii=False), name, name, "", "", "",
         None, "", None, now, now))
    return guid


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/vol1/1000/Media")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    db = fam.db_path()
    if not db:
        print("!! 找不到 trimmedia.db")
        return 1
    if not os.path.isdir(args.root):
        print("目录不存在:", args.root)
        return 2

    nfos = []
    for dp, dns, fns in os.walk(args.root):
        dns[:] = [d for d in dns if d not in ("#recycle", "_actors")]
        for f in sorted(fns):
            if f.lower().endswith(".nfo"):
                nfos.append(os.path.join(dp, f))
    if args.limit:
        nfos = nfos[:args.limit]

    src = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=8)
    src.row_factory = sqlite3.Row
    mapping = _mapping(src)
    src.close()

    backup = ""
    if args.write:
        bdir = os.path.join(_data_dir(), "actor_credits_backups")
        os.makedirs(bdir, exist_ok=True)
        backup = os.path.join(bdir, time.strftime("%Y%m%d-%H%M%S") + ".db")
        src = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=8)
        dst = sqlite3.connect(backup)
        src.backup(dst)
        dst.close()
        src.close()
        print("备份:", backup)

    con = sqlite3.connect(db, timeout=15)
    con.row_factory = sqlite3.Row
    linked = new_person = 0
    for nfo in nfos:
        folder = os.path.dirname(nfo)
        try:
            videos = [f for f in os.listdir(folder) if media_ext.is_video(f)]
        except OSError:
            continue
        if not videos:
            continue
        stem = os.path.splitext(videos[0])[0]
        item_guid = mapping.get(stem)
        if not item_guid:
            print("SKIP 无飞牛条目:", nfo)
            continue
        try:
            text = open(nfo, "rb").read().decode("utf-8-sig")
        except OSError:
            continue
        names = an.normalize_list(fam.nfo_actor_names(text))
        names = [n for n in names if n and not an.is_junk(n)]
        if not names:
            continue
        for name in names:
            row = _find_person(con, name)
            if row:
                pg = str(row["guid"])
            else:
                pg = _make_person(con, name)
                new_person += 1
                print("NEW  person %s -> %s" % (name, pg))
            exists = con.execute(
                "select 1 from item_person where item_guid=? and person_guid=? and job='Actor' "
                "limit 1", (item_guid, pg)).fetchone()
            if exists:
                continue
            order = con.execute(
                "select coalesce(max(\"order\"),0)+1 from item_person "
                "where item_guid=? and job='Actor'", (item_guid,)).fetchone()[0]
            print("LINK %s  %s -> %s" % (os.path.basename(folder), name, pg))
            linked += 1
            if args.write:
                con.execute(
                    "insert into item_person(item_guid,person_guid,role,job,\"order\",department) "
                    "values(?,?,?,?,?,?)",
                    (item_guid, pg, "", "Actor", int(order or 1), ""))

    if args.write:
        con.commit()
        print("完成：新增 person %d / 新增 Actor 关联 %d" % (new_person, linked))
    else:
        print("DRY-RUN：将新增 person/关联；加 --write 写入")
    con.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
