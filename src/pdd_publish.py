#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pdd_publish.py —— 把上架清单逐条填写到拼多多商家后台的发布表单

【真实流程】(由 debug_category.html / debug_publish.html 真实 DOM 校准)
  第1步 /goods/category : 商品主图(本地上传) + 商品标题(60字符) + #bottomSubmitBtnId
                          「下一步, 完善商品信息」
  第2步 完整表单        : 轮播图 / 标题 / 详情图(本地上传) / 规格与库存表格 /
                          商品属性 / 运费模板(radio 卡片组, 后台已默认选中) / 提交

【字段定位】旧版按中文标签猜 input, 在真实表单里全部落空(控件 placeholder 只有
  "请输入")。本版改用稳定属性 data-tracking-click-viewid / data-testid / placeholder
  精确定位, React 兼容的原生 setter + input/change 事件写值, 结果如实回报。

【图片合规】只下载源站公开 http(s) 商品图, 校验真实 jpg/png 文件头与大小上限,
  合并全部主图与详情图去重后按平台上限(轮播 10 / 详情 50)上传; 不去水印、
  不合成、不伪装来源; 图片授权由商家自行确认。

【价格】单买价 = 建议售价 × 1.10, 拼单价 = 单买价 - 1~3 元且不低于按
  进货价+运费+佣金反算的保本线; 保本线顶穿价差时整体抬价, 始终保留价差。

【属性与运费】属性按页面 label 动态映射 1688 attributes, 品牌需商标授权故默认留空;
  运费模板是后台预先配置好的 radio 卡片组, 不能照搬 1688 的进货运费规则。

【安全】
  - publish() 默认 dry_run=True: 只填写预览, 绝不点提交。
  - 真实提交须显式 dry_run=False(GUI 端需勾选『真实发布』)。
  - 预览结束保留浏览器供核对, 关闭弹窗或超时后由发布线程自行关闭。
  - 核心字段未齐不提交, 避免残次上架。
