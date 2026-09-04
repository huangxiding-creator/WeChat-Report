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


def imread_png(path):
    """中文路径安全读图：cv2.imread 在 Windows 对非 ASCII 路径直接返回
    None（实测 output/批量导出/…png 全部 WARN can't open），统一走
    np.fromfile + imdecode。失败返回 None（调用方静默降级）。
    """
    import cv2

    try:
        buf = np.fromfile(str(path), dtype=np.uint8)
    except (OSError, ValueError):
        return None
    if buf.size == 0:
        return None
    return cv2.imdecode(buf, cv2.IMREAD_COLOR)


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
