#!/bin/bash
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
# 清理媒体目录：每个影片文件夹只保留最大的视频，删除广告视频/垃圾文件/孤儿 NFO。
#       1) 每个影片文件夹只保留最大的视频（需 >100M，否则整目录跳过）
#       2) 文件名去掉 @ 之前的域名前缀：example.com@ABC-123.mp4 -> ABC-123.mp4
#       3) 删除被删视频留下的同名 NFO/封面/演员图，以及广告名 NFO
#       PT 保护：检测到 @GROUP 做种文件 / *.torrent 时拒绝执行，绝不改名或删除
#       .strm 保护：流指针文件无条件不删、不改名（删了 STRM 库就废了）
# 用法：clean_qh.sh dry   (只打印清单，不改动)
#       clean_qh.sh run   (真正执行)
# 环境：XIEYIN_PT_GUARD=1|0         PT 保护开关（默认 1）
#       XIEYIN_KEEP_SIDECARS=1|0    保留 nfo/图片/字幕等非视频文件（默认 1）
#       XIEYIN_CLEAN_ORPHANS=1|0    清理广告名孤儿 NFO/图片（默认 1）
#       XIEYIN_MIN_MAIN=100M        小于该体积的目录视为无主视频，整目录跳过（默认 100M）
set -uo pipefail

# 目录优先级：环境变量 XIEYIN_BASE > 第 2 个参数 > 默认远程共享
BASE="${XIEYIN_BASE:-${2:-/vol1/1000/Media}}"
MODE="${1:-dry}"
MIN_MAIN="${XIEYIN_MIN_MAIN:-100M}"   # 小于这个体积的目录视为没有主视频，整目录跳过
MIN_MAIN="${MIN_MAIN#+}"
MAXPASS=6
PT_GUARD="${XIEYIN_PT_GUARD:-1}"
KEEP_SIDECARS="${XIEYIN_KEEP_SIDECARS:-1}"
CLEAN_ORPHANS="${XIEYIN_CLEAN_ORPHANS:-1}"

R_DEL=0; R_KEEP=0; R_REN=0; R_ORPHAN=0; R_JUNK=0; R_SKIP=0

[ -d "$BASE" ] || { echo "目录不存在: $BASE"; exit 1; }
cd "$BASE" || exit 1

is_video() {
  case "${1,,}" in
    *.mp4|*.mkv|*.avi|*.mov|*.wmv|*.flv|*.m4v|*.mpg|*.mpeg|*.ts|*.webm|*.rmvb|*.iso) return 0 ;;
    *) return 1 ;;
  esac
}

# .strm 是 Kodi/Jellyfin 的流指针文件（几百字节的文本），永远不能删。
# 也不算「视频」：否则「只留最大视频」会把它当垃圾淘汰掉。
is_strm() {
  case "${1,,}" in
    *.strm) return 0 ;;
    *) return 1 ;;
  esac
}

# 纯广告/网页垃圾：不影响媒体库，直接删。
is_junk() {
  local b
  b=$(basename -- "$1")
  case "${b,,}" in
    *.html|*.htm|*.mhtml|*.mht|*.url|*.webloc|*.lnk) return 0 ;;
    # 旧版翻译/演员整理生成的 NFO 备份，统一清掉
    *.nfo.bak|*.nfo.bak-*|*.bak-retranslate|*.bak-actor|*.bak-thumb|*.bak-zh) return 0 ;;
    *.txt|*.part) is_ad_name "$b" && return 0 ;;
  esac
  return 1
}

# 广告/引流文件名特征：只用于清理孤儿 NFO/图片，避免误删正常中文标题。
is_ad_name() {
  local n="${1//[[:space:]]/}"
  n="${n//$'\u3000'/}"
  case "${n,,}" in
    *直播*|*情報*|*情报*|*最新*|*地址*|*网址*|*網址*|*聚合*|*福利*|*广告*|*廣告*|*获取*|*獲取*|*社区*|*社區*|*下載*|*下载*|*永久*|*备用*|*備用*|*发布*|*發布*) return 0 ;;
    *) return 1 ;;
  esac
}

