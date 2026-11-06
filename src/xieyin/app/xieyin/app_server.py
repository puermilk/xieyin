#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""撷引（xieyin）—— fnOS 应用服务端（纯 Python 标准库）。

把原来 Docker 版里的四个脚本包成一个常驻 Web 服务：
  jav_scrape.py   番号 AV 刮削（JavBus）
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

VERSION = "1.2.0"
APP_DIR = os.path.dirname(os.path.realpath(__file__))
DATA_DIR = os.environ.get("XIEYIN_DATA") or os.path.join(APP_DIR, "data")
CONF_PATH = os.path.join(DATA_DIR, "config.json")
LOG_DIR = os.path.join(DATA_DIR, "logs")
SCRIPTS = os.path.join(APP_DIR, "scripts")
WWW = os.path.join(APP_DIR, "www")
PYTHON = sys.executable or "/usr/bin/python3"

sys.path.insert(0, SCRIPTS)
import auto_watch  # noqa: E402

DEFAULT_ROOTS = [
    "/vol1/1000/Media",
]

DEFAULT_CONF = {
    "port": 22668,
    "bind": "0.0.0.0",
    "media_root": "",
    "proxy": "",
    "jav": {"via": "auto", "recursive": True},
    "tmdb": {},
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
        "skip_pt": True,           # PT 保护：跳过 @GROUP / *.torrent 目录
        "no_images": False,
        "via": "auto",
    },
}

VIDEO_EXT = (".mp4", ".mkv", ".avi", ".ts", ".mov", ".wmv")

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
        pt_guard = bool(data.get("pt_guard", True))
        env["XIEYIN_BASE"] = root
        env["XIEYIN_PT_GUARD"] = "1" if pt_guard else "0"
        env["XIEYIN_KEEP_SIDECARS"] = "1"
        title = "清理共享 · " + ("执行" if mode == "run" else "预览")
        if pt_guard:
            title += "（PT 保护）"
        return ("clean", title,
                ["bash", os.path.join(SCRIPTS, "clean_qh.sh"), mode], env,
                {"root": root, "mode": mode, "pt_guard": pt_guard})

    if kind == "jav":
        dry = bool(data.get("dry"))
        cmd = [PYTHON, os.path.join(SCRIPTS, "jav_scrape.py"),
               "--log", os.path.join(LOG_DIR, "jav_scrape.log"),
               "--via", str(data.get("via") or "auto")]
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
        if data.get("force"):
            cmd.append("--force")
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
                 "via": str(data.get("via") or "auto")})

    if kind == "tmdb":
        dry = bool(data.get("dry"))
        folder = str(data.get("folder") or "").strip()
        tmdb_id = re.sub(r"\D", "", str(data.get("tmdb_id") or ""))
        query = str(data.get("query") or "").strip()
        cmd = [PYTHON, os.path.join(SCRIPTS, "tmdb_scrape.py"), "--root", root]
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
        if data.get("no_images"):
            cmd.append("--no-images")
        if no_probe:
            cmd.append("--no-probe")
        title = "电影刮削 · " + ("预览" if dry else "开始")
        return ("tmdb", title, cmd, env,
                {"root": root, "folder": folder, "dry": dry,
                 "tmdb_id": tmdb_id, "query": query,
                 "force": bool(data.get("force"))})

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
        elif path == "/api/status":
            self._json(status_dict())
        elif path == "/api/auto":
            self._json({"ok": True, **auto_status()})
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

    def _config_save(self) -> None:
        data = self._read_json()
        conf = load_conf()
        if "media_root" in data:
            try:
                conf["media_root"] = require_dir(str(data.get("media_root") or ""))
            except ValueError as e:
                return self._err(str(e))
        if "proxy" in data:
            conf["proxy"] = str(data.get("proxy") or "").strip()
        for key in ("jav", "tmdb"):
            if isinstance(data.get(key), dict):
                _merge(conf[key], data[key])
        try:
            save_conf(conf)
        except OSError as e:
            return self._err(f"保存失败: {e}", 500)
        self._json({"ok": True, "config": conf})

    def _auto_save(self) -> None:
        data = self._read_json()
        conf = load_conf()
        auto = conf.setdefault("auto", {})
        for key in ("enabled", "scan_enabled", "qbit_enabled", "skip_pt", "no_images"):
            if key in data:
                auto[key] = bool(data.get(key))
        for key in ("scan_interval_minutes", "qbit_poll_seconds", "settle_minutes"):
            if key in data:
                try:
                    auto[key] = max(0, int(data.get(key)))
                except (TypeError, ValueError):
                    return self._err("%s 必须是整数" % key)
        for key in ("qbit_url", "qbit_username", "qbit_password", "via"):
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
