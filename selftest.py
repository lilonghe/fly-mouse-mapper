# -*- coding: utf-8 -*-
"""引擎逻辑自测：不产生真实按键，直接喂给钩子回调。运行：python selftest.py"""
import ctypes
import time
import remapper as R


def fire(vk, down=True, flags=0, t=0):
    kb = R.KBDLLHOOKSTRUCT(vk, 0, flags, t, 0)
    msg = R.WM_KEYDOWN if down else R.WM_KEYUP
    return R.hook_callback(0, msg, ctypes.addressof(kb))


def reset(rules, enabled=True):
    R.STATE.config = {"version": 1, "enabled": enabled, "rules": rules, "labels": {}}
    R.STATE.pressed.clear()
    R.STATE.suppressed.clear()
    R.STATE.hold_outputs.clear()
    R.STATE.ghost_ms = -1
    R.STATE.ghost_fresh = False
    R.STATE.peak_chord = 0
    R.STATE.chord_history.clear()
    R.STATE.recording = None
    R.STATE.events.clear()
    while not R.ACTION_QUEUE.empty():
        R.ACTION_QUEUE.get_nowait()


def start_record(mode="trigger"):
    R.STATE.capture = None
    R.STATE.capture_seq = 0
    R.STATE.recording = {"mode": mode, "slot": "r4c7:main",
                         "t0": time.monotonic(), "last_mod": None}


def check(name, cond):
    print(("  PASS  " if cond else "  FAIL  ") + name)
    if not cond:
        raise SystemExit(1)


rule_q = {"slot": "r1c2:main", "trigger": {"vk": 0x51, "mods": []},
          "action": {"type": "none", "display": "禁用"}, "active": True}

print("1. 单键命中 / 抑制")
reset([rule_q])
check("Q 按下被拦截", fire(0x51, True) == 1)
check("动作进入注入队列", not R.ACTION_QUEUE.empty())
check("Q 抬起被拦截", fire(0x51, False) == 1)
check("A 按下放行", fire(0x41, True) != 1)
fire(0x41, False)

print("2. 修饰键组合不应误伤")
reset([rule_q])
fire(0xA4, True)                      # 左 Alt
check("Alt+Q 不命中仅 Q 的规则", fire(0x51, True) != 1)
fire(0x51, False)
fire(0xA4, False)

print("3. 带修饰键的规则")
rule_alt_q = {"slot": "r1c3:fn", "trigger": {"vk": 0x51, "mods": ["alt"]},
              "action": {"type": "keys", "keys": [0x1B], "display": "Esc"}, "active": True}
reset([rule_alt_q])
fire(0xA4, True)
check("Alt+Q 命中", fire(0x51, True) == 1)
reset([rule_alt_q])
check("单独按 Q 放行", fire(0x51, True) != 1)

print("4. 总开关关闭时全部放行")
reset([rule_q], enabled=False)
check("关闭后 Q 放行", fire(0x51, True) != 1)
fire(0x51, False)

print("5. 注入事件不被二次映射")
reset([rule_q])
check("injected 标记放行", fire(0x51, True, flags=R.LLMHF_INJECTED) != 1)

print("6. 录制组合键")
reset([])
start_record()
check("Ctrl 按下被吞", fire(0xA2, True) == 1)
check("Q 按下被吞并完成录制", fire(0x51, True) == 1)
cap = R.STATE.capture
check("录制结果含 ctrl 修饰键", cap and cap["trigger"]["mods"] == ["ctrl"] and cap["trigger"]["vk"] == 0x51)
check("录制状态已结束", R.STATE.recording is None)

print("7. 键码显示与规则查找")
check("Esc 显示名正确", R.vk_name(0x1B) == "Esc")
check("左右 Ctrl 归一化", R.norm_vk(0xA3) == 0x11 and R.norm_vk(0xA2) == 0x11)
reset([rule_q])
check("find_rule 命中", R.find_rule(0x51, set()) is rule_q)

# ---- 以下用假的输入发送函数，避免自测真的往系统注入按键 ----
REAL_SEND = R.send_inputs
sent = []
R.send_inputs = lambda items: sent.append(len(items))

print("8. 单独按下修饰键也能录到")
reset([])
start_record()
check("Ctrl 按下被吞", fire(0xA2, True) == 1)
check("按下时还没结束录制", R.STATE.recording is not None)
check("Ctrl 松开完成录制", fire(0xA2, False) == 1)
cap = R.STATE.capture
check("录到单独的 Ctrl", cap and cap["trigger"]["vk"] == 0x11
      and cap["trigger"]["mods"] == [] and R.STATE.recording is None)
reset([])
start_record()
fire(0x5B, True)
fire(0x5B, False)
check("Win 键也能单独录到", R.STATE.capture["trigger"]["vk"] == 0x5B)

