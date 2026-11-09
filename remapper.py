# -*- coding: utf-8 -*-
"""
空中飞鼠 (Air Mouse) - 背面键盘 按键映射引擎

零第三方依赖，仅使用 Python 标准库：
  * WH_KEYBOARD_LL 全局低级键盘钩子  -> 捕获 / 拦截真实按键
  * SendInput                        -> 注入替换后的按键
  * http.server                      -> 本机 127.0.0.1 的 GUI 配置接口

启动：python remapper.py   （或直接双击 start.vbs）
"""

import atexit
import ctypes
import ctypes.wintypes as wt
import json
import os
import queue
import re
import socket
import subprocess
import sys
import threading
import time
import webbrowser
from collections import deque
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import urlparse, parse_qs

try:
    sys.stdout.reconfigure(encoding="utf-8", errors="replace")
except Exception:
    pass

BASE = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.path.join(BASE, "mappings.json")
LOG_PATH = os.path.join(BASE, "remapper.log")
HOST, PORT = "127.0.0.1", 8765

# ----------------------------------------------------------------------------
# 键码表
# ----------------------------------------------------------------------------

# AltGr（右 Alt）在 Windows 里常表现为「假 LCtrl + RAlt(带 EXT 标志)」，
# 这里用一个不与真实 VK 冲突的逻辑键码，把它和左 Alt / Ctrl+Alt 区分开。
ALTGR_VK = 0xE5

VK_NAMES = {}
for _c in range(ord("A"), ord("Z") + 1):
    VK_NAMES[chr(_c)] = _c
for _d in range(10):
    VK_NAMES[str(_d)] = 0x30 + _d
for _f in range(1, 25):
    VK_NAMES["F%d" % _f] = 0x6F + _f
VK_NAMES.update({
    "Esc": 0x1B, "Escape": 0x1B, "Enter": 0x0D, "Return": 0x0D,
    "Space": 0x20, "Tab": 0x09, "Backspace": 0x08, "Delete": 0x2E,
    "Insert": 0x2D, "Home": 0x24, "End": 0x23, "PgUp": 0x21, "PgDn": 0x22,
    "Up": 0x26, "Down": 0x28, "Left": 0x25, "Right": 0x27,
    "Ctrl": 0xA2, "Lctrl": 0xA2, "Rctrl": 0xA3,
    "Shift": 0xA0, "Lshift": 0xA0, "Rshift": 0xA1,
    "Alt": 0xA4, "Lalt": 0xA4, "Ralt": 0xA5,
    "Win": 0x5B, "Lwin": 0x5B, "Rwin": 0x5C, "Menu": 0x5D, "Apps": 0x5D,
    "AltGr": ALTGR_VK,
    "Capslock": 0x14, "Printscreen": 0x2C, "Numlock": 0x90, "Scrolllock": 0x91,
    "Pause": 0x13, "Grave": 0xC0, "`": 0xC0, "Minus": 0xBD, "-": 0xBD,
    "Equal": 0xBB, "=": 0xBB, "Lbracket": 0xDB, "[": 0xDB,
    "Rbracket": 0xDD, "]": 0xDD, "Backslash": 0xDC, "\\": 0xDC,
    "Semicolon": 0xBA, ";": 0xBA, "Quote": 0xDE, "'": 0xDE,
    "Comma": 0xBC, ",": 0xBC, "Period": 0xBE, ".": 0xBE,
    "Slash": 0xBF, "/": 0xBF,
    "Mute": 0xAD, "Voldown": 0xAE, "Volup": 0xAF,
    "Mediaplay": 0xB3, "Medianext": 0xB0, "Mediaprev": 0xB1, "Mediastop": 0xB2,
    "Browserback": 0xA6, "Browserforward": 0xA7, "Browserrefresh": 0xA8,
})
for _n in range(10):
    VK_NAMES["Numpad%d" % _n] = 0x60 + _n
VK_DISPLAY = {}
for _n, _v in VK_NAMES.items():
    VK_DISPLAY.setdefault(_v, _n)

MODS_BY_VK = {0x10: "shift", 0x11: "ctrl", 0x12: "alt", 0x5B: "win"}
MODS_ORDER = ["ctrl", "shift", "alt", "win"]
MODS_SEND_VK = {"ctrl": 0xA2, "shift": 0xA0, "alt": 0xA4, "win": 0x5B}


def norm_vk(vk):
    if vk in (0xA0, 0xA1):
        return 0x10
    if vk in (0xA2, 0xA3):
        return 0x11
    if vk in (0xA4, 0xA5):
        return 0x12
    if vk == 0x5C:
        return 0x5B
    return vk


def vk_name(vk):
    if vk in VK_DISPLAY:
        return VK_DISPLAY[vk]
    if vk in (0x10, 0x11, 0x12):
        return {0x10: "Shift", 0x11: "Ctrl", 0x12: "Alt"}[vk]
    return "VK 0x%02X" % vk


# ----------------------------------------------------------------------------
# USB HID Usage 推断
# WH_KEYBOARD_LL 只能提供 Windows 转换后的 vkCode / scanCode / flags，
# 拿不到 USB HID 的原始 8 字节 report；下面的 Usage / report 是按 HID 1.12
# 的 Keyboard-Keypad(0x07)、Consumer(0x0C) 页由 VK 推算的，仅供对照。
# ----------------------------------------------------------------------------

HID_USAGE = {}
for _i, _c in enumerate("abcdefghijklmnopqrstuvwxyz"):
    HID_USAGE[0x41 + _i] = (0x07, 0x04 + _i, "Keyboard " + _c)
for _i in range(9):
    HID_USAGE[0x31 + _i] = (0x07, 0x1E + _i, "Keyboard %d" % (_i + 1))
HID_USAGE[0x30] = (0x07, 0x27, "Keyboard 0")
for _i in range(12):                       # F1..F12  -> 0x3A..0x45
    HID_USAGE[0x70 + _i] = (0x07, 0x3A + _i, "Keyboard F%d" % (_i + 1))
for _i in range(12):                       # F13..F24 -> 0x68..0x73
    HID_USAGE[0x7C + _i] = (0x07, 0x68 + _i, "Keyboard F%d" % (_i + 13))
for _i in range(9):
    HID_USAGE[0x61 + _i] = (0x07, 0x59 + _i, "Keypad %d" % (_i + 1))
