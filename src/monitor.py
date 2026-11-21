#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""定时无人值守监控: 货源价格变化 + SKU 库存预警 (v1)。

设计:
- 后台 daemon 线程按配置周期检查, 不阻塞 GUI; 结果通过 emit(kind, payload) 回主线程。
- 价格监控: 对指定 1688 链接重采(复用登录态), 与上次快照比较, 变化超阈值才告警。
  第一次运行只建立基线, 不算异常。
- 库存监控: 基于本地采集数据, 不联网, 零库存/低库存才告警。
- 报告落盘 data/monitor_report.html, 状态/快照落盘 data/monitor_state.json。
- **只在异常时通知**(emit "monitor_alert"), 正常检查只更新状态。
"""
import json
import os
import threading
import time
from datetime import datetime

try:
    from paths import ROOT, DATA
except Exception:
    from pathlib import Path
    ROOT = Path(__file__).resolve().parent.parent
    DATA = ROOT / "data"

CFG_PATH = DATA / "monitor_config.json"
STATE_PATH = DATA / "monitor_state.json"
REPORT_PATH = DATA / "monitor_report.html"

DEFAULT_CFG = {
    "enabled": False,
    "interval_min": 60,          # 检查周期(分钟)
    "price_change_pct": 5.0,     # 价格变动超过此百分比才告警
    "low_stock": 50,             # 库存低于此值告警
    "max_items": 10,             # 最多重采几个商品(重采较慢, 控制成本)
    "urls": [],                  # 显式监控链接; 为空则取当前采集数据的前 N 个
}


def load_config():
    cfg = dict(DEFAULT_CFG)
    try:
        cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8")))
    except Exception:
        pass
    return cfg


def save_config(cfg):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        CFG_PATH.write_text(json.dumps(cfg, ensure_ascii=False, indent=2),
                            encoding="utf-8")
        return True
    except Exception:
        return False


def load_state():
    try:
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return {"snapshots": {}, "last_check": "", "last_alerts": []}


def _save_state(state):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2),
                              encoding="utf-8")
    except Exception:
        pass


def _item_key(url_or_id):
    import re
    m = re.search(r"(\d{8,})", str(url_or_id))
    return m.group(1) if m else str(url_or_id)


def _price_of(d):
    """loader 兼容 dict 的到手成本 = 进货价 + 进货运费"""
    try:
        return round(float(d.get("price") or 0) + float(d.get("express_fee") or 0), 2)
    except Exception:
        return 0.0


def _build_monitor_urls(cfg, products):
    urls = cfg.get("urls") or []
    if not urls:
        for p in (products or [])[:int(cfg.get("max_items") or 10)]:
            u = getattr(p, "item_url", "") or ""
            if not u and getattr(p, "item_id", ""):
                u = f"https://detail.1688.com/offer/{p.item_id}.html"
            if u:
                urls.append(u)
    return urls[:int(cfg.get("max_items") or 10)]


def _stock_alerts(products, low_threshold):
    """本地库存检查(不联网)"""
    alerts = []
    for prod in products or []:
        stocks = [int(c.get("stock") or 0) for c in (getattr(prod, "sku_combos", None) or [])]
        total = sum(stocks)
        title = (getattr(prod, "title", "") or "")[:40]
        iid = getattr(prod, "item_id", "")
        if not stocks or total == 0:
            alerts.append({"type": "stock", "level": "high", "item": iid,
                           "title": title, "detail": "零库存/未取到库存"})
        elif total < low_threshold:
            alerts.append({"type": "stock", "level": "medium", "item": iid,
                           "title": title, "detail": f"低库存仅 {total} 件"})
    return alerts


def check_once(products=None, config=None, emit=None):
    """执行一次检查(同步, 可在后台线程调用)。返回报告 dict。"""
    cfg = config or load_config()
    state = load_state()
    now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
    alerts = []

    # ---- 1) 库存(本地, 始终检查) ----
    alerts += _stock_alerts(products or [], float(cfg.get("low_stock") or 50))

    # ---- 2) 价格(联网重采) ----
    urls = _build_monitor_urls(cfg, products)
    new_snapshots = dict(state.get("snapshots") or {})
    baseline = not bool(new_snapshots)
    price_results = []
    if urls:
        try:
            import collector
            results = collector.collect(urls, headless=True,
                                        progress=lambda k, m: None)
            for d in results or []:
                iid = str(d.get("item_id") or "")
                if not iid:
                    continue
                price = _price_of(d)
                title = str(d.get("title") or "")[:40]
                old = new_snapshots.get(iid, {}).get("price")
                new_snapshots[iid] = {"price": price, "title": title, "time": now}
                price_results.append({"item": iid, "title": title,
                                      "old": old, "new": price})
                if old and price > 0:
                    pct = (price - old) / old * 100
                    if abs(pct) >= float(cfg.get("price_change_pct") or 5):
                        alerts.append({
                            "type": "price",
                            "level": "high" if pct > 0 else "medium",
                            "item": iid, "title": title,
                            "detail": f"到手成本 {old} → {price} ({pct:+.1f}%)",
                            "pct": round(pct, 1),
                        })
        except Exception as e:
            alerts.append({"type": "error", "level": "low", "item": "",
                           "title": "重采失败", "detail": str(e)[:200]})

    report = {
        "time": now,
        "checked_urls": len(urls),
        "baseline": baseline,
        "alerts": alerts,
        "prices": price_results[:20],
    }

    # 状态落盘(即使无异常也更新检查时间/快照)
    state["snapshots"] = new_snapshots
    state["last_check"] = now
    state["last_alerts"] = alerts
    _save_state(state)
    _write_report(report)

    # 只在异常时通知(基线首轮除外, 避免首轮库存告警轰炸)
    if alerts and not baseline and emit:
        try:
            emit("monitor_alert", report)
        except Exception:
            pass
    elif baseline and emit:
        try:
            emit("monitor_info", f"监控基线已建立: {len(new_snapshots)} 个商品, "
                                 f"下次检查起开始比对价格")
        except Exception:
            pass
    return report


def _write_report(report):
    try:
        DATA.mkdir(parents=True, exist_ok=True)
        rows = []
        for a in report["alerts"]:
            color = {"high": "#eb5757", "medium": "#f2994a", "low": "#999"}.get(a["level"], "#999")
            rows.append(
                f'<tr><td style="color:{color};font-weight:bold">'
                f'{ {"price":"价格","stock":"库存","error":"错误"}.get(a["type"],a["type"]) }</td>'
                f'<td>{a.get("item","")}</td><td>{a.get("title","")}</td>'
                f'<td>{a.get("detail","")}</td></tr>')
        price_rows = "".join(
            f"<tr><td>{p['item']}</td><td>{p['title']}</td>"
            f"<td>{p['old'] if p['old'] is not None else '基线'}</td>"
            f"<td>{p['new']}</td></tr>" for p in report["prices"])
        html = f"""<!doctype html><meta charset="utf-8">
