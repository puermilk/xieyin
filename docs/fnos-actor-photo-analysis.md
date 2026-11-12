# 飞牛影视演员照片写入机制分析

> 状态：分析文档，基于本机飞牛影视 `trim-media` 实际数据库 / API / 日志验证。
> 结论先行：**当前 xieyin 没有写演员照片**；飞牛实际有两条可用路径：
> 1. **NFO `<actor><thumb>` + 本地图片**（扫描时由飞牛复制进人物头像缓存）
> 2. **内部 API：`image/temp/upload` (`image_type=profile`) + `person/saveEditDetail`**（立即生效）
>
> 项目里现有的 `/api/media/meta/update` + `actors[].photo_url` 不是 stock 飞牛接口，本机实测 404，不能作为主方案。

---

## 1. 飞牛的数据模型

### 1.1 相关表

`/usr/local/apps/@appdata/trim.media/database/trimmedia.db`

```sql
person
  guid           TEXT PRIMARY KEY
  trim_id        TEXT        -- LOCAL_PERSON_<guid> 或 tp<tmdb_id>
  tmdb_id        INTEGER
  lan            TEXT
  name           TEXT
  original_name  TEXT
  biography      TEXT
  profile_path   TEXT        -- 演员头像，形如 /44/04/upload_profile_xxx.webp
  create_time    INTEGER
  update_time    INTEGER

item_person
  item_guid      TEXT        -- -> item.guid
  person_guid    TEXT        -- -> person.guid
  role           TEXT
  job            TEXT        -- Actor / Director / Screenplay / Producer
  order          INTEGER
  department     TEXT

field_lock
  entity_type    TEXT        -- item / person
  entity_id      TEXT        -- guid
  field_name     TEXT        -- profile_path / name / biography ...
  is_locked      INTEGER
```

关键点：

- `person.profile_path` 不是 URL，也不是本地绝对路径，而是**飞牛图片缓存里的 hash 相对路径**。
- 演员和条目的关系在 `item_person`；同一个人可以关联多个条目。
- `profile_path_locked` 不存在 `person` 表，而是存在 `field_lock`：
  - `entity_type='person'`
  - `entity_id=<person_guid>`
  - `field_name='profile_path'`
  - `is_locked=0/1`
- 图片实际位置：
  - 临时：`/vol1/@appmeta/trim.media/cache/img/temp/...`
  - 正式：`/vol1/@appmeta/trim.media/img/<xx>/<yy>/<file>.webp`

### 1.2 本机现状

- `person` 表数量较多，其中只有少量 `profile_path` 非空。
- 示例演员存在多条重复 person 记录：
  - 1 条 `trim_id=LOCAL_PERSON_...`，`profile_path=''`，关联大量条目；
  - 若干条 `trim_id=tp<tmdb_id>`（TMDB 人物），`profile_path` 有值，但只关联少量条目。
- `ABC-123` 的 `item_person` 指向的是 `LOCAL_PERSON_bbb8...`，所以页面上没有演员照片。

原因：

- 飞牛的 TMDB 云刮削会创建 `tp<tmdb_id>` 人物，带 TMDB 头像；
- 本地 NFO 刮削会创建 `LOCAL_PERSON_<guid>` 人物，默认 `profile_path=''`；
- 条目如果走本地 NFO，就绑定到本地人物，不会自动合并到 TMDB 人物。

---

## 2. 路径 A：NFO `<actor><thumb>`（文件式，推荐用于新入库）

### 2.1 飞牛 NFO 解析器支持

从 `trim-media` 二进制符号和 XML tag 可以确认：

```text
xml:"actor"
xml:"name"
xml:"role"
xml:"thumb"
```

`<order>` 是 Kodi/Emby 的标准字段，但本机 `trim-media` 的 XML tag 列表里没有看到 `xml:"order"`；写进去无害，但飞牛是否按它排序未确认。

相关函数：

