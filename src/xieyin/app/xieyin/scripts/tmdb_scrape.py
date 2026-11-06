#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""
tmdb_scrape.py — 用 TMDB 刮削远程共享里的“真电影”（#recycle 内）
数据源: www.themoviedb.org 网页（官方 API 域名 DNS 被污染，不依赖 key）
输出:   <视频名>.nfo + poster.jpg + folder.jpg + fanart.jpg + thumb.jpg

用法: python3 tmdb_scrape.py [--dry] [--root DIR] [--force] [--no-probe]
"""
import argparse, html, json, re, shutil, subprocess, sys, ssl, time, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

import media_ext

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
CTX = ssl.create_default_context()
TMDB = "https://www.themoviedb.org"
IMG = "https://image.tmdb.org/t/p/original"

def get(url, tries=3):
    """用 curl 抓（TMDB 会挡 python-urllib 的 TLS 指纹）"""
    for t in range(tries):
        try:
            r = subprocess.run(
                ["curl", "-sL", "--compressed", "-A", UA,
                 "-H", "Accept-Language: zh-CN,zh;q=0.9,en;q=0.8",
                 "-H", "Accept: text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
                 "--max-time", "45", url],
                capture_output=True, timeout=60)
            if r.returncode == 0 and r.stdout:
                return r.stdout
        except Exception:
            pass
        time.sleep(2 + t)
    print(f"   ! 请求失败 {url}", file=sys.stderr)
    return b""

def txt(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s or "")).strip()

# ---------------------------------------------------------------- 文件夹名解析
CJK = re.compile(r"[一-鿿-鿿぀-ヿ가-힯]")

def _clean_title(s):
    s = re.sub(r"[\[\]\(\)（）【】]", " ", s or "")
    s = re.sub(r"[._]+", " ", s)
    return re.sub(r"\s+", " ", s).strip(" -")


def parse_folder(name):
    """返回 (中文名, 英文名, 年份)。

    兼容这些常见写法：
      [中文名].Example.Movie.2024.2160p...
      示例电影.Example.Movie.2024...
      中文名.2023.1080p.BluRay
      Example.Movie.2025.1080p.WEBRip
      Movie (2024)
    """
    name = (name or "").strip()
    zh = ""
    rest = name
    m = re.match(r"^\[([^\]]+)\]\.?(.*)$", name)
    if m:
        zh, rest = m.group(1).strip(), m.group(2)
    else:
        head, sep, tail = name.partition(".")
        if sep and CJK.search(head) and not re.fullmatch(r"(19|20)\d{2}", head.strip()):
            zh, rest = head.strip(), tail

    tokens = [t for t in re.split(r"[.\s_]+", rest) if t]
    year = ""
    cut = None
    for i, t in enumerate(tokens):
        t2 = t.strip("[]()（）【】-")
        if re.fullmatch(r"(19|20)\d{2}", t2):
            year, cut = t2, i
            break

    en = _clean_title(" ".join(tokens[:cut]) if cut is not None else rest)
    en = re.sub(r"\s+(2160p|1080p|720p|WEB-DL|WEBRip|BluRay|REMUX|x265|x264|"
                r"HEVC|H265|HDR|DV|10bit).*", "", en, flags=re.I).strip()
    if not zh and not en:
        en = _clean_title(name)
    return zh, en, year


# ---------------------------------------------------------------- TMDB
def _norm(s):
    return re.sub(r"[^0-9a-z\u4e00-\u9fff]+", " ", (s or "").lower()).strip()


def search_movie(query, year):
    """搜索并按「年份 + 片名相似度」挑最像的一部，返回 (slug, id, title, year)。"""
    url = f"{TMDB}/search?query={urllib.parse.quote(query)}"
    page = get(url).decode("utf-8", "ignore")

    hits = []
    cards = re.split(r'<div[^>]*class="[^"]*comp:media-card', page)[1:]
    for card in cards:
        m = re.search(r'href="(/movie/(\d+)[^"]*)"[^>]*>\s*<h2[^>]*>(.*?)</h2>', card, re.S)
        if not m:
            continue
        slug, mid, title = m.group(1), m.group(2), txt(m.group(3))
        if not title or len(title) > 120:
            continue
        y = re.search(r'class="release_date[^"]*">\s*((?:19|20)\d{2})', card)
        hits.append((slug, mid, title, y.group(1) if y else ""))

    # 兼容页面结构变化：卡片切不出来时退回旧的链接扫描
    if not hits:
        for m in re.finditer(r'href="(/movie/(\d+)[^"]*)"[^>]*>(.*?)</a>', page, re.S):
            slug, mid, title = m.group(1), m.group(2), txt(m.group(3))
            if not title or len(title) > 120:
                continue
            tail = page[m.end():m.end() + 3000]
            y = re.search(r'class="release_date[^"]*">\s*((?:19|20)\d{2})', tail) \
                or re.search(r"\((19\d{2}|20\d{2})\)", tail)
            hits.append((slug, mid, title, y.group(1) if y else ""))

    seen, uniq = set(), []
    for h in hits:
        if h[1] not in seen:
            seen.add(h[1]); uniq.append(h)
    if not uniq:
        return None

    qn = _norm(query)
    STOP = {"the", "a", "an", "of", "and", "movie", "film"}
    q_tokens = [t for t in qn.split() if t not in STOP] or qn.split()
    qt = set(q_tokens)

    def coverage(h):
        tt = set(_norm(h[2]).split())
        return len(qt & tt) / len(qt) if qt else 0.0

    def score(h, idx):
        s = coverage(h) * 10
        if year and h[3]:
            try:
                diff = abs(int(h[3]) - int(year))
            except ValueError:
                diff = 99
            # 部分地区发行年差 1 年，按命中算
            s += 5 if diff <= 1 else -min(20, diff * 3)
        # TMDB 自己的相关度顺序仍然有用，但只作为小权重
        s += max(0.0, 5 - idx * 0.6)
        return s

    pool = [i for i, h in enumerate(uniq) if coverage(h) > 0] or list(range(len(uniq)))
    best = max(pool, key=lambda i: (score(uniq[i], i), -i))
    return uniq[best]


def movie_pages(slug, mid):
    """抓中文页 / 英文页 / 演员页（slug 形如 /movie/911430-f1）"""
    base = f"{TMDB}{slug if slug.startswith('/') else '/movie/' + slug}"
    zh = get(f"{base}?language=zh-CN").decode("utf-8", "ignore")
    en = get(f"{base}?language=en-US").decode("utf-8", "ignore")
    cast = get(f"{base}/cast?language=zh-CN").decode("utf-8", "ignore")
    return zh, en, cast

def parse_movie(zh, en, cast, mid):
    d = {"tmdbid": mid}
    m = re.search(r"<title>(.*?)</title>", zh, re.S)
    t = txt(m.group(1)) if m else ""
    m2 = re.search(r"^(.*?)\s*\((19\d{2}|20\d{2})\)", t)
    if m2:
        d["title"], d["year"] = m2.group(1).strip(), m2.group(2)
    else:
        d["title"], d["year"] = t.split("—")[0].strip(), ""
    m = re.search(r"<title>(.*?)</title>", en, re.S)
    te = txt(m.group(1)) if m else ""
    te = re.sub(r"\s*[\(（](19\d{2}|20\d{2})[\)）]", "", te.split("—")[0].split(" - ")[0])
    d["originaltitle"] = te.strip()

    m = re.search(r'<span class="certification">\s*([^<]+?)\s*</span>', zh)
    d["mpaa"] = m.group(1).strip() if m else ""
    m = re.search(r'<span class="release">\s*([^<]+?)\s*</span>', zh)
    rel = m.group(1).strip() if m else ""
    m = re.match(r"(\d{4}-\d{2}-\d{2})", rel)
    d["releasedate"] = m.group(1) if m else ""
    m = re.search(r'<span class="genres">(.*?)</span>', zh, re.S)
    d["genres"] = [txt(x) for x in re.findall(r"<a[^>]*>([^<]+)</a>", m.group(1))] if m else []
    m = re.search(r'<span class="runtime">\s*([^<]+?)\s*</span>', zh)
    rt = m.group(1).strip() if m else ""
    mins = 0
    mh = re.search(r"(\d+)h", rt); mm = re.search(r"(\d+)m", rt)
    if mh: mins += int(mh.group(1)) * 60
    if mm: mins += int(mm.group(1))
    d["runtime"] = str(mins) if mins else ""
    m = re.search(r'<h3 class="tagline"[^>]*>(.*?)</h3>', zh, re.S)
    d["tagline"] = txt(m.group(1)) if m else ""
    m = re.search(r'<div class="overview"[^>]*>\s*<p>(.*?)</p>', zh, re.S)
    d["plot"] = txt(m.group(1)) if m else ""
    m = re.search(r'data-percent="([\d.]+)"', zh)
    d["rating"] = f"{float(m.group(1))/10:.1f}" if m else ""

    d["directors"], d["writers"] = [], []
    for blk in re.findall(r'<li class="profile">(.*?)</li>', zh, re.S):
        nm = re.search(r'<a href="/person/[^"]+">([^<]+)</a>', blk)
        ch = re.search(r'<p class="character">(.*?)</p>', blk, re.S)
        if not nm:
            continue
        role = txt(ch.group(1)) if ch else ""
        if "Director" in role:
            d["directors"].append(txt(nm.group(1)))
        elif "Screenplay" in role or "Writer" in role or "Novel" in role:
            d.setdefault("writers", []).append(txt(nm.group(1)))

    cast_list = []
    for blk in re.split(r'<li data-order="', cast)[1:]:
        nm = re.search(r'<p><a href="/person/[^"]+">([^<]+)</a></p>', blk)
        ro = re.search(r'<p class="character">(.*?)</p>', blk, re.S)
        if nm:
            cast_list.append((txt(nm.group(1)), txt(ro.group(1)) if ro else ""))
        if len(cast_list) >= 15:
            break
    d["cast"] = cast_list

    imgs = re.findall(r'<meta property="og:image" content="(https://[^"]+/t/p/\w+/([A-Za-z0-9]+)\.jpg)"', zh)
    d["poster_path"] = imgs[0][1] if len(imgs) > 0 else ""
    d["backdrop_path"] = imgs[1][1] if len(imgs) > 1 else ""
    return d

# ---------------------------------------------------------------- nfo / fileinfo
def cdata(s):
    return "<![CDATA[" + (s or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"

def ffprobe_info(path):
    try:
        out = subprocess.run(["ffprobe", "-v", "quiet", "-print_format", "json",
                              "-show_format", "-show_streams", str(path)],
                             capture_output=True, timeout=180).stdout
        return json.loads(out or b"{}")
    except Exception:
        return {}

def fileinfo_xml(info):
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    if not v:
        return ""
    fmt = info.get("format", {})
    dur = fmt.get("duration") or v.get("duration") or "0"
    try:
        dur_i = int(float(dur))
    except Exception:
        dur_i = 0
    ar = ""
    if v.get("width") and v.get("height"):
        g = __import__("math").gcd(int(v["width"]), int(v["height"]))
        ar = f'{v["width"]//g}:{v["height"]//g}'
    fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or ""
    if "/" in str(fr):
        try:
            n_, d_ = str(fr).split("/"); fr = f"{float(n_)/float(d_):.5f}" if float(d_) else ""
        except Exception:
            fr = ""
    L = ["  <fileinfo>", "    <streamdetails>", "      <video>",
         f"        <codec>{v.get('codec_name','')}</codec>"]
    if v.get("bit_rate"): L.append(f"        <bitrate>{v['bit_rate']}</bitrate>")
    L += [f"        <width>{v.get('width','')}</width>",
          f"        <height>{v.get('height','')}</height>",
          f"        <aspect>{ar}</aspect>",
          f"        <aspectratio>{ar}</aspectratio>"]
    if fr: L.append(f"        <framerate>{fr}</framerate>")
    L += ["        <scantype>progressive</scantype>",
          "        <default>True</default>", "        <forced>False</forced>",
          f"        <duration>{dur_i}</duration>",
          f"        <durationinseconds>{dur_i}</durationinseconds>", "      </video>"]
    if a:
        L += ["      <audio>", f"        <codec>{a.get('codec_name','')}</codec>"]
        if a.get("bit_rate"): L.append(f"        <bitrate>{a['bit_rate']}</bitrate>")
        L += [f"        <language>{a.get('language','und')}</language>",
              f"        <channels>{a.get('channels','')}</channels>",
              f"        <samplingrate>{a.get('sample_rate','')}</samplingrate>",
              "        <default>True</default>", "        <forced>False</forced>",
              "      </audio>"]
    L += ["    </streamdetails>", "  </fileinfo>"]
    return "\n".join(L)

def build_nfo(d, info, video_name):
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = ['﻿<?xml version="1.0" encoding="utf-8" standalone="yes"?>', "<movie>"]
    p.append(f"  <plot>{cdata(d.get('plot',''))}</plot>")
    p.append(f"  <outline>{cdata(d.get('tagline') or d.get('plot','')[:80])}</outline>")
    p.append(f"  <title>{cdata(d.get('title',''))}</title>")
    p.append(f"  <originaltitle>{cdata(d.get('originaltitle',''))}</originaltitle>")
    p.append(f"  <sorttitle>{cdata(d.get('originaltitle') or d.get('title',''))}</sorttitle>")
    if d.get("rating"):
        p.append(f"  <rating>{d['rating']}</rating>")
        p.append(f"  <criticrating>{float(d['rating'])*10:.0f}</criticrating>")
    p.append(f"  <lockdata>false</lockdata>")
    p.append(f"  <dateadded>{now}</dateadded>")
    p.append(f'  <uniqueid type="tmdb" default="true">{d["tmdbid"]}</uniqueid>')
    if d.get("mpaa"):
        p.append(f"  <mpaa>{cdata(d['mpaa'])}</mpaa>")
    for dir_ in d.get("directors", []):
        p.append(f"  <director>{cdata(dir_)}</director>")
    for w in dict.fromkeys(d.get("writers", [])):
        p.append(f"  <credits>{cdata(w)}</credits>")
    for nm, role in d.get("cast", []):
        p += ["  <actor>", f"    <name>{cdata(nm)}</name>"]
        if role:
            p.append(f"    <role>{cdata(role)}</role>")
        p.append("  </actor>")
    for g in d.get("genres", []):
        p.append(f"  <genre>{cdata(g)}</genre>")
    if d.get("year"):
        p.append(f"  <year>{d['year']}</year>")
    if d.get("releasedate"):
        p.append(f"  <premiered>{d['releasedate']}</premiered>")
        p.append(f"  <releasedate>{d['releasedate']}</releasedate>")
    if d.get("runtime"):
        p.append(f"  <runtime>{d['runtime']}</runtime>")
    fi = fileinfo_xml(info)
    if fi:
        p.append(fi)
    p.append("</movie>")
    return "\n".join(p) + "\n"

# ---------------------------------------------------------------- 主流程
def do_folder(folder: Path, args):
    vids = [f for f in folder.iterdir()
            if f.is_file() and media_ext.is_video(f.name)]
    if not vids:
        return "SKIP  无视频"
    video = max(vids, key=lambda f: f.stat().st_size)
    nfo = video.with_suffix(".nfo")
    if nfo.exists() and not args.force:
        return f"EXIST {nfo.name}"

    zh_name, en_name, year = parse_folder(folder.name)
    # 文件夹名太笼统（如 downloads、电影）时，退回用视频文件名解析
    vz, ve, vy = parse_folder(video.stem)
    if (not year and vy) or (not zh_name and not year and (vz or ve)):
        zh_name, en_name, year = vz, ve, vy
        print(f"   用文件名解析: {video.name}")
    print(f"   中文名={zh_name} | 英文名={en_name} | 年份={year}")

    if args.tmdb_id:
        mid = re.sub(r"\D", "", str(args.tmdb_id))
        if not mid:
            return "MISS  --tmdb-id 不是数字"
        slug = f"/movie/{mid}"
        print(f"   使用指定 TMDB ID: {mid}")
    else:
        queries = []
        for q in (args.query, zh_name, en_name):
            q = (q or "").strip()
            if q and q not in queries:
                queries.append(q)
        if not queries:
            return "MISS  文件夹名/文件名里解析不出片名"
        hit = None
        for q in queries:
            print(f"   搜索 TMDB: {q}" + (f" ({year})" if year else ""))
            hit = search_movie(q, year)
            if hit:
                break
        if not hit:
            return f"MISS  TMDB 搜不到 {' / '.join(queries)}"
        slug, mid, s_title, s_year = hit
        print(f"   命中: [{mid}] {s_title} ({s_year})")


    zh, en, cast = movie_pages(slug, mid)
    if "Page Not Found" in zh[:3000] or "<title>" not in zh:
        return f"FAIL  TMDB 页面异常 ({mid})"
    d = parse_movie(zh, en, cast, mid)
    if not d.get("title") or not d.get("plot"):
        return f"FAIL  解析不到标题/简介 ({mid}) title={d.get('title','')!r}"
    if year and d.get("year") and d["year"] != year:
        print(f"   ! 年份不一致 文件夹={year} TMDB={d['year']}（仍继续）")

    # 图片
    if not args.no_images:
        if d.get("poster_path"):
            raw = get(f"{IMG}/{d['poster_path']}.jpg")
            if raw[:3] == b"\xff\xd8\xff":
                poster = folder / "poster.jpg"
                if not poster.exists():
                    poster.write_bytes(raw)
                    folder_jpg = folder / "folder.jpg"
                    if not folder_jpg.exists():
                        shutil.copyfile(poster, folder_jpg)
        if d.get("backdrop_path"):
            raw = get(f"{IMG}/{d['backdrop_path']}.jpg")
            if raw[:3] == b"\xff\xd8\xff":
                fanart = folder / "fanart.jpg"
                if not fanart.exists():
                    fanart.write_bytes(raw)
                    thumb = folder / "thumb.jpg"
                    if not thumb.exists():
                        shutil.copyfile(fanart, thumb)

    info = {} if (args.no_probe or not media_ext.is_probeable(video.name)) else ffprobe_info(video)
    nfo.write_text(build_nfo(d, info, video.name), encoding="utf-8")
    return (f"OK    {folder.name[:36]} <- TMDB {d['tmdbid']} | {d.get('title','')} ({d.get('year','')}) "
            f"| {d.get('releasedate','')} {d.get('runtime','')}分 | 导演 {len(d.get('directors',[]))} "
            f"| 演员 {len(d.get('cast',[]))} | 类型 {len(d.get('genres',[]))}")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default="/vol1/1000/Media")
    ap.add_argument("--dir", action="append", default=[],
                    help="直接指定要刮的文件夹（可重复），忽略 --root/#recycle")
    ap.add_argument("--query", default="",
                    help="手动指定 TMDB 搜索词（文件夹名认不出来时用）")
    ap.add_argument("--tmdb-id", default="",
                    help="手动指定 TMDB 数字 ID（最准，跳过搜索）")
    ap.add_argument("--dry", action="store_true")
    ap.add_argument("--force", action="store_true")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    args = ap.parse_args()

    if args.dir:
        folders = [Path(d) for d in args.dir]
        missing = [str(f) for f in folders if not f.is_dir()]
        if missing:
            sys.exit("目录不存在: " + ", ".join(missing))
        root = folders[0]
    else:
        root = Path(args.root)
        folders = sorted([p for p in (root / "#recycle").iterdir()
                          if p.is_dir()], key=lambda p: p.name) if (root / "#recycle").exists() else []
    print(f"待刮 {len(folders)} 个文件夹")
    if args.dry:
        for f in folders:
            print("   ", f.name, "->", parse_folder(f.name))
        return
    ok = err = 0
    for i, f in enumerate(folders, 1):
        try:
            msg = do_folder(f, args)
        except Exception as e:
            msg = f"ERROR {f.name}: {e}"
        print(f"[{i}/{len(folders)}] {msg}")
        if msg.startswith("OK"): ok += 1
        elif msg.startswith("EXIST"): pass
        else: err += 1
        time.sleep(1.5)
    print(f"\n成功 {ok} / 未成功 {err}")

if __name__ == "__main__":
    main()
