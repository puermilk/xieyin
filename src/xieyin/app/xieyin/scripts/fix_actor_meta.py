#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""演员名中文化 + 清理飞牛 person 脏数据 / 推荐位演员。

默认 dry-run；--write 才改 NFO 和 trimmedia.db。
- NFO：优先从文件夹名取主演（如「ABC-123 某演员」），没有则从标题里匹配已知演员名；
  重建 <actor> 段，拆复合名、去重、丢推荐位和 XML 残片。
- 飞牛：person.name 按 actor_names_zh.json 改名并锁 name；清掉 XML 残片 person；
  对文件夹名能确定主演的条目，删除 item_person 里多余的 Actor 关联（保留 Director）。
- 不再生成 .nfo.bak-actor；数据库请先自行备份。
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sqlite3
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import actor_names as an
import jav_scrape as j
import media_ext


def folder_actors(folder_name: str) -> list:
    name = (folder_name or "").strip()
    if not name:
        return []
    # 去掉「8.」这种排序前缀
    name = re.sub(r"^\s*\d+[.、]\s*", "", name).strip()
    code = j.code_of(name)
    rest = name
    if code:
        m = re.match(r"^\s*" + re.escape(code) + r"[\s_\-]*", rest, re.I)
        if m:
            rest = rest[m.end():]
    # 去掉 -C/-UC/_C/FHD 这类版本尾缀（code_of 可能把 -C 的前缀吃掉，剩 C 开头）
    rest = re.sub(r"^(?:[-_]\s*)?(?:C|UC|FHD|HD|4K)(?=[\s_\-]|$)[\s_\-]*", "", rest, flags=re.I)
    rest = rest.strip(" -_")
    if not rest or len(rest) > 40:
        return []
    parts = [p.strip("（）()[]【】 ") for p in re.split(r"[,，、/]+", rest) if p.strip()]
    if not parts or any(len(p) > 12 for p in parts):
        return []
    return an.normalize_list(parts)


def actors_from_title(title: str, known) -> list:
    found = []
    for k in known:
        if not k:
            continue
        idx = title.find(k)
        if idx >= 0:
            found.append((idx, k))
    out = []
    for _idx, k in sorted(found):
        n = an.normalize(k)
        if n and n not in out:
            out.append(n)
    return out


NFO_NAME_RE = re.compile(r"<name>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</name>", re.S)


def nfo_actor_names(text: str) -> list:
    """只读 <actor> 块里的 <name>，不能把 <set><name>（系列名）当演员。"""
    out = []
    for m in re.finditer(r"<actor>.*?</actor>", text, re.S):
        nm = NFO_NAME_RE.search(m.group(0))
        if nm:
            out.append((nm.group(1) if nm.group(1) is not None else nm.group(2) or "").strip())
    return out


def actor_blocks(names: list) -> str:
    return "\n".join(
        "  <actor>\n    <name><![CDATA[%s]]></name>\n    <type>Actor</type>\n  </actor>"
        % n.replace("]]>", "]]]]><![CDATA[>")
        for n in names
    )


def replace_actor_section(text: str, names: list) -> str:
    block = actor_blocks(names)
    start = text.find("<actor>")
    end = text.rfind("</actor>")
    if start != -1 and end != -1:
        head = text[:start]
        tail = text[end + len("</actor>"):]
        return head + block + ("\n" if block else "") + tail.lstrip("\n")
    if block:
        idx = text.rfind("</movie>")
        if idx != -1:
            return text[:idx] + block + "\n" + text[idx:]
    return text


