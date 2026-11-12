#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""LLM 翻译测试：输出一段标题 + 简介，供网页「测试 LLM」按钮调用。"""
from __future__ import annotations

import argparse
import json
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
os.environ["XIEYIN_PLOT_ZH"] = "1"

import translate_zh as t


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--title", required=True)
    ap.add_argument("--actors", default="")
    ap.add_argument("--code", default="TEST-001")
    ap.add_argument("--genres", default="NTR、人妻")
    args = ap.parse_args()

    title = (args.title or "").strip()[:300]
    actors = [x.strip() for x in (args.actors or "").split(",") if x.strip()]
    d = {
        "code": args.code,
        "title": title,
        "desc": title,
        "actors": actors,
        "genres": [x.strip() for x in (args.genres or "").split("、") if x.strip()],
        "releasedate": "",
        "runtime": "",
    }
    started = time.time()
    zh = t.translate_title_zh(d)
    plot = ""
    if zh and hasattr(t, "translate_plot_zh"):
        plot = t.translate_plot_zh(d, zh)
    if zh and not plot:
        plot = t.build_plot_zh(d)
    err = t.last_error()
    print(json.dumps({
        "ok": bool(zh),
        "title": zh,
        "plot": plot,
        "error": "" if zh else "%s:%s" % err,
        "provider": t.PROVIDER,
        "model": t.LLM_MODEL,
        "base": t.LLM_BASE,
        "elapsed": round(time.time() - started, 2),
    }, ensure_ascii=False))
    return 0 if zh else 1


if __name__ == "__main__":
    sys.exit(main())
