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
        return self._filter(self.parse_raw(img_bgr, floor=self.score_threshold),
                            self.score_threshold)

    def parse_raw(self, img_bgr: np.ndarray, floor: float = 0.1) -> list[dict]:
        """一次推理返回 ≥floor 的全部文本块（不过 parser 阈值）。

        供"分级阈值"消费：会话名要干净（0.4+），右列时间戳是更淡的
        小灰字（老聊天 2024/* 戳实测常落在 0.28~0.4）——同一帧一次
        推理，两种阈值各取所需。
        """
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
            if score < floor or not text:
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

    @staticmethod
    def _filter(blocks: list[dict], threshold: float) -> list[dict]:
        return [t for t in blocks if t["score"] >= threshold]
