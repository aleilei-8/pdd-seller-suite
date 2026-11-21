#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""AI 助手: 自然语言 → 业务动作 (v1)。

把「人围着工具转」升级为「AI 调度工具」的第一步:
  用户一句话 → 解析意图 → 调用既有业务模块 → 产出可读结论/触发预填。

设计原则:
- 解析: 优先 LLM(OpenAI 兼容接口, 如 DeepSeek); 未配置/调用失败 → 本地规则解析兜底, 离线也可用。
- 安全: 全部只读动作 + 采集预填(不自动执行任何写操作); 定价/上架/客服回复等仍由人最终确认。
- 客服模块(cs_*): 只生成建议话术/统计, 隐私先脱敏, 违禁词先护栏, 绝不自动发送或改售后单。
- 编排升级: daily_brief(全店早报) / listing_audit(文案合规体检) 均为只读聚合/扫描, 不写任何数据。
- 解耦: 本模块不 import tkinter, 不直接改 GUI 状态; 结果通过返回值交给 GUI 层。
"""
import json
import re
import urllib.request

try:
    from paths import ROOT
except Exception:  # 独立脚本测试时允许相对路径
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent

CFG_PATH = ROOT / "ai_config.json"
DEFAULT_CFG = {
    "api_base": "https://api.deepseek.com/v1",
    "api_key": "",
    "model": "deepseek-chat",
    "temperature": 0.2,
}

# ---------- 配置 ----------

def load_config():
    cfg = dict(DEFAULT_CFG)
    try:
        cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return True
    except Exception:
        return False


# ---------- LLM 意图解析 ----------

SYSTEM_PROMPT = """你是「拼多多全链路工作台」的 AI 助手, 负责把卖家的中文指令解析成动作。
只输出一个 JSON 对象, 格式:
{"action": "<动作名>", "params": {...}, "reply": "<面向用户的中文回复>"}

可用动作:
1. price_calc  保本价/定价测算。params: cost(进货价,必填), inbound(进货运费,缺省0),
   outbound(发货运费,缺省0), target_profit(想每件净赚的金额), target_margin(目标净利率,0.08表示8%),
   category(类目名,可选)
2. select_report  选品评分报告。params: top_n(默认8)
3. orders_summary  店铺订单/经营分析
4. stock_report  库存预警
5. collect  采集1688商品。params: urls(数组, 从消息里抽取 detail.1688.com/offer/<数字> 链接或8位以上纯数字商品ID)
6. ad_report  广告投产分析(需已导入推广报表)
7. finance_report  全成本利润对账(把广告费扣进净利, 算真实净利; 需已导入订单CSV和推广报表)
8. competitor_report  竞品价格监控(比价/跟价建议; 从竞品台账取数)
9. review  点评选品/指定商品(配置了模型时给 AI 建议)。params: top_n(默认5)
10. cs_script  客服话术建议。params: question(买家问题原文,必填), product_info(商品公开信息,可选)。
    注意: 本动作只读, 只生成建议话术供人工复制, 绝不自动发送。
11. cs_qa  客服记录质检(需先在客服助手页导入聊天CSV)。统计高频问题/情绪/过度承诺。
12. cs_aftersales  售后处理建议(需先在客服助手页导入售后CSV)。只出建议, 不代替执行。
13. cs_sla  客服响应时效看板(需先导入聊天CSV)。纯统计。
14. daily_brief  全店经营早报。跨模块聚合订单/广告/财务/竞品/库存/客服 → 全店体检 + 今日待办。纯只读。
15. listing_audit  商品文案合规体检。扫描已采集商品标题/详情或手动文案里的极限词/违禁词。params: text(可选,手动文案); 无 text 则扫描已采集商品。
16. chat  自由问答/闲聊/业务咨询, reply 里给出简洁专业的中文回答(可结合消息上下文)
17. help  问你能做什么

