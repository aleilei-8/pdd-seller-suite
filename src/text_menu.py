#!/usr/bin/env python
# -*- coding: utf-8 -*-
"""全局右键文本菜单 —— 让工作台所有文本控件都能右键复制。

customtkinter 5.2.2 的 CTkTextbox / CTkEntry / CTkLabel 都没有内置右键菜单,
而它们内部其实是原生 tk.Text / tk.Entry / tk.Label, 因此用 tkinter 的
bind_class 在「类」级别绑定一次, 所有现有和后续新建的控件自动生效。

能力:
  - 文本框 (CTkTextbox): 复制选中 / 剪切 / 粘贴 / 全选 / 复制全部
  - 输入框 (CTkEntry):   复制 / 剪切 / 粘贴 / 全选
  - 标签   (CTkLabel):   复制文字(整段)
  - 只读报告文本框: 仍可复制/全选, 禁止剪切/粘贴

用法:
    import text_menu
    text_menu.install(root)   # 在 Workbench.__init__ 里调一次即可
"""
import tkinter as tk

_MENU = None
_INSTALLED = set()


# ---------- 工具 ----------
def _editable(widget):
    """控件是否可编辑(只读报告 state=disabled 时为 False)。"""
    try:
        return str(widget.cget("state")).lower() != "disabled"
    except Exception:
        return True


def _has_selection(widget, kind):
    try:
        if kind == "text":
            return bool(widget.tag_ranges("sel"))
        if kind == "entry":
            return bool(widget.selection_present())
    except Exception:
        pass
    return False


def _selected_text(widget, kind):
    try:
        if kind == "text":
            return widget.get("sel.first", "sel.last")
        if kind == "entry":
            return widget.selection_get()
    except Exception:
        pass
    return ""


def _all_text(widget, kind):
    try:
        if kind == "text":
            return widget.get("1.0", "end-1c")
        if kind == "entry":
            return widget.get()
        if kind == "label":
            return str(widget.cget("text") or "")
    except Exception:
        pass
    return ""


def _copy_to_clipboard(root, text):
    try:
        root.clipboard_clear()
        root.clipboard_append(text or "")
        root.update_idletasks()
        return True
    except Exception:
        return False


def _temp_normal(widget, fn):
    """只读文本框: 临时切 normal 执行操作(仅加 sel 标记,不改内容), 再恢复。"""
    restore = False
    try:
        if str(widget.cget("state")).lower() == "disabled":
            widget.configure(state="normal")
            restore = True
        return fn()
    finally:
        if restore:
            try:
                widget.configure(state="disabled")
            except Exception:
                pass


# ---------- 命令 ----------
def copy_selection(widget, kind, root=None):
    root = root or widget.winfo_toplevel()
    text = _selected_text(widget, kind)
    if not text:
        text = _all_text(widget, kind)
    return _copy_to_clipboard(root, text)


def copy_all(widget, kind, root=None):
    root = root or widget.winfo_toplevel()
    return _copy_to_clipboard(root, _all_text(widget, kind))


def cut_selection(widget, kind, root=None):
    root = root or widget.winfo_toplevel()
    if not _editable(widget) or not _has_selection(widget, kind):
        return False
    text = _selected_text(widget, kind)
    try:
        if kind == "text":
            widget.delete("sel.first", "sel.last")
        else:
            widget.delete("sel.first", "sel.last")
    except Exception:
        return False
    return _copy_to_clipboard(root, text)


def paste(widget, kind, root=None):
    root = root or widget.winfo_toplevel()
    if not _editable(widget):
        return False
    try:
        text = root.clipboard_get()
    except Exception:
        return False
    try:
        if kind == "text":
            widget.insert("insert", text)
        else:
            widget.insert("insert", text)
        return True
    except Exception:
        return False


def select_all(widget, kind):
    if kind == "text":
        def _do():
            widget.tag_add("sel", "1.0", "end-1c")
            return True
        return _temp_normal(widget, _do) if not _editable(widget) else _do()
    if kind == "entry":
        try:
            widget.select_range(0, "end")
            return True
        except Exception:
            pass
    return False


