#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""合规护栏 (纯规则 · 离线可用 · 只读)。

客服折中方案的底座: 所有「AI 生成的话术 / 导入的聊天记录」在送模型前、
在展示给客服复制前, 都经过本模块过滤, 确保两件事:

1. 不出现广告法极限词 / 平台违禁词 / 虚假承诺
   —— 只提示与建议替换, 不代替卖家做最终判断。
2. 买家隐私 (手机/地址/姓名/订单号/微信/身份证/银行卡/邮箱) 先脱敏
   —— 送 LLM 的文本必须是脱敏后的; 绝不把原始 PII 发给第三方接口。

设计原则:
- 只读: 本模块不联网、不写店铺、不碰账号, 只做文本扫描与替换。
- 保守: 规则宁可多报(提示), 不漏报; 但是否修改由卖家/客服决定。
- 可解释: 每条命中给出 category / severity / hint, 方便 GUI 展示为什么。

用法:
    scan(text)          -> [finding, ...]              扫描违规词
    desensitize(text)   -> (clean_text, hits)          脱敏隐私
    guard(text)         -> {passed, findings, safe_text, summary}
"""
import re
import unicodedata

import active_words

# ---------- 违禁词表 ----------
# (词/正则, 类别, 严重度 high/medium/low, 替换建议)
# 说明: 广告法《广告用语》极限词 + 电商平台常见违禁表达。仅作风险提示,
#       最终合规判断以平台规则与法务为准。
BANNED = [
    # —— 广告法极限词 ——
    ("最好", "极限词", "high", "体验更佳"),
    ("最佳", "极限词", "high", "优选"),
    ("最强", "极限词", "high", "表现出色"),
    ("最优", "极限词", "high", "优质"),
    ("全网最低价", "极限词", "high", "平台优惠价"),
    ("最低价", "极限词", "high", "优惠价"),
    ("全网最低", "极限词", "high", "平台优惠价"),
    ("史上最低", "极限词", "high", "近期优惠价"),
    ("最便宜", "极限词", "high", "价格实惠"),
    ("第一品牌", "极限词", "high", "优质品牌"),
    ("全网第一", "极限词", "high", "热销"),
    ("销量第一", "极限词", "high", "销量领先"),
    ("排名第一", "极限词", "high", "名列前茅"),
    ("顶级", "极限词", "high", "高品质"),
    ("顶尖", "极限词", "high", "优质"),
    ("极品", "极限词", "high", "精品"),
    ("极致", "极限词", "medium", "出色"),
    ("唯一", "极限词", "high", "独具特色"),
    ("独一无二", "极限词", "high", "颇具特色"),
    ("国家级", "极限词", "high", "优质"),
    ("世界级", "极限词", "high", "高品质"),
    ("绝对", "极限词", "medium", ""),
    ("100%", "极限词", "medium", ""),
    ("百分百", "极限词", "medium", ""),
    ("万能", "极限词", "high", "多用途"),
    ("永久", "极限词", "medium", "长期"),
    ("终身", "极限词", "medium", "长期使用"),
    ("无敌", "极限词", "medium", "出色"),
    ("冠军", "极限词", "medium", "优选"),
    ("王牌", "极限词", "medium", "主推"),
    ("首选", "极限词", "medium", "推荐"),
    ("顶级配置", "极限词", "high", "高配置"),
    (r"最[好佳强优便低高]", "极限词", "medium", "较"),  # 兜底: 最好/最贵等
    (r"第[一1](?:名|品牌|的位置)", "极限词", "high", "领先"),

    # —— 虚假 / 过度承诺 ——
    ("包赔", "虚假承诺", "high", "按平台规则处理"),
    ("假一赔十", "虚假承诺", "high", "正品保障(如属实)"),
    ("无条件退", "虚假承诺", "high", "支持售后申请"),
    ("全额退款", "虚假承诺", "medium", "可申请退款"),
    ("包治", "虚假承诺", "high", "有助于"),
    ("根治", "虚假承诺", "high", "改善"),
    ("药到病除", "虚假承诺", "high", "谨慎表述"),
    ("三天见效", "虚假承诺", "high", "效果因人而异"),
    ("无效退款", "虚假承诺", "high", "谨慎表述"),
    ("终身保修", "虚假承诺", "medium", "质保期内保修"),
    ("全国联保", "虚假承诺", "medium", "按厂家政策保修"),

    # —— 平台违禁: 引导私域 / 虚假交易 ——
    ("加微信", "平台违禁", "high", "请在平台内沟通"),
    ("微信", "平台违禁", "medium", "平台内联系"),
    ("加QQ", "平台违禁", "high", "请在平台内沟通"),
    ("私下交易", "平台违禁", "high", "请通过平台下单"),
    ("线下付款", "平台违禁", "high", "请通过平台支付"),
    ("脱离平台", "平台违禁", "high", "请通过平台交易"),
    ("好评返现", "平台违禁", "high", ""),
    ("好评返红包", "平台违禁", "high", ""),
    ("五星返现", "平台违禁", "high", ""),
    ("刷单", "平台违禁", "high", ""),
    ("刷好评", "平台违禁", "high", ""),
]

# ---------- 语义等价簇 (换皮词检测) ----------
# 广告法判断的是语义, 不是字面: "最好"换成"最佳/首选/顶级/最棒"照样违规。
# 字面命中的词由 BANNED 负责; 这里覆盖的是"换了皮但语义没变"的表达。
# 每条: id / canonical / category / severity / hint / aliases / patterns
# - aliases: 明确等价词, 在归一化文本上精确匹配
# - patterns: 变形写法(插空格、简繁、拆字、英文排名写法)
# 注意: 这里只做本地规则提示, 不联网、不送模型, 误报时人工复核即可。
SEMANTIC_CLUSTERS = [
    {
        "id": "absolute_top", "canonical": "最好",
        "category": "极限词(语义等价)", "severity": "high", "hint": "体验更佳/表现不错",
        "aliases": [
            "最棒", "最赞", "最牛", "最牛逼", "最给力", "最靠谱", "最专业", "最先进",
            "最安全", "最有效", "最优秀", "最强悍", "最好用", "最佳选择", "首选品牌",
            "顶配", "顶流", "顶级配置", "巅峰之作", "无出其右", "无人能及",
            "史无前例", "绝无仅有", "前无古人", "行业第一", "全网第一", "销量冠军",
            "no1", "no.1", "top1", "top 1", "number one",
        ],
        "patterns": [
            r"最[\s·•\-—_]*[好佳强优棒赞牛省惠]",
            r"(?:行业|全网|销量|排名|全网销量)第\s*[一1]",
            r"no\.?\s*1", r"top\s*0?1", r"number\s*one",
        ],
    },
    {
        "id": "absolute_rare", "canonical": "唯一",
        "category": "排他用语(语义等价)", "severity": "high", "hint": "独具特色",
        "aliases": ["唯一指定", "仅此一家", "别无二家", "独家首发", "绝无二家", "独此一家"],
        "patterns": [r"唯\s*一\s*(?:指定|一家|选择|渠道)"],
    },
    {
        "id": "exclusive_soft", "canonical": "独家",
        "category": "排他用语(语义等价)", "severity": "medium", "hint": "有授权的独家可保留, 否则改成具体特点",
        "aliases": ["独家", "首家", "首创", "独家定制", "限量首发", "独一无二"],
        "patterns": [],
    },
    {
        "id": "exaggeration", "canonical": "天花板",
        "category": "夸大宣传(语义等价)", "severity": "medium", "hint": "用可验证的具体描述",
        "aliases": ["天花板", "天花板级", "天花板级别", "神级", "封神", "王者级", "标杆产品"],
        "patterns": [],
    },
    {
        "id": "price_bottom", "canonical": "最低价",
        "category": "极限词(语义等价)", "severity": "high", "hint": "平台优惠价/近期优惠价",
        "aliases": [
            "全网底价", "地板价", "跳楼价", "亏本价", "白菜价", "史低", "历史最低",
            "击穿底价", "一降到底", "史上最划算", "全网最低价", "全网最低",
        ],
        "patterns": [r"史[上]?最(?:低|便宜|划算)", r"(?:全网|全店|历史)最(?:低|划算|便宜)"],
    },
    {
        "id": "price_soft", "canonical": "超低价",
        "category": "价格表述(语义等价)", "severity": "medium", "hint": "价格实惠",
        "aliases": ["超低价", "特价中的特价", "冰点价", "抄底价"],
        "patterns": [],
    },
    {
        "id": "absolute_promise", "canonical": "绝对",
        "category": "虚假承诺(语义等价)", "severity": "high", "hint": "改成可验证的具体条件",
        "aliases": [
            "零风险", "无副作用", "万无一失", "保证有效", "一定有效", "包好",
            "包过", "永不", "百分百正品", "100%正品", "绝对正品", "无条件退换",
        ],
        "patterns": [r"100\s*%\s*(?:正品|有效|安全|无副作用)"],
    },
    {
        "id": "medical_claim", "canonical": "治疗",
        "category": "医疗功效(语义等价)", "severity": "high", "hint": "删除医疗功效表述(如属实需资质)",
        "aliases": [
            "治疗", "治愈", "疗效", "药效", "康复", "抗癌", "防癌", "增强免疫力",
            "延年益寿", "降血压", "降血糖", "处方药", "包治百病", "治百病",
        ],
        "patterns": [],
    },
    {
        "id": "effect_claim", "canonical": "立竿见影",
        "category": "效果承诺(语义等价)", "severity": "high", "hint": "效果因人而异",
        "aliases": [
            "立竿见影", "一次见效", "瞬间见效", "马上见效", "即刻见效",
            "快速见效", "见效快", "永久有效", "终身有效",
        ],
        "patterns": [r"\d+\s*天\s*(?:见效|美白|祛斑|瘦身|见效)"],
    },
    {
        "id": "private_traffic", "canonical": "私聊",
        "category": "平台违禁(语义等价)", "severity": "high", "hint": "请在平台内沟通",
        "aliases": [
            "私聊", "站外交易", "二维码", "扫码", "薇信", "威信", "扣扣",
            "QQ群", "加Q", "vx", "wechat", "手机号联系", "联系方式",
        ],
        "patterns": [r"加\s*[Vv薇威]\s*信?", r"[Vv]\s*我", r"vx\s*[:：]?", r"扫码\s*(?:加|聊|领)"],
    },
    {
        "id": "rebate", "canonical": "返现",
        "category": "平台违禁(语义等价)", "severity": "high", "hint": "删除好评返现相关表述",
        "aliases": [
            "返现", "好评送礼", "晒图返现", "晒单返现", "评价返现",
            "五星好评返现", "好评有礼", "晒图有礼",
        ],
        "patterns": [r"(?:好评|晒图|晒单|五星)\s*(?:返|送|礼|奖)"],
    },
    {
        "id": "authority", "canonical": "权威认证",
        "category": "权威背书(语义等价)", "severity": "high", "hint": "无授权/依据请删除",
        "aliases": [
            "央视推荐", "央视上榜", "中科院认证", "国家认证", "权威认证",
            "政府推荐", "指定用品", "驰名商标", "中国名牌", "免检产品",
        ],
        "patterns": [],
    },
    {
        "id": "authority_soft", "canonical": "明星同款",
        "category": "权威背书(语义等价)", "severity": "medium", "hint": "需真实可查且有授权",
        "aliases": ["明星同款", "专家推荐", "医生推荐", "达人推荐"],
        "patterns": [],
    },
    {
        "id": "superstition", "canonical": "转运",
        "category": "迷信低俗(语义等价)", "severity": "medium", "hint": "避免封建迷信表述",
        "aliases": ["转运", "招财", "辟邪", "开光", "风水", "旺夫", "驱邪", "改运"],
        "patterns": [],
    },
    {
        "id": "beauty_special", "canonical": "美白",
        "category": "美妆功效(需特证)", "severity": "low", "hint": "特殊功效需特证/检测报告, 无依据请删除; 非美妆品类可忽略",
        "aliases": [
            "美白", "祛斑", "抗衰老", "除皱", "抗皱", "淡斑", "防晒",
            "祛痘", "生发", "瘦身", "减肥", "丰胸",
        ],
        "patterns": [],
    },
]

# ---------- 隐私脱敏规则 ----------
# (名称, 正则, 掩码). 顺序重要: 长/特定的放前面, 避免被短规则先吃掉。
PII_PATTERNS = [
    ("身份证", re.compile(r"(?<!\d)\d{17}[\dXx](?!\d)"), "【身份证】"),
    ("手机号", re.compile(r"(?<!\d)1[3-9]\d{9}(?!\d)"), "【手机号】"),
    ("银行卡", re.compile(r"(?<!\d)\d{16,19}(?!\d)"), "【银行卡】"),
    ("邮箱", re.compile(r"[\w.+-]+@[\w-]+\.[\w.-]+"), "【邮箱】"),
    ("微信号", re.compile(r"(?:微信号?|vx|weixin|微信ID)\s*[:：]?\s*[A-Za-z0-9_-]{5,20}", re.I), "【微信号】"),
    ("QQ号", re.compile(r"(?:QQ|qq)\s*[:：]?\s*\d{5,12}"), "【QQ号】"),
    ("姓名", re.compile(r"(?:收货人|收件人|姓名|买家)\s*[:：]\s*[\u4e00-\u9fa5]{1,4}(?=[\s，,。；;]|$)"), "姓名：【姓名】"),
    ("订单号", re.compile(r"(?:订单号?|单号|订单编号)\s*[:：]?\s*\d{8,20}"), "订单号：【订单号】"),
    ("地址", re.compile(
        r"[\u4e00-\u9fa5]{2,8}(?:省|市|自治区)[\u4e00-\u9fa50-9]{2,40}?"
        r"(?:号|栋|幢|室|单元|层|楼|小区|花园|大厦|村|镇|街道|路|街|巷)[0-9\u4e00-\u9fa5]{0,8}"), "【地址】"),
    ("长数字", re.compile(r"(?<!\d)\d{11,19}(?!\d)"), "【编号】"),
]

# 送模型前的硬性约束说明(GUI 展示用)
PRIVACY_NOTICE = (
    "合规提示: 本模块只生成『建议话术』, 不会自动发送、不登录店铺、不执行任何写操作。"
    "所有买家个人信息在送模型前均已脱敏; 请人工确认后复制到拼多多后台。"
)

# 已编译的正则型违禁词
_COMPILED = []
for _w, _cat, _sev, _hint in BANNED:
    if _w.startswith("r:"):
        _COMPILED.append((re.compile(_w[2:]), _cat, _sev, _hint, _w[2:]))
    else:
        _COMPILED.append((None, _cat, _sev, _hint, _w))

# ---------- 语义等价: 归一化 ----------
# 换皮常见手法: 插空格/标点、简繁替换、谐音同形字、全角、英文排名写法。
# 归一化时同步记录每个字符在原文中的下标, 命中后能准确回到原文位置替换。
_SKIP_PUNCT = frozenset(
    " \t\r\n\v\f·•∙・‧-—–_~～./\\|[]()（）【】〔〕《》〈〉「」『』"
    ",，。.!！?？:：;；'\"“”‘’`^*+=<>￥$"
)
_CONFUSABLE = {
    "蕞": "最", "冣": "最", "頂": "顶", "級": "级", "選": "选", "優": "优",
    "極": "极", "網": "网", "絡": "络", "價": "价", "銷": "销", "國": "国",
    "際": "际", "獨": "独", "無": "无", "萬": "万", "終": "终", "療": "疗",
    "癒": "愈", "証": "证", "認": "认", "權": "权", "實": "实", "產": "产",
    "薇": "微", "衛": "卫", "饋": "馈", "贈": "赠",
}


def _normalize(text):
    """返回 (归一化文本, 每个归一化字符对应的原文下标)。"""
    chars, positions = [], []
    for i, ch in enumerate(text or ""):
        c = unicodedata.normalize("NFKC", ch)
        if len(c) != 1:
            c = ch
        c = _CONFUSABLE.get(c, c)
        if c in _SKIP_PUNCT:
            continue
        chars.append(c)
        positions.append(i)
    return "".join(chars), positions


_SEMANTIC_READY = []
for _c in SEMANTIC_CLUSTERS:
    _SEMANTIC_READY.append({
        "id": _c["id"],
        "category": _c["category"],
        "severity": _c["severity"],
        "hint": _c["hint"],
        "aliases": [
            (a, _normalize(a)[0].lower(), _c["category"], _c["severity"], _c["hint"])
            for a in _c.get("aliases", []) if _normalize(a)[0]
        ],
        "patterns": [re.compile(p) for p in _c.get("patterns", [])],
    })

# 通用字面词的「换皮变体」也走语义层: 繁体(頂級)、同形字(蕞好)、插空格(最 好)、
# 全角(１００％) 等, 字面规则抓不到, 归一化后按原词的类别/严重度报出。
_SEMANTIC_READY.append({
    "id": "banned_variant",
    "category": "换皮词(语义等价)",
    "severity": "high",
    "hint": "",
    "aliases": [
        (w, _normalize(w)[0].lower(), cat, sev, hint)
        for w, cat, sev, hint in BANNED
        if not w.startswith("r:") and _normalize(w)[0]
    ],
    "patterns": [],
})

_SEV_RANK = {"high": 3, "medium": 2, "low": 1}
_SRC_RANK = {"active": 3, "literal": 2, "semantic": 1}


def _finding(keyword, category, severity, hint, start, end, matched,
             source="literal", match_type="exact", cluster="", confidence="high"):
    """统一命中结构; 旧字段保持不变, 新增 source/match_type/cluster/confidence。"""
    return {
        "keyword": keyword,
        "category": category,
        "severity": severity,
        "hint": hint,
        "start": start,
        "end": end,
        "matched": matched,
        "source": source,
        "match_type": match_type,
        "cluster": cluster,
        "confidence": confidence,
    }


def _better(a, b):
    """重叠命中取舍: 严重度 > 覆盖长度 > 来源优先级(私有活清单最高)。"""
    sa, sb = _SEV_RANK.get(a["severity"], 0), _SEV_RANK.get(b["severity"], 0)
    if sa != sb:
        return sa > sb
    la, lb = a["end"] - a["start"], b["end"] - b["start"]
    if la != lb:
        return la > lb
    return _SRC_RANK.get(a.get("source"), 0) > _SRC_RANK.get(b.get("source"), 0)


def _dedup(out):
    """按位置去重; 同位置保留更危险/更长/来源更可信的一条。"""
    out.sort(key=lambda x: (x["start"], -(x["end"] - x["start"])))
    dedup = []
    for f in out:
        if dedup and f["start"] < dedup[-1]["end"]:
            if _better(f, dedup[-1]):
                dedup[-1] = f
            continue
        dedup.append(f)
    return dedup


def _scan_literal(text):
    """原有字面/正则规则扫描 (保留为独立入口, 便于对比)。"""
    out = []
    for rx, cat, sev, hint, word in _COMPILED:
        it = rx.finditer(text) if rx else re.finditer(re.escape(word), text, re.I)
        for m in it:
            out.append(_finding(word, cat, sev, hint, m.start(), m.end(), m.group(0),
                                "literal", "regex" if rx else "exact"))
    return out


def scan_semantic(text):
    """语义等价扫描: 换皮词 / 插空格 / 简繁 / 同形字 / 英文排名写法。"""
    text = text or ""
    norm, pos = _normalize(text)
    low = norm.lower()
    if not low:
        return []
    out = []
    for c in _SEMANTIC_READY:
        for display, needle, cat, sev, hint in c["aliases"]:
            if not needle:
                continue
            start = 0
            while True:
                i = low.find(needle, start)
                if i < 0:
                    break
                s, e = pos[i], pos[i + len(needle) - 1] + 1
                out.append(_finding(display, cat, sev, hint,
                                    s, e, text[s:e], "semantic", "alias", c["id"]))
                start = i + 1
        for rx in c["patterns"]:
            for m in rx.finditer(low):
                if not m.group(0):
                    continue
                s, e = pos[m.start()], pos[m.end() - 1] + 1
                out.append(_finding(m.group(0), c["category"], c["severity"], c["hint"],
                                    s, e, text[s:e], "semantic", "pattern", c["id"]))
    return out


def scan_active(text):
    """店铺私有活清单扫描 (精确字面匹配, 不模糊归一化, 避免误伤)。"""
    text = text or ""
    low = text.lower()
    out = []
    for e in active_words.entries():
        for term in [e["term"]] + list(e["aliases"]):
            needle = str(term).strip()
            if not needle:
                continue
            nl = needle.lower()
            start = 0
            while True:
                i = low.find(nl, start)
                if i < 0:
                    break
                out.append(_finding(needle, e["category"], e["severity"], e["hint"],
                                    i, i + len(needle), text[i:i + len(needle)],
                                    "active", "active", "active"))
                start = i + 1
    return out


def scan(text: str):
    """扫描违禁词, 返回命中列表 (不修改文本)。

    命中来源 source: literal(通用规则) / semantic(语义等价) / active(私有活清单)。
    兼容旧字段: keyword, category, severity, hint, start, end, matched。
    """
    text = text or ""
    return _dedup(_scan_literal(text) + scan_semantic(text) + scan_active(text))


def hit_summary(findings):
    """按来源统计命中, 供 GUI / 报告展示。"""
    from collections import Counter
    c = Counter(f.get("source", "literal") for f in findings or [])
    bits = []
    if c.get("literal"):
        bits.append(f"通用规则 {c['literal']}")
    if c.get("semantic"):
        bits.append(f"语义等价 {c['semantic']}")
    if c.get("active"):
        bits.append(f"私有活清单 {c['active']}")
    return " / ".join(bits) if bits else "无命中"


def redact(text: str, findings=None):
    """按扫描结果替换违禁词为建议词 (保留原文结构)。"""
    text = text or ""
    findings = findings if findings is not None else scan(text)
    # 从后往前替换, 避免位置偏移
    for f in sorted(findings, key=lambda x: x["start"], reverse=True):
        hint = f.get("hint") or ""
        text = text[:f["start"]] + hint + text[f["end"]:]
    return text


def desensitize(text: str):
    """脱敏买家隐私 -> (clean_text, hits)。

    hits: [{type, masked}] 记录脱敏了哪些类型 (不记录原值, 避免二次泄露)。
    """
    text = text or ""
    hits = []
    for name, rx, mask in PII_PATTERNS:
        new_text, n = rx.subn(mask, text)
        if n:
            text = new_text
            hits.append({"type": name, "count": n, "masked": mask})
    return text, hits


def guard(text: str, mask_privacy=True):
    """综合护栏: 脱敏隐私 + 扫描违禁词, 返回结构化结果。

    {passed: bool, findings: [...], safe_text: str, privacy_hits: [...], summary: str}
    - passed: 没有 high 级违规 (medium 提示但不阻断复制)
    - safe_text: 脱敏+替换后的可复制文本 (始终不含原始 PII)
    """
    clean, privacy_hits = desensitize(text) if mask_privacy else (text or "", [])
    findings = scan(clean)
    safe = redact(clean, findings)
    high = [f for f in findings if f["severity"] == "high"]
    med = [f for f in findings if f["severity"] == "medium"]
    bits = []
    if privacy_hits:
        bits.append("隐私已脱敏: " + "、".join(f"{h['type']}×{h['count']}" for h in privacy_hits))
    if high:
        bits.append(f"⛔ {len(high)} 处高风险词(已替换), 复制前请确认")
    elif med:
        bits.append(f"⚠ {len(med)} 处提示词(已替换)")
    else:
        bits.append("✅ 未发现违禁词")
    bits.append(PRIVACY_NOTICE)
    return {
        "passed": not high,
        "findings": findings,
        "safe_text": safe,
        "privacy_hits": privacy_hits,
        "summary": "  ".join(bits),
    }


def scan_for_llm(text: str):
    """送模型前的强制脱敏入口: 只返回脱敏文本, 绝不外传原始 PII。"""
    return desensitize(text or "")[0]


if __name__ == "__main__":
    demo = ("买家张三 电话13812345678 地址广东省深圳市南山区科技园1号, "
            "订单号12345678901234, 想要全网最低价, 加微信给你好评返现")
    print("原文:", demo)
    print("\n扫描:", scan(demo))
    print("\n来源:", hit_summary(scan(demo)))
    print("\n脱敏:", desensitize(demo))
    print("\n护栏:", guard(demo))
    print("\n语义等价示例:", [f["matched"] for f in scan("最 棒 的顶级面膜 NO.1 加薇信")])
