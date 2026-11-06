#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""撷引（xieyin）FPK 打包脚本 —— 支持 x86 / arm 双架构。

用法：
    python3 tools/build_fpk.py 1.1.0                  # 同时产出 x86 与 arm
    python3 tools/build_fpk.py 1.1.0 --platform x86   # 只产 x86
    python3 tools/build_fpk.py 1.1.0 --platform arm   # 只产 arm

产出（每个架构一份）：
    dist/xieyin-<版本>-<平台>.fpk
    dist/xieyin-<版本>-<平台>.fpk.sha256

包结构：
    顶层      app.tgz  cmd/  config/  ICON.PNG  ICON_256.PNG  manifest
    app.tgz   xieyin/  ui/  config/
    manifest  platform = x86|arm；checksum = app.tgz 的 md5

要点：
    - app.tgz 与 x86/arm 完全相同（纯 Python 标准库 + shell，无原生二进制），
      只有 manifest 的 platform 行不同，因此打包一次、复用两份。
    - cmd/ 与 app/xieyin/*.sh 必须是 0755。
    - 不打包 __pycache__ / *.pyc / data/（配置和日志属于运行时）。
"""
import argparse
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

PLATFORMS = ("x86", "arm")


# ---------------------------------------------------------------- 纯函数

def normalize_platform(value: str) -> str:
    """fnOS manifest 的 platform 取值：x86 | arm | all。"""
    p = (value or "").strip().lower()
    if p not in PLATFORMS + ("all",):
        raise ValueError("platform 只能是 %s 或 all，收到 %r" % (", ".join(PLATFORMS), value))
    return p


def output_name(version: str, platform: str, ext: str = ".fpk") -> str:
    """产物文件名：xieyin-<版本>-<平台><ext>。"""
    return "xieyin-%s-%s%s" % (version, normalize_platform(platform), ext)


def render_manifest(text: str, version: str, platform: str, checksum: str) -> str:
    """按构建目标改写 manifest：version / platform / 唯一 checksum 行。"""
    platform = normalize_platform(platform)
    man = re.sub(r"(?m)^version\s*=.*$", "version               = %s" % version, text)
    man = re.sub(r"(?m)^platform\s*=.*$", "platform              = %s" % platform, man)
    man = re.sub(r"(?m)^checksum\s*=.*\n?", "", man)
    if not man.endswith("\n"):
        man += "\n"
    return man + "checksum              = %s\n" % checksum


# ---------------------------------------------------------------- 打包

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


def build_app_tgz(out_app: str) -> str:
    """打包 app.tgz，返回其 md5（所有架构共用）。"""
    if os.path.exists(out_app):
        os.unlink(out_app)
    with tarfile.open(out_app, "w:gz", format=tarfile.GNU_FORMAT) as tf:
        for item in APP_ITEMS:
            add_tree(tf, os.path.join(APP_DIR, item), item)
        for path in APP_EXTRA:
            add_tree(tf, path, os.path.basename(path))
    with open(out_app, "rb") as fh:
        return hashlib.md5(fh.read()).hexdigest()


def build_fpk(out_fpk: str, out_app: str, out_manifest: str) -> str:
    """打包一个 FPK，返回 sha256。"""
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
    return h.hexdigest()


def main() -> int:
    ap = argparse.ArgumentParser(description="打包 xieyin FPK（x86 / arm）")
    ap.add_argument("version")
    ap.add_argument("--platform", default="all",
                    help="x86 / arm / all（默认 all，两份都出）")
    ns = ap.parse_args()

    ver = ns.version
    platforms = list(PLATFORMS) if ns.platform == "all" else [normalize_platform(ns.platform)]

    out_dir = os.path.join(ROOT, "dist")
    os.makedirs(out_dir, exist_ok=True)
    out_app = os.path.join(APP, "app.tgz")

    print("### 1) 打包 app.tgz（多架构共用）")
    ck = build_app_tgz(out_app)
    print("    app.tgz = %d 字节  md5 = %s" % (os.path.getsize(out_app), ck))

    with open(os.path.join(APP, "manifest"), encoding="utf-8") as fh:
        man_text = fh.read()

    tmpdir = tempfile.mkdtemp(prefix="xieyin-fpk-")
    for plat in platforms:
        out_fpk = os.path.join(out_dir, output_name(ver, plat))
        out_manifest = os.path.join(tmpdir, "manifest-%s" % plat)
        with open(out_manifest, "w", encoding="utf-8") as fh:
            fh.write(render_manifest(man_text, ver, plat, ck))
        print("### 2) 打包 fpk [%s]" % plat)
        sha = build_fpk(out_fpk, out_app, out_manifest)
        with open(out_fpk + ".sha256", "w", encoding="utf-8") as fh:
            fh.write("%s  %s\n" % (sha, os.path.basename(out_fpk)))
        print("    %s  %d 字节" % (out_fpk, os.path.getsize(out_fpk)))
        print("    sha256 %s" % sha)
    return 0


if __name__ == "__main__":
    sys.exit(main())
