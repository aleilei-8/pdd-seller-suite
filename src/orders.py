#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
orders.py —— 拼多多订单 / 结算 导入 与 单笔利润计算(数据驱动)

数据来源: 拼多多商家后台 → 订单/结算 导出 CSV(用户手动导出, 无需登录爬虫)。
列名各版本略有差异, 这里做柔性映射(中文同义词归一)。

利润链路(与定价引擎同源):
  单笔净利 = 成交额 - 进货成本(1688进货+进货运费) - 拼多多发货运费(卖家承担)
            - 平台佣金 - 支付手续费 - 其他
"""
import csv
import re
from models import Order, Product
from pricing import PricingConfig, compute_profit, commission_rate_for, REMOTE_PROVINCES


# 列名归一映射: 目标字段 -> 候选中文表头(任一命中即取)
COLUMN_MAP = {
    "order_sn":        ["订单号", "订单编号", "order_sn", "order_id"],
    "goods_id":        ["商品ID", "商品编号", "货号", "goods_id", "商品id"],
    "goods_name":      ["商品名称", "商品标题", "宝贝标题", "goods_name"],
    "sku":             ["规格", "SKU", "sku", "型号"],
    "qty":             ["数量", "件数", "购买数量", "qty"],
    "price":           ["实付金额", "成交金额", "支付金额", "售价", "单价", "price"],
    "shipping_fee":    ["运费", "买家运费", "shipping_fee"],
    "commission":      ["佣金", "平台佣金", "技术服务费", "commission"],
    "payment_fee":     ["支付手续费", "支付费率", "payment_fee"],
    "discount":        ["优惠", "平台补贴", "活动优惠", "discount"],
    "receiver":        ["收货人", "收件人", "receiver"],
    "address":         ["收货地址", "地址", "address"],
    "province":        ["省份", "省", "province", "收货省份"],
    "status":          ["订单状态", "状态", "status"],
    "pay_time":        ["成交时间", "支付时间", "付款时间", "pay_time"],
}


def _norm_header(h: str) -> str:
    return re.sub(r"[\s（）()]", "", str(h or "")).lower()


def _build_lookup(headers: list) -> dict:
    norm = {_norm_header(h): h for h in headers}
    lookup = {}
    for target, cands in COLUMN_MAP.items():
        for c in cands:
            cn = _norm_header(c)
            if cn in norm:
                lookup[target] = norm[cn]
                break
    return lookup


def _g(row: dict, lookup: dict, field: str, default=""):
    col = lookup.get(field)
    if not col:
        return default
    return row.get(col, default)


def _num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").replace("¥", "").replace("元", "").strip() or default)
    except (TypeError, ValueError):
        return default


def parse_orders(path: str) -> list:
    """解析拼多多订单/结算 CSV → list[Order]"""
    orders = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        headers = reader.fieldnames or []
        lookup = _build_lookup(headers)
        for r in reader:
            o = Order()
            o.order_sn = str(_g(r, lookup, "order_sn", "")).strip()
            o.goods_id = str(_g(r, lookup, "goods_id", "")).strip()
            o.goods_name = str(_g(r, lookup, "goods_name", "")).strip()
            o.sku = str(_g(r, lookup, "sku", "")).strip()
            o.qty = int(_num(_g(r, lookup, "qty", 1), 1))
            # price: 实付金额通常为"单件成交价", 若有数量则按件; 这里存单件实付
            amt = _num(_g(r, lookup, "price", 0))
            o.price = round(amt / o.qty, 2) if o.qty > 0 else amt
            o.shipping_fee = _num(_g(r, lookup, "shipping_fee", 0))
            o.commission = _num(_g(r, lookup, "commission", 0))
            o.payment_fee = _num(_g(r, lookup, "payment_fee", 0))
            o.discount = _num(_g(r, lookup, "discount", 0))
            o.receiver = str(_g(r, lookup, "receiver", "")).strip()
            o.address = str(_g(r, lookup, "address", "")).strip()
            o.province = str(_g(r, lookup, "province", "")).strip()
            o.status = str(_g(r, lookup, "status", "")).strip()
            o.pay_time = str(_g(r, lookup, "pay_time", "")).strip()
            orders.append(o)
    return orders


def load_cost_map(path: str) -> dict:
    """载入 拼多多商品ID → 1688 item_id 的映射表(CSV: goods_id,item_id[,cost,express])。
    没有映射也能算, 只是成本取不到(标记为未关联)。"""
    m = {}
    try:
        with open(path, encoding="utf-8-sig", newline="") as f:
            for r in csv.DictReader(f):
                gid = str(r.get("goods_id", "")).strip()
                iid = str(r.get("item_id", "")).strip()
                if gid:
                    m[gid] = {"item_id": iid,
                              "cost": _num(r.get("cost")),
                              "express": _num(r.get("express"))}
    except Exception:
        pass
    return m


def match_costs(orders: list, products: list, cost_map: dict = None) -> dict:
    """把订单关联到 1688 成本。返回 {order_index: Product or None}"""
    by_item = {p.item_id: p for p in products}
    # 标题相似度兜底(归一后包含匹配)
    def title_key(t):
        return re.sub(r"[\s，,。、/（）()\-]", "", str(t or "")).lower()
    title_index = {}
    for p in products:
        tk = title_key(p.title)
        if tk:
            title_index.setdefault(tk[:8], p)

    links = {}
    for i, o in enumerate(orders):
        p = None
        cm = (cost_map or {}).get(o.goods_id)
        if cm and cm.get("item_id") in by_item:
            p = by_item[cm["item_id"]]
            p = _override_cost(p, cm)
        elif o.goods_id in by_item:
            p = by_item[o.goods_id]
        else:
            tk = title_key(o.goods_name)
            if tk:
                p = title_index.get(tk[:8])
        links[i] = p
    return links


def _override_cost(p: Product, cm: dict) -> Product:
    if cm.get("cost"):
        p = Product(**{**p.to_dict(), "price": cm["cost"]})
    if cm.get("express"):
        p = Product(**{**p.to_dict(), "express_fee": cm["express"]})
    return p


def compute_order_profit(o: Order, product: Product, config: PricingConfig,
                        shipping_out: float = 0.0):
    """计算单笔订单净利(基于关联交易到的 1688 成本)。
    发货运费: 包邮(买家运费=0)时由卖家承担 → 用 config 运费模板估算;
              否则卖家承担部分 = 模板估算(平台运费通常不足以覆盖, 这里保守取估算值)。
    """
    cost_price = product.price if product else 0.0
    inbound = product.express_fee if product else 0.0
    if shipping_out <= 0:
        shipping_out = config.shipping.estimate(product.weight_kg if product else 0.0, o.province)
    cr = commission_rate_for(product.category if product else "", config.commission_rate)
    # 订单自带佣金/支付费则优先用实际值
    commission = o.commission if o.commission > 0 else round(o.price * cr, 2)
    payment_fee = o.payment_fee if o.payment_fee > 0 else round(o.price * config.payment_fee_rate, 2)
    # 用 compute_profit 但佣金/支付费用实际值
    revenue = o.price
    net = (revenue - cost_price - inbound - shipping_out - commission - payment_fee
           - config.default_other_fee)
    margin = (net / revenue) if revenue > 0 else 0.0
    from pricing import ProfitBreakdown
    return ProfitBreakdown(
        revenue=round(revenue, 2), cost_price=round(cost_price, 2),
        inbound_shipping=round(inbound, 2), outbound_shipping=round(shipping_out, 2),
        commission=round(commission, 2), payment_fee=round(payment_fee, 2),
        other_fee=round(config.default_other_fee, 2),
        net_profit=round(net * o.qty, 2), net_margin=round(margin * 100, 2))


def enrich_orders(orders: list, products: list, config: PricingConfig,
                  cost_map: dict = None) -> list:
    """给订单批量算利润, 写回 Order 字段并返回"""
    links = match_costs(orders, products, cost_map)
    for i, o in enumerate(orders):
        p = links.get(i)
        o.matched_item = p.item_id if p else ""
        o.cost_price = p.price if p else 0.0
        o.inbound_shipping = p.express_fee if p else 0.0
        if p:
            pb = compute_order_profit(o, p, config)
            o.outbound_shipping = pb.outbound_shipping
            o.net_profit = pb.net_profit
        else:
            o.net_profit = 0.0
    return orders


if __name__ == "__main__":
    import sys
    if len(sys.argv) > 1:
        os_ = parse_orders(sys.argv[1])
        print(f"解析到 {len(os_)} 条订单:")
        for o in os_[:5]:
            print(f"  {o.order_sn} | {o.goods_name[:20]} | ¥{o.price} x{o.qty}")
