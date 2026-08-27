# -*- coding: utf-8 -*-
"""聊天记录 Map-Reduce 分析：分块要点提取 → 全局综合成稿。

面向免费模型的预算友好设计：
  - 分块（chunk_chars）逐块提取结构化要点（events/decisions/todos/...）
  - 全局阶段把所有要点 + 统计摘要 交给模型综合
  - 输出为结构化 ReportContent，供 docx 渲染
"""
from __future__ import annotations

import base64
import json
import logging
from pathlib import Path
from typing import Callable, Optional

from ..models import Chat
from .zhipu_client import ZhipuClient, parse_json_loose

log = logging.getLogger("wcr.analyzer")

# ---------------------------------------------------------------- prompts
CHUNK_SYSTEM = """你是资深的企业信息整理专家。你将收到一段微信群/好友聊天记录（OCR而来，可能有少量错字，请智能纠正）。
请从中提取高价值结构化信息，只输出 JSON（不要 markdown 围栏、不要多余文字）：
{
 "events":   [{"date":"YYYY-MM-DD 或原文时间","desc":"发生的事件，一句话"}],
 "decisions":[{"desc":"达成的决定/共识","by":"相关人（可空）"}],
 "todos":    [{"desc":"待办/行动项","owner":"负责人（可空）","due":"期限（可空）"}],
 "viewpoints":[{"who":"发言人","point":"核心观点"}],
 "problems": [{"desc":"暴露的问题/风险"}],
 "facts":    [{"desc":"值得记录的数据/事实（参数、编号、时间节点等）"}]
}
没有某类内容就给空数组。聊天记录可能含闲聊，只保留有价值内容。"""

SYNTH_SYSTEM = """你是公文写作专家，为单位领导撰写专业报告。基于给定的聊天记录要点与统计数据，
输出一份正式报告的 JSON（不要 markdown 围栏）：
{
 "title": "报告标题（不超过 30 字）",
 "summary": "200 字以内的总述（综述本期聊天记录整体情况、核心结论）",
 "sections": [
   {"heading": "一、×××", "paras": ["正文段落……（可多段）"]}
 ],
 "conclusions": ["结论1", "结论2"],
 "suggestions": ["建议1", "建议2"],
 "appendix_note": "大事记说明（可空）"
}
要求：用语正式、条理清晰、聚焦最有价值的内容（成果/问题/风险/行动），
sections 3~6 个，每个 section 1~3 段，段落每段不超过 300 字。"""

TIMELINE_SYSTEM = """你是大事记整理专家。把给定的聊天记录要点按时间整理成"大事记"条目。
只输出 JSON：{"items":[{"date":"YYYY-MM-DD","desc":"关键节点，一句话，含关键人物/参数"}]}
按日期升序，只保留关键节点（普通闲聊不要），最多 60 条。"""

VISION_PROMPT = ("请用一句话（不超过 60 字）描述这张微信聊天图片的核心内容"
                 "（若是截图/照片/图表/文件，说明其主题与关键信息）。")


# ---------------------------------------------------------------- content
class ReportContent:
    """分析结果的结构化形态（与渲染解耦）。"""

    def __init__(self):
        self.title: str = ""
        self.summary: str = ""
        self.sections: list[dict] = []       # {heading, paras}
        self.conclusions: list[str] = []
        self.suggestions: list[str] = []
        self.timeline: list[dict] = []       # {date, desc}
        self.image_captions: list[dict] = []  # {path, caption}
        self.meta: dict = {}

    @classmethod
    def from_json(cls, d: dict) -> "ReportContent":
        c = cls()
        c.title = d.get("title", "")
        c.summary = d.get("summary", "")
        c.sections = d.get("sections", [])
        c.conclusions = d.get("conclusions", [])
        c.suggestions = d.get("suggestions", [])
        c.timeline = d.get("timeline", [])
        c.meta = d.get("meta", {})
        return c

    def to_json(self) -> dict:
        return {
            "title": self.title, "summary": self.summary,
            "sections": self.sections, "conclusions": self.conclusions,
            "suggestions": self.suggestions, "timeline": self.timeline,
            "image_captions": self.image_captions, "meta": self.meta,
        }


