# 撷引自动入库流水线设计（v1 草案）

> 状态：设计文档，暂不改代码。
> 目标读者：后续实现 `ingest.py`、`app_server.py`、`auto_watch.py` 的开发者。
> 已确认的产品决策：
> - 源视频处理：同时支持 **move（移动并清空源目录）** 和 **hardlink（同盘硬链接，保留源目录做种）**。
> - 保存路径结构：**`{code} {title}` 平铺**。
> - STRM：**指向远端 URL / 网盘**；NFO 和图片放本地。
> - 先出设计文档，不改代码。

---

## 1. 目标与非目标

### 1.1 目标

后台常驻运行，周期扫描源目录，自动完成：

1. 发现新视频（稳定、非临时文件）。
2. 按番号刮削元数据。
3. 校验 NFO / 封面完整性。
4. 把目录规范命名为 `{code} {title}`。
5. 按配置 **move** 或 **hardlink** 到保存路径。
6. 在保存路径生成 `.strm`（指向远端 URL / 网盘）。
7. 把 NFO、封面、字幕、`extrafanart` 写到保存路径。
8. 校验成功后按规则清理源目录垃圾；move 模式下最终清空源目录。
9. 全程幂等、可重试、可 dry-run、可审计。

### 1.2 非目标

- 不做下载器，不替代 qBittorrent。
- 不做转码、压制、字幕翻译。
- 不做网盘上传客户端；假设目标路径已由网盘挂载/同步工具落盘。
- 不做媒体库全量重建；只触发必要刷新。
- P1 不自动删除做种中的视频（hardlink 模式保留源视频）。

---

## 2. 名词与路径

| 名称 | 含义 |
|---|---|
| `source_root` | 下载/待整理根目录，如 `/fs/1000/ftp/CCTV` |
| `target_root` | 媒体库保存根目录，如 `/vol1/1000/Media/AV` |
| `unit` | 一个刮削单元：一个视频文件 + 所在目录 |
| `code` | 番号，如 `JUL-002`、`011820-001` |
| `stem` | 目标目录内视频/STRM/NFO/图片共用的主名 |
| `move` | 移动视频到目标，清理源目录 |
| `hardlink` | 同盘硬链接视频到目标，保留源目录做种 |
| `strm` | 文本指针文件，内容为远端 URL |

### 2.1 目标目录

已确认平铺结构：

```text
{target_root}/{code} {title}/
    {stem}.strm              # 或 {stem}.mp4（本地视频模式）
    {stem}.nfo
    {stem}-poster.jpg
    {stem}-fanart.jpg
    {stem}-thumb.jpg
    poster.jpg
    folder.jpg
    fanart.jpg
    thumb.jpg
    {stem}.srt / .ass ...    # 原字幕
    extrafanart/             # 如存在
```

`stem` 默认 `{code}`，可配置为 `{code} {title}`。默认用 `{code}` 的原因：

- 目标目录名已经包含标题，文件名再重复会很长；
- SMB / Windows / 网盘对长文件名和特殊字符更敏感；
- 媒体库按同 stem 匹配 NFO/图片，`JUL-002.nfo` 最稳。

---

## 3. 现状复盘：必须先修的 P0

自动入库依赖刮削产物完整。当前代码有三类问题，必须先修，否则入库会把残缺结果搬进媒体库。

### 3.1 封面套图不完整

#### AV 流程（`race_scrape.py:write_artifacts`）

正常写入 5 个文件：

```text
<stem>-poster.jpg
<stem>-fanart.jpg
<stem>-thumb.jpg
poster.jpg      # 单视频目录
folder.jpg      # 单视频目录
```

缺失原因：

| 现象 | 原因 |
|---|---|
| 只有 NFO、没有图 | `race_one` 先写 NFO，再下图；`fetch_cover` 失败只记 `E001`，任务仍返回 OK |
| 只有 3 个前缀图 | 多视频目录 `nvid > 1`，代码故意不写别名 |
| 只有 3 个前缀图 | 1.7.4 之前刮的旧目录，当时没有别名 |
| 只补 1~2 个 | `--image-policy keep` 下 `allowed()` 逐文件判断，已有合法 JPEG 跳过 |
| 新旧图混用 | 同一 `allowed()` 策略导致 `-poster` 是旧图、`-fanart` 是新图 |

