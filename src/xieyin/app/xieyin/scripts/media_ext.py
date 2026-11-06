#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""撷引（xieyin）媒体扩展名判定 —— 全项目唯一决定点。

三个概念分开，别混：

  视频     刮削、体检、自动扫描都要认的媒体文件
  STRM     Kodi / Jellyfin 的流指针文件，几百字节的文本，内含一个播放 URL。
           刮削时当视频（nfo 要配它），但清理时当受保护文件（绝不能删）。
  受保护   清理永不删除的文件：.strm 与所有附属文件（nfo / 图片 / 字幕）

shell 脚本（clean_qh.sh / check_meta.sh）无法 import 这里，
它们的清单要与本文件保持一致；改一处就要同步另一处。
"""
from __future__ import annotations

import os

# 真视频：清理「只留最大视频」只对这些生效
VIDEO_EXT = (".mp4", ".mkv", ".avi", ".ts", ".mov", ".wmv")

# 流指针：刮削当视频，清理永不删
STRM_EXT = (".strm",)

# 刮削要认的媒体 = 真视频 + STRM
MEDIA_EXT = VIDEO_EXT + STRM_EXT

# 附属文件：清理永不删
SIDECAR_EXT = (".nfo", ".jpg", ".jpeg", ".png", ".srt", ".ass", ".sup",
               ".sub", ".idx", ".vtt", ".torrent")


def _lower(name: str) -> str:
    return os.path.basename(name).lower()


def is_video(name: str) -> bool:
    """刮削口径：真视频 + STRM 都算。"""
    return _lower(name).endswith(MEDIA_EXT)


def is_real_video(name: str) -> bool:
    """清理口径：只认真视频，STRM 不算（它不能被「留最大」淘汰）。"""
    return _lower(name).endswith(VIDEO_EXT)


def is_strm(name: str) -> bool:
    return _lower(name).endswith(STRM_EXT)


def is_probeable(name: str) -> bool:
    """能不能跑 ffprobe。STRM 是文本，探测没有意义。"""
    return not is_strm(name)


def is_sidecar(name: str) -> bool:
    return _lower(name).endswith(SIDECAR_EXT)


def is_protected(name: str) -> bool:
    """清理永不删除：STRM 与所有附属文件。"""
    return is_strm(name) or is_sidecar(name)
