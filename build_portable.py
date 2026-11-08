#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""构建「免安装程序包」(PyInstaller onedir)。

用法:
    python build_portable.py                 # 标准包(推荐): 不含登录态/浏览器, 体积最小
    python build_portable.py --with-login    # 附带本机 1688/PDD 登录态(仅限自用, 勿外发)
    python build_portable.py --with-browsers # 附带 Chromium, 拷到没装浏览器的电脑也能跑
    python build_portable.py --skip-build    # 跳过 PyInstaller, 只刷新数据/启动器/说明

输出: dist/PDD工作台_免安装版/  —— 整个文件夹拷走即用, 不需要 Python、不需要 pip install。
     主入口是带正式图标、无黑窗的「启动工作台.exe」。
"""
import argparse
import os
import shutil
import subprocess
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent
DIST = ROOT / "dist" / "PDD工作台_免安装版"
SPEC = ROOT / "PDDWorkbench.spec"
ICON = ROOT / "assets" / "app.ico"
LAUNCHER_EXE = "启动工作台.exe"
LAUNCHER_BUILD = ROOT / "build" / "launcher_dist" / "WorkbenchLauncher.exe"

# 随包携带的非敏感数据(报告/样本/采集结果)
CORE_FILES = [
    "pricing_config.json", "publish_config.json",
    "store_products.json", "suggest_cache.json",
]
CORE_DATA = [
    "collected_1688.json", "freight_rules.json", "freight_rules.csv",
    "freight_rules.html", "sample_goods.json", "sample_listing.csv",
    "sample_report_listing.csv", "sample_report_selection.csv",
    "sample_report_selection.json",
]
# 登录态(敏感, 仅 --with-login 时复制)
LOGIN_DATA = [
    "1688_state.json", "1688_channel.txt", "1688_profile_channel.txt",
]

VBS = r'''Option Explicit
Dim WshShell, fso, scriptDir, exePath, cmd
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
exePath = fso.BuildPath(scriptDir, "PDDWorkbench.exe")
If Not fso.FileExists(exePath) Then
    MsgBox "Cannot find PDDWorkbench.exe next to this launcher.", vbExclamation, "Launch failed"
    WScript.Quit 1
End If
WshShell.CurrentDirectory = scriptDir
cmd = """" & exePath & """"
WshShell.Run cmd, 0, False
'''

BAT = r'''@echo off
chcp 65001 >nul
cd /d "%~dp0"
echo Starting PDD Workbench (console kept for troubleshooting)...
"PDDWorkbench.exe"
if errorlevel 1 pause
'''

# 一键在桌面创建带正式图标的快捷方式。
# 全部字符串用 ChrW 拼出中文, 文件保持纯 ASCII, 避免 VBScript 的 ANSI 编码乱码。
SHORTCUT_VBS = r'''Option Explicit
Dim WshShell, fso, scriptDir, exeName, exePath, lnk, lnkName, desktopPath
Set WshShell = CreateObject("WScript.Shell")
Set fso = CreateObject("Scripting.FileSystemObject")
scriptDir = fso.GetParentFolderName(WScript.ScriptFullName)
exeName = ChrW(21551) & ChrW(21160) & ChrW(24037) & ChrW(20316) & ChrW(21488) & ".exe"
exePath = fso.BuildPath(scriptDir, exeName)
If Not fso.FileExists(exePath) Then
  MsgBox "Cannot find the launcher next to this script.", vbExclamation, "Shortcut failed"
  WScript.Quit 1
End If
desktopPath = WshShell.SpecialFolders("Desktop")
lnkName = ChrW(25340) & ChrW(22810) & ChrW(22810) & ChrW(24037) & ChrW(20316) & ChrW(21488) & ".lnk"
Set lnk = WshShell.CreateShortcut(fso.BuildPath(desktopPath, lnkName))
lnk.TargetPath = exePath
lnk.WorkingDirectory = scriptDir
lnk.IconLocation = exePath & ",0"
lnk.Description = "PDD Workbench"
lnk.Save
MsgBox "Desktop shortcut created: PDD Workbench", vbInformation, "Done"
'''

README = """拼多多全链路工作台 · 免安装版
================================================

【怎么启动】
  双击「启动工作台.exe」即可（正式图标、无黑窗，推荐）。
  首次解压后建议双击「创建桌面快捷方式.vbs」，桌面会生成带正式图标的
  「拼多多工作台」快捷方式；也可右键 exe →「发送到」→「桌面快捷方式」，
  或「固定到任务栏 / 开始屏幕」，图标会自动跟随。
  「启动工作台.vbs」是同效果的备用启动器（旧版入口）。
  排错时用「启动工作台_控制台.bat」，会保留控制台看报错。