最关键的漏洞：`auto_watch.py` 只判断“有没有 NFO”。NFO 写成功、封面失败后，后续扫描永远认为已完成。

#### 电影流程（`tmdb_scrape.py:329-345`）

```python
if not poster.exists() or args.force:
    poster.write_bytes(raw_poster)
    folder = ...
    if not folder.exists() or args.force:
        shutil.copyfile(poster, folder)

if not fanart.exists() or args.force:
    fanart.write_bytes(raw_backdrop)
    thumb = ...
    if not thumb.exists() or args.force:
        shutil.copyfile(fanart, thumb)
```

嵌套条件导致：

- `poster.jpg` 已存在、`folder.jpg` 缺失 → 永远不补 `folder.jpg`
- `fanart.jpg` 已存在、`thumb.jpg` 缺失 → 永远不补 `thumb.jpg`
- poster 成功、backdrop 失败 → 只有 `poster.jpg + folder.jpg`
- poster 失败、backdrop 成功 → 只有 `fanart.jpg + thumb.jpg`

#### 实际库抽样

`/fs/1000/ftp/CCTV` 下 284 个含视频目录：

```text
274 个：3 个前缀图 + poster.jpg + folder.jpg
  9 个：只有 3 个前缀图（多视频目录 / 旧版本）
  1 个：只有 poster/folder/fanart/thumb（电影目录）
  0 个：前缀图只有 1~2 个（当前样本）
```

### 3.2 演员解析错误

`race_scrape.py:104-107` 的 Caribbeancom 解析：

```python
actors = [re.sub(r"<[^>]+>", "", x).strip()
          for x in re.findall(r'class="[^"]*actress[^"]*"[^>]*>([^<]{2,20})', html)]
```

它匹配的是页面里的 `class="player-rec-actress"`，即**相关推荐卡片**的演员，不是当前影片主演。

证据：`011820-001.nfo` 写成：

```text
某演员
某演员 某演员
百多えみり 江波りゅう ルナ 須藤なこ
```

后两条是推荐位演员串。正确主演在：

```html
<span class="spec-title">出演</span>
<span class="spec-content">
  <a itemprop="actor" ...><span itemprop="name">某演员</span></a>
</span>
```

### 3.3 其他 P0 问题

- `scrape_caribbeancom` 写 `d["release"]`，但 `build_nfo` 读 `d["releasedate"]`，日期型番号 NFO 发行日期为空。
- `build_nfo` 写全部 actor，但没有 `<order>`；部分媒体库主演排序不稳定。
- `download_cover` 允许返回半截 JPEG；写盘前需要 `jpeg_ok()` 校验。
- `img_cache_sync` 只扫描 `<stem>-poster.jpg`；poster 缺失时封面不会同步进飞牛。

### 3.4 P0 修复规格

新增/改造一个共享函数，AV 和电影都用：

```python
def write_cover_set(folder, stem, cover_bytes, backdrop_bytes, policy, single_video):
    """
    从同一张原图一次性生成完整套图：
      <stem>-poster.jpg  crop
      <stem>-fanart.jpg  raw
      <stem>-thumb.jpg   raw
    单视频目录再写：
      poster.jpg / folder.jpg / fanart.jpg / thumb.jpg
    全部先写 .tmp，再 os.replace() 原子落盘。
    """
```

规则：

1. 写入前 `jpeg_ok()`；不完整则重下，不把半截图当成功。
2. 一套图要么全成，要么全不成；不允许只写 1~2 个。
3. 别名独立判断，不再嵌套。
4. 已存在但套图不完整时，补齐整套；`keep` 只表示“不换源”，不表示“允许残缺”。
5. 单视频目标目录固定写 7 个文件（3 前缀 + 4 别名）。
6. NFO `<art>` 只引用真实存在的文件，但写入器保证存在。

