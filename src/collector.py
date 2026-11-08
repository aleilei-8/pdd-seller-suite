#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
collector.py —— 1688 链接采集器(替代穿云箭的可持续数据源)

输入: 1688 商品链接(offer / offer_detail 页) 或 本地 HTML 文件 / 已抓 HTML 文本
输出: 与 loader.parse_cyj_item 完全兼容的 dict 列表 -> 可直接进选品/定价/上架

设计要点:
  1. parse_1688_html(html, item_id) 把"页面解析"与"浏览器抓取"解耦:
     - 浏览器只负责拿到渲染后的 HTML, 解析逻辑纯函数化, 可离线用本地 HTML 验证/复用
     - 也支持直接传已保存的 HTML 文件(无浏览器也能跑)
  2. 1688 不包运费: 这里尽力从商品页抓 express_fee(运费),
     抓不到返回 0 并在 GUI 标注 "需补进货运费", 避免利润虚高(你最在意的修正点)。
  3. 容错: 任意字段缺失/解析失败都降级为空或 0, 不影响整批导入。

依赖: Playwright(仅 collect 在线抓取时需要)。解析本身零依赖。
首次在线抓取前请执行:  playwright install chromium
"""
import json
import os
import re
import time
from typing import Optional

# 登录态/浏览器指纹目录(持久化到项目 data 下, 不外泄)
import paths as _paths
_paths.setup_env()
from paths import DATA_S, SRC_S

_THIS_DIR = SRC_S
STATE_PATH = os.path.join(DATA_S, "1688_state.json")
CHANNEL_PATH = os.path.join(DATA_S, "1688_channel.txt")
DATA_DIR = DATA_S
# 真实持久化浏览器 profile: 让 1688 当成『真实浏览器』而非自动化/无痕环境,
# 这是解决『验证码每几秒疯狂刷新、点了图片就重置』的关键 —— 临时无痕上下文即使注入 cookie
# 也常被风控识别为机器人, 导致 punish 图片验证码不停重置, 用户永远提交不了。
PROFILE_DIR = os.path.join(DATA_DIR, "1688_profile")
PROFILE_CHANNEL_PATH = os.path.join(DATA_DIR, "1688_profile_channel.txt")


def _write_channel_marker(channel: str):
    """记录登录时实际使用的浏览器引擎, 供采集时复用同一引擎(避免跨引擎 storage_state 不兼容)。"""
    try:
        with open(CHANNEL_PATH, "w", encoding="utf-8") as f:
            f.write(channel or "")
    except Exception:
        pass


def _read_channel_marker() -> str:
    try:
        with open(CHANNEL_PATH, encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def _write_profile_channel(channel: str):
    """记录首次为持久化 profile 选定的浏览器引擎(chrome/msedge/''), 之后复用同一引擎,
    避免 Chrome/Edge/Chromium 反复改写同一 profile 导致互相不兼容。"""
    try:
        with open(PROFILE_CHANNEL_PATH, "w", encoding="utf-8") as f:
            f.write(channel or "")
    except Exception:
        pass


def _read_profile_channel() -> str:
    try:
        with open(PROFILE_CHANNEL_PATH, encoding="utf-8") as f:
            return (f.read() or "").strip()
    except Exception:
        return ""


def _sanitize_cookies(cookies: list) -> list:
    """把浏览器原生 cookie 清洗成 Playwright storage_state 可接受的格式。
    关键: 1688 部分 cookie 的 sameSite 来自真实 Chrome 时是 'no_restriction'/'unspecified' 或
          小写的 'none'/'lax'/'strict', 而 Playwright 的 new_context(storage_state=) 输入校验
          只接受首字母大写的 'Strict'|'Lax'|'None' —— 不匹配会直接抛
          'storageState.cookies[0].sameSite: expected one of (Strict|Lax|None)' 导致所有引擎都启动失败。
          这里严格归一化到这三个值, 同时规范 expires/path, 丢弃无 domain 的非法 cookie。"""
    out = []
    now = time.time()
    for c in cookies or []:
        domain = c.get("domain")
        if not domain:
            continue
        ss = (c.get("sameSite") or "")
        s = ss.lower() if isinstance(ss, str) else ""
        if s in ("none", "no_restriction", "unspecified", ""):
            ss_norm = "None"
        elif s == "lax":
            ss_norm = "Lax"
        elif s == "strict":
            ss_norm = "Strict"
        else:
            ss_norm = "None"  # 任何未知值兜底为 None, 避免校验失败
        nc = {
            "name": c.get("name"),
            "value": c.get("value"),
            "domain": domain,
            "path": c.get("path") or "/",
            "sameSite": ss_norm,
        }
        exp = c.get("expires")
        if exp is None or exp == -1 or exp <= now:
            nc["expires"] = -1
        else:
            nc["expires"] = exp
        nc["httpOnly"] = bool(c.get("httpOnly"))
        nc["secure"] = bool(c.get("secure"))
        out.append(nc)
    return out


def _load_storage_state(path: str) -> dict:
    """读取已保存的 storage_state(含 cookie+localStorage), 并清洗 cookie 部分。
    返回可直接传给 new_context(storage_state=...) 的 dict; 读取/清洗失败返回 None。"""
    try:
        with open(path, encoding="utf-8") as f:
            st = json.load(f)
        if isinstance(st, dict) and st.get("cookies"):
            st["cookies"] = _sanitize_cookies(st["cookies"])
            return st
    except Exception:
        pass
    return None


_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
       "(KHTML, like Gecko) Chrome/120.0 Safari/537.36")
# 登录时用于触发并捕获 x5sec 的示例详情页(任意有效 offer 均可, x5sec 是域名级风控凭证)
SAMPLE_OFFER = "613103202040"
# 去掉 webdriver 等自动化特征, 降低被 1688 识别概率
_STEALTH_JS = """
Object.defineProperty(navigator, 'webdriver', { get: () => undefined });
try { window.chrome = { runtime: {} }; } catch(e){}
Object.defineProperty(navigator, 'plugins', { get: () => [1,2,3] });
Object.defineProperty(navigator, 'languages', { get: () => ['zh-CN','zh','en'] });
"""

# 1688 常见的 offer 页链接形态
_LINK_RE = re.compile(r"(?:detail|www)\.1688\.com/offer/(\d+)\.html", re.I)
_BARE_ID_RE = re.compile(r"\b(\d{8,20})\b")  # 裸 1688 offer id

# 价格区间 "2.50-8.00" -> 取最小成交价作为进货价
_PRICE_RANGE_RE = re.compile(r"(\d+(?:\.\d+)?)\s*-\s*(\d+(?:\.\d+)?)")
_NUM_RE = re.compile(r"(\d+(?:\.\d+)?)")

# 运费文本: "运费 6.00 起" / "包邮" / "快递 8 元"
_FREIGHT_TEXT_RE = re.compile(r"(?:运费|快递费|物流)[^\d]{0,6}?(\d+(?:\.\d+)?)\s*元?")
_MONTH_SALE_RE = re.compile(r"(?:成交|30天成交|月销)[^\d]{0,4}?(\d+(?:\.\d+)?\s*[亿万]?)")
_ATTR_KV_RE = re.compile(r"<th[^>]*>(.*?)</th>\s*<td[^>]*>(.*?)</td>", re.S)


def parse_links(text: str) -> list:
    """从粘贴文本里提取 1688 offer 链接或裸 id, 去重返回 id 列表。"""
    if not text:
        return []
    ids = []
    seen = set()
    for m in _LINK_RE.finditer(text):
        i = m.group(1)
        if i not in seen:
            seen.add(i)
            ids.append(i)
    if not ids:  # 没有链接形态时, 退而求其次抓裸数字 id
        for m in _BARE_ID_RE.finditer(text):
            i = m.group(1)
            if i not in seen:
                seen.add(i)
                ids.append(i)
    return ids


def _extract_initial_state(html: str) -> Optional[dict]:
    """提取 1688 页面内嵌的 window.__INITIAL_STATE__ 大 JSON(React 渲染数据)。
    支持两种写法:  = {...}   或   =JSON.parse("...") """
    if not html:
        return None
    # 写法2: JSON.parse("...")  (字符串被转义). 直接去掉转义引号还原 JSON, 避免 unicode_escape 把中文当 latin-1 破坏
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*JSON\.parse\(\s*\"(.*?)\"\)\s*;", html, re.S)
    if m:
        raw = m.group(1).replace('\\"', '"')
        try:
            return json.loads(raw)
        except Exception:
            pass
        # 兜底: 仍失败则尝试 unicode_escape(处理纯 \uXXXX 转义场景)
        try:
            return json.loads(m.group(1).encode("utf-8").decode("unicode_escape"))
        except Exception:
            pass
    # 写法1: 直接对象
    m = re.search(r"window\.__INITIAL_STATE__\s*=\s*(\{.*?\})\s*;", html, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    # 兜底: 任意 __INITIAL_STATE__ 之后的大括号对象
    m = re.search(r"__INITIAL_STATE__\s*=\s*(\{.*\})\s*;", html, re.S)
    if m:
        try:
            return json.loads(m.group(1))
        except Exception:
            pass
    return None


def _extract_context_data(html: str):
    """提取【新版】1688 详情页内嵌数据。

    1688 详情页已不再使用 window.__INITIAL_STATE__, 而是把商品数据挂在
    window.context=(function(b,d){...})(KEY, DATA) 的 DATA 参数里(合法 JSON, 键带引号)。
    返回解析后的 dict(根含 'result'), 失败/非新版结构返回 None。"""
    if not html or "window.context" not in html:
        return None
    # 定位函数声明 'window.context=(function(b,d){', 用括号匹配找到函数体真正的结束 '}',
    # 再往后定位调用 ')( ' 之后的 DATA 对象。
    # (不能用非贪婪 '.*?}}(' 直接找调用点 —— 大页面函数体内常有嵌套 IIFE 的 '})(' 导致过早截断。)
    m = re.search(r"window\.context\s*=\s*\(function\([^)]*\)\s*\{", html, re.S)
    if not m:
        return None
    # 从函数体 '{' 起做括号匹配, 找函数结束 '}'
    n = len(html)
    i = m.end() - 1  # 指向函数体的 '{'
    depth = 0
    in_str = False
    esc = False
    func_end = -1
    while i < n:
        ch = html[i]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    func_end = i
                    break
        i += 1
    if func_end < 0:
        return None
    # 函数结束 '}' 之后应是 ')' '(' 的调用, 再往后才是 DATA 对象
    rest = html[func_end:]
    j = rest.find("(")  # 调用运算符的 '('
    if j < 0:
        return None
    ob = rest.find("{", j)
    if ob < 0:
        return None
    # 从 ob 做括号匹配找配对的 '}'(正确处理字符串内的括号与转义)
    depth = 0
    in_str = False
    esc = False
    end = -1
    rn = len(rest)
    for k in range(ob, rn):
        ch = rest[k]
        if in_str:
            if esc:
                esc = False
            elif ch == "\\":
                esc = True
            elif ch == '"':
                in_str = False
        else:
            if ch == '"':
                in_str = True
            elif ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
                if depth == 0:
                    end = k + 1
                    break
    if end < 0:
        return None
    raw = rest[ob:end]
    # 1688 的 DATA 是 JS 对象字面量而非严格 JSON(含未加引号的裸键名, 如
    # "skuFeatures":{4764806828474:{...}}), 先尝试严格 JSON, 失败再用字符串感知的
    # 修复器(只给【字符串外】的裸键名加引号、去掉尾随逗号)再解析。
    try:
        return json.loads(raw)
    except Exception:
        pass
    try:
        return json.loads(_js_object_to_json(raw))
    except Exception:
        return None


def _js_object_to_json(s: str) -> str:
    """把 JS 对象字面量修复成可 json.loads 的字符串。

    仅处理【字符串外】的语法差异, 避免破坏字符串内文本:
      - 裸键名(标识符或数字) => 加双引号:  {foo:1} / {4764806828474:{...}}
      - 尾随逗号:  {a:1,} / [1,2,] => 去掉逗号
    不处理 undefined/NaN/new Date(这些在 1688 详情页 DATA 中极少出现, 出现时由上层容错)。"""
    out = []
    i = 0
    n = len(s)
    while i < n:
        ch = s[i]
        if ch == '"':
            # 整段字符串原样拷贝(含转义), 不对其内部做任何修改
            out.append(ch)
            i += 1
            while i < n:
                c = s[i]
                out.append(c)
                if c == "\\":
                    if i + 1 < n:
                        out.append(s[i + 1])
                        i += 2
                        continue
                elif c == '"':
                    i += 1
                    break
                i += 1
            continue
        if ch in "{[,":
            out.append(ch)
            i += 1
            # 跳过空白
            while i < n and s[i] in " \t\r\n":
                out.append(s[i])
                i += 1
            # 当前是否未加引号的键(标识符/纯数字)后跟 ':'
            j = i
            if j < n and (s[j].isalpha() or s[j] == "_" or s[j] == "$" or s[j].isdigit()):
                kstart = j
                if s[j].isdigit():
                    while j < n and s[j].isdigit():
                        j += 1
                else:
                    while j < n and (s[j].isalnum() or s[j] == "_" or s[j] == "$"):
                        j += 1
                k = j
                while k < n and s[k] in " \t\r\n":
                    k += 1
                if k < n and s[k] == ":":
                    out.append('"' + s[kstart:j] + '"')
                    i = k  # 跳到 ':' 之前, 下一轮正常输出 ':'
                    continue
            continue
        if ch == ",":
            # 尾随逗号: ',' 后紧跟 '}' 或 ']'
            k = i + 1
            while k < n and s[k] in " \t\r\n":
                k += 1
            if k < n and s[k] in "}]":
                i = k  # 跳过该逗号
                continue
        out.append(ch)
        i += 1
    return "".join(out)


def _ctx_get(d, *path, default=None):
    """安全按路径取嵌套 dict 值(list 索引用 int)。"""
    cur = d
    for p in path:
        if isinstance(cur, dict) and isinstance(p, str) and p in cur:
            cur = cur[p]
        elif isinstance(cur, list) and isinstance(p, int) and 0 <= p < len(cur):
            cur = cur[p]
        else:
            return default
    return cur


def _context_to_fields(ctx: dict) -> dict:
    """从新版 window.context DATA 里提取 loader 兼容字段。"""
    out = {}
    if not isinstance(ctx, dict):
        return out
    res = ctx.get("result", {}) or {}
    # 标题
    subject = _ctx_get(res, "data", "gallery", "fields", "subject") or \
        _ctx_get(res, "global", "globalData", "model", "offerDetail", "subject")
    if subject:
        out["name"] = _clean_text(subject)
    # 价格: 优先现成价格区间字符串(skuPriceScale='30.00-32.00'), 否则阶梯价最小值
    scale = _ctx_get(res, "data", "Root", "fields", "dataJson", "skuModel", "skuPriceScale")
    if scale:
        out["price"] = _parse_price_range(str(scale))
        out["price_range"] = str(scale)
    if "price" not in out:
        rp = _ctx_get(res, "data", "Root", "fields", "dataJson", "orderParamModel",
                      "orderParam", "skuParam", "skuRangePrices")
        if isinstance(rp, list) and rp:
            prices = []
            for it in rp:
                p = it.get("price")
                if p is not None:
                    prices.append(_parse_price_range(str(p)))
            if prices:
                out["price"] = min(prices)
                out["price_range"] = f"{min(prices):.2f}-{max(prices):.2f}"
    if "price" not in out:
        sp = _ctx_get(res, "data", "shippingServices", "fields", "price")
        if sp is not None:
            out["price"] = _parse_price_range(str(sp))
    # item_id
    oid = _deep_search(res, ("offerId",)) or _deep_search(res, ("offer_id",))
    if oid is not None:
        out["item_id"] = str(oid).split(".")[0]
    # 类目
    cid = _ctx_get(res, "global", "globalData", "model", "offerDetail", "leafCategoryId")
    if cid is not None:
        out["cat_id"] = str(cid)
    cname = _ctx_get(res, "global", "globalData", "model", "offerDetail", "leafCategoryName")
    if cname:
        out["category_name"] = _clean_text(cname)
    # 销量
    sc = _ctx_get(res, "data", "Root", "fields", "dataJson", "tempModel", "saledCount") or \
        _ctx_get(res, "data", "Root", "fields", "dataJson", "orderParamModel", "orderParam", "saledCount")
    if sc is not None:
        out["saleCount"] = str(sc)
    else:
        sc2 = _ctx_get(res, "data", "productTitle", "fields", "saleNum")
        if sc2 is not None:
            out["saleCount"] = str(sc2)
    # 重量(kg)
    wt = _ctx_get(res, "data", "productPackInfo", "fields", "unitWeight") or \
        _ctx_get(res, "data", "shippingServices", "fields", "unitWeight")
    if wt is not None:
        try:
            out["weight_kg"] = float(wt)
        except (TypeError, ValueError):
            pass
    # 图片
    imgs = _ctx_get(res, "global", "globalData", "model", "offerDetail", "imageList") or \
        _ctx_get(res, "data", "gallery", "fields", "imageList")
    if isinstance(imgs, list):
        urls = []
        for it in imgs:
            if not isinstance(it, dict):
                continue
            u = it.get("fullPathImageURI") or it.get("original") or it.get("imageURI") or it.get("url")
            if u and isinstance(u, str):
                urls.append(u)
        if urls:
            out["images"] = [{"url": u} for u in urls[:8]]
    # 属性(featureAttributes: [{name, value, values, ...}])
    fa = _ctx_get(res, "global", "globalData", "model", "offerDetail", "featureAttributes")
    if isinstance(fa, list):
        d = {}
        for a in fa:
            if not isinstance(a, dict):
                continue
            nm = a.get("name")
            v = a.get("value") or (a.get("values") or [None])[0]
            if nm and v:
                d.setdefault(_clean_text(nm), []).append(_clean_text(v))
        if d:
            out["attributes"] = d
    # SKU: skuProps(组名: 颜色/尺码) + skuInfoMap(键如 '黑色>尺码') 取值
    sku_props = _ctx_get(res, "global", "globalData", "model", "offerDetail", "skuProps") or \
        _ctx_get(res, "data", "Root", "fields", "dataJson", "skuModel", "skuProps")
    sku_map = _ctx_get(res, "data", "Root", "fields", "dataJson", "skuModel", "skuInfoMap") or \
        _ctx_get(res, "global", "globalData", "model", "tradeModel", "skuMap")
    if isinstance(sku_props, list) and isinstance(sku_map, dict):
        pos_vals = [set() for _ in sku_props]
        for key in sku_map:
            parts = str(key).replace("&gt;", ">").split(">")
            for idx, part in enumerate(parts):
                if idx < len(pos_vals):
                    pos_vals[idx].add(_clean_text(part))
        values_list = []
        for idx, grp in enumerate(sku_props):
            gname = grp.get("prop") if isinstance(grp, dict) else None
            names = [{"name": v} for v in sorted(pos_vals[idx]) if v]
            if names:
                values_list.append({"values": names, "prop": _clean_text(gname) if gname else None})
        if values_list:
            out["sourceSku"] = {"skuBase": values_list}
    # 运费: freightInfo.deliveryFee/totalCost + logisticsText
    fi = _ctx_get(res, "data", "shippingServices", "fields", "freightInfo")
    if isinstance(fi, dict):
        fee = fi.get("deliveryFee") or fi.get("totalCost") or fi.get("freeDeliverFee")
        if isinstance(fee, (int, float)) and fee > 0:
            out["express_fee"] = float(fee)
        elif isinstance(fee, str):
            mm = _NUM_RE.search(fee)
            if mm:
                out["express_fee"] = float(mm.group(1))
        lt = fi.get("logisticsText")
        if lt:
            out["freight_text"] = _clean_text(lt)
    return out


def _deep_search(obj, keys: tuple, max_depth: int = 8):
    """递归在嵌套 dict/list 里找首个命中任一 key 的值。"""
    if max_depth <= 0 or obj is None:
        return None
    if isinstance(obj, dict):
        for k, v in obj.items():
            if isinstance(k, str) and k in keys:
                return v
            r = _deep_search(v, keys, max_depth - 1)
            if r is not None:
                return r
        return None
    if isinstance(obj, list):
        for it in obj:
            r = _deep_search(it, keys, max_depth - 1)
            if r is not None:
                return r
    return None


def _clean_text(s) -> str:
    if s is None:
        return ""
    return re.sub(r"\s+", " ", str(s)).strip()


def _parse_price_range(text: str) -> float:
    """'2.50-8.00' -> 2.5 ; '¥8.00' -> 8.0 ; '8' -> 8.0"""
    if not text:
        return 0.0
    t = str(text).replace("¥", "").replace("￥", "")
    mr = _PRICE_RANGE_RE.search(t)
    if mr:
        return float(mr.group(1))
    m = _NUM_RE.search(t)
    return float(m.group(1)) if m else 0.0


def _state_to_fields(state: dict) -> dict:
    """从 INITIAL_STATE 大 JSON 里尽力提取兼容字段。"""
    out = {}
    if not isinstance(state, dict):
        return out
    subject = _deep_search(state, ("subject", "title", "name"))
    if subject:
        out["name"] = _clean_text(subject)
    # 价格: 优先 priceRange.priceText / discountPrice, 再 price / priceRange
    price_candidate = _deep_search(state, ("priceText", "discountPrice", "price", "minPrice"))
    if price_candidate is not None:
        out["price"] = _parse_price_range(str(price_candidate))
    pr = _deep_search(state, ("priceRange",))
    if "price" not in out and isinstance(pr, dict):
        out["price"] = _parse_price_range(str(pr.get("priceText", "")))
    # item_id / offerId
    oid = _deep_search(state, ("offerId", "itemId", "offer_id", "id"))
    if oid is not None:
        out["item_id"] = str(oid)
    # cat_id
    cid = _deep_search(state, ("categoryId", "catId", "category_id", "cateId"))
    if cid is not None:
        out["cat_id"] = str(cid)
    # 销量文本
    sc = _deep_search(state, ("saleQuantity", "saleCount", "dealCount", "soldQuantity"))
    if sc is not None:
        out["saleCount"] = str(sc)
    # 图片
    imgs = _deep_search(state, ("imageList", "images", "imageUrls"))
    if isinstance(imgs, list):
        urls = []
        for it in imgs:
            if isinstance(it, dict) and it.get("original"):
                urls.append(it["original"])
            elif isinstance(it, dict) and it.get("url"):
                urls.append(it["url"])
            elif isinstance(it, str):
                urls.append(it)
        out["images"] = [{"url": u} for u in urls[:8] if u]
    # 属性: 支持 [{"name":k,"value":v}] 或 {k:[v]} 或 {k:v}
    attrs_raw = _deep_search(state, ("attributes", "productFeatures", "props"))
    if isinstance(attrs_raw, list):
        d = {}
        for a in attrs_raw:
            if isinstance(a, dict) and a.get("name") is not None:
                k = _clean_text(a.get("name"))
                v = a.get("value")
                d.setdefault(k, []).append(_clean_text(v))
        if d:
            out["attributes"] = d
    elif isinstance(attrs_raw, dict):
        d = {}
        for k, v in attrs_raw.items():
            if isinstance(v, list):
                d[k] = [_clean_text(x) for x in v]
            else:
                d[k] = [_clean_text(v)]
        if d:
            out["attributes"] = d
    # SKU -> loader 期望 {skuBase:[{values:[{name}]}]}
    sku = _deep_search(state, ("sku",))
    if isinstance(sku, dict):
        groups = sku.get("skuBaseProps") or sku.get("skuProps") or []
        values_list = []
        for g in groups:
            vals = g.get("values") or g.get("skuValues") or []
            names = []
            for v in vals:
                nm = v.get("name") if isinstance(v, dict) else str(v)
                if nm:
                    names.append({"name": _clean_text(nm)})
            if names:
                values_list.append({"values": names})
        if values_list:
            out["sourceSku"] = {"skuBase": values_list}
    # 运费: 1688 运费结构复杂(按地区), 尽力抓首重数字
    fr = _deep_search(state, ("freight", "transport", "freightInfo"))
    if isinstance(fr, dict):
        # 常见: {"toFee": "...", "baseFee": 6.0} 或字符串
        for kk in ("baseFee", "firstFee", "toFee", "fee"):
            if isinstance(fr.get(kk), (int, float)):
                out["express_fee"] = float(fr[kk])
                break
            if isinstance(fr.get(kk), str):
                m = _NUM_RE.search(fr[kk])
                if m:
                    out["express_fee"] = float(m.group(1))
                    break
    return out


def _fill_from_dom(html: str, data: dict) -> dict:
    """DOM 文本兜底: 当 INITIAL_STATE 没覆盖时, 用正则从页面文本抓标题/价格/运费/销量/属性表。"""
    # 标题
    if not data.get("name"):
        m = re.search(r"<h1[^>]*class=\"[^\"]*title[^\"]*\"[^>]*>(.*?)</h1>", html, re.S | re.I)
        if m:
            data["name"] = _clean_text(re.sub(r"<[^>]+>", "", m.group(1)))
        else:
            m = re.search(r"<title>(.*?)</title>", html, re.S | re.I)
            if m:
                data["name"] = _clean_text(m.group(1)).replace("- 1688", "").replace("阿里巴巴", "").strip()
    # 价格
    if "price" not in data or not data.get("price"):
        m = re.search(r"<span[^>]*class=\"[^\"]*price[^\"]*\"[^>]*>(.*?)</span>", html, re.S | re.I)
        if m:
            p = _parse_price_range(re.sub(r"<[^>]+>", "", m.group(1)))
            if p:
                data["price"] = p
    # 运费
    if "express_fee" not in data:
        # 包邮
        if re.search(r"包邮", html):
            data["express_fee"] = 0.0
            data["freight_text"] = "包邮"
        else:
            m = _FREIGHT_TEXT_RE.search(html)
            if m:
                data["express_fee"] = float(m.group(1))
                data["freight_text"] = m.group(0)
    # 销量
    if not data.get("saleCount"):
        m = _MONTH_SALE_RE.search(html)
        if m:
            data["saleCount"] = _clean_text(m.group(1))
    # 属性表 #mod-detail-attributes
    if "attributes" not in data:
        block = re.search(r"id=\"mod-detail-attributes\".*?</table>", html, re.S | re.I)
        if block:
            d = {}
            for km, vm in _ATTR_KV_RE.findall(block.group(0)):
                k = _clean_text(re.sub(r"<[^>]+>", "", km))
                v = _clean_text(re.sub(r"<[^>]+>", "", vm))
                if k and v:
                    d[k] = [v]
            if d:
                data["attributes"] = d
    return data


_IMG_URL_RE = re.compile(
    r'(?:src|data-src|data-original|data-lazyload-src|data-ks-lazyload)="([^"]*'
    r'(?:jpg|jpeg|png|webp|gif)[^"]*)"', re.I)
_DEF_IMG_RE = re.compile(
    r'(?:src|data-src|data-original|data-lazyload-src|data-ks-lazyload)\s*=\s*'
    r'["\']([^"\']*?(?:jpg|jpeg|png|webp)[^"\']*?)["\']', re.I)


# 1688 平台通用图: 实测在多个完全无关的商品详情页重复出现, 且以
# xxx.jpg_60x60q90.jpg 的 UI 缩略图形式加载, 不是该商品的详情图。
# 若当作详情图上传到 PDD 会造成「图文不符」的合规风险, 一律剔除。
_GENERIC_IMG_IDS = ("22185873824_536529798",)


def _norm_img(u: str) -> str:
    """规范化图片 URL, 过滤占位/loading 图; 返回空表示丢弃。"""
    u = (u or "").strip()
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("/"):
        return ""  # 相对路径/站内占位, 丢弃
    if not u.startswith("http"):
        return ""
    low = u.lower()
    if any(k in low for k in ("loading", "placeholder", "lazyload.gif",
                              "blank", "spacer", "1x1", "pixel", "gray", "default_")):
        return ""
    if any(g in low for g in _GENERIC_IMG_IDS):
        return ""
    # 源头就归一到原图: 去掉 _60x60q90 / _.webp / _sum.jpg 等变体后缀,
    # 否则采集数据里同一张图会存成多份, 下游发布时会重复上传或上传模糊缩略图。
    try:
        from sku_map import clean_img_url
        return clean_img_url(u)
    except Exception:
        return u


def _extract_sku_full(ctx: dict) -> dict:
    """从 window.context DATA 提取【完整 SKU】: 维度(skuBase) + 每组合价格/库存(combos)。

    PDD 发布需要: 规格维度(颜色/尺码) + 每个组合(颜色×尺码)的 拼团价 与 库存。
    1688 的 skuInfoMap 形如 { '黑色>M': {price, quantity/skuStock, skuId}, ... }。"""
    out = {"skuBase": [], "combos": []}
    if not isinstance(ctx, dict):
        return out
    res = ctx.get("result", {}) or {}

    # ---- 维度(规格组名 + 取值) ----
    sku_props = (_ctx_get(res, "global", "globalData", "model", "offerDetail", "skuProps")
                 or _ctx_get(res, "data", "Root", "fields", "dataJson", "skuModel", "skuProps")
                 or _deep_search(ctx, ("skuProps",))
                 or [])
    if not isinstance(sku_props, list):
        sku_props = []
    val_img = {}   # 规格值名 -> 规格图(在线页在 value.imageUrl 里)
    if isinstance(sku_props, list):
        for grp in sku_props:
            if not isinstance(grp, dict):
                continue
            gname = grp.get("prop") or grp.get("name")
            vals = []
            # 新版页面用 "value"(单数) + imageUrl; 旧版/穿云箭用 "values" + image
            for v in (grp.get("values") or grp.get("value") or []):
                nm = v.get("name") if isinstance(v, dict) else str(v)
                if not nm:
                    continue
                nm = _clean_text(nm)
                img = _norm_img(v.get("imageUrl") or v.get("image") or "") \
                    if isinstance(v, dict) else ""
                vals.append({"name": nm, "image": img})
                if img:
                    val_img[nm] = img
            if vals:
                out["skuBase"].append(
                    {"prop": _clean_text(gname) if gname else None, "values": vals})
    # 规格图可能来自另一条 skuProps 路径, 按规格值名补回, 保证发布端有图可用
    if val_img:
        for grp in out["skuBase"]:
            for v in grp.get("values") or []:
                if isinstance(v, dict) and not v.get("image"):
                    v["image"] = val_img.get(v.get("name"), "")

    # ---- 每组合价格/库存 ----
    sku_info = (_ctx_get(res, "data", "Root", "fields", "dataJson", "skuModel", "skuInfoMap")
                or _ctx_get(res, "global", "globalData", "model", "tradeModel", "skuMap")
                or {})
    if not isinstance(sku_info, dict):
        sku_info = _deep_search(ctx, ("skuInfoMap", "skuMap")) or {}
    if isinstance(sku_info, dict):
        for key, info in sku_info.items():
            if not isinstance(info, dict):
                info = {}
            # 组合键可能是 '黑色>M' / '黑色>M' / 用 ; | 分隔, 统一规整为 '>'
            combo_key = (str(key).replace("&gt;", ">")
                         .replace("&amp;>", ">").replace(";", ">")
                         .replace("|", ">").replace("：", ">").replace(":", ">"))
            # 新版 1688 skuInfoMap 用 discountPrice(元, 字符串) / canBookCount(可订库存),
            # 老版用 price / quantity; 两套都兼容, 否则逐款价与库存会全变成 0。
            price = (info.get("price") or info.get("skuPrice")
                     or info.get("salePrice") or info.get("originalPrice")
                     or info.get("discountPrice") or info.get("promotionPrice")
                     or info.get("finalPrice") or info.get("priceText"))
            stock = (info.get("quantity") or info.get("skuStock")
                     or info.get("stock") or info.get("amount")
                     or info.get("sellableQuantity") or info.get("canBookCount")
                     or info.get("availableQuantity") or info.get("bookCount"))
            sku_id = info.get("skuId") or info.get("sku_id") or info.get("id")
            p = _parse_price_range(str(price)) if price is not None else 0.0
            try:
                s = int(float(stock)) if stock is not None else 0
            except (TypeError, ValueError):
                s = 0
            if combo_key and (p or s or sku_id):
                specs = [x.strip() for x in combo_key.split(">") if x.strip()]
                out["combos"].append({
                    "key": combo_key, "price": p, "stock": s,
                    "image": val_img.get(specs[0], "") if specs else "",
                    "sku_id": str(sku_id) if sku_id else "",
                })
    return out


def _extract_detail_images(html: str, ctx: dict) -> list:
    """尽力抽取 1688 商品【详情图】(详情页大图, 区别于主图/轮播图)。

    1688 详情图常懒加载, 可能藏在 window.context 的 desc/offerDesc 描述字符串里,
    或在 <div class="...description/desc/detail..."> 容器内。best-effort, 抓不到则回退主图。"""
    urls = []
    # 1) context DATA 中描述字符串(含 <img>)
    if isinstance(ctx, dict):
        for key in ("desc", "offerDesc", "detail", "detailImages", "descImages",
                    "productDesc", "description", "richText", "descInfo"):
            v = _deep_search(ctx, (key,))
            if isinstance(v, str) and len(v) > 100:
                for m in _DEF_IMG_RE.finditer(v):
                    u = _norm_img(m.group(1))
                    if u:
                        urls.append(u)
    # 2) html 描述容器(详情图区)
    for m in re.finditer(
            r'<div[^>]*(?:description|desc|detail|offer-desc)[^>]*>(.*?)</div>',
            html, re.S | re.I):
        for im in _DEF_IMG_RE.finditer(m.group(1)):
            u = _norm_img(im.group(1))
            if u:
                urls.append(u)
    # 3) 全局兜底: 1688 图床且路径含 desc/detail 的图片
    for m in _IMG_URL_RE.finditer(html):
        u = _norm_img(m.group(1))
        if u and any(k in u.lower() for k in ("desc", "detail", "offer", "cbu")):
            urls.append(u)
    seen, out = set(), []
    for u in urls:
        if u in seen:
            continue
        seen.add(u)
        out.append(u)
    return out[:40]


# ---------- 1688 详情描述 CDN(异步详情图补全) ----------
# 新版 1688 把「详情描述」放在 window.context 的 detailUrl 指向的 CDN 上,
# 页面内联 HTML 里通常只有主图, 直接解析只能得到 0~1 张详情图(甚至 0 张)。
# 实测 https://itemcdn.tmall.com/1688offer/icossXXXX 返回:
#   var offer_details={"content":"<div ...><img src=\"https://cbu01.alicdn.com/...jpg\">..."}
# 里面才是详情页真正展示的长图。补齐它才能做到「图片全部搬过来」。
_DETAIL_CDN_RE = re.compile(
    r'"detailUrl"\s*:\s*"(https?://itemcdn\.tmall\.com/1688offer/[^"\s]+)"', re.I)
_CDN_IMG_RE = re.compile(
    r'(?:src|data-src|data-original)\s*=\s*["\']([^"\']+?\.(?:jpg|jpeg|png|webp)[^"\']*)["\']',
    re.I)
_CDN_RAW_IMG_RE = re.compile(
    r'https?://[^"\'\s<>\\]+?\.(?:jpg|jpeg|png|webp)[^"\'\s<>\\]*', re.I)
_CDN_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
           "(KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36")


def _parse_detail_cdn_text(text: str) -> list:
    """解析详情 CDN 返回的 JS 文本 -> 归一化后的详情图 URL(保序去重)。"""
    if not text:
        return []
    content = text
    m = re.match(r'\s*var\s+offer_details\s*=\s*', text)
    if m:
        try:
            obj = json.loads(text[m.end():].strip().rstrip(";").strip())
            content = obj.get("content") or ""
        except Exception:
            content = text
    raw = _CDN_IMG_RE.findall(content) or _CDN_RAW_IMG_RE.findall(content)
    out, seen = [], set()
    for u in raw:
        n = _norm_img(u)
        if not n:
            continue
        k = n.split("?")[0]
        if k in seen:
            continue
        seen.add(k)
        out.append(n)
    return out


def fetch_detail_cdn_images(html: str, item_id: str = "", page=None) -> list:
    """按页面 detailUrl 拉取异步详情描述, 补全真正的详情长图。
    优先复用 Playwright 请求上下文(带真实 cookie), 失败再用 urllib 兜底;
    任何异常都静默返回空列表, 绝不影响主采集流程。"""
    m = _DETAIL_CDN_RE.search(html or "")
    if not m:
        return []
    url = m.group(1).replace("\\/", "/")
    headers = {
        "User-Agent": _CDN_UA,
        "Referer": f"https://detail.1688.com/offer/{item_id}.html" if item_id
                   else "https://detail.1688.com/",
    }
    text = ""
    try:
        if page is not None:
            resp = page.request.get(url, headers=headers, timeout=15000)
            if resp.ok:
                text = resp.text()
    except Exception:
        pass
    if not text:
        try:
            import urllib.request
            req = urllib.request.Request(url, headers=headers)
            with urllib.request.urlopen(req, timeout=15) as r:
                text = r.read().decode("utf-8", errors="ignore")
        except Exception:
            return []
    return _parse_detail_cdn_text(text)


def parse_1688_html(html: str, item_id: str = "", extra_detail_images=None) -> dict:
    """把 1688 商品详情页 HTML 解析成 loader 兼容 dict。纯函数、零依赖、容错。
    优先解析【新版】window.context 内嵌数据(1688 已弃用 __INITIAL_STATE__),
    再用旧 __INITIAL_STATE__ 补充, 最后 DOM 文本兜底。"""
    data: dict = {}
    ctx = _extract_context_data(html)
    if ctx:
        data.update(_context_to_fields(ctx))
    # 旧结构 / 补充
    state = _extract_initial_state(html)
    if state:
        data.update(_state_to_fields(state))
    data = _fill_from_dom(html, data)

    # ---- 为 PDD 全量搬迁补齐: 统一用 sku_map 归一化(与穿云箭缓存同结构) ----
    import sku_map
    full_sku = _extract_sku_full(ctx) if ctx else {"skuBase": [], "combos": []}
    merged = dict(data.get("sourceSku") or {})
    if not merged.get("skuBase"):
        merged["skuBase"] = full_sku.get("skuBase") or []
    if full_sku.get("combos"):
        merged["combos"] = full_sku["combos"]
    dims, combos = sku_map.normalize_source_sku(merged)
    data["sourceSku"] = merged
    data["sku_dims"] = dims
    data["sku_combos"] = combos
    # 主图统一成 URL 字符串(images 原本是 [{url:...}] )
    main = sku_map.normalize_images(data.get("images") or [])
    data["images"] = main
    # 详情图: 优先 detail HTML(穿云箭), 再补在线页 HTML 抠图; 与主图去重
    detail_html = data.get("detail") or ""
    details = sku_map.extract_detail_images(detail_html, main)
    main_keys = {x.split("?")[0] for x in main}
    seen_keys = {x.split("?")[0] for x in details}
    # 在线采集时额外的详情 CDN 图会由 fetch_detail_cdn_images() 传入,
    # 与页面内联候选一起做同一套归一 + 主图/SKU 去重。
    candidates = list(_extract_detail_images(html, ctx))
    candidates += [_norm_img(u) for u in (extra_detail_images or [])]
    for u in candidates:
        if not u:
            continue
        k = u.split("?")[0]
        if k not in main_keys and k not in seen_keys:
            seen_keys.add(k)
            details.append(u)
    data["detail_images"] = details
    data["detail_html"] = detail_html
    data["category_name"] = data.get("category_name") or ""

    # item_id 兜底: 从入参 / DOM 文本里的 offer/(\d+)
    if not data.get("item_id"):
        m = re.search(r"offer/(\d+)\.html", html)
        data["item_id"] = m.group(1) if m else (item_id or "")
    if item_id and not data.get("item_id"):
        data["item_id"] = item_id
    if not data.get("item_id"):
        data["item_id"] = item_id or ""

    # 必填字段规范化
    data.setdefault("name", "")
    data.setdefault("price", 0.0)
    data.setdefault("saleCount", "")
    data.setdefault("cat_id", "")
    data.setdefault("express_fee", 0.0)
    data.setdefault("item_url", f"https://detail.1688.com/offer/{data['item_id']}.html" if data["item_id"] else "")
    data.setdefault("images", [])
    data.setdefault("attributes", {})
    data.setdefault("sourceSku", {})
    # 数值清洗
    try:
        data["price"] = float(data.get("price") or 0)
    except (TypeError, ValueError):
        data["price"] = 0.0
    try:
        data["express_fee"] = float(data.get("express_fee") or 0)
    except (TypeError, ValueError):
        data["express_fee"] = 0.0
    data["itemSource"] = 2  # 1688
    return data


def _read_html_file(path: str) -> str:
    try:
        with open(path, encoding="utf-8") as f:
            return f.read()
    except Exception:
        try:
            with open(path, encoding="utf-8-sig") as f:
                return f.read()
        except Exception:
            return ""


def collect_from_html_files(paths: list) -> list:
    """无浏览器路径: 直接解析本地保存的 1688 商品页 HTML。"""
    out = []
    for p in paths:
        html = _read_html_file(p)
        if not html:
            continue
        iid = re.search(r"(\d{8,20})", os.path.basename(p))
        out.append(parse_1688_html(html, item_id=iid.group(1) if iid else ""))
    return [o for o in out if o.get("item_id")]


# ---------- 登录态/拦截判定增强 ----------
# 1688 登录墙/风控关键词: 命中且无明显商品数据 => 被拦截
_LOGIN_WALL_MARKERS = re.compile(
    r"(请登录|登录后查看|扫码登录|账号密码登录|短信登录|"
    r"拖动滑块|滑块验证|安全验证|人机验证|滑动验证|"
    r"去登录|立即登录|登录1688|登录淘宝|您还没有登录|"
    r"请先登录|验证中心|风险验证)", re.I)
# x5sec 风控重定向页特征(仅一个极小跳转脚本, 没有 __INITIAL_STATE__)。
# 注意: 不能包含 'windvane' —— 它是阿里系每个正常页面都加载的 JS SDK, 会误判真实商品页为风控页。
_X5SEC_REDIRECT_RE = re.compile(r"x5referer|localStorage\.x5sec|x5security", re.I)
# 滑块/人机验证码页面(用户在浏览器里手动滑动才能拿到 x5sec): 比纯 x5sec 重定向页更广,
# 覆盖『亲, 请按照说明进行验证哦』这种带滑块的验证墙(页面较大, 不是极小的 windvane 跳转页)。
_CAPTCHA_RE = re.compile(
    r"(请按照说明进行验证|验证哦|拖动滑块|拖动下方滑块|滑动验证|滑块验证|"
    r"人机验证|安全验证|请完成安全验证|点击按钮进行验证|"
    r"请选择符合描述|选择下图中所有|请点击图中|验证图片|点选验证|"
    r"verify you are human|please slide|nc\.js|slide.{0,6}?verify|slider)", re.I)

# x5sec 安全验证拦截页(punish/_tmd_)的 URL 特征: 出现这些即代表『需要人工通过验证』,
# 此时【绝不能重新 goto 刷新页面】, 否则验证码会不停重置, 用户永远提交不了。
_PUNISH_RE = re.compile(r"punish|_tmd_|x5secdata", re.I)


def _on_punish_page(page) -> bool:
    """当前是否停留在 x5sec 安全验证(punish)页 —— 由 URL 判定, 比正文更可靠。"""
    try:
        return bool(_PUNISH_RE.search(page.url or ""))
    except Exception:
        return False


def _has_x5sec(ctx) -> bool:
    """当前浏览器上下文是否已拿到 .1688.com 域的 x5sec 风控凭证(通过滑块后才会下发)。"""
    try:
        return any(c.get("name") == "x5sec"
                   for c in ctx.cookies()
                   if (c.get("domain") or "").endswith("1688.com"))
    except Exception:
        return False


def _is_captcha_page(html: str) -> bool:
    """判断当前页是否处于滑块/人机验证码状态(需要用户手动通过才能继续拿 x5sec)。"""
    if not html:
        return False
    if _CAPTCHA_RE.search(html):
        return True
    if _X5SEC_REDIRECT_RE.search(html) and len(html) < 20000:
        return True
    return False


def _is_logged_in(cookies) -> bool:
    """可靠的 1688 登录判定: 检查 .1688.com / .taobao.com 域下的真实会话 cookie。
    关键坑: unb 是访客也会下发的 base cookie, 之前用它判断登录 => 把『访客态』误判为
    『已登录』, 保存了无效登录态, 导致采集永远被拦。这里改用 cookie2 / __cn_logon__ /
    login_aliyunid / sgcookie 等真正的会话凭证。"""
    wanted = {"cookie2", "__cn_logon__", "login_aliyunid", "sgcookie", "_hvn_lgc_"}
    for c in (cookies or []):
        if c.get("name") not in wanted:
            continue
        d = (c.get("domain") or "")
        if d.endswith("1688.com") or d.endswith("taobao.com"):
            return True
    return False


def _is_blocked(html: str, url: str = "") -> bool:
    """判断 1688 详情页是否真的被登录墙/风控拦截(没有商品数据)。
    1688 详情页已弃用 __INITIAL_STATE__, 改挂 window.context(DATA); 只要能从新结构提取到
    商品 subject, 即为真实商品页, 不算拦截。综合判断:
      1) URL 被重定向到 login/verify/x5sec/security => 拦截
      2) 新版 window.context 能提取到商品 subject => 未拦截
      3) 旧版 __INITIAL_STATE__ 存在 => 未拦截
      4) 命中登录墙关键词 => 拦截
      5) 极小页面 + x5referer/x5security 风控特征 => 拦截(x5sec 缺失)
      6) 命中滑块/验证码墙(非大商品页) => 拦截
      7) 其余(无数据也无明确特征) => 保守视为异常拦截
    """
    if not html:
        return True
    if url and re.search(r"(login|verify|x5sec|security|risk|sec\.1688)", url, re.I):
        return True
    ctx = _extract_context_data(html)
    if ctx is not None:
        subj = (_ctx_get(ctx, "result", "data", "gallery", "fields", "subject")
                or _ctx_get(ctx, "result", "global", "globalData", "model", "offerDetail", "subject"))
        if subj:
            return False  # 新结构能提取到商品标题 => 真实商品页
    if _extract_initial_state(html) is not None:
        return False  # 旧结构有初始数据, 不是拦截
    if _LOGIN_WALL_MARKERS.search(html):
        return True
    if _X5SEC_REDIRECT_RE.search(html) and len(html) < 20000:
        return True
    if _CAPTCHA_RE.search(html) and len(html) < 50000:
        return True
    return True


def _new_context(pw, headless: bool, channel: str = None, storage_state=None, profile_dir: str = None):
    """创建浏览器上下文。
    - profile_dir 给定时, 用【真实持久化 profile】(launch_persistent_context)启动:
      1688 会把它当成『真实浏览器』(有真实指纹/历史), 不会在验证码上疯狂自动刷新/重置;
      同时 cookie/localStorage 自然落盘到 profile, 跨采集自动复用登录态, 无需反复注入 storage_state。
      这是解决『验证码每几秒刷新、点图片就重置、永远提交不了』的关键。
    - 否则用临时上下文(无持久化 profile), 通过 storage_state 注入登录态(旧逻辑, 作为 profile 不可用时的兜底)。
    返回: Playwright context(调用方统一用 ctx.close() 关闭即可, 持久化上下文 close 会连带关浏览器)。"""
    args = ["--no-sandbox", "--disable-blink-features=AutomationControlled"]
    if profile_dir:
        try:
            os.makedirs(profile_dir, exist_ok=True)
            ctx = pw.chromium.launch_persistent_context(
                profile_dir, headless=headless, args=args, channel=channel,
                locale="zh-CN", viewport={"width": 1280, "height": 800})
            try:
                ctx.add_init_script(_STEALTH_JS)
            except Exception:
                pass
            return ctx
        except Exception as e:
            # profile 被其它浏览器实例占用, 或本机没有该引擎 -> 回退临时上下文(带 storage_state 兜底)
            print(f"[warn] 持久化 profile 启动失败, 回退临时上下文: {e}")
    kwargs = dict(headless=headless, args=args)
    if channel:
        kwargs["channel"] = channel
    browser = pw.chromium.launch(**kwargs)  # user_agent 不能传给 launch, 只能放 new_context
    ctx_kwargs = dict(locale="zh-CN", viewport={"width": 1280, "height": 800})
    if not channel:
        ctx_kwargs["user_agent"] = _UA
    if storage_state:
        ctx_kwargs["storage_state"] = storage_state
    ctx = browser.new_context(**ctx_kwargs)
    try:
        ctx.add_init_script(_STEALTH_JS)
    except Exception:
        pass
    return ctx


def _open_login(page, prog=None):
    """打开 1688 登录页(优先专用 signin 页, 失败回退首页)。"""
    for url in ("https://login.1688.com/member/signin.htm", "https://login.1688.com/"):
        try:
            page.goto(url, wait_until="domcontentloaded", timeout=30000)
            return
        except Exception as e:
            if prog:
                prog("warn", f"打开登录页失败, 重试: {e}")
    try:
        page.goto("https://www.1688.com/", wait_until="domcontentloaded", timeout=30000)
    except Exception:
        pass


def _wait_qr(page, prog=None):
    """等待登录页二维码就绪, 并提示可能需要先完成滑块验证。"""
    # 1688 常先弹『拖动滑块完成验证』, 完成后二维码才真正可用; 否则扫码是无效的
    try:
        if page.locator("text=拖动滑块").count() or page.locator("text=请完成验证").count():
            if prog:
                prog("warn", "检测到滑块/人机验证, 请先在浏览器里拖动滑块完成验证, 之后二维码才会生效")
    except Exception:
        pass
    sel = ("xpath=//*[contains(@class,'qr') or contains(@id,'qr') "
           "or contains(@class,'qrcode') or contains(@class,'scan') "
           "or contains(@class,'login-code') or contains(@class,'code')]")
    try:
        page.wait_for_selector(sel, timeout=12000)
    except Exception:
        if prog:
            prog("warn", "未探测到标准二维码元素, 但页面应已加载, 请直接扫码或切密码登录")
    page.wait_for_timeout(1500)  # 再等渲染
    if prog:
        prog("wait", "二维码已就绪, 请用手机 1688 App / 阿里旺旺 扫码")


def _switch_password(page, prog=None) -> bool:
    """尝试切到 1688 登录页的『密码登录』tab(比扫码稳定, 验证全在浏览器内完成)。"""
    candidates = [
        "a:has-text('密码登录')",
        "span:has-text('密码登录')",
        "xpath=//*[self::a or self::button or self::span][contains(., '密码登录')]",
        "text=密码登录",
    ]
    for sel in candidates:
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=1500):
                loc.click(timeout=2000)
                prog("info", "已切到『密码登录』: 请在弹出的浏览器里输入 1688 账号 + 密码; "
                             "若要求短信/滑块验证, 也在浏览器内完成即可(不会卡手机端)")
                page.wait_for_timeout(800)
                return True
        except Exception:
            continue
    prog("warn", "未找到『密码登录』入口, 将仍按扫码方式; 你也可手动在页面点『密码登录』")
    return False


def _qr_expired(page) -> bool:
    """检测 1688 二维码是否已过期/失效(需要刷新)。"""
    try:
        if (page.locator("text=二维码已过期").count() or
                page.locator("text=点击刷新").count() or
                page.locator("text=刷新二维码").count()):
            return True
    except Exception:
        pass
    return False


def _verify_logged(page, prog=None) -> bool:
    """用当前 cookie 判断是否已登录, 并导航到 www.1688.com 固化会话。"""
    try:
        if _is_logged_in(page.context.cookies()):
            try:
                page.goto("https://www.1688.com/", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(1500)
            except Exception:
                pass
            return True
    except Exception:
        pass
    return False


def _handle_keep_login(page, prog=None):
    """1688 登录成功后常弹『保持登录状态』弹窗, 8 秒后自动选『不保持』(会话有效期变短)。
    处理策略: 先把浏览器多留 7 秒, 让用户有机会自己点『保持』; 若用户没点, 我们自动点『保持』
    以延长会话有效期(否则下次采集可能仍需重新登录)。无论是否出现该弹窗都不应报错。"""
    for sel in ("role=button[name='保持']",
                "button:text-is('保持')",
                "xpath=//button[normalize-space(.)='保持']"):
        try:
            loc = page.locator(sel).first
            if loc.count() and loc.is_visible(timeout=7000):
                loc.click(timeout=2000)
                page.wait_for_timeout(1500)
                if prog:
                    prog("info", "已自动选择『保持登录』, 会话将长期有效(你不点也会自动帮你选)")
                return True
        except Exception:
            continue
    return False


def _capture_x5sec(page, ctx, prog, timeout: int = 120) -> bool:
    """登录成功后访问一个详情页, 轮询直到捕获 x5sec 风控凭证(通过滑块验证后才会下发)。

    【关键修复】旧逻辑只等 8 秒就保存并关闭浏览器, 用户根本来不及滑完滑块 => x5sec 永远抓不到,
    保存的是『缺风控凭证』的残态登录态, 之后采集永远被拦。现在最长等待 timeout 秒(默认 2 分钟),
    期间若弹滑块会反复提示你在浏览器里手动滑动, 抓到 x5sec 才返回 True; 超时返回 False
    (仍会保存当前登录态, 但会警告缺 x5sec)。本函数【不会关闭浏览器】, 由调用方统一关闭。"""
    url = f"https://detail.1688.com/offer/{SAMPLE_OFFER}.html"
    try:
        page.goto(url, wait_until="domcontentloaded", timeout=30000)
    except Exception as e:
        if prog:
            prog("warn", f"x5sec 预热访问出错(可忽略): {e}")
        return False
    if _has_x5sec(ctx):
        return True
    warned = False
    refreshed = False  # 仅给一次『纯登录墙(非滑块)』刷新机会, 避免死循环打断滑块
    tick = 0
    deadline = time.time() + timeout
    while time.time() < deadline:
        if _has_x5sec(ctx):
            return True
        try:
            html = page.content()
        except Exception:
            html = ""
        captcha = _is_captcha_page(html) or _on_punish_page(page)
        wall = bool(_LOGIN_WALL_MARKERS.search(html)) and not _has_x5sec(ctx)
        if captcha:
            # 验证码页: 全程只读轮询, 绝不 goto 重载(重载会让 1688 验证码不停重置)。
            # 第一次提示一次操作说明, 之后每 ~9 秒播报一次倒计时, 让界面顶部状态条持续刷新。
            if not warned and prog:
                prog("warn", "详情页弹出滑块/图片验证墙! 请在弹出的浏览器里手动滑动或点选图片完成验证"
                             "(不要关闭窗口, 我们不会刷新它), 我们会等你通过, 最长 2 分钟")
                warned = True
            else:
                tick += 1
                if prog and tick % 3 == 0:
                    elapsed = int(timeout - (deadline - time.time()))
                    prog("wait", f"仍在等待你完成验证… 已等待 {elapsed}s / {timeout}s, 请在浏览器里继续操作"
                                 f"(页面由真实浏览器承载, 验证码不会自动刷新)")
        # 若登录态丢了(纯登录墙而非验证码/验证墙), 给一次刷新机会(仅一次)。
        # 注意: 若当前就在 x5sec 验证(punish)页, 绝不刷新, 否则验证码会不停重置、提交不了。
        if wall and not captcha and not _on_punish_page(page) and not refreshed:
            try:
                page.goto("https://www.1688.com/", wait_until="domcontentloaded", timeout=15000)
                page.wait_for_timeout(1500)
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
            except Exception:
                pass
            refreshed = True
        time.sleep(3)
    return _has_x5sec(ctx)


def interactive_login(state_path: str = None, progress=None, stop_event=None,
                      refresh_event=None, prefer: str = "qr") -> bool:
    """一次性有头登录: 弹出真实浏览器让用户登录1688(扫码/账密), 登录态持久化到本地 profile。
    返回 True 表示拿到登录态。仅在用户机器(有显示器)执行。
    - stop_event:   GUI 点『我已登录』时置位, 立即保存并退出浏览器(自动探测失败时的兜底)
    - refresh_event: GUI 点『刷新二维码』时置位, 重新打开登录页/刷新二维码(不自动刷新, 避免
                    作废用户已扫但还没确认的二维码——这正是手机端一直『正在加载』的主因)
    - prefer:       'qr'(默认, 扫码) 或 'password'(账号密码登录, 最稳, 验证全在浏览器内完成)"""
    from playwright.sync_api import sync_playwright
    state_path = state_path or STATE_PATH

    def prog(k, m):
        if progress:
            progress(k, m)

    prog("login", "正在打开登录窗口…")
    with sync_playwright() as pw:
        ctx = None
        last_err = None
        # 优先用【真实持久化 profile】启动本机真实 Chrome/Edge(1688 最信任, 验证码不会疯狂刷新),
        # 失败回退 Playwright 自带 Chromium。profile 锁定相同引擎(存 1688_profile_channel.txt)。
        preferred = _read_profile_channel()
        _channels = []
        if preferred:
            _channels.append(preferred)
        for c in ("chrome", "msedge", None):
            if c not in _channels:
                _channels.append(c)
        for ch in _channels:
            try:
                ctx = _new_context(pw, headless=False, channel=ch, profile_dir=PROFILE_DIR)
                if ch:
                    prog("info", f"使用本机 {ch} 真实浏览器登录(持久化 profile, 验证码不会自动刷新)")
                break
            except Exception as e:
                last_err = e
                prog("warn", f"{'Playwright Chromium' if ch is None else ch} 启动失败: {e}")
        if ctx is None:
            prog("err", f"无法启动任何浏览器进行登录: {last_err}")
            return False
        _write_profile_channel(ch or "")
        page = ctx.new_page()
        _open_login(page, prog)
        if prefer == "password":
            _switch_password(page, prog)
        else:
            _wait_qr(page, prog)
        logged = False
        for i in range(150):  # 最多约 7.5 分钟
            if stop_event and stop_event.is_set():
                prog("wait", "检测到『我已登录』, 正在保存登录态…")
                logged = _verify_logged(page, prog)
                break
            if refresh_event and refresh_event.is_set():
                refresh_event.clear()
                prog("wait", "手动刷新登录页/二维码…")
                _open_login(page, prog)
                if prefer != "password":
                    _wait_qr(page, prog)
                continue
            try:
                if _is_logged_in(ctx.cookies()):
                    logged = True
                    break
            except Exception:
                pass
            if prefer != "password" and _qr_expired(page):
                prog("warn", "二维码已过期/失效, 请点本页『刷新二维码』重新获取后再扫")
            else:
                tip = ("请在浏览器输入 1688 账号 + 密码, 并完成页面上的短信/滑块验证"
                       if prefer == "password"
                       else "请手机扫码并在手机点『确认登录』(若需短信/滑块验证, 先在本机浏览器完成)")
                prog("wait", f"{tip} …(已等待 {(i + 1) * 3}s)")
            time.sleep(3)
        if not logged:
            # 兜底: 打开商品页验证是否仍被拦截
            try:
                page.goto("https://detail.1688.com/offer/613103202040.html",
                          wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)
                if not _is_blocked(page.content()):
                    logged = True
            except Exception:
                pass
        # 登录成功后: 处理『保持登录状态』弹窗(避免 8 秒后自动选『不保持』导致会话短暂),
        # 再把浏览器多留几秒让你可以核对, 然后固化会话并完整保存登录态。
        if logged:
            prog("ok", "登录成功! 若弹出『保持登录状态』, 我们会在 7 秒内自动为你选『保持』(你也可以自己点)")
            _handle_keep_login(page, prog)
            # 固化会话: 导航到 1688 首页, 确保 cookie/localStorage 最终生效
            try:
                page.goto("https://www.1688.com/", wait_until="domcontentloaded", timeout=20000)
                page.wait_for_timeout(2000)
            except Exception:
                pass
            # 关键修复: 登录页不会下发 detail.1688.com 域的 x5sec(详情页风控强依赖)。
            # 主动访问一个详情页, 轮询最多 2 分钟捕获 x5sec 凭证; 若弹滑块, 停留等用户
            # 手动通过(不再像旧版只等 8 秒就关浏览器 => x5sec 抓不到, 登录态残态)。
            try:
                got = _capture_x5sec(page, ctx, prog, timeout=120)
                if got:
                    prog("ok", "✅ 已捕获 x5sec 风控凭证(详情页风控通过)")
                else:
                    prog("warn", "⚠ 2 分钟内未捕获到 x5sec: 你可能没赶在超时前通过滑块, "
                                 "或详情页风控较严。已保存当前登录态, 若采集仍被拦请重新登录并务必通过滑块")
            except Exception as e:
                prog("warn", f"x5sec 预热访问出错(可忽略): {e}")
            # 用 Playwright 原生 storage_state 完整保存(cookie + localStorage)。
            # 1688 这类 Alibaba 系 SPA 的登录态大量依赖 localStorage, 仅注入 cookie 会判定未登录 -> 被拦截。
            try:
                ctx.storage_state(path=state_path)  # 含 cookies + localStorage + origins
                # 落地后再次清洗文件(归一化 sameSite 等), 保证后续 new_context(storage_state=文件) 也能直接加载
                try:
                    with open(state_path, encoding="utf-8") as f:
                        _st = json.load(f)
                    if _st.get("cookies"):
                        _st["cookies"] = _sanitize_cookies(_st["cookies"])
                    with open(state_path, "w", encoding="utf-8") as f:
                        json.dump(_st, f, ensure_ascii=False)
                except Exception:
                    pass
                _write_channel_marker(ch or "")
                # 统计保存的 cookie 数 + 是否拿到 x5sec 用于提示
                try:
                    with open(state_path, encoding="utf-8") as f:
                        _st = json.load(f)
                    n_cookies = len(_st.get("cookies", []))
                    _have_x5 = any(c.get("name") == "x5sec" for c in _st.get("cookies", []))
                except Exception:
                    n_cookies, _have_x5 = 0, False
                prog("ok", f"已保存登录态(含 {n_cookies} 个 cookie + 本地会话, 引擎={ch or 'Chromium'})"
                          f"到本地, 采集时自动复用, 无需重新登录"
                          + ("；✅ 已捕获 x5sec 风控凭证" if _have_x5 else
                             "；⚠ 未捕获到 x5sec(详情页风控凭证), 若采集仍被拦多半是它缺失, "
                             "请重新点『登录1688』并确保浏览器里通过了滑块验证"))
            except Exception as e:
                prog("warn", f"保存登录态失败: {e}")
        ctx.close()
    prog("ok" if logged else "warn",
         "1688 登录态已保存, 可返回重新采集" if logged else "未检测到登录态, 点『我已登录』或确认手机已完成验证")
    return logged


def collect(links: list, headless: bool = True, save_dir: str = None,
            progress=None, state_path: str = None) -> list:
    """在线抓取: 用 Playwright 打开每个 1688 链接, 等渲染后解析。
    - 通过 storage_state(含 cookie + localStorage) 注入已保存登录态, 并与登录使用同一浏览器引擎,
      彻底避开『只注入 cookie 导致 Alibaba SPA 判定未登录』与『持久化 profile 跨引擎冲突』两类问题
    - 若无头(headless)模式下全部被拦截, 自动改用有头(headless=False)重试一次(1688 对无头更敏感)
    - 若仍全部被拦截(登录态失效), 返回空并在 progress 提示先重新登录
    progress(callable): 传入 (阶段, 文本) 用于 GUI 进度/日志。
    返回 loader 兼容 dict 列表。"""
    from playwright.sync_api import sync_playwright  # 惰性导入, 无浏览器时不崩

    ids = parse_links("\n".join(links)) if any("/" in l for l in links) else parse_links(" ".join(links))
    if not ids:
        ids = [str(x).strip() for x in links if str(x).strip()]
    results, failed = [], []

    state_path = state_path or STATE_PATH
    storage = _load_storage_state(state_path) if os.path.exists(state_path) else None
    if not storage:
        if progress:
            progress("warn", "未找到已保存登录态, 将匿名抓取(极可能被拦截), 请先『登录1688』")
    # 采集优先复用登录时的真实 profile 引擎(写在 1688_profile_channel.txt), 否则尝试其它引擎
    rec = _read_profile_channel() or _read_channel_marker()
    channels = [rec] if rec else []
    for c in ("chrome", "msedge", None):
        if c not in channels:
            channels.append(c)

    def _run_once(pw, hl):
        """在给定 playwright 实例上用指定 headless 跑一遍采集, 返回 (res, fails, blocked_all)。"""
        r_res, r_fail, r_blocked = [], [], True
        ctx = None
        # 采集复用登录时的真实持久化 profile(同一引擎), 自动带上登录态且 1688 不弹疯狂刷新验证码;
        # profile 不可用时(被占用/无该引擎)回退临时上下文 + storage_state 注入。
        for ch in channels:
            try:
                ctx = _new_context(pw, headless=hl, channel=ch,
                                   storage_state=storage, profile_dir=PROFILE_DIR)
                if ch and progress:
                    progress("info", f"使用本机 {ch} 真实浏览器抓取(持久化 profile, 已自动复用登录态)")
                break
            except Exception as e:
                if progress:
                    progress("warn", f"{'Playwright Chromium' if ch is None else ch} 启动失败: {e}")
        if ctx is None:
            if progress:
                progress("err", "无法启动任何浏览器进行抓取")
            return r_res, r_fail, r_blocked
        # 【关键修复】持久化 profile 分支不会注入 storage_state。若 profile 里的会话 cookie
        # 被清理/跨引擎重建过(很常见), 采集就变成匿名访问, 1688 直接弹登录墙/滑块。
        # 这里检测关键会话 cookie, 缺失时把『登录1688』保存的 state 补注入, 自动恢复登录态。
        if storage:
            try:
                cur = {c.get("name") for c in ctx.cookies()}
                if not ({"cookie2", "_tb_token_", "unb"} & cur):
                    ctx.add_cookies(storage.get("cookies") or [])
                    if progress:
                        progress("info", "持久化 profile 登录态已失效, 已自动补注入 "
                                         f"{len(storage.get('cookies') or [])} 个已保存 cookie")
            except Exception as e:
                if progress:
                    progress("warn", f"补注入登录态失败(将按当前 profile 继续): {e}")
        page = ctx.new_page()
        # 预热: 先访问 www.1688.com 刷新/补发 x5sec 风控凭证(详情页强依赖),
        # 再抓详情页 —— 避免一上来就撞风控重定向页。
        try:
            page.goto("https://www.1688.com/", wait_until="domcontentloaded", timeout=20000)
            page.wait_for_timeout(2000)
        except Exception:
            pass
        # 诊断: 注入后是否真带上了关键登录 cookie(尤其是 x5sec)
        try:
            names = {c["name"] for c in ctx.cookies()}
            key = {"unb", "login_aliyunid", "x5sec", "_tb_token_", "cookie2"} & names
            if progress:
                if key:
                    progress("info", f"登录态已注入, 关键 cookie: {', '.join(sorted(key))}")
                    if "x5sec" not in names:
                        # 实测: 已有 unb/cookie2/_tb_token_ 时缺 x5sec 也能正常采集,
                        # 只有真的命中登录墙才需要重新登录, 这里不再误报成"必然被拦"。
                        progress("info", "未检测到 x5sec(详情页风控凭证); 若后续出现登录墙, "
                                         "再点『登录1688』重新捕获即可")
                else:
                    progress("warn", "注入后未检测到关键登录 cookie(可能登录态已失效, 请重新登录)")
        except Exception:
            pass
        for i, iid in enumerate(ids, 1):
            url = f"https://detail.1688.com/offer/{iid}.html"
            try:
                if progress:
                    progress("fetch", f"[{i}/{len(ids)}] 抓取 {url}")
                page.goto(url, wait_until="domcontentloaded", timeout=30000)
                page.wait_for_timeout(3000)  # 等 JS 渲染
                html_before = page.content()
                # 1688 详情图在页面下方且懒加载(data-src → src), 不滚动只能拿到主图。
                # 分段滚动触发加载后再取完整 HTML; 若滚动意外触发风控, 回退滚动前快照。
                try:
                    for _ in range(5):
                        page.evaluate(
                            "window.scrollBy(0, Math.max(700, "
                            "Math.round((document.body.scrollHeight || 8000) / 7)))")
                        page.wait_for_timeout(350)
                    page.wait_for_timeout(900)
                except Exception:
                    pass
                html = page.content()
                if _is_blocked(html, url=url) and not _is_blocked(html_before, url=url):
                    html = html_before
                blocked = _is_blocked(html, url=url)
                # 有头模式下, 若弹的是滑块/图片验证码/登录墙(用户可手动处理), 给最多 90 秒等待通过。
                # 【关键】全程【不重新 goto】! 否则每 3 秒重载一次页面会让验证码不停刷新,
                # 用户永远赶不上、也提交不了(正是『刷新频率太快』的根因)。只在原 tab 轮询,
                # 检测到用户通过(离开 punish/_tmd_ 验证页 且不再是验证码页)才继续。
                on_punish = _on_punish_page(page)
                is_captcha = bool(_CAPTCHA_RE.search(html)) or bool(_LOGIN_WALL_MARKERS.search(html)) or on_punish
                if blocked and is_captcha and not hl:
                    if progress:
                        progress("warn", f"  ⚠ {iid} 详情页弹出验证/登录墙, 请在浏览器里手动通过"
                                         f"(不要关窗口, 我们不会刷新它), 等待最多 90 秒…")
                    _dl = time.time() + 90
                    passed = False
                    _tick = 0
                    while time.time() < _dl:
                        time.sleep(3)
                        _tick += 1
                        try:
                            cur_url = page.url or ""
                            cur_html = page.content()
                        except Exception:
                            continue
                        # 通过验证后会跳离 punish/_tmd_ 页, 且页面不再是验证码页
                        if not _PUNISH_RE.search(cur_url) and not _is_captcha_page(cur_html):
                            html = cur_html
                            blocked = False
                            passed = True
                            break
                        # 周期性播报倒计时, 让界面顶部状态条持续刷新(不会让你以为卡死)
                        if progress and _tick % 3 == 0:
                            progress("wait", f"  仍在等你完成 {iid} 的验证… 已等待 {int(90 - (_dl - time.time()))}s / 90s")
                    if not passed and progress:
                        progress("warn", f"  ✗ {iid} 90 秒内仍未通过验证, 已存诊断页")
                if blocked:
                    r_fail.append((iid, "被验证码拦截/需登录"))
                    # 保存诊断页, 方便定位是被登录墙还是 x5sec 风控拦截
                    is_x5 = bool(_X5SEC_REDIRECT_RE.search(html) and len(html) < 20000)
                    ts = time.strftime("%Y%m%d_%H%M%S")
                    blk_path = os.path.join(DATA_DIR, f"blocked_{iid}_{ts}.html")
                    try:
                        os.makedirs(DATA_DIR, exist_ok=True)
                        with open(blk_path, "w", encoding="utf-8") as f:
                            f.write(html)
                    except Exception:
                        blk_path = ""
                    if progress:
                        if is_x5:
                            progress("warn", f"  ✗ {iid} 命中 1688 x5sec 风控重定向(缺风控凭证), "
                                             f"已存诊断页 {blk_path}")
                        else:
                            progress("warn", f"  ✗ {iid} 被拦截(登录墙/异常), 已存诊断页 {blk_path}")
                    continue
                r_blocked = False
                # 详情描述是异步 CDN, 单独补抓(页面内联通常只有主图)
                cdn_imgs = fetch_detail_cdn_images(html, iid, page)
                d = parse_1688_html(html, item_id=iid, extra_detail_images=cdn_imgs)
                if cdn_imgs and progress:
                    progress("info", f"  ↳ {iid} 详情CDN候选 {len(cdn_imgs)} 张, "
                                     f"合并后独立详情图 {len(d.get('detail_images') or [])} 张")
                if d.get("name") and d.get("price"):
                    r_res.append(d)
                    if progress:
                        fr = d.get("express_fee") or 0
                        ftxt = str(d.get("freight_text") or "")
                        if fr:
                            fr_note = f"运费¥{fr}"
                        elif any(k in ftxt for k in ("包邮", "免运费", "免邮")):
                            fr_note = "包邮¥0"   # 包邮商品的 0 元是真实值, 别误报成"需补"
                        else:
                            fr_note = "运费需补"
                        progress("ok", f"  ✓ {iid} {d['name'][:18]} ¥{d['price']} {fr_note}")
                else:
                    r_fail.append((iid, "标题/价格未解析"))
                    if progress:
                        progress("warn", f"  ✗ {iid} 解析不全(可能页面结构变化)")
            except Exception as e:
                r_fail.append((iid, str(e)[:60]))
                if progress:
                    progress("err", f"  ✗ {iid} 抓取失败: {e}")
        try:
            ctx.close()
        except Exception:
            pass
        return r_res, r_fail, r_blocked

    if progress:
        progress("start", f"准备抓取 {len(ids)} 个 1688 商品")
    with sync_playwright() as pw:
        results, failed, blocked_all = _run_once(pw, headless)
        # 无头全拦截 -> 自动改用有头重试一次(1688 对无头更敏感, 有头更接近真人环境)
        if blocked_all and headless and not results:
            if progress:
                progress("warn", "无头模式全部被拦截, 改用有头浏览器重试一次(请留意弹出的浏览器)…")
            results, failed, blocked_all = _run_once(pw, False)
            if blocked_all and not results and progress:
                progress("warn", "有头重试仍被拦截: 登录态可能已失效, 请重新点『登录1688』登录")

    if blocked_all and not results and progress:
        progress("done", "全部被拦截: 请先在采集对话框点'登录1688(一次性)'完成登录后再采集")

    if save_dir and results:
        os.makedirs(save_dir, exist_ok=True)
        out_path = os.path.join(save_dir, "collected_1688.json")
        existing = []
        if os.path.exists(out_path):
            try:
                existing = json.load(open(out_path, encoding="utf-8"))
            except Exception:
                existing = []
        merged = {r["item_id"]: r for r in existing}
        for r in results:
            merged[r["item_id"]] = r
        json.dump(list(merged.values()), open(out_path, "w", encoding="utf-8"),
                  ensure_ascii=False, indent=2)
        if progress:
            progress("save", f"已保存/合并 {len(merged)} 个到 {out_path}")
    if progress and failed:
        progress("done", f"完成: 成功 {len(results)}, 被拦截/失败 {len(failed)}")
    return results


if __name__ == "__main__":
    # 离线自测: 用构造的样例 HTML 验证解析(无需浏览器)
    sample = """
    <html><head><title>测试发夹 - 1688</title></head><body>
    <script>window.__INITIAL_STATE__=JSON.parse("{\\"offer\\":{\\"subject\\":\\"网红发夹女气质发卡\\",\\"priceRange\\":{\\"priceText\\":\\"2.50-8.00\\"},\\"offerId\\":\\"643237120099\\",\\"categoryId\\":\\"12345\\",\\"saleQuantity\\":\\"3000\\",\\"imageList\\":[{\\"original\\":\\"https://img/a.jpg\\"}],\\"attributes\\":[{\\"name\\":\\"产地\\",\\"value\\":\\"义乌\\"},{\\"name\\":\\"材质\\",\\"value\\":\\"合金\\"},{\\"name\\":\\"净重\\",\\"value\\":\\"0.05kg\\"}],\\"sku\\":{\\"skuBaseProps\\":[{\\"values\\":[{\\"name\\":\\"黑色\\"},{\\"name\\":\\"米色\\"}]}]},\\"freight\\":{\\"baseFee\\":6.0}}}");</script>
    <div id="mod-detail-attributes"><table><tr><th>品牌</th><td>其他</td></tr></table></div>
    </body></html>
    """
    d = parse_1688_html(sample, item_id="643237120099")
    print("离线解析样例:")
    for k in ("item_id", "name", "price", "saleCount", "cat_id", "express_fee", "attributes", "sourceSku", "images"):
        print(f"  {k}: {d.get(k)}")
    # 校验 loader 可正常消费
    import sys
    sys.path.insert(0, ".")
    from loader import parse_cyj_item
    from models import Product
    p = parse_cyj_item(d)
    print(f"\nloader 消费成功: {p.title} ¥{p.price} 运费{p.express_fee} 重量{p.weight_kg}kg 类目[{p.category}]")
