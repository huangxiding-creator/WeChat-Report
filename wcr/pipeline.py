# -*- coding: utf-8 -*-
"""全流程编排：提取 → 统计 → AI 分析 → Word 报告 → 企业微信通知。

run(spec, cfg, on_progress) 是唯一入口，GUI / CLI 都调它。
关键节点通过企业微信推送；报告文件完成后直接推送。
"""
from __future__ import annotations

import logging
import re
import traceback
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .models import ReportSpec

log = logging.getLogger("wcr.pipeline")


@dataclass
class RunResult:
    success: bool = False
    chats_done: int = 0
    n_messages: int = 0
    report_docx: str = ""
    ai_stats: str = ""
    error: str = ""
    log: list = field(default_factory=list)


def _fmt_dt(dt) -> str:
    return dt.strftime("%Y-%m-%d %H:%M:%S") if dt else "—"


def make_visual_extractor(cfg: Config, output_dir: Path,
                          scrollup_mode: str = "probe"):
    """按 INI 配置构建视觉提取器（pipeline / batch 共用）。"""
    from .extractor.visual import VisualExtractor
    return VisualExtractor(
        navigate_mode=cfg.get("extract", "navigate_mode", "auto"),
        manual_countdown=cfg.get_int("extract", "manual_countdown", 10),
        settle_wait=cfg.get_float("extract", "settle_wait", 1.5),
        scroll_pause=cfg.get_float("extract", "scroll_pause", 0.35),
        scroll_step=cfg.get_int("extract", "scroll_step", 15),
        scroll_attempts=cfg.get_int("extract", "scroll_to_top_attempts", 800),
        stable_frames=cfg.get_int("extract", "stable_frames_to_stop", 3),
        scrollup_time_budget=cfg.get_int("extract", "scrollup_time_budget", 300),
        scrollup_mode=scrollup_mode,
        max_screens=cfg.get_int("extract", "max_screens", 3000),
        ocr_threshold=cfg.get_float("ocr", "score_threshold", 0.5),
        speaker_attribution=cfg.get_bool("ocr", "speaker_attribution", True),
        checkpoint_enabled=cfg.get_bool("extract", "checkpoint_enabled", True),
        keep_screenshots=cfg.get_bool("extract", "keep_screenshots", True),
        output_dir=output_dir,
        image_min_w=cfg.get_int("bubbles", "image_min_w", 90),
        image_min_h=cfg.get_int("bubbles", "image_min_h", 90),
        image_min_std=cfg.get_float("bubbles", "image_min_std", 20.0),
        image_iou_merge=cfg.get_float("bubbles", "image_iou_merge", 0.3),
        input_zone_ratio=cfg.get_float("safety", "input_zone_ratio", 0.80),
    )


