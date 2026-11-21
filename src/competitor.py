#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
competitor.py —— 竞品价格监控(竞品 Agent)

定位: 盯"别人卖多少钱", 并结合我方成本判断"要不要跟价"。
与 monitor.py(盯 1688 货源成本) 互补:
  - monitor.py    : 上游进货价变了没 → 成本预警
  - competitor.py : 下游竞品价变了没 → 竞争力/跟价预警

数据源(务实设计, 不臆造能爬竞品):
  1. 用户录入竞品价格台账(主): 每个我方商品登记 1~N 个竞品(名称/售价/链接/备注)。
  2. 可选抓价(辅): 竞品链接是公开商品页时, 尝试用 Playwright 抓取页面上的 ¥ 价格;
     失败/被拦时保留手动价, 不阻断 —— 强烈反爬的对手一律走手动。

核心能力(区别于普通比价工具):
  - 价差: 我方售价 vs 竞品最低价/均价, 算贵了/便宜了百分比。
  - **跟价可行性**: 用 pricing.compute_profit 反算"降到竞品价还赚不赚钱",
    低于保本线就明确劝退 —— 这是"懂生意"的部分。
  - 只在异常时通知: 竞品价变动超阈值、竞品价击穿我方保本线、我方价格失去优势。
  - 落盘 data/competitors.json(台账) + data/competitor_report.html(报告)。
