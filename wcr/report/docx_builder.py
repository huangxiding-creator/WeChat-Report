# -*- coding: utf-8 -*-
"""公文级 Word 报告生成器。

排版规范（中国公文习惯）：
  标题：方正小标宋/宋体 加粗 居中
  一级标题：黑体 三号 居中
  二级标题：楷体 四号 加粗
  正文：仿宋_GB2312/仿宋 四号，首行缩进 2 字符，行距 28pt
  表格：表头浅蓝底 + 黑体
  页面：A4，上 3.7cm 下 3.5cm 左 2.8cm 右 2.6cm（公文页边距）
"""
from __future__ import annotations

import logging
from datetime import datetime
from pathlib import Path
from typing import Optional

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.oxml import parse_xml
from docx.oxml.ns import nsdecls, qn
from docx.shared import Cm, Inches, Pt, RGBColor

from ..ai.analyzer import ReportContent
from .stats import stats_brief

log = logging.getLogger("wcr.docx")

RED = RGBColor(0xC0, 0x00, 0x00)
GRAY = RGBColor(0x66, 0x66, 0x66)


class DocxBuilder:
    """把 ReportContent + stats 渲染为专业 Word。"""

    def __init__(self, org_name: str = "", title_prefix: str = ""):
        self.org_name = org_name
        self.title_prefix = title_prefix
        self.doc = Document()
        self._setup_page()
        self._setup_style()

    # ------------------------------------------------------------ setup
    def _setup_page(self) -> None:
        for section in self.doc.sections:
            section.page_width = Cm(21)
            section.page_height = Cm(29.7)
            section.top_margin = Cm(3.7)
            section.bottom_margin = Cm(3.5)
            section.left_margin = Cm(2.8)
            section.right_margin = Cm(2.6)

    def _setup_style(self) -> None:
        style = self.doc.styles["Normal"]
        style.font.name = "仿宋"
        style.font.size = Pt(14)
        style.element.rPr.rFonts.set(qn("w:eastAsia"), "仿宋")
        style.paragraph_format.line_spacing = Pt(28)

    # ------------------------------------------------------------ helpers
    def _set_run(self, run, font="仿宋", size=14, bold=False,
                 color: Optional[RGBColor] = None, eastAsia=None):
        run.font.name = font
        run.font.size = Pt(size)
        run.bold = bold
        if color:
            run.font.color.rgb = color
        ea = eastAsia or font
        rPr = run._element.get_or_add_rPr()
        rFonts = rPr.find(qn("w:rFonts"))
        if rFonts is None:
            rFonts = parse_xml(f'<w:rFonts {nsdecls("w")} w:eastAsia="{ea}"/>')
            rPr.insert(0, rFonts)
        else:
            rFonts.set(qn("w:eastAsia"), ea)

    def _para(self, text, font="仿宋", size=14, bold=False,
              color: Optional[RGBColor] = None, alignment=None,
              space_before=0, space_after=0, indent_first=None,
              line_spacing=None):
        p = self.doc.add_paragraph()
        if alignment is not None:
            p.alignment = alignment
        if space_before:
            p.paragraph_format.space_before = Pt(space_before)
        if space_after:
            p.paragraph_format.space_after = Pt(space_after)
        if indent_first:
            p.paragraph_format.first_line_indent = Pt(indent_first)
        if line_spacing:
            p.paragraph_format.line_spacing = Pt(line_spacing)
        r = p.add_run(text)
        self._set_run(r, font, size, bold, color)
        return p

    def _h1(self, text):
        return self._para(text, font="黑体", size=18,
                          alignment=WD_ALIGN_PARAGRAPH.CENTER,
                          space_before=20, space_after=16, line_spacing=30)

    def _h2(self, text):
        return self._para(text, font="楷体", size=16, bold=True,
                          space_before=14, space_after=8, line_spacing=28)

    def _body(self, text, indent=True):
        return self._para(text, font="仿宋", size=14,
                          indent_first=28 if indent else None, line_spacing=28)

    # ------------------------------------------------------------ TOC
    def _add_toc(self):
        p = self.doc.add_paragraph()
        run = p.add_run()
        fldChar = parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="begin"/>')
        instr = parse_xml(f'<w:instrText {nsdecls("w")} xml:space="preserve">'
                          f' TOC \\o "1-2" \\h \\z \\u </w:instrText>')
        sep = parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="separate"/>')
        end = parse_xml(f'<w:fldChar {nsdecls("w")} w:fldCharType="end"/>')
        run._r.append(fldChar)
        run._r.append(instr)
        run._r.append(sep)
        run._r.append(end)

    # ------------------------------------------------------------ cover
    def _cover(self, title: str, meta_items: list[tuple[str, str]]):
        for _ in range(6):
            self.doc.add_paragraph()
        self._para("━" * 27, size=12, color=RED,
                   alignment=WD_ALIGN_PARAGRAPH.CENTER)
        full_title = (self.title_prefix + title) if self.title_prefix else title
        # 长标题拆两行
        if len(full_title) > 14:
            mid = len(full_title) // 2
            self._para(full_title[:mid], font="方正小标宋简体", size=28, color=RED,
                       alignment=WD_ALIGN_PARAGRAPH.CENTER, space_before=30)
            self._para(full_title[mid:], font="方正小标宋简体", size=28, color=RED,
                       alignment=WD_ALIGN_PARAGRAPH.CENTER, space_after=10)
        else:
            self._para(full_title, font="方正小标宋简体", size=28, color=RED,
                       alignment=WD_ALIGN_PARAGRAPH.CENTER,
                       space_before=30, space_after=10)
        self._para("━" * 27, size=12, color=RED,
                   alignment=WD_ALIGN_PARAGRAPH.CENTER)
        for _ in range(8):
            self.doc.add_paragraph()
        for label, value in meta_items:
            p = self.doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            p.paragraph_format.space_after = Pt(6)
            r1 = p.add_run(f"{label}：")
            self._set_run(r1, "仿宋", 14, bold=True)
            r2 = p.add_run(value)
            self._set_run(r2, "仿宋", 14)
        self.doc.add_page_break()

    # ------------------------------------------------------------ tables
    def _table(self, headers: list[str], rows: list[list[str]]):
        table = self.doc.add_table(rows=len(rows) + 1, cols=len(headers),
                                   style="Table Grid")
        table.alignment = WD_ALIGN_PARAGRAPH.CENTER
        for j, h in enumerate(headers):
            cell = table.rows[0].cells[j]
            cell.text = ""
            p = cell.paragraphs[0]
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(h)
            self._set_run(r, "黑体", 12, bold=True)
            shd = parse_xml(f'<w:shd {nsdecls("w")} w:fill="D9E2F3" w:val="clear"/>')
            cell._element.get_or_add_tcPr().append(shd)
        for i, row in enumerate(rows):
            for j, v in enumerate(row):
                cell = table.rows[i + 1].cells[j]
                cell.text = ""
                p = cell.paragraphs[0]
                p.alignment = WD_ALIGN_PARAGRAPH.CENTER
                r = p.add_run(str(v))
                self._set_run(r, "仿宋", 12)
        self.doc.add_paragraph()

    def _image(self, path: Path, caption: str, width=4.5):
        if not Path(path).exists():
            log.warning("图片不存在，跳过：%s", path)
            return
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.CENTER
        p.paragraph_format.space_before = Pt(6)
        run = p.add_run()
        run.add_picture(str(path), width=Inches(width))
        cap = self.doc.add_paragraph()
        cap.alignment = WD_ALIGN_PARAGRAPH.CENTER
        cap.paragraph_format.space_after = Pt(10)
        r = cap.add_run(caption)
        self._set_run(r, "楷体", 10, color=GRAY)

    # ------------------------------------------------------------ main
    def build(self, content: ReportContent, stats: dict,
              chat_names: list[str], out_path: Path) -> Path:
        title = content.title or "聊天记录分析报告"
        time_start = stats.get("time_start")
        time_end = stats.get("time_end")
        cover_meta = [
            ("编制单位", self.org_name or "（编制单位）"),
            ("资料来源", "、".join(chat_names)[:60]),
            ("涵盖时段", (f"{time_start:%Y年%m月%d日} 至 {time_end:%Y年%m月%d日}"
                        if time_start and time_end else "（全部可见记录）")),
            ("编制日期", datetime.now().strftime("%Y年%m月")),
        ]
        self._cover(title, cover_meta)

        # 目录
        self._h1("目　　录")
        self._add_toc()
        self.doc.add_page_break()

        # 一、总体概述
        self._h1("第一部分　总体概述")
        self._body(content.summary or "（无综述）")
        self._h2("一、数据概况")
        bk = stats.get("by_kind", {})
        self._table(
            ["指标", "数值"],
            [
                ["消息总量", f"{stats.get('total', 0)} 条"],
                ["文字消息", f"{bk.get('text', 0)} 条"],
                ["图片消息", f"{bk.get('image', 0)} 条"],
                ["语音消息", f"{bk.get('voice', 0)} 条"],
                ["活跃天数", f"{stats.get('active_days', 0)} 天"],
                ["消息峰值日", f"{stats['peak_day'][0]}（{stats['peak_day'][1]} 条）"
                 if stats.get("peak_day") else "—"],
            ],
        )
        if stats.get("top_speakers"):
            self._h2("二、参与者活跃度")
            self._table(["序号", "参与者", "消息数"],
                        [[str(i + 1), w, str(n)]
                         for i, (w, n) in enumerate(stats["top_speakers"])])
        if stats.get("keywords"):
            self._h2("三、高频关键词")
            self._table(["序号", "关键词", "出现次数"],
                        [[str(i + 1), w, str(n)]
                         for i, (w, n) in enumerate(stats["keywords"])])

        # 二、主体章节
        self._h1("第二部分　专题分析")
        for sec in content.sections:
            self._h2(sec.get("heading", ""))
            for para in sec.get("paras", []):
                self._body(para)

        # 三、结论与建议
        if content.conclusions or content.suggestions:
            self._h1("第三部分　结论与建议")
            if content.conclusions:
                self._h2("一、主要结论")
                for i, c in enumerate(content.conclusions, 1):
                    self._body(f"{i}．{c}", indent=False)
            if content.suggestions:
                self._h2("二、工作建议")
                for i, s in enumerate(content.suggestions, 1):
                    self._body(f"{i}．{s}", indent=False)

        # 四、大事记
        if content.timeline:
            self._h1("第四部分　大事记")
            last_month = ""
            for item in content.timeline:
                d = str(item.get("date", ""))
                if d[:7] != last_month:
                    last_month = d[:7]
                    self._h2(d[:7].replace("-", " 年 ") + " 月" if "-" in d else d)
                self._body(f"■ {d}　{item.get('desc', '')}", indent=False)

        # 五、附录：图片资料
        if content.image_captions:
            self._h1("附录　关键图片资料")
            for i, cap in enumerate(content.image_captions, 1):
                self._image(Path(cap["path"]), f"图 {i}　{cap['caption']}")

        # 落款
        self.doc.add_paragraph()
        self.doc.add_paragraph()
        if self.org_name:
            p = self.doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
            r = p.add_run(self.org_name)
            self._set_run(r, "仿宋", 14)
        p = self.doc.add_paragraph()
        p.alignment = WD_ALIGN_PARAGRAPH.RIGHT
        r = p.add_run(datetime.now().strftime("%Y年%m月"))
        self._set_run(r, "仿宋", 14)

        out_path = Path(out_path)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        self.doc.save(str(out_path))
        log.info("报告已生成：%s", out_path)
        return out_path
