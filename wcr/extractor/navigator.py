# -*- coding: utf-8 -*-
"""微信聊天导航：受控切换到指定群聊/好友（零键盘、零危险区）。

两种模式：
  manual  — 倒计时等待用户人工点开目标聊天（最稳）
  session — OCR 读左侧会话列表，点击标题匹配项（近期活跃聊天可直达）

安全设计：
  × 只点击会话列表区域（几何白名单 + SafetyGuard.check_nav_click 双重校验）
  × 全程不使用键盘
  × 点击后验证聊天确实已打开（OCR 标题头），失败自动重试一次
"""
from __future__ import annotations

import logging
import re
import time
from typing import Callable, Optional

from .capture import ScreenCapture
from .ocr import OCRParser
from .safety import SafetyGuard

log = logging.getLogger("wcr.navigator")


def scroll_session_list(win, guard: SafetyGuard, delta: int, n: int = 4,
                        pause: float = 0.2) -> None:
    """滚轮滚动左侧会话列表（只读；列表中央，远离底部输入禁区）。"""
    import pyautogui

    region = win.session_list_rect()
    cx = region[0] + region[2] // 2
    cy = region[1] + min(region[3] // 2, 300)
    guard.check_scroll(cx, cy, win.rect)
    pyautogui.moveTo(cx, cy, duration=0.1)
    for _ in range(n):
        pyautogui.scroll(delta)
        time.sleep(pause)


_NOISE_CHARS = re.compile(r"[\s·.。:：()（）\[\]【】\-—_]+")

# 列表顶部的搜索框锚点（OCR 常读成 搜茶/搜系/搜文 等；位于截图顶部 ~30px 内。
# 真顶实测还会把放大镜图标混读成前缀字：「以搜索」「以搜系」）
_SEARCH_ANCHOR = re.compile(r"^[以QO]?搜[索茶系文]?$")


def at_list_top(ocr_texts: list[dict]) -> bool:
    """判定列表已滚到顶：搜索框锚点(cy<15) + 锚点下方 20~70px 内有正文。

    仅看锚点不够——微信 4.1 滚动时会把搜索框悬浮头"露出"在列表顶端
    （实测列表底部滚动后 搜索@y5 持续可见），需靠下方间距区分：
    真顶部：锚点 y≈4，首条会话名 y≈47（间距 ~43px）；
    底部露头：锚点 y≈5，最近正文 y≥121（间距 100px+，下方是空白）。
    """
    anchor_y = None
    for t in ocr_texts:
        if _SEARCH_ANCHOR.match((t["text"] or "").strip()) and t["cy"] < 15:
            anchor_y = t["cy"]
            break
    if anchor_y is None:
        return False
    return any(20 <= t["cy"] - anchor_y <= 70 and len((t["text"] or "").strip()) >= 2
               for t in ocr_texts)


def _confirm_list_top(win, cap, ocr, guard: Optional[SafetyGuard] = None) -> bool:
    """双帧确认 + **上推验证**（假顶防线）。

    锚点+间距判据存在假阳性：悬浮搜索头下半遮的行（实测 2024/* 戳的
    "假顶"，间距 25px 落在 20~70px 判据带内），双帧稳定同时成立——
    枚举起点因此错过整个列表上部（试点 8 实证）。
    到顶铁证：再向上滚**行名集合纹丝不动**（真顶无处可滚）；
    连续 2 次上推集合不变才确认（快速连滚吞档风险下取双确认）。
    过顶态（2026-09-06 实测）：列表可越过锚点位继续上滑把搜索头整个
    折叠出视野——首行顶到区顶、锚点消失，确认永不成立、回顶空磨到
    预算耗尽。处置：锚点不可见时小步**下退** 2 档再验——过顶态一退
    即露出锚点（顺带自愈回锚点位），中段滚动退多少都不会出现锚点，
    以此区分，不引入中段假阳性。
    """
    for _ in range(2):
        if not at_list_top(ocr.parse(cap.grab())):
            if guard is None:
                return False   # 无护栏（单测桩）无法滚动验证
            scroll_session_list(win, guard, -120, 2, pause=0.15)
            time.sleep(0.4)
            if not at_list_top(ocr.parse(cap.grab())):
                return False
        time.sleep(0.35)
    if guard is None:
        return True   # 无护栏（单测桩）无法滚动验证，退化为双帧判据

    def _names_sig():
        from .session_enum import cluster_sessions   # 惰性导入防环
        region = win.session_list_rect()
        found = cluster_sessions(ocr.parse(cap.grab()),
                                 list_width=region[2])
        # 剔除最上 3 行再取签名：活跃时段新消息每分钟插入顶部，
        # 顶部行必然变（真顶也变）；中段滚动则全屏行都会变。
        # 比较下部行集合 = 滚动 vs 重排 的区分判据。
        return tuple(n for n, _y in found[3:])

    base = _names_sig()
    for _ in range(2):
        scroll_session_list(win, guard, +120, 8, pause=0.1)
        time.sleep(0.5)
        if _names_sig() != base:
            return False
    return True


def _norm_name(s: str) -> str:
    """名称归一化（去空白/标点），供模糊匹配。"""
    return _NOISE_CHARS.sub("", s)


_DIGIT_RUN = re.compile(r"\d{6,}")


def _shared_digit_run(a: str, b: str) -> bool:
    """两侧都含 ≥6 位长数字串（手机号/QQ号/工号），且有一段数字互为前缀。

    长数字串区分度极高，同名变体（钧棋↔匀棋）里数字几乎不变 →
    汉字 OCR 拖低整体 ratio 时用数字串兜底判定。
    """
    da = _DIGIT_RUN.findall(a)
    db = _DIGIT_RUN.findall(b)
    if not da or not db:
        return False
    return any(x in y or y in x for x in da for y in db)


def fuzzy_same(a: str, b: str, threshold: float = 0.70) -> bool:
    """两个名称是否模糊相同（OCR 变体容错，如 嫣嫣↔景煨 / 嫣嫣AI→A）。

    归一化后序列相似度 ≥ threshold 且长度相近即认为相同；
    或两侧共享 ≥6 位长数字串（手机/QQ 号，区分度极高）直接判定相同。
    任一侧归一化后过短（<2 字）直接判否，避免短词高比例误判。
    实测变体最低 ratio 0.71（赵嫣嫣AI事务所→赵A事务所）→ 阈值 0.70；
    误开由 verify_chat_open（标题 OCR）兜底拦截。
    """
    import difflib
    na, nb = _norm_name(a), _norm_name(b)
    if len(na) < 2 or len(nb) < 2:
        return False
    if _shared_digit_run(a, b):
        return True
    if abs(len(na) - len(nb)) > max(2, len(na) // 3):
        return False
    return difflib.SequenceMatcher(None, na, nb).ratio() >= threshold


def match_session(target: str,
                  found: list[tuple[str, int]]) -> Optional[tuple[str, int]]:
    """在聚簇结果中找目标会话：先精确/子串，再变体（name_variant 兜底）。

    OCR 常见变体：AI↔AⅠ（罗马数字Ⅰ）/Al/A1、时间戳粘连、空格插入、
    跳字（赵嫣嫣AI事务所→赵A事务所）。变体判据与去重/验证同源
    （name_variant），误选由 verify_chat_open（标题 OCR）兜底拦截。
    """
    t = target.strip()
    for n, y in found:
        if n == t or t in n or n in t:
            return n, y
    if len(_norm_name(t)) < 2:
        return None
    for n, y in found:
        if name_variant(t, n):
            return n, y
    return None


def scroll_session_list_to_top(win, guard: SafetyGuard,
                               expected_items: int = 0,
                               max_iter: int = 0,
                               time_budget: float = 0.0) -> bool:
    """回顶（距离制 + 搜索框锚点确认，活跃列表上的确定性方案）。

    帧稳定判"到顶"在实时刷新的列表上不可靠（假稳定/永不稳定均实测出现），
    改为：按列表长度估算所需滚动次数（8 档/次 ≈ 296px，项高 65px），
    前 70% 距离盲滚（不可能到顶），之后每轮 OCR 检查搜索框锚点，
    锚点出现立即停；超量滚动在顶处空转无害。
    time_budget<=0 → 按轮数自适应（每轮实耗可达 ~3s：吞档+负载下 900s
    常量对 500+ 轮长列表不够——2026-09-05 实测 300 轮/903s 超时，列表
    未到顶即开始枚举会漏采顶部段）。
    返回是否确认到顶（锚点可见）。
    """
    region = win.session_list_rect()
    cap = ScreenCapture(region)
    ocr = OCRParser(0.4)
    # 悲观预算 ×2：快速连滚时档位会被吞（实测每轮实际 120~296px），
    # 按 ~150px/轮估需翻倍才能保证从列表底到顶；已在顶部时首轮即返回无浪费
    est = max(int(expected_items * 65 / 150) * 2 + 8, 60) if expected_items else 60
    if max_iter:
        est = min(est, max_iter)
    if time_budget <= 0:
        time_budget = max(900.0, est * 6.0)
    blind = int(est * 0.3)   # est 含 3~4× 吞档悲观余量，实测全程爬升仅需 ~30%；
    # 自 _confirm_list_top 加上推验证后假阳性已可控，可更早开始逐轮检查
    t0 = time.monotonic()
    time.sleep(0.3)   # 先等调用方的滚动动画结束，防动量伪影
    for i in range(est):
        # 前 2 轮也检查（已在顶部时秒回）；中段盲滚（不可能到顶）
        if i < 2 or i >= blind:
            if _confirm_list_top(win, cap, ocr, guard):
                log.info("回顶确认（第 %d/%d 轮，%.0fs）", i + 1, est,
                         time.monotonic() - t0)
                return True
        if i and i % 60 == 0:
            log.info("回顶中：%d/%d 轮（%.0fs）", i, est, time.monotonic() - t0)
        if time.monotonic() - t0 > time_budget:
            log.warning("回顶超时（%.0fs > 预算 %.0fs），按当前位置继续",
                        time.monotonic() - t0, time_budget)
            break
        scroll_session_list(win, guard, +120, 8, pause=0.08)
        time.sleep(0.2)
    return _confirm_list_top(win, cap, ocr, guard)


def scaled_scan_rounds(expected_items: int, base: int = 60,
                       cap: int = 300) -> int:
    """按列表长度放大单程扫描轮数（悲观预算：快速连滚时档位会被吞，
    每轮 8 档实际位移实测 120~296px，按 110px/轮 ≈ 1.7 项 + 20% 余量）。

    expected_items=0/未知 → 用 base。实测列表高 ~15,700px（242 项），
    枚举名数会因 OCR 变体合并而**低估**真实高度（140 名 ≈ 242 项高），
    故除数取实测最差值；向下扫描另有 3× 硬上限 + 端点检测兜底。
    """
    if expected_items <= 0:
        return base
    est = expected_items * 65 / 110 * 1.2 + 8
    return int(max(base, min(est, cap)))


def window_contains(target: str, token: str, threshold: float = 0.80) -> bool:
    """截断名包容匹配：会话列表把长名截成前几个字（target），
    面板标题显示全名（token），且 OCR 常翻转 1 个字（优↔尤）。

    在 token 上取 len(target) 的滑窗，任一窗口相似度 ≥ threshold 判中。
    仅对 ≥4 字目标生效（4 字窗需全对，5 字窗容 1 错 = 0.8，6+ 字容 1 错），
    防短名在无关长名里随机撞中。
    """
    import difflib
    t = _norm_name(target)
    n = _norm_name(token)
    if len(t) < 4 or len(n) <= len(t):
        return False
    return any(difflib.SequenceMatcher(None, t, n[i:i + len(t)]).ratio()
               >= threshold
               for i in range(len(n) - len(t) + 1))


def _subseq_drop(short: str, long: str) -> bool:
    """跳字变体：短读数是长读数的子序列（OCR 漏掉中间字，如
    「赵A事务所」⊂「赵嫣嫣AI事务所」——既非前缀也非模糊相等）。

    约束：短侧 ≥4 字、首字相同、长度差 1~6 字，控制误合并。
    """
    s, l = _norm_name(short), _norm_name(long)
    if len(s) < 4 or not (1 <= len(l) - len(s) <= 6) or s[0] != l[0]:
        return False
    it = iter(l)
    return all(c in it for c in s)


def name_variant(a: str, b: str) -> bool:
    """两个读数是否同一聊天名的 OCR 变体（用于枚举/去重/验证）。

    三级判据：模糊相等（单字翻转）→ 滑窗包容（前缀截断+翻转）
    → 跳字子序列（中间漏字）。任一成立即视为同一名。
    """
    return (fuzzy_same(a, b)
            or window_contains(a, b) or window_contains(b, a)
            or _subseq_drop(a, b) or _subseq_drop(b, a))


class WeChatNavigator:
    def __init__(self, win, guard: SafetyGuard,
                 settle_wait: float = 1.5,
                 input_zone_ratio: float = 0.80):
        self.win = win
        self.guard = guard
        self.settle_wait = settle_wait
        self.input_zone_ratio = input_zone_ratio

    # ------------------------------------------------------------ manual
    def manual(self, chat_name: str, countdown: int = 10,
               on_progress: Optional[Callable[[str], None]] = None) -> None:
        say = on_progress or (lambda m: log.info(m))
        say(f"➡ 请在 {countdown} 秒内手动点开聊天「{chat_name}」并保持微信窗口在最前 …")
        for i in range(countdown, 0, -1):
            say(f"   {i} …")
            time.sleep(1)
        time.sleep(self.settle_wait)

    # ------------------------------------------------------------ session
    def by_session_list(self, chat_name: str,
                        on_progress: Optional[Callable[[str], None]] = None,
                        retries: int = 2) -> bool:
        say = on_progress or (lambda m: log.info(m))
        ocr = OCRParser(0.4)

        for attempt in range(retries):
            self.win.activate()
            region = self.win.session_list_rect()
            img = ScreenCapture(region).grab()
            texts = ocr.parse(img)
            if not texts:
                say("   会话列表 OCR 为空")
                continue

            target = chat_name.strip()
            exact = [t for t in texts if t["text"].strip() == target]
            partial = [t for t in texts
                       if target in t["text"] or t["text"] in target]
            cand = exact or partial
            if not cand:
                say(f"   会话列表中未找到「{target}」（可见 {len(texts)} 项），"
                    "回落手动模式或先在微信里打开该聊天")
                return False

            t = cand[0]
            abs_x = region[0] + t["cx"]
            abs_y = region[1] + t["cy"]
            try:
                self.guard.check_nav_click(abs_x, abs_y, self.win.rect,
                                           session_rect=region)
            except Exception as e:
                say(f"   ⚠ 候选坐标被安全护栏拒绝（{e}），回落手动模式")
                return False
            say(f"   点击会话「{t['text']}」 @ ({abs_x},{abs_y})")
            self._click(abs_x, abs_y)
            time.sleep(self.settle_wait)

            if self.verify_chat_open(chat_name, ocr, say):
                return True
            say(f"   ⚠ 第 {attempt + 1} 次点击后未检测到目标聊天标题，重试 …")

        return self.verify_chat_open(chat_name, ocr, say)

    # ------------------------------------------------------------ batch open
    def open_chat(self, chat_name: str,
                  on_progress: Optional[Callable[[str], None]] = None,
                  max_rounds: int = 0,
                  expected_items: int = 0,
                  start_from_current: bool = False) -> bool:
        """批量模式：滚动会话列表查找目标聊天，点击并验证打开。

        扫描顺序（start_from_current=True，批量按列表序导出）：
          1. 从当前位置向下扫（目标通常就在下方一两屏内）
          2. 向上扫一段（新消息把目标顶到视野上方的情况）
          3. 回顶全量向下扫（兜底：重排/起点异常）
        扫描轮数按 expected_items（枚举总数）自动放大；
        找到后点击并 OCR 标题头验证，验证失败重试一次点击。
        """
        say = on_progress or (lambda m: log.info(m))
        ocr = OCRParser(0.4)
        self.win.activate()
        region = self.win.session_list_rect()
        cap = ScreenCapture(region)
        target = chat_name.strip()
        rounds = max_rounds or scaled_scan_rounds(expected_items)

        if start_from_current:
            if self._scan_and_click(cap, region, ocr, target, say,
                                    rounds, direction=-1):
                return True
            say(f"   ↕ 「{target}」下方未见，转向上扫 …")
            if self._scan_and_click(cap, region, ocr, target, say,
                                    rounds // 3 or 20, direction=+1):
                return True
        # 回顶全量重扫（列表可能重排或中途有新消息插入）
        if not scroll_session_list_to_top(self.win, self.guard,
                                          expected_items=expected_items):
            say(f"   ⚠ 「{target}」回顶未确认（搜索框锚点未识别），仍全量扫 …")
        if self._scan_and_click(cap, region, ocr, target, say,
                                rounds, direction=-1):
            return True
        say(f"   ✗ 会话列表中未找到「{chat_name}」（可能无近期会话）")
        return False

    def _scan_and_click(self, cap, region, ocr, target: str, say,
                        max_rounds: int, direction: int = -1) -> bool:
        """沿 direction 扫描列表（-1 向下 / +1 向上），找到即点击+验证。

        帧比较判端点（OCR 文本集抖动大，不可靠）；未命中结束时
        打印最后可见的会话名，留下诊断证据。
        """
        from .session_enum import cluster_sessions
        from .capture import ScreenCapture

        stable = 0
        guard_rejects = 0   # 连续护栏拒绝计数（防无进展死循环）
        last_names: list[str] = []
        # 向下扫描的预算按枚举数估算，而枚举名数因 OCR 变体合并会低估
        # 列表真实高度（实测 140 名 ≈ 242 项高 ≈ 15,700px）→ 预算可能
        # 覆盖不到列表尾段。向下扫不因预算截断：给 3× 硬上限，
        # 靠端点检测（连续 3 帧不动）确认真正到底
        hard_cap = max_rounds * 3 if direction < 0 else max_rounds
        rnd = -1
        for rnd in range(hard_cap):
            texts = ocr.parse(cap.grab())
            if direction > 0 and at_list_top(texts) \
                    and _confirm_list_top(self.win, cap, ocr):
                return False   # 上扫已到顶（双帧确认，防动画伪影）
            found = cluster_sessions(texts, list_width=region[2])
            last_names = [n for n, _ in found[:6]]
            cand = match_session(target, found)
            if cand:
                name, y = cand
                # 名称经聚簇清洗（剥时间戳尾巴）后与 OCR 原文可能不等，
                # 用子串关系回找原文本坐标；找不到则退回列表固定 x。
                t = next((t for t in texts
                          if t["text"].strip() == name
                          or name in t["text"] or t["text"] in name), None)
                abs_x = region[0] + (t["cx"] if t else 120)
                abs_y = region[1] + y
                try:
                    self.guard.check_nav_click(abs_x, abs_y, self.win.rect,
                                               session_rect=region)
                except Exception as e:
                    # 护栏拒绝：滚半屏让目标移位后重试；连续多次仍拒绝
                    # （如端点处目标钉死在不可点位置）则放弃本趟，绝无进展
                    # 死循环——continue 前必须真的滚动
                    guard_rejects += 1
                    say(f"   ⚠ 「{target}」点击坐标被安全护栏拒绝（{e}），"
                        f"滚动后重试（{guard_rejects}/4）")
                    if guard_rejects >= 4:
                        say(f"   ⚠ 「{target}」连续被护栏拒绝，放弃本趟扫描")
                        return False
                    scroll_session_list(self.win, self.guard,
                                        direction * 120, 4, pause=0.1)
                    time.sleep(0.3)
                    continue
                guard_rejects = 0
                self._click(abs_x, abs_y)
                time.sleep(self.settle_wait)
                if self.verify_chat_open(target, ocr, say):
                    return True
                # 验证失败：可能点歪，重试一次
                self._click(abs_x, abs_y)
                time.sleep(self.settle_wait)
                if self.verify_chat_open(target, ocr, say):
                    return True
                say(f"   ⚠ 「{target}」点击后验证未通过，继续查找 …")
            # 未命中：滚 8 档（≈4.5 项；视口 ~9 项，重叠充分不漏项）
            before = cap.grab()
            scroll_session_list(self.win, self.guard,
                                direction * 120, 8, pause=0.1)
            time.sleep(0.3)
            if ScreenCapture.frames_equal(before, cap.grab()):
                stable += 1
                if stable >= 3:
                    return False   # 已到端点（底部/顶部）
            else:
                stable = 0
        say(f"   · 扫描 {rnd + 1} 轮未见「{target}」，"
            f"末屏可见：{'、'.join(last_names)}")
        return False

    # ------------------------------------------------------------ verify
    def verify_chat_open(self, chat_name: str, ocr: OCRParser,
                         say: Callable[[str], None]) -> bool:
        """OCR 聊天面板标题区，确认目标聊天名出现（含模糊容错）。"""
        try:
            region = self.win.chat_header_rect()
            img = ScreenCapture(region).grab()
            texts = ocr.parse(img)
            if not texts:   # 面板可能仍在加载（切换动画中）→ 等一拍重读
                time.sleep(1.0)
                texts = ocr.parse(ScreenCapture(region).grab())
            header = " ".join(t["text"] for t in texts)
            target = chat_name.strip()
            ok = (target in header) or any(
                target in t["text"] or t["text"] in target
                or name_variant(target, t["text"])
                for t in texts if len(t["text"]) >= 2)
            say(f"   标题头 OCR：{header[:40]!r} → {'✅ 已打开' if ok else '❌ 未匹配'}")
            return ok
        except Exception as e:
            log.warning("标题验证异常：%s", e)
            return False

    # ------------------------------------------------------------ internals
    def _click(self, x: int, y: int) -> None:
        import pyautogui

        pyautogui.moveTo(x, y, duration=0.2)
        pyautogui.click(button="left")   # 仅左键；右键被 SafetyGuard 语义禁用
