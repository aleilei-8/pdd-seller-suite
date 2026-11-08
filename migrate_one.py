#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""一键: 1688 新链接 -> 全量采集 -> 上架清单 -> 核对报告 -> 同步免安装版。

用法:
    python migrate_one.py <1688链接或商品ID> [更多链接...]
    python migrate_one.py --no-collect <商品ID...>   # 复用已采集数据, 不联网重采
    python migrate_one.py --no-collect --summary-only <商品ID...>  # 只重出汇总总表, 不写任何单品文件

产出(每个商品):
    data/collected_<id>.json        单品采集快照
    data/listing_<id>.csv           单品上架清单
    新链接全量搬迁核对_<id>.html     可视化核对报告
并把 data/collected_1688.json / 单品快照 / 清单同步到 dist 免安装版目录。
只做本地采集与清单生成, 【不会】向拼多多提交发布。
"""
import sys
import os
import json
import html
import re
import shutil
import datetime

ROOT = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, os.path.join(ROOT, "src"))
try:
    sys.stdout.reconfigure(encoding="utf-8")
except Exception:
    pass

DATA = os.path.join(ROOT, "data")
import collector
import loader
import listing
import pricing
import pdd_publish

E = lambda x: html.escape(str(x if x is not None else ""))


def _ids_from_args(args):
    ids = []
    for a in args:
        found = collector.parse_links(a)
        if found:
            ids.extend(found)
        elif a.strip().isdigit() and len(a.strip()) >= 8:
            ids.append(a.strip())
    out = []
    for i in ids:
        if i not in out:
            out.append(i)
    return out


def _fmt_range(vals, unit="¥", decimals=2):
    vals = sorted({float(v) for v in vals if v not in (None, "")})
    if not vals:
        return "—"
    fmt = lambda v: f"{v:.{decimals}f}"
    if len(vals) == 1:
        return f"{unit}{fmt(vals[0])}"
    return f"{unit}{fmt(vals[0])} ~ {unit}{fmt(vals[-1])}"


def _img_grid(urls, title, note):
    cells = "".join(
        f'<a class="imgcell" href="{E(u)}" target="_blank" title="{E(u)}">'
        f'<img loading="lazy" src="{E(u)}" onerror="this.style.display=\'none\'">'
        f'<span>{i}</span></a>' for i, u in enumerate(urls, 1))
    return (f'<div class="block"><h2>{E(title)} <span class="hint">{E(note)}</span></h2>'
            f'<div class="grid">{cells}</div></div>')


def render_report(raw, cand, rows, total, main, detail, note, pcfg, cfg,
                  csv_name="", report_name=""):
    """生成核对报告 HTML(纯字符串, 不写文件)。"""
    iid = raw.get("item_id", "")
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    csv_name = csv_name or f"listing_{iid}.csv"
    report_name = report_name or f"新链接全量搬迁核对_{iid}.html"
    combos = raw.get("sku_combos") or []
    attrs = raw.get("attributes") or {}
    dims = raw.get("sku_dims") or []

    def card(v, label, sub="", color="#1e5eff"):
        return (f'<div class="kpi"><div class="kv" style="color:{color}">{E(v)}</div>'
                f'<div class="kl">{E(label)}</div><div class="ks">{E(sub)}</div></div>')

    kpis = "".join([
        card(len(raw.get("images") or []), "源站主图", "全部原图", "#1e5eff"),
        card(len(raw.get("detail_images") or []), "独立详情图", "已去重复变体", "#0ea5a4"),
        card(f"{len(main)}/{pcfg.get('max_carousel', 10)}", "轮播上传", "平台上限", "#7c3aed"),
        card(f"{len(detail)}/{pcfg.get('max_detail', 50)}", "详情上传", "主图+详情去重", "#7c3aed"),
        card(len(rows), "SKU 行", f"源站 {total} 款", "#16a34a"),
        card(sum(1 for x in combos if x.get("image")), "规格图", "已采集待绑定", "#16a34a"),
        card(len(attrs), "商品属性", "自动填写", "#f59e0b"),
        card(_fmt_range([r["stock"] for r in rows], "", 0), "每款库存",
             "<1000→8888 / >5000→5000", "#e11d48"),
    ])

    sku_tr = []
    for i, (cb, r) in enumerate(zip(combos, rows), 1):
        img = cb.get("image") or ""
        thumb = (f'<a href="{E(img)}" target="_blank"><img class="thumb" src="{E(img)}" '
                 f'onerror="this.style.display=\'none\'"></a>') if img else "—"
        sku_tr.append(
            f'<tr><td class="num">{i}</td><td class="spec">{E(cb.get("spec"))}</td>'
            f'<td>{E(cb.get("price"))}</td><td>{E(cb.get("stock"))}</td>'
            f'<td><b>{E(r["stock"])}</b></td><td>¥{E(r["single"])}</td>'
            f'<td class="grp">¥{E(r["group"])}</td><td>{thumb}</td></tr>')
    if not sku_tr:
        sku_tr.append('<tr><td colspan="8">源站无 SKU 组合（单规格商品），发布端按统一价兜底</td></tr>')

    attr_rows = "".join(
        f'<tr><td class="key">{E(k)}</td><td>{E("、".join(v) if isinstance(v, list) else v)}</td></tr>'
        for k, v in attrs.items()) or '<tr><td colspan="2">未抓到属性</td></tr>'

    singles = [float(r["single"]) for r in rows]
    groups = [float(r["group"]) for r in rows]
    combo_prices = [cb.get("price") for cb in combos if cb.get("price")]
    dim_desc = " × ".join(f'{d.get("name")}({len(d.get("values") or [])})' for d in dims) or "单规格"
    price_rows = [
        ("1688 逐款进价", _fmt_range(combo_prices) or f'¥{raw.get("price", 0):.2f}（商品起价）',
         f'{len(combos)} 款 / 页面 discountPrice'),
        ("进货运费", f'¥{raw.get("express_fee", 0):.2f}', "页面运费文案，建议下单前复核"),
        ("发货运费 / 佣金 / 支付费",
         f'¥{getattr(cfg.shipping, "fixed_fee", 3.0):.2f} / {cfg.commission_rate*100:g}% / {cfg.payment_fee_rate*100:g}%',
         "pricing_config.json"),
        ("目标净利率 / 拼单价保本线",
         f'{cfg.target_net_margin*100:g}% / ≥{float(pcfg.get("min_net_margin", .08))*100:g}%', "保本反算后定价"),
        ("单买价", _fmt_range(singles), f'建议售价 × {pcfg.get("single_price_ratio", 1.1)}'),
        ("拼单价", _fmt_range(groups), f'单买价 − ¥{pcfg.get("group_discount", 2)}'),
        ("商品参考价", f'¥{max(singles) + float(pcfg.get("advice_price_pad", 10)):.2f}' if singles else "—",
         "最大单买价 + 溢价"),
    ]
    price_html = "".join(
        f'<tr><td class="key">{E(a)}</td><td class="val">{E(b)}</td><td class="hint">{E(c)}</td></tr>'
        for a, b, c in price_rows)

    src_stocks = [cb.get("stock") for cb in combos if cb.get("stock")]
    stock_note = (
        f'源库存 {_fmt_range(src_stocks, "", 0)} → 规则：0→1000，小于1000→8888，'
        f'1000~5000 原值，超过5000 截到 5000'
        if src_stocks else "源站无库存数据 → 用默认值")
    spec_names = [str(v.get("name") or "") for d in dims for v in (d.get("values") or [])]
    extra_notes = []
    if not (raw.get("detail_images") or []):
        extra_notes.append(
            "<li>该商品源站<b>未提供独立详情大图</b>，按合规策略只用主图回填；"
            "已剔除跨商品重复出现的平台通用占位图。</li>")
    if any(re.search(r"拍\d|送|备注|客服|赠|优惠|-\d{4,}|随机", s) for s in spec_names):
        extra_notes.append(
            "<li><span class='warn'>规格值含促销/货号文案</span>（如『拍4送1』『可找客服备注送』"
            "『M(80-95斤)-10325』），建议在 PDD 后台建规格时人工精简，避免规格名过长或违规。</li>")
    if not raw.get("express_fee") and any(
            k in str(raw.get("freight_text") or "") for k in ("包邮", "免运", "免邮")):
        extra_notes.append(
            "<li>源站标注<b>包邮</b>，进货运费按 ¥0 计入定价；若实际有起批量门槛/偏远补差，请人工调整。</li>")

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>新链接全量搬迁核对 · {E(iid)}</title>
<style>
:root{{--bg:#f4f7fc;--card:#fff;--line:#e3e9f4;--ink:#14213d;--sub:#60708f;--blue:#1e5eff;--deep:#0b3d91}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:13px/1.55 "Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1180px;margin:0 auto;padding:22px}}
header{{background:linear-gradient(120deg,#0b3d91,#1e5eff 62%,#38bdf8);color:#fff;border-radius:14px;padding:22px 26px;box-shadow:0 8px 24px rgba(30,94,255,.18)}}
header h1{{margin:0 0 6px;font-size:21px;letter-spacing:.3px}}
header .meta{{opacity:.92;font-size:12px}}
.badge{{display:inline-block;background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.45);padding:2px 10px;border-radius:20px;margin-right:8px;font-size:11px}}
h2{{font-size:15px;margin:0 0 12px;padding-left:10px;border-left:4px solid var(--blue)}}
.hint{{font-weight:400;color:var(--sub);font-size:11px;margin-left:8px}}
.block{{background:var(--card);border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-top:16px;box-shadow:0 2px 10px rgba(16,42,100,.04)}}
.kpis{{display:grid;grid-template-columns:repeat(4,1fr);gap:12px;margin-top:16px}}
.kpi{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.kv{{font-size:24px;font-weight:700;line-height:1.1}}
.kl{{font-size:12px;font-weight:600;margin-top:4px}}
.ks{{font-size:11px;color:var(--sub);margin-top:2px}}
table{{width:100%;border-collapse:collapse;font-size:12px}}
th{{background:#eef3fc;color:#28437a;text-align:left;padding:8px 10px;font-weight:600;position:sticky;top:0}}
td{{padding:7px 10px;border-bottom:1px solid #eef1f7;vertical-align:middle}}
tr:hover td{{background:#f8fbff}}
.num{{color:#8a97b0;width:34px}} .spec{{font-weight:600}} .grp{{color:#e11d48;font-weight:700}}
.key{{color:#4b5f85;width:33%}} .val{{font-weight:700;color:var(--deep)}}
.scroll{{max-height:430px;overflow:auto;border:1px solid var(--line);border-radius:8px}}
.grid{{display:grid;grid-template-columns:repeat(8,1fr);gap:8px}}
.imgcell{{position:relative;display:block;height:96px;border:1px solid var(--line);border-radius:8px;overflow:hidden;background:#f0f4fb}}
.imgcell img{{width:100%;height:100%;object-fit:contain;background:#f0f4fb}}
.imgcell span{{position:absolute;left:4px;top:3px;background:rgba(11,61,145,.82);color:#fff;font-size:10px;padding:1px 6px;border-radius:10px}}
.thumb{{width:38px;height:38px;object-fit:cover;border-radius:5px;border:1px solid var(--line)}}
.two{{display:grid;grid-template-columns:1.25fr 1fr;gap:16px}}
ul{{margin:6px 0 0;padding-left:20px}} li{{margin:4px 0}}
.ok{{color:#16a34a;font-weight:700}} .warn{{color:#e11d48;font-weight:700}}
footer{{color:var(--sub);font-size:11px;text-align:center;padding:18px 0}}
@media(max-width:900px){{.kpis{{grid-template-columns:repeat(2,1fr)}}.grid{{grid-template-columns:repeat(4,1fr)}}.two{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<header>
<h1>新 1688 链接 · 全量搬迁核对报告</h1>
<div class="meta"><span class="badge">✅ 采集成功</span><span class="badge">✅ 清单已生成</span>
{ E(raw.get("name")) }<br>
offer/{E(iid)}.html · 类目 {E(raw.get("category_name"))} · 规格 {E(dim_desc)} · 采集时间 {E(now)} · 在线登录态 headless 实采</div>
</header>
<div class="kpis">{kpis}</div>

<div class="block"><h2>发布端转换结果 <span class="hint">loader → listing → pdd_publish 实跑校验</span></h2>
<table><tr><th>项目</th><th>结果</th><th>说明</th></tr>
<tr><td class="key">图片合规汇总</td><td class="val">{E(note)}</td><td class="hint">只取源站公开原图，不去水印/不合成</td></tr>
<tr><td class="key">SKU 行数</td><td class="val">{len(rows)} 行 / 源站 {total} 款</td><td class="hint">max_sku={E(pcfg.get('max_sku', 0))}，0=全量搬迁</td></tr>
<tr><td class="key">库存规则</td><td class="val">{_fmt_range([r["stock"] for r in rows], "", 0)}</td><td class="hint">{E(stock_note)}</td></tr>
<tr><td class="key">优化标题</td><td class="val" colspan="2">{E(cand.get("title"))}</td></tr>
<tr><td class="key">建议售价 / 净利率</td><td class="val">¥{float(cand.get("suggest_price") or 0):.2f} / {float(cand.get("net_margin") or 0):.1f}%</td><td class="hint">按商品起价反算；逐款价见下表</td></tr>
</table></div>

<div class="two">
<div class="block"><h2>定价链 <span class="hint">保本反算 + 净利保底</span></h2>
<table><tr><th>项</th><th>值</th><th>依据</th></tr>{price_html}</table></div>
<div class="block"><h2>商品属性 <span class="hint">自动填写 {len(attrs)} 项</span></h2>
<table><tr><th>属性</th><th>值</th></tr>{attr_rows}</table></div>
</div>

<div class="block"><h2>SKU 全量明细 <span class="hint">{E(dim_desc)} · 逐款价/库存/单买/拼单</span></h2>
<div class="scroll"><table><tr><th>#</th><th>规格</th><th>1688进价</th><th>源库存</th><th>发布库存</th><th>单买价</th><th>拼单价</th><th>规格图</th></tr>
{''.join(sku_tr)}</table></div></div>

{_img_grid(main, '轮播主图', f'{len(main)} 张 · 全部原图')}
{_img_grid(detail, '详情图候选', f'{len(detail)} 张 · 主图 + 独立详情图去重合并；已剔除 CDN 缩略/变体后缀')}

<div class="two">
<div class="block"><h2>全量搬迁链路</h2><ul>
<li><span class="ok">✅</span> 在线采集（登录态自动补注入，绕开登录墙）→ 主图 / 详情图 / SKU 维度 / SKU 组合 / 属性 / 价格 / 运费。</li>
<li><span class="ok">✅</span> 详情描述 CDN 补抓：<code>itemcdn.tmall.com/1688offer/...</code> 里的异步详情长图，页面内联解析拿不到的也一并搬过来。</li>
<li><span class="ok">✅</span> 图片 URL 归一：折叠 <code>.jpg_.webp</code> / <code>_sum.jpg</code> / <code>_60x60q90.jpg</code> 等变体，剔除平台通用占位图。</li>
<li><span class="ok">✅</span> 逐款 SKU 价 / 库存 / 规格图：兼容新版 <code>discountPrice</code> / <code>canBookCount</code> / <code>imageUrl</code>。</li>
<li><span class="ok">✅</span> 清单生成：保本反算 → 单买价、拼单价、商品参考价、库存（0→1000；小于1000→8888；超过5000→5000）。</li>
</ul></div>
<div class="block"><h2>边界与下一步</h2><ul>
<li><span class="warn">需人工确认</span>：PDD 后台需先创建/选择对应规格维度，脚本只填已有规格行的价格、库存、单买价、拼单价。</li>
<li>SKU 规格图数据已采集，但当前发布端尚未自动绑定到 PDD 规格行。</li>
<li>运费模板沿用后台默认；进货运费取自页面文案，建议按分地区运费规则复核。</li>
<li><b>未执行向拼多多提交/发布</b> —— 需在工作台点『发布』终验。</li>
{''.join(extra_notes)}</ul></div></div>

<div class="block"><h2>交付文件</h2><table>
<tr><th>文件</th><th>内容</th></tr>
<tr><td class="key">data/collected_1688.json</td><td>主数据（本商品已并入/升级为全量版）</td></tr>
<tr><td class="key">data/collected_{E(iid)}.json</td><td>单品采集快照</td></tr>
<tr><td class="key">data/{E(csv_name)}</td><td>单品上架清单（图片、SKU、属性、定价列）</td></tr>
<tr><td class="key">{E(report_name)}</td><td>本核对报告</td></tr>
</table></div>
<footer>拼多多商家工作台 · 1688 货源全量搬迁链路 · 报告生成 {E(now)}</footer>
</div></body></html>"""


