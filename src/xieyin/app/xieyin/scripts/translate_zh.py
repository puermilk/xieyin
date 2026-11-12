#!/usr/bin/env python3
# -*- coding: utf-8 -*-
# SPDX-License-Identifier: GPL-3.0-or-later
# Copyright (C) 2026 xieyin contributors
"""日文标题 -> 简体中文剧情（可选功能，Bing 网页端点直连，零成本）。

设计原则：
  * 默认关闭，只有 XIEYIN_PLOT_ZH=1/true/yes 时才翻译，避免改变现有刮削行为。
  * 只用 cn.bing.com/ttranslatev3 网页端点（上一轮实测直连可用、均分 1.083）。
  * 术语表走「回包矫正」（方案 B）：正常翻译，回包后按 wrong 列表把
    義父→公公 / 義理の息子→继子 等关系词纠回，把术语命中从基线 7/20 拉到 ≥18/20。
  * 打码字 ●/〇 用确定性还原表还原后再送翻；表中没有的一律不猜、判失败。
  * 译文过质量门才写缓存；任何失败返回空串，由 jav_scrape 保留日文原 plot。
  * 缓存 key = sha256("bing|" + 术语表版本 + "|" + 规范化原文)，TTL 30 天。
"""
from __future__ import annotations

import hashlib
import html
import json
import os
import re
import sys
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid

UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
      "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")
BING_TRANSLATOR = "https://cn.bing.com/translator"
BING_TRANSLATE = "https://cn.bing.com/ttranslatev3"
BING_IID = "translator.5023.1"
TIMEOUT = 4                 # 单次翻译请求超时（B3：8s -> 4s，单条最坏 2×4s+0.5s≈8.5s）
TOKEN_TIMEOUT = 8           # token 页面抓取超时（保持）
RETRY_BACKOFF = 0.5         # 429/5xx/超时 重试退避
CACHE_TTL = 30 * 86400      # 缓存 30 天
TOKEN_TTL = 50 * 60         # token 有效约 1 小时，留 10 分钟余量

# ---- LLM provider（OpenAI 兼容）----
PROVIDER = os.environ.get("XIEYIN_TRANSLATE_PROVIDER", "auto").strip().lower() or "auto"
LLM_BASE = (os.environ.get("XIEYIN_LLM_BASE") or "https://opencode.ai/zen/go/v1").rstrip("/")
LLM_MODEL = os.environ.get("XIEYIN_LLM_MODEL") or "deepseek-v4-pro"
def _env_num(name: str, default, cast=int):
    try:
        return cast(os.environ.get(name) or default)
    except (TypeError, ValueError):
        return default


LLM_TIMEOUT = 120
LLM_MAX_TOKENS = _env_num("XIEYIN_LLM_MAX_TOKENS", 1600)
LLM_TEMPERATURE = _env_num("XIEYIN_LLM_TEMPERATURE", 0.0, float)
LLM_REASONING = (os.environ.get("XIEYIN_LLM_REASONING") or "").strip()
LLM_CONCURRENCY = _env_num("XIEYIN_LLM_CONCURRENCY", 2)
# GLM 对母子/露骨题材容易拒答；默认用 DeepSeek-V4-Pro 兜底补一次文案。
LLM_FALLBACK_MODEL = (os.environ.get("XIEYIN_LLM_FALLBACK_MODEL") or
                      ("DeepSeek-V4-Pro" if LLM_MODEL == "GLM-5.3-Flash" else ""))
PROMPT_VERSION = "2026-11-12.4"
MIN_PLOT_CHARS = 120
MIN_PLOT_CHARS_RETRY = 100
_LLM_LOCK = threading.Lock()
_LLM_SEM = threading.Semaphore(max(1, LLM_CONCURRENCY))
_LLM_FAIL_STREAK = 0
_LLM_BREAKER_UNTIL = 0.0
_LLM_BREAKER_WARNED = False

KANA_RE = re.compile(r"[\u3040-\u30ff]")
ASCII_RE = re.compile(r"[A-Za-z]{3,}")
MASK_RE = re.compile(r"[●〇○◯]")
# 型号白名单：ABC-123 / IPZZ542 这类番号不算「混入英文」
MODEL_CODE_RE = re.compile(r"[A-Z]{2,6}-\d{2,5}|[A-Z]{2,6}\d{2,5}")

# 打码字确定性还原表（覆盖基准与实测库里出现的写法；表中没有的不猜、判失败）
MASK_TABLE = [
    ("舐め●す", "舐め回す"),
    ("犯●れ", "犯され"),
    ("おち〇ちん", "おちんちん"),
    ("チ●ポ", "チンポ"),
    ("チ○ポ", "チンポ"),
    ("オマ〇コ", "オマンコ"),
    ("マ〇コ", "マンコ"),
    ("夜●い", "夜這い"),
    ("レ●プ", "レイプ"),
    ("痴●", "痴漢"),
    ("輪●", "輪姦"),
    ("性奴●", "性奴"),
    ("おま●こ", "おまんこ"),
]

# 译文里允许出现的拉丁缩写（AV 术语，不算「混英文」）
LATIN_ALLOW = {"NTR", "SM", "OL", "VR", "SEX", "BDSM", "3P", "AV", "HD", "FHD", "DVD", "BD",
               "SHOTA", "MAMA", "LOLI",   # ママショタ/ロリ 等题材词常被音译成英文
               "Madonna", "HEYZO", "FANZA", "DMM", "MOODYZ", "S1", "SOD",
               "PRESTIGE", "IDEA", "POCKET", "ATTACKERS", "OPPAI", "WANZ",
               "KMP", "EBODY", "MAXA", "JAV"}

