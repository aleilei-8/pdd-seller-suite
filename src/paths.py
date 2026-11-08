#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""统一便携路径解析 (源码 / PyInstaller 免安装包 双模式)。

源码运行:      ROOT = 项目根目录(src 的上一级)
免安装包运行:  ROOT = exe 所在目录, 配置/登录态/报告全部写这里,
              绝不写进 PyInstaller 的临时解压目录(否则重启就丢)。

所有需要定位项目根的模块都应 from paths import ROOT / DATA / PROFILE,
不要再用 Path(__file__).resolve().parent.parent。
"""
import os
import sys
from pathlib import Path

FROZEN = bool(getattr(sys, "frozen", False))

if FROZEN:
    # onedir: exe 位于包根, _internal 只是依赖目录
    ROOT = Path(sys.executable).resolve().parent
    BUNDLE = Path(getattr(sys, "_MEIPASS", str(ROOT)))
    SRC = BUNDLE
else:
    ROOT = Path(__file__).resolve().parent.parent
    BUNDLE = Path(__file__).resolve().parent
    SRC = BUNDLE

DATA = ROOT / "data"                       # 登录态 / 采集结果 / 报告
PROFILE = ROOT / "pdd_profile"             # PDD 持久化浏览器会话
BROWSERS = ROOT / "browsers"               # 可选: 随包携带的离线浏览器

# 字符串形式, 供仍使用 os.path 的模块直接替换
ROOT_S = str(ROOT)
DATA_S = str(DATA)
SRC_S = str(SRC)
PROFILE_S = str(PROFILE)

_ENV_READY = False


def setup_env():
    """在 import playwright 之前调用: 准备数据目录 / 可选离线浏览器 / 工作目录。

    - 若包内存在非空的 browsers/ 目录, 让 Playwright 优先用它, 使程序拷到
      没装过 Playwright 浏览器的电脑也能跑; 否则沿用系统默认位置。
    - 保证 cwd 指向包根, 避免直接双击 exe 时相对路径错位。
    """
    global _ENV_READY
    if _ENV_READY:
        return
    _ENV_READY = True
    try:
        DATA.mkdir(parents=True, exist_ok=True)
    except Exception:
        pass
    try:
        if FROZEN and "PLAYWRIGHT_BROWSERS_PATH" not in os.environ:
            # PyInstaller 打包态下, Playwright 会默认去
            # _internal/playwright/driver/package/.local-browsers 找浏览器(那里是空的)。
            # 因此必须显式指定: 优先用随包携带的 browsers/, 否则复用系统已装浏览器。
            if BROWSERS.is_dir() and any(BROWSERS.iterdir()):
                os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(BROWSERS)
            else:
                local = os.environ.get("LOCALAPPDATA")
                if local:
                    sysdir = Path(local) / "ms-playwright"
                    if sysdir.is_dir():
                        os.environ["PLAYWRIGHT_BROWSERS_PATH"] = str(sysdir)
    except Exception:
        pass
    try:
        os.chdir(str(ROOT))
    except Exception:
        pass
