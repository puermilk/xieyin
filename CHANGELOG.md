# 更新日志

## 1.2.0 — 2026-11-06

新增**自动刮削**：文件增加了就知道去刮。

- **定时扫描**：每 N 分钟扫媒体目录，找出「有视频但缺 nfo」的文件夹自动刮
- **qBittorrent 联动**：轮询下载完成事件，只刮刚下完的目录（已处理过的不重复刮）
- 两个触发源共用同一套「找待刮目录」判定，网页里可分别开关
- **稳定期**（默认 10 分钟）：文件 mtime 早于 N 分钟才刮，避免误刮下到一半的文件
- **PT 保护**（网页可开关，默认避开）：`@GROUP` / `*.torrent` 目录跳过
- 新增 `tools/test_auto_watch.py` 22 个用例
- 修复：无扩展名的 PT 目录名（如 `Movie.2024.1080p@GROUP`）漏判，
  Python 与 `clean_qh.sh` 同源修复

## 1.1.0 — 2026-11-06

支持 ARM 架构，x86 与 arm 分别打包、分别发布。

- 打包脚本 `tools/build_fpk.py` 重构为纯函数：`render_manifest` / `output_name` /
  `normalize_platform`，并新增 9 个单元测试
- `--platform x86|arm|all` 参数；默认一次产出两份 FPK
- manifest 的 `platform` 字段按目标架构写入 `x86` 或 `arm`
- 产物命名：`xieyin-<版本>-x86.fpk` / `xieyin-<版本>-arm.fpk`
- app.tgz 多架构共用（纯 Python 标准库 + shell，无原生二进制）
- 新增 `tools/test_scrape_parsing.py`：钉住 `code_of` 与 `parse_folder` 行为（11 个用例）
- 移除 `jav_scrape.py` 中未使用的 `struct` 导入

## 1.0.0 — 2026-11-06

首个公开版本。

- 中文名 **撷引**（xié yǐn），含义「撷取信息，引入媒体库」；包名 / 代号 `xieyin`
- 番号 AV 刮削（JavBus）：标题、演员、类别、发行日期、封面、fanart、thumb
- 电影刮削（TMDB）：按片名 + 年份匹配；支持手动搜索词、TMDB ID
- 媒体体检：视频 / nfo / 图片统计，nfo XML 校验
- 共享清理：每个文件夹只保留最大视频；可选去域名前缀
- 网页操作、实时日志、目录浏览、任务历史、停止任务
- 清理默认开启 **PT 保护**：检测到 `@GROUP` 做种文件 / `*.torrent` 直接中止
- 刮削只新增 `.nfo` 与图片，不改视频文件名，不覆盖已有图片
- 每次刮削任务启动时重新探测并验证 JavBus 真实 IP
- 纯 Python 标准库，不依赖 Docker
- 开源：GPL-3.0-or-later，附版权、引用与免责声明
