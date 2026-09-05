# -*- coding: utf-8 -*-
"""WeChat-Report 入口。

    python main.py                  # 桌面 GUI（默认）
    python main.py --cli            # 命令行模式（按 config.ini 跑全流程）
    python main.py --cli --chats "群A" --chats "好友B" --window 7d --template work
"""
from __future__ import annotations

import argparse
import logging
import sys
from pathlib import Path

# 保证从任意目录启动都能 import wcr
sys.path.insert(0, str(Path(__file__).resolve().parent))

from wcr import __version__  # noqa: E402
from wcr.config import Config  # noqa: E402
from wcr.models import ReportSpec  # noqa: E402


def main():
    parser = argparse.ArgumentParser(
        prog="WeChat-Report",
        description=f"把微信聊天记录变成公文级专业报告。v{__version__}",
    )
    parser.add_argument("--cli", action="store_true", help="命令行模式（不开 GUI）")
    parser.add_argument("--chats", action="append", default=[],
                        help="目标聊天名（可多次指定：多聊天合并报告）")
    parser.add_argument("--window", default="", help="时间窗：7d / 2026-03 / 2026-01-01~2026-08-27；或年份 2026=只采列表戳在2026内的会话(全量深度)")
    parser.add_argument("--template", default="work", choices=["work", "progress", "general"])
    parser.add_argument("--title", default="", help="报告标题（空=自动）")
    parser.add_argument("--json", action="append", default=[],
                        help="离线 JSON 数据源（可多次指定；用于回放/测试）")
    parser.add_argument("--all", action="store_true",
                        help="批量模式：枚举全部会话，每个聊天导出一份聊天记录 word")
    parser.add_argument("--limit", type=int, default=0,
                        help="批量模式试点：只处理前 N 个聊天（0=全部）")
    parser.add_argument("--only", default="",
                        help="批量模式定向名单：逗号分隔，模糊匹配（如 \"黄藏寺项目值班,黄春健\"）")
    parser.add_argument("--output", default="", help="输出目录（默认 ./output）")
    parser.add_argument("--gen-example-config", action="store_true",
                        help="生成 config.example.ini 后退出")
    parser.add_argument("--version", action="version",
                        version=f"WeChat-Report v{__version__}")
    args = parser.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
        datefmt="%H:%M:%S",
    )

    cfg = Config.load()

    if args.gen_example_config:
        p = cfg.write_example()
        print(f"已生成：{p}")
        return 0

    if args.all:
        # 批量模式：所有会话 → 每聊天一个独立聊天记录 word（无 AI）
        if not args.cli:
            print("批量模式需要 --cli（会长时间占用鼠标，请勿操作电脑）")
            return 1
        from wcr.batch import BatchExporter
        out_dir = Path(args.output) if args.output else cfg.output_dir / "批量导出"
        res = BatchExporter(cfg, on_progress=print).run(
            out_dir, time_window=args.window or "365d", limit=args.limit,
            only=[x.strip() for x in args.only.split(",") if x.strip()] or None)
        return 0 if res.failed == 0 else 1

    if args.cli:
        spec = ReportSpec(
            chat_names=args.chats,
            time_window=args.window,
            template=args.template,
            title=args.title,
            org_name=cfg.get("report", "org_name", ""),
            output_dir=Path(args.output) if args.output else cfg.output_dir,
            json_sources=[Path(p) for p in args.json],
        )
        from wcr.pipeline import run
        res = run(spec, cfg, on_progress=print)
        return 0 if res.success else 1

    # GUI 模式
    from wcr.gui import main as gui_main
    gui_main(cfg)
    return 0


if __name__ == "__main__":
    sys.exit(main())
