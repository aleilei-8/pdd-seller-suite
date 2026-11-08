#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
pdd_session.py —— 拼多多商家后台 登录态管理

复用 tax-daikai-bot 的持久化上下文范式:
  - launch_persistent_context 返回 BrowserContext(不是 Browser, 没有 .contexts())
  - user_data_dir 落在项目内的 pdd_profile/, 登录一次后自动复用(cookie 持久化)
  - 首次运行需用户在弹出的浏览器窗口里扫码/输密码完成登录(一次性)

安全原则(对齐用户个人文件规则):
  - 登录态只保存在本项目 pdd_profile/, 不外泄
  - 任何"下架/删除"动作都不在本模块执行, 由 pdd_store 里的确认清单门控
"""
import os
import time
from pathlib import Path

import paths as _paths
_paths.setup_env()          # 必须在 import playwright 之前, 让可选离线浏览器生效
from paths import PROFILE_S

from playwright.sync_api import sync_playwright

MMS_LOGIN = "https://mms.pinduoduo.com/login/"
MMS_HOME = "https://mms.pinduoduo.com/"

# 项目内持久化登录目录(不要放到桌面/下载等个人目录)
PROFILE_DIR = PROFILE_S


def launch_context(headless: bool = False):
    """启动持久化上下文, 返回 (pw, context)"""
    os.makedirs(PROFILE_DIR, exist_ok=True)
    pw = sync_playwright().start()
    context = pw.chromium.launch_persistent_context(
        user_data_dir=PROFILE_DIR,
        headless=headless,
        args=[
            "--disable-blink-features=AutomationControlled",
            "--no-sandbox",
        ],
        locale="zh-CN",
    )
    return pw, context


def is_logged_in(page) -> bool:
    """已登录判定(正向信号优先, 不仅凭 URL).

    拼多多商家后台是 SPA, 登录成功后是客户端路由, 浏览器地址栏的 URL 常常长期停留在
    /login/ 而实际已渲染出后台(你看着就是登录成功了). 因此绝不能 'URL 含 /login/ 就判未登录',
    否则正向检测永远没机会执行 -> 等满超时.

    策略:
      1) 命中后台专属元素(商品管理/订单管理/数据中心/工作台/退出登录/用户昵称) -> 已登录
      2) 出现登录表单(密码框/短信验证码框)且无后台元素 -> 未登录
      3) 以上都不明显时, 回退到 URL 启发式(含 login/passport/auth 判未登录)
    """
    # 1) 强正向信号: 仅后台才有的导航/账户元素
    positive = [
        "text=商品管理", "text=订单管理", "text=数据中心", "text=工作台",
        "text=店铺管理", "text=营销活动", "text=售后管理", "text=退出登录",
        "text=我的店铺", "text=商家后台",
    ]
    try:
        for sel in positive:
            if page.locator(sel).count() > 0:
                return True
    except Exception:
        pass
    # 用户昵称/头像区(已登录标志): 仅在没有登录表单时才算正向信号
    try:
        has_login_form = page.locator("input[type=password]").count() > 0
        if not has_login_form and page.locator(
            ".user-name, .user-info, .avatar, [class*=userName], "
            "[class*=userInfo], [class*=nickname], [class*=NickName]"
        ).count() > 0:
            return True
    except Exception:
        pass

    # 2) 负向信号: 登录表单(密码框或短信验证码框)且无非登录态
    try:
        has_pwd = page.locator("input[type=password]").count() > 0
        has_sms = page.locator(
            "input[placeholder*='验证码'], input[maxlength='6']"
        ).count() > 0
        if has_pwd or has_sms:
            return False
    except Exception:
        pass

    # 3) URL 启发式(最后兜底)
    try:
        url = (page.url or "").lower()
    except Exception:
        return False
    if "login" in url or "passport" in url or "auth" in url or "signin" in url:
        return False
    return True


def ensure_login(context, timeout: int = 240, progress=None) -> "Page":
    """确保已登录: 未登录则打开登录页等待用户手动登录(扫码/账号)。
    progress: 可选回调 progress(msg), 用于把等待进度回传到 GUI。"""
    def prog(msg):
        if progress:
            try:
                progress(f"登录: {msg}")
            except Exception:
                pass

    page = context.pages[0] if context.pages else context.new_page()
    page.goto(MMS_LOGIN, wait_until="domcontentloaded", timeout=60000)
    if is_logged_in(page):
        prog("检测到已登录(复用 pdd_profile 会话)")
        return page

    print("=" * 60)
    print("请在弹出的浏览器窗口中登录拼多多商家后台:")
    print("  · 手机扫码 或 账号密码登录")
    print("  · 登录成功后本程序会自动继续")
    print(f"  · 等待上限 {timeout} 秒")
    print("=" * 60)
    prog("浏览器已打开登录页, 请在弹出的窗口里完成登录/扫码(成功后自动继续)")

    start = time.time()
    last_tip = 0
    last_url = ""
    while time.time() - start < timeout:
        try:
            last_url = page.url or ""
            if is_logged_in(page):
                print("✅ 登录成功, 会话已保存到 pdd_profile/")
                prog("✅ 登录成功, 正在进入商品列表页…")
                # 等待跳转稳定
                try:
                    page.wait_for_load_state("networkidle", timeout=15000)
                except Exception:
                    pass
                return page
        except Exception:
            pass
        if time.time() - last_tip > 15:
            prog(f"仍在等待登录 ({int(time.time() - start)}s), 请在浏览器完成登录/安全验证(不要关闭窗口)")
            last_tip = time.time()
        time.sleep(2)

    # 超时诊断: 带上最后停留页 URL, 给出针对性指引
    diag = f"登录等待超时({timeout}s)。最后停留页: {last_url or '无法获取'}"
    if "/login/" in last_url or "passport" in last_url or "auth" in last_url:
        diag += ("\n（页面仍在登录/验证地址）。若你已在浏览器里看到后台菜单(如『商品管理』), "
                 "多为 SPA 客户端路由导致地址栏没变——本程序已优化检测, 请直接重试点『扫描在售商品』(会话已保存, 会自动续上)。"
                 "\n若确实仍停在登录页, 请在浏览器手动完成登录/扫码, 再重试一次。")
    else:
        diag += ("\n（已离开登录地址, 但未能确认后台菜单）。多半停留在『选择店铺』『安全验证』等中间页, "
                 "请先在弹出的浏览器里完成那一步, 再重新点『扫描在售商品』。")
    raise TimeoutError(diag)


if __name__ == "__main__":
    pw, ctx = launch_context(headless=False)
    try:
        ensure_login(ctx)
        print("登录态就绪, 可以交由 pdd_store 继续操作。")
        input("按回车关闭浏览器…")
    finally:
        ctx.close()
        pw.stop()
