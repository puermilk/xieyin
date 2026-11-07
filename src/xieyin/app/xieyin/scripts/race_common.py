#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""多源刮削公共层：DoH / 多通道并发竞速 / preflight（带缓存）/ 合规白名单。

实测依据（/root/多源竞速方案对照测试_20261106.md）：
  * doh.pub 对 www.jav321.com 曾连续 3 次返回污染 IP -> KNOWN_GOOD 硬兜底
  * 单通道 12.8~19.2s 且 DNS 只有 2/3；多通道并发 3/3、7.61s
  * JavDB 返回 Content-Encoding: zstd -> 必须 --compressed，判定用解压后 body 长度
全程零代理。
"""
from __future__ import annotations

import html, json, os, re, subprocess, tempfile, time
from concurrent.futures import ThreadPoolExecutor, as_completed

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DOH_URL = "https://doh.pub/dns-query"

# DoH 被污染时的已知真实 IP（沿用 xieyin 原 KNOWN_IPS 机制）
KNOWN_GOOD = {
    "www.jav321.com": ["139.162.115.161"],
    "javdb580.com": ["172.67.160.243", "104.21.9.179"],
    "c0.jdbstatic.com": ["172.67.156.24", "104.21.42.41"],
}

# 静态排除：内容站 / 播放站 / 停放页 / 无按番号搜索能力 / 国内已阻断
EXCLUDED = {
    "avbase.app": "合规排除：meta 含 Stream in HD（播放站）",
    "javdb.club": "无按番号搜索能力（/search 全 404）",
    "javlibrary.cc": "内容站 Download ... for Free",
    "javpark.net": "内容站 Watch Jav Online Free",
    "javbus.one": "引流/内容站（迅雷/夸克短链）",
    "javbus.vip": "停放页", "javbus.app": "停放页",
    "javdb.net": "ParkLogic 停放页",
    "www.javbus.com": "国内直连 302 / 假 IP，已阻断",
    "www.avbase.net": "旧域名 SNI 阻断",
    "www.minnano-av.com": "旧域名 SNI 阻断",
    "www.javlibrary.com": "CF 403",
}

# 字段白名单：与 jav_scrape.build_nfo 对齐，绝不写入片源/播放/磁力
WHITELIST = ("code", "title", "actors", "studio", "director", "releasedate",
             "runtime", "genres", "desc", "series", "label", "rating",
             "cover", "source")
_BAD = re.compile(r"magnet:|\.torrent|/watch|/stream|/download|\.m3u8|ed2k://", re.I)

CACHE_TTL = 600

SOURCES = [
    dict(name="jav321", host="www.jav321.com", feat="jav321",
         probe="/video/adn00451", kind="scrape", role="快源"),
    dict(name="javdb580", host="javdb580.com", feat="JavDB",
         probe="/search?q=ADN-451", kind="scrape", role="慢源/补字段"),
    dict(name="jdbstatic", host="c0.jdbstatic.com", feat=None,
         probe="/covers/1b/1BgmOn.jpg", kind="resource", role="封面图床"),
]


def data_dir():
    return os.environ.get("XIEYIN_DATA") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def cache_path():
    return os.environ.get("XIEYIN_PREFLIGHT_CACHE") or os.path.join(data_dir(), "preflight_cache.json")


def doh(name):
    """DoH 直连（不走代理）取 A 记录，并并入 KNOWN_GOOD 兜底。"""
    ips = []
    try:
        out = subprocess.run(
            ["curl", "-s", "-m", "8", "-H", "accept: application/dns-json",
             DOH_URL + "?name=" + name + "&type=A"],
            capture_output=True, text=True, timeout=10).stdout
        ips = [a["data"] for a in json.loads(out).get("Answer", []) if a.get("type") == 1]
    except Exception:
        pass
    for ip in KNOWN_GOOD.get(name, []):
        if ip not in ips:
            ips.append(ip)
    return ips


def curl(host, path, ip=None, timeout=16):
    """零代理 HTTPS；--compressed 自动解 zstd/gzip；返回 (status, 压缩字节, 解压后 body)。"""
    fd, tmp = tempfile.mkstemp(prefix="xieyin-race-")
    os.close(fd)
    cmd = ["curl", "-s", "--compressed", "-m", str(timeout), "-A", UA,
           "-H", "Connection: close"]
    if ip:
        cmd += ["--resolve", "%s:443:%s" % (host, ip)]
    cmd += ["-o", tmp, "-w", "%{http_code}|%{size_download}", "https://" + host + path]
    try:
        out = subprocess.run(cmd, capture_output=True, text=True,
                             timeout=timeout + 3).stdout.strip()
    except Exception:
        out = "000|0"
    parts = out.split("|")
    st = int(parts[0]) if parts and parts[0].isdigit() else 0
    sz = int(parts[1]) if len(parts) > 1 and parts[1].isdigit() else 0
    try:
        body = open(tmp, "rb").read()
    except Exception:
        body = b""
    try:
        os.unlink(tmp)
    except Exception:
        pass
    return st, sz, body


def fetch_multi(host, path, ips, timeout=16, min_size=5000):
    """同一 URL 同时打『域名直连 + 每个候选 IP 硬连』，谁先返回有效内容用谁。"""
    jobs = [None] + [i for i in ips if i]
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futs = {ex.submit(curl, host, path, ip, timeout): (ip or "dns") for ip in jobs}
        for f in as_completed(futs):
            st, sz, body = f.result()
            if st == 200 and len(body) > min_size:
                return st, sz, body, futs[f]
    return 0, 0, b"", "none"


def strip_tags(s):
    return re.sub(r"\s+", " ", html.unescape(re.sub(r"<[^>]+>", " ", s or ""))).strip()


def clean_title(t):
    """去开头番号、去尾部 SEO 垃圾（bittorrent/Download/dmm/番号+演员）。"""
    t = html.unescape(t or "").strip().split(" | ")[0].strip()
    t = re.sub(r"^[A-Z0-9]{2,6}-\d{2,5}\s+", "", t)
    t = re.sub(r"\s+(bittorrent|Download|dmm|JavDB).*$", "", t, flags=re.I)
    t = re.sub(r"\s+[A-Za-z]{2,6}-\d{2,5}\s.*$", "", t, flags=re.I)
    return t.strip()


def sanitize(d):
    """字段白名单 + 显式丢弃 magnet/torrent/播放/下载。"""
    out = {}
    for k, v in (d or {}).items():
        if k not in WHITELIST or not v:
            continue
        if isinstance(v, str) and _BAD.search(v):
            continue
        if isinstance(v, list):
            v = [x for x in v if isinstance(x, str) and x.strip() and not _BAD.search(x)]
            if not v:
                continue
        out[k] = v
    return out


def jpeg_ok(b):
    """JPEG 必须同时有 SOI 和 EOI；图床限速时 -m 超时会返回半截文件。"""
    return len(b) > 10000 and b[:3] == b"\xff\xd8\xff" and b[-2:] == b"\xff\xd9"


def download_cover(url, timeout=22):
    """下图：域名直连 + 所有候选 IP 并发竞速；完整 JPEG 优先，否则降级取最大半截图。

    图床限速时 curl 常在 EOI 前被截断，严格只收完整图会导致整批无封面。
    fanart/thumb 对半截 JPEG 仍可显示，poster 裁剪由 LOAD_TRUNCATED_IMAGES 兜住。
    """
    m = re.match(r"https?://([^/]+)(.*)", url or "")
    if not m:
        return None
    host, path = m.group(1), m.group(2)
    ips = list(dict.fromkeys(doh(host) + KNOWN_GOOD.get(host, [])))
    jobs = [None] + [i for i in ips if i]
    best = None
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futs = {ex.submit(curl, host, path, ip, timeout): (ip or "dns") for ip in jobs}
        for f in as_completed(futs):
            st, sz, body = f.result()
            if st != 200 or len(body) < 10000 or body[:3] != b"\xff\xd8\xff":
                continue
            if jpeg_ok(body):
                return body
            if best is None or len(body) > len(best):
                best = body
    return best


def _preflight_one(c):
    name, host, feat, probe, kind, role = (c["name"], c["host"], c["feat"],
                                          c["probe"], c["kind"], c["role"])
    r = dict(name=name, host=host, role=role, ips=[], dns="", resolve=[],
             mode="", enabled=False, reason="", resource=False)
    r["ips"] = doh(host)
    st, sz, body = curl(host, "/", timeout=10)
    r["dns"] = "%s/%dB(解压)" % (st, len(body))
    # 图床根路径本来 403，必须用真实封面路径校验
    if kind == "resource":
        ok = False
        for ip in (r["ips"] or [None]):
            s2, z2, b2 = curl(host, probe, ip, 12)
            r["resolve"].append("%s:%s/%dB%s" % (
                ip, s2, len(b2), "✓" if (s2 == 200 and b2[:3] == b"\xff\xd8\xff") else "✗"))
            if s2 == 200 and b2[:3] == b"\xff\xd8\xff":
                ok = True
        r["mode"] = "resource"
        r["resource"] = ok
        r["reason"] = "图床资源 %s（%s）" % ("可用" if ok else "不可用", probe)
        return r
    # 首页探测仅作记录：网络抖动时首页超时不代表详情页不可用，
    # 唯一入池依据是下面的「真实入口能力校验」。
    dns_ok = st == 200 and len(body) > 10000 and (not feat or feat.encode() in body)
    good = []
    for ip in r["ips"]:
        s2, z2, b2 = curl(host, "/", ip, 12)
        ok = s2 == 200 and len(b2) > 10000 and (not feat or feat.encode() in b2)
        r["resolve"].append("%s:%s/%dB%s" % (ip, s2, len(b2), "✓" if ok else "✗"))
        if ok:
            good.append(ip)
    if dns_ok:
        r["mode"] = "dns(域名直连)"
    elif good:
        r["mode"] = "resolve:" + good[0]
    else:
        r["mode"] = "由能力校验决定"
    # 能力校验：按各源真实入口多通道并发，失败重试一轮
    s3, b3, via3 = 0, b"", "none"
    for _ in range(2):
        s3, z3, b3, via3 = fetch_multi(host, probe, r["ips"], timeout=18, min_size=5000)
        if s3 == 200:
            break
    if s3 != 200:
        r["reason"] = "能力校验失败 %s HTTP %s" % (probe, s3)
        return r
    t3 = b3.decode("utf-8", "ignore")
    if probe.startswith("/search") and not re.search(r'href="(/v/[A-Za-z0-9]+)"', t3):
        r["reason"] = "搜索页无 /v/ 详情链接"
        return r
    if probe.startswith("/video") and not re.search(r"出演者|メーカー|品番", t3):
        r["reason"] = "详情页无元数据字段"
        return r
    r["enabled"] = True
    if r["mode"] == "由能力校验决定":
        r["mode"] = "via:" + via3
    r["reason"] = "验证通过（能力校验@%s）" % via3
    return r


def preflight(force=False, ttl=CACHE_TTL, verbose=True):
    """启动前验证，结果缓存 ttl 秒；返回 (入池源列表, 完整报告)。"""
    cp = cache_path()
    if not force:
        try:
            cache = json.load(open(cp, encoding="utf-8"))
            if time.time() - cache.get("ts", 0) < ttl:
                rep = cache["results"]
                pool = [x for x in rep if x["enabled"]]
                if verbose:
                    print("   preflight 命中缓存（%d 源入池，%ds 内有效）" % (len(pool), ttl))
                return pool, rep
        except Exception:
            pass
    with ThreadPoolExecutor(max_workers=len(SOURCES)) as ex:
        rep = list(ex.map(_preflight_one, SOURCES))
    pool = [x for x in rep if x["enabled"]]
    try:
        os.makedirs(os.path.dirname(cp), exist_ok=True)
        with open(cp, "w", encoding="utf-8") as fh:
            json.dump({"ts": time.time(), "results": rep}, fh, ensure_ascii=False)
    except Exception:
        pass
    if verbose:
        print("   preflight：静态排除 %d 个域名（内容站/播放站/停放页/阻断）" % len(EXCLUDED))
        for x in rep:
            print("   %s %-20s IP=%s DNS=%s -> %s %s" % (
                "✅" if x["enabled"] else "⛔", x["host"], x["ips"], x["dns"],
                x["mode"] or "-", x["reason"]))
        print("   -> 入池 %d 个源：%s" % (len(pool), [x["name"] for x in pool]))
    return pool, rep