```text
trim-media/internal/service/scraper/builder.ProcessNfoImage
trim-media/internal/service/scraper/builder.processNfoImageAsync
trim-media/internal/service/scraper/builder.downloadNfoUrlImageSync
trim-media/internal/service/image.GetPersonProfileFilePath
```

日志字符串：

```text
person image relative path not found: %s
person image absolute path not found: %s
person image absolute path %s is outside library roots %v
Copied person image from %s to %s for person %s
Failed to copy person image from %s to %s for person %s
```

结论：NFO 里的 `<actor><thumb>` 支持：

- 相对路径（相对于 NFO / 视频所在目录）
- 绝对路径（必须在媒体库根目录内）
- URL（会下载）

飞牛会把图片复制到人物头像缓存，并更新 `person.profile_path`。

### 2.2 推荐 NFO 写法（新条目必须用绝对路径）

本机实测结论：

- **绝对路径**：新条目首次扫描就能写入 `person.profile_path`。
- **相对路径**：新条目首次扫描会失败，日志为
  `cannot find item <guid> for relative path resolution`；因为 NFO 图片处理时 item 还没落库。

所以新入库推荐写**媒体库内的绝对路径**：

```xml
<actor>
  <name><![CDATA[某演员]]></name>
  <role></role>
  <type>Actor</type>
  <order>1</order>
  <thumb>/vol1/1000/Media/ABC-123/ABC-123 某演员/ABC-123-actor-某演员.jpg</thumb>
</actor>
```

目录结构：

```text
ABC-123 某演员/
├── ABC-123.mp4
├── ABC-123.nfo
├── ABC-123-actor-某演员.jpg
├── ABC-123-poster.jpg
└── ...
```

如果条目已经在飞牛里存在，再触发 rescrap，可以尝试相对路径：

```xml
<thumb>ABC-123-actor-某演员.jpg</thumb>
```

但为了稳定，建议统一用绝对路径；飞牛会把图片复制进自己的缓存，后续 NFO 路径变化不影响已写入的 `profile_path`。

### 2.3 优点

- 不需要管理员 token，不需要内部 API 签名。
- 图片跟着媒体库走，换机/重建库也能恢复。
- 与现有 `build_nfo` 改动最小：只需加 `<thumb>` 和 `<order>`。
- 飞牛扫描 NFO 时会把图片复制进人物头像缓存，写入 `person.profile_path`。

### 2.4 注意点

- 绝对路径必须在媒体库根目录内，否则日志会报 `person image absolute path ... outside library roots`。
- 相对路径在新条目首次扫描时不可用；只适合“已有 item 再 rescrap”。
- 文件名要清洗 `\ / : * ? " < > |`，避免 Windows/SMB 问题。
- NFO 改了之后需要触发重扫/重刮；已有条目可能不会自动重读 NFO。
  - 可调用 `POST /v/api/v1/mdb/scan/:guid`；
  - 或 `mbd_rebuild.py`；
  - 或 touch NFO / 强制 rescrap。
- 如果 `profile_path_locked=1`，飞牛会保留原头像，不覆盖。
- `<order>` 是 Kodi/Emby 标准字段，但本机 `trim-media` 的 XML tag 列表里没有看到 `xml:"order"`；写进去无害，但飞牛是否按它排序未确认。

### 2.5 本机实测结果（NFO 路线）

测试目录：

```text
/vol1/1000/Media/ABC-123/ActorTest/
├── TEST-001.mp4                    # 7.4MB，绕过 skip_filesize
├── TEST-001.nfo
└── TEST-001-actor.jpg
```

NFO：

```xml
<actor>
  <name>测试演员</name>
  <role></role>
  <type>Actor</type>
  <order>1</order>
  <thumb>TEST-001-actor.jpg</thumb>
</actor>
```

测试 1：相对路径

```text
结果：
  item 创建成功
  person 创建成功
  person.profile_path = ''
日志：
  cannot find item <guid> for relative path resolution
```

测试 2：绝对路径

```xml
<thumb>/vol1/1000/Media/ABC-123/ActorTest/TEST-001-actor.jpg</thumb>
```

