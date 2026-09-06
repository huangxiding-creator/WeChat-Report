# -*- coding: utf-8 -*-
"""鼠标接管监督器：人动鼠标 → 暂停批量爬取；静默 >30s → 自动接续。

用户 2026-09-06 指示：爬取运行期间用户一碰鼠标就停手（避免人工移动与
pyautogui 自动滚轮/点击互相干扰——点错会话、碰角触发 fail-safe 使上滚
成果作废），用户不碰鼠标超过 30s 就自己接续，全程无需人工值守。

判别核心：WH_MOUSE_LL 低级鼠标钩子里，注入事件（pyautogui/SendInput/
mouse_event）带 LLMHF_INJECTED 标志，物理鼠标事件不带——只统计物理
事件，驱动自己的鼠标动作**永不**误触发暂停。

状态机（GateState，纯逻辑可单测）：
  RUNNING --物理鼠标活动--> taskkill /T /F 驱动进程树 --> PAUSED
  PAUSED  --物理活动静默 >idle(默认30s)--> 原命令重启驱动（断点续爬）
驱动在 RUNNING 态自然退出（批完成 rc=0 / 崩溃 rc!=0）→ 响亮报告后
监督器退出，绝不静默吞掉。

用法：
  python -X utf8 mouse_supervisor.py                 # 生产（原批量命令）
  python -X utf8 mouse_supervisor.py --dummy         # 假驱动自测
  python -X utf8 mouse_supervisor.py --dummy --debug-any-input --idle 6
        # 端到端自测：把注入事件也当物理事件（勿用于生产）
"""
from __future__ import annotations

import argparse
import ctypes
import os
import subprocess
import sys
import threading
import time
from ctypes import wintypes
from pathlib import Path

ROOT = Path(__file__).resolve().parent

DEFAULT_CMD = [sys.executable, "-u", "-X", "utf8", "main.py", "--cli",
               "--all", "--window", "2026", "--output", "./output/批量导出"]

DUMMY_CODE = (
    "import time\n"
    "print('DUMMY 驱动启动', flush=True)\n"
    "t0 = time.time()\n"
    "while time.time() - t0 < 1200:\n"
    "    time.sleep(1)\n"
    "    print('DUMMY 心跳 %d' % int(time.time() - t0), flush=True)\n"
)

TICK_S = 0.4      # 状态机轮询间隔
ACT_PX = 8.0      # 判定"用户在动"的累计位移阈值（慢速挪动也累积得到）
LEAK_PX = 2.0     # 漏桶：无活动的 tick 让累计位移漏掉一点（传感器噪点积不起来）


# ---------------------------------------------------------------- 纯状态机
class GateState:
    """暂停/接续状态机。now 一律 time.monotonic()，可离线单测。"""

    RUNNING = "RUNNING"
    PAUSED = "PAUSED"

    def __init__(self, idle_s: float = 30.0, boot_now: float = 0.0):
        self.idle_s = idle_s
        self.state = self.PAUSED          # 启动即暂停态：静默满窗即首发
        self.last_activity = boot_now - (idle_s + 1.0)   # 视为早已静默
        self.n_pauses = 0
        self.n_resumes = 0

    def on_activity(self, now: float) -> bool:
        """物理鼠标活动。返回 True = 需要暂停（RUNNING→PAUSED）。"""
        self.last_activity = now
        if self.state == self.RUNNING:
            self.state = self.PAUSED
            self.n_pauses += 1
            return True
        return False                        # 已暂停：只刷新静默计时

    def tick(self, now: float):
        """返回 'resume'（应重启驱动）或 None。"""
        if (self.state == self.PAUSED
                and now - self.last_activity > self.idle_s):
            self.state = self.RUNNING
            self.n_resumes += 1
            return "resume"
        return None


# ------------------------------------------------ 低级鼠标钩子（只认物理）
WM_QUIT = 0x0012
WH_MOUSE_LL = 14
LLMHF_INJECTED = 0x01
_MOVE = {0x0200}                             # WM_MOUSEMOVE
_CLICK_OR_WHEEL = {0x0201, 0x0202, 0x0204, 0x0205, 0x0207, 0x0208,
                   0x020A, 0x020B, 0x020C, 0x020E}