演员修复：

- Caribbeancom 只解析 `出演` 块中的 `itemprop="actor"` / `itemprop="name"`。
- 所有源统一 `normalize_actors()`：去空白、去重、去空值；分隔符只认 `,，、/`，不按空格拆（避免拆坏欧美姓名）。
- NFO 增加 `<order>1..N</order>`，全部演员都是 `<type>Actor</type>`。
- `d["release"]` 同步写 `d["releasedate"]`。

---

## 4. 总体架构

新增编排层 `ingest.py`，不要让 `race_scrape.py` 继续承担移动/清理职责。

```text
┌─────────────────────────────────────────────────────────────┐
│ app_server.py / auto_watch.py                               │
│  定时扫描 source_root，或 qBittorrent 完成事件              │
└───────────────────────┬─────────────────────────────────────┘
                        │ trigger(kind="ingest", dirs=[...])
                        ▼
┌─────────────────────────────────────────────────────────────┐
│ ingest.py                                                   │
│                                                             │
│  discover → stable → scrape → validate → plan               │
│      → materialize(move|hardlink) → strm → sidecars         │
│      → push(fnos) → clean → done                            │
│                                                             │
│  state: data/ingest.db (SQLite)                             │
└───────────────────────┬─────────────────────────────────────┘
                        │
        ┌───────────────┼────────────────┐
        ▼               ▼                ▼
  race_scrape.py   fnos_meta.py     clean rules
  刮削+NFO/图      推送飞牛          源目录清理
```

### 4.1 为什么用 SQLite

- 逐条事务，崩溃后可恢复。
- 并发扫描/手动操作时不会写坏 JSON。
- 可以按 `status`、`code`、`src_path` 查询。
- 记录重试次数、错误、目标路径、manifest。

建议表：

```sql
CREATE TABLE IF NOT EXISTS ingest (
    id              INTEGER PRIMARY KEY AUTOINCREMENT,
    src_path        TEXT NOT NULL,
    code            TEXT NOT NULL,
    title           TEXT,
    actors          TEXT,              -- JSON array
    mode            TEXT NOT NULL,     -- move | hardlink
    status          TEXT NOT NULL,
    target_path     TEXT,
    stem            TEXT,
    video_size      INTEGER,
    video_hash      TEXT,
    attempts        INTEGER DEFAULT 0,
    last_error      TEXT,
    manifest        TEXT,              -- JSON: 已移动/已清理文件
    created_at      TEXT,
    updated_at      TEXT,
    UNIQUE(src_path, code)
);

CREATE INDEX IF NOT EXISTS idx_ingest_status ON ingest(status);
CREATE INDEX IF NOT EXISTS idx_ingest_code ON ingest(code);
```

### 4.2 状态机

```text
discovered
  → stable
  → scraping
  → scraped
  → validated
  → planned
  → materializing
  → materialized
  → strm_written
  → pushed (optional)
  → cleaned
  → done

任一阶段失败 → failed
需要人工确认 → needs_review
跳过 → skipped
```

恢复规则（启动时）：

| 旧状态 | 恢复动作 |
|---|---|
| `scraping` | 重新刮削（幂等） |
| `materializing` | 检查目标是否存在且完整：完整→继续；不完整→删临时目录重做 |
| `materialized` | 检查源/目标，继续 STRM/清理 |
| `cleaned` | 标 `done` |
| `done` | 跳过 |
| `failed` | 达到重试上限→`needs_review`，否则重试 |

---

## 5. 配置

建议放在 `config.json` 顶层：

```json
{
  "ingest": {
    "enabled": false,
    "source_root": "/fs/1000/ftp/CCTV",
    "target_root": "/vol1/1000/Media/AV",
    "mode": "move",
    "layout": "{code} {title}",
    "stem": "{code}",
    "settle_minutes": 10,
    "stable_checks": 2,
    "max_attempts": 3,
    "dry_run": true,

    "strm": {
      "enabled": true,
      "url_template": "https://alist.example.com/d/AV/{relative_path}",
      "encode": true
    },

    "clean": {
      "enabled": true,
      "delete_samples": true,
      "delete_torrent": false,
      "delete_empty_dirs": true,
      "trash_dir": ""
    },

    "qbit": {
      "enabled": false,
      "mode": "wait"          // wait | pause | remove
    }
  }
}
```