```text
结果：
  item 创建成功
  person 创建成功
  person.profile_path = /df/02/local_4410d7a8e98b4c3fb533844375baf6a2.jpg
  item/getEditDetail credits 里同步出现 profile_path
  GET /v/api/v1/person/<guid> 返回 profile
文件：
  /vol1/@appmeta/trim.media/img/df/02/local_4410d7a8e98b4c3fb533844375baf6a2.jpg
  766 bytes
```

结论：**新条目首次扫描必须用绝对路径；相对路径会因 item 尚未落库而失败。**

---

## 3. 路径 B：内部 API（已验证，适合修已有库 / 立即生效）

这是本机实测跑通的路径。

### 3.1 鉴权

复用 `mbd_rebuild.py` 里的：

- `_admin_tokens()`：从 `trimactivity.db` 的 `user_token` 取管理员 token；
- `_authx()`：生成 `nonce/timestamp/sign`；
- `Client`：封装请求。

### 3.2 读取人物编辑详情

```http
POST /v/api/v1/person/getEditDetail
Content-Type: application/json

{"guid": "<person_guid>"}
```

返回：

```json
{
  "code": 0,
  "data": {
    "guid": "<person_guid>",
    "trim_id": "LOCAL_PERSON_<person_guid>",
    "is_official": false,
    "name": "某演员",
    "name_locked": false,
    "biography": "",
    "biography_locked": false,
    "profile_path": "",
    "profile_path_locked": false
  }
}
```

### 3.3 上传头像

```http
POST /v/api/v1/image/temp/upload
Content-Type: multipart/form-data

image_type=profile
file=<jpeg/png/webp>
```

实测有效 `image_type`：

```text
poster    ✅
profile   ✅
person    ❌ Invalid Params
actor     ❌ Invalid Params
avatar    ❌ Invalid Params
```

返回：

```json
{"code": 0, "data": {"hash_path": "/44/04/upload_profile_<md5>.webp"}}
```

### 3.4 保存人物头像

把 `getEditDetail` 的 `data` 原样回传，只改 `profile_path`：

```http
POST /v/api/v1/person/saveEditDetail
Content-Type: application/json

{
  "guid": "<person_guid>",
  "trim_id": "LOCAL_PERSON_<person_guid>",
  "is_official": false,
  "name": "某演员",
  "name_locked": false,
  "biography": "",
  "biography_locked": false,
  "profile_path": "/44/04/upload_profile_8ff296ba04838463daa17970773fd3c0.webp",
  "profile_path_locked": true
}
```

返回：

```json
{"code": 0, "msg": "", "data": null}
```

飞牛后台会自动：

```text
image upload detected format: jpeg
processImageField: moved image from .../cache/img/temp/upload_profile_xxx.webp
  to .../img/44/04/upload_profile_xxx.webp
```

并更新 `person.profile_path`。

### 3.5 本机实测结果

对 `某演员` 的 `LOCAL_PERSON_bbb8...`（关联 80 个条目）执行：

```text
BEFORE:
  profile_path = ""

UPLOAD:
  /44/04/upload_profile_8ff296ba04838463daa17970773fd3c0.webp

AFTER:
  profile_path = "/44/04/upload_profile_8ff296ba04838463daa17970773fd3c0.webp"

FILE:
  /vol1/@appmeta/trim.media/img/44/04/upload_profile_8ff296ba04838463daa17970773fd3c0.webp
  49442 bytes

TEMP:
  已删除
```

`item/getEditDetail` 的 `credits` 也同步变成：

```json
{
  "person_guid": "<person_guid>",
  "name": "某演员",
  "job": "Actor",
  "role": "",
  "order": 2,
  "profile_path": "/44/04/upload_profile_8ff296ba04838463daa17970773fd3c0.webp"
}
```

`GET /v/api/v1/person/<guid>` 返回：

