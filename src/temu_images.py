#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""temu_images.py —— Temu 全托管「商品图片素材包」本地处理(只读在线, 不碰平台)。

定位:
  把已采集的 1688 商品图(只有 URL)变成一套可直接照着上传 Temu 后台的素材包:
    1) 批量下载主图/详情图到 data/temu_images/<item_id>/
    2) 智能白底化: 把「接近边缘背景色且与边缘连通」的像素替换成纯白 RGB(255,255,255),
       复杂背景(渐变/场景/主体贴边)不硬抠, 明确标记 REVIEW, 由人工重拍或另行处理。
    3) 合规质检: 尺寸>=800、短边正方形、文件<=3MB、白底纯度、疑似文字/水印角标
    4) 导出「图片体检表」CSV + 「建品素材清单」CSV(图路径 + 标题 + 属性 + 供货价)

为什么不接 rembg/AI 抠图:
  rembg 需下载 ~176MB u2net 模型且 CPU 慢, 会让免安装包体积翻倍、首次运行卡住。
  美妆工具/小件货源图多为浅底或纯底, 纯 PIL+numpy 的边缘连通白底化已够用;
  复杂底图本来就该重拍而不是硬抠(硬抠边缘发白反而过不了审)。

白底化算法(保守):
  - 取图片四边若干像素估计背景色 (中位数, 抗单点水印干扰)
  - 计算每个像素到背景色的色距, < tol 的作为「背景候选」
  - 从四边做 BFS 连通域, 只把与边缘连通的候选染白 (保护主体内部同色区域)
  - 若染白后非白像素占比 > 主体上限(即几乎没抠掉背景=复杂底), 判为 REVIEW, 放弃该结果
  - 居中缩放到目标正方形, 白底填充, 保存 JPG 并控制 <=3MB

用法:
  from temu_images import build_package
  report = build_package(products, rows)   # products=采集商品, rows=temu.screen()结果