def db_path() -> str:
    for d in ("/usr/local/apps/@appdata/trim.media/database",
              "/vol1/@appdata/trim.media/database"):
        p = os.path.join(d, "trimmedia.db")
        if os.path.exists(p):
            return p
    return ""


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/vol1/1000/Media")
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--limit", type=int, default=0)
    args = ap.parse_args()

    nfo_files = []
    for dp, dns, fns in os.walk(args.root):
        dns[:] = [d for d in dns if d != "#recycle"]
        for f in sorted(fns):
            if f.lower().endswith(".nfo"):
                nfo_files.append(os.path.join(dp, f))
    if args.limit:
        nfo_files = nfo_files[:args.limit]

    db = db_path()
    known = set(an.load_map().keys()) | set(an.load_map().values()) | set(an.COMMON_ACTORS)

    changed = 0
    nfo_actors = {}
    for p in nfo_files:
        try:
            raw = open(p, "rb").read()
            bom = raw.startswith(b"\xef\xbb\xbf")
            text = raw.decode("utf-8-sig")
            folder_path = os.path.dirname(p)
            folder = os.path.basename(folder_path)
            try:
                multi = sum(1 for f in os.listdir(folder_path)
                            if media_ext.is_video(f)) > 1
            except OSError:
                multi = False
            names = folder_actors(folder) if not multi else []
            if not names:
                # 优先保留 NFO 里已有的演员（只做规范化），避免把标题/系列名碎片当人名
                names = an.normalize_list(nfo_actor_names(text))
            if not names:
                # 仅当演员段损坏/为空时，才从标题里匹配少量已知名字
                m = re.search(r"<originaltitle>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</originaltitle>", text, re.S)
                title = (m.group(1) if m and m.group(1) is not None else (m.group(2) if m else "")) or ""
                names = actors_from_title(title, known)
            new = replace_actor_section(text, names)
            if new != text:
                changed += 1
                nfo_actors[p] = names
                print("NFO", p, "->", ", ".join(names) or "(空)")
                if args.write:
                    with open(p, "w", encoding="utf-8", newline="\n") as f:
                        f.write(("\ufeff" if bom else "") + new)
        except Exception as e:
            print("ERR", p, e)

    if not db:
        print("!! 找不到 trimmedia.db")
        return 1

    scope = os.path.abspath(args.root).rstrip(os.sep) + os.sep

    def in_scope(path):
        try:
            return os.path.abspath(path or "").startswith(scope)
        except Exception:
            return False

    con = sqlite3.connect(db, timeout=8)
    con.row_factory = sqlite3.Row
    persons = []
    scoped_guids = set()
    for r in con.execute(
            "select distinct p.guid,p.name,i.path item_path from person p "
            "join item_person ip on ip.person_guid=p.guid "
            "join item i on i.guid=ip.item_guid"):
        if in_scope(r["item_path"]):
            scoped_guids.add(r["guid"])
            persons.append({"guid": r["guid"], "name": r["name"]})
    con.close()

    rename_plan = []
    junk_plan = []
    for r in persons:
        raw = r.get("name") or ""
        if an.is_junk(raw):
            junk_plan.append(r)
            continue
        names = an.normalize_list([raw])
        new = names[0] if names else ""
        if new and new != raw:
            rename_plan.append((r, new))

    print("人物改名 %d / 脏人物 %d" % (len(rename_plan), len(junk_plan)))
    for r, n in rename_plan:
        print("  PERSON", r.get("name"), "->", n)
    for r in junk_plan:
        print("  JUNK  ", r.get("name"), r.get("guid"))

    # 预览要删除的推荐位 Actor 关联
    prune = []
    if db:
        con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=8)
        con.row_factory = sqlite3.Row
        folder_count = {}
        for r in con.execute("select path from item_media"):
            f = os.path.dirname(r["path"] or "")
            folder_count[f] = folder_count.get(f, 0) + 1
        for row in con.execute(
                "select i.guid item_guid, i.path item_path, ip.person_guid, p.name "
                "from item i join item_person ip on ip.item_guid=i.guid "
                "join person p on p.guid=ip.person_guid where ip.job='Actor'"):
            path = row["item_path"] or ""
            if not in_scope(path):
                continue
            folder_path = os.path.dirname(path)
            folder = os.path.basename(folder_path)
            # 一个目录多个视频时，文件夹名只代表其中一部，不能用来裁演员
            if folder_count.get(folder_path, 0) > 1:
                continue
            keep = set(folder_actors(folder))
            name = an.normalize(row["name"])
            if keep and name and not an.is_junk(name) and name not in keep:
                prune.append(dict(row))
        con.close()
    print("推荐位/多余 Actor 关联 %d" % len(prune))
    for r in prune[:12]:
        print("  PRUNE", r["name"], "|", os.path.basename(os.path.dirname(r["item_path"] or "")))

    if args.write:
        con = sqlite3.connect(db, timeout=8)
        con.row_factory = sqlite3.Row
        try:
            now = int(time.time())
            with con:
                # 1) 删脏人物
                for r in junk_plan:
                    con.execute("delete from item_person where person_guid=?", (r["guid"],))
                    con.execute("delete from field_lock where entity_type='person' and entity_id=?", (r["guid"],))
                    con.execute("delete from person where guid=?", (r["guid"],))
                # 2) 改名 + 锁 name
                for r, n in rename_plan:
                    con.execute("update person set name=?, original_name=?, update_time=? where guid=?",
                                (n, n, now, r["guid"]))
                    con.execute("insert into field_lock(entity_type,entity_id,field_name,is_locked,create_time,update_time) "
                                "values('person',?,'name',1,?,?) "
                                "on conflict(entity_type,entity_id,field_name) do update set is_locked=1,update_time=excluded.update_time",
                                (r["guid"], now, now))
                # 3) 删推荐位 Actor 关联
                for r in prune:
                    con.execute("delete from item_person where item_guid=? and person_guid=? and job='Actor'",
                                (r["item_guid"], r["person_guid"]))
                # 4) 合并重名（保留关联最多的那条）
                dup = [dict(x) for x in con.execute(
                    "select name, count(*) c from person group by name having c>1")]
                for d in dup:
                    rows = [dict(x) for x in con.execute(
                        "select p.guid, count(ip.item_guid) c from person p "
                        "left join item_person ip on ip.person_guid=p.guid "
                        "where p.name=? group by p.guid order by c desc", (d["name"],))]
                    rows = [x for x in rows if x["guid"] in scoped_guids]
                    if len(rows) < 2:
                        continue
                    keep = rows[0]["guid"]
                    for r in rows[1:]:
                        con.execute("update item_person set person_guid=? where person_guid=?", (keep, r["guid"]))
                        con.execute("delete from field_lock where entity_type='person' and entity_id=?", (r["guid"],))
                        con.execute("delete from person where guid=?", (r["guid"],))
        finally:
            con.close()

    print("完成：NFO 改动 %d / 改名 %d / 脏人物 %d / 关联清理 %d%s" %
          (changed, len(rename_plan), len(junk_plan), len(prune),
           "" if args.write else "（dry-run，未改文件/库）"))
    return 0


if __name__ == "__main__":
    sys.exit(main())
