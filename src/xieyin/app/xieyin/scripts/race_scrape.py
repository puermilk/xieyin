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

import argparse, os, re, shutil, sys, threading, time
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime
from pathlib import Path

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import jav_scrape as j            # 复用 build_nfo / ffprobe_info / crop_poster / code_of
import race_common as rc
import actor_names
import actor_photo_sources as aps
import copy_scraped
import fnos_meta as fm
from javdb_parse import parse_javdb
from jav321_parse import parse_jav321, make_cid

DELAY = 1.8

_ACTOR_LOCAL = threading.local()


def _actor_session():
    """线程内复用的 JavBus Session（演员图防盗链，必须走真实通道）。"""
    s = getattr(_ACTOR_LOCAL, "s", None)
    if s is None:
        s = j.Session(validate=False)
        s.ips = list(j.KNOWN_IPS)
        _ACTOR_LOCAL.s = s
    return s


def _actor_img_ok(b: bytes) -> bool:
    """演员图常只有 5~10KB，不能用封面那套 >10KB 的 jpeg_ok。"""
    return bool(b) and len(b) > 500 and b[:3] == b"\xff\xd8\xff"


def check_code(body, code):
    """源可能模糊匹配（ZZZ-999 -> ZZZM-999），必须按页面原文校验。

    除常规 XXX-NNN，还要认日期型（112312_786 / 071518-714）和
    Tokyo Hot（n0502）；否则这些番号在 javbus 明明有页面也会被判"不一致"。
    """
    txt = body.decode("utf-8", "ignore").upper()
    got = re.findall(r"\b([A-Z0-9]{2,6}-\d{2,5})\b", txt)
    got += re.findall(r"\b(\d{6}[-_]\d{2,3})\b", txt)
    got += [m.lower() for m in re.findall(r"\b([NK]\d{3,5})\b", txt)]
    return code.upper() in got or code.lower() in got


def folders_for(base, dirs, scan):
    """单元收集的目录枚举（问题②）：每视频一个单元的候选文件夹列表。

    平铺布局（根目录直接放视频）下 scan 的叶子枚举已含根目录，
    此前再前置一次 base 会让每个视频进两个单元（刮两遍）——去重兜底。
    """
    if dirs:
        return [Path(d) for d in dirs]
    if scan:
        folders = j.scan_video_folders(Path(scan))
    elif base.is_dir():
        folders = sorted([p for p in base.iterdir()
                          if p.is_dir() and p.name != "#recycle"], key=lambda p: p.name)
    else:
        folders = []
    # 根目录直接放的视频也进单元（base 自身有视频就并入候选）
    if base.is_dir():
        if any(f.is_file() and j.media_ext.is_video(f.name) for f in base.iterdir()):
            folders = [base] + folders
    return list(dict.fromkeys(folders))


def fetch_javbus(code, ips):
    """JavBus 真实通道：legacy Session（固定 IP 直连 + 年龄墙换 IP 重试）。"""
    sess = j.Session(validate=False)
    if ips:
        sess.ips = [i for i in ips if i] or sess.ips
    return sess.request("/" + code)


# 日期型番号（横线形态）：MMDDYY-NNN = Caribbeancom 专属编号。
# 这类在 javbus/javdb/javdatabase 全部 404，只有官网有，故单独路由。
# 注意：下划线形态（MMDDYY_NNN，1pondo/10musume 系）**不在此列**——
# 实测 javbus 对 112312_786 / 072916_133 返回 200 有完整数据，必须走正常源。
DATE_CODE_RE = re.compile(r"^\d{6}-\d{2,3}$")


