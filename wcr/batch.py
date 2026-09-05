# -*- coding: utf-8 -*-
"""批量导出器：枚举全部会话 → 逐个采集 → 每聊天一个独立聊天记录 Word。

用途（对应用户需求）：把 PC 微信里所有私聊/群聊在时间窗内的聊天记录
分别整理成单独的 word 文件（无 AI、零成本、断点可续、企业微信汇报）。

只读保证：全程滚轮 + 会话列表受护栏左键（SafetyGuard），零键盘零右键。
"""
from __future__ import annotations

import json
import logging
import re
import subprocess
import sys
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
            retry_failed: bool = True,
            only: Optional[list[str]] = None) -> BatchResult:
        res = BatchResult()
        out_dir.mkdir(parents=True, exist_ok=True)
        render_q = out_dir / "_render_queue"
        render_q.mkdir(exist_ok=True)
        # 渲染子进程的驱动心跳：随每条进度日志触碰（上滚每 60 轮必报一条，
        # 大聊天 2.5h+ 期间也不会被误判驱动死亡而提前退出）
        _hb = render_q / "_driver_alive"
        _orig_say = self.say

        def _say_hb(m: str) -> None:
            try:
                _hb.write_text("", encoding="utf-8")
            except OSError:
                pass
            _orig_say(m)

        self.say = _say_hb
        progress_path = out_dir / "_batch_progress.json"
        progress = self._load_progress(progress_path) if resume else {}
        if retry_failed:
            # 上次失败的（error 项）本次重试一次；重跑原命令即可，避免反复撞墙
            failed = [k for k, v in progress.items() if "error" in v]
            for k in failed:
                progress.pop(k)

        # 启动门：鼠标停在屏幕角点时，pyautogui 任何动作都会立刻触发
        # fail-safe（实测两次启动 36 秒/55 秒即死——用户把鼠标歇在角落）。
        # 只等待移开，绝不代为移动；fail-safe 杀手锏本身保留（人工随时
        # 可把鼠标甩到角落中止自动化）。
        _wait_mouse_off_corner(self.say)

        from .pipeline import make_visual_extractor
        # 年份令牌（如 "2026"）= 活跃年选择语义（用户 2026-09-05 指示）：
        # 只采列表右列戳在 2026 内的会话（= 最后一条消息在 2026 年），
        # 深度为**全量**——滚到验证过的真顶、不做消息级时间窗过滤；
        # "365d" 等仍是消息窗语义
        year_sel = re.fullmatch(r"(20\d{2})", (time_window or "").strip())
        capture_window = "" if year_sel else time_window
        # probe 模式：上滚逐屏 OCR 探测，见到早于窗起点的标签即停（年份令牌
        # 下无窗起点 → 滚到动态加载两轮验证的真顶）；文本集连续稳定 = 到顶
        # 候选（GIF 动图免疫）。top 模式实测会在缓存顶转轮上 800 档空转 10
        # 分钟（懒加载转轮让帧差永不相等）
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
        # 采戳自诊断：戳字典大小 + 年份戳样本（预过滤覆盖率异常时定位用）；
        # 落盘供离线分析（快速滚动下老聊天 2024/* 淡灰戳漏采率高，需迭代）
        st_n = len(enumerator.stamps)
        st_year = sum(1 for v in enumerator.stamps.values()
                      if any(y in v for y in ("2024", "2025", "2026", "2O24", "2U24")))
        self.say(f"   采戳：{st_n} 名有时间戳（其中年份戳 {st_year}）")
        try:
            (out_dir / "_stamps.json").write_text(
                json.dumps(enumerator.stamps, ensure_ascii=False, indent=1),
                encoding="utf-8")
        except OSError:
            pass
        deduped = dedupe_names(names)
        if len(deduped) != len(names):
            self.say(f"   枚举去重：{len(names)} → {len(deduped)}（OCR 变体合并）")
        kept = self._prefilter_window(deduped, enumerator.stamps, time_window)
        n_all = len(deduped)   # 预过滤前的全列表长度：滚动/扫描预算按真实高度
        names = kept
        if only:
            hits = _match_only(names, only)
            if not hits:
                self.say(f"   ⚠ 定向名单无命中：{only}（枚举 {len(names)} 名）")
                raise RuntimeError("定向名单无命中")
            self.say(f"   定向名单：{len(names)} → {len(hits)} 个命中（请求 "
                     f"{len(only)} 名）")
            names = hits
        if limit:
            names = names[:limit]
        res.total = len(names)
        # 枚举后列表停在底部：回顶一次，后续按名单顺序自上而下扫最省时
        # （400+ 项列表高 ~26,000px，必须按 expected_items 放大回顶预算，
        #   默认 60 轮只滚 ~9,000px 会停在中段；注意预算用 n_all 而非
        #   预过滤后的数量——窗内 100 项 × 65px 远小于列表真实高度）
        from .extractor.navigator import scroll_session_list_to_top
        scroll_session_list_to_top(win, guard, expected_items=n_all)
        scope = (f"活跃 ≥ {year_sel.group(1)} 年（全量深度）" if year_sel
                 else f"时间窗 {time_window}")
        self.say(f"🎯 批量导出：{res.total} 个聊天，{scope}")
        self._notify(f"【WeChat-Report 批量导出】启动\n目标聊天：{res.total} 个"
                     f"\n范围：{scope}\n已完成（断点）：{len(progress)} 个")

        # 2. 逐个导出（docx 由渲染子进程并行生成——大文档实测 ~5 分钟，
        #    不让微信空闲等它；微信侧滚轮/点击/停顿节奏零改动）
        render_proc, render_log = _start_render_worker(render_q)
        render_errs: list[Path] = []
        t0 = time.monotonic()
        try:
            for i, name in enumerate(names, 1):
                (render_q / "_driver_alive").write_text("", encoding="utf-8")
                done_key = _progress_key(name, progress)
                if done_key is not None:
                    res.skipped += 1
                    self.say(f"⏭ [{i}/{res.total}] 「{name}」已完成"
                             f"（断点跳过：{done_key}）")
                    continue
                self.say(f"\n───── [{i}/{res.total}] 「{name}」 ─────")
                ok = False
                last_err: Optional[Exception] = None
                for attempt in (1, 2):
                    if attempt == 2:
                        # fail-safe 人工碰角重试：等鼠标离开角落后原目标再试一次
                        #（目标 1 实测：15 分钟上滚成果被一次碰角全部作废）。
                        # 持续按住角落 = 人为停机，等待不打扰；杀进程仍可随时终止。
                        if last_err is None or "fail-safe" not in str(last_err).lower():
                            break
                        self.say("   🖱 fail-safe（鼠标碰角）——等待鼠标离开角落后重试本聊天")
                        _wait_mouse_off_corner(self.say)
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
                        # 采集（already_open：跳过 extract 内部导航；年份令牌下
                        # capture_window="" → 全量深度到真顶）
                        chat = ext.extract(name, capture_window, self.say,
                                           already_open=True)
                        # docx 渲染排队（子进程并行）：JSON 已在 extract 内落盘，
                        # 进度即时可记（断点语义不变），docx 名预知供验收核对
                        stamp = datetime.now().strftime("%Y%m%d_%H%M%S")
                        docx_path = out_dir / f"{_safe(name)}_聊天记录_{stamp}.docx"
                        _enqueue_render_job(render_q, {
                            "name": name,
                            "json_path": str(out_dir / f"{_safe(name)}_messages.json"),
                            "docx_path": str(docx_path),
                            "time_window": capture_window,
                            "max_images": max_images,
                        })
                        self.say(f"💾 「{name}」采集完成：{len(chat.messages)} 条，"
                                 f"docx 后台渲染已排队")
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
                        ok = True
                        break
                    except Exception as e:
                        last_err = e
                if not ok:
                    res.failed += 1
                    res.errors[name] = str(last_err)[:200]
                    self.say(f"   ✗ 「{name}」失败：{last_err}")
                    log.warning("导出 %s 失败：%s", name, last_err, exc_info=True)
                    # 失败也记录，避免断点重跑时反复撞墙
                    progress[name] = {"error": str(last_err)[:200],
                                      "at": datetime.now().isoformat(timespec="seconds")}
                    self._save_progress(progress_path, progress)
                # 每个聊天之间稍歇，降低微信渲染压力
                time.sleep(0.5)
        finally:
            # 渲染队列收尾：停机标记 → 等排空（末尾若干 docx 补完）
            _drain_render_queue(render_q, render_proc, self.say)
            render_errs = sorted(render_q.glob("*.err.json"))
            if render_errs:
                self.say(f"⚠ docx 渲染失败 {len(render_errs)} 个"
                         f"（_render_queue/*.err.json，下次启动自动重试）")
            if render_log:
                render_log.close()

        # 3. 汇总
        el = int(time.monotonic() - t0)
        self.say(f"\n🎉 批量导出结束：成功 {res.done} / 失败 {res.failed} / "
                 f"跳过(已导) {res.skipped} · 共 {res.messages} 条 · {el // 60} 分钟")
        self._notify(f"【WeChat-Report 批量导出】{'🎉 完成' if not res.failed else '⚠ 结束'}\n"
                     f"成功：{res.done} 个 / 失败：{res.failed} 个\n"
                     f"累计消息：{res.messages} 条\n总耗时：{el // 60} 分钟\n"
                     + (f"docx 渲染失败：{len(render_errs)} 个\n" if render_errs else "")
                     + f"输出目录：{out_dir}")
        return res

    # ------------------------------------------------------------ helpers
    def _prefilter_window(self, names: list[str], stamps: dict[str, str],
                          time_window: str, today=None) -> list[str]:
        """按列表时间戳预过滤：戳明确早于截止日的聊天直接跳过。

        列表按最近活跃排序，右列戳（如 2024/07/23、12/17）= 最后一条
        消息时间。年份令牌（"2026"）语义下微信显示规则把无年份形式也
        变成可判定（今年内才显示 MM/DD，未来 MM/DD 必为往年残片），
        实测 612 戳中仅 16 个老戳、595+ 个可证 2026 活跃。无戳/坏读数
        → 保守保留（采集阶段再兜底）。

        戳按 OCR 读数键控；去重后的目标名可能是另一读数（更长变体），
        先经 name_variant 簇匹配把戳关联到目标名，再判窗。
        """
        from .extractor.navigator import name_variant
        from .extractor.session_enum import stamp_older_than
        cutoff = _window_cutoff(time_window)
        if cutoff is None or not stamps:
            return names

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
            older = stamp_older_than(_stamp_for(n), cutoff, today)
            (dropped if older else kept).append(n)
        # 裁尾规则：列表严格按最近活跃排序——最后一个"明确在窗内"的戳
        # 之后的全部项都必然更旧（含无戳老聊天：右列 2024/* 淡灰小字戳
        # 在快速滚动下漏采严重，逐名过滤滤不掉，靠排序性质整段裁）。
        last_in = -1
        for idx, n in enumerate(kept):
            if stamp_older_than(_stamp_for(n), cutoff, today) is False:
                last_in = idx
        if 0 <= last_in < len(kept) - 1:
            n_tail = len(kept) - 1 - last_in
            kept = kept[:last_in + 1]
            self.say(f"   裁尾：最后窗内戳之后 {n_tail} 项按列表排序跳过")
        if dropped:
            self.say(f"   窗预过滤：{len(names)} → {len(kept)}"
                     f"（戳明确早于 {cutoff} 的 {len(dropped)} 项 + 裁尾跳过）")
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


