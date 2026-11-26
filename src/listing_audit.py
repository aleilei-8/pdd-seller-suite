#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""商品文案合规体检 Agent (把客服护栏复用到「上架前」)。

扫描对象:
- 商品标题 / 卖点 / 详情文案 (来自已采集的货源 products, 或手动粘贴)
- 本地话术库 (data/cs_faq.json)

用 compliance.py 的违禁词表识别 广告法极限词 / 虚假承诺 / 平台违禁词,
给出"风险等级 + 命中词 + 安全替换文案"。

设计原则:
- 纯只读: 不登录、不修改商品、不写回货源; 只扫描 + 给建议。
- 人工确认: 替换文案仅供参考, 需人工到上架页修改。
- 离线可用: 不依赖模型。
"""
import active_words
import compliance

RISK_ORDER = {"高": 0, "中": 1, "低": 2}
_SOURCE_TAG = {"literal": "通用", "semantic": "语义等价", "active": "私有活清单"}


def _word_label(f):
    tag = _SOURCE_TAG.get(f.get("source", "literal"), f.get("source", "通用"))
    return f"{f['keyword']}({tag})"


def audit_text(text, label=""):
    """扫描单段文案 -> finding dict (无命中则 level=低)。"""
    text = text or ""
    findings = compliance.scan(text)
    high = [f for f in findings if f["severity"] == "high"]
    med = [f for f in findings if f["severity"] == "medium"]
    level = "高" if high else ("中" if med else "低")
    return {
        "label": label or text[:24],
        "level": level,
        "text": text[:120],
        "findings": findings,
        "safe_text": compliance.redact(text, findings),
        "words": sorted({f["keyword"] for f in findings}),
        "hit_labels": sorted({_word_label(f) for f in findings}),
        "active_words": sorted({f["keyword"] for f in findings if f.get("source") == "active"}),
        "sources": compliance.hit_summary(findings),
        "categories": sorted({f["category"] for f in findings}),
    }


def audit_products(products):
    """批量扫描已采集商品的标题 + 卖点/详情文本。"""
    out = []
    for p in products or []:
        title = getattr(p, "title", "") or ""
        # 兼容不同 Product 结构: 卖点/详情可能在 subtitle/detail/attrs
        extra = ""
        for attr in ("subtitle", "sell_point", "detail", "description"):
            v = getattr(p, attr, "")
            if v:
                extra += str(v) + " "
        text = (title + " " + extra).strip()
        if not text:
            continue
        r = audit_text(text, label=f"[{getattr(p,'item_id','')}] {title[:20]}")
        r["item_id"] = getattr(p, "item_id", "")
        out.append(r)
    out.sort(key=lambda x: RISK_ORDER.get(x["level"], 9))
    return out


def audit_faq():
    """扫描本地话术库 data/cs_faq.json。"""
    try:
        import cs_scripts
        items = cs_scripts.load_faq()
    except Exception:
        items = []
    out = []
    for it in items:
        text = it.get("answer") or ""
        r = audit_text(text, label="话术: " + "/".join(it.get("keys", []))[:20])
        out.append(r)
    out.sort(key=lambda x: RISK_ORDER.get(x["level"], 9))
    return out


def summarize(results):
    """统计风险分布 + 命中来源 -> dict。"""
    from collections import Counter
    c = Counter(r["level"] for r in results)
    src = Counter(f.get("source", "literal") for r in results for f in r["findings"])
    return {
        "total": len(results),
        "high": c.get("高", 0),
        "medium": c.get("中", 0),
        "low": c.get("低", 0),
        "all_words": sorted({w for r in results for w in r["words"]}),
        "semantic_hits": src.get("semantic", 0),
        "active_hits": src.get("active", 0),
    }


def render_report(results, sm=None):
    """中文体检报告。results=audit_products()/audit_faq() 的结果列表。"""
    sm = sm or summarize(results)
    st = active_words.stats()
    lines = [f"🛡 商品文案合规体检 (共 {sm['total']} 条文案)",
             f"高风险 {sm['high']} 条 / 中风险 {sm['medium']} 条 / 通过 {sm['low']} 条",
             f"命中来源: {compliance.hit_summary([f for r in results for f in r['findings']])}",
             "═" * 40]
    if st.get("error"):
        lines.append(f"⚠ 私有活清单: {st['error']} ({st['path']})")
    else:
        lines.append(f"🔒 私有活清单: 已加载 {st['loaded']} 条 ({st['path']})")
    lines.append("")
    risky = [r for r in results if r["level"] != "低"]
    if not risky:
        lines.append("✅ 未发现极限词/虚假承诺/平台违禁词, 文案合规。")
    else:
        for r in risky[:15]:
            lines.append(f"[{r['level']}] {r['label']}")
            lines.append(f"   命中: {', '.join(r.get('hit_labels') or r['words'])} ({', '.join(r['categories'])})")
            lines.append(f"   原文: {r['text'][:70]}")
            lines.append(f"   建议: {r['safe_text'][:70]}")
        if len(risky) > 15:
            lines.append(f"(另有 {len(risky)-15} 条风险文案未展开)")
    if sm.get("all_words"):
        lines.append("")
        lines.append("高频风险词: " + "、".join(sm["all_words"][:12]))
    lines.append("")
    lines.append("（本地规则扫描: 通用规则 + 语义等价 + 私有活清单; 仅供参考, 上架前请人工复核。）")
    return "\n".join(lines)


if __name__ == "__main__":
    demo = [
        audit_text("全网最低价 最好用的面膜 假一赔十", "测试标题1"),
        audit_text("正品保障 支持七天无理由", "测试标题2"),
        audit_text("加微信好评返现", "测试标题3"),
        audit_text("蕞棒的面膜 顶级配置 行业第一 加薇信", "换皮词测试"),
    ]
    print(render_report(demo))