print("9. AltGr 识别（剥离 Windows 的假 Ctrl）")
reset([])
start_record()
check("AltGr 直接按下即录到", fire(0xA5, True, flags=0x01) == 1)
check("录制结果是 AltGr 且无修饰键",
      R.STATE.capture["trigger"]["vk"] == R.ALTGR_VK
      and R.STATE.capture["trigger"]["mods"] == []
      and R.STATE.capture["display"] == "AltGr")
reset([])
start_record()
check("不带 EXT 标志的右 Alt 也算 AltGr", fire(0xA5, True, 0) == 1)
check("仍是干净的 AltGr", R.STATE.capture["trigger"]["vk"] == R.ALTGR_VK)
reset([])
start_record()
check("通用 Alt + EXT 也算 AltGr", fire(0x12, True, 0x01) == 1)
check("通用 Alt 识别结果正确", R.STATE.capture["trigger"]["vk"] == R.ALTGR_VK)
reset([])
start_record()
fire(0xA2, True, 0, 1000)                 # 假 LCtrl（同一报告，1ms 内）
check("假 Ctrl + AltGr 也录到干净的 AltGr", fire(0xA5, True, 0x01, 1001) == 1)
check("没有被记成 Ctrl + AltGr", R.STATE.capture["trigger"]["mods"] == []
      and R.STATE.capture["trigger"]["vk"] == R.ALTGR_VK)
kb = R.KBDLLHOOKSTRUCT(0xA5, 0x38, 0x01, 0, 0)
check("原始 HID 字段仍是右 Alt 0xE6",
      R.raw_fields(kb, R.WM_KEYDOWN, set())["hid"]["usage"] == "0x00E6")

rule_altgr = {"slot": "r4c7:main",
              "trigger": {"vk": R.ALTGR_VK, "mods": [], "name": "AltGr"},
              "action": {"type": "keys", "keys": [0x5B], "hold": True,
                         "display": "Win 键（按住生效）"}, "active": True}

print("10. AltGr → Win（按住生效，同步注入）")
reset([rule_altgr])
sent.clear()
check("AltGr 按下被拦截", fire(0xA5, True, 0x01) == 1)
check("登记保持按下的 Win", R.STATE.hold_outputs.get(R.ALTGR_VK) == [0x5B])
check("Win down 已在钩子内同步注入", sent == [1])
check("hold 动作不再走异步队列", R.ACTION_QUEUE.empty())
sent.clear()
check("AltGr 抬起被拦截", fire(0xA5, False, 0x01) == 1)
check("抬起时补发 Win keyup", sent == [1])
check("hold 状态已清理", R.ALTGR_VK not in R.STATE.hold_outputs)

print("11. 左 Alt 与假 Ctrl 不受牵连")
reset([rule_altgr])
check("左 Alt 放行", fire(0xA4, True) != 1)
fire(0xA4, False)
reset([rule_altgr])
sent.clear()
fire(0xA2, True, 0, 2000)
check("带假 Ctrl 的 AltGr 仍命中", fire(0xA5, True, 0x01, 2001) == 1)
check("命中时先抵消假 Ctrl，再注入 Win", sent == [1, 1])
fire(0xA5, False, 0x01)
check("假 Ctrl 抬起不影响状态", 0x11 not in R.STATE.pressed)
reset([rule_altgr], enabled=False)
check("总开关关闭时 AltGr 放行", fire(0xA5, True, 0x01) != 1)

print("12. 组合键 Ctrl + AltGr → Ctrl + Win")
reset([rule_altgr])
fire(0xA2, True, 0, 5000)                 # 先按住物理 Ctrl（与 AltGr 间隔远，不会被当成假 Ctrl）
check("Ctrl 保持按下", 0x11 in R.STATE.pressed)
sent.clear()
check("Ctrl+AltGr 仍能命中 hold 规则", fire(0xA5, True, 0x01, 9000) == 1)
check("登记保持按下的 Win", R.STATE.hold_outputs.get(R.ALTGR_VK) == [0x5B])
check("注入的 Win 参与后续按键的 mods 判定", R.hold_active_mods() == {"win"})
check("只注入 Win down，没有松开 Ctrl", sent == [1])
check("Ctrl 仍在内部按下状态", 0x11 in R.STATE.pressed)
check("系统等效组合是 Ctrl + Win",
      (R.current_mods(R.STATE.pressed) | R.hold_active_mods()) == {"ctrl", "win"})
payload = R.state_payload()
check("接口暴露真实按下的 Ctrl",
      any(k["name"] == "Ctrl" and k["mod"] == "ctrl" for k in payload["chord"]["pressed"]))
check("接口暴露注入保持的 Win",
      payload["chord"]["holds"][0]["keys"][0]["name"] == "Win")
