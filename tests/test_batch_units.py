# -*- coding: utf-8 -*-
"""批量导出测试：会话列表聚簇（纯函数）+ 聊天记录 docx 生成。"""
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

from docx import Document

from wcr.extractor.navigator import (at_list_top, fuzzy_same,
                                     match_session, scaled_scan_rounds,
                                     window_contains)
from wcr.extractor.safety import ReadOnlyViolation, SafetyGuard
from wcr.extractor.session_enum import (DEFAULT_SKIP, cluster_sessions,
                                         merge_round_names)
from wcr.batch import dedupe_names
from wcr.models import Chat, Message
from wcr.report.transcript_docx import build_transcript_docx


def ocr_item(text, cx, cy):
    return {"text": text, "cx": cx, "cy": cy, "score": 0.9}


class TestClusterSessions(unittest.TestCase):
    def test_two_sessions_with_noise(self):
        """两个会话项（名称+预览+时间+徽标）→ 只提取出两个名称。"""
        texts = [
            # 项1（y 90~150）
            ocr_item("【畅聊1群】", 120, 90),
            ocr_item("22:17", 220, 90),               # 右侧时间
            ocr_item("竹言墨雨：找他要5000W刀", 110, 112),  # 预览行
            # 空白 ~40px
            # 项2（y 190~250）
            ocr_item("黄春健", 120, 190),
            ocr_item("21:36", 222, 190),
            ocr_item("[7条]黄春健：收到", 110, 212),
        ]
        out = cluster_sessions(texts, list_width=248)
        self.assertEqual([n for n, _ in out], ["【畅聊1群】", "黄春健"])

    def test_filters_time_and_badge(self):
        """纯时间/徽标文本不进入候选。"""
        texts = [ocr_item("昨天", 220, 90), ocr_item("[56条]", 230, 112),
                 ocr_item("天使小镇丹丹", 120, 190), ocr_item("[视频", 110, 212)]
        out = cluster_sessions(texts, list_width=248)
        self.assertEqual([n for n, _ in out], ["天使小镇丹丹"])

    def test_empty(self):
        self.assertEqual(cluster_sessions([], list_width=248), [])

    def test_anchor_zone_dropped_structurally(self):
        """锚点区（cy<22）任何文本不入名：以搜索/reILILy 乱读都堵死。"""
        texts = [ocr_item("以搜索", 118, 5), ocr_item("reILILy rU: s≤TSH/X", 110, 8),
                 ocr_item("黄藏寺项目值班值守", 150, 31)]
        out = cluster_sessions(texts, list_width=248)
        self.assertEqual(out, [("黄藏寺项目值班值守", 31)])

    def test_multiline_name_only(self):
        """预览缺失的项也能提取名称。"""
        texts = [ocr_item("度量衡工程咨询", 120, 300)]
        out = cluster_sessions(texts, list_width=248)
        self.assertEqual(out, [("度量衡工程咨询", 300)])

    def test_strips_fused_date_stamp(self):
        """OCR 把日期戳粘进长名称尾部（"…专...08/29"）→ 剥离。"""
        texts = [ocr_item("HZS对下支付管理专...08/29", 120, 90)]
        out = cluster_sessions(texts, list_width=248)
        self.assertEqual(out, [("HZS对下支付管理专", 90)])

    def test_name_ending_in_digits_kept(self):
        """本身以数字结尾的短名（如 "3-2班"）不被误剥。"""
        texts = [ocr_item("3-2班", 120, 90)]
        out = cluster_sessions(texts, list_width=248)
        self.assertEqual(out, [("3-2班", 90)])

    def test_strips_fused_clock_stamp(self):
        """无省略号前缀粘连的 HH:MM（"值守20:57"）与 昨天+时间 → 剥离。"""
        for raw, want in (("黄藏寺项目值班值守20:57", "黄藏寺项目值班值守"),
                          ("新疆兵团设计院昨天14:12", "新疆兵团设计院"),
                          ("七年级14班达善..昨天11:02", "七年级14班达善")):
            out = cluster_sessions([ocr_item(raw, 120, 90)], list_width=248)
            self.assertEqual(out, [(want, 90)], raw)

    def test_strips_fused_year(self):
        """置顶老聊天粘连年份戳（"钧棋13674969...2024/10"）→ 剥离。"""
        for raw, want in (("钧棋13674969...2024/10", "钧棋13674969"),
                          ("隋梓晨爸爸151..2024/", "隋梓晨爸爸151"),
                          ("大有咨询王博 2024/9", "大有咨询王博")):
            out = cluster_sessions([ocr_item(raw, 120, 90)], list_width=248)
            self.assertEqual(out, [(want, 90)], raw)

    def test_pure_numbers_name_kept(self):
        """纯数字昵称（如 QQ 号 "136749694202"）不被年份规则误剥。"""
        out = cluster_sessions([ocr_item("136749694202", 120, 90)],
                               list_width=248)
        # 202 尾剥后仍 ≥2 字 → 应保留原样（不匹配年月日形态不剥）
        self.assertEqual(out, [("136749694202", 90)])


class TestMatchSession(unittest.TestCase):
    def test_exact_and_substring(self):
        found = [("赵嫣嫣AI事务所", 90), ("黄春健", 190)]
        self.assertEqual(match_session("赵嫣嫣AI事务所", found),
                         ("赵嫣嫣AI事务所", 90))
        # 目标名被 OCR 拼上时间戳尾巴 → 子串仍命中
        self.assertEqual(match_session("赵嫣嫣AI事务所",
                                       [("赵嫣嫣AI事务所21:18", 90)]),
                         ("赵嫣嫣AI事务所21:18", 90))

    def test_fuzzy_ocr_variant(self):
        """AI → AⅠ（罗马数字Ⅰ）等 OCR 变体走模糊匹配。"""
        found = [("赵嫣嫣AⅠ事务所", 90), ("河美恬园8号楼业主群", 190)]
        self.assertEqual(match_session("赵嫣嫣AI事务所", found),
                         ("赵嫣嫣AⅠ事务所", 90))

    def test_no_false_positive(self):
        found = [("河美恬园8号楼业主群", 190), ("黄藏寺项目值班值守", 300)]
        self.assertIsNone(match_session("赵嫣嫣AI事务所", found))

    def test_empty(self):
        self.assertIsNone(match_session("任意", []))


class TestFuzzySame(unittest.TestCase):
    def test_ocr_variant_long_name(self):
        """长名称 OCR 单字变体（嫣嫣↔景煨）→ 模糊相同。"""
        self.assertTrue(fuzzy_same("赵嫣嫣AI事务所", "赵景煨AI事务所"))

    def test_distinct_chats_differ(self):
        self.assertFalse(fuzzy_same("赵嫣嫣AI事务所", "河美恬园8号楼业主群"))
        self.assertFalse(fuzzy_same("赵嫣嫣AI事务所", "黄藏寺项目值班值守"))

    def test_short_name_strict(self):
        """3 字名 1 字之差（0.67）判否：短名模糊风险大于收益。"""
        self.assertFalse(fuzzy_same("黄春健", "黄春徤"))
        self.assertTrue(fuzzy_same("黄春健", "黄春健"))   # 完全相同仍为真

    def test_shared_digit_run(self):
        """汉字 OCR 拖低 ratio 时，共享 ≥6 位数字串（手机/QQ号）兜底。"""
        self.assertTrue(fuzzy_same("钧棋13674969", "匀棋13674969937"))
        # 数字串不同 → 不兜底
        self.assertFalse(fuzzy_same("钧棋13674969", "李四13800138000"))


