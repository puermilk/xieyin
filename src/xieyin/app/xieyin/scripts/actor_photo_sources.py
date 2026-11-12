#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""演员头像多源兜底：JavBus/DMM 占位图时换 AV-League。

问题：JavBus 的演员图直接引用 DMM `mono/actjpgs/nowprinting.gif`。
      DMM 未收录头像的演员就会一直缺图。
方案：主源图片是占位图/下载失败时，按日文名搜 AV-League 女优页，
      取 `image/act_quality/.../xxx-0.jpg`，统一转成 JPEG 后再上传。
"""
from __future__ import annotations

import re
import urllib.parse
import urllib.request
from io import BytesIO

import actor_names as an

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
      "AppleWebKit/537.36 (KHTML, like Gecko) Chrome/124.0 Safari/537.36")
PLACEHOLDER = ("nowprinting", "noimage", "blank.gif", "/default.",
               "no_photo", "nophoto", "now_printing")


def is_placeholder(url: str) -> bool:
    low = (url or "").lower()
    return (not low) or any(x in low for x in PLACEHOLDER)


def image_ok(data: bytes) -> bool:
    if not data or len(data) < 1000:
        return False
    if data[:3] == b"\xff\xd8\xff":          # JPEG
        return True
    if data[:8] == b"\x89PNG\r\n\x1a\n":     # PNG
        return True
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        return True
    return False


def to_jpeg(data: bytes) -> bytes:
    if not data:
        return b""
    if data[:3] == b"\xff\xd8\xff":
        return data
    try:
        from PIL import Image
        im = Image.open(BytesIO(data)).convert("RGB")
        out = BytesIO()
        im.save(out, "JPEG", quality=92)
        return out.getvalue()
    except Exception:
        return b""


def avatar_from_cover(data: bytes) -> bytes:
    """最后兜底：从影片封面裁一张头像。

    DMM pl 是左右两张封面拼图，取右半张正封面；再按 3:4 居中裁切。
    不是完美头像，但比空白/占位图强，且不锁定，后续有真图可覆盖。
    """
    if not data:
        return b""
    try:
        from PIL import Image
        im = Image.open(BytesIO(data))
        im.load()
        im = im.convert("RGB")
        w, h = im.size
        if w > h * 1.2:                      # 左右拼图 -> 右半张
            im = im.crop((w // 2, 0, w, h))
        w, h = im.size
        tw = min(w, int(h * 0.75))           # 3:4 居中
        if tw < w:
            left = (w - tw) // 2
            im = im.crop((left, 0, left + tw, h))
        if im.height > 600:
            im = im.resize((max(1, int(im.width * 600 / im.height)), 600))
        out = BytesIO()
        im.save(out, "JPEG", quality=90)
        return out.getvalue()
    except Exception:
        return b""


def fetch(url: str, referer: str = "", timeout: int = 15) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8")
    if referer:
        req.add_header("Referer", referer)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return b""


def av_league_image(name: str) -> tuple[bytes, str, str]:
    """按演员名搜 AV-League，返回 (JPEG bytes, 图片 URL, 女优页 URL)。"""
    ja = an.to_japanese(name) or (name or "")
    q = urllib.parse.quote(ja)
    search_url = "https://www.av-league.com/search/search.php?k=" + q
    raw = fetch(search_url, referer="https://www.av-league.com/")
    if not raw:
        return b"", "", ""
    s = raw.decode("utf-8", "ignore")
    # 搜索结果第一项：女优页 + 头像
    m = re.search(r'<a href="(/actress/\d+\.html)">\s*<img[^>]+src="([^"]+)"', s, re.S)
    page_path = img = ""
    if m:
        page_path, img = m.group(1), m.group(2)
    else:
        # 直接命中的女优页
        m = re.search(r'<div class="i-pic">\s*<img[^>]+src="([^"]+)"', s, re.S)
        if m:
            img = m.group(1)
    if not img:
        return b"", "", ""
    if img.startswith("/"):
        img = "https://www.av-league.com" + img
    # 搜索页给 jpg，详情页给 webp；优先 jpg，上传链路最稳
    jpg = re.sub(r"-0\.webp(?:\?.*)?$", "-0.jpg", img)
    if jpg != img:
        data = fetch(jpg, referer="https://www.av-league.com/")
        if image_ok(data):
            return to_jpeg(data), jpg, ("https://www.av-league.com" + page_path if page_path.startswith("/") else page_path)
    data = fetch(img, referer="https://www.av-league.com/")
    if image_ok(data):
        return to_jpeg(data), img, ("https://www.av-league.com" + page_path if page_path.startswith("/") else page_path)
    return b"", "", ""


def resolve(name: str, primary_url: str = "", primary_fetch=None) -> tuple[bytes, str, str]:
    """返回 (JPEG bytes, 来源 URL, 来源标记)。

    primary_fetch 是 JavBus 的下载函数（需要固定 IP/Referer）。
    """
    if primary_url and not is_placeholder(primary_url):
        try:
            raw = primary_fetch(primary_url) if primary_fetch else fetch(primary_url)
        except Exception:
            raw = b""
        jpg = to_jpeg(raw)
        if jpg and image_ok(jpg):
            return jpg, primary_url, "primary"
    data, url, page = av_league_image(name)
    if data:
        return data, url, "av-league"
    return b"", "", ""