# ---------- 弹出菜单 ----------
def _build_menu(event, kind):
    global _MENU
    widget = event.widget
    root = widget.winfo_toplevel()
    if _MENU is None or _MENU.tk is not root.tk:
        _MENU = tk.Menu(root, tearoff=0)
    menu = _MENU
    menu.delete(0, "end")

    editable = _editable(widget)
    has_sel = _has_selection(widget, kind)

    if kind == "label":
        menu.add_command(label="复制文字", command=lambda: copy_all(widget, kind, root))
        menu.add_separator()
        menu.add_command(label="（标签文字只读）", state="disabled")
        return menu

    if kind == "text":
        menu.add_command(label="复制选中" if has_sel else "复制内容",
                         command=lambda: copy_selection(widget, kind, root))
        menu.add_command(label="复制全部", command=lambda: copy_all(widget, kind, root))
        if editable:
            menu.add_separator()
            menu.add_command(label="剪切", command=lambda: cut_selection(widget, kind, root))
            menu.add_command(label="粘贴", command=lambda: paste(widget, kind, root))
        else:
            menu.add_separator()
            menu.add_command(label="（只读，不可编辑）", state="disabled")
        menu.add_separator()
        menu.add_command(label="全选", command=lambda: select_all(widget, kind))
        return menu

    # entry
    menu.add_command(label="复制", command=lambda: copy_selection(widget, kind, root))
    if editable:
        menu.add_command(label="剪切", command=lambda: cut_selection(widget, kind, root))
        menu.add_command(label="粘贴", command=lambda: paste(widget, kind, root))
    menu.add_separator()
    menu.add_command(label="全选", command=lambda: select_all(widget, kind))
    return menu


def _popup(event, kind):
    try:
        menu = _build_menu(event, kind)
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        try:
            _MENU.grab_release()
        except Exception:
            pass


# ---------- 安装 ----------
def install(root):
    """给根窗口安装全局右键菜单; 重复调用无副作用。"""
    key = id(root)
    if key in _INSTALLED:
        return False
    root.bind_class("Text", "<Button-3>", lambda e: _popup(e, "text"))
    root.bind_class("Entry", "<Button-3>", lambda e: _popup(e, "entry"))
    root.bind_class("TEntry", "<Button-3>", lambda e: _popup(e, "entry"))
    root.bind_class("Label", "<Button-3>", lambda e: _popup(e, "label"))
    # 常用快捷键: 只读报告里 Ctrl+A 也能全选(Ctrl+C 原生即支持)
    root.bind_class("Text", "<Control-a>", lambda e: select_all(e.widget, "text"))
    root.bind_class("Entry", "<Control-a>", lambda e: select_all(e.widget, "entry"))
    _INSTALLED.add(key)
    return True


if __name__ == "__main__":
    # 轻量自测: 不弹窗, 直接验证命令函数
    r = tk.Tk()
    r.withdraw()
    install(r)

    t = tk.Text(r)
    t.insert("1.0", "第一行报告\n第二行报告")
    t.tag_add("sel", "1.0", "1.3")
    assert copy_selection(t, "text", r)
    assert r.clipboard_get() == "第一行"
    assert copy_all(t, "text", r)
    assert r.clipboard_get() == "第一行报告\n第二行报告"
    assert select_all(t, "text")

    t.configure(state="disabled")
    assert not _editable(t)
    assert copy_all(t, "text", r)
    assert select_all(t, "text")  # 只读也能全选
    assert not cut_selection(t, "text", r)
    assert not paste(t, "text", r)

    e = tk.Entry(r)
    e.insert(0, "hello world")
    e.select_range(0, 5)
    assert copy_selection(e, "entry", r)
    assert r.clipboard_get() == "hello"
    assert cut_selection(e, "entry", r)
    assert e.get() == " world"

    lab = tk.Label(r, text="标签文字可复制")
    assert copy_all(lab, "label", r)
    assert r.clipboard_get() == "标签文字可复制"

    r.destroy()
    print("text_menu selftest OK")