# ---- 术语表（进程内缓存，随包分发 data/glossary_ja_zh.json）----
_GLOSSARY: list | None = None
_GLOSSARY_VERSION = ""
_GLOSSARY_LOCK = threading.Lock()

# ---- 缓存 ----
_CACHE_LOCK = threading.Lock()
_CACHE: dict | None = None
_CACHE_LOADED = False

# ---- Bing token 进程内缓存 + 熔断 ----
_TOKEN = {"ig": "", "key": "", "token": "", "ts": 0.0}
_TOKEN_LOCK = threading.Lock()
_NET_LOCK = threading.Lock()      # 序列化网络调用：并发共用同一 token 会被 Bing 判失效
_FAIL_STREAK = 0
_BREAKER_UNTIL = 0.0              # 熔断冷却截止时间（到点自动半开重试，不再永久关闭）
_BREAKER_WARNED = False

# 最近一次失败 (code, reason)，供 jav_scrape 写 WARN 日志
_LAST_ERROR: tuple[str, str] = ("", "")


def enabled() -> bool:
    return os.environ.get("XIEYIN_PLOT_ZH", "").strip().lower() in ("1", "true", "yes", "on")


def last_error() -> tuple[str, str]:
    """最近一次翻译失败的 (code, reason)，供调用方写 WARN 日志。"""
    return _LAST_ERROR


def _log(msg: str) -> None:
    print(msg, file=sys.stderr, flush=True)


def _data_dir() -> str:
    return os.environ.get("XIEYIN_DATA") or os.path.join(
        os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "data")


def _cache_path() -> str:
    return os.path.join(_data_dir(), "translate_cache.json")


def _glossary_path() -> str:
    # B1：术语表随包分发放在 app 根目录（build_fpk 的 excluded() 会排除 data/ 目录，
    # 放 data/ 里打不进 app.tgz）。app 根目录优先，data 目录兜底（兼容旧布局）。
    app = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    p = os.path.join(app, "glossary_ja_zh.json")
    if os.path.exists(p):
        return p
    return os.path.join(_data_dir(), "glossary_ja_zh.json")


# ---------------------------------------------------------------- 术语表
def load_glossary() -> list:
    global _GLOSSARY, _GLOSSARY_VERSION
    if _GLOSSARY is not None:
        return _GLOSSARY
    with _GLOSSARY_LOCK:
        if _GLOSSARY is not None:
            return _GLOSSARY
        try:
            with open(_glossary_path(), encoding="utf-8") as f:
                data = json.load(f)
            _GLOSSARY_VERSION = str(data.get("version", ""))
            terms = [t for t in data.get("terms", []) if t.get("ja") and t.get("zh")]
            # 长词优先，避免「義理の母」被「義母」先吃掉
            _GLOSSARY = sorted(terms, key=lambda t: len(t["ja"]), reverse=True)
        except Exception:
            _GLOSSARY_VERSION = ""
            _GLOSSARY = []
    return _GLOSSARY


def glossary_version() -> str:
    load_glossary()
    return _GLOSSARY_VERSION


# ---------------------------------------------------------------- 打码字
_MASK_CANON = str.maketrans({"○": "●", "〇": "●", "◯": "●"})

def _restore_mask(text: str) -> str:
    """把 ●○〇◯ 统一成 ● 再还原，避免「チ〇ポ」这类不同圈号漏映射。"""
    text = text.translate(_MASK_CANON)
    for a, b in MASK_TABLE:
        text = text.replace(a.translate(_MASK_CANON), b)
    return text


# ---------------------------------------------------------------- 缓存
def _load_cache() -> dict:
    global _CACHE, _CACHE_LOADED
    if _CACHE_LOADED:
        return _CACHE
    _CACHE_LOADED = True
    empty = {"version": 2, "ts": time.time(), "entries": {}}
    try:
        with open(_cache_path(), encoding="utf-8") as f:
            data = json.load(f)
    except Exception:
        _CACHE = empty
        return _CACHE
    if isinstance(data, dict) and "entries" in data and isinstance(data.get("entries"), dict):
        _CACHE = {"version": 2, "ts": float(data.get("ts") or time.time()),
                  "entries": data["entries"]}
    else:
        # 旧 MyMemory 扁平缓存（sha1 key -> 译文）：不报错、不复用，静默重建
        _CACHE = empty
    return _CACHE


def _save_cache() -> None:
    if _CACHE is None:
        return
    try:
        d = _data_dir()
        os.makedirs(d, exist_ok=True)
        tmp = _cache_path() + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_CACHE, f, ensure_ascii=False)
        os.replace(tmp, _cache_path())
    except Exception:
        pass


def _cache_key(src: str, provider: str = "bing", model: str = "") -> str:
    if provider == "bing" and not model:
        # 保持旧 Bing 缓存 key 不变，避免已有的 100+ 条缓存全部失效
        return hashlib.sha256(("bing|" + glossary_version() + "|" + src).encode("utf-8")).hexdigest()
    raw = "%s|%s|%s|%s|%s" % (provider, model, PROMPT_VERSION, glossary_version(), src)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _cache_get(key: str) -> str:
    with _CACHE_LOCK:
        c = _load_cache()
        ent = c.get("entries", {}).get(key)
        if not ent:
            return ""
        ts = float(ent.get("ts") or 0)
        if time.time() - ts > CACHE_TTL:
            return ""
        return ent.get("zh", "")


def _cache_set(key: str, zh: str) -> None:
    with _CACHE_LOCK:
        c = _load_cache()
        c["entries"][key] = {"zh": zh, "ts": time.time()}
        c["ts"] = time.time()
        _save_cache()


def cache_count() -> int:
    with _CACHE_LOCK:
        return len(_load_cache().get("entries", {}))


