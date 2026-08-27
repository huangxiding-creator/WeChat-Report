# -*- coding: utf-8 -*-
"""INI 配置管理。

设计原则：
  * 一切与用户需求相关的参数都进 config.ini，最大化脚本灵活性；
  * 默认值内置（本文件），INI 缺项自动回落默认值；
  * 密钥（智谱 API Key、企业微信 Webhook）支持环境变量注入：
      WCR_ZHIPU_API_KEY / WCR_WECOM_WEBHOOK
  * Config.save() 写回时会保留注释段说明。

用法：
    cfg = Config.load()          # 读取项目根 config.ini
    cfg.get("extract", "scroll_pause", cast=float)
    cfg.set("extract", "scroll_pause", 0.5); cfg.save()
"""
from __future__ import annotations

import configparser
import os
from pathlib import Path
from typing import Optional

PROJECT_ROOT = Path(__file__).resolve().parent.parent
CONFIG_PATH = PROJECT_ROOT / "config.ini"
EXAMPLE_PATH = PROJECT_ROOT / "config.example.ini"

# ---------------------------------------------------------------- defaults
DEFAULTS: dict[str, dict[str, str]] = {
    "app": {
        "name": "WeChat-Report",
        "version": "1.0.0",
        "language": "zh",
        "theme": "dark-blue",
        "appearance": "dark",
    },
    # ------- 提取（视觉路线） -------
    "extract": {
        # 导航模式：auto=受控点击搜索框自动切换会话；manual=倒计时等待人工打开
        "navigate_mode": "auto",
        "manual_countdown": "10",
        # 会话切换后的稳定等待（秒）
        "settle_wait": "1.5",
        # 时间窗：留空=全量；支持 N天（如 7d / 30d）或 YYYY-MM-DD~YYYY-MM-DD
        "time_window": "",
        # 滚动
        "scroll_to_top_attempts": "800",
        "scroll_pause": "0.35",
        "stable_frames_to_stop": "3",
        "scrollup_time_budget": "300",
        "scroll_step": "15",
        "capture_overlap": "80",
        # 屏数安全上限
        "max_screens": "3000",
        # 断点续采
        "checkpoint_enabled": "true",
        # 输出
        "output_dir": "./output",
        "keep_screenshots": "true",
    },
    # ------- OCR -------
    "ocr": {
        "score_threshold": "0.5",
        # 参与者识别：是否按气泡 x 位置推断"我/对方"
        "speaker_attribution": "true",
    },
    # ------- 图片气泡检测 -------
    "bubbles": {
        "image_min_w": "90",
        "image_min_h": "90",
        "image_min_std": "20.0",
        "image_iou_merge": "0.3",
    },
    # ------- AI（智谱免费模型） -------
    "ai": {
        "api_key": "",
        "base_url": "https://open.bigmodel.cn/api/paas/v4",
        # 文本模型轮换池（全部免费）
        "text_models": "glm-4-flash-250414,glm-4.5-flash",
        # 视觉模型（免费）
        "vision_model": "glm-4v-flash",
        "max_tokens": "4096",
        "temperature": "0.3",
        # Map-Reduce 分块大小（字符）
        "chunk_chars": "3500",
        "max_chunks": "80",
        # 图片理解：每聊天最多送审图片数（0=关闭）
        "vision_images": "12",
        # 限流退避
        "retry_times": "4",
        "retry_backoff": "2.0",
        "request_timeout": "120",
        # glm-4.5+ 混合推理模型：true=关闭深度思考（报告任务更稳）
        "disable_thinking": "true",
    },
    # ------- 报告 -------
    "report": {
        # 模板：work=工作汇报 / progress=项目进展 / general=通用总结
        "template": "work",
        "title_prefix": "",
        "org_name": "",
        # 统计
        "top_keywords": "15",
        "top_speakers": "10",
    },
    # ------- 企业微信通知 -------
    "notify": {
        "enabled": "true",
        "webhook": "",
        # 里程碑通知：start/extract/analyze/report/done/all
        "milestones": "start,extract,analyze,report,done",
        "send_report_file": "true",
    },
    # ------- 只读安全（硬约束，一般不动） -------
    "safety": {
        "input_zone_ratio": "0.80",
        "allow_enter_key": "false",
        "allow_right_click": "false",
    },
}