字段说明：

| 字段 | 说明 |
|---|---|
| `mode` | `move` 或 `hardlink`；hardlink 跨盘自动回退/报错 |
| `layout` | 固定 `{code} {title}`；保留模板是为了以后扩展 |
| `stem` | 目标内文件主名，默认 `{code}` |
| `stable_checks` | 连续 N 次扫描大小/mtime 不变才算稳定 |
| `dry_run` | 只打印计划，不移动/不删除 |
| `strm.url_template` | 必须包含 `{relative_path}` 或 `{filename}`；见 §8 |
| `clean.trash_dir` | 非空时删除改为移动到回收目录，便于回滚 |
| `qbit.mode` | `wait` 等做种结束；`pause` 暂停后移动；`remove` 删除种子后移动 |

---

## 6. 发现与稳定性

### 6.1 扫描

遍历 `source_root`：

- 跳过 `#recycle`、`.` 开头目录、`@eaDir`、`tmp`。
- 一个含视频文件的叶子目录 = 一个候选 unit。
- 根目录直接放视频也作为 unit。
- 视频扩展名以 `media_ext.MEDIA_EXT` 为准（含 `.strm`）。
- 跳过 sample/trailer：`[-_. ](sample|trailer)$`。

### 6.2 番号

优先级：

1. 文件夹名 `code_of(folder.name)`
2. 视频文件名 `code_of(video.stem)`

识别不到 → `skipped`，日志记录，不移动。

### 6.3 稳定性

同时满足：

- 视频 `mtime` 早于 `settle_minutes`。
- 连续 `stable_checks` 次扫描，文件大小和 mtime 不变。
- 目录内无 `.part`、`.!qB`、`.crdownload`、`.tmp`、`.aria2`。
- 可选：qBittorrent 中该路径的种子 `amount_left == 0`。

### 6.4 去重

- `code` 已存在 `done` 记录 → 跳过。
- 目标目录已存在同 `code`：
  - 同视频大小/hash → 合并 sidecar，标 `done`。
  - 内容不同 → `needs_review` 或加 ` (2)`，默认 `needs_review`（避免误合并）。
- `--force` 才允许重刮/重搬。

---

## 7. 刮削与校验

### 7.1 刮削

调用现有 `race_scrape.py`：

```bash
python3 race_scrape.py \
  --dirs <unit.dir> \
  --sink inplace \
  --image-policy replace \
  --no-probe false
```

要点：

- `sink=inplace`，先写源目录；校验通过后再搬。
- 新 unit 用 `--image-policy replace`，保证封面套图完整。
- 已有 NFO 但封面不完整 → `--force-images`，不加 `--force`。
- 刮削失败重试 `max_attempts` 次，指数退避。
- 日志按 unit 保存，便于排查。
- 未来中文剧情翻译可通过 `XIEYIN_PLOT_ZH=1` 接入；本设计不展开。

### 7.2 校验

必须全部通过：

1. NFO 存在且 XML 可解析。
2. `<num>` 与 `code` 一致。
3. `<plot>` 非空（允许回退标题）。
4. 至少 1 个 `<actor>`；为 0 时 `needs_review`（不阻塞搬运，但标记）。
5. 封面套图完整：
   - `<stem>-poster.jpg`、`<stem>-fanart.jpg`、`<stem>-thumb.jpg` 存在；
   - 每个 > 10KB 且 `jpeg_ok()`；
   - 单视频目录 4 个别名存在且与对应前缀图内容一致。
6. 视频可读，`ffprobe` 时长 > 0（可选）。
7. 目标路径可写、剩余空间足够。

校验失败：

