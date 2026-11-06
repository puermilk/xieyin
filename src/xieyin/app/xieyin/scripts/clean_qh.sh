#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
# 清理远程挂载共享 /vol1/1000/Media
# 规则：1) 每个文件夹只保留最大的视频（>500MB），删除其余视频
#       2) 可选：文件名去掉 @ 之前的域名前缀：489155.com@VNDS-2850-C.mp4 -> VNDS-2850-C.mp4
#       PT 保护：检测到 @GROUP 做种文件 / *.torrent 时拒绝执行，绝不改名或删除
# 用法：clean_qh.sh dry   (只打印清单，不改动)
#       clean_qh.sh run   (真正执行)
# 环境：XIEYIN_PT_GUARD=1|0     PT 保护开关（默认 1）
#       XIEYIN_KEEP_SIDECARS=1|0 保留 nfo/图片/字幕等非视频文件（默认 1）
set -uo pipefail

# 目录优先级：环境变量 XIEYIN_BASE > 第 2 个参数 > 默认远程共享
BASE="${XIEYIN_BASE:-${2:-/vol1/1000/Media}}"
MODE="${1:-dry}"
BIG="+500M"
MAXPASS=6
PT_GUARD="${XIEYIN_PT_GUARD:-1}"
KEEP_SIDECARS="${XIEYIN_KEEP_SIDECARS:-1}"

R_DEL=0; R_KEEP=0; R_REN=0

[ -d "$BASE" ] || { echo "目录不存在: $BASE"; exit 1; }
cd "$BASE" || exit 1

is_video() {
  case "${1,,}" in
    *.mp4|*.mkv|*.avi|*.mov|*.wmv|*.flv|*.m4v|*.mpg|*.mpeg|*.ts|*.webm|*.rmvb|*.iso) return 0 ;;
    *) return 1 ;;
  esac
}