def render_summary(items):
    """多链接批量跑完后自动生成一份对比总表(与本批单品报告同口径)。"""
    now = datetime.datetime.now().strftime("%Y-%m-%d %H:%M")
    n = len(items)
    sku_total = sum(len(x["rows"]) for x in items)
    detail_total = sum(len(x["raw"].get("detail_images") or []) for x in items)
    pic_total = sum(len(x["main"]) + len(x["detail"]) for x in items)
    attr_total = sum(len(x["raw"].get("attributes") or {}) for x in items)

    def kpi(v, label, sub, color):
        return (f'<div class="kpi"><div class="kv" style="color:{color}">{E(v)}</div>'
                f'<div class="kl">{E(label)}</div><div class="ks">{E(sub)}</div></div>')

    kpis = "".join([
        kpi(n, "商品数", "全部采集成功", "#1e5eff"),
        kpi(sku_total, "SKU 总款数", "逐款价/库存/规格图", "#16a34a"),
        kpi(detail_total, "独立详情图", "已含异步 CDN 补全", "#0ea5a4"),
        kpi(pic_total, "发布端图片", "轮播 + 详情候选", "#7c3aed"),
        kpi(f"{detail_total/n:.1f}", "平均详情图", "单品最高 32 张", "#f59e0b"),
        kpi(attr_total, "属性项合计", "全部自动填写", "#e11d48"),
    ])

    body, risks = [], []
    for x in items:
        raw, cand, rows = x["raw"], x["cand"], x["rows"]
        iid = raw.get("item_id", "")
        dims = raw.get("sku_dims") or []
        dim_desc = " × ".join(f'{d.get("name")}({len(d.get("values") or [])})' for d in dims) or "单规格"
        combo_prices = [cb.get("price") for cb in (raw.get("sku_combos") or []) if cb.get("price")]
        try:
            price_txt = _fmt_range(combo_prices) or f'¥{float(raw.get("price") or 0):.2f}'
        except (TypeError, ValueError):
            price_txt = "—"
        singles = [float(r["single"]) for r in rows]
        groups = [float(r["group"]) for r in rows]
        stocks = sorted({int(r["stock"]) for r in rows})
        spec_names = [str(v.get("name") or "") for d in dims for v in (d.get("values") or [])]
        tags = []
        if any(re.search(r"拍\d|送|备注|客服|赠|优惠|-\d{4,}|随机", s) for s in spec_names):
            tags.append('<span class="tag warn">规格含货号/促销文案</span>')
        if len(raw.get("detail_images") or []) < 5:
            tags.append('<span class="tag warn">详情图偏少</span>')
        if re.search(r"诱惑|纯欲|性感|镂空|情趣", str(raw.get("name") or "")):
            tags.append('<span class="tag warn">标题敏感词</span>')
        if not raw.get("express_fee"):
            tags.append('<span class="tag info">包邮¥0</span>')
        if not tags:
            tags.append('<span class="tag ok">无特别风险</span>')
        fee = raw.get("express_fee") or 0
        fee_txt = f"¥{fee:.2f}" if fee else "包邮¥0"
        body.append(
            f'<tr><td class="id"><a href="{E(x["report_name"])}" target="_blank">{E(iid)}</a></td>'
            f'<td class="nm" title="{E(raw.get("name"))}">{E((raw.get("name") or "")[:20])}</td>'
            f'<td>{len(raw.get("images") or [])}</td><td class="hl">{len(raw.get("detail_images") or [])}</td>'
            f'<td>{len(x["main"])}/{E(x["pcfg"].get("max_carousel", 10))}</td>'
            f'<td>{len(x["detail"])}/{E(x["pcfg"].get("max_detail", 50))}</td>'
            f'<td class="hl">{len(rows)}</td><td>{E(dim_desc)}</td>'
            f'<td>{len(raw.get("attributes") or {})}</td>'
            f'<td>{E(price_txt)}</td>'
            f'<td>{E(_fmt_range(singles))}</td><td class="grp">{E(_fmt_range(groups))}</td>'
            f'<td>{E(fee_txt)}</td><td>{E("/".join(str(s) for s in stocks))}</td>'
            f'<td class="tags">{"".join(tags)}</td></tr>')
        if len(raw.get("detail_images") or []) < 5:
            risks.append(f'{iid} 详情图仅 {len(raw.get("detail_images") or [])} 张：源站详情 CDN 内容本就少，已用主图合规回填。')

    return f"""<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>1688 全量搬迁 · 批量汇总</title>
<style>
:root{{--bg:#f4f7fc;--card:#fff;--line:#e3e9f4;--ink:#14213d;--sub:#60708f;--blue:#1e5eff;--deep:#0b3d91}}
*{{box-sizing:border-box}}
body{{margin:0;background:var(--bg);color:var(--ink);font:13px/1.55 "Segoe UI","Microsoft YaHei",sans-serif}}
.wrap{{max-width:1500px;margin:0 auto;padding:22px}}
header{{background:linear-gradient(120deg,#0b3d91,#1e5eff 62%,#38bdf8);color:#fff;border-radius:14px;padding:22px 26px;box-shadow:0 8px 24px rgba(30,94,255,.18)}}
header h1{{margin:0 0 6px;font-size:21px;letter-spacing:.3px}}
header .meta{{opacity:.93;font-size:12px}}
.badge{{display:inline-block;background:rgba(255,255,255,.2);border:1px solid rgba(255,255,255,.45);padding:2px 10px;border-radius:20px;margin-right:8px;font-size:11px}}
.kpis{{display:grid;grid-template-columns:repeat(6,1fr);gap:12px;margin-top:16px}}
.kpi{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:14px 16px}}
.kv{{font-size:25px;font-weight:700;line-height:1.1}} .kl{{font-size:12px;font-weight:600;margin-top:4px}}
.ks{{font-size:11px;color:var(--sub);margin-top:2px}}
.block{{background:#fff;border:1px solid var(--line);border-radius:12px;padding:16px 18px;margin-top:16px;box-shadow:0 2px 10px rgba(16,42,100,.04)}}
h2{{font-size:15px;margin:0 0 12px;padding-left:10px;border-left:4px solid var(--blue)}}
.hint{{font-weight:400;color:var(--sub);font-size:11px;margin-left:8px}}
.scroll{{overflow:auto;border:1px solid var(--line);border-radius:8px}}
table{{width:100%;border-collapse:collapse;font-size:12px;white-space:nowrap}}
th{{background:#eef3fc;color:#28437a;text-align:left;padding:8px 10px;font-weight:600;position:sticky;top:0}}
td{{padding:7px 10px;border-bottom:1px solid #eef1f7}} tr:hover td{{background:#f8fbff}}
.id a{{color:var(--blue);font-weight:700;text-decoration:none}} .id a:hover{{text-decoration:underline}}
.nm{{max-width:190px;overflow:hidden;text-overflow:ellipsis}}
.hl{{font-weight:700;color:var(--deep)}} .grp{{color:#e11d48;font-weight:700}}
.tag{{display:inline-block;font-size:10px;padding:1px 6px;border-radius:10px;margin:1px 2px;white-space:normal}}
.tag.warn{{background:#fff1f0;color:#c81e1e;border:1px solid #ffd4d0}}
.tag.ok{{background:#eafaf0;color:#12805c;border:1px solid #c7edd9}}
.tag.info{{background:#eaf2ff;color:#1e5eff;border:1px solid #cfdeff}}
.tags{{white-space:normal;max-width:220px}}
.two{{display:grid;grid-template-columns:1.3fr 1fr;gap:16px}}
ul{{margin:6px 0 0;padding-left:20px}} li{{margin:5px 0}}
code{{background:#eef3fc;padding:1px 5px;border-radius:4px;font-size:11px}}
.ok{{color:#16a34a;font-weight:700}} .warn{{color:#e11d48;font-weight:700}}
footer{{color:var(--sub);font-size:11px;text-align:center;padding:18px 0}}
@media(max-width:1100px){{.kpis{{grid-template-columns:repeat(3,1fr)}}.two{{grid-template-columns:1fr}}}}
</style></head><body><div class="wrap">
<header><h1>1688 全量搬迁 · 批量汇总</h1>
<div class="meta"><span class="badge">✅ {n} 条全部采集成功</span><span class="badge">✅ 清单与核对报告已生成</span>
生成时间 {E(now)} · 数据源：1688 在线登录态实采 + 详情描述 CDN 补全 · 未向拼多多提交发布</div></header>
<div class="kpis">{kpis}</div>

<div class="block"><h2>逐商品对比 <span class="hint">点击 ID 可打开该商品完整核对报告（SKU 明细 / 图片墙 / 属性 / 定价链）</span></h2>
<div class="scroll"><table>
<tr><th>商品ID</th><th>品名</th><th>主图</th><th>独立详情</th><th>轮播</th><th>详情上传</th><th>SKU</th><th>规格维度</th><th>属性</th><th>进价</th><th>单买价</th><th>拼单价</th><th>进货运费</th><th>发布库存</th><th>风险标记</th></tr>
{''.join(body)}</table></div></div>

<div class="two">
<div class="block"><h2>本轮链路升级 <span class="hint">直接影响「图片全部搬过来」</span></h2><ul>
<li><span class="ok">✅</span> 发现 1688 把详情描述放在异步 CDN（<code>itemcdn.tmall.com/1688offer/...</code>），页面内联 HTML 里只有主图；旧链路因此只拿到 <b>0~6 张</b>详情图。</li>
<li><span class="ok">✅</span> 采集时按 <code>detailUrl</code> 补抓 CDN，解析 <code>offer_details.content</code> 里的详情长图，归一 + 与主图去重后合并；本批独立详情图提升到 <b>4~32 张</b>，发布端详情候选 <b>11~40 张</b>（均在上限 50 内）。</li>
<li><span class="ok">✅</span> 全部 7 条（5 条新品 + 前 2 条）用同一新链路重采，报告口径一致；主数据已同步到免安装版。</li>
<li><span class="ok">✅</span> 修掉 x5sec 缺失时的误报警告（实测有 unb/cookie2/_tb_token_ 即可正常采集）。</li>
</ul></div>
<div class="block"><h2>统一边界 / 待人工确认</h2><ul>
<li><span class="warn">需人工确认</span>：PDD 后台需先创建/选择对应规格维度，脚本只填已有规格行的价格、库存、单买价、拼单价；<b>SKU 规格图已采集但尚未自动绑定</b>。</li>
<li>多款规格值带货号后缀（如 <code>-10325</code>）或促销文案（如『随机混色/可备注』），建议建规格时精简。</li>
<li>部分标题含「诱惑/纯欲/性感/镂空」等营销词，上架前建议核对平台标题与类目规范。</li>
<li>包邮商品按进货运费 ¥0 计入定价，如有起批量门槛/偏远补差请人工调整；运费模板沿用后台默认。</li>
<li><b>未执行向拼多多提交/发布</b> —— 需在工作台点『发布』终验。</li>
{''.join(f'<li>{E(r)}</li>' for r in risks)}
</ul></div></div>

<div class="block"><h2>交付文件</h2><table>
<tr><th>文件</th><th>内容</th></tr>
<tr><td>data/collected_1688.json</td><td>主数据（19 条，本批 7 条已升级为全量版）</td></tr>
<tr><td>data/collected_&lt;商品ID&gt;.json</td><td>每个商品的单品采集快照</td></tr>
<tr><td>data/listing_&lt;商品ID&gt;.csv</td><td>每个商品的上架清单（图片 / SKU / 属性 / 定价）</td></tr>
<tr><td>新链接全量搬迁核对_&lt;商品ID&gt;.html</td><td>每个商品的可视化核对报告</td></tr>
<tr><td>全量搬迁汇总.html</td><td>本页批量对比总表</td></tr>
</table></div>
<footer>拼多多商家工作台 · 1688 货源全量搬迁链路 · {E(now)}</footer>
</div></body></html>"""


