#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""jav321 详情页解析器（纯函数，输出与 jav_scrape.build_nfo 兼容的字段）。"""
from __future__ import annotations

import re
import race_common as rc

FIELDS = {"出演者": "actors", "メーカー": "studio",
          "配信開始日": "releasedate", "収録時間": "runtime",
          "平均評価": "rating"}


def make_cid(code):
    m = re.match(r"^([A-Za-z]+)-?(\d+)$", code or "")
    if not m:
        return ""
    return m.group(1).lower() + m.group(2).zfill(5)


def parse_jav321(body, code):
    t = body.decode("utf-8", "ignore")
    m = re.search(r"<h3[^>]*>(.*?)<small>", t, re.S | re.I)
    d = {"code": code, "title": rc.strip_tags(m.group(1)) if m else "",
         "source": "jav321"}
    for ja, key in FIELDS.items():
        mm = re.search(r"<b>" + re.escape(ja) + r"</b>:\s*(.*?)(?:<br|&nbsp;|</div)", t, re.S)
        if not mm:
            continue
        blk = mm.group(1)
        links = [rc.strip_tags(a) for a in re.findall(r"<a[^>]*>(.*?)</a>", blk, re.S)]
        links = [x for x in links if x]
        if key == "actors":
            if links:
                d[key] = links
        elif links:
            d[key] = links[0]
        else:
            v = rc.strip_tags(blk)
            if v:
                d[key] = v
    mc = re.search(r"(https?://pics\.dmm\.co\.jp/digital/video/[^\"']+ps\.jpg)", t)
    if mc:
        d["cover"] = mc.group(1).replace("http://", "https://")
    return rc.sanitize(d)
