# -*- coding: utf-8 -*-
"""统一消息与聊天数据模型（提取层 → 分析层 → 报告层的公共契约）。"""
from __future__ import annotations

import hashlib
import json
import re
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from typing import Optional


def text_fingerprint(text: str) -> str:
    """文本指纹：去空白后 md5，避免 OCR 空格抖动导致漏去重。"""
    normalized = re.sub(r"\s+", "", text or "")
    return "t|" + hashlib.md5(normalized.encode("utf-8")).hexdigest()


# ------------------------------------------------------------------ message
@dataclass
class Message:
    kind: str = "text"            # text | image | voice | time | unknown
    text: str = ""                # 文本内容 / 图片说明 / 语音时长标记
    img_path: str = ""            # 图片裁剪文件路径（相对 output）
    box: tuple = (0, 0, 0, 0)     # 屏内坐标（调试用）
    time_label: str = ""          # 所属时间标签原文，如 "2026年8月5日" / "14:30"
    timestamp: Optional[datetime] = None   # 解析后的时间（尽力而为）
    screen_idx: int = 0
    speaker: str = ""             # 发送者（启发式，可能为空）
    side: str = ""                # left=对方 right=我方（启发式）
    chat_name: str = ""           # 归属聊天（多聊天合并时区分）
    fingerprint: str = ""

    def to_dict(self) -> dict:
        d = asdict(self)
        d["timestamp"] = self.timestamp.strftime("%Y-%m-%d %H:%M:%S") if self.timestamp else ""
        d["box"] = list(self.box)
        return d

    @classmethod
    def from_dict(cls, d: dict) -> "Message":
        m = cls(**{k: v for k, v in d.items() if k in cls.__dataclass_fields__ and k != "timestamp"})
        if d.get("timestamp"):
            try:
                m.timestamp = datetime.strptime(d["timestamp"][:19], "%Y-%m-%d %H:%M:%S")
            except ValueError:
                pass
        return m


# ------------------------------------------------------------------ chat
@dataclass
class Chat:
    """一次采集得到的完整聊天记录。"""
    name: str = ""                        # 群名 / 好友名
    messages: list[Message] = field(default_factory=list)
    captured_at: str = ""
    time_window: str = ""
    coverage: str = ""                    # 过滤前的实际覆盖区间（如 2024-10-12 ~ 2024-10-24）

    # 便捷视图
    @property
    def texts(self) -> list[Message]:
        return [m for m in self.messages if m.kind == "text"]

    @property
    def images(self) -> list[Message]:
        return [m for m in self.messages if m.kind == "image"]

    @property
    def voices(self) -> list[Message]:
        return [m for m in self.messages if m.kind == "voice"]

    def to_json(self, path: Path) -> Path:
        payload = {
            "name": self.name,
            "captured_at": self.captured_at,
            "time_window": self.time_window,
            "coverage": self.coverage,
            "messages": [m.to_dict() for m in self.messages],
        }
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(payload, ensure_ascii=False, indent=1), encoding="utf-8")
        return path

    @classmethod
    def from_json(cls, path: Path) -> "Chat":
        payload = json.loads(Path(path).read_text(encoding="utf-8"))
        chat = cls(name=payload.get("name", ""),
                   captured_at=payload.get("captured_at", ""),
                   time_window=payload.get("time_window", ""),
                   coverage=payload.get("coverage", ""))
        chat.messages = [Message.from_dict(d) for d in payload.get("messages", [])]
        return chat


# ------------------------------------------------------------------ report spec
@dataclass
class ReportSpec:
    """一次报告任务的完整输入。"""
    chat_names: list[str] = field(default_factory=list)  # 待采集聊天（名称）
    time_window: str = ""       # ""=全量；"7d"；"2026-01-01~2026-08-27"
    template: str = "work"      # work / progress / general
    title: str = ""
    org_name: str = ""
    output_dir: Path = Path("./output")
    json_sources: list[Path] = field(default_factory=list)  # 离线 JSON 数据源（测试/复用）

    def resolved_title(self) -> str:
        if self.title:
            return self.title
        names = "、".join(self.chat_names) if self.chat_names else "聊天记录"
        if len(names) > 30:
            names = names[:30] + "…"
        suffix = {"work": "工作情况报告", "progress": "项目进展报告", "general": "综合分析报告"}
        return f"{names}{suffix.get(self.template, '分析报告')}"
