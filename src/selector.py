#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
selector.py —— 选品评分器(接入运费感知定价引擎)

维度与权重:
  - 利润空间(0.45): 用 pricing.suggest_price 在"目标净利率"下算出的真实净利/净利率评分
  - 需求信号(0.30): 1688 销量等级(10万+ > 万 > 千 > 其他)
  - 上架风险(0.25): 敏感词/品牌词扣分(限流/侵权)
输出: 综合分 + 等级 + 建议售价 + 预估净利 + 净利率 + 建议。
"""
from models import Product
from pricing import PricingConfig, suggest_price, commission_rate_for


def _demand_level(sale_text: str) -> float:
    t = str(sale_text or "")
    if "10万" in t or ("万" in t and (int("".join(filter(str.isdigit, t)) or 0) >= 5)):
        return 1.0
    if "万" in t:
        return 0.8
    n = int("".join(filter(str.isdigit, t)) or 0)
    if n >= 5000:
        return 0.7
    if n >= 1000:
        return 0.5
    if n >= 100:
        return 0.3
    return 0.15


# 风险词(标题/属性含这些 → 上架易被审核/限流/侵权)
RISK_KEYS = ["乳头", "凸点", "性感", "丰胸", "内衣", "胸罩", "情趣", "裸", "透视",
             "私处", "媚", "诱惑", "丝袜", "女仆", "御姐"]


def _brand_of(p: Product) -> str:
    raw = p.attributes.get("品牌")
    if not raw:
        return ""
    b = raw[0] if isinstance(raw, list) else raw
    if b in ("其他", "无", "否", "", "无品牌", "不限定", "不适用"):
        return ""
    return b


def score_product(p: Product, config: PricingConfig = None, weight_kg: float = 0.0):
    """返回评分 dict(含定价引擎给出的建议售价/净利/净利率)"""
    config = config or PricingConfig()
    brand = _brand_of(p)

    # ---- 利润空间: 用定价引擎反算建议售价, 再看真实净利/净利率 ----
    pb = suggest_price(p.price, p.express_fee, 0.0, config,
                       category=p.category, weight_kg=(weight_kg or p.weight_kg))
    suggest_price_val = pb.revenue
    net_profit = pb.net_profit
    net_margin = pb.net_margin / 100.0

    # 利润分(0-100): 净利率≥25% 满分基础, 单件净利≥¥5 再加分(双门槛)
    profit_score = min(100, max(0,
                                (net_margin / 0.25) * 50 + min(net_profit / 5.0, 1.0) * 50))

    # ---- 需求信号 ----
    demand = _demand_level(p.sale_count_text)
    demand_score = demand * 100

    # ---- 上架风险 ----
    risk_score = 100
    notes = []
    title_low = (p.title or "")
    if any(k in title_low for k in RISK_KEYS):
        risk_score -= 30
        notes.append("标题含敏感词, 上架易限流, 需改写")
    if brand:
        risk_score -= 30
        notes.append(f"含品牌词[{brand}], 除非授权否则侵权风险")
    risk_score = max(0, min(100, risk_score))

    total = profit_score * 0.45 + demand_score * 0.30 + risk_score * 0.25

    if total >= 75:
        grade = "强烈推荐"
    elif total >= 60:
        grade = "可上架"
    elif total >= 45:
        grade = "谨慎"
    else:
        grade = "淘汰"

    if net_profit < 2:
        notes.append(f"预估单件净利仅¥{net_profit:.2f}, 利润薄(目标净利率{config.target_net_margin*100:.0f}%)")
    if demand < 0.3:
        notes.append("1688销量信号弱, 需求待验证")
    if p.express_fee <= 0 and p.source == "1688":
        notes.append("未取到1688进货运费, 利润可能虚高, 建议补全 express_fee")

    return {
        "item_id": p.item_id,
        "title": p.title,
        "category": p.category,
        "cost": round(p.price + p.express_fee, 2),
        "cost_price": p.price,
        "inbound_shipping": p.express_fee,
        "outbound_shipping": pb.outbound_shipping,
        "commission_rate": round(commission_rate_for(p.category), 4),
        "suggest_price": suggest_price_val,
        "net_profit": net_profit,
        "net_margin": round(net_margin * 100, 1),
        "demand_level": round(demand, 2),
        "profit_score": round(profit_score, 1),
        "demand_score": round(demand_score, 1),
        "risk_score": round(risk_score, 1),
        "total": round(total, 1),
        "grade": grade,
        "notes": notes,
    }


def score_all(products: list, config: PricingConfig = None, weight_kg: float = 0.0):
    config = config or PricingConfig()
    return [score_product(p, config, weight_kg) for p in products]


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __file__.rsplit("\\src", 1)[0].replace("\\", "/") + "/src")
    from loader import load_from_cyj_cache
    prods = load_from_cyj_cache()
    cfg = PricingConfig()
    print("目标净利率:", cfg.target_net_margin * 100, "%\n")
    for s in score_all(prods, cfg):
        print(f"[{s['grade']:>4}] {s['total']:>5} | {s['item_id']} | 成本¥{s['cost']}→建议¥{s['suggest_price']} 净利¥{s['net_profit']}({s['net_margin']}%)")
        print(f"        利润{s['profit_score']} 需求{s['demand_score']} 风险{s['risk_score']} | {s['notes']}")
