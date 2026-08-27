# -*- coding: utf-8 -*-
"""图片理解辅助（独立入口，便于单独调用与测试）。"""
from __future__ import annotations

import base64
from pathlib import Path
from typing import Callable, Optional

from .zhipu_client import ZhipuClient


def caption_image(client: ZhipuClient, image_path: Path,
                  prompt: str = "请用一句话描述这张图片的核心内容。",
                  on_progress: Optional[Callable[[str], None]] = None) -> str:
    """单图说明。"""
    if not image_path.exists():
        raise FileNotFoundError(image_path)
    b64 = base64.b64encode(image_path.read_bytes()).decode("ascii")
    return client.vision(prompt, b64, on_progress=on_progress)
