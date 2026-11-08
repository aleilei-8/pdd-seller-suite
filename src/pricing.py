#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pricing.py —— 运费感知的利润定价引擎(全套工具的核心)

为什么重做:
  1688 货源"基本不包运费", 且拼多多上架若标"包邮"则发货运费由卖家承担;
  再加上平台佣金 + 支付手续费, 旧版"进货价 × 加价倍率"会严重高估利润、定错价。

本引擎把每一笔成本拆清楚:
  售价(revenue)
    - 进货价 cost_price                 (1688 不含运费)
    - 进货运费 inbound_shipping        (1688 发到你这里的运费)
    - 发货运费 outbound_shipping       (你发给买家的运费, 包邮时由你出)
    - 平台佣金 commission  = revenue × commission_rate
    - 支付手续费 payment_fee = revenue × payment_fee_rate
    - 其他费用 other_fee               (包装/耗材等)
  = 净利 net_profit
  净利率 net_margin = net_profit / revenue

两路计算:
  1) compute_profit : 给定售价 → 算净利(已上架/有成交时复盘)
  2) price_for_target: 给定目标净利率 或 目标单件净利 → 反算建议售价(上新定价)

运费估算: 区域化"首重+续重"模板(内地与偏远各一套价) + 固定运费模式, 默认按重量。
          偏远(新疆/西藏/内蒙/青海/宁夏/海南/甘肃)自动套用更高的首重/续重价,
          与真实快递报价一致; 所有参数均可在 GUI「⚙运费/佣金」里按你的实际承运商校准。
