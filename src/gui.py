#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""
gui.py —— 拼多多全链路工作台(数据驱动为主)

八个 Tab:
  选品评分 | 标题优化 | 定价利润(核心) | 店铺分析 | 上架导出 | 下架 | 发货 | 开票

设计:
  - 全部数据驱动(导入 CSV/JSON/1688缓存), 不强制登录;
  - 长任务(导入/抓推流词/扫描店铺)走子线程 + queue, 主线程 after 轮询渲染, 避免卡死;
  - 定价利润页是核心: 逐商品展示 进货/进货运费/发货运费/佣金 → 实时净利与建议售价。
"""
import os
import sys
import re
import json
import time
import queue
import threading
import tkinter as tk
from pathlib import Path
from tkinter import ttk, messagebox, filedialog

import customtkinter as ctk

import paths as _paths
_paths.setup_env()
from paths import ROOT, DATA, SRC

HERE = SRC
CONFIG_PATH = (ROOT / "pricing_config.json").as_posix()

# 让 GUI 可 import 同目录模块 (打包后 SRC 指向解压目录, 依然可用)
sys.path.insert(0, HERE.as_posix())

import models
import pricing
import loader
import selector
import listing
import orders as orders_mod
import analysis
import shipping as shipping_mod
import invoice as invoice_mod
# 注意: title_opt(jieba) 与 suggest(playwright) 体积大, 改为按需惰性导入, 不阻塞启动
# (用到时再 import, 见 _on_optimize / _build_title_profile / _fetch_suggest)

ctk.set_appearance_mode("Light")
ctk.set_default_color_theme("blue")

# 等级配色
GRADE_COLORS = {"强烈推荐": "#0f9d58", "可上架": "#2563eb",
                "谨慎": "#e0820c", "淘汰": "#dd4444", "": "#6d7d99"}

# ======================= 统一设计系统 (v2 视觉重构) =======================
UI_BG       = "#f4f7fc"   # 窗口/内容区底色
UI_CARD     = "#ffffff"   # 卡片
UI_BORDER   = "#dfe8f5"   # 描边
UI_TEXT     = "#1b2b45"   # 主文字
UI_MUTED    = "#6d7d99"   # 次要文字
UI_BLUE     = "#2563eb"
UI_BLUE_D   = "#1d4ed8"
UI_PURPLE   = "#7c3aed"
UI_GREEN    = "#0f9d58"
UI_ORANGE   = "#e0820c"
UI_RED      = "#dd4444"
UI_SOFT     = "#eaf2ff"
UI_SOFT_WARN= "#fff4e5"
UI_SOFT_RED = "#fff0ee"
UI_SOFT_GRN = "#eafaf1"

TONE = {"blue": UI_BLUE, "purple": UI_PURPLE, "green": UI_GREEN,
        "orange": UI_ORANGE, "red": UI_RED}


def F(size=12, bold=False):
    """统一字体(微软雅黑), 全局字号比旧版大 1~2 号。"""
    return ctk.CTkFont(family="Microsoft YaHei UI", size=size,
                       weight="bold" if bold else "normal")


_TREE_STYLE_READY = False


def _init_tree_style():
    """ttk.Treeview 统一皮肤: 大字号 / 高行距 / 蓝色表头 / 选中高亮。"""
    global _TREE_STYLE_READY
    if _TREE_STYLE_READY:
        return
    st = ttk.Style()
    try:
        st.theme_use("clam")       # clam 下 heading 背景/内边距才完全生效
    except Exception:
        pass
    st.configure("WB.Treeview", font=("Microsoft YaHei UI", 11), rowheight=30,
                 background=UI_CARD, fieldbackground=UI_CARD, foreground=UI_TEXT,
                 borderwidth=0, relief="flat")
    st.configure("WB.Treeview.Heading", font=("Microsoft YaHei UI", 11, "bold"),
                 background="#e7f0fd", foreground="#1d4ed8",
                 borderwidth=0, relief="flat", padding=(9, 9))
    st.map("WB.Treeview", background=[("selected", UI_BLUE)],
           foreground=[("selected", "#ffffff")])
    st.map("WB.Treeview.Heading", background=[("active", "#d7e6fb")])
    try:
        st.configure("TScrollbar", troughcolor=UI_BG, background="#c8d7ec",
                     bordercolor=UI_BG, arrowcolor=UI_MUTED, width=12)
    except Exception:
        pass
    _TREE_STYLE_READY = True
# ======================================================================



class Table:
    """统一表格: 大字号 / 高行距 / 斑马纹 / 双向滚动条 / 固定可视高度。

    固定可视高度是关键 —— 表格不再无限占满整屏, 显示不下的行用滚动条查看,
    腾出的空间留给分区标题、说明与详情卡片。
    """
    ROW_H = 30
    MAX_ROWS = 14

    def __init__(self, master, columns, widths=None, height=18):
        _init_tree_style()
        rows = max(4, min(int(height or 12), self.MAX_ROWS))
        self.frame = ctk.CTkFrame(master, fg_color=UI_CARD, corner_radius=12,
                                  border_width=1, border_color=UI_BORDER)
        # 固定高度内容层: 外部即使 expand=True, 表格也只占这么多
        self.inner = ctk.CTkFrame(self.frame, fg_color=UI_CARD,
                                  height=rows * self.ROW_H + 42)
        self.inner.pack(fill="x", padx=3, pady=3)
        self.inner.pack_propagate(False)
        hsb = ttk.Scrollbar(self.inner, orient="horizontal")
        hsb.pack(side="bottom", fill="x")
        box = ctk.CTkFrame(self.inner, fg_color=UI_CARD)
        box.pack(fill="both", expand=True)
        self.tree = ttk.Treeview(box, columns=columns, show="headings",
                                 height=rows, style="WB.Treeview")
        for i, c in enumerate(columns):
            self.tree.heading(c, text=c)
            self.tree.column(c, width=(widths[i] if widths else 110),
                             minwidth=60, anchor="center", stretch=True)
        vsb = ttk.Scrollbar(box, orient="vertical", command=self.tree.yview)
        self.tree.configure(yscrollcommand=vsb.set, xscrollcommand=hsb.set)
        hsb.configure(command=self.tree.xview)
        self.tree.pack(side="left", fill="both", expand=True)
        vsb.pack(side="right", fill="y")
        self.tree.tag_configure("alt", background="#f4f8ff")
        for _g, _c in GRADE_COLORS.items():
            self.tree.tag_configure(_g, foreground=_c)

    def clear(self):
        for r in self.tree.get_children():
            self.tree.delete(r)

    def add(self, values, tag=None):
        self.tree.insert("", "end", values=values, tags=(tag or "",))

    def rows(self):
        return [self.tree.item(r)["values"] for r in self.tree.get_children()]

    def selected(self):
        sel = self.tree.selection()
        return self.tree.item(sel[0])["values"] if sel else None

# ======================= 侧栏式导航容器 =======================
class _NavTip:
    """折叠态的轻量文字提示: 只剩图标时告诉用户这是什么功能。"""

    def __init__(self, widget, text, enabled):
        self.widget, self.text, self.enabled = widget, text, enabled
        self.tip = None
        widget.bind("<Enter>", self._show, add="+")
        widget.bind("<Leave>", self._hide, add="+")
        widget.bind("<ButtonPress>", self._hide, add="+")

    def _show(self, _=None):
        if self.tip or not self.enabled():
            return
        try:
            w = self.widget
            x = w.winfo_rootx() + w.winfo_width() + 10
            y = w.winfo_rooty() + max(0, (w.winfo_height() - 26) // 2)
            self.tip = tk.Toplevel(w)
            self.tip.wm_overrideredirect(True)
            self.tip.wm_geometry(f"+{x}+{y}")
            tk.Label(self.tip, text=self.text, bg="#1b2b45", fg="#ffffff",
                     font=("Microsoft YaHei", 11), padx=10, pady=5).pack()
        except Exception:
            self.tip = None

    def _hide(self, _=None):
        if self.tip:
            try:
                self.tip.destroy()
            except Exception:
                pass
            self.tip = None


class _TabHost:
    """左侧竖向分组图标侧栏(可折叠) + 右侧堆叠内容区。

    对外只暴露 .tab(name) 接口, 返回内容容器 frame, 让既有 _tab_* 填充函数
    一行都不用改; 切换 tab 由侧栏按钮驱动 (hide/show 用 lift/lower 控制层叠)。
    折叠态只留图标(64px), 展开态图标+文字+分组标题(204px)。
    """

    W_OPEN = 204
    W_MINI = 64
    UI_STATE = ROOT / "ui_state.json"   # 便携包下 ROOT = exe 目录, 保证可写

    GROUPS = [
        ("运营分析", [
            ("选品评分", "🌟"),
            ("标题优化", "✏️"),
            ("定价利润", "💰"),
            ("店铺分析", "📊"),
        ]),
        ("执行操作", [
            ("上架导出", "📤"),
            ("下架",     "⬇️"),
            ("发货",     "🚚"),
            ("开票",     "🧾"),
        ]),
    ]

    def __init__(self, master):
        self._pages = {}
        self._btns = {}          # name -> (button, icon)
        self._group_labels = []
        self._active = None
        self._collapsed = False

        self.root = ctk.CTkFrame(master, fg_color="transparent")
        self.root.pack(fill="both", expand=True, padx=14, pady=(0, 14))

        # 左侧竖向侧栏
        self.sidebar = ctk.CTkFrame(self.root, width=self.W_OPEN, fg_color=UI_CARD,
                                    corner_radius=14, border_width=1,
                                    border_color=UI_BORDER)
        self.sidebar.pack(side="left", fill="y", padx=(0, 14))
        self.sidebar.pack_propagate(False)

        # 品牌行 + 折叠开关(始终可见)
        brand = ctk.CTkFrame(self.sidebar, fg_color="transparent")
        brand.pack(fill="x", padx=16, pady=(14, 6))
        self._brand1 = ctk.CTkLabel(brand, text="拼多多", font=F(15, True),
                                    text_color=UI_BLUE)
        self._brand1.pack(side="left")
        self._brand2 = ctk.CTkLabel(brand, text="工作台", font=F(15, True),
                                    text_color=UI_TEXT)
        self._brand2.pack(side="left", padx=(2, 0))
        self._toggle = ctk.CTkButton(brand, text="«", width=28, height=26,
                                     font=F(14, True), corner_radius=7,
                                     fg_color="#eef3fb", hover_color="#dbe6f5",
                                     text_color=UI_BLUE, command=self.toggle)
        self._toggle.pack(side="right")
        self._brand_line = ctk.CTkFrame(self.sidebar, height=1, fg_color=UI_BORDER)
        self._brand_line.pack(fill="x", padx=16, pady=(0, 4))

        # 右侧内容堆叠区
        self.content = ctk.CTkFrame(self.root, fg_color="transparent")
        self.content.pack(side="left", fill="both", expand=True)

        for g_title, items in self.GROUPS:
            lab = ctk.CTkLabel(self.sidebar, text=g_title, font=F(11, True),
                               text_color=UI_MUTED)
            lab.pack(anchor="w", padx=16, pady=(14, 4))
            self._group_labels.append((lab, g_title))
            for name, icon in items:
                self._add_page(name, icon)

        # 默认选中第一组第一个
        self.select(self.GROUPS[0][1][0][0])
        self._load_state()

    def _add_page(self, name, icon):
        pg = ctk.CTkFrame(self.content, fg_color="transparent")
        pg.place(relx=0, rely=0, relwidth=1, relheight=1)
        self._pages[name] = pg

        btn = ctk.CTkButton(
            self.sidebar, text=f"  {icon}  {name}", height=50, anchor="w",
            font=F(13, True), corner_radius=10, fg_color="transparent",
            hover_color="#eaf2ff", text_color=UI_TEXT, border_width=0,
            command=lambda n=name: self.select(n))
        btn.pack(fill="x", padx=12, pady=4)
        self._btns[name] = (btn, icon)
        _NavTip(btn, f"{icon}  {name}", lambda: self._collapsed)

    def toggle(self):
        self._collapsed = not self._collapsed
        self._apply()
        self._save_state()

    def _apply(self):
        mini = self._collapsed
        self.sidebar.configure(width=self.W_MINI if mini else self.W_OPEN)
        self._toggle.configure(text="»" if mini else "«")
        self._brand1.configure(text="" if mini else "拼多多")
        self._brand2.configure(text="" if mini else "工作台")
        self._brand_line.pack_configure(padx=10 if mini else 16)
        for lab, title in self._group_labels:
            lab.configure(text="" if mini else title)
            lab.pack_configure(padx=0 if mini else 16,
                               pady=(8, 2) if mini else (14, 4))
        for name, (btn, icon) in self._btns.items():
            btn.configure(text=icon if mini else f"  {icon}  {name}",
                          anchor="center" if mini else "w",
                          font=F(18) if mini else F(13, True),
                          height=46 if mini else 50)
            btn.pack_configure(padx=6 if mini else 12, pady=3 if mini else 4)
        self.root.update_idletasks()

    def _load_state(self):
        try:
            self._collapsed = bool(json.loads(
                self.UI_STATE.read_text(encoding="utf-8")).get("sidebar_collapsed"))
        except Exception:
            self._collapsed = False
        if self._collapsed:
            self._apply()

    def _save_state(self):
        try:
            try:
                d = json.loads(self.UI_STATE.read_text(encoding="utf-8"))
            except Exception:
                d = {}
            d["sidebar_collapsed"] = self._collapsed
            self.UI_STATE.write_text(json.dumps(d, ensure_ascii=False, indent=2),
                                     encoding="utf-8")
        except Exception:
            pass

    def select(self, name):
        if name not in self._pages:
            return
        for n, pg in self._pages.items():
            (pg.lift if n == name else pg.lower)()
        for n, (btn, _icon) in self._btns.items():
            if n == name:
                btn.configure(fg_color=UI_BLUE, text_color="#ffffff",
                              hover_color=UI_BLUE_D)
            else:
                btn.configure(fg_color="transparent", text_color=UI_TEXT,
                              hover_color="#eaf2ff")
        self._active = name

    def tab(self, name):
        return self._pages[name]


class Workbench(ctk.CTk):
    def __init__(self):
        super().__init__()
        self.title("拼多多全链路工作台  v2.0  ·  全链路数据驱动")
        self.geometry("1320x860")
        self.minsize(1100, 720)
        self.configure(fg_color=UI_BG)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

        self.config = self._load_config()
        self.products = []
        self.orders = []
        self.terms_map = {}     # item_id -> [kept 推流词]
        self.drop_map = {}      # item_id -> [(term, score)]
        self._best_title = ""   # 当前商品的推荐标题(供『复制推荐标题』使用)
        self.cost_map_path = ""
        self.selected_item = None
        self._price_province = "非偏远(默认)"

        self._q = queue.Queue()
        self.busy = False
        self._busy_label = ""

        self._build_topbar()
        self._build_tabs()
        self._pump()
        self._restore_collected()

    # ---------- 配置持久化 ----------
    def _load_config(self):
        try:
            with open(CONFIG_PATH, encoding="utf-8") as f:
                return pricing.PricingConfig.from_dict(json.load(f))
        except Exception:
            return pricing.PricingConfig()

    def _save_config(self):
        try:
            with open(CONFIG_PATH, "w", encoding="utf-8") as f:
                json.dump(self.config.to_dict(), f, ensure_ascii=False, indent=2)
        except Exception:
            pass

    # ---------- 顶栏: 品牌行 + 参数卡片 ----------
    def _build_topbar(self):
        head = ctk.CTkFrame(self, fg_color=UI_BG, corner_radius=0)
        head.pack(fill="x")

        brand = ctk.CTkFrame(head, fg_color="transparent")
        brand.pack(fill="x", padx=18, pady=(12, 4))
        ctk.CTkLabel(brand, text="拼多多全链路工作台", font=F(19, True),
                     text_color=UI_TEXT).pack(side="left")
        ctk.CTkLabel(brand, text="  选品 · 优化 · 定价 · 上架 · 下架 · 发货 · 开票",
                     font=F(11), text_color=UI_MUTED).pack(side="left", padx=12)
        self.status = ctk.CTkLabel(brand, text="就绪", font=F(11),
                                   text_color=UI_BLUE, anchor="e")
        self.status.pack(side="right", padx=8)

        card = ctk.CTkFrame(head, fg_color=UI_CARD, corner_radius=12,
                            border_width=1, border_color=UI_BORDER)
        card.pack(fill="x", padx=16, pady=(2, 10))
        ctk.CTkLabel(card, text="全局定价参数", font=F(12, True),
                     text_color=UI_TEXT).pack(side="left", padx=(16, 8))
        self._mk_entry(card, "目标净利率%", str(int(self.config.target_net_margin * 100)), 8, self._on_cfg)
        self._mk_entry(card, "平台佣金率%", str(self.config.commission_rate * 100), 8, self._on_cfg)
        self._mk_entry(card, "支付费率%", str(self.config.payment_fee_rate * 100), 8, self._on_cfg)
        ctk.CTkFrame(card, width=1, height=38, fg_color=UI_BORDER).pack(side="left", padx=12)
        self._btn(card, "⚙ 运费/佣金", self._open_settings, "ghost", 120)
        self._btn(card, "保存参数", self._save_config, "green", 92)

    def _mk_entry(self, master, label, val, w, cb):
        f = ctk.CTkFrame(master, fg_color="transparent")
        f.pack(side="left", padx=8, pady=6)
        ctk.CTkLabel(f, text=label, font=F(11), text_color=UI_MUTED).pack(anchor="w")
        e = ctk.CTkEntry(f, width=w * 10, height=30, font=F(12), corner_radius=8,
                         border_width=1, border_color=UI_BORDER)
        e.insert(0, val)
        e.pack(pady=(3, 0))
        e.bind("<FocusOut>", cb)
        e.bind("<Return>", cb)
        setattr(self, f"_cfg_{label}", e)

    def _cfg_val(self, label):
        return getattr(self, f"_cfg_{label}").get().strip()

    def _on_cfg(self, ev=None):
        try:
            self.config.target_net_margin = float(self._cfg_val("目标净利率%")) / 100
            self.config.commission_rate = float(self._cfg_val("平台佣金率%")) / 100
            self.config.payment_fee_rate = float(self._cfg_val("支付费率%")) / 100
        except ValueError:
            self._set_status("参数格式错误, 已忽略")
            return
        self._save_config()
        self._set_status("全局参数已更新")

    # ---------- 导航(左侧竖向图标侧栏) ----------
    def _build_tabs(self):
        # 用侧栏式容器替换顶部横向 Tabview: 左侧分组图标导航 + 右侧堆叠内容区。
        # _TabHost.tab(name) 与原 CTkTabview.tab(name) 行为一致, 既有 _tab_* 无需改动。
        self.tabs = _TabHost(self)
        self._tab_select()
        self._tab_title()
        self._tab_price()
        self._tab_analysis()
        self._tab_listing()
        self._tab_delist()
        self._tab_ship()
        self._tab_invoice()

    # ---------- 统一布局组件 ----------
    def _btn(self, parent, text, command, tone="blue", width=110):
        """统一按钮: 按功能配色, 自动排布(不再逐条写 pack)。"""
        pal = {
            "blue":   (UI_BLUE,   UI_BLUE_D,  "#ffffff"),
            "purple": (UI_PURPLE, "#6d28d9", "#ffffff"),
            "green":  (UI_GREEN,  "#0b7a43", "#ffffff"),
            "orange": (UI_ORANGE, "#bd6a08", "#ffffff"),
            "red":    (UI_RED,    "#b73333", "#ffffff"),
            "ghost":  ("#eef3fb", "#dbe6f5", UI_TEXT),
        }.get(tone, (UI_BLUE, UI_BLUE_D, "#ffffff"))
        b = ctk.CTkButton(parent, text=text, width=width, height=32, font=F(12),
                          corner_radius=8, fg_color=pal[0], hover_color=pal[1],
                          text_color=pal[2], command=command)
        b.pack(side="left", padx=5, pady=7)
        return b

    def _section(self, parent, title, subtitle="", tone="blue"):
        """功能分区: 彩色标题条 + 白色操作/内容容器。返回容器。"""
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="x", padx=14, pady=(10, 2))
        h = ctk.CTkFrame(wrap, fg_color="transparent")
        h.pack(fill="x")
        ctk.CTkFrame(h, width=4, height=18, fg_color=TONE.get(tone, UI_BLUE),
                     corner_radius=2).pack(side="left", padx=(0, 9))
        ctk.CTkLabel(h, text=title, font=F(13, True), text_color=UI_TEXT).pack(side="left")
        if subtitle:
            ctk.CTkLabel(h, text=subtitle, font=F(11), text_color=UI_MUTED).pack(side="left", padx=12)
        bar = ctk.CTkFrame(wrap, fg_color=UI_CARD, corner_radius=10,
                           border_width=1, border_color=UI_BORDER)
        bar.pack(fill="x", pady=(7, 0))
        return bar

    def _section_label(self, parent, title, subtitle="", tone="green"):
        """只要标题条(表格/内容区上方), 不创建容器。"""
        wrap = ctk.CTkFrame(parent, fg_color="transparent")
        wrap.pack(fill="x", padx=14, pady=(10, 3))
        h = ctk.CTkFrame(wrap, fg_color="transparent")
        h.pack(fill="x")
        ctk.CTkFrame(h, width=4, height=18, fg_color=TONE.get(tone, UI_BLUE),
                     corner_radius=2).pack(side="left", padx=(0, 9))
        ctk.CTkLabel(h, text=title, font=F(13, True), text_color=UI_TEXT).pack(side="left")
        if subtitle:
            ctk.CTkLabel(h, text=subtitle, font=F(11), text_color=UI_MUTED).pack(side="left", padx=12)
        return wrap

    def _hint(self, parent, text, tone="info"):
        """说明/警示卡片(替代原来挤在角落的小灰字)。"""
        pal = {"info": (UI_SOFT, UI_BLUE), "warn": (UI_SOFT_WARN, "#b45309"),
               "danger": (UI_SOFT_RED, UI_RED), "ok": (UI_SOFT_GRN, UI_GREEN)}
        bg, fg = pal.get(tone, pal["info"])
        box = ctk.CTkFrame(parent, fg_color=bg, corner_radius=9)
        box.pack(fill="x", padx=14, pady=(4, 2))
        ctk.CTkLabel(box, text=text, font=F(11), text_color=fg, wraplength=1200,
                     justify="left", anchor="w").pack(fill="x", padx=14, pady=9)
        return box

    def _info_card(self, parent, initial="", min_h=90):
        """底部详情卡片: 占满剩余空间, 让表格不再『一头独大』。"""
        card = ctk.CTkFrame(parent, fg_color=UI_CARD, corner_radius=10,
                            border_width=1, border_color=UI_BORDER)
        card.pack(fill="both", expand=True, padx=14, pady=(6, 14))
        lab = ctk.CTkLabel(card, text=initial, font=F(12), text_color=UI_TEXT,
                           justify="left", anchor="nw", wraplength=1180)
        lab.pack(fill="both", expand=True, padx=16, pady=13)
        return lab

    # ===== Tab 1 选品评分 =====
    def _tab_select(self):
        f = self.tabs.tab("选品评分")
        top = self._section(f, "① 货源导入与采集", "导入 1688 货源 → 抓推流词 → 自动评分排序", "blue")
        self._btn(top, "导入1688缓存", lambda: self._run_bg(self._load_cache, "导入1688缓存"), "blue", 124)
        self._btn(top, "导入JSON", self._load_json, "ghost", 92)
        self._btn(top, "导入商品CSV", self._load_prod_csv, "ghost", 110)
        self._btn(top, "采集1688链接", self._open_collector, "purple", 124)
        self._btn(top, "抓推流词并优化", self._fetch_suggest, "purple", 134)
        self._btn(top, "刷新评分", self._render_select, "green", 96)
        self._hint(f, "提示：导入后自动评分；「抓推流词并优化」会抓取拼多多下拉词并改进标题。"
                      "表格可上下/左右滚动，单击一行查看该商品明细。", "info")
        self._section_label(f, "② 选品评分结果", "等级颜色区分推荐度，单击行查看详情", "green")
        cols = ["等级", "综合分", "商品ID", "标题", "成本", "建议售价", "单件净利", "净利率%", "类目"]
        self.t_select = Table(f, cols, widths=[76, 68, 104, 330, 68, 84, 84, 70, 82], height=13)
        self.t_select.frame.pack(fill="both", expand=False, padx=14, pady=(2, 4))
        self.t_select.tree.bind("<<TreeviewSelect>>", self._on_select_row)
        self.select_detail = self._info_card(f, "👆 在上面表格单击任一行，这里会显示该商品的完整评分依据与明细。")

    def _load_cache(self):
        self.products = loader.load_from_cyj_cache()
        self._emit("loaded", f"缓存导入 {len(self.products)} 个")

    def _load_json(self):
        p = filedialog.askopenfilename(filetypes=[("JSON", "*.json")])
        if p:
            self.products = loader.load_from_json_file(p)
            self._emit("loaded", f"JSON导入 {len(self.products)} 个")

    def _load_prod_csv(self):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if p:
            self.products = loader.load_from_csv(p)
            self._emit("loaded", f"CSV导入 {len(self.products)} 个")

    def _restore_collected(self):
        """启动时自动恢复上次『采集1688链接』的结果(collected_1688.json),
        避免重开工作台后已采集的商品丢失; 没有该文件则提示先导入/采集。"""
        try:
            p = (ROOT / "data" / "collected_1688.json").as_posix()
            if os.path.exists(p):
                prods = loader.load_from_json_file(p)
                if prods:
                    self.products = prods
                    # 旧版采集器只存了主图+规格名, 缺 SKU 组合/详情图; 明确提示升级,
                    # 避免用户拿旧数据上架却发现「没搬全」。
                    try:
                        raw = json.load(open(p, encoding="utf-8"))
                        stale = sum(1 for d in raw if isinstance(d, dict)
                                    and (not d.get("sku_combos") or not d.get("detail_images")))
                    except Exception:
                        stale = 0
                    msg = f"已自动恢复上次采集 {len(prods)} 个(来自 data/collected_1688.json)"
                    if stale:
                        msg += (f" ｜ ⚠ {stale} 条为旧版采集(缺SKU组合/详情图), "
                                "请到『上架导出』点『升级旧采集数据』重新采集")
                    self._emit("loaded", msg)
                    return
        except Exception:
            pass
        self._set_status("就绪。请先到『选品评分』导入 1688 货源, 或点『采集1688链接』在线抓取。")

    def _render_select(self):
        self.t_select.clear()
        for i, p in enumerate(self.products):
            s = selector.score_product(p, self.config)
            p.score = s
            g = s["grade"]
            self.t_select.add([
                g, s["total"], p.item_id, p.title[:40], s["cost"],
                s["suggest_price"], s["net_profit"], s["net_margin"], s["category"],
            ], tag=("alt" if i % 2 else ""))
            # 等级色标
        self._color_grades(self.t_select)

    def _color_grades(self, tbl):
        for it in tbl.tree.get_children():
            g = tbl.tree.item(it)["values"][0]
            c = GRADE_COLORS.get(g, "#666")
            tbl.tree.tag_configure(g, foreground=c)

    def _on_select_row(self, ev):
        v = self.t_select.selected()
        if not v:
            return
        item_id = v[2]
        p = next((x for x in self.products if x.item_id == item_id), None)
        if not p:
            return
        s = p.score or selector.score_product(p, self.config)
        detail = (f"【{s['grade']}】综合{s['total']}  |  进货¥{s['cost_price']} + 进货运费¥{s['inbound_shipping']}"
                  f" + 发货¥{s['outbound_shipping']}  →  建议售价 ¥{s['suggest_price']}\n"
                  f"单件净利 ¥{s['net_profit']} ({s['net_margin']}%)  |  佣金率 {s['commission_rate']*100:.1f}%  |  需求信号 {s['demand_level']}\n"
                  f"建议: {' ; '.join(s['notes']) or '无明显风险'}")
        self.select_detail.configure(text=detail)

    # ===== Tab 2 标题优化 =====
    def _tab_title(self):
        f = self.tabs.tab("标题优化")
        top = self._section(f, "① 选择商品与操作", "推流词会参与标题重组，可随时微调", "blue")
        ctk.CTkLabel(top, text="选择商品", font=F(12, True), text_color=UI_TEXT).pack(side="left", padx=(14, 4))
        self.title_picker = ctk.CTkComboBox(top, width=360, height=32, font=F(12),
                                            values=[], command=self._on_title_pick)
        self.title_picker.pack(side="left", padx=6, pady=7)
        self._btn(top, "重新优化", self._reopt_title, "ghost", 96)
        self._btn(top, "复制推荐标题", self._copy_best_title, "green", 122)

        # 就近进度区(不用去顶部状态栏盲等)
        prog_f = ctk.CTkFrame(f, fg_color=UI_CARD, corner_radius=10,
                              border_width=1, border_color=UI_BORDER)
        prog_f.pack(fill="x", padx=14, pady=(6, 2))
        self.title_prog = ctk.CTkProgressBar(prog_f, height=12, corner_radius=6,
                                             fg_color="#e7eef9", progress_color=UI_BLUE)
        self.title_prog.set(0)
        self.title_prog.pack(side="left", fill="x", expand=True, padx=(14, 10), pady=11)
        self.title_prog_label = ctk.CTkLabel(
            prog_f, text="空闲 · 在「选品评分」点『抓推流词并优化』后可在此微调",
            font=F(11), text_color=UI_MUTED, anchor="w")
        self.title_prog_label.pack(side="right", padx=12)

        # 主体: 左推流词 / 右标题结果(两张独立卡片, 分区清晰)
        body = ctk.CTkFrame(f, fg_color="transparent")
        body.pack(fill="both", expand=True, padx=14, pady=(8, 14))
        body.grid_columnconfigure(0, weight=1, uniform="cols")
        body.grid_columnconfigure(1, weight=1, uniform="cols")
        body.grid_rowconfigure(0, weight=1)

        left = ctk.CTkFrame(body, fg_color=UI_CARD, corner_radius=10,
                            border_width=1, border_color=UI_BORDER)
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 7))
        ctk.CTkLabel(left, text="② 推流词管理", font=F(13, True),
                     text_color=UI_TEXT).pack(anchor="w", padx=14, pady=(12, 2))
        ctk.CTkLabel(left, text="✅ 保留（双击移除，会进入标题）", font=F(12, True),
                     text_color=UI_GREEN).pack(anchor="w", padx=14, pady=(6, 2))
        self.t_kept = Table(left, ["推流词"], widths=[300], height=7)
        self.t_kept.frame.pack(fill="x", padx=10, pady=2)
        ctk.CTkLabel(left, text="🗑 已剔除（双击回捞）", font=F(12, True),
                     text_color=UI_RED).pack(anchor="w", padx=14, pady=(12, 2))
        self.t_dropped = Table(left, ["推流词", "相关性分"], widths=[220, 100], height=7)
        self.t_dropped.frame.pack(fill="x", padx=10, pady=2)

        right = ctk.CTkFrame(body, fg_color=UI_CARD, corner_radius=10,
                             border_width=1, border_color=UI_BORDER)
        right.grid(row=0, column=1, sticky="nsew", padx=(7, 0))
        ctk.CTkLabel(right, text="③ 标题优化结果", font=F(13, True),
                     text_color=UI_TEXT).pack(anchor="w", padx=14, pady=(12, 4))
        rec = ctk.CTkFrame(right, fg_color=UI_SOFT, corner_radius=10,
                           border_color=UI_BLUE, border_width=1)
        rec.pack(fill="x", padx=12, pady=4)
        ctk.CTkLabel(rec, text="🏆 优先推荐（综合分最高，建议直接上架）",
                     font=F(12, True), text_color=UI_BLUE_D).pack(anchor="w", padx=12, pady=(10, 3))
        self.title_rec_title = ctk.CTkLabel(rec, text="（请选择商品）", font=F(16, True),
                                            wraplength=520, justify="left", anchor="w",
                                            text_color=UI_TEXT)
        self.title_rec_title.pack(anchor="w", padx=12, pady=(0, 4))
        self.title_rec_meta = ctk.CTkLabel(rec, text="", font=F(11), text_color=UI_MUTED,
                                           wraplength=520, justify="left", anchor="w")
        self.title_rec_meta.pack(anchor="w", padx=12, pady=(0, 8))
        ctk.CTkButton(rec, text="复制推荐标题", width=124, height=30, font=F(12),
                      corner_radius=8, fg_color=UI_GREEN, hover_color="#0b7a43",
                      command=self._copy_best_title).pack(anchor="e", padx=12, pady=(0, 10))

        ctk.CTkLabel(right, text="📋 备用标题（双击可提为优先推荐）", font=F(12, True),
                     text_color=UI_TEXT).pack(anchor="w", padx=14, pady=(10, 3))
        self.title_backup = ctk.CTkScrollableFrame(right, height=170, fg_color="#f8fbff",
                                                   corner_radius=8)
        self.title_backup.pack(fill="both", expand=True, padx=12, pady=(2, 6))
        self.title_hint = ctk.CTkLabel(right, text="", font=F(11), text_color=UI_RED,
                                       wraplength=520, justify="left")
        self.title_hint.pack(anchor="w", padx=14, pady=(0, 10))

        self.t_kept.tree.bind("<Double-1>", lambda e: self._move_term("kept"))
        self.t_dropped.tree.bind("<Double-1>", lambda e: self._move_term("dropped"))

    def _rebuild_title_picker(self):
        vals = [f"{p.item_id} | {p.title[:24]}" for p in self.products]
        self.title_picker.configure(values=vals)
        if vals:
            self.title_picker.set(vals[0])
            self._on_title_pick(vals[0])

    def _on_title_pick(self, key):
        if not key:
            return
        item_id = key.split("|")[0].strip()
        p = next((x for x in self.products if x.item_id == item_id), None)
        if not p:
            return
        self.selected_item = p
        terms = self.terms_map.get(item_id, [])
        dropped = self.drop_map.get(item_id, [])
        self.t_kept.clear()
        for t in terms:
            self.t_kept.add([t])
        self.t_dropped.clear()
        for t, sc in dropped:
            self.t_dropped.add([t, sc])
        self._render_title_preview()

    def _move_term(self, src):
        if src == "kept":
            v = self.t_kept.selected()
            if not v:
                return
            self.t_kept.tree.delete(self.t_kept.tree.selection()[0])
            self.t_dropped.add([v[0], "回捞"])
        else:
            v = self.t_dropped.selected()
            if not v:
                return
            self.t_dropped.tree.delete(self.t_dropped.tree.selection()[0])
            self.t_kept.add([v[0]])
        self._render_title_preview()

    def _render_title_preview(self, prefer=None):
        p = self.selected_item
        if not p:
            return
        kept = [r[0] for r in self.t_kept.rows()]
        self.terms_map[p.item_id] = kept
        dropped = [r[0] for r in self.t_dropped.rows()]
        self.drop_map[p.item_id] = [(t, "") for t in dropped]
        # 清空旧备用卡片
        for w in list(self.title_backup.winfo_children()):
            w.destroy()
        try:
            import title_opt
            r = title_opt.optimize(p, extra_terms=kept)
            cands = r.get("candidates", []) or []
            if prefer and cands:
                cands.sort(key=lambda x: (x["title"] != prefer, -x["score"]))
            if not cands:
                self._best_title = p.title
                self.title_rec_title.configure(text=p.title)
                self.title_rec_meta.configure(text="（无候选，已用原标题）")
                self.title_hint.configure(text="")
                return
            best = cands[0]
            self._best_title = best["title"]
            self.title_rec_title.configure(text=best["title"])
            bd = best.get("breakdown", {}) or {}
            self.title_rec_meta.configure(
                text=f"综合评分 {best['score']}　|　" + "　".join(f"{k}:{v}" for k, v in bd.items()))
            # 备用: 其余候选, 每条一张小卡片
            for c in cands[1:]:
                self._add_backup_card(c)
            # 提示 / 建议
            advice = list(r.get("advice") or [])
            if not kept and not dropped:
                advice = [
                    "⚠ 当前未抓取推流词，以上仅按原标题重组，没有注入拼多多爆款下拉词。",
                    "   若要真正优化：回『选品评分』页点『抓推流词并优化』，跑完再来本页微调保留/剔除词。",
                ] + advice
            self.title_hint.configure(text=("\n".join(advice)) if advice else "")
        except Exception as e:
            self._best_title = ""
            self.title_rec_title.configure(text="优化失败")
            self.title_rec_meta.configure(text=str(e))
            self.title_hint.configure(text="")

    def _add_backup_card(self, c):
        card = ctk.CTkFrame(self.title_backup, fg_color="#ffffff", corner_radius=9,
                            border_color=UI_BORDER, border_width=1)
        card.pack(fill="x", padx=5, pady=4)
        head = ctk.CTkLabel(card, text=f"[{c['score']}] {c['label']}", font=F(11, True),
                            text_color=UI_BLUE, width=140, anchor="w")
        head.pack(side="left", padx=10, pady=9)
        body = ctk.CTkLabel(card, text=c["title"], font=F(12), text_color=UI_TEXT,
                            wraplength=340, justify="left", anchor="w")
        body.pack(side="left", fill="x", expand=True, padx=6, pady=9)
        btn = ctk.CTkButton(card, text="复制", width=58, height=28, font=F(11),
                            corner_radius=7, fg_color=UI_GREEN, hover_color="#0b7a43",
                            command=lambda t=c["title"]: self._copy_text(t))
        btn.pack(side="right", padx=8, pady=8)
        # 双击整卡 / 标题 / 头部 均可设为优先推荐
        for wgt in (card, head, body):
            wgt.bind("<Double-1>", lambda e, t=c["title"]: self._promote(t))

    def _promote(self, title):
        if self.selected_item:
            self._render_title_preview(prefer=title)
            self._set_status("已将该标题设为优先推荐")

    def _copy_text(self, txt):
        if txt:
            try:
                self.clipboard_clear()
                self.clipboard_append(txt)
                self._set_status("已复制标题到剪贴板")
            except Exception:
                self._set_status("复制失败(剪贴板不可用)")

    def _copy_best_title(self):
        if not getattr(self, "_best_title", ""):
            self._render_title_preview()
        txt = getattr(self, "_best_title", "") or (self.selected_item.title if self.selected_item else "")
        if txt:
            try:
                self.clipboard_clear()
                self.clipboard_append(txt)
                self._set_status("已复制推荐标题到剪贴板")
            except Exception:
                self._set_status("复制失败(剪贴板不可用)")
        else:
            self._set_status("请先选择一个商品")

    def _reopt_title(self):
        if self.selected_item:
            self._render_title_preview()
            self._set_status("已按当前保留词重算标题")

    # ===== Tab 3 定价利润(核心) =====
    def _tab_price(self):
        f = self.tabs.tab("定价利润")
        self._hint(f, "逐商品利润拆解：售价 − 进货 − 进货运费 − 发货运费 − 佣金 − 支付费 = 净利。"
                      "单击表格任一行，可在下方「单品精算」里改参数即时重算。", "info")

        region = self._section(f, "① 运费区域", "切换偏远地区，观察运费对利润的冲击", "orange")
        ctk.CTkLabel(region, text="运费区域", font=F(12), text_color=UI_TEXT).pack(side="left", padx=(14, 4))
        self._price_region = ctk.CTkComboBox(region, width=220, height=32, font=F(12),
                                             values=["非偏远(默认)", "偏远(新疆/西藏/内蒙等)"],
                                             command=self._on_price_region)
        self._price_region.pack(side="left", padx=6, pady=7)
        self._price_region.set(self._price_province)
        self._btn(region, "查1688地区运费", self._probe_freight, "purple", 140)

        self._section_label(f, "② 逐商品利润明细", "表格可滚动；单击行载入下方精算区", "green")
        self.t_price = Table(f, ["商品ID", "标题", "进货", "进货运费", "发货运费", "佣金%",
                                 "建议售价", "单件净利", "净利率%"],
                             widths=[100, 260, 64, 80, 80, 64, 84, 84, 70], height=11)
        self.t_price.frame.pack(fill="both", expand=False, padx=14, pady=(2, 4))
        self.t_price.tree.bind("<<TreeviewSelect>>", self._on_price_row)

        edit = self._section(f, "③ 单品精算", "修改任一参数即时重算（失焦或回车生效）", "blue")
        self._pe = {}
        for i, (lab, key) in enumerate([("进货价¥", "cost"), ("进货运费¥", "inb"),
                                        ("发货运费¥", "out"), ("佣金率%", "cr"),
                                        ("目标净利率%", "tm"), ("建议售价¥", "sp")]):
            ctk.CTkLabel(edit, text=lab, font=F(11), text_color=UI_MUTED).grid(row=0, column=i, padx=7, pady=(9, 0))
            e = ctk.CTkEntry(edit, width=86, height=30, font=F(12), corner_radius=8)
            e.grid(row=1, column=i, padx=7, pady=(2, 4))
            e.bind("<FocusOut>", self._recalc_price)
            e.bind("<Return>", self._recalc_price)
            self._pe[key] = e
        for j, (lab, key) in enumerate([("重量kg", "wt"), ("其他费(包装)¥", "oth")]):
            ctk.CTkLabel(edit, text=lab, font=F(11), text_color=UI_MUTED).grid(row=2, column=j, padx=7)
            e = ctk.CTkEntry(edit, width=86, height=30, font=F(12), corner_radius=8)
            e.grid(row=3, column=j, padx=7, pady=(2, 9))
            e.bind("<FocusOut>", self._recalc_price)
            e.bind("<Return>", self._recalc_price)
            self._pe[key] = e
        b = ctk.CTkButton(edit, text="应用建议价", width=100, height=32, font=F(12),
                          corner_radius=8, fg_color=UI_GREEN, hover_color="#0b7a43",
                          command=self._apply_suggest)
        b.grid(row=2, column=4, rowspan=2, padx=12, pady=6, sticky="w")
        self.price_detail = self._info_card(f, "👆 单击上方表格任一行，这里显示完整的利润拆解与建议售价依据。")

    def _probe_freight(self):
        """探测 1688 各商品按收货地区计算的运费, 生成 JSON/CSV/HTML 报告。"""
        import os
        import threading
        import freight_1688

        dlg = ctk.CTkToplevel(self)
        dlg.configure(fg_color=UI_BG)
        dlg.title("1688 分地区运费规则")
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(760, max(660, sw - 140))
        h = min(500, max(400, int((sh - 80) / 1.25)))
        dlg.geometry(f"{w}x{h}+{max(0, (sw - w) // 2)}+16")
        dlg.transient(self)
        dlg.grab_set()

        # 按钮区最先固定到底部, 避免被日志框挤到屏幕外(1107 实测教训)
        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent", height=52)
        btn_frame.pack(side="bottom", fill="x", padx=16, pady=(0, 12))
        btn_frame.pack_propagate(False)
        btn_close = ctk.CTkButton(btn_frame, text="关闭", width=100, height=36,
                                  font=F(12), corner_radius=8, fg_color="#e7eef9",
                                  hover_color="#d6e3f6", text_color=UI_TEXT)
        btn_close.pack(side="right", padx=6)
        btn_open = ctk.CTkButton(btn_frame, text="打开报告", width=110, height=36,
                                 font=F(12), corner_radius=8, state="disabled",
                                 fg_color=UI_GREEN, hover_color="#0b7a43")
        btn_open.pack(side="right", padx=6)
        btn_start = ctk.CTkButton(btn_frame, text="开始查询", width=120, height=36,
                                  font=F(13, True), corner_radius=8,
                                  fg_color=UI_BLUE, hover_color=UI_BLUE_D)
        btn_start.pack(side="right", padx=6)

        ctk.CTkLabel(dlg, text="按收货地址查询 1688 官方运费接口 · 会弹出浏览器，请勿手动关闭",
                     font=F(13, True), text_color=UI_TEXT).pack(anchor="w", padx=18, pady=(16, 2))
        status = ctk.CTkLabel(dlg, text="点「开始查询」后运行；只记录省市与运费，不保存收货人信息。",
                              font=F(11), text_color=UI_MUTED, anchor="w")
        status.pack(fill="x", padx=18, pady=(0, 8))
        txt = ctk.CTkTextbox(dlg, height=140, font=("Consolas", 11),
                             fg_color="#ffffff", corner_radius=10,
                             border_width=1, border_color=UI_BORDER)
        txt.pack(fill="both", expand=True, padx=18, pady=(0, 10))
        txt.insert("0.0", "（等待开始…）\n")

        state = {"alive": True, "started": False}

        def log(msg):
            if not state["alive"]:
                return
            self._safe_ui(lambda: (txt.insert("end", str(msg) + "\n"), txt.see("end")))

        def set_status(s):
            if state["alive"]:
                self._safe_ui(lambda: status.configure(text=s))

        def open_report():
            p = str(freight_1688.OUT_HTML)
            if os.path.exists(p):
                try:
                    os.startfile(p)   # noqa: S606 - Windows 打开本地 HTML 报告
                except Exception:
                    import webbrowser
                    webbrowser.open("file:///" + p.replace("\\", "/"))

        def run():
            try:
                items = freight_1688._load_items()
                if not items:
                    raise RuntimeError("没有已采集的 1688 商品，请先采集链接")
                res = freight_1688.probe(items, max_addr=30, progress=log)
                s = freight_1688.export(res)
                def done():
                    if not state["alive"]:
                        return
                    btn_start.configure(state="normal", text="重新查询")
                    btn_open.configure(state="normal")
                    status.configure(text=(
                        f"✅ 完成：{s['items']} 个商品 / {s['records']} 条地区记录 / "
                        f"{s['provinces']} 个省级行政区；"
                        f"运费 ¥{s['fee_min']:g}~¥{s['fee_max']:g}，中位 ¥{s['fee_median']:g}"
                        if s["fee_min"] is not None else "完成，但未取到运费数据"))
                self._safe_ui(done)
            except Exception as e:
                def fail():
                    if not state["alive"]:
                        return
                    btn_start.configure(state="normal", text="重新查询")
                    status.configure(text="⚠ 查询失败：" + str(e)[:110])
                    txt.insert("end", "错误：" + repr(e) + "\n")
                self._safe_ui(fail)

        def do_start():
            if state["started"] or not state["alive"]:
                return
            state["started"] = True
            btn_start.configure(state="disabled", text="查询中…")
            btn_open.configure(state="disabled")
            set_status("正在启动浏览器并逐个切换收货地址，请稍候…")
            txt.insert("end", "▶ 已开始查询\n")
            threading.Thread(target=run, daemon=True).start()

        def do_close():
            state["alive"] = False
            dlg.destroy()

        btn_start.configure(command=do_start)
        btn_open.configure(command=open_report)
        btn_close.configure(command=do_close)
        dlg.protocol("WM_DELETE_WINDOW", do_close)

    def _render_price(self):
        self.t_price.clear()
        for i, p in enumerate(self.products):
            pb = pricing.suggest_price(p.price, p.express_fee, 0.0, self.config, category=p.category,
                                      weight_kg=p.weight_kg, province=self._price_province)
            p.pricing = pb.to_dict()
            cr = pricing.commission_rate_for(p.category)
            self.t_price.add([p.item_id, p.title[:32], p.price, p.express_fee,
                              pb.outbound_shipping, round(cr * 100, 1),
                              pb.revenue, pb.net_profit, pb.net_margin],
                             tag=("alt" if i % 2 else ""))

    def _on_price_row(self, ev):
        v = self.t_price.selected()
        if not v:
            return
        item_id = v[0]
        p = next((x for x in self.products if x.item_id == item_id), None)
        if not p:
            return
        pb = pricing.suggest_price(p.price, p.express_fee, 0.0, self.config, category=p.category,
                                  weight_kg=p.weight_kg, province=self._price_province)
        self._price_cur = p
        self._populate_price_edit(p, pb)

    def _populate_price_edit(self, p, pb):
        cr = pricing.commission_rate_for(p.category)
        self._pe["cost"].delete(0, "end"); self._pe["cost"].insert(0, p.price)
        self._pe["inb"].delete(0, "end"); self._pe["inb"].insert(0, p.express_fee)
        self._pe["out"].delete(0, "end"); self._pe["out"].insert(0, pb.outbound_shipping)
        self._pe["cr"].delete(0, "end"); self._pe["cr"].insert(0, round(cr * 100, 1))
        self._pe["tm"].delete(0, "end"); self._pe["tm"].insert(0, int(self.config.target_net_margin * 100))
        self._pe["sp"].delete(0, "end"); self._pe["sp"].insert(0, pb.revenue)
        self._pe["wt"].delete(0, "end"); self._pe["wt"].insert(0, p.weight_kg)
        self._pe["oth"].delete(0, "end"); self._pe["oth"].insert(0, self.config.default_other_fee)
        self._show_price_breakdown(pb, p)

    def _on_price_region(self, choice):
        self._price_province = choice
        if not self.products:
            return
        self._render_price()
        p = getattr(self, "_price_cur", None)
        if p:
            pb = pricing.suggest_price(p.price, p.express_fee, 0.0, self.config, category=p.category,
                                      weight_kg=p.weight_kg, province=self._price_province)
            self._populate_price_edit(p, pb)

    def _recalc_price(self, ev=None):
        p = getattr(self, "_price_cur", None)
        if not p:
            return
        try:
            cost = float(self._pe["cost"].get() or 0)
            inb = float(self._pe["inb"].get() or 0)
            out = float(self._pe["out"].get() or 0)
            cr = float(self._pe["cr"].get() or 0) / 100
            tm = float(self._pe["tm"].get() or 0) / 100
            sp = float(self._pe["sp"].get() or 0)
            wt = float(self._pe["wt"].get() or 0)
            oth = float(self._pe["oth"].get() or 0)
        except ValueError:
            return
        # 发货运费: 用户在编辑区清空/置0时, 按"重量×区域"模板重估
        if out <= 0:
            out = self.config.shipping.estimate(wt, self._price_province)
        # 若用户改了建议售价 → 复盘利润; 否则用目标净利率反算
        if ev and getattr(ev, "widget", None) is self._pe["sp"] and sp > 0:
            pb = pricing.compute_profit(cost, inb, sp, out, cr, self.config.payment_fee_rate, other_fee=oth)
        else:
            sp = pricing.price_for_target(cost, inb, out, cr, self.config.payment_fee_rate,
                                         other_fee=oth, target_margin=tm)
            self._pe["sp"].delete(0, "end"); self._pe["sp"].insert(0, sp)
            pb = pricing.compute_profit(cost, inb, sp, out, cr, self.config.payment_fee_rate, other_fee=oth)
        p.pricing = pb.to_dict()
        self._show_price_breakdown(pb, p)
        self._render_price()

    def _apply_suggest(self):
        if getattr(self, "_price_cur", None):
            self._recalc_price()

    def _show_price_breakdown(self, pb, p):
        self.price_detail.configure(text=(
            f"售价 ¥{pb.revenue} 拆解:  进货 ¥{pb.cost_price}  −  进货运费 ¥{pb.inbound_shipping}"
            f"  −  发货运费 ¥{pb.outbound_shipping}  −  佣金 ¥{pb.commission}  −  支付费 ¥{pb.payment_fee}\n"
            f"= 单件净利 ¥{pb.net_profit}  (净利率 {pb.net_margin}%)"
        ))

    # ===== Tab 4 店铺分析 =====
    def _tab_analysis(self):
        f = self.tabs.tab("店铺分析")
        top = self._section(f, "① 数据导入与成本关联", "订单/结算 CSV + 1688 成本映射 → 真实净利", "blue")
        self._btn(top, "导入订单/结算CSV", self._load_orders, "blue", 150)
        self._btn(top, "导入成本映射CSV", self._load_costmap, "ghost", 142)
        self._btn(top, "关联1688成本", self._link_orders, "green", 124)

        self._section_label(f, "② 核心经营指标", "关联成本后自动刷新", "green")
        kpi = ctk.CTkFrame(f, fg_color=UI_CARD, corner_radius=10,
                           border_width=1, border_color=UI_BORDER)
        kpi.pack(fill="x", padx=14, pady=(2, 4))
        self._kpi_labels = {}
        keys = ["订单数", "件数", "成交额", "总成本", "发货运费", "净利", "净利率%", "成本关联率"]
        for i, k in enumerate(keys):
            kpi.grid_columnconfigure(i, weight=1)
            cell = ctk.CTkFrame(kpi, fg_color=UI_SOFT, corner_radius=8)
            cell.grid(row=0, column=i, padx=6, pady=10, sticky="nsew")
            ctk.CTkLabel(cell, text=k, font=F(11), text_color=UI_MUTED).pack(pady=(9, 0))
            lab = ctk.CTkLabel(cell, text="—", font=F(16, True), text_color=UI_BLUE)
            lab.pack(pady=(1, 9))
            self._kpi_labels[k] = lab

        self._section_label(f, "③ 分类目 / 按日期拆解", "两张表可分别滚动", "purple")
        nb = ctk.CTkFrame(f, fg_color="transparent")
        nb.pack(fill="x", padx=14, pady=(2, 4))
        self.t_cat = Table(nb, ["类目", "订单", "件数", "成交额", "成本", "发货运费", "净利", "净利率%"],
                           widths=[92, 58, 58, 92, 82, 82, 92, 70], height=10)
        self.t_cat.frame.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self.t_date = Table(nb, ["日期", "订单", "件数", "成交额", "净利", "净利率%"],
                            widths=[110, 58, 58, 92, 92, 70], height=10)
        self.t_date.frame.pack(side="left", fill="both", expand=True, padx=(6, 0))

    def _load_orders(self):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if p:
            self.orders = orders_mod.parse_orders(p)
            self._emit("orders", f"订单导入 {len(self.orders)} 条")

    def _load_costmap(self):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if p:
            self.cost_map_path = p
            self._emit("info", f"成本映射: {p}")

    def _link_orders(self):
        if not self.orders:
            self._set_status("请先导入订单 CSV")
            return
        cost_map = orders_mod.load_cost_map(self.cost_map_path) if self.cost_map_path else None
        self.orders = orders_mod.enrich_orders(self.orders, self.products, self.config, cost_map)
        self._render_analysis()

    def _render_analysis(self):
        k = analysis.store_kpi(self.orders)
        self._kpi_labels["订单数"].configure(text=k["orders"])
        self._kpi_labels["件数"].configure(text=k["units"])
        self._kpi_labels["成交额"].configure(text=k["revenue"])
        self._kpi_labels["总成本"].configure(text=k["cost"])
        self._kpi_labels["发货运费"].configure(text=k["shipping_out"])
        self._kpi_labels["净利"].configure(text=k["net_profit"])
        self._kpi_labels["净利率%"].configure(text=k["net_margin"])
        self._kpi_labels["成本关联率"].configure(text=f"{k['linked_rate']}%")
        self.t_cat.clear()
        for r in analysis.aggregate(self.orders, "category"):
            self.t_cat.add([r.key, r.orders, r.units, r.revenue, r.cost, r.shipping_out, r.net_profit, r.net_margin])
        self.t_date.clear()
        for r in analysis.aggregate(self.orders, "date"):
            self.t_date.add([r.key, r.orders, r.units, r.revenue, r.net_profit, r.net_margin])
        self._set_status("店铺分析已刷新")

    # ===== Tab 5 上架导出 =====
    def _tab_listing(self):
        f = self.tabs.tab("上架导出")
        g1 = self._section(f, "① 生成上架清单", "优化标题 + 每款 SKU 按自身进价单独反算售价", "blue")
        self._btn(g1, "采集1688新链接", lambda: self._open_collector(auto_listing=True), "purple", 132)
        self._btn(g1, "升级旧采集数据", self._upgrade_stale_collected, "ghost", 132)
        self._btn(g1, "生成上架清单", self._gen_listing, "blue", 130)
        self._btn(g1, "导出CSV", self._export_listing, "ghost", 96)

        g2 = self._section(f, "② 发布到拼多多", "按上架清单逐条填写商家后台发布表单", "purple")
        self._btn(g2, "发布配置", self._open_publish_config_dialog, "purple", 100)
        self._btn(g2, "一键发布到拼多多", self._open_publish_dialog, "blue", 154)

        self._hint(f, "发布时会填写：标题 / 轮播主图 / 详情图 / 规格价与库存 / 运费模板；"
                      "双击表格行可查看该商品的数据概览。", "info")
        self._section_label(f, "③ 上架候选清单", "末列显示该商品已采集到的资料概况", "green")
        self.listing_path = ""
        self.t_listing = Table(
            f, ["商品ID", "优化标题", "进货", "进货运费", "发货", "建议售价", "净利", "净利率%", "资料概况"],
            widths=[98, 280, 64, 82, 64, 84, 74, 70, 210], height=12)
        self.t_listing.frame.pack(fill="both", expand=False, padx=14, pady=(2, 12))
        self.t_listing.tree.bind("<Double-1>", lambda e: self._show_listing_detail())
        self._listings = []

    def _gen_listing(self):
        if not self.products:
            self._set_status("请先导入 1688 货源")
            return
        self._listings = listing.build_listings(self.products, self.config, self.terms_map)
        self.t_listing.clear()
        for i, c in enumerate(self._listings):
            brief = (f"主图{len(c.images or [])} 详情{len(c.detail_images or [])} "
                     f"SKU{len(c.sku_combos or [])} 属性{len(c.attributes or {})}")
            self.t_listing.add([c.item_id, c.title[:40], c.cost_price, c.inbound_shipping,
                                c.outbound_shipping, c.suggest_price, c.net_profit, c.net_margin,
                                brief],
                               tag=("alt" if i % 2 else ""))
        self._set_status(f"已生成 {len(self._listings)} 条上架候选（双击行可查看资料）")

    def _export_listing(self):
        if not self._listings:
            self._gen_listing()
        if not self._listings:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            listing.export_csv(self._listings, path)
            self.listing_path = path
            messagebox.showinfo("导出完成", f"已导出:\n{path}")

    def _upgrade_stale_collected(self):
        """用当前采集器重新抓取旧版数据, 补齐 SKU 组合 / 详情图(全量搬迁)。

        旧版 collected_1688.json 只有主图 + 规格名, 上架时 SKU/详情图为空。
        这里只重采「缺字段」的记录, 已完整的条目不动。
        """
        p = (ROOT / "data" / "collected_1688.json").as_posix()
        try:
            raw = json.load(open(p, encoding="utf-8"))
        except Exception:
            raw = []
        stale = [d for d in raw if isinstance(d, dict)
                 and (not d.get("sku_combos") or not d.get("detail_images"))]
        if not stale:
            messagebox.showinfo("无需升级", "已采集商品均含完整 SKU 组合与详情图，无需重采。")
            return
        links = [str(d.get("item_url") or d.get("item_id") or "") for d in stale]
        links = [x for x in links if x]
        if not messagebox.askyesno(
                "重新采集旧数据",
                f"检测到 {len(stale)} 条旧版采集数据缺少 SKU 组合 / 详情图。\n\n"
                "将用当前采集器重新在线采集这些链接并覆盖，补齐全部 SKU 与图片。\n"
                "需要 1688 登录态有效；期间请勿关闭弹出的浏览器。\n\n是否继续？"):
            return

        def job():
            import collector
            res = collector.collect(
                links, headless=True, save_dir=(ROOT / "data").as_posix(),
                progress=lambda k, m: self._emit("info", f"[重采] {m}"))
            self.products = loader.load_from_json_file(p)
            self._emit("loaded", f"重采完成 {len(res)}/{len(links)} 条，已刷新商品库")
            self._emit("listing", f"已按最新数据重新生成上架清单（重采 {len(res)} 条）")

        self._run_bg(job, "升级旧采集数据")

    # ===== 上架导出: 发布配置 + 资料核对 =====
    def _open_publish_config_dialog(self):
        """编辑 publish_config.json: 运费模板/库存/图片上限等发布参数。"""
        import pdd_publish
        cfg = pdd_publish._load_cfg()
        dlg = ctk.CTkToplevel(self)
        dlg.configure(fg_color=UI_BG)
        dlg.title("发布配置")
        # 本机 DPI 1.25 倍: 逻辑高 600 → 实际约 750px, 保证底部按钮落在屏幕内
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        h = min(600, max(480, int((sh - 60) / 1.25)))
        dlg.geometry(f"700x{h}+{max(0, (sw - 700) // 2)}+16")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(True, True)
        ctk.CTkLabel(dlg, text="发布参数 · 图片 / 价格 / 属性 / 运费",
                     font=F(14, True)).pack(pady=(10, 2))
        ctk.CTkLabel(dlg, text="写入 publish_config.json，重启工作台后仍生效；图片按平台上限合规上传。",
                     text_color="#888", font=F(11)).pack(pady=(0, 6))

        fields = [
            ("类目搜索词", "category_keyword", "留空=按1688类目/标题推断"),
            ("类目全路径", "category_path", "逗号分隔, 如 女装,连衣裙"),
            ("运费模板", "freight_template", "留空=用后台已选默认模板"),
            ("库存上限", "stock_cap", "1688库存不照搬, 代发建议5000封顶"),
            ("默认库存", "default_stock", "无库存数据时使用, 默认1000"),
            ("轮播主图上限", "max_carousel", "平台最多10张"),
            ("详情图上限", "max_detail", "平台最多50张"),
            ("SKU发布上限", "max_sku", "0=全量搬迁(默认); 卡顿可填30"),
            ("拼单价优惠(元)", "group_discount", "1~3元, 默认2, 不低于保本价"),
            ("最低净利率%", "min_net_margin", "拼单价保本线, 默认8"),
            ("参考价加价(元)", "advice_price_pad", "必填, 默认+10, 须大于最大单买价"),
            ("自动填属性", "auto_fill_attributes", "on/off, 按1688属性填"),
            ("自动填品牌", "auto_fill_brand", "off=品牌需授权, 默认留空"),
            ("定价方式", "price_source", "sku/single"),
            ("发货时间", "shipping_time", "可选, 如 48小时"),
        ]
        entries = {}
        for label, key, hint in fields:
            row = ctk.CTkFrame(dlg)
            row.pack(fill="x", padx=14, pady=2)
            ctk.CTkLabel(row, text=label, width=100, anchor="w").pack(side="left")
            e = ctk.CTkEntry(row, width=160)
            v = cfg.get(key, "")
            if isinstance(v, list):
                v = ",".join(str(x) for x in v)
            elif key == "min_net_margin":
                try:
                    v = f"{float(v or 0) * 100:g}"
                except (TypeError, ValueError):
                    v = "8"
            elif isinstance(v, bool):
                v = "on" if v else "off"
            e.insert(0, str(v))
            e.pack(side="left", padx=6)
            ctk.CTkLabel(row, text=hint, text_color="#888",
                         font=F(11), anchor="w").pack(side="left", padx=4)
            entries[key] = e

        def save():
            new = {}
            for key, e in entries.items():
                v = e.get().strip()
                if key == "category_path":
                    new[key] = [x.strip() for x in re.split(r"[,，/]", v) if x.strip()]
                elif key in ("stock_cap", "default_stock", "max_carousel", "max_detail", "max_sku"):
                    try:
                        new[key] = int(v)
                    except (TypeError, ValueError):
                        new[key] = cfg.get(key)
                elif key == "group_discount":
                    try:
                        new[key] = min(3.0, max(1.0, float(v)))
                    except (TypeError, ValueError):
                        new[key] = cfg.get(key)
                elif key == "min_net_margin":
                    try:
                        new[key] = min(0.60, max(0.0, float(v) / 100.0))
                    except (TypeError, ValueError):
                        new[key] = cfg.get(key)
                elif key == "advice_price_pad":
                    try:
                        new[key] = min(200.0, max(1.0, float(v)))
                    except (TypeError, ValueError):
                        new[key] = cfg.get(key)
                elif key in ("auto_fill_attributes", "auto_fill_brand"):
                    new[key] = v.strip().lower() in ("on", "1", "true", "yes", "是", "开", "y")
                else:
                    new[key] = v
            pdd_publish._save_cfg(new)
            self._set_status("✅ 发布配置已保存到 publish_config.json")
            messagebox.showinfo("已保存", "发布配置已写入 publish_config.json，重启工作台后仍生效。")
            dlg.destroy()

        btnf = ctk.CTkFrame(dlg)
        btnf.pack(fill="x", padx=14, pady=(10, 12))
        ctk.CTkButton(btnf, text="保存配置", width=120, command=save,
                      fg_color="#2f80ed", hover_color="#1f6fd0").pack(side="right", padx=6)
        ctk.CTkButton(btnf, text="关闭", width=100, command=dlg.destroy,
                      fg_color="#e7eef9", hover_color="#d6e3f6", text_color=UI_TEXT).pack(side="right", padx=6)


    def _show_listing_detail(self):
        """双击上架清单某行: 展示该商品已采集到的资料(类目/主图/详情图/SKU/属性)。"""
        sel = self.t_listing.tree.selection()
        if not sel:
            return
        try:
            idx = self.t_listing.tree.index(sel[0])
        except Exception:
            return
        if not (0 <= idx < len(self._listings)):
            return
        c = self._listings[idx]
        dlg = ctk.CTkToplevel(self)
        dlg.configure(fg_color=UI_BG)
        dlg.title(f"商品资料 · {c.item_id}")
        dlg.geometry("760x620")
        dlg.transient(self)
        dlg.resizable(True, True)
        ctk.CTkLabel(dlg, text=f"商品 {c.item_id} · 资料核对",
                     font=F(14, True)).pack(pady=(10, 4))
        txt = ctk.CTkTextbox(dlg)
        txt.pack(fill="both", expand=True, padx=12, pady=(2, 10))
        L = []
        L.append(f"优化标题 : {c.title}")
        L.append(f"1688类目 : {c.category_name or '(未识别, 发布时按标题推断)'}")
        L.append(f"建议售价 : ¥{c.suggest_price:.2f}   净利 ¥{c.net_profit:.2f}   净利率 {c.net_margin*100:.1f}%")
        L.append(f"货源链接 : {c.item_url}")
        L.append("")
        L.append(f"▶ 轮播主图 ({len(c.images or [])} 张)")
        for u in (c.images or [])[:5]:
            L.append("   " + u)
        if len(c.images or []) > 5:
            L.append(f"   … 其余 {len(c.images)-5} 张")
        L.append("")
        L.append(f"▶ 详情图 ({len(c.detail_images or [])} 张)")
        for u in (c.detail_images or [])[:5]:
            L.append("   " + u)
        if len(c.detail_images or []) > 5:
            L.append(f"   … 其余 {len(c.detail_images)-5} 张")
        L.append("")
        L.append(f"▶ SKU 规格维度 ({len(c.sku_dims or [])} 个)")
        for d in (c.sku_dims or []):
            L.append(f"   {d.get('name')}: " + " / ".join(
                v.get("name", "") for v in d.get("values", [])[:20]))
        L.append("")
        L.append(f"▶ SKU 组合 ({len(c.sku_combos or [])} 款) — 每款单独反算售价")
        for x in (c.sku_combos or [])[:20]:
            L.append(f"   {x.get('spec','')[:28]:<30} 售价¥{x.get('suggest_price',0):.2f} "
                     f"库存{x.get('stock',0)} (1688进价¥{x.get('price',0):.2f})")
        if len(c.sku_combos or []) > 20:
            L.append(f"   … 其余 {len(c.sku_combos)-20} 款")
        L.append("")
        try:
            import pdd_publish as _pp
            _lim = int(_pp._load_cfg().get("max_sku", 0))
        except Exception:
            _lim = 0
        L.append("▶ 发布说明（全量搬迁的边界）")
        L.append(f"   源站共 {len(c.sku_combos or [])} 款 SKU；发布上限 max_sku={_lim}"
                 + ("（0=全量搬迁）" if _lim == 0 else "，在『发布配置』里可改"))
        L.append("   规格维度 / 规格值需在拼多多后台创建或选择；脚本负责把每款的")
        L.append("   价格、库存、单买价、拼单价填入已有规格行，并上传全部合规图片。")
        L.append("   若后台规格行数少于源款数，发布日志会提示『需先在后台添加规格类型』。")
        L.append("")
        L.append(f"▶ 商品属性 ({len(c.attributes or {})} 项)")
        for k, v in list((c.attributes or {}).items())[:25]:
            vv = " / ".join(v) if isinstance(v, list) else str(v)
            L.append(f"   {k}: {vv}")
        txt.insert("0.0", "\n".join(L))
        txt.configure(state="disabled")
        ctk.CTkButton(dlg, text="关闭", width=110, command=dlg.destroy,
                      fg_color="#e7eef9", hover_color="#d6e3f6", text_color=UI_TEXT).pack(pady=(0, 10))

    # ===== Tab 6 下架(安全, 需登录) =====


    def _tab_delist(self):
        f = self.tabs.tab("下架")
        g1 = self._section(f, "① 扫描店铺在售商品", "登录拼多多后台后逐页抓取全部商品", "blue")
        self._btn(g1, "扫描在售商品", self._open_scan_dialog, "blue", 130)
        self._btn(g1, "生成零销量清单", self._do_report_zero, "ghost", 136)
        g2 = self._section(f, "② 下架试运行", "仅生成清单，不改动账号；表格支持 Ctrl/Shift 多选", "orange")
        self._btn(g2, "下架选中(试运行)", self._do_delist_selected, "orange", 142)
        self._btn(g2, "批量试运行", lambda: self._do_delist(items=None, dry=True), "ghost", 108)
        self._hint(f, "⚠ 下架是破坏性操作。本页只做「试运行」（打印清单，不碰账号）；"
                      "真实下架请用命令行 python -m pdd_store delist --confirm。", "danger")
        self._section_label(f, "③ 在售 / 零销量商品", "可上下滚动查看全部", "red")
        self.t_delist = Table(f, ["商品ID", "标题", "销量"], widths=[120, 380, 96], height=13)
        self.t_delist.tree.configure(selectmode="extended")
        self.t_delist.frame.pack(fill="both", expand=False, padx=14, pady=(2, 12))


    def _do_scan(self):
        try:
            import pdd_store
            def prog(msg):
                self._emit("info", f"扫描在售: {msg}")
            items = pdd_store.scan(headless=False, progress=prog)
            self._emit("scan", items)
        except Exception as e:
            self._emit("err", f"扫描失败: {e}")


    def _do_report_zero(self):
        try:
            import pdd_store
            zero = pdd_store.report_zero()
            self.t_delist.clear()
            for it in zero:
                self.t_delist.add([it.get("goods_id", ""), it.get("title", "")[:40], it.get("sales_text", "")])
            self._set_status(f"零销量 {len(zero)} 个")
        except Exception as e:
            self._set_status(f"生成清单失败: {e}")


    def _do_delist_selected(self):
        """把用户在表格中多选的行作为下架子集, 走 dry_run 试运行(安全, 不碰账号)"""
        sel = self.t_delist.tree.selection()
        if not sel:
            self._set_status("请先在表格中勾选(多选)要下架的商品")
            return
        items = []
        for s in sel:
            v = self.t_delist.tree.item(s)["values"]
            items.append({"goods_id": v[0], "title": v[1]})
        self._run_bg(lambda: self._do_delist(items=items, dry=True),
                     f"下架选中({len(items)}个)试运行")


    def _do_delist(self, items=None, dry=True):
        try:
            import pdd_store
            pdd_store.delist(items=items, confirm=not dry, dry_run=dry)
            self._set_status("下架试运行清单已生成(见运行控制台 / zero_sales.csv)" if dry
                             else "下架已执行(若 selector 已校准)")
        except Exception as e:
            self._set_status(f"下架失败: {e}")


    def _tab_ship(self):
        f = self.tabs.tab("发货")
        top = self._section(f, "① 发货计划", "导入订单 → 关联 1688 货源 → 生成发货清单", "blue")
        self._btn(top, "导入订单CSV", self._load_orders_ship, "blue", 130)
        self._btn(top, "生成发货计划", self._gen_ship, "purple", 130)
        self._btn(top, "导出发货CSV", self._export_ship, "green", 122)
        self._hint(f, "按订单关联 1688 货源与收货地区，自动标记偏远件；生成后可在下方核对每行。", "info")
        self._section_label(f, "② 发货明细", "表格可上下/左右滚动", "green")
        self.t_ship = Table(f, ["订单号", "1688货源", "买家", "地址", "件数", "偏远", "关联"],
                            widths=[140, 118, 96, 280, 58, 82, 92], height=12)
        self.t_ship.frame.pack(fill="both", expand=False, padx=14, pady=(2, 6))
        self._ship_rows = []
        self._ship_info = self._info_card(f, "👆 点『生成发货计划』后，这里显示订单数 / 关联数 / 偏远件汇总。")


    def _load_orders_ship(self):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if p:
            self.orders = orders_mod.parse_orders(p)
            self._set_status(f"订单导入 {len(self.orders)} 条(发货页)")

    def _gen_ship(self):
        if not self.orders:
            self._set_status("请先导入订单")
            return
        cost_map = orders_mod.load_cost_map(self.cost_map_path) if self.cost_map_path else None
        self._ship_rows = shipping_mod.build_ship_plan(self.orders, self.products, cost_map)
        self.t_ship.clear()
        for i, r in enumerate(self._ship_rows):
            self.t_ship.add([r["order_sn"], r["1688_item"] or "—", r["buyer"],
                             (r["address"] or "")[:30], r["qty"], r["remote"], r["linked"]],
                            tag=("alt" if i % 2 else ""))
        sm = shipping_mod.summary(self._ship_rows)
        self._set_status(f"发货计划: {sm['orders']}单 / 关联{sm['linked']} / 偏远{sm['remote']}")
        try:
            self._ship_info.configure(
                text="📦 发货计划汇总" + chr(10) + chr(10) +
                     "订单数：" + str(sm['orders']) + " 单    已关联 1688 货源：" + str(sm['linked']) + " 单    " +
                     "偏远件：" + str(sm['remote']) + " 单" + chr(10) + chr(10) +
                     "上方表格共 " + str(len(self._ship_rows)) + " 行，可滚动核对；确认无误后点『导出发货CSV』。")
        except Exception:
            pass

    def _export_ship(self):
        if not self._ship_rows:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            shipping_mod.export_csv(self._ship_rows, path)
            messagebox.showinfo("导出完成", path)


    def _tab_invoice(self):
        f = self.tabs.tab("开票")
        top = self._section(f, "① 开票匹配", "订单 × 1688 进货成本 → 销项/进项明细", "blue")
        self._btn(top, "导入订单CSV", self._load_orders_inv, "blue", 130)
        self._btn(top, "开票匹配", self._gen_invoice, "purple", 106)
        self._btn(top, "导出明细CSV", lambda: self._export_invoice("detail"), "green", 122)
        self._btn(top, "导出进项视图", lambda: self._export_invoice("purchase"), "ghost", 122)
        self._hint(f, "按订单匹配 1688 供应商与进货额，计算毛利；可分别导出销项明细与进项视图。", "info")
        self._section_label(f, "② 开票明细", "表格可上下/左右滚动", "green")
        self.t_inv = Table(f, ["订单号", "商品", "件数", "销售额", "进货额", "毛利", "供应商", "关联"],
                           widths=[128, 210, 58, 92, 92, 80, 104, 82], height=12)
        self.t_inv.frame.pack(fill="both", expand=False, padx=14, pady=(2, 6))
        self._inv_rows = []
        self._inv_info = self._info_card(f, "👆 点『开票匹配』后，这里显示匹配单数与汇总说明。")


    def _load_orders_inv(self):
        p = filedialog.askopenfilename(filetypes=[("CSV", "*.csv")])
        if p:
            self.orders = orders_mod.parse_orders(p)
            self._set_status(f"订单导入 {len(self.orders)} 条(开票页)")

    def _gen_invoice(self):
        if not self.orders:
            self._set_status("请先导入订单")
            return
        cost_map = orders_mod.load_cost_map(self.cost_map_path) if self.cost_map_path else None
        self._inv_rows = invoice_mod.build_invoice_records(self.orders, self.products, cost_map)
        self.t_inv.clear()
        for i, r in enumerate(self._inv_rows):
            self.t_inv.add([r["order_sn"], r["goods_name"][:24], r["qty"], r["sale_amount"],
                            r["cost_amount"], r["gross"], r["supplier_item"] or "—", r["linked"]],
                           tag=("alt" if i % 2 else ""))
        self._set_status(f"开票匹配 {len(self._inv_rows)} 单")
        try:
            linked = sum(1 for r in self._inv_rows if str(r.get("linked", "")).strip() not in ("", "—", "否", "0"))
            gross = sum(float(r.get("gross") or 0) for r in self._inv_rows)
            self._inv_info.configure(
                text="🧾 开票匹配汇总" + chr(10) + chr(10) +
                     "明细行数：" + str(len(self._inv_rows)) + "    已关联供应商：" + str(linked) + " 行    " +
                     "毛利合计：¥" + format(gross, ",.2f") + chr(10) + chr(10) +
                     "可分别导出『明细CSV』(销项) 与『进项视图』。")
        except Exception:
            pass

    def _export_invoice(self, view):
        if not self._inv_rows:
            return
        path = filedialog.asksaveasfilename(defaultextension=".csv", filetypes=[("CSV", "*.csv")])
        if path:
            invoice_mod.export_csv(self._inv_rows, path, view)
            messagebox.showinfo("导出完成", path)


    def _fetch_suggest(self):
        if not self.products:
            self._set_status("请先导入 1688 货源")
            return
        if self.busy:
            self._set_status(f"『{self._busy_label or '抓取推流词'}』仍在后台运行, 请稍候(不要重复点击)")
            return
        self._open_fetch_dialog()


    def _open_fetch_dialog(self):
        dlg = ctk.CTkToplevel(self)
        dlg.configure(fg_color=UI_BG)
        dlg.title("抓取推流词")
        dlg.geometry("560x500")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(True, True)

        ctk.CTkLabel(dlg, text="正在为商品抓取拼多多下拉推流词",
                     font=F(14, True)).pack(pady=(10, 2))
        status = ctk.CTkLabel(dlg, text="准备中…", text_color="#888")
        status.pack(pady=2)
        bar = ctk.CTkProgressBar(dlg, width=480)
        bar.pack(padx=20, pady=4)
        bar.set(0)

        ctk.CTkLabel(dlg, text="已抓取词(实时):", anchor="w").pack(anchor="w", padx=20, pady=(6, 0))
        txt = ctk.CTkTextbox(dlg, height=270)
        txt.pack(fill="both", expand=True, padx=20, pady=(2, 8))
        txt.insert("0.0", "（等待开始…）\n")

        btn_frame = ctk.CTkFrame(dlg)
        btn_frame.pack(fill="x", padx=20, pady=(0, 12))
        btn_ok = ctk.CTkButton(btn_frame, text="确定", state="disabled", width=120)
        btn_ok.pack(side="right", padx=8)
        btn_close = ctk.CTkButton(btn_frame, text="关闭", width=120, fg_color="#e7eef9", hover_color="#d6e3f6", text_color=UI_TEXT)
        btn_close.pack(side="right", padx=8)

        # 共享状态: alive=弹窗是否还开着; done=是否完整抓完
        state = {"alive": True, "done": False, "raw": {}, "profiles": {}, "total": 0, "i": 0}

        def set_status(s):
            try:
                status.configure(text=s)
            except Exception:
                pass

        def add_word(i, total, word, n):
            try:
                txt.insert("end", f"  [{i}/{total}] {word} → {n} 个联想词\n")
                txt.see("end")
            except Exception:
                pass

        def on_done(kept_n, drop_n):
            state["done"] = True
            try:
                bar.set(1)
                set_status(f"✅ 抓取完成: 保留 {kept_n} / 剔除 {drop_n} 个推流词（点『确定』应用并关闭）")
                btn_ok.configure(state="normal")  # 抓完 → 高亮可用
            except Exception:
                pass

        def on_err(msg):
            state["done"] = True
            try:
                set_status(f"⚠ 出错: {msg}（可点『关闭』保留已抓到的词）")
                btn_ok.configure(state="disabled")
            except Exception:
                pass

        def _finish():
            state["alive"] = False
            self.busy = False
            self._busy_label = ""
            try:
                dlg.destroy()
            except Exception:
                pass

        def do_ok():
            if not state["done"]:
                return
            self._refresh_title_after_fetch()
            self._set_status(f"推流词完成 保留{sum(len(v) for v in self.terms_map.values())} "
                             f"剔除{sum(len(v) for v in self.drop_map.values())} ｜ 可到『标题优化』回捞剔除词")
            _finish()

        def do_close():
            # 提前关闭: 应用关闭时已抓到的词语(保留它们, 不丢失)
            if not state["done"]:
                self._apply_terms_from_raw(state["raw"], state["profiles"])
                self._refresh_title_after_fetch()
                self._set_status(f"已提前关闭, 保留关闭时抓到的推流词(保留 "
                                 f"{sum(len(v) for v in self.terms_map.values())} 个)")
            _finish()

        btn_ok.configure(command=do_ok)
        btn_close.configure(command=do_close)
        dlg.protocol("WM_DELETE_WINDOW", do_close)  # 点右上角 X 同『关闭』

        self.busy = True
        self._busy_label = "抓取推流词"
        threading.Thread(target=self._run_fetch_thread,
                         args=(state, set_status, bar, add_word, on_done, on_err),
                         daemon=True).start()


    def _safe_ui(self, fn, *args):
        """把 UI 更新安全调度到主线程(tkinter 不允许后台线程直接改控件)"""
        try:
            self.after(0, lambda: fn(*args))
        except Exception:
            pass


    def _run_fetch_thread(self, state, set_status, bar, add_word, on_done, on_err):
        try:
            from suggest import SuggestClient, build_enriched_terms
            import title_opt
            cache_path = (ROOT / "suggest_cache.json").as_posix()
            raw = state["raw"]
            if os.path.exists(cache_path):
                try:
                    with open(cache_path, encoding="utf-8") as fh:
                        raw.update(json.load(fh))
                except Exception:
                    pass
            # 标题画像
            self._safe_ui(set_status, "正在为商品做标题画像…")
            profiles = {}
            for p in self.products:
                ex = title_opt.optimize(p)["extracted"]
                profiles[p.item_id] = ex["core"] + ex["attr"]
            state["profiles"] = profiles
            need = []
            for pid, words in profiles.items():
                for w in words:
                    if w and w not in raw and w not in need:
                        need.append(w)
            # 词数过多时封顶, 避免一次跑太久(无头浏览器每个词约 3-8 秒)
            if len(need) > 50:
                self._safe_ui(set_status, f"待抓词较多({len(need)}), 本次仅抓前 50, 其余复用缓存")
                need = need[:50]
            total = len(need)
            state["total"] = total
            if need:
                self._safe_ui(set_status, f"即将打开浏览器抓 {total} 个词(每个约 3-8 秒, 请耐心等待)…")
                try:
                    client = SuggestClient(headless=True)
                except Exception as e:
                    self._safe_ui(on_err, f"浏览器启动失败(确认 Playwright Chromium 已安装): {e}")
                    return
                t0 = time.time()
                try:
                    for i, w in enumerate(need, 1):
                        if not state["alive"]:
                            break  # 用户提前关闭: 停止继续抓, 保留已抓部分
                        sugg = client.suggest(w) or []
                        raw[w] = sugg
                        # 增量落盘, 即使提前关闭也不丢已抓数据
                        try:
                            with open(cache_path, "w", encoding="utf-8") as fh:
                                json.dump(raw, fh, ensure_ascii=False, indent=2)
                        except Exception:
                            pass
                        if state["alive"]:
                            self._safe_ui(
                                lambda i=i, w=w, n=len(sugg), t0=t0, total=total: (
                                    bar.set(i / total),
                                    add_word(i, total, w, n),
                                    set_status(f"抓取 {i}/{total} 『{w}』(已用 {int(time.time() - t0)}s)"),
                                )
                            )
                finally:
                    try:
                        client.close()
                    except Exception:
                        pass
            else:
                self._safe_ui(set_status, "全部词已有缓存, 直接过滤")
            # 过滤(用当前 raw, 部分或完整均可)
            kept_total = drop_total = 0
            for p in self.products:
                merged = []
                for w in profiles.get(p.item_id, []):
                    merged.extend(raw.get(w, []) or [])
                kept, dropped = build_enriched_terms(merged, profiles[p.item_id], profiles[p.item_id],
                                                    support_text=p.title, min_score=0.55, return_dropped=True)
                self.terms_map[p.item_id] = kept
                self.drop_map[p.item_id] = dropped
                kept_total += len(kept)
                drop_total += len(dropped)
            if state["alive"]:
                self._safe_ui(lambda: on_done(kept_total, drop_total))
        except Exception as e:
            if state["alive"]:
                self._safe_ui(on_err, f"抓取失败: {e}")


    def _apply_terms_from_raw(self, raw, profiles):
        """用当前 raw(可能不完整)为各商品计算保留/剔除词, 写入 terms_map/drop_map"""
        try:
            from suggest import build_enriched_terms
        except Exception:
            return
        if not profiles:
            return
        for p in self.products:
            merged = []
            for w in profiles.get(p.item_id, []):
                merged.extend(raw.get(w, []) or [])
            try:
                kept, dropped = build_enriched_terms(merged, profiles[p.item_id], profiles[p.item_id],
                                                    support_text=p.title, min_score=0.55, return_dropped=True)
            except Exception:
                kept, dropped = [], []
            self.terms_map[p.item_id] = kept
            self.drop_map[p.item_id] = dropped

    # ===== 扫描在售商品: 独立进度弹窗 =====


    def _open_scan_dialog(self):
        dlg = ctk.CTkToplevel(self)
        dlg.configure(fg_color=UI_BG)
        dlg.title("扫描在售商品")
        dlg.geometry("560x520")
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(True, True)

        ctk.CTkLabel(dlg, text="正在扫描拼多多商家后台在售商品",
                     font=F(14, True)).pack(pady=(10, 2))
        status = ctk.CTkLabel(dlg, text="准备中…（请在弹出的浏览器里先完成登录）", text_color="#888")
        status.pack(pady=2)
        bar = ctk.CTkProgressBar(dlg, width=480)
        bar.pack(padx=20, pady=4)
        bar.set(0)

        ctk.CTkLabel(dlg, text="扫描日志(实时):", anchor="w").pack(anchor="w", padx=20, pady=(6, 0))
        txt = ctk.CTkTextbox(dlg, height=300)
        txt.pack(fill="both", expand=True, padx=20, pady=(2, 8))
        txt.insert("0.0", "（等待开始…）\n")

        btn_frame = ctk.CTkFrame(dlg)
        btn_frame.pack(fill="x", padx=20, pady=(0, 12))
        btn_ok = ctk.CTkButton(btn_frame, text="确定", state="disabled", width=120)
        btn_ok.pack(side="right", padx=8)
        btn_close = ctk.CTkButton(btn_frame, text="关闭", width=120, fg_color="#e7eef9", hover_color="#d6e3f6", text_color=UI_TEXT)
        btn_close.pack(side="right", padx=8)

        state = {"alive": True, "done": False, "items": [], "total_pages": 0}

        def set_status(s):
            try:
                status.configure(text=s)
            except Exception:
                pass

        def log(s):
            try:
                txt.insert("end", s + "\n")
                txt.see("end")
            except Exception:
                pass

        def on_done(items, n):
            state["done"] = True
            try:
                bar.set(1)
                set_status(f"✅ 扫描完成: 共 {n} 个在售商品（点『确定』载入下方表格）")
                btn_ok.configure(state="normal")
            except Exception:
                pass

        def on_err(msg):
            state["done"] = True
            try:
                set_status(f"⚠ 出错: {msg}（可点『关闭』载入已扫描到的部分）")
                btn_ok.configure(state="disabled")
            except Exception:
                pass

        def _finish():
            state["alive"] = False
            self.busy = False
            self._busy_label = ""
            try:
                dlg.destroy()
            except Exception:
                pass

        def do_ok():
            if not state["done"]:
                return
            self._emit("scan", state["items"])
            _finish()

        def do_close():
            # 提前关闭: 若已扫到部分, 也一并载入(不丢失)
            if state["items"]:
                self._emit("scan", state["items"])
                self._set_status(f"已提前关闭, 载入扫描到的 {len(state['items'])} 个商品")
            _finish()

        btn_ok.configure(command=do_ok)
        btn_close.configure(command=do_close)
        dlg.protocol("WM_DELETE_WINDOW", do_close)  # 点右上角 X 同『关闭』

        self.busy = True
        self._busy_label = "扫描在售商品"
        threading.Thread(target=self._run_scan_thread,
                         args=(state, set_status, bar, log, on_done, on_err),
                         daemon=True).start()


    def _run_scan_thread(self, state, set_status, bar, log, on_done, on_err):
        try:
            import pdd_store
            def prog(msg):
                if not state["alive"]:
                    return
                self._safe_ui(lambda m=msg: self._scan_progress_ui(m, state, set_status, bar, log))
            items = pdd_store.scan(headless=False, progress=prog)
            if state["alive"]:
                state["items"] = items
                self._safe_ui(lambda: on_done(items, len(items)))
        except Exception as e:
            if state["alive"]:
                self._safe_ui(lambda: on_err(str(e)))


    def _scan_progress_ui(self, msg, state, set_status, bar, log):
        try:
            log(msg)
        except Exception:
            pass
        # 进度条: 优先用『共 N 页』『第 X 页』; 否则保持滚动态
        if "共" in msg and "页" in msg:
            m = re.search(r"共\s*(\d+)\s*页", msg)
            if m:
                state["total_pages"] = int(m.group(1))
        if "第" in msg and "页" in msg:
            m = re.search(r"第\s*(\d+)\s*页", msg)
            if m:
                p = int(m.group(1))
                frac = (p / max(1, state["total_pages"])) if state["total_pages"] else min(0.95, 0.08 + 0.08 * p)
                bar.set(min(0.99, frac))
        set_status(msg)


    def _refresh_title_after_fetch(self):
        try:
            self._rebuild_title_picker()
            self._render_select()
            self._render_price()
            if self.selected_item:
                self._render_title_preview()
        except Exception:
            pass

    # ===== 一键发布到拼多多: 独立进度弹窗 =====


    def _open_publish_dialog(self):
        if not self._listings:
            self._set_status("请先在『上架导出』点『生成上架清单』，再发布")
            messagebox.showinfo("提示", "上架清单为空。请先点『生成上架清单』生成候选，再发布到拼多多。")
            return
        dlg = ctk.CTkToplevel(self)
        dlg.configure(fg_color=UI_BG)
        dlg.title("一键发布到拼多多")
        # 弹窗曾被内容撑到 950px 而屏幕只有 864px, 底部按钮区被挤出可视范围,
        # 用户点不到「开始发布」→ 一直停在「等待开始」也不会弹浏览器。
        # 这里按屏幕高度限高并靠上居中, 同时把按钮区固定到窗口底部。
        sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
        w = min(820, max(700, sw - 120))
        x = max(0, (sw - w) // 2)
        h = 620
        dlg.geometry(f"{w}x{h}+{x}+16")
        dlg.minsize(700, 460)
        dlg.transient(self)
        dlg.grab_set()
        dlg.resizable(True, True)

        # ⑥ 按钮: 必须最先 pack 到 side=bottom 并固定高度。
        # 否则后面 expand=True 的日志框会把它压成 1px / 挤到窗口外。
        btn_frame = ctk.CTkFrame(dlg, fg_color="transparent", height=52)
        btn_frame.pack(side="bottom", fill="x", padx=18, pady=(6, 12))
        btn_frame.pack_propagate(False)
        btn_ok = ctk.CTkButton(btn_frame, text="确定", state="disabled", width=110,
                               height=36, font=F(13, True), corner_radius=8,
                               fg_color=UI_BLUE, hover_color=UI_BLUE_D)
        btn_ok.pack(side="right", padx=6)
        btn_close = ctk.CTkButton(btn_frame, text="关闭（随时可关）", width=150,
                                  height=36, font=F(12), corner_radius=8,
                                  fg_color="#e7eef9", hover_color="#d6e3f6",
                                  text_color=UI_TEXT)
        btn_close.pack(side="right", padx=6)
        btn_start = ctk.CTkButton(btn_frame, text="开始发布", width=130,
                                  height=36, font=F(13, True), corner_radius=8,
                                  fg_color=UI_BLUE, hover_color=UI_BLUE_D)
        btn_start.pack(side="right", padx=6)

        # ① 标题条
        head = ctk.CTkFrame(dlg, fg_color=UI_BLUE, corner_radius=0, height=60)
        head.pack(fill="x")
        head.pack_propagate(False)
        ctk.CTkLabel(head, text="一键发布到拼多多", font=F(18, True),
                     text_color="#ffffff").pack(side="left", padx=22)

        # ② 流程说明
        flow = ctk.CTkFrame(dlg, fg_color=UI_SOFT, corner_radius=10)
        flow.pack(fill="x", padx=18, pady=(14, 6))
        ctk.CTkLabel(flow, text="填写流程", font=F(12, True),
                     text_color=UI_BLUE_D).pack(anchor="w", padx=15, pady=(11, 3))
        ctk.CTkLabel(flow, text="① 主图 + 标题   →   ② 详情图 / 规格价与库存 / 运费模板   →   提交",
                     font=F(12), text_color=UI_TEXT, wraplength=730,
                     justify="left").pack(anchor="w", padx=15, pady=(0, 5))
        ctk.CTkLabel(flow, text="预览只填写不提交；浏览器会保持打开，请在页面里核对后再关闭本窗口。",
                     font=F(11), text_color=UI_MUTED).pack(anchor="w", padx=15, pady=(0, 11))

        # ③ 安全选项
        opt = ctk.CTkFrame(dlg, fg_color=UI_CARD, corner_radius=10,
                           border_width=1, border_color=UI_BORDER)
        opt.pack(fill="x", padx=18, pady=6)
        ctk.CTkLabel(opt, text="安全选项", font=F(12, True),
                     text_color=UI_TEXT).pack(anchor="w", padx=15, pady=(11, 3))
        var_real = ctk.StringVar(value="off")
        chk_real = ctk.CTkCheckBox(opt, text="真实发布（不勾选＝仅预览填写，绝不提交）",
                                   font=F(12), text_color=UI_RED,
                                   variable=var_real, onvalue="on", offvalue="off")
        chk_real.pack(anchor="w", padx=20, pady=3)
        var_test = ctk.StringVar(value="on")
        chk_test = ctk.CTkCheckBox(opt, text="仅试发前 1 条（先核对，再发布其余商品）",
                                   font=F(12), text_color=UI_TEXT,
                                   variable=var_test, onvalue="on", offvalue="off")
        chk_test.pack(anchor="w", padx=20, pady=(3, 12))

        # ④ 状态 + 进度
        st_card = ctk.CTkFrame(dlg, fg_color=UI_CARD, corner_radius=10,
                               border_width=1, border_color=UI_BORDER)
        st_card.pack(fill="x", padx=18, pady=6)
        status = ctk.CTkLabel(st_card, text="请先核对下方选项，再点『开始发布』；登录在弹出的浏览器里完成",
                              font=F(12), text_color=UI_BLUE, anchor="w", wraplength=720)
        status.pack(fill="x", padx=15, pady=(11, 5))
        bar = ctk.CTkProgressBar(st_card, height=12, corner_radius=6,
                                 fg_color="#e7eef9", progress_color=UI_BLUE)
        bar.pack(fill="x", padx=15, pady=(0, 13))
        bar.set(0)

        # ⑤ 日志
        ctk.CTkLabel(dlg, text="发布日志（实时）", font=F(12, True),
                     text_color=UI_TEXT, anchor="w").pack(anchor="w", padx=20, pady=(9, 3))
        txt = ctk.CTkTextbox(dlg, height=110, font=("Consolas", 11),
                             fg_color="#ffffff", corner_radius=10,
                             border_width=1, border_color=UI_BORDER)
        txt.pack(fill="both", expand=True, padx=18, pady=(0, 6))
        txt.insert("0.0", "（等待开始…）" + chr(10))

        total = len(self._listings)
        state = {"alive": True, "done": False, "result": None, "total": total,
                 "started": False}

        def set_status(s):
            try:
                status.configure(text=s)
            except Exception:
                pass

        def log(s):
            try:
                txt.insert("end", s + "\n")
                txt.see("end")
            except Exception:
                pass

        def on_done(res):
            if state["done"]:
                return
            state["done"] = True
            # 浏览器仍开着供核对, 保留 busy 锁避免其它任务抢同一个登录目录;
            # 标签改写清楚, 不再误报"仍在后台运行"
            self.busy = True
            self._busy_label = "浏览器核对中（关闭本窗口即结束）"
            try:
                bar.set(1)
                written = res.get("written", 0)
                miss = sum(len(x.get("missing") or []) for x in (res.get("results") or []))
                total_r = res.get("total", 0)
                if res.get("error"):
                    set_status(f"⚠ 发布中断: {res['error']}（详见下方日志，可重新发布）")
                    btn_ok.configure(state="normal")
                    btn_start.configure(state="normal", text="重新发布", command=do_restart)
                    return
                if res.get("dry_run", True):
                    label = (f"预览完成（未提交）· 共 {total_r} 条，实际写入表单 {written} 项，"
                             f"未完成 {miss} 项。浏览器保持打开，请核对；"
                             f"确认无误后勾选『真实发布』，点『重新发布』即可上架。")
                else:
                    label = (f"完成 · 共 {total_r} 条，实际写入表单 {written} 项，"
                             f"已点击提交 {res.get('submitted', 0)} 条（结果请在商家后台确认）。")
                set_status(label)
                btn_ok.configure(state="normal")
                btn_start.configure(state="normal", text="重新发布", command=do_restart)
            except Exception:
                pass

        def on_err(msg):
            state["done"] = True
            self.busy = False
            self._busy_label = ""
            try:
                set_status(f"⚠ 出错: {msg}")
                btn_ok.configure(state="normal")
                btn_start.configure(state="normal", text="重新发布", command=do_restart)
            except Exception:
                pass
            try:
                import pdd_publish
                pdd_publish.close_preview()
            except Exception:
                pass

        def _finish():
            state["alive"] = False
            self.busy = False
            self._busy_label = ""
            # 通知发布线程结束预览并关闭浏览器(仅置标志, 线程安全)
            try:
                import pdd_publish
                pdd_publish.close_preview()
            except Exception:
                pass
            try:
                dlg.destroy()
            except Exception:
                pass

        def do_restart():
            """结束后重开一轮: 先让上一轮浏览器彻底关闭, 再延迟启动, 避免登录目录冲突。"""
            try:
                import pdd_publish
                pdd_publish.close_preview()
            except Exception:
                pass
            state["alive"] = True
            state["done"] = False
            state["started"] = False
            state["result"] = None
            try:
                chk_real.configure(state="normal")
                chk_test.configure(state="normal")
                btn_ok.configure(state="disabled")
                btn_start.configure(state="disabled", text="准备中…", command=do_start)
                txt.delete("0.0", "end")
                bar.set(0)
                set_status("正在结束上一轮浏览器，请稍候…")
            except Exception:
                pass
            self.after(2000, do_start)

        def do_start():
            if state["started"] or not state["alive"]:
                return
            state["started"] = True
            state["done"] = False
            dry_run = (var_real.get() != "on")
            limit = 1 if (var_test.get() == "on") else None
            # 立刻给可见反馈: 旧版这一步没有任何输出, 用户分不清
            # 「按钮没点到」和「点到了但浏览器启动卡住」。
            mode = "真实发布" if not dry_run else "仅预览(不提交)"
            scope = "前 1 条" if limit else f"全部 {total} 条"
            log(f"▶ 已点『开始发布』· {mode} · 范围: {scope}")
            log("  正在唤起浏览器(首次约 10~30 秒), 请勿关闭本窗口…")
            set_status("正在启动浏览器并登录，请稍候…（下方日志会实时显示进度）")
            try:
                chk_real.configure(state="disabled")
                chk_test.configure(state="disabled")
                btn_start.configure(state="disabled", text="发布中…")
                btn_ok.configure(state="disabled")
            except Exception:
                pass
            try:
                import pdd_publish
                pdd_publish.begin_preview()
            except Exception as e:
                log(f"  ⚠ 预览状态初始化失败(不影响发布): {e}")
            self.busy = True
            self._busy_label = "一键发布"
            threading.Thread(target=self._run_publish_thread,
                             args=(state, set_status, bar, log, on_done, on_err,
                                   dry_run, limit),
                             daemon=True).start()

        def do_ok():
            _finish()

        def do_close():
            _finish()

        btn_start.configure(command=do_start)
        btn_ok.configure(command=do_ok)
        btn_close.configure(command=do_close)
        dlg.protocol("WM_DELETE_WINDOW", do_close)

        # 内容全部布局完成后再校正一次窗口高度: customtkinter 的 DPI 缩放会把
        # geometry 的 700 放大成 875px, 加上内容高度, 窗口底部会掉出屏幕。
        # 用实测比例反算逻辑高度, 确保底部按钮区完整可见。
        try:
            dlg.update()
            real = dlg.winfo_height() or h
            if real + 16 > sh - 24:
                scale = max(1.0, real / float(h))
                new_h = max(430, int((sh - 56) / scale))
                dlg.geometry(f"{w}x{new_h}+{x}+16")
                dlg.update_idletasks()
        except Exception:
            pass


    def _run_publish_thread(self, state, set_status, bar, log, on_done, on_err,
                            dry_run, limit):
        try:
            import pdd_publish

            def prog(msg):
                if not state["alive"]:
                    return
                self._safe_ui(lambda m=msg: log(m))
                m = re.search(r"【(\d+)/(\d+)】", msg)
                if m:
                    p, t = int(m.group(1)), int(m.group(2))
                    self._safe_ui(lambda v=min(0.99, p / max(1, t)): bar.set(v))

            def on_result(res):
                res["dry_run"] = dry_run
                if state["alive"]:
                    state["result"] = res
                    self._safe_ui(lambda: on_done(res))

            # publish() 在浏览器关闭前通过 on_result 交付结果(仅一次)。
            # 这里不再重复调用 on_done: 否则「重新发布」后, 上一轮线程迟到返回时
            # 会把新一轮「发布中…」的界面覆盖成「完成」。异常兜底仍由 on_err 处理。
            pdd_publish.publish(headless=False, candidates=self._listings,
                                progress=prog, dry_run=dry_run, limit=limit,
                                on_result=on_result)
        except Exception as e:
            if state["alive"]:
                self._safe_ui(lambda: on_err(str(e)))

    def _publish_progress_ui(self, msg, state, set_status, bar, log):
        try:
            log(msg)
        except Exception:
            pass
        m = re.search(r"【(\d+)/(\d+)】", msg)
        if m:
            p, t = int(m.group(1)), int(m.group(2))
            bar.set(min(0.99, p / max(1, t)))
        set_status(msg)


    # ---------- 线程/队列 ----------
    def _run_bg(self, job, label):
        if self.busy:
            self._set_status(f"『{self._busy_label}』仍在后台运行, 请稍候(不要重复点击)")
            return
        self.busy = True
        self._busy_label = label
        self._set_status(f"{label} 中…")
        threading.Thread(target=self._wrap, args=(job, label), daemon=True).start()

    def _wrap(self, job, label):
        try:
            job()
        except Exception as e:
            self._emit("err", f"{label}异常: {e}")
        finally:
            self.busy = False

    def _emit(self, kind, payload):
        self._q.put((kind, payload))

    def _pump(self):
        try:
            while True:
                kind, payload = self._q.get_nowait()
                self._handle(kind, payload)
        except queue.Empty:
            pass
        self.after(120, self._pump)

    def _handle(self, kind, payload):
        if kind == "loaded":
            self._rebuild_title_picker()
            self._render_select()
            self._render_price()
            self._set_status(payload)
        elif kind == "listing":
            # 后台采集完成 → 主线程重新生成上架清单, 实现「粘链接 → 直接可发布」
            self._gen_listing()
            self._set_status(payload)
        elif kind == "orders":
            self._set_status(payload)
        elif kind == "info":
            self._set_status(payload)
            if isinstance(payload, str) and payload.startswith("推流词:"):
                self._update_title_progress(payload)
        elif kind == "suggest_done":
            self._rebuild_title_picker()
            self._render_select()
            self._render_price()
            self._update_title_progress("done")
            self._set_status(payload + " ｜ 可到『标题优化』回捞剔除词")
        elif kind == "scan":
            items = payload or []
            self.t_delist.clear()
            for it in items:
                self.t_delist.add([it.get("goods_id", ""), it.get("title", "")[:40], it.get("sales_text", "")])
            if items:
                self._set_status(f"✅ 扫描到 {len(items)} 个在售商品(已载入下方表格, 可多选后『下架选中(试运行)』)")
            else:
                self._set_status("⚠ 扫描到 0 个商品: 可能后台改版或仍停留在登录/验证页。"
                                 " 请查看 data/debug_store.html 并反馈给我校准选择器")
        elif kind == "err":
            if isinstance(payload, str) and payload.startswith("推流词"):
                self._update_title_progress("err:" + payload)
            self._set_status(f"⚠ {payload}")
            messagebox.showerror("错误", payload)
        else:
            self._set_status(str(payload))

    def _update_title_progress(self, text):
        """把『抓推流词』进度就近显示到标题优化页的进度条(不再只去顶部状态栏盲等)。"""
        if not hasattr(self, "title_prog"):
            return
        if text == "done":
            self.title_prog.set(1.0)
            self.title_prog_label.configure(
                text="✅ 推流词抓取完成，可在此页双击调整保留/剔除词", text_color="#1f9d55")
            # 若正停在标题页, 立刻用新词重算结果
            if self.selected_item:
                self._render_title_preview()
            return
        if isinstance(text, str) and text.startswith("err:"):
            self.title_prog.set(0)
            self.title_prog_label.configure(text=text[4:].strip(), text_color="#eb5757")
            return
        # 解析 抓取 i/N
        m = re.search(r"抓取\s+(\d+)\s*/\s*(\d+)", text or "")
        if m:
            i, n = int(m.group(1)), int(m.group(2))
            self.title_prog.set((i / n) if n else 0)
            self.title_prog_label.configure(text=text, text_color="#2f80ed")
        elif "过滤" in (text or ""):
            self.title_prog.set(0.92)
            self.title_prog_label.configure(text=text, text_color="#2f80ed")
        elif "即将打开" in (text or ""):
            self.title_prog.set(0.12)
            self.title_prog_label.configure(text=text, text_color="#2f80ed")
        else:
            # 画像 / 启动 等不确定阶段, 给个起步值 + 文字
            self.title_prog.set(0.06)
            self.title_prog_label.configure(text=text, text_color="#2f80ed")

    def _open_settings(self):
        """运费/佣金校准对话框: 把你的真实承运商报价与平台费率填进来, 定价立刻生效。"""
        sh = self.config.shipping
        s = ctk.CTkToplevel(self)
        s.title("运费 / 佣金 校准")
        s.geometry("480x560")
        s.transient(self)
        try:
            s.grab_set()
        except Exception:
            pass
        rows = []

        def mk(label, val, key, is_float=True, is_choice=None):
            fr = ctk.CTkFrame(s)
            fr.pack(fill="x", padx=12, pady=3)
            ctk.CTkLabel(fr, text=label, width=210, anchor="w").pack(side="left")
            if is_choice:
                e = ctk.CTkComboBox(fr, width=130, values=is_choice)
                e.set(str(val))
            else:
                e = ctk.CTkEntry(fr, width=130)
                e.insert(0, str(val))
            e.pack(side="right", padx=6)
            rows.append((key, e, is_float))
            return e

        ctk.CTkLabel(s, text="① 运费模板(按你的快递报价填)",
                     font=F(13, True), text_color="#2f80ed").pack(anchor="w", padx=12, pady=(10, 2))
        mk("运费模式", sh.mode, "mode", is_float=False, is_choice=["weight", "fixed"])
        mk("固定运费¥(fixed 模式用)", sh.fixed_fee, "fixed_fee")
        mk("固定模式·偏远附加¥", sh.remote_surcharge, "remote_surcharge")
        mk("内地首重(kg)", sh.first_weight, "first_weight")
        mk("内地首重费¥", sh.first_fee, "first_fee")
        mk("续重单位(kg)", sh.add_weight, "add_weight")
        mk("内地续重¥/单位", sh.add_fee, "add_fee")
        mk("偏远首重费¥", sh.remote_first_fee, "remote_first_fee")
        mk("偏远续重¥/单位", sh.remote_add_fee, "remote_add_fee")

        ctk.CTkLabel(s, text="② 平台费率 / 目标(类目佣金可在定价页按商品看)",
                     font=F(13, True), text_color="#2f80ed").pack(anchor="w", padx=12, pady=(10, 2))
        mk("默认佣金率%", round(self.config.commission_rate * 100, 2), "commission_rate")
        mk("支付费率%", round(self.config.payment_fee_rate * 100, 2), "payment_fee_rate")
        mk("目标净利率%", int(self.config.target_net_margin * 100), "target_net_margin")
        mk("默认其他费(包装)¥", self.config.default_other_fee, "default_other_fee")

        prev = ctk.CTkLabel(s, text="", text_color="#1f9d55", justify="left")
        prev.pack(anchor="w", padx=14, pady=6)

        def refresh_preview(*a):
            try:
                d = {}
                for key, e, fl in rows:
                    d[key] = (float(e.get()) if fl else e.get())
                tmp = pricing.ShippingTemplate(
                    mode=d.get("mode", "weight"),
                    fixed_fee=d.get("fixed_fee", 3.0),
                    remote_surcharge=d.get("remote_surcharge", 5.0),
                    first_weight=d.get("first_weight", 1.0),
                    first_fee=d.get("first_fee", 3.5),
                    add_weight=d.get("add_weight", 1.0),
                    add_fee=d.get("add_fee", 1.5),
                    remote_first_fee=d.get("remote_first_fee", 10.0),
                    remote_add_fee=d.get("remote_add_fee", 6.0),
                )
                n = tmp.estimate(1.0, "")
                r = tmp.estimate(1.0, "新疆")
                prev.configure(text=f"示例(1kg):  内地运费 ¥{n}    |    偏远(新疆) ¥{r}")
            except Exception as ex:
                prev.configure(text=f"预览错误: {ex}")

        for _, e, _ in rows:
            e.bind("<KeyRelease>", refresh_preview)
            if isinstance(e, ctk.CTkComboBox):
                e.configure(command=lambda v: refresh_preview())
        refresh_preview()

        btns = ctk.CTkFrame(s)
        btns.pack(fill="x", padx=12, pady=(4, 12))

        def do_save():
            try:
                d = {}
                for key, e, fl in rows:
                    d[key] = (float(e.get()) if fl else e.get())
                sh.mode = d["mode"] if d["mode"] in ("fixed", "weight") else "weight"
                sh.fixed_fee = d["fixed_fee"]
                sh.remote_surcharge = d["remote_surcharge"]
                sh.first_weight = d["first_weight"]
                sh.first_fee = d["first_fee"]
                sh.add_weight = d["add_weight"]
                sh.add_fee = d["add_fee"]
                sh.remote_first_fee = d["remote_first_fee"]
                sh.remote_add_fee = d["remote_add_fee"]
                self.config.commission_rate = d["commission_rate"] / 100
                self.config.payment_fee_rate = d["payment_fee_rate"] / 100
                self.config.target_net_margin = d["target_net_margin"] / 100
                self.config.default_other_fee = d["default_other_fee"]
                self._save_config()
                if self.products:
                    self._render_select()
                    self._render_price()
                self._set_status("运费/佣金已校准并保存")
                s.destroy()
            except ValueError:
                self._set_status("参数格式错误, 未保存")

        ctk.CTkButton(btns, text="保存并应用", width=130, command=do_save).pack(side="left", padx=12)
        ctk.CTkButton(btns, text="取消", width=90, command=s.destroy).pack(side="left", padx=10)

    def _set_status(self, text):
        self.status.configure(text=text)

    def _on_close(self):
        self._save_config()
        self.destroy()

    # ===== 1688 链接采集器对话框 =====
    def _open_collector(self, auto_listing=False):
        """采集 1688 商品链接(自采数据, 可直接用于上架)。

        在线抓取用 Playwright(需 playwright install chromium); 也支持解析本地保存的 HTML。

        auto_listing=True: 采完自动重新生成上架清单(上架导出页的直达入口)。
        """
        try:
            import collector
        except Exception as e:
            messagebox.showerror("模块缺失", f"collector 导入失败: {e}")
            return
        d = ctk.CTkToplevel(self)
        d.title("采集 1688 商品链接")
        d.geometry("720x680")
        d.minsize(640, 560)
        d.resizable(True, True)
        d.transient(self)
        try:
            d.grab_set()
        except Exception:
            pass

        # 说明文字: 重要警示用红, 常规信息用中性色, 分点更清爽
        info_text = ("用法: 在下方『商品链接清单』里粘贴/输入 1688 商品链接(每行一个, 形如 "
                     "detail.1688.com/offer/xxxx.html), 也可直接填纯数字商品ID。\n"
                     "• 登录态(cookie+本地会话)仅保存在本地 data/1688_state.json, 不上传; 采集时自动复用同一浏览器引擎注入。\n"
                     "• 进货运费抓不到会标注『需补』; 也可点『批量导入』导入已保存的 1688 详情页(HTML)或含链接的 txt/csv/xlsx/json。")
        ctk.CTkLabel(d, text=info_text, justify="left", font=F(11),
                     text_color=("white" if ctk.get_appearance_mode() == "Dark" else "#333333")
                     ).pack(anchor="w", padx=12, pady=(8, 2))
        warn_text = ("★ 重要: 1688 风控极严,『扫码登录』常手机端一直『正在加载』(发出的二维码是无效握手码)。"
                     "强烈建议把上方『登录方式』切到【账号密码登录】, 验证全在电脑浏览器内完成, 不卡手机。\n"
                     "★ 登录成功后自动访问详情页捕获 x5sec 风控凭证(日志显示『已捕获 x5sec』即成功); "
                     "若弹滑块, 浏览器停留最多 2 分钟等你手动滑过, 别急着关窗口。")
        ctk.CTkLabel(d, text=warn_text, justify="left", font=F(11),
                     text_color="#eb5757").pack(anchor="w", padx=12, pady=(2, 4))

        # 登录方式切换: 扫码(默认) / 账号密码(更稳, 绕过手机端 loading)
        mode_frame = ctk.CTkFrame(d)
        mode_frame.pack(anchor="w", padx=12, pady=(2, 2))
        ctk.CTkLabel(mode_frame, text="登录方式:", font=F(12)).pack(side="left", padx=(2, 6))
        login_mode = ctk.StringVar(value="扫码登录")
        mode_seg = ctk.CTkSegmentedButton(mode_frame, values=["扫码登录", "账号密码登录"],
                                         variable=login_mode, width=200)
        mode_seg.pack(side="left", padx=2)

        # ---------- 主操作按钮栏(固定在上方, 始终可见, 避免被下方清单/日志挤出窗口) ----------
        action_bar = ctk.CTkFrame(d)
        action_bar.pack(fill="x", padx=12, pady=(4, 4))
        # 第 1 行: 核心动作
        row1 = ctk.CTkFrame(action_bar)
        row1.pack(fill="x", pady=(2, 2))
        ctk.CTkButton(row1, text="开始采集(在线)", width=140, height=32, command=lambda: do_collect()).pack(side="left", padx=6)
        ctk.CTkButton(row1, text="批量导入", width=120, height=32, command=lambda: do_import(),
                      fg_color="#9b51e0", hover_color="#7b3fc4").pack(side="left", padx=6)
        ctk.CTkButton(row1, text="登录1688(一次性)", width=160, height=32, command=lambda: do_login(),
                      fg_color="#2f80ed", hover_color="#1f5fbf").pack(side="left", padx=6)
        ctk.CTkButton(row1, text="关闭", width=90, height=32, command=d.destroy).pack(side="right", padx=6)
        # 第 2 行: 登录过程中的辅助动作(点『登录1688』后才用到)
        row2 = ctk.CTkFrame(action_bar)
        row2.pack(fill="x", pady=(2, 2))
        ctk.CTkButton(row2, text="我已登录", width=140, height=30,
                      command=lambda: stop_ev.set(),
                      fg_color="#1f9d55", hover_color="#157a40").pack(side="left", padx=6)
        ctk.CTkButton(row2, text="刷新二维码", width=140, height=30,
                      command=lambda: refresh_ev.set()).pack(side="left", padx=6)
        ctk.CTkLabel(row2, text="(登录时在弹出的浏览器里操作, 这两按钮用于在界面侧辅助)",
                     font=F(11), text_color="#888").pack(side="left", padx=6)

        # ---------- 实时状态条(顶部, 与日志同步刷新; 验证码等待时会显示倒计时, 不再像"卡死") ----------
        status_lbl = ctk.CTkLabel(d, text="就绪。点『登录1688(一次性)』或『开始采集(在线)』。",
                                  font=F(12, True), text_color="#2f80ed")
        status_lbl.pack(anchor="w", padx=12, pady=(2, 4))

        # ---------- 链接管理区(可滚动清单 + 批量粘贴 + 逐行删除 + 全部删除) ----------
        link_box = ctk.CTkFrame(d)
        link_box.pack(fill="x", padx=12, pady=(2, 4))

        # ① 标题行: 清单标题 + 全部删除
        head = ctk.CTkFrame(link_box)
        head.pack(fill="x", padx=4, pady=(2, 2))
        ctk.CTkLabel(head, text="① 商品链接清单", font=F(13, True)).pack(side="left", padx=2)
        ctk.CTkLabel(head, text="", textvariable=None).pack(side="left", fill="x", expand=True)
        cnt_lbl = ctk.CTkLabel(head, text="共 0 个", font=F(11), text_color="#2f80ed")
        cnt_lbl.pack(side="left", padx=6)
        ctk.CTkButton(head, text="全部删除", width=90, fg_color="#eb5757", hover_color="#c0392b",
                      font=F(11), command=lambda: clear_all()).pack(side="right", padx=4)

        # ② 批量粘贴区(支持一次粘贴多行) + 加入列表
        paste_tb = ctk.CTkTextbox(link_box, height=52, font=F(11))
        paste_tb.pack(fill="x", padx=4, pady=(0, 2))
        paste_tb.insert("0.0", "在此粘贴 1688 商品链接(每行一个, 可一次粘贴多个), 然后点『加入列表』\n"
                              "也支持纯商品ID, 例如 643237120099")
        paste_tb.configure(text_color="gray")
        paste_tb.bind("<FocusIn>", lambda e: _clear_paste_placeholder())

        addrow = ctk.CTkFrame(link_box)
        addrow.pack(fill="x", padx=4, pady=(0, 2))
        single_entry = ctk.CTkEntry(addrow, placeholder_text="或在此输入单个链接/ID, 回车或点『添加』")
        single_entry.pack(side="left", fill="x", expand=True, padx=(0, 4))
        ctk.CTkButton(addrow, text="添加", width=70, command=lambda: add_single()).pack(side="left", padx=2)
        ctk.CTkButton(addrow, text="加入列表 ▶", width=110, command=lambda: add_from_paste()).pack(side="left", padx=2)
        single_entry.bind("<Return>", lambda e: add_single())

        # ③ 可滚动链接清单
        list_scroll = ctk.CTkScrollableFrame(link_box, height=150, label_text="已添加链接(可双击编辑, 行尾✕删除本行)")
        list_scroll.pack(fill="x", padx=4, pady=(2, 4))

        # 内部状态
        link_rows = []   # [(frame, entry)]
        link_entries = []  # [entry] —— do_collect 直接读这些

        def _clear_paste_placeholder():
            if paste_tb.get("0.0", "end").startswith("在此粘贴"):
                paste_tb.delete("0.0", "end")
                paste_tb.configure(text_color=("white" if ctk.get_appearance_mode() == "Dark" else "black"))

        def _render():
            for fr, _ in link_rows:
                fr.destroy()
            link_rows.clear()
            link_entries.clear()
            if not urls:
                empty = ctk.CTkLabel(list_scroll, text="（暂无链接，请在上方粘贴/输入后加入）",
                                     text_color="gray", font=F(11))
                empty.pack(anchor="w", padx=6, pady=8)
                link_rows.append((empty, None))
            else:
                for idx, u in enumerate(urls, 1):
                    row = ctk.CTkFrame(list_scroll)
                    row.pack(fill="x", padx=2, pady=2)
                    ctk.CTkLabel(row, text=f"{idx:>3}.", width=30, font=F(11),
                                 text_color="#888").pack(side="left", padx=(2, 4))
                    ent = ctk.CTkEntry(row, font=F(11))
                    ent.insert(0, u)
                    ent.pack(side="left", fill="x", expand=True, padx=2)
                    link_entries.append(ent)
                    ctk.CTkButton(row, text="✕", width=30, fg_color="#eb5757", hover_color="#c0392b",
                                  font=F(11),
                                  command=lambda i=idx - 1: remove_one(i)).pack(side="right", padx=(2, 4))
                    link_rows.append((row, ent))
            cnt_lbl.configure(text=f"共 {len(urls)} 个")

        def _normalize(raw: str):
            raw = raw.strip().rstrip("/")
            if not raw:
                return None
            # 只接受 1688 商品链接 或 纯数字商品ID, 其余(cookie串/无关链接)忽略
            if raw.isdigit():
                return f"https://detail.1688.com/offer/{raw}.html"
            if raw.startswith("http") and "1688.com" in raw and "offer" in raw:
                # 去掉 ?查询参数/#锚点, 让同一商品(带/不带追踪参数)只算一行, 避免重复
                base = raw.split("?")[0].split("#")[0]
                return base
            return None  # 非 1688 链接忽略, 避免误加 cookie 串

        # 从任意文本中找出 1688 商品链接/ID(兼容杂乱格式: 带无关字符、逗号/空格混排、多行)
        _OFFER_RE = re.compile(r"detail\.1688\.com/offer/(\d+)", re.I)
        _ID_RE = re.compile(r"(?<!\d)(\d{8,})(?!\d)")  # 8 位及以上独立数字(1688 商品ID)

        def _find_links(text: str):
            """只认两类输入, 杜绝『凭空多出行』:
            ① 完整 1688 商品链接 —— 用 _OFFER_RE 仅取 /offer/<数字>, 忽略 ?查询参数;
            ② 独立数字 token —— 先把整段文本里的完整 URL 抹掉再扫数字,
               避免把 ?ptid/pid/订单号 等长数字误判成商品ID(这是『5变11』的主因)。"""
            out, seen = [], set()
            # 1) 优先从完整 1688 商品链接抽取 offer ID(只取 /offer/<数字>, 忽略 ?查询参数)
            for m in _OFFER_RE.finditer(text or ""):
                u = f"https://detail.1688.com/offer/{m.group(1)}.html"
                if u not in seen:
                    seen.add(u); out.append(u)
            # 2) 仅对『独立数字 token』(用户手敲/单列的商品ID) 接受;
            #    先把完整 URL 行去掉, 避免把 ?查询参数里的长数字(ptid/pid/订单号)误判成商品ID
            cleaned = re.sub(r"https?://\S+", " ", text or "")
            for m in _ID_RE.finditer(cleaned):
                u = f"https://detail.1688.com/offer/{m.group(1)}.html"
                if u not in seen:
                    seen.add(u); out.append(u)
            return out

        def add_from_paste():
            _clear_paste_placeholder()
            text = paste_tb.get("0.0", "end")
            added = 0
            for u in _find_links(text):
                if u not in urls:
                    urls.append(u)
                    added += 1
            paste_tb.delete("0.0", "end")
            _render()
            if added:
                dq.put(f"已加入 {added} 个链接（共 {len(urls)} 个）")
            else:
                dq.put("未检测到有效 1688 链接(支持 detail.1688.com 链接或纯数字商品ID)")

        def add_single():
            v = single_entry.get().strip().rstrip("/")
            single_entry.delete(0, "end")
            if not v:
                return
            for part in v.replace(",", " ").split():
                n = _normalize(part)
                if n and n not in urls:
                    urls.append(n)
            _render()

        def remove_one(i):
            if 0 <= i < len(urls):
                removed = urls.pop(i)
                _render()
                dq.put(f"已删除: {removed}")

        def clear_all():
            if urls:
                urls.clear()
                _render()
                dq.put("已清空全部链接")

        urls = []  # 不预置任何示例链接: 你粘贴/输入几个就显示几个, 避免『凭空多出行』
        _render()

        # 有头/无头开关: 1688 对无头浏览器更敏感, 无头全拦截时可勾选有头重试
        headful_var = ctk.BooleanVar(value=False)
        ctk.CTkCheckBox(d, text="有头采集(遇验证码需手动通过时务必勾选, 会弹出真实浏览器窗口)", variable=headful_var,
                       font=F(11)).pack(anchor="w", padx=12, pady=(2, 2))

        log = ctk.CTkTextbox(d, height=210)
        log.pack(fill="both", expand=True, padx=12, pady=4)

        dq = queue.Queue()  # 对话框专属日志队列: 后台线程 -> 主线程
        stop_ev = threading.Event()  # 用户点『我已登录』时置位, 让登录线程立即保存并退出
        refresh_ev = threading.Event()  # 用户点『刷新二维码』时置位, 重新打开登录页

        def log_msg(s):
            log.configure(state="normal")
            log.insert("end", s + "\n")
            log.configure(state="disabled")
            log.see("end")
            # 同步刷新顶部状态条, 让用户随时看到当前进度(尤其验证码等待倒计时), 不再以为卡死
            try:
                status_lbl.configure(text=(s[:90] + "…") if len(s) > 90 else s)
            except Exception:
                pass

        def pump_log():
            try:
                while True:
                    log_msg(dq.get_nowait())
            except queue.Empty:
                pass
            d.after(150, pump_log)
        pump_log()

        save_dir = (ROOT / "data").as_posix()

        def _finish(res):
            if not res:
                dq.put("未解析出任何商品")
                return
            out_path = os.path.join(save_dir, "collected_1688.json")
            existing = []
            if os.path.exists(out_path):
                try:
                    existing = json.load(open(out_path, encoding="utf-8"))
                except Exception:
                    existing = []
            merged = {r["item_id"]: r for r in existing}
            for r in res:
                merged[r["item_id"]] = r
            json.dump(list(merged.values()), open(out_path, "w", encoding="utf-8"),
                      ensure_ascii=False, indent=2)
            dq.put(f"完成: 成功 {len(res)} 个, 已存 data/collected_1688.json")
            # 并入主选品并刷新(走与『导入1688缓存』相同路径)
            self.products = loader.load_from_json_file(out_path)
            self._emit("loaded", f"采集导入 {len(self.products)} 个")
            if auto_listing:
                self._emit("listing", f"新链接采集完成，已自动生成 {len(res)} 条上架候选")

        def worker_online(links):
            dq.put("开始在线抓取(需 chromium 浏览器)…")
            try:
                res = collector.collect(links, headless=not headful_var.get(), save_dir=save_dir,
                                        progress=lambda k, m: dq.put(f"[{k}] {m}"))
            except Exception as e:
                dq.put(f"采集失败: {e}")
                dq.put("提示: 若报『Executable doesn't exist』, 请先在命令行执行:  playwright install chromium")
                return
            _finish(res)

        def worker_local(paths):
            dq.put(f"解析本地 HTML: {len(paths)} 个文件…")
            try:
                res = collector.collect_from_html_files(list(paths))
            except Exception as e:
                dq.put(f"解析失败: {e}")
                return
            _finish(res)

        def worker_login():
            prefer = "password" if login_mode.get() == "账号密码登录" else "qr"
            dq.put(f"打开真实浏览器登录1688({login_mode.get()})…")
            try:
                ok = collector.interactive_login(
                    progress=lambda k, m: dq.put(f"[{k}] {m}"),
                    stop_event=stop_ev, refresh_event=refresh_ev, prefer=prefer)
            except Exception as e:
                dq.put(f"登录失败: {e}")
                return
            dq.put("登录完成, 现在可以返回『开始采集(在线)』了" if ok else "登录未成功, 请重试")

        def do_collect():
            links = [e.get().strip().rstrip("/") for e in link_entries if e.get().strip()]
            if not links:
                dq.put("请先添加 1688 链接（在上方清单加入）")
                return
            threading.Thread(target=worker_online, args=(links,), daemon=True).start()

        def do_import():
            """批量导入: 支持 txt/csv/xlsx/json/html 多种格式, 自动抽取 1688 链接/商品ID。
            - txt/csv/xlsx/json: 抽取链接/ID 加入上方清单(供在线采集)
            - html/htm: 直接解析已保存的详情页并并入主选品(等价于原『选择本地HTML』)
            """
            paths = filedialog.askopenfilenames(
                title="批量导入 1688 链接(支持 txt/csv/xlsx/json/html)",
                filetypes=[("全部支持", "*.txt;*.csv;*.xlsx;*.xls;*.json;*.html;*.htm"),
                           ("文本/CSV", "*.txt;*.csv"),
                           ("Excel", "*.xlsx;*.xls"),
                           ("JSON", "*.json"),
                           ("本地HTML", "*.html;*.htm"),
                           ("All", "*.*")])
            if not paths:
                return
            html_paths, added = [], 0
            for p in paths:
                ext = p.lower().rsplit(".", 1)[-1]
                if ext in ("html", "htm"):
                    html_paths.append(p)
                    continue
                text = ""
                try:
                    if ext == "txt":
                        text = open(p, encoding="utf-8", errors="ignore").read()
                    elif ext == "csv":
                        import csv as _csv
                        with open(p, encoding="utf-8", errors="ignore", newline="") as f:
                            text = "\n".join(",".join("" if c is None else str(c) for c in row)
                                            for row in _csv.reader(f))
                    elif ext in ("xlsx", "xls"):
                        try:
                            import openpyxl
                            wb = openpyxl.load_workbook(p, read_only=True, data_only=True)
                            rows = []
                            for ws in wb.worksheets:
                                for r in ws.iter_rows(values_only=True):
                                    rows.append(" ".join("" if c is None else str(c) for c in r))
                            text = "\n".join(rows)
                        except Exception as e:
                            dq.put(f"读取Excel失败 {p}: {e}")
                            continue
                    elif ext == "json":
                        import json as _json
                        try:
                            data = _json.load(open(p, encoding="utf-8"))
                        except Exception as e:
                            dq.put(f"读取JSON失败 {p}: {e}")
                            continue
                        # 兼容多种 JSON 结构: 字符串列表 / 对象列表(取 url/link/item_id/offer)
                        # 或 {products/data/items/list:[...]}, 全部转为文本再统一抽取
                        def _walk(o):
                            if isinstance(o, str):
                                yield o
                            elif isinstance(o, list):
                                for x in o:
                                    yield from _walk(x)
                            elif isinstance(o, dict):
                                for k in ("url", "link", "offer_url", "item_url", "item_id", "offer", "id"):
                                    if o.get(k) is not None:
                                        yield str(o[k])
                                for v in o.values():
                                    yield from _walk(v)
                        text = "\n".join(_walk(data))
                    else:
                        text = open(p, encoding="utf-8", errors="ignore").read()
                    for u in _find_links(text):
                        if u not in urls:
                            urls.append(u)
                            added += 1
                except Exception as e:
                    dq.put(f"导入失败 {p}: {e}")
            if html_paths:
                threading.Thread(target=worker_local, args=(list(html_paths),), daemon=True).start()
            if added:
                _render()
                dq.put(f"批量导入: 已加入 {added} 个链接（共 {len(urls)} 个）")
            elif not html_paths:
                dq.put("批量导入: 未从文件中检测到有效 1688 链接/ID")

        def do_login():
            threading.Thread(target=worker_login, daemon=True).start()


if __name__ == "__main__":
    try:
        Workbench().mainloop()
    except Exception:
        # pythonw 无控制台, 把启动错误落盘并弹窗, 避免静默失败
        import traceback as _tb
        import os as _os
        _log = _os.path.join(_os.path.dirname(_os.path.abspath(__file__)), "..", "data", "launch_error.log")
        try:
            _os.makedirs(_os.path.dirname(_log), exist_ok=True)
            with open(_log, "w", encoding="utf-8") as _fh:
                _fh.write("拼多多工作台启动失败:\n" + _tb.format_exc())
        except Exception:
            pass
        try:
            import tkinter as _tk
            from tkinter import messagebox as _mb
            _r = _tk.Tk()
            _r.withdraw()
            _mb.showerror("启动失败", f"拼多多工作台启动出错, 详情见:\n{_log}\n\n{_tb.format_exc()[:900]}")
        except Exception:
            pass
