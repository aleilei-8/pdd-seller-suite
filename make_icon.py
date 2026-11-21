#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成「拼多多全链路工作台」应用图标 (assets/app.ico)。

设计 v2 · 现代 APP 质感:
  · Squircle 超椭圆轮廓(连续圆角, iOS/macOS 图标标志性造型)
  · Ocean 蓝三段渐变 + 斜向玻璃高光 + 顶部柔光
  · 白色粗体 P(带柔和投影) + 右下递增数据柱 + 青色数据节点
  · 每个尺寸单独 4x 超采样绘制, 小尺寸自动省略细节, 16px 依旧清晰

用法: python make_icon.py
产出: assets/app.ico           多尺寸图标(16/24/32/48/64/128/256)
      assets/app_preview.png   排版预览(浅色/深色背景对照)
      assets/app_icon_512.png  独立高清大图(快捷方式/商店图可用)
"""
import io
import math
import struct
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFilter, ImageFont

try:
    import numpy as np
except ImportError:
    np = None

ASSETS = Path(__file__).resolve().parent / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)

# 字体: Segoe UI Bold, 退化到 Arial Bold
_FONT_CANDIDATES = [
    "C:/Windows/Fonts/segoeuib.ttf",
    "C:/Windows/Fonts/arialbd.ttf",
]
FONT_BOLD = next((p for p in _FONT_CANDIDATES if Path(p).exists()), _FONT_CANDIDATES[0])

TOP = (72, 165, 255)      # 亮天蓝
MID = (26, 98, 214)       # 品牌蓝
BOT = (9, 42, 116)        # 深海军蓝
SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4                    # 超采样倍数
SQUIRCLE_N = 4.6          # 超椭圆指数: 越大越接近方块, 4~5 最像 APP 图标


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _squircle_mask(w, h, margin, n=SQUIRCLE_N):
    """超椭圆连续圆角轮廓, 比圆角矩形更有 APP 感。"""
    a = (w - 1) / 2 - margin
    b = (h - 1) / 2 - margin
    cx = cy = (w - 1) / 2
    pts = []
    steps = 1440
    exp = 2.0 / n
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        x = cx + a * math.copysign(abs(ct) ** exp, ct)
        y = cy + b * math.copysign(abs(st) ** exp, st)
        pts.append((x, y))
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).polygon(pts, fill=255)
    return m


def _gradient(w, h):
    col = Image.new("RGB", (1, h))
    p = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        p[0, y] = _lerp(TOP, MID, t / 0.55) if t < 0.55 else \
                  _lerp(MID, BOT, (t - 0.55) / 0.45)
    return col.resize((w, h), Image.BILINEAR).convert("RGBA")


def _glass_highlight(w, h):
    """斜向 + 顶部柔光叠加, 制造玻璃/珐琅质感。"""
    if np is None:
        # 无 numpy 时退化为简单顶部竖向高光
        ha = Image.new("L", (1, h))
        hp = ha.load()
        for y in range(h):
            hp[0, y] = int(60 * max(0.0, 1.0 - y / (h * 0.55)))
        layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        layer.putalpha(ha.resize((w, h), Image.BILINEAR))
        return layer
    yy, xx = np.mgrid[0:h, 0:w].astype(np.float32)
    # 斜向: 左上亮 → 右下暗
    diag = (xx / w) * 0.55 + (yy / h) * 0.45
    diag_a = np.clip(1.0 - diag, 0, 1) ** 2.2 * 78
    # 顶部椭圆柔光
    top = np.clip(1.0 - yy / (h * 0.42), 0, 1) ** 1.6 * 46
    # 底部轻微反光, 让深蓝不死黑
    bottom = np.clip((yy / h - 0.82) / 0.18, 0, 1) ** 2 * 22
    alpha = np.clip(diag_a + top + bottom, 0, 255).astype(np.uint8)
    layer = Image.new("RGBA", (w, h), (255, 255, 255, 0))
    layer.putalpha(Image.fromarray(alpha))
    return layer


def _text_box(draw, text, font):
    b = draw.textbbox((0, 0), text, font=font)
    return b, b[2] - b[0], b[3] - b[1]


def render(size: int, detail: bool) -> Image.Image:
    w = h = size * SS
    margin = int(w * 0.052)
    mask = _squircle_mask(w, h, margin)

    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    # 投影: 让图标在浅色桌面/资源管理器里浮起来
    if detail:
        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        off = int(w * 0.022)
        ImageDraw.Draw(sh).polygon(
            [(x, y + off) for x, y in _squircle_pts(w, h, margin)],
            fill=(5, 20, 56, 105))
        canvas = Image.alpha_composite(
            canvas, sh.filter(ImageFilter.GaussianBlur(w * 0.02)))

    # 主体渐变
    base = _gradient(w, h)
    base = Image.alpha_composite(base, _glass_highlight(w, h))
    base.putalpha(mask)
    canvas = Image.alpha_composite(canvas, base)

    # 内描边: 顶部亮、整体一圈细白线
    edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(edge).polygon(
        _squircle_pts(w, h, margin),
        outline=(255, 255, 255, 92 if detail else 58),
        width=max(1, int(w * 0.005)))
    edge.putalpha(ImageChops.multiply(edge.getchannel("A"), mask))
    canvas = Image.alpha_composite(canvas, edge)

    # 白色 P: 先投影再主体, 增加立体感
    font = ImageFont.truetype(FONT_BOLD, int(w * (0.615 if detail else 0.68)))
    dummy = ImageDraw.Draw(Image.new("RGBA", (w, h)))
    bbox, tw, th = _text_box(dummy, "P", font)
    tx = (w - tw) / 2 - bbox[0]
    ty = (h - th) / 2 - bbox[1] - h * (0.02 if detail else 0.0)

    if detail:
        shadow = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        ImageDraw.Draw(shadow).text(
            (tx + w * 0.008, ty + w * 0.012), "P", font=font,
            fill=(4, 18, 52, 130))
        canvas = Image.alpha_composite(
            canvas, shadow.filter(ImageFilter.GaussianBlur(w * 0.009)))

    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(layer).text(
        (tx, ty), "P", font=font, fill=(255, 255, 255, 255),
        stroke_width=max(1, int(w * 0.003)),
        stroke_fill=(255, 255, 255, 255))
    canvas = Image.alpha_composite(canvas, layer)

    if detail:
        deco = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        dd = ImageDraw.Draw(deco)
        # 右下三条递增数据柱: 深→亮, 代表数据/工作台
        base_y = h * 0.742
        heights = (0.112, 0.178, 0.242)
        colors = ((138, 200, 255, 225), (182, 224, 255, 238),
                  (255, 255, 255, 250))
        for i, (hh, cc) in enumerate(zip(heights, colors)):
            x0 = w * (0.648 + i * 0.096)
            x1 = x0 + w * 0.064
            y0 = base_y - h * hh
            dd.rounded_rectangle([x0, y0, x1, base_y],
                                 radius=w * 0.02, fill=cc)
        # 底部细横线, 稳重基座
        dd.rounded_rectangle(
            [w * 0.352, h * 0.795, w * 0.632,
             h * 0.795 + max(2, int(h * 0.014))],
            radius=h * 0.007, fill=(255, 255, 255, 180))
        # 右上青色数据节点(带发光)
        cx, cy, r = w * 0.782, h * 0.248, w * 0.036
        dd.ellipse([cx - r * 1.9, cy - r * 1.9, cx + r * 1.9, cy + r * 1.9],
                   fill=(96, 226, 208, 42))
        dd.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=(104, 236, 220, 250))
        deco.putalpha(ImageChops.multiply(deco.getchannel("A"), mask))
        canvas = Image.alpha_composite(canvas, deco)

    resample = getattr(Image, "LANCZOS", Image.BICUBIC)
    return canvas.resize((size, size), resample)


def _squircle_pts(w, h, margin):
    a = (w - 1) / 2 - margin
    b = (h - 1) / 2 - margin
    cx = cy = (w - 1) / 2
    pts = []
    steps = 1440
    exp = 2.0 / SQUIRCLE_N
    for i in range(steps):
        t = 2 * math.pi * i / steps
        ct, st = math.cos(t), math.sin(t)
        pts.append((cx + a * math.copysign(abs(ct) ** exp, ct),
                    cy + b * math.copysign(abs(st) ** exp, st)))
    return pts


def save_ico(images, path: Path):
    """手写 ICO 容器: 每尺寸独立 PNG, 避免统一缩放导致小图标发糊。"""
    blobs, entries, offset = [], [], 6 + 16 * len(images)
    for size, im in images.items():
        buf = io.BytesIO()
        im.save(buf, format="PNG", optimize=True)
        data = buf.getvalue()
        b = 0 if size >= 256 else size
        entries.append(struct.pack("<BBBBHHII", b, b, 0, 0, 1, 32,
                                   len(data), offset))
        blobs.append(data)
        offset += len(data)
    head = struct.pack("<HHH", 0, 1, len(images))
    path.write_bytes(head + b"".join(entries) + b"".join(blobs))


def main():
    images = {s: render(s, detail=(s >= 64)) for s in SIZES}
    ico = ASSETS / "app.ico"
    save_ico(images, ico)
    print("ICO:", ico, f"{ico.stat().st_size / 1024:.1f} KB",
          "sizes:", ",".join(str(s) for s in SIZES))

    # 独立 512px 高清大图
    big = render(512, detail=True)
    hero = ASSETS / "app_icon_512.png"
    big.save(hero, format="PNG", optimize=True)
    print("HERO:", hero, f"{big.size}")

    # 排版预览: 浅色主视觉 + 多尺寸 + 深色背景对照
    prev = Image.new("RGB", (1160, 560), (243, 247, 252))
    prev.paste(big.resize((512, 512), Image.LANCZOS), (28, 24),
               big.resize((512, 512), Image.LANCZOS))
    for i, s in enumerate((128, 64, 32, 16)):
        im = images[s]
        x, y = 596 + (i % 2) * 132, 66 + (i // 2) * 220
        prev.paste(im, (x, y), im)
    dark = Image.new("RGB", (250, 250), (20, 34, 60))
    db = images[128].resize((210, 210), Image.LANCZOS)
    dark.paste(db, (20, 20), db)
    prev.paste(dark, (880, 70))
    p = ASSETS / "app_preview.png"
    prev.save(p, format="PNG", optimize=True)
    print("PREVIEW:", p)


if __name__ == "__main__":
    main()