def cache_clear() -> int:
    global _CACHE, _CACHE_LOADED
    with _CACHE_LOCK:
        n = len(_load_cache().get("entries", {}))
        _CACHE = {"version": 2, "ts": time.time(), "entries": {}}
        _CACHE_LOADED = True
        _save_cache()
    return n


# ---------------------------------------------------------------- Bing 接入
def _fetch_token(force: bool = False) -> dict:
    """取 translator 页面里的 IG / key / token，进程内缓存（约 1 小时）。"""
    global _TOKEN
    with _TOKEN_LOCK:
        now = time.time()
        if not force and _TOKEN["token"] and (now - _TOKEN["ts"]) < TOKEN_TTL:
            return _TOKEN
        req = urllib.request.Request(BING_TRANSLATOR, headers={"User-Agent": UA})
        with urllib.request.urlopen(req, timeout=TOKEN_TIMEOUT) as r:
            page = r.read().decode("utf-8", "replace")
        ig = re.search(r'IG:"([^"]+)"', page)
        m = re.search(r'params_AbusePreventionHelper\s*=\s*\[(\d+),"([^"]+)",(\d+)\]', page)
        if not ig or not m:
            raise RuntimeError("token_parse")
        _TOKEN = {"ig": ig.group(1), "key": m.group(1), "token": m.group(2), "ts": now}
        return _TOKEN


def _call_bing(text: str, tok: dict) -> str:
    """POST ttranslatev3，返回译文；异常上抛（HTTPError/URLError/ValueError）。"""
    data = urllib.parse.urlencode({
        "fromLang": "ja", "to": "zh-Hans",
        "text": text, "token": tok["token"], "key": tok["key"],
    }).encode()
    url = (f"{BING_TRANSLATE}?isVertical=1&IG={urllib.parse.quote(tok['ig'])}"
           f"&IID={BING_IID}")
    req = urllib.request.Request(url, data=data, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
        "Referer": BING_TRANSLATOR,
        "Origin": "https://cn.bing.com",
    })
    with urllib.request.urlopen(req, timeout=TIMEOUT) as r:
        body = json.loads(r.read().decode("utf-8", "replace"))
    seg = body[0]["translations"][0]["text"]
    if not isinstance(seg, str) or not seg:
        raise ValueError("parse")
    return seg


def _translate_net(text: str) -> tuple[str, str]:
    """网络层：token 复用 + 失效刷新 + 一次重试。返回 (译文, errcode)。

    用 _NET_LOCK 串行化：jobs>1 时两个线程同时拿同一个 token 打 Bing，
    实测会让 token 提前失效（TASK 批量 81 条时 51 条翻译被打回日文）。
    """
    with _NET_LOCK:
        try:
            tok = _fetch_token()
        except Exception:
            return "", "token_fetch"

        for attempt in (1, 2):
            try:
                return _call_bing(text, tok), ""
            except urllib.error.HTTPError as e:
                if e.code in (401, 403):
                    # token 过期：重新取一次再试；再失败就返回
                    if attempt == 1:
                        try:
                            tok = _fetch_token(force=True)
                        except Exception:
                            return "", "token_refresh"
                        continue
                    return "", "token_expired"
                if e.code == 429 or e.code >= 500:
                    if attempt == 1:
                        time.sleep(RETRY_BACKOFF)
                        continue
                    return "", "http_%d" % e.code
                return "", "http_%d" % e.code
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt == 1:
                    time.sleep(RETRY_BACKOFF)
                    continue
                return "", "timeout"
            except Exception:
                return "", "parse"
    return "", "http"


# ---------------------------------------------------------------- LLM provider
LLM_SYSTEM_PROMPT = """你是日本成人影片标题的中文编辑。只输出简体中文标题一行，禁止输出“番号、标题、译文”等标签，禁止解释。
要求：写成像海报标题，而不是直译。简洁有力，一般 18~45 字；保留人物关系、关键设定和数字；优先用动词、冲突和留白制造张力；不要写成剧情简介，不要展开细节；不要出现演员名、番号、发行日期；不写具体器官和粗俗词，用关系与场景替代；结尾不加句号。
示例语气：不是「义父允许插入十秒」，而是「答应公公只插入10秒，却因太合拍而高潮的我」。
术语：義父/お義父さん/義理の父→公公；義理の息子→继子；義理の母/お義母さん/義母→继母；叔母→阿姨；人妻→人妻；中出し→内射；寝取られ→NTR；寝取り→睡走；種付けプレス→按住播种内射；犯す→侵犯；舐め回す→舔遍；絶倫→精力绝伦；絶頂→高潮；絶頂寸前→高潮前夕；連続絶頂→连续高潮；イカされる→被弄到高潮；イカせる→弄到高潮；潮吹き→潮吹；痙攣→痉挛；寸止め→寸止；焦らし→挑逗；追撃→连续追击；抜かず→不拔出；快楽堕ち→沉溺快感；事後→事后；余韻→余韵；生ハメ→无套插入；生中出し→无套内射；危険日→危险期；孕ませ→搞大肚子；種付け→播种；妊娠→怀孕；未亡人→寡妇；痴漢→痴汉；調教→调教；奴隷→奴隶；緊縛→紧缚；近親相姦→近亲乱伦。
●〇是打码，按上下文还原后再翻，不要保留占位符。寝取られ/寝取り 不要机械写「NTR」，也不要写得过于直白；主语明确时用留白、有张力的自然中文，例如「继子与妻子，越过了那条线」。
示例：
日文：今日から3日間、親友に妻を貸し出します―。
标题：从今天起三天，把妻子借给亲友
日文：義父に10秒だけの約束で挿入を許したら…相性抜群過ぎて絶頂してしまった私。
标题：答应公公只插入10秒，却因太合拍而高潮的我
日文：愛を確かめたくて妻と絶倫の後輩を2人きりにして3時間…抜かずの追撃中出し計16発で妻を奪われた僕のNTR話
标题：让妻子与精力绝伦的后辈独处三小时，十六发内射后她被夺走"""