def scrape_caribbeancom(code, ips):
    """日期型番号 → Caribbeancom 官网（页面是 EUC-JP，不是 Shift_JIS）。

    只取标题/发行日/演员/时长/封面 URL，绝不提取 mp4/m3u8 播放链接。
    作品下架时官网直接断连（HTTP 0），所以失败非常快。
    """
    path = "/moviepages/%s/index.html" % code
    st, sz, body, via = rc.fetch_multi("www.caribbeancom.com", path, ips, 14, min_size=3000)
    if st != 200:
        raise RuntimeError("caribbeancom /%s HTTP %s" % (code, st))
    html = body.decode("euc-jp", "ignore")
    d = {"code": code}
    m = re.search(r"<h1[^>]*>(.*?)</h1>", html, re.S)
    title = re.sub(r"<[^>]+>", "", m.group(1)).strip() if m else ""
    if not title or "NOT FOUND" in title.upper():
        raise RuntimeError("官网无此作品（已下架）")
    # 官网路径对任意编号都返回 200（集团内同编号可能指向别的作品），
    # 必须校验页面里真的出现该番号，否则会拿错标题污染 nfo。
    if not check_code(body, code):
        raise RuntimeError("官网页面与番号不符")
    d["title"] = title
    m = re.search(r"(20\d{2})[/-](\d{1,2})[/-](\d{1,2})", html)
    if m:
        d["releasedate"] = "%s-%02d-%02d" % (m.group(1), int(m.group(2)), int(m.group(3)))
        d["release"] = d["releasedate"]
    # 主演：只取「出演」spec-content 里的 itemprop=actor，不能抓 player-rec-actress
    #（那是页面底部的相关推荐，会把 3~5 个无关女优写进 NFO）。
    actors = []
    m = re.search(r'<span class="spec-title">出演</span>\s*<span class="spec-content">(.*?)</span>\s*</li>',
                  html, re.S)
    if m:
        actors = [re.sub(r"<[^>]+>", "", x).strip()
                  for x in re.findall(r'itemprop="name"[^>]*>(.*?)</span>', m.group(1), re.S)]
    if not actors:
        # 兜底：只取主区块第一个 actress，不再全页扫推荐位
        m = re.search(r'<span class="spec-title">出演</span>(.*?)</li>', html, re.S)
        if m:
            a = re.search(r'<(?:a|span)[^>]*(?:itemprop="name"|class="[^"]*actress[^"]*")[^>]*>([^<]{1,30})',
                          m.group(1), re.S)
            if a:
                actors = [a.group(1).strip()]
    actors = actor_names.normalize_list(actors)
    if actors:
        d["actors"] = actors
    m = re.search(r"(/moviepages/[^\"\']+/images/l_l\.jpg)", html)
    if m:
        d["cover"] = "https://www.caribbeancom.com" + m.group(1)
    # 时长：正文用全角数字（２９分），必须转半角；且要保持字符串，
    # 下游 build_nfo 会对它做 re.sub（传 int 会 TypeError）。
    m = re.search(r"([0-9０-９]{2,3})\s*(?:分|min)", html)
    if m:
        d["runtime"] = m.group(1).translate(
            str.maketrans("０１２３４５６７８９", "0123456789"))
    return d


def scrape_source(name, code, cfg):
    ips = cfg["ips"]
    if name == "javbus":
        st, body = fetch_javbus(code, ips)
        if st != 200 or not body:
            raise RuntimeError("javbus /%s HTTP %s" % (code, st))
        if not check_code(body, code):
            raise RuntimeError("番号不一致")
        d = j.parse_page(body.decode("utf-8", "ignore"))
        if not d.get("title"):
            raise RuntimeError("标题缺失")
        return d
    if name == "caribbeancom":
        return scrape_caribbeancom(code, ips)
    if name == "javdatabase":
        st, sz, body, via = rc.fetch_multi(
            "www.javdatabase.com", "/movies/" + code.lower() + "/", ips, 16)
        if st != 200:
            raise RuntimeError("/movies/%s/ HTTP %s" % (code.lower(), st))
        from javdatabase_parse import parse_javdatabase
        d = parse_javdatabase(body, code)
        if not d.get("title") and not d.get("actors"):
            raise RuntimeError("详情页无元数据字段")
        d["_via"] = "tier-2备胎"
        return d
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


def _jpeg_bytes(im, quality=92):
    import io
    buf = io.BytesIO()
    im.save(buf, "JPEG", quality=quality)
    return buf.getvalue()


def make_thumb_bytes(poster_im, w=190, h=270):
    """A2：thumb 由 poster 缩小（约 190×270，2:3），不再用封面原图。"""
    from PIL import Image
    return _jpeg_bytes(poster_im.resize((w, h), Image.LANCZOS), 88)