- 封面不完整 → 只重下图片，最多 3 次。
- NFO 不完整 → 重刮。
- 仍失败 → `failed`，源目录不动。

---

## 8. 命名、目标路径与 STRM

### 8.1 命名

```text
title = NFO <originaltitle> 或 <title> 去掉 code 和演员名
name  = "{code} {title}"
```

清洗规则：

- 替换 `\ / : * ? " < > |` 为 `_`。
- 去掉控制字符、首尾空格/点。
- 合并连续空白。
- 限制 120 个字符（UTF-8 字节不超过 180，兼容 SMB/Windows）。
- 避开 Windows 保留名：`CON PRN AUX NUL COM1..9 LPT1..9`。
- 标题为空 → 只用 `{code}`。

### 8.2 目标路径

```text
target_root/{code} {title}/
```

`target_root` 可以是：

- 本地媒体库目录；
- 网盘挂载目录（如 Alist/CloudDrive 挂载点）；
- 同步盘目录。

### 8.3 STRM

已确认 STRM 指向远端 URL / 网盘。

```text
{target_root}/{code} {title}/{stem}.strm
```

内容示例：

```text
https://alist.example.com/d/AV/JUL-002%20%E3%80%8C...%E3%80%8D/JUL-002.mp4
```

模板占位符：

| 占位符 | 含义 |
|---|---|
| `{relative_path}` | 相对 `target_root` 的路径，如 `JUL-002 .../JUL-002.mp4` |
| `{filename}` | 视频文件名 |
| `{code}` | 番号 |
| `{title}` | 标题 |
| `{target_path}` | 目标绝对路径 |

规则：

- `strm.enabled=true` 时 `url_template` 必填，否则配置错误。
- 每个路径段单独 URL encode；保留 `/`。
- 文件末尾加换行，UTF-8 无 BOM。
- `.strm` 的 stem 必须与 NFO/图片一致。
- 如果 `target_root` 本身就是媒体服务器可访问的 URL 根，`url_template` 可用 `{relative_path}`。
- 如果 STRM 指向远端，但视频被 `move` 到本地目标，需要确认目标目录会同步到远端；否则 URL 会 404。

### 8.4 视频/STRM 与 NFO 的 stem

目标目录内统一：

```text
JUL-002.strm
JUL-002.nfo
JUL-002-poster.jpg
JUL-002-fanart.jpg
JUL-002-thumb.jpg
```

如果 `mode=move` 且视频保留在目标：

```text
JUL-002.mp4
JUL-002.nfo
...
```

不要出现 `JUL-002.mp4` + `JUL-002.strm` 同时存在（媒体库会重复识别）。配置 `strm.enabled` 决定二选一。

---

## 9. 搬运（move / hardlink）

### 9.1 公共前置

- 目标目录不存在则创建；存在则走冲突策略。
- 检查目标所在文件系统剩余空间。
- 检查源视频未被占用（可选 `lsof`）。
- 写临时目录/临时文件，成功后原子替换。
- 记录 manifest：源文件 → 目标文件、大小、hash。

### 9.2 move

```text
source/ABC-123 原目录/
  ABC-123.mp4
  ABC-123.nfo
  ABC-123-poster.jpg ...
        │
        ▼
target/ABC-123 标题/
  ABC-123.mp4
  ABC-123.nfo
  ABC-123-poster.jpg ...
```

- 同一文件系统：`os.rename(src_dir, dst_dir)`，最快且原子。
- 跨文件系统：复制到 `dst_dir.tmp-<pid>`，`fsync`，校验大小/hash，`os.replace`，再删除源目录。
- 目标已存在：逐文件合并；同名且内容不同 → `needs_review`。
- 成功后源目录应只剩垃圾，交给清理阶段。

### 9.3 hardlink

- 仅同一文件系统；`os.stat(src).st_dev == os.stat(target_root).st_dev`。
- 视频用 `os.link(src, dst)`；sidecar（NFO/图片/字幕）用复制，避免后续重写 NFO 影响源。
- 校验 `st_ino` 一致、大小一致。
- 源目录保留视频，供 qBittorrent 做种；垃圾清理只删非视频。
- 跨盘时：
  - 默认 `fallback_copy=true` → 复制视频到目标，源保留；
  - 或 `needs_review`，不自动处理。