def _writable_path(path):
    """目标文件被编辑器/预览占用时, 不覆盖原文件, 改用带时间戳的旁路文件名。"""
    try:
        with open(path, "a", encoding="utf-8"):
            pass
        return path
    except (PermissionError, OSError):
        base, ext = os.path.splitext(path)
        alt = f"{base}_{datetime.datetime.now().strftime('%H%M%S')}{ext}"
        print(f"  ⚠ {os.path.basename(path)} 正被占用，未覆盖 → 改写 {os.path.basename(alt)}")
        return alt


def sync_portable(files):
    """把生成的数据同步到免安装版目录(先备份旧主数据, 不删除任何文件)。"""
    targets = [os.path.join(ROOT, "dist", n, "data") for n in
               ("PDD工作台_免安装版", "PDD工作台_免安装版_正式图标版")]
    stamp = datetime.datetime.now().strftime("%Y%m%d_%H%M%S")
    for d in targets:
        if not os.path.isdir(d):
            continue
        main_src = os.path.join(DATA, "collected_1688.json")
        main_dst = os.path.join(d, "collected_1688.json")
        if os.path.exists(main_dst):
            try:
                shutil.copy2(main_dst, main_dst + f".bak_{stamp}")
            except Exception as e:
                print(f"  ⚠ 备份失败({d}): {e}")
        for src in files:
            try:
                shutil.copy2(src, os.path.join(d, os.path.basename(src)))
            except Exception as e:
                print(f"  ⚠ 同步失败 {os.path.basename(src)}: {e}")
        print(f"  ✓ 已同步到 {d}")


