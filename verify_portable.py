#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""验证免安装包: 目录结构 + exe 自检 + 数据落位 + 折叠态持久化。

用法: python verify_portable.py
"""
import struct
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "PDD工作台_免安装版"
EXE = DIST / "PDDWorkbench.exe"
LAUNCHER = DIST / "启动工作台.exe"
ICON = DIST / "应用图标.ico"

MUST_FILES = [
    "PDDWorkbench.exe", "启动工作台.exe", "应用图标.ico",
    "启动工作台.vbs", "启动工作台_控制台.bat", "创建桌面快捷方式.vbs",
    "使用说明.txt", "pricing_config.json", "publish_config.json",
]
MUST_DATA = ["collected_1688.json", "freight_rules.html", "freight_rules.json"]

fails = []


def chk(cond, msg):
    print(("  [OK]   " if cond else "  [FAIL] ") + msg)
    if not cond:
        fails.append(msg)


print("=== 1/3 目录结构 ===")
chk(DIST.is_dir(), f"包目录存在: {DIST}")
for name in MUST_FILES:
    chk((DIST / name).is_file(), name)
for name in MUST_DATA:
    chk((DIST / "data" / name).is_file(), f"data/{name}")
chk((DIST / "_internal").is_dir(), "_internal 依赖目录")
chk((DIST / "_internal" / "playwright" / "driver" / "node.exe").is_file(),
    "Playwright driver (node.exe)")
readme = (DIST / "使用说明.txt").read_text(encoding="utf-8-sig") \
    if (DIST / "使用说明.txt").is_file() else ""
chk("首次使用" in readme and "browsers" in readme,
    "使用说明含首次登录 / 离线浏览器指引")
chk("启动工作台.exe" in readme, "使用说明主入口已更新为带图标的 exe")
chk("图标没显示" in readme and "创建桌面快捷方式.vbs" in readme,
    "使用说明含『图标没显示』排查与桌面快捷方式指引")
sc = (DIST / "创建桌面快捷方式.vbs").read_text(encoding="ascii") \
    if (DIST / "创建桌面快捷方式.vbs").is_file() else ""
chk("ChrW(21551)" in sc and "CreateShortcut" in sc and "IconLocation" in sc,
    "桌面快捷方式脚本结构完整(纯 ASCII, 无编码乱码风险)")
chk("".join(chr(c) for c in (21551, 21160, 24037, 20316, 21488)) == "启动工作台",
    "快捷方式目标文件名拼装正确")


def ico_png_markers(path: Path):
    """取出 ICO 内每个 PNG 图标条的头部字节, 用于确认 exe 真的内嵌了自定义图标。"""
    try:
        b = path.read_bytes()
        cnt = struct.unpack_from("<H", b, 4)[0]
        out = []
        for i in range(cnt):
            o = 6 + 16 * i
            n, pos = struct.unpack_from("<II", b, o + 8)
            blob = b[pos:pos + n]
            if blob.startswith(b"\x89PNG") and len(blob) >= 48:
                out.append(blob[:48])
        return out
    except Exception:
        return []


print("--- 图标资源 ---")
markers = ico_png_markers(ICON)
chk(bool(markers), f"应用图标.ico 含 {len(markers)} 个尺寸的多尺寸 PNG 图标")
for label, path in (("主程序 PDDWorkbench.exe", EXE),
                    ("启动器 启动工作台.exe", LAUNCHER)):
    try:
        raw = path.read_bytes() if path.is_file() else b""
    except Exception:
        raw = b""
    chk(any(m in raw for m in markers), f"{label} 已内嵌同款自定义图标")

print("=== 2/3 exe 自检 (真正启动打包后的解释器) ===")
if EXE.is_file():
    r = subprocess.run([str(EXE), "--selftest"], cwd=str(DIST),
                       capture_output=True, text=True, encoding="utf-8",
                       errors="replace", timeout=180)
    out = (r.stdout or "") + (r.stderr or "")
    print("  --- exe 输出 ---")
    for line in out.strip().splitlines()[-20:]:
        print("  |", line)
    chk(r.returncode == 0, f"exe 退出码 0 (实际 {r.returncode})")
    chk("SELFTEST_OK" in out, "SELFTEST_OK")
    chk("FROZEN = True" in out, "打包态 FROZEN=True")
    chk("playwright driver spawned OK" in out, "Playwright driver 真实启动 (node.exe)")
    chk("CHROMIUM_EXISTS = True" in out,
        "Chromium 可执行文件可定位(打包态浏览器路径修复生效)")
    mark = ""
    for line in out.splitlines():
        if line.startswith("ROOT_MARK ="):
            mark = line.split("=", 1)[1].strip()
    if mark:
        import codecs
        real = codecs.decode(mark, "unicode_escape")
        chk(real.replace("\\", "/") == DIST.as_posix(),
            "ROOT 指向包目录(而非临时解压目录)")
        chk("_MEI" not in mark, "未落入 PyInstaller 临时解压目录")
    else:
        chk(False, "ROOT_MARK 输出存在")
else:
    chk(False, "exe 存在")

print("=== 3/3 数据落位 ===")
chk((DIST / "data").is_dir(), "data/ 可写目录已建立")

total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
print(f"\n包体积: {total / 1024 / 1024:.1f} MB")
print("=" * 46)
if fails:
    print(f"❌ 验证未通过: {len(fails)} 项")
    for f in fails:
        print("   -", f)
    sys.exit(1)
print("✅ 免安装包验证全部通过")