HID_USAGE[0x60] = (0x07, 0x62, "Keypad 0")
HID_USAGE.update({
    0x0D: (0x07, 0x28, "Keyboard Return"), 0x1B: (0x07, 0x29, "Keyboard Escape"),
    0x08: (0x07, 0x2A, "Keyboard Backspace"), 0x09: (0x07, 0x2B, "Keyboard Tab"),
    0x20: (0x07, 0x2C, "Keyboard Spacebar"),
    0xBD: (0x07, 0x2D, "Keyboard Minus"), 0xBB: (0x07, 0x2E, "Keyboard Equal"),
    0xDB: (0x07, 0x2F, "Keyboard LeftBracket"), 0xDD: (0x07, 0x30, "Keyboard RightBracket"),
    0xDC: (0x07, 0x31, "Keyboard Backslash"), 0xBA: (0x07, 0x33, "Keyboard Semicolon"),
    0xDE: (0x07, 0x34, "Keyboard Apostrophe"), 0xC0: (0x07, 0x35, "Keyboard Grave"),
    0xBC: (0x07, 0x36, "Keyboard Comma"), 0xBE: (0x07, 0x37, "Keyboard Period"),
    0xBF: (0x07, 0x38, "Keyboard Slash"), 0x14: (0x07, 0x39, "Keyboard CapsLock"),
    0x2D: (0x07, 0x49, "Keyboard Insert"), 0x24: (0x07, 0x4A, "Keyboard Home"),
    0x21: (0x07, 0x4B, "Keyboard PageUp"), 0x2E: (0x07, 0x4C, "Keyboard Delete"),
    0x23: (0x07, 0x4D, "Keyboard End"), 0x22: (0x07, 0x4E, "Keyboard PageDown"),
    0x27: (0x07, 0x4F, "Keyboard RightArrow"), 0x25: (0x07, 0x50, "Keyboard LeftArrow"),
    0x28: (0x07, 0x51, "Keyboard DownArrow"), 0x26: (0x07, 0x52, "Keyboard UpArrow"),
    0x90: (0x07, 0x53, "Keypad NumLock"), 0x6F: (0x07, 0x54, "Keypad Slash"),
    0x6A: (0x07, 0x55, "Keypad Asterisk"), 0x6D: (0x07, 0x56, "Keypad Minus"),
    0x6B: (0x07, 0x57, "Keypad Plus"),
    0xA0: (0x07, 0xE1, "Keyboard LeftShift"), 0xA1: (0x07, 0xE5, "Keyboard RightShift"),
    0xA2: (0x07, 0xE0, "Keyboard LeftControl"), 0xA3: (0x07, 0xE4, "Keyboard RightControl"),
    0xA4: (0x07, 0xE2, "Keyboard LeftAlt"), 0xA5: (0x07, 0xE6, "Keyboard RightAlt"),
    0x5B: (0x07, 0xE3, "Keyboard LeftGUI"), 0x5C: (0x07, 0xE7, "Keyboard RightGUI"),
    0x5D: (0x07, 0x65, "Keyboard Application"),
    0x2C: (0x07, 0x46, "Keyboard PrintScreen"), 0x91: (0x07, 0x47, "Keyboard ScrollLock"),
    0x13: (0x07, 0x48, "Keyboard Pause"),
    0xAD: (0x0C, 0x00E2, "Consumer Mute"), 0xAE: (0x0C, 0x00EA, "Consumer VolumeDown"),
    0xAF: (0x0C, 0x00E9, "Consumer VolumeUp"), 0xB3: (0x0C, 0x00CD, "Consumer PlayPause"),
    0xB0: (0x0C, 0x00B5, "Consumer NextTrack"), 0xB1: (0x0C, 0x00B6, "Consumer PrevTrack"),
    0xB2: (0x0C, 0x00B7, "Consumer Stop"),
    0xA6: (0x0C, 0x0224, "Consumer AC Back"), 0xA7: (0x0C, 0x0225, "Consumer AC Forward"),
    0xA8: (0x0C, 0x0227, "Consumer AC Refresh"),
})

MSG_NAMES = {0x0100: "WM_KEYDOWN", 0x0101: "WM_KEYUP",
             0x0104: "WM_SYSKEYDOWN", 0x0105: "WM_SYSKEYUP"}


def hid_info(raw_vk, ext, mods, released):
    """由 VK + 扩展键标志推断 HID Usage，并重建一份等效的 8 字节键盘报告。"""
    page, usage, name = HID_USAGE.get(raw_vk, (None, None, None))
    if raw_vk == 0x0D and ext:
        page, usage, name = 0x07, 0x58, "Keypad Enter"
    elif raw_vk in (0x10, 0x11, 0x12):
        left_u, right_u, label = {0x10: (0xE1, 0xE5, "Shift"),
                                  0x11: (0xE0, 0xE4, "Control"),
                                  0x12: (0xE2, 0xE6, "Alt")}[raw_vk]
        page, usage = 0x07, right_u if ext else left_u
        name = "Keyboard " + ("Right" if ext else "Left") + label
    mod = 0
    for _m, _bit in (("ctrl", 0x01), ("shift", 0x02), ("alt", 0x04), ("win", 0x08)):
        if _m in mods:
            mod |= _bit
    if page is None:
        return {"page": "—", "usage": "—", "name": "未收录的 VK（可能是厂商自定义键）",
                "mod": "0x%02X" % mod, "report": None}
    report = None
    if page == 0x07:
        rep = [mod, 0x00, 0 if released else (usage & 0xFF)] + [0] * 5
        report = " ".join("%02X" % _b for _b in rep)
    return {"page": "0x%02X" % page, "usage": "0x%04X" % usage, "name": name,
            "mod": "0x%02X" % mod, "report": report}


def raw_fields(kb, w_param, mods):
    """把 KBDLLHOOKSTRUCT 的原始字段一字不改地记下来，供事件流展示。"""
    f = int(kb.flags)
    raw_vk, scan = int(kb.vkCode), int(kb.scanCode)
    # 少数驱动在 keyup 时不一定置 LLKHF_UP，用窗口消息兜底
    released = bool(f & 0x80) or int(w_param) in (WM_KEYUP, WM_SYSKEYUP)
    return {
        "vk": "0x%02X" % raw_vk,
        "scan": "0x%02X" % scan,
        "flags": "0x%02X" % f,
        "ext": bool(f & 0x01),
        "lowerIL": bool(f & 0x02),
        "injected": bool(f & 0x10),
        "altdown": bool(f & 0x20),
        "released": released,
        "msg": "0x%04X" % int(w_param),
        "msgName": MSG_NAMES.get(int(w_param), "WM_?"),
        "time": int(kb.time),
        "extra": "0x%X" % int(kb.dwExtraInfo),
        "hid": hid_info(raw_vk, bool(f & 0x01), mods, released),
    }


def display_trigger(t):
    mods = [m.capitalize() if m != "win" else "Win" for m in MODS_ORDER if m in t.get("mods", [])]
    parts = mods + [t.get("name") or vk_name(t.get("vk", 0))]
    return " + ".join(parts)


# ----------------------------------------------------------------------------
# 手写触发条件解析
# 录制不是唯一入口：用户可以直接输入 Ctrl+Win+Left / AltGr / Q / 0x51 /
# usage 0x0014，解析成与 capture_now() 同构的 trigger，find_rule() 无需改动。
# ----------------------------------------------------------------------------

# 常见别名（Windows / macOS 两种叫法、左右修饰键、缩写）
TRIGGER_ALIASES = {
    "control": "Ctrl", "ctrlleft": "Lctrl", "ctrlright": "Rctrl",
    "shiftleft": "Lshift", "shiftright": "Rshift",
    "altleft": "Lalt", "altgraph": "AltGr", "alt gr": "AltGr", "alt-gr": "AltGr",
    "windows": "Win", "command": "Win", "cmd": "Win", "super": "Win",
    "meta": "Win", "gui": "Win", "winkey": "Win",
    "del": "Delete", "esc": "Esc", "return": "Enter", "spacebar": "Space",
    "pageup": "PgUp", "pagedown": "PgDn", "prtsc": "Printscreen",
    "arrowup": "Up", "arrowdown": "Down", "arrowleft": "Left", "arrowright": "Right",
    "plus": "Equal", "minus": "Minus", "volumeup": "Volup", "volumedown": "Voldown",
}

# HID Usage -> VK 反查，方便照着事件流里的「HID Page / Usage」直接手写
_HID_TO_VK = {}
for _vk, (_page, _usage, _label) in HID_USAGE.items():
    _HID_TO_VK.setdefault((_page, _usage), _vk)
for _u, _vk in {0xE0: 0xA2, 0xE1: 0xA0, 0xE2: 0xA4, 0xE3: 0x5B,
                0xE4: 0xA3, 0xE5: 0xA1, 0xE6: 0xA5, 0xE7: 0x5C,
                0x58: 0x0D}.items():
    _HID_TO_VK.setdefault((0x07, _u), _vk)


def _norm_token(s):
    return re.sub(r"[\s_]", "", str(s).lower())


_TRIG_NAME_LOOKUP = {}
for _n, _v in VK_NAMES.items():
    _TRIG_NAME_LOOKUP.setdefault(_norm_token(_n), _v)
for _a, _n in TRIGGER_ALIASES.items():
    _TRIG_NAME_LOOKUP.setdefault(_norm_token(_a), VK_NAMES[_n])
