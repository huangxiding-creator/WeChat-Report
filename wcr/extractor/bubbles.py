# -*- coding: utf-8 -*-
"""图片气泡检测（OpenCV 启发式）与语音标记识别。"""
from __future__ import annotations

import cv2
import numpy as np

from .timelabels import VOICE_RE


class ImageBubbleDetector:
    """检测聊天区里的图片消息气泡。

    启发式：边缘密集、面积足够、灰度标准差大的矩形区域，
    且不与 OCR 文字框重叠。
    """

    def __init__(self, image_min_w: int = 90, image_min_h: int = 90,
                 image_min_std: float = 20.0, image_iou_merge: float = 0.3):
        self.image_min_w = image_min_w
        self.image_min_h = image_min_h
        self.image_min_std = image_min_std
        self.image_iou_merge = image_iou_merge

    def detect(self, img_bgr: np.ndarray, ocr_texts: list[dict]) -> list[tuple[int, int, int, int]]:
        gray = cv2.cvtColor(img_bgr, cv2.COLOR_BGR2GRAY)
        edges = cv2.Canny(gray, 40, 120)
        edges = cv2.dilate(edges, np.ones((3, 3), np.uint8), iterations=1)
        contours, _ = cv2.findContours(edges, cv2.RETR_EXTERNAL, cv2.CHAIN_APPROX_SIMPLE)

        h_img, w_img = img_bgr.shape[:2]
        rects = []
        for cnt in contours:
            x, y, w, h = cv2.boundingRect(cnt)
            if w < self.image_min_w or h < self.image_min_h:
                continue
            if w > w_img * 0.95 or h > h_img * 0.95:
                continue
            if self._overlaps_ocr(x, y, w, h, ocr_texts):
                continue
            roi = gray[y:y + h, x:x + w]
            if roi.std() < self.image_min_std:
                continue
            rects.append((x, y, w, h))
        return self._merge(rects)

    def split_voice_marks(self, ocr_texts: list[dict]) -> tuple[list[dict], list[dict]]:
        """把 OCR 文本块分成（语音时长标记, 其余文本）。

        微信语音气泡渲染为 "12''" 样式的时长文字，OCR 常读成 12" / 12″ 等。
        """
        voices, rest = [], []
        for t in ocr_texts:
            if VOICE_RE.match(t["text"]):
                voices.append(t)
            else:
                rest.append(t)
        return voices, rest

    # ------------------------------------------------------------ internals
    def _overlaps_ocr(self, x, y, w, h, ocr_texts, threshold=0.4) -> bool:
        ax1, ay1, ax2, ay2 = x, y, x + w, y + h
        for t in ocr_texts:
            bx1, by1 = t["box"][:, 0].min(), t["box"][:, 1].min()
            bx2, by2 = t["box"][:, 0].max(), t["box"][:, 1].max()
            ix1, iy1 = max(ax1, bx1), max(ay1, by1)
            ix2, iy2 = min(ax2, bx2), min(ay2, by2)
            if ix1 < ix2 and iy1 < iy2:
                inter = (ix2 - ix1) * (iy2 - iy1)
                ocr_area = max(1, (bx2 - bx1) * (by2 - by1))
                if inter / ocr_area > threshold:
                    return True
        return False

    def _merge(self, rects: list[tuple[int, int, int, int]]) -> list[tuple[int, int, int, int]]:
        merged = []
        for r in sorted(rects, key=lambda r: (r[1], r[0])):
            for i, m in enumerate(merged):
                if (self._iou(r, m) > self.image_iou_merge
                        or self._contains(m, r) or self._contains(r, m)):
                    merged[i] = self._union(m, r)
                    break
            else:
                merged.append(r)
        return merged

    @staticmethod
    def _iou(a, b) -> float:
        ax1, ay1, ax2, ay2 = a[0], a[1], a[0] + a[2], a[1] + a[3]
        bx1, by1, bx2, by2 = b[0], b[1], b[0] + b[2], b[1] + b[3]
        ix1, iy1, ix2, iy2 = max(ax1, bx1), max(ay1, by1), min(ax2, bx2), min(ay2, by2)
        if ix1 >= ix2 or iy1 >= iy2:
            return 0.0
        inter = (ix2 - ix1) * (iy2 - iy1)
        union = a[2] * a[3] + b[2] * b[3] - inter
        return inter / max(1, union)

    @staticmethod
    def _contains(outer, inner) -> bool:
        ox1, oy1, ox2, oy2 = outer[0], outer[1], outer[0] + outer[2], outer[1] + outer[3]
        ix1, iy1, ix2, iy2 = inner[0], inner[1], inner[0] + inner[2], inner[1] + inner[3]
        return ox1 <= ix1 and oy1 <= iy1 and ix2 <= ox2 and iy2 <= oy2

    @staticmethod
    def _union(a, b):
        ax1, ay1, ax2, ay2 = a[0], a[1], a[0] + a[2], a[1] + a[3]
        bx1, by1, bx2, by2 = b[0], b[1], b[0] + b[2], b[1] + b[3]
        x1, y1, x2, y2 = min(ax1, bx1), min(ay1, by1), max(ax2, bx2), max(ay2, by2)
        return (x1, y1, x2 - x1, y2 - y1)
