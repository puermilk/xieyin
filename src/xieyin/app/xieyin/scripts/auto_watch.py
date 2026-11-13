#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""撷引（xieyin）自动刮削 —— 定时扫描 + qBittorrent 联动。

两个触发源，共用同一套「找待刮目录」的判定：

  定时扫描     每 N 分钟扫媒体目录，找出「有视频但缺 nfo」的文件夹
  qBittorrent  轮询下载完成事件，只刮刚下完的目录

两个保险（都可配）：
  稳定期       文件 mtime 早于 N 分钟才刮，避免误刮下到一半的文件
  PT 保护      检测到 @GROUP / *.torrent 的目录跳过；网页里可关掉这层保护

纯函数集中在文件上半部分，IO 放在下半部分，方便单测。
"""
from __future__ import annotations

import http.cookiejar
import json
import os
import re
import sqlite3
import threading
import time
import urllib.parse
import urllib.request

import media_ext

VIDEO_EXT = media_ext.MEDIA_EXT
# 飞牛下载中心/qBittorrent 的“下载完成”状态并不只有 completed：
# completed / uploading / stalledUP 都表示数据已下完（后两者是在做种）。
DONE_STATES = {"completed", "uploading", "stalledup", "queuedup", "forcedup",
               "pausedup", "checkingup", "moving"}


# ================================================================ 纯函数

HASH_RE = re.compile(r"^[0-9a-fA-F]{32,40}$")


def task_name_from_uri(uri: str) -> str:
    """从 magnet / 包装 URL 的 dn= 参数取任务目录名。"""
    m = re.search(r"(?:[?&]|^)dn=([^&]+)", uri or "")
    if not m:
        return ""
    return urllib.parse.unquote_plus(m.group(1)).strip()


def resolve_task_name(name: str, uri: str = "") -> str:
    """NAME 为空或是 32/40 位 hash 时，回退 URI 的 dn=；都不行则返回空。"""
    n = (name or "").strip()
    if n and not HASH_RE.fullmatch(n):
        return n
    alt = task_name_from_uri(uri)
    if alt and not HASH_RE.fullmatch(alt):
        return alt
    return ""


def has_video(names) -> bool:
    return any(media_ext.is_video(n) for n in names)


def has_nfo(names) -> bool:
    return any(n.lower().endswith(".nfo") for n in names)


def is_domain_prefix(pre: str) -> bool:
    """只认 example.com / www.example.net 这类真域名，避免把 Movie.BluRay 当域名。"""
    if len(pre) > 40:
        return False
    return bool(re.fullmatch(
        r"([A-Za-z0-9-]+\.)+(com|net|org|cc|tv|me|xyz|top|vip|info|club|site|online|cn|io|co)",
        pre))


def is_pt_name(name: str) -> bool:
    """PT 发布名：xxx@GROUP / xxx@GROUP-10bit（@ 在名字末尾，忽略扩展名）。

    域名前缀形式 example.com@ABC-123.mp4 不算 PT。与 clean_qh.sh 保持同一套规则。
    只有干净的短后缀才当扩展名剥离，避免把 Movie.2024.1080p@GROUP 的 @GROUP 吃掉。
    """
    base = os.path.basename(name)
    ext = os.path.splitext(base)[1]
    stem = base[:-len(ext)] if ext and re.fullmatch(r"\.[A-Za-z0-9]{1,5}", ext) else base
    if "@" not in stem:
        return False
    if is_domain_prefix(stem.split("@")[0]):
        return False
    return bool(re.search(r"@[A-Za-z][A-Za-z0-9_-]{0,19}$", stem))


def is_pt_folder(path: str, names) -> bool:
    if any(is_pt_name(n) for n in names):
        return True
    return any(n.lower().endswith(".torrent") for n in names)


def is_stable(mtime: float, now: float, settle_minutes: int) -> bool:
    """文件是否过了「稳定期」（早于 N 分钟），用于跳过下到一半的文件。"""
    return (now - mtime) >= settle_minutes * 60


def pick_targets(folders, now: float, settle_minutes: int, skip_pt: bool) -> list:
    """从候选文件夹里挑出真正要刮的。

    folders: [{"path": str, "names": [str], "mtimes": [float]}]
    规则：有视频、没有 nfo、所有视频都过了稳定期、不在 #recycle、PT 保护未命中。
    返回路径列表。
    """
    out = []
    for f in folders:
        path = f.get("path", "")
        names = f.get("names", [])
        if not path or "#recycle" in path.replace("\\", "/").split("/"):
            continue
        if not has_video(names) or has_nfo(names):
            continue
        mtimes = f.get("mtimes", [])
        if mtimes and not all(is_stable(m, now, settle_minutes) for m in mtimes):
            continue
        if skip_pt and is_pt_folder(path, names):
            continue
        out.append(path)
    return out


def select_new_completed(torrents, seen: dict) -> list:
    """挑出「刚下载完成」且没处理过的种子。

    torrents: [{"hash","content_path","save_path","amount_left","completion_on",...}]
    seen: {hash: completion_on}
    完成判定：amount_left == 0 且 completion_on > 0；completion_on 变了（做种重校验）会重刮。
    """
    out = []
    for t in torrents:
        h = t.get("hash", "")
        if not h:
            continue
        if t.get("amount_left", 1) != 0 or not t.get("completion_on"):
            continue
        if seen.get(h) == t.get("completion_on"):
            continue
        path = t.get("content_path") or os.path.join(t.get("save_path", ""), t.get("name", ""))
        out.append({"hash": h, "completion_on": t.get("completion_on"), "path": path})
    return out


def merge_state(seen: dict, completed: list) -> None:
    for c in completed:
        seen[c["hash"]] = c["completion_on"]


# ================================================================ IO

def list_scan_folders(root: str) -> list:
    """扫 root，返回直接含视频文件的文件夹（与 jav_scrape --scan 同口径）。"""
    out = []
    for dirpath, dirnames, filenames in os.walk(root):
        dirnames[:] = [d for d in dirnames if d != "#recycle"]
        if any(f.lower().endswith(VIDEO_EXT) for f in filenames):
            names = list(filenames)
            out.append({"path": dirpath, "names": names,
                        "mtimes": [_safe_mtime(os.path.join(dirpath, n)) for n in names]})
    return out


def _safe_mtime(path: str) -> float:
    try:
        return os.path.getmtime(path)
    except OSError:
        return 0.0


def load_state(path: str) -> dict:
    try:
        with open(path, encoding="utf-8") as f:
            data = json.load(f)
        return data if isinstance(data, dict) else {}
    except (OSError, ValueError):
        return {}


def save_state(path: str, state: dict) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(state, f, ensure_ascii=False)
    os.replace(tmp, path)


class QbitClient:
    """qBittorrent WebUI API 极简客户端（只读种子列表）。"""

    def __init__(self, url: str, username: str = "", password: str = ""):
        self.base = url.rstrip("/")
        self.cj = http.cookiejar.CookieJar()
        self.opener = urllib.request.build_opener(
            urllib.request.HTTPCookieProcessor(self.cj))

    def _post(self, path: str, data: dict, timeout: int = 15) -> bytes:
        body = urllib.parse.urlencode(data).encode()
        req = urllib.request.Request(self.base + path, data=body)
        with self.opener.open(req, timeout=timeout) as r:
            return r.read()

    def login(self, username: str, password: str) -> bool:
        try:
            out = self._post("/api/v2/auth/login",
                             {"username": username, "password": password})
            return out.strip() == b"Ok."
        except Exception:
            return False

    def torrents(self) -> list:
        try:
            with self.opener.open(self.base + "/api/v2/torrents/info", timeout=15) as r:
                data = json.loads(r.read().decode("utf-8", "ignore"))
            return data if isinstance(data, list) else []
        except Exception:
            return []


# ================================================================ 后台循环

class AutoWatcher:
    """两个后台线程：定时扫描 + qBittorrent 轮询。

    trigger(kind, title, dirs) -> None：由 app_server 注入，负责真正起刮削任务。
    同一时刻只跑一个刮削任务，trigger 内部会做互斥；这里只负责「找活干」。
    """

    def __init__(self, conf_get, state_path: str, trigger, log=None):
        self.conf_get = conf_get
        self.state_path = state_path
        self.trigger = trigger
        self.log = log or (lambda m: None)
        self._stop = threading.Event()
        self._threads = []
        self.last_scan: dict = {"at": 0, "found": 0}
        self.last_qbit: dict = {"at": 0, "found": 0, "ok": False}
        self.last_fnos: dict = {"at": 0, "found": 0, "ok": False}
        self._qbit_seen = load_state(state_path)
        self._fnos_seen = load_state(state_path + ".fnos")
        self._part_warned: set = set()
        self._bad_name_warned: set = set()
        self._qbit_seen_lock = threading.Lock()

    # -- 生命周期 ---------------------------------------------------
    def start(self) -> None:
        if self._threads:
            return
        self._stop.clear()
        for target, name in ((self._scan_loop, "scan"), (self._qbit_loop, "qbit"),
                             (self._fnos_loop, "fnos")):
            t = threading.Thread(target=target, name="xieyin-auto-" + name, daemon=True)
            t.start()
            self._threads.append(t)

    def stop(self) -> None:
        self._stop.set()
        self._threads = []

    def status(self) -> dict:
        with self._qbit_seen_lock:
            seen_n = len(self._qbit_seen)
        return {"scan": dict(self.last_scan), "qbit": dict(self.last_qbit),
                "fnos": dict(self.last_fnos),
                "qbit_tracked": seen_n, "fnos_tracked": len(self._fnos_seen)}

    # -- 定时扫描 ---------------------------------------------------
    def _scan_loop(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            full = self.conf_get()
            conf = full.get("auto", {})
            interval = max(1, int(conf.get("scan_interval_minutes", 30))) * 60
            if (conf.get("enabled") and conf.get("scan_enabled", True)
                    and time.time() - last >= interval):
                last = time.time()
                try:
                    self._run_scan(full)
                except Exception as e:
                    self.log("自动扫描出错: %s" % e)
            # 5 秒一醒：改了间隔 / 开关 5 秒内生效，不用等满一轮
            self._stop.wait(5)

    def _run_scan(self, full: dict) -> None:
        conf = full.get("auto", {})
        root = str(conf.get("scan_root") or conf.get("media_root") or full.get("media_root") or "")
        if not root or not os.path.isdir(root):
            return
        now = time.time()
        folders = list_scan_folders(root)
        targets = pick_targets(
            folders, now,
            int(conf.get("settle_minutes", 10)),
            False)                 # 1.8.0：已移除 PT 保护，做种目录照常刮削
        self.last_scan = {"at": now, "found": len(targets), "root": root}
        if not targets:
            self.log("自动扫描：没有待刮目录")
            return
        self.log("自动扫描：发现 %d 个待刮目录" % len(targets))
        self.trigger("jav", "自动扫描刮削 · %d 个" % len(targets),
                     self._auto_opts(full, targets))

    def _auto_opts(self, full: dict, targets: list) -> dict:
        conf = full.get("auto", {})
        jc = full.get("jav", {})
        return {
            "dirs": targets,
            "recursive": False,
            "no_images": bool(conf.get("no_images", jc.get("no_images", False))),
            "force": bool(conf.get("force", jc.get("force", False))),
            "force_images": bool(conf.get("force_images", jc.get("force_images", False))),
            "no_probe": bool(conf.get("no_probe", jc.get("no_probe", False))),
            "image_policy": str(conf.get("image_policy") or jc.get("image_policy") or "keep"),
            "via": conf.get("via", "auto"),
            "plot_zh": bool(conf.get("plot_zh", jc.get("plot_zh", True))),
            "actor_sync": bool(conf.get("actor_sync", jc.get("actor_sync", False))),
            "fnos_sync": bool(conf.get("fnos_sync", True)),
            "clean_before": bool(conf.get("clean_enabled", False)),
        }

    # -- 飞牛下载中心 ------------------------------------------------
    def _fnos_loop(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            conf = self.conf_get().get("auto", {})
            poll = max(10, int(conf.get("fnos_download_poll_seconds", 30)))
            if (conf.get("enabled") and conf.get("fnos_download_enabled", False)
                    and time.time() - last >= poll):
                last = time.time()
                try:
                    self._run_fnos(self.conf_get())
                except Exception as e:
                    self.log("飞牛下载监听出错: %s" % e)
            self._stop.wait(5)

    def _run_fnos(self, full: dict) -> None:
        conf = full.get("auto", {})
        db = str(conf.get("fnos_download_db") or
                 "/usr/trim/var/downloadcenter/downloadcenter.db")
        if not os.path.isfile(db):
            self.last_fnos = {"at": time.time(), "found": 0, "ok": False,
                              "error": "数据库不存在"}
            return
        try:
            con = sqlite3.connect("file:" + db + "?mode=ro", uri=True, timeout=6)
            con.row_factory = sqlite3.Row
            rows = [dict(r) for r in con.execute(
                "select ID,TASK_ID,NAME,SAVE_DIR,COMPLETION_TIME,STATE,SIZE,DL_TOTAL_SIZE "
                "from TASKS order by ID desc limit 200")]
            con.close()
        except Exception as e:
            self.last_fnos = {"at": time.time(), "found": 0, "ok": False, "error": str(e)}
            return
        targets = []
        changed = False
        for r in rows:
            state = str(r.get("STATE") or "").strip().lower()
            if state not in DONE_STATES:
                continue
            size = int(r.get("SIZE") or 0)
            done = int(r.get("DL_TOTAL_SIZE") or 0)
            # completed 直接信；uploading/stalledUP 用体积兜底，避免半截也被当成完成
            if state != "completed" and size > 0 and done < size * 0.995:
                continue
            key = "%s:%s" % (r.get("TASK_ID") or r.get("ID"), r.get("COMPLETION_TIME") or "")
            if key in self._fnos_seen:
                continue
            save_dir = str(r.get("SAVE_DIR") or "").strip()
            name = resolve_task_name(r.get("NAME"), r.get("URI"))
            if not save_dir or not name:
                # NAME 为空（aria2）或是 qbt 的 hash 时，绝不能拿 SAVE_DIR
                # 整个媒体根目录去兜底，否则会把整库当成一个任务。
                if key not in self._bad_name_warned:
                    self.log("飞牛下载任务名缺失/为 hash，跳过：TASK_ID=%s" %
                             (r.get("TASK_ID") or r.get("ID")))
                    self._bad_name_warned.add(key)
                continue
            cand = os.path.join(save_dir, name)
            if os.path.isdir(cand):
                folder = cand
            elif os.path.isfile(cand):
                folder = os.path.dirname(cand)
            else:
                if key not in self._bad_name_warned:
                    self.log("飞牛下载任务目录不存在，跳过：%s" % cand)
                    self._bad_name_warned.add(key)
                continue
            if not os.path.isdir(folder):
                continue
            try:
                names = os.listdir(folder)
            except OSError:
                names = []
            if any(n.lower().endswith(".nfo") for n in names):
                # 已有 NFO，说明刮过了；记状态，避免每轮重复检查
                self._fnos_seen[key] = r.get("COMPLETION_TIME") or 0
                changed = True
                self._part_warned.discard(folder)
                continue
            has_final = any(media_ext.is_video(n) and not n.lower().endswith(".part")
                            for n in names)
            has_part = any(n.lower().endswith(".part") and media_ext.is_video(n[:-5])
                           for n in names)
            if has_final:
                if folder not in targets:
                    targets.append(folder)
                self._fnos_seen[key] = r.get("COMPLETION_TIME") or 0
                changed = True
                self._part_warned.discard(folder)
            elif has_part:
                # 数据下完了，但 qBittorrent 还没把 .part 改成正式扩展名。
                # 不提前刮，避免刮到半截文件；等下一轮自动重试。
                if folder not in self._part_warned:
                    self.log("飞牛下载完成但仍是 .part，等待 qBittorrent 重命名：%s" % folder)
                    self._part_warned.add(folder)
            else:
                self._part_warned.discard(folder)
        if targets:
            save_state(self.state_path + ".fnos", self._fnos_seen)
            self.last_fnos = {"at": time.time(), "found": len(targets), "ok": True}
            self.log("飞牛下载完成：发现 %d 个目录" % len(targets))
            self.trigger("jav", "飞牛下载完成刮削 · %d 个" % len(targets),
                         self._auto_opts(full, targets))
        else:
            if changed:
                save_state(self.state_path + ".fnos", self._fnos_seen)
            self.last_fnos = {"at": time.time(), "found": 0, "ok": True}

    # -- qBittorrent ------------------------------------------------
    def _qbit_loop(self) -> None:
        last = 0.0
        while not self._stop.is_set():
            conf = self.conf_get().get("auto", {})
            poll = max(10, int(conf.get("qbit_poll_seconds", 45)))
            if (conf.get("enabled") and conf.get("qbit_enabled", False)
                    and time.time() - last >= poll):
                last = time.time()
                try:
                    self._run_qbit(conf)
                except Exception as e:
                    self.log("qBittorrent 轮询出错: %s" % e)
            self._stop.wait(5)

    def _run_qbit(self, conf: dict) -> None:
        url = conf.get("qbit_url", "")
        if not url:
            return
        client = QbitClient(url)
        if not client.login(conf.get("qbit_username", ""),
                            conf.get("qbit_password", "")):
            self.last_qbit = {"at": time.time(), "found": 0, "ok": False,
                              "error": "登录失败"}
            self.log("qBittorrent 登录失败")
            return
        torrents = client.torrents()
        with self._qbit_seen_lock:
            completed = select_new_completed(torrents, self._qbit_seen)
            merge_state(self._qbit_seen, completed)
            save_state(self.state_path, self._qbit_seen)
        self.last_qbit = {"at": time.time(), "found": len(completed), "ok": True}
        if not completed:
            return
        now = time.time()
        settle = int(conf.get("settle_minutes", 10))
        folders = []
        for c in completed:
            p = c["path"]
            if not os.path.isdir(p):
                continue
            names = os.listdir(p)
            folders.append({"path": p, "names": names,
                            "mtimes": [_safe_mtime(os.path.join(p, n)) for n in names]})
        targets = pick_targets(folders, now, settle, False)  # 1.8.0：无 PT 保护
        if not targets:
            self.log("qBittorrent：新完成 %d 个，但无需刮削（已刮 / 未稳定）"
                     % len(completed))
            return
        self.log("qBittorrent：新完成 %d 个，刮削 %d 个" % (len(completed), len(targets)))
        self.trigger("jav", "qBittorrent 下载完成刮削 · %d 个" % len(targets),
                     {"dirs": targets, "recursive": False,
                      "no_images": bool(conf.get("no_images", False)),
                      "via": conf.get("via", "auto")})
