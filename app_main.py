#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""免安装包入口 (PyInstaller 用)。

源码态也能直接运行: python app_main.py
支持 --selftest: 只做环境自检并打印结果, 不弹 GUI, 供打包验证使用。
"""
import os
import sys
import traceback
from pathlib import Path

# 源码态: 把 src 加入模块搜索路径; 打包态该目录不存在(模块已在 PYZ 内), 跳过即可
_src = Path(__file__).resolve().parent / "src"
if _src.is_dir():
    sys.path.insert(0, str(_src))

import paths
paths.setup_env()


def _selftest():
    print("FROZEN =", paths.FROZEN)
    print("ROOT   =", paths.ROOT)
    print("DATA   =", paths.DATA)
    print("PROFILE=", paths.PROFILE)
    print("cwd    =", Path.cwd())
    import customtkinter as ctk
    print("customtkinter =", ctk.__version__)
    import gui
    print("gui.ROOT =", gui.ROOT)
    print("CONFIG_PATH =", gui.CONFIG_PATH)
    import pdd_publish, pdd_session, pdd_store, freight_1688, collector
    print("modules OK: pdd_publish / pdd_session / pdd_store / freight_1688 / collector")
    import jieba
    print("jieba OK")
    # 真正 spawn 一次 driver (node.exe), 验证中文路径/打包环境下驱动可用
    from playwright.sync_api import sync_playwright
    pw = sync_playwright().start()
    print("playwright driver spawned OK")
    try:
        cpath = Path(pw.chromium.executable_path)
        print("CHROMIUM_EXISTS =", cpath.is_file())
        print("BROWSERS_PATH =", os.environ.get("PLAYWRIGHT_BROWSERS_PATH", ""))
    except Exception as e:
        print("chromium path note:", e)
    pw.stop()
    # ASCII 安全标记: 控制台可能是 GBK, 中文目录名会乱码, 用转义串做机器校验
    print("ROOT_MARK =", paths.ROOT.as_posix().encode("unicode_escape").decode("ascii"))
    app = gui.Workbench()
    print("Workbench OK: pages =", len(app.tabs._pages), "active =", app.tabs._active)
    app.destroy()
    print("SELFTEST_OK")
    return 0


def _fatal(exc):
    """启动失败时把堆栈写到包内 data, 并弹窗, 避免隐藏窗口下静默失败。"""
    txt = "拼多多工作台启动失败:\n" + "".join(
        traceback.format_exception(type(exc), exc, exc.__traceback__))
    try:
        log = paths.DATA / "launch_error.log"
        log.parent.mkdir(parents=True, exist_ok=True)
        log.write_text(txt, encoding="utf-8")
    except Exception:
        pass
    try:
        import tkinter as tk
        from tkinter import messagebox
        r = tk.Tk()
        r.withdraw()
        messagebox.showerror("启动失败", txt[-1500:])
        r.destroy()
    except Exception:
        pass
    print(txt)
    return 1


if __name__ == "__main__":
    if "--selftest" in sys.argv:
        try:
            sys.exit(_selftest())
        except Exception as e:
            sys.exit(_fatal(e))
    try:
        from gui import Workbench
        Workbench().mainloop()
    except Exception as e:
        sys.exit(_fatal(e))
