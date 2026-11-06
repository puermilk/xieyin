#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
# 检查某个目录里的视频是否都有元数据（nfo / 海报 / 背景图），并校验 nfo XML 是否合法
# 用法: bash check_meta.sh                    # 默认查远程共享
#       bash check_meta.sh /vol1/1000/Media    # 查指定目录（含子目录）
set -uo pipefail

DIR="${1:-/vol1/1000/Media}"
[ -d "$DIR" ] || { echo "目录不存在: $DIR"; exit 1; }

echo "检查目录: $DIR"
echo

mapfile -d '' VIDS < <(find "$DIR" -type f \
    \( -iname '*.mp4' -o -iname '*.mkv' -o -iname '*.avi' -o -iname '*.wmv' \
       -o -iname '*.ts' -o -iname '*.mov' \) -print0)

total=${#VIDS[@]}
with_nfo=0
declare -a MISS_NFO=()

for v in "${VIDS[@]}"; do
    if [ -f "${v%.*}.nfo" ]; then
        with_nfo=$((with_nfo+1))
    else
        MISS_NFO+=("$v")
    fi
done

echo "视频总数      : $total"
echo "有同名 nfo    : $with_nfo"
echo "缺 nfo        : $((total - with_nfo))"

if [ ${#MISS_NFO[@]} -gt 0 ]; then
    echo
    echo "缺 nfo 的视频（这些通常是没刮过，或还没下完）:"
    for v in "${MISS_NFO[@]}"; do
        sz=$(stat -c %s "$v" 2>/dev/null)
        # 能不能播
        if timeout 25 ffprobe -v error -show_entries format=duration -of csv=p=0 "$v" >/dev/null 2>&1; then
            st="可播放"
        else
            st="⚠ 无法播放(文件不完整)"
        fi
        printf '   [%s] %6.2f GB  %s\n' "$st" "$(awk -v s="$sz" 'BEGIN{print s/1073741824}')" "$v"
    done
fi

echo
echo -n "图片统计      : poster "
find "$DIR" \( -name '*poster*.jpg' -o -name 'poster.jpg' -o -name 'folder.jpg' \) | wc -l
echo -n "                fanart "
find "$DIR" \( -name '*fanart*.jpg' -o -name 'fanart.jpg' \) | wc -l

echo
python3 - "$DIR" <<'EOF'
import glob, sys, xml.etree.ElementTree as ET
root = sys.argv[1]
files = glob.glob(root + '/**/*.nfo', recursive=True)
bad = []
for f in files:
    try:
        ET.parse(f)
    except Exception as e:
        bad.append((f, str(e)))
print(f"nfo XML 校验  : 共 {len(files)} 个，损坏 {len(bad)} 个")
for f, e in bad:
    print("   ✗", f, "->", e)
EOF
