#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""店铺私有「活清单」加载器 (不入库 / 不打包 / 热重载)。

活清单 = 你自己店铺被驳回过的标题词、同行被罚过的表达、平台临时收紧的词。
公开版只带通用规则 (compliance.BANNED + SEMANTIC_CLUSTERS), 绝不携带任何
店铺私有数据 —— 这正是「卖铲子不卖金矿位置图」的落地点。

文件位置:
    源码 / 免安装包: <ROOT>/data/active_banned_words.json
    可用环境变量 PDD_ACTIVE_WORDS 覆盖 (多店切换时很方便)。

格式 (完整示例见 assets/active_banned_words.example.json):
    {
      "version": 1, "updated": "2026-11-27",
      "words": [
        {"term": "最好用", "category": "驳回词", "severity": "high",
         "hint": "体验不错", "aliases": ["最好用的"],
         "source": "2026-11-20 美白面膜标题被驳回", "enabled": true},
        "全网最低价"      // 也支持纯字符串(默认 high)
      ]
    }

行为约定:
- 只读扫描: 本模块不联网、不登录、不上传, 绝不把活清单内容发给任何接口。
- 唯一的写操作是 append_word()/create_template(), 只写本地 JSON, 且需显式调用。
- 文件损坏时降级为空清单并通过 stats()['error'] 说明原因, 不影响主流程。
- 按 mtime+size 自动热重载, GUI 长时间开着也能立刻看到新词。

用法:
    entries()             -> 当前生效条目
    stats()               -> {path, exists, loaded, updated, error}
    create_template()     -> 生成空活清单 (已有则不动)
    append_word("词")     -> 记录一条驳回词
