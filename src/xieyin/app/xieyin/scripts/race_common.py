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


def _env_int(name: str, default: int, lo: int, hi: int) -> int:
    """读取整数型环境变量（由 app_server 按配置下传），越界即回退默认。"""
    try:
        v = int(os.environ.get(name, "") or default)
    except ValueError:
        return default
    return max(lo, min(hi, v))


# 高级设置（app_server 经 env 下传，可在网页「设置 → 高级」调整）
CONCURRENCY = _env_int("XIEYIN_CONCURRENCY", 3, 1, 8)
TIMEOUT = _env_int("XIEYIN_TIMEOUT", 12, 5, 120)
RETRIES = _env_int("XIEYIN_RETRIES", 1, 0, 5)

# DoH 被污染时的已知真实 IP（沿用 xieyin 原 KNOWN_IPS 机制）
KNOWN_GOOD = {
    "www.jav321.com": ["139.162.115.161"],
    "javdb580.com": ["172.67.160.243", "104.21.9.179"],
    "c0.jdbstatic.com": ["172.67.156.24", "104.21.42.41"],
    "www.javbus.com": ["104.25.112.111", "172.67.80.44", "104.25.111.111"],
    "www.javdatabase.com": ["104.21.74.67", "172.67.200.42"],
    "pics.dmm.com": ["54.230.70.96", "54.230.70.127", "54.230.70.13", "54.230.70.27"],
    # 日期型番号（MMDDYY-NNN：素人/無修正）的唯一合规元数据源
    "www.caribbeancom.com": ["104.18.19.203"],
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
    # jav321 已剔除：实测详情页恒 16s 超时（HTTP 0），真实 IP 也救不回来
    dict(name="javbus", host="www.javbus.com", feat="JavBus",
         probe="/ADN-451", kind="scrape", role="主力/快源(1-2s)"),
    dict(name="javdb580", host="javdb580.com", feat="JavDB",
         probe="/search?q=ADN-451", kind="scrape", role="补充字段"),
    dict(name="jdbstatic", host="c0.jdbstatic.com", feat=None,
         probe="/covers/1b/1BgmOn.jpg", kind="resource", role="封面图床"),
    dict(name="dmm", host="pics.dmm.com", feat=None,
         probe="/digital/video/ssis00109/ssis00109pl.jpg", kind="resource",
         role="封面图床(DMM .com 镜像)"),
    dict(name="dmm2", host="imgsrc.dmm.com", feat=None,
         probe="/pics/digital/video/ssis00109/ssis00109pl.jpg", kind="resource",
         role="封面图床(DMM 第二镜像)"),
    dict(name="javdatabase", host="www.javdatabase.com", feat="JAV Database",
         probe="/movies/ssis-001/", kind="scrape", role="末位兜底"),
    # 日期型番号（071518-714 这类）专供：主流库全不收录，只有官网有
    dict(name="caribbeancom", host="www.caribbeancom.com", feat="Caribbeancom",
         probe="/moviepages/010120-001/index.html", kind="scrape",
         role="日期型番号专用"),
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


def curl(host, path, ip=None, timeout=None, referer=None):
    """零代理 HTTPS；--compressed 自动解 zstd/gzip；返回 (status, 压缩字节, 解压后 body)。

    referer：javbus 图床有防盗链，必须带来源页才返回 200。
    """
    if timeout is None:
        timeout = TIMEOUT
    fd, tmp = tempfile.mkstemp(prefix="xieyin-race-")
    os.close(fd)
    cmd = ["curl", "-s", "--compressed", "-m", str(timeout), "-A", UA,
           "-H", "Connection: close"]
    if ip:
        cmd += ["--resolve", "%s:443:%s" % (host, ip)]
    if referer:
        cmd += ["-e", referer]
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
    # 只用调用方给的真实 IP（preflight/DoH 验证过的），不再掺系统 DNS：
    # 本机系统 DNS 被污染（会解析到 Facebook 段假 IP），掺进去只会白占
    # 一个并发槽位、白等一个超时。
    jobs = [i for i in ips if i] or [None]
    with ThreadPoolExecutor(max_workers=max(1, min(len(jobs), CONCURRENCY))) as ex:
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
    """完整 JPEG：SOI 开头 + EOI 收尾。

    注意 DMM 等图床的图尾部常带 1~4 字节填充（如 0a），EOI 不一定在最后 2 字节，
    必须按 rfind 定位 EOI 后允许少量尾随字节，否则会把完整图误判成半截图。
    """
    if len(b) <= 10000 or b[:3] != b"\xff\xd8\xff":
        return False
    eoi = b.rfind(b"\xff\xd9")
    return eoi != -1 and len(b) - eoi <= 16


def javdatabase_cid(code, timeout=12, retries=1):
    """从 javdatabase 页面解析该片在 DMM 的真实 cid。

    为什么需要：DMM 的 cid 除了 `label+5位数字`，还有大量内部前缀
    （`h_068mxgs01438`、`18sprd01506`、`1stars00355`），直接推导会 302/无图。
    javdatabase 的 og:image 恰好写着 `pics.dmm.co.jp/digital/video/<cid>/`，
    等于免费的 cid 解析器。返回 "" 表示解析不到。
    """
    c = str(code or "").strip().lower().replace("_", "-")
    if not c:
        return ""
    path = "/movies/%s/" % c
    ips = KNOWN_GOOD.get("www.javdatabase.com", [])
    for _ in range(max(1, retries)):
        for ip in (list(ips) + [None]):   # 系统 DNS 实测必超时，优先固定 IP
            st, _sz, body = curl("www.javdatabase.com", path, ip, timeout)
            if st == 200 and body:
                m = re.search(rb"pics\.dmm\.co\.jp/digital/video/([a-z0-9_]+)/", body)
                if m:
                    return m.group(1).decode("ascii", "ignore")
    return ""


def download_cover(url, timeout=12):
    """下图：域名直连 + 所有候选 IP 并发竞速；完整 JPEG 优先，否则降级取最大半截图。

    图床限速时 curl 常在 EOI 前被截断，严格只收完整图会导致整批无封面。
    fanart/thumb 对半截 JPEG 仍可显示，poster 裁剪由 LOAD_TRUNCATED_IMAGES 兜住。
    """
    # DMM 图床：pics.dmm.co.jp 的 SNI 在部分网络（实测腾讯云）被阻断，
    # pics.dmm.com 是同一 CloudFront、同路径同文件，SNI 未被封。统一重映射。
    if url and "pics.dmm.co.jp" in url:
        url = url.replace("pics.dmm.co.jp", "pics.dmm.com")
    m = re.match(r"https?://([^/]+)(.*)", url or "")
    if not m:
        return None
    host, path = m.group(1), m.group(2)
    ips = list(dict.fromkeys(doh(host) + KNOWN_GOOD.get(host, [])))
    # 只用调用方给的真实 IP（preflight/DoH 验证过的），不再掺系统 DNS：
    # 本机系统 DNS 被污染（会解析到 Facebook 段假 IP），掺进去只会白占
    # 一个并发槽位、白等一个超时。
    jobs = [i for i in ips if i] or [None]
    # javbus 自家图床有防盗链：不带 Referer 会 403，带了才 200
    ref = "https://www.javbus.com/" if "javbus.com" in host else None
    best = None
    with ThreadPoolExecutor(max_workers=max(1, len(jobs))) as ex:
        futs = {ex.submit(curl, host, path, ip, timeout, ref): (ip or "dns") for ip in jobs}
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
    # javbus：年龄墙/反爬会把 curl 引去验证页，真实通道是 legacy Session
    #（固定 IP 直连 + 换 IP 重试），探活也用它，避免误判
    if name == "javbus":
        try:
            import jav_scrape as js
            sess = js.Session(validate=True)
            if sess.validated:
                r["ips"] = list(sess.ips)
                r["mode"] = "固定IP直连(Session)"
                r["enabled"] = True
                r["reason"] = "验证通过（Session 探活 %d IP）" % len(sess.ips)
            else:
                r["mode"] = "固定IP直连(Session)"
                r["reason"] = "全部 IP 探活失败"
            return r
        except Exception as e:
            r["reason"] = "javbus 探活异常 %s" % e
            return r
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
    # 能力校验：按各源真实入口多通道并发，失败按 RETRIES 重试
    s3, b3, via3 = 0, b"", "none"
    for _ in range(1 + RETRIES):
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
    if name == "javdatabase" and not re.search(r"Release Date|Runtime|Studio", t3):
        r["reason"] = "详情页无元数据字段（可能是壳页）"
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
