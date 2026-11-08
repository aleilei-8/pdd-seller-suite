# -*- mode: python ; coding: utf-8 -*-
"""PyInstaller onedir 免安装包配置。

用法: pyinstaller PDDWorkbench.spec --noconfirm
输出: dist/PDD工作台_免安装版/  (整个文件夹拷走即用, 无需 Python / 无需安装)
注意: 必须保持 console=True, 由启动器隐藏窗口; 改成 windowed 会让 Playwright
      driver 因缺少控制台报 "Assertion failed: process_title"。
"""
import os
from PyInstaller.utils.hooks import collect_all

# 应用图标: SPECPATH 是 PyInstaller 注入的 spec 所在目录, 不依赖调用时的 cwd
APP_ICON = os.path.join(SPECPATH, "assets", "app.ico")

# 惰性导入 / 动态导入的模块, PyInstaller 静态分析抓不到, 必须显式声明
hidden = [
    "paths", "gui", "models", "pricing", "loader", "selector", "listing",
    "orders", "analysis", "shipping", "invoice", "sku_map", "title_opt",
    "suggest", "pdd_session", "pdd_store", "pdd_publish", "freight_1688",
    "collector", "cli", "playwright.sync_api",
]

datas = []
binaries = []
for _pkg in ("customtkinter", "jieba", "playwright"):
    _d, _b, _h = collect_all(_pkg)
    datas += _d
    binaries += _b
    hidden += _h

a = Analysis(
    ["app_main.py"],
    pathex=["src"],
    binaries=binaries,
    datas=datas,
    hiddenimports=hidden,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        "matplotlib", "numpy", "pandas", "scipy", "PyQt5", "PySide2",
        "PySide6", "pytest", "IPython", "notebook", "tkinter.test",
    ],
    noarchive=False,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=None)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name="PDDWorkbench",
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=True,          # 关键: 不能改 windowed
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=APP_ICON,         # 自定义品牌图标(替代 PyInstaller 默认图标)
)

coll = COLLECT(
    exe,
    a.binaries,
    a.zipfiles,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name="PDD工作台_免安装版",
)
