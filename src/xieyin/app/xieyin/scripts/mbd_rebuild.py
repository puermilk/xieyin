#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""刮削完成后重建飞牛影视媒体库，让本地 NFO / 封面立即生效。

流程（对刮削 root 所属的媒体库）：
  1. 查配置：GET    /v/api/v1/mdb/list  -> 按 dir_list 找出覆盖 root 的库
             GET    /v/api/v1/mdb/:guid -> 读取该库完整配置
  2. 备份：把配置写到 <data>/mdb_backups/<guid>-<ts>.json（不含任何 token）
  3. 删库：DELETE /v/api/v1/mdb/:guid
  4. 立即重建：PUT /v/api/v1/mdb/create（原配置字段，服务端重新生成 guid）
  5. 触发重扫：POST /v/api/v1/mdb/scan/:guid

零凭据：token 从飞牛影视自己的 user_token 表只读取得（免密），
按官方前端 authx 算法签名。任何一步失败都会自动用备份重建回滚，
绝不会留下「库被删掉、没建回来」的状态。
"""
from __future__ import annotations

import hashlib
import json
import os
import random
import re
import sqlite3
import time
import urllib.parse
import urllib.request

# 官方前端硬编码的签名常量（非用户凭据）
SALT = "NDzZTVxnRKP8Z0jXg1VAMonaG8akvh"
APIKEY = "16CCEB3D-AB42-077D-36A1-F355324E4237"
BASE = os.environ.get("XIEYIN_FNOS_API") or "http://127.0.0.1:8005"
API = "/v/api/v1"
TIMEOUT = 10

DB_CANDIDATES = [
    "/usr/local/apps/@appdata/trim.media/database",
    "/vol1/@appdata/trim.media/database",
    "/vol2/@appdata/trim.media/database",
]

# 建库字段白名单：details 里这些字段原样带回；guid/时间戳/排序由服务端重建
_CREATE_FIELDS = (
    "name", "category", "dir_list", "lan", "poster", "poster_type", "posters",
    "view_type", "skip_filesize", "auto_progress_thumb", "prefer_local_nfo",
    "subtitle_lan", "auto_scrap_subtitle",
)


def _md5(s: str) -> str:
    return hashlib.md5(s.encode()).hexdigest()


def _pu(params: dict) -> str:
    """复刻前端 pu()：key 排序 + URLSearchParams 编码 + '+' 换 %20"""
    parts = []
    for k in sorted(params or {}):
        v = params[k]
        if v is None:
            continue
        parts.append(urllib.parse.quote_plus(str(k)) + "=" +
                     urllib.parse.quote_plus(str(v)))
    return "&".join(parts).replace("+", "%20")


def _hu(a: str) -> str:
    """复刻前端 hu()：md5(decodeURIComponent(...))"""
    t = re.sub(r"%(?![0-9A-Fa-f]{2})", "%25", a or "")
    try:
        return _md5(urllib.parse.unquote(t))
    except Exception:
        return _md5(a or "")


def _authx(path: str, params: dict, is_get: bool) -> str:
    a = _pu(params) if is_get else json.dumps(
        params or {}, separators=(",", ":"), ensure_ascii=False)
    o = _hu(a) if is_get else _md5(a)
    nonce = str(random.randint(100000, 999999))
    ts = str(int(time.time() * 1000))
    sig = _md5("_".join([SALT, path, nonce, ts, o, APIKEY]))
    return "nonce=%s&timestamp=%s&sign=%s" % (nonce, ts, sig)


def _request(method: str, path: str, token: str,
             body=None, query=None) -> dict:
    """发一个带 authx 签名的请求；body 必须与签名输入逐字节一致。"""
    q = dict(query or {})
    if method == "GET":
        ax = _authx(path, q, True)
        data = None
    else:
        payload = body if body is not None else {}
        text = json.dumps(payload, separators=(",", ":"), ensure_ascii=False)
        ax = _authx(path, payload, False)
        data = text.encode("utf-8")
    url = BASE + path + ("?" + urllib.parse.urlencode(q) if q else "")
    req = urllib.request.Request(
        url, data=data, method=method,
        headers={"User-Agent": "Mozilla/5.0",
                 "Content-Type": "application/json",
                 "authx": ax, "Authorization": token or ""})
    try:
        with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
            return json.loads(r.read().decode("utf-8", "replace"))
    except Exception as e:
        return {"code": -1, "msg": repr(e)[:160]}


class Client:
    def __init__(self, token: str):
        self.token = token

    def list(self) -> list:
        r = _request("GET", API + "/mdb/list", self.token)
        if r.get("code") != 0:
            raise RuntimeError("mdb/list: %s %s" % (r.get("code"), r.get("msg")))
        d = r.get("data")
        libs = d.get("list") if isinstance(d, dict) else d
        return list(libs or [])

    def details(self, guid: str) -> dict:
        r = _request("GET", API + "/mdb/" + guid, self.token)
        if r.get("code") != 0:
            raise RuntimeError("mdb/details: %s %s" % (r.get("code"), r.get("msg")))
        return r.get("data") or {}

    def delete(self, guid: str) -> None:
        # 官方 DELETE 走 body={} 的签名（无 body 会 invalid sign）
        r = _request("DELETE", API + "/mdb/" + guid, self.token, body={})
        if r.get("code") != 0:
            raise RuntimeError("mdb/delete: %s %s" % (r.get("code"), r.get("msg")))

    def create(self, payload: dict) -> str:
        r = _request("PUT", API + "/mdb/create", self.token, body=payload)
        if r.get("code") != 0:
            raise RuntimeError("mdb/create: %s %s" % (r.get("code"), r.get("msg")))
        d = r.get("data") or {}
        return str(d.get("guid") or "") if isinstance(d, dict) else ""

    def get_edit_detail(self, item_guid: str) -> dict:
        """读取条目编辑详情（save 时原样回传，避免覆盖其它元数据字段）"""
        r = _request("POST", API + "/item/getEditDetail", self.token,
                     body={"item_guid": item_guid})
        if r.get("code") != 0:
            raise RuntimeError("getEditDetail: %s %s" % (r.get("code"), r.get("msg")))
        return dict(r.get("data") or {})

    def save_edit_detail(self, payload: dict) -> None:
        r = _request("POST", API + "/item/saveEditDetail", self.token,
                     body=payload)
        if r.get("code") != 0:
            raise RuntimeError("saveEditDetail: %s %s" % (r.get("code"), r.get("msg")))

    def upload_image(self, file_path: str, image_type: str = "poster") -> str:
        """官方图片上传 POST /image/temp/upload（multipart）。

        飞牛会自行校验、转 webp、生成多尺寸缩略图；saveEditDetail 引用后
        自动把文件从 cache/img/temp 移到 img/ 正式目录并删除旧封面。
        返回 hash_path（形如 /7c/10/upload_poster_<md5>.webp）。

        签名与 JSON 请求不同：multipart 的 body hash 是「原始字节的 md5」。
        """
        import uuid
        with open(file_path, "rb") as f:
            data = f.read()
        if not data:
            raise RuntimeError("upload: 图片文件为空")
        boundary = uuid.uuid4().hex
        body = ("--%s\r\nContent-Disposition: form-data; name=\"image_type\""
                "\r\n\r\n%s\r\n" % (boundary, image_type)).encode()
        fname = os.path.basename(str(file_path)) or "poster.jpg"
        body += ("--%s\r\nContent-Disposition: form-data; name=\"file\"; "
                 "filename=\"%s\"\r\nContent-Type: application/octet-stream"
                 "\r\n\r\n" % (boundary, fname)).encode()
        body += data + b"\r\n"
        body += ("--%s--\r\n" % boundary).encode()
        path = API + "/image/temp/upload"
        nonce = str(random.randint(100000, 999999))
        ts = str(int(time.time() * 1000))
        bh = hashlib.md5(body).hexdigest()
        ax = "nonce=%s&timestamp=%s&sign=%s" % (
            nonce, ts, _md5("_".join([SALT, path, nonce, ts, bh, APIKEY])))
        req = urllib.request.Request(
            BASE + path, data=body, method="POST",
            headers={"User-Agent": "Mozilla/5.0",
                     "Content-Type": "multipart/form-data; boundary=" + boundary,
                     "authx": ax, "Authorization": self.token or ""})
        try:
            with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
                d = json.loads(r.read().decode("utf-8", "replace"))
        except Exception as e:
            raise RuntimeError("upload: %r" % (e,)) from e
        if d.get("code") != 0:
            raise RuntimeError("upload: %s %s" % (d.get("code"), d.get("msg")))
        hp = str((d.get("data") or {}).get("hash_path") or "")
        if not hp.startswith("/"):
            raise RuntimeError("upload: 未返回 hash_path: %s" % str(d)[:120])
        return hp


    def scan(self, guid: str) -> None:
        r = _request("POST", API + "/mdb/scan/" + guid, self.token, body={})
        if r.get("code") != 0:
            raise RuntimeError("mdb/scan: %s %s" % (r.get("code"), r.get("msg")))


def _db_dir() -> str:
    for d in DB_CANDIDATES:
        if os.path.exists(os.path.join(d, "trimactivity.db")) and \
           os.path.exists(os.path.join(d, "trimmedia.db")):
            return d
    return ""


def _admin_tokens() -> list[str]:
    """只读飞牛影视自己的令牌库，取管理员的有效 token（不打印、不落盘）。"""
    d = _db_dir()
    if not d:
        return []
    try:
        a = sqlite3.connect("file:" + os.path.join(d, "trimactivity.db") +
                            "?mode=ro", uri=True, timeout=1)
        m = sqlite3.connect("file:" + os.path.join(d, "trimmedia.db") +
                            "?mode=ro", uri=True, timeout=1)
        admins = {r[0] for r in m.execute(
            "select guid from user where is_admin=1 and status=1")}
        rows = a.execute(
            "select token,user_guid from user_token where status=1 "
            "order by update_time desc").fetchall()
        a.close()
        m.close()
    except Exception:
        return []
    out = []
    for t, ug in rows:
        if ug in admins and re.fullmatch(r"[0-9a-fA-F]{32}", t or ""):
            out.append(t)
    return out


def _pick_client() -> "Client | None":
    for t in _admin_tokens():
        c = Client(t)
        try:
            c.list()          # 用只读请求验证 token 是否仍有效
            return c
        except Exception:
            continue
    return None


def _matches(root: str, dir_list) -> bool:
    """root 是否属于该库：库目录 == root，或库目录是 root 的父目录。"""
    r = os.path.normpath(root).rstrip("/")
    for item in (dir_list or []):
        d = os.path.normpath(str(item)).rstrip("/")
        if r == d or r.startswith(d + "/"):
            return True
    return False


def _create_payload(details: dict) -> dict:
    """details -> create 字段；include_adult 必须是 bool（数字会 Internal Error）"""
    p = {k: details[k] for k in _CREATE_FIELDS if k in details}
    p["include_adult"] = bool(details.get("include_adult"))
    return p


def _backup_path(data_dir: str, guid: str) -> str:
    d = os.path.join(data_dir, "mdb_backups")
    os.makedirs(d, exist_ok=True)
    return os.path.join(d, "%s-%s.json" % (guid, time.strftime("%Y%m%d-%H%M%S")))


def rebuild(root: str, data_dir: str = "", log=None) -> tuple[bool, list[str]]:
    """刮削完成后调用。返回 (是否全部成功, 日志行)。"""
    lines: list[str] = []

    def emit(s: str) -> None:
        lines.append(s)
        if log:
            try:
                log(s)
            except Exception:
                pass

    root = str(root or "").strip()
    if not root:
        emit("[媒体库] INFO 无刮削根目录，跳过重建")
        return True, lines
    c = _pick_client()
    if c is None:
        emit("[媒体库] INFO 未取得飞牛影视管理员令牌，跳过重建（不影响刮削结果）")
        return True, lines
    try:
        libs = c.list()
    except Exception as e:
        emit("[媒体库] WARN 读取媒体库列表失败，跳过重建：%s" % e)
        return True, lines
    targets = [l for l in libs if isinstance(l, dict) and l.get("guid")
               and _matches(root, l.get("dir_list"))]
    if not targets:
        emit("[媒体库] INFO 刮削目录未归属任何飞牛影视媒体库，跳过重建：%s" % root)
        return True, lines

    ok_all = True
    for lib in targets:
        guid = str(lib["guid"])
        name = str(lib.get("name") or guid)
        emit("[媒体库] INFO 命中媒体库「%s」(%s) dir_list=%s" %
             (name, guid, json.dumps(lib.get("dir_list"), ensure_ascii=False)))
        try:
            cfg = c.details(guid)          # ① 先查看他的配置
        except Exception as e:
            emit("[媒体库] ERROR 读取「%s」配置失败，跳过该库：%s" % (name, e))
            ok_all = False
            continue
        payload = _create_payload(cfg)
        emit("[媒体库] INFO 配置已读取：category=%s prefer_local_nfo=%s dir_list=%s" %
             (cfg.get("category"), cfg.get("prefer_local_nfo"),
              json.dumps(cfg.get("dir_list"), ensure_ascii=False)))
        backup = ""
        try:
            backup = _backup_path(data_dir or os.path.join(
                os.path.dirname(os.path.realpath(__file__)), "..", "data"), guid)
            with open(backup, "w", encoding="utf-8") as f:
                json.dump({"guid": guid, "name": name, "config": cfg,
                           "create_payload": payload, "root": root}, f,
                          ensure_ascii=False, indent=2)
            emit("[媒体库] INFO 配置已备份：%s" % backup)
        except Exception as e:
            emit("[媒体库] ERROR 备份失败，为安全起见不删库：%s" % e)
            ok_all = False
            continue

        try:
            c.delete(guid)                 # ② 删除媒体库
            emit("[媒体库] INFO 已删除媒体库「%s」(%s)" % (name, guid))
        except Exception as e:
            emit("[媒体库] ERROR 删库失败（配置未动）：%s" % e)
            ok_all = False
            continue

        # ③ 马上用同配置重建；失败重试，绝不留空库
        new_guid = ""
        for attempt in (1, 2, 3):
            try:
                new_guid = c.create(payload)
                emit("[媒体库] INFO 已重建媒体库「%s」新 guid=%s（第 %d 次）" %
                     (name, new_guid or "?", attempt))
                break
            except Exception as e:
                emit("[媒体库] WARN 重建第 %d 次失败：%s" % (attempt, e))
                time.sleep(1)
        if not new_guid:
            emit("[媒体库] ERROR 重建彻底失败！请手动用备份恢复：%s" % backup)
            ok_all = False
            continue
        try:
            c.scan(new_guid)               # ④ 触发重扫，封面随即生效
            emit("[媒体库] INFO 已触发重扫，飞牛影视打开即显示刮削封面")
        except Exception as e:
            emit("[媒体库] WARN 触发重扫失败（库已重建）：%s" % e)
    return ok_all, lines


if __name__ == "__main__":
    import sys
    ok, out = rebuild(sys.argv[1] if len(sys.argv) > 1 else "")
    for line in out:
        print(line)
    sys.exit(0 if ok else 1)
