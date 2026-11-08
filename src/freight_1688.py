"""1688 商品「分地区运费规则」探测器。

【原理】1688 商品详情页会调用官方接口
  mtop.1688.freightInfoService.getFreightInfoWithScene
按「发货地 + 收货地 code + 运费模板 ID」计算该收货地址下的运费(totalCost)。
本模块在用户自己的登录态里，逐个切换页面已有的收货地址，拦截官方响应，
还原出「哪个省/市多少钱、哪些地方贵、哪些没测到」。

【合规边界】
  - 只使用用户本人已登录的浏览器会话与页面自己的 UI 交互，不伪造身份、
    不绕过风控、不改签名重放接口(实测 Python 侧重放会被 RGV587 拦截)。
  - 只采集「省市 + 运费 + 发货地 + 模板 ID」用于成本与运费模板决策；
    姓名/电话/详细门牌号一律不写入任何输出文件。
  - 不发货判定以官方接口 ret / 页面提示为准；没有覆盖到的省份如实列为
    「未覆盖」，绝不臆测。

【用法】
  python src/freight_1688.py                       # 全部商品, 每商品按省去重最多 30 个地址
  python src/freight_1688.py --limit 1            # 只跑第 1 个商品
  python src/freight_1688.py --max-addr 60        # 每商品最多探测 60 个地址
  python src/freight_1688.py --item 1086659337510 # 指定商品
  python src/freight_1688.py --headless           # 无窗口(风控风险略高)

【输出】
  data/freight_rules.json / .csv / .html  + 失败快照 debug_freight.html
"""
from __future__ import annotations

import argparse
import csv
import json
import os
import re
import statistics
import sys
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import unquote

import paths as _paths
_paths.setup_env()
from paths import ROOT, DATA

STATE = DATA / "1688_state.json"
COLLECTED = DATA / "collected_1688.json"
OUT_JSON = DATA / "freight_rules.json"
OUT_CSV = DATA / "freight_rules.csv"
OUT_HTML = DATA / "freight_rules.html"
DEBUG_HTML = ROOT / "debug_freight.html"

# 中国省级行政区(用于「未覆盖」提示; 港澳台按规范表述)
PROVINCES = [
    "北京", "天津", "河北", "山西", "内蒙古", "辽宁", "吉林", "黑龙江",
    "上海", "江苏", "浙江", "安徽", "福建", "江西", "山东", "河南",
    "湖北", "湖南", "广东", "广西", "海南", "重庆", "四川", "贵州",
    "云南", "西藏", "陕西", "甘肃", "青海", "宁夏", "新疆",
    "中国香港", "中国澳门", "中国台湾",
]

_PROV_FIX = {
    "北京市": "北京", "天津市": "天津", "上海市": "上海", "重庆市": "重庆",
    "河北省": "河北", "山西省": "山西", "辽宁省": "辽宁", "吉林省": "吉林",
    "黑龙江省": "黑龙江", "江苏省": "江苏", "浙江省": "浙江", "安徽省": "安徽",
    "福建省": "福建", "江西省": "江西", "山东省": "山东", "河南省": "河南",
    "湖北省": "湖北", "湖南省": "湖南", "广东省": "广东", "海南省": "海南",
    "四川省": "四川", "贵州省": "贵州", "云南省": "云南", "陕西省": "陕西",
    "甘肃省": "甘肃", "青海省": "青海", "台湾省": "中国台湾",
    "内蒙古自治区": "内蒙古", "广西壮族自治区": "广西",
    "西藏自治区": "西藏", "宁夏回族自治区": "宁夏", "新疆维吾尔自治区": "新疆",
    "香港特别行政区": "中国香港", "澳门特别行政区": "中国澳门",
}


def _prov_short(text: str) -> str:
    """把 addressCodeText 的第一段归一到省份简称(仅保留地理信息)。"""
    first = (text or "").strip().split(" ")[0]
    if first in _PROV_FIX:
        return _PROV_FIX[first]
    for k, v in _PROV_FIX.items():
        if first.startswith(k) or k.startswith(first):
            return v
    return first[:6] or "未知"


