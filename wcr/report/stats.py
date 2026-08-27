# -*- coding: utf-8 -*-
"""聊天记录统计（本地计算，零 AI 成本）：消息量/参与者/时间分布/关键词。"""
from __future__ import annotations

import re
from collections import Counter
from datetime import datetime
from typing import Optional

from ..models import Chat, Message

# 高频虚词/助词（粗粒度停用表，够用即可）
_STOPWORDS = set("""的 了 是 在 我 你 他 她 它 我们 你们 他们 好 很 也 都 和 与 及 或 有 没 不 这 那 就 还 要
会 可以 现在 今天 明天 昨天 一个 一些 什么 怎么 请 谢谢 没事 嗯 哦 哈 呵 啊 吧 吗 呢 啦 呀
的话 如果 但是 然后 所以 因为 而且 或者 这个 那个 上 下 里 中 到 去 来 说 看 做 用 对 从 被 把
时间 时候 地方 问题 东西 事情 情况 直接 知道 觉得 可能 应该 需要 开始 继续 已经 还是 就是
不是 没有 这个 那个 图片 语音 发送 收到 老板 老师 领导""".split())


def compute_stats(chats: list[Chat],
                  top_keywords: int = 15,
                  top_speakers: int = 10) -> dict:
    """返回报告用统计数据 dict（同时生成可读 brief）。"""
    msgs = [m for c in chats for m in c.messages]
    stats: dict = {
        "chats": len(chats),
        "chat_names": [c.name for c in chats],
        "total": len(msgs),
        "by_kind": Counter(m.kind for m in msgs),
        "by_chat": {c.name: len(c.messages) for c in chats},
    }

    # 时间范围
    ts = [m.timestamp for m in msgs if m.timestamp]
    if ts:
        stats["time_start"] = min(ts)
        stats["time_end"] = max(ts)
    else:
        stats["time_start"] = stats["time_end"] = None

    # 参与者（启发式 speaker；右侧=我方）
    speakers: Counter = Counter()
    for m in msgs:
        if m.kind not in ("text", "voice", "image"):
            continue
        who = m.speaker or ("我方" if m.side == "right" else "其他")
        speakers[who] += 1
    stats["top_speakers"] = speakers.most_common(top_speakers)

    # 按日分布
    by_day: Counter = Counter()
    for m in msgs:
        if m.timestamp:
            by_day[m.timestamp.strftime("%Y-%m-%d")] += 1
    stats["by_day"] = sorted(by_day.items())
    stats["active_days"] = len(by_day)
    if by_day:
        stats["peak_day"] = by_day.most_common(1)[0]

    # 按小时分布
    by_hour: Counter = Counter()
    for m in msgs:
        if m.timestamp:
            by_hour[m.timestamp.hour] += 1
    stats["by_hour"] = sorted(by_hour.items())

    # 关键词（CJK bigram + ASCII 词）
    text_all = "".join(m.text for m in msgs if m.kind == "text")
    stats["keywords"] = extract_keywords(text_all, top_keywords)
    return stats


def extract_keywords(text: str, top: int = 15) -> list[tuple[str, int]]:
    """简单 CJK 二元组词频（无外部分词依赖）。"""
    text = re.sub(r"[0-9a-zA-Z\s\W]+", lambda m: " " if m.group(0).isascii() else "，", text)
    cjk_segments = re.findall(r"[一-鿿]+", text)
    counter: Counter = Counter()
    for seg in cjk_segments:
        for i in range(len(seg) - 1):
            bg = seg[i:i + 2]
            if bg[0] in _STOPWORDS or bg[1] in _STOPWORDS:
                continue
            if len(bg) == 2 and re.match(r"^[一-鿿]{2}$", bg):
                counter[bg] += 1
    # 英文/数字词
    for w in re.findall(r"[A-Za-z][A-Za-z0-9_-]{2,}", text):
        counter[w.lower()] += 1
    return [(w, n) for w, n in counter.most_common(top) if n >= 3]


def stats_brief(stats: dict) -> str:
    """给 AI 综合阶段的统计摘要文本。"""
    lines = []
    if stats.get("time_start") and stats.get("time_end"):
        lines.append(f"时间范围：{stats['time_start']:%Y-%m-%d} ~ {stats['time_end']:%Y-%m-%d}"
                     f"（活跃 {stats.get('active_days', 0)} 天）")
    bk = stats.get("by_kind", {})
    lines.append(f"消息总量：{stats.get('total', 0)} 条"
                 f"（文字 {bk.get('text', 0)} / 图片 {bk.get('image', 0)} / 语音 {bk.get('voice', 0)}）")
    if len(stats.get("chat_names", [])) > 1:
        lines.append("各聊天分布：" + "，".join(f"{k} {v} 条" for k, v in stats.get("by_chat", {}).items()))
    if stats.get("top_speakers"):
        lines.append("活跃参与者：" + "，".join(f"{w} {n} 条" for w, n in stats["top_speakers"][:8]))
    if stats.get("peak_day"):
        d, n = stats["peak_day"]
        lines.append(f"消息峰值日：{d}（{n} 条）")
    if stats.get("keywords"):
        lines.append("高频关键词：" + "、".join(w for w, _ in stats["keywords"]))
    return "\n".join(lines)