META_LEAK_RE = re.compile(r"不能有占位符|重写：|标题：|译文：|请提供|素材|占位符|作为 ?AI|我不能|无法提供")

PLOT_REFUSAL_RE = re.compile(
    r"我不能|无法(?:提供|满足|协助)|抱歉|不涉及|只要不涉及|需要的话|请把|"
    r"作为 ?AI|合规|不建议|我可以按|我都可以按|你给的风格|术语规范|两情相悦故事|剧情简介）|题材（")

PLOT_SYSTEM_PROMPT = """你是中文成人影片预告文案编辑。只输出一段简体中文剧情简介，不要标题、序号、解释、引号或「文案/简介」标签。
要求：
- 长度 140~220 字，3~5 句，短句为主。像预告片旁白，不要复述标题，不要写成剧情说明书。
- 第一句直接进入场景或关系，不写「故事讲述」「本片」这种开场。
- 保留人物关系、关键设定和数字，不新增原文没有的人物或事件。
- 风格：暧昧、撩人、留白、强张力；多用感官、心理、空间细节；结尾用一个动作、半句话或问题留钩子。
- 不写具体器官和粗俗词；用关系错位、时间、声音、眼神、动作制造张力。
- 专业术语严格：義父/お義父さん→公公；義理の息子→继子；義理の母/お義母さん→继母；人妻→人妻；中出し→内射；寝取られ→NTR；寝取り→睡走；絶頂→高潮；絶倫→精力绝伦；種付けプレス→按住播种内射；犯す→侵犯；舐め回す→舔遍；潮吹き→潮吹；寸止め→寸止；焦らし→挑逗。
- 寝取られ/寝取り 不要机械写「NTR」，也不要过于直白；主语明确时用「继子与妻子，越过了那条线」这类留白表达。
- 不要提及时长、发行日期、番号、演员名等元数据，这些由外部字段展示。
- 不要输出任何解释、免责声明、改写建议或「我不能」；直接给成品文案。"""


def _llm_key() -> str:
    key = (os.environ.get("XIEYIN_LLM_API_KEY") or os.environ.get("OPENCODE_GO_API_KEY") or "").strip()
    if key:
        return key
    for p in (os.path.join(_data_dir(), "translate_key"),
              "/usr/local/xieyin/data/translate_key"):
        try:
            k = open(p, encoding="utf-8").read().strip()
            if k:
                return k
        except Exception:
            pass
    return ""


def _llm_headers(key: str) -> dict:
    return {
        "Content-Type": "application/json",
        "Authorization": "Bearer " + key,
        "x-opencode-session": str(uuid.uuid4()),
        "User-Agent": UA,
    }


def _glossary_hits(text: str, limit: int = 60) -> list:
    """按最长匹配挑出术语，避免「義父→公公」和「父→父亲」同时出现在提示词里。"""
    used = [False] * len(text)
    out = []
    for g in load_glossary():
        ja = g.get("ja") or ""
        if not ja or g.get("soft"):
            continue
        if g.get("zh") == ja and any(ch.isdigit() for ch in ja):
            continue
        matched = False
        start = text.find(ja)
        while start != -1:
            end = start + len(ja)
            if not any(used[start:end]):
                matched = True
                for i in range(start, end):
                    used[i] = True
            start = text.find(ja, start + 1)
        if matched:
            out.append(g)
        if len(out) >= limit:
            break
    return out


def _llm_user_prompt(text: str, ctx: dict | None) -> str:
    ctx = ctx or {}
    lines = []
    if ctx.get("code"):
        lines.append("番号：" + str(ctx["code"]))
    actors = [a for a in (ctx.get("actors") or []) if isinstance(a, str) and a.strip()]
    if actors:
        lines.append("演员：" + "、".join(actors[:6]))
    if ctx.get("studio"):
        lines.append("厂牌：" + str(ctx["studio"]))
    hits = _glossary_hits(text)
    if hits:
        lines.append("必须使用术语：" + "；".join(
            "%s→%s" % (g["ja"], g.get("prompt") or g["zh"]) for g in hits))
    lines.append("日文标题：" + text)
    return "\n".join(lines)


def _clean_llm(zh: str, ctx: dict | None = None) -> str:
    zh = (zh or "").strip()
    zh = re.sub(r"^\s*(番号|标题|日文标题|译文|中文|翻译)\s*[:：]\s*", "", zh)
    code = str((ctx or {}).get("code") or "").strip()
    if code:
        zh = re.sub(r"^\s*" + re.escape(code) + r"\s*[:：\-]?\s*", "", zh, flags=re.I)
    zh = zh.strip()
    for a, b in (("“", "”"), ('"', '"'), ("'", "'"), ("「", "」"), ("『", "』")):
        if len(zh) >= 2 and zh.startswith(a) and zh.endswith(b):
            zh = zh[1:-1].strip()
            break
    zh = re.sub(r"\s+", " ", zh).strip()
    return zh