sent.clear()
check("AltGr 抬起被拦截", fire(0xA5, False, 0x01, 9100) == 1)
check("只抬起 Win", sent == [1])
check("Ctrl 依然按住", 0x11 in R.STATE.pressed)
fire(0xA2, False, 0, 9200)

print("13. 精确规则优先 / 普通替换仍释放修饰键")
rule_exact = {"slot": "x", "trigger": {"vk": R.ALTGR_VK, "mods": ["ctrl"]},
              "action": {"type": "none", "display": "精确"}, "active": True}
reset([rule_altgr, rule_exact])
check("Ctrl+AltGr 命中精确规则", R.find_rule(R.ALTGR_VK, {"ctrl"}) is rule_exact)
check("其它修饰键回退到透传规则", R.find_rule(R.ALTGR_VK, {"alt"}) is rule_altgr)
sent.clear()
R.perform_action({"type": "keys", "keys": [0x1B]}, {"ctrl"})
check("普通替换先松开 Ctrl 再发 Esc", sent == [1, 2])

print("14. 三键场景：假 Ctrl 抵消不得误杀真实 Ctrl")
reset([rule_altgr])
fire(0xA2, True, 0, 30000)                # 真实 Ctrl
fire(0xA0, True, 0, 30001)                # 真实 Shift（第三个键）
sent.clear()
check("Ctrl+Shift+AltGr 命中", fire(0xA5, True, 0x01, 30002) == 1)
check("没有补发任何 LCtrl 抬起", sent == [1])
check("Ctrl 与 Shift 都还在按下状态", {0x11, 0x10} <= R.STATE.pressed)
check("等效组合是 Ctrl + Shift + Win",
      (R.current_mods(R.STATE.pressed) | R.hold_active_mods()) == {"ctrl", "shift", "win"})
check("第四个键到达时仍带着三个修饰键",
      R.current_mods(R.STATE.pressed) | R.hold_active_mods() == {"ctrl", "shift", "win"})
check("方向键放行且未被替换", fire(0x25, True, 0, 30003) != 1)  # ← 方向键

print("15. 按住不放的自动重复不重复注入")
reset([rule_altgr])
sent.clear()
check("第一次 AltGr 按下", fire(0xA5, True, 0x01, 40000) == 1)
check("重复 keydown 直接吞掉", fire(0xA5, True, 0x01, 40050) == 1)
check("再重复也吞掉", fire(0xA5, True, 0x01, 40100) == 1)
check("只注入了一次 Win down", sent == [1])
sent.clear()
fire(0xA5, False, 0x01, 40150)
check("松开只补一次 Win up", sent == [1])

print("16. 组合键诊断记录 / 抬起事件")
reset([rule_altgr])
fire(0xA2, True, 0, 50000)
fire(0xA0, True, 0, 50001)
fire(0xA5, True, 0x01, 50002)
fire(0xA0, False, 0, 50003)               # 普通键抬起
d = R.state_payload()["diag"]
check("峰值键数达到 3", d["peak"] >= 3)
check("记录了最近组合", len(d["history"]) >= 1)
check("普通抬起也出现在事件流",
      any(e["kind"] == "up" and e.get("detail") == "抬起" for e in R.state_payload()["events"]))
R.STATE.peak_chord = 0
R.STATE.chord_history.clear()
check("重置后峰值归零", R.state_payload()["diag"]["peak"] == 0)

print("17. 键盘捕获开关（真正卸载 / 安装钩子）")
REAL_START, REAL_STOP = R.start_hook, R.stop_hook
REAL_SAVE, REAL_STUCK = R.STATE.save, R.stuck_mods
hooks = []
def fake_start():
    hooks.append("start")
    R._hook_handle = 12345
    return True
def fake_stop():
    hooks.append("stop")
    R._hook_handle = None
R.start_hook, R.stop_hook = fake_start, fake_stop
R.STATE.save = lambda: None                       # 不污染用户真实的 mappings.json
R.stuck_mods = lambda: [(0xA5, "RAlt (AltGr)", True)]   # 模拟系统里卡住 AltGr
reset([rule_altgr])
R.STATE.config["capture"] = True
R._hook_handle = 12345
sent.clear(); hooks.clear()
check("关闭捕获返回成功", R.set_capture(False) is True)
check("关闭时确实卸载了钩子", hooks == ["stop"])
check("关闭时补发抬起解除卡键", sent == [1])
check("捕获选择写进配置", R.STATE.config["capture"] is False)
check("内部按键状态全部清空",
      not R.STATE.pressed and not R.STATE.suppressed and not R.STATE.hold_outputs)
