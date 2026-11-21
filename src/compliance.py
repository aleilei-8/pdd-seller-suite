#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""合规护栏 (纯规则 · 离线可用 · 只读)。

客服折中方案的底座: 所有「AI 生成的话术 / 导入的聊天记录」在送模型前、
在展示给客服复制前, 都经过本模块过滤, 确保两件事:

1. 不出现广告法极限词 / 平台违禁词 / 虚假承诺
   —— 只提示与建议替换, 不代替卖家做最终判断。
2. 买家隐私 (手机/地址/姓名/订单号/微信/身份证/银行卡/邮箱) 先脱敏
   —— 送 LLM 的文本必须是脱敏后的; 绝不把原始 PII 发给第三方接口。

设计原则:
- 只读: 本模块不联网、不写店铺、不碰账号, 只做文本扫描与替换。
- 保守: 规则宁可多报(提示), 不漏报; 但是否修改由卖家/客服决定。
- 可解释: 每条命中给出 category / severity / hint, 方便 GUI 展示为什么。

用法:
    scan(text)          -> [finding, ...]              扫描违规词
    desensitize(text)   -> (clean_text, hits)          脱敏隐私
    guard(text)         -> {passed, findings, safe_text, summary}
"""
import re

# ---------- 违禁词表 ----------
# (词/正则, 类别, 严重度 high/medium/low, 替换建议)
# 说明: 广告法《广告用语》极限词 + 电商平台常见违禁表达。仅作风险提示,
#       最终合规判断以平台规则与法务为准。
BANNED = [
    # —— 广告法极限词 ——
    ("最好", "极限词", "high", "体验更佳"),
    ("最佳", "极限词", "high", "优选"),
    ("最强", "极限词", "high", "表现出色"),
    ("最优", "极限词", "high", "优质"),
    ("全网最低价", "极限词", "high", "平台优惠价"),
    ("最低价", "极限词", "high", "优惠价"),
    ("全网最低", "极限词", "high", "平台优惠价"),
    ("史上最低", "极限词", "high", "近期优惠价"),
    ("最便宜", "极限词", "high", "价格实惠"),
    ("第一品牌", "极限词", "high", "优质品牌"),
    ("全网第一", "极限词", "high", "热销"),
    ("销量第一", "极限词", "high", "销量领先"),
    ("排名第一", "极限词", "high", "名列前茅"),
    ("顶级", "极限词", "high", "高品质"),
    ("顶尖", "极限词", "high", "优质"),
    ("极品", "极限词", "high", "精品"),
    ("极致", "极限词", "medium", "出色"),
    ("唯一", "极限词", "high", "独具特色"),
    ("独一无二", "极限词", "high", "颇具特色"),
    ("国家级", "极限词", "high", "优质"),
    ("世界级", "极限词", "high", "高品质"),
    ("绝对", "极限词", "medium", ""),
    ("100%", "极限词", "medium", ""),
    ("百分百", "极限词", "medium", ""),
    ("万能", "极限词", "high", "多用途"),
    ("永久", "极限词", "medium", "长期"),
    ("终身", "极限词", "medium", "长期使用"),
    ("无敌", "极限词", "medium", "出色"),
    ("冠军", "极限词", "medium", "优选"),
    ("王牌", "极限词", "medium", "主推"),
    ("首选", "极限词", "medium", "推荐"),
    ("顶级配置", "极限词", "high", "高配置"),
    (r"最[好佳强优便低高]", "极限词", "medium", "较"),  # 兜底: 最好/最贵等
    (r"第[一1](?:名|品牌|的位置)", "极限词", "high", "领先"),

    # —— 虚假 / 过度承诺 ——
    ("包赔", "虚假承诺", "high", "按平台规则处理"),
    ("假一赔十", "虚假承诺", "high", "正品保障(如属实)"),
    ("无条件退", "虚假承诺", "high", "支持售后申请"),
    ("全额退款", "虚假承诺", "medium", "可申请退款"),
    ("包治", "虚假承诺", "high", "有助于"),
    ("根治", "虚假承诺", "high", "改善"),
    ("药到病除", "虚假承诺", "high", "谨慎表述"),
    ("三天见效", "虚假承诺", "high", "效果因人而异"),
    ("无效退款", "虚假承诺", "high", "谨慎表述"),
    ("终身保修", "虚假承诺", "medium", "质保期内保修"),
    ("全国联保", "虚假承诺", "medium", "按厂家政策保修"),

    # —— 平台违禁: 引导私域 / 虚假交易 ——
    ("加微信", "平台违禁", "high", "请在平台内沟通"),
    ("微信", "平台违禁", "medium", "平台内联系"),
    ("加QQ", "平台违禁", "high", "请在平台内沟通"),
    ("私下交易", "平台违禁", "high", "请通过平台下单"),
    ("线下付款", "平台违禁", "high", "请通过平台支付"),
    ("脱离平台", "平台违禁", "high", "请通过平台交易"),
    ("好评返现", "平台违禁", "high", ""),
    ("好评返红包", "平台违禁", "high", ""),
    ("五星返现", "平台违禁", "high", ""),
    ("刷单", "平台违禁", "high", ""),
    ("刷好评", "平台违禁", "high", ""),
]

# ---------- 隐私脱敏规则 ----------
# (名称, 正则, 掩码). 顺序重要: 长/特定的放前面, 避免被短规则先吃掉。
PII_PATTERNS = [
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "【身份证】"),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "【手机号】"),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "【银行卡】"),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "【邮箱】"),
    ("微信号", re.compile(r"(?:微信号?|vx|weixin|微信ID)\s*[:：]?\s*[A-Za-z0-9_-]{5,20}", re.I), "【微信号】"),
    ("QQ号", re.compile(r"(?:QQ|qq)\s*[:：]?\s*\d{5,12}"), "【QQ号】"),
    ("姓名", re.compile(r"(?:收货人|收件人|姓名|买家)\s*[:：]\s*[\u4e00-\u9fa5]{1,4}(?=[\s，,。；;]|$)"), "姓名：【姓名】"),
    ("订单号", re.compile(r"(?:订单号?|单号|订单编号)\s*[:：]?\s*\d{8,20}"), "订单号：【订单号】"),
    ("地址", re.compile(
        r"[\u4e00-\u9fa5]{2,8}(?:省|市|自治区)[\u4e00-\u9fa50-9]{2,40}?"
        r"(?:号|栋|幢|室|单元|层|楼|小区|花园|大厦|村|镇|街道|路|街|巷)[0-9\u4e00-\u9fa5]{0,8}"), "【地址】"),
    ("长数字", re.compile(r"(?<!\d)\d{11,19}(?!\d)"), "【编号】"),
]

# 送模型前的硬性约束说明(GUI 展示用)
PRIVACY_NOTICE = (
    "合规提示: 本模块只生成『建议话术』, 不会自动发送、不登录店铺、不执行任何写操作。"
    "所有买家个人信息在送模型前均已脱敏; 请人工确认后复制到拼多多后台。"
)

# 已编译的正则型违禁词
_COMPILED = []
for _w, _cat, _sev, _hint in BANNED:
    if _w.startswith("r:"):
        _COMPILED.append((re.compile(_w[2:]), _cat, _sev, _hint, _w[2:]))
    else:
        _COMPILED.append((None, _cat, _sev, _hint, _w))


def scan(text: str):
    """扫描违禁词, 返回命中列表 (不修改文本)。

    每条命中: {keyword, category, severity, hint, start, end, matched}
    """
    text = text or ""
    out = []
    for rx, cat, sev, hint, word in _COMPILED:
        if rx:
            it = rx.finditer(text)
        else:
            # 普通词: 大小写不敏感的字面匹配
            it = re.finditer(re.escape(word), text, re.I)
        for m in it:
            out.append({
                "keyword": word,
                "category": cat,
                "severity": sev,
                "hint": hint,
                "start": m.start(),
                "end": m.end(),
                "matched": m.group(0),
            })
    # 按位置排序, 同起点时更长的匹配优先(避免「全网最低」吃掉「全网最低价」的价)
    out.sort(key=lambda x: (x["start"], -x["end"]))
    dedup = []
    for f in out:
        if dedup and f["start"] < dedup[-1]["end"]:
            rank = {"high": 3, "medium": 2, "low": 1}
            cur = dedup[-1]
            # 更危险 或 同危险但更长 → 采用新命中
            if (rank.get(f["severity"], 0) > rank.get(cur["severity"], 0)
                    or (f["severity"] == cur["severity"]
                        and (f["end"] - f["start"]) > (cur["end"] - cur["start"]))):
                dedup[-1] = f
            continue
        dedup.append(f)
    return dedup


def redact(text: str, findings=None):
    """按扫描结果替换违禁词为建议词 (保留原文结构)。"""
    text = text or ""
    findings = findings if findings is not None else scan(text)
    # 从后往前替换, 避免位置偏移
    for f in sorted(findings, key=lambda x: x["start"], reverse=True):
        hint = f.get("hint") or ""
        text = text[:f["start"]] + hint + text[f["end"]:]
    return text


def desensitize(text: str):
    """脱敏买家隐私 -> (clean_text, hits)。

    hits: [{type, masked}] 记录脱敏了哪些类型 (不记录原值, 避免二次泄露)。
    """
    text = text or ""
    hits = []
    for name, rx, mask in PII_PATTERNS:
        new_text, n = rx.subn(mask, text)
        if n:
            text = new_text
            hits.append({"type": name, "count": n, "masked": mask})
    return text, hits


def guard(text: str, mask_privacy=True):
    """综合护栏: 脱敏隐私 + 扫描违禁词, 返回结构化结果。

    {passed: bool, findings: [...], safe_text: str, privacy_hits: [...], summary: str}
    - passed: 没有 high 级违规 (medium 提示但不阻断复制)
    - safe_text: 脱敏+替换后的可复制文本 (始终不含原始 PII)
    """
    clean, privacy_hits = desensitize(text) if mask_privacy else (text or "", [])
    findings = scan(clean)
    safe = redact(clean, findings)
    high = [f for f in findings if f["severity"] == "high"]
    med = [f for f in findings if f["severity"] == "medium"]
    bits = []
    if privacy_hits:
        bits.append("隐私已脱敏: " + "、".join(f"{h['type']}×{h['count']}" for h in privacy_hits))
    if high:
        bits.append(f"⛔ {len(high)} 处高风险词(已替换), 复制前请确认")
    elif med:
        bits.append(f"⚠ {len(med)} 处提示词(已替换)")
    else:
        bits.append("✅ 未发现违禁词")
    bits.append(PRIVACY_NOTICE)
    return {
        "passed": not high,
        "findings": findings,
        "safe_text": safe,
        "privacy_hits": privacy_hits,
        "summary": "  ".join(bits),
    }


def scan_for_llm(text: str):
    """送模型前的强制脱敏入口: 只返回脱敏文本, 绝不外传原始 PII。"""
    return desensitize(text or "")[0]


if __name__ == "__main__":
    demo = ("买家张三 电话13812345678 地址广东省深圳市南山区科技园1号, "
            "订单号12345678901234, 想要全网最低价, 加微信给你好评返现")
    print("原文:", demo)
    print("\n扫描:", scan(demo))
    print("\n脱敏:", desensitize(demo))
    print("\n护栏:", guard(demo))