"""
from dataclasses import dataclass, field, asdict
from typing import Optional


# 平台佣金率(技术服务费)按类目关键字匹配, 缺失用 DEFAULT_COMMISSION。
# 拼多多佣金普遍 0.6%~5%, 这里给常见类目经验值, 可在界面逐商品覆盖。
# 说明: 这些是"平台向商家收的技术服务费"经验区间, 实际以商家后台类目费率为准。
CATEGORY_COMMISSION = {
    # 美妆个护(偏高)
    "美妆": 0.03, "化妆品": 0.03, "护肤": 0.03, "彩妆": 0.03,
    "个护": 0.03, "洗护": 0.03, "香水": 0.05, "面膜": 0.03,
    # 饰品(偏高)
    "饰品": 0.05, "发饰": 0.05, "首饰": 0.05, "珠宝": 0.05, "手表": 0.05, "钟表": 0.05,
    # 服饰鞋包
    "服饰": 0.02, "女装": 0.02, "男装": 0.02, "童装": 0.02, "内衣": 0.02,
    "鞋": 0.02, "箱包": 0.02,
    # 家居日用
    "家居": 0.02, "收纳": 0.02, "日用": 0.02, "百货": 0.02,
    "厨具": 0.02, "餐具": 0.02, "家纺": 0.02, "床上用品": 0.02,
    # 数码电子
    "数码": 0.02, "电子": 0.02, "手机": 0.02, "家电": 0.02, "小家电": 0.02,
    # 食品生鲜
    "食品": 0.02, "零食": 0.02, "生鲜": 0.02, "粮油": 0.02, "茶": 0.02,
    # 母婴玩具
    "母婴": 0.02, "玩具": 0.02,
    # 文具办公
    "文具": 0.02, "办公": 0.02, "图书": 0.02, "文创": 0.02,
    # 运动户外
    "运动": 0.02, "户外": 0.02, "健身": 0.02,
    # 其他
    "宠物": 0.02, "汽车": 0.02, "车品": 0.02, "五金": 0.02, "工具": 0.02,
    "鲜花": 0.02, "绿植": 0.02,
}
DEFAULT_COMMISSION = 0.02          # 2% 兜底
PAYMENT_FEE_RATE = 0.006           # 支付手续费 ~0.6%(微信/支付宝)

# 偏远地区(运费上浮)
REMOTE_PROVINCES = {"新疆", "西藏", "内蒙古", "青海", "宁夏", "海南", "甘肃"}


@dataclass
class ShippingTemplate:
    """拼多多发货运费估算模板(卖家承担部分)。

    真实物流(中通/圆通/韵达等)是"首重+续重"且分区域:
      内地: 首重便宜、续重低;  偏远(新疆/西藏/内蒙等): 首重与续重都明显更贵。
    本模板用两套参数分别建模, estimate() 按收货省份自动选内地/偏远价。
    """
    mode: str = "weight"           # "fixed" 固定 / "weight" 按重量(默认更真实)
    # —— 固定模式 ——
    fixed_fee: float = 3.0         # 固定运费(中小件包邮平均真实成本)
    remote_surcharge: float = 5.0  # 固定模式下偏远地区附加
    # —— 重量模式(内地) ——
    first_weight: float = 1.0      # 首重(kg)
    first_fee: float = 3.5         # 内地首重费用
    add_weight: float = 1.0        # 续重单位(kg)
    add_fee: float = 1.5           # 内地续重每单位费用
    # —— 重量模式(偏远) ——
    remote_first_fee: float = 10.0 # 偏远首重费用
    remote_add_fee: float = 6.0    # 偏远续重每单位费用

    def estimate(self, weight_kg: float = 0.0, province: str = "") -> float:
        remote = bool(province) and any(p in province for p in REMOTE_PROVINCES)
        if self.mode == "weight":
            # 未给重量时按首重处理(中小件大多 <1kg)
            w = weight_kg if weight_kg > 0 else self.first_weight
            if remote:
                fw, aw = self.remote_first_fee, self.remote_add_fee
            else:
                fw, aw = self.first_fee, self.add_fee
            if w <= self.first_weight:
                fee = fw
            else:
                over = w - self.first_weight
                adds = max(0, int((over + self.add_weight - 0.0001) // self.add_weight))
                fee = fw + adds * aw
        else:
            fee = self.fixed_fee
            if remote:
                fee += self.remote_surcharge
        return round(fee, 2)

    def to_dict(self):
        return asdict(self)

    @classmethod
    def from_dict(cls, d: dict):
        if not d:
            return cls()
        return cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__})


@dataclass
class PricingConfig:
    """全局定价配置(可在界面里改, 逐商品可覆盖)"""
    commission_rate: float = DEFAULT_COMMISSION
    payment_fee_rate: float = PAYMENT_FEE_RATE
    shipping: ShippingTemplate = field(default_factory=ShippingTemplate)
    target_net_margin: float = 0.25     # 建议售价的目标净利率
    default_other_fee: float = 0.0      # 默认每件其他费用(包装等)
    markup_fallback: float = 2.8        # 无目标利润率时的兜底加价倍率(仅展示)

    def to_dict(self):
        return {"commission_rate": self.commission_rate,
                "payment_fee_rate": self.payment_fee_rate,
                "shipping": self.shipping.to_dict(),
                "target_net_margin": self.target_net_margin,
                "default_other_fee": self.default_other_fee,
                "markup_fallback": self.markup_fallback}

    @classmethod
    def from_dict(cls, d: dict):
        if not d:
            return cls()
        shipping = ShippingTemplate.from_dict(d.get("shipping", {}))
        kw = {k: v for k, v in d.items()
              if k in cls.__dataclass_fields__ and k != "shipping"}
        return cls(shipping=shipping, **kw)


def commission_rate_for(category: str, override: Optional[float] = None) -> float:
    """按类目匹配佣金率, 允许显式覆盖"""
    if override is not None:
        return float(override)
    if category:
        for k, v in CATEGORY_COMMISSION.items():
            if k in category:
                return v
    return DEFAULT_COMMISSION


@dataclass
class ProfitBreakdown:
    revenue: float
    cost_price: float
    inbound_shipping: float
    outbound_shipping: float
    commission: float
    payment_fee: float
    other_fee: float
    net_profit: float
    net_margin: float

    def to_dict(self):
        return asdict(self)


def compute_profit(cost_price: float, inbound_shipping: float, pdd_price: float,
                   outbound_shipping: float = 0.0,
                   commission_rate: float = DEFAULT_COMMISSION,
                   payment_fee_rate: float = PAYMENT_FEE_RATE,
                   other_fee: float = 0.0) -> ProfitBreakdown:
    """给定售价, 拆解每一笔成本并算净利。

    这是 1688→拼多多 代发的真实利润公式:
      净利 = 售价 - 进货 - 进货运费 - 发货运费 - 佣金 - 支付费 - 其他
    其中 佣金/支付费 按售价比例计提(拼多多从成交额里扣)。
    """
    revenue = float(pdd_price)
    cost_price = float(cost_price)
    inbound = float(inbound_shipping)
    outbound = float(outbound_shipping)
    other = float(other_fee)
    commission = round(revenue * float(commission_rate), 4)
    payment_fee = round(revenue * float(payment_fee_rate), 4)
    net = revenue - cost_price - inbound - outbound - commission - payment_fee - other
    margin = (net / revenue) if revenue > 0 else 0.0
    return ProfitBreakdown(
        revenue=round(revenue, 2),
        cost_price=round(cost_price, 2),
        inbound_shipping=round(inbound, 2),
        outbound_shipping=round(outbound, 2),
        commission=round(commission, 2),
        payment_fee=round(payment_fee, 2),
        other_fee=round(other, 2),
        net_profit=round(net, 2),
        net_margin=round(margin * 100, 2),
    )


def price_for_target(cost_price: float, inbound_shipping: float,
                     outbound_shipping: float = 0.0,
                     commission_rate: float = DEFAULT_COMMISSION,
                     payment_fee_rate: float = PAYMENT_FEE_RATE,
                     other_fee: float = 0.0,
                     target_margin: Optional[float] = None,
                     target_profit: Optional[float] = None) -> float:
    """反算建议售价(含运费+佣金+目标利润)。

    两种情况(互斥, target_profit 优先):
      A) 目标单件净利 target_profit=P:
         售价 ×(1-佣金率-支付费率) = 成本合计 + P
         → 售价 = (成本合计 + P) / (1 - 佣金率 - 支付费率)
      B) 目标净利率 target_margin=M:
         售价 ×(1-佣金率-支付费率-M) = 成本合计
         → 售价 = 成本合计 / (1 - 佣金率 - 支付费率 - M)

    成本合计 = 进货 + 进货运费 + 发货运费 + 其他
    """
    c = float(cost_price) + float(inbound_shipping) + float(outbound_shipping) + float(other_fee)
    fee_rate = float(commission_rate) + float(payment_fee_rate)
    if fee_rate >= 1:
        raise ValueError("佣金率+支付费率之和必须小于 1")
    if target_profit is not None:
        price = (c + float(target_profit)) / (1 - fee_rate)
    elif target_margin is not None:
        denom = 1 - fee_rate - float(target_margin)
        if denom <= 0:
            raise ValueError("目标净利率过高, 分母非正, 无法定价")
        price = c / denom
    else:
        # 无任何目标: 兜底加价(不含运费的旧逻辑, 仅作占位)
        price = c * 2.8
    return round(price, 2)


def suggest_price(product_cost: float, inbound_shipping: float,
                  shipping_out: float, config: PricingConfig,
                  category: str = "", override_commission: Optional[float] = None,
                  weight_kg: float = 0.0, province: str = "",
                  other_fee: Optional[float] = None) -> ProfitBreakdown:
    """一步得到"建议售价 + 完整利润拆解", 供选品/上架直接调用。

    发货运费: 若 shipping_out 已给定(逐商品)则用之; 否则按模板估算。
    佣金率: 优先 override, 否则按类目匹配。
    """
    out_ship = shipping_out if shipping_out > 0 else config.shipping.estimate(weight_kg, province)
    # 仅在逐商品显式覆盖时才用 override; 否则按类目表匹配(缺失回退默认佣金)
    cr = override_commission if override_commission is not None \
        else commission_rate_for(category)
    pf = config.payment_fee_rate
    other = other_fee if other_fee is not None else config.default_other_fee
    price = price_for_target(product_cost, inbound_shipping, out_ship,
                             commission_rate=cr, payment_fee_rate=pf,
                             other_fee=other, target_margin=config.target_net_margin)
    return compute_profit(product_cost, inbound_shipping, price, out_ship, cr, pf, other)


if __name__ == "__main__":
    # 自测: 1688 进货 ¥8, 进货运费 ¥2, 发货运费 ¥3(包邮卖家出), 佣金2%, 支付0.6%
    cfg = PricingConfig()
    pb = suggest_price(8.0, 2.0, 3.0, cfg, category="发饰")
    print("建议售价:", pb.revenue, "净利:", pb.net_profit, "净利率%:", pb.net_margin)
    print(pb.to_dict())
    # 反算: 想每件净赚 ¥5
    p = price_for_target(8.0, 2.0, 3.0, target_profit=5.0)
    print("目标净利¥5 → 售价:", p)
