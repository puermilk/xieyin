#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""撷引（xieyin）—— fnOS 应用服务端（纯 Python 标准库）。

把原来 Docker 版里的四个脚本包成一个常驻 Web 服务：
  race_scrape.py  番号 AV 刮削（国内直连多源竞速：jav321 / javdb580）
  jav_scrape.py   番号 AV 刮削（JavBus 旧通道，engine=legacy 时回退用）
  tmdb_scrape.py  电影刮削（TMDB）
  check_meta.sh   媒体体检
  clean_qh.sh     共享清理
  net_test.sh     网络与依赖自检

用法：
  app_server.py serve          前台起服务（systemd 用）
  app_server.py status         打印状态 JSON

数据目录：$XIEYIN_DATA 或 <脚本目录>/data
  config.json   网页里改的配置
  logs/         每个任务的完整日志
"""
from __future__ import annotations

import json
import os
import re
import shlex
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.parse
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

VERSION = "2.1.2"
APP_DIR = os.path.dirname(os.path.realpath(__file__))
DATA_DIR = os.environ.get("XIEYIN_DATA") or os.path.join(APP_DIR, "data")
CONF_PATH = os.path.join(DATA_DIR, "config.json")
TRANSLATE_KEY_PATH = os.path.join(DATA_DIR, "translate_key")
LOG_DIR = os.path.join(DATA_DIR, "logs")
SCRIPTS = os.path.join(APP_DIR, "scripts")
WWW = os.path.join(APP_DIR, "www")
PYTHON = sys.executable or "/usr/bin/python3"

sys.path.insert(0, SCRIPTS)
import auto_watch  # noqa: E402
import media_ext  # noqa: E402
import translate_zh  # noqa: E402

DEFAULT_ROOTS = [
    "/vol1/1000/Media",
]

DEFAULT_CONF = {
    "port": 22668,
    "bind": "0.0.0.0",
    "media_root": "",
    "proxy": "",
    # engine: race=国内直连多源竞速（默认） / legacy=原 JavBus 通道
    "jav": {"via": "auto", "recursive": True, "engine": "race",
            "preflight_ttl": 600, "plot_zh": True,
            "translate_provider": "auto", "translate_model": "GLM-5.3-Flash",
            "translate_api_base": "", "translate_max_tokens": 1600,
            "translate_temperature": 0.7,
            "actor_sync": False,
            "copy_enabled": False, "copy_root": ""},
    "tmdb": {},
    # 方案A：飞牛影视 API 直连（元数据推送，PT 目录零写入）
    "fnos": {
        "enabled": True,        # 对接飞牛影视（主模式）
        "sync_meta": True,      # 刮削/整理后把本地 NFO 标题/简介写入飞牛（官方 saveEditDetail）
        "url": "",              # 旧版推送地址（可选）
        "token": "",            # 旧版推送令牌（可选）
        "export_nfo": False,    # 兜底选项：导出 NFO 到独立目录（兼容 Jellyfin/Emby）
        "meta_dir": "",         # 独立元数据目录（export_nfo 开启时用）
    },
    "history_limit": 80,
    "auto": {
        "enabled": False,          # 自动刮削总开关
        "scan_enabled": True,      # 定时扫描
        "scan_interval_minutes": 30,
        "qbit_enabled": False,     # qBittorrent 联动
        "qbit_url": "http://127.0.0.1:8085",
        "qbit_username": "",
        "qbit_password": "",
        "qbit_poll_seconds": 45,
        "settle_minutes": 10,      # 稳定期：文件早于 N 分钟才刮
        "skip_pt": False,          # 1.8.0 起无 PT 保护：该值不再生效，保留仅为兼容旧配置
        "no_images": False,
        "plot_zh": True,        # 自动刮削：标题/简介翻译
        "actor_sync": False,    # 自动刮削：演员整理/补头像/回写 NFO
        "fnos_sync": True,      # 自动刮削：完成后同步到飞牛影视
        "clean_enabled": False, # 自动刮削：完成后自动整理当前目录
        "via": "auto",
    },
}

VIDEO_EXT = media_ext.MEDIA_EXT

_fnos_token_backup = ""

_jobs_lock = threading.Lock()
_jobs: list["Job"] = []
_current: "Job | None" = None
_seq = 0
_watcher: "auto_watch.AutoWatcher | None" = None


# --------------------------------------------------------------------- 配置

def _merge(base: dict, over: dict) -> None:
    for k, v in (over or {}).items():
        if isinstance(v, dict) and isinstance(base.get(k), dict):
            _merge(base[k], v)
        else:
            base[k] = v


def load_conf() -> dict:
    conf = json.loads(json.dumps(DEFAULT_CONF))
    try:
        with open(CONF_PATH, encoding="utf-8") as f:
            _merge(conf, json.load(f))
    except FileNotFoundError:
        pass
    except Exception as e:
        log(f"配置读取失败，使用默认值: {e}")
    if not conf.get("media_root"):
        conf["media_root"] = detect_default_root()
    # 2.0.3 迁移：老配置升级后默认开启翻译，只迁移一次；用户之后仍可手动关闭
    if not conf.get("_translation_default_v2"):
        conf.setdefault("jav", {})["plot_zh"] = True
        conf["_translation_default_v2"] = True
        try:
            save_conf(conf)
        except Exception:
            pass
    return conf


def save_conf(conf: dict) -> None:
    os.makedirs(DATA_DIR, exist_ok=True)
    tmp = CONF_PATH + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(conf, f, ensure_ascii=False, indent=2)
    os.replace(tmp, CONF_PATH)


def detect_default_root() -> str:
    for p in DEFAULT_ROOTS + ["/vol1/1000", "/vol2/1000", "/vol02", "/vol1"]:
        try:
            if os.path.isdir(p) and os.listdir(p):
                return p
        except OSError:
            continue
    for p in DEFAULT_ROOTS + ["/vol1/1000", "/vol1"]:
        if os.path.isdir(p):
            return p
    return "/vol1/1000"


def known_roots() -> list[dict]:
    out = []
    seen = set()
    for p in DEFAULT_ROOTS:
        seen.add(p)
        out.append({"path": p, "exists": os.path.isdir(p)})
    for base in ("/vol1/1000", "/vol2/1000", "/vol02"):
        try:
            for name in sorted(os.listdir(base))[:40]:
                p = os.path.join(base, name)
                if os.path.isdir(p) and p not in seen and not name.startswith("."):
                    seen.add(p)
                    out.append({"path": p, "exists": True})
        except OSError:
            pass
    return out


# --------------------------------------------------------------------- 日志

def log(msg: str) -> None:
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {msg}"
    print(line, flush=True)
    try:
        os.makedirs(DATA_DIR, exist_ok=True)
        with open(os.path.join(DATA_DIR, "server.log"), "a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


# --------------------------------------------------------------------- 任务

class Job:
    def __init__(self, kind: str, title: str, cmd: list[str], env: dict | None = None,
                 meta: dict | None = None):
        global _seq
        _seq += 1
        self.id = time.strftime("%Y%m%d-%H%M%S") + "-%02d" % (_seq % 100)
        self.kind = kind
        self.title = title
        self.cmd = cmd
        self.env = env
        self.meta = meta or {}
        self.status = "running"
        self.returncode: int | None = None
        self.started = time.time()
        self.ended: float | None = None
        self.lines: list[str] = []
        self.lock = threading.Lock()
        self.proc: subprocess.Popen | None = None
        self.stopping = False
        self._fh = None
        os.makedirs(LOG_DIR, exist_ok=True)
        self.log_path = os.path.join(LOG_DIR, f"{self.id}-{self.kind}.log")
        self.thread = threading.Thread(target=self._run, daemon=True)

    # -- 输出 ---------------------------------------------------------
    def append(self, line: str) -> None:
        line = line.rstrip("\n")
        with self.lock:
            self.lines.append(line)
        try:
            if self._fh:
                self._fh.write(line + "\n")
                self._fh.flush()
            else:
                with open(self.log_path, "a", encoding="utf-8") as f:
                    f.write(line + "\n")
        except OSError:
            pass

    def snapshot(self, offset: int = 0, limit: int = 4000) -> dict:
        with self.lock:
            total = len(self.lines)
            chunk = self.lines[offset:offset + limit]
            next_offset = offset + len(chunk)
        return {
            "id": self.id,
            "kind": self.kind,
            "title": self.title,
            "status": self.status,
            "returncode": self.returncode,
            "started": self.started,
            "ended": self.ended,
            "elapsed": round((self.ended or time.time()) - self.started, 1),
            "total_lines": total,
            "offset": offset,
            "next_offset": next_offset,
            "lines": chunk,
            "log_path": self.log_path,
            "command": " ".join(shlex.quote(x) for x in self.cmd),
            "meta": self.meta,
        }

    # -- 完成后 -------------------------------------------------------
    def _after_success(self) -> None:
        """1.8.2：刮削成功后把封面经飞牛影视官方图片接口同步进去（不删库）。

        走 POST /image/temp/upload 上传封面（飞牛自行校验/转 webp/生成缩略图），
        再用官方 getEditDetail/saveEditDetail 把 posters 指向返回的 hash_path；
        飞牛随即把图从 temp 移到正式目录并删除旧截图封面，约 5 秒生效。
        删库重建（mbd_rebuild）降级为手动兜底，不再自动触发。
        任何失败只写 WARN，不影响已成功的刮削任务与产物。
        """
        meta = self.meta or {}
        root = str(meta.get("root") or "")
        if meta.get("dry") or not root or self.status != "success":
            return
        # 文件整理会改名/删除视频：成功后先触发飞牛扫描，再同步标题/简介
        if self.kind == "clean":
            if str(meta.get("mode") or "") == "run":
                self.status = "running"
                try:
                    self._sync_fnos_meta(root, scan=True)
                finally:
                    self.status = "success"
            return
        if self.kind not in ("jav", "tmdb"):
            return
        only = str(meta.get("only") or "").strip()
        dirs = [str(d) for d in (meta.get("dirs") or []) if d]
        # 自动任务只传 dirs（本次实际刮削目录），封面/翻译/演员/飞牛都必须用它；
        # 用全局 media_root 会扫错库，日志显示“已一致”但新封面从未上传。
        scopes = dirs or [root]
        # 刮削完成后补译已有 NFO 的标题/简介（只补缺，不重复跑完整翻译）
        if (self.kind == "jav"
                and str((self.env or {}).get("XIEYIN_PLOT_ZH") or "") == "1"):
            # 同步期间保持 running，前端才会继续轮询补译日志；完成后恢复 success
            self.status = "running"
            try:
                for scope in scopes:
                    self._sync_translations(scope, only=only if not dirs else "")
            finally:
                self.status = "success"
        # 工作台/自动刮削勾选“演员整理”时：补头像/合并演员/回写 NFO
        if (self.kind == "jav"
                and str((self.env or {}).get("XIEYIN_ACTOR_SYNC") or "") == "1"):
            self.status = "running"
            try:
                for scope in scopes:
                    self._sync_actors(scope)
            finally:
                self.status = "success"
        # 把最终 NFO 标题/简介写入飞牛（官方 saveEditDetail，无需旧版 push 令牌）
        fn = load_conf().get("fnos") or {}
        fnos_override = str((self.env or {}).get("XIEYIN_FNOS_SYNC") or "")
        if (fnos_override != "0" and fn.get("enabled", True)
                and fn.get("sync_meta", True)):
            self.status = "running"
            try:
                for scope in scopes:
                    self._sync_fnos_meta(scope, scan=True)
            finally:
                self.status = "success"
        # 封面同步放在飞牛扫描之后：新条目先入库，再上传封面。
        # 每个 scope 都是本次真实刮削目录，避免扫到错误媒体库。
        try:
            import img_cache_sync
            ok_all = True
            for scope in scopes:
                ok, lines = img_cache_sync.sync_covers(scope, data_dir=DATA_DIR)
                for line in lines:
                    self.append(line)
                ok_all = ok_all and ok
            if not ok_all:
                self.append("[封面缓存] WARN 部分条目未同步；"
                            "如需强制重建媒体库请手动执行 mbd_rebuild")
        except Exception as e:
            self.append("[封面缓存] WARN 封面缓存同步跳过：%s" % e)

    def _sync_actors(self, root: str) -> None:
        script = os.path.join(SCRIPTS, "actor_sync_all.py")
        if not os.path.isfile(script) or not os.path.isdir(root):
            return
        self.append("[演员] 整理演员名 / 补头像 / 回写 NFO ...")
        env = os.environ.copy()
        env.update(self.env or {})
        env.setdefault("PYTHONUNBUFFERED", "1")
        cmd = [PYTHON, script, "--root", root, "--photo-limit", "20", "--write"]
        try:
            p = subprocess.Popen(
                cmd, cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, errors="replace", env=env, start_new_session=True)
            assert p.stdout is not None
            for line in p.stdout:
                self.append("[演员] " + line.rstrip())
            rc = p.wait()
            self.append("[演员] 完成 rc=%d" % rc)
        except Exception as e:
            self.append("[演员] WARN 同步跳过：%s" % e)

    def _sync_fnos_meta(self, root: str, scan: bool = True) -> None:
        script = os.path.join(SCRIPTS, "sync_fnos_meta.py")
        if not os.path.isfile(script) or not os.path.isdir(root):
            return
        self.append("[飞牛] 同步标题/简介%s ..." % ("（先扫描媒体库）" if scan else ""))
        env = os.environ.copy()
        env.update(self.env or {})
        env.setdefault("PYTHONUNBUFFERED", "1")
        env.setdefault("XIEYIN_FNOS_API", "http://127.0.0.1:8005")
        cmd = [PYTHON, script, "--root", root, "--write"]
        if scan:
            cmd.append("--scan")
        meta = self.meta or {}
        only = str(meta.get("only") or "").strip()
        if only:
            cmd += ["--only", only]
        try:
            p = subprocess.Popen(
                cmd, cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, errors="replace", env=env, start_new_session=True)
            assert p.stdout is not None
            for line in p.stdout:
                self.append("[飞牛] " + line.rstrip())
            rc = p.wait()
            self.append("[飞牛] 完成 rc=%d" % rc)
        except Exception as e:
            self.append("[飞牛] WARN 同步跳过：%s" % e)

    def _clean_scope(self, root: str) -> None:
        script = os.path.join(SCRIPTS, "clean_qh.sh")
        if not os.path.isfile(script) or not os.path.isdir(root):
            return
        self.append("[整理] 清理广告/垃圾/孤儿文件：%s" % root)
        env = os.environ.copy()
        env.update(self.env or {})
        env["XIEYIN_BASE"] = root
        env.setdefault("XIEYIN_PT_GUARD", "1")
        env.setdefault("XIEYIN_KEEP_SIDECARS", "1")
        env.setdefault("XIEYIN_CLEAN_ORPHANS", "1")
        env.setdefault("PYTHONUNBUFFERED", "1")
        try:
            p = subprocess.Popen(
                ["bash", script, "run"], cwd=APP_DIR, stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT, text=True, bufsize=1,
                errors="replace", env=env, start_new_session=True)
            assert p.stdout is not None
            for line in p.stdout:
                self.append("[整理] " + line.rstrip())
            rc = p.wait()
            self.append("[整理] 完成 rc=%d" % rc)
        except Exception as e:
            self.append("[整理] WARN 跳过：%s" % e)

    def _sync_translations(self, root: str, only: str = "") -> None:
        script = os.path.join(SCRIPTS, "retranslate_nfo.py")
        if not os.path.isfile(script) or not os.path.isdir(root):
            return
        self.append("[翻译] 同步检查已有 NFO 的标题/简介：%s%s"
                    % (root, (" · 仅 " + only) if only else ""))
        env = os.environ.copy()
        env.update(self.env or {})
        env.setdefault("PYTHONUNBUFFERED", "1")
        cmd = [PYTHON, script, "--root", root, "--write", "--fill", "--jobs", "2"]
        if only:
            cmd += ["--only", only]
        try:
            p = subprocess.Popen(
                cmd, cwd=APP_DIR, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                text=True, bufsize=1, errors="replace", env=env, start_new_session=True)
            assert p.stdout is not None
            for line in p.stdout:
                self.append("[翻译] " + line.rstrip())
            rc = p.wait()
            self.append("[翻译] 完成 rc=%d" % rc)
        except Exception as e:
            self.append("[翻译] WARN 同步跳过：%s" % e)

    # -- 执行 ---------------------------------------------------------
    def _run(self) -> None:
        try:
            self._fh = open(self.log_path, "w", encoding="utf-8")
        except OSError:
            self._fh = None
        self.append("$ " + " ".join(shlex.quote(x) for x in self.cmd))
        self.append("")
        try:
            env = os.environ.copy()
            env.update(self.env or {})
            env.setdefault("PYTHONUNBUFFERED", "1")
            # 自动任务：先整理本次目录（广告/垃圾/孤儿/域名前缀），再刮削
            if (self.kind == "jav"
                    and str(env.get("XIEYIN_CLEAN_BEFORE") or "") == "1"):
                meta = self.meta or {}
                dirs = [str(d) for d in (meta.get("dirs") or []) if d]
                scopes = dirs or [str(meta.get("root") or "")]
                for scope in scopes:
                    if scope:
                        self._clean_scope(scope)
            self.proc = subprocess.Popen(
                self.cmd,
                cwd=APP_DIR,
                stdout=subprocess.PIPE,
                stderr=subprocess.STDOUT,
                text=True,
                bufsize=1,
                errors="replace",
                start_new_session=True,
                env=env,
            )
            assert self.proc.stdout is not None
            for line in self.proc.stdout:
                self.append(line)
            rc = self.proc.wait()
            self.returncode = rc
            if self.stopping:
                self.status = "stopped"
            elif rc == 0:
                self.status = "success"
                self._after_success()
            elif self.kind == "clean" and rc == 2:
                self.status = "blocked"      # PT 保护中止，不是故障
            else:
                self.status = "failed"
        except Exception as e:
            self.append(f"!! 启动失败: {e}")
            self.returncode = -1
            self.status = "failed"
        finally:
            self.ended = time.time()
            self.append("")
            self.append(f"== 任务结束：{self.status}（{self.returncode}）"
                        f" 用时 {self.ended - self.started:.1f}s ==")
            if self._fh:
                try:
                    self._fh.close()
                except OSError:
                    pass
                self._fh = None

    def start(self) -> None:
        self.thread.start()

    def stop(self) -> None:
        self.stopping = True
        p = self.proc
        if not p or p.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(p.pid), signal.SIGTERM)
        except OSError:
            return
        def _kill():
            try:
                p.wait(timeout=6)
            except subprocess.TimeoutExpired:
                try:
                    os.killpg(os.getpgid(p.pid), signal.SIGKILL)
                except OSError:
                    pass
        threading.Thread(target=_kill, daemon=True).start()


def start_job(kind: str, title: str, cmd: list[str], env: dict | None = None,
              meta: dict | None = None) -> Job:
    global _current
    with _jobs_lock:
        if _current and _current.status == "running":
            raise RuntimeError("已有任务在运行，请等它结束或先停止")
        job = Job(kind, title, cmd, env=env, meta=meta)
        _jobs.append(job)
        limit = int(load_conf().get("history_limit") or 80)
        del _jobs[:-limit]
        _current = job
    job.start()
    log(f"任务开始 {job.id} {title}: {job.snapshot(0, 1)['command']}")
    return job


def job_by_id(job_id: str) -> Job | None:
    with _jobs_lock:
        for j in _jobs:
            if j.id == job_id:
                return j
    return None


# --------------------------------------------------------------------- 命令构造

def require_dir(path: str) -> str:
    path = os.path.abspath(os.path.expanduser(path or ""))
    if not os.path.isdir(path):
        raise ValueError(f"目录不存在: {path}")
    return path


def has_ffprobe() -> bool:
    return bool(shutil.which("ffprobe"))


def resolve_sink(conf: dict) -> str:
    """1.7.0 以刮削为主：始终就地写 NFO/封面进视频文件夹；
    推送/导出仅在配置齐全时叠加，缺失即跳过（不再 E004 阻塞刮削）。"""
    fn = conf.get("fnos") or {}
    parts = []
    if bool(fn.get("enabled", True)) and str(fn.get("url") or "").strip():
        parts.append("push")
    if bool(fn.get("export_nfo")) and str(fn.get("meta_dir") or "").strip():
        parts.append("export")
    parts.append("inplace")
    return "+".join(parts)


def build_job(data: dict) -> tuple[str, str, list[str], dict, dict]:
    kind = str(data.get("kind") or "").strip()
    conf = load_conf()
    # 自动刮削按「指定文件夹列表」走，不强求根目录存在
    dirs = [str(d) for d in (data.get("dirs") or []) if d] if kind == "jav" else []
    raw_root = str(data.get("root") or conf.get("media_root") or "")
    root = raw_root if dirs else require_dir(raw_root)
    proxy = str(data.get("proxy") or conf.get("proxy") or "").strip()
    no_probe = bool(data.get("no_probe")) or not has_ffprobe()
    env: dict[str, str] = {}
    if proxy:
        env["XIEYIN_PROXY"] = proxy
    jc = conf.get("jav") or {}
    if jc.get("concurrency"):
        env["XIEYIN_CONCURRENCY"] = str(int(jc["concurrency"]))
    if jc.get("timeout"):
        env["XIEYIN_TIMEOUT"] = str(int(jc["timeout"]))
    if jc.get("retries") is not None:
        env["XIEYIN_RETRIES"] = str(int(jc["retries"]))
    plot_zh = data.get("plot_zh")
    if plot_zh is None:
        plot_zh = jc.get("plot_zh")
    if plot_zh:
        env["XIEYIN_PLOT_ZH"] = "1"
    if jc.get("translate_provider"):
        env["XIEYIN_TRANSLATE_PROVIDER"] = str(jc["translate_provider"])
    if jc.get("translate_model"):
        env["XIEYIN_LLM_MODEL"] = str(jc["translate_model"])
    if jc.get("translate_api_base"):
        env["XIEYIN_LLM_BASE"] = str(jc["translate_api_base"])
    if jc.get("translate_max_tokens"):
        env["XIEYIN_LLM_MAX_TOKENS"] = str(int(jc["translate_max_tokens"]))
    if jc.get("translate_temperature") is not None:
        env["XIEYIN_LLM_TEMPERATURE"] = str(float(jc["translate_temperature"]))
    actor_sync = data.get("actor_sync")
    if actor_sync is None:
        actor_sync = jc.get("actor_sync")
    if actor_sync:
        env["XIEYIN_ACTOR_SYNC"] = "1"
    if data.get("fnos_sync") is False:
        env["XIEYIN_FNOS_SYNC"] = "0"
    if data.get("clean_before"):
        env["XIEYIN_CLEAN_BEFORE"] = "1"
    copy_root = str(jc.get("copy_root") or "").strip()
    if jc.get("copy_enabled") and copy_root:
        env["XIEYIN_COPY_ROOT"] = copy_root
    if conf.get("auth_key"):
        env["XIEYIN_AUTH_KEY"] = str(conf["auth_key"])

    if kind == "net":
        env["XIEYIN_PY"] = PYTHON
        env["XIEYIN_PROXY"] = proxy
        return ("net", "网络与依赖检测",
                ["bash", os.path.join(SCRIPTS, "net_test.sh"), root], env,
                {"root": root})

    if kind == "check":
        return ("check", f"媒体体检 · {os.path.basename(root)}",
                ["bash", os.path.join(SCRIPTS, "check_meta.sh"), root], env,
                {"root": root})

    if kind == "clean":
        mode = str(data.get("mode") or "dry")
        if mode not in ("dry", "run"):
            raise ValueError("mode 只能是 dry 或 run")
        if mode == "run" and str(data.get("confirm") or "") != "CLEAN":
            raise ValueError("执行清理需要确认（confirm 必须是 CLEAN）")
        pt_guard = bool(data.get("pt_guard", True))   # 做种目录守卫（防误删）保留
        env["XIEYIN_BASE"] = root
        env["XIEYIN_PT_GUARD"] = "1" if pt_guard else "0"
        env["XIEYIN_KEEP_SIDECARS"] = "1"
        title = "清理共享 · " + ("执行" if mode == "run" else "预览")
        if pt_guard:
            title += "（做种保护）"
        return ("clean", title,
                ["bash", os.path.join(SCRIPTS, "clean_qh.sh"), mode], env,
                {"root": root, "mode": mode, "pt_guard": pt_guard})

    if kind == "retranslate":
        write = bool(data.get("write"))
        fill = bool(data.get("fill"))
        force = bool(data.get("force", not fill))
        env["XIEYIN_PLOT_ZH"] = "1"       # 重译任务本身就是翻译，不再依赖 plot_zh 开关
        cmd = [PYTHON, os.path.join(SCRIPTS, "retranslate_nfo.py"), "--root", root]
        if write:
            cmd.append("--write")
        if force:
            cmd.append("--force")
        elif fill:
            cmd.append("--fill")
        only = str(data.get("only") or "").strip()
        if only:
            cmd += ["--only", only]
        limit = int(data.get("limit") or 0)
        if limit > 0:
            cmd += ["--limit", str(limit)]
        mode = "全部重译" if force else ("补缺重译" if fill else "预览")
        return ("retranslate", "NFO 重译 · " + ("写入 · " if write else "预览 · ") + mode
                + " · " + os.path.basename(root),
                cmd, env, {"root": root, "write": write, "force": force, "fill": fill})

    if kind == "actors":
        write = bool(data.get("write"))
        limit = int(data.get("photo_limit") or 20)
        cmd = [PYTHON, os.path.join(SCRIPTS, "actor_sync_all.py"),
               "--root", root, "--photo-limit", str(limit)]
        if write:
            cmd.append("--write")
        return ("actors", "演员整理 · " + ("写入" if write else "预览") + " · " + os.path.basename(root),
                cmd, env, {"root": root, "write": write})

    if kind == "jav":
        dry = bool(data.get("dry"))
        # 1.7.0：inplace 恒在（刮削直接写文件夹）；push/export 配置齐全才叠加
        fn = conf.get("fnos") or {}
        sink = resolve_sink(conf)
        engine = str((conf.get("jav") or {}).get("engine") or "race")
        legacy = engine == "legacy"
        script = "jav_scrape.py" if legacy else "race_scrape.py"
        logname = "jav_scrape.log" if legacy else "race_scrape.log"
        cmd = [PYTHON, os.path.join(SCRIPTS, script),
               "--log", os.path.join(LOG_DIR, logname),
               "--via", str(data.get("via") or "auto")]
        if not legacy:
            cmd += ["--preflight-ttl",
                    str(int((conf.get("jav") or {}).get("preflight_ttl") or 600))]
        cmd += ["--sink", sink]
        if "push" in sink:
            cmd += ["--fnos-url", str(fn.get("url") or ""),
                    "--fnos-token", str(fn.get("token") or "")]
        if "export" in sink:
            cmd += ["--meta-dir", str(fn.get("meta_dir") or "")]
        if root:
            cmd += ["--root", root]
        only = str(data.get("only") or "").strip()
        recursive = bool(data.get("recursive"))
        if dirs:
            for d in dirs:
                cmd += ["--dirs", require_dir(d)]
        elif only:
            cmd += ["--only", only]
        elif recursive:
            cmd += ["--scan", root]
        limit = int(data.get("limit") or 0)
        if limit > 0:
            cmd += ["--limit", str(limit)]
        if dry:
            cmd.append("--dry")
        if jc.get("copy_enabled") and copy_root:
            if root and (os.path.abspath(copy_root) == os.path.abspath(root)
                         or os.path.abspath(copy_root).startswith(os.path.abspath(root) + os.sep)):
                raise ValueError("复制目录不能放在刮削目录里面")
            cmd += ["--copy-root", copy_root]
        # 条目级并发（与界面「并发数」不同：那是单条内的多源通道数）
        if not legacy:
            try:
                cmd += ["--jobs", str(int((conf.get("jav") or {}).get("jobs") or 2))]
            except Exception:
                cmd += ["--jobs", "2"]
        if data.get("force"):
            cmd.append("--force")
        if data.get("force_images"):
            cmd.append("--force-images")
        img_pol = str(data.get("image_policy") or (jc.get("image_policy") or "keep"))
        if img_pol in ("keep", "replace", "smart"):
            cmd += ["--image-policy", img_pol]
        if data.get("no_images"):
            cmd.append("--no-images")
        if no_probe:
            cmd.append("--no-probe")
        title = "番号刮削 · " + ("预览" if dry else "开始")
        if dirs:
            title += "（%d 个目录）" % len(dirs)
        return ("jav", title, cmd, env,
                {"root": root, "dry": dry, "recursive": recursive, "only": only,
                 "dirs": dirs, "limit": limit, "force": bool(data.get("force")),
                 "via": str(data.get("via") or "auto"), "engine": engine,
                 "sink": sink})

    if kind == "tmdb":
        dry = bool(data.get("dry"))
        fn = conf.get("fnos") or {}
        sink = resolve_sink(conf)
        folder = str(data.get("folder") or "").strip()
        tmdb_id = re.sub(r"\D", "", str(data.get("tmdb_id") or ""))
        query = str(data.get("query") or "").strip()
        cmd = [PYTHON, os.path.join(SCRIPTS, "tmdb_scrape.py"), "--root", root,
               "--sink", sink]
        if "push" in sink:
            cmd += ["--fnos-url", str(fn.get("url") or ""),
                    "--fnos-token", str(fn.get("token") or "")]
        if "export" in sink:
            cmd += ["--meta-dir", str(fn.get("meta_dir") or "")]
        if folder:
            cmd += ["--dir", require_dir(folder)]
        if tmdb_id:
            cmd += ["--tmdb-id", tmdb_id]
        elif query:
            cmd += ["--query", query]
        if dry:
            cmd.append("--dry")
        if data.get("force"):
            cmd.append("--force")
        if data.get("force_images"):
            cmd.append("--force-images")
        img_pol = str(data.get("image_policy") or (jc.get("image_policy") or "keep"))
        if img_pol in ("keep", "replace", "smart"):
            cmd += ["--image-policy", img_pol]
        if data.get("no_images"):
            cmd.append("--no-images")
        if no_probe:
            cmd.append("--no-probe")
        title = "电影刮削 · " + ("预览" if dry else "开始")
        return ("tmdb", title, cmd, env,
                {"root": root, "folder": folder, "dry": dry,
                 "tmdb_id": tmdb_id, "query": query,
                 "force": bool(data.get("force")), "sink": sink})

    raise ValueError(f"未知任务类型: {kind}")


# --------------------------------------------------------------------- 状态

def auto_trigger(kind: str, title: str, opts: dict) -> None:
    """自动刮削回调：起一个 jav 任务；已有任务在跑就跳过本轮。"""
    try:
        data = {"kind": kind, "dry": False, **opts}
        job_kind, job_title, cmd, env, meta = build_job(data)
        start_job(job_kind, job_title, cmd, env=env, meta=meta)
        log("自动触发：%s" % job_title)
    except RuntimeError:
        log("自动触发跳过（已有任务在跑）：%s" % title)
    except Exception as e:
        log("自动触发失败：%s —— %s" % (title, e))


def status_dict() -> dict:
    conf = load_conf()
    with _jobs_lock:
        running = _current.snapshot(0, 0) if _current and _current.status == "running" else None
        jobs = [j.snapshot(0, 0) for j in reversed(_jobs[-30:])]
    roots = known_roots()
    pillow = False
    try:
        import PIL  # noqa: F401
        pillow = True
    except Exception:
        pass
    return {
        "ok": True,
        "app": "xieyin",
        "version": VERSION,
        "port": int(conf.get("port") or 22668),
        "media_root": conf.get("media_root") or detect_default_root(),
        "proxy": conf.get("proxy") or "",
        "fnos": {**{k: v for k, v in (conf.get("fnos") or {}).items() if k != "token"},
                 "has_token": bool((conf.get("fnos") or {}).get("token"))},
        "has_auth_key": bool(conf.get("auth_key") or ""),
        "has_translate_key": bool(os.path.exists(TRANSLATE_KEY_PATH)
                                  and os.path.getsize(TRANSLATE_KEY_PATH) > 0),
        "jav": conf.get("jav") or {},
        "running": running,
        "jobs": jobs,
        "roots": roots,
        "auto": auto_status(),
        "deps": {
            "python": sys.version.split()[0],
            "ffprobe": shutil.which("ffprobe") or "",
            "ffmpeg": shutil.which("ffmpeg") or "",
            "curl": shutil.which("curl") or "",
            "pillow": pillow,
        },
        "paths": {"data": DATA_DIR, "logs": LOG_DIR, "scripts": SCRIPTS},
        "server_time": time.time(),
    }


def auto_status() -> dict:
    conf = load_conf().get("auto", {})
    snap = _watcher.status() if _watcher else {}
    return {"config": conf, "status": snap}


# --------------------------------------------------------------------- HTTP

def json_bytes(obj) -> bytes:
    return json.dumps(obj, ensure_ascii=False).encode("utf-8")


class Handler(BaseHTTPRequestHandler):
    server_version = "xieyin/" + VERSION
    protocol_version = "HTTP/1.1"

    def log_message(self, fmt: str, *args) -> None:
        # 不打每个请求，避免日志被轮询刷满
        pass

    # -- 工具 ---------------------------------------------------------
    def _send(self, code: int, body: bytes, ctype: str = "application/json; charset=utf-8",
              extra: dict | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", ctype)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        for k, v in (extra or {}).items():
            self.send_header(k, v)
        self.end_headers()
        try:
            self.wfile.write(body)
        except (BrokenPipeError, ConnectionResetError):
            pass

    def _json(self, obj, code: int = 200) -> None:
        self._send(code, json_bytes(obj))

    def _err(self, msg: str, code: int = 400) -> None:
        self._json({"ok": False, "error": msg}, code)

    def _read_json(self) -> dict:
        try:
            n = int(self.headers.get("Content-Length") or 0)
        except ValueError:
            n = 0
        raw = self.rfile.read(n) if n else b"{}"
        try:
            data = json.loads(raw.decode("utf-8"))
            return data if isinstance(data, dict) else {}
        except Exception:
            return {}

    def _static(self, rel: str) -> None:
        rel = rel.lstrip("/")
        path = os.path.realpath(os.path.join(WWW, rel))
        if not path.startswith(os.path.realpath(WWW) + os.sep) or not os.path.isfile(path):
            self._err("not found", 404)
            return
        ext = os.path.splitext(path)[1].lower()
        ctype = {
            ".html": "text/html; charset=utf-8",
            ".css": "text/css; charset=utf-8",
            ".js": "application/javascript; charset=utf-8",
            ".png": "image/png",
            ".svg": "image/svg+xml",
            ".ico": "image/x-icon",
        }.get(ext, "application/octet-stream")
        with open(path, "rb") as f:
            self._send(200, f.read(), ctype)

    # -- GET ----------------------------------------------------------
    def do_GET(self) -> None:
        parsed = urllib.parse.urlsplit(self.path)
        path = urllib.parse.unquote(parsed.path)
        qs = urllib.parse.parse_qs(parsed.query)

        if path in ("/", "/index.html"):
            self._static("index.html")
        elif path == "/favicon.ico":
            self._static("favicon.ico")
        elif path == "/icon.png":
            self._static("icon.png")
        elif path == "/api/status":
            self._json(status_dict())
        elif path == "/api/auto":
            self._json({"ok": True, **auto_status()})
        elif path == "/api/tcache":
            self._tcache_get()
        elif path == "/api/browse":
            self._json(self._browse(qs.get("path", [""])[0]))
        elif path.startswith("/api/jobs/"):
            self._job_get(path, qs)
        else:
            self._err("not found", 404)

    def do_POST(self) -> None:
        path = urllib.parse.unquote(urllib.parse.urlsplit(self.path).path)
        if path == "/api/config":
            self._config_save()
        elif path == "/api/translate/test":
            self._translate_test()
        elif path == "/api/tcache":
            self._tcache_clear()
        elif path == "/api/auto":
            self._auto_save()
        elif path == "/api/jobs":
            self._job_start()
        elif path.startswith("/api/jobs/") and path.endswith("/stop"):
            self._job_stop(path)
        else:
            self._err("not found", 404)

    # -- API ----------------------------------------------------------
    def _browse(self, raw: str) -> dict:
        try:
            path = require_dir(raw) if raw else "/"
        except ValueError as e:
            return {"ok": False, "error": str(e)}
        if path == "/":
            return {
                "ok": True, "path": "/", "parent": "",
                "entries": [{"name": r["path"], "path": r["path"]} for r in known_roots()
                            if r["exists"]],
            }
        if any(path == p or path.startswith(p + "/") for p in ("/proc", "/sys", "/dev", "/run")):
            return {"ok": False, "error": "不允许浏览这个目录"}
        entries = []
        try:
            names = sorted(os.listdir(path), key=lambda s: s.lower())
        except OSError as e:
            return {"ok": False, "error": f"读不了目录: {e}"}
        for name in names:
            if name.startswith("."):
                continue
            p = os.path.join(path, name)
            try:
                if os.path.isdir(p):
                    entries.append({"name": name, "path": p})
            except OSError:
                continue
        parent = os.path.dirname(path.rstrip("/")) or "/"
        return {"ok": True, "path": path, "parent": parent, "entries": entries[:800]}

    def _translate_test(self) -> None:
        data = self._read_json()
        title = str(data.get("title") or "").strip()[:300]
        if not title:
            return self._err("请输入测试标题")
        conf = load_conf()
        jc = conf.get("jav") or {}
        env = os.environ.copy()
        env["XIEYIN_DATA"] = DATA_DIR
        env["XIEYIN_PLOT_ZH"] = "1"
        for key, var in (("translate_provider", "XIEYIN_TRANSLATE_PROVIDER"),
                         ("translate_model", "XIEYIN_LLM_MODEL"),
                         ("translate_api_base", "XIEYIN_LLM_BASE"),
                         ("translate_max_tokens", "XIEYIN_LLM_MAX_TOKENS"),
                         ("translate_temperature", "XIEYIN_LLM_TEMPERATURE")):
            if jc.get(key) not in (None, ""):
                env[var] = str(jc[key])
        cmd = [PYTHON, os.path.join(SCRIPTS, "test_llm.py"),
               "--title", title, "--code", "TEST-001"]
        try:
            cp = subprocess.run(cmd, cwd=APP_DIR, env=env, capture_output=True,
                                text=True, timeout=150, errors="replace")
        except subprocess.TimeoutExpired:
            return self._err("测试超时（150 秒）", 504)
        raw = (cp.stdout or "").strip()
        line = raw.splitlines()[-1] if raw else ""
        try:
            out = json.loads(line)
        except Exception:
            return self._err("测试失败：" + ((cp.stderr or raw or "无输出")[-500:]), 502)
        self._json(out)

    def _tcache_get(self) -> None:
        try:
            self._json({"ok": True, "count": translate_zh.cache_count()})
        except Exception as e:
            self._err(f"读取翻译缓存失败: {e}", 500)

    def _tcache_clear(self) -> None:
        try:
            n = translate_zh.cache_clear()
            log("翻译缓存已清除（%d 条）" % n)
            self._json({"ok": True, "cleared": n, "count": 0})
        except Exception as e:
            self._err(f"清除翻译缓存失败: {e}", 500)

    def _config_save(self) -> None:
        global _fnos_token_backup
        data = self._read_json()
        conf = load_conf()
        _fnos_token_backup = str((conf.get("fnos") or {}).get("token") or "")
        if "media_root" in data:
            try:
                conf["media_root"] = require_dir(str(data.get("media_root") or ""))
            except ValueError as e:
                return self._err(str(e))
        if "proxy" in data:
            conf["proxy"] = str(data.get("proxy") or "").strip()
        if "auth_key" in data:
            conf["auth_key"] = str(data.get("auth_key") or "").strip()
        for key in ("jav", "tmdb", "fnos"):
            if isinstance(data.get(key), dict):
                _merge(conf[key], data[key])
        # 访问令牌脱敏输入框语义：留空 = 保留旧值
        if isinstance(data.get("fnos"), dict) and "token" in data["fnos"] \
                and not str(data["fnos"].get("token") or "").strip():
            conf.setdefault("fnos", {})["token"] = _fnos_token_backup
        key = data.get("translate_api_key")
        if key is not None:
            key = str(key).strip()
            if key:
                try:
                    os.makedirs(DATA_DIR, exist_ok=True)
                    tmp = TRANSLATE_KEY_PATH + ".tmp"
                    with open(tmp, "w", encoding="utf-8") as f:
                        f.write(key + "\n")
                    os.chmod(tmp, 0o600)
                    os.replace(tmp, TRANSLATE_KEY_PATH)
                except OSError as e:
                    return self._err(f"保存翻译 Key 失败: {e}", 500)
            elif data.get("translate_clear_key"):
                try:
                    os.remove(TRANSLATE_KEY_PATH)
                except FileNotFoundError:
                    pass
        try:
            save_conf(conf)
        except OSError as e:
            return self._err(f"保存失败: {e}", 500)
        self._json({"ok": True, "config": conf})

    def _auto_save(self) -> None:
        data = self._read_json()
        conf = load_conf()
        auto = conf.setdefault("auto", {})
        for key in ("enabled", "scan_enabled", "qbit_enabled", "fnos_download_enabled", "skip_pt", "no_images", "force", "force_images", "no_probe", "plot_zh", "actor_sync", "fnos_sync", "clean_enabled"):
            if key in data:
                auto[key] = bool(data.get(key))
        for key in ("scan_interval_minutes", "qbit_poll_seconds", "fnos_download_poll_seconds", "settle_minutes"):
            if key in data:
                try:
                    auto[key] = max(0, int(data.get(key)))
                except (TypeError, ValueError):
                    return self._err("%s 必须是整数" % key)
        for key in ("scan_root", "qbit_url", "qbit_username", "qbit_password", "via", "image_policy", "fnos_download_db"):
            if key in data:
                auto[key] = str(data.get(key) or "").strip()
        try:
            save_conf(conf)
        except OSError as e:
            return self._err(f"保存失败: {e}", 500)
        self._json({"ok": True, **auto_status()})

    def _job_start(self) -> None:
        data = self._read_json()
        try:
            kind, title, cmd, env, meta = build_job(data)
            job = start_job(kind, title, cmd, env=env, meta=meta)
        except RuntimeError as e:
            return self._err(str(e), 409)
        except ValueError as e:
            return self._err(str(e))
        except Exception as e:
            return self._err(f"启动失败: {e}", 500)
        self._json({"ok": True, "job": job.snapshot(0, 0)})

    def _job_get(self, path: str, qs: dict) -> None:
        rest = path[len("/api/jobs/"):]
        if rest.endswith("/log"):
            job = job_by_id(rest[:-4].strip("/"))
            if not job:
                return self._err("任务不存在", 404)
            try:
                with open(job.log_path, "rb") as f:
                    body = f.read()
            except OSError:
                body = b""
            return self._send(200, body, "text/plain; charset=utf-8", {
                "Content-Disposition": f'attachment; filename="{job.id}-{job.kind}.log"'})
        job = job_by_id(rest.strip("/"))
        if not job:
            return self._err("任务不存在", 404)
        try:
            offset = int(qs.get("offset", ["0"])[0])
        except ValueError:
            offset = 0
        self._json({"ok": True, "job": job.snapshot(max(0, offset))})

    def _job_stop(self, path: str) -> None:
        rest = path[len("/api/jobs/"):]
        job_id = rest[:-len("/stop")].strip("/")
        job = job_by_id(job_id)
        if not job:
            return self._err("任务不存在", 404)
        job.stop()
        self._json({"ok": True})


# --------------------------------------------------------------------- main

def main() -> int:
    cmd = sys.argv[1] if len(sys.argv) > 1 else "serve"
    conf = load_conf()
    if cmd == "status":
        d = status_dict()
        d.pop("jobs", None)
        print(json.dumps(d, ensure_ascii=False, indent=2))
        return 0
    if cmd != "serve":
        print(__doc__)
        return 2

    os.makedirs(LOG_DIR, exist_ok=True)
    save_conf(conf)
    bind = str(conf.get("bind") or "0.0.0.0")
    port = int(conf.get("port") or 22668)

    global _watcher
    _watcher = auto_watch.AutoWatcher(
        conf_get=load_conf,
        state_path=os.path.join(DATA_DIR, "auto_qbit_state.json"),
        trigger=auto_trigger,
        log=log)
    _watcher.start()

    httpd = ThreadingHTTPServer((bind, port), Handler)
    httpd.daemon_threads = True
    log(f"撷引（xieyin） v{VERSION} 监听 {bind}:{port}，数据目录 {DATA_DIR}")
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        if _watcher:
            _watcher.stop()
        with _jobs_lock:
            for j in _jobs:
                if j.status == "running":
                    j.stop()
        httpd.server_close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
