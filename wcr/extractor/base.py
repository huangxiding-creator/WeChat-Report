# -*- coding: utf-8 -*-
"""提取器抽象接口。"""
from __future__ import annotations

import abc
from typing import Callable, Optional

from ..models import Chat


class BaseExtractor(abc.ABC):
    """所有提取器的公共契约。

    提取层与下游（AI 分析、报告）完全解耦：
    任何能把聊天记录变成 Chat 对象的手段（视觉 / JSON / 未来数据库插件）
    都可以插进来。
    """

    @abc.abstractmethod
    def extract(self, chat_name: str, time_window: str = "",
                on_progress: Optional[Callable[[str], None]] = None) -> Chat:
        """采集指定聊天，返回 Chat 对象。"""

    def extract_many(self, chat_names: list[str], time_window: str = "",
                     on_progress: Optional[Callable[[str], None]] = None) -> list[Chat]:
        return [self.extract(name, time_window, on_progress) for name in chat_names]