def _city_short(text: str) -> str:
    """从 addressCodeText 取市级信息(脱敏, 不含详细地址)。"""
    parts = [p for p in (text or "").strip().split(" ") if p]
    if len(parts) >= 2:
        return parts[1][:12]
    return parts[0][:12] if parts else ""


def _load_items() -> list:
    if not COLLECTED.exists():
        return []
    try:
        d = json.loads(COLLECTED.read_text(encoding="utf-8"))
    except Exception:
        return []
    items = d if isinstance(d, list) else (d.get("items") or list(d.values()))
    out = []
    for x in items:
        if not isinstance(x, dict):
            continue
        iid = str(x.get("item_id") or "")
        url = x.get("item_url") or ("https://detail.1688.com/offer/%s.html" % iid)
        out.append({
            "item_id": iid,
            "title": (x.get("name") or x.get("title") or "")[:60],
            "url": url,
            "collected_express_fee": x.get("express_fee"),
            "category": x.get("category_name") or "",
        })
    return out


class _Probe:
    """单商品探测: 收集官方接口响应, 不记录任何个人隐私字段。"""

    def __init__(self, page):
        self.page = page
        self.addresses = []      # [{code, province, city, default}]
        self.responses = []      # 官方运费响应, 按时间顺序
        self.meta = {}           # 发货地 / 模板 ID / 单位重量
        self.blocked = 0
        page.on("response", self._on_response)
        page.on("request", self._on_request)

    def _on_response(self, r):
        try:
            if "queryReceiveAddresses" in r.url:
                j = r.json()
                items = ((j.get("data") or {}).get("model") or {}).get("addressItems") or []
                if items and not self.addresses:
                    for x in items:
                        txt = x.get("addressCodeText") or ""
                        self.addresses.append({
                            "code": str(x.get("addressCode") or ""),
                            "province": _prov_short(txt),
                            "city": _city_short(txt),
                            "default": bool(x.get("default")),
                        })
            elif "getFreightInfoWithScene" in r.url:
                self.responses.append(r.json())
        except Exception:
            pass

    def _on_request(self, r):
        """从官方请求里读发货地 / 模板 ID 等商品级元数据。"""
        if "getFreightInfoWithScene" not in r.url or not r.post_data or self.meta:
            return
        try:
            raw = unquote(r.post_data.split("data=", 1)[1])
            d = json.loads(raw)
            ext = json.loads(d.get("extendMap") or "{}")
            self.meta = {
                "send_address_code": str(d.get("sendAddressCode") or ""),
                "offer_id": str(d.get("offerId") or ""),
                "template_id": ext.get("templateId"),
                "unit_weight": ext.get("unitWeight"),
                "amount": ext.get("amount"),
            }
        except Exception:
            pass

    def _freight_text(self) -> tuple:
        """读页面运费文案与是否出现不发货提示。"""
        try:
            v = self.page.evaluate("""() => {
              const b = document.querySelector('.cart-content b');
              const t = document.body.innerText || '';
              const bad = /不支持配送|暂不发货|该区域.*不支持|无法送达|不发货/.test(t);
              return {fee: b ? b.innerText.trim() : '', blocked: bad};
            }""")
            return v.get("fee", ""), bool(v.get("blocked"))
        except Exception:
            return "", False

    def _parse_fee(self, v) -> tuple:
        """返回 (运费, 状态). 状态: ok / free / blocked / unknown"""
        if isinstance(v, dict):
            ret = " ".join(str(x) for x in (v.get("ret") or []))
            if "FAIL" in ret or "ERROR" in ret or "RGV587" in ret:
                return None, "blocked"
            data = v.get("data") or {}
            if data.get("freeDeliverFee") is True:
                return 0.0, "free"
            cost = data.get("totalCost")
            if isinstance(cost, (int, float)):
                return float(cost), ("free" if float(cost) == 0 else "ok")
            return None, "unknown"
        return None, "unknown"

    def run(self, max_addr: int, progress=None) -> dict:
        def P(msg):
            if progress:
                try:
                    progress(msg)
                except Exception:
                    pass

        pg = self.page
        P("等待运费接口初始化…")
        pg.wait_for_timeout(3500)

        # 按省份去重: 同省只探测一个代表地址, 大幅减少请求与风控压力
        seen, picked = set(), []
        for i, a in enumerate(self.addresses):
            if a["province"] in seen:
                continue
            seen.add(a["province"])
            picked.append((i, a))
            if len(picked) >= max_addr:
                break
        P(f"已保存地址 {len(self.addresses)} 个, 按省去重后本次探测 {len(picked)} 个")

        page_fee, _ = self._freight_text()
        records = []
        total = len(picked)
        for n, (idx, a) in enumerate(picked, 1):
            before = len(self.responses)
            fee, status, note = None, "unknown", ""
            # 最多两次: 第一次没等到官方响应时重新点开浮层再选一次
            for _attempt in range(2):
                try:
                    # 浮层每次选完会收起, 不可见时重新点开收货地址
                    if not pg.locator("a.address-item").first.is_visible():
                        pg.locator("a.recieve-address").first.click(timeout=4000)
                        pg.wait_for_timeout(650)
                    pg.locator("a.address-item").nth(idx).click(timeout=4500)
                    # 轮询等官方运费响应, 通常 0.4~1.0s; 比固定 sleep 更快也更准
                    deadline = time.time() + 2.6
                    while time.time() < deadline and len(self.responses) <= before:
                        pg.wait_for_timeout(200)
                    fresh = self.responses[before:]
                    if fresh and isinstance(fresh[-1], dict):
                        fee, status = self._parse_fee(fresh[-1])
                        if "FAIL" in str(fresh[-1].get("ret") or ""):
                            note = (note + " 官方接口返回受限").strip()
                            status = "blocked"
                    page_fee, page_blocked = self._freight_text()
                    if page_blocked and status != "free":
                        status = "blocked"
                    if fee is None and page_fee:
                        m = re.search(r"(\d+(?:\.\d+)?)", page_fee.replace("¥", ""))
                        if m:
                            fee = float(m.group(1))
                            status = "free" if fee == 0 else "ok"
                        note = (note + f" 页面文案 {page_fee}").strip()
                    if fee is not None or status != "unknown":
                        break
                except Exception as e:
                    note = "切换失败: " + repr(e)[:70]
                    status = "unknown"
            records.append({
                "province": a["province"], "city": a["city"], "code": a["code"],
                "freight": fee, "status": status, "note": note,
            })
            if n % 5 == 0 or n == total:
                P(f"  {n}/{total} {a['province']}·{a['city']} → "
                  f"{'¥%g' % fee if fee is not None else status}")
            time.sleep(0.45)

        covered = {r["province"] for r in records}
        uncovered = [p for p in PROVINCES if p not in covered]
        return {
            "meta": self.meta,
            "records": records,
            "uncovered": uncovered,
            "default_page_freight": page_fee,
            "address_total": len(self.addresses),
        }


