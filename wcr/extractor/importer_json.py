# -*- coding: utf-8 -*-
"""JSON 离线导入器：复用历史采集 / 单元测试 / 数据回放。"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from ..models import Chat
from .base import BaseExtractor


class JsonImporter(BaseExtractor):
    """从 JSON 文件加载 Chat（与 VisualExtractor 输出同构）。"""

    def __init__(self):
        self._loaded: dict[str, Chat] = {}

    def register(self, path: Path, alias: str = "") -> None:
        chat = Chat.from_json(Path(path))
        self._loaded[alias or chat.name] = chat

    def names(self) -> list[str]:
        """已注册的聊天名（JSON 模式下未显式指定 --chats 时使用全部）。"""
        return list(self._loaded)

    def extract(self, chat_name: str, time_window: str = "",
                on_progress: Optional[Callable[[str], None]] = None) -> Chat:
        if chat_name not in self._loaded:
            raise KeyError(f"JSON 数据源未注册聊天「{chat_name}」"
                           f"（已注册：{list(self._loaded)}）")
        say = on_progress or (lambda m: None)
        chat = self._loaded[chat_name]
        say(f"📥 JSON 导入「{chat_name}」：{len(chat.messages)} 条")
        return chat