### 9.4 冲突

| 情况 | 处理 |
|---|---|
| 目标目录不存在 | 正常创建 |
| 目标存在，同 code 同大小/hash | 合并 sidecar，标 done |
| 目标存在，同 code 不同内容 | `needs_review`，不覆盖 |
| 目标存在，文件名冲突但内容相同 | 跳过该文件 |
| 目标存在，文件名冲突且内容不同 | `needs_review` |

---

## 10. 目标 sidecar 写入

搬运后，所有 sidecar 使用目标 `stem` 重命名：

```text
{stem}.nfo
{stem}-poster.jpg
{stem}-fanart.jpg
{stem}-thumb.jpg
poster.jpg
folder.jpg
fanart.jpg
thumb.jpg
```

规则：

- 目标目录只有一个视频/STRM，所以别名始终写。
- 封面套图必须来自同一张原图，内容一致。
- `poster.jpg` 与 `{stem}-poster.jpg` 内容一致；`folder.jpg` 同。
- `fanart.jpg` 与 `{stem}-fanart.jpg` 一致；`thumb.jpg` 同。
- 字幕保留原文件名；如果媒体库要求同 stem，可配置重命名为 `{stem}.srt`。
- `extrafanart/` 整个目录复制/移动。
- NFO `<art>` 由 `build_nfo` 根据真实文件生成；搬运后如果 stem 变化，需要重新生成 NFO 或替换 `<art>` 路径。
- NFO 内 `<num>`、`<title>` 不改；只改文件名。

### 10.1 飞牛推送

当前 `race_scrape` 的 `push` 发生在搬运前，推送的是源路径。入库流水线应：

1. `race_scrape --sink inplace`（不 push）。
2. 搬运到目标。
3. 用目标视频/STRM 路径调用 `fnos_meta.build_payload` + `push_meta`。
4. 推送成功才标 `pushed`；失败可重试，不阻塞本地文件。

---

## 11. 源目录清理

### 11.1 触发条件

只有同时满足才清理：

- 目标 NFO 可解析。
- 目标封面 7 件套完整。
- 目标视频/STRM 存在且大小正确（move/hardlink）。
- manifest 已落库。
- 当前 unit 不在 qBittorrent 活跃做种中（hardlink 可选）。

### 11.2 move 模式

- 目标校验通过后，源目录里剩下的都是垃圾/旧 sidecar。
- 按规则删除，最后 `rmdir` 空目录。
- `source_root` 本身不删。
- 默认先 dry-run 打印清单；`trash_dir` 非空时移动到回收目录而不是永久删除。

### 11.3 hardlink 模式

- 源视频必须保留（做种）。
- 删除：
  - sample/trailer 视频；
  - 未被引用的图片；
  - `.txt .url .htm .html .DS_Store Thumbs.db`；
  - 空目录。
- 保留：
  - 主视频；
  - `.torrent`（默认保留，`clean.delete_torrent=true` 才删）；
  - 字幕（可选保留）；
  - NFO/封面可保留（不影响做种，方便排查）。

### 11.4 垃圾判定

删除白名单（可配置）：

```text
.txt .url .lnk .htm .html .torrent .DS_Store Thumbs.db
*.tmp *.part *.crdownload
sample/trailer 视频
未被 NFO/封面引用的 jpg/png
```

绝不删除：

```text
主视频（除非 move 已成功）
.strm
.nfo
封面 7 件套
.srt .ass .ssa .sup .sub .idx .vtt
extrafanart/
```

### 11.5 回滚

- `trash_dir` 模式下保留相对路径，可手工恢复。
- 删除清单写入 `manifest`，包含原路径、大小、时间。
- 失败时不动源目录。

---

## 12. 失败处理与重试

