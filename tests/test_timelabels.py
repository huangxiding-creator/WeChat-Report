# -*- coding: utf-8 -*-
"""时间标签与时间窗解析测试。"""
import unittest
from datetime import datetime

from wcr.extractor.timelabels import (
    is_time_label, parse_time_label, parse_window, VOICE_RE,
)

NOW = datetime(2026, 8, 27, 20, 0, 0)  # 周四


class TestTimeLabel(unittest.TestCase):
    def test_is_label(self):
        for t in ["14:30", "14:30:25", "昨天 14:30", "前天 09:05",
                  "星期二 14:30", "周三 08:00", "2026年8月5日",
                  "2026年8月5日 14:30", "8月5日 14:30"]:
            self.assertTrue(is_time_label(t), t)

    def test_not_label(self):
        for t in ["开会时间是14:30分", "收到", "2026年8月", "张三：你好"]:
            self.assertFalse(is_time_label(t), t)

    def test_full_date(self):
        self.assertEqual(parse_time_label("2026年8月5日", NOW),
                         datetime(2026, 8, 5))
        self.assertEqual(parse_time_label("2026年8月5日 14:30", NOW),
                         datetime(2026, 8, 5, 14, 30))

    def test_absurd_year_rejected(self):
        """OCR 把 "2026年7月25日" 误读成 "2005年7月25日" → 按乱读返回 None
        （否则上滚探测假性到窗起点 + 消息被盖远古日期后遭窗过滤丢弃）。"""
        self.assertIsNone(parse_time_label("2005年7月25日", NOW))
        self.assertIsNone(parse_time_label("1999年12月31日", NOW))
        self.assertIsNone(parse_time_label("2027年1月1日", NOW))   # 未来年也不认
        # 正常历史年份照常解析（本账号最老会话 2024/06）
        self.assertEqual(parse_time_label("2024年6月30日", NOW),
                         datetime(2024, 6, 30))

    def test_absurd_clock_rejected(self):
        """乱读时刻（正则 \d{1,2} 挡不住 24:41/12:60，OCR 数字翻转所致）→
        返回 None 而非抛异常（实测 2026-09-06 赵A事务所上滚探测即死于此：
        'hour must be in 0..23' 杀掉整个导出）。"""
        self.assertIsNone(parse_time_label("24:41", NOW))
        self.assertIsNone(parse_time_label("昨天 24:15", NOW))
        self.assertIsNone(parse_time_label("星期二 25:00", NOW))
        self.assertIsNone(parse_time_label("12:60", NOW))
        self.assertIsNone(parse_time_label("99:99", NOW))

    def test_md_no_year(self):
        self.assertEqual(parse_time_label("3月16日", NOW),
                         datetime(2026, 3, 16))
        # 未来日期 → 去年
        self.assertEqual(parse_time_label("12月30日", NOW),
                         datetime(2025, 12, 30))

    def test_weekday(self):
        # NOW 是周四；"星期二" → 2 天前
        self.assertEqual(parse_time_label("星期二 10:00", NOW),
                         datetime(2026, 8, 25, 10, 0))

    def test_yesterday(self):
        self.assertEqual(parse_time_label("昨天 14:30", NOW),
                         datetime(2026, 8, 26, 14, 30))
        self.assertEqual(parse_time_label("前天 09:05", NOW),
                         datetime(2026, 8, 25, 9, 5))

    def test_clock_only(self):
        self.assertEqual(parse_time_label("10:00", NOW),
                         datetime(2026, 8, 27, 10, 0))
        # 晚于当前时刻 → 昨天
        self.assertEqual(parse_time_label("23:00", NOW),
                         datetime(2026, 8, 26, 23, 0))

    def test_voice_re(self):
        for t in ["12''", '12"', "60″", "5'"]:
            self.assertTrue(VOICE_RE.match(t), t)
        for t in ["12", "12:30", "abc''"]:
            self.assertFalse(VOICE_RE.match(t), t)


class TestWindow(unittest.TestCase):
    def test_empty(self):
        self.assertEqual(parse_window("", NOW), (None, None))

    def test_days(self):
        s, e = parse_window("7d", NOW)
        self.assertEqual(s, datetime(2026, 8, 21))
        self.assertEqual(e, NOW)

    def test_month(self):
        s, e = parse_window("2026-03", NOW)
        self.assertEqual((s.year, s.month, s.day), (2026, 3, 1))
        self.assertEqual((e.year, e.month, e.day, e.hour), (2026, 3, 31, 23))

    def test_range(self):
        s, e = parse_window("2026-01-01~2026-08-27", NOW)
        self.assertEqual(s, datetime(2026, 1, 1))
        self.assertEqual(e, datetime(2026, 8, 27, 23, 59, 59))
        s, e = parse_window("2026-01-01~", NOW)
        self.assertEqual(s, datetime(2026, 1, 1))
        self.assertIsNone(e)

    def test_invalid(self):
        with self.assertRaises(ValueError):
            parse_window(" nonsense ")


if __name__ == "__main__":
    unittest.main()