"""
import json
import os
import re
import time
import tempfile
import threading
import urllib.request
from pathlib import Path

import paths as _paths
_paths.setup_env()
from paths import ROOT, SRC

HERE = SRC
DEBUG_PUBLISH = (ROOT / "debug_publish.html").as_posix()
DEBUG_CATEGORY = (ROOT / "debug_category.html").as_posix()
DEBUG_STEP2 = (ROOT / "debug_publish_step2.html").as_posix()
DEBUG_ATTR = (ROOT / "debug_attr_dropdown.html").as_posix()
PUBLISH_CFG = (ROOT / "publish_config.json").as_posix()

MMS_HOME = "https://mms.pinduoduo.com/"
CATEGORY_URL = MMS_HOME + "goods/category"
CREATE_URLS = [
    MMS_HOME + "goods/goods_publish",
    MMS_HOME + "goods/create",
    MMS_HOME + "goods/add",
]

DEFAULT_CFG = {
    "category_keyword": "",     # PDD 类目搜索词; 留空=自动用 1688 类目名/标题推断
    "category_path": [],        # 手工指定类目全路径(优先级最高)
    "freight_template": "",     # 留空=采用后台已选的默认运费模板
    "stock_cap": 5000,          # 每款库存上限(1688 库存常几十万, 不照搬; 代发填 5000 较稳)
    "default_stock": 1000,      # 无库存数据时的默认值(100 太少, 调到 1000)
    "max_carousel": 10,         # 轮播主图平台上限 10 张
    "max_detail": 50,           # 详情图平台上限 50 张(页面显示 /50张)
    "max_sku": 0,               # SKU 行数上限; 0=全量搬迁(按源站全部款数准备数据)
                                # 后台规格表过大/卡顿时可改 30 之类的小值
                                # 注: 实际能填多少行仍取决于 PDD 后台已添加的规格行数
    "image_max_bytes": 8388608, # 单张图片大小上限 8MB, 超了跳过
    "price_source": "sku",      # sku=按每款反算价 / single=统一用 suggest_price
    "single_price_ratio": 1.10, # 单买价 = 建议售价 × 该系数
    "group_discount": 2.0,      # 拼单价 = 单买价 - 1~3 元(默认 2)
    "min_net_margin": 0.08,     # 拼单价保本线: 至少保留 8% 净利率
    "advice_price_pad": 10.0,   # 商品参考价(必填) = 最大单买价 + 该值
    "auto_fill_attributes": True,   # 按 1688 属性自动填写商品属性
    "auto_fill_brand": False,       # 品牌需授权, 默认不自动填写
    "shipping_time": "",        # 发货时间承诺, 留空不填
    "submit_only_if_core_ok": True,
}

# 预览浏览器句柄: close_preview() 只置标志/发事件, 由发布线程自己关闭(Playwright
# sync 对象不能跨线程操作, 因此 GUI 主线程绝不直接调 ctx.close())。
_PREVIEW = {"pw": None, "ctx": None, "page": None, "evt": None,
            "closed": False, "released": threading.Event()}
_PREVIEW["released"].set()   # 初始无浏览器占用


# ---------------- 配置 ----------------
def _load_cfg() -> dict:
    cfg = dict(DEFAULT_CFG)
    try:
        if os.path.exists(PUBLISH_CFG):
            with open(PUBLISH_CFG, encoding="utf-8") as f:
                cfg.update(json.load(f) or {})
    except Exception:
        pass
    return cfg


def _save_cfg(cfg: dict):
    try:
        merged = dict(DEFAULT_CFG)
        merged.update(cfg or {})
        with open(PUBLISH_CFG, "w", encoding="utf-8") as f:
            json.dump(merged, f, ensure_ascii=False, indent=2)
    except Exception:
        pass


def begin_preview():
    """由 GUI 主线程在启动发布线程前调用: 重置预览状态。"""
    _PREVIEW["closed"] = False
    _PREVIEW["evt"] = threading.Event()
    _PREVIEW.update({"pw": None, "ctx": None, "page": None})


def close_preview():
    """通知发布线程结束预览并关闭浏览器(线程安全, 不触碰 Playwright 对象)。"""
    _PREVIEW["closed"] = True
    evt = _PREVIEW.get("evt")
    if evt is not None:
        try:
            evt.set()
        except Exception:
            pass


def _release_preview(ctx=None, pw=None):
    """仅由发布线程调用: 显式传入自己创建的句柄。

    注意: 绝不清空 _PREVIEW['evt'] —— 上一轮收尾若晚于下一轮 begin_preview(),
    清空 evt 会让下一轮的关闭信号丢失, 浏览器只能等 10 分钟超时才退出。
    """
    if ctx is None and pw is None:
        ctx, pw = _PREVIEW.get("ctx"), _PREVIEW.get("pw")
    _PREVIEW.update({"pw": None, "ctx": None, "page": None})
    try:
        if ctx:
            ctx.close()
    except Exception:
        pass
    try:
        if pw:
            pw.stop()
    except Exception:
        pass
    _PREVIEW["released"].set()


# ---------------- 图片(合规: 只取源站公开商品图, 不改图/不去水印) ----------------
def _norm_urls(items) -> list:
    """把 1688 图片字段统一成 URL 列表, 去缩略图后缀、去占位图、按原图去重。"""
    from sku_map import clean_img_url
    out, seen = [], set()
    for x in (items or []):
        u = x.get("url") if isinstance(x, dict) else (x if isinstance(x, str) else "")
        cu = clean_img_url(u)
        if not cu:
            continue
        key = cu.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        out.append(cu)
    return out


def _real_image_ext(data: bytes) -> str:
    """按文件头判断真实图片格式。拼多多只收 jpg/png, webp/其它一律跳过。"""
    if data[:3] == b"\xff\xd8\xff":
        return ".jpg"
    if data[:8] == b"\x89PNG\r\n\x1a\n":
        return ".png"
    return ""


def _download_image(url: str, dest_dir: str, max_bytes: int = 8388608) -> str:
    os.makedirs(dest_dir, exist_ok=True)
    req = urllib.request.Request(url, headers={
        "User-Agent": "Mozilla/0.5 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
                      "(KHTML, like Gecko) Chrome/120.0 Safari/537.36",
        "Referer": "https://www.1688.com/",
    })
    with urllib.request.urlopen(req, timeout=30) as resp:
        data = resp.read(int(max_bytes) + 1)
    if not data or len(data) < 512:
        raise ValueError("空图片或占位图")
    if len(data) > int(max_bytes):
        raise ValueError("超过单张大小上限")
    ext = _real_image_ext(data)
    if not ext:
        raise ValueError("非 jpg/png 真实图片")
    path = os.path.join(dest_dir, "pdd_img_%d%s" % (int(time.time() * 1000000), ext))
    with open(path, "wb") as f:
        f.write(data)
    return path


def _download_many(urls, limit, prog=None, max_bytes=8388608):
    """遍历全部候选 URL, 直到凑够 limit 张可用图片; 单张失败自动跳过并试下一张。"""
    out, skipped = [], 0
    cap = int(limit or 0)
    for u in (urls or []):
        if len(out) >= cap:
            break
        try:
            out.append(_download_image(u, tempfile.gettempdir(), max_bytes))
        except Exception:
            skipped += 1
    if prog and skipped:
        prog(f"    ⚠ {skipped} 张图片下载失败/非 jpg-png/超限, 已跳过去试下一张")
    return out


def _image_pool(c: dict, cfg: dict, prog=None):
    """把 1688 的所有可用图片组织成 (轮播图候选, 详情图候选, 说明)。

    合规边界: 只合并源站公开 http(s) 商品图, 不去水印/不合成/不伪装来源;
    去重后按平台上限(轮播 10 / 详情 50)截断, 超出部分如实汇报不静默丢弃。
    详情图 = 全部主图(含首图) + 详情图, 不再像旧版那样 main[1:] 丢掉第一张。
    """
    main = _norm_urls(c.get("images") or [])
    detail = _norm_urls(c.get("detail_images") or [])
    cap_m = max(1, min(10, int(cfg.get("max_carousel") or 10)))
    cap_d = max(1, min(50, int(cfg.get("max_detail") or 50)))
    merged, seen = [], set()
    for u in main + detail:
        key = u.split("?")[0]
        if key in seen:
            continue
        seen.add(key)
        merged.append(u)
    main_sel = main[:cap_m]
    detail_sel = merged[:cap_d]
    src = ("主图 %d 张 + 详情图 %d 张" % (len(main), len(detail))) if detail else \
          ("仅主图 %d 张(源数据无详情图, 已用全部主图回填)" % len(main))
    note = (f"{src}; 去重后 {len(merged)} 张; 本次候选 轮播 {len(main_sel)}/{cap_m}, "
            f"详情 {len(detail_sel)}/{cap_d}"
            + (f"; 平台详情上限 50 张, 其余 {len(merged) - len(detail_sel)} 张未上传"
               if len(merged) > len(detail_sel) else ""))
    if prog:
        prog(f"  图片合规汇总: {note}")
    return main_sel, detail_sel, note


# ---------------- 诊断 ----------------
def _dump(page, path=DEBUG_PUBLISH):
    try:
        with open(path, "w", encoding="utf-8") as f:
            f.write(page.content())
        return path
    except Exception:
        return ""


def _page_text(page, limit=20000):
    try:
        return re.sub(r"\s+", " ", page.locator("body").inner_text())[:limit]
    except Exception:
        try:
            return re.sub(r"<[^>]+>", " ", page.content())[:limit]
        except Exception:
            return ""


# ---------------- 稳定控件(由真实 DOM 校准, 不再靠中文文案猜) ----------------
# 第1步与第2步的通配: 两步的标题框 placeholder 相同, 主图上传控件 viewid 不同
S_TITLE = 'input[placeholder^="商品标题组成"]'
S_CAROUSEL = ('input[type=file][data-tracking-click-viewid="local_upload"], '
              'input[type=file][data-tracking-click-viewid="carousel_img_localfile_upload"]')
S_DETAIL = 'input[type=file][data-tracking-click-viewid="detail_img_localfile_upload"]'
S_ADVICE_PRICE = 'input[data-tracking-click-viewid="goods_advice_price"]'
# 运费模板真实控件是 radio 卡片组, 不是「请选择运费模板」下拉框(旧版找错控件)
S_FREIGHT = '[id="service.is_default_template_id"]'
S_SKU_ROW = '[data-testid="beast-core-table-body-tr"]'
S_ATTR_ITEM = '[data-testid="beast-core-form-item"][id^="basic.propertys."]'

_SET_BY_JS = """(args) => {
  const els = Array.from(document.querySelectorAll(args.sel));
  const el = els[args.i || 0];
  if (!el) return {ok: false, count: els.length};
  try {
    const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
    if (d && d.set) d.set.call(el, String(args.val)); else el.value = String(args.val);
  } catch (e) { el.value = String(args.val); }
  el.dispatchEvent(new Event('input',  {bubbles: true}));
  el.dispatchEvent(new Event('change', {bubbles: true}));
  el.dispatchEvent(new Event('blur',   {bubbles: true}));
  return {ok: true, value: el.value, count: els.length};
}"""


_READ_JS = """(args) => {
  const els = Array.from(document.querySelectorAll(args.sel));
  return {count: els.length, values: els.slice(0, 10).map(e => String(e.value ?? ''))};
}"""

# 主图上传后页面会显示计数, 两步文案不同(由真实诊断页校准):
#   第1步 /goods/category → 「上传图片 (0/10)」
#   第2步 完整表单        → 「已上传 0 /10张」
# 用它复核上传接口是否真的成功, 而不是把 set_input_files 返回就当成功。
_UPLOAD_COUNT_JS = r"""() => {
  const t = document.body.innerText || '';
  let m = t.match(/已上传\s*(\d+)\s*\/\s*10/);
  if (m) return parseInt(m[1], 10);
  m = t.match(/上传图片\s*\(\s*(\d+)\s*\/\s*10\s*\)/);
  if (m) return parseInt(m[1], 10);
  return -1;
}"""

# 第2步详情图计数: 「已上传 0 /50张」
_DETAIL_COUNT_JS = r"""() => {
  const t = document.body.innerText || '';
  const m = t.match(/已上传\s*(\d+)\s*\/\s*50/);
  return m ? parseInt(m[1], 10) : -1;
}"""


def _set_by(page, sel, value, i=0):
    try:
        r = page.evaluate(_SET_BY_JS, {"sel": sel, "val": value, "i": i})
        return bool(r.get("ok")), r
    except Exception as e:
        return False, {"err": str(e)[:80]}


def _read_by(page, sel):
    """回读控件当前值。React 重渲染常把 JS 写进去的值冲掉, 必须以回读为准。"""
    try:
        return page.evaluate(_READ_JS, {"sel": sel})
    except Exception:
        return {"count": 0, "values": []}


def _wait_uploaded(page, timeout=12, js=None):
    """轮询图片上传结果。返回 (数量, 是否明确确认):
       n>0 已成功 / n==0 明确失败 / n==-1 页面没有该计数文案(无法复核)。
       js 可换成本模块的 _DETAIL_COUNT_JS 复核详情图。
    """
    script = js or _UPLOAD_COUNT_JS
    n = -1
    for _ in range(int(timeout)):
        try:
            n = page.evaluate(script)
        except Exception:
            n = -1
        if isinstance(n, int) and n > 0:
            time.sleep(1.0)
            return n, True
        if isinstance(n, int) and n == 0:
            time.sleep(1.0)
            continue          # 上传可能还在进行, 再等等
        break                 # 找不到计数文案, 别死等
    return n, (n is not None and n > 0)


def _upload_by(page, sel, paths, prog=None):
    if not paths:
        return False, "无可用图片文件"
    try:
        loc = page.locator(sel).first
        if loc.count() == 0:
            return False, "未找到上传控件"
        loc.set_input_files(paths)
        return True, "已选 %d 张" % len(paths)
    except Exception:
        done = 0
        for p in paths[:10]:
            try:
                page.locator(sel).first.set_input_files(p)
                done += 1
                time.sleep(0.3)
            except Exception:
                break
        if done:
            return True, "逐张已选 %d 张" % done
        return False, "上传控件拒绝文件"


# 规格与库存表格: 每行 td 顺序 = 库存 / 拼单价 / 单买价 / 规格编码 / 商品编码 / 状态
# 【实测教训 1108】beast-core 的 inputNumber 是受控组件: 写入任意一个字段后都会
# 同步重渲染整行。旧版在一次 evaluate 里连续写「库存→单买价→拼单价」, 后面复用
# 的还是渲染前抓到的 DOM 引用, 于是写进了脱离文档的老节点 —— 页面表现为
# 拼单价 57.26 有值、单买价仍显示「请输入」(debug_publish.html 快照亦如此)。
# 现改为: 每个字段单独一次 evaluate, 每次都重新查询 DOM, 中间留时间给 React 提交。
_FILL_SKU_JS = """(args) => {
  const rows = Array.from(document.querySelectorAll('[data-testid="beast-core-table-body-tr"]'));
  if (!rows.length) return {ok: false, rows: 0, filled: 0, detail: []};
  const phase = args.phase || 'all';
  const setVal = (el, v) => {
    if (!el) return null;
    try {
      const d = Object.getOwnPropertyDescriptor(Object.getPrototypeOf(el), 'value');
      if (d && d.set) d.set.call(el, String(v)); else el.value = String(v);
    } catch (e) { el.value = String(v); }
    el.dispatchEvent(new Event('input',  {bubbles: true}));
    el.dispatchEvent(new Event('change', {bubbles: true}));
    el.dispatchEvent(new Event('blur',   {bubbles: true}));
    return String(el.value ?? '');
  };
  const out = [];
  const n = Math.min(args.items.length, rows.length);
  for (let i = 0; i < n; i++) {
    const row = rows[i], it = args.items[i];
    const tds = Array.from(row.querySelectorAll('td'));
    const qtyTd = tds.find(td => (td.className || '').indexOf('quantity') >= 0) || tds[0];
    const qty = qtyTd ? qtyTd.querySelector('input') : null;
    const prices = Array.from(row.querySelectorAll('.sku-beast-price-input-container input'));
    const gEl = prices[0] || (tds[1] && tds[1].querySelector('input'));
    const sEl = prices[1] || (tds[2] && tds[2].querySelector('input'));
    const rec = {stock: null, group: null, single: null,
                 want: {stock: String(it.stock), group: String(it.group), single: String(it.single)}};
    // 一次只写一个字段; phase='all'/'base' 仅保留给离线自测兼容
    if (phase === 'all' || phase === 'base' || phase === 'stock')
      rec.stock = setVal(qty, it.stock);
    if (phase === 'all' || phase === 'base' || phase === 'single')
      rec.single = setVal(sEl, it.single);
    if (phase === 'all' || phase === 'group')
      rec.group = setVal(gEl, it.group);
    out.push(rec);
  }
  const same = (x, k) => x[k] === x.want[k];
  const verified = out.filter(x => {
    if (phase === 'stock')  return same(x, 'stock');
    if (phase === 'single') return same(x, 'single');
    if (phase === 'group')  return same(x, 'group');
    if (phase === 'base')   return same(x, 'stock') && same(x, 'single');
    return same(x, 'stock') && same(x, 'group') && same(x, 'single');
  }).length;
  return {ok: verified > 0, rows: rows.length, filled: n, phase: phase,
          verified: verified, detail: out};
}"""

# 独立回读整行三列, 用于最后确认(不依赖写入时的返回值)
_READ_SKU_JS = """() => {
  const rows = Array.from(document.querySelectorAll('[data-testid="beast-core-table-body-tr"]'));
  return rows.slice(0, 5).map(row => {
    const tds = Array.from(row.querySelectorAll('td'));
    const qtyTd = tds.find(td => (td.className || '').indexOf('quantity') >= 0) || tds[0];
    const q = qtyTd ? qtyTd.querySelector('input') : null;
    const prices = Array.from(row.querySelectorAll('.sku-beast-price-input-container input'));
    return {
      stock:  q ? String(q.value ?? '') : '',
      group:  prices[0] ? String(prices[0].value ?? '') : '',
      single: prices[1] ? String(prices[1].value ?? '') : ''
    };
  });
}"""

# 真实运费模板 DOM(由 debug_publish.html 校准):
#   <div data-testid="beast-core-form-item" id="service.is_default_template_id">
#     <div data-testid="beast-core-radioGroup">
#       <label data-testid="beast-core-radio" data-checked="true">新疆西藏收费默认模板 推荐</label>
#       <label ...>其他模板</label>  <label ...>同城配送</label>
# 后台进入表单时已默认选中第一个模板, 所以通常根本不需要"选模板"。
_FREIGHT_STATE_JS = """() => {
  const fi = document.querySelector('[id="service.is_default_template_id"]');
  if (!fi) return {ok: false, reason: '运费模板表单项未找到'};
  const radios = Array.from(fi.querySelectorAll('[data-testid="beast-core-radio"]'));
  const texts = radios.map(r => (r.innerText || r.textContent || '').replace(/\\s+/g, ' ').trim());
  const checked = radios.find(r => r.getAttribute('data-checked') === 'true');
  return {ok: true, count: radios.length, options: texts,
          checked: checked ? (checked.innerText || checked.textContent || '').replace(/\\s+/g, ' ').trim() : ''};
}"""

_FREIGHT_PICK_JS = """(args) => {
  const fi = document.querySelector('[id="service.is_default_template_id"]');
  if (!fi) return {ok: false, reason: '运费模板表单项未找到'};
  const radios = Array.from(fi.querySelectorAll('[data-testid="beast-core-radio"]'));
  if (!radios.length) return {ok: false, reason: '未找到运费模板单选项'};
  const want = (args.name || '').trim();
  const txt = r => (r.innerText || r.textContent || '').replace(/\\s+/g, ' ').trim();
  let hit = null;
  if (want) {
    hit = radios.find(r => txt(r).indexOf(want) >= 0 || want.indexOf(txt(r)) >= 0);
    if (!hit) {
      // 指定模板名不在默认卡片里: 只能切到「其他模板」入口, 具体模板需人工在浮层里选
      const other = radios.find(r => txt(r).indexOf('其他模板') >= 0);
      if (other) { try { other.click(); } catch (e) {} }
      return {ok: false, reason: '指定模板『' + want + '』需在「其他模板」浮层里手动选择'};
    }
  } else {
    hit = radios.find(r => r.getAttribute('data-checked') === 'true')
       || radios.find(r => txt(r).indexOf('同城配送') < 0) || radios[0];
  }
  try { hit.click(); } catch (e) { return {ok: false, reason: '运费模板点击失败'}; }
  return {ok: true, text: txt(hit), matched: !!want};
}"""

# ---- 商品属性(由真实 DOM 校准: basic.propertys.N.value) ----
# 每个表单项的 label 类名带版本后缀, 用 [class*=Form_itemLabelContent] 兜住。
_ATTR_SCAN_JS = """() => {
  const items = Array.from(document.querySelectorAll(
      '[data-testid="beast-core-form-item"][id^="basic.propertys."]'));
  return items.map(it => {
    const lab = it.querySelector('[class*="Form_itemLabelContent"]');
    const label = lab ? (lab.innerText || lab.textContent || '')
                        .replace(/重要/g, '').replace(/\\s+/g, ' ').trim() : '';
    const inp = it.querySelector('input');
    return {id: it.id, label: label,
            ph: inp ? (inp.getAttribute('placeholder') || '') : '',
            testid: inp ? (inp.getAttribute('data-testid') || '') : '',
            value: inp ? String(inp.value || '') : ''};
  });
}"""

# 打开下拉前给现有 DOM 打标记。浮层是异步新建的节点, 只有"没有标记"的
# 节点才算真实选项 —— 避免把属性区里已有的静态说明文字(如「女」)误当成选项点掉。
_ATTR_MARK_JS = """() => {
  document.querySelectorAll('*').forEach(e => e.setAttribute('data-pdd-mark', '1'));
  return true;
}"""

# 点开某个属性下拉; 浮层选项由 _ATTR_PICK_JS 异步读取
_ATTR_OPEN_JS = """(args) => {
  const it = document.getElementById(args.id);
  if (!it) return {ok: false};
  const inp = it.querySelector('input');
  if (!inp) return {ok: false};
  try { inp.click(); } catch (e) {}
  try { (inp.closest('[data-testid="beast-core-select"]') || inp.parentElement).click(); } catch (e) {}
  return {ok: true};
}"""

# 在浮层里按候选值模糊匹配并点选; 只点能明确匹配到的叶子节点, 不做猜测
_ATTR_PICK_JS = """(args) => {
  const bad = ['请选择', '请输入', '加载中', '暂无数据', '无数据', '点击申请', '没有合适'];
  const it = document.getElementById(args.id);
  const anchor = it ? it.querySelector('input') : null;
  const ar = anchor ? anchor.getBoundingClientRect() : null;
  const sels = ['[data-testid="beast-core-option"]', '[data-testid*="option"]',
                '[data-testid*="dropdown"] *', '[role="option"]', '[role="listbox"] *',
                '[class*="Option"]', '[class*="option"]', '[class*="MenuItem"]',
                '[class*="select-item"]', 'li', 'div', 'span'];
  let all = [], seen = new Set();
  for (const s of sels) {
    for (const e of document.querySelectorAll(s)) {
      if (!seen.has(e)) { seen.add(e); all.push(e); }
    }
  }
  // 只认下拉展开后新建的节点(没有 data-pdd-mark)
  const fresh = all.filter(e => !e.hasAttribute('data-pdd-mark'));
  const isFresh = fresh.length > 0;
  if (isFresh) {
    all = fresh;
  } else {
    // 浮层若是预渲染(只是改 CSS 显隐), 就限定在可见的浮层容器内找
    const layers = Array.from(document.querySelectorAll(
        '[data-testid*="dropdown"], [data-testid*="option"], [role="listbox"], ' +
        '[class*="Dropdown"], [class*="dropdown"], [class*="Popup"], [class*="popover"]'))
      .filter(e => { const r = e.getBoundingClientRect(); return r.width > 60 && r.height > 20; });
    if (!layers.length) {
      return {ok: false, reason: '下拉浮层未展开(未检测到新增选项节点)'};
    }
    all = layers.flatMap(L => Array.from(L.querySelectorAll('*')));
  }
  const norm = t => (t || '').replace(/[\\s()（）·、,，\\/【】\\[\\]]/g, '').toLowerCase();
  const wants = (args.cands || []).map(norm).filter(Boolean);
  const vis = all.map(e => ({e: e, t: (e.innerText || e.textContent || '').trim()}))
    .filter(x => {
      if (!x.t || x.t.length > 30 || bad.some(b => x.t.indexOf(b) >= 0)) return false;
      if (x.e.children.length > 5) return false;   // 允许带勾选图标的选项节点
      const r = x.e.getBoundingClientRect();
      if (r.width < 20 || r.height < 10 || r.height > 120) return false;
      if (r.top < -20 || r.top > window.innerHeight + 20) return false;
      // 预渲染浮层才需按位置收紧; 新增节点本身就是浮层, 不必再卡相对位置
      if (!isFresh && ar && (r.top < ar.top - 500 || r.top > ar.bottom + 500)) return false;
      if (!isFresh && ar && (r.right < ar.left - 500 || r.left > ar.right + 500)) return false;
      return true;
    });
  // 打分: 文本完全相同 > 互相包含; 同时优先靠近下拉框的浮层节点
  const scored = vis.map(x => {
    const t = norm(x.t);
    let s = 0;
    wants.forEach((w, i) => {
      if (!w) return;
      if (t === w) s = Math.max(s, 100);
      else if (t.indexOf(w) >= 0 || w.indexOf(t) >= 0) s = Math.max(s, 60 - i);
    });
    if (s > 0 && ar) {
      const r = x.e.getBoundingClientRect();
      s += Math.max(0, 20 - Math.abs(r.top - ar.bottom) / 40);
    }
    return {e: x.e, t: x.t, s: s};
  }).filter(x => x.s >= 55).sort((a, b) => b.s - a.s);
  if (!scored.length) {
    return {ok: false, reason: '下拉里没有匹配到可用选项',
            seen: vis.slice(0, 10).map(x => x.t)};
  }
  const hit = scored[0];
  try { hit.e.click(); } catch (e) { return {ok: false, reason: '选项点击失败'}; }
  return {ok: true, text: hit.t, score: Math.round(hit.s)};
}"""

_SUBMIT_JS = """() => {
  const kws = ['提交并上架', '确认发布', '立即发布', '发布商品', '提交审核', '提交', '发布'];
  const bad = ['保存草稿', '加载中', '下一步', '取消'];
  const btns = Array.from(document.querySelectorAll('button'));
  for (const k of kws) {
    for (const b of btns) {
      if (b.disabled) continue;
      const t = (b.innerText || b.textContent || '').trim();
      if (!t || t.length > 20 || bad.some(x => t.indexOf(x) >= 0)) continue;
      if (t.indexOf(k) >= 0) { b.click(); return {ok: true, text: t}; }
    }
  }
  return {ok: false};
}"""

_NEXT_JS = """() => {
  const byId = document.querySelector('#bottomSubmitBtnId');
  if (byId) { byId.click(); return {ok: true, text: (byId.innerText || '').trim()}; }
  const kws = ['下一步, 完善商品信息', '下一步，完善商品信息', '完善商品信息', '下一步'];
  for (const el of document.querySelectorAll('button, [data-testid*=button], a, div[role=button]')) {
    const t = (el.innerText || el.textContent || '').trim();
    if (t && t.length < 30 && kws.some(k => t.indexOf(k) !== -1)) {
      try { el.click(); return {ok: true, text: t}; } catch (e) {}
    }
  }
  return {ok: false};
}"""

_CLICK_JS = """(args) => {
  const kws = args.kws.map(k => String(k));
  const els = Array.from(document.querySelectorAll(
      'button, a, li, div[role=button], span[role=button], [data-testid*=button]'));
  for (const k of kws) {
    for (const el of els) {
      const t = (el.innerText || el.textContent || '').trim();
      if (t && t.length < 40 && t.indexOf(k) !== -1) {
        try { el.click(); return {ok: true, text: t}; } catch (e) {}
      }
    }
  }
  return {ok: false};
}"""


def _click_text(page, kws, prog=None):
    try:
        return page.evaluate(_CLICK_JS, {"kws": kws})
    except Exception:
        return {"ok": False}


# ---------------- 发布向导阶段 ----------------
_STEP2_KEYS = ("运费模板", "拼团价", "商品规格", "商品详情", "总库存", "单买价", "市场价格")


def _page_stage(page):
    """step1 / step2 / list / unknown。可见文字 + 原始 HTML 双重判定。

    顺序要点: 先判 step2(_STEP2_KEYS 更具体), 后判 step1。否则第2步完整表单
    里既含『商品标题』又含底部『下一步』按钮时, 会被误判回 step1。
    """
    t = _page_text(page)
    try:
        raw = page.content()
        raw = re.sub(r"<script[\s\S]*?</script>", " ", raw, flags=re.I)
        raw = re.sub(r"<style[\s\S]*?</style>", " ", raw, flags=re.I)
        raw = re.sub(r"<[^>]+>", " ", raw)
        raw = re.sub(r"\s+", " ", raw)
    except Exception:
        raw = ""
    blob = (t or "") + " " + (raw or "")
    if not blob.strip():
        return "unknown"
    # step2 优先: 运费模板/商品规格/单买价/商品详情 等只在完整表单出现
    if any(k in blob for k in _STEP2_KEYS):
        return "step2"
    if "商品标题" in blob and any(k in blob for k in ("下一步", "完善商品信息")):
        return "step1"
    if "发布新商品" in blob and "上传图片" in blob:
        return "step1"
    return "unknown"


def _wait_form_ready(page, timeout=20):
    """等完整表单渲染: 运费模板 radio 组 + 规格表格行 + 属性项, 且不在加载中。

    旧版等的是根本不存在的『请选择运费模板』下拉框, 每次都白等 20 秒。
    """
    for _ in range(int(timeout)):
        try:
            r = page.evaluate("""() => {
              const freight = !!document.querySelector('[id="service.is_default_template_id"]');
              const rows = document.querySelectorAll('[data-testid="beast-core-table-body-tr"]').length;
              const attrs = document.querySelectorAll(
                  '[data-testid="beast-core-form-item"][id^="basic.propertys."]').length;
              const loading = Array.from(document.querySelectorAll('button'))
                  .some(b => !b.disabled && (b.innerText || '').trim() === '加载中');
              return {freight: freight, rows: rows, attrs: attrs, loading: loading};
            }""")
            if r.get("rows", 0) > 0 and not r.get("loading") and \
                    (r.get("freight") or r.get("attrs", 0) > 0):
                time.sleep(1.5)
                return True
        except Exception:
            pass
        time.sleep(1.0)
    return False


def _open_create_form(page, c, cfg, prog=None):
    """打开发布向导第1步。返回 (ok, msg, stage)。"""
    try:
        page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        return False, f"发布页打开失败: {str(e)[:60]}", "unknown"
    try:
        page.wait_for_selector("input[type=text], input[type=file]", timeout=15000)
    except Exception:
        pass
    time.sleep(2.0)
    _dump(page, DEBUG_CATEGORY)
    if "/login" in (page.url or ""):
        return False, "登录态失效, 被重定向到登录页", "unknown"
    stage = _page_stage(page)
    if stage in ("step1", "step2"):
        return True, ("第1步: 商品主图 + 商品标题" if stage == "step1"
                      else "已在完整信息表单"), stage
    for k in ("发布新商品", "发布商品"):
        if _click_text(page, [k]).get("ok"):
            time.sleep(2.0)
            break
    stage = _page_stage(page)
    if stage in ("step1", "step2"):
        return True, "经『发布新商品』入口进入第1步", stage
    for url in CREATE_URLS:
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=20000)
            time.sleep(2.0)
            stage = _page_stage(page)
            if stage in ("step1", "step2"):
                return True, f"经备用入口进入({url})", stage
        except Exception:
            continue
    return False, f"未能进入发布向导(当前页面类型: {stage})", stage


def _capture_validation(page):
    """点『下一步』后抓 PDD 的拦截提示。

    多半是瞬时 toast(校验失败/图片上传中/请选择类目等), 25 秒后的快照抓不到。
    这里在前 8 秒高频轮询 body 文本 + 扫描 toast/红字容器, 把出现的提示原样返回。
    """
    try:
        kw = re.compile(r"(请|必填|不通过|未通过|校验|不能|无效|格式|上传中|审核|"
                        r"稍候|不符合|限制|错误|失败|类目|选择)")
        snippets = []
        for _ in range(16):                       # 约 8 秒
            t = page.evaluate("() => document.body.innerText || ''") or ""
            t = re.sub(r"\s+", " ", t)
            for seg in re.split(r"[。\n\r]", t):
                seg = seg.strip()
                if 2 <= len(seg) <= 60 and kw.search(seg):
                    snippets.append(seg)
            time.sleep(0.5)
        red = page.evaluate("""() => {
          const sel = ('[class*=toast],[class*=Toast],[class*=error],'
                       '[class*=Error],[class*=warn],[class*=Warn],'
                       '[class*=tip],[role=alert]');
          return Array.from(document.querySelectorAll(sel))
                     .map(e => (e.innerText || '').trim()).filter(Boolean).slice(0, 8);
        }""") or []
        out, seen = [], set()
        for s in (snippets + list(red)):
            s = s.strip()
            if s and s not in seen and len(s) <= 80:
                seen.add(s); out.append(s)
        return out[:8]
    except Exception:
        return []


def _goto_step2(page, prog=None):
    """第1步 → 点『下一步, 完善商品信息』 → 等完整表单。返回 (stage, msg)。

    稳健化: 用 Playwright 真实(受信任)点击, 最多重试 3 次(应对『图片上传中/
    审核中』这类瞬时拦截); 每次未进入第2步都实时抓取 PDD 校验提示写进日志。
    """
    last_msg = "点击后仍未进入完整表单"
    for attempt in range(3):
        try:
            # 优先真实点击 #bottomSubmitBtnId(本页它就是『下一步』); 退化: 文本匹配
            try:
                page.locator("#bottomSubmitBtnId").click(timeout=4000)
                clicked = {"ok": True, "text": "下一步"}
            except Exception:
                clicked = page.evaluate(_NEXT_JS)
                if not clicked.get("ok"):
                    loc = page.locator('button:has-text("下一步")')
                    if loc.count() > 0:
                        loc.first.click(timeout=4000)
                        clicked = {"ok": True, "text": "下一步"}
        except Exception as e:
            return "unknown", f"点击下一步异常: {str(e)[:60]}"
        if not clicked.get("ok"):
            return "unknown", "未找到『下一步, 完善商品信息』按钮"
        if prog:
            prog(f"  已点『下一步』(第{attempt + 1}次), 等待完整表单加载…")
        # 最长等 30s 看是否真正进入第2步
        reached = False
        for _ in range(30):
            time.sleep(1.0)
            if _page_stage(page) == "step2":
                reached = True
                break
            try:
                if page.locator(S_FREIGHT).count() > 0:
                    reached = True
                    break
            except Exception:
                pass
        if reached:
            _dump(page, DEBUG_STEP2)
            return "step2", "已进入完整信息表单(主图/详情图/规格价库存/运费模板)"
        msgs = _capture_validation(page)
        last_msg = ("；".join(msgs)) if msgs else \
            "无明确提示(多半是第1步主图/标题/类目未通过校验, 或图片仍在审核中)"
        if prog:
            prog(f"  ⚠ 第{attempt + 1}次点『下一步』未进入第2步: {last_msg}")
        if attempt < 2:
            time.sleep(1.5)
    _dump(page, DEBUG_STEP2)
    return "step1", (f"多次点击『下一步』均被拦下 —— {last_msg}；"
                     f"页面快照已存 {DEBUG_STEP2}")


# ---------------- 商品属性 ----------------
# PDD 属性 label → 1688 attributes 的候选 key(按优先级)。类目不同属性会变,
# 运行时按页面 label 动态匹配, 不硬编码 basic.propertys.N 的下标。
ATTR_MAP = {
    "品牌": ["品牌"],
    "材质": ["材质", "面料名称", "面料成分", "里料名称", "主要材质"],
    "有无胸垫钢托": ["是否带钢拖", "是否带钢托", "胸垫钢托"],
    "产品类型": ["产品类型", "规格类型", "款式", "品类"],
    "适用性别": ["适用性别", "适用人群"],
    "袖长": ["袖长"],
    "流行元素": ["流行元素", "图案风格", "风格", "图案"],
    "款式": ["款式", "版型", "风格"],
    "货号": ["货号", "款号"],
    "商品资质": ["商品资质", "资质"],
}

# 1688 与拼多多枚举值常有措辞差异; 只在明确等价时才扩展, 不做语义猜测
ATTR_SYNONYMS = {
    "带胸垫无钢托": ["有胸垫无钢托", "带胸垫", "无钢托"],
    "无钢托无胸垫": ["无胸垫无钢托", "无胸垫"],
    "有钢托": ["带钢托", "钢托"],
    "锦纶": ["聚酰胺纤维", "尼龙"],
    "聚酰胺纤维(锦纶)": ["锦纶", "聚酰胺纤维", "尼龙"],
    "聚酯纤维（涤纶）": ["涤纶", "聚酯纤维"],
    "女": ["女款", "女性", "女士"],
    "男": ["男款", "男性", "男士"],
    "成年": ["成人", "成年人"],
    "长袖": ["长袖"],
    "纯色": ["纯色"],
}


def _attr_values(attrs: dict, label: str) -> list:
    """取 1688 属性里能用于该 PDD 属性的候选值(去掉多值逗号串里的噪声)。"""
    out = []

    def add(p):
        p = (p or "").strip()
        if p and p not in ("其他", "无", "否", "不限") and p not in out:
            out.append(p)
            for syn in ATTR_SYNONYMS.get(p, []):
                if syn not in out:
                    out.append(syn)

    for key in ATTR_MAP.get(label, []):
        for v in (attrs.get(key) or []):
            for piece in re.split(r"[,，/、]", str(v)):
                add(piece)
            add(str(v))
    return out[:8]


def _fill_attributes(page, c, cfg, prog=None):
    """按真实属性表单逐项填写。返回 (已确认列表, 未完成列表, 说明)。

    合规: 品牌需商标授权, 默认不自动填(auto_fill_brand=False); 下拉只点选
    能与 1688 值明确匹配到的选项, 匹配不到就留空, 绝不乱猜(错填会触发处罚)。
    """
    def P(m):
        if prog:
            prog(m)

    if not cfg.get("auto_fill_attributes", True):
        return [], [], "已按配置跳过属性填写"
    attrs = c.get("attributes") or {}
    if not isinstance(attrs, dict) or not attrs:
        return [], [], "源数据无商品属性"
    try:
        items = page.evaluate(_ATTR_SCAN_JS)
    except Exception as e:
        return [], [], f"属性表单扫描失败: {str(e)[:60]}"
    if not items:
        return [], [], "页面未找到商品属性表单项"
    filled, missing = [], []
    allow_brand = bool(cfg.get("auto_fill_brand", False))
    dumped = [False]
    for it in items:
        label, fid = it.get("label") or "", it.get("id") or ""
        if not label or label not in ATTR_MAP:
            continue
        if label == "品牌" and not allow_brand:
            missing.append("品牌(需商标授权, 默认不自动填写, 请在页面确认)")
            continue
        cands = _attr_values(attrs, label)
        if not cands:
            missing.append(f"{label}(1688 无对应属性值)")
            continue
        sel = f'[id="{fid}"] input'
        if it.get("testid") == "beast-core-input-htmlInput":
            # 纯文本项(货号等): 直接写入并回读
            ok, _ = _set_by(page, sel, cands[0])
            time.sleep(0.2)
            vals = (_read_by(page, sel).get("values") or [""])[0]
            if ok and vals == cands[0]:
                filled.append(f"{label}={cands[0]}(已回读)")
            else:
                missing.append(f"{label}(写入后回读不符)")
        else:
            # 下拉项: 先给现有 DOM 打标记, 再用真实鼠标事件展开
            # (beast-core-select 监听 pointerdown, 纯 JS .click() 可能不触发)
            try:
                page.evaluate(_ATTR_MARK_JS)
            except Exception:
                pass
            try:
                page.locator(sel).first.click(timeout=2500)
            except Exception:
                try:
                    page.evaluate(_ATTR_OPEN_JS, {"id": fid})
                except Exception:
                    pass
            pk = {"ok": False, "reason": "下拉未展开"}
            for _ in range(5):
                time.sleep(0.4)
                try:
                    pk = page.evaluate(_ATTR_PICK_JS, {"id": fid, "cands": cands})
                except Exception as e:
                    pk = {"ok": False, "reason": str(e)[:60]}
                if pk.get("ok"):
                    break
            if pk.get("ok"):
                time.sleep(0.3)
                got = ((_read_by(page, sel).get("values") or [""])[0] or "").strip()
                if got:
                    filled.append(f"{label}={got}(下拉已选并回读)")
                else:
                    # 点了选项但控件没回显: 不虚报成功, 让用户在页面上核对
                    missing.append(f"{label}(已点『{pk.get('text')}』但控件未回显, 请在页面核对)")
            else:
                if not dumped[0]:
                    # 只存一次: 把展开后的真实浮层 DOM 留下来, 便于按实际选项校准
                    try:
                        _dump(page, DEBUG_ATTR)
                        dumped[0] = True
                        P(f"  ⚠ 属性下拉未匹配, 已存 {DEBUG_ATTR}; 回传该文件可精确校准选项")
                    except Exception:
                        pass
                seen = pk.get("seen") or []
                missing.append(f"{label}(候选 {cands[:2]}; {pk.get('reason')}"
                               + (f"; 页面可见 {seen[:4]}" if seen else "") + ")")
    P(f"  商品属性: 表单 {len(items)} 项, 已填 {len(filled)} 项, 未填 {len(missing)} 项")
    return filled, missing, f"表单 {len(items)} 项 / 已填 {len(filled)} 项"


def _fill_freight(page, cfg, prog=None):
    """运费模板: 真实控件是 radio 卡片组; 后台默认已选一个模板。"""
    def P(m):
        if prog:
            prog(m)
    try:
        st = page.evaluate(_FREIGHT_STATE_JS)
    except Exception as e:
        st = {"ok": False, "reason": str(e)[:60]}
    if not st.get("ok"):
        return False, f"运费模板({st.get('reason')})"
    want = (cfg.get("freight_template") or "").strip()
    if not want and st.get("checked"):
        P(f"  运费模板: 后台已默认选中『{st.get('checked')}』, 无需单独设置")
        return True, f"运费模板(后台默认已选『{st.get('checked')}』)"
    try:
        pk = page.evaluate(_FREIGHT_PICK_JS, {"name": want})
    except Exception as e:
        pk = {"ok": False, "reason": str(e)[:60]}
    if pk.get("ok"):
        time.sleep(0.4)
        st2 = page.evaluate(_FREIGHT_STATE_JS)
        P(f"  运费模板: 已选『{pk.get('text')}』(当前选中 {st2.get('checked') or '未回显'})")
        return True, f"运费模板(已选『{pk.get('text')}』)"
    return False, f"运费模板({pk.get('reason')}; 默认卡片: {', '.join(st.get('options') or [])})"


def _fill_sku_real(page, idx, value, retries=4):
    """JS 写入失败时的真实交互兜底(受控数字组件只认真实键盘事件)。

    idx: -1=库存, 0=拼单价, 1=单买价。反复核对, 直到页面回读值稳定(最多 retries 次)。

    Playwright 的 fill() 走「原生 setter + input 事件」, 常被 beast-core 的 React
    值追踪器在下次重渲染时冲掉(实测单买价因此始终为空)。改用真实逐字按键
    (press_sequentially) 才真正触发 React onChange, 写入最稳。
    """
    sval = str(value)
    last = ""
    for _att in range(max(1, retries)):
        try:
            row = page.locator(S_SKU_ROW).first
            if idx < 0:
                loc = row.locator("td.quantity input").first
            else:
                loc = row.locator(".sku-beast-price-input-container input").nth(idx)
            loc.click(timeout=3000)
            time.sleep(0.1)
            # 清空现有内容(全选 + 删除), 避免旧值残留
            loc.press("Control+a", timeout=2000)
            loc.press("Delete", timeout=2000)
            time.sleep(0.1)
            # 真实逐字输入, 触发 React onChange
            loc.press_sequentially(sval, timeout=5000)
            time.sleep(0.3)
            # 失焦提交
            loc.press("Tab", timeout=2000)
            time.sleep(0.2)
            last = str(loc.input_value(timeout=3000) or "").strip()
            if last == sval:
                return last
        except Exception:
            time.sleep(0.3)
    return last


def _fill_group_real(page, value):
    """兼容旧调用的别名: 补拼单价。"""
    return _fill_sku_real(page, 0, value)


# ---------------- 单条填写 ----------------
def _clip_title(t, max_units=60):
    """拼多多商品标题上限 60 字符(汉字按 2 计)。"""
    out, used = [], 0
    for ch in (t or ""):
        w = 2 if "\u4e00" <= ch <= "\u9fff" else 1
        if used + w > max_units:
            break
        out.append(ch)
        used += w
    return "".join(out)


def _resolve_price(c, cfg):
    return float(c.get("suggest_price") or 0)


def _price_floor(c, cfg):
    """拼单价保本线: 按进货价+进货运费+发货运费+佣金+支付费反算目标净利率售价。

    用户要求「比正常售价低 1-3 元」但「保证利润」, 所以拼单价最多降到这条线为止;
    若降 2 元会跌破保本线, 就反过来把单买价一起抬高, 始终保留 1-3 元价差。
    """
    from pricing import price_for_target
    try:
        return float(price_for_target(
            float(c.get("cost_price") or 0),
            float(c.get("inbound_shipping") or 0),
            float(c.get("outbound_shipping") or 0),
            commission_rate=float(c.get("commission_rate") or 0.01),
            target_margin=float(cfg.get("min_net_margin") or 0.08)))
    except Exception:
        return round(_resolve_price(c, cfg) * 0.90, 2)


def _resolve_stock(combo, cfg):
    try:
        cap = int(cfg.get("stock_cap") or 1000)
    except (TypeError, ValueError):
        cap = 1000
    try:
        dft = int(cfg.get("default_stock") or 100)
    except (TypeError, ValueError):
        dft = 100
    try:
        s = int(combo.get("stock") or 0)
    except (TypeError, ValueError):
        s = 0
    # 1688 单款库存常几十万, 代发不照搬; 收敛到一个合理上限
    stock = min(s, cap) if s > 0 else dft
    # 用户要求: 库存低于 1000 一律改成 8888(代发常备量, 避免 100/几百这类偏低的数字)
    if stock < 1000:
        stock = 8888
    return stock


def _price_pair(base: float, cfg: dict, floor: float):
    """返回 (拼单价, 单买价): 单买价 = 建议售价上浮, 拼单价低 1~3 元且不低于保本线。"""
    try:
        ratio = float(cfg.get("single_price_ratio") or 1.10)
    except (TypeError, ValueError):
        ratio = 1.10
    try:
        disc = float(cfg.get("group_discount") or 2.0)
    except (TypeError, ValueError):
        disc = 2.0
    disc = min(3.0, max(1.0, disc))          # 用户要求 1~3 元区间
    single = round(base * ratio, 2)
    group = round(max(single - disc, floor), 2)
    if group >= single - 0.5:                # 保本线顶穿了价差 → 整体抬高
        single = round(group + disc, 2)
    return group, single


def _sku_rows(c, cfg):
    """把清单里的价格/库存整理成规格表格行数据。没有每款数据时用统一售价兜底。

    max_sku: 0=全量搬迁(默认, 按源站全部款数准备数据);
             >0 = 最多生成多少行(后台规格表过大/卡顿时可设 30)。
    实际能写入多少行仍取决于 PDD 后台已添加的规格行数, 写入端按 DOM 行数裁剪。
    """
    base = _resolve_price(c, cfg)
    floor = _price_floor(c, cfg)
    combos = c.get("sku_combos") or []
    try:
        lim = int(cfg.get("max_sku") if cfg.get("max_sku") is not None else 0)
    except (TypeError, ValueError):
        lim = 0
    sel = combos if lim <= 0 else combos[:max(1, lim)]
    rows = []
    if sel:
        for cb in sel:
            try:
                p = float(cb.get("suggest_price") or 0) or base
            except (TypeError, ValueError):
                p = base
            group, single = _price_pair(p, cfg, floor)
            rows.append({
                "stock": _resolve_stock(cb, cfg),
                "group": f"{group:.2f}",
                "single": f"{single:.2f}",
            })
    else:
        group, single = _price_pair(base, cfg, floor)
        rows.append({
            "stock": _resolve_stock({}, cfg),
            "group": f"{group:.2f}",
            "single": f"{single:.2f}",
        })
    return rows, len(combos)


def _fill_one(page, c, cfg, prog=None, dry_run=True):
    """按真实两步向导填一条商品。filled 只记录真正写进页面的字段。"""
    def P(m):
        if prog:
            prog(m)

    r = {"item_id": c.get("item_id", ""), "title": (c.get("title") or "")[:40],
         "filled": [], "missing": [], "ok": False, "submitted": False, "stage": ""}
    try:
        opened, msg, stage = _open_create_form(page, c, cfg, P)
        if not opened:
            r["missing"].append("发布向导: " + msg)
            _dump(page)
            return r
        P(f"  ☑ {msg}")
        r["stage"] = stage

        # ===== 第1步: 标题 + 轮播主图 =====
        title = _clip_title(c.get("title") or "")
        ok, _ = _set_by(page, S_TITLE, title)
        # 回读确认: React 完全可能在 setter 之后重渲染并把输入清空
        time.sleep(0.4)
        vals = _read_by(page, S_TITLE).get("values") or []
        title_ok = bool(ok and vals and vals[0] == title)
        (r["filled"] if title_ok else r["missing"]).append(
            "商品标题(已回读确认)" if title_ok else
            ("商品标题(未找到输入框)" if not ok else
             f"商品标题(写入后回读不符: {repr((vals[0] if vals else '')[:20])})"))

        main_urls, detail_urls, img_summary = _image_pool(c, cfg, P)
        max_bytes = int(cfg.get("image_max_bytes") or 8388608)
        main_paths = []
        img_ok = False
        img_note = "无图源"
        if main_urls:
            main_paths = _download_many(main_urls, len(main_urls), P, max_bytes)
            P(f"  主图: 候选 {len(main_urls)} 张, 合规下载成功 {len(main_paths)} 张")
            ok, d = _upload_by(page, S_CAROUSEL, main_paths, P)
            img_ok, img_note = ok, d
        else:
            r["missing"].append("轮播主图(无图源)")

        if stage == "step1":
            # 主图走的是异步上传接口, set_input_files 成功 ≠ 图片已上传。
            # 必须等页面「已上传 N/10」真正 >0, 否则点『下一步』必被校验拦下。
            n_up, _ = _wait_uploaded(page, timeout=12)
            if n_up > 0:
                img_ok = True
                img_note = f"已选 {len(main_paths)} 张, 页面回读已上传 {n_up}/10"
                P(f"  主图上传复核: {n_up}/10 已成功")
            elif n_up == 0:
                # 第1步的「上传图片 (0/10)」有可能是静态文案、不随上传刷新。
                # 这里不硬停: 继续点『下一步』, 由后台校验给出最终判据,
                # _goto_step2 若被拦下会明确报「主图或标题未通过校验」。
                img_note = f"已选 {len(main_paths)} 张, 页面计数仍显示 0/10"
                P("  ⚠ 主图计数仍为 0, 继续点『下一步』由后台校验")
            else:
                img_note = f"{img_note}(页面无上传计数, 未复核)"
        if main_urls:
            r["missing"] = [x for x in r["missing"] if not x.startswith("轮播主图")]
            r["filled"] = [x for x in r["filled"] if not x.startswith("轮播主图")]
            (r["filled"] if img_ok else r["missing"]).append(f"轮播主图({img_note})")

        if stage == "step1":
            if not (title_ok and img_ok):
                r["missing"].append("第1步必填未齐(主图/标题), 未点『下一步』")
                P("  ⚠ 第1步必填未齐, 不点『下一步』, 详见未完成项")
                _dump(page)
                return r
            st2, m2 = _goto_step2(page, P)
            stage = st2
            r["stage"] = stage
            P(f"  {'☑' if st2 == 'step2' else '⚠'} {m2}")
            if st2 != "step2":
                r["missing"].append(m2)
                _dump(page)
                return r

        # ===== 第2步: 完整信息表单 =====
        _wait_form_ready(page, timeout=15)
        _dump(page, DEBUG_STEP2)
        try:
            has_freight = page.locator(S_FREIGHT).count() > 0
        except Exception:
            has_freight = False
        if has_freight:
            stage = "step2"      # 以真实控件为准, 不被文案判定误伤
        r["stage"] = stage
        if stage != "step2":
            r["missing"].append(f"未进入完整表单(当前 {stage})")
            _dump(page)
            return r

        # 标题复核(SPA 切换后输入框会被重建, 值可能被清空)
        _set_by(page, S_TITLE, title)
        time.sleep(0.4)
        v2 = (_read_by(page, S_TITLE).get("values") or [""])[0]
        r["missing"] = [x for x in r["missing"] if not x.startswith("商品标题")]
        r["filled"] = [x for x in r["filled"] if not x.startswith("商品标题")]
        if v2 == title:
            title_ok = True
            r["filled"].append("商品标题(第2步已回读确认)")
        else:
            title_ok = False
            r["missing"].append("商品标题(第2步回读不符, 疑似被重渲染清空)")

        # 主图复核: 第2步是重新渲染的完整表单, 若「已上传 0/10」则补传一次并再复核
        try:
            carried = page.evaluate(_UPLOAD_COUNT_JS)
        except Exception:
            carried = -1
        if carried == 0 and main_paths:
            _upload_by(page, S_CAROUSEL, main_paths, P)
            P("  第2步主图复核: 页面显示 0/10, 已补传")
            n2, _ = _wait_uploaded(page, timeout=10)
            r["missing"] = [x for x in r["missing"] if not x.startswith("轮播主图")]
            r["filled"] = [x for x in r["filled"] if not x.startswith("轮播主图")]
            if n2 > 0:
                img_ok = True
                r["filled"].append(f"轮播主图(第2步补传后回读 {n2}/10)")
            else:
                img_ok = False
                r["missing"].append("轮播主图(第2步补传后仍未确认上传成功)")

        # --- 详情图: 合并全部主图 + 详情图(不再丢首图), 按平台 50 张上限合规上传 ---
        if detail_urls:
            paths = _download_many(detail_urls, len(detail_urls), P, max_bytes)
            P(f"  详情图: 候选 {len(detail_urls)} 张, 合规下载成功 {len(paths)} 张")
            P(f"  图片来源: {img_summary}")
            ok, d = _upload_by(page, S_DETAIL, paths, P)
            note = d
            if ok and paths:
                nd, _ = _wait_uploaded(page, timeout=12, js=_DETAIL_COUNT_JS)
                if nd > 0:
                    note = f"已选 {len(paths)} 张, 页面回读已上传 {nd}/50"
                elif nd == 0:
                    note = f"已选 {len(paths)} 张, 页面计数仍显示 0/50(请在页面核对)"
                else:
                    note = f"{d}(页面无上传计数, 未复核)"
            (r["filled"] if ok else r["missing"]).append(f"详情图({note})")
        else:
            r["missing"].append("详情图(无图源)")

        # --- 规格与库存表格: 库存 / 拼单价 / 单买价 ---
        rows, n_combos = _sku_rows(c, cfg)
        floor = _price_floor(c, cfg)
        try:
            _lim = int(cfg.get("max_sku") if cfg.get("max_sku") is not None else 0)
        except (TypeError, ValueError):
            _lim = 0
        P(f"  SKU: 源站共 {n_combos} 款 -> 本次准备 {len(rows)} 行"
          + (f" (上限 {_lim}; 配置 max_sku=0 可全量搬迁)" if n_combos > len(rows) else " (全量)"))
        P(f"  定价: 保本价 {floor:.2f} 元; 拼单价 {rows[0]['group']} / "
          f"单买价 {rows[0]['single']} (拼单价低 {cfg.get('group_discount')} 元且不低于保本线)")

        def _num(s):
            try:
                return float(str(s).strip())
            except (TypeError, ValueError):
                return None

        # 每个字段单独一次 evaluate, 每次都重新查询 DOM。旧版一次写三个字段时,
        # 库存写入触发的 React 重渲染会让后面两个 DOM 引用失效 → 单买价始终为空。
        # 相位顺序特意把『单买价』放最后: 写完后没有后续写入再触发重渲染, 最易一次成型。
        step = {}
        for phase in ("stock", "group", "single"):
            try:
                step[phase] = page.evaluate(_FILL_SKU_JS, {"items": rows, "phase": phase})
            except Exception as e:
                step[phase] = {"ok": False, "err": str(e)[:80]}
            time.sleep(0.35)
        P("  规格分步写入: " + ", ".join(
            f"{lab} 回读 {step[ph].get('verified')}/{len(rows)} 行"
            for ph, lab in (("stock", "库存"), ("single", "单买价"),
                            ("group", "拼单价"))))

        # 以页面真实值为准, 缺哪个字段就单独用真实键盘补哪个, 反复核对直到稳定
        try:
            real = page.evaluate(_READ_SKU_JS)
        except Exception:
            real = []
        want = rows[0]
        for key, idx, label in (("stock", -1, "库存"),
                                ("single", 1, "单买价"),
                                ("group", 0, "拼单价")):
            want_v = str(want[key])
            for _att in range(4):
                got = _num((real[0].get(key) if real else "") or "")
                if got is not None and abs(got - float(want_v)) < 0.01:
                    break
                newv = _fill_sku_real(page, idx, want_v)
                time.sleep(0.25)
                try:
                    real = page.evaluate(_READ_SKU_JS)
                except Exception:
                    real = []
            final = _num((real[0].get(key) if real else "") or "")
            okfill = final is not None and abs(final - float(want_v)) < 0.01
            P(f"  {label}真实交互兜底: 最终回读『{real[0].get(key) if real else ''}』"
              + (" ✅" if okfill else " ⚠ 仍不符, 请人工核对"))
            time.sleep(0.2)

        verified = 0
        for i, row in enumerate(rows[:len(real)]):
            v = real[i]
            g, s, q = _num(v.get("group")), _num(v.get("single")), _num(v.get("stock"))
            if (q is not None and g is not None and s is not None
                    and abs(g - float(row["group"])) < 0.01
                    and abs(s - float(row["single"])) < 0.01
                    and abs(q - float(row["stock"])) < 0.01):
                verified += 1
        sku_ok = verified > 0
        if sku_ok:
            r["filled"].append(
                f"规格价库存(整行回读确认 {verified}/{len(rows)} 行: "
                f"库存{rows[0]['stock']} / 拼单价{rows[0]['group']} / 单买价{rows[0]['single']})")
            if n_combos and len(real) < len(rows):
                P(f"  ⚠ 源数据 {n_combos} 款, 后台当前只有 {len(real)} 行规格 "
                  "(需先在后台添加规格类型), 本次按现有行填写")
        else:
            r["missing"].append(
                f"规格价库存(整行回读不符: 页面 {real[:1]}, 期望 {rows[0]})")

        # --- 商品参考价(页面红色必填: 应大于商品最大单买价) ---
        # 旧版定义了 S_ADVICE_PRICE 却从未写入, 导致「商品参考价必须填写!」校验失败。
        try:
            pad = float(cfg.get("advice_price_pad") or 10)
        except (TypeError, ValueError):
            pad = 10.0
        try:
            max_single = max(float(x["single"]) for x in rows)
        except (TypeError, ValueError, KeyError):
            max_single = 0.0
        advice = round(max_single + max(1.0, pad), 2)
        _set_by(page, S_ADVICE_PRICE, f"{advice:.2f}")
        time.sleep(0.30)
        try:
            ad_val = ((_read_by(page, S_ADVICE_PRICE).get("values") or [""])[0] or "").strip()
        except Exception:
            ad_val = ""
        if not ad_val:
            try:
                loc = page.locator(S_ADVICE_PRICE).first
                loc.click(timeout=2500)
                loc.fill(f"{advice:.2f}", timeout=2500)
                loc.press("Tab", timeout=2500)
                ad_val = (loc.input_value(timeout=2500) or "").strip()
            except Exception:
                pass
        advice_ok = bool(ad_val)
        if advice_ok:
            r["filled"].append(f"商品参考价({ad_val}; 高于最大单买价 {max_single:.2f})")
            P(f"  商品参考价: 已填 {ad_val} 元 > 最大单买价 {max_single:.2f} 元")
        else:
            r["missing"].append("商品参考价(页面必填项, 写入后未回读到值)")

        # --- 满件折扣会把拼单价再压低, 如实提示是否击穿保本线 ---
        try:
            disc = page.evaluate(r"""() => {
              const t = document.body.innerText || '';
              const m = t.match(/折后拼单价[:：]?\s*(\d+(?:\.\d+)?)\s*元/);
              return m ? m[1] : '';
            }""")
        except Exception:
            disc = ""
        dv = _num(disc)
        if dv is not None:
            note = " ⚠ 低于保本价, 建议在后台调高折扣或整体抬价" if dv < floor else \
                   f" (仍高于保本价 {floor:.2f})"
            P(f"  店铺满件折扣: 折后拼单价 {dv:.2f} 元{note}")

        # --- 商品属性: 按真实表单 label 映射 1688 属性 ---
        attr_ok, attr_miss, attr_note = _fill_attributes(page, c, cfg, P)
        for a in attr_ok:
            r["filled"].append(f"属性·{a}")
        for a in attr_miss:
            r["missing"].append(f"属性·{a}")
        r["attr_note"] = attr_note

        # --- 运费模板: radio 卡片组, 后台已默认选中一个 ---
        freight_ok, freight_note = _fill_freight(page, cfg, P)
        (r["filled"] if freight_ok else r["missing"]).append(freight_note)

        # --- 提交 ---
        # 商品参考价是页面红色必填项, 缺了后台一律拒绝提交, 必须纳入核心判定
        core = bool(title_ok and img_ok and sku_ok and advice_ok and freight_ok)
        r["ok"] = bool(core and r["stage"] == "step2")
        if not dry_run:
            if cfg.get("submit_only_if_core_ok", True) and not r["ok"]:
                r["missing"].append("核心字段未齐, 已跳过提交(避免残次上架)")
            else:
                try:
                    clicked = page.evaluate(_SUBMIT_JS)
                except Exception:
                    clicked = {"ok": False}
                if clicked.get("ok"):
                    time.sleep(3.0)
                    r["submitted"] = True
                    r["filled"].append(f"已点击『{clicked.get('text')}』(结果请在后台确认)")
                else:
                    r["missing"].append("提交按钮未找到或仍禁用(必填项可能未齐)")
        else:
            P("  · 预览模式: 未点击提交")
        _dump(page)
    except Exception as e:
        r["missing"].append("异常:" + str(e)[:100])
        try:
            _dump(page)
        except Exception:
            pass
    return r


# ---------------- 主入口 ----------------
def publish(headless: bool = False, candidates: list = None, progress=None,
            dry_run: bool = True, limit: int = None, on_result=None) -> dict:
    """把上架清单逐条填写到拼多多。默认 dry_run=True(只填不提交)。

    on_result: GUI 可传入回调, 在浏览器关闭前收到结果; 预览模式下浏览器会保留
               到该回调触发 close_preview() 或 10 分钟超时, 便于人工核对。
    """
    from pdd_session import launch_context, ensure_login
    from models import ListingCandidate

    def prog(msg):
        if progress:
            try:
                progress(f"发布: {msg}")
            except Exception:
                pass

    empty = {"total": 0, "ok": 0, "submitted": 0, "results": [], "debug": "",
             "dry_run": dry_run}
    if not candidates:
        prog("⚠ 没有可发布的候选(请先在『上架导出』点『生成上架清单』)")
        return empty

    norm = [c.to_dict() if isinstance(c, ListingCandidate) else (c or {})
            for c in candidates]
    if limit:
        norm = norm[:int(limit)]

    # 等上一轮浏览器彻底退出, 避免 persistent profile 目录被占用
    if not _PREVIEW["released"].wait(timeout=25):
        prog("⚠ 上一轮浏览器尚未释放，仍继续尝试（若报 profile 占用请稍后重试）")
    _PREVIEW["released"].clear()

    # 本轮的关闭事件由发布线程自己持有(局部量), 不依赖全局 evt:
    # 这样「重新发布」时上一轮收尾不会误清下一轮的事件。
    # 若用户在启动前就已点了关闭, 继承这个已触发的信号, 别把浏览器又留下来。
    prev_evt = _PREVIEW.get("evt")
    my_evt = threading.Event()
    if prev_evt is not None and prev_evt.is_set():
        my_evt.set()
    _PREVIEW["evt"] = my_evt
    _PREVIEW.update({"pw": None, "ctx": None, "page": None})

    cfg = _load_cfg()
    prog(f"配置: 运费模板={cfg.get('freight_template') or '采用后台默认'} "
         f"库存≤{cfg.get('stock_cap')} 主图≤{cfg.get('max_carousel')} "
         f"详情图≤{cfg.get('max_detail')} 拼单价优惠={cfg.get('group_discount')}元 "
         f"参考价+{cfg.get('advice_price_pad')}元 "
         f"属性填写={'开' if cfg.get('auto_fill_attributes', True) else '关'} "
         f"品牌={'自动填' if cfg.get('auto_fill_brand') else '留空待授权'}")

    pw, ctx, page = None, None, None
    # 无论预览还是真实发布, 都保留浏览器供人工核对结果(关闭弹窗/超时后自行关闭)
    keep = bool(on_result is not None)
    result = dict(empty)
    try:
        prog("① 正在启动浏览器(复用 pdd_profile/ 登录态)…")
        pw, ctx = launch_context(headless=headless)
        prog("② 浏览器已启动, 正在打开拼多多商家后台…")
        page = ensure_login(ctx, timeout=240, progress=progress)
        prog("✅ 登录成功, 进入『发布新商品』向导")
        results = []
        for i, c in enumerate(norm, 1):
            if _PREVIEW.get("closed"):
                prog("已收到关闭信号，停止后续商品")
                break
            prog(f"【{i}/{len(norm)}】填写: {(c.get('title') or '')[:30]}")
            r = _fill_one(page, c, cfg, prog=prog, dry_run=dry_run)
            results.append(r)
            tag = "已点击提交" if r.get("submitted") else ("预览完成" if dry_run else "未提交")
            prog(f"  → {tag} · 实际写入 {len(r['filled'])} 项, 未完成 {len(r['missing'])} 项")
            for f in r["filled"]:
                prog(f"      ✓ {f}")
            for m in r["missing"]:
                prog(f"      · 未完成: {m}")
        ok = sum(1 for x in results if x["ok"])
        submitted = sum(1 for x in results if x.get("submitted"))
        written = sum(len(x["filled"]) for x in results)
        dbg = _dump(page)
        result = {"total": len(results), "ok": ok, "submitted": submitted,
                  "results": results, "debug": dbg, "dry_run": dry_run,
                  "written": written}
        prog(f"🏁 完成: 共 {len(results)} 条, 字段写入 {written} 项, "
             f"核心齐全 {ok} 条, 已点击提交 {submitted} 条")
        if dry_run:
            prog("预览模式不会提交上架; 浏览器保持打开, 请核对表单后关闭本弹窗")
        return result
    except Exception as e:
        msg = str(e)
        low = msg.lower()
        hint = ""
        if any(k in low for k in ("processsingleton", "user data directory", "already in use",
                                  "being used", "singletonlock", "profile is")):
            hint = ("；多半是上一次的浏览器没关干净、占用了 pdd_profile/，"
                    "请关闭残留的 Chromium 窗口后重试")
        elif "timeout" in low or "timed out" in low:
            hint = "；浏览器启动或页面加载超时，请检查网络后重试"
        prog(f"⚠ 发布中断: {msg[:160]}{hint}")
        result["error"] = msg[:160] + hint
        return result
    finally:
        if keep:
            # 先把结果交给 GUI(此时浏览器仍打开), 再在本线程等待用户核对完。
            # 结果只在这里交付一次; 调用方线程不得再触发一次 on_done,
            # 否则「重新发布」后上一轮的迟到回调会覆盖新一轮的界面状态。
            try:
                on_result(result)
            except Exception:
                pass
            _PREVIEW.update({"pw": pw, "ctx": ctx, "page": page})
            # 浏览器都没起来时不空等, 直接收尾
            if ctx is not None and not _PREVIEW.get("closed") and not my_evt.is_set():
                my_evt.wait(timeout=600)   # 10 分钟兜底, 避免浏览器常驻
            _release_preview(ctx, pw)
        else:
            try:
                if ctx:
                    ctx.close()
            except Exception:
                pass
            try:
                if pw:
                    pw.stop()
            except Exception:
                pass
            _PREVIEW.update({"pw": None, "ctx": None, "page": None})
            _PREVIEW["released"].set()


def dump_create_html(headless: bool = False, progress=None) -> str:
    """只登录 + 打开发布向导 + 存真实 DOM, 不填不提交(供校准控件)。"""
    from pdd_session import launch_context, ensure_login

    def prog(m):
        if progress:
            try:
                progress(f"诊断: {m}")
            except Exception:
                pass

    pw, ctx = launch_context(headless=headless)
    try:
        page = ensure_login(ctx, timeout=240, progress=progress)
        page.goto(CATEGORY_URL, wait_until="domcontentloaded", timeout=30000)
        time.sleep(3.0)
        _dump(page, DEBUG_CATEGORY)
        prog(f"第1步页面已存: {DEBUG_CATEGORY}")
        _dump(page, DEBUG_PUBLISH)
        prog(f"当前页 HTML 已存: {DEBUG_PUBLISH}")
        return DEBUG_PUBLISH
    finally:
        try:
            ctx.close()
        except Exception:
            pass
        try:
            pw.stop()
        except Exception:
            pass


if __name__ == "__main__":
    import sys
    sys.path.insert(0, HERE.as_posix())
    cmd = sys.argv[1] if len(sys.argv) > 1 else "help"
    if cmd == "dump-create-html":
        dump_create_html(headless=False, progress=print)
    elif cmd == "cfg":
        print(json.dumps(_load_cfg(), ensure_ascii=False, indent=2))
    else:
        print("pdd_publish 发布填写引擎")
        print("  python -m pdd_publish dump-create-html   # 抓发布页真实 DOM")
        print("  python -m pdd_publish cfg                # 查看发布配置")
        print("  代码调用 publish(candidates=..., dry_run=True)  # 默认只预览不提交")