"""
import json
import os
import threading
from datetime import date
from pathlib import Path

from paths import DATA, ROOT

ENV_KEY = "PDD_ACTIVE_WORDS"
DEFAULT_PATH = DATA / "active_banned_words.json"
TEMPLATE_PATH = ROOT / "assets" / "active_banned_words.example.json"
# 兜底模板: 即使免安装包漏带 assets 文件, GUI「生成活清单」也能用。
_BUILTIN_TEMPLATE = {
    "_note": "私有活清单：只记录你自己店铺被驳回过的词。此文件不入库、不打包、不上传。",
    "version": 1,
    "updated": "",
    "shop": "",
    "words": [],
    "_example": [{
        "term": "最好用", "category": "驳回词", "severity": "high",
        "hint": "体验不错", "aliases": ["最好用的"],
        "source": "示例：请替换成你的真实驳回记录", "enabled": True,
    }],
}
VALID_SEVERITY = {"high", "medium", "low"}

_LOCK = threading.RLock()
_CACHE = {"sig": None, "entries": [], "error": "", "meta": {}, "path": ""}


def resolve_path(path=None):
    """优先级: 显式参数 > 环境变量 PDD_ACTIVE_WORDS > data/active_banned_words.json。"""
    if path:
        return Path(path).expanduser()
    env = (os.environ.get(ENV_KEY) or "").strip()
    if env:
        return Path(env).expanduser()
    return DEFAULT_PATH


def _sig(p: Path):
    try:
        st = p.stat()
        return (str(p), st.st_mtime_ns, st.st_size)
    except OSError:
        return (str(p), None, None)


def _norm_entry(item):
    """兼容纯字符串 / 对象两种写法, 返回统一结构。"""
    if isinstance(item, str):
        term = item.strip()
        if not term:
            return None
        return {
            "term": term, "aliases": [], "category": "私有活清单",
            "severity": "high", "hint": "", "source": "本地活清单", "enabled": True,
        }
    if not isinstance(item, dict):
        return None
    term = str(item.get("term") or item.get("word") or "").strip()
    if not term:
        return None
    aliases = item.get("aliases") or []
    if isinstance(aliases, str):
        aliases = [aliases]
    aliases = [str(a).strip() for a in aliases if str(a).strip()]
    sev = str(item.get("severity") or "high").strip().lower()
    if sev not in VALID_SEVERITY:
        sev = "high"
    return {
        "term": term,
        "aliases": aliases,
        "category": str(item.get("category") or "私有活清单"),
        "severity": sev,
        "hint": str(item.get("hint") or ""),
        "source": str(item.get("source") or "本地活清单"),
        "enabled": item.get("enabled", True) is not False,
    }


def load(path=None, force=False):
    """加载/热重载活清单, 返回生效条目列表 (只含 enabled != false)。"""
    with _LOCK:
        p = resolve_path(path)
        sig = _sig(p)
        if not force and _CACHE["sig"] == sig:
            return list(_CACHE["entries"])

        entries, error, meta = [], "", {}
        if sig[1] is None:
            error = "文件不存在 (可点『生成活清单』创建)"
        else:
            try:
                data = json.loads(p.read_text(encoding="utf-8-sig"))
                if isinstance(data, dict):
                    meta = {k: data.get(k) for k in ("version", "updated", "shop", "note", "_note")}
                    words = data.get("words", [])
                elif isinstance(data, list):
                    words = data
                else:
                    raise ValueError("顶层必须是对象或数组")
                if not isinstance(words, list):
                    raise ValueError("words 必须是数组")
                for w in words:
                    e = _norm_entry(w)
                    if e and e["enabled"]:
                        entries.append(e)
            except Exception as exc:
                error = f"读取失败: {exc}"
                entries = []

        _CACHE.update({
            "sig": sig, "entries": entries, "error": error,
            "meta": meta, "path": str(p),
        })
        return list(entries)


def entries(path=None):
    return load(path)


def stats(path=None):
    """活清单状态, 供 GUI / 报告展示。"""
    load(path)
    with _LOCK:
        sig = _CACHE["sig"] or (str(resolve_path(path)), None, None)
        return {
            "path": _CACHE["path"] or str(resolve_path(path)),
            "exists": sig[1] is not None,
            "loaded": len(_CACHE["entries"]),
            "updated": _CACHE["meta"].get("updated") or "",
            "shop": _CACHE["meta"].get("shop") or "",
            "error": _CACHE["error"],
        }


def clear_cache():
    with _LOCK:
        _CACHE.update({"sig": None, "entries": [], "error": "", "meta": {}, "path": ""})


def create_template(path=None):
    """生成空活清单 (已存在则不动)。返回 (ok, msg)。"""
    p = resolve_path(path)
    if p.exists():
        return False, f"已存在: {p}"
    try:
        text = (TEMPLATE_PATH.read_text(encoding="utf-8") if TEMPLATE_PATH.exists()
                else json.dumps(_BUILTIN_TEMPLATE, ensure_ascii=False, indent=2) + "\n")
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8")
    except Exception as exc:
        return False, f"创建失败: {exc}"
    clear_cache()
    return True, f"已创建: {p}"


def append_word(term, category="驳回词", severity="high", hint="", source="", aliases=None):
    """追加一条驳回词到本地活清单。只写本地 JSON, 不入库不上传。

    返回 (ok, msg)。重复词不会重复写入。
    """
    term = (term or "").strip()
    if not term:
        return False, "词不能为空"
    p = resolve_path()
    if not p.exists():
        ok, msg = create_template()
        if not ok:
            return False, msg
    try:
        data = json.loads(p.read_text(encoding="utf-8-sig"))
        if isinstance(data, list):
            data = {"version": 1, "words": data}
        if not isinstance(data, dict):
            data = {"version": 1, "words": []}
        words = data.setdefault("words", [])
        if not isinstance(words, list):
            words = data["words"] = []
        known = set()
        for w in words:
            known.add((w.strip() if isinstance(w, str) else str(w.get("term") or "")).strip())
        if term in known:
            return False, f"已在活清单中: {term}"
        sev = str(severity or "high").lower()
        if sev not in VALID_SEVERITY:
            sev = "high"
        item = {
            "term": term,
            "category": category or "驳回词",
            "severity": sev,
            "hint": hint or "",
            "aliases": [a.strip() for a in (aliases or []) if str(a).strip()],
            "source": source or f"手工记录 {date.today().isoformat()}",
            "enabled": True,
        }
        words.append(item)
        data["updated"] = date.today().isoformat()
        p.write_text(json.dumps(data, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    except Exception as exc:
        return False, f"写入失败: {exc}"
    clear_cache()
    return True, f"已记录: {term}"


if __name__ == "__main__":
    print(json.dumps(stats(), ensure_ascii=False, indent=2))
    for e in entries():
        print(" ", e["severity"], e["term"], e["aliases"], e["source"])