def run(spec: ReportSpec, cfg: Config,
        on_progress: Optional[Callable[[str], None]] = None) -> RunResult:
    res = RunResult()

    def say(msg: str):
        res.log.append(msg)
        if on_progress:
            on_progress(msg)

    def notify(text: str, milestone: str = ""):
        if not cfg.get_bool("notify", "enabled"):
            return
        if milestone and not cfg.milestone_enabled(milestone):
            return
        try:
            from .notify.wecom import WeComNotifier
            WeComNotifier(cfg.webhook).send_text(text)
        except Exception as e:
            say(f"⚠ 企业微信通知失败：{e}")

    try:
        started = datetime.now()
        # -------------------------------------------------- extractor
        if spec.json_sources:
            from .extractor.importer_json import JsonImporter
            ext = JsonImporter()
            for p in spec.json_sources:
                ext.register(Path(p))
            say(f"📥 JSON 数据源：{len(spec.json_sources)} 个")
            if not spec.chat_names:
                spec.chat_names = list(ext.names())
                say(f"   JSON 源聊天：{'、'.join(spec.chat_names)}")
        else:
            from .extractor.visual import VisualExtractor
            ext = VisualExtractor(
                navigate_mode=cfg.get("extract", "navigate_mode", "auto"),
                manual_countdown=cfg.get_int("extract", "manual_countdown", 10),
                settle_wait=cfg.get_float("extract", "settle_wait", 1.5),
                scroll_pause=cfg.get_float("extract", "scroll_pause", 0.35),
                scroll_step=cfg.get_int("extract", "scroll_step", 15),
                scroll_attempts=cfg.get_int("extract", "scroll_to_top_attempts", 800),
                stable_frames=cfg.get_int("extract", "stable_frames_to_stop", 3),
                scrollup_time_budget=cfg.get_int("extract", "scrollup_time_budget", 300),
                max_screens=cfg.get_int("extract", "max_screens", 3000),
                ocr_threshold=cfg.get_float("ocr", "score_threshold", 0.5),
                speaker_attribution=cfg.get_bool("ocr", "speaker_attribution", True),
                checkpoint_enabled=cfg.get_bool("extract", "checkpoint_enabled", True),
                keep_screenshots=cfg.get_bool("extract", "keep_screenshots", True),
                output_dir=spec.output_dir,
                image_min_w=cfg.get_int("bubbles", "image_min_w", 90),
                image_min_h=cfg.get_int("bubbles", "image_min_h", 90),
                image_min_std=cfg.get_float("bubbles", "image_min_std", 20.0),
                image_iou_merge=cfg.get_float("bubbles", "image_iou_merge", 0.3),
                input_zone_ratio=cfg.get_float("safety", "input_zone_ratio", 0.80),
            )
        say("｜" * 12)
        say(f"🚀 任务启动：{spec.resolved_title()}")
        say(f"   聊天：{('、'.join(spec.chat_names)) or '（JSON 源）'}")
        say(f"   时间窗：{spec.time_window or '全量'} ｜ 模板：{spec.template}")
        notify(f"【WeChat-Report】任务启动\n目标：{spec.resolved_title()}\n"
               f"聊天：{('、'.join(spec.chat_names)) or '（JSON 源）'}\n"
               f"时间窗：{spec.time_window or '全量'}", "start")

        # -------------------------------------------------- extract
        chats = []
        for name in spec.chat_names:
            say(f"───── 采集「{name}」 ─────")
            chat = ext.extract(name, spec.time_window, say)
            chats.append(chat)
            res.chats_done += 1
            res.n_messages += len(chat.messages)
        if not chats:
            raise RuntimeError("没有可用的聊天数据（未指定聊天名或 JSON 源）")
        notify(f"【WeChat-Report】提取完成\n"
               f"聊天 {res.chats_done} 个 / 消息 {res.n_messages} 条\n"
               f"耗时 {int((datetime.now() - started).total_seconds())}s", "extract")

        # -------------------------------------------------- stats
        from .report.stats import compute_stats, stats_brief
        stats = compute_stats(
            chats,
            top_keywords=cfg.get_int("report", "top_keywords", 15),
            top_speakers=cfg.get_int("report", "top_speakers", 10),
        )
        say("📊 统计：")
        for ln in stats_brief(stats).split("\n"):
            say("   " + ln)

        # -------------------------------------------------- AI
        from .ai.analyzer import ChatAnalyzer
        from .ai.zhipu_client import ZhipuClient
        client = ZhipuClient(
            api_key=cfg.api_key,
            base_url=cfg.get("ai", "base_url", "https://open.bigmodel.cn/api/paas/v4"),
            text_models=cfg.text_models(),
            vision_model=cfg.get("ai", "vision_model", "glm-4v-flash"),
            max_tokens=cfg.get_int("ai", "max_tokens", 4096),
            temperature=cfg.get_float("ai", "temperature", 0.3),
            retry_times=cfg.get_int("ai", "retry_times", 4),
            retry_backoff=cfg.get_float("ai", "retry_backoff", 2.0),
            request_timeout=cfg.get_int("ai", "request_timeout", 120),
            disable_thinking=cfg.get_bool("ai", "disable_thinking", True),
        )
        analyzer = ChatAnalyzer(
            client,
            chunk_chars=cfg.get_int("ai", "chunk_chars", 3500),
            max_chunks=cfg.get_int("ai", "max_chunks", 80),
            vision_images=cfg.get_int("ai", "vision_images", 12),
            on_progress=say,
        )
        content = analyzer.analyze(chats, template=spec.template,
                                   stats_brief=stats_brief(stats))
        res.ai_stats = client.report_stats()
        say(f"🤖 模型用量：{res.ai_stats}")
        notify(f"【WeChat-Report】AI 分析完成\n{res.ai_stats}", "analyze")

        # -------------------------------------------------- docx
        from .report.docx_builder import DocxBuilder
        builder = DocxBuilder(
            org_name=spec.org_name or cfg.get("report", "org_name", ""),
            title_prefix=cfg.get("report", "title_prefix", ""),
        )
        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
        safe_title = re.sub(r'[\\/:*?"<>|\r\n]+', "_", spec.resolved_title()).strip("_ ")[:80] or "报告"
        out = spec.output_dir / f"{stamp}_{safe_title}.docx"
        out = builder.build(content, stats, [c.name for c in chats], out)
        res.report_docx = str(out)
        kb = out.stat().st_size / 1024
        say(f"💾 报告已生成：{out.name}（{kb:.0f} KB）")
        notify(f"【WeChat-Report】报告生成完成\n{out.name}（{kb:.0f} KB）", "report")

        # -------------------------------------------------- push file
        if cfg.get_bool("notify", "send_report_file", True):
            try:
                from .notify.wecom import WeComNotifier
                WeComNotifier(cfg.webhook).send_file(out)
                say("📤 报告文件已推送至企业微信")
            except Exception as e:
                say(f"⚠ 报告文件推送失败：{e}")

        res.success = True
        total_s = int((datetime.now() - started).total_seconds())
        say(f"🎉 全部完成（总耗时 {total_s}s）")
        notify(f"【WeChat-Report】🎉 任务全部完成\n"
               f"报告：{out.name}\n消息：{res.n_messages} 条\n"
               f"总耗时：{total_s}s", "done")
        return res

    except Exception as e:
        res.error = str(e)
        say(f"❌ 失败：{e}")
        say(traceback.format_exc(limit=3))
        notify(f"【WeChat-Report】❌ 任务失败\n{e}", "done")
        return res
