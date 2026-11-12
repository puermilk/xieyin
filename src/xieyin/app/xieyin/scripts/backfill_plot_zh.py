#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""把已有 NFO 的日文剧情翻译成中文（不访问刮削源）。

用途：
  * 已经刮好的库，不想为了翻译再跑一遍 JavBus/JavDB，避免触发站点限流/封禁。
  * 只读 NFO，调用翻译接口，替换 <plot>，保留 <originalplot> 日文原文。
  * 默认 dry-run；加 --write 才改文件，不再生成 .nfo.bak-zh 备份。

用法：
  python3 backfill_plot_zh.py --root /vol1/1000/Media/AV --limit 3 --dry
  python3 backfill_plot_zh.py --root /vol1/1000/Media/AV --limit 3 --write
"""
from __future__ import annotations

import argparse
import os
import re
import shutil
import sys
import time
import xml.etree.ElementTree as ET

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import translate_zh as t

KANA_RE = re.compile(r"[\u3040-\u30ff]")


def cdata(s: str) -> str:
    return "<![CDATA[" + (s or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"


def read_text(path: str) -> tuple[str, bool]:
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    return raw.decode("utf-8-sig"), bom


def parse_nfo(text: str) -> dict:
    root = ET.fromstring(text)
    def txt(tag):
        e = root.find(tag)
        return (e.text or "").strip() if e is not None else ""
    actors = []
    for a in root.findall("actor"):
        n = a.findtext("name")
        if n and n.strip():
            actors.append(n.strip())
    rt = txt("runtime")
    return {
        "code": txt("num"),
        "title": txt("originaltitle") or txt("title"),
        "desc": txt("originalplot") or txt("plot"),
        "releasedate": txt("releasedate") or txt("premiered"),
        "runtime": rt,
        "actors": actors,
    }


def replace_plot(text: str, zh: str) -> str:
    return re.sub(r"<plot>.*?</plot>", "<plot>" + cdata(zh) + "</plot>",
                  text, count=1, flags=re.S)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--sleep", type=float, default=1.5, help="每条翻译间隔秒数")
    ap.add_argument("--only", default="")
    args = ap.parse_args()

    if not os.path.isdir(args.root):
        print("目录不存在:", args.root)
        return 2

    files = []
    for dp, dns, fns in os.walk(args.root):
        dns[:] = [d for d in dns if d != "#recycle"]
        for f in sorted(fns):
            if f.lower().endswith(".nfo"):
                files.append(os.path.join(dp, f))
    if args.only:
        files = [p for p in files if args.only in p]
    if args.limit:
        files = files[:args.limit]

    ok = skip = fail = 0
    for i, p in enumerate(files, 1):
        try:
            text, bom = read_text(p)
            d = parse_nfo(text)
            plot = d.get("desc") or ""
            if not KANA_RE.search(plot):
                skip += 1
                continue
            zh = t.build_plot_zh(d)
            # 只检查正文：中文剧情里仍会带日文演员名（某演员），不能因为演员名有假名就判失败
            body = re.sub(r"^【[^】]*】[^，]*，", "", zh or "")
            body = re.sub(r"^【[^】]*】[^，]*，", "", body)
            for a in sorted(d.get("actors") or [], key=len, reverse=True):
                if a:
                    body = body.replace(a, "")
            try:
                import actor_names as _an
                for a in _an.load_map():
                    if a:
                        body = body.replace(a, "")
            except Exception:
                pass
            body = re.sub(r"\([A-Za-z0-9\-_]+\)", "", body)
            kana = len(KANA_RE.findall(body))
            cjk = len(re.findall(r"[\u4e00-\u9fff]", body))
            if not zh or zh == plot or (kana and kana > max(2, int(cjk * 0.3))):
                skip += 1
                continue
            print("[%d/%d] %s" % (i, len(files), p))
            print("   ->", zh[:120])
            if args.write:
                new = replace_plot(text, zh)
                raw = ("\ufeff" if bom else "") + new
                tmp = p + ".tmp-zh"
                with open(tmp, "w", encoding="utf-8", newline="\n") as f:
                    f.write(raw)
                os.replace(tmp, p)
            ok += 1
            time.sleep(max(0, args.sleep))
        except Exception as e:
            fail += 1
            print("   !! 失败:", p, e)
    print("完成：翻译 %d / 跳过 %d / 失败 %d%s" %
          (ok, skip, fail, "" if args.write else "（dry-run，未改文件）"))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