p = R.state_payload()
check("接口报告实际捕获已关闭", p["capture_on"] is False)
check("接口报告持久选择已关闭", p["capture_saved"] is False)
check("接口报告卡住的键", p["stuck_mods"][0]["name"] == "RAlt (AltGr)")
check("关闭动作写进事件流",
      any(e["kind"] == "toggle" and "关闭" in e.get("detail", "") for e in p["events"]))

sent.clear(); hooks.clear()
R.stuck_mods = lambda: []
check("重新开启捕获返回成功", R.set_capture(True) is True)
check("开启时确实安装了钩子", hooks == ["start"])
check("捕获选择恢复为开启", R.STATE.config["capture"] is True)
check("接口报告实际捕获已开启", R.state_payload()["capture_on"] is True)

print("18. 配置保存 / 导入不丢捕获开关")
check("保存规则时保留当前 capture", R.normalize_config({"rules": [], "enabled": True})["capture"] is True)
check("导入文件里显式关闭仍保留",
      R.normalize_config({"rules": [], "enabled": True, "capture": False})["capture"] is False)
check("旧配置无该字段时默认开启",
      R.normalize_config({"rules": [], "enabled": False})["capture"] is True)

print("19. 内部补发的抬起不污染事件流")
R._hook_handle = None
reset([])
n = len(R.STATE.events)
kb = R.KBDLLHOOKSTRUCT(0xA5, 0, R.LLMHF_INJECTED, 0, R.CLEANUP_MARK)
R.hook_callback(0, R.WM_KEYUP, ctypes.addressof(kb))
check("解除卡键的注入被静默放行", len(R.STATE.events) == n)

print("20. 手写触发条件解析")
t, d, e = R.parse_trigger_text("Ctrl+Win+Left")
check("组合键：修饰键在前、最后是主键", e == "" and t["vk"] == 0x25
      and t["mods"] == ["ctrl", "win"] and t["source"] == "manual")
check("组合键给出 HID 字段", "0x0050" in d and "Report" in d)
check("AltGr 手写与录制结果一致",
      R.parse_trigger_text("AltGr")[0]["vk"] == R.ALTGR_VK)
check("Command / RightAlt 别名",
      R.parse_trigger_text("Command+R")[0]["mods"] == ["win"]
      and R.parse_trigger_text("RightAlt")[0]["vk"] == R.ALTGR_VK)
check("十六进制 / 十进制 VK",
      R.parse_trigger_text("0x51")[0]["vk"] == 0x51
      and R.parse_trigger_text("VK 229")[0]["vk"] == R.ALTGR_VK)
check("HID Usage 反查", R.parse_trigger_text("usage 0x0014")[0]["vk"] == 0x51
      and R.parse_trigger_text("HID 0x07/0x0014")[0]["vk"] == 0x51)
check("Fn 明确报错并给出替代做法", "Fn" in R.parse_trigger_text("Fn+Q")[2])
check("非法顺序报错", "修饰键" in R.parse_trigger_text("Ctrl+Q+Alt")[2])
check("空输入报错", bool(R.parse_trigger_text("")[2]))
check("不认识的键名报错", bool(R.parse_trigger_text("NotAKey")[2]))

loose = R.parse_trigger_text("Q", any_mods=True)[0]
rule_loose = {"slot": "x", "trigger": loose,
              "action": {"type": "none"}, "active": True}
reset([rule_loose])
check("宽松匹配：多按修饰键也命中", R.find_rule(0x51, {"ctrl"}) is rule_loose)
exact = R.parse_trigger_text("Ctrl+Q")[0]
rule_exact2 = {"slot": "y", "trigger": exact,
               "action": {"type": "none"}, "active": True}
reset([rule_exact2])
check("精确匹配命中", R.find_rule(0x51, {"ctrl"}) is rule_exact2)
check("精确匹配不会误命中多按修饰键", R.find_rule(0x51, {"ctrl", "shift"}) is None)
n = R.normalize_trigger({"vk": "0x51", "mods": ["ctrl", "bogus", "shift"]})
check("配置规范化修掉非法 vk / mods", n["vk"] == 0x51 and n["mods"] == ["ctrl", "shift"])
check("规范化补上可读键名", n["name"] == "Q")

check("make_key_input 带 EXTENDEDKEY 与标记",
      R.make_key_input(0xA5, up=True, ext=True, extra=R.CLEANUP_MARK).u.ki.dwFlags
      == (R.KEYEVENTF_KEYUP | R.KEYEVENTF_EXTENDEDKEY))

R._hook_handle = None
R.start_hook, R.stop_hook = REAL_START, REAL_STOP
R.STATE.save, R.stuck_mods = REAL_SAVE, REAL_STUCK
R.send_inputs = REAL_SEND
print("\n全部通过：匹配 / 拦截 / 录制 / AltGr / Win 按住 / 组合键透传 / 三键诊断 / 捕获开关 / 手写触发条件均正常。")
