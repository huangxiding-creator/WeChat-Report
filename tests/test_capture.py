# -*- coding: utf-8 -*-
"""截图模块测试：中文路径安全写图、帧比较。"""
import tempfile
import unittest
from pathlib import Path

import numpy as np

from wcr.extractor.capture import ScreenCapture, imwrite_png


class TestImwriteUnicode(unittest.TestCase):
    def test_cjk_path_roundtrip(self):
        """中文目录/文件名写图后必须能按原路径读回（cv2.imwrite 会乱码）。"""
        d = Path(tempfile.mkdtemp()) / "畅聊1群" / "子目录"
        p = d / "screen0001.png"
        img = np.full((40, 60, 3), 128, dtype=np.uint8)
        self.assertTrue(imwrite_png(p, img))
        self.assertTrue(p.exists())
        self.assertGreater(p.stat().st_size, 100)

    def test_creates_parent_dirs(self):
        p = Path(tempfile.mkdtemp()) / "新建文件夹" / "a" / "b.png"
        self.assertTrue(imwrite_png(p, np.zeros((10, 10, 3), np.uint8)))
        self.assertTrue(p.exists())


class TestFramesEqual(unittest.TestCase):
    def test_equal_and_diff(self):
        a = np.full((20, 20, 3), 100, np.uint8)
        self.assertTrue(ScreenCapture.frames_equal(a, a.copy()))
        b = a.copy()
        b[:] = 240
        self.assertFalse(ScreenCapture.frames_equal(a, b))

    def test_shape_mismatch(self):
        a = np.zeros((20, 20, 3), np.uint8)
        b = np.zeros((30, 20, 3), np.uint8)
        self.assertFalse(ScreenCapture.frames_equal(a, b))


if __name__ == "__main__":
    unittest.main()
