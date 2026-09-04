# -*- coding: utf-8 -*-
"""微信 PC 窗口定位与几何计算（只读）。

微信 4.x 布局（关键实测结论）：
  左侧图标栏 ~64px + 会话列表 **固定约 256px**（不随窗口宽度等比缩放）
  → 聊天面板左缘 ≈ 窗口左 + 320px。小窗口（如 896px 宽）时按比例
    （如 22%）计算会落进会话列表，导致 OCR 混入列表时间戳。
  因此聊天区左缘用「像素分界线检测」动态标定，失败回落 36% 经验值。
"""
from __future__ import annotations

import logging
import time

import cv2
import numpy as np

log = logging.getLogger("wcr.window")


class WeChatWindow:
    """定位微信（4.x Weixin.exe）主窗口，计算聊天内容区矩形。"""

    TITLE_HINTS = ("微信", "WeChat")

    _EXCLUDE_SUBSTR = (
        "visual studio", "vscode", "chrome", "edge", "firefox",
        "notepad", " intellij", " pycharm", ".md", ".txt", ".py",
        "wechat-report", "文件传输助手",
    )

    def __init__(self) -> None:
        self.win = None
        self.rect: tuple[int, int, int, int] = (0, 0, 0, 0)  # left, top, w, h
        self._chat_left_rel: int | None = None   # 标定结果（相对窗口 x 偏移）

    # ------------------------------------------------------------ find
    def find(self) -> "WeChatWindow":
        try:
            import pygetwindow as gw
        except ImportError as e:
            raise RuntimeError("缺少 pygetwindow，请 pip install pygetwindow") from e

        candidates: list = []
        for hint in self.TITLE_HINTS:
            try:
                candidates.extend(gw.getWindowsWithTitle(hint))
            except Exception:
                pass
        if not candidates:
            raise RuntimeError(
                "找不到微信主窗口。请确认微信 PC 端已登录且未退出（托盘最小化可被自动恢复）。"
            )

        exact = [w for w in candidates if w.title.strip() in ("微信", "WeChat")]
        pool = exact
        if not exact:
            filtered = [
                w for w in candidates
                if not any(s in w.title.lower() for s in self._EXCLUDE_SUBSTR)
            ]
            pool = filtered or candidates

        self.win = max(pool, key=lambda w: w.width * w.height)
        self.rect = (self.win.left, self.win.top, self.win.width, self.win.height)
        log.info("定位微信窗口: rect=%s 标题=%r", self.rect, self.win.title)
        return self

    # ------------------------------------------------------------ state
    def is_minimized(self) -> bool:
        return self.rect[0] <= -30000 and self.rect[1] <= -30000

    def restore(self) -> None:
        try:
            if hasattr(self.win, "restore"):
                self.win.restore()
            elif hasattr(self.win, "unminimize"):
                self.win.unminimize()
            time.sleep(0.8)
            self.refresh()
        except Exception as e:
            log.warning("restore 失败: %s", e)

    def refresh(self) -> None:
        if self.win is not None:
            self.rect = (self.win.left, self.win.top, self.win.width, self.win.height)

    def activate(self) -> None:
        if self.is_minimized():
            self.restore()
        try:
            self.win.activate()
        except Exception as e:
            log.warning("activate 失败（可忽略）: %s", e)
        time.sleep(0.6)
        self.refresh()

    # ------------------------------------------------------------ calibrate
    def calibrate(self, window_img_bgr: np.ndarray) -> int:
        """检测「会话列表 | 聊天面板」分界线，返回聊天区相对窗口的 x 偏移。

        原理：分界处存在一条贯穿多数行高的竖直弱边缘。
        搜索范围 10%~60% 宽度（跳过左侧图标栏 ~64px 处的强边缘）。
        回落值用 min(36%, 340px)：微信 4.x 会话列表为固定宽度（64+256），
        大窗口下按比例计算会严重偏右。
        """
        h, w = window_img_bgr.shape[:2]
        gray = cv2.cvtColor(window_img_bgr, cv2.COLOR_BGR2GRAY)
        grad = np.abs(np.diff(gray.astype(np.int16), axis=1))   # (h, w-1)
        strong = (grad > 6).astype(np.uint8)
        # 竖线应为连续强边缘：用 3px 宽度收缩抑制文字噪声
        kernel = np.ones((1, 3), np.uint8)
        strong = cv2.erode(strong, kernel, iterations=1)
        col_score = strong.sum(axis=0)

        # 下限 110px：跳过图标栏边缘与会话头像列左缘（~x=91，实测最强假边）
        lo = max(110, int(w * 0.10))
        hi = int(w * 0.60)
        fallback = min(int(w * 0.36), 340)
        if hi <= lo:
            self._chat_left_rel = fallback
            return fallback
        seg = col_score[lo:hi]
        best = int(np.argmax(seg)) + lo
        best_score = col_score[best]
        if best_score >= h * 0.30:      # 贯穿至少 30% 行高才算分界线
            self._chat_left_rel = best + 6
        else:
            self._chat_left_rel = fallback
            log.warning("分界线检测不确定（score=%d@x=%d），回落 %dpx",
                        best_score, best, fallback)
        log.info("聊天面板左缘标定：窗口内 x=%d（%.0f%%）",
                 self._chat_left_rel, self._chat_left_rel / w * 100)
        return self._chat_left_rel

    @property
    def chat_left_rel(self) -> int:
        if self._chat_left_rel is None:
            return min(int(self.rect[2] * 0.36), 340)
        return self._chat_left_rel

    # ------------------------------------------------------------ geometry
    def input_zone_top(self, input_zone_ratio: float = 0.80) -> int:
        left, top, width, height = self.rect
        return top + int(height * input_zone_ratio)

    def chat_area_rect(self, input_zone_ratio: float = 0.80) -> tuple[int, int, int, int]:
        """聊天内容区（绝对屏幕坐标）：分界线右侧、标题栏以下、输入区以上。"""
        left, top, width, height = self.rect
        x0 = left + self.chat_left_rel + 8
        y0 = top + max(int(height * 0.08), 60)          # 跳过聊天标题头
        x1 = left + width - 24                            # 右侧留边
        y1 = top + int(height * (input_zone_ratio - 0.03))
        return (x0, y0, x1 - x0, max(120, y1 - y0))

    def chat_center(self, input_zone_ratio: float = 0.80) -> tuple[int, int]:
        l, t, w, h = self.chat_area_rect(input_zone_ratio)
        return (l + w // 2, t + h // 2)

    def chat_header_rect(self) -> tuple[int, int, int, int]:
        """聊天面板标题区（用于导航验证：应显示目标聊天名）。

        实测（微信 4.1，704px 高窗口）：标题文字落在窗口相对 y≈7%~9%，
        旧版 0~6.5% 的裁剪带正好切在字的上缘。
        """
        left, top, width, height = self.rect
        x0 = left + self.chat_left_rel + 8
        w_panel = max(120, width - self.chat_left_rel - 40)
        return (x0, top + int(height * 0.045), w_panel, max(36, int(height * 0.075)))

    def session_list_rect(self) -> tuple[int, int, int, int]:
        """会话列表区（绝对坐标）：图标栏右侧到分界线。

        实测（微信 4.1，704px 高窗口）：列表一直延伸到窗口底缘附近
        （条目可见至 y≈0.97H）。旧值 0.65H 把底部 2~4 个会话行
        切出 OCR 范围（白思俊/GM/刘宇峰 一度"不可见"）。
        """
        left, top, width, height = self.rect
        x0 = left + max(64, int(width * 0.045))
        x1 = left + self.chat_left_rel - 10
        y0 = top + int(height * 0.08)
        return (x0, y0, max(60, x1 - x0), int(height * 0.90))

    def search_box_center(self) -> tuple[int, int]:
        left, top, width, height = self.rect
        return (left + int(width * 0.115), top + int(height * 0.045))

    def session_list_center_x(self) -> int:
        left, top, width, _ = self.rect
        return left + int(width * 0.11)
