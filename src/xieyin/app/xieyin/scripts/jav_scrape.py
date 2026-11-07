#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""
jav_scrape.py — 刮削 媒体目录里的番号 AV 文件夹
数据源: JavBus (www.javbus.com)，需 DoH 取真实 IP 绕过 DNS 污染
输出:   <番号>.nfo + <番号>-poster.jpg + <番号>-fanart.jpg + <番号>-thumb.jpg + extrafanart/

用法:
  python3 jav_scrape.py --dry                 # 只预览要做什么
  python3 jav_scrape.py --limit 2             # 先刮 2 个试试
  python3 jav_scrape.py --only ABC-123-UC     # 只刮某个文件夹
  python3 jav_scrape.py                       # 全部刮完
  选项: --no-probe 不跑 ffprobe 生成 fileinfo  --no-images 不下图
"""
import argparse, html, json, os, re, shutil, ssl, socket, subprocess, sys, tempfile, time
import http.client, urllib.parse, urllib.request
from datetime import datetime
from pathlib import Path

import media_ext

BASE = Path("/vol1/1000/Media")
HOST = "www.javbus.com"
VIDEO_EXT = media_ext.MEDIA_EXT
UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
DELAY = 1.8          # 请求间隔，别太猛
CTX = ssl.create_default_context()

# ---------------------------------------------------------------- 网络
class PinConn(http.client.HTTPSConnection):
    """固定到指定 IP 的 HTTPS 连接（绕 DNS 污染，SNI 仍是域名）"""
    def __init__(self, host, ip, timeout=25):
        super().__init__(host, 443, timeout=timeout, context=CTX)
        self._ip = ip

    def connect(self):
        sock = socket.create_connection((self._ip, 443), self.timeout)
        self.sock = self._context.wrap_socket(sock, server_hostname=self.host)


def doh_ips(name):
    """腾讯 DoH 取真实 A 记录"""
    try:
        req = urllib.request.Request(
            f"https://doh.pub/dns-query?name={name}&type=A",
            headers={"User-Agent": UA, "accept": "application/dns-json"})
        with urllib.request.urlopen(req, timeout=12, context=CTX) as r:
            data = json.load(r)
        return [a["data"] for a in data.get("Answer", []) if a.get("type") == 1]
    except Exception as e:
        print(f"   ! DoH 失败 {name}: {e}", file=sys.stderr)
        return []


# 国内 DoH（doh.pub / dns.alidns.com / dns.pub）对 javbus.com 会返回假 IP
# （Dropbox 之类的污染地址），所以这里存一份验证过的真实 Cloudflare IP 兜底。
KNOWN_IPS = ["104.25.112.111", "172.67.80.44", "104.25.111.111"]


class Session:
    def __init__(self, validate=True):
        self.cookies = {}
        # 候选：DoH 给的 + 已知好 IP，去重
        cand = []
        for ip in list(doh_ips(HOST)) + KNOWN_IPS:
            if ip and ip not in cand:
                cand.append(ip)
        self.ips = []
        if validate:
            for ip in cand:
                if self.probe_ok(ip):
                    self.ips.append(ip)
                time.sleep(0.2)
            print(f"   IP 探测: 候选 {len(cand)} 个，可用 {len(self.ips)} 个 -> {', '.join(self.ips)}")
        # 直连通道是否真的探测通过（兜底判断要用）
        self.validated = bool(self.ips)
        if not self.ips:
            print("   ! 所有 IP 探测失败：暂时用硬编码 KNOWN_IPS 硬闯；"
                  "如果持续失败，请配置代理或更新 IP 列表", file=sys.stderr)
            self.ips = KNOWN_IPS[:]      # 全探测失败就硬闯已知 IP
        self.ipidx = 0

    @staticmethod
    def probe(ip, timeout=12):
        """固定 IP 访问首页，验证这个 IP 是不是真的 JavBus（DoH 答案可能被污染）"""
        hdr = {"User-Agent": UA,
               "Accept": "text/html,application/xhtml+xml",
               "Accept-Language": "zh-CN,zh;q=0.9",
               "Connection": "close"}
        conn = PinConn(HOST, ip, timeout=timeout)
        try:
            conn.request("GET", "/", headers=hdr)
            resp = conn.getresponse()
            body = resp.read()
            return resp.status, body
        except Exception:
            return 0, b""
        finally:
            try:
                conn.close()
            except Exception:
                pass

    @classmethod
    def probe_ok(cls, ip, timeout=12):
        """唯一的 IP 验证标准：HTTP 200 且页面确实是 JavBus。"""
        st, body = cls.probe(ip, timeout)
        return st == 200 and (b"avBus" in body or b"javbus" in body.lower())

    def _ip(self):
        ip = self.ips[self.ipidx % len(self.ips)]
        return ip

    def rotate(self):
        self.ipidx += 1
        if self.ipidx % len(self.ips) == 0:
            # 转回起点时顺手再问一次 DoH（可能换了新 IP），只收验证通过的
            for ip in doh_ips(HOST):
                if ip not in self.ips and self.probe_ok(ip):
                    self.ips.append(ip)

    def request(self, path, referer=None, tries=4):
        hdr = {
            "User-Agent": UA,
            "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,image/webp,*/*;q=0.8",
            "Accept-Language": "zh-CN,zh;q=0.9,en;q=0.8",
            "Connection": "close",
        }
        if referer:
            hdr["Referer"] = referer
        if self.cookies:
            hdr["Cookie"] = "; ".join(f"{k}={v}" for k, v in self.cookies.items())
        for t in range(tries):
            conn = PinConn(HOST, self._ip())
            try:
                conn.request("GET", path, headers=hdr)
                resp = conn.getresponse()
                body = resp.read()
                sc = resp.getheader("Set-Cookie", "")
                for m in re.finditer(r"([^=;]+)=([^;]*)", sc):
                    self.cookies[m.group(1).strip()] = m.group(2).strip()
                loc = resp.getheader("Location")
                code = resp.status
                conn.close()
                if code in (301, 302, 303, 307, 308) and loc:
                    if "driver-verify" in loc or "age" in loc.lower():
                        time.sleep(2.0 + t)
                        self.rotate()
                        continue          # 年龄验证/反爬，换 IP 重试
                    p = urllib.parse.urlparse(loc)
                    return self.request(p.path + ("?" + p.query if p.query else ""),
                                        referer=referer, tries=tries - t)
                return code, body
            except Exception:
                conn.close()
                self.rotate()
                time.sleep(1.5 + t)
        return 0, b""

    def fetch_url(self, url, referer=None):
        """下载图片：javbus 域名走固定 IP，其他域名（DMM 图床）直连"""
        u = urllib.parse.urlparse(url)
        if not u.hostname or u.hostname == HOST:
            path = u.path + ("?" + u.query if u.query else "")
            return self.request(path, referer=referer or f"https://{HOST}/")[1]
        try:
            req = urllib.request.Request(
                url, headers={"User-Agent": UA,
                              "Referer": referer or f"https://{HOST}/"})
            with urllib.request.urlopen(req, timeout=25, context=CTX) as r:
                return r.read()
        except Exception:
            return b""

    def get_image(self, path):
        body = self.fetch_url(path if path.startswith("http") else f"https://{HOST}{path}")
        return body if body[:3] in (b"\xff\xd8\xff", b"\x89PN") else b""


# ---------------------------------------------------------------- 传输层：两套方案互为兜底
PROXY_DEFAULT = "http://127.0.0.1:7890"
PROXY_FAIL_LIMIT = 3        # 主通道连续失败几次就切到备用
PROXY_RETRY_EVERY = 20      # 切到备用后，每成功 N 个请求回头探一次主通道，恢复了就切回来


class ProxySession:
    """方案 B：走本地 HTTP 代理。

    为什么用 curl 子进程而不是 http.client：Cloudflare 对机房出口 IP 会校验 TLS
    指纹，Python ssl 的 JA3 特征会被直接 RST（实测 curl 能过、http.client 被重置），
    和 tmdb_scrape.py 里不用 urllib 是同一个原因。

    出站之后国内/被墙怎么分流由 sing-box 的 GFW 规则决定，脚本这边不用管。
    """

    def __init__(self, proxy):
        self.proxy = proxy
        self.jar = tempfile.mktemp(prefix="javbus-", suffix=".cookie")
        self.cookies = {}

    def _curl(self, url, referer=None, tries=3, timeout=45, binary=False):
        for t in range(tries):
            out = tempfile.mktemp(prefix="javbus-", suffix=".bin")
            cmd = ["curl", "-sL", "--compressed", "-A", UA,
                   "-H", "Accept-Language: zh-CN,zh;q=0.9",
                   "-b", self.jar, "-c", self.jar,     # 靠 cookie 过年龄验证
                   "-x", self.proxy, "--max-time", str(timeout),
                   "-o", out, "-w", "%{http_code}"]
            if referer:
                cmd += ["-e", referer]
            if binary:
                cmd.append("--raw")
            cmd.append(url)
            code, data = 0, b""
            try:
                r = subprocess.run(cmd, capture_output=True, timeout=timeout + 15)
                code = int((r.stdout or b"0").decode().strip() or 0)
                if Path(out).exists():
                    data = Path(out).read_bytes()
            except Exception:
                code, data = 0, b""
            finally:
                try:
                    Path(out).unlink(missing_ok=True)
                except Exception:
                    pass
            if code:
                return code, data
            time.sleep(1.5 + t)
        return 0, b""

    def request(self, path, referer=None, tries=3, timeout=45):
        url = path if path.startswith("http") else f"https://{HOST}{path}"
        return self._curl(url, referer, tries, timeout)

    def fetch_url(self, url, referer=None):
        code, data = self._curl(url, referer or f"https://{HOST}/", 2, 60, binary=True)
        return data if code else b""

    def get_image(self, path):
        body = self.fetch_url(path if path.startswith("http") else f"https://{HOST}{path}")
        return body if body[:3] in (b"\xff\xd8\xff", b"\x89PN") else b""


class FallbackSession:
    """把两套方案串起来：主通道连续失败 PROXY_FAIL_LIMIT 次自动切到备用，
    之后每成功 N 个请求回头探一次主通道，恢复了就切回去。

    两套方案接口一致（request / fetch_url / get_image），所以调用方无感。
    """

    def __init__(self, sessions, names):
        self.sessions = list(sessions)
        self.names = list(names)
        self.idx = 0
        self.fail = 0
        self.ok = 0
        self.cookies = {}

    @property
    def cur(self):
        return self.sessions[self.idx]

    @property
    def channel(self):
        return self.names[self.idx]

    def _probe(self, idx):
        """轻量探测某个通道是否可用（用于决定要不要切回去）"""
        try:
            if isinstance(self.sessions[idx], ProxySession):
                st, _ = self.sessions[idx].request("/", tries=1, timeout=10)
            else:
                st, _ = self.sessions[idx].request("/", tries=1)
            return bool(st)
        except Exception:
            return False

    def _mark_ok(self):
        self.fail = 0
        self.ok += 1
        if self.idx != 0 and self.ok % PROXY_RETRY_EVERY == 0:
            print(f"   ↺ 回头探测 {self.names[0]} 通道是否恢复", file=sys.stderr)
            if self._probe(0):
                print(f"   ✓ {self.names[0]} 已恢复，切回主通道", file=sys.stderr)
                self.idx, self.fail = 0, 0

    def _mark_bad(self, why):
        self.fail += 1
        if self.fail >= PROXY_FAIL_LIMIT and len(self.sessions) > 1:
            nxt = (self.idx + 1) % len(self.sessions)
            print(f"   ⚠ {self.names[self.idx]} 连续失败 {self.fail} 次（{why}），"
                  f"切换到 {self.names[nxt]}", file=sys.stderr)
            self.idx, self.fail = nxt, 0

    def request(self, path, referer=None, tries=4):
        st, body = self.cur.request(path, referer=referer, tries=tries)
        if st:
            self._mark_ok()
        else:
            self._mark_bad(f"取不到 {path}")
        return st, body

    def fetch_url(self, url, referer=None):
        body = self.cur.fetch_url(url, referer=referer)
        if body:
            self._mark_ok()
        else:
            self._mark_bad("取不到图片")
        return body

    def get_image(self, path):
        body = self.fetch_url(path if path.startswith("http") else f"https://{HOST}{path}")
        return body if body[:3] in (b"\xff\xd8\xff", b"\x89PN") else b""


def _port_open(host, port, timeout=1.5):
    try:
        s = socket.create_connection((host, port), timeout)
        s.close()
        return True
    except Exception:
        return False


def pick_session(via, proxy_url):
    """按 --via 决定通道顺序，并探测可用性。

    auto（默认）：代理可用就代理优先（不用维护固定 IP 清单），直连兜底；
                 代理不可用就直连优先，但代理仍留在队列里当兜底、随时会回头探。
    """
    direct = Session()

    proxy_sess = None
    proxy_ok = False
    if proxy_url:
        print(f"   探测代理通道 {proxy_url} …", file=sys.stderr)
        proxy_sess = ProxySession(proxy_url)
        st, _ = proxy_sess.request("/", tries=1, timeout=12)
        proxy_ok = bool(st)
        print(f"   代理通道: {'可用' if proxy_ok else '不可用（会保留为兜底，随时重试）'}",
              file=sys.stderr)

    if via == "direct":
        order = [direct] + ([proxy_sess] if proxy_sess else [])
        names = ["固定IP直连"] + (["代理"] if proxy_sess else [])
    elif via == "proxy":
        order = ([proxy_sess] if proxy_sess else []) + [direct]
        names = (["代理"] if proxy_sess else []) + ["固定IP直连"]
    else:                                   # auto
        if proxy_ok:
            order, names = [proxy_sess, direct], ["代理", "固定IP直连"]
        else:
            order, names = [direct] + ([proxy_sess] if proxy_sess else []), \
                           ["固定IP直连"] + (["代理"] if proxy_sess else [])

    fs = FallbackSession(order, names)
    print(f"   主通道: {fs.channel}" + (f"，兜底: {names[1]}" if len(names) > 1 else ""),
          file=sys.stderr)
    return fs


# ---------------------------------------------------------------- 解析
def txt(s):
    return html.unescape(re.sub(r"<[^>]+>", "", s)).strip()

def parse_page(page):
    """解析 JavBus 影片页 -> dict"""
    d = {}
    m = re.search(r"<h3>(.*?)</h3>", page, re.S)
    h3 = txt(m.group(1)) if m else ""
    d["h3"] = h3
    # 识别码（红字）
    m = re.search(r'識別碼:</span>\s*<span[^>]*>([^<]+)</span>', page)
    d["code"] = m.group(1).strip() if m else ""
    # 标题 = h3 去掉开头的番号
    title = h3
    if d["code"] and title.startswith(d["code"]):
        title = title[len(d["code"]):].strip()
    d["title"] = title
    d["_title_full"] = h3

    def field(label):
        m = re.search(r'<span class="header">' + label + r':</span>\s*(.*?)</p>', page, re.S)
        if not m:
            return ""
        inner = m.group(1)
        a = re.search(r"<a[^>]*>(.*?)</a>", inner, re.S)
        return txt(a.group(1)) if a else txt(inner)

    d["releasedate"] = field("發行日期") or field("發布日期")
    d["runtime"]     = field("長度")
    d["director"]    = field("導演")
    d["studio"]      = field("製作商")
    d["label"]       = field("發行商")
    d["series"]      = field("系列")

    i = page.find("類別:")
    j = page.find("star-show")
    gblock = page[i:j] if i >= 0 else ""
    d["genres"] = list(dict.fromkeys(
        txt(x) for x in re.findall(r'<a href="[^"]*/genre/[^"]+">([^<]+)</a>', gblock)))

    stars = []
    for blk in re.findall(r'<div id="star_[^"]+".*?</div>\s*</div>', page, re.S):
        a = re.search(r'<div class="star-name"><a[^>]*title="([^"]+)"', blk)
        if a:
            stars.append(html.unescape(a.group(1)))
    if not stars:
        stars = [html.unescape(x) for x in re.findall(r'/star/[^"]+" title="([^"]+)"', page)]
    seen, out = set(), []
    for s in stars:
        if s not in seen:
            seen.add(s); out.append(s)
    d["actors"] = out

    m = re.search(r'class="bigImage" href="([^"]+)"', page)
    d["cover"] = m.group(1) if m else ""
    d["samples"] = list(dict.fromkeys(
        re.findall(r'<a class="sample-box" href="([^"]+)"', page)))[:12]
    m = re.search(r'<meta name="description" content="([^"]*)"', page)
    d["desc"] = html.unescape(m.group(1)) if m else ""
    m = re.search(r'<meta name="keywords" content="([^"]*)"', page)
    d["keywords"] = html.unescape(m.group(1)) if m else ""
    return d


# ---------------------------------------------------------------- ffprobe
def ffprobe_info(path):
    try:
        out = subprocess.run(
            ["ffprobe", "-v", "quiet", "-print_format", "json",
             "-show_format", "-show_streams", str(path)],
            capture_output=True, timeout=120).stdout
        return json.loads(out or b"{}")
    except Exception:
        return {}


def fileinfo_xml(info):
    v = next((s for s in info.get("streams", []) if s.get("codec_type") == "video"), None)
    a = next((s for s in info.get("streams", []) if s.get("codec_type") == "audio"), None)
    fmt = info.get("format", {})
    if not v:
        return ""
    dur = fmt.get("duration") or (v or {}).get("duration") or "0"
    try:
        dur_i = int(float(dur))
    except Exception:
        dur_i = 0
    fr = v.get("avg_frame_rate") or v.get("r_frame_rate") or ""
    if "/" in str(fr):
        try:
            a_, b_ = str(fr).split("/")
            fr = f"{float(a_)/float(b_):.5f}" if float(b_) else ""
        except Exception:
            fr = ""
    ar = ""
    if v.get("width") and v.get("height"):
        g = __import__("math").gcd(int(v["width"]), int(v["height"]))
        ar = f'{v["width"]//g}:{v["height"]//g}'
    lines = ["  <fileinfo>", "    <streamdetails>", "      <video>"]
    for k, val in [("codec", v.get("codec_name")), ("bitrate", v.get("bit_rate")),
                   ("width", v.get("width")), ("height", v.get("height")),
                   ("aspect", ar), ("aspectratio", ar),
                   ("framerate", fr),
                   ("scantype", "progressive")]:
        if val:
            lines.append(f"        <{k}>{val}</{k}>")
    lines += ["        <default>True</default>", "        <forced>False</forced>",
              f"        <duration>{dur_i}</duration>",
              f"        <durationinseconds>{dur_i}</durationinseconds>",
              "      </video>"]
    if a:
        lines += ["      <audio>",
                  f"        <codec>{a.get('codec_name','')}</codec>"]
        if a.get("bit_rate"):
            lines.append(f"        <bitrate>{a['bit_rate']}</bitrate>")
        lines += [f"        <language>{a.get('language','und')}</language>",
                  f"        <channels>{a.get('channels','')}</channels>",
                  f"        <samplingrate>{a.get('sample_rate','')}</samplingrate>",
                  "        <default>True</default>",
                  "        <forced>False</forced>",
                  "      </audio>"]
    lines += ["    </streamdetails>", "  </fileinfo>"]
    return "\n".join(lines)


# ---------------------------------------------------------------- nfo
def cdata(s):
    return "<![CDATA[" + (s or "").replace("]]>", "]]]]><![CDATA[>") + "]]>"

def build_nfo(d, info, dirname):
    code = d.get("code") or dirname
    t = (d.get("title") or "").strip()
    for a in d.get("actors", []):          # 去掉标题末尾的演员名
        if a and t.endswith(a):
            t = t[: -len(a)].strip()
    title = f"{code} {t}".strip()
    year = (d.get("releasedate") or "")[:4]
    mins = re.sub(r"[^\d]", "", d.get("runtime", "")) or "0"
    desc = d.get("desc") or ""
    plot = desc or title
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    p = ['﻿<?xml version="1.0" encoding="utf-8" standalone="yes"?>', "<movie>"]
    p.append(f"  <plot>{cdata(plot)}</plot>")
    p.append(f"  <outline>{cdata('發行日期 ' + (d.get('releasedate') or ''))}</outline>")
    p.append("  <customrating>NC-17</customrating>")
    p.append("  <lockdata>false</lockdata>")
    p.append(f"  <dateadded>{now}</dateadded>")
    p.append(f"  <title>{cdata(title)}</title>")
    p.append(f"  <originaltitle>{cdata(t)}</originaltitle>")
    for a in d.get("actors", []):
        p += ["  <actor>", f"    <name>{cdata(a)}</name>", "    <type>Actor</type>", "  </actor>"]
    if d.get("director"):
        p.append(f"  <director>{cdata(d['director'])}</director>")
    if year:
        p.append(f"  <year>{year}</year>")
    if mins != "0":
        p.append(f"  <runtime>{mins}</runtime>")
        p.append(f"  <runtimeinseconds>{int(mins) * 60}</runtimeinseconds>")
    p.append(f"  <sorttitle>{cdata(title)}</sorttitle>")
    p.append("  <mpaa>NC-17</mpaa>")
    if d.get("releasedate"):
        p.append(f"  <premiered>{d['releasedate']}</premiered>")
        p.append(f"  <releasedate>{d['releasedate']}</releasedate>")
    p.append(f"  <tagline>{cdata('發行日期 ' + (d.get('releasedate') or ''))}</tagline>")
    if d.get("studio"):
        p.append(f"  <studio>{cdata(d['studio'])}</studio>")
    if d.get("series"):
        p += ["  <set>", f"    <name>{cdata(d['series'])}</name>", "  </set>"]
    fi = fileinfo_xml(info)
    if fi:
        p.append(fi)
    if t:
        p.append(f"  <originalplot>{cdata(d.get('desc') or t)}</originalplot>")
    if d.get("releasedate"):
        p.append(f"  <release>{d['releasedate']}</release>")
    p.append(f"  <num>{cdata(code)}</num>")
    for k in ("maker", "publisher"):
        if d.get("studio"):
            p.append(f"  <{k}>{cdata(d['studio'])}</{k}>")
    if d.get("label"):
        p.append(f"  <label>{cdata(d['label'])}</label>")
    for g in d.get("genres", []):
        p.append(f"  <tag>{cdata(g)}</tag>")
    p.append("</movie>")
    return "\n".join(p) + "\n"


# ---------------------------------------------------------------- 图片
def crop_poster(src, dst, w=379, h=538):
    try:
        from PIL import Image, ImageFile
        ImageFile.LOAD_TRUNCATED_IMAGES = True   # 图床限速常返回半截 JPEG，仍可裁出 poster
        im = Image.open(src)
        iw, ih = im.size
        tw, th = w / h, iw / ih
        if iw / ih > tw:                 # 太宽 -> 中间裁两边
            nw = int(ih * tw); left = (iw - nw) // 2
            im = im.crop((left, 0, left + nw, ih))
        else:                            # 太高 -> 上下裁
            nh = int(iw / th); top = int((ih - nh) * 0.35)
            im = im.crop((0, top, iw, top + nh))
        im.resize((w, h), Image.LANCZOS).save(dst, "JPEG", quality=92)
        return True
    except Exception as e:
        print(f"   ! poster 裁剪失败: {e}", file=sys.stderr)
        return False


def code_of(foldername):
    """ABC-123-UC -> ABC-123 ; PRED- 566- UC -> XYZ-456 ; XYZ-789_FHD_CH -> XYZ-789"""
    s = re.sub(r"[\s_]+", "", foldername).upper()
    m = re.match(r"([A-Z0-9]{2,10}-\d{3,6})", s)
    return m.group(1) if m else ""


# ---------------------------------------------------------------- 主流程
def scrape_one(sess, folder: Path, args):
    name = folder.name
    code = code_of(name)
    video = None
    for f in sorted(folder.iterdir()):
        if f.is_file() and media_ext.is_video(f.name):
            video = f; break
    if not video:
        return f"SKIP  {name}: 没有视频文件"
    if not code:
        return f"SKIP  {name}: 番号解析失败"

    # 输出文件与视频同名（Kodi/Emby 只认和视频同名的 nfo）
    stem = video.stem
    nfo = folder / f"{stem}.nfo"
    if not args.force:
        have = sorted(p.name for p in folder.glob("*.nfo"))
        if have:
            return f"EXIST {name}: {have[0]} 已存在"

    # 1. 抓页面（404 -> 搜索回退）
    path = f"/{code}"
    st, body = sess.request(path, referer=f"https://{HOST}/")
    page = body.decode("utf-8", "ignore")
    if st == 404 or "404 Page Not Found" in page[:2000]:
        st, body = sess.request(f"/search/{code}", referer=f"https://{HOST}/")
        page = body.decode("utf-8", "ignore")
        m = re.search(r'<a class="movie-box" href="([^"]+)"', page)
        if not m:
            return f"MISS  {name}: JavBus 查不到 {code}"
        p = urllib.parse.urlparse(m.group(1))
        path = p.path
        st, body = sess.request(p.path, referer=f"https://{HOST}/")
        page = body.decode("utf-8", "ignore")
    if st != 200 or "<h3>" not in page:
        return f"FAIL  {name}: HTTP {st}"

    d = parse_page(page)
    if not d.get("code"):
        d["code"] = code

    # 2. 图片
    page_url = f"https://{HOST}{path}"
    if not args.no_images:
        if d.get("cover"):
            raw = sess.fetch_url(d["cover"], referer=page_url)
            if raw and raw[:3] == b"\xff\xd8\xff":
                full = folder / f"{stem}-fanart.jpg"
                thumb = folder / f"{stem}-thumb.jpg"
                poster = folder / f"{stem}-poster.jpg"
                # 绝不覆盖已存在的文件（PT 种子里的文件可能同名）
                if not full.exists():
                    full.write_bytes(raw)
                if not thumb.exists():
                    shutil.copyfile(full, thumb)
                if not poster.exists():
                    crop_poster(full, poster)
            else:
                print(f"   ! 封面下载失败 {code}", file=sys.stderr)
        if d.get("samples"):
            ex = folder / "extrafanart"
            ex.mkdir(exist_ok=True)
            for i, s in enumerate(d["samples"][:10], 1):
                if (ex / f"fanart{i}.jpg").exists():
                    continue
                raw = sess.fetch_url(s, referer=page_url)
                if raw and raw[:3] == b"\xff\xd8\xff":
                    (ex / f"fanart{i}.jpg").write_bytes(raw)
                time.sleep(0.3)

    # 3. fileinfo
    info = {} if (args.no_probe or not media_ext.is_probeable(video.name)) else ffprobe_info(video)

    # 4. nfo
    nfo.write_text(build_nfo(d, info, name), encoding="utf-8")
    return (f"OK    {name} <- {d['code']} | {d.get('releasedate','')} "
            f"{d.get('runtime','')} | 演员 {len(d.get('actors',[]))} | "
            f"类别 {len(d.get('genres',[]))} | {(d.get('title') or '')[:46]}")


def scan_video_folders(root: Path):
    """递归找出 root 下所有直接含视频文件的叶子文件夹（下载目录结构里番号在叶子层）。

    跳过 #recycle 与已刮过的（含同名 nfo 的不进列表，由 scrape_one 兜底再判一次）。
    """
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "#recycle"]
        p = Path(dirpath)
        has_video = any(f.lower().endswith(VIDEO_EXT) for f in filenames)
        if has_video:
            out.append(p)
    return sorted(out)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--dry", action="store_true", help="只列出要处理的文件夹")
    ap.add_argument("--limit", type=int, default=0)
    ap.add_argument("--only", default="")
    ap.add_argument("--dirs", action="append", default=[],
                    help="直接指定要刮的文件夹路径（可重复），可为任意绝对路径；"
                         "给了就忽略 BASE/--only")
    ap.add_argument("--scan", default="",
                    help="递归扫描该目录下所有含视频的叶子文件夹并逐个刮削（用于下载目录）")
    ap.add_argument("--force", action="store_true", help="已有 nfo 也重刮")
    ap.add_argument("--no-probe", action="store_true")
    ap.add_argument("--no-images", action="store_true")
    ap.add_argument("--log", default="jav_scrape.log")
    ap.add_argument("--root", default=os.environ.get("XIEYIN_ROOT", str(BASE)),
                    help="平铺模式下的根目录（默认 /vol1/1000/Media，也可用环境变量 XIEYIN_ROOT）")
    ap.add_argument("--via", choices=["auto", "direct", "proxy"], default="auto",
                    help="通道: auto=自动探测+兜底(默认) / direct=固定IP直连优先 / proxy=代理优先")
    ap.add_argument("--proxy", default="",
                    help=f"代理地址，默认探测 {PROXY_DEFAULT}（也可用环境变量 XIEYIN_PROXY）")
    args = ap.parse_args()

    base = Path(args.root)
    folders = sorted([p for p in base.iterdir()
                      if p.is_dir() and p.name != "#recycle"], key=lambda p: p.name) \
        if base.is_dir() else []
    if args.dirs:
        folders = [Path(d) for d in args.dirs]
        missing = [str(f) for f in folders if not f.is_dir()]
        if missing:
            sys.exit("目录不存在: " + ", ".join(missing))
    elif args.scan:
        folders = scan_video_folders(Path(args.scan))
    elif args.only:
        folders = [p for p in folders if p.name == args.only]
    if args.limit:
        folders = folders[:args.limit]

    print(f"共 {len(folders)} 个文件夹待刮")
    if args.dry:
        for f in folders:
            print(f"   {f.name:24s} -> 番号 {code_of(f.name)}")
        return

    # 通道选择：--proxy > 环境变量 XIEYIN_PROXY > 自动探测 127.0.0.1:7890
    proxy_url = args.proxy or os.environ.get("XIEYIN_PROXY", "")
    if not proxy_url and args.via != "direct" and _port_open("127.0.0.1", 7890):
        proxy_url = PROXY_DEFAULT

    sess = pick_session(args.via, proxy_url)
    ok = miss = err = exist = 0
    log = open(args.log, "a", encoding="utf-8")
    log.write(f"\n===== {datetime.now()} 开始，共 {len(folders)} 个，"
              f"通道 {sess.channel} =====\n")
    for i, folder in enumerate(folders, 1):
        try:
            msg = scrape_one(sess, folder, args)
        except Exception as e:
            msg = f"ERROR {folder.name}: {e}"
        print(f"[{i:2d}/{len(folders)}] {msg}")
        log.write(msg + "\n"); log.flush()
        if msg.startswith("OK"): ok += 1
        elif msg.startswith(("MISS", "SKIP")): miss += 1
        elif msg.startswith("EXIST"): exist += 1
        else: err += 1
        time.sleep(DELAY)
    print(f"\n成功 {ok} / 已存在跳过 {exist} / 未找到 {miss} / 失败 {err}   "
          f"通道 {sess.channel}   日志: {args.log}")
    log.close()

if __name__ == "__main__":
    main()