def _vignette(bg, edge=0.85, min_ratio=0.35):
    """径向暗角：中心不变，边缘亮度 ×edge；四角/中心比低于 min_ratio 时自动提亮边缘。

    深色封面先压暗再乘暗角会塌成黑框（实测四角/中心 0.19~0.22），
    这里先量一次四角/中心比，再反推需要的边缘系数，避免死角。
    """
    from PIL import Image, ImageChops
    W, H = bg.size
    grad = Image.radial_gradient("L").resize((W, H), Image.BILINEAR)
    floor = edge
    try:
        sm = bg.resize((max(8, W // 10), max(8, H // 10)), Image.BILINEAR).convert("L")
        sw, sh = sm.size
        px = sm.load()
        cw, ch = max(1, sw // 5), max(1, sh // 5)

        def mean_box(x0, y0, x1, y1):
            s = n = 0
            for x in range(x0, x1):
                for y in range(y0, y1):
                    s += px[x, y]
                    n += 1
            return s / max(1, n)

        center = mean_box(sw // 2 - cw // 2, sh // 2 - ch // 2,
                          sw // 2 + cw // 2 + 1, sh // 2 + ch // 2 + 1)
        corner = sum(mean_box(x0, y0, x0 + cw, y0 + ch) for x0, y0 in (
            (0, 0), (sw - cw, 0), (0, sh - ch), (sw - cw, sh - ch))) / 4.0
        if center > 8 and corner > 0:
            r0 = corner / center
            if r0 * floor < min_ratio:
                floor = min(0.97, max(floor, min_ratio / max(r0, 1e-6)))
    except Exception:
        floor = edge
    lut = [255 - int(round(255 * (1.0 - floor) * i / 255.0)) for i in range(256)]
    mask = grad.point(lut).convert("RGB")
    return ImageChops.multiply(bg, mask)


def make_fanart_bytes(poster_im, W=1920, H=1080):
    """A2：fanart 1920×1080 标准 16:9。竖版海报模糊铺底 + 居中放完整海报。

    1. 取 poster 等比放大铺满画布 -> GaussianBlur(≈40) -> 亮度 ×0.62 -> 叠加自适应径向暗角；
    2. 完整 poster 等比缩放到高度 ≈1000px，居中粘贴，右下角不加任何水印/文字。
    """
    from PIL import Image, ImageFilter, ImageEnhance
    iw, ih = poster_im.size
    scale = max(W / iw, H / ih)
    nw, nh = int(iw * scale), int(ih * scale)
    bg = poster_im.resize((nw, nh), Image.LANCZOS)
    left = (nw - W) // 2; top = (nh - H) // 2
    bg = bg.crop((left, top, left + W, top + H))
    bg = bg.filter(ImageFilter.GaussianBlur(40))
    bg = ImageEnhance.Brightness(bg).enhance(0.62)
    bg = _vignette(bg)                         # 轻微暗角，突出中间海报
    ph = 1000                                  # 完整 poster 高度 ≈1000px
    pw = max(1, int(round(iw * ph / ih)))
    fg = poster_im.resize((pw, ph), Image.LANCZOS)
    bg.paste(fg, ((W - pw) // 2, (H - ph) // 2))
    return _jpeg_bytes(bg, 90)


def make_fanart_from_still(still_im, W=1920, H=1080, edge=0.92):
    """16:9 剧照直接做背景（素人/配信片源）：居中裁到 16:9 再放大，轻微暗角压一下四角。

    这类源本身就是横向画面，模糊铺底反而丢信息；直接放大比「模糊海报铺底」更自然。
    """
    from PIL import Image, ImageFilter
    im = still_im.convert("RGB")
    iw, ih = im.size
    tw = W / H
    if iw / ih > tw:
        nw = int(ih * tw); left = (iw - nw) // 2
        im = im.crop((left, 0, left + nw, ih))
    else:
        nh = int(iw / tw); top = int((ih - nh) * 0.45)
        im = im.crop((0, top, iw, top + nh))
    im = im.resize((W, H), Image.LANCZOS)
    im = im.filter(ImageFilter.SMOOTH_MORE)      # 800×450 放大后压一下块感
    return _jpeg_bytes(_vignette(im, edge=edge), 90)


def make_poster_from_still(still_im, w=379, h=538):
    """16:9 剧照做 2:3 竖版海报：整幅居中 + 模糊铺底（不裁切，避免把人物头/脸切掉）。

    素人/配信片源只有 16:9 剧照，直接居中裁 2:3 会切掉两侧人物（实测第一行头都被切没了），
    所以改成「完整画面缩到宽度 + 上下模糊铺底」，与影视库常见的 landscape→poster 处理一致。
    """
    from PIL import Image, ImageFilter, ImageEnhance
    im = still_im.convert("RGB")
    bg = im.resize((w, h), Image.LANCZOS).filter(ImageFilter.GaussianBlur(18))
    bg = ImageEnhance.Brightness(bg).enhance(0.6)
    fw = w
    fh = max(1, int(round(im.size[1] * fw / max(1, im.size[0]))))
    if fh > h:
        fh = h
        fw = max(1, int(round(im.size[0] * fh / max(1, im.size[1]))))
    fg = im.resize((fw, fh), Image.LANCZOS)
    bg.paste(fg, ((w - fw) // 2, (h - fh) // 2))
    return bg


def write_artifacts(d, folder: Path, video: Path, args, cover_bytes=None, cover_kind=None):
    stem = video.stem
    info = {} if (args.no_probe or not j.media_ext.is_probeable(video.name)) \
        else j.ffprobe_info(video)
    nfo = folder / (stem + ".nfo")
    # smart 策略看的是「本轮之前」是否已有本工具的 nfo（<num> 签名）
    had_our_nfo = False
    if nfo.exists():
        try:
            txt = nfo.read_text(encoding="utf-8")
            # 专属签名优先；兼容旧版（无签名）：customrating+num 是本工具的特征组合
            had_our_nfo = ("xieyin:generated" in txt
                           or ("<customrating>NC-17</customrating>" in txt and "<num>" in txt))
        except OSError:
            had_our_nfo = False
    if cover_bytes and not args.no_images:
        policy = str(getattr(args, "image_policy", "") or "keep")
        allow = bool(getattr(args, "force_images", False)) or policy == "replace" \
            or (policy == "smart" and had_our_nfo)
        full = folder / (stem + "-fanart.jpg")
        thumb = folder / (stem + "-thumb.jpg")
        poster = folder / (stem + "-poster.jpg")
        # 无前缀别名（与电影侧命名统一，媒体库两种都认）
        alias_poster = folder / "poster.jpg"
        alias_folder = folder / "folder.jpg"

        def allowed(path, data_len):
            """不存在→写；自己写坏的半截图→自愈；allow→覆盖；否则跳过（PT 保护）。"""
            if not path.exists():
                return True
            cur = path.read_bytes()
            return (not rc.jpeg_ok(cur) and data_len > len(cur)) or allow

        # A2：按用途生成三张图（不再把封面包原样落盘）。
        # cover_kind：vertical/pl_right/still -> poster+thumb+fanart；lowres/None -> 仅 poster 回退
        do_full = cover_kind in ("vertical", "pl_right", "still")
        poster_im = None
        still_im = None
        poster_bytes = None
        try:
            from PIL import Image, ImageFile
            import io
            ImageFile.LOAD_TRUNCATED_IMAGES = True   # 图床限速常返回半截 JPEG
            with Image.open(io.BytesIO(cover_bytes)) as im:
                im.load()
                if cover_kind == "still":
                    still_im = im.convert("RGB").copy()     # 剧照直接做背景
                    poster_im = make_poster_from_still(still_im)
                else:
                    poster_im = j._poster_im(im, 379, 538, right_half=(cover_kind == "pl_right"))
            poster_bytes = _jpeg_bytes(poster_im, 92)
        except Exception as e:
            print(f"   ! 海报生成失败 {video.name}: {e}", file=sys.stderr)

        if poster_bytes:
            if allowed(poster, len(poster_bytes)):
                poster.write_bytes(poster_bytes)
            if do_full:
                thumb_bytes = make_thumb_bytes(poster_im)
                if allowed(thumb, len(thumb_bytes)):
                    thumb.write_bytes(thumb_bytes)
                try:
                    if cover_kind == "still" and still_im is not None:
                        fanart_bytes = make_fanart_from_still(still_im)
                    else:
                        fanart_bytes = make_fanart_bytes(poster_im)
                except Exception:
                    fanart_bytes = None          # fanart 生成失败不写，不回退原图
                if fanart_bytes and allowed(full, len(fanart_bytes)):
                    full.write_bytes(fanart_bytes)
        # 1.7.4：别名只在单视频目录写——多视频/平铺目录里别名会互相覆盖
        nvid = sum(1 for x in folder.iterdir()
                   if x.is_file() and j.media_ext.is_video(x.name))
        if nvid <= 1:
            for al in (alias_poster, alias_folder):
                if allowed(al, len(poster_bytes or cover_bytes)) and poster.exists():
                    shutil.copyfile(poster, al)
    # 演员头像：下载 JavBus 女优图，NFO 里写绝对路径（新条目相对路径会失败）
    if not args.no_images and d.get("actor_images"):
        thumbs = d.setdefault("actor_thumbs", {})
        for ai in d["actor_images"]:
            name = (ai.get("name") or "").strip()
            url = (ai.get("url") or "").strip()
            if not name or name in thumbs:
                continue
            safe = re.sub(r'[\\/:*?"<>|]', "_", name)[:60] or "actor"
            fp = folder / (stem + "-actor-" + safe + ".jpg")
            if fp.exists() and _actor_img_ok(fp.read_bytes()):
                thumbs[name] = str(fp.resolve())
                continue
            raw, used, source = aps.resolve(
                name, url,
                primary_fetch=lambda u: _actor_session().fetch_url(
                    u, referer="https://www.javbus.com/"))
            if not _actor_img_ok(raw):
                # 最后兜底：从已下载封面裁头像
                pdata = b""
                try:
                    pp = folder / (stem + "-poster.jpg")
                    if pp.exists():
                        pdata = pp.read_bytes()
                except OSError:
                    pass
                if not pdata and cover_bytes:
                    pdata = cover_bytes
                raw = aps.avatar_from_cover(pdata)
                source = "poster"
            if _actor_img_ok(raw):
                fp.write_bytes(raw)
                thumbs[name] = str(fp.resolve())
                if source != "primary":
                    print("   演员图兜底 %s <- %s" % (name, source))
            time.sleep(0.25)
    # 尊重既有刮削（PT 资源常自带发布组 nfo）：不是本工具写的 nfo 默认不覆盖，
    # 本轮只补缺失/劣质的图片。需要强制重写时加 --force。
    if nfo.exists() and not had_our_nfo and not getattr(args, "force", False):
        return nfo
    # nfo 最后写：此时图片已落盘，<art> 才能按真实文件生成（避免飞牛错配同目录其它条目的封面）
    nfo.write_text(j.build_nfo(d, info, folder.name, stem, folder), encoding="utf-8")
    return nfo



def pick_poster_url(cands):
    """问题①封面择优：竖版大图（jdbstatic 等）优先，DMM ps 仅 147x200 兜底。"""
    ok = [c for c in (cands or [])
          if isinstance(c, str) and c.startswith("http") and not rc._BAD.search(c)]
    for c in ok:
        if "pics.dmm." not in c:
            return c
    return ok[0] if ok else None


def pick_backdrop_url(cands):
    """问题①背景图择优：TMDB backdrop 优先；DMM ps -> pl 推导（pl=800x538 横版实测）。"""
    ok = [c for c in (cands or [])
          if isinstance(c, str) and c.startswith("http") and not rc._BAD.search(c)]
    for c in ok:
        if "image.tmdb.org" in c:
            return c
    for c in ok:
        if "pics.dmm." in c and c.endswith("ps.jpg"):
            return c[:-len("ps.jpg")] + "pl.jpg"
    return None


def cover_candidates(cover_urls, merged):
    """封面候选序列（A1）：竖版大图优先 -> 横版包装图(DMM pl)次选 -> 低分辨率 ps 兜底。

    select_cover 会按实际像素判断（竖版/横版包装/低分辨率拒收），不靠 URL 猜，
    所以这里只需按「期望质量」排序；select_cover 最多取前 3 个。
    """
    cid = make_cid((merged or {}).get("code") or "")
    seq = []
    # 竖版大图优先：javbus / jdbstatic 等非 DMM 源封面（多为竖版正封面）
    seq.append(pick_poster_url(cover_urls))
    seq += list(cover_urls or [])
    if merged:
        seq.append(merged.get("cover"))
    # 横版包装图（DMM pl 800×538，左右双封面）作为次选 -> select_cover 取右半张
    if cid:
        for host, pre in (("pics.dmm.com", "/digital/video/"),
                          ("imgsrc.dmm.com", "/pics/digital/video/")):
            seq.append("https://%s%s%s/%spl.jpg" % (host, pre, cid, cid))
    # 低分辨率 ps（147×200）兜底 -> select_cover 会拒收，仅留作 poster 回退
    if cid:
        for host, pre in (("pics.dmm.com", "/digital/video/"),
                          ("imgsrc.dmm.com", "/pics/digital/video/")):
            seq.append("https://%s%s%s/%sps.jpg" % (host, pre, cid, cid))
    out = []
    for u in seq:
        if isinstance(u, str) and u.startswith("http") and not rc._BAD.search(u) \
                and u not in out:
            out.append(u)
    return out


MIN_COVER_WIDTH = 300          # A1：宽度 < 300px 拒收（ADN-556 的 ps.jpg 147×200）
MIN_COVER_BYTES = 30 * 1024    # A1：文件 < 30KB 拒收
PL_RATIO_LO, PL_RATIO_HI = 1.35, 1.55   # DMM pl 横版包装图比例区间
STILL_RATIO_HI = 2.00          # 16:9 剧照上限（素人/配信片源，800×450 / 960×540）


def _cover_dims(data):
    """解码 JPEG 拿到 (宽, 高)；失败返回 (0, 0)。"""
    try:
        from PIL import Image
        import io
        with Image.open(io.BytesIO(data)) as im:
            return im.size
    except Exception:
        return (0, 0)


def select_cover(urls, log, code=""):
    """A1 选图：最多尝试 3 个候选，按实际像素判断，命中即停（不靠 URL 猜）。

    返回 (cover_bytes, used_url, kind)：
      kind='vertical'  竖版大图（width<height 且 width≥300px）——最优 poster 源
      kind='pl_right'  横版包装图（比例≈1.35–1.55，DMM pl 这类）——取右半张得竖版块
      kind='still'     16:9 剧照（比例 1.55–2.00，素人/配信片源）——整幅裁 2:3 做 poster，本身做 fanart
      kind='lowres'    全部候选不合格但至少下载到一张（低分辨率/超宽）——仅 poster 回退
      kind=None        一张都没下载成功
    width<300px 或文件<30KB 一律拒收；全部不合格写 WARN cover_lowres。
    """
    tried = []
    lowres = None
    for u in (urls or [])[:3]:
        host = u.split("/")[2] if "://" in u else u
        cov = rc.download_cover(u)
        if not cov:
            tried.append("%s#下载失败" % host)
            time.sleep(0.4)
            continue
        w, h = _cover_dims(cov)
        if w < MIN_COVER_WIDTH or len(cov) < MIN_COVER_BYTES:
            lowres = (cov, u)                       # 留作 poster 回退
            if log:
                log.write("   [封面] 拒收 %s %dx%d %dB（低分辨率）\n" % (host, w, h, len(cov)))
            tried.append("%s#%dx%d" % (host, w, h))
            time.sleep(0.4)
            continue
        if w < h:
            return cov, u, "vertical"
        ratio = w / h
        if PL_RATIO_LO <= ratio <= PL_RATIO_HI:
            return cov, u, "pl_right"
        if ratio <= STILL_RATIO_HI:                 # 16:9 剧照（素人/配信片源）
            return cov, u, "still"
        lowres = (cov, u)                           # 超宽横幅等，留作回退
        if log:
            log.write("   [封面] 拒收 %s %dx%d（超宽比例 %.2f）\n" % (host, w, h, ratio))
        tried.append("%s#%dx%d" % (host, w, h))
        time.sleep(0.4)
    if log:
        log.write("   [封面] WARN cover_lowres %s（尝试 %s）\n" % (
            code, ", ".join(tried) or "无候选"))
    if lowres:
        return lowres[0], lowres[1], "lowres"
    return None, None, None


def export_target(args, folder, video):
    """export 模式落点：镜像源相对路径；视频不在 root 下则按文件夹名平铺。"""
    meta_dir = Path(getattr(args, "meta_dir", "") or "")
    root = Path(getattr(args, "root", "") or "")
    try:
        rel = video.parent.resolve().relative_to(root.resolve())
    except (ValueError, OSError):
        rel = Path(folder.name)
    out = meta_dir / rel
    out.mkdir(parents=True, exist_ok=True)
    return out


class _LockedLog:
    """多线程并发写日志时的串行化包装（race_one 内部也会写 log）。"""

    def __init__(self, f):
        self._f = f
        self._lock = threading.Lock()

    def write(self, s):
        with self._lock:
            self._f.write(s)
            self._f.flush()      # 逐行刷盘：否则界面/日志看不到实时进度

    def flush(self):
        with self._lock:
            self._f.flush()

    def close(self):
        with self._lock:
            self._f.close()


def emit_artifact(d, folder, video, args, cover_bytes=None, cover_kind=None):
    """方案A 产物分发（用户设计文档模式矩阵）：

    push    = 推送飞牛影视 API（POST /api/media/meta/update），PT 目录零写入
    export  = 落独立元目录（镜像源结构），PT 目录零写入
    preview = 只识别，零产出
    inplace = 就地写 nfo/封面进视频文件夹（1.7.0 起默认主模式）
    """
    sink = str(getattr(args, "sink", "inplace") or "inplace")
    parts = [s for s in sink.split("+") if s]
    if "preview" in parts:
        return None
    pushed = None
    if "push" in parts:
        payload = fm.build_payload(d, str(video.resolve()))
        ok, msg = fm.push_meta(getattr(args, "fnos_url", ""),
                               getattr(args, "fnos_token", ""), payload)
        print("   %s 推送飞牛 %s: %s" % (video.name, "成功" if ok else "失败", msg))
        pushed = bool(ok)
        if ok:
            fm.mark_pushed(video)      # 失败不标 -> 下次可重试
    if "export" in parts:
        write_artifacts(d, export_target(args, folder, video), video, args,
                        cover_bytes, cover_kind)
    if "inplace" in parts:
        write_artifacts(d, folder, video, args, cover_bytes, cover_kind)
    return pushed


def race_one(code, pool, folder, video, args, log):
    """快源先写 / 慢源补字段；慢源失败不影响已写产物。

    方案A：产物经 emit_artifact 分发（push 推飞牛 / export 落元目录 / preview 零产出 / inplace 就地写）。
    封面走 URL 择优：push 只推 URL 不下图；export/inplace 下载择优图落盘。
    """
    t0 = time.time()
    cfg = {x["name"]: dict(ips=x["ips"], mode=x["mode"]) for x in pool}
    # 日期型番号（071518-714 这类素人/無修正）：主流库 100% 404，
    # 直接只问 Caribbeancom 官网，拿不到就立刻放弃，不再浪费
    # javdb580/javdatabase 的 12~30 秒超时。
    if DATE_CODE_RE.match(code or ""):
        pool = [s for s in pool if s["name"] == "caribbeancom"]
        cfg = {k: v for k, v in cfg.items() if k == "caribbeancom"}
        if not pool:
            return "SKIP  %s: 日期型番号（%s），Caribbeancom 未入池" % (folder.name, code)
    merged = None
    cover_done = False
    cover_size = 0
    cover_urls = []          # 全部源的封面 URL 候选（问题①择优素材）
    push_results = []        # 每次 emit 的推送结果（None=非推送）
    timeline = []
    sink = str(getattr(args, "sink", "inplace") or "inplace")

    def run(n):
        s = time.time()
        try:
            return n, round(time.time() - s, 2), scrape_source(n, code, cfg[n]), ""
        except Exception as e:
            return n, round(time.time() - s, 2), None, str(e)

    def emit(d, cov, kind=None):
        # 问题①：poster/backdrop URL 择优注入（build_payload 直接消费）
        d["poster_url"] = pick_poster_url(cover_urls) or d.get("cover") or ""
        d["backdrop_url"] = pick_backdrop_url(cover_urls) or ""
        r = emit_artifact(d, folder, video, args, cov, kind)
        if r is not None:
            push_results.append(r)

    # 1.9.4：顺序兜底（不再多源竞速合并）——按 pool 顺序逐个尝试，
    # 第一个命中就写 nfo 收工，不再等其它源回来补字段。
    # javbus 1~2s 命中即结束；javdb580 只在 javbus 没找到时才上场兜底。
    # 代价：拿不到 javdb580 独有的 rating/个别系列名（用户已确认接受）。
    for _src in pool:
        n = _src["name"]
        if n not in cfg:
            continue
        n, el, d, err = run(n)
        rel = round(time.time() - t0, 2)
        if not d:
            timeline.append((rel, n, "FAIL", err))
            log.write("   [%6.2fs] %-10s 未命中 %s\n" % (rel, n, err))
            continue
        d.pop("_via", None)
        if d.get("cover") and d["cover"] not in cover_urls:
            cover_urls.append(d["cover"])
        merged = dict(d)
        emit(merged, None)
        timeline.append((rel, n, "FIRST", list(merged.keys())))
        log.write("   [%6.2fs] %-10s 命中并写入 nfo（%d 字段）\n" % (rel, n, len(merged)))
        break                      # 成功即停

    # 兜底：慢源没下图成功 -> A1 选图（最多 3 候选，按像素判断）
    if sink != "push" and not args.no_images and not cover_done and merged:
        cov, used, kind = select_cover(cover_candidates(cover_urls, merged), log,
                                       code=(merged.get("code") or ""))
        if cov:
            cover_size = len(cov)
            emit(merged, cov, kind)
            log.write("   [兜底] 下图 %dB（%s，%s）\n" % (
                len(cov), (used or "").split("/")[2] if used and "://" in used else "?",
                {"vertical": "竖版大图", "pl_right": "横版包装右半",
                 "lowres": "低分辨率回退"}.get(kind, kind)))

    if not merged:
        return "FAIL  %s: 全部源未刮出（%s）" % (
            folder.name, "; ".join(t[3] for t in timeline))
    # 纯推送模式：推送全失败 = 失败（元数据未落地）；有 export/inplace 兜底则算成功
    if "push" in sink and "export" not in sink and "inplace" not in sink \
            and push_results and not any(push_results):
        return "FAIL  %s: 刮削成功但推送失败（元数据未落地）" % video.name
    copy_root = str(getattr(args, "copy_root", "") or "").strip()
    if copy_root and sink != "preview":
        try:
            copy_scraped.copy_scraped(merged.get("code") or code, folder, video,
                                      copy_root, log=log.write)
        except Exception as e:
            log.write("   [复制] WARN %s\n" % e)
    first = next((t for t in timeline if t[2] == "FIRST"), None)
    want_img = sink != "push" and not args.no_images
    return ("OK    %s <- %s | %s | 首出 %ss | 完成 %ss | 字段 %d | 导演 %s | 类别 %d%s" % (
        folder.name, merged.get("code", code), (merged.get("title") or "")[:40],
        first[0] if first else "-", round(time.time() - t0, 2), len(merged),
        "有" if merged.get("director") else "无", len(merged.get("genres") or []),
        (" | 封面 %dB" % cover_size) if (want_img and cover_size) else
        (" | 封面 无" if want_img else "")))


def main():
    ap = argparse.ArgumentParser(description="撷引 国内直连多源竞速刮削")
    ap.add_argument("--root", default=os.environ.get("XIEYIN_ROOT", str(j.BASE)))
    ap.add_argument("--dirs", action="append", default=[])
    ap.add_argument("--only", default="")
    ap.add_argument("--scan", default="")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--force-images", action="store_true",
                    help="允许覆盖已有图片（--force 只管 nfo）")
    ap.add_argument("--image-policy", choices=["keep", "replace", "smart"], default="keep",
                    help="图片覆盖策略：keep 不动已有 / replace 总是覆盖 / smart 只覆盖本工具写过的")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--log", default="race_scrape.log")
    ap.add_argument("--via", choices=["auto", "direct", "proxy"], default="direct",
                    help="保留兼容；本引擎零代理，仅 proxy 时给出提示")
    ap.add_argument("--proxy", default="")
    ap.add_argument("--jobs", type=int, default=int(os.environ.get("XIEYIN_JOBS", "2")),
                    help="条目级并发数（默认 3；1=完全串行）")
    ap.add_argument("--no-cache", action="store_true", help="强制重跑 preflight")
    ap.add_argument("--preflight-ttl", type=int, default=rc.CACHE_TTL,
                    help="preflight 结果缓存秒数（对应配置 jav.preflight_ttl）")
    ap.add_argument("--sink", default="inplace",
                    help="产物去向：push 推飞牛 API / export 落独立元目录 / "
                         "preview 只识别零产出 / inplace 就地写（默认）")
    ap.add_argument("--fnos-url", default=os.environ.get("XIEYIN_FNOS_URL", ""),
                    help="飞牛地址（sink=push 时必填）")
    ap.add_argument("--fnos-token", default=os.environ.get("XIEYIN_FNOS_TOKEN", ""),
                    help="飞牛访问令牌（Bearer）")
    ap.add_argument("--meta-dir", default=os.environ.get("XIEYIN_META_DIR", ""),
                    help="独立元数据目录（sink=export 时必填）")
    ap.add_argument("--copy-root", default=os.environ.get("XIEYIN_COPY_ROOT", ""),
                    help="刮削后复制到 <copy-root>/<番号>/（留空不复制）")
    args = ap.parse_args()
    bad = [s for s in str(args.sink).split("+")
           if s not in ("push", "export", "preview", "inplace")]
    if bad:
        ap.error("--sink 只能是 push/export/preview/inplace 的组合（+连接），收到: %s" % args.sink)

    base = Path(args.root)
    folders = folders_for(base, args.dirs, args.scan)
    units = j.collect_units(folders)
    if args.only:
        only = args.only
        units = [u for u in units if only in (u[0].name, u[1].stem, u[2])]
    if args.limit:
        units = units[:args.limit]
    print("共 %d 个刮削单元待处理" % len(units))
    if args.dry:
        for folder, video, code in units:
            print("   %-24s %-32s -> 番号 %s" % (folder.name, video.name, code))
        return 0
    if args.proxy or args.via == "proxy":
        print("   ! 本引擎为零代理直连，--proxy/--via proxy 已忽略；"
              "如需代理通道请在配置里切 jav.engine=legacy", file=sys.stderr)

    pool, rep = rc.preflight(force=args.no_cache, ttl=args.preflight_ttl, verbose=True)
    if not pool:
        print("!! 没有可用源，终止刮削", file=sys.stderr)
        return 1

    log = _LockedLog(open(args.log, "a", encoding="utf-8"))
    log.write("\n===== %s 开始，共 %d 个单元，入池 %s，sink=%s，并发=%s =====\n" % (
        datetime.now(), len(units), [x["name"] for x in pool],
        getattr(args, "sink", "inplace"), getattr(args, "jobs", 1)))
    ok = miss = err = exist = 0

    def _one(i, folder, video, code):
        # 已刮判定按 sink 分：push 看推送状态，export 看元目录 nfo，inplace 看同目录 nfo
        parts = [s for s in str(args.sink).split("+") if s]
        done = True
        if "push" in parts:
            done = done and fm.already_pushed(video)
        if "export" in parts:
            done = done and (export_target(args, folder, video)
                             / (video.stem + ".nfo")).exists()
        if "inplace" in parts:
            # nfo 在 + 封面在 才算完成；只有 nfo 的（发布组自带）要进去补封面
            nfo_ok = (folder / (video.stem + ".nfo")).exists()
            poster_ok = args.no_images or (folder / (video.stem + "-poster.jpg")).exists()
            done = done and nfo_ok and poster_ok
        if "preview" in parts:
            done = False
        if not code:
            msg = "SKIP  %s: 番号解析失败" % folder.name
        elif args.sink != "preview" and not args.force and done:
            msg = "EXIST %s: 已刮过" % video.name
        else:
            try:
                msg = race_one(code, pool, folder, video, args, log)
            except Exception as e:
                msg = "ERROR %s: %s" % (folder.name, e)
        time.sleep(DELAY)
        return "[%2d/%d] %s" % (i, len(units), msg), msg

    def _tally(msg):
        nonlocal ok, miss, err, exist
        if msg.startswith("OK"):
            ok += 1
        elif msg.startswith(("MISS", "SKIP")):
            miss += 1
        elif msg.startswith("EXIST"):
            exist += 1
        else:
            err += 1

    def _report(line, msg):
        print(line)
        log.write(msg + "\n")
        _tally(msg)

    jobs = max(1, int(getattr(args, "jobs", 1) or 1))
    if jobs <= 1:
        for i, (f, v, c) in enumerate(units, 1):
            _report(*_one(i, f, v, c))
    else:
        with ThreadPoolExecutor(max_workers=jobs) as ex:
            futs = [ex.submit(_one, i, f, v, c)
                    for i, (f, v, c) in enumerate(units, 1)]
            # 流式消费：谁先完成就先报，不再等全部 future 结束后一次性输出
            for fu in as_completed(futs):
                _report(*fu.result())
    log.flush()
    print("\n成功 %d / 已存在 %d / 跳过 %d / 失败 %d  日志: %s" % (
        ok, exist, miss, err, args.log))
    log.close()
    return 0 if err == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
