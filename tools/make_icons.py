#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""生成「刮削工具」图标：256 / 64 两档，RGBA 圆角外透明。"""
import os
from PIL import Image, ImageDraw, ImageFilter

HERE = os.path.dirname(os.path.abspath(__file__))
SRC = os.path.join(HERE, "..", "src", "xieyin")
N = 1024


def lerp(a, b, t):
    return tuple(round(a[i] + (b[i] - a[i]) * t) for i in range(3))


def gradient():
    im = Image.new("RGB", (N, N))
    px = im.load()
    c0, c1 = (10, 29, 48), (13, 116, 144)
    for y in range(N):
        for x in range(N):
            px[x, y] = lerp(c0, c1, (x * 0.55 + y * 0.45) / (N - 1))
    return im


def rounded_mask(radius=190):
    m = Image.new("L", (N, N), 0)
    ImageDraw.Draw(m).rounded_rectangle([0, 0, N - 1, N - 1], radius=radius, fill=255)
    return m


def build():
    bg = gradient().convert("RGBA")
    bg.putalpha(rounded_mask())

    # 左上柔光
    glow = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    gd = ImageDraw.Draw(glow)
    gd.ellipse([-260, -320, 700, 520], fill=(120, 230, 255, 46))
    glow = glow.filter(ImageFilter.GaussianBlur(110))
    bg.alpha_composite(Image.composite(glow, Image.new("RGBA", (N, N), (0, 0, 0, 0)), rounded_mask()))

    layer = Image.new("RGBA", (N, N), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer, "RGBA")

    # 胶片框
    fx0, fy0, fx1, fy1 = 168, 232, 704, 792
    d.rounded_rectangle([fx0, fy0, fx1, fy1], radius=36,
                        fill=(255, 255, 255, 20), outline=(235, 248, 255, 190), width=16)
    # 齿孔
    for i in range(5):
        cy = fy0 + 62 + i * 104
        for cx in (fx0 + 34, fx1 - 34):
            d.rounded_rectangle([cx - 17, cy - 26, cx + 17, cy + 26], radius=10,
                                fill=(235, 248, 255, 200))
    # 画面分割线
    d.line([fx0 + 74, 512, fx1 - 74, 512], fill=(235, 248, 255, 120), width=8)

    # 放大镜手柄（先画，圆压在它上面）
    d.line([(748, 658), (886, 796)], fill=(255, 209, 102, 255), width=74)
    d.ellipse([886 - 44, 796 - 44, 886 + 44, 796 + 44], fill=(255, 209, 102, 255))
    # 镜片
    cx, cy, r = 668, 566, 218
    d.ellipse([cx - r, cy - r, cx + r, cy + r], fill=(150, 235, 255, 42))
    d.ellipse([cx - r, cy - r, cx + r, cy + r], outline=(255, 209, 102, 255), width=54)
    # 高光
    d.arc([cx - r + 34, cy - r + 34, cx + r - 34, cy + r - 34], 200, 265,
          fill=(255, 255, 255, 150), width=20)

    bg.alpha_composite(layer)

    out = []
    for size, name in ((256, "ICON_256.PNG"), (64, "ICON.PNG")):
        im = bg.resize((size, size), Image.LANCZOS)
        p = os.path.join(SRC, name)
        im.save(p)
        out.append(p)
    for size, name in ((256, "icon_256.png"), (64, "icon_64.png")):
        im = bg.resize((size, size), Image.LANCZOS)
        p = os.path.join(SRC, "app", "ui", "images", name)
        im.save(p)
        out.append(p)
    for p in out:
        print(p)


if __name__ == "__main__":
    build()