"""
import json
import re
import threading
import time
from dataclasses import dataclass, asdict, field
from datetime import datetime

try:
    from paths import ROOT, DATA
except Exception:
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    DATA = ROOT / "data"

TABLE_PATH = DATA / "competitors.json"
STATE_PATH = DATA / "competitor_state.json"
REPORT_PATH = DATA / "competitor_report.html"

DEFAULT_CFG = {
    "enabled": False,
    "interval_min": 120,         # 复查周期(分钟)
    "price_change_pct": 5.0,     # 竞品价变动超过此百分比才告警
    "advantage_pct": 3.0,        # 我方比竞品最便宜价高出超过此值 → 价格劣势告警
    "auto_fetch": False,         # 是否尝试自动抓价(默认关, 手动价为主)
}


@dataclass
class Watch:
    """一个我方商品 + 它的竞品集合"""
    key: str = ""                       # 唯一键: 我方商品ID或自定义
    my_name: str = ""                   # 我方商品名
    my_price: float = 0.0               # 我方当前售价(拼多多)
    cost_price: float = 0.0             # 进货价(1688)
    inbound: float = 0.0                # 进货运费
    outbound: float = 0.0               # 发货运费(卖家承担)
    commission_rate: float = 0.02
    payment_fee_rate: float = 0.006
    competitors: list = field(default_factory=list)  # [{name, price, url, note, time}]
    updated: str = ""

    def to_dict(self):
        return asdict(self)


def _now():
    return datetime.now().strftime("%Y-%m-%d %H:%M:%S")


def load_table() -> list:
    try:
        raw = json.loads(TABLE_PATH.read_text(encoding="utf-8"))
        return [Watch(**w) for w in (raw.get("watches") or [])]
    except Exception:
        return []


def save_table(watches: list) -> bool:
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        TABLE_PATH.write_text(
            json.dumps({"watches": [w.to_dict() for w in watches]},
                       ensure_ascii=False, indent=2), encoding="utf-8")
        return True
    except Exception:
        return False


def load_state() -> dict:
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"snapshots": {}, "last_check": ""}


def _save_state(state: dict):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def _snap_key(w: Watch, c: dict) -> str:
    return f"{w.key}::{c.get('name') or c.get('url') or '?'}::{(c.get('url') or '')[:60]}"


def _cmp_prices(w: Watch) -> list:
    vals = []
    for c in w.competitors or []:
        try:
            p = float(c.get("price") or 0)
        except (TypeError, ValueError):
            p = 0.0
        if p > 0:
            vals.append(p)
    return vals


def breakeven_price(w: Watch) -> float:
    """保本售价: 净利=0 时的价格 = 成本合计 / (1 - 佣金率 - 支付费率)"""
    cost = float(w.cost_price or 0) + float(w.inbound or 0) + float(w.outbound or 0)
    denom = 1.0 - float(w.commission_rate or 0) - float(w.payment_fee_rate or 0)
    return round(cost / denom, 2) if denom > 0 else 0.0


def analyze(w: Watch, target_margin: float = 0.0) -> dict:
    """单个我方商品的竞品分析: 价差 + 竞争力 + 跟价可行性。"""
    import pricing
    prices = _cmp_prices(w)
    be = breakeven_price(w)
    my = float(w.my_price or 0)
    out = {
        "key": w.key, "my_name": w.my_name[:40], "my_price": my,
        "breakeven": be, "competitors": len(w.competitors or []),
    }
    if not prices:
        out.update({"lowest": 0.0, "avg": 0.0, "gap_pct": 0.0,
                    "level": "无数据", "follow": "未录入竞品售价, 无法比价",
                    "advice": "先在台账里给这个商品登记竞品价格"})
        return out

    lowest, avg = min(prices), round(sum(prices) / len(prices), 2)
    gap_pct = round((my - lowest) / lowest * 100, 1) if lowest > 0 else 0.0
    out.update({"lowest": lowest, "avg": avg, "gap_pct": gap_pct})

    # 竞争力分级
    if gap_pct > 15:
        out["level"] = "严重劣势"
    elif gap_pct > 5:
        out["level"] = "价格偏高"
    elif gap_pct >= -3:
        out["level"] = "持平"
    else:
        out["level"] = "有优势"

    # 跟价可行性: 降到竞品最低价还赚不赚钱
    net_if_follow = None
    try:
        b = pricing.compute_profit(
            w.cost_price, w.inbound, lowest, w.outbound,
            commission_rate=w.commission_rate, payment_fee_rate=w.payment_fee_rate)
        net_if_follow = b.net_profit
    except Exception:
        pass
    out["net_if_follow"] = net_if_follow

    if lowest < be:
        out["follow"] = "不可跟价(低于保本)"
        out["advice"] = (f"竞品 ¥{lowest} 已低于我方保本 ¥{be}, 跟价必亏; "
                         "应突出差异化/服务, 或压上游进货价")
    elif net_if_follow is not None and target_margin and lowest > 0:
        margin_if = net_if_follow / lowest
        if margin_if < target_margin:
            out["follow"] = "跟价后利润不足"
            out["advice"] = (f"跟到 ¥{lowest} 净利仅 ¥{net_if_follow} "
                             f"({margin_if*100:.1f}%), 低于目标 {target_margin*100:.0f}%; 谨慎")
        else:
            out["follow"] = "可跟价"
            out["advice"] = f"跟到 ¥{lowest} 仍有净利 ¥{net_if_follow}, 可考虑"
    else:
        out["follow"] = "可跟价" if lowest >= be else "不可跟价"
        out["advice"] = (f"竞品最低 ¥{lowest}, 我方 ¥{my} "
                         + (f"高 {gap_pct}%" if gap_pct > 0 else f"低 {abs(gap_pct)}%"))

    if out["level"] in ("严重劣势", "价格偏高") and lowest >= be:
        out["advice"] += "; 我方价格明显偏高且仍有利润空间, 建议下调或促销"
    return out


def analyze_all(watches: list = None, target_margin: float = 0.0) -> dict:
    watches = watches if watches is not None else load_table()
    rows = [analyze(w, target_margin) for w in watches]
    rows.sort(key=lambda r: (r["level"] != "严重劣势", -(r.get("gap_pct") or 0)))
    return {"rows": rows, "count": len(rows)}


def _extract_price(text: str) -> float:
    """从页面文本里抽最像售价的 ¥ 数字(保守: 取出现在价格上下文的)。"""
    if not text:
        return 0.0
    # 优先带 价格/售价/券后/到手 等上下文
    for pat in (r"(?:券后|到手|售价|价格|活动价)[^¥￥\d]{0,12}[¥￥]\s*([\d.]+)",
                r"[¥￥]\s*([\d]+\.?\d*)"):
        m = re.search(pat, text)
        if m:
            try:
                return round(float(m.group(1)), 2)
            except ValueError:
                pass
    return 0.0


def fetch_price(url: str, timeout_ms: int = 20000) -> tuple:
    """(可选)尝试抓公开商品页售价。失败返回 (0.0, 原因)。不抛异常。"""
    if not url or "http" not in str(url):
        return 0.0, "无效链接"
    try:
        from playwright.sync_api import sync_playwright
        with sync_playwright() as pw:
            browser = pw.chromium.launch(headless=True,
                                         args=["--no-sandbox",
                                               "--disable-blink-features=AutomationControlled"])
            page = browser.new_page(locale="zh-CN")
            page.goto(url, wait_until="domcontentloaded", timeout=timeout_ms)
            try:
                page.wait_for_timeout(1500)
            except Exception:
                pass
            text = page.locator("body").inner_text(timeout=5000)[:20000]
            browser.close()
        price = _extract_price(text)
        return (price, "抓取成功") if price > 0 else (0.0, "未识别到价格(可能需登录/反爬)")
    except Exception as e:
        return 0.0, f"抓取失败: {str(e)[:80]}"


def refresh_prices(watches: list = None, only_key: str = "") -> dict:
    """尝试对开启了自动抓价的竞品刷新售价(需 watch 里 url 存在)。返回更新统计。"""
    watches = watches if watches is not None else load_table()
    updated, failed, skipped = 0, 0, 0
    for w in watches:
        if only_key and w.key != only_key:
            continue
        for c in w.competitors or []:
            url = c.get("url") or ""
            if not url:
                skipped += 1
                continue
            price, msg = fetch_price(url)
            if price > 0:
                c["price"] = price
                c["time"] = _now()
                c["source"] = "auto"
                updated += 1
            else:
                failed += 1
                c["last_error"] = msg
        w.updated = _now()
    save_table(watches)
    return {"updated": updated, "failed": failed, "skipped": skipped}


def check_once(watches: list = None, config: dict = None, emit=None,
               target_margin: float = 0.0) -> dict:
    """一次检查: 比对快照, 只在异常时通知。可在后台线程调用。"""
    cfg = config or dict(DEFAULT_CFG)
    watches = watches if watches is not None else load_table()
    state = load_state()
    snaps = dict(state.get("snapshots") or {})
    now = _now()
    alerts = []

    for w in watches:
        an = analyze(w, target_margin)
        # 1) 竞争力告警
        if an["level"] in ("严重劣势", "价格偏高") and an.get("lowest"):
            alerts.append({
                "type": "advantage", "level": "high" if an["level"] == "严重劣势" else "medium",
                "key": w.key, "title": w.my_name[:40],
                "detail": f"我方 ¥{an['my_price']} 比竞品最低 ¥{an['lowest']} 高 {an['gap_pct']}% "
                          f"({an['level']})",
            })
        # 2) 保本线告警
        if an["lowest"] and an["lowest"] < an["breakeven"]:
            alerts.append({
                "type": "breakeven", "level": "high", "key": w.key,
                "title": w.my_name[:40],
                "detail": f"竞品 ¥{an['lowest']} 已低于我方保本 ¥{an['breakeven']}, 跟价必亏",
            })
        # 3) 竞品价变动(对比上次快照)
        for c in w.competitors or []:
            k = _snap_key(w, c)
            try:
                cur = float(c.get("price") or 0)
            except (TypeError, ValueError):
                cur = 0.0
            old = snaps.get(k, {}).get("price")
            snaps[k] = {"price": cur, "name": c.get("name", ""), "time": now}
            if old and cur > 0:
                pct = (cur - old) / old * 100
                if abs(pct) >= float(cfg.get("price_change_pct") or 5):
                    alerts.append({
                        "type": "competitor_price",
                        "level": "high" if pct < 0 else "medium",
                        "key": w.key, "title": (c.get("name") or "竞品")[:40],
                        "detail": f"竞品价 {old} → {cur} ({pct:+.1f}%)",
                    })

    baseline = not bool(state.get("snapshots"))
    report = {
        "time": now, "watches": len(watches), "baseline": baseline,
        "alerts": alerts, "analysis": analyze_all(watches, target_margin)["rows"],
    }
    state["snapshots"] = snaps
    state["last_check"] = now
    state["last_alerts"] = alerts
    _save_state(state)
    _write_report(report)

    if emit:
        try:
            if alerts and not baseline:
                emit("competitor_alert", report)
            elif baseline:
                emit("competitor_info", f"竞品基线已建立: {len(watches)} 个商品, "
                                         f"下次检查起开始比对")
        except Exception:
            pass
    return report


def _write_report(report: dict):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        color = {"high": "#eb5757", "medium": "#f2994a", "low": "#999"}
        rows = "".join(
            f'<tr><td style="color:{color.get(a["level"],"#999")};font-weight:bold">'
            f'{a["type"]}</td><td>{a.get("title","")}</td><td>{a.get("detail","")}</td></tr>'
            for a in report["alerts"])
        arows = "".join(
            f"<tr><td>{r['my_name']}</td><td>¥{r['my_price']}</td>"
            f"<td>¥{r.get('lowest') or '-'}</td><td>{r.get('gap_pct') or 0}%</td>"
            f"<td>{r['level']}</td><td>{r.get('follow','')}</td>"
            f"<td>{r.get('advice','')}</td></tr>" for r in report["analysis"])
        html = f"""<!doctype html><meta charset="utf-8">