【图标没显示？】
  在 360 压缩 / WinRAR 里浏览 zip 内部时，压缩软件只显示通用类型图标，
  不会读取 exe 内嵌图标（连 .ico 文件本身也会显示成空白），这是正常现象。
  请把整个文件夹解压出来，用资源管理器查看即可看到正式图标。
  若解压后仍是空白，是 Windows 图标缓存没刷新：运行 ie4uinit.exe -show，
  或重启资源管理器 / 注销一次；最省事的是直接双击「创建桌面快捷方式.vbs」。

【从一条新 1688 链接直接上架】
  工作台『上架导出』页 → 点『采集1688新链接』→ 粘贴 1688 商品链接 → 开始采集，
  采完自动生成上架清单：主图 / 详情图 / 全部 SKU 组合 / 属性一并搬迁，
  再点『一键发布到拼多多』即可。首次需在工作台点一次『登录1688』。
  『升级旧采集数据』会把早期只含主图+规格名的旧记录重新采集补齐。
  说明：SKU 默认全量（发布配置 max_sku=0）；规格维度/规格值需在拼多多后台
  创建或选择，脚本负责把每款价格、库存、单买价、拼单价填入已有规格行。

【完全免安装】
  不需要 Python、不需要 pip install、不写注册表。
  整个文件夹可以直接复制到 U 盘或另一台电脑，双击即用。
  所有配置、采集结果、报告、登录态都保存在本文件夹内，不会跑到系统目录。

【首次使用：登录】
  1688 采集 / 拼多多发布 需要各自的登录态。首次使用时在工作台里
  点「登录 1688」「登录拼多多」完成一次登录，登录态会自动保存在本文件夹：
      data/1688_state.json、data/1688_profile/、pdd_profile/
  之后每次启动自动复用，无需重复登录。

【关于浏览器】
  本包已内置 Playwright 驱动，1688 采集默认调用系统已安装的 Chrome / Edge。
  拼多多登录、联想词等功能会用到 Playwright 自带 Chromium：
    - 若本机已装过 Playwright 浏览器（%LOCALAPPDATA%\\ms-playwright），直接可用；
    - 若要拷到全新电脑，把 chromium-1208 / chromium_headless_shell-1208 /
      ffmpeg-1011 三个目录放进本文件夹的 browsers\\ 目录即可，
      程序会自动识别并优先使用，无需任何环境变量配置。
      （也可用 build_portable.py --with-browsers 自动生成完整离线包）

【目录说明】
  创建桌面快捷方式.vbs   一键生成带正式图标的桌面快捷方式
  启动工作台.exe         主入口（带正式图标、隐藏黑窗）
  应用图标.ico           同款图标源文件，可用于自定义快捷方式
  PDDWorkbench.exe      主程序（由启动器调用，一般无需双击）
  _internal\\           运行依赖（请勿删除/改名）
  data\\                登录态、采集结果、运费报告
  pdd_profile\\         拼多多浏览器会话（登录后自动创建）
  ui_state.json         界面状态（侧栏折叠等偏好）
  pricing_config.json   全局定价参数
  publish_config.json   发布参数

【出问题怎么办】
  data\\launch_error.log 会记录启动异常；也可运行控制台版查看实时输出。

【隐私提醒】
  若用 --with-login 构建，包内会带有你的 1688 / 拼多多登录 Cookie，
  属于个人凭证，**请勿外发或上传到网盘**。发给别人请用标准包。
