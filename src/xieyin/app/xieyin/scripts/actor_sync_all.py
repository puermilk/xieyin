#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""演员整理编排：中文名/唯一性/关联影片 + 飞牛头像 + NFO <thumb>。

顺序：
  1. fix_actor_meta.py   规范化 NFO 演员段，改名/合并飞牛 person，清理多余关联
  2. sync_actor_photos.py 给 person.profile_path 补头像（仅 --write）
  3. attach_actor_thumbs.py 把头像回写到 NFO <actor><thumb>

默认 dry-run；--write 才写入。头像抓取受 --photo-limit 限制，避免批量打源站。
"""
from __future__ import annotations

import argparse
import os
import subprocess
import sys

HERE = os.path.dirname(os.path.abspath(__file__))


def run(cmd: list) -> int:
    print("$ " + " ".join(cmd), flush=True)
    return subprocess.call(cmd, cwd=HERE)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--write", action="store_true")
    ap.add_argument("--photo-limit", type=int, default=20)
    ap.add_argument("--sleep", type=float, default=2.0)
    args = ap.parse_args()
    if not os.path.isdir(args.root):
        print("目录不存在:", args.root)
        return 2

    py = sys.executable or "/usr/bin/python3"
    rc = 0

    cmd = [py, os.path.join(HERE, "fix_actor_meta.py"), "--root", args.root]
    if args.write:
        cmd.append("--write")
    rc |= run(cmd)

    if args.write:
        cmd = [py, os.path.join(HERE, "sync_actor_photos.py"),
               "--root", args.root,
               "--limit", str(max(1, args.photo_limit)),
               "--sleep", str(max(0.0, args.sleep)), "--write"]
        rc |= run(cmd)

    cmd = [py, os.path.join(HERE, "attach_actor_thumbs.py"), "--root", args.root]
    if args.write:
        cmd.append("--write")
    rc |= run(cmd)

    print("演员整理完成（%s）" % ("已写入" if args.write else "预览，未修改"))
    return 0 if rc == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
