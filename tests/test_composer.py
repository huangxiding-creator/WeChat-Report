# -*- coding: utf-8 -*-
"""消息组装器测试：去重、排序、时间回填、语音标记、断点。"""
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

import numpy as np

from wcr.extractor.composer import MessageComposer, _NOISE_RE


def fake_ocr(text, x, y, w=120, h=24):
    box = np.array([[x, y], [x + w, y], [x + w, y + h], [x, y + h]], dtype=np.int32)
    return {"text": text, "box": box, "cx": x + w // 2, "cy": y + h // 2, "score": 0.9}


class TestComposer(unittest.TestCase):
    def _composer(self):
        return MessageComposer(speaker_attribution=True)

    def test_dedup_across_screens(self):
        c = self._composer()
        img = np.zeros((600, 800, 3), dtype=np.uint8)
        d = Path(tempfile.mkdtemp())
        a = c.add_screen(0, [fake_ocr("会议纪要已发邮箱", 300, 100)], [], [], img, d, 800)
        b = c.add_screen(1, [fake_ocr("会议纪要已发邮箱", 300, 80)], [], [], img, d, 800)
        self.assertEqual(a, 1)
        self.assertEqual(b, 0)  # 同文本跨屏去重
        self.assertEqual(len(c.messages), 1)

    def test_noise_filtered(self):
        self.assertTrue(_NOISE_RE.match("以下为新消息"))
        for ui_text in ("113条新消息", "跳转到最新消息", "查看更多消息", "对方正在输入…"):
            self.assertTrue(_NOISE_RE.match(ui_text), ui_text)
        c = self._composer()
        img = np.zeros((600, 800, 3), dtype=np.uint8)
        d = Path(tempfile.mkdtemp())
        n = c.add_screen(0, [fake_ocr("以下为新消息", 300, 100)], [], [], img, d, 800)
        self.assertEqual(n, 0)

    def test_voice_marks(self):
        c = self._composer()
        img = np.zeros((600, 800, 3), dtype=np.uint8)
        d = Path(tempfile.mkdtemp())
        voices, rest = c.__class__.__mro__[0], None
        # 直接构造 voice_marks 输入
        n = c.add_screen(0, [fake_ocr("收到请回复", 300, 200)], [], [],
                         img, d, 800)
        # voice_marks 单独加：模拟 OCR 读到 12''
        from wcr.extractor.bubbles import ImageBubbleDetector
        det = ImageBubbleDetector()
        v, r = det.split_voice_marks([fake_ocr("12''", 300, 300)])
        self.assertEqual(len(v), 1)
        self.assertEqual(len(r), 0)
        n2 = c.add_screen(1, [], [], v, img, d, 800)
        self.assertEqual(n2, 1)
        self.assertEqual(c.messages[-1].kind, "voice")
        self.assertEqual(c.messages[-1].text, "12''")

    def test_side_attribution(self):
        c = self._composer()
        img = np.zeros((600, 800, 3), dtype=np.uint8)
        d = Path(tempfile.mkdtemp())
        c.add_screen(0, [
            fake_ocr("李琦", 300, 100),          # 左侧昵称
            fake_ocr("设备已上线", 320, 140),     # 左侧正文
            fake_ocr("好的收到", 600, 200),       # 右侧（我方）
        ], [], [], img, d, 800)
        left = [m for m in c.messages if m.side == "left"]
        right = [m for m in c.messages if m.side == "right"]
        self.assertTrue(left)
        self.assertTrue(right)
        body = [m for m in left if m.text == "设备已上线"][0]
        self.assertEqual(body.speaker, "李琦")

    def test_time_assign(self):
        c = self._composer()
        img = np.zeros((600, 800, 3), dtype=np.uint8)
        d = Path(tempfile.mkdtemp())
        c.add_screen(0, [
            fake_ocr("2026年8月5日", 400, 50),
            fake_ocr("上午完成配置", 300, 120),
            fake_ocr("14:30", 400, 220),
            fake_ocr("下午开始测试", 300, 280),
        ], [], [], img, d, 800)
        labels = {0: [
            {"text": "2026年8月5日", "cy": 62},
            {"text": "14:30", "cy": 232},
        ]}
        c.assign_times(labels)
        m1 = [m for m in c.messages if m.text == "上午完成配置"][0]
        m2 = [m for m in c.messages if m.text == "下午开始测试"][0]
        self.assertEqual(m1.time_label, "2026年8月5日")
        self.assertEqual(m1.timestamp, datetime(2026, 8, 5))
        self.assertEqual(m2.time_label, "14:30")
        self.assertEqual(m2.timestamp, datetime(2026, 8, 5, 14, 30))

    def test_checkpoint_roundtrip(self):
        c = self._composer()
        img = np.zeros((600, 800, 3), dtype=np.uint8)
        d = Path(tempfile.mkdtemp())
        c.add_screen(0, [fake_ocr("断点测试", 300, 100)], [], [], img, d, 800)
        ckpt = d / "ck.json"
        c.save_checkpoint(ckpt)
        c2 = self._composer()
        self.assertTrue(c2.load_checkpoint(ckpt))
        self.assertEqual(len(c2.messages), 1)
        self.assertEqual(c2.messages[0].text, "断点测试")
        # 恢复后继续去重
        n = c2.add_screen(1, [fake_ocr("断点测试", 300, 80)], [], [], img, d, 800)
        self.assertEqual(n, 0)


if __name__ == "__main__":
    unittest.main()