规则: 只输出 JSON, 不要输出其他任何文字。数字字段输出为数字而非字符串。"""


def _extract_json(text: str):
    """从模型回复里稳健地抠出 JSON(容忍 ```json 围栏/前后杂文)。"""
    if not text:
        return None
    m = re.search(r"\{.*\}", text, re.S)
    if not m:
        return None
    try:
        return json.loads(m.group(0))
    except Exception:
        return None


def llm_chat(messages: list, cfg: dict, timeout: float = 40.0):
    """调用 OpenAI 兼容 /chat/completions, 返回回复文本; 失败返回 None。"""
    base = (cfg.get("api_base") or "").rstrip("/")
    key = cfg.get("api_key") or ""
    if not base or not key:
        return None
    url = base + "/chat/completions"
    body = json.dumps({
        "model": cfg.get("model") or "deepseek-chat",
        "messages": messages,
        "temperature": float(cfg.get("temperature") or 0.2),
        "stream": False,
    }).encode("utf-8")
    req = urllib.request.Request(url, data=body, headers={
        "Authorization": "Bearer " + key,
        "Content-Type": "application/json",
    }, method="POST")
    try:
        with urllib.request.urlopen(req, timeout=timeout) as r:
            data = json.loads(r.read().decode("utf-8"))
        return data["choices"][0]["message"]["content"]
    except Exception:
        return None


def llm_parse(text: str, cfg: dict):
    """LLM 解析意图; 失败/未配置返回 None(调用方降级规则解析)。"""
    out = llm_chat([
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": text},
    ], cfg)
    j = _extract_json(out or "")
    if not j or j.get("action") not in (
            "price_calc", "select_report", "orders_summary",
            "stock_report", "collect", "ad_report", "finance_report",
            "competitor_report", "review", "chat", "help",
            "cs_script", "cs_qa", "cs_aftersales", "cs_sla",
            "daily_brief", "listing_audit"):
        return None
    j.setdefault("params", {})
    j.setdefault("reply", "")
    j["source"] = "llm"
    return j


# ---------- 本地规则解析(离线兜底) ----------

_OFFER_RE = re.compile(r"detail\.1688\.com/offer/(\d+)", re.I)
_ID_RE = re.compile(r"(?<!\d)(\d{8,})(?!\d)")
_NUM_RE = re.compile(r"\d+(?:\.\d+)?")


def _extract_urls(text: str):
    out, seen = [], set()
    for m in _OFFER_RE.finditer(text or ""):
        u = f"https://detail.1688.com/offer/{m.group(1)}.html"
        if u not in seen:
            seen.add(u); out.append(u)
    cleaned = re.sub(r"https?://\S+", " ", text or "")
    for m in _ID_RE.finditer(cleaned):
        u = f"https://detail.1688.com/offer/{m.group(1)}.html"
        if u not in seen:
            seen.add(u); out.append(u)
    return out


def rule_parse(text: str):
    t = (text or "").strip()
    low = t.lower()
    urls = _extract_urls(t)

    # 1) 含 1688 链接/ID → 采集(优先级最高, 链接即意图)
    if urls:
        return {"action": "collect", "params": {"urls": urls},
                "reply": "", "source": "rule"}

    # 2) 编排层二次升级: 全店早报 / 文案体检(需先于"经营/广告/标题"等单模块词)
    if any(k in t for k in ("经营早报", "全店早报", "每日简报", "今日待办", "全店体检", "日报")):
        return {"action": "daily_brief", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("文案体检", "违禁词", "极限词", "合规检查", "合规体检", "标题检查")):
        m = re.search(r"[:：]\s*(.+)", t)
        return {"action": "listing_audit", "params": {"text": m.group(1).strip() if m else ""},
                "reply": "", "source": "rule"}

    # 3) 定价测算: 关键词 + 数字
    nums = [float(x) for x in _NUM_RE.findall(t.replace("％", "%"))]
    if any(k in t for k in ("保本", "定价", "售价", "卖多少", "定价多少", "定价", "赚", "利润", "净利")) \
            and "率" not in t.replace("净利率", "") and nums:
        cost = nums[0]
        inbound = 0.0
        m = re.search(r"运费\s*[:：]?\s*(\d+(?:\.\d+)?)", t)
        if m:
            inbound = float(m.group(1))
        elif len(nums) >= 2 and any(k in t for k in ("运费", "邮费")):
            inbound = nums[1]
        target_profit = None
        m = re.search(r"(?:赚|净利)\s*(?:一?件)?\s*(\d+(?:\.\d+)?)", t)
        if m:
            target_profit = float(m.group(1))
        target_margin = None
        m = re.search(r"净利率\s*[:：]?\s*(\d+(?:\.\d+)?)\s*%", t)
        if m:
            target_margin = float(m.group(1)) / 100.0
        return {"action": "price_calc",
                "params": {"cost": cost, "inbound": inbound,
                           "target_profit": target_profit,
                           "target_margin": target_margin},
                "reply": "", "source": "rule"}

    # 3) 各类报告
    if any(k in t for k in ("选品", "评分", "值得上", "推荐卖什么", "爆款")):
        return {"action": "select_report", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("订单", "经营", "店铺分析", "营业额", "成交额", "数据分析")):
        return {"action": "orders_summary", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("库存", "缺货", "积压", "断货")):
        return {"action": "stock_report", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("对账", "真实利润", "真实净利", "广告后", "扣掉广告", "广告费扣", "全成本")):
        return {"action": "finance_report", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("竞品", "对手", "比价", "跟价", "别人卖", "对家")):
        return {"action": "competitor_report", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("广告", "投产", "ROI", "roi", "推广", "点击成本", "投产比")):
        return {"action": "ad_report", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("点评", "AI建议", "给建议", " Analyze")):
        return {"action": "review", "params": {}, "reply": "", "source": "rule"}
    # 客服折中方案(只读)
    if any(k in t for k in ("售后", "退款建议", "怎么处理退款", "售后建议")):
        return {"action": "cs_aftersales", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("响应时效", "首响", "回复速度", "客服时效", "未回复")):
        return {"action": "cs_sla", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("客服质检", "质检", "聊天记录分析", "客服记录")):
        return {"action": "cs_qa", "params": {}, "reply": "", "source": "rule"}
    if any(k in t for k in ("话术", "怎么回复", "回复买家", "客服回复", "帮我回")):
        return {"action": "cs_script", "params": {"question": t}, "reply": "", "source": "rule"}
    if any(k in low for k in ("help", "帮助", "你能做什么", "怎么用", "功能")):
        return {"action": "help", "params": {}, "reply": "", "source": "rule"}

    return {"action": "chat", "params": {}, "reply": "", "source": "rule"}


def parse(text: str, cfg: dict = None):
    """统一入口: LLM 优先, 失败降级规则。"""
    cfg = cfg or load_config()
    if cfg.get("api_key"):
        try:
            j = llm_parse(text, cfg)
            if j:
                return j
        except Exception:
            pass
    return rule_parse(text)


# ---------- 动作执行 ----------

def _money(v):
    return f"¥{v:.2f}"


def _do_price_calc(p: dict, ctx: dict) -> str:
    import pricing
    cfg = ctx["config"]
    cost = float(p.get("cost") or 0)
    if cost <= 0:
        return "请告诉我进货价, 例如: 进货8运费2想每件赚3块"
    inbound = float(p.get("inbound") or 0)
    outbound = float(p.get("outbound") or 0)
    category = p.get("category") or ""
    tp = p.get("target_profit")
    tm = p.get("target_margin")
    lines = [f"📊 定价测算 (进货{_money(cost)} + 进货运费{_money(inbound)}"
             + (f" + 发货运费{_money(outbound)}" if outbound else "") + ")"]

    if tp:
        price = pricing.price_for_target(cost, inbound, outbound,
                                         target_profit=float(tp))
        pb = pricing.compute_profit(cost, inbound, price, outbound)
        lines.append(f"目标每件净赚 {_money(float(tp))} → 建议售价 {_money(price)}")
    elif tm:
        price = pricing.price_for_target(cost, inbound, outbound,
                                         target_margin=float(tm))
        pb = pricing.compute_profit(cost, inbound, price, outbound)
        lines.append(f"目标净利率 {float(tm)*100:.1f}% → 建议售价 {_money(price)}")
    else:
        pb = pricing.suggest_price(cost, inbound, outbound, cfg, category=category)
        price = pb.revenue
        lines.append(f"按全局目标净利率 {cfg.target_net_margin*100:.0f}% → "
                     f"建议售价 {_money(price)}")

    lines.append(
        f"拆解: 售价{_money(pb.revenue)} − 进货{_money(pb.cost_price)} "
        f"− 进货运费{_money(pb.inbound_shipping)} − 发货运费{_money(pb.outbound_shipping)} "
        f"− 佣金{_money(pb.commission)} − 支付费{_money(pb.payment_fee)}"
        + (f" − 其他{_money(pb.other_fee)}" if pb.other_fee else ""))
    lines.append(f"→ 单件净利 {_money(pb.net_profit)} (净利率 {pb.net_margin:.1f}%)")
    # 拼单/参考价与上架页一致的建议区间
    lines.append(f"参考: 单买价 {_money(round(price*1.10, 2))}, "
                 f"拼单价 {_money(round(price*1.10-2, 2))}")
    return "\n".join(lines)


def _do_select_report(p: dict, ctx: dict) -> str:
    import selector
    products = ctx["products"]
    if not products:
        return "还没有货源数据。先在「上架导出」页采集 1688 链接, 或直接把链接发给我。"
    rows = selector.score_all(products, ctx["config"])
    rows.sort(key=lambda r: r.get("total", 0), reverse=True)
    top_n = int(p.get("top_n") or 8)
    grades = {}
    for r in rows:
        grades[r["grade"]] = grades.get(r["grade"], 0) + 1
    lines = [f"🌟 选品评分报告 (共 {len(rows)} 个商品)",
             "等级分布: " + "  ".join(f"{k}×{v}" for k, v in grades.items()) or "—"]
    lines.append("")
    for r in rows[:top_n]:
        lines.append(f"[{r['grade']}] {r['title'][:28]}  综合分{r['total']}"
                     f" | 进货{_money(r['cost'])} 建议售{_money(r['suggest_price'])}"
                     f" 净利{_money(r['net_profit'])}({r['net_margin']}%)")
        for note in (r.get("notes") or [])[:2]:
            lines.append(f"    ⚠ {note}")
    if len(rows) > top_n:
        lines.append(f"(其余 {len(rows)-top_n} 个分数较低, 已省略)")
    lines.append("\n💬 想让 AI 逐条点评? 输入「点评这几个商品」(需配置模型, 未配置则用本地规则)")
    return "\n".join(lines)


def _do_orders_summary(p: dict, ctx: dict) -> str:
    import analysis
    orders = ctx["orders"]
    if not orders:
        return "还没有订单数据。请先在「店铺分析」页导入订单/结算 CSV, 再让我分析。"
    k = analysis.store_kpi(orders)
    lines = [f"📊 店铺经营概览 (订单 {k['orders']} 笔 / {k['units']} 件)",
             f"成交额 {_money(k['revenue'])} | 客单价 {_money(k['avg_order_value'])}",
             f"总成本 {_money(k['cost'])} | 发货运费 {_money(k['shipping_out'])}",
             f"净利 {_money(k['net_profit'])} (净利率 {k['net_margin']}%)"
             f" | 成本关联率 {k['linked_rate']}%"]
    cats = analysis.aggregate(orders, "category")
    if cats:
        lines.append("")
        lines.append("按类目净利 Top5:")
        for r in cats[:5]:
            lines.append(f"  {r.key}: {r.orders}单 成交{_money(r.revenue)} "
                         f"净利{_money(r.net_profit)} ({r.net_margin}%)")
    return "\n".join(lines)


def _do_stock_report(p: dict, ctx: dict) -> str:
    products = ctx["products"]
    if not products:
        return "还没有货源数据。先把 1688 商品采集进来, 我才能核对 SKU 库存。"
    LOW = 50
    zero, low, total = [], [], 0
    for prod in products:
        stocks = [int(c.get("stock") or 0) for c in (prod.sku_combos or [])]
        s = sum(stocks)
        total += s
        if not stocks or s == 0:
            zero.append(prod)
        elif s < LOW:
            low.append((prod, s))
    lines = [f"📦 库存预警 (在库货源 {len(products)} 个, SKU总库存 {total})"]
    if zero:
        lines.append(f"\n🔴 无库存/未取到库存 {len(zero)} 个:")
        for prod in zero[:8]:
            lines.append(f"  {prod.title[:30]} (ID {prod.item_id})")
    if low:
        lines.append(f"\n🟡 低库存(<{LOW}) {len(low)} 个:")
        for prod, s in low[:8]:
            lines.append(f"  {prod.title[:30]} 仅 {s} 件")
    if not zero and not low:
        lines.append("✅ 暂无缺货/积压风险")
    lines.append("\n提示: 库存来自采集时的 1688 数据, 可能已过期, 重要决策前请重新采集核对。")
    return "\n".join(lines)


def _do_collect(p: dict, ctx: dict) -> dict:
    urls = p.get("urls") or []
    if not urls:
        return {"reply": "请把 1688 商品链接发给我(每行一个, 支持 detail.1688.com/offer/xxx 或纯数字商品ID)。"}
    if len(urls) > 30:
        urls = urls[:30]
    return {"reply": f"已识别 {len(urls)} 个商品链接, 正在为你打开采集窗口并预填。\n"
                     "请在弹窗里确认链接后点『开始采集(在线)』(写操作需人工确认)。",
            "collect_urls": urls}


def _do_review(p: dict, ctx: dict) -> dict:
    """LLM 批量点评商品: 一次请求点评 Top N, 控制 token; 未配模型优雅降级。"""
    import selector
    cfg = load_config()
    products = ctx["products"]
    if not products:
        return {"reply": "还没有货源数据。先采集 1688 商品后, 我才能点评。"}
    if not cfg.get("api_key"):
        # 无模型: 用本地评分理由给出结构化点评(离线降级)
        rows = selector.score_all(products, ctx["config"])
        rows.sort(key=lambda r: r.get("total", 0), reverse=True)
        top_n = int(p.get("top_n") or 5)
        lines = [f"📝 选品点评(本地规则版, 配置模型可获 AI 建议):"]
        for r in rows[:top_n]:
            notes = "；".join((r.get("notes") or [])[:2]) or "无明显风险信号"
            lines.append(f"• {r['title'][:26]} [{r['grade']}] 净利{_money(r['net_profit'])} "
                         f"— {notes}")
        return {"reply": "\n".join(lines)}

    # 有模型: 一次请求批量点评
    rows = selector.score_all(products, ctx["config"])
    rows.sort(key=lambda r: r.get("total", 0), reverse=True)
    top_n = min(int(p.get("top_n") or 5), 10)
    picked = rows[:top_n]
    items = [{
        "序号": i + 1,
        "标题": r["title"][:50],
        "等级": r["grade"],
        "进货": r["cost"],
        "建议售价": r["suggest_price"],
        "净利": r["net_profit"],
        "评分理由": (r.get("notes") or [])[:2],
    } for i, r in enumerate(picked)]

    sys_prompt = (
        "你是资深拼多多电商买手。针对每个商品给一句一针见血的中文点评(≤40字), "
        "聚焦: 值不值得上、最大风险、可执行动作。\n"
        '只输出 JSON: {"reviews":[{"序号":1,"点评":"..."}]}, 不要其他文字。'
    )
    out = llm_chat([
        {"role": "system", "content": sys_prompt},
        {"role": "user", "content": json.dumps(items, ensure_ascii=False)},
    ], cfg, timeout=60)
    j = _extract_json(out or "")
    reviews = (j or {}).get("reviews") or []
    if not reviews:
        return {"reply": "AI 点评失败(模型返回格式异常), 已为你保留本地评分报告。"}
    by_no = {int(r.get("序号") or 0): r.get("点评", "") for r in reviews
             if str(r.get("序号", "")).isdigit()}
    lines = ["📝 AI 选品点评:"]
    for i, r in enumerate(picked, 1):
        lines.append(f"{i}. {r['title'][:26]} [{r['grade']}]\n"
                     f"   {by_no.get(i, '（无点评）')}")
    return {"reply": "\n".join(lines)}


HELP_TEXT = """🤖 我能帮你做这些(直接说人话就行):

💰 定价测算   「进货8运费2想每件赚3块」「进价23 运费5 净利率8%」
🌟 选品报告   「哪些商品值得上架」「给个选品评分」
📊 经营分析   「分析一下订单」「店铺赚了多少钱」
📦 库存预警   「检查库存」「有没有缺货风险」
📣 广告投产   「分析广告投产」(先在广告投产页导入推广CSV)
📒 财务对账   「算一下扣掉广告费后的真实利润」(订单CSV+推广CSV, 在财务对账页刷新)
🎯 竞品比价   「分析竞品价格」「对手卖多少钱」(在竞品监控页登记台账, 结合保本线给跟价建议)
📝 商品点评   「点评这几个商品」(配置模型给 AI 建议, 没有则用本地规则)
💬 客服话术   「怎么回复买家: 全网最低价吗」(生成建议话术, 合规检查后供人工复制, 不自动发送)
🔍 客服质检   「客服质检」(需先导入聊天CSV; 高频问题/情绪/过度承诺)
🛠 售后建议   「售后建议」(需先导入退款CSV; 只出处理建议, 不代替执行)
⏱ 响应时效   「客服时效」(需先导入聊天CSV; 首响/未回复/高峰)
📰 全店早报   「今日待办」「全店体检」(跨模块聚合出全店简报+优先待办)
🛡 文案体检   「合规检查: 全网最低价面膜」(极速扫违禁词给安全替换文案)
🔗 采集预填   直接粘贴 1688 链接, 我帮你填进采集窗口
⏰ 定时监控   点上方「定时监控」设置无人值守(价格/库存异常才提醒)
💬 自由咨询   配置了模型 API 后, 电商问题也可以直接问我

⚙️ 点上方「模型设置」可接入 DeepSeek 等 OpenAI 兼容接口;
   未接入时用本地规则解析, 核心功能照常可用。"""

WELCOME = HELP_TEXT + "\n\n────────────────────────\n在下方输入指令开始 👇"


def run_action(intent: dict, ctx: dict):
    """执行意图, 返回 dict: {reply: str, collect_urls?: [..]}。

    GUI 层负责: 展示 reply; 若有 collect_urls 则在主线程打开采集窗口预填。
    """
    action = intent.get("action") or "help"
    params = intent.get("params") or {}
    try:
        if action == "price_calc":
            return {"reply": _do_price_calc(params, ctx)}
        if action == "select_report":
            return {"reply": _do_select_report(params, ctx)}
        if action == "orders_summary":
            return {"reply": _do_orders_summary(params, ctx)}
        if action == "stock_report":
            return {"reply": _do_stock_report(params, ctx)}
        if action == "collect":
            return _do_collect(params, ctx)
        if action == "ad_report":
            rows = ctx.get("ad_rows") or []
            if not rows:
                return {"reply": "还没有广告数据。请先在「AI 中心 → 广告投产」页导入推广报表 CSV。"}
            import ad_roi
            max_cpc = None
            try:
                max_cpc = float(ctx.get("config").max_cpc)
            except Exception:
                pass
            return {"reply": ad_roi.render_report(rows, max_cpc)}
        if action == "finance_report":
            res = ctx.get("finance_result")
            if not res:
                return {"reply": "还没有对账数据。请先在「财务对账」页导入订单 CSV（店铺分析页导入后会自动关联成本），"
                                 "再在「广告投产」页导入推广报表 CSV，然后点「刷新对账」。"}
            import finance
            return {"reply": finance.render_report(res)}
        if action == "competitor_report":
            import competitor
            watches = competitor.load_table()
            res = ctx.get("competitor_result")
            if not watches and not res:
                return {"reply": ("还没有竞品台账。请到「🎯 竞品监控」页点「＋ 添加商品/竞品」，"
                                  "登记我方售价和竞品售价; 也可以直接告诉我「我方卖X元, 竞品卖Y元」,")}
            res = res or {"analysis": competitor.analyze_all(watches)["rows"]}
            return {"reply": competitor.render_report(res)}
        if action == "review":
            return _do_review(params, ctx)
        # —— 客服折中方案(全部只读, 不触发任何写操作) ——
        if action == "cs_script":
            import cs_scripts
            question = params.get("question") or intent.get("_text") or ""
            res = cs_scripts.generate(question, params.get("product_info") or "")
            if not res.get("reply"):
                return {"reply": res.get("summary") or "没有匹配到话术。"}
            src = {"llm": "AI 生成", "local": "本地话术库"}.get(res["source"], "本地")
            body = res["reply"]
            if res.get("findings"):
                body += "\n\n" + res["summary"]
            body += "\n\n（来源: " + src + "；仅供人工确认后复制, 不会自动发送）"
            return {"reply": body}
        if action == "cs_qa":
            import cs_qa
            rows = ctx.get("cs_chat_rows") or []
            if not rows:
                return {"reply": "还没有客服聊天记录。请先在「客服助手 → 客服质检」页导入聊天 CSV。"}
            return {"reply": cs_qa.render_report(cs_qa.analyze(rows))}
        if action == "cs_aftersales":
            import cs_aftersales
            rows = ctx.get("cs_after_rows") or []
            if not rows:
                return {"reply": "还没有售后数据。请先在「客服助手 → 售后建议」页导入退款 CSV。"}
            return {"reply": cs_aftersales.render_report(cs_aftersales.analyze_all(rows))}
        if action == "cs_sla":
            import cs_sla
            rows = ctx.get("cs_chat_rows") or []
            if not rows:
                return {"reply": "还没有客服聊天记录。请先在「客服助手 → 响应时效」页导入聊天 CSV。"}
            return {"reply": cs_sla.render_report(cs_sla.analyze(rows))}
        if action == "daily_brief":
            import daily_brief
            res = daily_brief.build(ctx)
            return {"reply": daily_brief.render_report(res)}
        if action == "listing_audit":
            import listing_audit
            text = params.get("text") or ""
            if text:
                results = [listing_audit.audit_text(text, "手动文案")]
            else:
                results = listing_audit.audit_products(ctx.get("products") or [])
                if not results:
                    return {"reply": "还没有采集到商品。请先采集 1688 货源, 或用「合规检查: 你的文案」直接粘贴文案体检。"}
            return {"reply": listing_audit.render_report(results)}
    except Exception as e:
        return {"reply": f"执行出错: {e}\n请换个说法重试, 或直接用对应功能页操作。"}
    # chat / help
    if action == "chat":
        cfg = load_config()
        if cfg.get("api_key"):
            reply = llm_chat([
                {"role": "system",
                 "content": "你是拼多多商家工具里的电商顾问, 回答简洁实用, 用中文, "
                            "重点给可执行建议; 不确定就说不确定, 不要编造数据。"},
                {"role": "user", "content": intent.get("_text") or "你好"},
            ], cfg)
            if reply:
                return {"reply": reply}
        return {"reply": "这个问题需要接入模型才能回答。点上方「⚙️ 模型设置」配置 "
                         "DeepSeek 等 API Key 后即可自由问答; 当前我仍可处理:\n" + HELP_TEXT}
    return {"reply": HELP_TEXT}


if __name__ == "__main__":
    # 自测: 规则解析各意图
    for t in ["进货8运费2想每件赚3块", "帮我算保本价 进货23 运费5 净利率8%",
              "https://detail.1688.com/offer/956643085100.html?src=xxx",
              "哪些商品值得上架", "分析一下订单", "检查库存", "你能做什么"]:
        j = rule_parse(t)
        print(t[:26], "→", j["action"], j.get("params"))
