# -*- coding: utf-8 -*-
"""消息组装：跨屏去重、时间归属、说话人启发式归属、排序、断点序列化。"""
from __future__ import annotations

import hashlib
import json
import logging
import re
from datetime import datetime
from pathlib import Path
from typing import Optional

import cv2
import numpy as np

from ..models import Message, text_fingerprint
from .capture import imwrite_png
from .timelabels import is_time_label, parse_time_label

log = logging.getLogger("wcr.composer")

# 疑似系统提示/红包/撤回/界面按钮等无正文价值的模式
_NOISE_RE = re.compile(
    r"^(以下为新消息|你已添加了|以上是打招呼的内容|收到了一条新消息|\[.*撤回.*\]|"
    r"领了红包|收到了红包|发出了红包|拍了拍|以下为合并的消息|"
    r"\d+条新消息|跳转到最新消息|查看更多消息|对方正在输入.*)$"
)


def image_fingerprint(crop_bgr: np.ndarray) -> str:
    """dHash：同一张图在不同屏位置也能匹配。"""
    try:
        gray = cv2.cvtColor(crop_bgr, cv2.COLOR_BGR2GRAY)
        resized = cv2.resize(gray, (9, 8))
        diff = resized[:, 1:] > resized[:, :-1]
        bits = "".join(diff.flatten().astype(int).astype(str))
        return "i|" + bits
    except Exception:
        return "i|bytes|" + hashlib.md5(np.ascontiguousarray(crop_bgr).tobytes()).hexdigest()


