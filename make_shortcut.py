#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""创建「拼多多工作台」APP 快捷方式 (.lnk)。

.vbs/.bat 是脚本文本, Windows 按文件类型显示统一的脚本图标, 无法单独换;
真正的 APP 体验靠带自定义图标的 .lnk 实现。本脚本一次生成三个入口:

  1) 桌面           拼多多工作台.lnk   —— 双击即用, 像真 APP
  2) 开始菜单       拼多多工作台.lnk   —— Win 键搜索"拼多多工作台"可启动
  3) 项目根目录     拼多多工作台.lnk   —— 便于拷贝/分发/打包

快捷方式用 wscript.exe 承载 start_workbench.vbs(隐藏控制台, Playwright 不崩),
图标指向 assets/app.ico(新版 squircle 图标)。

用法: python make_shortcut.py
      python make_shortcut.py --no-desktop   (只生成项目内快捷方式)
      python make_shortcut.py --remove       (移除已创建的快捷方式)
"""
import os
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
ICO = ROOT / "assets" / "app.ico"
VBS = ROOT / "start_workbench.vbs"
NAME = "拼多多工作台.lnk"
WINDIR = Path(os.environ.get("WINDIR", "C:/Windows"))
WSCRIPT = WINDIR / "System32" / "wscript.exe"


def targets(include_desktop=True):
    out = [ROOT / NAME]
    if include_desktop:
        desktop = Path(os.environ.get("USERPROFILE", "")) / "Desktop"
        if not desktop.is_dir():
            desktop = Path(os.environ.get("USERPROFILE", "")) / "桌面"
        if desktop.is_dir():
            out.append(desktop / NAME)
        start_menu = (Path(os.environ.get("APPDATA", "")) / "Microsoft" /
                      "Windows" / "Start Menu" / "Programs")
        if start_menu.is_dir():
            out.append(start_menu / NAME)
    return out


def make(lnk: Path):
    from win32com.client import Dispatch
    shell = Dispatch("WScript.Shell")
    sc = shell.CreateShortcut(str(lnk))
    sc.TargetPath = str(WSCRIPT)
    sc.Arguments = '"{}"'.format(VBS)
    sc.WorkingDirectory = str(ROOT)
    sc.IconLocation = "{},0".format(ICO)
    sc.Description = "拼多多全链路工作台 · 采集 / 上架 / AI 助手"
    sc.WindowStyle = 7  # 最小化(配合 wscript 隐藏控制台)
    sc.save()
    return lnk.exists()


def remove(lnk: Path):
    if lnk.exists():
        lnk.unlink()
        return True
    return False


def main():
    args = sys.argv[1:]
    if "--remove" in args:
        for t in targets():
            print(("REMOVED " if remove(t) else "SKIP    ") + str(t))
        return 0
    if not ICO.is_file():
        print("[ERROR] 缺少图标:", ICO, "请先运行 python make_icon.py")
        return 1
    if not VBS.is_file():
        print("[ERROR] 缺少启动脚本:", VBS)
        return 1
    include_desktop = "--no-desktop" not in args
    ok = True
    for t in targets(include_desktop):
        good = make(t)
        ok &= good
        print(("OK      " if good else "FAILED  ") + str(t))
    print("\n完成: 双击「拼多多工作台」即可启动 GUI(无黑窗口)。")
    print("提示: 若图标未立即刷新, 注销重登或重启资源管理器即可。")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main())
