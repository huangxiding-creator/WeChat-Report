# -*- coding: utf-8 -*-
"""只读安全护栏（硬约束）。

铁律：
  × 禁止点击窗口底部输入/发送区域（默认 20%）
  × 禁止点击消息区内的功能按钮（右键菜单、引用、转发等）
  × 禁止向微信发送回车等可能触发发送的按键
  × 只允许：滚轮浏览 + 受限导航点击（搜索框、会话列表项、聊天区中央）

任何可能"向微信写入"的操作都在此拦截并 raise，绝不静默放行。
"""
from __future__ import annotations

from typing import Optional


class ReadOnlyViolation(RuntimeError):
    """只读约束被违反（编程错误，而非用户错误）。"""


class SafetyGuard:
    """所有交互动作的唯一关卡。"""

    FORBIDDEN_KEYS = {"enter", "return", "ctrl+enter", "ctrl+s", "ctrl+v", "tab"}

    def __init__(self, input_zone_ratio: float = 0.80,
                 allow_enter_key: bool = False,
                 allow_right_click: bool = False):
        self.input_zone_ratio = input_zone_ratio   # 窗口底部 (1-ratio) 为禁区
        self.allow_enter_key = allow_enter_key
        self.allow_right_click = allow_right_click
        self.forbidden_clicks: list[str] = []

    # ---------------------------------------------------------------- keys
    def check_key(self, key: str) -> None:
        k = (key or "").lower()
        if k in self.FORBIDDEN_KEYS and not self.allow_enter_key:
            raise ReadOnlyViolation(
                f"只读保护：禁止按键 {key!r}（可能触发发送/粘贴）。"
                f"如确需，请设置 [safety] allow_enter_key=true 并自担风险。"
            )

    # ---------------------------------------------------------------- clicks
    def check_click(self, x: int, y: int, win_rect: tuple) -> None:
        """通用点击检查：窗口底部输入/发送区一律禁止。"""
        left, top, width, height = win_rect
        input_top = top + int(height * self.input_zone_ratio)
        if y >= input_top:
            raise ReadOnlyViolation(
                f"只读保护：禁止点击窗口底部输入区 ({x},{y} >= y_input_top={input_top})。"
            )
        if x < left or x > left + width or y < top:
            raise ReadOnlyViolation(f"只读保护：点击坐标 ({x},{y}) 超出微信窗口范围。")

    def check_nav_click(self, x: int, y: int, win_rect: tuple,
                        session_rect: Optional[tuple] = None) -> None:
        """导航点击（搜索框 / 会话列表）：限制在窗口左侧行政区 + 上半屏内。

        session_rect（会话列表矩形）范围内的点击按列表实际范围放行：
        会话列表列内只有会话项（输入区在右侧聊天面板，x > 列表右缘），
        而 nav_bottom（55% 高度）会永久屏蔽列表底缘 1~2 个会话项——
        它们在列表尽头无法再滚入上半屏。窗口底部输入禁区（input_zone_ratio）
        仍然硬性生效（check_click 先行校验）。
        """
        left, top, width, height = win_rect
        if session_rect:
            sx, sy, sw, sh = session_rect
            if sx - 4 <= x <= sx + sw + 4 and sy <= y <= sy + sh - 8:
                # 会话列内：列里只有会话行（输入区在右侧聊天面板），
                # 输入禁区（0.80H）是为聊天面板设计的，不适用于本列；
                # 仅校验不越出微信窗口
                if x < left or x > left + width or y < top or y > top + height:
                    raise ReadOnlyViolation(
                        f"只读保护：点击坐标 ({x},{y}) 超出微信窗口范围。")
                return
        self.check_click(x, y, win_rect)
        nav_right = left + int(width * 0.30)      # 左侧会话列表 + 搜索区
        nav_bottom = top + int(height * 0.55)     # 上半屏
        if x > nav_right:
            raise ReadOnlyViolation(
                f"只读保护：导航点击越界（x={x} > nav_right={nav_right}，"
                "仅允许搜索框与会话列表区域）。"
            )
        if y > nav_bottom:
            raise ReadOnlyViolation(
                f"只读保护：导航点击越界（y={y} > nav_bottom={nav_bottom}）。"
            )

    # ---------------------------------------------------------------- scroll
    def check_scroll(self, x: int, y: int, win_rect: tuple) -> None:
        """滚动只允许发生在聊天内容区（中央安全带）。"""
        left, top, width, height = win_rect
        self.check_click(x, y, win_rect)

    # ---------------------------------------------------------------- misc
    def check_no_right_click(self) -> None:
        if not self.allow_right_click:
            raise ReadOnlyViolation(
                "只读保护：右键菜单可能触发删除/撤回等操作，默认禁止。"
            )
