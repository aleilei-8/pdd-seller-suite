#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""客服记录质检 (方案③: 只读分析 + 强制脱敏)。

数据来源: 拼多多商家后台 → 客服/聊天记录, 手动导出 CSV。
列名各版本差异大, 做柔性映射(中文同义词归一), 缺列按空处理。

质检维度:
  - 高频问题: 买家消息聚类, 找出重复咨询点(可用于补充话术库/详情页)。
  - 情绪信号: 负面词命中(生气/投诉/差评/骗人...), 提示需优先跟进的会话。
  - 过度承诺: 客服回复命中违禁词(包赔/全网最低/加微信...), 合规风险点。
  - 响应情况: 有问无答的会话(需人工回访)。

安全约束 (硬性):
- 全程只读: 不登录、不发送、不修改店铺任何数据。
- 送模型前强制脱敏: 任何发给 LLM 的文本都先经 compliance.scan_for_llm(),
  原始手机号/地址/订单号/姓名绝不外传。
- 报告里只展示脱敏后的消息片段, 不落盘原始 PII。
"""
import csv
import re
from collections import Counter
from dataclasses import dataclass, asdict

import compliance

# 目标字段 -> 候选表头
COLUMN_MAP = {
    "time":     ["时间", "消息时间", "日期", "发送时间", "time", "date"],
    "role":     ["角色", "发送方", "身份", "类型", "来源", "role", "sender"],
    "buyer":    ["买家", "客户", "买家昵称", "客户昵称", "买家账号", "昵称", "buyer", "customer"],
    "content":  ["内容", "消息内容", "聊天内容", "消息", "文本", "content", "message", "msg"],
}
# 客服/买家角色判定词
STAFF_WORDS = ["客服", "商家", "卖家", "我", "店小二", "staff", "seller", "merchant"]
BUYER_WORDS = ["买家", "客户", "顾客", "用户", "buyer", "customer", "访客"]
# 负面情绪词
NEGATIVE_WORDS = ["生气", "投诉", "差评", "骗人", "骗子", "太差", "退货", "不满意",
                  "气死", "恶心", "垃圾", "虚假", "欺诈", "举报", "曝光"]
# 高频问题分类词典(命中即归类)
QUESTION_TOPICS = {
    "发货时效": ["发货", "什么时候发", "几天到", "多久发", "催单", "加急"],
    "物流查询": ["物流", "快递", "单号", "到哪", "查件"],
    "退换退款": ["退货", "换货", "退款", "七天", "售后"],
    "价格优惠": ["便宜", "优惠", "降价", "最低", "砍价", "券"],
    "规格参数": ["尺寸", "尺码", "规格", "颜色", "型号", "多大"],
    "质量正品": ["质量", "正品", "真假", "保障"],
    "发票开票": ["发票", "开票"],
}


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


@dataclass
class ChatRow:
    time: str = ""
    role: str = ""       # 原始角色值
    side: str = ""       # 'buyer' / 'staff' / 'unknown'
    buyer: str = ""      # 买家标识(会被脱敏)
    content: str = ""
    content_safe: str = ""   # 脱敏后文本

    def to_dict(self):
        return asdict(self)


def _detect_side(role, buyer):
    r = (role or "").lower()
    for w in STAFF_WORDS:
        if w in r:
            return "staff"
    for w in BUYER_WORDS:
        if w in r:
            return "buyer"
    # 有买家标识但角色空 → 视为买家
    if buyer and not r:
        return "buyer"
    return "unknown"


def parse_chat_csv(path):
    """解析客服聊天记录 CSV -> (list[ChatRow], stats)。

    stats: {total, buyer, staff, unknown, privacy_masked}
    """
    rows = []
    privacy_masked = 0
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        lookup = _build_lookup(reader.fieldnames or [])
        for r in reader:
            def g(key, default=""):
                col = lookup.get(key)
                return r.get(col, default) if col else default
            content = str(g("content")).strip()
            safe, hits = compliance.desensitize(content)
            if hits:
                privacy_masked += 1
            buyer_raw = str(g("buyer")).strip()
            buyer_safe, _ = compliance.desensitize(buyer_raw)
            role = str(g("role")).strip()
            rows.append(ChatRow(
                time=str(g("time")).strip(),
                role=role,
                side=_detect_side(role, buyer_raw),
                buyer=buyer_safe[:20],
                content=content,
                content_safe=safe,
            ))
    counts = Counter(x.side for x in rows)
    return rows, {
        "total": len(rows),
        "buyer": counts.get("buyer", 0),
        "staff": counts.get("staff", 0),
        "unknown": counts.get("unknown", 0),
        "privacy_masked": privacy_masked,
    }


def _classify_topic(text):
    t = (text or "").lower()
    hits = []
    for topic, keys in QUESTION_TOPICS.items():
        if any(k in t for k in keys):
            hits.append(topic)
    return hits or ["其他咨询"]


def analyze(rows, use_llm=False, cfg=None):
    """质检分析 -> result dict (只读, 不写店铺, 不落原始 PII)。"""
    rows = rows or []
    buyer_msgs = [r for r in rows if r.side == "buyer"]
    staff_msgs = [r for r in rows if r.side == "staff"]

    # 高频问题(用脱敏文本统计)
    topics = Counter()
    for r in buyer_msgs:
        for topic in _classify_topic(r.content_safe):
            topics[topic] += 1

    # 负面情绪会话(记录脱敏片段)
    negative = []
    for r in buyer_msgs:
        hits = [w for w in NEGATIVE_WORDS if w in r.content_safe]
        if hits:
            negative.append({"time": r.time, "buyer": r.buyer,
                             "snippet": r.content_safe[:60], "words": hits})
        if len(negative) >= 12:
            break

    # 过度承诺 / 违禁词(只看客服回复)
    over_promise = []
    for r in staff_msgs:
        findings = compliance.scan(r.content_safe)
        high = [f for f in findings if f["severity"] == "high"]
        if high:
            over_promise.append({
                "time": r.time,
                "snippet": r.content_safe[:80],
                "words": [f["keyword"] for f in high],
                "category": [f["category"] for f in high],
            })
        if len(over_promise) >= 12:
            break

    # 有问无答: 买家消息后无客服回复(简单按总量对比 + 末条角色推断)
    unanswered = max(0, len(buyer_msgs) - len(staff_msgs))

    result = {
        "total": len(rows),
        "buyer": len(buyer_msgs),
        "staff": len(staff_msgs),
        "topics": topics.most_common(8),
        "negative": negative,
        "over_promise": over_promise,
        "unanswered": unanswered,
        "risk_level": "高" if over_promise else ("中" if negative else "低"),
        "notice": compliance.PRIVACY_NOTICE,
    }

    # 可选 LLM 总结: 只发送脱敏后的统计与脱敏片段, 绝不发原始消息
    if use_llm:
        try:
            import ai_assistant
            llm_cfg = cfg or ai_assistant.load_config()
            if llm_cfg.get("api_key"):
                payload = {
                    "消息总数": result["total"],
                    "买家消息": result["buyer"],
                    "客服回复": result["staff"],
                    "高频问题": [{"topic": k, "count": v} for k, v in result["topics"]],
                    "负面片段(已脱敏)": [x["snippet"] for x in negative[:6]],
                    "违规片段(已脱敏)": [x["snippet"] for x in over_promise[:6]],
                }
                # 二次保险: 送模型前再脱敏一次
                safe_payload = compliance.scan_for_llm(
                    json_dumps(payload))
                out = ai_assistant.llm_chat([
                    {"role": "system", "content":
                     "你是电商客服质检顾问。基于给定的脱敏统计, 用中文给 3 条可执行的改进建议, "
                     "每条不超过 30 字, 用序号列出。不要复述任何个人信息。"},
                    {"role": "user", "content": safe_payload},
                ], llm_cfg, timeout=45)
                if out:
                    result["ai_advice"] = out.strip()
        except Exception:
            pass
    return result


def json_dumps(obj):
    import json
    return json.dumps(obj, ensure_ascii=False)


def render_report(result):
    """生成中文质检报告(给 GUI 展示 / AI 助手返回)。"""
    r = result or {}
    lines = [f"📋 客服质检报告 (消息 {r.get('total', 0)} 条 "
             f"/ 买家 {r.get('buyer', 0)} / 客服 {r.get('staff', 0)})",
             f"综合风险: {r.get('risk_level', '低')}", ""]
    topics = r.get("topics") or []
    if topics:
        lines.append("高频问题 Top:")
        for topic, cnt in topics:
            lines.append(f"  · {topic}: {cnt} 次")
        lines.append("")
    if r.get("over_promise"):
        lines.append(f"⛔ 客服过度承诺/违禁 {len(r['over_promise'])} 处:")
        for x in r["over_promise"][:6]:
            lines.append(f"  · [{','.join(x['category'])}] {x['snippet']}")
        lines.append("")
    if r.get("negative"):
        lines.append(f"⚠ 负面情绪会话 {len(r['negative'])} 条:")
        for x in r["negative"][:6]:
            lines.append(f"  · {x['snippet']} ({','.join(x['words'])})")
        lines.append("")
    if r.get("unanswered"):
        lines.append(f"📌 疑似有问未答 {r['unanswered']} 条, 建议人工回访。")
    if r.get("ai_advice"):
        lines.append("")
        lines.append("🤖 AI 改进建议:")
        lines.append(r["ai_advice"])
    lines.append("")
    lines.append(r.get("notice", compliance.PRIVACY_NOTICE))
    return "\n".join(lines)


if __name__ == "__main__":
    import tempfile, os
    sample = ["时间,角色,买家,内容",
              "10:01,买家,张三13812345678,这个全网最低价吗 太贵了",
              "10:02,客服,,亲 已是活动价 不支持退换",
              "10:05,买家,李四,什么时候发货"]
    p = os.path.join(tempfile.gettempdir(), "cs_qa_demo.csv")
    with open(p, "w", encoding="utf-8-sig") as f:
        f.write("\n".join(sample))
    rows, st = parse_chat_csv(p)
    print("解析:", st)
    print(render_report(analyze(rows)))