| 错误类型 | 处理 |
|---|---|
| 网络/图床超时 | 重试 3 次，指数退避 |
| 番号查不到 | `needs_review`，不移动 |
| NFO 不完整 | 重刮 |
| 封面不完整 | 只补图，最多 3 次 |
| 磁盘满/权限 | `failed`，告警，不删源 |
| 目标冲突 | `needs_review` |
| qBittorrent 做种中且 mode=move | 按 `qbit.mode` 等待/暂停/删除 |
| 搬运中断 | 启动时恢复，删临时目录重做 |

重试上限 `max_attempts=3`；超过后 `needs_review`，避免无限重试。

日志：

- 应用日志：任务级。
- 每 unit 一行 JSONL：`data/ingest.log`。
- 保留最近 N 天。

---

## 13. 与现有模块集成

### 13.1 `auto_watch.py`

新增触发源：

```python
if conf.get("ingest", {}).get("enabled"):
    self.trigger("ingest", "自动入库 · %d 个" % len(targets),
                 {"dirs": targets, "mode": conf["ingest"]["mode"]})
else:
    self.trigger("jav", ...)  # 旧行为
```

`pick_targets` 不再只判断 NFO：

```python
def needs_work(names):
    if not has_video(names):
        return False
    if not has_nfo(names):
        return True
    if not has_complete_cover_set(path, names):
        return True   # NFO 有但图不完整
    return False
```

### 13.2 `app_server.py`

- `DEFAULT_CONF` 增加 `ingest`。
- `build_job(kind="ingest")` 构造 `ingest.py` 命令。
- `/api/config` 保存 ingest 配置。
- `/api/status` 返回 ingest 状态。
- 任务完成后不再直接 `img_cache_sync`，由 ingest 在目标 root 上调用。

### 13.3 `img_cache_sync.py`

- 目标 root 而不是源 root。
- 扫描 `{stem}-poster.jpg`。
- 如果 poster 缺失，先补齐再同步。

### 13.4 `fnos_meta.py`

- push 的 `file_path` 用目标视频/STRM 路径。
- 目标目录只有一个条目，避免同目录多视频错配。

### 13.5 `mbd_rebuild.py`

- 批量入库完成后可选触发一次媒体库重扫。
- 不建议每条 unit 触发，避免频繁删库重建。

---

## 14. 安全与边界

1. 路径必须 `resolve()` 后校验在 `source_root` / `target_root` 内，防止 `../`。
2. 不跟随符号链接删除；删除前 `lstat`。
3. 不使用 root 运行；至少对源只读、对目标可写。
4. 搬运前检查磁盘空间：`video_size * 1.1`。
5. 原子写：临时文件 + `os.replace`。
6. 单一 ingest 进程：`data/ingest.lock`，避免定时任务和手动任务并发。
7. 源目录只读挂载时，move 模式必须报错，不尝试删除。
8. 网盘挂载断连时，目标写入失败 → `failed`，源不动。
9. 不在 `#recycle` 内操作。
10. 所有删除默认 dry-run；首次上线先跑 1~2 天只记录。

---

## 15. 测试计划

### 15.1 单元测试

- `sanitize_name()`：特殊字符、长标题、保留名、空标题。
- `plan_target()`：`{code} {title}`、冲突、大小写。
- `cover_set_ok()`：缺 1 个、半截图、别名不一致。
- `normalize_actors()`：推荐位串、多演员、重复、空。
- `render_strm()`：URL encode、模板占位符、中文路径。
- 状态机：每阶段失败/恢复。

### 15.2 集成测试

临时目录 + 假视频 + mock 刮削：

1. move 同盘：源目录清空，目标 7 件套 + STRM。
2. move 跨盘：复制校验后删除源。
3. hardlink：`st_ino` 一致，源视频保留。
4. 封面失败：只补图，不重复刮 NFO。
5. 目标冲突：不覆盖，标 `needs_review`。
6. 断点恢复：`materializing` 中断后重启。
7. 清理 dry-run：清单正确，文件不动。
8. STRM URL 中文/空格编码。

### 15.3 真实验证