<title>监控报告 {report['time']}</title>
<body style="font-family:Microsoft YaHei;background:#f5f8fc;color:#222;padding:24px">
<h2 style="color:#2f80ed">拼多多工作台 · 定时监控报告</h2>
<p>检查时间：<b>{report['time']}</b>　|　重采链接：{report['checked_urls']}　|　异常：<b style="color:#eb5757">{len(report['alerts'])}</b></p>
<h3>异常明细</h3>
<table border="1" cellpadding="8" cellspacing="0" style="background:#fff;border-color:#dde5f0">
<tr style="background:#eef3fb"><th>类型</th><th>商品ID</th><th>标题</th><th>详情</th></tr>
{''.join(rows) if rows else '<tr><td colspan="4" style="color:#1f9d55">✅ 无异常</td></tr>'}
</table>
<h3>价格快照</h3>
<table border="1" cellpadding="8" cellspacing="0" style="background:#fff;border-color:#dde5f0">
<tr style="background:#eef3fb"><th>商品ID</th><th>标题</th><th>上次到手价</th><th>本次到手价</th></tr>
{price_rows or '<tr><td colspan="4">（无）</td></tr>'}
</table></body>"""
        REPORT_PATH.write_text(html, encoding="utf-8")
    except Exception:
        pass


class MonitorThread:
    """后台定时调度器。start() 启动, stop() 停止; 首次检查在 start 后立即执行。"""

    def __init__(self, emit, get_products):
        self.emit = emit
        self.get_products = get_products
        self._stop = threading.Event()
        self._thread = None
        self.running = False
        self.next_check = ""

    def start(self, cfg=None):
        if self.running:
            return False
        self._stop.clear()
        self._cfg = cfg or load_config()
        self.running = True
        self._thread = threading.Thread(target=self._loop, daemon=True)
        self._thread.start()
        return True

    def stop(self):
        self._stop.set()
        self.running = False

    def _loop(self):
        # 首次立即检查一次(建立基线)
        while not self._stop.is_set():
            cfg = self._cfg or load_config()
            try:
                report = check_once(self.get_products(), cfg, self.emit)
                self.next_check = datetime.fromtimestamp(
                    time.time() + int(cfg.get("interval_min") or 60) * 60
                ).strftime("%H:%M")
            except Exception as e:
                if self.emit:
                    try:
                        self.emit("monitor_info", f"监控检查出错: {e}")
                    except Exception:
                        pass
            minutes = max(1, int(cfg.get("interval_min") or 60))
            # 分段 sleep, 保证 stop() 立即生效
            for _ in range(minutes * 60):
                if self._stop.is_set():
                    return
                time.sleep(1)


if __name__ == "__main__":
    r = check_once([], load_config(), None)
    print("检查时间:", r["time"], "异常:", len(r["alerts"]),
          "重采:", r["checked_urls"])
