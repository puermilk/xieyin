#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""把飞牛已有的人物头像回写到 NFO 的 <actor><thumb>（绝对路径）。

用途：已有库的 person.profile_path 已经有头像，NFO 里还没有 <thumb>。
把飞牛缓存图复制到媒体库根目录 _actors/，NFO 引用该绝对路径；以后重扫/重建库
也能恢复演员头像。

默认 dry-run；--write 才复制图片和改 NFO。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import actor_names as an

IMG_ROOTS = (
    "/vol1/@appmeta/trim.media/img",
    "/vol2/@appmeta/trim.media/img",
    "/vol3/@appmeta/trim.media/img",
    "/vol1/trim.media/img",
    "/vol2/trim.media/img",
    "/vol3/trim.media/img",
    "/usr/local/apps/@appmeta/trim.media/img",
)


def image_file(profile_path: str) -> str:
    rel = str(profile_path or "").lstrip("/")
    if not rel:
        return ""
    for root in IMG_ROOTS:
        p = os.path.join(root, rel)
        if os.path.isfile(p):
            return p
    return ""
NFO_NAME_RE = re.compile(r"<name>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</name>", re.S)


def db_path() -> str:
    for d in ("/usr/local/apps/@appdata/trim.media/database",
              "/vol1/@appdata/trim.media/database"):
        p = os.path.join(d, "trimmedia.db")
        if os.path.exists(p):
            return p
    return ""


def safe(name: str) -> str:
    return re.sub(r'[\\/:*?"<>|]', "_", name or "actor")[:60] or "actor"


def actor_names(text: str) -> list:
    out = []
    for m in re.finditer(r"<actor>.*?</actor>", text, re.S):
        nm = NFO_NAME_RE.search(m.group(0))
        if nm:
            n = (nm.group(1) if nm.group(1) is not None else nm.group(2) or "").strip()
            n = an.normalize(n)
            if n and n not in out:
                out.append(n)
    return out


def blocks(names, photos, existing=None) -> str:
    existing = existing or {}
    out = []
    for i, n in enumerate(names, 1):
        b = ["  <actor>", "    <name><![CDATA[%s]]></name>" % n.replace("]]>", "]]]]><![CDATA[>")]
        thumb = existing.get(n) or photos.get(n)
        if thumb:
            b.append("    <thumb>%s</thumb>" % thumb)
        b += ["    <type>Actor</type>", "    <order>%d</order>" % i, "  </actor>"]
        out.append("\n".join(b))
    return "\n".join(out)


def existing_thumbs(text: str) -> dict:
    out = {}
    for m in re.finditer(r"<actor>.*?</actor>", text, re.S):
        b = m.group(0)
        nm = NFO_NAME_RE.search(b)
        th = re.search(r"<thumb>(.*?)</thumb>", b, re.S)
        if nm and th:
            n = (nm.group(1) if nm.group(1) is not None else nm.group(2) or "").strip()
            n = an.normalize(n)
            if n:
                out[n] = th.group(1).strip()
    return out


def local_photo(folder: str, stem: str, name: str, root: str) -> str:
    """优先用同目录的 <stem>-actor-<name>.jpg，其次 _actors/<name>.*。"""
    cands = [os.path.join(folder, stem + "-actor-" + safe(name) + ".jpg")]
    for base in (os.path.join(folder, "_actors"), os.path.join(root, "_actors")):
        for ext in (".jpg", ".jpeg", ".webp", ".png"):
            cands.append(os.path.join(base, safe(name) + ext))
    for p in cands:
        if os.path.isfile(p):
            return os.path.abspath(p)
    return ""


def replace_section(text: str, names, photos, existing=None) -> str:
    block = blocks(names, photos, existing)
    start = text.find("<actor>")
    end = text.rfind("</actor>")
    if start != -1 and end != -1:
        return text[:start] + block + ("\n" if block else "") + text[end + len("</actor>"):].lstrip("\n")
    if block:
        idx = text.rfind("</movie>")
        if idx != -1:
            return text[:idx] + block + "\n" + text[idx:]
    return text


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/vol1/1000/Media")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--only", default="", help="只处理路径包含该关键词的 NFO")
    args = ap.parse_args()

    db = db_path()
    if not db:
        print("!! 找不到 trimmedia.db")
        return 1
    con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=8)
    con.row_factory = sqlite3.Row
    persons = [dict(r) for r in con.execute(
        "select name,profile_path from person where profile_path is not null and profile_path!=''")]
    con.close()

    # 只建立「演员名 -> 飞牛头像文件」映射，不提前复制。
    # 之前遍历所有 person 把全库演员图都塞进当前 _actors，既慢又污染媒体目录。
    src_by_name = {}
    for p in persons:
        name = an.normalize(p["name"])
        src = image_file(p["profile_path"])
        if name and src:
            src_by_name.setdefault(name, src)

    actor_dir = os.path.join(args.root, "_actors")
    changed = 0
    for dp, dns, fns in os.walk(args.root):
        dns[:] = [d for d in dns if d not in ("#recycle", "_actors")]
        for f in sorted(fns):
            if not f.lower().endswith(".nfo"):
                continue
            p = os.path.join(dp, f)
            if args.only and args.only.lower() not in p.lower():
                continue
            try:
                raw = open(p, "rb").read()
                bom = raw.startswith(b"\xef\xbb\xbf")
                text = raw.decode("utf-8-sig")
                names = actor_names(text)
                exist = existing_thumbs(text)
                for n in names:
                    if n in exist:
                        continue
                    src = src_by_name.get(n) or local_photo(dp, os.path.splitext(f)[0], n, args.root)
                    if not src:
                        continue
                    ext = os.path.splitext(src)[1] or ".jpg"
                    dst = os.path.join(actor_dir, safe(n) + ext)
                    if args.write:
                        try:
                            os.makedirs(actor_dir, exist_ok=True)
                            if not os.path.exists(dst):
                                shutil.copyfile(src, dst)
                        except (OSError, PermissionError) as e:
                            print("WARN 跳过 %s：%s" % (n, e))
                            continue
                    exist[n] = dst
                    print("PHOTO", n, "->", dst)
                new = replace_section(text, names, {}, exist)
                if new != text:
                    changed += 1
                    if args.write:
                        with open(p, "w", encoding="utf-8", newline="\n") as fh:
                            fh.write(("\ufeff" if bom else "") + new)
            except Exception as e:
                print("ERR", p, e)

    print("完成：NFO 改动 %d%s" % (changed, "" if args.write else "（dry-run）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
