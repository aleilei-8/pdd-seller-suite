#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""temu.py —— Temu 全托管上架前的本地测算器(只读, 纯规则)

定位:
  不登录、不建品、不报价、不备货 —— 那些必须在 seller.temu.com 后台人工做。
  本模块只做上架前的三件事:
    1) 从已采集的 1688 商品里筛出适合 Temu 全托管的轻小件(非液体/非带电/低风险)
    2) 按 Temu 供货价公式反推: 报价 / 核价底线 / 目标净利
    3) 导出「建品清单」与「核价记录表」, 照着往后台填、记录买手回价

供货价公式(与国内拼多多定价引擎不同, 不可复用 pricing.py):
  成本合计 = 1688采购价 + 1688发货运费 + 国内到仓服务费 + 包装耗材 + 采购额×损耗率
  核价底线 = 成本合计 / (1 - 核价底线净利率)     低于此价弃款
  目标报价 = 成本合计 / (1 - 目标净利率)
  建议报价 = 目标报价 × (1 + 议价空间)            留砍价余地, 砍完≈目标报价

为什么独立:
  - 国内拼多多你定售价、承担佣金/支付费/发货运费; Temu 全托管你只报供货价,
    平台承担跨境物流/售后/流量, 没有佣金与支付费, 但会砍价。两套公式不通用。
  - 液体/膏霜/带电/品牌 IP 在 Temu 是高风险类目, 必须前置拦截, 与国内选品规则不同。
