# 引用与第三方组件说明

> 撷引（xieyin）本身只做**本地文件读取 + 元数据抓取 + nfo/图片写入**。
> 本项目**不打包**任何第三方源码库；下面列出的是运行时可能用到的系统组件与数据来源。
>
> 如果你认为这里的署名、链接或许可证标注有误，欢迎提 Issue 更正。

---

## 一、运行时依赖（不随 FPK 打包）

| 组件 | 作用 | 是否必需 | 许可证 | 来源 |
|---|---|---|---|---|
| **Python 3** | 服务端、任务调度、脚本运行时 | 必需（fnOS 自带） | PSF-2.0 | <https://www.python.org/> |
| **FFmpeg / ffprobe** | 读取视频编码、分辨率、时长，写入 `<fileinfo>` | 可选 | LGPL-2.1-or-later / GPL（取决于发行版构建） | <https://ffmpeg.org/> |
| **Pillow** | 把封面裁剪成 379×538 的 poster | 可选 | HPND | <https://python-pillow.org/> |
| **curl** | 抓取 TMDB 页面与图片 | 可选 | curl license | <https://curl.se/> |
| **bash / coreutils / findutils** | 体检与清理脚本 | 必需（fnOS 自带） | GPL 系 | GNU 项目 |

> 缺失 `ffprobe` / `Pillow` / `curl` 时，对应步骤会自动跳过，不影响 nfo 主体与基本刮削。

## 二、数据来源

| 来源 | 用途 | 说明 |
|---|---|---|
| **JavBus** <https://www.javbus.com/> | 番号 AV 的标题、演员、类别、发行日期、封面 | 本项目与 JavBus 无隶属或合作关系；请遵守其服务条款与访问频率限制 |
| **TMDB** <https://www.themoviedb.org/> | 电影元数据、海报、背景图 | 本项目与 TMDB 无隶属或合作关系；TMDB 名称与商标归其所有者 |
| **DoH（doh.pub）** | 解析 `www.javbus.com` 的真实 A 记录 | 仅做 DNS over HTTPS 查询，不发送本地媒体信息 |
| **飞牛 fnOS** <https://www.fnnas.com/> | 运行平台、应用中心、桌面入口 | 本项目为第三方应用，与飞牛官方无隶属关系 |

## 三、代码来源

- `jav_scrape.py`、`tmdb_scrape.py`、`check_meta.sh`、`clean_qh.sh` 的早期 Docker 版本
  由本项目作者编写；本仓库是在其基础上的 FPK 化、网页化与安全加固版本。
- `app_server.py`、`net_test.sh`、`www/index.html`、`tools/*` 为本项目新增。
- 图标由 `tools/make_icons.py` 使用 Pillow 生成，不使用第三方图标素材。
- 本项目**没有**复制或打包 TVBox / drpy / QuickJS 等项目的代码。

### 早期 Docker 版的额外依赖（不随 FPK 打包）

| 组件 | 用途 | 许可证 | 来源 |
|---|---|---|---|
| **python:3.11-slim-bookworm** | Docker 基础镜像 | PSF-2.0 + Debian 各组件许可证 | <https://hub.docker.com/_/python> |
| **requests** | 早期 `qbit_watch.py` 轮询 qBittorrent WebAPI | Apache-2.0 | <https://requests.readthedocs.io/> |
| **qBittorrent WebUI API** | 下载完成后触发刮削的接口 | 官方文档 | <https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)> |

> 以上组件只出现在早期 Docker 版，当前 FPK 不包含 `qbit_watch.py`，也不依赖 `requests`。

## 四、公开协议、格式与社区致谢

本项目没有复制第三方源码；以下**公开资料、协议与同类项目**为实现提供了参考，
也方便读者进一步查阅。本项目与这些项目/组织**没有隶属或合作关系**。

### 4.1 元数据格式

| 资料 | 用途 | 链接 |
|---|---|---|
| **Kodi NFO / artwork 约定** | nfo 字段、`poster.jpg` / `fanart.jpg` / `thumb.jpg` 命名 | <https://kodi.wiki/view/NFO_files/Movies> |
| **Emby** | 媒体库读取 nfo / 图片的兼容行为 | <https://emby.media/> |
| **Jellyfin** | 媒体库读取 nfo / 图片的兼容行为 | <https://jellyfin.org/> |

> 本项目写出的 nfo 字段与图片命名，遵循 Kodi / Emby / Jellyfin 社区长期形成的公开约定。

### 4.2 网络与协议

| 资料 | 用途 | 链接 |
|---|---|---|
| **DNS over HTTPS（RFC 8484）** | 解析 JavBus 真实 IP | <https://datatracker.ietf.org/doc/html/rfc8484> |
| **doh.pub** | DoH 服务 | <https://doh.pub/> |
| **Cloudflare 1.1.1.1 DoH** | DoH 协议与实现参考 | <https://developers.cloudflare.com/1.1.1.1/> |
| **qBittorrent WebUI API** | 早期下载完成监听 | <https://github.com/qbittorrent/qBittorrent/wiki/WebUI-API-(qBittorrent-4.1)> |

### 4.3 同类开源项目（仅生态参考）

以下项目属于「JavBus / TMDB 刮削 + NFO」公开生态，提供了实现思路与文档参考。
**本项目未复制其代码**，列出仅为方便读者比较与进一步查阅：

| 项目 | 说明 | 链接 |
|---|---|---|
| **JavScraper / Emby.Plugins.JavScraper** | Emby / Jellyfin 的 JAV 刮削插件 | <https://github.com/JavScraper/Emby.Plugins.JavScraper> |
| **mdcz** | JAV 元数据刮削与媒体库整理 | <https://github.com/ShotHeadman/mdcz> |
| **JavBoss** | 本地 JAV / 视频刮削、管理、播放 | <https://github.com/Solr159/JavBoss> |
| **OpenAver** | 开源 JAV 元数据刮削桌面应用 | <https://github.com/slive777/OpenAver> |
| **MetaTube** | 通用元数据刮削服务 | <https://github.com/metatube-community> |

### 4.4 代码来源核查

- 现有源码中没有第三方版权头、来源注释或整段照搬的标记。
- 对若干独特函数名与中文注释做过公开代码检索，未发现匹配。
- GitHub 代码搜索无法穷尽；如果你认为某段实现与你的项目高度相似，
  请提 Issue 并附上链接，我们会核实并补充署名，或改写实现。

## 五、许可证文本


- 本项目：GPL-3.0-or-later，见 [LICENSE](LICENSE)
- Python：<https://docs.python.org/3/license.html>
- FFmpeg：<https://ffmpeg.org/legal.html>
- Pillow：<https://python-pillow.org/about/#license>
- curl：<https://curl.se/docs/copyright.html>
- TMDB：<https://www.themoviedb.org/documentation/api/terms-of-use>

---

**再次感谢所有把工具、协议和踩坑经验公开出来的作者。**