def _window_cutoff(time_window: str):
    """预过滤截止日期：'2026' 年份令牌 → 该年 1 月 1 日（活跃年选择语义，
    用户 2026-09-05 指示）；'365d' 等 → 窗起点；''/无法解析 → None。"""
    from datetime import date, datetime
    m = re.fullmatch(r"(20\d{2})", (time_window or "").strip())
    if m:
        return date(int(m.group(1)), 1, 1)
    from .extractor.timelabels import parse_window
    start_dt, _ = parse_window(time_window or "", now=datetime.now())
    return start_dt.date() if start_dt else None


def _progress_key(name: str, progress: dict) -> Optional[str]:
    """断点键匹配：先精确，再变体。

    枚举名逐轮是 OCR 变体——2026-09-06 实测重启后「2028届八年级14班」
    被读成截断的「2028届八年级1」，精确匹配失配导致已完成 2.5h 的聊天
    被整个重爬。变体判据与枚举去重同源（name_variant），误跳风险与
    枚举误合并同界；返回命中的进度键（供审计），未命中返回 None。
    """
    if name in progress:
        return name
    from .extractor.navigator import name_variant
    for k in progress:
        if name_variant(name, k):
            return k
    return None


def _match_only(names: list[str], only: list[str]) -> list[str]:
    """定向名单过滤：列表名与任一请求名互为变体/互含即命中（保序）。

    请求名常比枚举名短（"黄藏寺项目值班" vs 列表 "黄藏寺项目值班值守"），
    双向子串 + name_variant 三级判据兜 OCR 变体；命中为空 = 全部失配。
    """
    from .extractor.navigator import name_variant

    hits: list[str] = []
    for n in names:
        if any(n == o or name_variant(n, o) or o in n or n in o for o in only):
            hits.append(n)
    return hits