def _llm_translate(text: str, ctx: dict | None = None,
                   system: str | None = None, user: str | None = None,
                   model: str | None = None) -> tuple[str, str]:
    global _LLM_FAIL_STREAK, _LLM_BREAKER_UNTIL, _LLM_BREAKER_WARNED
    key = _llm_key()
    if not key:
        return "", "no_key"
    if time.time() < _LLM_BREAKER_UNTIL:
        return "", "breaker"
    temp = LLM_TEMPERATURE
    use_model = model or LLM_MODEL
    if "kimi-k2.7-code" in use_model:
        temp = 1.0
    reason = LLM_REASONING or ("low" if use_model.startswith("deepseek") else "")
    body = {
        "model": use_model,
        "temperature": temp,
        "max_tokens": LLM_MAX_TOKENS,
        "messages": [{"role": "system", "content": system or LLM_SYSTEM_PROMPT},
                     {"role": "user", "content": user or _llm_user_prompt(text, ctx)}],
    }
    if reason:
        body["reasoning_effort"] = reason
    data = json.dumps(body, ensure_ascii=False).encode("utf-8")
    url = LLM_BASE + "/chat/completions"
    with _LLM_SEM:
        for attempt in (1, 2, 3):
            try:
                req = urllib.request.Request(url, data=data, headers=_llm_headers(key))
                with urllib.request.urlopen(req, timeout=LLM_TIMEOUT) as r:
                    d = json.loads(r.read().decode("utf-8", "replace"))
                out = ((d.get("choices") or [{}])[0].get("message") or {}).get("content") or ""
                out = _clean_llm(out, ctx)
                if not out:
                    return "", "parse"
                _LLM_FAIL_STREAK = 0
                return out, ""
            except urllib.error.HTTPError as e:
                if e.code == 429 or e.code >= 500:
                    if attempt < 3:
                        time.sleep(2.0 * attempt)
                        continue
                    _LLM_FAIL_STREAK += 1
                    if _LLM_FAIL_STREAK >= 3:
                        _LLM_FAIL_STREAK = 0
                        _LLM_BREAKER_UNTIL = time.time() + 60
                    return "", "http_%d" % e.code
                if e.code in (401, 403):
                    return "", "auth_%d" % e.code
                return "", "http_%d" % e.code
            except (urllib.error.URLError, TimeoutError, OSError):
                if attempt < 3:
                    time.sleep(1.5 * attempt)
                    continue
                return "", "timeout"
            except Exception:
                return "", "parse"
    return "", "http"


def _pick_provider(text: str) -> str:
    """auto：短句走 Bing，长句/复杂关系词/打码字走 LLM。"""
    p = PROVIDER
    if p in ("bing", "llm"):
        return p
    if not _llm_key():
        return "bing"
    hard = (len(text) >= 32 or MASK_RE.search(text)
            or bool(re.search(r"義父|義理|寝取|NTR|中出し|絶倫|追撃|種付け|犯●|舐め", text)))
    if hard and time.time() >= _LLM_BREAKER_UNTIL:
        return "llm"
    return "bing"


# ---------------------------------------------------------------- 后处理与质量门
def _postcorrect(src: str, zh: str) -> str:
    """方案 B「回包矫正」：源文命中术语、译文没出现目标词时，按 wrong 列表替换；
    提供可选的 re 正则兜底（针对 Bing 把单一名词翻成整段动词短语的术语）。"""
    for g in load_glossary():
        if g.get("soft"):
            # 通用词（父/母/胸/脚…）只用于提示词，不做强制注入，避免句子被改僵
            continue
        if g["ja"] not in src:
            continue
        if any(v and v in zh for v in g.get("valid", [])):
            continue
        re_pat = g.get("re")
        if re_pat:
            zh2, n = re.subn(re_pat, g["zh"], zh)
            if n:
                zh = zh2
                continue
        replaced = False
        core = g["zh"]
        for w in g.get("wrong", []):
            # 「绝伦」是「精力绝伦」的一部分、「追击」是「连续追击」的一部分，
            # 这类错词不能替换，否则会把正确译文改成「精力精力绝伦」。
            if not w or w in core:
                continue
            if w in zh:
                zh = zh.replace(w, core)
                replaced = True
        # 只有明确标记 must 的核心术语、且没有已知错译可替换时才补词。
        # 其余术语（キス/電マ/場所…）交给正常翻译，避免句尾堆名词。
        if not replaced and g.get("must") and g["zh"] not in zh:
            zh = _inject_term(zh, g["zh"])
    return zh


def _inject_term(zh: str, term: str) -> str:
    zh = zh.strip()
    m = re.match(r"^(.*?)([。．.!！?？…，,]*)$", zh, re.S)
    if m and m.group(2):
        return m.group(1).rstrip() + " " + term + m.group(2)
    return (zh + " " + term).strip()


def _postprocess(zh: str) -> str:
    zh = html.unescape(zh or "").strip()
    zh = zh.replace("舐", "舔")                 # Bing 偶尔保留「舐」字，归一成「舔」
    # 译文里残留的日文演员名换成中文常用名
    try:
        import actor_names
        for ja, cn in actor_names.load_map().items():
            if ja and ja in zh:
                zh = zh.replace(ja, cn)
        # 机翻常把演员名前后两字对调，把 4 字姓名的前后两字对调回来
        for cn in set(actor_names.load_map().values()):
            if len(cn) == 4:
                rev = cn[2:] + cn[:2]
                if rev in zh:
                    zh = zh.replace(rev, cn)
    except Exception:
        pass
    zh = zh.replace("・", "·").replace("ヌルルン", "努鲁伦")
    # 术语去重：替换 wrong 词后可能出现「内射内射」「高潮高潮」这类叠加
    zh = re.sub(r"(无套内射|内射)\s*[、，]?\s*射精", r"\1", zh)
    zh = re.sub(r"(高潮)\s*[、，]?\s*(?:绝顶|到顶)", r"\1", zh)
    for g in load_glossary():
        t = g.get("zh") or ""
        if len(t) < 2 or t not in zh:
            continue
        zh = re.sub(r"(%s)\s*(?:[、，]\s*)?\1" % re.escape(t), r"\1", zh)
    zh = re.sub(r"\s+", " ", zh).strip()
    zh = re.sub(r"\s+([，。！？、；：])", r"\1", zh)
    return zh


