#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""客服响应时效看板 (方案⑤: 纯统计, 无任何写操作)。

数据来源: 复用 cs_qa.parse_chat_csv() 解析的客服聊天记录(已脱敏)。
统计维度:
  - 首响时间: 每个买家会话从买家首条消息到客服首次回复的间隔(分钟)。
  - 未回复会话: 有买家消息但无客服回复的会话数。
  - 咨询高峰: 按小时统计买家消息量, 找出高峰时段(用于排班)。
  - 达标率: 首响 ≤ 阈值(默认 5 分钟) 的会话占比。

安全约束: 只读统计; 会话标识与消息均已脱敏; 不登录、不发送、不落原始 PII。
"""
from collections import Counter, defaultdict
import re

import compliance
import cs_qa


def _parse_minute(t):
    """从时间字符串提取可比较的分钟数(HH:MM 或 HH:MM:SS)。"""
    m = re.search(r"(\d{1,2})\s*[:：]\s*(\d{2})", str(t or ""))
    if m:
        return int(m.group(1)) * 60 + int(m.group(2))
    return None


def analyze(rows, first_reply_target_min=5):
    """响应时效统计 -> result dict。"""
    rows = rows or []
    # 按顺序邻接分组: 买家消息开启一个会话, 其后的客服回复归属该会话,
    # 直到下一个买家消息开始新会话。真实聊天导出里客服行通常没有买家标识,
    # 因此不能按 buyer 字段分组(那样会把所有客服 reply 归到"匿名会话")。
    sessions = []
    current = None
    for r in rows:
        if r.side == "buyer":
            current = {"buyer": r.buyer, "msgs": []}
            sessions.append(current)
        if r.side in ("buyer", "staff"):
            if current is None:
                current = {"buyer": r.buyer, "msgs": []}
                sessions.append(current)
            current["msgs"].append(r)

    first_replys = []
    unanswered = 0
    for sess in sessions:
        msgs = sess["msgs"]
        buyer_first, staff_first = None, None
        for m in msgs:
            t = _parse_minute(m.time)
            if m.side == "buyer" and buyer_first is None and t is not None:
                buyer_first = t
            elif m.side == "staff" and staff_first is None and t is not None:
                staff_first = t
        if buyer_first is not None and staff_first is None:
            unanswered += 1
        if buyer_first is not None and staff_first is not None and staff_first >= buyer_first:
            first_replys.append(staff_first - buyer_first)

    # 咨询高峰(按小时)
    hourly = Counter()
    for r in rows:
        if r.side == "buyer":
            m = re.search(r"(\d{1,2})\s*[:：]", str(r.time or ""))
            if m:
                hourly[int(m.group(1))] += 1
    peak = hourly.most_common(3)

    total = len(first_replys)
    avg = round(sum(first_replys) / total, 1) if total else 0.0
    within = sum(1 for x in first_replys if x <= first_reply_target_min)
    rate = round(within / total * 100, 1) if total else 0.0

    return {
        "total_sessions": len(sessions),
        "total_messages": len(rows),
        "buyer_messages": sum(1 for r in rows if r.side == "buyer"),
        "staff_messages": sum(1 for r in rows if r.side == "staff"),
        "replied_sessions": total,
        "unanswered_sessions": unanswered,
        "avg_first_reply_min": avg,
        "max_first_reply_min": max(first_replys) if first_replys else 0,
        "target_min": first_reply_target_min,
        "within_target": within,
        "target_rate": rate,
        "peak_hours": peak,
        "notice": compliance.PRIVACY_NOTICE,
    }


def render_report(result):
    """中文时效看板报告(给 GUI / AI 助手)。"""
    r = result or {}
    lines = [f"⏱ 客服响应时效看板",
             f"会话 {r.get('total_sessions', 0)} 个 | 消息 {r.get('total_messages', 0)} 条 "
             f"(买家 {r.get('buyer_messages', 0)} / 客服 {r.get('staff_messages', 0)})", ""]
    lines.append(f"· 平均首次响应: {r.get('avg_first_reply_min', 0)} 分钟")
    lines.append(f"· 最慢首次响应: {r.get('max_first_reply_min', 0)} 分钟")
    lines.append(f"· 达标率(≤{r.get('target_min', 5)}分钟): {r.get('target_rate', 0)}% "
                 f"({r.get('within_target', 0)}/{r.get('replied_sessions', 0)})")
    lines.append(f"· 未回复会话: {r.get('unanswered_sessions', 0)} 个")
    peaks = r.get("peak_hours") or []
    if peaks:
        lines.append("· 咨询高峰(小时→消息数): " + "  ".join(f"{h}:00 → {c}" for h, c in peaks))
    # 建议
    lines.append("")
    if r.get("target_rate", 0) < 80:
        lines.append("💡 建议: 首响达标率偏低, 可考虑高峰时段增加客服或设置常见问题的快捷短语。")
    if r.get("unanswered_sessions", 0) > 0:
        lines.append(f"💡 建议: 有 {r['unanswered_sessions']} 个会话未回复, 请人工回访, 避免差评。")
    if peaks and peaks[0][1] >= 5:
        lines.append(f"💡 建议: {peaks[0][0]}:00 左右为咨询高峰, 可提前安排值班。")
    lines.append("")
    lines.append(r.get("notice", compliance.PRIVACY_NOTICE))
    return "\n".join(lines)


if __name__ == "__main__":
    import tempfile, os
    sample = ["时间,角色,买家,内容",
              "09:00,买家,张三,在吗", "09:03,客服,,在的亲",
              "10:00,买家,李四,发货了吗", "10:20,买家,王五,尺寸",
              "14:00,买家,赵六,多少钱", "14:01,客服,,活动价"]
    p = os.path.join(tempfile.gettempdir(), "cs_sla_demo.csv")
    with open(p, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(sample))
    rows, _ = cs_qa.parse_chat_csv(p)
    print(render_report(analyze(rows)))