# 只认 489155.com / www.xxx.net 这类真正的域名前缀，
# 避免把 Movie.2024.BluRay 这种发布名当成域名。
is_domain_prefix() {
  local p="$1"
  [ ${#p} -le 40 ] || return 1
  [[ "$p" =~ ^([A-Za-z0-9-]+\.)+(com|net|org|cc|tv|me|xyz|top|vip|info|club|site|online|cn|io|co)$ ]]
}

# PT 发布名：xxx@GROUP / xxx@GROUP-10bit / xxx@GROUP（@ 在名字末尾，忽略扩展名）
# 域名前缀形式 489155.com@VNDS-2850-C.mp4 不算 PT。
# 只有干净的短后缀才当扩展名剥离，避免把 Movie.2024.1080p@GROUP 的 @GROUP 吃掉。
is_pt_name() {
  local b stem ext pre
  b=$(basename -- "$1")
  stem="$b"
  ext="${b##*.}"
  if [ "$ext" != "$b" ] && [[ "$ext" =~ ^[A-Za-z0-9]{1,5}$ ]]; then
    stem="${b%.*}"
  fi
  [[ "$stem" == *@* ]] || return 1
  pre="${stem%%@*}"
  if is_domain_prefix "$pre"; then
    return 1
  fi
  [[ "$stem" =~ @[A-Za-z][A-Za-z0-9_-]{0,19}$ ]]
}

# 找到第一个 PT 特征就返回，避免在大目录上把所有文件都扫完
detect_pt() {
  local root="$1" f
  while IFS= read -r -d '' f; do
    printf '%s\n' "$f"
    return 0
  done < <(find "$root" -type f -name '*.torrent' -print0 2>/dev/null)
  while IFS= read -r -d '' f; do
    if is_pt_name "$f"; then
      printf '%s\n' "$f"
      return 0
    fi
  done < <(find "$root" \( -type f -o -type d \) -name '*@*' -print0 2>/dev/null)
  return 1
}

# ---------- 一次清理：root 下每个目录保留最大视频，其余删除 ----------
pass_cleanup() {
  local root="$1"
  local -A keep=() dirs=()
  local d f sz p best bestsz
  local -a delfiles=()
  R_DEL=0; R_KEEP=0

  [ -d "$root" ] || { echo "   (目录不存在: $root)"; return; }

  while IFS= read -r -d '' d; do dirs["$d"]=1; done \
    < <(find "$root" -type f -printf '%h\0' | sort -zu)

  for d in "${!dirs[@]}"; do
    best=""; bestsz=-1
    while IFS=$'\t' read -r sz p; do
      [ -n "${p:-}" ] || continue
      if is_video "$p" && [ "$sz" -gt "$bestsz" ]; then bestsz=$sz; best="$p"; fi
    done < <(find "$d" -maxdepth 1 -type f -size "$BIG" -printf '%s\t%p\n' | sort -rn)
    [ -n "$best" ] && keep["$d"]="$best"
  done

  while IFS= read -r -d '' f; do
    d=$(dirname -- "$f")
    if [ "${keep[$d]:-}" = "$f" ]; then
      R_KEEP=$((R_KEEP+1))
      continue
    fi
    # 保留 nfo / 图片 / 字幕等附属文件，避免把刮削结果清掉
    if [ "$KEEP_SIDECARS" = "1" ] && ! is_video "$f"; then
      continue
    fi
    delfiles+=("$f")
  done < <(find "$root" -type f -print0 | sort -z)

  R_DEL=${#delfiles[@]}
  echo "--- [$root] 保留 $R_KEEP 个大视频，待删 $R_DEL 个"
  for f in "${delfiles[@]}"; do printf '   DEL   %s\n' "$f"; done

  if [ "$MODE" = "run" ] && [ "$R_DEL" -gt 0 ]; then
    local failed=0
    for f in "${delfiles[@]}"; do
      rm -f -- "$f" || { echo "   !! 删除失败: $f"; failed=1; }
    done
    echo "   >>> 实际删除 $R_DEL 个（失败 $failed）"
  fi
}

# ---------- 重命名：去掉 @ 之前的域名样式前缀 ----------
pass_rename() {
  local f b pre post new
  R_REN=0
  while IFS= read -r -d '' f; do
    # 第二道保险：PT 发布名绝不改名
    is_pt_name "$f" && continue
    b=$(basename -- "$f")
    case "$b" in
      *@*)
        pre="${b%%@*}"; post="${b#*@}"
        # 只处理「域名@文件名」：域名必须是真正的域名，且 @ 后是带扩展名的文件名
        if is_domain_prefix "$pre" && [[ "$post" == *.* ]]; then
          new="$(dirname -- "$f")/$post"
          if [ -e "$new" ]; then
            echo "   SKIP(目标已存在) $f"
          else
            R_REN=$((R_REN+1))
            printf '   REN   %s\n         -> %s\n' "$f" "$new"
            [ "$MODE" = "run" ] && { mv -- "$f" "$new" || { echo "   !! 重命名失败: $f"; R_REN=$((R_REN-1)); }; }
          fi
        fi
        ;;
    esac
  done < <(find "$BASE" -type f -name '*@*' -print0 | sort -z)
  echo ">>> 需重命名 $R_REN 个"
}

# ---------- 清理空目录（只在 #recycle 内）----------
prune_empty() {
  local n=0 d
  [ -d "$BASE/#recycle" ] || return 0
  while IFS= read -r -d '' d; do
    n=$((n+1)); printf '   RMDIR %s\n' "$d"
    [ "$MODE" = "run" ] && rmdir -- "$d" 2>/dev/null
  done < <(find "$BASE/#recycle" -mindepth 1 -depth -type d -empty -print0)
  echo ">>> 空目录 $n 个"
}

echo "=========== 模式: $MODE ==========="
echo "PT 保护: $([ "$PT_GUARD" = "1" ] && echo 开启 || echo 关闭)   保留非视频文件: $([ "$KEEP_SIDECARS" = "1" ] && echo 是 || echo 否)"

if [ "$PT_GUARD" = "1" ]; then
  HIT=$(detect_pt "$BASE" || true)
  if [ -n "$HIT" ]; then
    echo
    echo "!! 检测到 PT 做种文件：$HIT"
    echo "!! 为避免破坏做种（改名/删除都会让 PT 校验失败），本次清理已中止。"
    echo "!! 没有改动任何文件。确认不是 PT 目录、或已停止做种后，"
    echo "!! 可设置 XIEYIN_PT_GUARD=0 再执行。"
    exit 2
  fi
fi

echo "=========== 第 1 轮：清理 80 个影片文件夹 ==========="
pass_cleanup "$BASE"
TOTAL_DEL=$R_DEL; TOTAL_KEEP=$R_KEEP

echo "=========== 第 2 轮起：清理 #recycle（服务端可能把刚删的文件丢进回收站）==========="
for pass in $(seq 1 $MAXPASS); do
  pass_cleanup "$BASE/#recycle"
  TOTAL_DEL=$((TOTAL_DEL + R_DEL))
  echo "   >>> 回收站第 $pass 次：删除 $R_DEL，保留 $R_KEEP"
  [ "$MODE" = "dry" ] && break
  [ "$MODE" = "run" ] && [ "$R_DEL" -eq 0 ] && break
done

echo "=========== 重命名 ==========="
pass_rename

echo "=========== 空目录清理 ==========="
prune_empty

echo "=========== 结果统计 ==========="
echo "大视频(>500MB)数量: $(find "$BASE" -type f -size "$BIG" | wc -l)"
echo "剩余文件总数:       $(find "$BASE" -type f | wc -l)"
echo "仍带 @ 的文件:      $(find "$BASE" -type f -name '*@*' | wc -l)"
if [ "$MODE" != "run" ]; then
  echo "=========== DRY-RUN：未做任何修改 ==========="
fi