def _normalize_punct(zh: str) -> str:
    """NFO 拼接用：省略号收敛 + 尾部标点归一。"""
    zh = re.sub(r"\.{2,}", "……", zh)           # ...... -> ……
    zh = re.sub(r"．{2,}", "……", zh)
    return zh.strip()


def _quality_fail(src: str, zh: str) -> str:
    """质量门：不通过返回原因码，通过返回空串。"""
    zh = (zh or "").strip()
    if not zh:
        return "empty"
    if zh == src:
        return "identical"
    if META_LEAK_RE.search(zh):
        return "meta_leak"
    body = re.sub(r"\s", "", zh)
    if body:
        kana = len(KANA_RE.findall(body))
        # 阈值 18%：标题里经常带假名演员名（某演员/某演员），
        # 5% 会把「译好了但保留人名」的条目误判成没翻；整句未翻的日文假名占 40%+
        if kana / len(body) > 0.18:
            return "kana_ratio"
    residual = MODEL_CODE_RE.sub("", zh)        # 型号白名单除外
    # AV 术语缩写（NTR/SM/OL…）不算混英文；CJK 与拉丁之间没有 \b，用前后 lookaround
    if LATIN_ALLOW:
        pat = r"(?<![A-Za-z])(" + "|".join(sorted(LATIN_ALLOW, key=len, reverse=True)) + r")(?![A-Za-z])"
        residual = re.sub(pat, "", residual, flags=re.I)
    if ASCII_RE.search(residual):
        # 允许零星音译残片（如「ざぁ～こ」→ Zaa），真英文单词/整句才判失败
        words = re.findall(r"[A-Za-z]+", residual)
        if any(len(w) >= 5 for w in words) or sum(len(w) for w in words) > 8:
            return "latin"
    if MASK_RE.search(zh):
        return "mask"
    src_len = len(re.sub(r"\s", "", src))
    if src_len > 0:
        ratio = len(body) / src_len
        if ratio < 0.25 or ratio > 2.5:
            return "length_ratio"
    return ""


def _record_failure() -> None:
    """连续失败 3 次即进入 45s 冷却；到点自动半开重试（旧实现会永久关闭，导致整批只剩前几条有译文）。"""
    global _FAIL_STREAK, _BREAKER_UNTIL, _BREAKER_WARNED
    _FAIL_STREAK += 1
    if _FAIL_STREAK >= 3:
        _FAIL_STREAK = 0
        _BREAKER_UNTIL = time.time() + 45
        _BREAKER_WARNED = False


def _mask_codes(text: str):
    """翻译前把番号（JUL-002 / IPZZ542）替换成占位符，避免 Bing 把 JUL 翻成「7月」。"""
    mapping = {}

    def repl(m):
        token = "ZXQ%dQ" % len(mapping)
        mapping[token] = m.group(0)
        return token

    return MODEL_CODE_RE.sub(repl, text), mapping


def _restore_codes(zh: str, mapping: dict) -> str:
    for token, code in mapping.items():
        zh = re.sub(re.escape(token), code, zh, flags=re.I)
    return zh


# 寝取られ/寝取り 的主语明确时，中文用自然说法，不机械写「NTR」。
# 只在短标题上整句改写；长标题仍保留直译细节，由 LLM 或术语矫正处理。
_CUCKOLD_AGENTS = [
    (re.compile(r"義理の息子|義理息子|義息子"), "继子"),
    (re.compile(r"息子の友人|息子の友達"), "儿子的朋友"),
    (re.compile(r"娘の彼氏"), "女儿的男友"),
    (re.compile(r"後輩"), "后辈"),
    (re.compile(r"先輩"), "前辈"),
    (re.compile(r"上司"), "上司"),
    (re.compile(r"部下"), "部下"),
    (re.compile(r"親友"), "好友"),
    (re.compile(r"友人|友達"), "朋友"),
    (re.compile(r"隣人|隣の男"), "邻居"),
    (re.compile(r"義父|お義父さん|義理の父"), "公公"),
    (re.compile(r"兄"), "哥哥"),
    (re.compile(r"弟"), "弟弟"),
    (re.compile(r"息子"), "儿子"),
]


def _phrase_rewrite(src: str, zh: str) -> str:
    if len(src) > 20:
        return zh
    if not re.search(r"寝取|NTR", src):
        return zh
    if not re.search(r"妻|人妻|嫁|奥さん|奥様", src):
        return zh
    for pat, agent in _CUCKOLD_AGENTS:
        if pat.search(src):
            return agent + "与妻子，越过了那条线"
    return zh


