#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""刮削工具 FPK 打包脚本。

用法：
    python3 tools/build_fpk.py 1.0.0

产出：
    dist/xieyin-<版本>.fpk
    dist/xieyin-<版本>.fpk.sha256

包结构：
    顶层      app.tgz  cmd/  config/  ICON.PNG  ICON_256.PNG  manifest
    app.tgz   xieyin/  ui/  config/
    manifest  checksum = app.tgz 的 md5（应用中心用它校验包体）

注意：
    - cmd/ 与 app/xieyin/*.sh 必须是 0755。
    - 不打包 __pycache__ / *.pyc / data/（配置和日志属于运行时）。
"""
import hashlib
import os
import re
import sys
import tarfile
import tempfile

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
APP = os.path.join(ROOT, "src", "xieyin")
APP_DIR = os.path.join(APP, "app")

APP_ITEMS = ["xieyin", "ui"]
APP_EXTRA = [os.path.join(APP, "config")]
FPK_ITEMS = ["app.tgz", "cmd", "config", "ICON.PNG", "ICON_256.PNG", "manifest"]


def excluded(name: str) -> bool:
    base = os.path.basename(name)
    return base in ("__pycache__", "data") or base.endswith(".pyc")


def is_exec(path: str) -> bool:
    if path.endswith(".sh"):
        return True
    return os.sep + "cmd" + os.sep in path + os.sep


def add_tree(tf: tarfile.TarFile, path: str, arcname: str) -> None:
    if excluded(path):
        return
    ti = tf.gettarinfo(path, arcname=arcname)
    if ti.isdir():
        ti.name = ti.name.rstrip("/")
        ti.mode = 0o755
        tf.addfile(ti)
        for child in sorted(os.listdir(path)):
            add_tree(tf, os.path.join(path, child), arcname + "/" + child)
    else:
        ti.mode = 0o755 if is_exec(path) else 0o644
        with open(path, "rb") as fh:
            tf.addfile(ti, fh)


def main() -> int:
    args = [a for a in sys.argv[1:] if not a.startswith("--")]
    if not args:
        print(__doc__)
        return 2
    ver = args[0]
    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out_fpk = os.path.join(out_dir, "xieyin-%s.fpk" % ver)
    out_app = os.path.join(APP, "app.tgz")
    out_manifest = os.path.join(tempfile.mkdtemp(prefix="xieyin-fpk-"), "manifest")

    # 1) app.tgz
    print("### 1) 打包 app.tgz")
    if os.path.exists(out_app):
        os.unlink(out_app)
    with tarfile.open(out_app, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        for item in APP_ITEMS:
            add_tree(tf, os.path.join(APP_DIR, item), item)
        for path in APP_EXTRA:
            add_tree(tf, path, os.path.basename(path))

    with open(out_app, "rb") as fh:
        ck = hashlib.md5(fh.read()).hexdigest()
    print("    app.tgz = %d 字节  md5 = %s" % (os.path.getsize(out_app), ck))

    # 2) manifest
    with open(os.path.join(APP, "manifest"), encoding="utf-8") as fh:
        man = fh.read()
    man = re.sub(r"(?m)^version\s*=.*$", "version               = %s" % ver, man)
    man = re.sub(r"(?m)^checksum\s*=.*\n?", "", man)
    if not man.endswith("\n"):
        man += "\n"
    man += "checksum              = %s\n" % ck
    with open(out_manifest, "w", encoding="utf-8") as fh:
        fh.write(man)
    print("### 2) manifest 版本 %s  checksum = %s" % (ver, ck))

    # 3) fpk
    print("### 3) 打包 fpk")
    if os.path.exists(out_fpk):
        os.unlink(out_fpk)
    with tarfile.open(out_fpk, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        for item in FPK_ITEMS:
            p = os.path.join(APP, item)
            if item == "app.tgz":
                p = out_app
            elif item == "manifest":
                p = out_manifest
            add_tree(tf, p, item)

    h = hashlib.sha256()
    with open(out_fpk, "rb") as fh:
        h.update(fh.read())
    with open(out_fpk + ".sha256", "w", encoding="utf-8") as fh:
        fh.write("%s  xieyin-%s.fpk\n" % (h.hexdigest(), ver))
    print("### 4) 产出")
    print("    %s  %d 字节" % (out_fpk, os.path.getsize(out_fpk)))
    print("    sha256 %s" % h.hexdigest())
    return 0


if __name__ == "__main__":
    sys.exit(main())
