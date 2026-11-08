#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
sku_map.py —— 1688 全量字段归一化(共享给 loader / collector / listing / pdd_publish)

目标: 把两种数据源统一成 PDD 发布可直接消费的结构:
  A) 穿云箭缓存 sourceSku = {skuBase:[{name,values:[{vid,name,image}]}],
                              skus:[{propPath:'1:72',skuId:'...'}],
                              sku2info:{skuId:{price:{priceMoney(分),priceText(元)},quantity}}}
  B) 在线采集 window.context 的 skuInfoMap / 已归一化 combos

统一产出:
  sku_dims   = [{"name":"颜色", "values":[{"name":"粉色", "image":"https://..."}]}, ...]
  sku_combos = [{"spec":"粉色/M", "specs":["粉色","M"], "price":7.10,
                 "stock":99691, "image":"https://...", "sku_id":"..."}, ...]
  detail_images = ["https://...", ...]   # 详情描述大图(去缩略图后缀/去重)

纯标准库, 零第三方依赖, 可离线单测。
"""
import re
from html import unescape

_IMG_RE = re.compile(
    r'(?:src|data-src|data-original|data-lazyload-src|data-ks-lazyload)\s*=\s*'
    r'["\']([^"\']*?(?:jpg|jpeg|png|webp|gif)[^"\']*?)["\']', re.I)

# 1688 缩略图后缀: .310x310.jpg / _310x310.jpg / .jpg_430x430q90.jpg 等
_THUMB_SUFFIX = re.compile(r'[._]\d{2,4}x\d{2,4}(?:q\d+)?(?=\.(?:jpg|jpeg|png|webp|gif))', re.I)
# 阿里 CDN 的"格式变体"后缀: xxx.jpg_.webp / xxx.jpg_sum.jpg / xxx.jpg_q90.jpg /
# xxx.jpg_b.jpg 等。它们与 xxx.jpg 是同一张图, 只是缩略/压缩/webp 版本;
# 不归一会导致同一张图被重复上传, 且 webp 会被 PDD 拒收、缩略图模糊。
_FORMAT_SUFFIX = re.compile(
    r'\.(jpg|jpeg|png|webp|gif)_[^/?#]*\.(?:jpg|jpeg|png|webp|gif)(?=$|[?#])', re.I)
_BAD_IMG = ("loading", "placeholder", "lazyload", "blank", "spacer",
            "1x1", "pixel", "shoptool", "default_")


def clean_img_url(u: str) -> str:
    """规范化 1688 图片 URL: 补全协议、去缩略图尺寸后缀、剔占位图。返回 ''=丢弃。"""
    u = unescape((u or "").strip())
    if not u:
        return ""
    if u.startswith("//"):
        u = "https:" + u
    if u.startswith("/") or not u.lower().startswith("http"):
        return ""
    low = u.lower()
    if any(k in low for k in _BAD_IMG):
        return ""
    # 先去 webp/sum/b/尺寸 等"变体后缀", 统一回原始 .jpg/.png。
    # 顺序很重要: xxx.jpg_60x60q90.jpg 必须先整体折叠成 xxx.jpg,
    # 若先删 _60x60q90 会留下 xxx.jpg.jpg 这种无效 URL。
    u = _FORMAT_SUFFIX.sub(r'.\1', u)
    # 再处理 xxx_310x310.jpg 这种没有前置扩展名的尺寸后缀
    u = _THUMB_SUFFIX.sub("", u)
    return u


def extract_detail_images(detail_html: str, main_images: list = None) -> list:
    """从穿云箭 detail HTML 抽详情图(大图), 去掉与主图重复的、占位的。"""
    out, seen = [], set()
    main = set()
    for u in (main_images or []):
        cu = clean_img_url(u if isinstance(u, str) else (u.get("url") if isinstance(u, dict) else ""))
        if cu:
            main.add(cu.split("?")[0])
    for m in _IMG_RE.finditer(detail_html or ""):
        u = clean_img_url(m.group(1))
        if not u:
            continue
        key = u.split("?")[0]
        if key in seen or key in main:
            continue
        seen.add(key)
        out.append(u)
    return out


def normalize_images(images: list) -> list:
    """主图统一成 URL 字符串列表(兼容 dict{url} / 字符串), 去缩略图后缀。"""
    out = []
    for x in (images or []):
        u = x.get("url") if isinstance(x, dict) else (x if isinstance(x, str) else "")
        cu = clean_img_url(u)
        if cu:
            out.append(cu)
    return out


def _price_from_info(info: dict) -> float:
    """sku2info 里取元: 优先 priceText, 否则 priceMoney(分)/100。"""
    if not isinstance(info, dict):
        return 0.0
    price = info.get("price") or {}
    if isinstance(price, dict):
        pt = price.get("priceText")
        if pt not in (None, ""):
            try:
                return round(float(str(pt).replace(",", "")), 2)
            except (TypeError, ValueError):
                pass
        pm = price.get("priceMoney")
        if pm not in (None, ""):
            try:
                return round(float(pm) / 100.0, 2)
            except (TypeError, ValueError):
                pass
    elif price not in (None, ""):
        try:
            return round(float(price), 2)
        except (TypeError, ValueError):
            pass
    # 兼容直接平铺的字段
    for k in ("priceText", "price", "priceMoney", "salePrice",
              "discountPrice", "promotionPrice", "finalPrice", "skuPrice"):
        v = info.get(k)
        if isinstance(v, (int, float)) and v:
            return round(float(v) / 100.0 if k == "priceMoney" else float(v), 2)
    return 0.0


def _stock_from_info(info: dict) -> int:
    if not isinstance(info, dict):
        return 0
    for k in ("quantity", "stock", "skuStock", "amount", "sellableQuantity",
              "canBookCount", "availableQuantity", "bookCount", "stockQuantity"):
        v = info.get(k)
        if v not in (None, ""):
            try:
                return max(0, int(float(v)))
            except (TypeError, ValueError):
                pass
    return 0


def normalize_source_sku(source_sku: dict) -> tuple:
    """把 sourceSku 归一化为 (sku_dims, sku_combos)。兼容穿云箭缓存与在线采集。

    返回:
      sku_dims   = [{"name": ..., "values":[{"name":..., "image":..., "vid":...}]}]
      sku_combos = [{"spec":..., "specs":[...], "price":float, "stock":int,
                     "image":..., "sku_id":...}]
    """
    sku = source_sku if isinstance(source_sku, dict) else {}
    dims = []
    vid_map = {}   # "pid:vid" -> (dim_index, value_name, value_image)

    # ---- 维度 ----
    base = sku.get("skuBase") or []
    for gi, grp in enumerate(base if isinstance(base, list) else []):
        if not isinstance(grp, dict):
            continue
        gname = grp.get("name") or grp.get("prop") or f"规格{gi+1}"
        pid = str(grp.get("pid") or grp.get("id") or gi)
        vals = []
        for vi, v in enumerate(grp.get("values") or []):
            if isinstance(v, dict):
                nm = v.get("name") or ""
                image = clean_img_url(v.get("image") or v.get("img") or "")
                vid = str(v.get("vid") or v.get("id") or vi)
            else:
                nm, image, vid = str(v), "", str(vi)
            nm = unescape(nm).strip()
            if not nm:
                continue
            vals.append({"name": nm, "image": image, "vid": vid})
            vid_map[f"{pid}:{vid}"] = (gi, nm, image)
        if vals:
            dims.append({"name": unescape(str(gname)).strip(), "values": vals})

    # ---- 组合 ----
    combos = []
    skus = sku.get("skus") or []
    s2i = sku.get("sku2info") or {}

    # 格式 A: 穿云箭 skus(propPath) + sku2info(skuId)
    if isinstance(skus, list) and skus:
        for s in skus:
            if not isinstance(s, dict):
                continue
            sid = str(s.get("skuId") or s.get("sku_id") or "")
            info = s2i.get(sid, {}) if isinstance(s2i, dict) else {}
            prop = str(s.get("propPath") or s.get("prop_path") or "")
            specs, spec_img = [], ""
            for seg in prop.split(";"):
                seg = seg.strip()
                if not seg:
                    continue
                hit = vid_map.get(seg)
                if hit:
                    specs.append(hit[1])
                    if not spec_img and hit[2]:
                        spec_img = hit[2]
                elif ":" in seg:
                    # 没建到映射时尽量保留 vid 作占位
                    specs.append(seg.split(":", 1)[1])
            combos.append({
                "spec": "/".join(specs) if specs else (sid[:8] or "默认"),
                "specs": specs,
                "price": _price_from_info(info) or _price_from_info(s),
                "stock": _stock_from_info(info) or _stock_from_info(s),
                "image": spec_img or clean_img_url(s.get("image") or ""),
                "sku_id": sid,
            })
    # 格式 A': 只有 sku2info(键就是 skuId, 无 skus)
    elif isinstance(s2i, dict) and s2i:
        for sid, info in s2i.items():
            combos.append({
                "spec": str(sid)[:10], "specs": [],
                "price": _price_from_info(info), "stock": _stock_from_info(info),
                "image": "", "sku_id": str(sid),
            })

    # 格式 B: 在线采集已归一化 combos / skuInfoMap
    raw_combos = sku.get("combos")
    if not combos and isinstance(raw_combos, list):
        for c in raw_combos:
            if not isinstance(c, dict):
                continue
            key = str(c.get("key") or c.get("spec") or "")
            specs = [x.strip() for x in re.split(r"[>|;]", key) if x.strip()]
            combos.append({
                "spec": "/".join(specs) if specs else key[:10],
                "specs": specs,
                "price": float(c.get("price") or 0),
                "stock": int(c.get("stock") or 0),
                "image": clean_img_url(c.get("image") or ""),
                "sku_id": str(c.get("sku_id") or ""),
            })
    sku_info = sku.get("skuInfoMap") or sku.get("skuMap")
    if not combos and isinstance(sku_info, dict):
        for key, info in sku_info.items():
            if not isinstance(info, dict):
                info = {}
            specs = [x.strip() for x in re.split(r"[>|;]", str(key)) if x.strip()]
            combos.append({
                "spec": "/".join(specs) if specs else str(key)[:10],
                "specs": specs,
                "price": _price_from_info(info),
                "stock": _stock_from_info(info),
                "image": clean_img_url(info.get("image") or ""),
                "sku_id": str(info.get("skuId") or ""),
            })

    return dims, combos


def sku_summary(dims: list, combos: list) -> str:
    """给 GUI/日志用的可读摘要: '颜色×尺码 共 96 款, 价 5.30~20.00, 库存合计 12万'"""
    if not combos:
        return "无SKU(单规格)"
    dn = "×".join(d["name"] for d in dims) if dims else "单规格"
    prices = [c["price"] for c in combos if c["price"] > 0]
    stock = sum(c["stock"] for c in combos)
    rng = f"{min(prices):.2f}~{max(prices):.2f}" if prices else "无价"
    return f"{dn} {len(combos)}款, 进价{rng}元, 库存合计{stock}"
