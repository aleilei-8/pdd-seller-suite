#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""客服话术助手 (方案①: 只读建议 + 人工复制, 绝不自动发送)。

工作流:
  买家问题(可能含隐私) → 强制脱敏 → [可选 LLM] 生成建议话术 → 合规护栏 → 展示/一键复制

安全约束 (硬性):
- 本模块不登录拼多多、不打开任何页面、不发送任何消息、不执行写操作。
- 送 LLM 的文本只有脱敏后的买家问题 + 公开商品信息, 绝不带原始 PII。
- 生成的每条话术都经过 compliance.guard: 极限词/违禁词替换 + 隐私再脱敏。
- 输出定位为「建议」, 需人工确认后复制到商家后台发送。

离线可用: 未配置模型时, 用内置 FAQ 模板库按关键词匹配, 生成标准话术。
用户可在 data/cs_faq.json 里增删自己的话术(不入库)。
"""
import json
from pathlib import Path

try:
    from paths import ROOT, DATA
except Exception:
    ROOT = Path(__file__).resolve().parent.parent
    DATA = ROOT / "data"

import compliance

FAQ_PATH = DATA / "cs_faq.json"

# 内置标准话术库 (可离线直接使用)。key 为触发关键词(任一命中即可)。
BUILTIN_FAQ = [
    {
        "keys": ["发货", "什么时候发", "几天发货", "多久发货", "啥时候发"],
        "answer": "亲, 下单后一般 48 小时内安排发出, 具体以物流更新为准, 您可以在订单页查看进度~",
    },
    {
        "keys": ["物流", "快递", "单号", "到哪了", "查件"],
        "answer": "亲, 您的订单发出后物流单号会同步到订单页, 请在『我的订单-查看物流』里查询实时进度哦~",
    },
    {
        "keys": ["退货", "换货", "退款", "怎么退", "七天"],
        "answer": "亲, 商品支持平台正常的售后申请, 如遇问题请在订单页提交售后, 我们会尽快为您处理~",
    },
    {
        "keys": ["尺寸", "尺码", "规格", "多大", "颜色", "型号"],
        "answer": "亲, 具体尺寸/规格请您参考商品详情页的参数哦, 如仍有疑问可以把需要的规格发我, 我帮您确认~",
    },
    {
        "keys": ["便宜", "优惠", "降价", "最低", "少点", "砍价"],
        "answer": "亲, 当前已是平台活动价, 优惠以页面显示为准, 有促销活动我们会第一时间同步~",
    },
    {
        "keys": ["质量", "正品", "假的", "保障"],
        "answer": "亲, 商品均为正规渠道货源, 如有质量问题请通过平台售后处理, 我们会负责到底~",
    },
    {
        "keys": ["发票", "开票"],
        "answer": "亲, 如需发票请在订单里备注开票信息, 我们会按平台规则为您处理~",
    },
    {
        "keys": ["缺货", "没货", "补货", "断货"],
        "answer": "亲, 该商品暂时缺货, 您可以点『到货通知』, 补货后会第一时间提醒您~",
    },
    {
        "keys": ["催单", "快点", "急用", "加急"],
        "answer": "亲, 理解您的心情, 我们已经加急处理, 会尽快安排发出, 请耐心等待一下哦~",
    },
]

LLM_SYS = (
    "你是拼多多商家的客服话术助手。根据买家问题和商品信息, 生成一段可直接复制的中文客服回复。\n"
    "硬性要求:\n"
    "1. 只输出回复话术本身, 不要解释、不要加引号以外的任何前后缀。\n"
    "2. 严禁使用广告法极限词(最好/第一/全网最低/绝对等)和平台违禁词(微信/线下/好评返现/刷单等)。\n"
    "3. 不做无法兑现的承诺(包赔/假一赔十/无条件退/无效退款等)。\n"
    "4. 不索要或复述买家的手机号/地址/订单号等隐私; 若问题里出现, 视为已脱敏占位符。\n"
    "5. 语气亲切专业, 100 字以内, 用『亲』开头, 结尾引导在平台内操作。\n"
    "你生成的是建议话术, 卖家会人工确认后发送。"
)


def load_faq():
    """读取用户自定义话术 (data/cs_faq.json), 失败返回空列表。"""
    try:
        data = json.loads(FAQ_PATH.read_text(encoding="utf-8"))
        return data if isinstance(data, list) else []
    except Exception:
        return []


def save_faq(items):
    """保存用户自定义话术到 data/ (不入库)。"""
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        FAQ_PATH.write_text(json.dumps(items, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return True
    except Exception:
        return False


def _local_match(question, custom=None):
    """本地关键词匹配 -> 返回 (answer, matched_key) 或 (None, None)。"""
    q = (question or "").lower()
    pool = (custom or []) + BUILTIN_FAQ
    best = None
    for item in pool:
        keys = item.get("keys") or []
        hit = [k for k in keys if k and k.lower() in q]
        if hit and (best is None or max(len(k) for k in hit) > best[1]):
            best = (item.get("answer") or "", max(len(k) for k in hit))
    return (best[0], best[1]) if best else (None, None)


def generate(question, product_info="", use_llm=True, cfg=None):
    """生成建议话术。

    返回 dict:
      reply      合规后的可复制话术 (已脱敏, 已替换违禁词)
      raw        合规处理前的草稿 (仅本地展示, 不用于发送)
      source     'llm' / 'local' / 'none'
      findings   违规词命中
      passed     是否无高风险词
      summary    合规摘要
      privacy_hits 脱敏记录
      notice     安全声明
    """
    q_clean, q_hits = compliance.desensitize(question or "")
    info_clean, info_hits = compliance.desensitize(product_info or "")
    privacy_hits = q_hits + info_hits

    draft, source = None, "none"
    # 1) 尝试 LLM (需已配置 api_key; 导入避免循环)
    if use_llm:
        try:
            import ai_assistant
            llm_cfg = cfg or ai_assistant.load_config()
            if llm_cfg.get("api_key"):
                user_msg = f"买家问题: {q_clean}\n"
                if info_clean:
                    user_msg += f"商品公开信息: {info_clean}\n"
                out = ai_assistant.llm_chat([
                    {"role": "system", "content": LLM_SYS},
                    {"role": "user", "content": user_msg},
                ], llm_cfg, timeout=45)
                if out:
                    draft = out.strip().strip('"').strip("'")
                    source = "llm"
        except Exception:
            draft = None

    # 2) LLM 不可用/失败 → 本地模板兜底
    if not draft:
        ans, _key = _local_match(q_clean, load_faq())
        if ans:
            draft = ans
            source = "local"

    if not draft:
        return {
            "reply": "", "raw": "", "source": "none",
            "findings": [], "passed": True,
            "summary": "未匹配到标准话术, 也未接入模型。可点『话术管理』补充, 或配置 AI 模型。",
            "privacy_hits": privacy_hits,
            "notice": compliance.PRIVACY_NOTICE,
        }

    g = compliance.guard(draft)
    return {
        "reply": g["safe_text"],
        "raw": draft,
        "source": source,
        "findings": g["findings"],
        "passed": g["passed"],
        "summary": g["summary"],
        "privacy_hits": privacy_hits,
        "notice": compliance.PRIVACY_NOTICE,
    }


if __name__ == "__main__":
    for q in ["亲, 这个全网最低价能便宜点吗? 我电话是13812345678",
              "什么时候发货?", "加微信给你好评返现"]:
        r = generate(q, use_llm=False)
        print("Q:", q)
        print("  ->", r["source"], "|", r["reply"])
        print("  ", r["summary"][:60])
