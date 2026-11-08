#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
listing.py —— 上架数据生成(数据驱动, 安全无副作用)

把 1688 货源 → 可直接上架的拼多多商品清单(全量搬迁版):
  · 标题用 title_opt 优化(含推流词)
  · 售价用 pricing 引擎反算(含运费+佣金+目标利润)
  · 多规格商品: 每个 SKU 按各自 1688 进价单独反算建议售价(避免统一售价亏损)
  · 全量携带: 轮播主图 / 详情图 / SKU维度 / 每款价+库存+规格图 / 属性 / 1688类目名
  · 输出 CSV: 供人工核对或商家后台批量上传; 也可一键发布到拼多多(pdd_publish)
"""
import csv
import json
import copy
from models import Product, ListingCandidate
from pricing import PricingConfig, suggest_price, commission_rate_for
import title_opt


def _sku_suggest_price(combo: dict, p: Product, config: PricingConfig,
                       weight_kg: float = 0.0) -> float:
    """单个 SKU: 用其自身 1688 进价反算拼多多建议售价。"""
    try:
        cost = float(combo.get("price") or 0)
    except (TypeError, ValueError):
        cost = 0.0
    if cost <= 0:
        cost = p.price
    try:
        pb = suggest_price(cost, p.express_fee, 0.0, config, category=p.category,
                           weight_kg=(weight_kg or p.weight_kg))
        return round(pb.revenue, 2)
    except Exception:
        return 0.0


def build_listings(products: list, config: PricingConfig = None,
                   terms_map: dict = None, weight_kg: float = 0.0) -> list:
    """products: list[Product] ; terms_map: {item_id: [推流词]}"""
    config = config or PricingConfig()
    out = []
    for p in products:
        terms = (terms_map or {}).get(p.item_id) or []
        try:
            r = title_opt.optimize(p, extra_terms=terms)
            best_title = r["candidates"][0]["title"] if r["candidates"] else p.title
        except Exception:
            best_title = p.title
        pb = suggest_price(p.price, p.express_fee, 0.0, config, category=p.category,
                           weight_kg=(weight_kg or p.weight_kg))
        cr = commission_rate_for(p.category, config.commission_rate)

        sku_combos = []
        for c in copy.deepcopy(p.sku_combos or []):
            c["suggest_price"] = _sku_suggest_price(c, p, config, weight_kg)
            c["stock"] = int(c.get("stock") or 0)
            sku_combos.append(c)

        out.append(ListingCandidate(
            item_id=p.item_id, title=best_title,
            cost_price=p.price, inbound_shipping=p.express_fee,
            outbound_shipping=pb.outbound_shipping, commission_rate=cr,
            suggest_price=pb.revenue, net_profit=pb.net_profit,
            net_margin=pb.net_margin, pdd_terms=terms,
            images=list(p.images or []),
            item_url=p.item_url,
            grade="",
            detail_images=list(p.detail_images or []),
            sku_dims=copy.deepcopy(p.sku_dims or []),
            sku_combos=sku_combos,
            attributes=copy.deepcopy(p.attributes or {}),
            category_name=p.category_name or p.category or "",
            detail_html=p.detail_html or "",
        ))
    return out


def export_csv(candidates: list, path: str):
    """导出全量搬迁清单: 基础列 + 图片/SKU/属性列。"""
    cols = ["item_id", "title", "cost_price", "inbound_shipping",
            "outbound_shipping", "commission_rate", "suggest_price",
            "net_profit", "net_margin", "pdd_terms", "images", "detail_images",
            "category_name", "sku_dims", "sku_combos", "attributes", "item_url"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(cols)
        for c in candidates:
            d = c.to_dict() if hasattr(c, "to_dict") else dict(c)
            w.writerow([
                d.get("item_id", ""), d.get("title", ""), d.get("cost_price", 0),
                d.get("inbound_shipping", 0), d.get("outbound_shipping", 0),
                d.get("commission_rate", 0), d.get("suggest_price", 0),
                d.get("net_profit", 0), d.get("net_margin", 0),
                " / ".join(d.get("pdd_terms") or []),
                " | ".join(d.get("images") or []),
                " | ".join(d.get("detail_images") or []),
                d.get("category_name", ""),
                json.dumps(d.get("sku_dims") or [], ensure_ascii=False),
                json.dumps(d.get("sku_combos") or [], ensure_ascii=False),
                json.dumps(d.get("attributes") or {}, ensure_ascii=False),
                d.get("item_url", ""),
            ])
    return path


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __file__.rsplit("\\src", 1)[0].replace("\\", "/") + "/src")
    from loader import load_from_cyj_cache
    prods = load_from_cyj_cache()
    cs = build_listings(prods, PricingConfig())
    for c in cs[:3]:
        print(f"{c.item_id} | ¥{c.suggest_price} | 净利¥{c.net_profit} | "
              f"主图{len(c.images)} 详情{len(c.detail_images)} SKU{len(c.sku_combos)} | {c.title[:30]}")
