#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
# 网络与依赖体检（不写任何文件）
# 用法: net_test.sh [媒体目录]
set -uo pipefail

PY="${XIEYIN_PY:-/usr/bin/python3}"
SCRIPTS="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
DIR="${1:-/vol1/1000/Media}"
UA='Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36'

ok()  { echo "  ✓ $*"; }
bad() { echo "  ✗ $*"; }

echo "== 1. DoH 解析 www.javbus.com =="
ips=$(curl -s --max-time 15 -H 'accept: application/dns-json' \
    'https://doh.pub/dns-query?name=www.javbus.com&type=A' \
    | "$PY" -c "import json,sys
try:
    d=json.load(sys.stdin)
    print(', '.join(a['data'] for a in d.get('Answer',[]) if a.get('type')==1))
except Exception:
    pass" 2>/dev/null)
if [ -n "$ips" ]; then ok "解析结果: $ips"; else bad "DoH 解析失败（可能被墙或 DNS 异常）"; fi

echo
echo "== 2. JavBus 直连（固定 IP） =="
XIEYIN_SCRIPTS="$SCRIPTS" "$PY" - <<'PY' 2>&1
import sys, os
sys.path.insert(0, os.environ.get("XIEYIN_SCRIPTS", "/usr/local/xieyin/scripts"))
try:
    from jav_scrape import Session
    s = Session()
    st, body = s.request("/")
    print(f"  ✓ HTTP {st}，首页 {len(body)} 字节，通道 {getattr(s, 'channel', 'direct')}")
except Exception as e:
    print(f"  ✗ 连接失败: {e}")
PY

echo
echo "== 3. TMDB 搜索页 =="
code=$(curl -s --max-time 20 -o /dev/null -w '%{http_code}' -A "$UA" \
    'https://www.themoviedb.org/search?query=test')
[ "$code" = "200" ] && ok "HTTP 200" || bad "HTTP $code"

echo
echo "== 4. TMDB 图床 =="
code=$(curl -s --max-time 20 -o /dev/null -w '%{http_code}' -A "$UA" \
    'https://image.tmdb.org/t/p/original/lMkW3CqS09KwLJ8k0iyJWk9FCPO.jpg')
[ "$code" = "200" ] && ok "HTTP 200" || bad "HTTP $code"

echo
echo "== 5. 本地依赖 =="
if command -v ffprobe >/dev/null 2>&1; then
    ok "ffprobe: $(ffprobe -version 2>/dev/null | head -1)"
else
    bad "没有 ffprobe：刮削会自动跳过 fileinfo（不影响 nfo/图片）"
fi
if command -v ffmpeg >/dev/null 2>&1; then ok "ffmpeg: $(command -v ffmpeg)"; else bad "没有 ffmpeg"; fi
if command -v curl >/dev/null 2>&1; then ok "curl: $(command -v curl)"; else bad "没有 curl（TMDB 刮削需要）"; fi
"$PY" - <<'PY'
try:
    from PIL import Image  # noqa
    print("  ✓ Pillow：可裁剪 poster")
except Exception:
    print("  ✗ Pillow：海报原图仍会保存，但不会裁剪成 379x538")
PY

echo
echo "== 6. 媒体目录 =="
if [ -d "$DIR" ]; then
    n=$(find "$DIR" -maxdepth 1 -mindepth 1 -type d 2>/dev/null | wc -l)
    ok "$DIR（一级目录 $n 个）"
else
    bad "$DIR 不存在或未挂载"
fi

echo
echo "== 7. 代理 =="
p="${XIEYIN_PROXY:-}"
if [ -n "$p" ]; then
    host=$(printf '%s' "$p" | sed -E 's#^[a-z]+://##; s#[:/].*$##')
    port=$(printf '%s' "$p" | sed -E 's#^[^:]+://##; s#^[^:]*:##; s#[/].*$##')
    if [ -n "$host" ] && [ -n "$port" ] && timeout 3 bash -c "</dev/tcp/$host/$port" 2>/dev/null; then
        ok "代理 $host:$port 可连接"
    else
        bad "代理 $host:$port 连不上（不影响直连）"
    fi
else
    echo "  · 未配置代理（直连优先；需要时可在设置里填 http://127.0.0.1:7890）"
fi

echo
echo "体检结束。"
