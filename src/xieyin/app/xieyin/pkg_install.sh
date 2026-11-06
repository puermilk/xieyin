#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
# xieyin 载荷：把运行文件铺到 /usr/local/xieyin，写 systemd 单元并拉起。
# 幂等：重复执行只更新代码，保留 data/config.json 与 data/logs。
set -u
DIR=/usr/local/xieyin
SRC="$(cd "$(dirname "$0")" && pwd)"
LOG=${TRIM_TEMP_LOGFILE:-/tmp/xieyin_install.log}

mkdir -p "$DIR/scripts" "$DIR/www" "$DIR/conf" "$DIR/data/logs"
chmod 755 "$DIR" "$DIR/data" "$DIR/data/logs" 2>/dev/null

# 1) 代码
install -m 0644 "$SRC/app_server.py" "$DIR/app_server.py"
for f in "$SRC"/scripts/*; do
    [ -f "$f" ] || continue
    case "$f" in
        *.py) install -m 0644 "$f" "$DIR/scripts/$(basename "$f")" ;;
        *)    install -m 0755 "$f" "$DIR/scripts/$(basename "$f")" ;;
    esac
done
for f in "$SRC"/www/*; do
    [ -f "$f" ] && install -m 0644 "$f" "$DIR/www/$(basename "$f")"
done
for f in "$SRC"/conf/*; do
    [ -f "$f" ] || continue
    [ -f "$DIR/conf/$(basename "$f")" ] || install -m 0644 "$f" "$DIR/conf/$(basename "$f")"
done
for f in "$SRC"/pkg_*.sh "$SRC"/setup_units.sh "$SRC"/teardown_units.sh; do
    [ -f "$f" ] && install -m 0755 "$f" "$DIR/$(basename "$f")"
done
chmod 755 "$DIR/scripts"/*.sh 2>/dev/null

# 2) 首次安装：写默认配置（自动选一个可用媒体目录，装完可在网页里改）
if [ ! -f "$DIR/data/config.json" ]; then
    /usr/bin/python3 - "$DIR/data" <<'PY'
import json, os, sys
candidates = ("/vol1/1000/Media", "/vol1/1000", "/vol1")
root = ""
for p in candidates:
    try:
        if os.path.isdir(p) and os.listdir(p):
            root = p
            break
    except OSError:
        pass
if not root:
    for p in candidates:
        if os.path.isdir(p):
            root = p
            break
conf = {
    "port": 22668,
    "bind": "0.0.0.0",
    "media_root": root or "/vol1/1000/Media",
    "proxy": "",
    "jav": {"via": "auto", "recursive": True},
    "tmdb": {},
    "history_limit": 80,
}
d = sys.argv[1]
os.makedirs(d, exist_ok=True)
with open(os.path.join(d, "config.json"), "w", encoding="utf-8") as f:
    json.dump(conf, f, ensure_ascii=False, indent=2)
PY
fi

# 3) 单元与启动
bash "$DIR/setup_units.sh" >> "$LOG" 2>&1
systemctl enable xieyin.service >/dev/null 2>&1
systemctl restart xieyin.service >> "$LOG" 2>&1
exit 0