_ENV_MAP = {
    ("ai", "api_key"): "WCR_ZHIPU_API_KEY",
    ("notify", "webhook"): "WCR_WECOM_WEBHOOK",
}


class Config:
    """config.ini 的薄封装：get/set + 环境变量注入 + 保存。"""

    def __init__(self, path: Optional[Path] = None):
        self.path = Path(path) if path else CONFIG_PATH
        self._cp = configparser.ConfigParser(interpolation=None)
        # 先铺默认骨架
        for sec, kv in DEFAULTS.items():
            self._cp.add_section(sec)
            for k, v in kv.items():
                self._cp.set(sec, k, v)
        self.dirty = False

    # ------------------------------------------------------------ load/save
    @classmethod
    def load(cls, path: Optional[Path] = None) -> "Config":
        cfg = cls(path)
        if cfg.path.exists():
            try:
                # utf-8-sig 同时兼容带 BOM（记事本编辑）与不带 BOM 的文件
                cfg._cp.read(cfg.path, encoding="utf-8-sig")
            except (UnicodeDecodeError, configparser.Error):
                # 损坏的 INI 不致命：回落默认值
                pass
        cfg._apply_env()
        return cfg

    def save(self, path: Optional[Path] = None) -> Path:
        p = Path(path) if path else self.path
        with open(p, "w", encoding="utf-8-sig") as f:  # BOM 便于 Windows 记事本编辑
            f.write("; WeChat-Report 配置文件（自动生成，可直接编辑）\n")
            self._cp.write(f)
        return p

    # ------------------------------------------------------------ accessors
    def get(self, section: str, key: str, default: Optional[str] = None) -> str:
        try:
            val = self._cp.get(section, key)
            return val if val != "" else (default if default is not None else "")
        except (configparser.NoSectionError, configparser.NoOptionError):
            return default if default is not None else ""

    def get_bool(self, section: str, key: str, default: bool = False) -> bool:
        v = self.get(section, key)
        if v == "":
            return default
        return v.strip().lower() in ("1", "true", "yes", "on")

    def get_int(self, section: str, key: str, default: int = 0) -> int:
        try:
            return int(float(self.get(section, key)))
        except ValueError:
            return default

    def get_float(self, section: str, key: str, default: float = 0.0) -> float:
        try:
            return float(self.get(section, key))
        except ValueError:
            return default

    def set(self, section: str, key: str, value) -> None:
        if not self._cp.has_section(section):
            self._cp.add_section(section)
        self._cp.set(section, key, str(value))
        self.dirty = True

    # ------------------------------------------------------------ helpers
    def _apply_env(self) -> None:
        """密钥优先从环境变量注入，便于 CI/无盘配置。"""
        for (sec, key), env in _ENV_MAP.items():
            v = os.environ.get(env, "")
            if v:
                self.set(sec, key, v)

    @property
    def api_key(self) -> str:
        return self.get("ai", "api_key")

    @property
    def webhook(self) -> str:
        return self.get("notify", "webhook")

    @property
    def output_dir(self) -> Path:
        p = Path(self.get("extract", "output_dir", "./output"))
        return p if p.is_absolute() else (PROJECT_ROOT / p)

    def milestone_enabled(self, name: str) -> bool:
        ms = self.get("notify", "milestones", "all").lower()
        return ms in ("all",) or name in {x.strip() for x in ms.split(",")}

    def text_models(self) -> list[str]:
        raw = self.get("ai", "text_models",
                       DEFAULTS["ai"]["text_models"])
        return [m.strip() for m in raw.split(",") if m.strip()]

    def write_example(self, path: Optional[Path] = None) -> Path:
        """生成脱敏的 config.example.ini（密钥留空）。"""
        p = Path(path) if path else EXAMPLE_PATH
        backup = Config.__new__(Config)
        backup.__dict__.update(self.__dict__)
        backup._cp = configparser.ConfigParser(interpolation=None)
        for sec, kv in DEFAULTS.items():
            backup._cp.add_section(sec)
            for k, v in kv.items():
                # 密钥类默认值在示例文件里一律留空
                backup._cp.set(sec, k, "" if k in ("api_key", "webhook") else v)
        with open(p, "w", encoding="utf-8-sig") as f:
            f.write("; WeChat-Report 示例配置 — 复制为 config.ini 后填写自己的密钥\n")
            backup._cp.write(f)
        return p