class MessageComposer:
    """跨屏去重、按时间标签分组排序；支持 checkpoint 序列化以断点续采。"""

    def __init__(self, speaker_attribution: bool = True):
        self.seen: set[str] = set()
        self.messages: list[Message] = []
        self.last_parsed_time: Optional[datetime] = None
        self.speaker_attribution = speaker_attribution
        self.screen_count = 0

    # ------------------------------------------------------------ add screen
    def add_screen(self, screen_idx: int,
                   ocr_texts: list[dict],
                   image_rects: list[tuple[int, int, int, int]],
                   voice_marks: list[dict],
                   img_bgr: np.ndarray,
                   screenshot_dir: Path,
                   img_width: int = 0) -> int:
        """添加一屏内容，返回新增消息数。

        ocr_texts 应已剔除时间标签（由调用方 split）。
        """
        added = 0
        # 当前屏上下文时间：取本屏所有时间标签中最晚（最靠下）的之前最近标签，
        # 这里简化为：更新 last_parsed_time（若本屏有时间标签则取其解析值）
        current_time_label = ""
        w = img_width or img_bgr.shape[1]
        mid_x = w // 2

        # 文本消息（按 y 自上而下）
        for t in sorted(ocr_texts, key=lambda t: t["cy"]):
            fp = text_fingerprint(t["text"])
            if fp in self.seen:
                continue
            if _NOISE_RE.match(t["text"]):
                continue
            self.seen.add(fp)
            box = (int(t["box"][:, 0].min()), int(t["box"][:, 1].min()),
                   int(t["box"][:, 0].max()), int(t["box"][:, 1].max()))
            msg = Message(
                kind="text", text=t["text"], box=box,
                time_label=current_time_label, screen_idx=screen_idx,
                fingerprint=fp,
            )
            if self.speaker_attribution:
                cx = t["cx"]
                msg.side = "right" if cx > mid_x * 1.02 else "left"
                msg.speaker = self._guess_speaker(ocr_texts, t, mid_x)
            self.messages.append(msg)
            added += 1

        # 图片消息（裁剪 → dHash 指纹 → 去重 → 落盘）
        for i, rect in enumerate(image_rects):
            x, y, rw, rh = rect
            crop = img_bgr[y:y + rh, x:x + rw]
            fp = image_fingerprint(crop)
            if fp in self.seen:
                continue
            self.seen.add(fp)
            img_path = screenshot_dir / f"screen{screen_idx:04d}_img{i:02d}.png"
            imwrite_png(img_path, crop)
            msg = Message(
                kind="image", img_path=str(img_path), box=rect,
                screen_idx=screen_idx, fingerprint=fp,
                side="right" if (x + rw / 2) > mid_x * 1.02 else "left",
            )
            self.messages.append(msg)
            added += 1

        # 语音标记（OCR 读到的 "12''"）
        for v in voice_marks:
            fp = "v|" + text_fingerprint(f"{v['text']}@{v['cy']}")
            if fp in self.seen:
                continue
            self.seen.add(fp)
            self.messages.append(Message(
                kind="voice", text=v["text"],
                box=(int(v["box"][:, 0].min()), int(v["box"][:, 1].min()),
                     int(v["box"][:, 0].max()), int(v["box"][:, 1].max())),
                screen_idx=screen_idx, fingerprint=fp,
                side="right" if v["cx"] > mid_x * 1.02 else "left",
            ))
            added += 1

        self.screen_count = max(self.screen_count, screen_idx + 1)
        return added

    # ------------------------------------------------------------ speaker
    @staticmethod
    def _guess_speaker(ocr_texts: list[dict], target: dict, mid_x: int) -> str:
        """启发式：群聊中他人消息的昵称行在气泡上方（左侧）。

        条件：短文本（≤12 字符）、在目标上方 8~40px、x 与目标左缘接近。
        我方消息（右侧）统一返回空（报告层显示为"我方"）。
        """
        if target["cx"] > mid_x * 1.02:
            return ""  # 右侧 = 我方
        tx_left = target["box"][:, 0].min()
        best = ""
        best_dy = 10 ** 9
        for t in ocr_texts:
            if t is target:
                continue
            txt = t["text"]
            if len(txt) > 12 or is_time_label(txt):
                continue
            if t["cx"] > mid_x:
                continue
            dy = target["box"][:, 1].min() - t["box"][:, 1].max()
            if 4 <= dy <= 44 and abs(t["box"][:, 0].min() - tx_left) < 90:
                if dy < best_dy:
                    best_dy = dy
                    best = txt
        return best

    # ------------------------------------------------------------ finalize
    def assign_times(self, time_labels_by_screen: dict[int, list[dict]]) -> None:
        """按屏时间标签为消息补充 time_label / timestamp。

        time_labels_by_screen: {screen_idx: [{text, cy}, ...]}
        采集时逐屏记录，最终统一回填（屏幕从上往下采集，时间递增）。
        """
        # 屏号升序遍历；每屏内消息 y 升序；时间标签按 y 排序后，
        # 消息的 time_label 取其 y 之上最近的时间标签（本屏或延续上一屏）
        carry_label = ""
        carry_ts = None
        for sidx in sorted(time_labels_by_screen.keys()):
            labels = sorted(time_labels_by_screen[sidx], key=lambda t: t["cy"])
            msgs = sorted([m for m in self.messages if m.screen_idx == sidx],
                          key=lambda m: m.box[1])
            li = 0
            cur_label, cur_ts = carry_label, carry_ts
            for m in msgs:
                while li < len(labels) and labels[li]["cy"] < m.box[1]:
                    cur_label = labels[li]["text"]
                    ts = parse_time_label(cur_label, last_seen=carry_ts)
                    if ts:
                        cur_ts = ts
                        carry_ts = ts
                    li += 1
                m.time_label = cur_label
                m.timestamp = cur_ts
            if labels:
                last = labels[-1]["text"]
                ts = parse_time_label(last, last_seen=carry_ts)
                if ts:
                    carry_ts = ts
                carry_label = cur_label

    def sort(self) -> list[Message]:
        self.messages.sort(key=lambda m: (m.screen_idx, m.box[1]))
        return self.messages

    # ------------------------------------------------------------ checkpoint
    def save_checkpoint(self, path: Path) -> None:
        payload = {
            "screen_count": self.screen_count,
            "seen": sorted(self.seen),
            "messages": [m.to_dict() for m in self.messages],
            "last_parsed_time": (self.last_parsed_time.strftime("%Y-%m-%d %H:%M:%S")
                                 if self.last_parsed_time else ""),
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    def load_checkpoint(self, path: Path) -> bool:
        if not path.exists():
            return False
        try:
            payload = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            return False
        self.screen_count = payload.get("screen_count", 0)
        self.seen = set(payload.get("seen", []))
        self.messages = [Message.from_dict(d) for d in payload.get("messages", [])]
        if payload.get("last_parsed_time"):
            try:
                self.last_parsed_time = datetime.strptime(payload["last_parsed_time"],
                                                          "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        log.info("断点恢复：已完成 %d 屏，%d 条消息", self.screen_count, len(self.messages))
        return True
