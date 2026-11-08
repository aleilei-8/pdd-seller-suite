#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
cli.py —— 拼多多全链路工具 命令行入口(数据驱动全流程)

示例:
  python src/cli.py                          # 从1688缓存跑 选品+定价+上架清单
  python src/cli.py --json goods.json        # 指定JSON
  python src/cli.py --enrich                 # 抓取推流词增强标题(需浏览器)
  python src/cli.py --orders orders.csv --costmap map.csv --analysis out
                                             # 订单利润分析 + 发货/开票导出
  python src/cli.py --out report             # 导出 report_*.csv/json
"""
import sys
import os
import json
import argparse

import paths as _paths
_paths.setup_env()
from paths import ROOT_S, SRC_S

HERE = SRC_S
ROOT = ROOT_S
sys.path.insert(0, HERE)

import models
import pricing
import loader
import selector
import listing
import orders as orders_mod
import analysis
import shipping as shipping_mod
import invoice as invoice_mod
import title_opt


def load_products(args):
    if args.json:
        return loader.load_from_json_file(args.json)
    if args.csv:
        return loader.load_from_csv(args.csv)
    return loader.load_from_cyj_cache()


def run_selection(prods, config, args):
    print(f"\n=== 选品评分 ({len(prods)} 个) | 目标净利率 {config.target_net_margin*100:.0f}% ===")
    rows = []
    for p in prods:
        s = selector.score_product(p, config)
        p.score = s
        print(f"  [{s['grade']:>4}] {s['total']:>5} | {p.item_id} | 成本¥{s['cost']}→建议¥{s['suggest_price']} 净利¥{s['net_profit']}({s['net_margin']}%)")
        rows.append({"item_id": p.item_id, "title": p.title, "grade": s["grade"],
                     "total": s["total"], "cost": s["cost"], "suggest_price": s["suggest_price"],
                     "net_profit": s["net_profit"], "net_margin": s["net_margin"],
                     "category": s["category"], "notes": " ; ".join(s["notes"])})
    return rows


def run_enrich(prods, args):
    from suggest import SuggestClient, build_enriched_terms
    print("\n=== 抓取拼多多下拉联想词(推流信号) ===")
    cache_path = os.path.join(ROOT, "suggest_cache.json")
    raw = {}
    if os.path.exists(cache_path):
        try:
            with open(cache_path, encoding="utf-8") as f:
                raw = json.load(f)
        except Exception:
            raw = {}
    profiles = {}
    for p in prods:
        ex = title_opt.optimize(p)["extracted"]
        profiles[p.item_id] = ex["core"] + ex["attr"]
    need = []
    for pid, words in profiles.items():
        for w in words:
            if w and w not in raw and w not in need:
                need.append(w)
    if need:
        client = SuggestClient(headless=True)
        try:
            for w in need:
                raw[w] = client.suggest(w)
        finally:
            client.close()
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(raw, f, ensure_ascii=False, indent=2)
    terms_map, drop_map = {}, {}
    for p in prods:
        merged = []
        for w in profiles[p.item_id]:
            merged.extend(raw.get(w, []) or [])
        kept, dropped = build_enriched_terms(merged, profiles[p.item_id], profiles[p.item_id],
                                            support_text=p.title, min_score=args.min_rel,
                                            return_dropped=True)
        terms_map[p.item_id] = kept
        drop_map[p.item_id] = dropped
    print(f"  保留 {sum(len(v) for v in terms_map.values())} 条, 剔除 {sum(len(v) for v in drop_map.values())} 条")
    return terms_map, drop_map


def run_orders(args, config, prods):
    if not args.orders:
        return
    print(f"\n=== 订单利润分析: {args.orders} ===")
    orders = orders_mod.parse_orders(args.orders)
    cost_map = orders_mod.load_cost_map(args.costmap) if args.costmap else None
    orders = orders_mod.enrich_orders(orders, prods, config, cost_map)
    k = analysis.store_kpi(orders)
    print(f"  订单{k['orders']} 件{k['units']} 成交额¥{k['revenue']} 净利¥{k['net_profit']}({k['net_margin']}%) 关联率{k['linked_rate']}%")
    if args.analysis:
        os.makedirs(args.analysis, exist_ok=True)
        import csv
        for dim in ("category", "date", "product"):
            rows = analysis.aggregate(orders, dim)
            with open(os.path.join(args.analysis, f"profit_by_{dim}.csv"), "w", encoding="utf-8-sig", newline="") as f:
                w = csv.DictWriter(f, fieldnames=["key", "orders", "units", "revenue", "cost",
                                                  "shipping_out", "commission", "payment_fee",
                                                  "net_profit", "net_margin"])
                w.writeheader()
                w.writerows([r.to_dict() for r in rows])
        # 发货 + 开票
        ship_rows = shipping_mod.build_ship_plan(orders, prods, cost_map)
        shipping_mod.export_csv(ship_rows, os.path.join(args.analysis, "ship_plan.csv"))
        inv_rows = invoice_mod.build_invoice_records(orders, prods, cost_map)
        invoice_mod.export_csv(inv_rows, os.path.join(args.analysis, "invoice_detail.csv"))
        invoice_mod.export_csv(inv_rows, os.path.join(args.analysis, "invoice_purchase.csv"), "purchase")
        print(f"  已导出分析包 → {args.analysis}/ (profit_by_*.csv, ship_plan.csv, invoice_*.csv)")


def main():
    ap = argparse.ArgumentParser(description="拼多多全链路工具(数据驱动)")
    ap.add_argument("--json", help="从 JSON 加载 1688 货源")
    ap.add_argument("--csv", help="从 CSV 加载 1688 货源")
    ap.add_argument("--enrich", action="store_true", help="抓取拼多多推流词增强标题")
    ap.add_argument("--min-rel", type=float, default=0.55, help="推流词相关性门槛")
    ap.add_argument("--orders", help="拼多多订单/结算 CSV")
    ap.add_argument("--costmap", help="商品ID→1688映射 CSV")
    ap.add_argument("--analysis", help="利润分析导出目录")
    ap.add_argument("--out", default="report", help="选品/上架导出前缀")
    ap.add_argument("--target-margin", type=float, default=25, help="目标净利率%")
    ap.add_argument("--commission", type=float, default=2, help="平台佣金率%")
    ap.add_argument("--ship-fee", type=float, default=3.0, help="默认发货运费¥")
    args = ap.parse_args()

    config = pricing.PricingConfig()
    config.target_net_margin = args.target_margin / 100
    config.commission_rate = args.commission / 100
    config.shipping.fixed_fee = args.ship_fee

    prods = load_products(args)
    if not prods:
        print("未加载到任何货源商品。请用 --json/--csv 或确认 1688 商品缓存路径。")
        return

    terms_map, drop_map = {}, {}
    if args.enrich:
        terms_map, drop_map = run_enrich(prods, args)

    rows = run_selection(prods, config, args)

    # 导出选品报告
    import csv
    with open(args.out + "_selection.csv", "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=list(rows[0].keys()))
        w.writeheader(); w.writerows(rows)
    with open(args.out + "_selection.json", "w", encoding="utf-8") as f:
        json.dump([p.to_dict() for p in prods], f, ensure_ascii=False, indent=2)

    # 上架清单
    listings = listing.build_listings(prods, config, terms_map)
    listing.export_csv(listings, args.out + "_listing.csv")
    print(f"\n✅ 选品报告: {args.out}_selection.csv / .json")
    print(f"✅ 上架清单: {args.out}_listing.csv ({len(listings)} 条)")

    run_orders(args, config, prods)


if __name__ == "__main__":
    main()
