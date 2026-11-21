#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
finance.py —— 全成本利润对账(财务 Agent)

把"订单净利"(已扣进货成本/进货运费/发货运费/平台佣金/支付手续费)
与"广告花费"合并, 算出老板真正关心的 **广告后真实净利**。

为什么需要它:
  现有 analysis.store_kpi 的净利是"广告前"口径 —— 没扣推广费。
  广告费是最大的变动成本之一, 不扣掉会系统性高估利润, 甚至把亏损品当成爆款。

关联规则(广告花费如何分摊到订单):
  1. goods   : 广告行 goods_id 与订单 goods_id 精确匹配; 未匹配部分按成交额占比分摊
  2. date    : 同日广告花费归集到同日订单; 日期对不上的按成交额占比分摊
  3. campaign: 广告花费按推广计划归集; 订单按成交额占比分摊到各计划
  4. all     : 整体合并

诊断口径:
  广告后净利<0        → 亏损, 降价/缩量/暂停该计划
  广告费/成交额>20%   → 广告依赖过高, 优化投放结构
  未关联货源订单占比高 → 成本未回填, 净利被高估(先做货源关联)
"""
from collections import defaultdict
from dataclasses import dataclass, asdict
import re


@dataclass
class FinanceRow:
    key: str = ""                  # 维度键(商品ID/日期/计划名)
    name: str = ""                 # 展示名
    orders: int = 0
    units: int = 0
    revenue: float = 0.0           # 成交额
    cost: float = 0.0              # 进货成本+进货运费
    shipping: float = 0.0          # 发货运费
    commission: float = 0.0        # 平台佣金
    payment_fee: float = 0.0       # 支付手续费
    ad_spend: float = 0.0          # 分摊的广告费
    net_before: float = 0.0        # 广告前净利
    net_after: float = 0.0         # 广告后真实净利
    margin_after: float = 0.0      # 广告后真实净利率 %
    ad_ratio: float = 0.0          # 广告费率 %
    unlinked: int = 0              # 未关联货源的订单数
    advice: str = ""

    def to_dict(self):
        return asdict(self)


def _date_only(s) -> str:
    s = str(s or "").strip().replace("/", "-")
    m = re.match(r"(\d{4}-\d{2}-\d{2})", s)
    return m.group(1) if m else (s[:10] or "未知日期")


def _order_key(o, dim):
    if dim == "goods":
        return str(getattr(o, "goods_id", "") or getattr(o, "goods_name", "") or "(未知商品)")
    if dim == "date":
        return _date_only(getattr(o, "pay_time", ""))
    return "all"


def _ad_key(a, dim):
    if dim == "goods":
        return str(getattr(a, "goods_id", "") or "")
    if dim == "date":
        return _date_only(getattr(a, "date", ""))
    if dim == "campaign":
        return str(getattr(a, "campaign", "") or "(未命名计划)")
    return "all"


def _advice(b, target_margin=0.0):
    tips = []
    if b["net_after"] < 0:
        tips.append("广告后亏损: 降价/缩量/暂停该投放")
    b["ad_ratio"] = round(b["ad_spend"] / b["revenue"] * 100, 2) if b["revenue"] > 0 else 0.0
    if b["ad_ratio"] > 20:
        tips.append("广告费率>20%: 优化投放结构")
    if b["unlinked"]:
        n = b["unlinked"]
        shown = f"{n:.1f}" if isinstance(n, float) and abs(n - round(n)) > 0.01 else str(int(n))
        tips.append(f"{shown}单未关联货源, 净利被高估")
    if not tips:
        if target_margin and b["margin_after"] >= target_margin * 100:
            tips.append("健康: 真实净利达标, 可放量")
        else:
            tips.append("健康: 保持观察")
    return "；".join(tips)


def reconcile(orders: list, ad_rows: list = None, dim: str = "goods",
              target_margin: float = 0.0) -> dict:
    """订单 + 广告 → 全成本对账结果。

    返回 {"rows": [FinanceRow, ...], "overall": FinanceRow, "meta": {...}}
    """
    ad_rows = ad_rows or []
    buckets = {}

    def _bucket(key):
        if key not in buckets:
            buckets[key] = {
                "key": key, "name": key, "orders": 0, "units": 0,
                "revenue": 0.0, "cost": 0.0, "shipping": 0.0,
                "commission": 0.0, "payment_fee": 0.0,
                "net_before": 0.0, "unlinked": 0,
            }
        return buckets[key]

    # 计划维度: 订单没有直接归属字段, 按各计划的广告花费占比分摊
    campaign_spend = defaultdict(float)
    if dim == "campaign":
        for a in ad_rows:
            campaign_spend[_ad_key(a, dim)] += float(getattr(a, "spend", 0) or 0)

    # 1) 订单侧: 广告前净利
    for o in orders or []:
        qty = int(getattr(o, "qty", 1) or 1)
        if dim == "campaign":
            cands = list(campaign_spend.keys()) or ["(无推广计划)"]
            total_spend = sum(campaign_spend.values())
            weights = [(k, (campaign_spend[k] / total_spend if total_spend > 0
                            else 1.0 / len(cands))) for k in cands]
        else:
            weights = [(_order_key(o, dim), 1.0)]
        for key, weight in weights:
            b = _bucket(key)
            b["orders"] += 1 if weight >= 1 else weight
            b["units"] += qty * weight
            b["revenue"] += float(getattr(o, "price", 0) or 0) * qty * weight
            b["cost"] += (float(getattr(o, "cost_price", 0) or 0)
                          + float(getattr(o, "inbound_shipping", 0) or 0)) * qty * weight
            b["shipping"] += float(getattr(o, "outbound_shipping", 0) or 0) * qty * weight
            b["commission"] += float(getattr(o, "commission", 0) or 0) * weight
            b["payment_fee"] += float(getattr(o, "payment_fee", 0) or 0) * weight
            b["net_before"] += float(getattr(o, "net_profit", 0) or 0) * weight
            if not getattr(o, "matched_item", ""):
                b["unlinked"] += 1 if weight >= 1 else weight
            if dim == "goods" and b["name"] == key:
                nm = getattr(o, "goods_name", "")
                if nm:
                    b["name"] = str(nm)[:22]

    keys = list(buckets.keys())
    total_rev = sum(b["revenue"] for b in buckets.values())

    # 2) 广告侧: 精确归集 + 未匹配按成交额占比分摊
    direct = defaultdict(float)
    leftover = 0.0
    for a in ad_rows:
        spend = float(getattr(a, "spend", 0) or 0)
        if dim == "campaign":
            # 计划维度: 广告按计划归集; 若该计划尚未出现在订单里, 也建一个桶
            k = _ad_key(a, dim)
            _bucket(k)
            if k not in keys:
                keys.append(k)
            direct[k] += spend
        else:
            k = _ad_key(a, dim)
            if k in buckets:
                direct[k] += spend
            else:
                leftover += spend

    alloc = {}
    for k in keys:
        share = (buckets[k]["revenue"] / total_rev) if total_rev > 0 else (1 / len(keys) if keys else 0)
        alloc[k] = direct.get(k, 0.0) + leftover * share

    # 3) 合成结果行
    rows = []
    for k in keys:
        b = buckets[k]
        net_before = round(b["net_before"], 2)
        ad = round(alloc.get(k, 0.0), 2)
        row = FinanceRow(
            key=k, name=str(b["name"])[:24],
            orders=round(b["orders"]), units=b["units"],
            revenue=round(b["revenue"], 2), cost=round(b["cost"], 2),
            shipping=round(b["shipping"], 2), commission=round(b["commission"], 2),
            payment_fee=round(b["payment_fee"], 2),
            ad_spend=ad, net_before=net_before,
            net_after=round(net_before - ad, 2),
            unlinked=round(b["unlinked"], 1),
        )
        row.margin_after = round(row.net_after / row.revenue * 100, 2) if row.revenue > 0 else 0.0
        row.advice = _advice({
            "net_after": row.net_after, "revenue": row.revenue,
            "ad_spend": row.ad_spend, "unlinked": row.unlinked,
            "ad_ratio": 0.0,
        }, target_margin)
        rows.append(row)

    rows.sort(key=lambda r: r.net_after, reverse=True)

    # 4) 整体
    ov = FinanceRow(
        key="all", name="整体",
        orders=sum(r.orders for r in rows), units=sum(r.units for r in rows),
        revenue=round(sum(r.revenue for r in rows), 2),
        cost=round(sum(r.cost for r in rows), 2),
        shipping=round(sum(r.shipping for r in rows), 2),
        commission=round(sum(r.commission for r in rows), 2),
        payment_fee=round(sum(r.payment_fee for r in rows), 2),
        ad_spend=round(sum(r.ad_spend for r in rows), 2),
        net_before=round(sum(r.net_before for r in rows), 2),
        net_after=round(sum(r.net_after for r in rows), 2),
        unlinked=sum(r.unlinked for r in rows),
    )
    ov.margin_after = round(ov.net_after / ov.revenue * 100, 2) if ov.revenue > 0 else 0.0
    ov.ad_ratio = round(ov.ad_spend / ov.revenue * 100, 2) if ov.revenue > 0 else 0.0
    ov.advice = _advice({"net_after": ov.net_after, "revenue": ov.revenue,
                         "ad_spend": ov.ad_spend, "unlinked": ov.unlinked,
                         "ad_ratio": ov.ad_ratio}, target_margin)

    return {
        "rows": rows, "overall": ov,
        "meta": {
            "dim": dim, "order_count": len(orders or []),
            "ad_rows": len(ad_rows), "unmatched_ad_spend": round(leftover, 2),
            "has_ad": bool(ad_rows),
        },
    }


def render_report(result: dict) -> str:
    """给 AI 助手/日志用的中文对账报告。"""
    ov = result["overall"]
    meta = result["meta"]
    lines = [
        "📒 全成本对账(广告后真实净利)",
        f"整体: 成交额 ¥{ov.revenue:.2f} | 广告费 ¥{ov.ad_spend:.2f} ({ov.ad_ratio}%) | "
        f"广告前净利 ¥{ov.net_before:.2f} → 广告后 ¥{ov.net_after:.2f} | 真实净利率 {ov.margin_after}%",
        f"订单 {meta['order_count']} 笔, 广告记录 {meta['ad_rows']} 条"
        + (f", 其中 ¥{meta['unmatched_ad_spend']} 未能精确匹配, 已按成交额分摊" if meta["unmatched_ad_spend"] else ""),
        "", "按" + {"goods": "商品", "date": "日期", "campaign": "计划", "all": "整体"}.get(meta["dim"], meta["dim"]) + ":",
    ]
    for r in result["rows"][:10]:
        lines.append(f"  {r.name[:20]}: 成交¥{r.revenue:.2f} 广告¥{r.ad_spend:.2f} "
                     f"净利 ¥{r.net_before:.2f}→¥{r.net_after:.2f} ({r.margin_after}%)")
        lines.append(f"      → {r.advice}")
    if not result["rows"]:
        lines.append("  (暂无数据, 请先导入订单 CSV 与推广报表 CSV)")
    return "\n".join(lines)


if __name__ == "__main__":
    from models import Order
    from ad_roi import AdRow

    orders = [
        Order(goods_id="1001", goods_name="示例商品A", qty=2, price=30,
              cost_price=10, inbound_shipping=1, outbound_shipping=3,
              commission=0.6, payment_fee=0.18, net_profit=45.22,
              matched_item="1688-1", pay_time="2026-11-21 10:00"),
        Order(goods_id="1002", goods_name="示例商品B", qty=1, price=20,
              cost_price=8, inbound_shipping=1, outbound_shipping=3,
              commission=0.4, payment_fee=0.12, net_profit=7.48,
              matched_item="", pay_time="2026-11-21 11:00"),
    ]
    ads = [
        AdRow(date="2026-11-21", campaign="计划A", goods_id="1001",
              spend=20, clicks=100, impressions=5000, orders=2, gmv=60),
        AdRow(date="2026-11-21", campaign="计划B", goods_id="",
              spend=15, clicks=80, impressions=4000, orders=1, gmv=20),
    ]
    for dim in ("goods", "date", "campaign", "all"):
        res = reconcile(orders, ads, dim)
        print(render_report(res))
        print("-" * 60)