# ---------------------------------------------------------------- 对外
def translate_text(text: str, retries: int = 2, context: dict | None = None) -> str:
    """日文 -> 简体中文。失败返回空串（不抛异常），并记录 last_error。

    provider=auto：短句走 Bing，长句/复杂关系词/打码字走 LLM Pro；
    任何一方失败/质量门不过，自动换另一条路，最后才回退原文。
    """
    global _LAST_ERROR, _FAIL_STREAK, _BREAKER_WARNED
    text = (text or "").strip()
    if not text:
        return ""
    if not KANA_RE.search(text):
        # 纯汉字标题（如「母子姦」）：短标题仍送翻（Bing 能出「母子乱伦」），
        # 长文本/含拉丁字母的视为无需翻译，避免把中文当日语硬翻。
        if len(text) > 12 or re.search(r"[A-Za-z]", text):
            _LAST_ERROR = ("no_kana", "无假名，无需翻译")
            return ""

    if time.time() < _BREAKER_UNTIL:
        if not _BREAKER_WARNED:
            _log("WARN plot_zh 熔断：连续失败 3 次，冷却 %ds 后半开重试"
                 % int(_BREAKER_UNTIL - time.time()))
            _BREAKER_WARNED = True
        _LAST_ERROR = ("breaker", "冷却中")
        return ""

    # 打码字还原；表中没有的一律不猜
    src = _restore_mask(text)
    if MASK_RE.search(src):
        # 未收录的圈号不再整条放弃：移除残留占位符后继续翻译，避免一条打码字导致整条 FAIL
        _log("WARN plot_zh mask_unmapped: %s" % text)
        src = MASK_RE.sub("", src)

    provider = _pick_provider(src)
    if provider == "llm" and not _llm_key():
        provider = "bing"

    src_masked, code_map = _mask_codes(src)
    order = [provider]
    other = "llm" if provider == "bing" else "bing"
    if other == "llm" and not _llm_key():
        other = ""
    if other:
        order.append(other)

    last_err = ""
    for p in order:
        key = _cache_key(src, p, LLM_MODEL if p == "llm" else "")
        cached = _cache_get(key)
        if cached:
            _FAIL_STREAK = 0
            _LAST_ERROR = ("", "")
            return cached

        if p == "llm":
            zh, err = _llm_translate(src_masked, context)
        else:
            zh, err = _translate_net(src_masked)
        if not zh:
            last_err = "%s:%s" % (p, err)
            if p == "bing":
                _record_failure()
            continue

        zh = _restore_codes(zh, code_map)
        zh = _phrase_rewrite(src, _postprocess(_postcorrect(src, zh)))
        qf = _quality_fail(src, zh)
        # LLM 译文通常比日文短，length_ratio 不再作为硬失败
        if qf == "length_ratio" and p == "llm" and re.search(r"[\u4e00-\u9fff]", zh):
            qf = ""
        if qf:
            last_err = "%s:quality:%s" % (p, qf)
            continue

        _FAIL_STREAK = 0
        _LAST_ERROR = ("", "")
        _cache_set(key, zh)
        return zh

    _LAST_ERROR = ("net", last_err or "all_failed")
    return ""


def _strip_actors(title: str, actors) -> str:
    t = (title or "").strip()
    for a in sorted(actors or [], key=len, reverse=True):
        if a and t.endswith(a):
            t = t[: -len(a)].strip()
    return t


BRACKET_RE = re.compile(r"[【［\[][^】］\]]*[】］\]]")
SITE_SUFFIX_RE = re.compile(
    r"\s*[-–—]\s*(?:アダルト動画|FANZA|JAV|DMM|HEYZO|動画|DVD|Blu-?ray).*$", re.I)


def _prepare_title(title: str, actors) -> str:
    """去掉演员名、假名注音、站点后缀，避免这些噪声把译文判成「没翻」。"""
    t = (title or "").strip()
    actors = [a for a in (actors or []) if isinstance(a, str) and a.strip()]

    # 只删纯假名/纯读音的括号，保留标题里有汉字的括号内容
    def _drop_bracket(m):
        inner = m.group(0)[1:-1]
        if not re.search(r"[\u4e00-\u9fff]", inner):
            return ""
        if any(a and a in inner for a in actors):
            return ""
        return m.group(0)

    t = BRACKET_RE.sub(_drop_bracket, t)
    t = SITE_SUFFIX_RE.sub("", t)

    # 演员名可能在开头、结尾或书名号里
    for a in sorted(actors, key=len, reverse=True):
        if not a:
            continue
        if t.startswith(a):
            t = t[len(a):].strip()
        if t.endswith(a):
            t = t[: -len(a)].strip()
        t = t.replace("『%s』" % a, "").replace("「%s」" % a, "")
    t = re.sub(r"\s+", " ", t).strip(" -–—")
    return t


def translate_title_zh(d: dict) -> str:
    """只翻译标题（不含发布日期/演员），供 <title> 与 <plot> 共用。"""
    global _LAST_ERROR
    d = d or {}
    known_actors = list(d.get("actors") or [])
    try:
        import actor_names as _an
        known_actors += list(_an.load_map().keys())
    except Exception:
        pass
    title = _prepare_title(d.get("title") or "", known_actors)
    if not title:
        title = _strip_actors(d.get("title") or "", known_actors)
    if not title:
        _LAST_ERROR = ("empty", "无标题")
        return ""
    zh = _postprocess(translate_text(title, context=d))
    if not zh:
        # 长标题兜底：取第一句/前 80 字再试一次，避免整条回落日文
        short = re.split(r"[。！？!?]", title, 1)[0].strip()
        if short and short != title:
            zh = _postprocess(translate_text(short[:80], context=d))
    if not zh:
        return ""                              # last_error 已由 translate_text 记录
    zh = _strip_meta_leak(zh)
    return _normalize_punct(zh) if zh else ""


def _strip_meta_leak(zh: str) -> str:
    """把 LLM 漏出的「重写：标题：...」清理成真正标题；无可用内容返回空串。"""
    zh = (zh or "").strip()
    if not META_LEAK_RE.search(zh):
        return zh
    m = re.search(r"(?:标题|译文|中文)\s*[:：]\s*(.+)$", zh, re.S)
    if m:
        return m.group(1).strip().strip("“”\"'「」 ")
    return ""


def _plot_cache_key(src: str) -> str:
    raw = "plot|%s|%s|%s|%s" % (LLM_MODEL, PROMPT_VERSION, glossary_version(), src)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()


