#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""广告投产 ROI 分析 (v1)。

数据来源: 拼多多商家后台 → 推广/搜索广告报表, 手动导出 CSV。
列名各版本差异大, 做柔性映射(中文同义词归一), 缺列按 0 处理。

核心指标:
  ROI(投产比) = 成交金额 / 花费        —— <1 亏损, ≥2 良好
  CTR(点击率) = 点击 / 曝光
  CVR(转化率) = 成交订单 / 点击
  CPC(点击成本) = 花费 / 点击
  成交成本 = 花费 / 成交订单

诊断建议规则(懂生意的部分):
  ROI<1 且花费高     → 立即降价/缩量或暂停该计划
  1≤ROI<1.5         → 接近保本, 优化转化或降出价
  CTR<1%            → 主图/标题吸引力不足
  CVR<2%            → 详情页/价格/评价问题
  CPC>净利          → 每次点击成本已超过单件净利, 必亏
"""
import csv
import re
from dataclasses import dataclass, field, asdict


@dataclass
class AdRow:
    date: str = ""
    campaign: str = ""       # 推广计划名
    goods_id: str = ""       # 商品ID
    goods_name: str = ""
    impressions: float = 0.0 # 曝光量
    clicks: float = 0.0      # 点击量
    spend: float = 0.0       # 花费(元)
    orders: float = 0.0      # 成交订单数
    gmv: float = 0.0         # 成交金额(元)

    def to_dict(self):
        return asdict(self)


# 目标字段 -> 候选表头(中文同义词, 归一化后比较)
COLUMN_MAP = {
    "date":        ["日期", "统计日期", "时间", "date", "day"],
    "campaign":    ["计划名称", "推广计划", "计划", "单元名称", "推广单元", "campaign", "plan"],
    "goods_id":    ["商品ID", "商品编号", "货号", "goods_id", "商品id", "宝贝ID"],
    "goods_name":  ["商品名称", "商品标题", "宝贝标题", "goods_name"],
    "impressions": ["曝光量", "展现量", "展示量", "曝光数", "impressions", "展现"],
    "clicks":      ["点击量", "点击数", "点击次数", "clicks", "点击"],
    "spend":       ["花费", "消耗", "花费(元)", "总花费", "广告花费", "spend", "cost"],
    "orders":      ["成交订单数", "成交笔数", "成交订单量", "支付订单数", "转化订单数", "orders", "成交单量"],
    "gmv":         ["成交金额", "支付金额", "成交额", "交易额", "GMV", "gmv", "成交金额(元)"],
}


def _norm_header(h):
    return re.sub(r"[\s（）()%％:：/\\-]", "", str(h or "")).lower()


def _build_lookup(headers):
    norm = {_norm_header(h): h for h in headers}
    lookup = {}
    for target, cands in COLUMN_MAP.items():
        for c in cands:
            cn = _norm_header(c)
            if cn in norm:
                lookup[target] = norm[cn]
                break
    return lookup


def _num(v, default=0.0):
    try:
        return float(str(v).replace(",", "").replace("¥", "").replace("元", "").strip() or default)
    except (TypeError, ValueError):
        return default


def parse_ad_csv(path):
    """解析广告推广报表 CSV -> list[AdRow]"""
    rows = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        lookup = _build_lookup(reader.fieldnames or [])
        for r in reader:
            def g(key, default=""):
                col = lookup.get(key)
                return r.get(col, default) if col else default
            rows.append(AdRow(
                date=str(g("date")).strip(),
                campaign=str(g("campaign")).strip(),
                goods_id=str(g("goods_id")).strip(),
                goods_name=str(g("goods_name")).strip(),
                impressions=_num(g("impressions")),
                clicks=_num(g("clicks")),
                spend=_num(g("spend")),
                orders=_num(g("orders")),
                gmv=_num(g("gmv")),
            ))
    return rows


@dataclass
class AdSummary:
    key: str = ""
    name: str = ""
    impressions: float = 0.0
    clicks: float = 0.0
    spend: float = 0.0
    orders: float = 0.0
    gmv: float = 0.0
    roi: float = 0.0
    ctr: float = 0.0       # 百分比
    cvr: float = 0.0       # 百分比
    cpc: float = 0.0
    cost_per_order: float = 0.0
    advice: str = ""


def _finish(s: AdSummary, max_cpc=None):
    s.roi = round(s.gmv / s.spend, 3) if s.spend > 0 else 0.0
    s.ctr = round(s.clicks / s.impressions * 100, 2) if s.impressions > 0 else 0.0
    s.cvr = round(s.orders / s.clicks * 100, 2) if s.clicks > 0 else 0.0
    s.cpc = round(s.spend / s.clicks, 2) if s.clicks > 0 else 0.0
    s.cost_per_order = round(s.spend / s.orders, 2) if s.orders > 0 else 0.0
    s.advice = diagnose(s, max_cpc)
    return s


def diagnose(s: AdSummary, max_cpc=None):
    """把 ROI/CTR/CVR/CPC 翻译成可执行的运营建议。"""
    tips = []
    if s.spend <= 0:
        return "无花费数据"
    if s.roi < 1:
        tips.append("投产比<1 亏损, 优先降价/缩量或暂停")
    elif s.roi < 1.5:
        tips.append("投产比接近保本, 降出价或优化转化")
    elif s.roi >= 3:
        tips.append("投产优秀, 可适度加预算放量")
    else:
        tips.append("投产正常, 保持观察")
    if s.impressions > 100 and s.ctr < 1.0:
        tips.append("点击率<1%, 换主图/优化标题")
    if s.clicks > 50 and s.cvr < 2.0:
        tips.append("转化率<2%, 检查详情页/价格/评价")
    if max_cpc and s.cpc > max_cpc:
        tips.append(f"点击成本{s.cpc}元已超过单件净利{max_cpc}元, 点击越多亏越多")
    return "；".join(tips)


def aggregate(rows, dim="campaign", max_cpc=None):
    """按 dim 聚合: campaign=计划 / goods=商品 / date=日期 / all=整体。"""
    groups = {}
    for r in rows:
        if dim == "campaign":
            key, name = r.campaign or "(未命名计划)", r.campaign
        elif dim == "goods":
            key, name = r.goods_id or r.goods_name or "(未知商品)", r.goods_name
        elif dim == "date":
            key, name = r.date or "(未知日期)", r.date
        else:
            key, name = "all", "整体"
        s = groups.setdefault(key, AdSummary(key=key, name=name))
        s.impressions += r.impressions
        s.clicks += r.clicks
        s.spend += r.spend
        s.orders += r.orders
        s.gmv += r.gmv
        if dim == "date" and not s.name:
            s.name = r.date
    out = [_finish(s, max_cpc) for s in groups.values()]
    if dim != "date":
        out.sort(key=lambda s: s.spend, reverse=True)
    else:
        out.sort(key=lambda s: s.key)
    return out


def overall(rows, max_cpc=None):
    """整体汇总一行。"""
    s = AdSummary(key="all", name="整体")
    for r in rows:
        s.impressions += r.impressions
        s.clicks += r.clicks
        s.spend += r.spend
        s.orders += r.orders
        s.gmv += r.gmv
    return _finish(s, max_cpc)


def render_report(rows, max_cpc=None):
    """生成给 AI 助手/日志用的中文文本报告。"""
    if not rows:
        return "广告报表为空。请先在「广告投产」页导入推广 CSV。"
    o = overall(rows, max_cpc)
    lines = [f"📣 广告投产分析 (共 {len(rows)} 条记录)",
             f"整体: 花费 ¥{o.spend:.2f} → 成交 ¥{o.gmv:.2f} | "
             f"ROI {o.roi} | 点击率 {o.ctr}% | 转化率 {o.cvr}% | "
             f"点击成本 ¥{o.cpc}"]
    lines.append("")
    lines.append("按计划(花费Top):")
    for s in aggregate(rows, "campaign", max_cpc)[:8]:
        lines.append(f"  {s.name[:24]}: 花费¥{s.spend:.2f} 成交¥{s.gmv:.2f} "
                     f"ROI{s.roi} | {s.advice}")
    bad = [s for s in aggregate(rows, "campaign", max_cpc) if s.roi and s.roi < 1]
    if bad:
        lines.append("")
        lines.append(f"⚠ {len(bad)} 个计划投产比<1 处于亏损, 建议优先处理。")
    return "\n".join(lines)


if __name__ == "__main__":
    # 自测: 构造样例
    rows = [
        AdRow(date="2026-11-20", campaign="计划A", goods_id="123", impressions=10000,
              clicks=150, spend=120, orders=5, gmv=400),
        AdRow(date="2026-11-20", campaign="计划B", goods_id="456", impressions=8000,
              clicks=60, spend=100, orders=1, gmv=60),
    ]
    print(render_report(rows))
