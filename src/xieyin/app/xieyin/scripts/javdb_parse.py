#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""JavDB 详情页解析器（纯函数，输出与 jav_scrape.build_nfo 兼容的字段）。"""
from __future__ import annotations

import re
import race_common as rc

FIELDS = {"日期": "releasedate", "時長": "runtime", "導演": "director",
          "片商": "studio", "演員": "actors", "類別": "genres",
          "系列": "series", "評分": "rating"}


def _block(t, zh):
    m = re.search(r"<strong>" + re.escape(zh) + r":</strong>(.*?)</div>", t, re.S)
    return m.group(1) if m else ""


def _links(block):
    out = []
    for a in re.findall(r"<a[^>]*>(.*?)</a>", block, re.S):
        v = rc.strip_tags(a)
        if v and v not in out:
            out.append(v)
    return out


def parse_javdb(body, code):
    t = body.decode("utf-8", "ignore")
    m = re.search(r"<title[^>]*>(.*?)</title>", t, re.S | re.I)
    d = {"code": code, "title": rc.clean_title(m.group(1)) if m else "",
         "source": "javdb580"}
    for zh, key in FIELDS.items():
        blk = _block(t, zh)
        if not blk:
            continue
        vals = _links(blk)
        if key in ("actors", "genres"):
            if vals:
                d[key] = vals
        elif vals:
            d[key] = vals[0]
        else:
            v = rc.strip_tags(blk).replace("&nbsp;", " ").strip()
            if v:
                d[key] = v
    mc = re.search(r"(https://c0\.jdbstatic\.com/covers/[^\"']+\.jpg)", t)
    if mc:
        d["cover"] = mc.group(1)
    return rc.sanitize(d)
