#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""只读 NFO，强制用 LLM 重译标题与简介（不访问任何刮削源）。

读取 <originaltitle> / <originalplot>（没有则用 <title>），调用 translate_zh，
只替换 <title> 和 <plot>，保留 <originaltitle>、演员、图片引用、视频等一切内容。

默认 dry-run；--write 才改文件。不再生成 .bak-retranslate 备份。
"""
from __future__ import annotations

import argparse
import concurrent.futures
import html
import os
import re
import shutil
import sys
import threading

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import actor_names as an
import jav_scrape as j
import translate_zh as t

# 这个脚本本身就是翻译任务，不依赖网页里的 plot_zh 开关
os.environ["XIEYIN_PLOT_ZH"] = "1"

KANA_RE = re.compile(r"[\u3040-\u30ff]")
AD_NAME_RE = re.compile(
    r"直播|情報|情报|最新|地址|网址|網址|聚合|福利|广告|廣告|获取|獲取|社区|社區|下载|下載|永久|备用|備用|发布|發布")
# 与 translate_zh 的重试下限一致；低于此值才补译，避免 100~119 字的文案每次刮削都重跑。
MIN_PLOT_CHARS = getattr(t, "MIN_PLOT_CHARS_RETRY", 100)


def _plot_body(plot: str) -> str:
    """取 plot 正文长度：去掉【發行日期】/【長度】/(番号) 前缀，忽略空白。"""
    s = (plot or "").strip()
    m = re.search(r"\)\s*(.*)$", s, re.S)
    if m:
        s = m.group(1)
    return re.sub(r"\s", "", s)


def genres_of(text: str) -> list:
    out = []
    for m in re.finditer(r"<genre>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</genre>", text, re.S):
        g = html.unescape((m.group(1) if m.group(1) is not None else m.group(2) or "")).strip()
        if g and g not in out:
            out.append(g)
    return out


def tag_text(text: str, tag: str) -> str:
    m = re.search(r"<%s>(?:<!\[CDATA\[(.*?)\]\]>|(.*?))</%s>" % (tag, tag), text, re.S)
    if not m:
        return ""
    return html.unescape((m.group(1) if m.group(1) is not None else m.group(2) or "")).strip()


def replace_tag(text: str, tag: str, value: str) -> str:
    safe = (value or "").replace("]]>", "]]]]><![CDATA[>")
    new = "<%s><![CDATA[%s]]></%s>" % (tag, safe, tag)
    out, n = re.subn(r"<%s>.*?</%s>" % (tag, tag), new, text, count=1, flags=re.S)
    return out if n else text


def actors_of(text: str) -> list:
    out = []
    for m in re.finditer(r"<actor>.*?</actor>", text, re.S):
        n = tag_text(m.group(0), "name")
        n = an.normalize(n)
        if n and n not in out:
            out.append(n)
    return out


def retranslate(path: str, write: bool, force: bool, fill: bool = False) -> str:
    raw = open(path, "rb").read()
    bom = raw.startswith(b"\xef\xbb\xbf")
    text = raw.decode("utf-8-sig")

    code = tag_text(text, "num")
    if not code:
        code = j.code_of(os.path.splitext(os.path.basename(path))[0]) or ""
    ja_title = tag_text(text, "originaltitle") or tag_text(text, "title")
    ja_plot = tag_text(text, "originalplot") or tag_text(text, "plot")
    if not ja_title:
        return "SKIP 无标题"

    cur_title = tag_text(text, "title")
    cur_plot = tag_text(text, "plot")
    body_len = len(_plot_body(cur_plot))
    if fill and not force:
        bad = (not cur_title or not cur_plot
               or KANA_RE.search(cur_title) or KANA_RE.search(cur_plot)
               or body_len < MIN_PLOT_CHARS
               or t.META_LEAK_RE.search(cur_plot)
               or t.PLOT_REFUSAL_RE.search(cur_plot))
        if not bad:
            return "SKIP 已完整"
        if not KANA_RE.search(ja_title) and not KANA_RE.search(ja_plot):
            return "SKIP 无日文源"
    elif not force and not KANA_RE.search(ja_title) and not KANA_RE.search(ja_plot):
        return "SKIP 无假名"

    # 去掉标题里可能带的番号，避免译名重复
    if code:
        ja_title = re.sub(r"^\s*" + re.escape(code) + r"[\s_\-:：]*", "", ja_title,
                          flags=re.I).strip()
    # 日文演员名常以「标题 演员名」结尾；NFO actor 段已是中文，需同时按日文名剥离
    actors = actors_of(text)
    known_actors = list(actors)
    try:
        known_actors += list(an.load_map().keys())
    except Exception:
        pass
    ja_title = t._prepare_title(ja_title, known_actors) or ja_title

    d = {
        "code": code,
        "title": ja_title,
        "desc": ja_plot or ja_title,
        "actors": actors,
        "genres": genres_of(text),
        "studio": tag_text(text, "studio") or tag_text(text, "maker"),
        "releasedate": tag_text(text, "releasedate") or tag_text(text, "premiered"),
        "runtime": tag_text(text, "runtime"),
    }
    zh_title = t.translate_title_zh(d)
    if not zh_title:
        e, r = t.last_error()
        return "FAIL %s %s" % (e, r)
    zh_plot = t.build_plot_zh(d, zh_title) or zh_title

    title_out = (code + " " + zh_title).strip() if code else zh_title
    if (title_out == tag_text(text, "title")
            and zh_plot == tag_text(text, "plot")
            and title_out == tag_text(text, "sorttitle")):
        return "SAME " + code

    if write:
        text = replace_tag(text, "title", title_out)
        text = replace_tag(text, "plot", zh_plot)
        text = replace_tag(text, "sorttitle", title_out)
        with open(path, "w", encoding="utf-8", newline="\n") as f:
            f.write(("\ufeff" if bom else "") + text)
    return "OK   %s | %s" % (code or os.path.basename(path), zh_title[:80])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--force", action="store_true", help="已是中文也重译")
    ap.add_argument("--fill", action="store_true", help="只补缺失/过短/残留假名/拒答的 NFO")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--sleep", type=float, default=0.0)
    ap.add_argument("--jobs", type=int, default=2, help="并发数（本地 LLM 建议 2）")
    args = ap.parse_args()

    files = []
    for dp, dns, fns in os.walk(args.root):
        dns[:] = [d for d in dns if d != "#recycle"]
        for f in sorted(fns):
            if f.lower().endswith(".nfo"):
                if AD_NAME_RE.search(re.sub(r"\s", "", f)):
                    continue
                files.append(os.path.join(dp, f))
    if args.only:
        files = [p for p in files if args.only.lower() in p.lower()]
    if args.limit:
        files = files[:args.limit]
    print("待处理 NFO %d 个（%s）" % (len(files), "写入" if args.write else "预览"))

    ok = skip = fail = 0
    import time
    lock = threading.Lock()

    def one(i, p):
        nonlocal ok, skip, fail
        try:
            r = retranslate(p, args.write, args.force, args.fill)
        except Exception as e:
            r = "ERR  %s" % e
        with lock:
            print("[%d/%d] %s  %s" % (i, len(files), os.path.relpath(p, args.root), r), flush=True)
            if r.startswith("OK"):
                ok += 1
            elif r.startswith("SKIP") or r.startswith("SAME"):
                skip += 1
            else:
                fail += 1
        if args.sleep > 0:
            time.sleep(args.sleep)

    jobs = max(1, int(args.jobs or 1))
    if jobs == 1:
        for i, p in enumerate(files, 1):
            one(i, p)
    else:
        with concurrent.futures.ThreadPoolExecutor(max_workers=jobs) as ex:
            list(ex.map(lambda x: one(x[0], x[1]), enumerate(files, 1)))
    print("完成：成功 %d / 跳过 %d / 失败 %d%s" %
          (ok, skip, fail, "" if args.write else "（预览，未改文件）"))
    return 0 if fail == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