# 只认 example.com / www.example.net 这类真正的域名前缀，
# 避免把 Movie.2024.BluRay 这种发布名当成域名。
is_domain_prefix() {
  local p="$1"
  [ ${#p} -le 40 ] || return 1
  [[ "$p" =~ ^([A-Za-z0-9-]+\.)+(com|net|org|cc|tv|me|xyz|top|vip|info|club|site|online|cn|io|co)$ ]]
}

# 纯域名文件名（98t.tv.nfo）通常是广告，不是影片标题。
is_domain_name() {
  local p="$1"
  [ ${#p} -le 40 ] || return 1
  [[ "$p" =~ ^([A-Za-z0-9-]+\.)+(com|net|org|cc|tv|me|xyz|top|vip|info|club|site|online|cn|io|co)$ ]]
}

# 每个影片一个文件夹（ABC-123-UC / DEF-456-C）才按「一主片 + 广告」处理；
# 平铺目录（一个目录几十个视频）绝不全删，只删广告名视频。
is_title_folder() {
  local n
  n=$(basename -- "$1")
  [[ "$n" =~ [A-Za-z]{2,6}-?[0-9]{2,5} ]]
}

# 正在下载/做种的目录不动：qBittorrent 的 .part / .!qB，aria2 的 .aria2。
# 否则改名/删广告会让 qBittorrent 找不到文件，甚至把正片重新标成 .part。
has_active_download() {
  find "$1" -maxdepth 1 -type f \( -name '*.part' -o -name '*.!qB' -o -name '*.aria2' \) \
    -print -quit 2>/dev/null | grep -q .
}

# PT 发布名：xxx@GROUP / xxx@GROUP-10bit / xxx@GROUP（@ 在名字末尾，忽略扩展名）
# 域名前缀形式 example.com@ABC-123.mp4 不算 PT。
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

# 删除被删视频的同名附属文件（nfo/封面/演员图/字幕），避免留下孤儿 NFO。
drop_sidecars() {
  local f="$1" d b stem x media=0
  d=$(dirname -- "$f"); b=$(basename -- "$f"); stem="${b%.*}"
  # 同 stem 还有别的视频/STRM（如 ABC-123.mkv + ABC-123.mp4 + ABC-123.strm）时，
  # nfo/图片是共用命名，不能跟着被删的这个容器一起删，更不能误删主视频或 STRM。
  while IFS= read -r -d '' x; do
    [ "$x" = "$f" ] && continue
    if is_video "$x" || is_strm "$x"; then media=1; break; fi
  done < <(find "$d" -maxdepth 1 -type f \( -name "$stem.*" -o -name "$stem-*" \) -print0 2>/dev/null)
  [ "$media" = "1" ] && return 0
  while IFS= read -r -d '' x; do
    [ "$x" = "$f" ] && continue
    is_video "$x" && continue
    is_strm "$x" && continue
    rm -f -- "$x" || true
  done < <(find "$d" -maxdepth 1 -type f \( -name "$stem.*" -o -name "$stem-*" \) -print0 2>/dev/null)
}

# ---------- 一次清理：root 下每个目录保留最大视频，其余视频/垃圾删除 ----------
pass_cleanup() {
  local root="$1" recycle="${2:-0}"
  local -A keep=() keepsz=() dirs=()
  local d f sz p best bestsz b
  local -a delfiles=() skipdirs=()
  R_DEL=0; R_KEEP=0; R_SKIP=0

  [ -d "$root" ] || { echo "   (目录不存在: $root)"; return; }

  # 回收站：里面的文件都是待清理的，全部删除，不再挑「最大视频」。
  if [ "$recycle" = "1" ]; then
    while IFS= read -r -d '' f; do
      # 即使文件被丢进回收站，.strm 也保留：它只是流指针，删了库就废了
      is_strm "$f" || delfiles+=("$f")
    done < <(find "$root" -type f -print0 | sort -z)
    R_DEL=${#delfiles[@]}
    echo "--- [$root] 回收站待删 $R_DEL 个文件"
    for f in "${delfiles[@]}"; do printf '   DEL   %s\n' "$f"; done
    if [ "$MODE" = "run" ] && [ "$R_DEL" -gt 0 ]; then
      local failed=0
      for f in "${delfiles[@]}"; do
        rm -f -- "$f" || { echo "   !! 删除失败: $f"; failed=1; }
      done
      echo "   >>> 实际删除 $R_DEL 个（失败 $failed）"
    fi
    return
  fi

  while IFS= read -r -d '' d; do dirs["$d"]=1; done \
    < <(find "$root" -type f -printf '%h\0' | sort -zu)

  for d in "${!dirs[@]}"; do
    if has_active_download "$d"; then
      R_SKIP=$((R_SKIP+1))
      skipdirs+=("$d（下载中）")
      continue
    fi
    best=""; bestsz=-1
    while IFS=$'\t' read -r sz p; do
      [ -n "${p:-}" ] || continue
      if is_video "$p" && [ "$sz" -gt "$bestsz" ]; then bestsz=$sz; best="$p"; fi
    done < <(find "$d" -maxdepth 1 -type f -size "+$MIN_MAIN" -printf '%s\t%p\n' | sort -rn)
    if [ -n "$best" ]; then
      keep["$d"]="$best"
      keepsz["$d"]="$bestsz"
    else
      R_SKIP=$((R_SKIP+1))
      skipdirs+=("$d")
    fi
  done

  while IFS= read -r -d '' f; do
    d=$(dirname -- "$f")
    if [ "${keep[$d]:-}" = "$f" ]; then
      R_KEEP=$((R_KEEP+1))
      continue
    fi
    # .strm 无条件保护：它是流指针，删了整个 STRM 库就废了
    if is_strm "$f"; then
      continue
    fi
    # 网页/广告垃圾文件，即使保留附属文件也清掉
    if is_junk "$f"; then
      delfiles+=("$f"); R_JUNK=$((R_JUNK+1)); continue
    fi
    if is_video "$f"; then
      b=$(basename -- "$f")
      # 只删广告名视频；平铺目录里不同番号的正常视频一律保留。
      if is_ad_name "$b"; then
        delfiles+=("$f")
        continue
      fi
      # 单个影片文件夹里，明显小于主视频的（<20%）视为预告/广告；平铺目录不套用。
      if is_title_folder "$d" && [ -n "${keep[$d]:-}" ]; then
        sz=$(stat -c %s -- "$f" 2>/dev/null || echo 0)
        ksz=${keepsz[$d]:-0}
        if [ "$ksz" -gt 0 ] && [ $((sz * 5)) -lt "$ksz" ]; then
          delfiles+=("$f")
          continue
        fi
      fi
      R_KEEP=$((R_KEEP+1))
      continue
    fi
    # 保留 nfo / 图片 / 字幕等附属文件，避免把刮削结果清掉
    if [ "$KEEP_SIDECARS" = "1" ]; then
      continue
    fi
    delfiles+=("$f")
  done < <(find "$root" -type f -print0 | sort -z)

  R_DEL=${#delfiles[@]}
  echo "--- [$root] 保留 $R_KEEP 个视频，待删 $R_DEL 个（垃圾 $R_JUNK），无主视频跳过 $R_SKIP 个目录"
  for d in "${skipdirs[@]}"; do printf '   SKIP  无>%s 主视频: %s\n' "$MIN_MAIN" "$d"; done
  for f in "${delfiles[@]}"; do printf '   DEL   %s\n' "$f"; done

  if [ "$MODE" = "run" ] && [ "$R_DEL" -gt 0 ]; then
    local failed=0
    for f in "${delfiles[@]}"; do
      rm -f -- "$f" || { echo "   !! 删除失败: $f"; failed=1; }
      if is_video "$f"; then
        drop_sidecars "$f"
      fi
    done
    echo "   >>> 实际删除 $R_DEL 个（失败 $failed）"
  fi
}

# ---------- 清理广告名孤儿 NFO / 图片（视频已删但 sidecar 还在）----------
pass_orphans() {
  local root="$1" d f b stem
  local -a orph=()
  [ "$CLEAN_ORPHANS" = "1" ] || return 0
  R_ORPHAN=0
  while IFS= read -r -d '' d; do
    has_active_download "$d" && continue
    local -A vstem=()
    while IFS= read -r -d '' f; do
      b=$(basename -- "$f")
      if is_video "$f" || is_strm "$f"; then
        vstem["${b%.*}"]=1
      elif [[ "${b,,}" == *.part ]]; then
        # 下载中的正片：ABC-123.mp4.part 的 stem 是 ABC-123
        b="${b%.part}"; vstem["${b%.*}"]=1
      fi
    done < <(find "$d" -maxdepth 1 -type f -print0)
    while IFS= read -r -d '' f; do
      b=$(basename -- "$f")
      stem=""
      case "${b,,}" in
        *.nfo) stem="${b%.*}" ;;
        *.nfo.bak-*) stem="${b%%.nfo.bak-*}" ;;
        *.nfo.bak) stem="${b%.nfo.bak}" ;;
        *.jpg|*.jpeg|*.png|*.webp)
          stem="${b%.*}"
          stem="${stem%-poster}"; stem="${stem%-thumb}"; stem="${stem%-fanart}"
          stem="${stem%-banner}"; stem="${stem%-landscape}"; stem="${stem%-clearlogo}"
          stem="${stem%-backdrop}"; stem="${stem%-logo}"; stem="${stem%-disc}"
          stem="${stem%%-actor-*}"
          ;;
        *) continue ;;
      esac
      case "$stem" in movie|tvshow|poster|fanart|folder|cover|"") continue ;; esac
      [ ${#vstem[@]} -gt 0 ] && [ -n "${vstem[$stem]:-}" ] && continue
      if is_ad_name "$stem" || is_domain_name "$stem"; then orph+=("$f"); fi
    done < <(find "$d" -maxdepth 1 -type f -print0 | sort -z)
  done < <(find "$root" -type d -print0 | sort -z)
  R_ORPHAN=${#orph[@]}
  echo "--- [$root] 广告名孤儿 sidecar 待删 $R_ORPHAN 个"
  for f in "${orph[@]}"; do printf '   ORPH  %s\n' "$f"; done
  if [ "$MODE" = "run" ] && [ "$R_ORPHAN" -gt 0 ]; then
    local failed=0
    for f in "${orph[@]}"; do
      rm -f -- "$f" || { echo "   !! 删除失败: $f"; failed=1; }
    done
    echo "   >>> 实际删除孤儿 $R_ORPHAN 个（失败 $failed）"
  fi
}

# ---------- 重命名：去掉 @ 之前的域名样式前缀 ----------
pass_rename() {
  local f b pre post new
  R_REN=0
  while IFS= read -r -d '' f; do
    # 下载中的目录不动，避免破坏 qBittorrent 做种
    has_active_download "$(dirname -- "$f")" && continue
    # .strm 绝不改名：改了就和它的 nfo/图片对不上
    is_strm "$f" && continue
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
            # 目标已存在：视频保留两边，避免误删不同内容；sidecar 视为重复，删掉带域名前缀的一份。
            if is_video "$f"; then
              echo "   SKIP(目标已存在) $f"
            else
              printf '   DUP-DEL %s（保留 %s）\n' "$f" "$new"
              [ "$MODE" = "run" ] && rm -f -- "$f" || true
            fi
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

# 重命名后修正 NFO 里 <thumb> 的旧域名路径，否则飞牛拿不到演员图/封面。
pass_fix_nfo_refs() {
  local f n=0
  [ "$MODE" = "run" ] || return 0
  while IFS= read -r -d '' f; do
    if grep -qE '<thumb>[^<]*[A-Za-z0-9.-]+\.(com|net|org|cc|tv|me|xyz|top|vip|info|club|site|online|cn|io|co)@' "$f" 2>/dev/null; then
      sed -i -E '/<thumb>/ s#[A-Za-z0-9.-]+\.(com|net|org|cc|tv|me|xyz|top|vip|info|club|site|online|cn|io|co)@##g' "$f" \
        && { echo "   FIX   $f"; n=$((n+1)); }
    fi
  done < <(find "$BASE" -type f -name '*.nfo' -print0 | sort -z)
  echo ">>> 修正 NFO 图片路径 $n 个"
}

# ---------- 清理空目录（在每个 #recycle 内）----------
prune_empty() {
  local n=0 d r
  while IFS= read -r -d '' r; do
    while IFS= read -r -d '' d; do
      n=$((n+1)); printf '   RMDIR %s\n' "$d"
      [ "$MODE" = "run" ] && rmdir -- "$d" 2>/dev/null
    done < <(find "$r" -mindepth 1 -depth -type d -empty -print0)
  done < <(find "$BASE" -type d -name '#recycle' -print0)
  echo ">>> 空目录 $n 个"
}

echo "=========== 模式: $MODE ==========="
echo "目录: $BASE"
echo "PT 保护: $([ "$PT_GUARD" = "1" ] && echo 开启 || echo 关闭)   保留非视频文件: $([ "$KEEP_SIDECARS" = "1" ] && echo 是 || echo 否)   清理广告孤儿: $([ "$CLEAN_ORPHANS" = "1" ] && echo 是 || echo 否)"

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

echo "=========== 第 1 轮：清理影片文件夹 ==========="
pass_cleanup "$BASE" 0
TOTAL_DEL=$R_DEL; TOTAL_KEEP=$R_KEEP

echo "=========== 第 2 轮起：清理 #recycle（服务端可能把刚删的文件丢进回收站）==========="
RECYCLES=()
while IFS= read -r -d '' r; do RECYCLES+=("$r"); done \
  < <(find "$BASE" -type d -name '#recycle' -print0 | sort -z)
if [ ${#RECYCLES[@]} -eq 0 ]; then
  echo "   (没有 #recycle 目录)"
else
  for r in "${RECYCLES[@]}"; do
    for pass in $(seq 1 $MAXPASS); do
      pass_cleanup "$r" 1
      TOTAL_DEL=$((TOTAL_DEL + R_DEL))
      echo "   >>> 回收站第 $pass 次：$r 删除 $R_DEL，保留 $R_KEEP"
      [ "$MODE" = "dry" ] && break
      [ "$MODE" = "run" ] && [ "$R_DEL" -eq 0 ] && break
    done
  done
fi

echo "=========== 孤儿 NFO / 广告 sidecar ==========="
pass_orphans "$BASE"

echo "=========== 重命名 ==========="
pass_rename
pass_fix_nfo_refs

echo "=========== 空目录清理 ==========="
prune_empty

echo "=========== 结果统计 ==========="
echo "视频文件数:         $(find "$BASE" -type f -print0 | while IFS= read -r -d '' f; do is_video "$f" && echo x; done | wc -l)"
echo "剩余文件总数:       $(find "$BASE" -type f | wc -l)"
echo "仍带 @ 的文件:      $(find "$BASE" -type f -name '*@*' | wc -l)"
if [ "$MODE" != "run" ]; then
  echo "=========== DRY-RUN：未做任何修改 ==========="
fi
