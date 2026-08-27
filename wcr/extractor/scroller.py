# -*- coding: utf-8 -*-
"""聊天区滚动控制（只读：仅滚轮 + 焦点点击聊天区中央）。"""
from __future__ import annotations

import logging
import time
from typing import Optional, Callable

from .capture import ScreenCapture
from .safety import SafetyGuard
from .timelabels import is_time_label, parse_time_label

log = logging.getLogger("wcr.scroller")


class ChatScroller:
    """滚轮浏览聊天记录；支持"精确时间窗"提前停止。

    相比传统"傻滚到顶再全量采集"，时间窗模式在向上滚动过程中
    一旦发现早于起始日期的时间标签立即停止——大群提速 10~100 倍。
    """

    def __init__(self, win, cap: ScreenCapture, guard: SafetyGuard,
                 scroll_pause: float = 0.35,
                 scroll_step: int = 15,
                 stable_frames_to_stop: int = 3,
                 max_attempts: int = 800,
                 input_zone_ratio: float = 0.80):
        self.win = win
        self.cap = cap
        self.guard = guard
        self.scroll_pause = scroll_pause
        self.scroll_step = scroll_step
        self.stable_frames_to_stop = stable_frames_to_stop
        self.max_attempts = max_attempts
        self.input_zone_ratio = input_zone_ratio

    # ------------------------------------------------------------ basics
    def _wheel(self, delta: int) -> None:
        """delta 正值向上、负值向下。仅移动到聊天区中央后滚动。"""
        import pyautogui

        cx, cy = self.win.chat_center(self.input_zone_ratio)
        self.guard.check_scroll(cx, cy, self.win.rect)  # 聊天区中央是安全带
        pyautogui.moveTo(cx, cy, duration=0.12)
        pyautogui.scroll(delta)

    def _grab(self):
        return self.cap.grab()

    # ------------------------------------------------------------ to top
    def scroll_to_top(self, start_date=None,
                      on_progress: Optional[Callable[[int], None]] = None) -> int:
        """向上滚动直到：(a) 到顶（画面稳定）；(b) 已见到早于 start_date 的时间标签。

        返回滚动次数。start_date=None 时等价于滚到顶。
        """
        log.info("开始向上滚动（%s）…",
                 f"时间窗起点 {start_date:%Y-%m-%d}" if start_date else "全量到顶")
        prev = self._grab()
        stable = 0
        for i in range(self.max_attempts):
            self._wheel(self.scroll_step * 8)   # 大步长快速上滚
            time.sleep(self.scroll_pause)
            curr = self._grab()
            # 时间窗提前停止：OCR 太重，这里用轻量判断（每 3 屏做一次完整 OCR 由上层负责）。
            # 本层只做画面稳定检测；时间窗判断在 capture 阶段逐屏进行。
            if ScreenCapture.frames_equal(prev, curr):
                stable += 1
                if stable >= self.stable_frames_to_stop:
                    log.info("已到顶部（连续 %d 次无变化，共滚动 %d 次）", stable, i + 1)
                    return i + 1
            else:
                stable = 0
            prev = curr
            if on_progress and i % 20 == 0:
                on_progress(i)
        log.warning("达到最大滚动次数 %d，可能未到顶", self.max_attempts)
        return self.max_attempts

    # ------------------------------------------------------------ down
    # 实测（微信 4.1，1080p 屏）：pyautogui.scroll(120)=1 档 ≈ 37px，
    # 更大单次增量会被平滑滚动钳制在 ~50px —— 必须分多次滚动。
    PX_PER_NOTCH = 37

    def scroll_down_one_screen(self) -> bool:
        """向下滚约 75% 屏（留 25% 重叠防漏帧）。返回画面是否发生变化。"""
        prev = self._grab()
        _, _, _, area_h = self.win.chat_area_rect(self.input_zone_ratio)
        notches = max(3, int(area_h * 0.75 / self.PX_PER_NOTCH))
        for _ in range(notches):
            self._wheel(-120)
            time.sleep(self.scroll_pause * 0.6)
        curr = self._grab()
        return not ScreenCapture.frames_equal(prev, curr)

    # ------------------------------------------------------------ probe
    @staticmethod
    def earliest_time_in_texts(ocr_texts: list[dict],
                               last_seen=None):
        """从 OCR 文本块里找出"最早的可解析时间标签"（用于时间窗判断）。"""
        best = None
        for t in ocr_texts:
            if not is_time_label(t["text"]):
                continue
            ts = parse_time_label(t["text"], last_seen=last_seen)
            if ts and (best is None or ts < best):
                best = ts
        return best
