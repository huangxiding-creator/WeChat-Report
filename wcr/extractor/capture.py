# -*- coding: utf-8 -*-
"""高速区域截图（mss）。"""
from __future__ import annotations

from pathlib import Path

import numpy as np


def imwrite_png(path, img) -> bool:
    """中文路径安全写图：cv2.imwrite 在 Windows 会把非 ASCII 路径按 ANSI
    码页编码，产生乱码文件名（如 畅聊1群→鐣呰亰1缇）。统一走 imencode + write_bytes。
    """
    import cv2

    ok, buf = cv2.imencode(".png", img)
    if ok:
        Path(path).parent.mkdir(parents=True, exist_ok=True)
        Path(path).write_bytes(buf.tobytes())
    return ok


class ScreenCapture:
    """对指定屏幕区域高速截图，返回 numpy BGR 数组。"""

    def __init__(self, region: tuple[int, int, int, int]):
        self.region = region

    def grab(self) -> np.ndarray:
        import mss

        l, t, w, h = self.region
        with mss.mss() as sct:
            monitor = {"left": l, "top": t, "width": w, "height": h}
            shot = sct.grab(monitor)
        return np.array(shot)[:, :, :3]  # BGRA -> BGR

    @staticmethod
    def frames_equal(a: np.ndarray, b: np.ndarray, mean_eps: float = 1.5) -> bool:
        if a.shape != b.shape:
            return False
        diff = np.abs(a.astype(np.int16) - b.astype(np.int16)).mean()
        return diff < mean_eps
