# -*- coding: utf-8 -*-
"""微信时间标签解析（纯函数，可单测）。

微信聊天区的时间分隔标签形态：
    14:30 / 14:30:25
    昨天 14:30 / 前天 09:05
    星期二 14:30 / 周三 08:00
    2026年8月5日 / 2026年8月5日 14:30
    8月5日 14:30（同年省略年份）
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from typing import Optional

WEEKDAY_CN = {"一": 0, "二": 1, "三": 2, "四": 3, "五": 4, "六": 5, "日": 6, "天": 6}

_FULL = re.compile(
    r"^(?P<y>\d{4})\s*年\s*(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"
    r"(?:\s*(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?)?\s*$"
)
_MD = re.compile(
    r"^(?P<mo>\d{1,2})\s*月\s*(?P<d>\d{1,2})\s*日"
    r"(?:\s*(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?)?\s*$"
)
_WEEK = re.compile(
    r"^星期(?P<w>[一二三四五六日天])\s*(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*$"
)
_ZHOU = re.compile(
    r"^周(?P<w>[一二三四五六日天])\s*(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*$"
)
_YESTERDAY = re.compile(
    r"^(昨天|前天)\s*(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*$"
)
_CLOCK = re.compile(r"^(?P<h>\d{1,2}):(?P<mi>\d{2})(?::(?P<s>\d{2}))?\s*$")

# 用于"这一屏是不是时间标签"的整体判断
TIME_LABEL_RE = re.compile(
    r"^\s*("
    r"\d{4}\s*年\s*\d{1,2}\s*月\s*\d{1,2}\s*日(\s*\d{1,2}:\d{2}(:\d{2})?)?"
    r"|\d{1,2}\s*月\s*\d{1,2}\s*日(\s*\d{1,2}:\d{2}(:\d{2})?)?"
    r"|昨天\s*\d{1,2}:\d{2}"
    r"|前天\s*\d{1,2}:\d{2}"
    r"|星期[一二三四五六日天]\s*\d{1,2}:\d{2}"
    r"|周[一二三四五六日天]\s*\d{1,2}:\d{2}"
    r"|\d{1,2}:\d{2}(:\d{2})?"
    r")\s*$"
)

# 语音时长标记：12'' / 12" / 60″（OCR 常把两撇读成各种引号，1~2 个引号字符）
VOICE_RE = re.compile(r"""^(\d{1,2})\s*['’”″"]{1,2}\s*$""")


def is_time_label(text: str) -> bool:
    return bool(TIME_LABEL_RE.match(text or ""))


def parse_time_label(text: str, now: Optional[datetime] = None,
                     last_seen: Optional[datetime] = None) -> Optional[datetime]:
    """把时间标签解析为绝对时间。无法解析返回 None。

    now: 参考当前时间（默认系统时间）
    last_seen: 上一条已解析时间（用于 N月N日 无年份时判断年份）
    """
    now = now or datetime.now()
    t = (text or "").strip()

    m = _FULL.match(t)
    if m:
        year = int(m["y"])
        # 年份合理性钳制：OCR 把 "2026年" 误读成 "2005年" 等荒谬年份时
        # （微信 2011 年才诞生），按乱读处理返回 None——否则不仅上滚探测
        # 会假性"到达窗起点"，对应消息还会被盖上远古日期后遭窗过滤丢弃
        if not 2011 <= year <= now.year:
            return None
        try:
            return datetime(year, int(m["mo"]), int(m["d"]),
                            int(m["h"] or 0), int(m["mi"] or 0), int(m["s"] or 0))
        except ValueError:
            return None

    m = _MD.match(t)
    if m:
        mo, d = int(m["mo"]), int(m["d"])
        year = last_seen.year if last_seen else now.year
        try:
            cand = datetime(year, mo, d, int(m["h"] or 0), int(m["mi"] or 0), int(m["s"] or 0))
        except ValueError:
            return None
        # 未到今年该日期 → 可能是去年（跨年边界）
        if cand > now + timedelta(days=1):
            try:
                cand = cand.replace(year=year - 1)
            except ValueError:
                return None   # 2月29日回退到非闰年
        return cand

    m = _WEEK.match(t) or _ZHOU.match(t)
    if m:
        target_wd = WEEKDAY_CN[m["w"]]
        delta = (now.weekday() - target_wd) % 7
        if delta == 0:
            delta = 7  # "星期二"显示时通常指过去的那天
        day = (now - timedelta(days=delta)).date()
        try:
            return datetime(day.year, day.month, day.day,
                            int(m["h"]), int(m["mi"]), int(m["s"] or 0))
        except ValueError:
            return None   # 乱读时刻（如 24:15）按不可解析处理

    m = _YESTERDAY.match(t)
    if m:
        base = now - timedelta(days=(1 if t.startswith("昨天") else 2))
        try:
            return base.replace(hour=int(m["h"]), minute=int(m["mi"]),
                                second=int(m["s"] or 0), microsecond=0)
        except ValueError:
            return None

    m = _CLOCK.match(t)
    if m:
        try:
            cand = now.replace(hour=int(m["h"]), minute=int(m["mi"]),
                               second=int(m["s"] or 0), microsecond=0)
        except ValueError:
            return None   # 正则 \d{1,2} 挡不住 24:41/12:60（OCR 数字翻转）
        if last_seen:
            # 裸时刻出现在日期标签之后：归属该日期；若早得离谱则是跨午夜
            cand = cand.replace(year=last_seen.year, month=last_seen.month,
                                day=last_seen.day)
            if cand < last_seen - timedelta(hours=12):
                cand += timedelta(days=1)
        elif cand > now:
            cand -= timedelta(days=1)  # 今天还没到这个时刻 → 昨天
        return cand

    return None


def parse_window(spec: str, now: Optional[datetime] = None) -> tuple[Optional[datetime], Optional[datetime]]:
    """解析时间窗配置 → (start, end)。None 表示不设界。

    支持：
      ""            → (None, None) 全量
      7d / 30D      → 近 N 天
      2026-01-01~2026-08-27
      2026-01-01~   /  ~2026-08-27
      2026-03       → 整月
    """
    now = now or datetime.now()
    spec = (spec or "").strip()
    if not spec:
        return None, None

    m = re.match(r"^(\d+)\s*[dD天]$", spec)
    if m:
        start = now.replace(hour=0, minute=0, second=0, microsecond=0) - timedelta(days=int(m.group(1)) - 1)
        return start, now

    m = re.match(r"^(\d{4})-(\d{1,2})$", spec)
    if m:
        y, mo = int(m.group(1)), int(m.group(2))
        start = datetime(y, mo, 1)
        ny, nm = (y + 1, 1) if mo == 12 else (y, mo + 1)
        return start, datetime(ny, nm, 1) - timedelta(seconds=1)

    m = re.match(r"^(\d{4}-\d{1,2}-\d{1,2})?\s*~\s*(\d{4}-\d{1,2}-\d{1,2})?$", spec)
    if m and (m.group(1) or m.group(2)):
        start = datetime.strptime(m.group(1), "%Y-%m-%d") if m.group(1) else None
        end = (datetime.strptime(m.group(2), "%Y-%m-%d")
               .replace(hour=23, minute=59, second=59)) if m.group(2) else None
        return start, end

    raise ValueError(
        f"无法解析时间窗配置: {spec!r}（示例：7d / 2026-01-01~2026-08-27 / 2026-03）"
    )
