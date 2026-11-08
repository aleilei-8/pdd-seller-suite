#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""「启动工作台.exe」—— 带正式图标、无黑窗的绿色启动器。

只做一件事: 隐藏控制台启动同目录下的 PDDWorkbench.exe, 然后立即退出。
主程序必须保持 console 模式(Playwright driver 需要控制台), 因此黑窗由本启动器
用 STARTF_USESHOWWINDOW / SW_HIDE 隐藏, 等价于旧版 vbs 的 WshShell.Run ..., 0。
"""
import ctypes
import subprocess
import sys
from pathlib import Path

MB_ICONERROR = 0x10


def _alert(msg: str):
    try:
        ctypes.windll.user32.MessageBoxW(
            0, msg, "拼多多工作台 · 启动失败", MB_ICONERROR)
    except Exception:
        pass


def main() -> int:
    base = Path(sys.executable).resolve().parent
    exe = base / "PDDWorkbench.exe"
    if not exe.is_file():
        _alert(f"未找到主程序：\n{exe}\n\n请确认 PDDWorkbench.exe 与本启动器在同一目录，"
               "且解压时保留了完整文件夹结构。")
        return 1
    try:
        si = subprocess.STARTUPINFO()
        si.dwFlags |= subprocess.STARTF_USESHOWWINDOW
        si.wShowWindow = 0                     # SW_HIDE: 隐藏主程序的控制台黑窗
        # 不重定向 stdout/stderr: 让主程序保留真实控制台句柄,
        # Playwright driver 才不会因无控制台报 "Assertion failed: process_title"。
        subprocess.Popen([str(exe)], cwd=str(base), startupinfo=si)
    except Exception as e:
        _alert(f"启动主程序时出错：\n{e}")
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main())
