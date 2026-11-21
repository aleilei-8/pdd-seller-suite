#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全店经营早报 Agent (编排层二次升级: 跨模块聚合 → 一句话全店体检 + 今日待办)。

把分散在各模块的数据(选品/订单/广告/财务/竞品/库存/客服)汇总成一份
"老板视角"的每日简报, 并按阈值自动生成"今日优先待办"。

设计原则:
- 纯只读: 不登录、不发送、不修改任何数据; 只是把已有模块的结果聚合呈现。
- 容错: 任一模块无数据就跳过该段并标注"待导入", 不影响整体出报。
- 数据来自 ctx(GUI 已缓存), 不重新抓取; 不落盘原始 PII。
- 可选 LLM 总结: 只发送聚合后的数字(无隐私), 失败自动降级本地规则。

用法:
    build(ctx)          -> result dict
    render_report(result) -> 中文早报文本
"""
from datetime import datetime

import compliance


def _money(v):
    return f"¥{v:.2f}"


def build(ctx, use_llm=False, cfg=None):
    """聚合 ctx 里各模块缓存 -> result dict。ctx 键与 ai_assistant.run_action 一致。"""
    products = ctx.get("products") or []
    orders = ctx.get("orders") or []
    ad_rows = ctx.get("ad_rows") or []
    fin = ctx.get("finance_result") or ctx.get("fin_result")
    comp = ctx.get("competitor_result") or ctx.get("comp_result")
    chat_rows = ctx.get("cs_chat_rows") or []
    config = ctx.get("config")

    sections = {}
    todos = []   # 今日待办: (优先级, 文案)

    # —— 订单经营 ——
    try:
        import analysis
        if orders:
            k = analysis.store_kpi(orders)
            sections["经营"] = k
            if k.get("net_margin", 0) < 10:
                todos.append(("高", f"整体净利率仅 {k['net_margin']}% (<10%), 检查定价与成本"))
            if k.get("linked_rate", 0) < 50:
                todos.append(("中", f"仅 {k['linked_rate']}% 订单关联到货源, 补 costmap 才能算真实利润"))
        else:
            sections["经营"] = None
    except Exception:
        sections["经营"] = None

    # —— 广告投产 ——
    try:
        import ad_roi
        if ad_rows:
            max_cpc = None
            try:
                max_cpc = float(getattr(config, "max_cpc") or 0) or None
            except Exception:
                pass
            ov = ad_roi.overall(ad_rows, max_cpc)
            sections["广告"] = ov
            if ov.roi < 1:
                todos.append(("高", f"广告投产比 {ov.roi} (<1, 亏损), 缩量/降价/暂停亏损计划"))
            elif ov.roi < 1.5:
                todos.append(("中", f"广告投产比 {ov.roi} 接近保本, 降出价或优化转化"))
        else:
            sections["广告"] = None
    except Exception:
        sections["广告"] = None

    # —— 财务对账 ——
    if fin:
        sections["财务"] = fin
        try:
            summary = fin.get("summary") or {}
            after = summary.get("net_profit_after_ad") or summary.get("net_profit")
            if after is not None and after < 0:
                todos.append(("高", f"扣广告后真实净利 {_money(after)} 为负, 先停亏损项"))
        except Exception:
            pass
    else:
        sections["财务"] = None

    # —— 竞品 ——
    try:
        import competitor
        watches = competitor.load_table()
        if watches:
            cres = comp or competitor.analyze_all(
                watches, float(getattr(config, "target_net_margin", 0) or 0))
            rows = cres.get("rows") or cres.get("analysis") or []
            sections["竞品"] = rows
            weak = [r for r in rows if "劣势" in str(r.get("level", "")) or "偏高" in str(r.get("level", ""))]
            if weak:
                todos.append(("中", f"{len(weak)} 个商品价格劣势/偏高, 到竞品监控页看能否跟价"))
        else:
            sections["竞品"] = None
    except Exception:
        sections["竞品"] = None

    # —— 库存 ——
    try:
        if products:
            zero = low = 0
            for p in products:
                stocks = [int(c.get("stock") or 0) for c in (getattr(p, "sku_combos", None) or [])]
                s = sum(stocks)
                if not stocks or s == 0:
                    zero += 1
                elif s < 50:
                    low += 1
            sections["库存"] = {"zero": zero, "low": low, "total": len(products)}
            if zero:
                todos.append(("高", f"{zero} 个商品无库存/未取到库存, 核对后决定是否下架"))
            elif low:
                todos.append(("中", f"{low} 个商品低库存(<50), 及时补货"))
        else:
            sections["库存"] = None
    except Exception:
        sections["库存"] = None

    # —— 客服质检 / 时效 ——
    try:
        if chat_rows:
            import cs_qa as qa
            import cs_sla as sla
            qres = qa.analyze(chat_rows)
            sres = sla.analyze(chat_rows)
            sections["客服"] = {"qa": qres, "sla": sres}
            if qres.get("over_promise"):
                todos.append(("高", f"客服 {len(qres['over_promise'])} 处过度承诺/违禁话术, 立即纠正"))
            if sres.get("unanswered_sessions", 0) > 0:
                todos.append(("中", f"{sres['unanswered_sessions']} 个会话未回复, 人工回访避免差评"))
        else:
            sections["客服"] = None
    except Exception:
        sections["客服"] = None

    # 待办按优先级排序
    rank = {"高": 0, "中": 1, "低": 2}
    todos.sort(key=lambda x: rank.get(x[0], 9))

    result = {
        "time": datetime.now().strftime("%Y-%m-%d %H:%M"),
        "sections": sections,
        "todos": todos,
        "notice": compliance.PRIVACY_NOTICE,
    }

    # 可选 LLM: 只发送聚合数字(已是脱敏统计), 失败降级
    if use_llm:
        try:
            import ai_assistant
            llm_cfg = cfg or ai_assistant.load_config()
            if llm_cfg.get("api_key"):
                payload = {"经营": sections.get("经营"),
                           "广告": ({"roi": sections["广告"].roi, "spend": sections["广告"].spend,
                                    "advice": sections["广告"].advice} if sections.get("广告") else None),
                           "待办": [t[1] for t in todos[:8]]}
                safe = compliance.scan_for_llm(
                    __import__("json").dumps(payload, ensure_ascii=False))
                out = ai_assistant.llm_chat([
                    {"role": "system", "content":
                     "你是电商店长助理。基于给定的经营数据, 用中文写一段 100 字内的今日经营总结, "
                     "语气简洁务实, 只讲结论和动作, 不要复述任何个人信息。"},
                    {"role": "user", "content": safe},
                ], llm_cfg, timeout=45)
                if out:
                    result["ai_summary"] = out.strip()
        except Exception:
            pass

    return result


def render_report(result):
    """中文早报文本。"""
    r = result or {}
    s = r.get("sections") or {}
    lines = [f"📰 全店经营早报  {r.get('time', '')}",
             "═" * 40]

    k = s.get("经营")
    if k:
        lines.append(f"📊 经营: {k['orders']} 单 / {k['units']} 件 | 成交 {_money(k['revenue'])} "
                     f"| 净利 {_money(k['net_profit'])} ({k['net_margin']}%)")
    else:
        lines.append("📊 经营: 待导入订单 CSV")

    ov = s.get("广告")
    if ov:
        lines.append(f"📣 广告: 花费 {_money(ov.spend)} | 成交 {_money(ov.gmv)} "
                     f"| ROI {ov.roi} | CPC {ov.cpc}")
        lines.append(f"     诊断: {ov.advice}")
    else:
        lines.append("📣 广告: 待导入推广 CSV")

    fin = s.get("财务")
    if fin:
        sm = fin.get("summary") or {}
        lines.append(f"📒 财务: 广告后真实净利 {_money(sm.get('net_profit_after_ad', sm.get('net_profit', 0)))}")
    else:
        lines.append("📒 财务: 待刷新对账")

    comp_rows = s.get("竞品")
    if comp_rows is None:
        lines.append("🎯 竞品: 待登记台账")
    else:
        weak = sum(1 for x in comp_rows if "劣势" in str(x.get("level", "")) or "偏高" in str(x.get("level", "")))
        lines.append(f"🎯 竞品: 监控 {len(comp_rows)} 个, 价格劣势/偏高 {weak} 个")

    st = s.get("库存")
    if st:
        lines.append(f"📦 库存: 无库存 {st['zero']} 个 / 低库存 {st['low']} 个 (共 {st['total']} 商品)")
    else:
        lines.append("📦 库存: 待采集货源")

    cs = s.get("客服")
    if cs:
        qa = cs.get("qa") or {}
        lines.append(f"💬 客服: 过度承诺 {len(qa.get('over_promise', []))} 处 / "
                     f"负面会话 {len(qa.get('negative', []))} 条")
    else:
        lines.append("💬 客服: 待导入聊天 CSV")

    lines.append("═" * 40)
    todos = r.get("todos") or []
    if todos:
        lines.append(f"✅ 今日优先待办 ({len(todos)} 项):")
        for i, (pri, text) in enumerate(todos, 1):
            lines.append(f"  {i}. [{pri}] {text}")
    else:
        lines.append("✅ 今日待办: 暂无明显风险, 保持观察")

    if r.get("ai_summary"):
        lines.append("")
        lines.append("🤖 AI 总结: " + r["ai_summary"])

    lines.append("")
    lines.append("（本早报为只读聚合, 数据来自已导入模块; 请到对应功能页查看详情后再决策）")
    return "\n".join(lines)


if __name__ == "__main__":
    r = build({})
    print(render_report(r))
