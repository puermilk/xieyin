#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""演员名中文化 / 清洗。

- 日文假名演员名按 actor_names_zh.json 映射成中文常用名。
- 拆开 NFO 里被错误合并的「某演员 某演员」这类复合名。
- 过滤 XML 标签残片（<actor>/<name>/<type>/<tmdbid>）和占位名。
"""
from __future__ import annotations

import json
import os
import re

_MAP = None
_MAP_PATH = os.path.join(os.path.dirname(os.path.dirname(os.path.abspath(__file__))),
                         "actor_names_zh.json")

_JUNK_RE = re.compile(r"[<>]|^</?[a-z]+|^<?[a-z]+>?$", re.I)
_PLACEHOLDER = {"", "他", "未知演员", "unknown", "n/a", "null", "none"}
_NON_ACTOR = {"中文字幕", "字幕", "中文", "中字", "无码", "無碼", "無修正",
              "高清", "合集", "破解", "最新", "内嵌", "外挂"}

# 只用于「NFO 里演员段损坏、需要从标题反推」的少量已知名字，避免把标题碎片当成人名
COMMON_ACTORS = {"北条麻妃", "朝桐光", "并木塔子", "加濑佳奈子", "白石さゆり",
                 "横山みれい", "北条真纪", "江波りゅう"}
_SPLIT_RE = re.compile(r"[、,，/]+|\s{2,}")


def load_map() -> dict:
    global _MAP
    if _MAP is not None:
        return _MAP
    try:
        with open(_MAP_PATH, encoding="utf-8") as f:
            data = json.load(f)
        _MAP = {str(k).strip(): str(v).strip() for k, v in (data.get("map") or {}).items()
                if str(k).strip() and str(v).strip()}
    except Exception:
        _MAP = {}
    return _MAP


def is_junk(name: str) -> bool:
    n = (name or "").strip()
    if n.lower() in _PLACEHOLDER or n in _NON_ACTOR:
        return True
    return bool(_JUNK_RE.search(n))


def split_names(name: str) -> list:
    """拆分被合并的多个演员名；不把「名字 姓氏」这种单个人名拆坏。

    规则：
      * 逗号/顿号/斜杠分隔 -> 一定是多个人名
      * 两个以上空格 -> 多个人名
      * 单空格 -> 只有每一段都是已知演员名时才拆（如「某演员 某演员」）
      * 欧美/俄语/泰语/韩语等非日文姓名，保留完整，不按空格截断
    """
    n = (name or "").strip()
    if not n:
        return []
    parts = [p.strip() for p in re.split(r"[、,，/]+", n) if p.strip()]
    if len(parts) > 1:
        return parts
    if re.search(r"\s{2,}", n):
        return [p.strip() for p in re.split(r"\s{2,}", n) if p.strip()]
    sp = [p for p in n.split(" ") if p]
    if len(sp) > 1:
        m = load_map()
        known = set(m.keys()) | set(m.values()) | set(COMMON_ACTORS)
        # 每一段都是已知演员名才拆；否则保留完整姓名，避免「蒙塔纳 埃法夫」被截成「蒙塔纳」
        if all(p in known for p in sp):
            return sp
    return [n]


def normalize(name: str) -> str:
    n = (name or "").strip()
    if not n:
        return ""
    m = load_map()
    n = m.get(n, n)
    # 去掉 XML/CDATA 残片和姓名外层的括号
    n = re.sub(r"<!\[CDATA\[|\]\]>", "", n).strip()
    n = n.strip("（）()[]【】 ").strip()
    n = m.get(n, n)
    if n not in m:
        compact = re.sub(r"[\s　]+", "", n)
        if compact in m:
            n = m[compact]
    if n not in m:
        # 「森沢かな（飯岡かなこ」这类别名/残缺括号，取括号前的主名再映射
        base = re.split(r"[（(]", n, 1)[0].strip()
        if base and base in m:
            n = m[base]
    return "" if is_junk(n) else n


def normalize_list(names) -> list:
    out = []
    for raw in names or []:
        for part in split_names(str(raw)):
            n = normalize(part)
            if n and n not in out:
                out.append(n)
    return out


def has_kana(s: str) -> bool:
    return bool(re.search(r"[\u3040-\u30ff]", s or ""))


def to_japanese(name: str) -> str:
    """中文常用名反查日文名；查不到就原样返回。"""
    n = normalize(name)
    if not n:
        return ""
    if has_kana(n):
        return n
    rev = {}
    for ja, cn in load_map().items():
        rev.setdefault(cn, ja)
    return rev.get(n, n)