# Windows 上「右 Alt」就是 AltGr，统一到逻辑键 0xE5，别让手写和录制结果对不上
_TRIG_NAME_LOOKUP["ralt"] = ALTGR_VK
_TRIG_NAME_LOOKUP["rightalt"] = ALTGR_VK
_TRIG_NAME_LOOKUP["altgr"] = ALTGR_VK

_EXT_RAW_VKS = {0xA1, 0xA3, 0xA5, 0x5C, 0x21, 0x22, 0x23, 0x24, 0x25, 0x26,
                0x27, 0x28, 0x2D, 0x2E, 0x2C, 0x90, 0x13, 0x6F, 0x0D}


def _parse_atom(tok):
    """把单个 token 解析成 (vk, 备注)；出错返回 ('BAD', 原因)。"""
    s = str(tok).strip()
    n = _norm_token(s)
    if n in ("fn", "fnkey", "function"):
        return ("FN", "")
    m = re.match(r"^(?:usage|hid)\s*(?:0x([0-9a-f]{1,2})\s*[/:]\s*)?0x([0-9a-f]{1,4})$",
                 s, re.I)
    if m:
        page = int(m.group(1), 16) if m.group(1) else 0x07
        usage = int(m.group(2), 16)
        vk = _HID_TO_VK.get((page, usage))
        if vk is None and page == 0x07:
            for (_p, _u), _v in _HID_TO_VK.items():
                if _u == usage:
                    vk, page = _v, _p
                    break
        if vk is None:
            return ("BAD", "HID Page 0x%02X / Usage 0x%04X 没有对应的虚拟键" % (page, usage))
        return (vk, "HID 0x%02X/0x%04X → VK 0x%02X" % (page, usage, vk))
    m = re.match(r"^(?:vk|vkey|keycode)?\s*[:#]?\s*0x([0-9a-f]{1,4})$", s, re.I)
    if m:
        vk = int(m.group(1), 16)
        return (vk, "十六进制 VK 0x%02X" % vk)
    m = re.match(r"^(?:vk|vkey|keycode)\s*[:#]?\s*(\d{1,3})$", s, re.I)
    if m:
        vk = int(m.group(1))
        return (vk, "十进制 VK %d" % vk)
    if n in _TRIG_NAME_LOOKUP:                 # 单个数字 1~9 优先当数字键
        return (_TRIG_NAME_LOOKUP[n], "")
    if re.match(r"^\d{2,3}$", s):              # 两位以上纯数字按十进制 VK
        vk = int(s)
        return (vk, "十进制 VK %d" % vk)
    return ("BAD", "不认识的键名「%s」" % s)


def parse_trigger_text(text, any_mods=False, scan=0):
    """手写组合 -> trigger。返回 (trigger|None, 详情, 错误)。"""
    raw = str(text or "").strip()
    if not raw:
        return None, "", "请输入按键组合，例如 Ctrl+Win+Left、AltGr、Q 或 0x51"
    toks = [t.strip() for t in re.split(r"[+,，、;；]", raw) if t.strip()]
    if not toks:
        return None, "", "没读到按键"
    atoms, notes = [], []
    for tok in toks:
        val, note = _parse_atom(tok)
        if val == "FN":
            return None, "", ("Fn 是空中飞鼠内部的层切换键，Windows 收不到它。"
                              "请点「录制实际按法」按一次 Fn+白键，看事件流里实际发出的"
                              "组合（通常是 Ctrl/Alt + 字母），再把那个组合手写进来。")
        if val == "BAD":
            return None, "", (note + "。可用 Ctrl / Shift / Alt / Win / AltGr / Q / Left "
                              "等键名，或直接写 0x51、VK 229、usage 0x0014")
        if not (0 < val <= 0xFF):
            return None, "", "VK 需要在 0x01～0xFF 之间"
        atoms.append((tok, val))
        if note:
            notes.append(note)

    mods = set()
    for tok, vk in atoms[:-1]:
        mod = MODS_BY_VK.get(norm_vk(vk))
        if not mod:
            return None, "", ("「%s」不是修饰键；修饰键请写在前面，最后一个写主键，"
                              "例如 Ctrl+Alt+Q" % tok)
        mods.add(mod)

    main_tok, main_raw = atoms[-1]
    main_vk = ALTGR_VK if main_raw == 0xA5 else norm_vk(main_raw)
    mods_list = [m for m in MODS_ORDER if m in mods]
    try:
        scan_val = int(str(scan), 0) & 0xFF
    except Exception:
        scan_val = 0
    ext = main_raw in _EXT_RAW_VKS
    hid = hid_info(0xA5 if main_raw == ALTGR_VK else main_raw,
                   ext, set(mods), False)
    trig = {
        "vk": main_vk, "rawVk": main_raw, "scan": scan_val,
        "mods": mods_list, "name": vk_name(main_vk),
        "any_mods": bool(any_mods), "ext": ext,
        "source": "manual", "input": raw, "hid": hid,
    }
    detail = "VK 0x%02X · Scan 0x%02X · Mods %s · HID Page %s Usage %s（%s）" % (
        main_raw, scan_val, "+".join(m.capitalize() for m in mods_list) or "无",
        hid["page"], hid["usage"], hid["name"])
    if hid.get("report"):
        # 不换行空格：8 字节报告作为一个整体，避免在窄侧栏里被拆成孤立的「00」
        detail += " · Report " + hid["report"].replace(" ", "\u00a0")
    if notes:
        detail += " · " + "，".join(notes)
    return trig, detail, ""


def display_action(a):
    if not a:
        return "未设置"
    if a.get("display"):
        return a["display"]
    typ = a.get("type", "keys")
    if typ == "none":
        return "禁用（什么都不做）"
    if typ == "text":
        return "输入文本：" + a.get("text", "")
    if typ == "program":
        return "启动：" + a.get("path", "")
    if typ == "keys":
        suffix = "（按住生效）" if a.get("hold") else ""
    return " + ".join(vk_name(v) for v in a.get("keys", [])) + suffix
    return typ


# GUI 里「替换为」的预设
def preset(label, *names):
    return {"label": label, "action": {"type": "keys", "keys": [VK_NAMES[n] for n in names], "display": label}}


def hold_preset(label, name):
    """把原键换成修饰键：原键按住时目标键保持按下，松开才抬起（可用 Win+R 这类组合）。"""
    return {"label": label, "action": {"type": "keys", "keys": [VK_NAMES[name]],
                                       "hold": True, "display": label}}