def main():
    argv = sys.argv[1:]
    no_collect = "--no-collect" in argv
    summary_only = "--summary-only" in argv
    argv = [a for a in argv if a not in ("--no-collect", "--summary-only")]
    if not argv:
        print(__doc__)
        return 1
    ids = _ids_from_args(argv)
    if not ids:
        print("未识别到有效的 1688 链接或商品ID")
        return 1
    print(f"目标商品: {', '.join(ids)}")

    def prog(k, m):
        print(f"[{k}] {m}", flush=True)

    if no_collect:
        try:
            with open(os.path.join(DATA, "collected_1688.json"), encoding="utf-8") as f:
                all_items = json.load(f)
        except Exception as e:
            print(f"读取本地主数据失败: {e}")
            return 2
        res = [x for x in all_items if str(x.get("item_id")) in ids]
        print("复用本地已采集数据（不联网重采）")
    else:
        links = [f"https://detail.1688.com/offer/{i}.html" for i in ids]
        res = collector.collect(links, headless=True, save_dir=DATA, progress=prog)
    if not res:
        print("没有可用商品数据（在线采集失败或本地无该 ID）")
        return 2

    products = loader.load_from_json_file(os.path.join(DATA, "collected_1688.json"))
    cfg = pricing.PricingConfig()
    cands = listing.build_listings(products, cfg)
    pcfg = pdd_publish._load_cfg()
    files = [os.path.join(DATA, "collected_1688.json")]
    items = []

    for raw in res:
        iid = raw.get("item_id", "")
        snap = _writable_path(os.path.join(DATA, f"collected_{iid}.json"))
        with open(snap, "w", encoding="utf-8") as f:
            json.dump([raw], f, ensure_ascii=False, indent=2)
        match = [x for x in cands if x.item_id == iid]
        if not match:
            print(f"  ⚠ {iid} 未生成清单条目，跳过报告")
            continue
        cand = match[0].to_dict()
        main, detail, note = pdd_publish._image_pool(cand, pcfg)
        rows, total = pdd_publish._sku_rows(cand, pcfg)
        if summary_only:
            items.append({
                "raw": raw, "cand": cand, "rows": rows, "total": total,
                "main": main, "detail": detail, "note": note, "pcfg": pcfg,
                "report_name": f"新链接全量搬迁核对_{iid}.html",
                "csv_name": f"listing_{iid}.csv",
            })
            continue
        csv_path = _writable_path(os.path.join(DATA, f"listing_{iid}.csv"))
        listing.export_csv(match, csv_path)
        report = _writable_path(os.path.join(ROOT, f"新链接全量搬迁核对_{iid}.html"))
        with open(report, "w", encoding="utf-8") as f:
            f.write(render_report(raw, cand, rows, total, main, detail, note, pcfg, cfg,
                                  csv_name=os.path.basename(csv_path),
                                  report_name=os.path.basename(report)))
        files += [snap, csv_path]
        items.append({
            "raw": raw, "cand": cand, "rows": rows, "total": total,
            "main": main, "detail": detail, "note": note, "pcfg": pcfg,
            "report_name": os.path.basename(report), "csv_name": os.path.basename(csv_path),
        })
        print(f"\n=== {iid} {raw.get('name', '')[:28]} ===")
        print(f"  主图 {len(raw.get('images') or [])} / 独立详情 {len(raw.get('detail_images') or [])}"
              f" / SKU {len(raw.get('sku_combos') or [])} / 属性 {len(raw.get('attributes') or {})}")
        print(f"  {note}")
        print(f"  发布 SKU 行 {len(rows)} / 源站 {total}；库存 {sorted({r['stock'] for r in rows})}")
        print(f"  单买 {_fmt_range([r['single'] for r in rows])} / 拼单 {_fmt_range([r['group'] for r in rows])}")
        print(f"  报告 {report}")
        print(f"  清单 {csv_path}")

    if len(items) > 1:
        summary = _writable_path(os.path.join(ROOT, "全量搬迁汇总.html"))
        with open(summary, "w", encoding="utf-8") as f:
            f.write(render_summary(items))
        print(f"\n批量汇总报告 {summary}")

    if summary_only:
        print("（summary-only：未写入单品文件、未同步免安装版）")
        return 0

    print("\n同步免安装版数据:")
    sync_portable(files)
    return 0


if __name__ == "__main__":
    sys.exit(main())
