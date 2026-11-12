#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""把刮削产物复制到指定目录：<copy_root>/<番号>/。

复制内容：
  * 原视频（保留原文件名）
  * 同 stem 的 NFO / 字幕 / 图片（stem.nfo、stem-poster.jpg、stem-actor-*.jpg…）
  * 单视频目录下的 poster.jpg / folder.jpg / fanart.jpg / thumb.jpg

只复制、不移动、不改原文件。目标已存在且大小/时间一致时跳过。
"""
from __future__ import annotations

import os
import re
import shutil
import sys
from pathlib import Path

import media_ext

SKIP_EXT = {".torrent", ".parts", ".aria2", ".tmp", ".!qb", ".!ut"}
SKIP_SUFFIX = (".bak-zh", ".bak-actor", ".tmp-zh", ".pyc")


def safe_code(code: str) -> str:
    c = re.sub(r'[\\/:*?"<>|]', "_", (code or "").strip())
    return c[:120] or "unknown"


def _wanted(p: Path, video: Path, single_video: bool) -> bool:
    n = p.name.lower()
    if p == video:
        return True
    if n.endswith(SKIP_SUFFIX) or p.suffix.lower() in SKIP_EXT:
        return False
    stem = video.stem
    if n == stem.lower() or n.startswith(stem.lower() + ".") \
            or n.startswith(stem.lower() + "-") or n.startswith(stem.lower() + "_"):
        return True
    if single_video and n in ("poster.jpg", "folder.jpg", "fanart.jpg", "thumb.jpg"):
        return True
    return False


def copy_scraped(code: str, folder: Path, video: Path, copy_root: str,
                 log=print) -> tuple[Path, int]:
    """复制单个视频的刮削产物，返回 (目标目录, 复制文件数)。"""
    root = Path(str(copy_root or "").strip())
    if not str(copy_root or "").strip():
        return Path(""), 0
    dest = root / safe_code(code)
    dest.mkdir(parents=True, exist_ok=True)

    folder = Path(folder)
    video = Path(video)
    try:
        single_video = sum(
            1 for x in folder.iterdir()
            if x.is_file() and media_ext.is_video(x.name)) <= 1
    except OSError:
        single_video = True

    n = 0
    for src in sorted(folder.iterdir(), key=lambda x: x.name.lower()):
        if not src.is_file() or not _wanted(src, video, single_video):
            continue
        dst = dest / src.name
        try:
            if dst.exists():
                ss, ds = src.stat(), dst.stat()
                if ss.st_size == ds.st_size and int(ss.st_mtime) == int(ds.st_mtime):
                    continue
            shutil.copy2(src, dst)
            n += 1
        except OSError as e:
            log("   [复制] WARN %s: %s" % (src.name, e))
    log("   [复制] %s -> %s（%d 个文件）" % (video.name, dest, n))
    return dest, n


def main() -> int:
    import argparse
    ap = argparse.ArgumentParser(description="把刮削产物复制到 <目标>/<番号>/")
    ap.add_argument("--root", required=True, help="视频所在目录")
    ap.add_argument("--copy-root", required=True, help="复制目标根目录")
    ap.add_argument("--code", default="", help="番号；留空自动识别")
    ap.add_argument("--dry", action="store_true")
    args = ap.parse_args()

    folder = Path(args.root)
    video = next((x for x in sorted(folder.iterdir())
                  if x.is_file() and media_ext.is_video(x.name)), None)
    if not video:
        print("没有视频文件")
        return 2
    code = args.code
    if not code:
        try:
            import jav_scrape as j
            code = j.code_of(folder.name) or j.code_of(video.stem)
        except Exception:
            code = video.stem
    if args.dry:
        print("%s -> %s" % (video, Path(args.copy_root) / safe_code(code)))
        return 0
    copy_scraped(code, folder, video, args.copy_root)
    return 0


if __name__ == "__main__":
    sys.exit(main())
