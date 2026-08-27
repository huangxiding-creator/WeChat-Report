# -*- coding: utf-8 -*-
"""CustomTkinter 桌面界面：一键启动、多聊天输入、时间窗、模板选择、实时日志。"""
from __future__ import annotations

import queue
import threading
import traceback
import webbrowser
from pathlib import Path
from typing import Optional

import customtkinter as ctk

from . import __version__
from .config import Config
from .models import ReportSpec
from .pipeline import run


class App(ctk.CTk):
    def __init__(self, cfg: Config):
        super().__init__()
        self.cfg = cfg
        self.title(f"WeChat-Report 微信聊天报告生成器 v{__version__}")
        self.geometry("880x720")
        ctk.set_appearance_mode(cfg.get("app", "appearance", "dark"))
        ctk.set_default_color_theme(cfg.get("app", "theme", "dark-blue"))

        self._q: queue.Queue = queue.Queue()
        self._worker: threading.Thread | None = None
        self._build()
        self.after(200, self._drain)

    # ------------------------------------------------------------ ui
    def _build(self):
        pad = {"padx": 12, "pady": 6}

        head = ctk.CTkFrame(self)
        head.pack(fill="x", **pad)
        ctk.CTkLabel(head, text="🖨 WeChat-Report",
                     font=ctk.CTkFont(size=22, weight="bold")).pack(side="left", padx=10, pady=8)
        ctk.CTkLabel(head, text=f"v{__version__} · 只读采集 · 智谱免费模型 · 公文级报告",
                     text_color="#8f8f8f").pack(side="left", padx=4)
        ctk.CTkButton(head, text="⚙ 设置", width=64,
                      command=self._open_settings).pack(side="right", padx=10)

        body = ctk.CTkFrame(self)
        body.pack(fill="both", expand=True, **pad)

        # 聊天名（每行一个）
        row1 = ctk.CTkFrame(body)
        row1.pack(fill="x", padx=10, pady=(10, 2))
        ctk.CTkLabel(row1, text="目标聊天（群名/好友名，每行一个，可多个合并成一份报告）").pack(anchor="w")
        self.txt_chats = ctk.CTkTextbox(row1, height=88)
        self.txt_chats.pack(fill="x", pady=4)
        self.txt_chats.insert("1.0", self._default_chats())

        # 参数行
        row2 = ctk.CTkFrame(body)
        row2.pack(fill="x", padx=10, pady=8)
        ctk.CTkLabel(row2, text="时间窗").pack(side="left", padx=(0, 4))
        self.ent_window = ctk.CTkEntry(row2, width=170,
                                       placeholder_text="空=全量 / 7d / 2026-03 / 起~止")
        self.ent_window.pack(side="left", padx=(0, 16))
        ctk.CTkLabel(row2, text="模板").pack(side="left", padx=(0, 4))
        self.cmb_template = ctk.CTkComboBox(
            row2, width=150, values=["work", "progress", "general"])
        self.cmb_template.set(self.cfg.get("report", "template", "work"))
        self.cmb_template.pack(side="left", padx=(0, 16))
        ctk.CTkLabel(row2, text="导航").pack(side="left", padx=(0, 4))
        self.cmb_nav = ctk.CTkComboBox(
            row2, width=110, values=["auto", "manual"])
        self.cmb_nav.set(self.cfg.get("extract", "navigate_mode", "auto"))
        self.cmb_nav.pack(side="left")

        # 标题/单位
        row3 = ctk.CTkFrame(body)
        row3.pack(fill="x", padx=10, pady=(0, 8))
        ctk.CTkLabel(row3, text="报告标题（空=自动）").pack(side="left", padx=(0, 4))
        self.ent_title = ctk.CTkEntry(row3, width=300)
        self.ent_title.pack(side="left", padx=(0, 16))
        ctk.CTkLabel(row3, text="编制单位（空=不落款）").pack(side="left", padx=(0, 4))
        self.ent_org = ctk.CTkEntry(row3, width=240)
        self.ent_org.insert(0, self.cfg.get("report", "org_name", ""))
        self.ent_org.pack(side="left")

        # 按钮
        row4 = ctk.CTkFrame(body)
        row4.pack(fill="x", padx=10, pady=4)
        self.btn_run = ctk.CTkButton(row4, text="🚀 生成报告", height=36,
                                     command=self._start)
        self.btn_run.pack(side="left", padx=(0, 8))
        self.btn_open = ctk.CTkButton(row4, text="📂 打开输出目录", height=36,
                                      command=self._open_output)
        self.btn_open.pack(side="left")
        self.lbl_state = ctk.CTkLabel(row4, text="就绪", text_color="#7fb069")
        self.lbl_state.pack(side="right", padx=8)

        # 日志
        ctk.CTkLabel(body, text="运行日志").pack(anchor="w", padx=10)
        self.txt_log = ctk.CTkTextbox(body, height=260, font=ctk.CTkFont(family="Consolas", size=13))
        self.txt_log.pack(fill="both", expand=True, padx=10, pady=(2, 12))

    def _default_chats(self) -> str:
        saved = self.cfg.get("extract", "last_chats", "")
        return saved or ""

    # ------------------------------------------------------------ actions
    def _start(self):
        if self._worker and self._worker.is_alive():
            self._log("⚠ 任务正在运行中")
            return
        names = [ln.strip() for ln in self.txt_chats.get("1.0", "end").splitlines()
                 if ln.strip()]
        if not names:
            self._log("❌ 请至少输入一个聊天名称")
            return
        spec = ReportSpec(
            chat_names=names,
            time_window=self.ent_window.get().strip(),
            template=self.cmb_template.get(),
            title=self.ent_title.get().strip(),
            org_name=self.ent_org.get().strip(),
            output_dir=self.cfg.output_dir,
        )
        # 保存本次选择，下次启动预填
        self.cfg.set("extract", "last_chats", "\n".join(names))
        self.cfg.set("report", "template", spec.template)
        self.cfg.set("extract", "navigate_mode", self.cmb_nav.get())
        self.cfg.set("report", "org_name", spec.org_name)
        try:
            self.cfg.save()
        except Exception:
            pass

        self.btn_run.configure(state="disabled", text="⏳ 运行中 …")
        self.lbl_state.configure(text="运行中", text_color="#e0a458")
        self._log(f"🚀 开始：{spec.resolved_title()}（{len(names)} 个聊天，"
                  f"时间窗 {spec.time_window or '全量'}）")

        def worker():
            try:
                res = run(spec, self.cfg, on_progress=lambda m: self._q.put(m))
                self._q.put(None if res.success else f"__FAIL__{res.error}")
            except Exception:
                self._q.put("__FAIL__" + traceback.format_exc(limit=3))

        self._worker = threading.Thread(target=worker, daemon=True)
        self._worker.start()

    def _open_output(self):
        out = self.cfg.output_dir
        out.mkdir(parents=True, exist_ok=True)
        webbrowser.open(out.resolve().as_uri())

    def _open_settings(self):
        dlg = SettingsDialog(self, self.cfg)

    # ------------------------------------------------------------ log pump
    def _log(self, msg: str):
        self.txt_log.insert("end", msg + "\n")
        self.txt_log.see("end")

    def _drain(self):
        try:
            while True:
                msg = self._q.get_nowait()
                if msg is None:
                    self._log("🎉 任务完成")
                    self.btn_run.configure(state="normal", text="🚀 生成报告")
                    self.lbl_state.configure(text="完成", text_color="#7fb069")
                elif isinstance(msg, str) and msg.startswith("__FAIL__"):
                    self._log(f"❌ {msg[8:]}")
                    self.btn_run.configure(state="normal", text="🚀 生成报告")
                    self.lbl_state.configure(text="失败", text_color="#d9534f")
                else:
                    self._log(str(msg))
        except queue.Empty:
            pass
        self.after(200, self._drain)