<title>竞品监控报告 {report['time']}</title>
<body style="font-family:Microsoft YaHei;background:#f5f8fc;color:#222;padding:24px">
<h2 style="color:#2f80ed">拼多多工作台 · 竞品价格监控报告</h2>
<p>检查时间：<b>{report['time']}</b>　|　监控商品：{report['watches']}　|　异常：<b style="color:#eb5757">{len(report['alerts'])}</b></p>
<h3>异常明细</h3>
<table border="1" cellpadding="8" cellspacing="0" style="background:#fff;border-color:#dde5f0">
<tr style="background:#eef3fb"><th>类型</th><th>商品/竞品</th><th>详情</th></tr>
{rows or '<tr><td colspan="3" style="color:#1f9d55">✅ 无异常</td></tr>'}</table>
<h3>比价与跟价建议</h3>
<table border="1" cellpadding="8" cellspacing="0" style="background:#fff;border-color:#dde5f0">
<tr style="background:#eef3fb"><th>我方商品</th><th>我方价</th><th>竞品最低</th><th>价差</th>
<th>竞争力</th><th>跟价</th><th>建议</th></tr>
{arows or '<tr><td colspan="7">（暂无台账）</td></tr>'}</table></body>"""
        REPORT_PATH.write_text(html, encoding="utf-8")
    except Exception:
        pass


def render_report(result: dict = None) -> str:
    """给 AI 助手用的中文竞品报告。兼容 analyze_all() 的 rows 与 check_once() 的 analysis。"""
    result = result or {}
    rows = result.get("analysis") or result.get("rows") or analyze_all()["rows"]
    if not rows:
        return ("还没有竞品台账。请先在「🎯 竞品监控」页添加我方商品和竞品售价，"
                "或直接说「帮我监控这个商品，我方卖X元，竞品卖Y元」。")
    lines = ["🎯 竞品价格监控", ""]
    for r in rows[:10]:
        lines.append(f"· {r['my_name']}: 我方 ¥{r['my_price']} | "
                     f"竞品最低 ¥{r.get('lowest') or '-'} | {r['level']}"
                     + (f" (高{r['gap_pct']}%)" if r.get('gap_pct') else ""))
        lines.append(f"    保本 ¥{r['breakeven']} · {r['follow']} → {r['advice']}")
    if result.get("alerts"):
        lines.append("")
        lines.append(f"⚠️ 本次异常 {len(result['alerts'])} 项:")
        for a in result["alerts"][:5]:
            lines.append(f"  - {a.get('title','')}: {a.get('detail','')}")
    return "\n".join(lines)


class CompetitorThread:
    """后台定时复查调度器(与 monitor.MonitorThread 同范式)。"""

    def __init__(self, emit, get_watches):
        self.emit = emit
        self.get_watches = get_watches
        self._stop = threading.Event()
        self._thread = None
        self.running = False
        self.next_check = ""
        self._cfg = None

    def start(self, cfg=None):
        if self.running:
            return False
        self._stop.clear()
        self._cfg = cfg or dict(DEFAULT_CFG)
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self.running = False

    def _loop(self):
        while not self._stop.is_set():
            cfg = self._cfg or dict(DEFAULT_CFG)
            try:
                check_once(self.get_watches(), cfg, self.emit)
                self.next_check = datetime.fromtimestamp(
                    time.time() + int(cfg.get("interval_min") or 120) * 60
                ).strftime("%H:%M")
            except Exception as e:
                if self.emit:
                    try:
                        self.emit("competitor_info", f"竞品检查出错: {e}")
                    except Exception:
                        pass
            minutes = max(1, int(cfg.get("interval_min") or 120))
            for _ in range(minutes * 60):
                if self._stop.is_set():
                    return
                time.sleep(1)


if __name__ == "__main__":
    w = Watch(key="demo-1", my_name="示例商品", my_price=39.9,
              cost_price=12, inbound=2, outbound=3,
              competitors=[{"name": "对手A", "price": 32.9, "url": "", "note": "示例"}])
    print(analyze(w, 0.08))
    print("-" * 50)
    print(render_report({"analysis": analyze_all([w])["rows"]}))