CATALOG = [
    {"name": "修饰键 / Win", "items": [
        hold_preset("Win 键（按住生效）", "Win"),
        hold_preset("Ctrl 键（按住生效）", "Ctrl"),
        hold_preset("Alt 键（按住生效）", "Alt"),
        hold_preset("Shift 键（按住生效）", "Shift"),
        preset("Win+R 运行", "Win", "R"), preset("Win+E 资源管理器", "Win", "E"),
        preset("Win+I 设置", "Win", "I"), preset("Win+L 锁屏", "Win", "L"),
        preset("Win+Tab 任务视图", "Win", "Tab"), preset("Win+D 显示桌面", "Win", "D"),
    ]},
    {"name": "常用导航", "items": [
        preset("Esc", "Esc"), preset("Enter", "Enter"), preset("Backspace", "Backspace"),
        preset("Delete", "Delete"), preset("Tab", "Tab"), preset("Space", "Space"),
        preset("Home", "Home"), preset("End", "End"), preset("PgUp", "PgUp"),
        preset("PgDn", "PgDn"), preset("↑ 上", "Up"), preset("↓ 下", "Down"),
        preset("← 左", "Left"), preset("→ 右", "Right"), preset("Insert", "Insert"),
        preset("CapsLock", "Capslock"),
    ]},
    {"name": "编辑快捷键", "items": [
        preset("Ctrl+C 复制", "Ctrl", "C"), preset("Ctrl+V 粘贴", "Ctrl", "V"),
        preset("Ctrl+X 剪切", "Ctrl", "X"), preset("Ctrl+Z 撤销", "Ctrl", "Z"),
        preset("Ctrl+A 全选", "Ctrl", "A"), preset("Ctrl+S 保存", "Ctrl", "S"),
        preset("Ctrl+F 查找", "Ctrl", "F"), preset("Ctrl+W 关闭", "Ctrl", "W"),
        preset("Ctrl+Shift+Z 重做", "Ctrl", "Shift", "Z"),
        preset("Alt+Tab 切换窗口", "Alt", "Tab"), preset("Win+D 显示桌面", "Win", "D"),
        preset("Win+V 剪贴板", "Win", "V"), preset("Ctrl+Alt+Del", "Ctrl", "Alt", "Delete"),
    ]},
    {"name": "功能键 / 媒体", "items": [
        preset("F1", "F1"), preset("F2", "F2"), preset("F3", "F3"), preset("F4", "F4"),
        preset("F5", "F5"), preset("F6", "F6"), preset("F7", "F7"), preset("F8", "F8"),
        preset("F9", "F9"), preset("F10", "F10"), preset("F11", "F11"), preset("F12", "F12"),
        preset("音量 +", "Volup"), preset("音量 -", "Voldown"), preset("静音", "Mute"),
        preset("播放 / 暂停", "Mediaplay"), preset("下一曲", "Medianext"), preset("上一曲", "Mediaprev"),
        preset("PrintScreen 截图", "Printscreen"),
    ]},
    {"name": "字母数字", "items": [
        preset(c, c) for c in "ABCDEFGHIJKLMNOPQRSTUVWXYZ0123456789"
    ]},
]

# ----------------------------------------------------------------------------
# 状态
# ----------------------------------------------------------------------------


def default_config():
    return {"version": 1, "enabled": False, "rules": [], "labels": {}, "capture": True}


def normalize_trigger(t):
    """手写 / 导入的触发条件统一成规范结构，避免非法 vk 让钩子回调抛异常。"""
    if not isinstance(t, dict):
        return {}
    out = dict(t)
    try:
        out["vk"] = int(t.get("vk") or 0)
    except (TypeError, ValueError):
        try:
            out["vk"] = int(str(t.get("vk")), 0)
        except Exception:
            out["vk"] = 0
    ms = t.get("mods") or []
    out["mods"] = [m for m in MODS_ORDER if isinstance(ms, (list, tuple)) and m in ms]
    out["any_mods"] = bool(t.get("any_mods"))
    if not out.get("name"):
        out["name"] = vk_name(norm_vk(out["vk"]))
    return out


def normalize_rule(r):
    if not isinstance(r, dict):
        return r
    out = dict(r)
    out["trigger"] = normalize_trigger(r.get("trigger") or {})
    return out


def normalize_config(data):
    """统一构造配置，避免导入 / 保存时把「键盘捕获开关」字段弄丢。"""
    return {"version": 1,
            "enabled": bool(data.get("enabled")),
            "rules": [normalize_rule(r) for r in (data.get("rules") or [])],
            "labels": data.get("labels") or {},
            "capture": bool(data.get("capture", STATE.config.get("capture", True)))}


class State:
    def __init__(self):
        self.lock = threading.RLock()
        self.config = default_config()
        self.recording = None            # {"mode": "trigger"|"output", "slot":..., "t0":..., "last_mod":...}
        self.capture_seq = 0
        self.capture = None              # 最近一次录制结果 {"seq":..,"mode":..,"trigger":..}
        self.events = deque(maxlen=80)
        self.pressed = set()            # 当前按下的标准化 vk
        self.suppressed = set()         # 被拦截、等待 keyup 的 vk
        self.hold_outputs = {}          # vk -> [保持按下的替换键]，原键松开时抬起
        self.ghost_ms = -1              # 最近一次「全新按下」的 LCtrl 时间
        self.ghost_fresh = False        # 该 LCtrl 是否为全新按下（排除自动重复 / 已按住的 Ctrl）
        self.peak_chord = 0             # 本次运行同时按住过的最大键数，用于诊断硬件多键回滚
        self.chord_history = deque(maxlen=8)
        self.log("engine initialised")

    def log(self, msg):
        line = "%s  %s" % (time.strftime("%H:%M:%S"), msg)
        try:
            with open(LOG_PATH, "a", encoding="utf-8") as fh:
                fh.write(line + "\n")
        except Exception:
            pass
        try:
            print(line, flush=True)
        except Exception:
            pass

    def load(self):
        if not os.path.exists(CONFIG_PATH):
            return
        try:
            with open(CONFIG_PATH, "r", encoding="utf-8") as fh:
                data = json.load(fh)
            if isinstance(data, dict) and isinstance(data.get("rules"), list):
                self.config = normalize_config(data)
                self.log("config loaded: %d rules, capture=%s" %
                         (len(self.config["rules"]), self.config["capture"]))
        except Exception as exc:
            self.log("config load failed: %s" % exc)

    def save(self):
        tmp = CONFIG_PATH + ".tmp"
        with open(tmp, "w", encoding="utf-8") as fh:
            json.dump(self.config, fh, ensure_ascii=False, indent=2)
        os.replace(tmp, CONFIG_PATH)

    def add_event(self, **kw):
        kw["t"] = time.strftime("%H:%M:%S")
        with self.lock:
            self.events.append(kw)


STATE = State()

# ----------------------------------------------------------------------------
# Win32：SendInput
# ----------------------------------------------------------------------------

user32 = ctypes.WinDLL("user32", use_last_error=True)
kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)

INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_UNICODE = 0x0004
LLMHF_INJECTED = 0x0010
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012

# 内部补发「抬起」用的 ExtraInformation 标记：钩子看到后直接放行，不写进事件流
CLEANUP_MARK = 0x4D524B55  # "MRKU"

# 需要检测 / 补发抬起的修饰键。右 Alt / 右 Ctrl / Win 等属于扩展键，补发时要带 EXTENDEDKEY。
STUCK_MODS = [
    (0xA0, "LShift", False), (0xA1, "RShift", False),
    (0xA2, "LCtrl", False),  (0xA3, "RCtrl", True),
    (0xA4, "LAlt", False),   (0xA5, "RAlt (AltGr)", True),
    (0x5B, "LWin", True),    (0x5C, "RWin", True),
    (0x10, "Shift", False),  (0x11, "Ctrl", False), (0x12, "Alt", False),
]


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wt.LONG), ("dy", wt.LONG), ("mouseData", wt.DWORD),
                ("dwFlags", wt.DWORD), ("time", wt.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wt.DWORD), ("wParamL", wt.WORD), ("wParamH", wt.WORD)]


