#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""撷引 1.4.0 —— 国内直连多源竞速刮削。

流程：preflight 验证（带缓存）→ 所有入池源并发刮削 → 快源先写 nfo
      → 慢源回来补空字段 / 封面并更新 nfo → 慢源失败不影响已写结果。

全程零代理；只做元数据，magnet / torrent / 播放地址一律过滤。
CLI 参数与 jav_scrape.py 兼容，app_server.py 只需换脚本名。
"""
from __future__ import annotations

import argparse, os, re, shutil, sys, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jav_scrape as j            # 复用 build_nfo / ffprobe_info / crop_poster / code_of
import race_common as rc
from javdb_parse import parse_javdb
from jav321_parse import parse_jav321, make_cid

DELAY = 1.8


def check_code(body, code):
    """源可能模糊匹配（ZZZ-999 -> ZZZM-999），必须按页面原文校验。"""
    got = re.findall(r"\b([A-Z0-9]{2,6}-\d{2,5})\b",
                     body.decode("utf-8", "ignore").upper())
    return code.upper() in got


def scrape_source(name, code, cfg):
    ips = cfg["ips"]
    if name == "jav321":
        cid = make_cid(code)
        if not cid:
            raise RuntimeError("番号无法构造 cid")
        st, sz, body, via = rc.fetch_multi("www.jav321.com", "/video/" + cid, ips, 16)
        if st != 200:
            raise RuntimeError("/video/%s HTTP %s" % (cid, st))
        if not check_code(body, code):
            raise RuntimeError("番号不一致")
        d = parse_jav321(body, code)
    elif name == "javdb580":
        st, sz, body, via = rc.fetch_multi("javdb580.com", "/search?q=" + code, ips, 15)
        if st != 200:
            raise RuntimeError("搜索页 HTTP %s" % st)
        links = re.findall(r'href="(/v/[A-Za-z0-9]+)"', body.decode("utf-8", "ignore"))
        if not links:
            raise RuntimeError("搜索页无 /v/ 详情链接")
        st2, sz2, detail, via2 = rc.fetch_multi("javdb580.com", links[0], ips, 16)
        if st2 != 200:
            raise RuntimeError("详情页 HTTP %s" % st2)
        if not check_code(detail, code):
            raise RuntimeError("番号不一致（模糊匹配被拒绝）")
        d = parse_javdb(detail, code)
        d["_via"] = "搜索@%s 详情@%s" % (via, via2)
    else:
        raise RuntimeError("未知源 " + name)
    if not d.get("title"):
        raise RuntimeError("标题缺失")
    return d


def merge(fast, slow):
    """只补空字段；cover 例外用慢源大图覆盖。"""
    out = dict(fast)
    added = []
    for k, v in slow.items():
        if k.startswith("_") or k == "source":
            continue
        if not out.get(k) or k == "cover":
            out[k] = v
            added.append(k)
    return out, added


def write_artifacts(d, folder: Path, video: Path, args, cover_bytes=None):
    stem = video.stem
    info = {} if (args.no_probe or not j.media_ext.is_probeable(video.name)) \
        else j.ffprobe_info(video)
    nfo = folder / (stem + ".nfo")
    nfo.write_text(j.build_nfo(d, info, folder.name), encoding="utf-8")
    if cover_bytes and not args.no_images:
        full = folder / (stem + "-fanart.jpg")
        thumb = folder / (stem + "-thumb.jpg")
        poster = folder / (stem + "-poster.jpg")
        # 常规不覆盖已存在文件（PT 种子里可能有同名文件）；
        # 例外：自己刚写的半截 JPEG，用完整图自愈覆盖。
        if (not full.exists()) or (not rc.jpeg_ok(full.read_bytes())
                                   and len(cover_bytes) > len(full.read_bytes())):
            full.write_bytes(cover_bytes)
        if not thumb.exists() or (not rc.jpeg_ok(thumb.read_bytes())
                                  and len(cover_bytes) > len(thumb.read_bytes())):
            shutil.copyfile(full, thumb)
        if not poster.exists():
            j.crop_poster(full, poster)
    return nfo


def race_one(code, pool, folder, video, args, log):
    """快源先写 / 慢源补字段；慢源失败不影响已写 nfo。"""
    t0 = time.time()
    cfg = {x["name"]: dict(ips=x["ips"], mode=x["mode"]) for x in pool}
    merged = None
    cover_done = False
    timeline = []

    def run(n):
        s = time.time()
        try:
            return n, round(time.time() - s, 2), scrape_source(n, code, cfg[n]), ""
        except Exception as e:
            return n, round(time.time() - s, 2), None, str(e)

    with ThreadPoolExecutor(max_workers=max(1, len(pool))) as ex:
        futs = {ex.submit(run, n): n for n in cfg}
        for f in as_completed(futs):   # 等全部完成（慢源要回来补字段）
            n, el, d, err = f.result()
            rel = round(time.time() - t0, 2)
            if not d:
                timeline.append((rel, n, "FAIL", err))
                log.write("   [%6.2fs] %-10s 失败 %s\n" % (rel, n, err))
                continue
            d.pop("_via", None)
            if merged is None:
                merged = dict(d)
                write_artifacts(merged, folder, video, args)
                timeline.append((rel, n, "FIRST", list(merged.keys())))
                log.write("   [%6.2fs] %-10s 快源先写 nfo（%d 字段）\n" % (rel, n, len(merged)))
            else:
                merged, added = merge(merged, d)
                cov = rc.download_cover(d["cover"]) if d.get("cover") else None
                if cov:
                    cover_done = True
                write_artifacts(merged, folder, video, args, cov)
                timeline.append((rel, n, "MERGE", added))
                log.write("   [%6.2fs] %-10s 慢源补字段并更新 nfo %s 封面 %s\n" % (
                    rel, n, added, ("%dB" % len(cov)) if cov else "无"))

    # 兜底：慢源没下图成功 -> 用合并结果里的 cover 再试一次（只一次）
    if not args.no_images and not cover_done and merged and merged.get("cover"):
        cov = rc.download_cover(merged["cover"])
        if cov:
            write_artifacts(merged, folder, video, args, cov)
            log.write("   [兜底] 下图 %dB（%s）\n" % (len(cov), merged.get("source", "?")))

    if not merged:
        return "FAIL  %s: 全部源未刮出（%s）" % (
            folder.name, "; ".join(t[3] for t in timeline))
    first = next((t for t in timeline if t[2] == "FIRST"), None)
    return ("OK    %s <- %s | %s | 首出 %ss | 完成 %ss | 字段 %d | 导演 %s | 类别 %d" % (
        folder.name, merged.get("code", code), (merged.get("title") or "")[:40],
        first[0] if first else "-", round(time.time() - t0, 2), len(merged),
        "有" if merged.get("director") else "无", len(merged.get("genres") or [])))


def main():
    ap = argparse.ArgumentParser(description="撷引 国内直连多源竞速刮削")
    ap.add_argument("--root", default=os.environ.get("XIEYIN_ROOT", str(j.BASE)))
    ap.add_argument("--dirs", action="append", default=[])
    ap.add_argument("--only", default="")
    ap.add_argument("--scan", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--log", default="race_scrape.log")
    ap.add_argument("--via", choices=["auto", "direct", "proxy"], default="direct",
                    help="保留兼容；本引擎零代理，仅 proxy 时给出提示")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--no-cache", action="store_true", help="强制重跑 preflight")
    ap.add_argument("--preflight-ttl", type=int, default=rc.CACHE_TTL,
                    help="preflight 结果缓存秒数（对应配置 jav.preflight_ttl）")
    args = ap.parse_args()

    base = Path(args.root)
    if args.dirs:
        folders = [Path(d) for d in args.dirs]
    elif args.scan:
        folders = j.scan_video_folders(Path(args.scan))
    elif base.is_dir():
        folders = sorted([p for p in base.iterdir()
                          if p.is_dir() and p.name != "#recycle"], key=lambda p: p.name)
        if args.only:
            folders = [p for p in folders if p.name == args.only]
    else:
        folders = []
    if args.limit:
        folders = folders[:args.limit]
    print("共 %d 个文件夹待刮" % len(folders))
    if args.dry:
        for f in folders:
            print("   %-24s -> 番号 %s" % (f.name, j.code_of(f.name)))
        return 0
    if args.proxy or args.via == "proxy":
        print("   ! 本引擎为零代理直连，--proxy/--via proxy 已忽略；"
              "如需代理通道请在配置里切 jav.engine=legacy", file=sys.stderr)

    pool, rep = rc.preflight(force=args.no_cache, ttl=args.preflight_ttl, verbose=True)
    if not pool:
        print("!! 没有可用源，终止刮削", file=sys.stderr)
        return 1

    log = open(args.log, "a", encoding="utf-8")
    log.write("\n===== %s 开始，共 %d 个，入池 %s =====\n" % (
        datetime.now(), len(folders), [x["name"] for x in pool]))
    ok = miss = err = exist = 0
    for i, folder in enumerate(folders, 1):
        code = j.code_of(folder.name)
        video = None
        if folder.is_dir():
            video = next((f for f in sorted(folder.iterdir())
                          if f.is_file() and j.media_ext.is_video(f.name)), None)
        if not code:
            msg = "SKIP  %s: 番号解析失败" % folder.name
        elif not video:
            msg = "SKIP  %s: 没有视频文件" % folder.name
        elif not args.force and list(folder.glob("*.nfo")):
            msg = "EXIST %s: 已有 nfo" % folder.name
        else:
            try:
                msg = race_one(code, pool, folder, video, args, log)
            except Exception as e:
                msg = "ERROR %s: %s" % (folder.name, e)
        print("[%2d/%d] %s" % (i, len(folders), msg))
        log.write(msg + "\n")
        log.flush()
        if msg.startswith("OK"):
            ok += 1
        elif msg.startswith(("MISS", "SKIP")):
            miss += 1
        elif msg.startswith("EXIST"):
            exist += 1
        else:
            err += 1
        time.sleep(DELAY)
    print("\n成功 %d / 已存在 %d / 跳过 %d / 失败 %d  日志: %s" % (
        ok, exist, miss, err, args.log))
    log.close()
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
