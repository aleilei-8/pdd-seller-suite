#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
analysis.py —— 店铺 / 利润 分析聚合(数据驱动)

从已关联成本的订单集合里, 按 类目 / 日期 / 商品 三个维度汇总利润,
并产出店铺级 KPI(成交额 / 总成本 / 净利 / 净利率 / 单量 / 关联率)。
同时支持从店铺扫描结果识别"零销量"商品(供下架决策)。
"""
from collections import defaultdict
from models import Order, ProfitRow
from datetime import datetime


def _date_only(s: str) -> str:
    if not s:
        return "未知日期"
    # 兼容 "2026-01-02 13:14" / "2026/01/02" / 时间戳
    s = str(s).strip().replace("/", "-")
    m = __import__("re").match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else s[:10]


def _category_of(o: Order, cat_map: dict = None) -> str:
    if cat_map and o.goods_id in cat_map:
        return cat_map[o.goods_id]
    return o.matched_item and "已关联" or "未关联"


def aggregate(orders: list, dim: str = "category", cat_map: dict = None) -> list:
    """按维度聚合利润。dim ∈ {category, date, product}"""
    buckets = defaultdict(lambda: {
        "orders": 0, "units": 0, "revenue": 0.0, "cost": 0.0,
        "shipping_out": 0.0, "commission": 0.0, "payment_fee": 0.0,
        "net_profit": 0.0,
    })
    for o in orders:
        if dim == "category":
            key = _category_of(o, cat_map)
        elif dim == "date":
            key = _date_only(o.pay_time)
        else:  # product
            key = o.goods_name[:20] or o.goods_id or "未命名"
        b = buckets[key]
        b["orders"] += 1
        b["units"] += o.qty
        b["revenue"] += o.price * o.qty
        b["cost"] += o.cost_price * o.qty + o.inbound_shipping * o.qty
        b["shipping_out"] += o.outbound_shipping * o.qty
        # 佣金/支付费按实际(订单可能含整单值), 这里按件近似已在 enrich 内算好 net
        b["net_profit"] += o.net_profit

    rows = []
    for k, b in buckets.items():
        margin = (b["net_profit"] / b["revenue"]) if b["revenue"] > 0 else 0.0
        rows.append(ProfitRow(
            key=k, orders=b["orders"], units=b["units"],
            revenue=round(b["revenue"], 2), cost=round(b["cost"], 2),
            shipping_out=round(b["shipping_out"], 2),
            commission=round(b["commission"], 2), payment_fee=round(b["payment_fee"], 2),
            net_profit=round(b["net_profit"], 2), net_margin=round(margin * 100, 2),
        ))
    rows.sort(key=lambda r: r.net_profit, reverse=True)
    return rows


def store_kpi(orders: list) -> dict:
    total_rev = sum(o.price * o.qty for o in orders)
    total_cost = sum((o.cost_price + o.inbound_shipping) * o.qty for o in orders)
    total_ship = sum(o.outbound_shipping * o.qty for o in orders)
    total_net = sum(o.net_profit for o in orders)
    linked = sum(1 for o in orders if o.matched_item)
    margin = (total_net / total_rev) if total_rev > 0 else 0.0
    return {
        "orders": len(orders),
        "units": sum(o.qty for o in orders),
        "revenue": round(total_rev, 2),
        "cost": round(total_cost, 2),
        "shipping_out": round(total_ship, 2),
        "net_profit": round(total_net, 2),
        "net_margin": round(margin * 100, 2),
        "linked_rate": round(linked / len(orders) * 100, 1) if orders else 0.0,
        "avg_order_value": round(total_rev / len(orders), 2) if orders else 0.0,
    }


def detect_zero_sales(store_items: list) -> list:
    """从店铺扫描结果(store_products.json 的 items)筛零销量商品"""
    return [it for it in store_items if (it.get("sales", 0) or 0) == 0]


def summarize(orders: list) -> dict:
    """一键汇总: KPI + 三维度利润表"""
    return {
        "kpi": store_kpi(orders),
        "by_category": [r.to_dict() for r in aggregate(orders, "category")],
        "by_date": [r.to_dict() for r in aggregate(orders, "date")],
        "by_product": [r.to_dict() for r in aggregate(orders, "product")],
    }


if __name__ == "__main__":
    import json, sys
    if len(sys.argv) > 1:
        from orders import parse_orders
        os_ = parse_orders(sys.argv[1])
        k = store_kpi(os_)
        print("KPI:", k)
        print("按类目:", [r.to_dict() for r in aggregate(os_, "category")][:5])
