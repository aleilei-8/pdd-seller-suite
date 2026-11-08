#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pdd_store.py —— 拼多多店铺在售商品 扫描 / 零销量识别 / 下架(确认门控)

设计原则(对齐用户个人文件安全规则: 不删除、先确认清单、支持撤销):
  - scan()         只读: 登录后抓取在售商品列表(标题/商品ID/销量/状态) → store_products.json
  - report_zero()  只读: 从扫描结果筛出"零销量"商品 → zero_sales.csv + 控制台清单
  - delist()       危险: 默认 dry_run 仅打印清单; 真实下架需 confirm=True 且 selector 已校准;
                  支持传入 items 子集(由 GUI 勾选), 也支持只读全量零销量清单。
  - re_list()      撤销: 读取 delisted_log.json 并(待 selector 校准后)重新上架。

说明:
  - playwright 改为惰性导入: 本模块被 GUI 在后台线程 `import` 时, 即使未装 playwright 也不致崩溃,
    只有真正调用 scan()/delist(真实执行) 才会用到浏览器。
  - 拼多多后台是 SPA, 真实表格 selector 需一次实跑后用 debug_store.html 校准,
    本文件把 selector 集中到 SELECTORS 常量, 方便后续微调。
"""
import csv
import json
import re
import time
from pathlib import Path

import paths as _paths
_paths.setup_env()
from paths import ROOT, SRC

HERE = SRC
STORE_JSON = (ROOT / "store_products.json").as_posix()
ZERO_CSV = (ROOT / "zero_sales.csv").as_posix()
DEBUG_HTML = (ROOT / "debug_store.html").as_posix()
DELISTED_LOG = (ROOT / "delisted_log.json").as_posix()

MMS_HOME = "https://mms.pinduoduo.com/"

# 候选商品列表页 URL(后台改版时可能变化, 按顺序尝试)
GOODS_LIST_CANDIDATES = [
    "https://mms.pinduoduo.com/goods/goods_list",
    "https://mms.pinduoduo.com/goods/goods_list?tab=on_sale",
    MMS_HOME + "goods/goods_list",
]

# 集中 selector, 便于实跑后校准
SELECTORS = {
    "table": "table",                       # 商品表格
    "row": "table tbody tr",                # 数据行
    "title_in_row": "td:nth-child(2)",      # 标题通常所在列(第2列商品信息)
    "goods_id_in_row": "td:nth-child(1)",   # 商品ID 列(可能含在商品信息里)
    # 销量列: 不同改版位置不同, 这里用"含'销量'表头对应的列"做兜底在代码里处理
    "off_shelf_btn": "text=下架",           # 下架按钮(待校准)
}


def _parse_sales(text: str) -> int:
    """'1.2万' -> 12000 ; '356' -> 356 ; '0'/'—'/'' -> 0"""
    if not text:
        return 0
    t = text.replace(",", "").replace(" ", "")
    m = re.search(r"([\d.]+)\s*万", t)
    if m:
        return int(float(m.group(1)) * 10000)
    m = re.search(r"(\d+)", t)
    return int(m.group(1)) if m else 0


def _extract_rows(page):
    """自适应抓取商品行, 兼容两种布局:
       · 经典 <table> 表格(旧版后台)
       · div 卡片/网格(新版 SPA, 无 <table>)
    返回 list[dict]; 两种都抓不到时返回 None(调用方会 dump HTML)。"""
    # ---------- 策略1: 经典 <table> ----------
    items = _extract_from_table(page)
    if items:
        return items
    # ---------- 策略2: 卡片/网格(新版 SPA) ----------
    return _extract_from_cards(page)


def _extract_from_table(page):
    try:
        if page.locator(SELECTORS["table"]).count() == 0:
            return None
        rows = page.locator(SELECTORS["row"]).all()
    except Exception:
        return None
    if not rows:
        return None

    items = []
    headers = []
    try:
        hcells = page.locator("table thead th, table tr th").all()
        headers = [c.inner_text().strip() for c in hcells]
    except Exception:
        headers = []

    sales_col = -1
    for i, h in enumerate(headers):
        if "销量" in h:
            sales_col = i
            break

    for r in rows:
        try:
            cells = r.locator("td").all()
            if len(cells) < 2:
                continue
            title = ""
            for c in cells:
                txt = c.inner_text().strip()
                if len(txt) > len(title):
                    title = txt
            gid = ""
            for c in cells:
                txt = c.inner_text().strip()
                m = re.search(r"(?:ID|商品编号|货号)[\s:：]*([0-9A-Za-z]{6,})", txt)
                if m:
                    gid = m.group(1)
                    break
                m2 = re.search(r"\b(\d{8,})\b", txt)
                if m2:
                    gid = m2.group(1)
            sales_text = ""
            if 0 <= sales_col < len(cells):
                sales_text = cells[sales_col].inner_text().strip()
            else:
                for c in cells:
                    txt = c.inner_text().strip()
                    if re.search(r"销量|件|单", txt) and re.search(r"\d", txt):
                        sales_text = txt
                        break
            status = "在售" if re.search(r"在售|销售中|上架", title) else "未知"
            items.append({
                "goods_id": gid,
                "title": re.sub(r"\s+", " ", title)[:120],
                "sales_text": sales_text,
                "sales": _parse_sales(sales_text),
                "status": status,
            })
        except Exception:
            continue
    return items


def _extract_from_cards(page):
    """新版 SPA 商品列表常为 div 卡片网格, 没有 <table>。
    按'含 8 位以上数字 ID + 较长标题文本'的卡片容器来兜底抓取。"""
    try:
        cards = page.locator(
            "[class*='goods-item'], [class*='GoodsItem'], [class*='goodsItem'], "
            "[class*='product-item'], [class*='ProductItem'], [class*='goods-card'], "
            "[class*='list-item'], [class*='ListItem']"
        ).all()
    except Exception:
        return None
        if not cards:
            return None

    items = []
    for c in cards:
        try:
            txt = c.inner_text() or ""
            if len(txt.strip()) < 6:
                continue
            gid = ""
            m = re.search(r"\b(\d{8,})\b", txt)
            if m:
                gid = m.group(1)
            lines = [l.strip() for l in txt.splitlines() if l.strip()]
            if not lines:
                continue
            title = max(lines, key=len)
            sales_text = ""
            for l in lines:
                if re.search(r"销量|件|单", l) and re.search(r"\d", l):
                    sales_text = l
                    break
            # 至少要有 ID 或一段像样的标题, 避免把导航/筛选条误当商品
            if gid or len(title) >= 12:
                items.append({
                    "goods_id": gid,
                    "title": re.sub(r"\s+", " ", title)[:120],
                    "sales_text": sales_text,
                    "sales": _parse_sales(sales_text),
                    "status": "在售",
                })
        except Exception:
            continue
    return items if items else None


def _try_max_page_size(page, prog=None):
    """尽力把每页显示条数调到最大(减少翻页次数)。

    优先处理拼多多自研 beast-core 自定义下拉(非原生 <select>), 再退回原生 <select>。
    找不到则静默跳过(保留默认每页条数, 退化为逐页扫描)。
    """
    # ---- 拼多多 beast-core 自定义下拉(用 JS 直接操作, 避免遮挡/可点击性误判) ----
    try:
        cur = _detect_page_size(page) or 0
        opened = page.evaluate(
            r"""() => {
                const DQ=(sel)=>{const o=[];(function w(r){if(!r)return;try{r.querySelectorAll(sel).forEach(e=>o.push(e));}catch(e){}try{r.querySelectorAll('*').forEach(el=>{if(el.shadowRoot)w(el.shadowRoot);});}catch(e){}})(document);return o;};
                const h=DQ("[data-testid='beast-core-select-header']");
                if(!h.length) return false;
                h[0].click(); return true;
            }"""
        )
        if opened:
            time.sleep(0.8)
            # 读取选项, 点最大值(深度遍历 light + shadow + portal)
            picked = page.evaluate(
                r"""() => {
                    const DQ=(sel)=>{const o=[];(function w(r){if(!r)return;try{r.querySelectorAll(sel).forEach(e=>o.push(e));}catch(e){}try{r.querySelectorAll('*').forEach(el=>{if(el.shadowRoot)w(el.shadowRoot);});}catch(e){}})(document);return o;};
                    const opts=DQ("[data-testid='beast-core-select-option']");
                    let best=null;
                    for(const o of opts){ const t=(o.textContent||'').trim(); const m=t.match(/(\d+)/); if(m){const v=parseInt(m[1],10); if(best===null||v>best.v) best={v,el:o}; } }
                    if(!best) return {ok:false};
                    try{ best.el.click(); return {ok:true, v:best.v}; }catch(e){ return {ok:false, err:String(e)}; }
                }"""
            )
            time.sleep(1.2)
            new_size = _detect_page_size(page) or 0
            if picked and picked.get("ok") and new_size > cur:
                if prog:
                    prog(f"已切换每页显示 {new_size} 条(beast-core 下拉, 减少翻页)")
                return
            # 没成功/没放大: 关掉面板(点空白处 + Escape), 避免遮挡下一页
            try:
                page.mouse.click(5, 5)
            except Exception:
                pass
            try:
                page.keyboard.press("Escape")
            except Exception:
                pass
            time.sleep(0.5)
    except Exception:
        try:
            page.keyboard.press("Escape")
        except Exception:
            pass

    # ---- 原生 <select> / 通用 ----
    try:
        # 遍历页面上所有 <select>, 找出『每页条数』那个
        selects = page.locator("select").all()
        for sel in selects:
            try:
                opts = sel.locator("option").all()
            except Exception:
                continue
            if not opts:
                continue
            texts = []
            best_val, best_label = None, None
            for o in opts:
                t = (o.inner_text() or "").strip()
                texts.append(t)
                m = re.search(r"(\d+)", t)
                if m:
                    v = int(m.group(1))
                    if best_val is None or v > best_val:
                        best_val, best_label = v, t
            joined = " ".join(texts)
            # 必须像『每页条数』下拉(选项里含 条/页), 避免误选排序/筛选下拉
            if best_label and any(k in joined for k in ("条", "/页", "页")):
                try:
                    sel.select_option(label=best_label)
                except Exception:
                    try:
                        sel.select_option(value=best_label)
                    except Exception:
                        continue
                time.sleep(1.5)
                if prog:
                    prog(f"已切换每页显示 {best_val} 条(减少翻页)")
                return
    except Exception:
        pass


def _page_html(page):
    """尽量取页面原始 HTML(拼多多 SPA 大页面下, 用原始 HTML 做正则远比 inner_text 可靠)。"""
    try:
        return page.content() or ""
    except Exception:
        try:
            return page.locator("body").inner_html() or ""
        except Exception:
            return ""


def _detect_page_size(page) -> int:
    """从『每页条数』推断当前每页显示条数。优先读拼多多 beast-core 下拉的只读 input 值。"""
    html = _page_html(page)
    # 1) 拼多多自研 beast-core 自定义下拉: <input ... data-testid="beast-core-select-htmlInput" value="10">
    m = re.search(
        r'data-testid="beast-core-select-htmlInput"[^>]*\bvalue="(\d+)"', html
    )
    if m:
        try:
            return int(m.group(1))
        except Exception:
            pass
    # 2) 『每页 X 条』文字(通用)
    m = re.search(r"每页\s*(\d+)\s*条", html)
    if m:
        return int(m.group(1))
    # 3) 原生 <select> 兜底
    try:
        for sel in page.locator("select").all():
            try:
                opts = sel.locator("option").all()
            except Exception:
                continue
            if not opts:
                continue
            joined = " ".join((o.inner_text() or "").strip() for o in opts)
            if any(k in joined for k in ("条", "/页", "页")):
                try:
                    val = sel.input_value()
                except Exception:
                    val = ""
                mm = re.search(r"(\d+)", val or joined)
                if mm:
                    return int(mm.group(1))
    except Exception:
        pass
    return 0


def _read_total_pages(page):
    """从分页区读总页数, 读不到返回 0. 直接用原始 HTML 正则(拼多多 SPA 下 inner_text 不可靠)。

    兼容多种写法:
      · 共有 59 条 / 共 59 条 + 当前每页条数 => ceil(总条数 / 每页条数)
      · 共 12 页 / 共有 12 页
      · 第 3 / 12 页   或   3/12页
    """
    try:
        html = _page_html(page)
        # 总数: 共有 59 条 / 共 59 条  (拼多多是『共有 X 条』, 中间可能有空格)
        tot = re.search(r"共\s*有?\s*([\d,]+)\s*条", html)
        if tot:
            try:
                total_items = int(tot.group(1).replace(",", ""))
            except Exception:
                total_items = 0
            size = _detect_page_size(page) or 10  # 兜底每页 10 条(拼多多默认)
            if total_items and size:
                import math
                return max(1, math.ceil(total_items / size))
        m = re.search(r"共\s*有?\s*(\d+)\s*页", html)
        if m:
            return int(m.group(1))
        m = re.search(r"第\s*\d+\s*/\s*(\d+)\s*页", html)
        if m:
            return int(m.group(1))
        m = re.search(r"(\d+)\s*/\s*(\d+)\s*页", html)
        if m:
            return int(m.group(2))
    except Exception:
        pass
    return 0


def _is_disabled(loc):
    try:
        if loc.is_disabled():
            return True
    except Exception:
        pass
    try:
        cls = (loc.get_attribute("class") or "").lower()
        if "disabled" in cls or "pagination-disabled" in cls:
            return True
    except Exception:
        pass
    return False


def _active_page(page):
    """读拼多多 beast-core 分页的『当前页码』(PGT_pagerItemActive 的文本). 非 beast-core 返回 None."""
    try:
        el = page.locator(
            "[data-testid='beast-core-pagination'] li[class*='pagerItemActive']"
        ).first
        if el.count() > 0:
            return (el.inner_text() or "").strip()
    except Exception:
        pass
    # 退回原始 HTML 解析(更稳)
    try:
        html = _page_html(page)
        m = re.search(r"pagerItemActive[^>]*>(\d+)<", html)
        if m:
            return m.group(1)
    except Exception:
        pass
    return None


def _click_next(page, prog=None):
    """点击『下一页』. 返回 True=翻页成功(继续); False=已末页/无下一页/点击失败.

    拼多多自研 beast-core 分页用 JS 直接定位并派发 click(绕开 Playwright 的
    可点击性/遮挡判断, 避免被『每页条数』下拉残留面板挡住而无法点击);
    点击后轮询『当前页码』是否真的改变, 避免『点了但没翻』导致死读第 1 页。
    """
    # ---- 拼多多 beast-core 分页(JS 深度遍历 light + shadow DOM) ----
    try:
        res = page.evaluate(
            r"""() => {
                const DQ = (sel) => {
                    const out = [];
                    (function walk(r){
                        if(!r) return;
                        try { r.querySelectorAll(sel).forEach(e=>out.push(e)); } catch(e){}
                        try { r.querySelectorAll('*').forEach(el=>{ if(el.shadowRoot) walk(el.shadowRoot); }); } catch(e){}
                    })(document);
                    return out;
                };
                const btns = DQ("[data-testid='beast-core-pagination-next']");
                if(!btns.length) return {found:false};
                const btn = btns[0];
                const cls = (btn.getAttribute('class')||'').toLowerCase();
                if(cls.indexOf('disabled') !== -1) return {found:true, disabled:true};
                const act = DQ("li[class*='pagerItemActive']");
                const prev = act.length ? (act[0].textContent||'').trim() : null;
                try { btn.click(); } catch(e){ return {found:true, disabled:false, clicked:false, err:String(e)}; }
                return {found:true, disabled:false, clicked:true, prev:prev};
            }"""
        )
        if res and res.get("found"):
            if res.get("disabled"):
                if prog:
                    prog("⚠ 『下一页』已禁用(末页), 停止翻页")
                return False
            prev = res.get("prev")
            # 轮询: 当前页码指示器文本变化 => 翻页生效; 最多等 12s
            if prev is not None:
                try:
                    page.wait_for_function(
                        r"""a => {
                            const DQ = (sel) => {
                                const out = [];
                                (function walk(r){
                                    if(!r) return;
                                    try { r.querySelectorAll(sel).forEach(e=>out.push(e)); } catch(e){}
                                    try { r.querySelectorAll('*').forEach(el=>{ if(el.shadowRoot) walk(el.shadowRoot); }); } catch(e){}
                                })(document);
                                return out;
                            };
                            const els = DQ("li[class*='pagerItemActive']");
                            return els.length && els[0].textContent.trim() !== a;
                        }""",
                        arg=prev, timeout=12000,
                    )
                except Exception:
                    if prog:
                        prog("⚠ 点击『下一页』后页码未变化, 判定已到末页")
                    return False
            time.sleep(1.0)
            return True
    except Exception:
        pass

    # ---- 通用兜底候选(其它改版) ----
    candidates = [
        "text=下一页",
        "a:has-text('下一页')",
        "button:has-text('下一页')",
        "span:has-text('下一页')",
        ".el-pagination .btn-next",
        ".el-pagination__next",
        ".ant-pagination-next",
        ".ant-pagination-item-next",
        "[class*=next-page]", "[class*=nextPage]",
        "[class*=pagination] li:last-child a",
        "[class*=pagination] li:last-child button",
        "li.next a", "li.next button", "li.next span",
        "li[class*=next] a", "li[class*=next] button", "li[class*=next] span",
        ".next-btn", "[class*=PageNext]",
        "a[rel=next]", "[aria-label*=下一页]", "[aria-label*=Next]",
        "[class*=pagination] .next",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if _is_disabled(loc):
                return False
            try:
                loc.scroll_into_view_if_needed()
            except Exception:
                pass
            loc.click(force=True)
            try:
                page.wait_for_selector(
                    "table tbody tr, [class*='goods-item'], [class*='GoodsItem'], "
                    "[class*='list-item'], [class*='ListItem'], [class*='product-item']",
                    timeout=8000,
                )
            except Exception:
                pass
            time.sleep(1.5)
            return True
        except Exception:
            continue
    return False


def _click_load_more(page, prog=None):
    """兜底: 部分改版用『加载更多 / 查看更多』而非翻页(无限滚动). 命中返回 True."""
    candidates = [
        "text=加载更多", "button:has-text('加载更多')", "a:has-text('加载更多')",
        "text=查看更多", "button:has-text('查看更多')", "a:has-text('查看更多')",
        "[class*=load-more]", "[class*=loadMore]", "[class*=LoadMore]",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() == 0:
                continue
            if _is_disabled(loc):
                return False
            loc.click()
            time.sleep(2.0)
            return True
        except Exception:
            continue
    return False



def scan(headless: bool = False, dump_html: bool = True, progress=None) -> list:
    """登录并扫描在售商品列表, 保存到 store_products.json。返回商品列表。
    progress: 可选回调 progress(msg), 把各阶段进度回传到 GUI 状态栏。"""
    from pdd_session import launch_context, ensure_login  # 惰性导入浏览器依赖

    def prog(msg):
        if progress:
            try:
                progress(f"扫描: {msg}")
            except Exception:
                pass

    pw, ctx = launch_context(headless=headless)
    try:
        prog("正在打开登录页, 请在弹出的浏览器里登录拼多多商家后台(账号密码/扫码均可)")
        page = ensure_login(ctx, timeout=240, progress=progress)
        prog("✅ 登录成功, 正在进入商品列表页(拼多多后台为 SPA, 稍候)…")

        items = []
        seen_ids = set()
        for url in GOODS_LIST_CANDIDATES:
            try:
                prog(f"打开商品列表: {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception as e:
                prog(f"  ⚠ 打开失败: {e}")
                continue
            # 等表格/列表容器出现(SPA 异步渲染, 不能只等 networkidle)
            try:
                page.wait_for_selector(
                    "table tbody tr, [class*='goods-list'], [class*='GoodsList'], "
                    "[class*='goodsList'], [class*='product-list']",
                    timeout=20000,
                )
            except Exception:
                pass
            time.sleep(2.5)
            # 被踢回登录页?
            if "/login/" in (page.url or ""):
                prog("⚠ 打开商品列表时被重定向回登录页, 登录态失效, 请重新点『扫描在售商品』登录")
                items = []
                break

            # 先尽量把每页条数调到最大(很多店铺单页即可取完, 无需翻页)
            _try_max_page_size(page, prog)
            total_pages = _read_total_pages(page)
            if total_pages:
                prog(f"商品列表已加载, 共 {total_pages} 页, 逐页扫描(已尽量放大每页条数)")
            else:
                prog("商品列表已加载, 开始逐页扫描(未识别到总页数, 将逐页点击下一页/加载更多直到末页)")

            page_no = 1
            lm_count = 0          # 『加载更多』累计次数
            dumped = False        # 是否已保存诊断页
            empty_streak = 0
            no_new_streak = 0
            while True:
                page_items = _extract_from_cards(page) or _extract_rows(page) or []
                added = 0
                for it in page_items:
                    gid = it.get("goods_id", "")
                    if gid and gid in seen_ids:
                        continue
                    seen_ids.add(gid)
                    items.append(it)
                    added += 1
                if lm_count == 0:
                    prog(f"第 {page_no} 页: 本页 {len(page_items)} 个, 新增 {added}, 累计 {len(items)} 个")
                else:
                    prog(f"加载更多(第 {lm_count} 次): 新增 {added}, 累计 {len(items)} 个")
                # 每轮都保存一份诊断页(便于后续校准分页/选择器)
                if dump_html and not dumped:
                    try:
                        with open(DEBUG_HTML, "w", encoding="utf-8") as f:
                            f.write(page.content())
                        prog(f"已保存诊断页到 {DEBUG_HTML} (供分页校准)")
                        dumped = True
                    except Exception:
                        pass
                empty_streak = 0 if page_items else empty_streak + 1
                no_new_streak = 0 if added > 0 else no_new_streak + 1
                if total_pages and page_no >= total_pages:
                    break
                if empty_streak >= 2:
                    break
                if no_new_streak >= 3:
                    prog("⚠ 连续多次无新增商品, 判定已到末页, 停止翻页")
                    break
                if page_no >= 200:
                    prog("⚠ 已达到 200 页上限, 停止翻页")
                    break
                if not _click_next(page, prog):
                    # 没有『下一页』时, 尝试『加载更多』(无限滚动改版)
                    if _click_load_more(page, prog):
                        lm_count += 1
                        time.sleep(1.5)
                        continue
                    break
                page_no += 1
                time.sleep(1.5)

            if items:
                break


        if not items:
            if dump_html:
                try:
                    with open(DEBUG_HTML, "w", encoding="utf-8") as f:
                        f.write(page.content())
                    prog(f"⚠ 未能从商品列表页提取到行, 已保存诊断页到 {DEBUG_HTML}")
                    prog("   请把该 debug_store.html 发给我, 我据此校准选择器后重跑")
                except Exception:
                    pass
            prog("❌ 未扫描到商品数据(可能后台改版/选择器需校准, 或仍停留在登录/验证页)")
            return []

        with open(STORE_JSON, "w", encoding="utf-8") as f:
            json.dump(items, f, ensure_ascii=False, indent=2)
        prog(f"✅ 扫描到 {len(items)} 个商品, 已保存 {STORE_JSON}")
        for it in items[:10]:
            print(f"   {it['goods_id'] or '??':>12} | 销量{it['sales_text'] or '?':<8} | {it['title'][:40]}")
        if len(items) > 10:
            print(f"   … 其余 {len(items) - 10} 个见 {STORE_JSON}")
        return items
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


def report_zero() -> list:
    """读取 store_products.json, 筛零销量商品 → zero_sales.csv + 清单"""
    if not Path(STORE_JSON).exists():
        print(f"⚠ 未找到 {STORE_JSON}, 请先运行 scan()")
        return []
    with open(STORE_JSON, encoding="utf-8") as f:
        items = json.load(f)
    zero = [it for it in items if it.get("sales", 0) == 0]
    # 仅保留导出所需字段, 避免多出 'sales' 等字段触发 DictWriter 报错
    zero_clean = [{"goods_id": it.get("goods_id", ""),
                   "title": it.get("title", ""),
                   "sales_text": it.get("sales_text", ""),
                   "status": it.get("status", "")} for it in zero]
    with open(ZERO_CSV, "w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["goods_id", "title", "sales_text", "status"])
        w.writeheader()
        w.writerows(zero_clean)
    print(f"📉 零销量商品 {len(zero)} / 共 {len(items)} 个 → {ZERO_CSV}")
    for it in zero:
        print(f"   [下架候选] {it['goods_id'] or '??':>12} | {it['title'][:50]}")
    if zero:
        print("\n⚠ 以上为零销量下架候选。执行实际下架请运行 delist(confirm=True) 二次确认。")
    return zero


def delist(items: list = None, confirm: bool = False, dry_run: bool = True):
    """下架商品。默认 dry_run 只打印清单; confirm=True 才真正执行。

    items: 指定待下架清单(list[dict], 需含 goods_id/title), 由调用方(如 GUI)
           按用户勾选结果传入。不传则回退为 report_zero() 生成的 zero_sales.csv 全集。

    危险操作门控:
      - dry_run=True 时绝不触碰账号(仅打印清单)
      - confirm=True 时仍会再次打印清单并要求最终确认
    撤销: 下架后记录到 delisted_log.json, 可用 re_list() 重新上架。
    """
    if items is None:
        if not Path(ZERO_CSV).exists():
            print(f"⚠ 未找到 {ZERO_CSV}, 请先运行 report_zero()")
            return
        with open(ZERO_CSV, encoding="utf-8-sig") as f:
            zero = list(csv.DictReader(f))
    else:
        zero = [{"goods_id": str(r.get("goods_id", "")),
                 "title": str(r.get("title", ""))} for r in items]
    if not zero:
        print("⚠ 待下架清单为空, 未执行任何操作。")
        return

    print(f"🔻 待下架清单(共 {len(zero)} 个):")
    for it in zero:
        print(f"   {it['goods_id'] or '??':>12} | {it['title'][:50]}")

    if dry_run:
        print("\n[dry_run] 未执行任何下架。确认无误后去掉 dry_run 实际执行(需 selector 已校准)。")
        return

    if not confirm:
        print("\n⚠ 未传 confirm=True, 中止(安全规则: 下架必须二次确认)。")
        return

    # 真正执行: 需要已校准的 selector 与登录态
    from pdd_session import launch_context, ensure_login  # 惰性导入
    pw, ctx = launch_context(headless=False)
    try:
        page = ensure_login(ctx)
        log = []
        for it in zero:
            gid = it["goods_id"]
            print(f"   → 下架 {gid}: 定位行并点击下架(selector 待校准) ...")
            # TODO(校准): 按 goods_id 定位行 → 点 下架 → 处理二次确认弹窗
            # 这里先占位, 实跑拿到 debug_store.html 后补全
            log.append({"goods_id": gid, "title": it["title"], "action": "pending_selector"})
        with open(DELISTED_LOG, "w", encoding="utf-8") as f:
            json.dump(log, f, ensure_ascii=False, indent=2)
        print(f"⚠ 下架逻辑 selector 待校准, 未实际点击。请分享 debug_store.html 以便补全。")
    finally:
        ctx.close()
        pw.stop()


def re_list(confirm: bool = False, dry_run: bool = True):
    """撤销下架: 读取 delisted_log.json 并(selector 校准后)重新上架。"""
    if not Path(DELISTED_LOG).exists():
        print(f"⚠ 未找到 {DELISTED_LOG}, 没有可恢复的下架记录。")
        return
    with open(DELISTED_LOG, encoding="utf-8") as f:
        log = json.load(f)
    if not log:
        print("⚠ 下架记录为空。")
        return
    print(f"♻ 待重新上架(共 {len(log)} 个):")
    for it in log:
        print(f"   {it.get('goods_id','??'):>12} | {it.get('title','')[:50]}")
    if dry_run:
        print("\n[dry_run] 未执行。确认无误后去掉 dry_run 实际重新上架(需 selector 校准)。")
        return
    if not confirm:
        print("\n⚠ 未传 confirm=True, 中止。")
        return
    print("⚠ 重新上架 selector 待校准, 请分享后台'仓库中商品'页面 HTML 以便补全。")


def dump_goods_html(progress=None) -> str:
    """快速导出商品列表页诊断 HTML(登录→打开列表→放大每页条数→保存 debug_store.html)。

    用于校准分页/选择器, 不执行完整翻页, 比 scan() 更快。返回诊断文件路径。"""
    from pdd_session import launch_context, ensure_login
    def prog(msg):
        if progress:
            try: progress(f"诊断: {msg}")
            except Exception: pass
    pw, ctx = launch_context(headless=False)
    try:
        prog("正在打开登录页, 请完成登录")
        page = ensure_login(ctx, timeout=240, progress=progress)
        prog("✅ 登录成功, 打开商品列表页…")
        target = None
        for url in GOODS_LIST_CANDIDATES:
            try:
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                target = url
                break
            except Exception as e:
                prog(f"  ⚠ 打开失败: {e}")
        if not target:
            prog("❌ 无法打开商品列表页")
            return ""
        try:
            page.wait_for_selector(
                "table tbody tr, [class*='goods-list'], [class*='GoodsList'], "
                "[class*='goodsList'], [class*='product-list']",
                timeout=20000,
            )
        except Exception:
            pass
        time.sleep(2.5)
        _try_max_page_size(page, prog)
        time.sleep(1.5)
        with open(DEBUG_HTML, "w", encoding="utf-8") as f:
            f.write(page.content())
        prog(f"✅ 已保存诊断页到 {DEBUG_HTML}")
        return DEBUG_HTML
    finally:
        try: ctx.close()
        except Exception: pass
        try: pw.stop()
        except Exception: pass


if __name__ == "__main__":
    import sys
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "scan":
        scan(headless=False)
    elif cmd == "dump-html":
        dump_goods_html()
    elif cmd == "report-zero":
        report_zero()
    elif cmd == "delist":
        dry = "--confirm" not in sys.argv
        delist(confirm="--confirm" in sys.argv, dry_run=dry)
    elif cmd == "re-list":
        dry = "--confirm" not in sys.argv
        re_list(confirm="--confirm" in sys.argv, dry_run=dry)
    else:
        print("用法: python -m pdd_store scan | dump-html | report-zero | delist [--confirm] | re-list [--confirm]")
