#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""javdatabase.com 详情页解析器（tier-2 备胎源，纯函数）。

页面结构：WordPress 式 `<p class="mb-1"><b>Label: </b>值</p>` 行。
图床：仅 webp（covers/full|thumb/...），不在图床范围（jdbstatic/DMM/JavBus 三家），
故 cover 恒为空。合规：只取元数据，页面若有外链一概不采。
"""
from __future__ import annotations

import html
import re


def _txt(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", "", s or ""))).strip()


def _row(t, label):
    m = re.search(r"<b>" + re.escape(label) + r":\s*</b>(.*?)</p>", t, re.S)
    return m.group(1) if m else ""


def _links(seg):
    return [_txt(x) for x in re.findall(r"<a[^>]*>([^<]+)</a>", seg or "") if _txt(x)]


def parse_javdatabase(body, code):
    t = body.decode("utf-8", "ignore")
    d = {"code": (code or "").upper(), "cover": ""}

    # 标题：优先 h1，其次 <title>CODE - ... - JAV Database</title> 中段
    m = re.search(r"<h1[^>]*>(.*?)</h1>", t, re.S)
    title = _txt(m.group(1)) if m else ""
    if not title:
        m = re.search(r"<title>([^<]*)</title>", t)
        if m:
            parts = [p.strip() for p in html.unescape(m.group(1)).split(" - ")]
            title = parts[1] if len(parts) > 2 else (parts[0] if parts else "")
    if d["code"] and title.upper().startswith(d["code"]):
        title = title[len(d["code"]):].strip()
    d["title"] = title

    m = re.search(r"Release Date:\s*</b>\s*(\d{4}-\d{2}-\d{2})", t)
    d["releasedate"] = m.group(1) if m else ""
    m = re.search(r"Runtime:\s*</b>\s*(\d+)", t)
    d["runtime"] = m.group(1) if m else ""

    for key, label in (("studio", "Studio"), ("director", "Director")):
        seg = _row(t, label)
        got = _links(seg)
        d[key] = got[0] if got else _txt(seg)

    d["actors"] = _links(_row(t, "Idol(s)/Actress(es)"))
    d["genres"] = _links(_row(t, "Genre(s)"))
    return d
