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
    parser.add_argument("--window", default="", help="时间窗：7d / 2026-03 / 2026-01-01~2026-08-27")
    parser.add_argument("--template", default="work", choices=["work", "progress", "general"])
    parser.add_argument("--title", default="", help="报告标题（空=自动）")
    parser.add_argument("--json", action="append", default=[],
                        help="离线 JSON 数据源（可多次指定；用于回放/测试）")
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
