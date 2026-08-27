# -*- coding: utf-8 -*-
"""配置管理 + 只读安全护栏测试。"""
import tempfile
import unittest
from pathlib import Path

from wcr.config import Config
from wcr.extractor.safety import SafetyGuard, ReadOnlyViolation

WIN = (100, 100, 1000, 800)  # left, top, w, h


class TestConfig(unittest.TestCase):
    def test_defaults(self):
        cfg = Config()
        self.assertEqual(cfg.get("ai", "vision_model"), "glm-4v-flash")
        self.assertTrue(cfg.get_bool("extract", "checkpoint_enabled"))
        self.assertEqual(cfg.get_int("extract", "max_screens"), 3000)
        self.assertAlmostEqual(cfg.get_float("extract", "scroll_pause"), 0.35)

    def test_roundtrip(self):
        d = Path(tempfile.mkdtemp())
        p = d / "config.ini"
        cfg = Config(p)
        cfg.set("extract", "scroll_pause", 0.55)
        cfg.save(p)
        cfg2 = Config.load(p)
        self.assertAlmostEqual(cfg2.get_float("extract", "scroll_pause"), 0.55)

    def test_missing_falls_back(self):
        cfg = Config(Path(tempfile.mkdtemp()) / "nonexistent.ini")
        self.assertEqual(cfg.get("ai", "base_url"),
                         "https://open.bigmodel.cn/api/paas/v4")

    def test_models_list(self):
        cfg = Config()
        models = cfg.text_models()
        self.assertIn("glm-4.5-flash", models)
        self.assertTrue(all("flash" in m for m in models))  # 免费模型

    def test_example_sanitized(self):
        d = Path(tempfile.mkdtemp())
        cfg = Config(d / "no.ini")
        p = cfg.write_example(d / "example.ini")
        text = p.read_text(encoding="utf-8-sig")
        self.assertEqual(text.count("api_key = \n") + text.count("api_key =\n"), 1)
        self.assertNotIn("4a84c545", text)


class TestSafetyGuard(unittest.TestCase):
    def setUp(self):
        self.g = SafetyGuard(input_zone_ratio=0.80)

    def test_forbidden_key(self):
        for k in ["enter", "Enter", "ctrl+enter", "ctrl+v"]:
            with self.assertRaises(ReadOnlyViolation):
                self.g.check_key(k)

    def test_input_zone_blocked(self):
        # 窗口 (100,100,1000,800)：输入区顶 = 100 + 640 = 740
        with self.assertRaises(ReadOnlyViolation):
            self.g.check_click(500, 750, WIN)
        self.g.check_click(500, 700, WIN)  # 正常聊天区不抛

    def test_out_of_window(self):
        with self.assertRaises(ReadOnlyViolation):
            self.g.check_click(50, 500, WIN)
        with self.assertRaises(ReadOnlyViolation):
            self.g.check_click(1200, 500, WIN)

    def test_nav_click_zone(self):
        # 导航白名单：x < 100+300=400 且 y < 100+440=540
        self.g.check_nav_click(200, 200, WIN)
        with self.assertRaises(ReadOnlyViolation):
            self.g.check_nav_click(600, 200, WIN)   # 消息区不允许导航点击
        with self.assertRaises(ReadOnlyViolation):
            self.g.check_nav_click(200, 600, WIN)   # 过低不允许

    def test_right_click_blocked(self):
        with self.assertRaises(ReadOnlyViolation):
            self.g.check_no_right_click()


if __name__ == "__main__":
    unittest.main()
