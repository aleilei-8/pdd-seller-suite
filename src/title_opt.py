#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
title_optimizer.py —— 拼多多标题优化核心

原理(对齐穿云箭的 Jieba 分词 + 自研 SEO 规则):
  1. 用 jieba 将原标题拆成词根; 再从 1688 属性(attributes)和规格(sku_specs)补属性词。
  2. 按拼多多推流机制重组标题: 核心品类词放首尾(权重高) + 属性词(材质/样式/人群/场景)
     + 长尾词 + 营销词; 总长控制在 ~30 字(覆盖足够且聚焦)。
  3. 违规过滤: 剔除极限词(最/第一/顶级...会降权)与品牌词(防侵权)。
  4. 生成多种侧重的候选标题 + 0-100 评分 + 扣分项说明 + 优化建议。

说明: 拼多多"推流"= 标题关键词匹配买家搜索词 × 商品权重(销量/点击/转化/价格/服务)。
      本工具只优化"关键词匹配"这一可掌控部分(覆盖搜索词 + 类目匹配 + 不违规),
      真正的流量还要靠商品权重(价格/主图/销量)后续经营。
"""
import re
import jieba
import jieba.posseg as pseg

# ---- 内置词库 ----
# 极限词/违规词(拼多多会判违规或降权, 必须剔除)
VIOLATION_WORDS = {
    "最", "第一", "顶级", "极致", "国家级", "唯一", "绝对", "百分百", "全网", "销量第一",
    "王牌", "最佳", "最好", "最大", "最低", "绝无仅有", "史无前例", "万能", "永久", "世界级",
    "极品", "第一品牌", "独家", "首选", "销量冠军", "全网第一", "最高级", "最先进", "最便宜",
    "最新", "最流行", "最时尚", "国家级", "顶级", "王牌", "销量第一", "冠军", "第一", "顶级",
    "唯一", "首创", "纯天然", "100%", "百分之百", "领袖", "领跑", "领导者", "领先",
}

# 敏感词(拼多多会审核/限流, 标题里建议改写或剔除)
SENSITIVE_WORDS = {
    "乳头", "凸点", "性感", "丰胸", "美乳", "迷情", "私处", "情趣", "媚", "诱惑", "透视",
    "裸", "内衣", "胸罩", "文胸", "内裤", "丝袜", "女仆", "御姐", "少妇",
}

# 优质核心品类词白名单(优先作为核心词, 可按品类扩展)
CORE_WHITELIST = {
    # 饰品/发饰
    "发夹", "发卡", "头饰", "发箍", "发圈", "皮筋", "发带", "发绳", "发网", "发抓", "大肠发圈",
    "发饰", "头花", "花环", "皇冠", "耳环", "耳钉", "项链", "手链", "戒指", "饰品", "胸针",
    # 贴/美妆
    "胸贴", "乳贴", "硅胶贴", "双眼皮贴", "美目贴",
    # 收纳
    "收纳盒", "收纳箱", "收纳袋", "收纳架", "收纳筐", "收纳桶", "置物架",
    # 数码配件
    "手机壳", "数据线", "充电线", "耳机", "充电宝", "钢化膜",
    # 服饰
    "打底衫", "打底裤", "连衣裙", "T恤", "卫衣", "外套", "裤子", "裙子",
}

# 绝不作核心词的碎片/歧义词(过碎或属属性/场景)
CORE_BLACKLIST = {
    "夹子", "卡", "婚纱", "硅胶", "隐形", "吊带", "婚礼", "女孩", "男孩", "娜斯", "贴防",
    "儿童", "女童", "宝宝", "婴儿", "成人", "男士", "女士", "韩国", "中国", "日本",
}

# 属性词清洗: 仅保留纯中文 2-6 字(去除括号/数字/标点/英文)
ATTR_RE = re.compile(r"^[\u4e00-\u9fa5]{2,6}$")

# 营销词(可用, 提升点击)
MARKETING_WORDS = {
    "新款", "现货", "包邮", "批发", "源头工厂", "工厂直供", "清仓", "特价", "ins风", "网红",
    "爆款", "高性价比", "厂家直销", "一件代发", "现货速发", "2024新款", "2025新款",
}

# 修饰黑名单(有信息量但非"核心品类词"): 人群/地名/通用修饰
MODIFIER_BLACKLIST = {
    "儿童", "女童", "男童", "宝宝", "小女孩", "小男孩", "婴儿", "成人", "男人", "女人", "男士", "女士",
    "韩国", "中国", "日本", "广州", "义乌", "深圳", "杭州",
    "新款", "可爱", "公主", "气质", "网红", "ins", "春秋", "冬季", "夏季", "春季", "秋季", "通用",
    "常规", "日常", "简约", "韩系", "日系", "欧式", "百搭", "高级感", "高颜值", "高品质",
    "批发", "包邮", "现货", "清仓", "特价", "工厂", "源头", "直销", "正品", "原单", "同款",
    "显瘦", "显大", "防走光", "透气", "舒适", "柔软", "加厚", "薄款", "厚款", "大码", "小码",
    "爆款", "ins风", "网红", "2024", "2025", "贴防", "供应", "神器", "专用", "神器",
}

# 从 attributes 提取时, 只保留这些"有价值属性键"(避免货号/品牌/序列号)
USEFUL_ATTR_KEYS = {
    "产地", "样式", "系列", "造型", "材质", "处理工艺", "风格", "适用人群", "适用场景",
    "功能", "图案", "流行元素", "适用对象", "款式", "型号", "形状", "颜色分类", "类别",
}

# 属性值里要剔除的无意义词(含属性键名泄漏/太泛/包装方式); 用"包含"匹配(覆盖"小号印刷袋"等组合值)
ATTR_NOISE = {"无", "其他", "通用", "常规", "否", "是", "不限", "默认", "普通", "基础",
              "图案", "系列", "款式", "类型", "种类", "风格", "型号", "形状", "类别", "颜色分类",
              "贴防", "独立装", "均码", "（均码）", "【独立装", "印刷袋", "包装", "散装", "袋", "盒", "件", "个",
              "印刷", "胶粘", "小号", "大号", "中号", "通用型", "普通型", "定制", "随机"}


def _clean_text(s: str) -> str:
    return re.sub(r"\s+", "", str(s or "")).strip()


def extract_words(title: str, attributes: dict = None, sku_specs: list = None, brand: str = None, extra_terms: list = None):
    """返回 {core, attr, market, violations, brand_hits, sensitive}

    extra_terms: 拼多多真实推流词(下拉联想词), 代表买家实际搜索词,
                 覆盖权重最高, 会排在属性词最前面优先入标题。
    """
    title = _clean_text(title)
    tokens = [(w, flag) for w, flag in pseg.cut(title) if len(w) >= 2 and re.match(r"[\u4e00-\u9fa5A-Za-z0-9]+", w)]

    core_cand, attr_from_title, market = set(), set(), set()
    violations, brand_hits, sensitive = set(), set(), set()

    for w, flag in tokens:
        if w in SENSITIVE_WORDS:
            sensitive.add(w)
            continue
        if w in VIOLATION_WORDS:
            violations.add(w)
            continue
        if brand and w and brand in w:
            brand_hits.add(w)
            continue
        if w in MODIFIER_BLACKLIST:
            if w in {"儿童", "女童", "男童", "宝宝", "小女孩", "小男孩", "婴儿", "成人", "男士", "女士", "韩系", "日系", "欧式", "公主", "气质", "ins风", "网红"}:
                attr_from_title.add(w)
            continue
        if flag.startswith("n") and w not in MODIFIER_BLACKLIST:  # 名词候选(排除修饰/碎片)
            core_cand.add(w)
        elif flag.startswith(("a", "v")):
            if w in MARKETING_WORDS:
                market.add(w)
            elif ATTR_RE.match(w):
                attr_from_title.add(w)
        else:
            if ATTR_RE.match(w):
                attr_from_title.add(w)

    # 核心词: 白名单优先, 排除黑名单/敏感/碎片; 取最具代表性的前若干
    in_white = [w for w in core_cand if w in CORE_WHITELIST]
    rest = [w for w in core_cand if w not in CORE_WHITELIST and w not in CORE_BLACKLIST]
    core = sorted(in_white + rest, key=len, reverse=True)[:4]

    # 属性词补充: 1688 attributes(只取有价值键的值, 清洗为纯中文2-6字)
    # 属性词补充: 1688 attributes(只取有价值键的值, 清洗为纯中文2-6字)
    # 产地(origin)词单独收集, 排末尾, 不参与主属性堆砌
    attr_from_meta, attr_origin = set(), set()
    if attributes:
        for k, vals in attributes.items():
            if k not in USEFUL_ATTR_KEYS:
                continue
            for v in vals:
                v = _clean_text(v)
                if not v or any(n in v for n in ATTR_NOISE) or v in SENSITIVE_WORDS:
                    continue
                v = re.split(r"[\d\s件个只条]", v)[0]
                if ATTR_RE.match(v):
                    (attr_origin if k == "产地" else attr_from_meta).add(v)
                if len(attr_from_meta) >= 10:
                    break

    # 规格值(颜色等) —— 同样需过 ATTR_NOISE 噪声过滤(小号/胶粘/印刷袋/件/个 等)
    for s in (sku_specs or []):
        s = _clean_text(s)
        s = re.split(r"[\d\s件个只条]", s)[0]
        if any(n in s for n in ATTR_NOISE) or s in SENSITIVE_WORDS:
            continue
        if ATTR_RE.match(s):
            attr_from_meta.add(s)

    # 属性词: 去除核心词/营销词, 以及"被其他属性包含"的重复(公主 vs 公主系列)
    all_attr = sorted((attr_from_title | attr_from_meta) - set(core) - market)
    attr = [a for i, a in enumerate(all_attr)
            if not any(a != b and a in b for b in all_attr)]
    # 统一噪声过滤(标题分词/属性/规格 三路都可能漏进 小号/胶粘/印刷袋/件/个 等)
    attr = [a for a in attr
            if not any(n in a for n in ATTR_NOISE) and a not in SENSITIVE_WORDS]
    # 后缀清洗(几何型→几何) + 重新校验 + 短词优先(长系列名靠后, 截断时先被砍)
    attr = [re.sub(r"(型|款|式)$", "", a) for a in attr]
    attr = sorted({a for a in attr if ATTR_RE.match(a)}, key=len)
    # 产地词(义乌/金华...)排末尾, 截断时优先被砍, 不堆砌
    attr_origin = sorted({re.sub(r"(型|款|式)$", "", a) for a in attr_origin
                          if ATTR_RE.match(re.sub(r"(型|款|式)$", "", a))}, key=len)
    attr = attr + attr_origin[:3]

    # 拼多多真实推流词优先: 清洗后前置(去过噪/违规/敏感/重复), 截断时最后才被砍
    if extra_terms:
        clean_extra, seen_extra = [], set()
        for e in extra_terms:
            e = _clean_text(e)
            if (not e or len(e) > 8 or e in seen_extra
                    or any(n in e for n in ATTR_NOISE)
                    or e in SENSITIVE_WORDS or e in VIOLATION_WORDS
                    or e in set(core) or e in market):
                continue
            seen_extra.add(e)
            clean_extra.append(e)
        attr = clean_extra + [a for a in attr if a not in seen_extra]

    if brand:
        brand_hits.add(brand)

    return {
        "core": core,
        "attr": attr,
        "market": sorted(market),
        "violations": sorted(violations),
        "brand_hits": sorted(brand_hits),
        "sensitive": sorted(sensitive),
    }


def _build_title(core, attr, market, layout, max_len=30):
    """按 layout 拼标题并截断到 max_len"""
    core_s = "".join(core[:3])
    attr_s = "".join(attr[:8])
    market_s = "".join(market[:1])

    if layout == "core_first":
        seq = [core_s, attr_s, market_s]
    elif layout == "long_tail":
        seq = [attr_s[:4], core_s, attr_s[4:]]
    elif layout == "market_first":
        seq = [market_s, core_s, attr_s]
    else:  # attr_stack
        seq = [core_s, attr_s, market_s]

    out = "".join(seq)
    # 截断: 从末尾属性词逐步削减, 保证 <= max_len
    while len(out) > max_len and attr:
        attr = attr[:-1]
        attr_s = "".join(attr[:8])
        if layout == "core_first":
            out = core_s + attr_s + market_s
        elif layout == "long_tail":
            out = attr_s[:4] + core_s + attr_s[4:]
        elif layout == "market_first":
            out = market_s + core_s + attr_s
        else:
            out = core_s + attr_s + market_s
    return out[:max_len]


def score_title(title, words, max_len=30):
    """返回 (score, breakdown)"""
    bd = {}
    L = len(title)
    # 长度分 20
    if 15 <= L <= max_len:
        bd["长度(20)"] = 20
    else:
        bd["长度(20)"] = max(0, 20 - abs(L - 24) * 2)
    # 核心词位置分 20 (核心词出现在首或尾)
    core_in_edge = any(title.startswith(c) or title.endswith(c) for c in words["core"])
    bd["核心词位置(20)"] = 20 if core_in_edge else 10
    # 违规分 30 (极限词/品牌词/敏感词 都扣分; 敏感词尤其要剔除避免限流)
    v = len(words["violations"]) + len(words["brand_hits"]) + len(words["sensitive"])
    bd["违规检查(30)"] = max(0, 30 - v * 12)
    # 词覆盖分 30 (覆盖的属性词数, 分母取前8)
    denom = min(8, max(1, len(words["attr"])))
    covered = sum(1 for a in words["attr"][:8] if a in title)
    bd["词覆盖(30)"] = round(covered / denom * 30)
    total = sum(bd.values())
    return total, bd


def optimize_title(title: str, attributes: dict = None, sku_specs: list = None, brand: str = None, max_len: int = 30, extra_terms: list = None):
    """对单个标题做优化, 返回结构化结果

    extra_terms: 拼多多下拉联想词(真实推流词), 会优先进标题以提升搜索覆盖。
    """
    words = extract_words(title, attributes, sku_specs, brand, extra_terms)
    layouts = {
        "大词覆盖(主推)": "core_first",
        "长尾精准(新品期)": "long_tail",
        "营销前置": "market_first",
        "属性堆叠(广覆盖)": "attr_stack",
    }
    market = words["market"] or ["包邮"]  # 原标题无营销词时, 默认补"包邮"(拼多多标题几乎都带)
    candidates = []
    for name, layout in layouts.items():
        t = _build_title(words["core"], words["attr"], market, layout, max_len)
        sc, bd = score_title(t, words, max_len)
        candidates.append({"label": name, "title": t, "score": sc, "breakdown": bd})

    candidates.sort(key=lambda x: x["score"], reverse=True)

    # 建议
    advice = []
    if words["violations"]:
        advice.append(f"原标题含极限词 {'/'.join(words['violations'])}, 已剔除(否则降权)")
    if words["brand_hits"]:
        advice.append(f"检测到品牌词 {'/'.join(words['brand_hits'])}, 已剔除(防侵权, 除非你是授权方)")
    if words["sensitive"]:
        advice.append(f"⚠ 原标题含敏感词 {'/'.join(words['sensitive'])}, 已剔除(拼多多会审核/限流, 建议改写)")
    if len(words["core"]) == 0:
        advice.append("未识别出核心品类词, 请确认原标题或手动指定品类词")
    if len(words["attr"]) == 0:
        advice.append("属性词较少, 建议在 attributes/规格 里补充材质/样式/场景以提升覆盖")

    return {
        "original": title,
        "extracted": {
            "core": words["core"], "attr": words["attr"], "market": words["market"],
            "violations": words["violations"], "brand_hits": words["brand_hits"],
            "sensitive": words["sensitive"],
        },
        "candidates": candidates,
        "advice": advice,
    }


def optimize(product, extra_terms: list = None) -> dict:
    """对 loader.Product 做优化

    extra_terms: 可选, 拼多多下拉联想词(真实推流词)。
    """
    brand = None
    raw = product.attributes.get("品牌")
    if raw:
        b = raw[0] if isinstance(raw, list) else raw
        # 与 selector_scorer 对齐: 占位/无品牌值不视为真实品牌词, 避免误报侵权剔除
        if b not in ("其他", "无", "否", "", "无品牌", "不限定", "不适用"):
            brand = b
    return optimize_title(product.title, product.attributes, product.sku_specs, brand,
                          extra_terms=extra_terms)


if __name__ == "__main__":
    import sys
    sys.path.insert(0, __file__.rsplit("\\src", 1)[0].replace("\\", "/") + "/src")
    from loader import load_from_cyj_cache
    prods = load_from_cyj_cache()
    for p in prods[:3]:
        r = optimize(p)
        print(f"\n商品 {p.item_id} | 进货价¥{p.price} | 销量{p.sale_count_text}")
        print(f"原标题: {r['original']}")
        print(f"  提取 核心词={r['extracted']['core']} 属性词={r['extracted']['attr'][:8]}")
        if r['extracted']['violations']: print(f"  违规词={r['extracted']['violations']}")
        if r['extracted']['brand_hits']: print(f"  品牌词={r['extracted']['brand_hits']}")
        if r['extracted']['sensitive']: print(f"  敏感词={r['extracted']['sensitive']}")
        for c in r["candidates"]:
            print(f"  [{c['score']:>3}] {c['label']}: {c['title']}")
        for a in r["advice"]:
            print(f"  ⚠ {a}")