class INPUTUNION(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", INPUTUNION)]


class KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wt.DWORD), ("scanCode", wt.DWORD), ("flags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ctypes.c_size_t)]


def make_key_input(vk, up=False, unicode_char=None, ext=False, extra=0):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    if unicode_char is not None:
        inp.u.ki.wVk = 0
        inp.u.ki.wScan = ord(unicode_char) & 0xFFFF
        inp.u.ki.dwFlags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
    else:
        inp.u.ki.wVk = vk & 0xFFFF
        inp.u.ki.wScan = 0
        inp.u.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
        if ext:
            inp.u.ki.dwFlags |= KEYEVENTF_EXTENDEDKEY
    inp.u.ki.dwExtraInfo = extra
    return inp


def send_inputs(items):
    if not items:
        return
    arr = (INPUT * len(items))(*items)
    user32.SendInput(len(items), ctypes.cast(arr, ctypes.c_void_p), ctypes.sizeof(INPUT))


def send_keys(vks):
    items = [make_key_input(v) for v in vks]
    items += [make_key_input(v, up=True) for v in reversed(vks)]
    send_inputs(items)


ACTION_QUEUE = queue.Queue()


def perform_action(action, release_mods):
    typ = action.get("type", "keys")
    hold = bool(action.get("hold"))
    # 普通替换要先松开触发组合里按住的修饰键，避免污染注入的目标键；
    # 但「换成修饰键」(hold) 必须保留它们，否则 Ctrl+AltGr→Win 会把 Ctrl 拆掉。
    if release_mods and not hold:
        send_inputs([make_key_input(MODS_SEND_VK[m], up=True)
                     for m in MODS_ORDER if m in release_mods])
    if typ == "keys":
        if hold:
            # 只发按下；抬起由原键 keyup 时负责（见 hook_callback）
            send_inputs([make_key_input(int(v)) for v in action.get("keys", [])])
        else:
            send_keys([int(v) for v in action.get("keys", [])])
    elif typ == "text":
        for ch in action.get("text", ""):
            send_inputs([make_key_input(0, unicode_char=ch),
                         make_key_input(0, up=True, unicode_char=ch)])
    elif typ == "program":
        try:
            subprocess.Popen(action.get("path", ""), shell=True)
        except Exception as exc:
            STATE.log("launch failed: %s" % exc)
    elif typ == "none":
        pass


def action_worker():
    while True:
        try:
            job = ACTION_QUEUE.get()
            if job is None:
                return
            perform_action(job[0], job[1])
        except Exception as exc:
            STATE.log("action failed: %s" % exc)


# ----------------------------------------------------------------------------
# Win32：低级键盘钩子
# ----------------------------------------------------------------------------

HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int, wt.WPARAM, wt.LPARAM)

# 64 位下必须显式声明句柄类型，否则默认 c_int 会截断指针
user32.SetWindowsHookExW.argtypes = [ctypes.c_int, HOOKPROC, ctypes.c_void_p, wt.DWORD]
user32.SetWindowsHookExW.restype = ctypes.c_void_p
user32.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int, wt.WPARAM, wt.LPARAM]
user32.CallNextHookEx.restype = ctypes.c_ssize_t
user32.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
user32.GetMessageW.argtypes = [ctypes.POINTER(wt.MSG), ctypes.c_void_p, wt.UINT, wt.UINT]
user32.TranslateMessage.argtypes = [ctypes.POINTER(wt.MSG)]
user32.DispatchMessageW.argtypes = [ctypes.POINTER(wt.MSG)]
user32.PostThreadMessageW.argtypes = [wt.DWORD, wt.UINT, wt.WPARAM, wt.LPARAM]
user32.SendInput.argtypes = [wt.UINT, ctypes.c_void_p, ctypes.c_int]
user32.SendInput.restype = wt.UINT
user32.GetAsyncKeyState.argtypes = [ctypes.c_int]
user32.GetAsyncKeyState.restype = ctypes.c_short
kernel32.GetCurrentThreadId.restype = wt.DWORD

_hook_handle = None            # None = 钩子未安装（键盘捕获关闭）
_hook_thread_id = None
_hook_thread_obj = None
_hook_proc_ref = None
_hook_gen = 0                  # 钩子线程代号，防止旧线程退出时清掉新装的钩子
_hook_lock = threading.Lock()
_hook_ready = threading.Event()


def stuck_mods():
    """返回当前系统异步状态里仍处于按下状态的修饰键。"""
    out = []
    for vk, name, ext in STUCK_MODS:
        try:
            if user32.GetAsyncKeyState(vk) & 0x8000:
                out.append((vk, name, ext))
        except Exception:
            pass
    return out


def release_stuck_mods():
    """补发抬起，解除卡在系统里的修饰键（AltGr 卡住会让所有按键都变成 AltGr+某键）。"""
    mods = stuck_mods()
    if not mods:
        return []
    items = [make_key_input(vk, up=True, ext=ext, extra=CLEANUP_MARK)
             for vk, _name, ext in mods]
    send_inputs(items)
    names = []
    for _vk, name, _ext in mods:
        if name not in names and len(names) < 4:
            names.append(name)
    STATE.log("released stuck modifiers: %s" % (", ".join(names) or "none"))
    return names


def is_mod(vk):
    return vk in MODS_BY_VK


def current_mods(pressed):
    return set(m for v, m in MODS_BY_VK.items() if v in pressed)


def hold_active_mods():
    """程序注入并保持着的目标修饰键，参与后续按键的 mods 判定（例如 Win+R）。"""
    mods = set()
    for keys in list(STATE.hold_outputs.values()):
        for k in keys:
            m = MODS_BY_VK.get(norm_vk(int(k)))
            if m:
                mods.add(m)
    return mods


def note_chord():
    """记录本次同时按住的键数与组合，用来判断空中飞鼠到底能不能发出三键。"""
    real = sorted(STATE.pressed)
    injected = [int(k) for ks in STATE.hold_outputs.values() for k in ks]
    # 峰值按「真实到达钩子的物理键」计数：被替换的原键算 1 个，注入键不再重复计，
    # 这样才能准确判断空中飞鼠硬件有没有把第三个键发出来。
    count = len(real)
    if count > STATE.peak_chord:
        STATE.peak_chord = count
    label = " + ".join([vk_name(v) for v in real] +
                       [vk_name(v) + "(注入)" for v in injected])
    if not label:
        return
    if not STATE.chord_history or STATE.chord_history[-1]["chord"] != label:
        STATE.chord_history.append({"t": time.strftime("%H:%M:%S"),
                                    "n": count, "chord": label})


def find_rule(vk, mods):
    cfg = STATE.config
    if not cfg.get("enabled"):
        return None
    passthrough = None
    for rule in cfg.get("rules", []):
        if not rule.get("active", True):
            continue
        trig = rule.get("trigger") or {}
        if not trig.get("vk") or norm_vk(int(trig["vk"])) != vk:
            continue
        if set(trig.get("mods", [])) == mods:
            return rule                     # 精确组合规则永远优先
        action = rule.get("action") or {}
        if passthrough is None and (action.get("hold") or trig.get("any_mods")):
            # 「把某键换成 Win/Ctrl 这类修饰键」时，同时按住的其它修饰键
            # 是组合键的一部分，应当透传而不是让规则失配（Ctrl+AltGr = Ctrl+Win）。
            passthrough = rule
    return passthrough


def capture_now(vk, scan, mods, mode, raw=None):
    trig = {
        "vk": vk, "scan": scan,
        "mods": [m for m in MODS_ORDER if m in mods],
        "name": vk_name(vk),
    }
    with STATE.lock:
        STATE.capture_seq += 1
        STATE.capture = {"seq": STATE.capture_seq, "mode": mode,
                         "trigger": trig, "display": display_trigger(trig),
                         "slot": (STATE.recording or {}).get("slot", "")}
        STATE.recording = None
    STATE.add_event(kind="capture", vk=vk, name=display_trigger(trig), detail="录制成功", raw=raw)
    STATE.log("captured %s (%s)" % (display_trigger(trig), mode))