def _at_failsafe_corner(pos, points) -> bool:
    """鼠标是否停在 pyautogui fail-safe 角点（判据与 pyautogui 一致：
    恰在角点像素上，本机实测 [(0,0),(0,1079),(1919,0),(1919,1079)]）。"""
    return tuple(pos) in {tuple(p) for p in points}


def _enqueue_render_job(queue_dir: Path, job: dict) -> None:
    """渲染作业落盘（文件名含毫秒 + 时间戳防碰撞）。"""
    p = queue_dir / (f"{int(time.time() * 1000) % 100_000:05d}-"
                     f"{datetime.now().strftime('%Y%m%d_%H%M%S')}.job.json")
    p.write_text(json.dumps(job, ensure_ascii=False, indent=1), encoding="utf-8")


def _revive_err_jobs(queue_dir: Path) -> int:
    """上次渲染失败的 .err 作业复活为 .job（每次启动重试一轮）。"""
    n = 0
    for e in sorted(queue_dir.glob("*.err.json")):
        e.replace(e.with_suffix("").with_suffix(".json"))
        n += 1
    return n


def _start_render_worker(queue_dir: Path):
    """启动 docx 渲染子进程（子进程只做本地渲染，不碰微信；驱动崩溃后
    靠 _driver_alive 心跳过期自杀，不留守成孤儿）。日志追加在队列目录内。
    """
    _revive_err_jobs(queue_dir)
    log = open(queue_dir / "_render_worker.log", "a", encoding="utf-8")
    proc = subprocess.Popen(
        [sys.executable, "-X", "utf8", "-m", "wcr.report.render_worker",
         str(queue_dir)],
        stdout=log, stderr=subprocess.STDOUT)
    return proc, log


