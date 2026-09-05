# -*- coding: utf-8 -*-
"""docx 后台渲染进程：消费文件队列，把 *_messages.json 渲染成聊天记录 docx。

效率设计（2026-09-06 用户指示：提速但绝不增加封号风险）：
docx 生成（含图片 OCR 附注，图片多的聊天实测 ~5 分钟）全程不碰微信，
把它从批量导出的关键路径挪到本进程并行——微信侧滚轮/点击/停顿节奏
一个毫秒都不变，驱动进程采完即进下一聊天。

队列协议（目录即队列，驱动崩溃不留死锁）：
  {queue}/NNNN-*.job.json  待渲染作业（name/json_path/docx_path/…）
  {queue}/*.err.json       渲染失败留档（下次启动由驱动复活重试一次）
  {queue}/_stop            优雅停机标记（排空后退出；--once 模式排空即退）
用法：
  python -X utf8 -m wcr.report.render_worker <queue_dir> [--once]
"""
from __future__ import annotations

import json
import sys
import time
from pathlib import Path


def render_job(job: dict) -> Path:
    """单个作业：JSON → Chat → docx（幂等，重复渲染覆盖同名文件）。"""
    from ..models import Chat
    from .transcript_docx import build_transcript_docx

    chat = Chat.from_json(Path(job["json_path"]))
    return build_transcript_docx(
        chat, Path(job["docx_path"]),
        time_window=job.get("time_window", ""),
        max_images=int(job.get("max_images", 50)))


def drain_queue(queue_dir: Path, poll_s: float = 2.0, once: bool = False,
                stale_exit_s: float = 900.0, say=print) -> int:
    """消费队列直到排空（--once）或见到 _stop 标记。返回成功渲染数。

    驱动心跳（_driver_alive，每聊天一次）过期且队列已空 → 自杀退出：
    驱动被硬杀后本进程不留守成孤儿（大聊天上滚可达 2.5h+，过期阈值
    取 15 分钟 = 队列空 + 半小时无心跳的双重确认）。
    """
    queue_dir = Path(queue_dir)
    queue_dir.mkdir(parents=True, exist_ok=True)
    stop_file = queue_dir / "_stop"
    heartbeat = queue_dir / "_driver_alive"
    done = 0
    while True:
        jobs = sorted(queue_dir.glob("*.job.json"))
        if not jobs:
            if once or stop_file.exists():
                return done
            try:
                fresh = time.time() - heartbeat.stat().st_mtime < stale_exit_s
            except OSError:
                fresh = False
            if not fresh:
                say("驱动心跳过期且队列已空，渲染进程退出")
                return done
            time.sleep(poll_s)
            continue
        for job_file in jobs:
            try:
                job = json.loads(job_file.read_text(encoding="utf-8"))
            except Exception as e:
                say(f"✗ 作业损坏 {job_file.name}：{e}")
                job_file.replace(job_file.with_suffix(".err.json"))
                continue
            try:
                out = render_job(job)
                done += 1
                say(f"✔ docx 落盘：{Path(job['docx_path']).name}"
                    f"（{out.stat().st_size // 1024} KB）")
                job_file.unlink()
            except Exception as e:
                say(f"✗ 渲染失败 {job_file.name}：{e}")
                job_file.replace(job_file.with_suffix(".err.json"))


def main(argv: list[str]) -> int:
    if len(argv) < 2:
        print(__doc__)
        return 2
    queue_dir = Path(argv[1])
    once = "--once" in argv[2:]
    t0 = time.monotonic()
    n = drain_queue(queue_dir, once=once)
    print(f"渲染进程退出：{n} 个 docx，{time.monotonic() - t0:.0f}s")
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