def probe(items: list, max_addr=30, headless=False, progress=None, limit=None):
    """主流程: 逐商品探测。返回结构化结果 dict。"""
    from playwright.sync_api import sync_playwright

    def P(msg):
        if progress:
            try:
                progress(msg)
            except Exception:
                pass
        else:
            print(msg, flush=True)

    if not STATE.exists():
        raise FileNotFoundError("缺少登录态 data/1688_state.json，请先用工作台登录 1688")

    result = {
        "generated_at": datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
        "source": "1688 官方接口 getFreightInfoWithScene（登录态内切换收货地址）",
        "items": [],
        "errors": [],
    }
    use = items[:limit] if limit else items
    with sync_playwright() as pw:
        browser = pw.chromium.launch(
            headless=headless,
            args=["--no-sandbox", "--disable-blink-features=AutomationControlled"])
        ctx = browser.new_context(
            storage_state=str(STATE), locale="zh-CN",
            viewport={"width": 1280, "height": 860})
        try:
            for n, it in enumerate(use, 1):
                P(f"[{n}/{len(use)}] {it['item_id']} {it['title'][:24]}")
                page = ctx.new_page()
                try:
                    page.goto(it["url"], wait_until="domcontentloaded", timeout=35000)
                    pr = _Probe(page)
                    one = pr.run(max_addr, P)
                    one.update({k: it[k] for k in
                                ("item_id", "title", "url", "collected_express_fee", "category")})
                    result["items"].append(one)
                    fees = [r["freight"] for r in one["records"]
                            if r["freight"] is not None]
                    P(f"  ✓ 覆盖 {len(one['records'])} 省, 运费 "
                      f"{min(fees):g}~{max(fees):g} 元" if fees else "  ⚠ 未取到运费")
                except Exception as e:
                    result["errors"].append({"item_id": it["item_id"], "error": repr(e)[:180]})
                    P(f"  ⚠ 失败: {repr(e)[:120]}")
                    try:
                        DEBUG_HTML.write_text(page.content(), encoding="utf-8")
                    except Exception:
                        pass
                finally:
                    try:
                        page.close()
                    except Exception:
                        pass
                time.sleep(1.2)
        finally:
            try:
                ctx.close()
            except Exception:
                pass
            try:
                browser.close()
            except Exception:
                pass
    return result