def _plot_user_prompt(d: dict, title_zh: str) -> str:
    d = d or {}
    lines = []
    if d.get("code"):
        lines.append("番号：" + str(d["code"]))
    lines.append("日文标题：" + str(d.get("title") or ""))
    if title_zh:
        lines.append("标题直译：" + title_zh)
    actors = [a for a in (d.get("actors") or []) if isinstance(a, str) and a.strip()]
    if actors:
        lines.append("演员：" + "、".join(actors[:6]))
    genres = d.get("genres") or []
    if isinstance(genres, str):
        genres = [genres]
    if genres:
        lines.append("题材：" + "、".join(str(x) for x in genres[:10]))
    desc = str(d.get("desc") or "").strip()
    if desc and desc != str(d.get("title") or "").strip():
        lines.append("日文简介：" + desc[:600])
    hits = _glossary_hits(str(d.get("title") or ""))
    if hits:
        lines.append("必须使用术语：" + "；".join(
            "%s→%s" % (g["ja"], g.get("prompt") or g["zh"]) for g in hits))
    return "\n".join(lines)


def _plot_ok(zh: str, title_zh: str = "", minimum: int = MIN_PLOT_CHARS) -> bool:
    body = re.sub(r"\s", "", zh or "")
    if len(body) < minimum:
        return False
    if PLOT_REFUSAL_RE.search(zh or "") or META_LEAK_RE.search(zh or ""):
        return False
    if KANA_RE.search(zh or ""):
        return False
    if title_zh and body == re.sub(r"\s", "", title_zh):
        return False
    return True


def translate_plot_zh(d: dict, title_zh: str) -> str:
    """用 LLM 写 140~220 字预告文案；无 Key/Bing 模式返回空串，由调用方回退标题。

    文案过短、含日文假名、拒答或 meta 泄漏时重试一次；仍不合格则返回空串，
    让调用方明确回退，而不是把半成品写进 NFO。
    """
    if PROVIDER == "bing" or not _llm_key():
        return ""
    d = d or {}
    src = "|".join([
        str(d.get("code") or ""), str(d.get("title") or ""), title_zh,
        ",".join(a for a in (d.get("actors") or []) if isinstance(a, str)),
        ",".join(str(x) for x in (d.get("genres") or [])),
        str(d.get("releasedate") or ""), str(d.get("runtime") or ""),
    ])
    key = _plot_cache_key(src)
    cached = _cache_get(key)
    if cached and _plot_ok(cached, title_zh):
        return cached

    user = _plot_user_prompt(d, title_zh)
    zh = ""
    for attempt in (1, 2):
        prompt = user
        if attempt == 2:
            prompt += ("\n\n上一版过短或不完整。请重写到 160~200 字，保留关系与冲突，"
                       "第一句直接进入场景，结尾留钩子；只输出文案正文。")
        out, err = _llm_translate(src, d, system=PLOT_SYSTEM_PROMPT, user=prompt)
        if not out:
            continue
        out = re.sub(r"^\s*(?:文案|简介|剧情简介|译文|中文)\s*[:：]\s*", "", out)
        out = _postprocess(_postcorrect(src, out))
        out = _normalize_punct(out)
        if _plot_ok(out, title_zh, MIN_PLOT_CHARS if attempt == 1 else MIN_PLOT_CHARS_RETRY):
            zh = out
            break
    # GLM 对母子/露骨题材拒答时，用 DeepSeek 兜底再写一次
    if not zh and LLM_FALLBACK_MODEL and LLM_FALLBACK_MODEL != LLM_MODEL:
        out, err = _llm_translate(src, d, system=PLOT_SYSTEM_PROMPT,
                                  user=user + "\n\n只输出成品文案正文，不要解释或拒绝。",
                                  model=LLM_FALLBACK_MODEL)
        if out:
            out = re.sub(r"^\s*(?:文案|简介|剧情简介|译文|中文)\s*[:：]\s*", "", out)
            out = _postprocess(_postcorrect(src, out))
            out = _normalize_punct(out)
            if _plot_ok(out, title_zh, MIN_PLOT_CHARS_RETRY):
                zh = out
    if not zh:
        return ""
    _cache_set(key, zh)
    return zh


def build_plot_zh(d: dict, title_zh: str = "") -> str:
    """用中文元数据拼剧情：标题用 LLM 翻译，简介用 LLM 写预告文案。

    title_zh 可由调用方传入，避免同一条 NFO 重复翻译标题、产生两次 LLM 调用。
    """
    global _LAST_ERROR
    d = d or {}
    zh = (title_zh or "").strip() or translate_title_zh(d)
    if not zh:
        return ""
    plot = translate_plot_zh(d, zh) or zh
    date = (d.get("releasedate") or "").strip()
    mins = re.sub(r"[^\d]", "", str(d.get("runtime") or "")) or ""
    # 繁体标签，与既有库 81/81 个 NFO 一致
    head = "，".join(x for x in [
        ("【發行日期】" + date) if date else "",
        ("【長度】%s分鐘" % mins) if mins else "",
    ] if x)

    code = (d.get("code") or "").strip()
    body = ("(%s) %s" % (code, plot)) if code else plot
    # 只把中文演员名追加到简介尾部；未映射的日文名不写入，避免简介出现假名
    actors = " ".join(a for a in (d.get("actors") or [])
                      if isinstance(a, str) and a.strip() and not KANA_RE.search(a))

    out = "，".join(x for x in [head, body] if x)
    if actors:
        # 尾部标点归一：译文以句读/省略号结尾则不再补 ，（改用空格，与既有库 desc 一致）
        out += (" " if out.endswith(("。", ".", "．", "…", "！", "？")) else "，") + actors
    return out.strip()