"""
from pathlib import Path
from collections import deque
import csv
import json
import re

try:
    from paths import ROOT, DATA
except Exception:
    _HERE = Path(__file__).resolve().parent
    ROOT = _HERE.parent
    DATA = ROOT / "data"

IMG_DIR = DATA / "temu_images"
CFG_PATH = ROOT / "temu_config.json"

DEFAULT_IMG_CFG = {
    "image_target_size": 1000,      # 输出正方形边长(Temu 常见规格; 1600 更清晰但体积大)
    "image_max_bytes": 3_000_000,   # Temu 单图上限 3MB
    "image_min_side": 800,          # Temu 最低分辨率
    "whiten_tol": 42,               # 背景色距离容差(越大抠得越狠, 越大越易伤主体)
    "max_white_ratio": 0.72,        # 非白像素占比上限: 超过说明主体太大/背景太复杂 -> REVIEW
    "max_images_per_item": 6,       # 每款最多处理几张(主图优先)
    "download_timeout": 20,
}

# 状态: OK=可直接用 / REVIEW=需人工(复杂底/带字/主体贴边) / FAIL=下载或解码失败
S_OK, S_REVIEW, S_FAIL = "OK", "REVIEW", "FAIL"

# 常见大牌/IP: 图片上印有这些会构成侵权, 人工验收时重点看(图片内文字需 OCR 才能自动抓)
BRAND_HINTS = ["sk-ii", "skii", "dior", "chanel", "lancome", "estee", "nike", "adidas",
               "apple", "disney", "marvel", "lv", "kitty", "雅诗兰黛", "兰蔻", "迪奥", "香奈儿"]


def brand_warning(p) -> str:
    """从商品标题/属性里找品牌露出风险(图片印刷的需人工看图)。"""
    txt = " ".join([getattr(p, "title", "") or getattr(p, "name", "") or "",
                    " ".join(str(v) for v in (getattr(p, "attributes", None) or {}).values())])
    low = txt.lower()
    hit = [b for b in BRAND_HINTS if b in low]
    return "标题/属性含大牌词: " + ",".join(hit) if hit else ""


def load_img_config() -> dict:
    cfg = dict(DEFAULT_IMG_CFG)
    try:
        if CFG_PATH.exists():
            cfg.update(json.loads(CFG_PATH.read_text(encoding="utf-8")).get("images", {}))
    except Exception:
        pass
    return cfg


# ---------- 工具 ----------
def _norm_url(u) -> str:
    if isinstance(u, dict):
        u = u.get("url") or u.get("original") or u.get("imageURI") or ""
    u = str(u or "").strip()
    return u.replace("http://", "https://")


def _image_urls(p) -> list:
    out = []
    for x in (getattr(p, "images", None) or []):
        u = _norm_url(x)
        if u and u not in out:
            out.append(u)
    return out


def _detail_urls(p) -> list:
    out = []
    for x in (getattr(p, "detail_images", None) or []):
        u = _norm_url(x)
        if u and u not in out:
            out.append(u)
    return out


def _edge_bg_color(a, k=12):
    """从四边像素估计背景色(中位数)。a: (h,w,3) uint8。"""
    import numpy as np
    h, w = a.shape[:2]
    k = max(4, min(k, h // 4, w // 4))
    strip = np.concatenate([
        a[:k].reshape(-1, 3), a[-k:].reshape(-1, 3),
        a[:, :k].reshape(-1, 3), a[:, -k:].reshape(-1, 3)])
    return np.median(strip, axis=0).astype(int)


def whiten(im, cfg=None):
    """保守白底化。返回 (新图 PIL.Image, 诊断 dict)。不修改原图。"""
    import numpy as np
    from PIL import Image
    cfg = cfg or load_img_config()
    src = im.convert("RGB")
    a = np.asarray(src).astype(np.int16)
    h, w = a.shape[:2]
    bg = _edge_bg_color(a)

    # 色距(加权亮度): 用 L1 更稳, 避免渐变底某通道漂移
    dist = np.abs(a - bg).sum(axis=2)
    tol = float(cfg["whiten_tol"])
    cand = dist < tol

    # 从四边 BFS, 只保留与边缘连通的背景候选
    seen = np.zeros((h, w), dtype=bool)
    q = deque()
    for x in range(w):
        for y in (0, h - 1):
            if cand[y, x] and not seen[y, x]:
                seen[y, x] = True
                q.append((y, x))
    for y in range(h):
        for x in (0, w - 1):
            if cand[y, x] and not seen[y, x]:
                seen[y, x] = True
                q.append((y, x))
    while q:
        y, x = q.popleft()
        for dy, dx in ((1, 0), (-1, 0), (0, 1), (0, -1)):
            ny, nx = y + dy, x + dx
            if 0 <= ny < h and 0 <= nx < w and cand[ny, nx] and not seen[ny, nx]:
                seen[ny, nx] = True
                q.append((ny, nx))

    out = np.asarray(src).copy()
    out[seen] = 255
    # 非白占比: 判断抠图是否"没抠动"(复杂底/主体占满)
    nonwhite = (np.abs(out.astype(np.int16) - 255).sum(axis=2) > 24)
    ratio = float(nonwhite.mean())
    diag = {
        "bg": [int(x) for x in bg],
        "whitened_ratio": round(1 - ratio, 3),
        "nonwhite_ratio": round(ratio, 3),
        "connected": bool(seen.any()),
    }
    if not seen.any() or ratio > float(cfg["max_white_ratio"]):
        return src, {**diag, "verdict": S_REVIEW,
                     "reason": "复杂背景/主体贴边, 自动抠白底不可靠, 建议重拍或人工处理"}
    return Image.fromarray(out), {**diag, "verdict": S_OK, "reason": ""}


def _square_crop_center(im):
    w, h = im.size
    s = min(w, h)
    return im.crop(((w - s) // 2, (h - s) // 2, (w - s) // 2 + s, (h - s) // 2 + s))


def _save_jpg(im, path, max_bytes):
    """保存 JPG, 自动降质直到 <=max_bytes。"""
    from PIL import Image
    q = 92
    while q >= 55:
        im.save(path, "JPEG", quality=q, optimize=True)
        if path.stat().st_size <= max_bytes:
            return q
        q -= 6
    # 仍太大 -> 缩边
    w, h = im.size
    small = im.resize((int(w * 0.8), int(h * 0.8)), Image.LANCZOS)
    small.save(path, "JPEG", quality=70, optimize=True)
    return 70


def audit(path: Path, cfg=None) -> dict:
    """合规质检。返回 dict(verdict, issues, size, bytes, white_ratio)。"""
    from PIL import Image
    import numpy as np
    cfg = cfg or load_img_config()
    d = {"path": str(path), "issues": [], "verdict": S_OK}
    try:
        with Image.open(path) as im:
            w, h = im.size
            d["size"] = [w, h]
            a = np.asarray(im.convert("RGB")).astype(np.int16)
    except Exception as e:
        d["verdict"] = S_FAIL
        d["issues"].append(f"无法解码: {e}")
        return d
    if min(w, h) < int(cfg["image_min_side"]):
        d["issues"].append(f"分辨率 {w}x{h} < {cfg['image_min_side']}")
    sz = path.stat().st_size
    d["bytes"] = sz
    if sz > int(cfg["image_max_bytes"]):
        d["issues"].append(f"文件 {sz/1024/1024:.1f}MB > 3MB")
    # 白底纯度: 沿四边细条采样(而非固定四角——主体贴边时四角会误落在主体上)。
    # 取每条边中段 60% 宽、向内 6% 深的带, 更贴近平台「背景是否纯白」的判定。
    k = max(6, int(min(w, h) * 0.06))
    ms, me = int(w * 0.2), int(w * 0.8)
    mks, mke = int(h * 0.2), int(h * 0.8)
    edges = np.concatenate([
        a[:k, ms:me].reshape(-1, 3), a[-k:, ms:me].reshape(-1, 3),
        a[mks:mke, :k].reshape(-1, 3), a[mks:mke, -k:].reshape(-1, 3)])
    white = (edges >= 246).all(axis=1).mean()
    d["white_ratio"] = round(float(white), 3)
    if white < 0.85:
        d["issues"].append(f"白底纯度 {white:.0%} < 85%")
    if d["issues"]:
        d["verdict"] = S_REVIEW if d["verdict"] != S_FAIL else S_FAIL
    return d


# ---------- 主流程 ----------
def build_package(products: list, rows: list, progress=None, dest: Path = None) -> dict:
    """下载+抠白底+质检+导出。rows 为 temu.screen() 结果(只用推荐款)。"""
    import requests
    from PIL import Image
    from io import BytesIO
    cfg = load_img_config()
    dest = Path(dest) if dest else IMG_DIR
    dest.mkdir(parents=True, exist_ok=True)
    pbar = progress or (lambda *a, **k: None)

    rec = {r.item_id: r for r in rows if getattr(r, "recommend", False)}
    targets = [p for p in products if getattr(p, "item_id", "") in rec]
    if not targets and rec:
        # rows 来自 temu.screen() 时 item_id 可能未在 products 中, 仍按 rec 找
        targets = [p for p in products if str(getattr(p, "item_id", "")) in rec]
    pbar("info", f"素材包: 待处理 {len(targets)} 款(推荐款)")

    audit_rows, material_rows, stats = [], [], {S_OK: 0, S_REVIEW: 0, S_FAIL: 0}
    size = int(cfg["image_target_size"])
    per = int(cfg["max_images_per_item"])

    for p in targets:
        iid = str(getattr(p, "item_id", ""))
        r = rec.get(iid) or rec.get(getattr(p, "item_id", None))
        folder = dest / iid
        folder.mkdir(parents=True, exist_ok=True)
        bw = brand_warning(p)
        urls = (_image_urls(p) + _detail_urls(p))[:per]
        pbar("info", f"  {iid}: {len(urls)} 张")
        if not urls:
            audit_rows.append({"item_id": iid, "title": getattr(p, "title", "") or getattr(p, "name", ""), "file": "",
                               "raw_file": "", "verdict": S_FAIL, "issues": "该商品无图片 URL",
                               "size": "", "bytes": "", "white_ratio": ""})
            stats[S_FAIL] += 1
            continue
        made = 0
        for idx, u in enumerate(urls, 1):
            try:
                resp = requests.get(u, timeout=int(cfg["download_timeout"]),
                                    headers={"User-Agent": "Mozilla/5.0"})
                resp.raise_for_status()
                im = Image.open(BytesIO(resp.content))
                im.load()
            except Exception as e:
                audit_rows.append({"item_id": iid, "title": getattr(p, "title", "") or getattr(p, "name", ""), "file": "",
                                   "raw_file": "", "verdict": S_FAIL, "issues": f"下载/解码失败: {str(e)[:50]}",
                                   "size": "", "bytes": "", "white_ratio": ""})
                stats[S_FAIL] += 1
                continue
            # 保留原图: REVIEW 图供人工去背/重拍时参考, 不被处理后的正方形图替代
            raw_path = folder / f"raw_{idx:02d}.jpg"
            if not raw_path.exists():
                try:
                    im.save(raw_path, "JPEG", quality=88, optimize=True)
                except Exception:
                    pass
            # 白底化(保守) -> 正方形 -> 保存
            white_im, diag = whiten(im, cfg)
            sq = _square_crop_center(white_im).resize((size, size), Image.LANCZOS)
            fname = f"{'main' if idx == 1 else 'sub'}_{idx:02d}.jpg"
            out = folder / fname
            try:
                _save_jpg(sq, out, int(cfg["image_max_bytes"]))
            except Exception as e:
                audit_rows.append({"item_id": iid, "title": getattr(p, "title", "") or getattr(p, "name", ""), "file": "",
                                   "raw_file": "", "verdict": S_FAIL, "issues": f"保存失败: {str(e)[:50]}",
                                   "size": "", "bytes": "", "white_ratio": ""})
                stats[S_FAIL] += 1
                continue
            a = audit(out, cfg)
            verdict = S_OK if diag["verdict"] == S_OK and a["verdict"] == S_OK else S_REVIEW
            issues = "; ".join(x for x in [diag.get("reason", ""), *a["issues"],
                                            bw if idx == 1 else ""] if x)
            if bw and idx == 1 and verdict == S_OK:
                verdict = S_REVIEW
            audit_rows.append({
                "item_id": iid, "title": getattr(p, "title", "") or getattr(p, "name", ""), "file": str(out.relative_to(ROOT)),
                "raw_file": str(raw_path.relative_to(ROOT)) if raw_path.exists() else "",
                "verdict": verdict, "issues": issues,
                "size": f"{a.get('size', [0, 0])[0]}x{a.get('size', [0, 0])[1]}",
                "bytes": a.get("bytes", ""), "white_ratio": a.get("white_ratio", "")})
            stats[verdict] += 1
            made += 1
            if made == 1 and verdict == S_OK:
                material_rows.append({
                    "item_id": iid, "title": getattr(p, "title", "") or getattr(p, "name", ""),
                    "category": getattr(r, "category", "") if r else "",
                    "suggest_price": getattr(r, "quote_price", "") if r else "",
                    "main_image": str(out.relative_to(ROOT)),
                    "images_in_package": len(urls),
                    "issue": issues})
        pbar("info", f"    -> {made} 张")

    a_path = export_audit_csv(audit_rows)
    m_path = export_material_csv(material_rows)
    return {"stats": stats, "audit_csv": a_path, "material_csv": m_path,
            "audit_rows": audit_rows, "dir": dest}


AUDIT_HEADERS = ["item_id", "商品名称", "白底图文件", "原图文件", "结论", "问题/原因", "尺寸", "字节", "白底纯度"]
MATERIAL_HEADERS = ["item_id", "商品名称", "类目", "建议供货价", "主图路径(白底)", "包内图数", "备注"]


def export_audit_csv(rows: list, path=None) -> Path:
    path = Path(path) if path else DATA / "temu_image_audit.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(AUDIT_HEADERS)
        for r in rows:
            w.writerow([r["item_id"], r["title"], r["file"], r.get("raw_file", ""), r["verdict"], r["issues"],
                        r["size"], r["bytes"], r["white_ratio"]])
    return path


def export_material_csv(rows: list, path=None) -> Path:
    path = Path(path) if path else DATA / "temu_material_list.csv"
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", newline="", encoding="utf-8-sig") as f:
        w = csv.writer(f)
        w.writerow(MATERIAL_HEADERS)
        for r in rows:
            w.writerow([r["item_id"], r["title"], r["category"], r["suggest_price"],
                        r["main_image"], r["images_in_package"], r["issue"]])
    return path


def render_report(result: dict) -> str:
    s = result.get("stats", {})
    lines = [
        "【图片素材包处理完成】",
        f"  可直接用 OK      : {s.get(S_OK, 0)} 张",
        f"  需人工 REVIEW    : {s.get(S_REVIEW, 0)} 张 (复杂背景/分辨率不足/白底不纯)",
        f"  失败 FAIL        : {s.get(S_FAIL, 0)} 张",
        f"  素材目录         : {result.get('dir')}",
        f"  图片体检表       : {result.get('audit_csv')}",
        f"  建品素材清单     : {result.get('material_csv')}",
        "",
        "说明: REVIEW 的图不要直接传 Temu —— 按体检表原因重拍白底图,",
        "或用图片工具去背景后重新质检。主图第 1 张必须纯白底、>=800px、无文字水印 logo。",
        "每张图旁附 raw_ 原图供人工处理; 图片上印有大牌 logo(如 SK-II)属侵权风险,",
        "标题/属性含大牌词的已在体检表标出, 图片里的印刷字样需你人工看图确认。",
    ]
    return "\n".join(lines)


def selftest():
    """不依赖网络的自测: 纯色底抠白 / 复杂底拒抠 / 质检。"""
    from PIL import Image
    import numpy as np
    cfg = dict(DEFAULT_IMG_CFG)
    # 纯色浅灰底 + 深色方块主体
    a = np.full((300, 300, 3), 238, dtype=np.uint8)
    a[110:190, 110:190] = [40, 60, 80]
    im = Image.fromarray(a)
    out, d = whiten(im, cfg)
    assert d["verdict"] == S_OK, d
    arr = np.asarray(out)
    assert arr[0, 0].tolist() == [255, 255, 255], "背景未染白"
    assert arr[150, 150].tolist() == [40, 60, 80], "主体被误抠"
    # 复杂渐变底 + 贴边主体 -> 应拒抠
    b = np.zeros((300, 300, 3), dtype=np.uint8)
    for y in range(300):
        b[y] = [y % 256, (y * 2) % 256, (y * 3) % 256]
    b[0:200, 0:200] = [30, 30, 30]
    _, d2 = whiten(Image.fromarray(b), cfg)
    assert d2["verdict"] == S_REVIEW, d2
    print("temu_images SELFTEST OK")
    return True


if __name__ == "__main__":
    selftest()
