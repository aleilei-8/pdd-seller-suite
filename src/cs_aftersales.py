#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""售后处理建议 (方案④: 只读建议 + 人工决策, 绝不代替执行)。

数据来源: 拼多多商家后台 → 售后/退款列表, 手动导出 CSV。
列名柔性映射; 缺列按空处理。

规则引擎(懂生意的部分) —— 只给建议与理由, 不点任何按钮:
  - 仅退款 + 未发货        → 建议同意(省运费, 避免平台介入)
  - 七天无理由 + 已发货    → 建议同意(合规要求), 提醒验收
  - 质量问题 + 有凭证      → 建议同意并道歉
  - 仅退款 + 已发货 + 高价 → 建议协商(退货退款), 保护成本
  - 诉求含违禁词/疑似恶意  → 建议提交平台, 不私下了结
  - 重复退款/超期          → 建议拒绝并给出平台依据

安全约束 (硬性):
- 全程只读: 本模块不登录、不点击同意/拒绝、不修改任何售后单。
- 所有买家信息先脱敏; 送 LLM 前再脱敏一次; 报告不落原始 PII。
- 生成的处理话术走 compliance.guard 合规检查。
"""
import csv
import re
from dataclasses import dataclass, asdict

import compliance

COLUMN_MAP = {
    "order":    ["订单号", "订单编号", "单号", "order", "order_id"],
    "goods":    ["商品", "商品名称", "商品标题", "货品", "goods", "product"],
    "buyer":    ["买家", "客户", "买家昵称", "昵称", "buyer"],
    "amount":   ["退款金额", "金额", "申请金额", "退款钱数", "amount", "refund"],
    "shipped":  ["是否发货", "发货状态", "已发货", "物流状态", "shipped"],
    "reason":   ["退款原因", "售后原因", "原因", "诉求", "申请原因", "reason"],
    "reason_detail": ["问题描述", "详情", "说明", "买家描述", "description", "detail"],
    "days":     ["下单天数", "购买天数", "订单天数", "days"],
}

HIGH_VALUE = 100.0  # 高于此金额且已发货的仅退款 → 建议协商退货退款


def _norm_header(h):
    return re.sub(r"[\s（）()%％:：/\\-]", "", str(h or "")).lower()


def _build_lookup(headers):
    norm = {_norm_header(h): h for h in headers}
    lookup = {}
    for target, cands in COLUMN_MAP.items():
        for c in cands:
            cn = _norm_header(c)
            if cn in norm:
                lookup[target] = norm[cn]
                break
    return lookup


def _num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").replace("¥", "").replace("元", "").strip() or default)
    except (TypeError, ValueError):
        return default


def _bool_shipped(v):
    s = str(v or "").strip().lower()
    # 先判否定, 避免「未发货」被「发货」命中
    if any(w in s for w in ("未发", "没发", "待发", "不发货", "false", "no", "0")):
        return False
    return any(w in s for w in ("已发", "发货", "已签收", "yes", "true", "1", "是"))


@dataclass
class AfterSaleRow:
    order: str = ""
    goods: str = ""
    amount: float = 0.0
    shipped: bool = False
    reason: str = ""
    reason_detail: str = ""
    days: float = 0.0
    suggestion: str = ""
    decision: str = ""       # 同意 / 协商 / 拒绝 / 提交平台 / 人工复核
    rationale: str = ""
    reply_safe: str = ""     # 合规后的建议回复话术

    def to_dict(self):
        return asdict(self)


def parse_aftersales_csv(path):
    """解析售后 CSV -> list[AfterSaleRow] (所有文本字段脱敏)。"""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        lookup = _build_lookup(reader.fieldnames or [])
        for r in reader:
            def g(key, default=""):
                col = lookup.get(key)
                return r.get(col, default) if col else default
            reason = compliance.desensitize(str(g("reason")).strip())[0]
            detail = compliance.desensitize(str(g("reason_detail")).strip())[0]
            goods = compliance.desensitize(str(g("goods")).strip())[0]
            rows.append(AfterSaleRow(
                order=compliance.desensitize(str(g("order")).strip())[0][:24],
                goods=goods[:40],
                amount=_num(g("amount")),
                shipped=_bool_shipped(g("shipped")),
                reason=reason,
                reason_detail=detail[:120],
                days=_num(g("days")),
            ))
    return rows


# 决策话术模板(均合规, 不含极限词/承诺词)
REPLY = {
    "同意_未发货": "亲, 您的申请我们已同意, 款项将按平台规则原路退回, 请留意退款进度~",
    "同意_七天": "亲, 已为您处理售后申请, 商品请按平台要求寄回, 我们收到后尽快为您退款~",
    "同意_质量": "亲, 非常抱歉给您带来不便, 已为您同意售后, 如有疑问请在平台内联系客服~",
    "协商_退货": "亲, 商品已发出, 为兼顾双方, 建议申请退货退款, 我们收到后第一时间处理~",
    "提交平台": "亲, 您的情况已记录, 建议通过平台售后通道提交凭证, 平台会协助处理~",
    "拒绝_超期": "亲, 该申请已超出平台售后时效, 建议您在订单页查看售后规则, 如有疑问可联系平台客服~",
    "人工复核": "亲, 您的申请我们已收到, 正在核实中, 会在平台规定时限内给您答复~",
}


def decide(row):
    """单条售后规则决策 -> 填充 decision/rationale/suggestion/reply_safe。"""
    reason = (row.reason + " " + row.reason_detail).lower()
    text = row.reason_detail or row.reason

    # 疑似恶意/违禁诉求 → 提交平台
    if any(w in reason for w in ("退款不退货", "仅退款不退", "白拿", "不退货就要钱")) \
            or compliance.scan(text):
        row.decision = "提交平台"
        row.rationale = "诉求含违禁词或疑似恶意, 不建议私下了结, 提交平台留凭证"
        row.suggestion = "提交平台介入, 保留聊天与商品凭证, 不线下私下退款"
        row.reply_safe = compliance.guard(REPLY["提交平台"])["safe_text"]
        return row

    # 质量问题 + 有凭证
    if any(w in reason for w in ("质量", "破损", "坏", "少件", "错发", "发错", "瑕疵")):
        row.decision = "同意"
        row.rationale = "质量问题且有明确诉求, 同意并道歉, 降低纠纷升级风险"
        row.suggestion = "同意售后, 视情况承担运费, 记录问题批次"
        row.reply_safe = compliance.guard(REPLY["同意_质量"])["safe_text"]
        return row

    # 七天无理由
    if "七天" in reason or "无理由" in reason:
        row.decision = "同意"
        row.rationale = "七天无理由属于平台合规要求, 建议同意, 提醒买家保持商品完好"
        row.suggestion = "同意退货退款, 确认商品不影响二次销售"
        row.reply_safe = compliance.guard(REPLY["同意_七天"])["safe_text"]
        return row

    # 仅退款 + 未发货
    if ("仅退款" in reason or "不想要" in reason or "拍错" in reason) and not row.shipped:
        row.decision = "同意"
        row.rationale = "未发货仅退款, 同意成本最低, 避免平台介入与发货运费损失"
        row.suggestion = "同意仅退款, 及时拦截订单避免误发"
        row.reply_safe = compliance.guard(REPLY["同意_未发货"])["safe_text"]
        return row

    # 仅退款 + 已发货 + 高价 → 协商
    if ("仅退款" in reason or "退款" in reason) and row.shipped and row.amount >= HIGH_VALUE:
        row.decision = "协商"
        row.rationale = f"已发货且金额¥{row.amount:.0f}≥¥{HIGH_VALUE:.0f}, 仅退款会损失货款, 建议协商退货退款"
        row.suggestion = "协商退货退款, 提供退货地址(平台内), 收到后退款"
        row.reply_safe = compliance.guard(REPLY["协商_退货"])["safe_text"]
        return row

    # 超期
    days = float(row.days or 0)
    if days and days > 15:
        row.decision = "拒绝"
        row.rationale = f"下单已 {days:.0f} 天, 超出常规售后时效, 可按平台规则拒绝"
        row.suggestion = "拒绝并说明平台售后时效依据, 保留沟通记录"
        row.reply_safe = compliance.guard(REPLY["拒绝_超期"])["safe_text"]
        return row

    # 兜底: 人工复核
    row.decision = "人工复核"
    row.rationale = "信息不足以自动判定, 建议人工核对订单状态与凭证"
    row.suggestion = "人工复核后决定, 不轻易承诺退款"
    row.reply_safe = compliance.guard(REPLY["人工复核"])["safe_text"]
    return row


def analyze_all(rows, use_llm=False, cfg=None):
    """批量决策 -> {rows:[...], summary:{...}}。"""
    rows = rows or []
    result = []
    for r in rows:
        result.append(decide(r))
    from collections import Counter
    decisions = Counter(r.decision for r in result)
    out = {
        "rows": [r.to_dict() for r in result],
        "summary": {
            "total": len(result),
            "by_decision": dict(decisions),
            "amount_total": round(sum(r.amount for r in result), 2),
        },
        "notice": compliance.PRIVACY_NOTICE,
    }
    return out


def render_report(result):
    """中文售后建议报告(给 GUI / AI 助手)。"""
    r = result or {}
    s = r.get("summary") or {}
    lines = [f"🛠 售后处理建议 (共 {s.get('total', 0)} 单, "
             f"金额合计 ¥{s.get('amount_total', 0):.2f})",
             "决策分布: " + "  ".join(f"{k}×{v}" for k, v in s.get("by_decision", {}).items()),
             ""]
    for row in r.get("rows", [])[:12]:
        lines.append(f"· {row.get('goods','')[:20]} ¥{row.get('amount',0):.0f} "
                     f"[{'已发' if row.get('shipped') else '未发'}] "
                     f"→ {row.get('decision','')}")
        lines.append(f"    理由: {row.get('rationale','')}")
        lines.append(f"    建议: {row.get('suggestion','')}")
        if row.get("reply_safe"):
            lines.append(f"    话术: {row['reply_safe']}")
    if not r.get("rows"):
        lines.append("(暂无售后数据, 请先在售后建议页导入退款 CSV)")
    lines.append("")
    lines.append(r.get("notice", compliance.PRIVACY_NOTICE))
    lines.append("⚠ 本模块只出建议, 不会自动同意/拒绝任何售后单, 请人工决策执行。")
    return "\n".join(lines)


if __name__ == "__main__":
    import tempfile, os
    sample = ["订单号,商品,买家,退款金额,是否发货,退款原因,问题描述",
              "12345678901,面膜,张三,69,未发货,不想要了,拍错了",
              "12345678902,精华,李四,299,已发货,仅退款,不想要了要退钱",
              "12345678903,口红,王五,59,已发货,破损,收到时盒子破了"]
    p = os.path.join(tempfile.gettempdir(), "cs_as_demo.csv")
    with open(p, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(sample))
    rows = parse_aftersales_csv(p)
    print(render_report(analyze_all(rows)))