def hook_callback(n_code, w_param, l_param):
    pending_suppress = None
    try:
        if n_code >= 0:
            kb = ctypes.cast(l_param, ctypes.POINTER(KBDLLHOOKSTRUCT)).contents
            raw_vk = int(kb.vkCode)
            flags = int(kb.flags)
            ext = bool(flags & 0x01)
            scan = int(kb.scanCode)
            injected = bool(flags & LLMHF_INJECTED)
            down = w_param in (WM_KEYDOWN, WM_SYSKEYDOWN)
            up = w_param in (WM_KEYUP, WM_SYSKEYUP)

            # 右 Alt（VK_RMENU，或通用 VK_MENU + EXT 标志）= 空中飞鼠上的 AltGr，
            # 单独成为一个逻辑键；左 Alt 与真正的 Ctrl+Alt 组合不受影响。
            # 不强制要求 EXT：有些蓝牙键盘驱动的 0xA5 不带这个标志。
            is_altgr = raw_vk == 0xA5 or (raw_vk == 0x12 and ext)
            vk = ALTGR_VK if is_altgr else norm_vk(raw_vk)

            # Windows 有些键盘布局会为 AltGr 先发一个「假 LCtrl」。
            # 抵消它会补发 LCtrl 抬起 —— 如果用户真的按着 Ctrl，这会把他按住的第三个键
            # 一起松开（表现为「三键同按失效」）。所以只在能确证是假 Ctrl 时才动手：
            #   1) LCtrl 必须是一次全新按下（自动重复 / 已经按住的 Ctrl 不算）
            #   2) 与右 Alt 由同一输入报告产生（时间戳相差 ≤ 8ms）
            #   3) 此刻没有任何其它键被按住（一旦有 Shift 等，那个 Ctrl 很可能是真的）
            # 且仅在总开关开启或正在录制时才干预内部状态（关闭时完全不改动按键行为）。
            ghost_stripped = False
            if down and not injected:
                if raw_vk in (0xA2, 0x11) and not ext:
                    fresh = 0x11 not in STATE.pressed
                    STATE.ghost_ms = int(kb.time) if fresh else -1
                    STATE.ghost_fresh = fresh
                else:
                    gm = STATE.ghost_ms
                    fresh = STATE.ghost_fresh
                    STATE.ghost_ms = -1
                    STATE.ghost_fresh = False
                    other_keys = STATE.pressed - {0x11}
                    if (is_altgr and fresh and gm >= 0
                            and ((int(kb.time) - gm) & 0xFFFFFFFF) <= 8
                            and 0x11 in STATE.pressed and not other_keys
                            and (STATE.config.get("enabled") or STATE.recording is not None)):
                        STATE.pressed.discard(0x11)
                        ghost_stripped = True

            raw = raw_fields(kb, w_param, current_mods(STATE.pressed) | hold_active_mods())

            if injected:
                # 内部补发的「解除卡键」抬起不属于按键事件，静默放行
                if int(kb.dwExtraInfo) == CLEANUP_MARK:
                    return user32.CallNextHookEx(_hook_handle, n_code, w_param, l_param)
                # 程序自己注入的替换输出也记一条，方便核对实际发出的键
                detail = "程序注入的输出键"
                if raw_vk in (0xA2, 0xA3, 0x11):
                    detail = "AltGr 的假 Ctrl / 注入的 Ctrl（已忽略）"
                STATE.add_event(kind="inject", vk=raw_vk, name=vk_name(vk),
                                detail=detail, raw=raw)
                return user32.CallNextHookEx(_hook_handle, n_code, w_param, l_param)

            # 紧急总闸：Ctrl+Alt+Shift+F11 -> 卸载键盘钩子，所有按键立即原样放行。
            # 卸载后钩子不存在，这个热键也就没法再把它打开，重新开启请点界面上的按钮。
            if down and vk == 0x7A and {0x10, 0x11, 0x12} <= STATE.pressed:
                threading.Timer(0.08, lambda: set_capture(False, "hotkey")).start()
                return 1

            # 紧急停止：Ctrl+Alt+Shift+F12（只切换映射总开关，钩子仍在工作）
            if down and vk == 0x7B and {0x10, 0x11, 0x12} <= STATE.pressed:
                with STATE.lock:
                    STATE.config["enabled"] = not STATE.config["enabled"]
                    try:
                        STATE.save()
                    except Exception:
                        pass
                    on = STATE.config["enabled"]
                STATE.log("emergency toggle -> %s" % ("ON" if on else "OFF"))
                STATE.add_event(kind="toggle", detail="紧急开关 -> %s" % ("开启" if on else "关闭"))
                return 1

            rec = STATE.recording
            if rec is not None:
                if time.monotonic() - rec["t0"] > 8.0:
                    with STATE.lock:
                        STATE.recording = None
                    rec = None
                    STATE.add_event(kind="capture", detail="录制超时，请重试")
                else:
                    if down:
                        STATE.pressed.add(vk)
                        others = current_mods(STATE.pressed - ({vk} if is_mod(vk) else set()))
                        label = ("+".join(sorted(others)) + " 同时按住") if others else "单独按下，松开即录到该键"
                        STATE.add_event(kind="down", vk=raw_vk, name=vk_name(vk),
                                        detail="录制中 · " + label, raw=raw)
                        if is_mod(vk):
                            rec["last_mod"] = {"vk": vk, "scan": scan, "raw": raw,
                                               "mods": sorted(others)}
                        else:
                            if ghost_stripped:
                                rec["last_mod"] = None   # 假 Ctrl 不算一次独立按键
                            capture_now(vk, scan, others, rec["mode"], raw)
                    elif up:
                        STATE.pressed.discard(vk)
                        last = rec.get("last_mod")
                        # 单独按下修饰键（Ctrl / Alt / Shift / Win）时，松开才确定没有后续组合键
                        if last and last["vk"] == vk:
                            capture_now(vk, last["scan"], set(last["mods"]),
                                        rec["mode"], last["raw"])
                    return 1  # 录制期间吞掉所有按键，避免输入到别的窗口

            # 按住不放时 Windows 会反复发同一个键的 keydown。已拦截的键必须直接吞掉，
            # 否则每重复一次就再注入一个 Win down，而松开时只补一次 Win up。
            if down and vk in STATE.suppressed:
                return 1

            # 维护按键状态：真实按下的修饰键 + 程序注入并保持着的目标修饰键
            real_mods = current_mods(STATE.pressed)
            mods_now = real_mods | hold_active_mods()
            if down:
                STATE.pressed.add(vk)
            elif up:
                STATE.pressed.discard(vk)

            if up and vk in STATE.suppressed:
                STATE.suppressed.discard(vk)
                hold = STATE.hold_outputs.pop(vk, None)
                if hold:
                    send_inputs([make_key_input(k, up=True) for k in reversed(hold)])
                    STATE.add_event(kind="up", vk=raw_vk, name=vk_name(vk),
                                    detail="已屏蔽的原键抬起" + ("（松开替换键）" if hold else ""), raw=raw)
                return 1

            # 普通抬起也记一条：诊断「第三个键到底有没有按下 / 松开」时很有用
            if up and not injected:
                STATE.add_event(kind="up", vk=raw_vk, name=vk_name(vk),
                                detail="抬起", raw=raw)

            if down:
                rule = find_rule(vk, mods_now)
                if rule:
                    action = rule.get("action") or {"type": "none"}
                    if ghost_stripped:
                        # 假 Ctrl 已经被系统收到，补一个抬起抵消，避免变成 Ctrl+Win
                        send_inputs([make_key_input(0xA2, up=True)])
                        STATE.add_event(kind="altgr", vk=raw_vk, name="AltGr",
                                        detail="已抵消伴随的假 Ctrl", raw=raw)
                    STATE.suppressed.add(vk)
                    pending_suppress = (raw_vk, ext, vk)
                    if action.get("hold"):
                        keys = [int(v) for v in action.get("keys", [])]
                        STATE.hold_outputs[vk] = keys
                        # 同步注入：保证后续第三个键到达时 Win 已经处于按下状态。
                        # 走异步队列会有极小概率让第三个键抢在注入前面，组合键时灵时不灵。
                        send_inputs([make_key_input(k) for k in keys])
                    else:
                        ACTION_QUEUE.put_nowait((action, set(real_mods)))
                    STATE.add_event(kind="hit", vk=raw_vk, name=display_trigger(rule["trigger"]),
                                    detail="→ " + display_action(action), slot=rule.get("slot"), raw=raw)
                    note_chord()
                    pending_suppress = None
                    return 1
                STATE.add_event(kind="down", vk=raw_vk, name=vk_name(vk),
                                detail="+".join(sorted(mods_now)) if mods_now else "", raw=raw)
                note_chord()
    except Exception as exc:
        # 绝不能出现「keydown 放行、keyup 却被吞掉」的情况，否则系统里会留下一个
        # 永久按住的键（表现为按任何键都没反应）。中途出错就回滚拦截状态并补发抬起。
        if pending_suppress is not None:
            raw_pending, ext_pending, vk_pending = pending_suppress
            STATE.suppressed.discard(vk_pending)
            held = STATE.hold_outputs.pop(vk_pending, None)
            try:
                items = [make_key_input(k, up=True, extra=CLEANUP_MARK) for k in (held or [])]
                items.append(make_key_input(raw_pending, up=True, ext=ext_pending,
                                            extra=CLEANUP_MARK))
                send_inputs(items)
            except Exception:
                pass
        STATE.log("hook error: %s" % exc)
    return user32.CallNextHookEx(_hook_handle, n_code, w_param, l_param)


