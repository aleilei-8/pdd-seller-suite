#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""生成「拼多多全链路工作台」应用图标 (assets/app.ico)。

设计: 科技蓝 Ocean Gradient 圆角方块 + 白色字母标 P + 数据柱细节。
每个尺寸单独绘制(4x 超采样), 小尺寸自动省略细节, 保证 16px 仍清晰。

用法: python make_icon.py
产出: assets/app.ico (16/24/32/48/64/128/256)
      assets/app_preview.png (512px 预览)
"""
import io
import struct
from pathlib import Path

from PIL import Image, ImageChops, ImageDraw, ImageFont, ImageFilter

ASSETS = Path(__file__).resolve().parent / "assets"
ASSETS.mkdir(parents=True, exist_ok=True)
FONT_BOLD = "C:/Windows/Fonts/segoeuib.ttf"

TOP = (46, 137, 255)      # 亮蓝
MID = (21, 89, 200)       # 品牌蓝
BOT = (9, 45, 120)        # 深蓝
SIZES = (16, 24, 32, 48, 64, 128, 256)
SS = 4                    # 超采样倍数


def _lerp(a, b, t):
    return tuple(int(a[i] + (b[i] - a[i]) * t) for i in range(3))


def _gradient(w, h):
    col = Image.new("RGB", (1, h))
    p = col.load()
    for y in range(h):
        t = y / max(1, h - 1)
        p[0, y] = _lerp(TOP, MID, t / 0.55) if t < 0.55 else \
                  _lerp(MID, BOT, (t - 0.55) / 0.45)
    return col.resize((w, h), Image.BILINEAR).convert("RGBA")


def _mask(w, h, margin, radius):
    m = Image.new("L", (w, h), 0)
    ImageDraw.Draw(m).rounded_rectangle(
        [margin, margin, w - 1 - margin, h - 1 - margin],
        radius=radius, fill=255)
    return m


def _text_box(draw, text, font):
    b = draw.textbbox((0, 0), text, font=font)
    return b, b[2] - b[0], b[3] - b[1]


def render(size: int, detail: bool) -> Image.Image:
    w = h = size * SS
    margin = int(w * 0.055)
    radius = int(w * 0.215)
    mask = _mask(w, h, margin, radius)

    canvas = Image.new("RGBA", (w, h), (0, 0, 0, 0))

    # 极淡的底部投影, 让图标在浅色资源管理器背景上有立体感
    if detail:
        sh = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        off = int(w * 0.018)
        ImageDraw.Draw(sh).rounded_rectangle(
            [margin, margin + off, w - 1 - margin, h - 1 - margin + off],
            radius=radius, fill=(6, 22, 60, 90))
        canvas = Image.alpha_composite(canvas, sh.filter(
            ImageFilter.GaussianBlur(w * 0.018)))

    base = _gradient(w, h)
    base.putalpha(mask)

    # 顶部高光 (只在较大尺寸呈现)
    if detail:
        ha = Image.new("L", (1, h))
        hp = ha.load()
        for y in range(h):
            hp[0, y] = int(52 * max(0.0, 1.0 - y / (h * 0.52)))
        hl = Image.new("RGBA", (w, h), (255, 255, 255, 0))
        hl.putalpha(ha.resize((w, h), Image.BILINEAR))
        base = Image.alpha_composite(base, hl)

    canvas = Image.alpha_composite(canvas, base)

    # 圆角描边, 增加边缘质感
    edge = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    ImageDraw.Draw(edge).rounded_rectangle(
        [margin, margin, w - 1 - margin, h - 1 - margin],
        radius=radius, outline=(255, 255, 255, 70 if detail else 45),
        width=max(1, int(w * 0.006)))
    edge.putalpha(ImageChops.multiply(edge.getchannel("A"), mask))
    canvas = Image.alpha_composite(canvas, edge)

    # 文字层: 白色粗体 P
    layer = Image.new("RGBA", (w, h), (0, 0, 0, 0))
    d = ImageDraw.Draw(layer)
    font = ImageFont.truetype(FONT_BOLD, int(w * (0.60 if detail else 0.66)))
    bbox, tw, th = _text_box(d, "P", font)
    tx = (w - tw) / 2 - bbox[0]
    ty = (h - th) / 2 - bbox[1] - h * (0.025 if detail else 0.0)
    d.text((tx, ty), "P", font=font, fill=(255, 255, 255, 255),
           stroke_width=max(1, int(w * 0.004)),
           stroke_fill=(255, 255, 255, 255))
    canvas = Image.alpha_composite(canvas, layer)

    if detail:
        deco = Image.new("RGBA", (w, h), (0, 0, 0, 0))
        dd = ImageDraw.Draw(deco)
        # 右下三条递增数据柱 (浅蓝 → 白), 代表数据/工作台
        base_y = h * 0.735
        heights = (0.105, 0.165, 0.225)
        colors = ((150, 208, 255, 220), (186, 226, 255, 235),
                  (255, 255, 255, 245))
        for i, (hh, cc) in enumerate(zip(heights, colors)):
            x0 = w * (0.655 + i * 0.093)
            x1 = x0 + w * 0.062
            y0 = base_y - h * hh
            dd.rounded_rectangle([x0, y0, x1, base_y],
                                 radius=w * 0.018, fill=cc)
        # 底部细横线, 稳重的"基座"
        dd.rounded_rectangle([w * 0.355, h * 0.792, w * 0.645,
                              h * 0.792 + max(2, int(h * 0.013))],
                             radius=h * 0.007, fill=(255, 255, 255, 175))
        # 右上角青色数据节点
        cx, cy, r = w * 0.775, h * 0.255, w * 0.035
        dd.ellipse([cx - r, cy - r, cx + r, cy + r],
                   fill=(96, 226, 208, 245))
        deco.putalpha(ImageChops.multiply(deco.getchannel("A"), mask))
        canvas = Image.alpha_composite(canvas, deco)

    return canvas.resize((size, size), Image.LANCZOS)


def save_ico(images, path: Path):
    """手写 ICO 容器: 每个尺寸用独立 PNG, 避免 PIL 统一缩放导致小图标发糊。"""
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

    # 512px 高清预览 (含浅灰/白两种背景, 便于看清透明边缘)
    big = render(512, detail=True)
    prev = Image.new("RGB", (1120, 560), (246, 249, 253))
    prev.paste(big.resize((512, 512), Image.LANCZOS), (24, 24),
               big.resize((512, 512), Image.LANCZOS))
    for i, s in enumerate((128, 64, 32, 16)):
        im = images[s]
        x, y = 580 + (i % 2) * 120, 60 + (i // 2) * 210
        prev.paste(im, (x, y), im)
    # 深色背景对照
    dark = Image.new("RGB", (240, 240), (24, 38, 66))
    db = images[128].resize((200, 200), Image.LANCZOS)
    dark.paste(db, (20, 20), db)
    prev.paste(dark, (830, 60))
    p = ASSETS / "app_preview.png"
    prev.save(p, format="PNG", optimize=True)
    print("PREVIEW:", p)


if __name__ == "__main__":
    main()