"""
from dataclasses import dataclass, asdict
from pathlib import Path
import csv
import json
import re

try:
    from paths import ROOT, DATA
except Exception:
    _HERE = Path(__file__).resolve().parent
    ROOT = _HERE.parent
    DATA = ROOT / "data"

CFG_PATH = ROOT / "temu_config.json"

# ---------- 默认配置 ----------
DEFAULT_CONFIG = {
    "inbound_per_item": 2.0,      # 供应商→Temu国内仓的服务费/件(代发/转发)
    # 1688 页面运费多为「单件快递 template 价」(常 15~18 元), 全托管批量发货
    # 实际按整箱摊销, 单件远低于此值。0=按原运费; >0=按该件数摊销。
    "express_amortize_qty": 50,   # 1688 运费摊销件数(批量发货到国内仓)
    "packaging_fee": 0.5,         # 包装耗材/件
    "wastage_rate": 0.04,         # 损耗备用金(按采购额比例)
    "min_net_margin": 0.08,       # 核价底线净利率: 低于此值弃款
    "target_net_margin": 0.25,    # 目标净利率
    "quote_buffer": 0.12,         # 报价议价空间(买手必砍)
    "max_weight_kg": 0.5,         # 轻小件重量上限
    "default_weight_kg": 0.2,     # 无重量数据时的保守估计
    "min_cost": 1.0,              # 采购价下限(太低利润绝对值不够)
    "max_cost": 30.0,             # 采购价上限(太高全托管没价格优势)
    "min_gross_margin": 0.30,     # 毛利空间门槛
    "top_n": 20,                  # 报告展示条数
}


def load_config() -> dict:
    cfg = dict(DEFAULT_CONFIG)
    try:
        if CFG_PATH.exists():
            cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg: dict) -> bool:
    try:
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return True
    except Exception:
        return False


# ---------- 风险词(前置拦截) ----------
# 液体/膏霜/喷雾(含内容物) —— 需 FDA / 欧代 / EPR, 个人店基本过不了
LIQUID_KEYS = [
    "口红", "唇釉", "唇彩", "粉底", "粉饼", "眼影", "睫毛膏", "眼线", "眉笔",
    "精华", "面霜", "乳液", "爽肤水", "化妆水", "面膜", "防晒霜", "卸妆水",
    "香水", "香薰", "精油", "洗发", "沐浴", "身体乳", "护手霜", "牙膏",
    "液体", "膏体", "喷雾", "气垫", "遮瑕", "腮红", "高光", "定妆喷雾",
]
# 带电 —— 需认证
ELECTRIC_KEYS = ["电池", "充电", "电动", "LED", "发光", "震动", "USB", "加热"]
# 品牌 / IP 风险(标题里出现已知品牌或卡通)
IP_KEYS = ["迪士尼", "迪斯尼", "漫威", "皮卡丘", "史努比", "hello kitty", "耐克", "阿迪",
           "苹果", "华为", "小米", "lv", "香奈儿", "迪奥", "雅诗兰黛", "兰蔻", "官方", "正品"]
# 成人/敏感(沿用选品经验)
SENSITIVE_KEYS = ["乳头", "凸点", "性感", "丰胸", "情趣", "私处", "诱惑", "丝袜", "女仆", "御姐"]
# 空瓶/分装类豁免: 命中液体词但明确是空容器 → 不算液体
EMPTY_CONTAINER_KEYS = ["空瓶", "分装瓶", "喷雾瓶（空", "喷雾瓶(空", "空罐", "空盒", "收纳瓶"]


@dataclass
class TemuRow:
    item_id: str
    title: str
    category: str
    cost: float                 # 1688 采购价
    express_fee: float          # 1688 发货运费
    weight_kg: float
    cost_total: float           # 成本合计
    quote_price: float          # 建议报价(报给平台)
    floor_price: float          # 核价底线(低于弃款)
    target_price: float         # 目标报价(砍完后)
    estimated_net: float        # 按目标报价成交的净利
    estimated_margin: float     # 净利率%
    gross_margin: float         # 毛利空间%
    recommend: bool             # 是否推荐
    score: float                # 推荐分
    risks: str                  # 风险说明(分号分隔)
    sku_count: int              # SKU 规格数
    image_count: int            # 图片数
    item_url: str = ""

    def to_dict(self):
        return asdict(self)


def _num(v, default=0.0):
    try:
        return float(v)
    except (TypeError, ValueError):
        return default


def _text_of(p) -> str:
    return str(getattr(p, "title", "") or "")


def _attrs_of(p) -> dict:
    return getattr(p, "attributes", {}) or {}


def _brand_of(p) -> str:
    a = _attrs_of(p)
    raw = a.get("品牌") or a.get("brand") or ""
    if isinstance(raw, list):
        raw = raw[0] if raw else ""
    raw = str(raw).strip()
    # 脏值/占位符不当品牌(1688 部分商品会把分隔符填进品牌字段)
    return "" if raw in ("", "其他", "无", "否", "不限定", "不适用", "-", "/", "--", "0", "null") else raw


def _sku_count(p) -> int:
    sku = getattr(p, "sku_combos", None)
    if isinstance(sku, list):
        return len(sku)
    src = getattr(p, "sourceSku", None)
    if isinstance(src, dict):
        return len(src.get("skuBase", []) or [])
    specs = getattr(p, "sku_specs", None)
    return len(specs) if isinstance(specs, list) else 0


def _image_count(p) -> int:
    imgs = getattr(p, "images", None)
    return len(imgs) if isinstance(imgs, list) else 0


def classify_risks(p) -> list:
    """返回风险说明列表; 空列表表示无硬伤。"""
    t = _text_of(p)
    low = t.lower()
    risks = []
    has_empty_excuse = any(k in t for k in EMPTY_CONTAINER_KEYS)

    liquid = []
    for k in LIQUID_KEYS:
        if k not in t:
            continue
        # "非液体"/"不含液体" 只对通用词豁免, 具体品类词(口红/精华等)照拦
        if k in ("液体", "膏体", "喷雾") and ("非液体" in t or "不含液体" in t):
            continue
        if has_empty_excuse and k in ("液体", "膏体", "喷雾"):
            continue
        liquid.append(k)
    if liquid:
        risks.append("液体/膏霜类(需FDA/欧代, 个人店勿碰):" + "/".join(liquid[:3]))
    electric = [k for k in ELECTRIC_KEYS if k.lower() in low]
    if electric:
        risks.append("带电类(需认证):" + "/".join(electric[:3]))
    ip = [k for k in IP_KEYS if k.lower() in low]
    brand = _brand_of(p)
    if ip:
        risks.append("品牌/IP词:" + "/".join(ip[:3]))
    if brand:
        risks.append("含品牌[" + brand + "]")
    sensitive = [k for k in SENSITIVE_KEYS if k in t]
    if sensitive:
        risks.append("敏感词:" + "/".join(sensitive[:3]))
    return risks


def price_row(p, cfg: dict = None) -> TemuRow:
    """对单个商品做筛选 + 供货价测算。"""
    cfg = cfg or DEFAULT_CONFIG
    cost = _num(getattr(p, "price", 0))
    express = _num(getattr(p, "express_fee", 0))
    amort_qty = _num(cfg.get("express_amortize_qty"), 0)
    if amort_qty > 0 and express > 0:
        express = round(express / amort_qty, 2)  # 全托管批量发货: 单件运费按整箱摊销
    weight = _num(getattr(p, "weight_kg", 0))
    if weight <= 0:
        weight = _num(cfg.get("default_weight_kg"), 0.2)

    inbound = _num(cfg.get("inbound_per_item"))
    packaging = _num(cfg.get("packaging_fee"))
    wastage = _num(cfg.get("wastage_rate"))
    min_margin = _num(cfg.get("min_net_margin"), 0.08)
    target = _num(cfg.get("target_net_margin"), 0.25)
    buffer = _num(cfg.get("quote_buffer"), 0.12)

    # 成本合计 = 采购 + 1688运费(发国内仓) + 到仓服务费 + 包装 + 损耗
    cost_total = cost + express + inbound + packaging + cost * wastage

    denom_floor = 1 - min_margin
    denom_target = 1 - target
    floor_price = round(cost_total / denom_floor, 2) if denom_floor > 0 else 0.0
    target_price = round(cost_total / denom_target, 2) if denom_target > 0 else cost_total * 2
    quote_price = round(target_price * (1 + buffer), 2)

    est_net = round(target_price - cost_total, 2)
    est_margin = round((est_net / target_price * 100), 1) if target_price > 0 else 0.0
    gross = round((target_price - cost - express) / target_price * 100, 1) if target_price > 0 else 0.0

    risks = classify_risks(p)
    hard_block = any(k.startswith(("液体/", "带电", "品牌/IP", "含品牌", "敏感词")) for k in risks)

    reasons = []
    if hard_block:
        reasons.append("类目/合规风险")
    if weight > _num(cfg.get("max_weight_kg"), 0.5):
        reasons.append(f"超重{weight}kg")
    if cost < _num(cfg.get("min_cost"), 1.0):
        reasons.append("采购价过低")
    if cost > _num(cfg.get("max_cost"), 30.0):
        reasons.append("采购价过高")
    if gross < _num(cfg.get("min_gross_margin"), 0.30) * 100:
        reasons.append("毛利空间不足")

    recommend = not reasons
    # 推荐分: 毛利空间(50) + 需求信号(20,用销量) + 素材完整度(20) + 轻小件(10)
    sale = getattr(p, "sale_count", 0)
    try:
        sale = int(str(sale).replace("+", "") or 0)
    except (TypeError, ValueError):
        sale = 0
    demand = min(1.0, sale / 500.0)
    images = _image_count(p)
    skus = _sku_count(p)
    material = min(1.0, images / 6.0) * 0.6 + min(1.0, skus / 3.0) * 0.4
    light = 1.0 if weight <= 0.3 else 0.5
    score = round(
        min(1.0, gross / 50.0) * 50 + demand * 20 + material * 20 + light * 10, 1
    ) if recommend else 0.0

    return TemuRow(
        item_id=str(getattr(p, "item_id", "") or ""),
        title=_text_of(p),
        category=str(getattr(p, "category", "") or ""),
        cost=round(cost, 2),
        express_fee=round(express, 2),
        weight_kg=weight,
        cost_total=round(cost_total, 2),
        quote_price=quote_price,
        floor_price=floor_price,
        target_price=target_price,
        estimated_net=est_net,
        estimated_margin=est_margin,
        gross_margin=gross,
        recommend=recommend,
        score=score,
        risks="；".join(risks + reasons),
        sku_count=skus,
        image_count=images,
        item_url=str(getattr(p, "item_url", "") or ""),
    )


def screen(products: list, cfg: dict = None) -> list:
    """批量筛选+测算, 按推荐分降序。"""
    cfg = cfg or load_config()
    rows = [price_row(p, cfg) for p in (products or [])]
    rows.sort(key=lambda r: (r.recommend, r.score), reverse=True)
    return rows


def render_report(rows: list, cfg: dict = None) -> str:
    """中文报告。"""
    cfg = cfg or load_config()
    ok = [r for r in rows if r.recommend]
    bad = [r for r in rows if not r.recommend]
    lines = [
        "🌍 Temu 全托管选品 + 供货价测算",
        f"共 {len(rows)} 款 | 推荐 {len(ok)} 款 | 不推荐 {len(bad)} 款",
        "═" * 46,
        f"公式: 报价 = (采购+运费+到仓{cfg.get('inbound_per_item')}+包装{cfg.get('packaging_fee')}"
        f"+损耗{float(cfg.get('wastage_rate',0))*100:.0f}%) ÷ (1−目标净利率{float(cfg.get('target_net_margin',.25))*100:.0f}%) × (1+议价{float(cfg.get('quote_buffer',.12))*100:.0f}%)",
        f"核价底线净利率 {float(cfg.get('min_net_margin',.08))*100:.0f}% — 低于此价弃款",
        (f"1688 运费按 {int(float(cfg.get('express_amortize_qty',0)))} 件批量摊销(全托管整箱发货)"
         if float(cfg.get("express_amortize_qty", 0)) > 0
         else "1688 运费按单件计算"),
        "",
    ]
    if not ok:
        lines.append("⚠ 没有符合 Temu 全托管条件的商品。试试降低毛利门槛或换一批非液体轻小件货源。")
    else:
        lines.append("【推荐款】(按推荐分排序)")
        for r in ok[:int(cfg.get("top_n", 20))]:
            lines.append(
                f"[{r.score:.0f}分] {r.title[:26]}"
            )
            lines.append(
                f"  采购¥{r.cost} + 运费¥{r.express_fee} → 成本¥{r.cost_total} | "
                f"建议报价¥{r.quote_price} (底线¥{r.floor_price})"
            )
            lines.append(
                f"  砍到¥{r.target_price} 成交 → 净利¥{r.estimated_net} ({r.estimated_margin}%) | 毛利空间{r.gross_margin}%"
            )
    if bad:
        lines.append("")
        lines.append("【不推荐】")
        for r in bad[:8]:
            lines.append(f"✗ {r.title[:24]} — {r.risks[:60]}")
        if len(bad) > 8:
            lines.append(f"(另 {len(bad)-8} 款未展开)")
    lines.append("")
    lines.append("（纯本地规则测算, 不登录/不报价/不备货；平台核价以买手回价为准。）")
    return "\n".join(lines)


# ---------- 导出 ----------
BUILD_HEADERS = ["item_id", "商品名称(中文)", "标题建议结构", "类目", "采购价", "1688运费",
                 "重量kg", "SKU数", "图片数", "建议供货价", "核价底线", "目标报价", "1688链接"]
RECORD_HEADERS = ["item_id", "商品名称", "建议报价", "买手回价(手填)", "是否接受(手填)",
                  "净利(回价后)", "寄样状态(手填)", "备货状态(手填)", "备注"]


def _title_hint(row: TemuRow) -> str:
    """Temu 英文标题结构建议(不翻译, 只给结构)。"""
    cat = row.category or "product"
    return f"{cat} + 核心属性词 + 规格/尺寸 + 适用场景 (英文, 参考平台搜索词)"


def export_build_list(rows: list, path=None) -> Path:
    """导出建品清单 CSV, 照着往 Temu 后台填。"""
    path = Path(path) if path else DATA / "temu_build_list.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(BUILD_HEADERS)
        for r in rows:
            if not r.recommend:
                continue
            w.writerow([r.item_id, r.title, _title_hint(r), r.category,
                        r.cost, r.express_fee, r.weight_kg, r.sku_count, r.image_count,
                        r.quote_price, r.floor_price, r.target_price, r.item_url])
    return path


def export_quote_record(rows: list, path=None) -> Path:
    """导出核价记录表 CSV(部分列留空供人工填买手回价/状态)。"""
    path = Path(path) if path else DATA / "temu_quote_record.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(RECORD_HEADERS)
        for r in rows:
            if not r.recommend:
                continue
            w.writerow([r.item_id, r.title, r.quote_price, "", "", "", "", "", ""])
    return path


def load_products():
    """从 data/collected_1688.json 读取采集结果(兼容 Product 对象与 dict)。"""
    import loader
    try:
        return loader.load_from_json_file(str(DATA / "collected_1688.json"))
    except Exception:
        return []


def selftest():
    """不依赖真实数据的自测。"""
    class P:
        def __init__(self, **kw):
            self.__dict__.update(kw)

    samples = [
        P(item_id="1", title="硅胶化妆刷套装 软毛 便携 非液体", price=8.0, express_fee=3.0,
          weight_kg=0.2, category="化妆刷", attributes={"品牌": ["其他"]},
          sourceSku={"skuBase": [{}, {}, {}]}, images=[1] * 7, sale_count=1200,
          item_url="https://detail.1688.com/offer/1.html"),
        P(item_id="2", title="口红 哑光 持久 不脱色 正品", price=15.0, express_fee=2.0,
          weight_kg=0.05, category="唇膏", attributes={"品牌": ["迪奥"]},
          sourceSku={}, images=[1] * 5, sale_count=8000),
        P(item_id="3", title="电动洁面仪 充电 USB 震动", price=30.0, express_fee=5.0,
          weight_kg=0.3, category="洁面仪", attributes={}, sourceSku={}, images=[1] * 4),
        P(item_id="4", title="美妆蛋 粉扑 超软 不吃粉 三只装", price=3.5, express_fee=1.0,
          weight_kg=0.08, category="粉扑", attributes={"品牌": ["无"]},
          sourceSku={"skuBase": [{}, {}]}, images=[1] * 6, sale_count=500),
    ]
    rows = screen(samples, DEFAULT_CONFIG)
    print(render_report(rows, DEFAULT_CONFIG))
    print("\n=== 断言 ===")
    assert rows[0].recommend and rows[0].item_id in ("1", "4"), "轻小件应推荐"
    assert not next(r for r in rows if r.item_id == "2").recommend, "口红应拦截"
    assert not next(r for r in rows if r.item_id == "3").recommend, "带电应拦截"
    for r in rows:
        if r.recommend:
            assert r.quote_price > r.floor_price > r.cost_total, "报价>底线>成本"
            assert r.floor_price > 0 and r.target_price > 0
    print("OK: 全部断言通过")


if __name__ == "__main__":
    selftest()