```json
{
  "guid": "<person_guid>",
  "name": "某演员",
  "trim_id": "LOCAL_PERSON_<person_guid>",
  "is_official": false,
  "profile": "/44/04/upload_profile_8ff296ba04838463daa17970773fd3c0.webp",
  "is_favorite": 0
}
```

说明接口路径和字段都正确。

### 3.6 批量算法

```text
1. 读取条目：item/getEditDetail
2. 取 credits 里 job=Actor 的 person_guid
3. 按 person_guid 去重（同一个人只传一次）
4. 下载演员照片
5. image/temp/upload (image_type=profile) -> hash_path
6. person/getEditDetail -> 原样回传 + profile_path=hash_path
7. person/saveEditDetail
8. 校验 person/<guid> 的 profile 字段
```

同一个 `person_guid` 关联的所有条目自动生效，不需要逐条改 `item_person`。

### 3.7 锁定策略

- `profile_path_locked=true`：飞牛云刮削/刷新不会覆盖你写的头像。
- `profile_path_locked=false`：后续 TMDB 刮削可能替换。
- 对人工/本地刮削头像，建议 `true`；对自动同步 TMDB 头像，可 `false`。

---

## 4. 路径 C：直接改数据库（不推荐）

可以直接：

```sql
UPDATE person SET profile_path='/44/04/xxx.webp', update_time=... WHERE guid=...;
```

并把图片放到 `/vol1/@appmeta/trim.media/img/44/04/xxx.webp`。

不推荐原因：

- 绕过飞牛的图片校验、格式转换、缩略图生成；
- 绕过 `field_lock`；
- 图片尺寸/格式不对时 UI 可能不显示；
- 后续迁移/备份可能不一致；
- 飞牛版本升级可能改表结构。

只作为最后兜底。

---

## 5. 路径 D：`/api/media/meta/update` + `photo_url`（当前项目方案）

`fnos_meta.py` 的文档写着：

```json
"actors": [{"name": "...", "photo_url": "..."}]
```

但本机实测：

| 地址 | 结果 |
|---|---|
| `http://127.0.0.1:5666/api/media/meta/update` | 404 |
| `http://127.0.0.1:8005/api/media/meta/update` | 返回 SPA HTML |
| `http://127.0.0.1:22668/api/media/meta/update` | 404 |

说明 stock 飞牛没有这个接口。它可能是：

- 用户自己写的 bridge / 中间层；
- 或者设计文档里的假想接口。

如果这个 bridge 不存在，`fnos_meta.push_meta` 实际上推不进去，更不会写演员照片。

**建议**：要么实现这个 bridge，要么改用 stock 的 `/v/api/v1/person/*` 与 `/v/api/v1/image/temp/upload`。

---

## 6. 演员照片从哪里来

### 6.1 JavBus

演员页/详情页的 star 区块通常有：

```html
<img src="/imgs/actress/<id>.jpg" title="某演员">
```

可以解析 `<id>` 下载头像。

### 6.2 JavDB

演员详情页可能有头像；需要在 `javdb580.com` 上验证具体选择器。

### 6.3 Caribbeancom

演员页有头像；但当前 `scrape_caribbeancom` 只解析主演名，没有取头像。

### 6.4 TMDB

`person_archive` / TMDB person API 有 `profile_path`，可拼：

```text
https://image.tmdb.org/t/p/original<profile_path>
```

电影流程最稳。

### 6.5 兜底

没有头像时：

- 不写 `<thumb>`；
- 或用首字母占位图（不推荐，容易被当成真实头像）。

---

## 7. 推荐落地方式

### 7.1 新入库：NFO 方案优先

1. 刮削时下载演员头像到目标目录 `actor/<safe_name>.jpg`。
2. `build_nfo` 输出：

```xml
<actor>
  <name>...</name>
  <role>...</role>
  <type>Actor</type>
  <order>1</order>
  <thumb>actor/<safe_name>.jpg</thumb>
</actor>
```

3. 入库后触发飞牛重扫。
4. 飞牛自动写入 `person.profile_path`。

优点：零 token、可迁移、和现有 NFO 流程一致。

### 7.2 已有库修复：API 方案