def release_all_holds():
    """引擎退出时松开所有「按住生效」的替换键，避免 Win/Ctrl 卡在按下状态。"""
    with STATE.lock:
        holds = STATE.hold_outputs
        STATE.hold_outputs = {}
    items = [make_key_input(k, up=True) for keys in holds.values() for k in reversed(keys)]
    if items:
        send_inputs(items)


def hook_thread(gen):
    global _hook_handle, _hook_thread_id, _hook_proc_ref
    tid = kernel32.GetCurrentThreadId()
    proc = HOOKPROC(hook_callback)
    handle = user32.SetWindowsHookExW(WH_KEYBOARD_LL, proc, None, 0)
    if gen == _hook_gen:
        _hook_thread_id, _hook_proc_ref = tid, proc
        _hook_handle = handle or None
    _hook_ready.set()
    if not handle:
        STATE.log("SetWindowsHookEx FAILED: %s" % ctypes.get_last_error())
        if gen != _hook_gen:
            try:
                user32.UnhookWindowsHookEx(handle)
            except Exception:
                pass
        return
    STATE.log("keyboard hook installed")
    msg = wt.MSG()
    while user32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))
    STATE.log("hook loop ended")
    # 只清自己这一代：旧线程退出时不能把新装的钩子句柄清掉
    if gen == _hook_gen:
        _hook_handle, _hook_thread_id = None, None


def start_hook():
    """安装钩子（必须在新线程里，低级钩子回调由安装线程的消息循环派发）。"""
    global _hook_gen, _hook_thread_obj
    with _hook_lock:
        if _hook_handle:
            return True
        _hook_gen += 1
        gen = _hook_gen
        _hook_ready.clear()
        _hook_thread_obj = threading.Thread(target=hook_thread, args=(gen,), daemon=True)
        _hook_thread_obj.start()
    if not _hook_ready.wait(3.0):
        STATE.log("hook install timeout")
        return False
    ok = bool(_hook_handle) and _hook_gen == gen
    if not ok:
        STATE.log("hook install failed")
    return ok


def stop_hook():
    """卸载钩子并结束钩子线程：关闭捕获后系统层面真的不再经过我们的代码。"""
    global _hook_handle, _hook_thread_id, _hook_thread_obj
    with _hook_lock:
        handle, tid, obj = _hook_handle, _hook_thread_id, _hook_thread_obj
        _hook_handle, _hook_thread_id, _hook_thread_obj = None, None, None
    if handle:
        try:
            user32.UnhookWindowsHookEx(handle)
        except Exception:
            pass
    if tid:
        try:
            user32.PostThreadMessageW(tid, WM_QUIT, 0, 0)
        except Exception:
            pass
    if obj and obj.is_alive() and obj is not threading.current_thread():
        obj.join(timeout=2.0)


def set_capture(on, source="ui"):
    """键盘捕获总闸。ON=装钩子；OFF=卸载钩子、清空按键状态、解除所有卡住的键。

    这个选择写进 mappings.json，重启引擎后仍然保持，避免「一开程序又被卡住」。
    """
    on = bool(on)
    with STATE.lock:
        if bool(STATE.config.get("capture", True)) == on and bool(_hook_handle) == on:
            return True
        STATE.config["capture"] = on
        try:
            STATE.save()
        except Exception as exc:
            STATE.log("capture save failed: %s" % exc)
        STATE.pressed.clear()
        STATE.suppressed.clear()
        STATE.recording = None
        STATE.ghost_ms = -1
        STATE.ghost_fresh = False
    if on:
        ok = start_hook()
        detail = "键盘捕获已开启（低级钩子已安装）" if ok else "键盘捕获开启失败：钩子安装失败"
        STATE.log("capture ON (%s) ok=%s" % (source, ok))
    else:
        release_all_holds()
        released = release_stuck_mods()
        stop_hook()
        ok = True
        detail = "键盘捕获已关闭：所有按键原样放行"
        if released:
            detail += "（顺带解除了卡住的 " + "、".join(released) + "）"
        STATE.log("capture OFF (%s) released=%s" % (source, released))
    STATE.add_event(kind="toggle", detail=detail)
    return ok


def stuck_watchdog():
    """卡键自动恢复：程序内部已没有任何键按住，系统却还显示修饰键按下时补发抬起。"""
    seen = {}
    while True:
        time.sleep(0.5)
        try:
            if not _hook_handle:
                seen.clear()
                continue
            with STATE.lock:
                busy = bool(STATE.pressed or STATE.suppressed or
                            STATE.hold_outputs or STATE.recording)
            mods = [] if busy else stuck_mods()
            current = set(mods)
            for k in list(seen):
                if k not in current:
                    seen.pop(k, None)
            for item in mods:
                seen[item] = seen.get(item, 0) + 1
                if seen[item] < 3:       # 连续约 1.5 秒才动手，避免误松用户正在按住的键
                    continue
                vk, name, ext = item
                send_inputs([make_key_input(vk, up=True, ext=ext, extra=CLEANUP_MARK)])
                seen.pop(item, None)
                STATE.add_event(kind="altgr", name=name, detail="自动解除卡住的键")
                STATE.log("auto released stuck key: %s" % name)
        except Exception as exc:
            STATE.log("watchdog error: %s" % exc)


atexit.register(stop_hook)

# ----------------------------------------------------------------------------
# 本机 HTTP 接口
# ----------------------------------------------------------------------------

INDEX_PATH = os.path.join(BASE, "index.html")


def port_in_use():
    """Windows 的 SO_REUSEADDR 允许多个进程绑同一端口，必须先主动探测。"""
    s = socket.socket(socket.AF_INET, socket.SOCK_STREAM)
    s.settimeout(0.3)
    try:
        s.connect((HOST, PORT))
        return True
    except OSError:
        return False
    finally:
        s.close()


