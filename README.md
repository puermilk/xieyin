# 撷引 · xieyin

<img src="src/xieyin/ICON_256.PNG" width="96" align="right" alt="撷引图标">

> **撷取信息，引入媒体库。**
>
> 飞牛 fnOS 上的媒体元数据刮削工具：番号 AV（JavBus）、电影（TMDB）、媒体体检、共享清理。
> 网页操作，实时日志，纯 Python 标准库，不依赖 Docker。

[![License: GPL-3.0-or-later](https://img.shields.io/badge/license-GPL--3.0--or--later-blue.svg)](LICENSE)
[![Python](https://img.shields.io/badge/python-3.9%2B-3776AB.svg)](https://www.python.org/)
[![fnOS](https://img.shields.io/badge/fnOS-1.2.0%2B-00A9A5.svg)](https://www.fnnas.com/)

---

## 名称释义：为什么叫「撷引」

| 字 | 拼音 | 含义 | 在插件里的对应 |
|---|---|---|---|
| **撷** | xié | 摘取、抓取、采集 | 刮取番号 / 电影元数据、海报、背景图、演员表 |
| **引** | yǐn | 拉取、索引、导入 | 把 `.nfo` / 图片写进媒体目录，导入飞牛影视媒体库 |

> **撷**：摘取、抓取（刮取元数据、海报）；**引**：拉取、索引、导入到飞牛影视。
> 合起来就是：**撷取信息，引入媒体库** —— 正好对应刮削插件的完整动作。

- 中文名：**撷引**
- 拼音：**xié yǐn**
- 包名 / 代号：**xieyin**
- 英文副标题：*Scrape the metadata, bring it into your library.*

「撷」比「刮」更雅，动作也更准确：不是粗暴地刮，而是有选择地**摘取**需要的信息；
「引」则点明了最终目的：**引入**飞牛影视媒体库，而不是把数据留在某个孤立目录里。

---

## 功能

| 功能 | 说明 | 数据源 |
|---|---|---|
| 番号 AV 刮削 | 从文件夹名解析番号，写 nfo、海报、fanart、thumb、演员与类别 | JavBus |
| 电影刮削 | 从文件夹名/文件名解析片名与年份，匹配 TMDB；支持手动搜索词、TMDB ID | TMDB |
| **自动刮削** | 定时扫描 / qBittorrent 联动，新增文件自动刮 | JavBus |
| 媒体体检 | 统计视频 / nfo / 图片，校验 nfo XML | 本地文件 |
| 共享清理 | 每个文件夹只保留最大视频；可选去域名前缀 | 本地文件 |
| 网络自检 | DoH / JavBus / TMDB / 图床 / ffprobe / 目录 / 代理 | — |

- 网页里选目录、点按钮即可运行，不用 SSH
- 任务日志实时回传，可停止、可下载
- 每次刮削任务启动时**重新解析并验证 JavBus 真实 IP**，不写死一个 IP 一直用
- **不含任何影视内容，也不内置任何片源**

---

## 自动刮削：文件增加了就自动刮

两个触发源，都在网页「自动刮削」卡片里开关：

| 触发源 | 原理 | 适合 |
|---|---|---|
| **定时扫描** | 每 N 分钟扫媒体目录，找出「有视频但缺 nfo」的文件夹 | 通用兜底，不依赖任何下载器 |
| **qBittorrent 联动** | 轮询下载完成事件，只刮刚下完的目录 | 精准，不会误刮下到一半的文件 |

两个保险（可配）：

- **稳定期**（默认 10 分钟）：文件 mtime 早于 N 分钟才刮，避免误刮正在下载的文件
- **PT 保护**（网页可开关，默认避开）：`@GROUP` / `*.torrent` 目录跳过，不写任何文件

> 自动刮削同样遵守「只新增、不覆盖已有图片、不改视频文件名」。
> qBittorrent 联动只刮「刚下载完成」的目录，已处理过的（按 hash + 完成时间）不会重复刮。

---


## PT 安全：默认不破坏做种文件

这是本项目的重点设计，默认配置就是安全配置。

### 清理

- 扫描到 `xxx@GROUP`、`xxx@GROUP-10bit` 这类做种文件/目录，或目录里有 `*.torrent`
- **直接中止，不改名、不删除**，任务状态显示「已阻止」
- 只对 `489155.com@ABC-123.mp4` 这种**域名前缀**形式做去前缀
- `Movie.2024.1080p@GROUP.mkv` 会被识别为 PT 发布名，绝不改名
- 默认只删除多余**视频**；nfo / 图片 / 字幕等附属文件保留
- 只有手动关闭「PT 保护」并输入 `CLEAN` 才会清理 PT 目录——不建议

### 刮削

- 只新增 `.nfo` 和图片
- 不改视频文件名、不删视频
- **不覆盖已有图片**：PT 种子里如果有同名 `poster.jpg` / `fanart.jpg`，保持原样
- 已有 nfo 默认跳过；只有勾选「强制重刮」才重写 nfo

> PT 保护的目的是避免误操作，不是绝对保证。清理前仍建议先「预览」，重要数据请自行备份。

---

## 安装

### 方式一：下载 FPK

从 [Releases](https://github.com/puermilk/xieyin/releases) 下载对应架构的 FPK：

| NAS 处理器 | 安装包 |
|---|---|
| Intel / AMD（x86_64） | `xieyin-<版本>-x86.fpk` |
| ARM（aarch64 / arm64） | `xieyin-<版本>-arm.fpk` |

> 两个包的代码完全相同（纯 Python 标准库 + shell，无原生二进制），只是 manifest 的
> `platform` 字段不同。选错架构会被 fnOS 拒绝安装。

然后：

```bash
appcenter-cli install-fpk /path/to/xieyin-1.2.0-x86.fpk   # 或 -arm.fpk
```

### 方式二：应用中心手动安装

应用中心 → 手动安装 → 选择对应架构的 `xieyin-*.fpk`。

安装完成后桌面出现 **撷引** 图标，点开是网页，默认端口 **22668**。

### 环境要求

- 飞牛 fnOS 1.2.0+
- fnOS 自带 `python3`（只用标准库）
- 可选：`ffprobe`（写入视频参数）、`Pillow`（裁剪海报）、`curl`（TMDB 刮削）
  缺失时会自动跳过对应步骤，不影响 nfo 与基本刮削

---

## 使用

1. 打开「撷引」
2. 选择媒体目录（可浏览选择，也可手动输入）
3. 番号刮削 / 电影刮削 → 先「预览」确认清单 → 再「开始」
4. 体检：检查视频是否都有 nfo、图片，以及 nfo XML 是否合法
5. 清理：先「预览清理」，确认后再「执行清理」

### 番号刮削

要求文件夹名能解析出番号，例如：

```
ABC-123-UC
XYZ-456-C
ABC-789_FHD_CH
```

按演员名组织的目录（视频文件名里才有番号）不会被番号刮削处理，这是原脚本的行为。

### 电影刮削

按「片名 + 年份」匹配 TMDB，不需要番号。能识别的命名示例：

```
示例电影.Example.Movie.2024.2160p.WEB-DL
[中文名].Example.Movie.2024.1080p
中文名.2024.1080p.BluRay
Example.Movie.2024.1080p.WEBRip
```

认不出来时：

1. 改文件夹名为 `片名.年份`
2. 在「搜索词」里手动填片名
3. 在「TMDB ID」里填 TMDB 页面地址中的数字（最准）

### JavBus IP 是怎么找的

每次刮削任务启动时都会重新找：

1. 先问 DoH（doh.pub）要 `www.javbus.com` 的 A 记录
2. 把 DoH 结果和内置 `KNOWN_IPS` 去重后逐个验证
   （固定 IP + 正确 SNI + 证书 + HTTP 200 + 页面含 JavBus 标记）
3. 只把验证通过的 IP 放进可用列表
4. 请求中遇到 `302 driver-verify` 会换 IP；轮完一圈再问一次 DoH 补充
5. 全部验证失败时提示「硬闯 KNOWN_IPS」；仍不行就切代理

同一个任务内的多个文件夹复用本次验证结果，不会每个文件夹都重新问 DoH。

---

## 从源码构建

```bash
git clone https://github.com/puermilk/xieyin.git
cd xieyin

# 行为测试（不联网）
python3 tools/test_ip_validation.py
python3 tools/test_scrape_parsing.py
python3 tools/test_build_fpk.py
python3 tools/test_auto_watch.py

# 打包：默认一次产出 x86 与 arm 两份（需要 python3 + tar/gzip）
python3 tools/build_fpk.py 1.2.0

# 也可以只打一个架构
python3 tools/build_fpk.py 1.2.0 --platform x86
python3 tools/build_fpk.py 1.2.0 --platform arm
```

产出：

```
dist/xieyin-1.2.0-x86.fpk
dist/xieyin-1.2.0-x86.fpk.sha256
dist/xieyin-1.2.0-arm.fpk
dist/xieyin-1.2.0-arm.fpk.sha256
```

> app.tgz 多架构共用（代码里没有原生二进制），只有 manifest 的 `platform` 行不同。

需要改图标时（依赖 Pillow）：

```bash
python3 tools/make_icons.py
python3 tools/build_fpk.py 1.2.0
```

### 项目结构

```
src/xieyin/
├── manifest                 # FPK 元信息
├── ICON.PNG / ICON_256.PNG  # 应用图标
├── cmd/                     # 应用中心生命周期脚本
├── config/                  # FPK 权限配置
└── app/
    ├── xieyin/              # 运行载荷
    │   ├── app_server.py    # 网页服务 + 任务调度
    │   ├── scripts/         # 刮削 / 体检 / 清理 / 网络自检 / auto_watch.py
    │   ├── www/index.html   # 单页界面
    │   └── *.sh             # 安装 / 单元 / 卸载
    └── ui/                  # 桌面入口
tools/
├── build_fpk.py
├── make_icons.py
├── test_ip_validation.py
├── test_scrape_parsing.py
├── test_build_fpk.py
└── test_auto_watch.py
```

---

## 引用与致谢

完整清单见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。本项目**不打包**任何第三方源码库，运行时依赖系统组件：

| 项目 | 作用 | 许可证 | 链接 |
|---|---|---|---|
| Python | 运行时与标准库 | PSF-2.0 | <https://www.python.org/> |
| FFmpeg / ffprobe | 读取视频参数（可选） | LGPL-2.1+ / GPL 视构建而定 | <https://ffmpeg.org/> |
| Pillow | 裁剪海报（可选） | HPND | <https://python-pillow.org/> |
| curl | TMDB 抓取（可选） | curl | <https://curl.se/> |
| JavBus | 番号元数据来源 | 站点条款 | <https://www.javbus.com/> |
| TMDB | 电影元数据与图片 | TMDB API 条款 | <https://www.themoviedb.org/> |
| 飞牛 fnOS | 运行平台 | 官方条款 | <https://www.fnnas.com/> |

特别说明：

- 本项目与 JavBus、TMDB、飞牛 fnOS 均**无隶属或合作关系**，相关名称与商标归各自所有者。
- 本项目不包含、不托管、不分发任何影视内容或片源。
- 原始 Docker 版脚本由本项目作者编写，已合并、改写为当前的 FPK 版本。
- nfo 字段与图片命名遵循 **Kodi / Emby / Jellyfin** 的公开约定；DoH 使用 **RFC 8484**。
- 同类开源项目（JavScraper、mdcz、JavBoss、OpenAver、MetaTube）提供了生态与思路参考，
  本项目**未复制其代码**；完整列表与说明见 [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md)。
- 如果认为署名、引用或许可证标注有误，欢迎提 Issue 更正。早前版本的代码来源核查结论也写在
  [THIRD_PARTY_NOTICES.md](THIRD_PARTY_NOTICES.md) 的「代码来源核查」一节。


---

## 版权与许可

```
Copyright (C) 2026 xieyin contributors
```

本项目以 **GNU General Public License v3.0 or later（GPL-3.0-or-later）** 发布。
完整文本见 [LICENSE](LICENSE)，版权与使用边界见 [COPYRIGHT.md](COPYRIGHT.md)。

源码文件带有 SPDX 标识：

```
SPDX-License-Identifier: GPL-3.0-or-later
```

---

## 免责声明

使用前请阅读 [DISCLAIMER.md](DISCLAIMER.md)。要点：

- 本工具**只做元数据抓取与本地文件整理**，不提供、不托管、不分发任何影视内容。
- 你访问的第三方站点、抓取的内容、以及由此产生的法律责任，由你自行承担。
- 请遵守当地法律法规、JavBus / TMDB 的服务条款，以及你所在 PT 站点的规则。
- 成人内容仅限 18 岁以上成年人；请勿向未成年人传播。
- 软件按「现状」提供，不提供任何明示或暗示担保；清理操作不可恢复，请先备份。

---

## 更新日志

见 [CHANGELOG.md](CHANGELOG.md)。

## 贡献

- 发现问题请提 Issue，附上 fnOS 版本、任务日志（注意打码路径与个人信息）
- 欢迎 PR；提交前请先跑 `python3 tools/test_ip_validation.py`
- 请不要在 Issue / PR 中粘贴真实番号、演员名、PT 站点地址或个人信息

如果这个项目对你有帮助，欢迎点一个 Star。
