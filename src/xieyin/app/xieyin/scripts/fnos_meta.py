#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""飞牛影视元数据推送客户端（方案A 主模式，零文件写入）。

接口规范（用户设计文档「飞牛影视API直连方案」）：
  接口前缀  http://{飞牛地址}/api/media/meta
  更新元数据 POST /api/media/meta/update
  鉴权      Header Authorization: Bearer {访问令牌}
  请求体    {"file_path": "<视频绝对路径>", "meta": {...}}
  成功      {"code": 0, "msg": "success", "data": {}}

file_path 是唯一关联 key。PT 目录全程只读，本模块只发 HTTP、不写任何文件。

meta 字段（文档）：title / original_title / overview / year / tmdb_id /
douban_id / genres / actors[{name, photo_url}] / poster_url / backdrop_url。
另附 AV 扩展键（code/studio/director/series/label/releasedate/runtime），
后端不认识的键忽略即可。
"""
from __future__ import annotations

import json
import os
import re
import time
import urllib.error
import urllib.request

API_PATH = "/api/media/meta/update"
_BAD = re.compile(r"magnet:|\.torrent|/watch|/stream|/download|\.m3u8|ed2k://", re.I)


def _clean(v):
    """值级合规过滤：磁力/播放/下载地址一律不进推送体。"""
    if isinstance(v, str):
        return "" if _BAD.search(v) else v
    return v


def build_payload(d, file_path):
    """刮削结果 -> 推送请求体。file_path=视频绝对路径（唯一关联 key）。"""
    d = d or {}
    year = d.get("year")
    if not year:
        m = re.match(r"(\d{4})", str(d.get("releasedate") or ""))
        year = int(m.group(1)) if m else None
    try:
        year = int(year) if year else None
    except (TypeError, ValueError):
        year = None
    actors = []
    for a in (d.get("actors") or d.get("cast") or []):
        if isinstance(a, str) and a.strip():
            actors.append({"name": a.strip()})
        elif isinstance(a, dict) and a.get("name"):
            item = {"name": str(a["name"]).strip()}
            if a.get("photo_url"):
                item["photo_url"] = _clean(str(a["photo_url"]))
            actors.append(item)

    meta = {}

    def put(key, val):
        val = _clean(val)
        if val not in (None, "", []):
            meta[key] = val

    put("title", d.get("title") or "")
    put("original_title", d.get("original_title") or d.get("title") or "")
    put("overview", d.get("overview") or d.get("plot") or d.get("desc") or "")
    if year:
        meta["year"] = year
    put("tmdb_id", str(d.get("tmdb_id") or d.get("tmdbid") or ""))
    put("douban_id", str(d.get("douban_id") or ""))
    put("genres", [g for g in (d.get("genres") or [])
                   if isinstance(g, str) and g.strip()])
    put("actors", actors)
    put("poster_url", d.get("poster_url") or d.get("cover") or "")
    put("backdrop_url", d.get("backdrop_url") or "")
    # AV 扩展键（文档另有字段以文档为准；后端可忽略不认识的键）
    put("code", d.get("code") or "")
    put("studio", d.get("studio") or "")
    directors = d.get("directors") if isinstance(d.get("directors"), list) else None
    put("director", "、".join(directors) if directors else (d.get("director") or ""))
    put("series", d.get("series") or "")
    put("label", d.get("label") or "")
    put("releasedate", d.get("releasedate") or "")
    rt = str(d.get("runtime") or "")
    m2 = re.search(r"\d+", rt)
    put("runtime", m2.group(0) if m2 else "")
    return {"file_path": os.path.abspath(str(file_path)), "meta": meta}


def _state_path():
    import shutil as _sh
    base = os.environ.get("XIEYIN_DATA") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")
    return os.path.join(base, "pushed_state.json")


def already_pushed(file_path):
    """push 模式去重：没 --force 时推过的跳过（省抓取流量）。"""
    try:
        with open(_state_path(), encoding="utf-8") as fh:
            return os.path.abspath(str(file_path)) in json.load(fh)
    except Exception:
        return False


def mark_pushed(file_path):
    try:
        os.makedirs(os.path.dirname(_state_path()), exist_ok=True)
        try:
            with open(_state_path(), encoding="utf-8") as fh:
                state = json.load(fh)
        except Exception:
            state = {}
        state[os.path.abspath(str(file_path))] = time.time()
        tmp = _state_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(state, fh)
        os.replace(tmp, _state_path())
    except OSError:
        pass


def push_meta(base, token, payload, timeout=15):
    """POST /api/media/meta/update。永不抛异常，返回 (ok, msg)。"""
    base = (base or "").strip().rstrip("/")
    if not base:
        return False, "未配置飞牛地址"
    if not re.match(r"^https?://", base):
        base = "http://" + base
    if base.endswith("/api/media/meta"):
        url = base + "/update"
    else:
        url = base + API_PATH
    body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
    req = urllib.request.Request(url, data=body, method="POST")
    req.add_header("Content-Type", "application/json")
    if token:
        req.add_header("Authorization", "Bearer " + token)
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            raw = r.read()
    except urllib.error.HTTPError as e:
        return False, "HTTP %s %s" % (e.code, url)
    except Exception as e:
        return False, "连接失败 %s: %s" % (url, e)
    try:
        out = json.loads(raw.decode("utf-8", "ignore") or "{}")
    except Exception:
        return False, "响应不是 JSON"
    if out.get("code") == 0:
        return True, out.get("msg") or "success"
    return False, out.get("msg") or ("code=%s" % out.get("code"))