# ---------------------------------------------------------------- analyzer
class ChatAnalyzer:
    """多聊天合并分析（Map-Reduce）。"""

    def __init__(self, client: ZhipuClient,
                 chunk_chars: int = 3500,
                 max_chunks: int = 80,
                 vision_images: int = 12,
                 on_progress: Optional[Callable[[str], None]] = None):
        self.client = client
        self.chunk_chars = chunk_chars
        self.max_chunks = max_chunks
        self.vision_images = vision_images
        self.say = on_progress or (lambda m: log.info(m))

    # ------------------------------------------------------------ transcript
    def build_transcript(self, chats: list[Chat]) -> str:
        """把 Chat 列表转成模型友好的文本流（含说话人与时间）。"""
        lines = []
        for chat in chats:
            if len(chats) > 1:
                lines.append(f"===== 聊天来源：{chat.name} =====")
            for m in chat.messages:
                if m.kind == "text":
                    who = m.speaker or ("我方" if m.side == "right" else "")
                    # 优先用已解析的绝对时间（"昨天18:41" 之类原始标签会让
                    # 大事记/事件日期失去锚点），无法解析时才退回原标签
                    ts = (m.timestamp.strftime("%Y-%m-%d %H:%M") if m.timestamp
                          else (m.time_label or ""))
                    prefix = f"[{ts}] {who}：" if who else f"[{ts}] "
                    lines.append(prefix + m.text)
                elif m.kind == "image":
                    cap = m.text if m.text else "（图片）"
                    ts = (m.timestamp.strftime("%Y-%m-%d %H:%M") if m.timestamp
                          else (m.time_label or ""))
                    lines.append(f"[{ts}] [图片] {cap}")
                elif m.kind == "voice":
                    ts = (m.timestamp.strftime("%Y-%m-%d %H:%M") if m.timestamp
                          else (m.time_label or ""))
                    lines.append(f"[{ts}] [语音 {m.text}]")
        return "\n".join(lines)

    # ------------------------------------------------------------ pipeline
    def analyze(self, chats: list[Chat], template: str = "work",
                stats_brief: str = "") -> ReportContent:
        transcript = self.build_transcript(chats)
        total = sum(len(c.messages) for c in chats)
        self.say(f"📊 待分析：{len(chats)} 个聊天 / {total} 条消息 / 转写 {len(transcript)} 字")

        # Map：分块提取
        chunks = self._chunk(transcript)
        if not chunks:
            raise RuntimeError("转写文本为空，无法分析")
        if len(chunks) > self.max_chunks:
            self.say(f"⚠ 分块数 {len(chunks)} 超过上限 {self.max_chunks}，截断"
                     f"（可调大 [ai] max_chunks）")
            chunks = chunks[:self.max_chunks]
        self.say(f"🧩 Map 阶段：{len(chunks)} 个分块")

        all_points = []
        for i, ch in enumerate(chunks):
            self.say(f"   分块 {i + 1}/{len(chunks)}（{len(ch)} 字）…")
            out = self.client.chat(ch, system=CHUNK_SYSTEM)
            data = parse_json_loose(out)
            if "_raw" not in data:
                all_points.append(data)

        merged = self._merge_points(all_points)
        self.say(f"   要点合并：事件 {len(merged.get('events', []))}，决定 "
                 f"{len(merged.get('decisions', []))}，待办 {len(merged.get('todos', []))}，"
                 f"观点 {len(merged.get('viewpoints', []))}，问题 {len(merged.get('problems', []))}，"
                 f"事实 {len(merged.get('facts', []))}")

        # 图片理解（Reduce 前完成，caption 供综合阶段引用）
        captions = self._caption_images(chats)
        if captions:
            self.say(f"🖼 图片理解完成：{len(captions)} 张")

        # Reduce：全局综合
        self.say("🧠 Reduce 阶段：综合成稿 …")
        synth_prompt = self._build_synth_prompt(merged, captions, stats_brief, template, chats)
        out = self.client.chat(synth_prompt, system=SYNTH_SYSTEM, max_tokens=8192)
        content = ReportContent.from_json(parse_json_loose(out))
        if "_raw" in content.meta or not content.sections:
            # 兜底：非结构化输出也保留
            raw = content.meta.get("_raw", "")
            if raw:
                content.summary = content.summary or raw[:600]
            content.sections = content.sections or [
                {"heading": "一、聊天记录要点", "paras": [json.dumps(merged, ensure_ascii=False)[:3000]]}
            ]
        content.image_captions = captions

        # 大事记（独立一次调用，成本可控）
        self.say("📅 生成大事记 …")
        tl_prompt = "以下是聊天记录要点（JSON）：\n" + json.dumps(merged, ensure_ascii=False)[:20000]
        try:
            tl = parse_json_loose(self.client.chat(tl_prompt, system=TIMELINE_SYSTEM))
            content.timeline = tl.get("items", [])
        except Exception as e:
            log.warning("大事记生成失败：%s", e)
        return content

    # ------------------------------------------------------------ helpers
    def _chunk(self, text: str) -> list[str]:
        lines = text.split("\n")
        chunks, buf, size = [], [], 0
        for ln in lines:
            buf.append(ln)
            size += len(ln)
            if size >= self.chunk_chars:
                chunks.append("\n".join(buf))
                buf, size = [], 0
        if buf:
            chunks.append("\n".join(buf))
        return chunks

    def _merge_points(self, points: list[dict]) -> dict:
        merged = {"events": [], "decisions": [], "todos": [],
                  "viewpoints": [], "problems": [], "facts": []}
        for p in points:
            for k in merged:
                merged[k].extend(p.get(k, []) or [])
        # 去重（desc 简单规范化）
        for k, items in merged.items():
            seen = set()
            out = []
            for it in items:
                key = "".join(sorted(set(str(it.get("desc", it)))))[:80]
                if key not in seen:
                    seen.add(key)
                    out.append(it)
            merged[k] = out
        return merged

    def _caption_images(self, chats: list[Chat]) -> list[dict]:
        """对前 N 张图片调用视觉模型生成说明。"""
        if self.vision_images <= 0:
            return []
        imgs = []
        for chat in chats:
            imgs.extend(chat.images)
        imgs = imgs[:self.vision_images]
        captions = []
        for i, m in enumerate(imgs):
            p = Path(m.img_path)
            if not p.exists():
                continue
            try:
                b64 = base64.b64encode(p.read_bytes()).decode("ascii")
                cap = self.client.vision(VISION_PROMPT, b64)
                m.text = cap  # 回填，参与后续分析
                captions.append({"path": str(p), "caption": cap})
                self.say(f"   图 {i + 1}/{len(imgs)}：{cap[:50]}")
            except Exception as e:
                log.warning("图片 %s 理解失败：%s", p.name, e)
        return captions

    def _build_synth_prompt(self, merged: dict, captions: list[dict],
                            stats_brief: str, template: str, chats: list[Chat]) -> str:
        tmpl_hint = {
            "work": "这是一份工作情况报告：聚焦工作进展、成果、问题与下一步安排。",
            "progress": "这是一份项目进展报告：聚焦里程碑、完成情况、风险与对策。",
            "general": "这是一份综合分析报告：全面梳理有价值的内容、观点与结论。",
        }[template]
        parts = [
            f"报告类型要求：{tmpl_hint}",
            f"数据来源：{len(chats)} 个聊天（{('、'.join(c.name for c in chats))[:80]}）",
        ]
        if stats_brief:
            parts.append(f"统计数据摘要：\n{stats_brief}")
        if captions:
            parts.append("聊天图片内容：\n" + "\n".join(
                f"- {c['caption']}" for c in captions))
        parts.append("分块提取的要点（JSON）：\n" + json.dumps(merged, ensure_ascii=False)[:60000])
        return "\n\n".join(parts)