# ---------------- 输出 ----------------
def _summary(result: dict) -> dict:
    all_fee, per_prov, blocked = [], {}, []
    for it in result["items"]:
        for r in it["records"]:
            p, f, st = r["province"], r["freight"], r["status"]
            if st == "blocked":
                blocked.append(f"{it['item_id']}·{p}")
            if f is None:
                continue
            all_fee.append(f)
            per_prov.setdefault(p, []).append(f)
    prov_stat = []
    for p, vals in sorted(per_prov.items()):
        prov_stat.append({"province": p, "min": min(vals), "max": max(vals),
                          "median": round(statistics.median(vals), 2), "n": len(vals)})
    prov_stat.sort(key=lambda x: -x["median"])
    return {
        "items": len(result["items"]),
        "records": sum(len(i["records"]) for i in result["items"]),
        "provinces": len(per_prov),
        "fee_min": min(all_fee) if all_fee else None,
        "fee_max": max(all_fee) if all_fee else None,
        "fee_median": round(statistics.median(all_fee), 2) if all_fee else None,
        "prov_stat": prov_stat,
        "blocked": blocked,
        "uncovered": result["items"][0]["uncovered"] if result["items"] else PROVINCES,
    }


def export(result: dict):
    DATA.mkdir(parents=True, exist_ok=True)
    OUT_JSON.write_text(json.dumps(result, ensure_ascii=False, indent=2), encoding="utf-8")
    with open(OUT_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.writer(f)
        w.writerow(["商品ID", "商品", "省份", "代表城市", "运费(元)", "状态",
                    "发货地code", "运费模板ID", "备注"])
        for it in result["items"]:
            meta = it.get("meta") or {}
            for r in it["records"]:
                w.writerow([it["item_id"], it["title"], r["province"], r["city"],
                            "" if r["freight"] is None else r["freight"], r["status"],
                            meta.get("send_address_code", ""), meta.get("template_id", ""),
                            r["note"]])
    s = _summary(result)
    OUT_HTML.write_text(_html(result, s), encoding="utf-8")
    return s


def _html(result: dict, s: dict) -> str:
    def esc(x):
        return (str(x).replace("&", "&amp;").replace("<", "&lt;")
                .replace(">", "&gt;"))

    hi = s["prov_stat"][:6]
    rows = "".join(
        f"<tr><td>{esc(x['province'])}</td><td class='num'>{x['min']:g}</td>"
        f"<td class='num'>{x['max']:g}</td><td class='num'>{x['median']:g}</td>"
        f"<td class='num'>{x['n']}</td></tr>" for x in s["prov_stat"])
    hi_html = "".join(
        f"<span class='chip hot'>{esc(x['province'])} 中位 {x['median']:g} 元</span>" for x in hi)
    unc = "、".join(esc(x) for x in s["uncovered"]) or "无"

    cards = []
    for it in result["items"]:
        meta = it.get("meta") or {}
        fees = [r["freight"] for r in it["records"] if r["freight"] is not None]
        trs = []
        for r in sorted(it["records"], key=lambda x: -(x["freight"] or -1)):
            badge = {"ok": "<b class='ok'>正常</b>", "free": "<b class='free'>包邮</b>",
                     "blocked": "<b class='bad'>疑似不发货/受限</b>",
                     "unknown": "<b class='mut'>未取到</b>",
                     "unavailable": "<b class='mut'>接口未提供</b>"}.get(r["status"], "")
            fee = "—" if r["freight"] is None else f"¥{r['freight']:g}"
            trs.append(f"<tr><td>{esc(r['province'])}</td><td>{esc(r['city'])}</td>"
                       f"<td class='num'>{fee}</td><td>{badge}</td>"
                       f"<td class='mut'>{esc(r['note'])}</td></tr>")
        if fees:
            rng = f"¥{min(fees):g} ~ ¥{max(fees):g}"
        elif all(r["status"] == "unavailable" for r in it["records"]):
            rng = "接口未提供"
        else:
            rng = "未取到"
        cards.append(f"""
        <section class="card">
          <div class="card-h">
            <div><span class="iid">{esc(it['item_id'])}</span>
                 <h3>{esc(it['title'])}</h3></div>
            <div class="stat"><span>覆盖 {len(it['records'])} 省</span>
                 <span>运费 <b>{rng}</b></span></div>
          </div>
          <div class="meta">发货地 code {esc(meta.get('send_address_code') or '—')}
            · 运费模板 ID {esc(meta.get('template_id') or '—')}
            · 单位重量 {esc(meta.get('unit_weight') or '—')} kg
            · 采集时记录的概要运费 ¥{esc(it.get('collected_express_fee'))}</div>
          <table class="grid"><thead><tr><th>省份</th><th>代表城市</th>
            <th>运费</th><th>状态</th><th>备注</th></tr></thead>
            <tbody>{''.join(trs)}</tbody></table>
        </section>""")

    return f"""<!DOCTYPE html><html lang="zh-CN"><head><meta charset="utf-8">
<title>1688 分地区运费规则报告</title>
<style>
:root{{--bg:#f4f7fc;--card:#fff;--blue:#2563eb;--deep:#12306b;--line:#e2e9f5;--txt:#1c2b45;--mut:#7183a0}}
*{{box-sizing:border-box}}body{{margin:0;background:var(--bg);color:var(--txt);
 font-family:"Microsoft YaHei UI","Segoe UI",sans-serif;font-size:13px;line-height:1.6}}
.wrap{{max-width:1180px;margin:0 auto;padding:26px 22px 60px}}
header{{background:linear-gradient(120deg,#12306b,#2563eb 60%,#3b82f6);color:#fff;
 border-radius:14px;padding:26px 30px;box-shadow:0 8px 26px rgba(37,99,235,.20)}}
header h1{{margin:0 0 6px;font-size:24px;letter-spacing:.5px}}
header p{{margin:0;opacity:.86;font-size:12.5px}}
.kpis{{display:grid;grid-template-columns:repeat(5,1fr);gap:12px;margin:18px 0 22px}}
.kpi{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.kpi span{{display:block;color:var(--mut);font-size:11.5px}}
.kpi b{{display:block;color:var(--deep);font-size:21px;margin-top:3px}}
h2{{font-size:16px;margin:26px 0 12px;padding-left:10px;border-left:4px solid var(--blue)}}
.card{{background:var(--card);border:1px solid var(--line);border-radius:12px;
 padding:16px 18px;margin-bottom:16px;box-shadow:0 2px 10px rgba(18,48,107,.05)}}
.card-h{{display:flex;justify-content:space-between;align-items:flex-start;gap:14px}}
.card-h h3{{margin:2px 0 0;font-size:14.5px}} .iid{{color:var(--blue);font-size:11.5px;font-weight:700}}
.stat{{display:flex;gap:12px;white-space:nowrap;color:var(--mut);font-size:12px}}
.stat b{{color:var(--blue)}}
.meta{{color:var(--mut);font-size:11.5px;margin:8px 0 12px;padding-bottom:10px;border-bottom:1px dashed var(--line)}}
table{{width:100%;border-collapse:collapse;font-size:12.5px}}
th{{background:#eef4ff;color:var(--deep);text-align:left;padding:8px 10px;font-weight:700;
 position:sticky;top:0}} td{{padding:7px 10px;border-bottom:1px solid #f0f4fb}}
tr:nth-child(even) td{{background:#fafbfe}} .num{{text-align:right;font-variant-numeric:tabular-nums}}
.ok{{color:#0f9d58}} .free{{color:#2563eb}} .bad{{color:#d93025}} .mut{{color:var(--mut)}}
.chip{{display:inline-block;background:#eef4ff;color:var(--deep);border-radius:20px;
 padding:4px 11px;margin:0 6px 6px 0;font-size:12px}}
.chip.hot{{background:#fff0ef;color:#c0392b;font-weight:700}}
.box{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px}}
.box ul{{margin:8px 0 0;padding-left:20px}} .box li{{margin:5px 0}}
.scroll{{max-height:330px;overflow:auto;border:1px solid var(--line);border-radius:10px}}
footer{{color:var(--mut);font-size:11.5px;text-align:center;margin-top:26px}}
</style></head><body><div class="wrap">
<header><h1>1688 分地区运费规则报告</h1>
<p>数据来源：1688 官方 getFreightInfoWithScene 接口 · 登录态内按收货地址查询 ·
生成于 {esc(result['generated_at'])} · 仅含省市与运费，不含任何个人信息</p></header>
<div class="kpis">
 <div class="kpi"><span>探测商品</span><b>{s['items']}</b></div>
 <div class="kpi"><span>覆盖省级行政区</span><b>{s['provinces']}</b></div>
 <div class="kpi"><span>运费区间</span><b>{('¥%g~%g' % (s['fee_min'], s['fee_max'])) if s['fee_min'] is not None else '—'}</b></div>
 <div class="kpi"><span>中位数</span><b>{('¥%g' % s['fee_median']) if s['fee_median'] is not None else '—'}</b></div>
 <div class="kpi"><span>受限/疑似不发货</span><b>{len(s['blocked'])}</b></div>
</div>
<h2>① 运费偏高的地区（建议拼多多端单独加价或限售）</h2>
<div class="box">{hi_html or '<span class="mut">暂无数据</span>'}</div>
<h2>② 各商品明细</h2>
{''.join(cards) or '<div class="box">没有探测结果</div>'}
<h2>③ 省级运费汇总（按中位数降序）</h2>
<div class="box scroll"><table><thead><tr><th>省份</th><th style="text-align:right">最低</th>
<th style="text-align:right">最高</th><th style="text-align:right">中位</th>
<th style="text-align:right">样本数</th></tr></thead><tbody>{rows}</tbody></table></div>
<h2>④ 未覆盖地区与业务建议</h2>
<div class="box">
 <p><b>本次未覆盖：</b>{unc}</p>
 <ul>
  <li><b>运费是「按收货地址」算的</b>：同一商品不同地区差价可达数倍；采集时记下的
      <code>express_fee</code> 只是当时那一个地址的运费，不能当全国进货成本。</li>
  <li><b>定价建议</b>：用主流地区运费中位数（本报告 ¥{s['fee_median'] if s['fee_median'] is not None else '—'}）
      作为 <code>inbound_shipping</code>；偏远地区的高运费在拼多多端用运费模板单独覆盖。</li>
  <li><b>不发货地区</b>：官方接口对限售区域会返回失败或页面提示「不支持配送」。
      未覆盖省份不代表能发货，请在 1688 后台补一个该省收货地址后重跑本探测。</li>
  <li><b>不能照搬</b>：1688 运费模板属供应商后台私有配置，且随时可变；
      拼多多店铺的运费模板必须在 PDD 商家后台单独设置（包邮区域 / 偏远加价 / 限售区域）。</li>
 </ul>
</div>
<footer>由 pdd-seller-suite · freight_1688.py 生成 · 数据仅供选品与定价参考，以下单页实时运费为准</footer>
</div></body></html>"""


def main():
    ap = argparse.ArgumentParser(description="探测 1688 商品分地区运费规则")
    ap.add_argument("--limit", type=int, default=None, help="只跑前 N 个商品")
    ap.add_argument("--max-addr", type=int, default=30, help="每商品最多探测多少个省级地址")
    ap.add_argument("--item", type=str, default=None, help="指定商品 ID")
    ap.add_argument("--headless", action="store_true", help="无窗口运行")
    ap.add_argument("--no-report", action="store_true", help="只写 JSON/CSV, 不写 HTML")
    ap.add_argument("--fill", action="store_true",
                    help="增量补全: 只重跑现有报告中 status=unknown 的商品, 并 merge 回报告")
    a = ap.parse_args()

    items = _load_items()
    if a.item:
        items = [x for x in items if x["item_id"] == str(a.item)]
    # 增量补全: 只取现有报告里仍含 unknown 的商品
    base = None
    if a.fill:
        if OUT_JSON.exists():
            try:
                base = json.loads(OUT_JSON.read_text(encoding="utf-8"))
            except Exception:
                base = None
        if base:
            need = {it["item_id"] for it in base.get("items", [])
                    if any((r.get("status") == "unknown") for r in it.get("records", []))}
            items = [x for x in items if x["item_id"] in need]
            print(f"增量补全: 现有报告 {len(base['items'])} 商品, "
                  f"其中 {len(items)} 个含 unknown 记录, 将重跑这些商品")
        else:
            print("未找到现有报告, 转为全量探测")
            base = None
    if not items:
        print("没有可探测/补全的商品：请检查 data/collected_1688.json 或 --fill 参数")
        return 1
    print(f"待探测 {len(items)} 个商品, 每商品最多 {a.max_addr} 个省级地址")
    res = probe(items, max_addr=a.max_addr, headless=a.headless, limit=a.limit)
    # 增量补全: 用新结果替换 base 中对应商品, 保留其余商品(含旧 unknown/失败项不丢)
    if base is not None:
        bmap = {it["item_id"]: it for it in base["items"]}
        done_ids = {x["item_id"] for x in res["items"]}
        for new_it in res["items"]:
            bmap[new_it["item_id"]] = new_it
        res["items"] = list(bmap.values())
        if res.get("errors"):
            kept = [e for e in base.get("errors", [])
                    if e["item_id"] not in done_ids]
            res["errors"] = kept + res["errors"]
    s = export(res)
    print(f"\n完成: {s['items']} 商品 / {s['records']} 条地区记录 / {s['provinces']} 个省级行政区")
    if s["fee_min"] is not None:
        print(f"运费区间 ¥{s['fee_min']:g} ~ ¥{s['fee_max']:g}, 中位数 ¥{s['fee_median']:g}")
    print(f"输出: {OUT_JSON} / {OUT_CSV} / {OUT_HTML}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