class TestComposerWindowDedup(unittest.TestCase):
    """窗口化去重：重叠屏去重 ✓，历史重复文本不再被折叠 ✓。"""

    def _composer(self):
        from wcr.extractor.composer import MessageComposer
        return MessageComposer(speaker_attribution=False)

    def _texts(self, items):
        import numpy as np
        out = []
        for txt, cy in items:
            box = np.array([[10, cy], [10, cy + 20], [200, cy + 20], [200, cy]],
                           dtype=np.float32)
            out.append({"text": txt, "cx": 100, "cy": cy, "box": box,
                        "score": 0.9})
        return out

    def test_overlap_screens_dedup(self):
        """同一条消息出现在相邻两屏（滚动重叠）→ 只留一条。"""
        import numpy as np
        c = self._composer()
        img = np.zeros((400, 500, 3), dtype=np.uint8)
        c.add_screen(1, self._texts([("早上好", 100)]), [], [], img,
                     __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
        c.add_screen(2, self._texts([("早上好", 120)]), [], [], img,
                     __import__("pathlib").Path(__import__("tempfile").mkdtemp()))
        texts = [m.text for m in c.messages if m.kind == "text"]
        self.assertEqual(texts, ["早上好"])

    def test_repeated_text_far_apart_kept(self):
        """群里多人隔了很多屏再发同文本（如'收到'）→ 各自保留。"""
        import numpy as np
        import tempfile
        from pathlib import Path
        c = self._composer()
        img = np.zeros((400, 500, 3), dtype=np.uint8)
        d = Path(tempfile.mkdtemp())
        c.add_screen(1, self._texts([("收到", 100)]), [], [], img, d)
        c.add_screen(2, self._texts([("别的消息", 110)]), [], [], img, d)
        c.add_screen(9, self._texts([("收到", 100)]), [], [], img, d)
        texts = [m.text for m in c.messages if m.kind == "text"]
        self.assertEqual(texts.count("收到"), 2)

    def test_same_screen_identical_texts_kept(self):
        """同一屏两条相同文本（两人同回'1'）→ 都保留。"""
        import numpy as np
        import tempfile
        from pathlib import Path
        c = self._composer()
        img = np.zeros((400, 500, 3), dtype=np.uint8)
        c.add_screen(1, self._texts([("1", 100), ("1", 300)]), [], [], img,
                     Path(tempfile.mkdtemp()))
        texts = [m.text for m in c.messages if m.kind == "text"]
        self.assertEqual(texts, ["1", "1"])


class TestAtListTop(unittest.TestCase):
    @staticmethod
    def ocr(text, cy, cx=100):
        return {"text": text, "cx": cx, "cy": cy, "score": 0.9}

    def test_true_top(self):
        """真顶部：搜索框 y4 + 首条会话名 y47（间距 43）。"""
        texts = [self.ocr("搜系", 4), self.ocr("赵嫣嫣AI事务所", 47),
                 self.ocr("22:07", 47, cx=220), self.ocr("黄春健", 112)]
        self.assertTrue(at_list_top(texts))

    def test_bottom_with_floating_header(self):
        """列表底部滚动后露出的悬浮搜索头：下方 100px+ 空白 → 不是顶。"""
        texts = [self.ocr("搜索", 5), self.ocr("鱼儿他江志红", 121),
                 self.ocr("07/21", 141, cx=218)]
        self.assertFalse(at_list_top(texts))

    def test_no_anchor(self):
        self.assertFalse(at_list_top([self.ocr("赵嫣嫣AI事务所", 47)]))
        self.assertFalse(at_list_top([]))

    def test_anchor_low_in_view(self):
        """锚点不在顶部条带（cy≥15，如截图中部的'搜索'预览）→ 不是顶。"""
        self.assertFalse(at_list_top([self.ocr("搜索", 200),
                                      self.ocr("某会话", 240)]))

    def test_anchor_with_icon_prefix(self):
        """真顶实测：放大镜图标被混读成前缀字「以搜索」→ 仍应判定为顶。"""
        texts = [self.ocr("以搜索", 5), self.ocr("黄藏寺项目值班值守", 48)]
        self.assertTrue(at_list_top(texts))
        texts2 = [self.ocr("以搜系", 4), self.ocr("某会话", 47)]
        self.assertTrue(at_list_top(texts2))


class TestConfirmListTopOverTop(unittest.TestCase):
    """过顶态自愈：锚点滚出视野（首行顶到区顶）时下退 2 档露出锚点 →
    确认成功；中段滚动下退不会出现锚点 → 确认失败（不引入中段假阳性）。"""

    ANCHOR_TOP = [
        TestAtListTop.ocr("搜系", 4),
        TestAtListTop.ocr("黄藏寺项目值班值守", 47),
        TestAtListTop.ocr("22:07", 47, cx=220),
        TestAtListTop.ocr("赵AI事务所", 112),
        TestAtListTop.ocr("昨天22:28", 112, cx=220),
        TestAtListTop.ocr("总包之声UP主", 177),
        TestAtListTop.ocr("昨天20:56", 177, cx=220),
        TestAtListTop.ocr("河美恬园8号楼业主群", 242),
    ]
    OVER_TOP = [
        TestAtListTop.ocr("2028届八年级1", 6),
        TestAtListTop.ocr("昨天23:00", 6, cx=220),
    ] + ANCHOR_TOP[1:]
    MID_LIST = [
        TestAtListTop.ocr("新疆兵团设计院总", 40),
        TestAtListTop.ocr("星期四", 40, cx=220),
        TestAtListTop.ocr("罗永祥平高电气", 105),
    ]

    def _run(self, script):
        from unittest import mock
        from wcr.extractor.navigator import _confirm_list_top

        class _OCR:
            def __init__(self):
                self._i = 0

            def parse(self, _img):
                i = min(self._i, len(script) - 1)
                self._i += 1
                return script[i]

        class _Cap:
            def grab(self):
                return None

        class _Win:
            rect = (0, 0, 900, 700)

            def session_list_rect(self):
                return (66, 78, 248, 600)

        class _Guard:
            def check_scroll(self, *_a):
                pass

        scrolls: list[tuple] = []
        with mock.patch("wcr.extractor.navigator.scroll_session_list",
                        side_effect=lambda *a, **k: scrolls.append(a)):
            ok = _confirm_list_top(_Win(), _Cap(), _OCR(), _Guard())
        return ok, scrolls

    def test_overtop_heals_and_confirms(self):
        """过顶帧 → 单档下退露出锚点 → 双帧+上推验证全过 → 确认。"""
        script = [self.OVER_TOP] + [self.ANCHOR_TOP] * 5
        ok, scrolls = self._run(script)
        self.assertTrue(ok)
        self.assertEqual(scrolls[0][2], -120)   # 第一个滚动是过顶自愈下退
        self.assertEqual(scrolls[0][3], 1)      # 单档步进（74px 会跨过锚点窗）

    def test_midlist_stays_rejected(self):
        """中段帧：3 档单步下退后仍无锚点 → 确认失败（不引入假阳性）。"""
        ok, scrolls = self._run([self.MID_LIST] * 4)
        self.assertFalse(ok)
        self.assertEqual([s[2] for s in scrolls], [-120, -120, -120])


class TestScaledScanRounds(unittest.TestCase):
    def test_unknown_uses_base(self):
        self.assertEqual(scaled_scan_rounds(0), 60)
        self.assertEqual(scaled_scan_rounds(-5), 60)

    def test_scales_with_list_size(self):
        # 242 项 ≈ 15,700px；悲观 110px/轮 ×1.2 + 8 ≈ 179
        self.assertEqual(scaled_scan_rounds(242), 179)
        # 大列表至少覆盖全列表（悲观位移），且被 300 封顶
        self.assertGreaterEqual(scaled_scan_rounds(500), 500 * 65 // 150)
        self.assertLessEqual(scaled_scan_rounds(10000), 300)
        # 小列表不低于 base
        self.assertEqual(scaled_scan_rounds(10), 60)


class TestWindowContains(unittest.TestCase):
    """截断名包容匹配：列表截断 + 标题 OCR 单字翻转也能验证通过。"""

    def test_truncated_with_ocr_flip(self):
        """实测案例：列表截断「优视光近视」/ 标题「尤视光近视防控护眼灯郑将军」。"""
        self.assertTrue(window_contains("优视光近视", "尤视光近视防控护眼灯郑将军"))

    def test_exact_prefix_contained(self):
        self.assertTrue(window_contains("HZS对下支付管理", "HZS对下支付管理专班"))

    def test_short_target_rejected(self):
        """<4 字目标不做滑窗（防短名在长名里随机撞中）。"""
        self.assertFalse(window_contains("李四", "王李四丰工程部通知群"))
        self.assertFalse(window_contains("黄春健", "黄春健的项目群里还有别人"))

    def test_unrelated_long_name_rejected(self):
        """无关长名里碰巧含 2 个同字也不中（ratio 不达 0.8）。"""
        self.assertFalse(window_contains("黄春健同志", "项目组通知：黄建国 健全制度"))


class TestMergeRoundNames(unittest.TestCase):
    """枚举并入：变体重现不算新增 + 更长读数原位替换（到底检测不被刷屏干扰）。"""

    def _run(self, names, seen, found, prev):
        return merge_round_names(names, set(seen), found, prev)

    def test_variant_reread_not_genuine(self):
        """底部重扫变体（赵A事务所 ↔ 赵嫣嫣AI事务所）→ 不计新增。"""
        names = ["赵嫣嫣AI事务所"]
        seen = set(names)
        # 同一屏重叠（prev_screen）先滤；簇内变体（换了读法）也滤
        g = self._run(names, seen, ["赵A事务所"], prev=["黄春健"])
        self.assertEqual(g, 0)
        self.assertEqual(names, ["赵嫣嫣AI事务所"])

    def test_longer_reading_replaces(self):
        """先见坏读数、后见长读数 → 原位替换为长读数。"""
        names = ["赵A事务所"]
        seen = set(names)
        g = self._run(names, seen, ["赵嫣嫣AI事务所"], prev=["黄春健"])
        self.assertEqual(g, 0)
        self.assertEqual(names, ["赵嫣嫣AI事务所"])

    def test_genuine_new_counted(self):
        names = ["黄春健"]
        seen = set(names)
        g = self._run(names, seen, ["李四丰", "大有咨询王博"], prev=["黄春健"])
        self.assertEqual(g, 2)
        self.assertEqual(names, ["黄春健", "李四丰", "大有咨询王博"])

    def test_overlap_prev_screen_ignored(self):
        """相邻屏滚动重叠（同名单重复出现）→ 不新增。"""
        names = ["黄春健"]
        seen = set(names)
        g = self._run(names, seen, ["黄春健", "李四丰"], prev=["黄春健", "李四丰"])
        self.assertEqual(g, 0)

    def test_old_variant_outside_window_is_new(self):
        """窗口（16 名）之外的相似名 → 保守计为新增（不误合并深处真会话）。"""
        names = ["张三丰"] + [f"占位{i}" for i in range(16)]
        seen = set(names)
        g = self._run(names, seen, ["张三丰同志"], prev=["占位15"])
        self.assertEqual(g, 1)


class TestDedupeNames(unittest.TestCase):
    def test_longest_representative_kept(self):
        """短变体先见、完整名后见 → 保留完整名（顺序无关）。"""
        out = dedupe_names(["赵A事务所", "黄春健", "赵嫣嫣AI事务所"])
        self.assertEqual(out, ["赵嫣嫣AI事务所", "黄春健"])

    def test_no_overmerge_distinct(self):
        out = dedupe_names(["黄春健", "黄春雅", "大有咨询王博"])
        self.assertEqual(out, ["黄春健", "黄春雅", "大有咨询王博"])


class TestNavClickSessionWhitelist(unittest.TestCase):
    """护栏会话列白名单：列表底缘的项（y > 55% 线）可点，禁区仍硬拦。"""

    # 实测布局：窗口 (0,0,940,743)，会话列表 (66,78,248,457) → 底缘 535
    WIN = (0, 0, 940, 743)
    LIST = (66, 78, 248, 457)

    def _guard(self):
        return SafetyGuard(input_zone_ratio=0.80)

    def test_list_bottom_item_allowed(self):
        """列表底缘项（y=522 > nav_bottom=409，仍在列表内）→ 放行。"""
        self._guard().check_nav_click(186, 522, self.WIN, session_rect=self.LIST)

    def test_below_list_rejected(self):
        """列表矩形之下（y=560，仍在输入禁区上）→ 拒。"""
        with self.assertRaises(ReadOnlyViolation):
            self._guard().check_nav_click(186, 560, self.WIN,
                                          session_rect=self.LIST)

    def test_outside_list_x_rejected(self):
        """列表列之外（聊天面板 x=500，y 超 55% 线）→ 拒。"""
        with self.assertRaises(ReadOnlyViolation):
            self._guard().check_nav_click(500, 500, self.WIN,
                                          session_rect=self.LIST)

    def test_window_bounds_still_hard(self):
        """会话列白名单只豁免输入禁区（那是聊天面板的概念），
        微信窗口范围校验仍然硬拦（伪列表矩形超出窗口无效）。"""
        g = self._guard()
        tall_list = (66, 78, 248, 900)   # 高度超出窗口底缘
        with self.assertRaises(ReadOnlyViolation):
            g.check_nav_click(186, 800, self.WIN, session_rect=tall_list)

    def test_no_session_rect_keeps_old_rule(self):
        """不传 session_rect：旧 55% 规则照旧生效。"""
        with self.assertRaises(ReadOnlyViolation):
            self._guard().check_nav_click(186, 522, self.WIN)


class TestFoldedGroupSkipped(unittest.TestCase):
    def test_pseudo_sessions_in_skip_list(self):
        """折叠群聊/订阅号伪会话项在默认跳过表中。"""
        for name in ("群聊", "折叠的群聊", "折叠的订阅号"):
            self.assertIn(name, DEFAULT_SKIP)


class TestStampOlderThan(unittest.TestCase):
    """列表时间戳窗判定：True=明确早于窗起点（可跳），False=明确不早于，
    None=无法判定（保守保留）。today 全部钉死（判定依赖真实日期会变时间炸弹）。"""

    def _cutoff(self):
        from datetime import date
        return date(2025, 9, 4)          # 365d 窗

    def _today(self):
        from datetime import date
        return date(2026, 9, 4)

    def test_year_stamp_variants_old(self):
        from wcr.extractor.session_enum import stamp_older_than
        c, t = self._cutoff(), self._today()
        for st in ("2024/07/23", "2024/10", "2024", "2024/7/5", "2024-06-30"):
            self.assertIs(stamp_older_than(st, c, t), True, st)

    def test_ocr_mangled_stamps(self):
        """实测乱读 '2U24/U9/2U'（=2024/09/20）、'2024/0//U2' → 归一后判旧。"""
        from wcr.extractor.session_enum import stamp_older_than
        c, t = self._cutoff(), self._today()
        self.assertIs(stamp_older_than("2U24/U9/2U", c, t), True)
        self.assertIs(stamp_older_than("2O24/1O/05", c, t), True)

    def test_mangled_stamp_captured(self):
        """右列乱读戳也能被 _row_stamp 捕获（采集侧不丢）。"""
        from wcr.extractor.session_enum import cluster_sessions
        texts = [ocr_item("刘宇峰", 120, 90), ocr_item("2U24/U9/2U", 220, 90)]
        out = cluster_sessions(texts, list_width=248, with_stamp=True)
        self.assertEqual(out, [("刘宇峰", 90, "2U24/U9/2U")])

    def test_dim_stamp_via_lowfloor_blocks(self):
        """淡灰戳（score 0.33，低于名称阈值 0.4）经 stamp_texts 低阈值
        全集捕获——分级阈值一次推理两用。"""
        from wcr.extractor.session_enum import cluster_sessions
        name = {"text": "刘宇峰", "cx": 120, "cy": 90, "score": 0.9}
        dim_stamp = {"text": "2024/06/30", "cx": 220, "cy": 91, "score": 0.33}
        out = cluster_sessions([name], list_width=248, with_stamp=True,
                               stamp_texts=[name, dim_stamp])
        self.assertEqual(out, [("刘宇峰", 90, "2024/06/30")])

    def test_in_window_not_old(self):
        from wcr.extractor.session_enum import stamp_older_than
        c, t = self._cutoff(), self._today()
        for st in ("2026/09/01", "2025/10/03", "2025/09/05"):
            self.assertIs(stamp_older_than(st, c, t), False, st)

    def test_unjudgeable_kept(self):
        """坏读数/空戳/同年仅年份 → None 保守保留。"""
        from wcr.extractor.session_enum import stamp_older_than
        c, t = self._cutoff(), self._today()
        for st in ("", "U4/U/ 1", "2025", "86/45"):
            self.assertIs(stamp_older_than(st, c, t), None, st)

    def test_recent_forms_decidable_in_window(self):
        """微信 UI 语义：一周内形式（昨天/星期/HH:MM）必在最近 7 天 →
        对一切 ≤今天的窗起点都可判"不早于"（用户 2026-09-05 指示的
        活跃年选择正是靠它 + MM/DD 语义判定 595+/612 戳）。"""
        from wcr.extractor.session_enum import stamp_older_than
        t = self._today()
        for st in ("昨天", "星期三", "21:36", "8-29"):
            self.assertIs(stamp_older_than(st, self._cutoff(), t), False, st)
            self.assertIs(stamp_older_than(st, t.replace(month=1, day=1), t),
                          False, st)

    def test_mmdd_future_is_previous_year_fragment(self):
        """未来 MM/DD = 往年残片（实测 名'杜雨北京海淀..2025/'配戳'12/17'）：
        选 2026 活跃年时 12/17 → 2025-12-17 → 判旧剔除。"""
        from wcr.extractor.session_enum import stamp_older_than
        t = self._today()
        y26 = t.replace(month=1, day=1)
        self.assertIs(stamp_older_than("12/17", y26, t), True)
        self.assertIs(stamp_older_than("12/29", y26, t), True)
        self.assertIs(stamp_older_than("04/07", y26, t), False)   # 今年4月
        # 365d 语义下 12/17 解析为 2025-12-17：仍在窗内（不早于 2025-09-04）
        self.assertIs(stamp_older_than("12/17", self._cutoff(), t), False)

    def test_year_only_token(self):
        from wcr.extractor.session_enum import stamp_older_than
        t = self._today()
        y26 = t.replace(month=1, day=1)
        self.assertIs(stamp_older_than("2025", y26, t), True)    # <2026
        self.assertIs(stamp_older_than("2027", y26, t), False)   # >2026
        self.assertIs(stamp_older_than("2026", y26, t), None)    # 同年无月

    def test_bad_month_falls_back(self):
        """坏月份（2024/13）逐级回退到年 → 仍判旧。"""
        from wcr.extractor.session_enum import stamp_older_than
        self.assertIs(stamp_older_than("2024/13/99", self._cutoff(),
                                       self._today()), True)


class TestClusterSessionsStamp(unittest.TestCase):
    def test_right_column_stamp(self):
        """右列年份戳被采集为第三元组；名称保持剥离。"""
        from wcr.extractor.session_enum import cluster_sessions
        texts = [ocr_item("大有咨询王博", 120, 90),
                 ocr_item("2024/9", 220, 90),
                 ocr_item("收到", 110, 112)]
        out = cluster_sessions(texts, list_width=248, with_stamp=True)
        self.assertEqual(out, [("大有咨询王博", 90, "2024/9")])

    def test_fused_year_as_stamp(self):
        """右列戳丢失时用粘连年份兜底。"""
        from wcr.extractor.session_enum import cluster_sessions
        texts = [ocr_item("钧棋13674969...2024/10", 120, 90)]
        out = cluster_sessions(texts, list_width=248, with_stamp=True)
        self.assertEqual(out, [("钧棋13674969", 90, "2024/10")])

    def test_no_stamp_empty(self):
        from wcr.extractor.session_enum import cluster_sessions
        texts = [ocr_item("黄春健", 120, 190), ocr_item("21:36", 222, 190)]
        out = cluster_sessions(texts, list_width=248, with_stamp=True)
        self.assertEqual(out, [("黄春健", 190, "21:36")])

    def test_default_two_tuple_unchanged(self):
        """默认仍返回二元组（既有调用/测试契约不破）。"""
        from wcr.extractor.session_enum import cluster_sessions
        out = cluster_sessions([ocr_item("黄春健", 120, 190)], list_width=248)
        self.assertEqual(out, [("黄春健", 190)])


class TestTranscriptDocx(unittest.TestCase):
    def _chat(self):
        msgs = [
            Message(kind="text", text="早上好，今天到现场吗", side="left",
                    speaker="黄春健", timestamp=datetime(2026, 8, 20, 8, 30)),
            Message(kind="text", text="收到，马上出发", side="right",
                    timestamp=datetime(2026, 8, 20, 8, 31)),
            Message(kind="voice", text="12\"", side="left",
                    timestamp=datetime(2026, 8, 20, 9, 0)),
            Message(kind="text", text="进度照片已发", side="right",
                    speaker="", timestamp=datetime(2026, 8, 21, 14, 5)),
        ]
        return Chat(name="黄春健", messages=msgs,
                    captured_at="2026-08-27 23:00:00", time_window="365d")

    def test_build_docx(self):
        chat = self._chat()
        out = Path(tempfile.mkdtemp()) / "黄春健_聊天记录_测试.docx"
        build_transcript_docx(chat, out, time_window="365d")
        self.assertTrue(out.exists())
        doc = Document(str(out))
        paras = [p.text for p in doc.paragraphs]
        joined = "\n".join(paras)
        # 标题与 meta
        self.assertIn("黄春健 · 聊天记录", joined)
        # 日期分隔
        self.assertIn("■ 2026年08月20日", joined)
        self.assertIn("■ 2026年08月21日", joined)
        # 说话人 + 内容 + 时间
        self.assertIn("【黄春健】 早上好，今天到现场吗　08:30", joined)
        self.assertIn("【我】 收到，马上出发　08:31", joined)
        # 语音标记
        self.assertIn("〔语音 12\"〕", joined)
        # 无名左侧消息不显示说话人前缀
        self.assertIn("进度照片已发　14:05", joined)
        # 只统计：文字 3 / 图片 0 / 语音 1
        self.assertIn("文字 3 条 / 图片 0 张 / 语音 1 条", joined)

    def test_chronological_ordering(self):
        """乱序消息（沉寂段整块错位）→ 渲染后日期分隔单调递增，
        无时间戳消息前向填充贴住前条（不飘到文档头、不另起分隔）。"""
        msgs = [
            Message(kind="text", text="八月消息A", side="left",
                    timestamp=datetime(2026, 8, 3, 9, 0)),
            Message(kind="text", text="四月旧块", side="left",
                    timestamp=datetime(2026, 4, 24, 10, 0)),
            Message(kind="text", text="二月出生", side="left",
                    timestamp=datetime(2026, 2, 11, 8, 0)),
            Message(kind="text", text="八月消息B", side="left",
                    timestamp=datetime(2026, 8, 5, 21, 0)),
            Message(kind="text", text="无时间戳跟随", side="left",
                    timestamp=None),
        ]
        chat = Chat(name="测试群", messages=msgs, captured_at="", time_window="")
        out = Path(tempfile.mkdtemp()) / "t.docx"
        build_transcript_docx(chat, out)
        doc = Document(str(out))
        dates = [p.text for p in doc.paragraphs if p.text.startswith("■")]
        self.assertEqual(dates, ["■ 2026年02月11日", "■ 2026年04月24日",
                                 "■ 2026年08月03日", "■ 2026年08月05日"])
        joined = "\n".join(p.text for p in doc.paragraphs)
        # None 前向填充 → 跟在 08-05 的消息之后（同日段内）
        self.assertGreater(joined.index("无时间戳跟随"),
                           joined.index("八月消息B"))

    def test_image_message_without_file(self):
        """图片消息但文件不存在 → 不嵌入也不崩。"""
        msgs = [Message(kind="image", img_path="Z:/不存在/x.png", side="left",
                        timestamp=datetime(2026, 8, 20, 10, 0))]
        chat = Chat(name="测试群", messages=msgs, captured_at="", time_window="")
        out = Path(tempfile.mkdtemp()) / "t.docx"
        build_transcript_docx(chat, out)
        self.assertTrue(out.exists())


class TestImageOcrNote(unittest.TestCase):
    """HZSMemo 图片OCR附注：图内文字（截图通知/报表）入档可检索。"""

    def test_has_meaningful_rules(self):
        from wcr.report.transcript_docx import _has_meaningful
        self.assertTrue(_has_meaningful("视频接入测试"))
        self.assertTrue(_has_meaningful("2026-09-01"))
        self.assertTrue(_has_meaningful("chat"))      # 字母数字 ≥4 位
        self.assertFalse(_has_meaningful("PDF"))      # 仅 3 位（HZSMemo 判据 ≥4）
        self.assertFalse(_has_meaningful("中"))
        self.assertFalse(_has_meaningful("12"))
        self.assertFalse(_has_meaningful(""))

    def test_note_rendered_below_image(self):
        """_ocr_image_notes 命中时 → docx 出现「图内文字：」附注段。"""
        import wcr.report.transcript_docx as td
        d = Path(tempfile.mkdtemp())
        # 1x1 占位图（真实嵌入无所谓，附注渲染被 monkeypatch 接管）
        import cv2
        import numpy as np
        png = d / "img1.png"
        cv2.imwrite(str(png), np.full((60, 60, 3), 255, dtype=np.uint8))
        orig = td._ocr_image_notes
        td._ocr_image_notes = lambda p, max_lines=10: ["会议时间：2026年9月1日", "地点：黄藏寺"]
        try:
            msgs = [Message(kind="image", img_path=str(png), side="left",
                            timestamp=datetime(2026, 8, 20, 10, 0))]
            chat = Chat(name="测试群", messages=msgs, captured_at="", time_window="")
            out = d / "t.docx"
            build_transcript_docx(chat, out)
            doc = Document(str(out))
            joined = "\n".join(p.text for p in doc.paragraphs)
            self.assertIn("图内文字：", joined)
            self.assertIn("会议时间：2026年9月1日", joined)
        finally:
            td._ocr_image_notes = orig

    def test_real_ocr_on_rendered_text(self):
        """端到端：PIL 渲染中文→PNG→_ocr_image_notes 4x放大识别。"""
        import numpy as np
        from PIL import Image, ImageDraw, ImageFont
        from wcr.report.transcript_docx import _ocr_image_notes
        d = Path(tempfile.mkdtemp())
        png = d / "real.png"
        img = Image.new("RGB", (280, 80), (255, 255, 255))
        draw = ImageDraw.Draw(img)
        font = ImageFont.truetype("C:/Windows/Fonts/msyh.ttc", 28)
        draw.text((10, 20), "视频接入验收通知", font=font, fill=(0, 0, 0))
        img.save(str(png))
        notes = _ocr_image_notes(str(png))
        self.assertTrue(any("验收" in n or "通知" in n or "接入" in n for n in notes),
                        f"OCR 未识别: {notes}")


class TestEnumFreezeRobustBottom(unittest.TestCase):
    """到底判定加固：连续零新增先做"大力滚动验证"（中途冻结会恢复）。

    实测教训（2026-09-05 晨）：负载下列表整段卡住，3 轮零新增即断底，
    629 项的列表只枚举出 74 个——必须冲开冻结复验，3 次均无新增才认底。
    """

    def _screen(self, *names_):
        return [{"text": n, "cx": 120, "cy": 40 + 65 * i, "score": 0.9}
                for i, n in enumerate(names_)]

    def _run(self, screens, max_rounds=24, roll_results=None,
             confirm_results=None):
        import sys
        from unittest import mock
        import wcr.extractor.session_enum as se

        roll_mock = mock.MagicMock()
        if roll_results is not None:
            roll_mock.side_effect = list(roll_results)
        confirm_mock = mock.MagicMock()
        if confirm_results is not None:
            confirm_mock.side_effect = list(confirm_results)
        burst_mock = mock.MagicMock()
        script = list(screens)

        class _FakeOCR:
            def __init__(self, _thr):
                self._i = 0

            def parse_raw(self, _img, floor=0.1):
                i = min(self._i, len(script) - 1)
                self._i += 1
                return script[i]

        class _FakeCap:
            def __init__(self, _region=None):
                pass

            def grab(self):
                import numpy as np
                return np.zeros((10, 10, 3), dtype=np.uint8)

        class _FakeWin:
            rect = (0, 0, 900, 700)

            def activate(self):
                pass

            def session_list_rect(self):
                return (0, 0, 248, 600)

        class _FakeGuard:
            def check_scroll(self, *_a):
                pass

        said: list[str] = []
        en = se.SessionEnumerator(_FakeWin(), _FakeGuard(),
                                  scroll_pause=0, max_rounds=max_rounds,
                                  on_progress=said.append)
        with mock.patch.object(se, "ScreenCapture", _FakeCap), \
                mock.patch.object(se, "OCRParser", _FakeOCR), \
                mock.patch("wcr.extractor.navigator."
                           "scroll_session_list_to_top", roll_mock), \
                mock.patch("wcr.extractor.navigator."
                           "_confirm_list_top", confirm_mock), \
                mock.patch("wcr.extractor.navigator."
                           "scroll_session_list", burst_mock), \
                mock.patch("time.sleep"), \
                mock.patch.dict(sys.modules, {"pyautogui": mock.MagicMock()}):
            names = en.enumerate()
        return names, said

    def test_freeze_recovers_not_bottom(self):
        """冻结 6 轮（两次触发验证）后恢复出新名 → 不断底，继续枚举。"""
        ab = self._screen("会话A", "会话B")
        # 恢复后的名字取互不相似的真实名池（一字之差会被 name_variant
        # 聚成变体、不计新增，测不出"持续枚举"）
        bank = ["大有咨询王博", "晓星所长林超", "黄春健同学", "赵嫣嫣AI事务所",
                "河美恬园8号楼业主群", "2028届八年级14班", "总包之声UP主",
                "度量衡工程咨询", "天使小镇丹丹", "竹言墨雨原创",
                "新疆兵团设计院", "钧棋136749694202", "隋梓晨爸爸151",
                "刘宇峰设计", "陶学长教室", "李四丰台账", "王李四丰工程部",
                "黄建国项目组", "HZS对下支付管理", "黄藏寺现场处置组",
                "鱼儿他江志红", "七年级14班达善", "黄藏寺机电安装标段",
                "河美恬园物业服务中心", "大有咨询李工", "总包学园济南站",
                "工程豹用户交流", "度量衡招标代理", "尤视光近视防控护眼灯",
                "赵氏健康管理"]
        tail = [self._screen(bank[i], bank[i + 1]) for i in range(0, 28, 2)]
        screens = [ab] * 8 + [self._screen("会话C", "会话D")] + tail
        names, said = self._run(screens)
        joined = "\n".join(said)
        self.assertIn("会话C", names)
        # 冻结若被误判为到底会在第 ~13 轮 break，等不到第 20 轮日志
        self.assertIn("（第 20 轮）", joined)      # 恢复后一直枚举到预算用尽
        self.assertNotIn("已到底", joined)        # 冻结未被误判为到底
        self.assertIn("大力滚动验证 1/3", joined)

    def test_roll_fail_burst_rescue(self):
        """回顶失败 → 冲屏破冻后补确认成功：不整轮重滚，扫描正常开始。"""
        ab = self._screen("会话A", "会话B")
        names, said = self._run([ab] * 8, roll_results=[False],
                                confirm_results=[True])
        joined = "\n".join(said)
        self.assertIn("回顶未确认", joined)
        self.assertIn("回顶补确认成功", joined)
        self.assertNotIn("回顶两轮未确认", joined)
        self.assertEqual(names, ["会话A", "会话B"])

    def test_roll_fail_double_resort_loud(self):
        """回顶失败 → 两次冲屏 + 整轮重滚均失败：响亮告警（✗ 行）
        后保守继续扫描——沉默漏采顶部段的事故不再无声。"""
        ab = self._screen("会话A", "会话B")
        names, said = self._run([ab] * 8, roll_results=[False, False],
                                confirm_results=[False, False])
        joined = "\n".join(said)
        self.assertIn("回顶两轮未确认", joined)
        self.assertEqual(names, ["会话A", "会话B"])   # 保守继续不中断

    def test_real_bottom_needs_three_verifies(self):
        """真到底：3 次大力滚动验证均无新增才认底。"""
        ab = self._screen("会话A", "会话B")
        names, said = self._run([ab] * 24)
        joined = "\n".join(said)
        self.assertEqual(names, ["会话A", "会话B"])
        self.assertIn("已到底", joined)
        for i in (1, 2, 3):
            self.assertIn(f"大力滚动验证 {i}/3", joined)


class TestUiNoiseFilter(unittest.TestCase):
    """渲染层 UI 噪声过滤：文件卡片大小/时钟残片/短拉丁碎片不入档。"""

    def test_noise_texts_dropped(self):
        from wcr.report.transcript_docx import _is_ui_noise
        for t in ("86.6K", "3M", "12.5k", ":38", "7:38", "14:05",
                  "and", "Bne", "PDF", "reILILy"[:3]):
            self.assertTrue(_is_ui_noise(t), t)

    def test_real_texts_kept(self):
        from wcr.report.transcript_docx import _is_ui_noise
        for t in ("收到", "12", "ok", "OK", "HZS对下支付管理", "明早7:30出发"):
            self.assertFalse(_is_ui_noise(t), t)

    def test_bad_speakers(self):
        from wcr.report.transcript_docx import _is_bad_speaker
        for s in ("86.6K", "581", "and", ":38"):
            self.assertTrue(_is_bad_speaker(s), s)
        for s in ("", "刘浩总包部安全", "张俊伟岱海"):
            self.assertFalse(_is_bad_speaker(s), s)

    def test_docx_render_skips_noise(self):
        """端到端：噪声文本不渲染、噪声说话人被清洗（消息本体保留）。"""
        msgs = [
            Message(kind="text", text="86.6K", side="left",
                    speaker="86.6K", timestamp=datetime(2026, 8, 20, 10, 0)),
            Message(kind="text", text="值班表已发", side="left",
                    speaker="86.6K", timestamp=datetime(2026, 8, 20, 10, 1)),
            Message(kind="text", text="收到", side="left",
                    speaker="刘浩", timestamp=datetime(2026, 8, 20, 10, 2)),
        ]
        chat = Chat(name="测试群", messages=msgs, captured_at="", time_window="")
        out = Path(tempfile.mkdtemp()) / "t.docx"
        build_transcript_docx(chat, out)
        doc = Document(str(out))
        joined = "\n".join(p.text for p in doc.paragraphs)
        self.assertNotIn("86.6K", joined)          # 噪声消息不渲染
        self.assertIn("值班表已发", joined)          # 噪声说话人的消息保留
        self.assertNotIn("【86.6K】", joined)        # 坏说话人不做前缀
        self.assertIn("【刘浩】 收到", joined)


class TestFailSafeStartGate(unittest.TestCase):
    """启动门：角点静止的鼠标会秒杀第一个 pyautogui 动作（实测 36s 即死）。"""

    def test_corner_predicate(self):
        from wcr.batch import _at_failsafe_corner
        pts = [(0, 0), (0, 1079), (1919, 0), (1919, 1079)]
        for pos in pts:
            self.assertTrue(_at_failsafe_corner(pos, pts), pos)
        for pos in ((1, 0), (0, 1), (960, 540), (1918, 1079), (100, 100)):
            self.assertFalse(_at_failsafe_corner(pos, pts), pos)

    def test_waiter_returns_once_clear(self):
        """鼠标一旦离开角点立即放行（不依赖真实鼠标位置）。"""
        from unittest import mock
        import wcr.batch as batch_mod

        pts = [(0, 0)]
        positions = iter([(0, 0), (0, 0), (500, 300)])
        with mock.patch.object(batch_mod, "time") as fake_time, \
                mock.patch("pyautogui.position",
                           side_effect=lambda: next(positions)):
            fake_time.monotonic.side_effect = iter(range(0, 100, 1))
            fake_time.sleep.return_value = None
            batch_mod._wait_mouse_off_corner(
                lambda m: None, poll_s=0.01, remind_every_s=120.0)
        self.assertEqual(fake_time.sleep.call_count, 2)


class TestCheckpointTimeLabels(unittest.TestCase):
    """断点恢复质量：时间标签必须随检查点持久化（实测 502 条恢复消息
    全被盖上恢复时刻那屏的 09-04 日期）。"""

    def test_roundtrip(self):
        import tempfile
        from pathlib import Path
        from wcr.extractor.composer import MessageComposer

        c1 = MessageComposer(speaker_attribution=False)
        c1.time_labels = {0: [{"text": "昨天 08:30", "cy": 30}],
                          1: [{"text": "星期三", "cy": 28}]}
        c1.screen_count = 2
        c1.messages = [Message(kind="text", text="收到", side="left",
                               timestamp=None)]
        p = Path(tempfile.mkdtemp()) / "ckpt.json"
        c1.save_checkpoint(p)

        c2 = MessageComposer(speaker_attribution=False)
        self.assertTrue(c2.load_checkpoint(p))
        self.assertEqual(c2.time_labels, c1.time_labels)
        self.assertEqual(c2.screen_count, 2)

    def test_realtime_labels_with_ndarray_box_survive(self):
        """真实采集的标签项带 box ndarray → 存档须剥掉（首次存档即崩的实测），
        且 text/cy 往返保真。"""
        import tempfile
        from pathlib import Path
        import numpy as np
        from wcr.extractor.composer import MessageComposer

        c1 = MessageComposer(speaker_attribution=False)
        box = np.array([[10, 20], [10, 40], [200, 40], [200, 20]],
                       dtype=np.float32)
        c1.time_labels = {0: [{"text": "2026年8月5日", "cy": 30, "box": box,
                               "score": 0.9}]}
        c1.screen_count = 1
        c1.messages = [Message(kind="text", text="收到", side="left",
                               timestamp=None)]
        p = Path(tempfile.mkdtemp()) / "ckpt.json"
        c1.save_checkpoint(p)          # 不应抛 JSON 序列化异常

        c2 = MessageComposer(speaker_attribution=False)
        self.assertTrue(c2.load_checkpoint(p))
        self.assertEqual(c2.time_labels[0],
                         [{"text": "2026年8月5日", "cy": 30}])


class TestFramesRelate(unittest.TestCase):
    """断点位置校验：同区域视图高相关、不同区域低相关。"""

    def _img(self, seed):
        import numpy as np
        rng = np.random.default_rng(seed)
        return rng.integers(0, 255, (482, 542, 3), dtype=np.uint8)

    def test_same_scene_high(self):
        from wcr.extractor.visual import VisualExtractor
        import numpy as np
        base = np.tile(np.linspace(0, 255, 482, dtype=np.uint8)[:, None, None],
                       (1, 542, 3))
        noise = self._img(7).astype(np.int16) * 0
        a = np.clip(base.astype(np.int16) + self._img(1).astype(np.int16) // 16,
                    0, 255).astype(np.uint8)
        b = np.clip(base.astype(np.int16) + self._img(2).astype(np.int16) // 16,
                    0, 255).astype(np.uint8)
        self.assertGreaterEqual(VisualExtractor._frames_relate(a, b), 0.6)

    def test_different_scene_low(self):
        from wcr.extractor.visual import VisualExtractor
        self.assertLess(VisualExtractor._frames_relate(self._img(3),
                                                       self._img(4)), 0.6)

    def test_none_or_broken(self):
        from wcr.extractor.visual import VisualExtractor
        self.assertEqual(VisualExtractor._frames_relate(None, self._img(5)), 0.0)


class TestPrefilterTailCut(unittest.TestCase):
    """裁尾规则：列表按日期排序，最后一个"明确窗内"戳之后的项整段跳过。
    today 钉死（MM/DD/昨天的可判性依赖真实日期）。"""

    def _today(self):
        from datetime import date
        return date(2026, 9, 4)

    def test_tail_cut(self):
        from wcr.batch import BatchExporter
        be = BatchExporter(None)
        names = ["A活跃", "B无戳", "C旧置顶", "D去年12月", "E无戳老", "F更老无戳"]
        stamps = {"A活跃": "昨天", "B无戳": "", "C旧置顶": "2024/10",
                  "D去年12月": "2025/12/18", "E无戳老": "", "F更老无戳": ""}
        out = be._prefilter_window(names, stamps, "365d", today=self._today())
        # C 被逐名过滤（2024 置顶老聊天）；D 之后再无窗内戳 → E/F 裁尾
        self.assertEqual(out, ["A活跃", "B无戳", "D去年12月"])

    def test_no_inwindow_stamp_no_cut(self):
        """无任何明确窗内戳 → 不裁尾（保守，全部保留交采集兜底）。"""
        from wcr.batch import BatchExporter
        be = BatchExporter(None)
        out = be._prefilter_window(
            ["A", "B", "C"], {"A": "昨天", "B": "8-29", "C": "2024/10"},
            "365d", today=self._today())
        self.assertEqual(out, ["A", "B"])    # 仅逐名过滤 C，8-29=今年→保留

    def test_year_token_selection(self):
        """年份令牌 "2026"：只留列表戳在 2026 内的会话（用户 2026-09-05
        指示）。明确年份戳与"未来 MM/DD 残片"逐名剔除；无戳名靠裁尾。"""
        from wcr.batch import BatchExporter
        be = BatchExporter(None)
        names = ["今天活跃", "三月聊过", "置顶2024", "无戳X", "去年残片",
                 "无戳尾1", "无戳尾2"]
        stamps = {"今天活跃": "19:30", "三月聊过": "03/15", "置顶2024": "2024/11/06",
                  "无戳X": "", "去年残片": "12/17", "无戳尾1": "", "无戳尾2": ""}
        out = be._prefilter_window(names, stamps, "2026", today=self._today())
        # 置顶2024 明确老、去年残片 12/17=2025-12-17 → 逐名剔除；
        # 三月聊过 03/15=2026-03-15 → 最后 2026 证据；其后（无戳X/尾1/尾2，
        # 按列表排序活跃必 ≤ 三月）整段裁尾——真实列表中 2026 证据延续到
        # ~第 611 项，被裁的只有真正的老聊天尾巴
        self.assertEqual(out, ["今天活跃", "三月聊过"])


class TestProgressKeyVariantMatch(unittest.TestCase):
    """断点跳过的变体容错：重启后枚举名换读法（实测「2028届八年级14班」
    →「2028届八年级1」）不能把已完成聊天整个重爬。"""

    def test_exact(self):
        from wcr.batch import _progress_key
        progress = {"2028届八年级14班": {"messages": 2314}}
        self.assertEqual(_progress_key("2028届八年级14班", progress),
                         "2028届八年级14班")

    def test_truncated_variant_hits(self):
        from wcr.batch import _progress_key
        progress = {"2028届八年级14班": {"messages": 2314}}
        self.assertEqual(_progress_key("2028届八年级1", progress),
                         "2028届八年级14班")

    def test_ocr_variant_hits(self):
        from wcr.batch import _progress_key
        progress = {"赵A事务所": {"messages": 10}}
        self.assertEqual(_progress_key("赵嫣嫣AI事务所", progress), "赵A事务所")

    def test_unrelated_miss(self):
        from wcr.batch import _progress_key
        progress = {"黄藏寺项目值班值守": {"messages": 671}}
        self.assertIsNone(_progress_key("黄藏寺现场处置组", progress))
        self.assertIsNone(_progress_key("河美恬园8号楼业主群", progress))

    def test_empty(self):
        from wcr.batch import _progress_key
        self.assertIsNone(_progress_key("任意", {}))


class TestMatchOnly(unittest.TestCase):
    """定向名单：--only 请求名可与枚举名互为变体/互含（请求常更短）。"""

    def test_short_request_hits_long_list_name(self):
        """请求「黄藏寺项目值班」命中列表「黄藏寺项目值班值守」。"""
        from wcr.batch import _match_only
        names = ["黄藏寺现场处置组", "黄藏寺项目值班值守", "总包之声UP主"]
        self.assertEqual(_match_only(names, ["黄藏寺项目值班"]),
                         ["黄藏寺项目值班值守"])

    def test_exact_and_variant(self):
        from wcr.batch import _match_only
        names = ["赵嫣嫣AI事务所", "黄春健"]
        self.assertEqual(_match_only(names, ["赵嫣嫣AI事务所"]),   # 精确
                         ["赵嫣嫣AI事务所"])
        self.assertEqual(_match_only(names, ["赵A事务所"]),         # OCR 变体
                         ["赵嫣嫣AI事务所"])

    def test_no_match_empty(self):
        from wcr.batch import _match_only
        self.assertEqual(_match_only(["黄春健", "刘宇峰"], ["不存在的人"]), [])

    def test_multiple_requests(self):
        from wcr.batch import _match_only
        names = ["处置组", "值班组", "无关群"]
        self.assertEqual(_match_only(names, ["处置", "值班"]),
                         ["处置组", "值班组"])


class TestScrollUpDynamicTop(unittest.TestCase):
    """上滚到顶甄别：文本稳定 3 次可能只是动态加载间隙（用户实测指正），
    必须大冲复验——验证发现新内容则继续上滚，两轮无变化才认顶。"""

    def _run(self, script, start_dt):
        """script: 每次 ocr.parse 返回的文本列表序列。返回 (says, wheel_log)。"""
        from datetime import datetime
        from unittest import mock
        from wcr.extractor.scroller import ChatScroller
        from wcr.extractor.visual import VisualExtractor

        wheel_log: list[int] = []

        class _Scroller:
            scroll_pause = 0
            scroll_step = 15
            earliest_time_in_texts = staticmethod(
                ChatScroller.earliest_time_in_texts)

            def _wheel(self, n):
                wheel_log.append(n)

        class _OCR:
            def __init__(self):
                self._i = 0

            def parse(self, _img):
                i = min(self._i, len(script) - 1)
                self._i += 1
                return script[i]

        class _Cap:
            def grab(self):
                return None

        says: list[str] = []
        ext = VisualExtractor(stable_frames=3, scrollup_time_budget=10 ** 9)
        with mock.patch("time.sleep"), \
                mock.patch("time.monotonic",
                           side_effect=range(0, 10 ** 6, 1)):
            ext._scroll_up_with_window(_Scroller(), _Cap(), _OCR(),
                                       start_dt, says.append)
        return says, wheel_log

    @staticmethod
    def _screen(texts):
        return [{"text": t, "cx": 100, "cy": 40 + 30 * i, "score": 0.9}
                for i, t in enumerate(texts)]

    def test_fetch_pause_not_top(self):
        """稳定 3 次后验证发现新内容（动态加载恢复）→ 继续上滚到窗起点。"""
        from datetime import datetime
        a = self._screen(["张三：收到", "李四：好的"])
        # 4 次相同 → 稳定 3 次触发验证；验证第 1 轮 OCR 到更早的日期标签
        old = self._screen(["2025年8月1日 09:00", "王五：开工令已发"])
        says, wheel = self._run(
            [a, a, a, a, old, old], datetime(2025, 9, 6))
        joined = "\n".join(says)
        self.assertIn("动态加载恢复", joined)      # 验证识破加载间隙
        self.assertIn("到达时间窗起点", joined)    # 继续滚到 2025-08-01 停
        self.assertNotIn("已滚动到聊天顶部", joined)   # 没有误判顶
        self.assertIn(15 * 24, wheel)              # 大冲 24 档确实发生

    def test_true_top_accepted(self):
        """真顶：稳定 3 次 + 两轮大冲验证均无变化 → 认顶。"""
        from datetime import datetime
        a = self._screen(["张三：收到"])
        script = [a] * 12
        says, wheel = self._run(script, datetime(2025, 9, 6))
        joined = "\n".join(says)
        self.assertIn("动态加载两轮验证均无变化", joined)
        self.assertEqual(wheel.count(15 * 24), 2)   # 两轮验证大冲

    def test_full_depth_no_window_to_top(self):
        """全量深度（年份令牌选择，start_dt=None）：无窗起点可停 → 探针
        一路滚到动态加载两轮验证的真顶（用户 2026-09-05 指示）。"""
        a = self._screen(["张三：收到", "李四：好的"])
        says, wheel = self._run([a] * 12, None)
        joined = "\n".join(says)
        self.assertIn("聊天顶部（全量深度）", joined)
        self.assertIn("动态加载两轮验证均无变化", joined)
        self.assertNotIn("到达时间窗起点", joined)
        self.assertEqual(wheel.count(15 * 24), 2)

    def test_verify_empty_ocr_not_top(self):
        """验证时 OCR 为空 → 不认顶，继续上滚（此处随后滚到窗起点收尾）。"""
        from datetime import datetime
        a = self._screen(["张三：收到"])
        empty: list[dict] = []
        old = self._screen(["2025年8月1日 09:00", "王五：开工令已发"])
        says, _ = self._run([a, a, a, a, empty, old, old],
                            datetime(2025, 9, 6))
        joined = "\n".join(says)
        self.assertIn("OCR 为空", joined)           # 空验证没有误认顶
        self.assertIn("到达时间窗起点", joined)       # 继续上滚至窗起点
        self.assertNotIn("动态加载两轮验证均无变化", joined)


class TestWindowFindStrict(unittest.TestCase):
    """窗口定位宁缺毋滥：微信托盘最小化时不得回退抓 VS Code
    （标题含 WeChat-Report，2026-09-05 实测抓错窗口后险些在 VS Code 里滚轮）。"""

    class _W:
        def __init__(self, title, l=0, t=0, w=900, h=700):
            self.title, self.left, self.top = title, l, t
            self.width, self.height = w, h

    def _find(self, windows):
        import sys
        from unittest import mock
        from wcr.extractor.window import WeChatWindow

        fake_gw = mock.MagicMock()

        def by_title(hint):
            return [w for w in windows if hint in w.title]

        fake_gw.getWindowsWithTitle.side_effect = by_title
        with mock.patch.dict(sys.modules, {"pygetwindow": fake_gw}):
            return WeChatWindow().find()

    def test_prefers_exact_wechat(self):
        vs = self._W("微信聊天记录报告工具 - WeChat-Report - Visual Studio Code")
        wx = self._W("微信", 2, 22, 896, 704)
        win = self._find([vs, wx])
        self.assertEqual(win.rect, (2, 22, 896, 704))

    def test_only_vscode_raises(self):
        """微信不可见时：只剩被排除窗口 → 报错，绝不抓错。"""
        vs = self._W("微信聊天记录报告工具 - WeChat-Report - Visual Studio Code")
        with self.assertRaises(RuntimeError):
            self._find([vs])

    def test_excluded_subtitle_still_raises(self):
        """无精确匹配且候选全被排除 → 报错（旧行为会回退抓错）。"""
        with self.assertRaises(RuntimeError):
            self._find([self._W("微信聊天记录.txt - 记事本")])


if __name__ == "__main__":
    unittest.main()
