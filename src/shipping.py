#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
shipping.py —— 发货计划生成(数据驱动, 一件代发)

流程:
  1. 导入拼多多订单(orders.parse_orders)
  2. 关联到 1688 货源(orders.match_costs)
  3. 生成"采购+发货"计划: 每个 1688 货源要买几件、发给谁(拼多多买家地址)
     —— 一件代发本质是: 把拼多多买家收货信息透传给 1688 供应商代发
  4. 导出 CSV: 供你在 1688 下单/填地址使用(或后续接 1688 自动下单)

安全: 只读取与导出, 不触碰任何账号。
"""
import csv
from models import Order
from orders import parse_orders, match_costs
from pricing import PricingConfig, REMOTE_PROVINCES


def build_ship_plan(orders: list, products: list, cost_map: dict = None) -> list:
    """返回发货行 list[dict]:
       {1688_item, 1688_url, 买家, 电话, 地址, 省份, 件数, 规格, 订单号, 备注}
    """
    links = match_costs(orders, products, cost_map)
    rows = []
    for i, o in enumerate(orders):
        p = links.get(i)
        rows.append({
            "1688_item": p.item_id if p else "",
            "1688_url": p.item_url if p else "",
            "buyer": o.receiver,
            "phone": "",                       # 订单导出通常含脱敏电话, 可在映射里补全
            "address": o.address,
            "province": o.province,
            "qty": o.qty,
            "sku": o.sku,
            "order_sn": o.order_sn,
            "remote": "偏远加价" if any(rp in (o.province or "") for rp in REMOTE_PROVINCES) else "",
            "linked": "已关联" if p else "未关联(需手工匹配货源)",
        })
    return rows


def export_csv(rows: list, path: str):
    cols = ["order_sn", "1688_item", "1688_url", "buyer", "phone",
            "address", "province", "qty", "sku", "remote", "linked"]
    with open(path, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=cols)
        w.writeheader()
        w.writerows(rows)
    return path


def summary(rows: list) -> dict:
    total = len(rows)
    linked = sum(1 for r in rows if r["linked"].startswith("已关联"))
    remote = sum(1 for r in rows if r["remote"])
    units = sum(int(r["qty"] or 0) for r in rows)
    return {"orders": total, "linked": linked, "unlinked": total - linked,
            "remote": remote, "units": units}


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        os_ = parse_orders(sys.argv[1])
        for r in build_ship_plan(os_, [])[:3]:
            print(r)