class _MSLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("pt", wintypes.POINT), ("mouseData", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


_HOOKPROC = ctypes.WINFUNCTYPE(
    ctypes.c_ssize_t, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)

user32 = ctypes.windll.user32
user32.SetWindowsHookExW.argtypes = (
    ctypes.c_int, _HOOKPROC, wintypes.HINSTANCE, wintypes.DWORD)
user32.SetWindowsHookExW.restype = wintypes.HHOOK
user32.CallNextHookEx.argtypes = (
    wintypes.HHOOK, ctypes.c_int, wintypes.WPARAM, wintypes.LPARAM)
user32.CallNextHookEx.restype = ctypes.c_ssize_t

_lock = threading.Lock()
_acc_px = [0.0]        # 自上次判定活动以来的累计位移（像素）
_btn = [False]         # 本轮内出现过物理点击/滚轮
_prev_pt = [None]
_debug_any_input = False

import collections
_evt_log = collections.deque(maxlen=512)   # (monotonic, injected, x, y)：诊断/自测


def _ll_proc(nCode, wParam, lParam):
    if nCode >= 0:
        try:
            info = ctypes.cast(
                lParam, ctypes.POINTER(_MSLLHOOKSTRUCT)).contents
            injected = bool(info.flags & LLMHF_INJECTED)
            with _lock:
                _evt_log.append((time.monotonic(), injected,
                                 info.pt.x, info.pt.y))
            if not injected or _debug_any_input:
                if wParam in _MOVE:
                    cur = (info.pt.x, info.pt.y)
                    prev = _prev_pt[0]
                    _prev_pt[0] = cur
                    if prev is not None:
                        _acc_px[0] += (abs(cur[0] - prev[0])
                                       + abs(cur[1] - prev[1]))
                elif wParam in _CLICK_OR_WHEEL:
                    _btn[0] = True
        except Exception:
            pass                          # 钩子回调绝不抛：超时会被系统摘钩
    return user32.CallNextHookEx(None, nCode, wParam, lParam)


_ll_proc_ref = _HOOKPROC(_ll_proc)        # 必须持引用：被 GC 会崩
_hook = None
_hook_thread_id = 0


def _pump() -> None:
    """钩子线程：装 WH_MOUSE_LL + 消息泵（低级钩子在装钩线程泵消息时回调）。"""
    global _hook, _hook_thread_id
    _hook_thread_id = ctypes.windll.kernel32.GetCurrentThreadId()
    _hook = user32.SetWindowsHookExW(WH_MOUSE_LL, _ll_proc_ref, None, 0)
    if not _hook:
        print("✗ SetWindowsHookEx 失败（钩子装不上，无法判别物理鼠标）",
              flush=True)
        os._exit(2)
    msg = wintypes.MSG()
    while True:
        r = user32.GetMessageW(ctypes.byref(msg), None, 0, 0)
        if r <= 0:                        # 0=WM_QUIT -1=错误
            break
        user32.TranslateMessage(ctypes.byref(msg))
        user32.DispatchMessageW(ctypes.byref(msg))


def _stop_hook() -> None:
    if _hook_thread_id:
        user32.PostThreadMessageW(_hook_thread_id, WM_QUIT, 0, 0)
    for _ in range(15):                   # 等泵退出（≤6s），不 join 死等
        if _hook_thread_id == 0:
            break
        time.sleep(0.4)
    if _hook:
        user32.UnhookWindowsHookEx(_hook)


# ---------------------------------------------------------------- 主循环
def _say(msg: str) -> None:
    print(time.strftime("[%H:%M:%S] ") + msg, flush=True)


def _kill_tree(pid: int) -> None:
    subprocess.run(["taskkill", "/PID", str(pid), "/T", "/F"],
                   capture_output=True)


def _single_instance() -> bool:
    """防止双监督器（双驱动=双进程操作微信，绝对禁止）。"""
    import msvcrt
    f = open(ROOT / "_supervisor.lock", "a+b")
    try:
        f.seek(0)
        msvcrt.locking(f.fileno(), msvcrt.LK_NBLCK, 1)
    except OSError:
        return False
    return True        # f 必须保持打开：进程退出锁自动释放


def main(argv=None) -> int:
    global _debug_any_input
    ap = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    ap.add_argument("--idle", type=float, default=30.0,
                    help="接续所需的鼠标静默秒数（默认 30）")
    ap.add_argument("--dummy", action="store_true",
                    help="用假驱动（1200s 心跳打印）自测")
    ap.add_argument("--debug-any-input", action="store_true",
                    help="自测：把注入事件也当物理事件（勿用于生产）")
    args = ap.parse_args(argv)
    _debug_any_input = args.debug_any_input

    if not _single_instance():
        _say("✗ 已有监督器在运行（_supervisor.lock 被占），拒绝双开")
        return 2

    cmd = ([sys.executable, "-X", "utf8", "-c", DUMMY_CODE]
           if args.dummy else DEFAULT_CMD)
    log_name = "_dummy_driver.log" if args.dummy else "_batch_full12.log"
    log = open(ROOT / log_name, "ab")
    gate = GateState(idle_s=args.idle, boot_now=time.monotonic())
    threading.Thread(target=_pump, daemon=True).start()
    proc = None
    _say(f"🚦 监督器启动：idle={args.idle:.0f}s mode="
         f"{'DUMMY' if args.dummy else '生产'}"
         f"{' +debug-any-input' if args.debug_any_input else ''}")
    try:
        while True:
            time.sleep(TICK_S)
            now = time.monotonic()
            with _lock:
                btn, _btn[0] = _btn[0], False
                px = _acc_px[0]
                if px >= ACT_PX:
                    _acc_px[0] = 0.0          # 达标即消费
                else:
                    _acc_px[0] = max(0.0, px - LEAK_PX)   # 漏桶
            if btn or px >= ACT_PX:
                if gate.on_activity(now) and proc is not None:
                    _say(f"🖐 用户接管鼠标（{px:.0f}px"
                         f"{'+键/轮' if btn else ''}）→ 暂停驱动"
                         f"（第 {gate.n_pauses} 次）")
                    _kill_tree(proc.pid)
                    proc = None
            if gate.tick(now) == "resume":
                _say(f"▶ 鼠标静默 >{args.idle:.0f}s → 接续运行"
                     f"（第 {gate.n_resumes} 次）")
                try:
                    proc = subprocess.Popen(cmd, stdout=log,
                                            stderr=subprocess.STDOUT,
                                            cwd=str(ROOT))
                except OSError as e:
                    _say(f"✗ 驱动启动失败：{e}")
                    return 1
            if proc is not None and gate.state == GateState.RUNNING:
                rc = proc.poll()
                if rc is not None:
                    _say(("🎉 驱动自然退出（rc=0）——批量完成？"
                          if rc == 0 else
                          f"✗ 驱动异常退出 rc={rc}（详见 {log_name}）")
                         + "，监督器退出")
                    return 0 if rc == 0 else 1
    except KeyboardInterrupt:
        _say("监督器收到 Ctrl+C，退出（驱动不动）")
        return 130
    finally:
        _stop_hook()
        log.close()


if __name__ == "__main__":
    sys.exit(main())
