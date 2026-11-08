#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
invoice.py —— 开票匹配(数据驱动)

代发模式的发票链路:
  · 进项票: 向 1688 供应商采购, 需向供应商索取进货发票(成本凭证)
  · 销项票: 拼多多买家如需发票, 由你(商家)开具销售发票

本模块把"拼多多订单 ↔ 1688 采购成本"逐单匹配, 产出:
  - 采购进项视图(按 1688 货源汇总, 便于向供应商催收进项票)
  - 销售销项视图(按订单, 标注是否需开票)
  - 毛利对照(销售额 - 进货额)
全部为导出, 不碰任何账号。
"""
import csv
from collections import defaultdict
from models import Order
from orders import parse_orders, match_costs
from pricing import PricingConfig


def build_invoice_records(orders: list, products: list, cost_map: dict = None) -> list:
    links = match_costs(orders, products, cost_map)
    rows = []
    for i, o in enumerate(orders):
        p = links.get(i)
        sale = round(o.price * o.qty, 2)
        cost = round((p.price if p else 0.0) * o.qty, 2) if p else 0.0
        rows.append({
            "order_sn": o.order_sn,
            "goods_name": o.goods_name,
            "qty": o.qty,
            "sale_amount": sale,
            "cost_amount": cost,
            "gross": round(sale - cost, 2),
            "supplier_item": p.item_id if p else "",
            "supplier_url": p.item_url if p else "",
            "need_invoice": "是" if o.receiver else "否",
            "linked": "已关联" if p else "未关联",
        })
    return rows


def purchase_view(rows: list) -> list:
    """按 1688 货源聚合进项(便于向供应商催收发票)"""
    agg = defaultdict(lambda: {"qty": 0, "cost": 0.0, "url": ""})
    for r in rows:
        if not r["supplier_item"]:
            continue
        a = agg[r["supplier_item"]]
        a["qty"] += r["qty"]
        a["cost"] += r["cost_amount"]
        a["url"] = r["supplier_url"]
    return [{"supplier_item": k, "qty": v["qty"], "cost_amount": round(v["cost"], 2),
             "supplier_url": v["url"]} for k, v in agg.items()]


def export_csv(rows: list, path: str, view: str = "detail"):
    if view == "purchase":
        data = purchase_view(rows)
        cols = ["supplier_item", "qty", "cost_amount", "supplier_url"]
    else:
        data = rows
        cols = list(rows[0].keys()) if rows else []
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(data)
    return path


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        os_ = parse_orders(sys.argv[1])
        rs = build_invoice_records(os_, [])
        print("销项/进项明细(前3):")
        for r in rs[:3]:
            print(r)