class SettingsDialog(ctk.CTkToplevel):
    """设置对话框：密钥 + 关键 AI 参数（写回 config.ini）。"""

    def __init__(self, master, cfg: Config):
        super().__init__(master)
        self.cfg = cfg
        self.title("设置")
        self.geometry("640x420")
        self.transient(master)
        self.grab_set()

        ctk.CTkLabel(self, text="⚙ 设置（保存写入 config.ini）",
                     font=ctk.CTkFont(size=16, weight="bold")).pack(pady=10)

        form = ctk.CTkFrame(self)
        form.pack(fill="both", expand=True, padx=14)

        self.vars: dict[tuple[str, str, str], ctk.CTkEntry] = {}
        fields = [
            ("智谱 API Key", ("ai", "api_key"), True),
            ("API Base URL", ("ai", "base_url", "https://open.bigmodel.cn/api/paas/v4"), False),
            ("文本模型（逗号分隔）", ("ai", "text_models", "glm-4.5-flash,glm-4-flash-250414"), False),
            ("视觉模型", ("ai", "vision_model", "glm-4v-flash"), False),
            ("企业微信 Webhook", ("notify", "webhook"), True),
            ("分块大小（字符）", ("ai", "chunk_chars", "3500"), False),
            ("图片理解数量", ("ai", "vision_images", "12"), False),
        ]
        for label, key, secret in fields:
            row = ctk.CTkFrame(form)
            row.pack(fill="x", pady=3)
            ctk.CTkLabel(row, text=label, width=180, anchor="w").pack(side="left", padx=(6, 4))
            var = ctk.CTkEntry(row, show="*" if secret else "",
                               width=400 if secret else 400)
            default = key[2] if len(key) == 3 else ""
            var.insert(0, cfg.get(key[0], key[1], default))
            var.pack(side="left", fill="x", expand=True)
            self.vars[(key[0], key[1])] = var

        ctk.CTkButton(self, text="保存", command=self._save).pack(pady=12)

    def _save(self):
        for (sec, key), var in self.vars.items():
            self.cfg.set(sec, key, var.get())
        self.cfg.save()
        self.destroy()


def main(cfg: Optional[Config] = None):
    app = App(cfg or Config.load())
    app.mainloop()


if __name__ == "__main__":
    main()