def state_payload():
    with STATE.lock:
        cfg = json.loads(json.dumps(STATE.config))
        rec = dict(STATE.recording) if STATE.recording else None
        cap = dict(STATE.capture) if STATE.capture else None
        evs = list(STATE.events)[-40:]
        pressed = sorted(STATE.pressed)
        holds = {vk: list(keys) for vk, keys in STATE.hold_outputs.items()}
        diag = {"peak": STATE.peak_chord, "history": list(STATE.chord_history)}
    if rec:
        t0 = rec.pop("t0", time.monotonic())
        rec.pop("last_mod", None)
        rec["remaining"] = max(0, 8.0 - (time.monotonic() - t0))
    return {
        "ok": True, "config": cfg, "recording": rec, "capture": cap,
        "events": evs, "catalog": CATALOG, "vkNames": VK_NAMES,
        "hook_ok": bool(_hook_handle),
        # 键盘捕获总闸：capture_on 是实际钩子状态，capture_saved 是持久化选择
        "capture_on": bool(_hook_handle),
        "capture_saved": bool(cfg.get("capture", True)),
        "stuck_mods": [{"vk": v, "name": n} for v, n, _e in stuck_mods()],
        "diag": diag,
        # 实时「当前一起按住的键」：真实按下的键 + 由规则注入并保持着的目标键
        "chord": {
            "pressed": [{"vk": v, "name": vk_name(v), "mod": MODS_BY_VK.get(v)}
                        for v in pressed],
            "holds": [{"trigger": vk_name(vk), "triggerVk": vk,
                       "keys": [{"name": vk_name(int(k)), "mod": MODS_BY_VK.get(norm_vk(int(k)))}
                                for k in ks]}
                      for vk, ks in holds.items()],
        },
        "version": "1.0",
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "AirMouseMapper/1.0"

    def log_message(self, *args):
        pass

    def _guard(self):
        host = (self.headers.get("Host") or "").split(":")[0]
        if host not in ("127.0.0.1", "localhost", "::1"):
            self._json({"ok": False, "error": "local access only"}, 403)
            return False
        return True

    def _cors_origin(self):
        """只允许本机其它端口的页面跨域访问（例如静态预览面板），公网页面一律拒绝。"""
        origin = self.headers.get("Origin") or ""
        if origin.startswith("http://127.0.0.1:") or origin.startswith("http://localhost:"):
            return origin
        return None

    def do_OPTIONS(self):
        if not self._guard():
            return
        origin = self._cors_origin()
        self.send_response(204)
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
            self.send_header("Access-Control-Allow-Headers", "Content-Type")
            self.send_header("Access-Control-Max-Age", "600")
            self.send_header("Vary", "Origin")
        self.send_header("Content-Length", "0")
        self.end_headers()

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode("utf-8")
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Cache-Control", "no-store")
        origin = self._cors_origin()
        if origin:
            self.send_header("Access-Control-Allow-Origin", origin)
            self.send_header("Vary", "Origin")
        self.end_headers()
        self.wfile.write(body)

    def _body(self):
        n = int(self.headers.get("Content-Length") or 0)
        if not n:
            return {}
        return json.loads(self.rfile.read(n).decode("utf-8"))

    def do_GET(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        if path in ("/", "/index.html"):
            try:
                with open(INDEX_PATH, "rb") as fh:
                    body = fh.read()
                self.send_response(200)
                self.send_header("Content-Type", "text/html; charset=utf-8")
                self.send_header("Content-Length", str(len(body)))
                self.send_header("Cache-Control", "no-store")
                self.end_headers()
                self.wfile.write(body)
            except Exception as exc:
                self._json({"ok": False, "error": str(exc)}, 500)
        elif path == "/api/state":
            self._json(state_payload())
        elif path == "/api/capture":
            # 也支持直接访问链接开关，例如 http://127.0.0.1:8765/api/capture?on=0
            q = parse_qs(urlparse(self.path).query)
            on = (q.get("on") or ["1"])[0].lower() not in ("0", "false", "off", "no")
            ok = set_capture(on, "link")
            payload = state_payload()
            payload["capture_result"] = ok
            self._json(payload)
        elif path == "/api/export":
            self._json(STATE.config)
        else:
            self._json({"ok": False, "error": "not found"}, 404)

    def do_POST(self):
        if not self._guard():
            return
        path = urlparse(self.path).path
        try:
            data = self._body()
        except Exception as exc:
            self._json({"ok": False, "error": "bad json: %s" % exc}, 400)
            return

        if path == "/api/config":
            cfg = data.get("config") or {}
            if not isinstance(cfg.get("rules"), list):
                self._json({"ok": False, "error": "rules must be a list"}, 400)
                return
            with STATE.lock:
                STATE.config = normalize_config(cfg)
                STATE.save()
            STATE.log("config saved: %d rules, enabled=%s" %
                      (len(STATE.config["rules"]), STATE.config["enabled"]))
        elif path == "/api/enable":
            with STATE.lock:
                STATE.config["enabled"] = bool(data.get("enabled"))
                STATE.save()
            STATE.log("enabled = %s" % STATE.config["enabled"])
        elif path == "/api/record/start":
            with STATE.lock:
                STATE.pressed.clear()
                STATE.suppressed.clear()
                STATE.recording = {"mode": data.get("mode", "trigger"),
                                   "slot": data.get("slot", ""),
                                   "t0": time.monotonic(), "last_mod": None}
            STATE.add_event(kind="capture", detail="开始录制，请按空中飞鼠上的键")
            STATE.log("recording started (%s)" % data.get("mode"))
        elif path == "/api/record/stop":
            with STATE.lock:
                STATE.recording = None
            STATE.add_event(kind="capture", detail="录制已取消")
        elif path == "/api/trigger/parse":
            trig, detail, err = parse_trigger_text(data.get("text", ""),
                                                   bool(data.get("any_mods")),
                                                   data.get("scan", 0))
            self._json({"ok": not bool(err), "trigger": trig,
                        "detail": detail, "error": err or None})
            return
        elif path == "/api/capture":
            ok = set_capture(bool(data.get("on")), "ui")
            payload = state_payload()
            payload["capture_result"] = ok
            self._json(payload)
            return
        elif path == "/api/release":
            released = release_stuck_mods()
            STATE.add_event(kind="toggle",
                            detail=("已解除卡住的键：" + "、".join(released)) if released
                                   else "检查完成：没有卡住的修饰键")
            payload = state_payload()
            payload["released"] = released
            self._json(payload)
            return
        elif path == "/api/diag/reset":
            with STATE.lock:
                STATE.peak_chord = 0
                STATE.chord_history.clear()
            STATE.log("chord diagnostics reset")
        elif path == "/api/test":
            act = data.get("action") or {"type": "none"}
            if isinstance(act, dict) and act.get("hold"):
                # 「试一下输出」时按下并立即抬起，避免修饰键卡在按住状态
                act = {k: v for k, v in act.items() if k != "hold"}
            ACTION_QUEUE.put_nowait((act, set()))
        elif path == "/api/import":
            cfg = data.get("config") or {}
            if not isinstance(cfg.get("rules"), list):
                self._json({"ok": False, "error": "invalid config"}, 400)
                return
            with STATE.lock:
                STATE.config = normalize_config(cfg)
                STATE.save()
        else:
            self._json({"ok": False, "error": "not found"}, 404)
            return
        self._json(state_payload())


class LocalServer(ThreadingHTTPServer):
    allow_reuse_address = False   # 禁止第二个进程重复绑定同一端口
    daemon_threads = True


def main():
    STATE.load()
    url = "http://%s:%d/" % (HOST, PORT)
    if port_in_use():
        STATE.log("another instance already serving %s, exiting" % url)
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    threading.Thread(target=action_worker, daemon=True).start()
    threading.Thread(target=stuck_watchdog, daemon=True).start()
    # 清掉上次运行（尤其是被强杀）可能留在系统里的按下状态
    released = release_stuck_mods()
    if released:
        STATE.log("startup released stuck keys: %s" % ", ".join(released))
    if STATE.config.get("capture", True):
        start_hook()
    else:
        STATE.log("capture is OFF in saved config; keyboard hook NOT installed")
    time.sleep(0.2)
    try:
        server = LocalServer((HOST, PORT), Handler)
    except OSError:
        # 已经有一个实例在跑：直接打开它的界面，不重复安装钩子
        STATE.log("port %d in use, opening existing instance" % PORT)
        if "--no-browser" not in sys.argv:
            webbrowser.open(url)
        return
    STATE.log("serving %s  (hook=%s)" % (url, bool(_hook_handle)))
    try:  # pythonw 下 sys.stdout 是 None，print 会抛异常
        print("\n  空中飞鼠 · 按键映射 (Air Mouse Key Mapper)")
        print("  " + url)
        print("  配置界面已/即将在浏览器打开。关闭此窗口即退出，映射随程序停止而失效。")
        print("  紧急开关：Ctrl+Alt+Shift+F12 切换映射 / Ctrl+Alt+Shift+F11 关闭键盘捕获")
        print("  界面右上角可直接开关「键盘捕获」，关闭后所有按键原样放行\n")
    except Exception:
        pass
    if "--no-browser" not in sys.argv:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass
    finally:
        with STATE.lock:
            STATE.save()
        release_all_holds()
        release_stuck_mods()
        stop_hook()
        server.server_close()   # shutdown() 必须在另一线程调用，这里用 server_close 避免死锁


if __name__ == "__main__":
    main()
