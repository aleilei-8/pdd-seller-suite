#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
loader.py —— 1688 货源数据导入层(一件代发上游)

数据源(按优先级):
  1. 穿云箭缓存: D:/穿云箭工具箱/soft/pddhousekeepernew/cache/alibabaitem/*.txt
     (JSON: name/price/saleCount/cat_id/item_url/images/attributes/sourceSku/express_fee)
  2. 粘贴 / 选择 1688 商品链接(后续: Playwright 抓 detail.1688.com/offer/<id>.html)
  3. 手动录入 / 导出 JSON

输出统一的 Product 数据模型, 供选品/定价/标题/上架共用。
注意: 1688 不包运费, express_fee 单独记账, 不再并入进货价。
"""
import json
import os
import re
import glob
from dataclasses import asdict

from models import Product
import sku_map

DEFAULT_CYJ_CACHE = r"D:/穿云箭工具箱/soft/pddhousekeepernew/cache/alibabaitem"


def parse_sale_count(text: str) -> int:
    """'10万+' -> 100000 ; '3589' -> 3589 ; '1.2万' -> 12000"""
    if not text:
        return 0
    t = str(text).replace("+", "").replace(" ", "").strip()
    m = re.match(r"^([\d.]+)\s*([亿万]?)$", t)
    if not m:
        return 0
    num = float(m.group(1))
    unit = m.group(2)
    if unit == "万":
        num *= 10_000
    elif unit == "亿":
        num *= 100_000_000
    return int(num)


def _read_json_file(path: str):
    try:
        with open(path, encoding="utf-8-sig") as f:   # 缓存带 BOM
            return json.load(f)
    except Exception:
        return None


# ---- 在线采集数据补全 ----------------------------------------------------
# 在线采集通常只有 skuBase(规格名)与主图, 没有详情图/每款价库存。
# 若本地 1688 商品缓存里存在同一 item_id 的完整快照, 按字段补齐(只补空, 不覆盖)。
_CACHE_INDEX = None


def _cache_index(cache_dir: str = DEFAULT_CYJ_CACHE) -> dict:
    global _CACHE_INDEX
    if _CACHE_INDEX is not None:
        return _CACHE_INDEX
    idx = {}
    if os.path.isdir(cache_dir):
        for path in sorted(glob.glob(os.path.join(cache_dir, "*.txt"))):
            iid = os.path.splitext(os.path.basename(path))[0]
            if iid.isdigit():
                idx[iid] = path
    _CACHE_INDEX = idx
    return idx


def enrich_from_cache(d: dict) -> dict:
    """按 item_id 从缓存快照补齐 detail_images / sku_combos / detail_html。

    只补在线采集缺失的字段; 命中不到缓存时原样返回, 不影响任何既有流程。
    """
    if not isinstance(d, dict):
        return d
    out = dict(d)
    iid = str(out.get("item_id") or "")
    path = _cache_index().get(iid) if iid else None
    if not path:
        return out
    cached = _read_json_file(path)
    if not isinstance(cached, dict):
        return out

    if not out.get("detail_images") or not out.get("detail_html"):
        main_imgs = sku_map.normalize_images(out.get("images") or [])
        # 缓存快照的 images 往往含该商品的全部图片(主图 + SKU 图 + 详情图),
        # 与 detail HTML 里抠出的详情图合并去重, 供发布端按平台上限全量上传。
        cached_imgs = sku_map.normalize_images(cached.get("images") or [])
        dh = cached.get("detail") or cached.get("detail_html") or ""
        cand, seen = [], set()
        for u in (cached_imgs
                  + sku_map.normalize_images(cached.get("detail_images") or [])
                  + sku_map.extract_detail_images(dh, cached_imgs)
                  + main_imgs):
            key = u.split("?")[0]
            if key not in seen:
                seen.add(key)
                cand.append(u)
        if cand and not out.get("detail_images"):
            out["detail_images"] = cand
        if dh and not out.get("detail_html"):
            out["detail_html"] = dh

    if not out.get("sku_combos"):
        sk = dict(out.get("sourceSku") or {})
        ck = cached.get("sourceSku") or {}
        if isinstance(ck, dict):
            for key in ("skuBase", "skus", "sku2info"):
                if not sk.get(key) and ck.get(key):
                    sk[key] = ck[key]
        dims, combos = sku_map.normalize_source_sku(sk)
        if combos:
            out["sourceSku"] = sk
            if not out.get("sku_dims"):
                out["sku_dims"] = dims
            out["sku_combos"] = combos
    return out


def _parse(d: dict) -> Product:
    return parse_cyj_item(enrich_from_cache(d))


def _extract_attributes(d: dict) -> dict:
    attrs = d.get("attributes") or {}
    out = {}
    if isinstance(attrs, dict):
        for k, v in attrs.items():
            if isinstance(v, list):
                out[k] = [str(x) for x in v if x]
            else:
                out[k] = [str(v)]
    return out


def _extract_sku_specs(d: dict) -> list:
    specs = []
    sku = d.get("sourceSku") or {}
    if isinstance(sku, dict):
        for grp in sku.get("skuBase", []) or []:
            for v in grp.get("values", []) or []:
                name = v.get("name") if isinstance(v, dict) else str(v)
                if name:
                    specs.append(str(name))
    return specs


def parse_cyj_item(d: dict) -> Product:
    p = Product()
    p.item_id = str(d.get("item_id", ""))
    p.source = {2: "1688", 1: "taobao"}.get(d.get("itemSource"), str(d.get("itemSource", "1688")))
    p.title = (d.get("name") or d.get("sourceName") or "").strip()
    try:
        p.price = float(d.get("price", 0) or 0)
    except (TypeError, ValueError):
        p.price = 0.0
    p.sale_count_text = str(d.get("saleCount", "") or "")
    p.sale_count = parse_sale_count(p.sale_count_text)
    p.cat_id = str(d.get("cat_id", "") or "")
    p.item_url = d.get("item_url", "") or ""
    # 轮播主图: 全量保留(不再只取第一张), 去缩略图尺寸后缀
    p.images = sku_map.normalize_images(d.get("images") or [])
    p.attributes = _extract_attributes(d)
    p.sku_specs = _extract_sku_specs(d)
    # ---- 全量搬迁: 详情图 / SKU 维度+组合价库存 / 详情HTML / 类目名 ----
    # 兼容两种输入: 穿云箭缓存(有 detail HTML) / 在线采集(已有归一化字段)
    p.detail_html = d.get("detail") or d.get("detail_html") or ""
    p.detail_images = (sku_map.normalize_images(d.get("detail_images") or [])
                       or sku_map.extract_detail_images(p.detail_html, p.images))
    _dims, _combos = sku_map.normalize_source_sku(d.get("sourceSku") or {})
    p.sku_dims = d.get("sku_dims") or _dims
    p.sku_combos = d.get("sku_combos") or _combos
    if not p.sku_specs:
        p.sku_specs = [v["name"] for dim in p.sku_dims for v in dim.get("values", [])]
    # 类目名: 显式 category_name > attributes 里的类目字段 > 空(发布端再用标题/类目ID匹配)
    p.category_name = str(d.get("category_name") or "")
    if not p.category_name:
        for k, v in (p.attributes or {}).items():
            if any(w in k for w in ("类目", "分类", "品类")):
                p.category_name = " ".join(v) if isinstance(v, list) else str(v)
                break
    try:
        p.express_fee = float(d.get("express_fee", 0) or 0)
    except (TypeError, ValueError):
        p.express_fee = 0.0
    # 重量(用于拼多多发货运费估算): 从属性/规格/标题尽量提取
    p.weight_kg = _extract_weight(p.attributes, p.sku_specs, p.title)
    # 类目推断(用于佣金率匹配): 优先用 1688 显式 cat_id, 标题/属性关键词作补充
    p.category = _guess_category(p.title, p.attributes, p.cat_id)
    return p


def _extract_weight(attrs: dict, sku_specs: list, title: str) -> float:
    """尽量从 1688 属性/规格/标题 提取净重(kg)。找不到返回 0(引擎退回首重兜底)。"""
    candidates = []
    for k, v in (attrs or {}).items():
        if any(w in k for w in ("重量", "净重", "毛重", "克重")):
            candidates.extend(v if isinstance(v, list) else [v])
    candidates.extend(sku_specs or [])
    candidates.append(title or "")
    for text in candidates:
        m = re.search(r"(\d+(?:\.\d+)?)\s*(kg|千克|公斤|g|克|KG|G)", str(text))
        if m:
            num = float(m.group(1))
            u = m.group(2).lower()
            return round(num / 1000.0, 3) if u in ("g", "克") else round(num, 3)
    return 0.0


def _guess_category(title: str, attrs: dict, cat_id: str = "") -> str:
    # 1) 显式类目(cat_id 或 属性里的类目字段)优先, 命中佣金表关键字直接用
    explicit = [(cat_id or "")]
    for k, v in (attrs or {}).items():
        if any(w in k for w in ("类目", "分类", "品类")):
            explicit.append(" ".join(v) if isinstance(v, list) else str(v))
    cat_keywords = ("美妆", "化妆品", "护肤", "彩妆", "发饰", "饰品", "首饰", "珠宝",
                   "服饰", "女装", "男装", "童装", "内衣", "鞋", "箱包",
                   "家居", "收纳", "数码", "电子", "手机", "食品", "零食",
                   "母婴", "玩具", "五金", "工具", "日用", "百货")
    for text in explicit:
        for kw in cat_keywords:
            if kw in (text or ""):
                return kw
    # 2) 标题/属性关键词补充(仍优先靠前的关键字)
    blob = (title or "")
    for k, v in (attrs or {}).items():
        if isinstance(v, list):
            blob += " " + " ".join(v)
        else:
            blob += " " + str(v)
    for kw in cat_keywords:
        if kw in blob:
            return kw
    return ""


def load_from_cyj_cache(cache_dir: str = DEFAULT_CYJ_CACHE) -> list:
    if not os.path.isdir(cache_dir):
        return []
    products, seen = [], set()
    for path in sorted(glob.glob(os.path.join(cache_dir, "*.txt"))):
        if os.path.basename(path) == "TextFile1.txt":
            continue
        d = _read_json_file(path)
        if not d or not d.get("item_id"):
            continue
        p = parse_cyj_item(d)
        if p.item_id in seen:
            continue
        seen.add(p.item_id)
        products.append(p)
    return products


def load_from_json_file(path: str) -> list:
    d = _read_json_file(path)
    if isinstance(d, list):
        return [_parse(x) for x in d if isinstance(x, dict)]
    if isinstance(d, dict) and d.get("item_id"):
        return [_parse(d)]
    return []


def load_from_json_array(text: str) -> list:
    try:
        d = json.loads(text)
    except Exception:
        return []
    if isinstance(d, list):
        return [_parse(x) for x in d if isinstance(x, dict)]
    if isinstance(d, dict) and d.get("item_id"):
        return [_parse(d)]
    return []


def load_from_csv(path: str) -> list:
    """从导出的商品 CSV(含 item_id/title/price/express_fee 等)读回 Product"""
    import csv
    products = []
    with open(path, encoding="utf-8-sig", newline="") as f:
        for r in csv.DictReader(f):
            p = Product()
            p.item_id = r.get("item_id", "")
            p.title = r.get("title", "") or r.get("original_title", "")
            try:
                p.price = float(r.get("price", 0) or 0)
            except (TypeError, ValueError):
                p.price = 0.0
            try:
                p.express_fee = float(r.get("express_fee", 0) or 0)
            except (TypeError, ValueError):
                p.express_fee = 0.0
            p.sale_count_text = r.get("sale_count_text", "")
            p.sale_count = parse_sale_count(p.sale_count_text)
            p.category = r.get("category", "")
            p.source = r.get("source", "1688")
            products.append(p)
    return products


def products_to_dicts(products: list) -> list:
    return [p.to_dict() for p in products]


if __name__ == "__main__":
    prods = load_from_cyj_cache()
    print(f"从 1688 商品缓存解析到 {len(prods)} 个商品:")
    for p in prods:
        print(f"  [{p.item_id}] ¥{p.price}(运费{p.express_fee}) | 销量{p.sale_count_text} | 类目{p.category} | {p.title[:30]}")