"""


def copy_optional(src: Path, dst: Path, label: str):
    if not src.exists():
        print(f"  [skip] {label}: 源不存在")
        return
    dst.parent.mkdir(parents=True, exist_ok=True)
    if src.is_dir():
        if dst.exists():
            shutil.rmtree(dst)
        shutil.copytree(src, dst)
    else:
        shutil.copy2(src, dst)
    print(f"  [ok]   {label}")


def build():
    print("=== 1/4 PyInstaller 构建 (onedir) ===")
    # PyInstaller --noconfirm 会先"清空"已有输出目录(上千文件), 可能触发安全删除拦截。
    # 这里改为把旧包整体重命名保留为备份, 让目标路径为空再构建: 不删任何文件, 可随时回退。
    if DIST.is_dir():
        import datetime
        bak = DIST.parent / (DIST.name + "_backup_" +
                             datetime.datetime.now().strftime("%Y%m%d_%H%M%S"))
        DIST.rename(bak)
        print(f"  [backup] 旧包已保留: {bak.name}")
    cmd = [sys.executable, "-m", "PyInstaller", str(SPEC.name),
           "--noconfirm", "--distpath", str(ROOT / "dist"),
           "--workpath", str(ROOT / "build"), "--clean"]
    print("  ", " ".join(cmd))
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0:
        print("❌ PyInstaller 构建失败")
        return False
    return True


def build_launcher():
    """构建「启动工作台.exe」: windowed onefile, 带正式图标, 隐藏黑窗启动主程序。"""
    print("=== 1b/4 构建带图标的启动器 (onefile / windowed) ===")
    if not ICON.is_file():
        print("❌ 缺少 assets/app.ico, 请先运行: python make_icon.py")
        return False
    out_dir = ROOT / "build" / "launcher_dist"
    cmd = [sys.executable, "-m", "PyInstaller",
           "--onefile", "--windowed", "--noconfirm",
           "--icon", str(ICON),
           "--name", "WorkbenchLauncher",
           "--distpath", str(out_dir),
           "--workpath", str(ROOT / "build" / "launcher_work"),
           "--specpath", str(ROOT / "build" / "launcher_spec"),
           "--log-level", "WARN",
           str(ROOT / "launcher_main.py")]
    out_dir.mkdir(parents=True, exist_ok=True)
    r = subprocess.run(cmd, cwd=str(ROOT))
    if r.returncode != 0 or not LAUNCHER_BUILD.is_file():
        print("❌ 启动器构建失败")
        return False
    DIST.mkdir(parents=True, exist_ok=True)
    shutil.copy2(LAUNCHER_BUILD, DIST / LAUNCHER_EXE)
    print(f"  [ok]   {LAUNCHER_EXE} ({LAUNCHER_BUILD.stat().st_size / 1024 / 1024:.1f} MB)")
    return True


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--with-login", action="store_true", help="附带本机登录态(敏感)")
    ap.add_argument("--with-browsers", action="store_true", help="附带 Chromium 离线浏览器")
    ap.add_argument("--skip-build", action="store_true", help="跳过 PyInstaller 只刷新数据/启动器")
    args = ap.parse_args()

    if not args.skip_build:
        if not build():
            return 1
        if not build_launcher():
            return 1
    elif not (DIST / LAUNCHER_EXE).exists():
        if not build_launcher():
            return 1
    if not (DIST / "PDDWorkbench.exe").exists():
        print(f"❌ 未找到 {DIST / 'PDDWorkbench.exe'}, 请先构建")
        return 1

    print("=== 2/4 复制数据文件 ===")
    for name in CORE_FILES:
        copy_optional(ROOT / name, DIST / name, name)
    for name in CORE_DATA:
        copy_optional(ROOT / "data" / name, DIST / "data" / name, f"data/{name}")
    if args.with_login:
        for name in LOGIN_DATA:
            copy_optional(ROOT / "data" / name, DIST / "data" / name, f"data/{name} (登录态)")
        copy_optional(ROOT / "data" / "1688_profile", DIST / "data" / "1688_profile",
                      "data/1688_profile (登录态)")
        copy_optional(ROOT / "pdd_profile", DIST / "pdd_profile", "pdd_profile (登录态)")
    else:
        (DIST / "data").mkdir(parents=True, exist_ok=True)
        print("  [skip] 登录态未打包 → 首次运行需登录一次（标准包，可安全分发）")

    print("=== 3/4 生成启动器与说明 ===")
    (DIST / "启动工作台.vbs").write_text(VBS, encoding="ascii")
    (DIST / "启动工作台_控制台.bat").write_text(BAT, encoding="ascii")
    (DIST / "创建桌面快捷方式.vbs").write_text(SHORTCUT_VBS, encoding="ascii")
    (DIST / "使用说明.txt").write_text(README, encoding="utf-8-sig")
    copy_optional(ICON, DIST / "应用图标.ico", "应用图标.ico")
    print(f"  [ok]   {LAUNCHER_EXE} / 快捷方式.vbs / vbs / bat / ico / 使用说明.txt")

    if args.with_browsers:
        print("=== 4/4 复制离线浏览器 ===")
        base = Path(os.environ.get("LOCALAPPDATA", "")) / "ms-playwright"
        for d in ("chromium-1208", "chromium_headless_shell-1208", "ffmpeg-1011"):
            copy_optional(base / d, DIST / "browsers" / d, f"browsers/{d}")
    else:
        print("=== 4/4 跳过离线浏览器（默认复用系统已装浏览器） ===")

    total = sum(f.stat().st_size for f in DIST.rglob("*") if f.is_file())
    print(f"\n✅ 免安装包完成: {DIST}")
    print(f"   体积: {total / 1024 / 1024:.1f} MB")
    return 0


if __name__ == "__main__":
    sys.exit(main())