- 先对一个样本目录 dry-run。
- 再跑一个 move、一个 hardlink。
- 检查 Emby/Jellyfin/飞牛识别、封面、主演、播放。
- 确认源目录/做种状态。

---

## 16. 实施计划

### P0：先修刮削产物（独立于入库）

1. `scrape_caribbeancom` 主演解析 + `releasedate`。
2. `build_nfo` 演员 `<order>`。
3. `tmdb_scrape.write_movie_files` 嵌套条件修复。
4. AV 补 `fanart.jpg/thumb.jpg` 别名。
5. `write_cover_set()` 原子写 + `jpeg_ok`。
6. `auto_watch` 完整性判断。

### P1：入库骨架

1. `ingest.py`：扫描、状态库、move/hardlink、目标命名。
2. 配置 + CLI + dry-run。
3. 目标 sidecar 重命名 + 飞牛推送目标路径。

### P2：STRM + 清理

1. `render_strm()` + URL 模板。
2. 源目录清理规则 + trash。
3. qBittorrent 做种检测/暂停/删除钩子。

### P3：UI + 运维

1. 网页“自动入库”配置卡。
2. 状态/日志/失败重试界面。
3. 批量完成后媒体库刷新。
4. 监控告警（可选 webhook）。

---

## 17. 待确认项

以下不影响设计主体，但实现前需要定：

1. `source_root` / `target_root` 的实际路径。
2. `strm.url_template` 的实际前缀（Alist/WebDAV/Emby 地址）。
3. hardlink 跨盘时的策略：`fallback_copy` 还是 `needs_review`。
4. `clean.delete_torrent` 默认值。
5. 是否保留源目录的 NFO/封面（hardlink 模式）。
6. 目标目录是否允许一个目录多个视频；本设计默认一目录一视频。
7. 是否需要把字幕重命名为 `{stem}.srt`。
8. 飞牛推送在 move 前还是 move 后；本设计建议 move 后。

---

## 18. 代码触点清单

| 文件 | 现状 | 需要改动 |
|---|---|---|
| `scripts/race_scrape.py` | 写 NFO/图、Caribbeancom 解析 | 主演解析、`releasedate`、封面套图 |
| `scripts/jav_scrape.py` | `build_nfo` | actor `<order>`、封面 helper |
| `scripts/tmdb_scrape.py` | 电影写图 | 去掉嵌套条件、统一封面 helper |
| `scripts/auto_watch.py` | 只判断 NFO | 增加封面完整性判断、ingest 触发 |
| `scripts/img_cache_sync.py` | 只扫 poster | 目标 root、套图校验 |
| `scripts/fnos_meta.py` | 推送源路径 | 目标路径推送 |
| `app_server.py` | 配置/任务 | ingest 配置、任务类型、状态 |
| `www/index.html` | 设置界面 | 自动入库配置卡 |
| 新增 `scripts/ingest.py` | — | 编排、状态机、搬运、STRM、清理 |
| 新增 `docs/ingest-pipeline-design.md` | 本文 | — |

---

## 19. 附：目标目录最终形态

move 模式 + STRM：

```text
/vol1/1000/Media/AV/
└── JUL-002 「私が教えてあげる…」
    ├── JUL-002.strm
    ├── JUL-002.nfo
    ├── JUL-002-poster.jpg
    ├── JUL-002-fanart.jpg
    ├── JUL-002-thumb.jpg
    ├── poster.jpg
    ├── folder.jpg
    ├── fanart.jpg
    ├── thumb.jpg
    └── extrafanart/
```

hardlink 模式 + STRM（源目录保留视频）：

```text
/fs/1000/ftp/CCTV/JUL-002 原目录/
├── JUL-002.mp4          # 原文件，继续做种
└── （垃圾已清理）

/vol1/1000/Media/AV/JUL-002 「私が教えてあげる…」/
├── JUL-002.strm         # 指向远端 URL
├── JUL-002.nfo
├── JUL-002-poster.jpg
└── ...
```

> hardlink 只适合同一文件系统；目标在网盘挂载点时不能硬链接，必须走 move/copy。
