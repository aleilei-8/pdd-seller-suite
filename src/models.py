#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
models.py —— 拼多多全链路工具 统一数据模型

贯穿 选品 / 定价 / 标题 / 上架 / 订单 / 利润分析 各环节的数据结构。
所有金额单位: 元(CNY)。运费单独记账, 不再并入"进货价"含糊处理。
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


@dataclass
class Product:
    """1688 货源商品(一件代发上游)"""
    item_id: str = ""
    source: str = "1688"                  # 1688 / taobao / manual
    title: str = ""                       # 原标题(待优化)
    price: float = 0.0                    # 进货价(不含运费, 1688 通常不包邮)
    sale_count_text: str = ""             # 原始销量文本, 如 "10万+"
    sale_count: int = 0                   # 销量估算值
    cat_id: str = ""                      # 平台类目ID
    item_url: str = ""
    images: list = field(default_factory=list)
    attributes: dict = field(default_factory=dict)   # 产地/品牌/样式/材质...
    sku_specs: list = field(default_factory=list)    # 规格值(颜色/尺寸...) 兼容旧逻辑
    # ---- 全量搬迁新增(与穿云箭缓存对齐) ----
    sku_dims: list = field(default_factory=list)     # [{"name":"颜色","values":[{"name","image","vid"}]}]
    sku_combos: list = field(default_factory=list)   # [{"spec","specs","price","stock","image","sku_id"}]
    detail_images: list = field(default_factory=list)  # 详情描述大图(非轮播主图)
    category_name: str = ""               # 1688 叶子类目名(用于 PDD 类目匹配)
    detail_html: str = ""                 # 原始详情描述 HTML(可选, 供富文本详情)
    express_fee: float = 0.0              # 1688 进货运费(到手成本的一部分)
    weight_kg: float = 0.0                # 商品重量(用于估算拼多多发货运费)
    category: str = ""                    # 业务类目(用于佣金率匹配)
    # 衍生产出(后续填充)
    optimized: dict = field(default_factory=dict)    # 标题优化结果
    score: dict = field(default_factory=dict)        # 选品评分
    pricing: dict = field(default_factory=dict)      # 利润定价结果
    pdd_terms: list = field(default_factory=list)    # 推流词(保留)
    pdd_dropped: list = field(default_factory=list)  # 推流词(剔除, 供回捞)

    def to_dict(self):
        return asdict(self)


@dataclass
class Order:
    """拼多多订单(从商家后台导出 CSV 解析)"""
    order_sn: str = ""                    # 订单号
    goods_id: str = ""                    # 商品ID(用于关联 1688 货源)
    goods_name: str = ""
    sku: str = ""
    qty: int = 1
    price: float = 0.0                    # 实付单价(含运费券等后的成交价)
    shipping_fee: float = 0.0             # 买家支付运费(包邮则为0)
    commission: float = 0.0               # 平台佣金
    payment_fee: float = 0.0              # 支付手续费
    discount: float = 0.0                 # 平台/活动补贴(负数表示商家承担)
    receiver: str = ""
    address: str = ""
    province: str = ""                    # 收货省份(用于运费/偏远判断)
    status: str = ""
    pay_time: str = ""
    # 关联后填
    cost_price: float = 0.0               # 关联到的 1688 进货价
    inbound_shipping: float = 0.0         # 1688 进货运费
    outbound_shipping: float = 0.0        # 拼多多发货运费(卖家承担部分)
    net_profit: float = 0.0               # 该订单净利
    matched_item: str = ""                # 关联到的 1688 item_id

    def to_dict(self):
        return asdict(self)


@dataclass
class ListingCandidate:
    """上架候选: 一个 1688 货源 → 一条可直接上架的拼多多商品"""
    item_id: str = ""
    title: str = ""                       # 优化后标题
    cost_price: float = 0.0
    inbound_shipping: float = 0.0
    outbound_shipping: float = 0.0
    commission_rate: float = 0.0
    suggest_price: float = 0.0            # 建议拼多多售价(含运费+佣金+目标利润)
    net_profit: float = 0.0
    net_margin: float = 0.0
    pdd_terms: list = field(default_factory=list)
    images: list = field(default_factory=list)       # 轮播主图(全量, 不只第一张)
    item_url: str = ""
    grade: str = ""
    # ---- 全量搬迁新增: 发布到拼多多所需 ----
    detail_images: list = field(default_factory=list)  # 详情图
    sku_dims: list = field(default_factory=list)       # 规格维度
    sku_combos: list = field(default_factory=list)     # 每款价格/库存/规格图
    attributes: dict = field(default_factory=dict)     # 商品属性(品牌/材质...)
    category_name: str = ""                            # 1688 类目名(供 PDD 类目匹配)
    detail_html: str = ""                              # 原始详情 HTML

    def to_dict(self):
        return asdict(self)


@dataclass
class ProfitRow:
    """利润分析单行(用于表格与汇总)"""
    key: str = ""                         # 维度值(类目/日期/商品)
    orders: int = 0
    units: int = 0
    revenue: float = 0.0                  # 成交额(含买家运费)
    cost: float = 0.0                     # 进货成本(含进货运费)
    shipping_out: float = 0.0             # 拼多多发货运费支出
    commission: float = 0.0
    payment_fee: float = 0.0
    net_profit: float = 0.0
    net_margin: float = 0.0

    def to_dict(self):
        return asdict(self)