新增 `scripts/fnos_person.py`：

```python
def get_edit(person_guid) -> dict
def upload_profile(image_path) -> str
def save_edit(data: dict, profile_path: str, locked=True) -> None
def sync_item_actors(item_guid, actor_images: dict[str, str]) -> list[str]
```

复用 `mbd_rebuild` 的 `_admin_tokens/_authx/Client`。

流程：

```text
item/getEditDetail
  -> credits job=Actor
  -> 下载/匹配演员图
  -> upload_profile
  -> save_edit(profile_path, locked=True)
  -> 校验 person/<guid>
```

适合：

- 已经入库但没有头像的老条目；
- 不想等媒体库重扫；
- 需要立即生效。

### 7.3 两者结合

- 新条目：NFO `<thumb>`，随库走。
- 老条目/批量修复：API 直接写。
- 同一演员先查 `person.profile_path` 和图片 md5，避免重复上传。

---

## 8. 风险与检查清单

- [ ] `person_guid` 是否是该条目真正关联的本地人物，而不是同名 TMDB 人物？
- [ ] `profile_path` 必须写 hash 路径，不要写 URL/绝对路径。
- [ ] `person/saveEditDetail` 必须回传 `getEditDetail` 的完整 data，不能只传 `profile_path`。
- [ ] `image_type` 必须是 `profile`，不是 `person/actor/avatar`。
- [ ] `profile_path_locked` 存在 `field_lock`，不是 `person` 表。
- [ ] 上传后要 `saveEditDetail`，否则临时图不会被移动，5 分钟后可能被清理。
- [ ] NFO `<thumb>` 新条目必须用媒体库内的绝对路径；相对路径会因 item 尚未落库而失败。
- [ ] NFO 改了要触发重扫；已有条目可能不会自动重读。
- [ ] `profile_path_locked=1` 时飞牛会保留原头像。
- [ ] 同名人物有多条记录，批量时按 `item_person` 实际关联的 guid 操作。
- [ ] 图片格式用 jpeg/png/webp；飞牛会转 webp。
- [ ] 批量上传要限速，避免飞牛图片服务压力。
- [ ] 操作前备份 `person` 相关行（至少记录 guid/profile_path）。

---

## 9. 代码触点

| 文件 | 现状 | 需要改动 |
|---|---|---|
| `scripts/jav_scrape.py` | `build_nfo` 只写 actor name/type | 加 `<thumb>`、`<order>`；解析 star 头像 URL |
| `scripts/race_scrape.py` | Caribbeancom 只解析主演名 | 解析演员头像 |
| `scripts/tmdb_scrape.py` | 有 TMDB 演员数据 | 下载 `profile_path` 头像，写 `<thumb>` |
| `scripts/fnos_meta.py` | 支持 `photo_url` 但接口不存在 | 改为 stock API，或保留给 bridge |
| `scripts/mbd_rebuild.py` | 有 authx/Client | 复用到 person 同步 |
| 新增 `scripts/fnos_person.py` | — | 人物头像上传/保存/批量同步 |
| `scripts/img_cache_sync.py` | 只同步 poster | 可扩展 person profile |
| `docs/ingest-pipeline-design.md` | 入库设计 | 增加演员头像步骤 |

---

## 10. 结论

- **飞牛写演员照片的正确入口是 `person.profile_path`**，不是 item 的 `posters`。
- **NFO `<actor><thumb>` 适合新入库**：文件式、零 token、可迁移；但**新条目首次扫描必须用媒体库内的绝对路径**。
- **相对路径在首次扫描会失败**，日志 `cannot find item <guid> for relative path resolution`；只适合已有 item 再 rescrap。
- **内部 API 已经实测可用**：`image_type=profile` + `person/saveEditDetail`，适合修已有库。
- **当前 `meta/update` 方案在本机不可用**，不能依赖 `photo_url`。
- 建议：新条目用“绝对路径 NFO `<thumb>`”，已有库用 API 批量补；两条路都已在当前机器验证。
