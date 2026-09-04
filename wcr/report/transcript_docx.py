# -*- coding: utf-8 -*-
"""聊天记录整理 Word（格式复刻 HZSMemo _generate_full_docx 的排版风格）。

每个聊天一个独立 docx：
  标题：{聊天名} · 聊天记录
  meta：导出时间 / 时间窗 / 消息统计
  正文：按日期分隔（■ 2026年8月5日），每条
        【说话人】 HH:MM 内容
        [语音 12"] / [图片]（缩略图嵌入，上限可配）
"""
from __future__ import annotations

import logging
import re
import time
from pathlib import Path

from docx import Document
from docx.enum.text import WD_ALIGN_PARAGRAPH
from docx.shared import Inches, Pt, RGBColor

from ..models import Chat

log = logging.getLogger("wcr.transcript_docx")

BLUE = RGBColor(0x1A, 0x73, 0xE8)
GRAY = RGBColor(0x88, 0x88, 0x88)
DARK = RGBColor(0x33, 0x33, 0x33)
GREEN = RGBColor(0x00, 0x66, 0x00)

# 图片 OCR 引擎单例（docx 生成阶段惰性加载，离线、零微信交互）
_OCR_ENGINE = None

# ---------- UI 噪声过滤（渲染层） ----------
# OCR 从文件卡片/时间戳碎片读出的伪消息（实测目标[1] docx）：
#   '86.6K'（文件大小）、':38'（时钟残片）、'PDF'/'and'/'Bne'（≤3 位拉丁
#   碎片，HZSMemo 判据 <4 位不构成有效内容）。
# 保守原则：纯数字（"12" 回复）保留；常见真实短回复白名单保留。
_NOISE_KEEP = {"ok", "okay", "no", "yes", "hi", "hello", "good", "fine"}
_UI_NOISE = re.compile(
    r"^(?:\d+(?:\.\d+)?[KMGkmg]"        # 带单位大小：86.6K、3M（纯数字保留）
    r"|[QO\d]{0,2}[:：]\d{1,2}"         # 时钟残片：:38、7:38、14:05
    r"|[A-Za-z]{1,3})$")                # 短拉丁碎片：and、Bne、PDF


def _is_ui_noise(text: str) -> bool:
    """文本是否为 UI 噪声（不渲染入档）。"""
    t = (text or "").strip()
    if not t:
        return False
    return _UI_NOISE.match(t) is not None and t.lower() not in _NOISE_KEEP


def _is_bad_speaker(speaker: str) -> bool:
    """说话人读数是否为 UI 噪声（如 '86.6K'/'581'/'and'）→ 不显示前缀。"""
    s = (speaker or "").strip()
    if not s:
        return False
    if _is_ui_noise(s):
        return True
    return bool(re.fullmatch(r"[\d\s.]+", s))   # 纯数字（文件大小/计数）


def _has_meaningful(text: str) -> bool:
    """HZSMemo _ocr_images 判据：中文≥2字 / 数字日期≥6位 / 字母数字≥4位。"""
    t = (text or "").strip()
    if len(t) < 2:
        return False
    import re
    if re.search(r"[一-鿿]", t):
        return True
    if re.match(r"^[\d.\-:/]+$", t) and len(t) >= 6:
        return True
    if re.match(r"^[A-Za-z0-9.\-:_/]+$", t) and len(t) >= 4:
        return True
    return False


def _ocr_image_notes(img_path: str, max_lines: int = 10) -> list[str]:
    """图片气泡 OCR 附注（HZSMemo 方法）：4x 放大后 OCR，图内文字入档可检索。

    聊天里的截图（通知/报表/联系单）往往承载关键信息，只嵌缩略图时
    这些信息在 docx 里不可检索；附注 OCR 文字让"整份记录"完整可查。
    失败静默返回 []（附注是增益，绝不阻断 docx 生成）。
    """
    global _OCR_ENGINE
    try:
        import cv2
        from ..extractor.capture import imread_png
        from ..extractor.ocr import OCRParser
        if _OCR_ENGINE is None:
            _OCR_ENGINE = OCRParser(0.5)
        # cv2.imread 在 Windows 读不了中文路径（批量导出目录名即中文），
        # 必须走 imread_png（np.fromfile + imdecode）
        img = imread_png(img_path)
        if img is None:
            return []
        h, w = img.shape[:2]
        big = cv2.resize(img, (w * 4, h * 4), interpolation=cv2.INTER_CUBIC)
        out, seen = [], set()
        for t in _OCR_ENGINE.parse(big):
            txt = (t.get("text") or "").strip()
            if txt and txt not in seen and _has_meaningful(txt):
                seen.add(txt)
                out.append(txt)
                if len(out) >= max_lines:
                    break
        return out
    except Exception as e:
        log.debug("图片 OCR 附注失败 %s: %s", img_path, e)
        return []


