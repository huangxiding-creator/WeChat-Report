# -*- coding: utf-8 -*-
"""微信聊天导航：受控切换到指定群聊/好友（零键盘、零危险区）。

两种模式：
  manual  — 倒计时等待用户人工点开目标聊天（最稳）
  session — OCR 读左侧会话列表，点击标题匹配项（近期活跃聊天可直达）

安全设计：
  × 只点击会话列表区域（几何白名单 + SafetyGuard.check_nav_click 双重校验）
  × 全程不使用键盘
  × 点击后验证聊天确实已打开（OCR 标题头），失败自动重试一次
"""
from __future__ import annotations

import logging
import time
from typing import Callable, Optional

from .capture import ScreenCapture
from .ocr import OCRParser
from .safety import SafetyGuard

log = logging.getLogger("wcr.navigator")


class WeChatNavigator:
    def __init__(self, win, guard: SafetyGuard,
                 settle_wait: float = 1.5,
                 input_zone_ratio: float = 0.80):
        self.win = win
        self.guard = guard
        self.settle_wait = settle_wait
        self.input_zone_ratio = input_zone_ratio

    # ------------------------------------------------------------ manual
    def manual(self, chat_name: str, countdown: int = 10,
               on_progress: Optional[Callable[[str], None]] = None) -> None:
        say = on_progress or (lambda m: log.info(m))
        say(f"➡ 请在 {countdown} 秒内手动点开聊天「{chat_name}」并保持微信窗口在最前 …")
        for i in range(countdown, 0, -1):
            say(f"   {i} …")
            time.sleep(1)
        time.sleep(self.settle_wait)

    # ------------------------------------------------------------ session
    def by_session_list(self, chat_name: str,
                        on_progress: Optional[Callable[[str], None]] = None,
                        retries: int = 2) -> bool:
        say = on_progress or (lambda m: log.info(m))
        ocr = OCRParser(0.4)

        for attempt in range(retries):
            self.win.activate()
            region = self.win.session_list_rect()
            img = ScreenCapture(region).grab()
            texts = ocr.parse(img)
            if not texts:
                say("   会话列表 OCR 为空")
                continue

            target = chat_name.strip()
            exact = [t for t in texts if t["text"].strip() == target]
            partial = [t for t in texts
                       if target in t["text"] or t["text"] in target]
            cand = exact or partial
            if not cand:
                say(f"   会话列表中未找到「{target}」（可见 {len(texts)} 项），"
                    "回落手动模式或先在微信里打开该聊天")
                return False

            t = cand[0]
            abs_x = region[0] + t["cx"]
            abs_y = region[1] + t["cy"]
            try:
                self.guard.check_nav_click(abs_x, abs_y, self.win.rect)
            except Exception as e:
                say(f"   ⚠ 候选坐标被安全护栏拒绝（{e}），回落手动模式")
                return False
            say(f"   点击会话「{t['text']}」 @ ({abs_x},{abs_y})")
            self._click(abs_x, abs_y)
            time.sleep(self.settle_wait)

            if self.verify_chat_open(chat_name, ocr, say):
                return True
            say(f"   ⚠ 第 {attempt + 1} 次点击后未检测到目标聊天标题，重试 …")

        return self.verify_chat_open(chat_name, ocr, say)

    # ------------------------------------------------------------ verify
    def verify_chat_open(self, chat_name: str, ocr: OCRParser,
                         say: Callable[[str], None]) -> bool:
        """OCR 聊天面板标题区，确认目标聊天名出现。"""
        try:
            region = self.win.chat_header_rect()
            img = ScreenCapture(region).grab()
            texts = ocr.parse(img)
            header = " ".join(t["text"] for t in texts)
            target = chat_name.strip()
            ok = (target in header) or any(
                target in t["text"] or t["text"] in target
                for t in texts if len(t["text"]) >= 2)
            say(f"   标题头 OCR：{header[:40]!r} → {'✅ 已打开' if ok else '❌ 未匹配'}")
            return ok
        except Exception as e:
            log.warning("标题验证异常：%s", e)
            return False

    # ------------------------------------------------------------ internals
    def _click(self, x: int, y: int) -> None:
        import pyautogui

        pyautogui.moveTo(x, y, duration=0.2)
        pyautogui.click(button="left")   # 仅左键；右键被 SafetyGuard 语义禁用
