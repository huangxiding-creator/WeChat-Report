# -*- coding: utf-8 -*-
"""OCR 封装（RapidOCR，CPU 离线）。"""
from __future__ import annotations

import numpy as np


class OCRParser:
    """返回带坐标的文本块：[{text, box, cx, cy, score}]。"""

    _engine = None  # 进程级单例（模型加载约 1~2 秒）

    def __init__(self, score_threshold: float = 0.5):
        self.score_threshold = score_threshold

    @classmethod
    def engine(cls):
        if cls._engine is None:
            try:
                from rapidocr_onnxruntime import RapidOCR
            except ImportError as e:
                raise RuntimeError(
                    "缺少 rapidocr-onnxruntime，请 pip install rapidocr-onnxruntime"
                ) from e
            cls._engine = RapidOCR()
        return cls._engine

    def parse(self, img_bgr: np.ndarray) -> list[dict]:
        result, _ = self.engine()(img_bgr)
        if not result:
            return []
        out = []
        for box, text, score in result:
            text = (text or "").strip()
            try:
                score = float(score)
            except (TypeError, ValueError):
                score = 0.0
            if score < self.score_threshold or not text:
                continue
            box = np.array(box, dtype=np.int32)
            out.append({
                "text": text,
                "box": box,
                "cx": int(box[:, 0].mean()),
                "cy": int(box[:, 1].mean()),
                "score": score,
            })
        return out
