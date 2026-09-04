# -*- coding: utf-8 -*-
"""批量导出器：枚举全部会话 → 逐个采集 → 每聊天一个独立聊天记录 Word。

用途（对应用户需求）：把 PC 微信里所有私聊/群聊在时间窗内的聊天记录
分别整理成单独的 word 文件（无 AI、零成本、断点可续、企业微信汇报）。

只读保证：全程滚轮 + 会话列表受护栏左键（SafetyGuard），零键盘零右键。
"""
from __future__ import annotations

import json
import logging
import time
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Callable, Optional

from .config import Config
from .extractor.navigator import WeChatNavigator
from .extractor.safety import SafetyGuard
from .extractor.session_enum import DEFAULT_SKIP, SessionEnumerator
from .extractor.window import WeChatWindow

log = logging.getLogger("wcr.batch")


@dataclass
class BatchResult:
    total: int = 0
    done: int = 0
    failed: int = 0
    skipped: int = 0
    messages: int = 0
    docx_files: list[str] = field(default_factory=list)
    errors: dict[str, str] = field(default_factory=dict)


class BatchExporter:
    def __init__(self, cfg: Config,
                 on_progress: Optional[Callable[[str], None]] = None):
        self.cfg = cfg
        self.say = on_progress or (lambda m: log.info(m))

    # ------------------------------------------------------------ entry
    def run(self, out_dir: Path, time_window: str = "365d",
            limit: int = 0, resume: bool = True,
            retry_failed: bool = True) -> BatchResult:
        res = BatchResult()
        out_dir.mkdir(parents=True, exist_ok=True)
        progress_path = out_dir / "_batch_progress.json"
        progress = self._load_progress(progress_path) if resume else {}
        if retry_failed:
            # 上次失败的（error 项）本次重试一次；重跑原命令即可，避免反复撞墙
            failed = [k for k, v in progress.items() if "error" in v]
            for k in failed:
                progress.pop(k)

        from .pipeline import make_visual_extractor
        # probe 模式：上滚逐屏 OCR 探测，见到早于窗起点的标签即停；文本集
        # 连续稳定 = 到缓存顶（GIF 动图免疫）；900s 预算兜底。top 模式实测
        # 会在缓存顶转轮上 800 档空转 10 分钟（懒加载转轮让帧差永不相等）
        ext = make_visual_extractor(self.cfg, out_dir, scrollup_mode="probe")
        max_images = self.cfg.get_int("batch", "max_images_embed", 50)
        report_every = self.cfg.get_int("batch", "report_every", 5)
        skip_names = self._skip_names()

        # 1. 枚举会话
        win = WeChatWindow().find()
        guard = SafetyGuard(input_zone_ratio=self.cfg.get_float(
            "safety", "input_zone_ratio", 0.80))
        enumerator = SessionEnumerator(
            win, guard, on_progress=self.say,
            max_rounds=self.cfg.get_int("batch", "enum_rounds", 160))
        names = enumerator.enumerate(skip=skip_names)
        # 采戳自诊断：戳字典大小 + 年份戳样本（预过滤覆盖率异常时定位用）
        st_n = len(enumerator.stamps)
        st_year = sum(1 for v in enumerator.stamps.values()
                      if any(y in v for y in ("2024", "2025", "2026", "2O24", "2U24")))
        self.say(f"   采戳：{st_n} 名有时间戳（其中年份戳 {st_year}）")
        deduped = dedupe_names(names)
        if len(deduped) != len(names):
            self.say(f"   枚举去重：{len(names)} → {len(deduped)}（OCR 变体合并）")
        kept = self._prefilter_window(deduped, enumerator.stamps, time_window)
        n_all = len(deduped)   # 预过滤前的全列表长度：滚动/扫描预算按真实高度
        names = kept
        if limit:
            names = names[:limit]
        res.total = len(names)
        # 枚举后列表停在底部：回顶一次，后续按名单顺序自上而下扫最省时
        # （400+ 项列表高 ~26,000px，必须按 expected_items 放大回顶预算，
        #   默认 60 轮只滚 ~9,000px 会停在中段；注意预算用 n_all 而非
        #   预过滤后的数量——窗内 100 项 × 65px 远小于列表真实高度）
        from .extractor.navigator import scroll_session_list_to_top
        scroll_session_list_to_top(win, guard, expected_items=n_all)
        self.say(f"🎯 批量导出：{res.total} 个聊天，时间窗 {time_window}")
        self._notify(f"【WeChat-Report 批量导出】启动\n目标聊天：{res.total} 个"
                     f"\n时间窗：{time_window}\n已完成（断点）：{len(progress)} 个")

        # 2. 逐个导出
        t0 = time.monotonic()
        for i, name in enumerate(names, 1):
            if name in progress:
                res.skipped += 1
                self.say(f"⏭ [{i}/{res.total}] 「{name}」已完成（断点跳过）")
                continue
            self.say(f"\n───── [{i}/{res.total}] 「{name}」 ─────")
            try:
                # 导航：滚动列表查找并打开（已有 open+verify 双重确认）
                nav = WeChatNavigator(win, guard,
                                      settle_wait=self.cfg.get_float(
                                          "extract", "settle_wait", 1.5),
                                      input_zone_ratio=guard.input_zone_ratio)
                # start_from_current：名单按列表顺序排列，从当前位置续扫省时；
                # expected_items：按列表总长放大扫描轮数（活跃账号 200+ 项）
                if not nav.open_chat(name, on_progress=self.say,
                                     expected_items=n_all,
                                     start_from_current=True):
                    raise RuntimeError("会话列表中未找到（或点击验证失败）")
                # 采集（already_open：跳过 extract 内部导航）
                chat = ext.extract(name, time_window, self.say,
                                   already_open=True)
                # 整理成独立 word（无 AI）
                from .report.transcript_docx import build_transcript_docx
                stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                docx_path = out_dir / f"{_safe(name)}_聊天记录_{stamp}.docx"
                build_transcript_docx(chat, docx_path,
                                      time_window=time_window,
                                      max_images=max_images)
                kb = docx_path.stat().st_size / 1024
                self.say(f"💾 「{name}」完成：{len(chat.messages)} 条 → "
                         f"{docx_path.name}（{kb:.0f} KB）")
                res.done += 1
                res.messages += len(chat.messages)
                res.docx_files.append(str(docx_path))
                progress[name] = {"messages": len(chat.messages),
                                  "docx": str(docx_path),
                                  "at": datetime.now().isoformat(timespec="seconds")}
                self._save_progress(progress_path, progress)
                if res.done % report_every == 0:
                    el = int(time.monotonic() - t0)
                    avg = el // max(res.done, 1)
                    eta = avg * (res.total - i)
                    self._notify(f"【批量导出进度】{i}/{res.total}\n"
                                 f"已完成 {res.done} · 失败 {res.failed} · "
                                 f"累计 {res.messages} 条\n"
                                 f"平均 {avg}s/个 · 预计剩余 ≈ {eta // 60} 分钟")
            except Exception as e:
                res.failed += 1
                res.errors[name] = str(e)[:200]
                self.say(f"   ✗ 「{name}」失败：{e}")
                log.warning("导出 %s 失败：%s", name, e, exc_info=True)
                # 失败也记录，避免断点重跑时反复撞墙
                progress[name] = {"error": str(e)[:200],
                                  "at": datetime.now().isoformat(timespec="seconds")}
                self._save_progress(progress_path, progress)
            # 每个聊天之间稍歇，降低微信渲染压力
            time.sleep(0.5)

        # 3. 汇总
        el = int(time.monotonic() - t0)
        self.say(f"\n🎉 批量导出结束：成功 {res.done} / 失败 {res.failed} / "
                 f"跳过(已导) {res.skipped} · 共 {res.messages} 条 · {el // 60} 分钟")
        self._notify(f"【WeChat-Report 批量导出】{'🎉 完成' if not res.failed else '⚠ 结束'}\n"
                     f"成功：{res.done} 个 / 失败：{res.failed} 个\n"
                     f"累计消息：{res.messages} 条\n总耗时：{el // 60} 分钟\n"
                     f"输出目录：{out_dir}")
        return res

    # ------------------------------------------------------------ helpers
    def _prefilter_window(self, names: list[str], stamps: dict[str, str],
                          time_window: str) -> list[str]:
        """按列表时间戳预过滤：戳明确早于窗起点的聊天直接跳过。

        列表按最近活跃排序，右列年份戳（如 2024/07/23）= 最后一条消息
        时间——早于窗起点意味着导出必为 0 条（试点 3 两个 2024/10 聊天
        实证）。无戳/戳无法判定 → 保守保留（采集阶段再兜底）。
        典型收益：本账号 ~600 项中 ~2024/* 戳的占多数，全量耗时大降。

        戳按 OCR 读数键控；去重后的目标名可能是另一读数（更长变体），
        先经 name_variant 簇匹配把戳关联到目标名，再判窗。
        """
        from datetime import datetime
        from .extractor.navigator import name_variant
        from .extractor.session_enum import stamp_older_than
        from .extractor.timelabels import parse_window
        start_dt, _ = parse_window(time_window, now=datetime.now())
        if start_dt is None or not stamps:
            return names
        cutoff = start_dt.date()

        # 目标名 → 簇内任一读数的戳（精确键优先，变体兜底）
        stamp_keys = list(stamps.keys())
        resolved: dict[str, str] = {}

        def _stamp_for(name: str) -> str:
            if name in resolved:
                return resolved[name]
            st = stamps.get(name, "")
            if not st:
                for k in stamp_keys:
                    if name_variant(name, k):
                        st = stamps[k]
                        break
            resolved[name] = st
            return st

        kept, dropped = [], []
        for n in names:
            older = stamp_older_than(_stamp_for(n), cutoff)
            (dropped if older else kept).append(n)
        if dropped:
            self.say(f"   窗预过滤：{len(names)} → {len(kept)}"
                     f"（戳明确早于 {cutoff} 的 {len(dropped)} 项跳过）")
        return kept

    def _skip_names(self) -> tuple[str, ...]:
        raw = self.cfg.get("batch", "skip_names", "")
        if raw:
            return tuple(x.strip() for x in raw.split(",") if x.strip())
        return DEFAULT_SKIP

    def _notify(self, text: str) -> None:
        if not self.cfg.get_bool("notify", "enabled", True):
            return
        try:
            from .notify.wecom import WeComNotifier
            WeComNotifier(self.cfg.webhook).send_text(text)
        except Exception as e:
            self.say(f"⚠ 企业微信通知失败：{e}")

    @staticmethod
    def _load_progress(p: Path) -> dict:
        try:
            return json.loads(p.read_text(encoding="utf-8"))
        except Exception:
            return {}

    @staticmethod
    def _save_progress(p: Path, data: dict) -> None:
        p.write_text(json.dumps(data, ensure_ascii=False, indent=1),
                     encoding="utf-8")


def _safe(name: str) -> str:
    return "".join(c if (c.isalnum() or c in "-_一-龥") else "_"
                   for c in name).strip("_")[:60] or "chat"


def dedupe_names(names: list[str]) -> list[str]:
    """OCR 变体聚类去重，**保留更长读数**。

    先见的可能是坏读数（列表首屏把「赵嫣嫣AI事务所」读成「赵A事务所」，
    模糊相似度和长度差护栏都拦不住这种跳字变体），保留先见者会让
    标题头验证必然失败——聚为同簇（name_variant 三级判据）时取更长的
    读数（更长 = 更接近列表真实显示名）。
    """
    from .extractor.navigator import name_variant

    deduped: list[str] = []
    for n in names:
        hit = next((i for i, m in enumerate(deduped)
                    if n == m or name_variant(n, m)), None)
        if hit is None:
            deduped.append(n)
        elif len(n) > len(deduped[hit]):
            deduped[hit] = n
    return deduped
