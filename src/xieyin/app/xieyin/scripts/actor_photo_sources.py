#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""演员头像多源兜底：JavBus/DMM → AV-League → JavDB → 百度百科。

问题：JavBus 的演员图直接引用 DMM `mono/actjpgs/nowprinting.gif`。
      DMM 未收录头像的演员就会一直缺图。
方案：主源图片是占位图/下载失败时，按日文名搜 AV-League 女优页；
      仍无图时再查百度百科移动版 `og:image`，统一转成 JPEG 后再上传。
"""
from __future__ import annotations

import html
import json
import re
import subprocess
import time
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


def fetch_html(url: str, referer: str = "", timeout: int = 15,
               cookie: str = "") -> bytes:
    """抓 HTML 页面：JavDB/百度百科对 Accept 头敏感，不能复用图片 Accept。"""
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8")
    req.add_header("Accept-Language", "zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7")
    if cookie:
        req.add_header("Cookie", cookie)
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
    raw = fetch_html(search_url, referer="https://www.av-league.com/")
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




_WD_CACHE = {}
_WD_LAST = 0.0


def _curl_html(url: str, referer: str = "", cookie: str = "") -> bytes:
    """JavDB 对 Python urllib 返回登录页；用 curl 的 TLS/HTTP 指纹可正常访问。"""
    cmd = ["curl", "-fsSL", "--max-time", "15", "-A", UA,
           "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
           "-H", "Accept-Language: zh-CN,zh;q=0.9,ja;q=0.8,en;q=0.7"]
    if cookie:
        cmd += ["-H", "Cookie: " + cookie]
    if referer:
        cmd += ["-H", "Referer: " + referer]
    cmd.append(url)
    try:
        return subprocess.run(cmd, capture_output=True, timeout=20).stdout
    except Exception:
        return b""


def fetch_json(url: str, timeout: int = 15) -> bytes:
    req = urllib.request.Request(url)
    req.add_header("User-Agent", UA)
    req.add_header("Accept", "application/json")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            return r.read()
    except Exception:
        return b""


def _wd_api(params: dict) -> dict:
    global _WD_LAST
    wait = 0.6 - (time.time() - _WD_LAST)
    if wait > 0:
        time.sleep(wait)
    _WD_LAST = time.time()
    raw = fetch_json("https://www.wikidata.org/w/api.php?" + urllib.parse.urlencode(params))
    try:
        return json.loads(raw)
    except Exception:
        return {}


def wikidata_image(name: str) -> tuple[bytes, str, str]:
    """Wikidata P18 / Wikimedia Commons：适合有百科词条的知名演员。"""
    if name in _WD_CACHE:
        d, u, p = _WD_CACHE[name]
        return d, u, p
    cands = []
    for n in (an.normalize(name), an.to_japanese(name), name):
        n = (n or "").strip()
        if n and n not in cands:
            cands.append(n)
    for cand in cands:
        for site in ("zhwiki", "jawiki", "enwiki"):
            d = _wd_api({"action": "wbgetentities", "format": "json",
                         "sites": site, "titles": cand,
                         "props": "claims|descriptions", "languages": "zh|ja|en"})
            for eid, ent in (d.get("entities") or {}).items():
                if not eid or eid.startswith("-") or ent.get("missing"):
                    continue
                desc_map = ent.get("descriptions") or {}
                desc = " ".join(
                    str(v.get("value", "")) if isinstance(v, dict) else str(v)
                    for v in desc_map.values()).lower()
                if not any(k in desc for k in ("av", "女優", "女优", "actress", "adult", "演员")):
                    continue
                claims = (ent.get("claims") or {}).get("P18") or []
                if not claims:
                    continue
                try:
                    fn = claims[0]["mainsnak"]["datavalue"]["value"]
                except Exception:
                    continue
                q = _wd_api({"action": "query", "format": "json",
                             "titles": "File:" + fn, "prop": "imageinfo",
                             "iiprop": "url", "iiurlwidth": "600"})
                pages = (q.get("query") or {}).get("pages") or {}
                for page in pages.values():
                    ii = (page.get("imageinfo") or [{}])[0]
                    img = ii.get("thumburl") or ii.get("url") or ""
                    if not img:
                        continue
                    data = fetch(img, referer="https://commons.wikimedia.org/")
                    if image_ok(data):
                        out = (to_jpeg(data), img, "https://www.wikidata.org/wiki/" + eid)
                        _WD_CACHE[name] = out
                        return out
    _WD_CACHE[name] = (b"", "", "")
    return b"", "", ""


_JAVDB_VIDEO_CACHE = {}


def _javdb_video_page(code: str) -> str:
    """按番号找 JavDB 影片页（javdb580 镜像，国内直连可用）。"""
    code = (code or "").strip()
    if not code:
        return ""
    if code in _JAVDB_VIDEO_CACHE:
        return _JAVDB_VIDEO_CACHE[code]
    q = urllib.parse.quote(code)
    raw = _curl_html("https://javdb580.com/search?q=" + q + "&type=video",
                     referer="https://javdb580.com/", cookie="over18=1")
    page = ""
    if raw:
        text = raw.decode("utf-8", "ignore")
        links = []
        for href in re.findall(r'href="(/v/[A-Za-z0-9]+)"', text):
            if href not in links:
                links.append(href)
        if links:
            page = "https://javdb580.com" + links[0]
    _JAVDB_VIDEO_CACHE[code] = page
    return page


def javdb_actor_image(code: str, name: str) -> tuple[bytes, str, str]:
    """JavDB 演员页头像：番号 -> 影片页 -> 演员链接 -> 演员页头像。"""
    page = _javdb_video_page(code)
    if not page:
        return b"", "", ""
    raw = _curl_html(page, referer="https://javdb580.com/", cookie="over18=1")
    if not raw:
        return b"", "", ""
    text = raw.decode("utf-8", "ignore")
    want = an.normalize(name) or (name or "").strip()
    actor_id = ""
    for aid, label in re.findall(
            r'<a[^>]+href="/actors/([A-Za-z0-9]+)"[^>]*>(.*?)</a>', text, re.S):
        label = re.sub(r"<[^>]+>", "", label).strip()
        got = an.normalize(label) or label
        if got == want or (want and (want in got or got in want)):
            actor_id = aid
            break
    if not actor_id:
        return b"", "", ""
    actor_page = "https://javdb580.com/actors/" + actor_id
    raw = _curl_html(actor_page, referer=page, cookie="over18=1")
    if not raw:
        return b"", "", ""
    html_text = raw.decode("utf-8", "ignore")
    m = re.search(r"background-image:\s*url\(([^)]+)\)", html_text)
    if not m:
        m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', html_text)
    if not m:
        return b"", "", ""
    img = html.unescape(m.group(1)).strip().strip('"\'')
    if "/avatars/" not in img:
        return b"", "", ""
    data = fetch(img, referer=actor_page)
    if not image_ok(data):
        return b"", "", ""
    return to_jpeg(data), img, actor_page


def _baike_page_image(page_url: str, name: str) -> tuple[bytes, str, str]:
    raw = fetch_html(page_url, referer="https://www.baidu.com/")
    if not raw:
        return b"", "", ""
    text = raw.decode("utf-8", "ignore")
    if "尚未收录" in text or "百度安全验证" in text:
        return b"", "", ""
    m = re.search(r'<meta[^>]+property="og:image"[^>]+content="([^"]+)"', text)
    if not m:
        return b"", "", ""
    img = html.unescape(m.group(1)).strip()
    low = img.lower()
    if not img or any(x in low for x in ("default", "logo", "icon/", "baikebcs.bdimg.com/front-end")):
        return b"", "", ""
    # 防止搜索结果跳到无关词条
    if name and name not in text:
        return b"", "", ""
    data = fetch(img, referer=page_url)
    if not image_ok(data):
        return b"", "", ""
    return to_jpeg(data), img, page_url


def baike_image(name: str) -> tuple[bytes, str, str]:
    """百度百科移动版头像：直接词条 -> 搜索首个词条。返回 (JPEG, 图片URL, 页面URL)。"""
    cands = []
    for n in (an.normalize(name), an.to_japanese(name), name):
        n = (n or "").strip()
        if n and n not in cands:
            cands.append(n)
    for n in cands:
        q = urllib.parse.quote(n)
        # 直接词条
        page = "https://wapbaike.baidu.com/item/" + q
        data, img, used = _baike_page_image(page, n)
        if data:
            return data, img, used
        # 搜索页：没有直接词条时，取搜索结果里的第一个 /item/ 链接
        search = "https://wapbaike.baidu.com/search/word?word=" + q
        raw = fetch_html(search, referer="https://www.baidu.com/")
        if not raw:
            continue
        text = raw.decode("utf-8", "ignore")
        if "尚未收录" in text:
            continue
        links = []
        for href in re.findall(r'href="(/item/[^"]+)"', text):
            href = html.unescape(href)
            if "百度百科" in urllib.parse.unquote(href):
                continue
            if href not in links:
                links.append(href)
        for href in links[:3]:
            page = "https://wapbaike.baidu.com" + href
            data, img, used = _baike_page_image(page, n)
            if data:
                return data, img, used
    return b"", "", ""


def resolve(name: str, primary_url: str = "", primary_fetch=None,
            code: str = "") -> tuple[bytes, str, str]:
    """返回 (JPEG bytes, 来源 URL, 来源标记)。

    primary_fetch 是 JavBus 的下载函数（需要固定 IP/Referer）。
    顺序：主源 -> AV-League -> Wikidata -> 百度百科。
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
    # JavDB 演员页有图，但反爬严格（无会话时会跳登录/403），暂不放进自动链路。
    # data, url, page = javdb_actor_image(code, name)
    # if data:
    #     return data, url, "javdb"
    data, url, page = wikidata_image(name)
    if data:
        return data, url, "wikidata"
    data, url, page = baike_image(name)
    if data:
        return data, url, "baike"
    return b"", "", ""
