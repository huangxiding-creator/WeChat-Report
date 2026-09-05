# -*- coding: utf-8 -*-
"""视觉提取器（核心）：截图 + OCR + 气泡检测 + 时间窗 + 断点续采。

只读铁律由 SafetyGuard 全程强制（详见 safety.py）。
"""
from __future__ import annotations

import logging
import time
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

import numpy as np

from ..models import Chat
from .base import BaseExtractor
from .bubbles import ImageBubbleDetector
from .capture import ScreenCapture, imwrite_png
from .composer import MessageComposer
from .navigator import WeChatNavigator
from .ocr import OCRParser
from .safety import SafetyGuard
from .scroller import ChatScroller
from .timelabels import is_time_label, parse_window
from .window import WeChatWindow

log = logging.getLogger("wcr.visual")


class VisualExtractor(BaseExtractor):
    """把指定聊天的「文字 + 图片 + 语音标记」按时间顺序采集为 Chat。"""

    def __init__(self,
                 navigate_mode: str = "auto",
                 manual_countdown: int = 10,
                 settle_wait: float = 1.5,
                 scroll_pause: float = 0.35,
                 scroll_step: int = 15,
                 scroll_attempts: int = 800,
                 stable_frames: int = 3,
                 scrollup_time_budget: int = 300,
                 scrollup_mode: str = "probe",   # probe=探测时间标签; top=滚到顶(快)
                 max_screens: int = 3000,
                 ocr_threshold: float = 0.5,
                 speaker_attribution: bool = True,
                 checkpoint_enabled: bool = True,
                 keep_screenshots: bool = True,
                 output_dir: Path = Path("./output"),
                 image_min_w: int = 90,
                 image_min_h: int = 90,
                 image_min_std: float = 20.0,
                 image_iou_merge: float = 0.3,
                 input_zone_ratio: float = 0.80):
        self.navigate_mode = navigate_mode
        self.manual_countdown = manual_countdown
        self.settle_wait = settle_wait
        self.scroll_pause = scroll_pause
        self.scroll_step = scroll_step
        self.scroll_attempts = scroll_attempts
        self.stable_frames = stable_frames
        self.scrollup_time_budget = scrollup_time_budget
        self.scrollup_mode = scrollup_mode
        self.max_screens = max_screens
        self.ocr_threshold = ocr_threshold
        self.speaker_attribution = speaker_attribution
        self.checkpoint_enabled = checkpoint_enabled
        self.keep_screenshots = keep_screenshots
        self.output_dir = Path(output_dir)
        self.detector = ImageBubbleDetector(image_min_w, image_min_h,
                                            image_min_std, image_iou_merge)
        self.guard = SafetyGuard(input_zone_ratio=input_zone_ratio)

    # ------------------------------------------------------------ entry
    def extract(self, chat_name: str, time_window: str = "",
                on_progress: Optional[Callable[[str], None]] = None,
                already_open: bool = False) -> Chat:
        say = on_progress or (lambda m: log.info(m))
        start_dt, end_dt = parse_window(time_window)
        say(f"📂 目标聊天：「{chat_name}」 时间窗：{time_window or '全量'}")

        # 1. 定位并激活微信窗口
        win = WeChatWindow().find()
        win.activate()

        # 2. 标定聊天面板左缘（像素级分界线检测）
        win_rect_full = ScreenCapture(win.rect).grab()
        win.calibrate(win_rect_full)
        chat_rect = win.chat_area_rect(self.guard.input_zone_ratio)
        say(f"   聊天采集区：{chat_rect}")

        # 3. 导航（批量模式由 BatchExporter 先 open_chat，跳过）
        nav = WeChatNavigator(win, self.guard, self.settle_wait,
                              self.guard.input_zone_ratio)
        if already_open:
            pass
        elif self.navigate_mode == "manual":
            nav.manual(chat_name, self.manual_countdown, say)
        else:
            if not nav.by_session_list(chat_name, say):
                say("   ↩ 自动导航失败，回落手动模式")
                nav.manual(chat_name, self.manual_countdown, say)

        # 4. 准备组件
        shot_dir = self.output_dir / "_screenshots" / _slug(chat_name)
        shot_dir.mkdir(parents=True, exist_ok=True)
        cap = ScreenCapture(chat_rect)
        ocr = OCRParser(self.ocr_threshold)
        scroller = ChatScroller(win, cap, self.guard,
                                scroll_pause=self.scroll_pause,
                                scroll_step=self.scroll_step,
                                stable_frames_to_stop=self.stable_frames,
                                max_attempts=self.scroll_attempts,
                                input_zone_ratio=self.guard.input_zone_ratio)
        composer = MessageComposer(self.speaker_attribution)

        # 断点恢复（先校验面板位置：微信重开会话/新消息涌入会把面板拉回
        # 底部，断点位置已失——此时续采会跳过中段消息，宁可放弃断点全量重采）
        ckpt = self.output_dir / "checkpoints" / f"{_slug(chat_name)}.json"
        resumed = False
        if self.checkpoint_enabled and composer.load_checkpoint(ckpt):
            if self._resume_position_valid(cap, shot_dir, composer.screen_count):
                resumed = True
                say(f"   ↩ 断点恢复：已有 {len(composer.messages)} 条")
            else:
                say("   ↩ 断点位置已失效（面板被移动）→ 放弃断点，全量重采")
                composer = MessageComposer(self.speaker_attribution)

        # 5. 向上滚动（时间窗模式：逐屏探测，见到早于 start 的标签即停）
        if not resumed:
            if self.scrollup_mode == "top":
                # 批量/全量导出：直接滚到本地缓存顶（最快），一年窗靠事后过滤
                say("⏫ 滚动到聊天顶部（本地缓存顶）…")
                scroller.scroll_to_top()
            else:
                self._scroll_up_with_window(scroller, cap, ocr, start_dt, say)

        # 6. 向下逐屏采集（时间标签记在 composer 上，随检查点持久化）
        screen_idx = 0 if not resumed else composer.screen_count
        total_added = 0
        zero_add_screens = 0
        say("📸 开始逐屏采集 …")
        while True:
            img = cap.grab()
            if self.keep_screenshots:
                imwrite_png(shot_dir / f"screen{screen_idx:04d}.png", img)

            ocr_texts = ocr.parse(img)
            time_texts = [t for t in ocr_texts if is_time_label(t["text"])]
            msg_texts = [t for t in ocr_texts if not is_time_label(t["text"])]
            voice_marks, msg_texts = self.detector.split_voice_marks(msg_texts)
            img_rects = self.detector.detect(img, ocr_texts)
            composer.time_labels[screen_idx] = time_texts

            added = composer.add_screen(
                screen_idx, msg_texts, img_rects, voice_marks, img,
                shot_dir, img_width=img.shape[1],
            )
            total_added += added
            zero_add_screens = zero_add_screens + 1 if added == 0 else 0

            # 时间窗上界：本屏最早时间标签已晚于 end → 停止
            if end_dt and time_texts:
                earliest = ChatScroller.earliest_time_in_texts(time_texts)
                if earliest and earliest > end_dt:
                    say(f"   ✔ 已越过时间窗上界 {end_dt:%Y-%m-%d}，停止采集")
                    break

            moved = scroller.scroll_down_one_screen()
            screen_idx += 1
            if screen_idx % 10 == 0:
                say(f"   已采集 {screen_idx} 屏 / 新增 {total_added} 条")
                if self.checkpoint_enabled:
                    composer.save_checkpoint(ckpt)
            if not moved:
                # 下滚冻结甄别：懒加载/负载会整段吞档（实测目标「黄藏寺项目
                # 值班」5 屏即停、只采到 1 天）——歇一拍再冲一屏，三重确认
                # 仍不动才算采集完成；冲开了就继续正常循环
                time.sleep(1.2)
                if scroller.scroll_down_one_screen():
                    screen_idx += 1
                    continue
                time.sleep(1.5)
                if scroller.scroll_down_one_screen():
                    screen_idx += 1
                    continue
                say(f"   ✔ 画面不再变化（三重确认），采集完成（共 {screen_idx} 屏）")
                break
            if zero_add_screens >= 20:
                say(f"   ⚠ 连续 {zero_add_screens} 屏无新增消息（可能有动图/视频在播放），停止")
                break
            if screen_idx > self.max_screens:
                say(f"   ⚠ 屏数超过 {self.max_screens} 上限，强制结束")
                break

        # 7. 收尾：时间回填 + 排序 + 过滤 + 持久化
        composer.assign_times(composer.time_labels)
        messages = composer.sort()
        # 实际覆盖区间：按过滤前的全部时间戳算（窗口外全部滤掉时仍能看到
        # 该聊天缓存的真实时间范围，如 "2024-10-12 ~ 2024-10-24"）
        all_ts = [m.timestamp for m in messages if m.timestamp]
        coverage = (f"{min(all_ts):%Y-%m-%d} ~ {max(all_ts):%Y-%m-%d}"
                    if all_ts else "")
        if start_dt or end_dt:
            n0 = len(messages)
            messages = [m for m in messages
                        if m.timestamp is None or
                        ((not start_dt or m.timestamp >= start_dt) and
                         (not end_dt or m.timestamp <= end_dt))]
            say(f"   时间窗过滤：{n0} → {len(messages)} 条")

        chat = Chat(name=chat_name,
                    messages=messages,
                    captured_at=datetime.now().strftime("%Y-%m-%d %H:%M:%S"),
                    time_window=time_window or "全量")
        chat.coverage = coverage
        for m in chat.messages:
            m.chat_name = chat_name

        json_path = self.output_dir / f"{_slug(chat_name)}_messages.json"
        chat.to_json(json_path)
        if self.checkpoint_enabled and ckpt.exists():
            ckpt.unlink()  # 采集完成，清理断点
        say(f"💾 「{chat_name}」采集完成：{len(messages)} 条 → {json_path.name}")
        return chat

    # ------------------------------------------------------------ helpers
    @staticmethod
    def _frames_relate(a, b) -> float:
        """两帧粗略相关性（0~1）：32×32 灰度归一化内积。同区域视图高相关，
        不同区域（如断点位置丢失后面板已回到底部）低相关。"""
        import cv2

        if a is None or b is None or a.shape[:2] != b.shape[:2] and (
                a.size == 0 or b.size == 0):
            return 0.0
        try:
            ga = cv2.resize(cv2.cvtColor(a, cv2.COLOR_BGR2GRAY), (32, 32),
                            interpolation=cv2.INTER_AREA).astype("float32")
            gb = cv2.resize(cv2.cvtColor(b, cv2.COLOR_BGR2GRAY), (32, 32),
                            interpolation=cv2.INTER_AREA).astype("float32")
        except cv2.error:
            return 0.0
        ga -= ga.mean()
        gb -= gb.mean()
        denom = float((np.sqrt((ga * ga).sum()) * np.sqrt((gb * gb).sum())))
        return float((ga * gb).sum() / denom) if denom > 1e-6 else 0.0

    def _resume_position_valid(self, cap: ScreenCapture, shot_dir: Path,
                               screen_count: int) -> bool:
        """断点位置校验：当前帧与检查点末屏截图仍高相关才算位置未丢。"""
        if screen_count <= 0:
            return False
        from .capture import imread_png

        last = imread_png(shot_dir / f"screen{screen_count - 1:04d}.png")
        if last is None:
            return False       # 末屏截图缺失（keep_screenshots=False/被清理）
        return self._frames_relate(cap.grab(), last) >= 0.6

    def _scroll_up_with_window(self, scroller: ChatScroller, cap: ScreenCapture,
                               ocr: OCRParser, start_dt, say) -> None:
        """向上滚动；时间窗模式下周期 OCR 探测最早时间标签，无窗起点
        （全量深度，年份令牌选择）时滚到动态加载两轮验证的真顶。

        兜底：连续多次探测不到任何时间标签 → 判定采集区异常，立即报错
        （避免空转）；画面稳定 → 已到顶，正常返回。
        """
        target = (f"时间窗起点 {start_dt:%Y-%m-%d}" if start_dt
                  else "聊天顶部（全量深度）")
        say(f"⏫ 向上滚动至{target} …")
        probe_every = 6          # 每 6 次大滚动 OCR 一次（OCR 是探针主要
                                  # 开销，加密滚轮提高推进速度）
        empty_probes = 0         # 连续 OCR 到 0 个文本块 → 采集区异常
        text_stable = 0          # 连续 OCR 文本集完全相同 → 已到顶（防 GIF 动图误判）
        last_seen = None
        prev_texts: frozenset | None = None
        # 预算语义：<=0 = 不限时（用户要求：加载是动态的，滚到窗起点或
        # 验证过的真顶为止）；>0 = 墙钟预算兜底
        no_budget = self.scrollup_time_budget <= 0
        deadline = (None if no_budget
                    else time.monotonic() + self.scrollup_time_budget)
        rounds_cap = (100_000 if no_budget else self.scroll_attempts)
        for round_ in range(rounds_cap):
            for _ in range(probe_every):
                scroller._wheel(scroller.scroll_step * 8)
                time.sleep(self.scroll_pause)
            texts = ocr.parse(cap.grab())
            if not texts:
                empty_probes += 1
                if empty_probes >= 6:
                    raise RuntimeError(
                        "连续多次 OCR 不到任何文本：聊天采集区可能不正确，"
                        "请检查微信窗口布局后重试。"
                    )
            else:
                empty_probes = 0
                labels = [t for t in texts if is_time_label(t["text"])]
                if labels:
                    earliest = scroller.earliest_time_in_texts(labels, last_seen)
                    if earliest:
                        last_seen = earliest
                        if start_dt and earliest <= start_dt:
                            say(f"   ✔ 已到达时间窗起点（见到 {earliest:%Y-%m-%d %H:%M}）")
                            return
                # 文本集稳定性：动图只改像素不改 OCR 文本，是比帧差更可靠的到顶判据
                cur = frozenset(t["text"] for t in texts)
                text_stable = text_stable + 1 if cur == prev_texts else 0
                prev_texts = cur
                if text_stable >= self.stable_frames:
                    # 动态加载甄别（用户实测指正：聊天记录上滚会按需从服务器
                    # 续载，稳定 3 次可能只是加载间隙）——休 3s 大冲 24 档再
                    # OCR，两轮验证；内容有变 = 更早历史正在加载，继续上滚
                    if self._top_verify_stable(scroller, cap, ocr, cur, say):
                        say("   ✔ 已滚动到聊天顶部（动态加载两轮验证均无变化），从顶部开始采集")
                        return
                    text_stable = 0
                    prev_texts = None
            if round_ % 10 == 0 and round_:
                tip = f"{last_seen:%Y-%m-%d %H:%M}" if last_seen else "尚无"
                say(f"   上滚第 {round_ * probe_every} 轮，最早见到 {tip}")
            if deadline is not None and time.monotonic() > deadline:
                say(f"   ⚠ 上滚超过 {int(self.scrollup_time_budget)}s 预算，按现有位置继续采集")
                return
        say("   ⚠ 上滚轮数用尽，按现有位置继续采集")

    def _top_verify_stable(self, scroller, cap, ocr, cur: frozenset,
                           say) -> bool:
        """到顶二次甄别：初判稳定后大冲两轮复验（防动态加载间隙误判顶）。

        真顶时大冲不产生位移、OCR 文本集不变；加载间隙时休 3s + 24 档
        大冲会给续载留出时间，内容一变即证伪。
        """
        for i in (1, 2):
            time.sleep(3.0)
            scroller._wheel(scroller.scroll_step * 24)
            time.sleep(2.5)
            texts = ocr.parse(cap.grab())
            if not texts:
                say(f"   ↺ 疑似顶第 {i} 轮验证 OCR 为空（非顶），继续上滚")
                return False
            if frozenset(t["text"] for t in texts) != cur:
                say(f"   ↺ 疑似顶第 {i} 轮验证发现新内容（动态加载恢复），继续上滚")
                return False
        return True


def _slug(name: str) -> str:
    """聊天名 → 安全文件名。"""
    keep = [c if (c.isalnum() or c in "-_一-龥") else "_" for c in name]
    s = "".join(keep).strip("_") or "chat"
    return s[:60]