def _drain_render_queue(queue_dir: Path, proc, say,
                        timeout_s: float = 1800.0) -> None:
    """写停机标记后等队列排空、子进程退出（末尾若干 docx 收尾）。"""
    (queue_dir / "_stop").write_text("", encoding="utf-8")
    t0 = time.monotonic()
    while time.monotonic() - t0 < timeout_s:
        if not list(queue_dir.glob("*.job.json")) and (
                proc is None or proc.poll() is not None):
            return
        time.sleep(2.0)
    left = len(list(queue_dir.glob("*.job.json")))
    say(f"⚠ 渲染队列 {timeout_s:.0f}s 未排空，剩 {left} 个 docx 未渲染"
        "（可 python -m wcr.report.render_worker <队列目录> --once 补渲染）")


def _wait_mouse_off_corner(say, poll_s: float = 3.0,
                           remind_every_s: float = 120.0) -> None:
    """等待鼠标离开屏幕四角（fail-safe 角点静止的鼠标会秒杀第一个动作）。"""
    import pyautogui

    points = list(pyautogui.FAILSAFE_POINTS)
    t0 = time.monotonic()
    last_remind = 0.0
    while _at_failsafe_corner(pyautogui.position(), points):
        el = time.monotonic() - t0
        if el - last_remind >= remind_every_s:
            say(f"🖱 鼠标停在屏幕角落（fail-safe 区），等待移开已 {el:.0f}s …")
            last_remind = el
        time.sleep(poll_s)


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
