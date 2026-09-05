# -*- coding: utf-8 -*-
"""会话列表枚举：滚动左侧列表，OCR 提取全部聊天名（只读）。

微信 4.x 会话列表项结构（实测 100% 缩放，~64px/项）：
    头像 | 名称（上，深色）              时间（右上，如 21:36）
         | 最近消息预览（下，灰色小字）  [N条] 徽标（右下）
枚举策略：OCR 文本按 y 聚簇成"行"，行间距 ≥ SESSION_GAP 分割成"项"，
每项取**最上一行最左侧**的文本作为聊天名候选（名称行在预览行上方，
时间戳/徽标在右侧被 x 过滤）。
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

from .capture import ScreenCapture
from .ocr import OCRParser
from .safety import SafetyGuard

log = logging.getLogger("wcr.session_enum")

# 默认跳过的系统/工具会话（不是私聊/群聊）
DEFAULT_SKIP = ("微信团队", "订阅号消息", "文件传输助手", "腾讯文档",
                "微信支付", "微信豆包",
                # 折叠群聊/订阅号的伪会话项（点击展开文件夹而非聊天）
                "群聊", "折叠的群聊", "折叠的订阅号")

_TIME_OR_BADGE = re.compile(r"^(\d{1,2}:\d{2}|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天]|\[\d+条\]|\d+条新消息?)$")
_NOISE = re.compile(r"^[\s·.。,，、()（）\[\]【】]{0,3}$")
# 列表顶部的搜索框文字（OCR 常读成"搜茶/搜系"等；真顶还会混入图标前缀
# 「以搜索」「以搜系」——与 navigator._SEARCH_ANCHOR 同步容错，否则锚点
# 文本会被当成聊天名（试点 9 目标 [1/5]「以搜索」实证）
_SEARCH_BOX = re.compile(r"^[以QO]?搜[索茶系文]?$")
# 日期式时间戳（右侧列，如 8-27 / 08-27）
_DATE_STAMP = re.compile(r"^\d{1,2}-\d{1,2}$")

# 聚簇参数（微信 4.1 实测 100% 缩放：项距 65px，名称→预览行距 20px，
# 名称与时间戳同行。行按 ±10px 边距计空白：项内名称→预览空白 0px，
# 项间（预览→下一名称）空白 25px，无预览时 45px → 阈值取 12px）
SAME_LINE_GAP = 10     # cy 差小于此 → 同一行（同行文本 cy 差 ≤2px）
SESSION_GAP = 12       # 行间空白大于此 → 不同会话项


def merge_round_names(names: list[str], seen: set[str],
                      found_names: list[str], prev_screen: list[str],
                      cluster_window: int = 16) -> int:
    """把一屏 OCR 到的名称并入累计列表（模糊聚类，保留更长读数）。

    返回"真实新增"数：
      - 与上一屏同名/模糊相同 → 滚动重叠的同一项，忽略；
      - 与近期（最近 cluster_window 个）名称模糊相同 → 列表到底后
        反复重扫出的 OCR 变体重现：**不算新增**（否则"连续无新增=到底"
        永远不成立，实测 160 轮刷出 391 个名字）；变体比已存读数更长时
        原位替换（更长读数更接近真实名，标题头验证更可靠——
        「赵A事务所」应收敛为「赵嫣嫣AI事务所」）；
      - 其余 → 真实新增。
    """
    from .navigator import name_variant

    genuine = 0
    for key in found_names:
        if not key or key in seen:
            continue
        if any(key == p or name_variant(key, p) for p in prev_screen):
            continue
        hit = next((i for i in range(max(0, len(names) - cluster_window),
                                     len(names))
                    if name_variant(key, names[i])), None)
        if hit is not None:
            if len(key) > len(names[hit]):
                names[hit] = key
                seen.add(key)
            continue
        seen.add(key)
        names.append(key)
        genuine += 1
    return genuine


def cluster_sessions(ocr_texts: list[dict], list_width: int = 0,
                     with_stamp: bool = False,
                     stamp_texts: Optional[list[dict]] = None) -> list[tuple]:
    """OCR 文本 → 按会话项聚簇，返回 [(聊天名, 项顶部y), ...]（自上而下）。

    ocr_texts: [{"text", "cx", "cy"}, ...]（相对会话列表截图坐标）
    list_width: 截图宽度，用于过滤右侧时间戳列（cx > 72% 宽度）。
    with_stamp: 返回 (聊天名, y, 时间戳) 三元组——右列戳（如 2024/07/23、
    昨天、21:36），无右列戳时用粘连进名称的年份（如 钧棋...2024/10）。
    stamp_texts: 采戳用的低阈值文本全集（一次推理分级阈值：老聊天右列
    2024/* 戳是更淡的小灰字，常落在 0.28~0.4，会被名称阈值 0.4 滤掉）；
    缺省 = ocr_texts 本身。
    """
    right_lim = int((list_width or 10 ** 6) * 0.72)
    # 采戳全集：噪声滤/阈值滤之前的低分块（时间戳是淡灰小字）。
    # 注意与名称管道相互独立：名称仍用调用方传入（已按 0.4 阈值滤）的
    # ocr_texts，只加 y 下限；不要从 raw_texts 派生名称（会把 0.28 低分
    # 噪声灌进名称管道）。
    raw_texts = [t for t in (stamp_texts if stamp_texts is not None else ocr_texts)
                 if (t.get("text") or "").strip()]
    # y 下限：搜索框/悬浮头锚点区（cy < 22）的任何文本都不是会话行——
    # 锚点 OCR 变体无穷（以搜索/reILILy rU: s≤TSH/X…），逐个正则堵不完，
    # 结构上按位置丢弃更可靠（首行会话名 cy ≥ 25，实测真顶 30~47）
    ocr_texts = [t for t in ocr_texts if t.get("cy", 0) >= 22]
    items = []
    for t in ocr_texts:
        txt = (t["text"] or "").strip()
        if not txt or _NOISE.match(txt) or _TIME_OR_BADGE.match(txt):
            continue
        if _SEARCH_BOX.match(txt) or _DATE_STAMP.match(txt):
            continue
        # 右侧列（时间/徽标拼进了名称行等）兜底：位置在右侧且像时间/计数
        if list_width and t["cx"] > right_lim and (
                _TIME_OR_BADGE.match(txt) or _DATE_STAMP.match(txt)
                or re.match(r"^\d+$", txt)):
            continue
        items.append(t)
    if not items:
        return []

    items.sort(key=lambda t: t["cy"])
    # 1) 分行：cy 差 < SAME_LINE_GAP 归同一行
    lines: list[list[dict]] = [[]]
    for t in items:
        if lines[-1] and t["cy"] - lines[-1][-1]["cy"] > SAME_LINE_GAP:
            lines.append([])
        lines[-1].append(t)
    # 2) 分项：相邻行中心距 > SESSION_GAP + 行高 → 新会话项
    #    （行内文本高度 ~20px；项内"名称行→预览行"间距小，项间空白大）
    sessions: list[list[list[dict]]] = [[]]
    prev_bottom = None
    for ln in lines:
        top = min(t["cy"] for t in ln) - 10
        if prev_bottom is not None and top - prev_bottom > SESSION_GAP:
            sessions.append([])
        sessions[-1].append(ln)
        prev_bottom = max(t["cy"] for t in ln) + 10
    # 3) 每项：最上一行、最左侧文本 = 名称（并剥离 OCR 拼进来的时间戳尾巴）
    out = []
    for sess in sessions:
        first = min(sess, key=lambda ln: min(t["cy"] for t in ln))
        name_t = min(first, key=lambda t: t["cx"])
        name = name_t["text"].strip()
        # 粘连的时间尾巴：'值守20:57'（无省略号前缀）/'新疆兵团设计院昨天14:12'
        # （昨天/星期X 也可能被拼进名称）；名称本身以数字+冒号结尾极罕见，安全。
        name = re.sub(
            r"[.。·…~～]{0,4}\s*(?:昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天])?"
            r"\s*[QO\d]{0,2}\d{1,2}[:：]\d{2}\s*$", "", name)
        # 粘连的日期戳（如 "HZS对下支付管理专...08/29"、"钧棋13674969...2024/10"）；
        # 仅在剩余 ≥2 字时剥。剥下来的戳留作 stamp（批量导出按窗预过滤用）
        fused = ""
        for pat in (r"[.。…]*\s*((?:19|20)\d{2}/?\d{0,2})$",   # 年份（2024 / 2024/10）
                    r"[.。…]*\s*(\d{1,2}/\d{1,2})$"):        # 月日（08/29）
            m = re.search(pat, name)
            if m:
                name2 = name[:m.start(1)]
                if len(name2.strip()) >= 2:
                    fused = m.group(1)
                    name = name2.strip()
        name = name.rstrip(".。·…~～ ").strip()
        if name and not _TIME_OR_BADGE.match(name) and len(name) >= 2:
            if with_stamp:
                row_y = int(min(t["cy"] for t in first))
                out.append((name, row_y,
                            _row_stamp(raw_texts, row_y, fused, right_lim)))
            else:
                out.append((name, int(min(t["cy"] for t in first))))
    return out


# 右列时间戳形态：21:36 / 昨天 / 星期三 / [56条] / 2024/07/23 / 8-29 / 2024/10
_STAMP_TXT = re.compile(
    r"^(?:\d{1,2}:\d{2}|昨天|前天|星期[一二三四五六日天]|周[一二三四五六日天]"
    r"|(?:19|20)\d{2}[/.]\d{1,2}(?:[/.]\d{1,2})?|\d{1,2}[/-]\d{1,2}|\[\d+条\]|\d+条新消息?)$")


def _norm_stamp_digits(s: str) -> str:
    """时间戳 OCR 数字翻转归一：U/O→0、I/l/|→1、去空格（实测 '2U24/U9/2U'、
    '2024/0//U2' 型乱读高频出现；仅用于戳解析，不改原读数）。"""
    t = re.sub(r"\s+", "", s or "")
    if re.search(r"[UOIl|]", t):
        t = re.sub(r"[UO]", "0", t)
        t = re.sub(r"[Il|]", "1", t)
    return t


def _stamp_like(s: str) -> bool:
    """原样或数字归一后形如列表时间戳。"""
    if _STAMP_TXT.match(s):
        return True
    t = _norm_stamp_digits(s)
    return bool(re.match(
        r"^(?:\d{1,2}:\d{2}|(?:19|20)\d{2}[/.]\d{1,2}(?:[/.]\d{1,2})?"
        r"|\d{1,2}[/.-]\d{1,2})$", t))


def _row_stamp(raw_texts: list[dict], row_y: int, fused: str,
               right_lim: int) -> str:
    """会话项的列表时间戳：**原始** OCR 文本（未过噪声滤）中与名称行
    同 y（±10px）且落在右列（>72% 宽）的戳文本；无则用粘连进名称的年份。

    HH:MM/昨天 这类戳在 cluster_sessions 入口被当噪声滤除，须回到原始
    文本上按行对齐取回（它们是"窗内活跃"的信号，不能丢）。
    """
    for t in raw_texts:
        txt = (t.get("text") or "").strip()
        if (right_lim and t.get("cx", 0) > right_lim
                and abs(t.get("cy", -999) - row_y) <= SAME_LINE_GAP
                and _stamp_like(txt)):
            return txt
    return fused


def stamp_older_than(stamp: str, cutoff, today=None) -> "bool | None":
    """列表时间戳是否**明确**早于窗起点 cutoff(datetime.date)。

    返回三值：True=明确早于（可安全跳过）；False=明确不早于；None=无法判定
    （OCR 坏读数 → 保守保留，交采集阶段处理）。

    按微信 4.x 列表戳显示语义解析（2026-09-05 用户指示 + 实测确认）：
      一周内 → HH:MM/昨天/星期X；**今年内 → MM/DD 不带年**；往年 → YYYY/MM。
    由此无年份形式也可判定：MM/DD 必为今年——除非晚于今天，晚于今天的
    "12/17" 只能是往年戳的残片（实测：名"杜雨北京海淀..2025/"配戳
    "12/17"，真值 2025/12/17）。
    """
    from datetime import date as _date, timedelta as _td
    s = _norm_stamp_digits(stamp)
    today = today or _date.today()
    m = re.match(r"^((?:19|20)\d{2})[/.-](\d{1,2})(?:[/.-](\d{1,2}))?", s)
    if m:
        y, mo, d = int(m.group(1)), int(m.group(2)), int(m.group(3) or 1)
        for cand in ((y, mo, d), (y, mo, 1), (y, 1, 1)):
            try:
                return _date(*cand) < cutoff
            except ValueError:
                continue
        return None
    if re.fullmatch(r"(?:19|20)\d{2}", s):        # 仅年份（粘连剥出）
        y = int(s)
        if y < cutoff.year:
            return True
        if y > cutoff.year:
            return False
        return None                               # 同年无法判月 → 保守保留
    # 一周内形式：真实日期必落在 [today-6, today]
    if re.fullmatch(r"\d{1,2}[:：]\d{2}|昨天|前天|星期[一二三四五六日天]"
                    r"|周[一二三四五六日天]", s):
        return (today - _td(days=6)) < cutoff
    # MM/DD（不带年）：今年显示语义；晚于今天 → 只能是往年残片
    m = re.fullmatch(r"(\d{1,2})[/.-](\d{1,2})", s)
    if m:
        mo, d = int(m.group(1)), int(m.group(2))
        if not (1 <= mo <= 12 and 1 <= d <= 31):
            return None                           # 86/45 之类坏读数
        try:
            cand = _date(today.year, mo, d)
            if cand > today:
                cand = _date(today.year - 1, mo, d)
        except ValueError:
            return None
        return cand < cutoff
    return None


class SessionEnumerator:
    """滚动会话列表，枚举一年内活跃过的全部聊天名。"""

    def __init__(self, win, guard: SafetyGuard,
                 settle_wait: float = 1.0,
                 scroll_pause: float = 0.3,
                 max_rounds: int = 160,       # 滚动轮数上限（200+ 项长列表需 ~60 轮）
                 stable_rounds: int = 3,      # 连续 N 轮无新名称 → 到底
                 on_progress: Optional[Callable[[str], None]] = None):
        self.win = win
        self.guard = guard
        self.settle_wait = settle_wait
        self.scroll_pause = scroll_pause
        self.max_rounds = max_rounds
        self.stable_rounds = stable_rounds
        self.say = on_progress or (lambda m: log.info(m))
        self.stamps: dict[str, str] = {}   # 聊天名 → 最近一次读到的列表时间戳

    # ------------------------------------------------------------ API
    def enumerate(self, skip: tuple[str, ...] = DEFAULT_SKIP) -> list[str]:
        """返回去重后的聊天名列表（保持列表出现顺序）。"""
        self.win.activate()
        region = self.win.session_list_rect()
        cap = ScreenCapture(region)
        ocr = OCRParser(0.4)
        names: list[str] = []
        seen: set[str] = set()
        prev_screen: list[str] = []   # 上一屏名称（OCR 变体判断用）
        stable = 0
        bottom_verify = 0   # 底验证次数：连续零新增 ≠ 到底（中途冻结甄别）
        self.say("🗂 枚举会话列表（滚动 + OCR）…")

        # 先回到列表顶部（距离制 + 搜索框锚点确认）。返回值必须接住：
        # 回顶失败直接开扫会漏掉整个顶部段——2026-09-06 实测重启后从中段
        # 起扫（首名=星期三/四戳的中段会话，当天 02:48 活跃的会话反在其
        # 后才发现，日期排序下自相矛盾），当晚活跃的顶部 6+ 会话全漏且
        # 无任何告警。处置：冲屏破冻复验 → 整轮重滚一次 → 仍失败则响亮
        # 告警后保守继续（人工介入信号）
        from .navigator import (_confirm_list_top, fuzzy_same,
                                scroll_session_list,
                                scroll_session_list_to_top)
        if not scroll_session_list_to_top(
                self.win, self.guard,
                expected_items=self.max_rounds * 148 // 65):
            self.say("   ⚠ 回顶未确认，冲屏破冻后复验 …")
            confirmed = False
            for attempt in range(2):
                time.sleep(3.0)
                scroll_session_list(self.win, self.guard, +120, 24, pause=0.1)
                time.sleep(1.0)
                if _confirm_list_top(self.win, cap, ocr, self.guard):
                    self.say(f"   ✔ 回顶补确认成功（第 {attempt + 1} 次冲屏）")
                    confirmed = True
                    break
            if not confirmed and not scroll_session_list_to_top(
                    self.win, self.guard,
                    expected_items=self.max_rounds * 148 // 65):
                self.say("   ✗ 回顶两轮未确认：顶部段有漏采风险，"
                         "如结果缺当晚活跃会话需重跑")

        for rnd in range(self.max_rounds):
            # 一次推理分级阈值：名称用 0.4（干净），采戳用 0.28（老聊天
            # 右列 2024/* 戳是更淡的小灰字，实测常落在 0.28~0.4）
            blocks = ocr.parse_raw(cap.grab(), floor=0.28)
            texts = [t for t in blocks if t["score"] >= 0.4]
            found = cluster_sessions(texts, list_width=region[2],
                                     with_stamp=True, stamp_texts=blocks)
            for name, _y, st in found:
                if st:
                    self.stamps[name] = st
            found_names = [name for name, _y, _st in found]
            genuine = merge_round_names(names, seen, found_names, prev_screen)
            prev_screen = found_names
            if genuine == 0:
                stable += 1
                if stable >= self.stable_rounds:
                    if bottom_verify >= 3:
                        self.say(f"   会话列表已到底（共 {len(names)} 个会话，"
                                 "3 次底验证均无新增）")
                        break
                    bottom_verify += 1
                    stable = 0
                    # 中途冻结甄别：负载下列表会整段卡住（实测 64 档零位移
                    # 后瞬间恢复），"连续零新增"≠到底——大力滚一屏冲开冻结
                    # 再看一轮；真到底时多滚无害（列表不动，多花几秒）。
                    # 冻结容限 = 3×(3轮×4档 + 24档) = 108 档 > 实测 64 档。
                    self.say(f"   疑似到底（{len(names)} 个），"
                             f"大力滚动验证 {bottom_verify}/3 …")
                    self._scroll_list(cap, -120, 24)
                    time.sleep(2.0)
            else:
                stable = 0
                bottom_verify = 0
            self._scroll_list(cap, -120, 4)   # 往下滚 4 档 ≈ 一屏会话
            time.sleep(self.scroll_pause)
            if rnd % 5 == 4:
                self.say(f"   已枚举 {len(names)} 个会话（第 {rnd + 1} 轮）")

        out = [n for n in names if n not in skip]
        skipped = [n for n in names if n in skip]
        if skipped:
            self.say(f"   跳过系统会话：{'、'.join(skipped)}")
        self.say(f"📋 枚举完成：{len(out)} 个目标聊天")
        return out

    # ------------------------------------------------------------ internals
    def _scroll_list(self, cap: ScreenCapture, delta: int, n: int, tag: str = "") -> None:
        """滚轮滚动会话列表（列表中央，安全区内）。"""
        import pyautogui

        region = self.win.session_list_rect()
        cx = region[0] + region[2] // 2
        cy = region[1] + min(region[3] // 2, 300)
        self.guard.check_scroll(cx, cy, self.win.rect)
        pyautogui.moveTo(cx, cy, duration=0.1)
        for _ in range(n):
            pyautogui.scroll(delta)
            time.sleep(self.scroll_pause * 0.6)