def build_transcript_docx(chat: Chat, out_path: Path,
                          time_window: str = "",
                          max_images: int = 50,
                          image_width_inch: float = 3.0) -> Path:
    """Chat → 聊天记录整理 docx（无 AI 参与，零成本，秒级完成）。"""
    doc = Document()

    def _sp(m) -> str:
        """说话人前缀（噪声读数如 '86.6K' 不显示，保留消息本体）。"""
        if _is_bad_speaker(m.speaker):
            return "【我】" if m.side == "right" else ""
        return _speaker(m)

    # ---------- 标题 + meta ----------
    title = doc.add_heading(f"{chat.name} · 聊天记录", level=0)
    title.alignment = WD_ALIGN_PARAGRAPH.CENTER
    n_text = sum(1 for m in chat.messages
                 if m.kind == "text" and not _is_ui_noise(m.text))
    n_img = sum(1 for m in chat.messages if m.kind == "image")
    n_voice = sum(1 for m in chat.messages if m.kind == "voice")
    ts_list = [m.timestamp for m in chat.messages if m.timestamp]
    span = chat.coverage or (
        f"{min(ts_list):%Y-%m-%d} ~ {max(ts_list):%Y-%m-%d}" if ts_list
        else "（无时间标签）")
    meta = doc.add_paragraph()
    meta.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = meta.add_run(f"导出时间：{time.strftime('%Y-%m-%d %H:%M')}　·　"
                     f"时间窗：{time_window or '全部可见记录'}（实际覆盖 {span}）　·　"
                     f"文字 {n_text} 条 / 图片 {n_img} 张 / 语音 {n_voice} 条")
    r.font.size = Pt(9)
    r.font.color.rgb = GRAY
    note = doc.add_paragraph()
    note.alignment = WD_ALIGN_PARAGRAPH.CENTER
    r = note.add_run("（视觉自动化截图 + OCR 提取，严格只读；语音仅记录时长）")
    r.font.size = Pt(8)
    r.font.color.rgb = RGBColor(0xAA, 0xAA, 0xAA)
    doc.add_paragraph()

    # ---------- 正文 ----------
    last_date = ""
    embedded = 0
    for m in chat.messages:
        # 日期分隔（消息有解析时间时按解析日期；否则沿用上一日期）
        dstr = m.timestamp.strftime("%Y年%m月%d日") if m.timestamp else ""
        if dstr and dstr != last_date:
            p = doc.add_paragraph()
            p.alignment = WD_ALIGN_PARAGRAPH.CENTER
            r = p.add_run(f"■ {dstr}")
            r.font.size = Pt(10)
            r.bold = True
            r.font.color.rgb = DARK
            last_date = dstr

        if m.kind == "voice":
            p = doc.add_paragraph()
            r = p.add_run(f"{_sp(m)} ")
            _fmt_speaker(r)
            p.add_run(f"〔语音 {m.text}〕")
            _fmt_time(p, m)
        elif m.kind == "image":
            p = doc.add_paragraph()
            r = p.add_run(f"{_sp(m)} ")
            _fmt_speaker(r)
            p.add_run("[图片]")
            _fmt_time(p, m)
            if embedded < max_images and m.img_path and Path(m.img_path).exists():
                try:
                    pic = doc.add_paragraph()
                    pic.alignment = WD_ALIGN_PARAGRAPH.CENTER
                    pic.add_run().add_picture(m.img_path, width=Inches(image_width_inch))
                    embedded += 1
                except Exception as e:
                    log.warning("图片嵌入失败 %s: %s", m.img_path, e)
            # HZSMemo 图片OCR附注：图内文字（截图通知/报表内容）入档可检索
            if m.img_path and Path(m.img_path).exists():
                notes = _ocr_image_notes(m.img_path)
                if notes:
                    hd = doc.add_paragraph()
                    r = hd.add_run("图内文字：")
                    r.bold = True
                    r.font.size = Pt(8)
                    r.font.color.rgb = GREEN
                    for ln in notes:
                        lp = doc.add_paragraph()
                        lp.paragraph_format.left_indent = Inches(0.3)
                        r2 = lp.add_run(ln)
                        r2.font.size = Pt(8)
                        r2.font.color.rgb = DARK
        else:  # text
            if _is_ui_noise(m.text):
                continue            # UI 噪声（86.6K / :38 / and）不入档
            p = doc.add_paragraph()
            sp = _sp(m)
            if sp:
                r = p.add_run(f"{sp} ")
                _fmt_speaker(r)
            p.add_run(m.text)
            _fmt_time(p, m)

    out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    doc.save(str(out_path))
    log.info("聊天记录 docx 已生成：%s（嵌入图片 %d/%d）",
             out_path.name, embedded, n_img)
    return out_path


def _speaker(m) -> str:
    if m.speaker:
        return f"【{m.speaker}】"
    if m.side == "right":
        return "【我】"
    return ""


def _fmt_speaker(run) -> None:
    run.bold = True
    run.font.color.rgb = BLUE


def _fmt_time(p, m) -> None:
    if m.timestamp:
        r = p.add_run(f"　{m.timestamp:%H:%M}")
        r.font.size = Pt(8)
        r.font.color.rgb = GRAY
