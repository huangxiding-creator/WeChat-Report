# -*- coding: utf-8 -*-
"""企业微信群机器人通知：文本 + 文件（upload_media → msgtype=file）。"""
from __future__ import annotations

import logging
from pathlib import Path
from typing import Optional

log = logging.getLogger("wcr.wecom")


class WeComNotifier:
    """webhook 推送。

    用法：
        n = WeComNotifier("https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=xxx")
        n.send_text("任务完成")
        n.send_file(Path("报告.docx"))
    """

    MAX_FILE_MB = 20

    def __init__(self, webhook: str, timeout: int = 30):
        if not webhook:
            raise ValueError("企业微信 webhook 未配置（config.ini [notify] webhook）")
        self.webhook = webhook
        self.timeout = timeout

    # ------------------------------------------------------------ text
    def send_text(self, text: str, mentioned: bool = False) -> bool:
        import requests

        payload = {
            "msgtype": "text",
            "text": {
                "content": text[:2000],  # 企业微信上限 2048 字节，保守截断
                "mentioned_list": ["@all"] if mentioned else [],
            },
        }
        try:
            r = requests.post(self.webhook, json=payload, timeout=self.timeout)
            data = r.json()
            if data.get("errcode") == 0:
                return True
            log.error("企业微信发送失败：%s", data)
        except Exception as e:
            log.error("企业微信请求异常：%s", e)
        return False

    def send_markdown(self, md: str) -> bool:
        import requests

        payload = {"msgtype": "markdown", "markdown": {"content": md[:4000]}}
        try:
            r = requests.post(self.webhook, json=payload, timeout=self.timeout)
            data = r.json()
            return data.get("errcode") == 0
        except Exception as e:
            log.error("企业微信请求异常：%s", e)
            return False

    # ------------------------------------------------------------ file
    def send_file(self, path: Path) -> bool:
        import requests

        path = Path(path)
        if not path.exists():
            log.error("文件不存在：%s", path)
            return False
        if path.stat().st_size > self.MAX_FILE_MB * 1024 * 1024:
            log.error("文件超过 %dMB 上限：%s", self.MAX_FILE_MB, path)
            return False

        key = self._key()
        if not key:
            return False
        upload_url = ("https://qyapi.weixin.qq.com/cgi-bin/webhook/upload_media"
                      f"?key={key}&type=file")
        try:
            with open(path, "rb") as f:
                r = requests.post(upload_url,
                                  files={"media": (path.name, f)},
                                  timeout=max(self.timeout, 120))
            data = r.json()
            media_id = data.get("media_id")
            if not media_id:
                log.error("上传失败：%s", data)
                return False
            payload = {"msgtype": "file", "file": {"media_id": media_id}}
            r2 = requests.post(self.webhook, json=payload, timeout=self.timeout)
            ok = r2.json().get("errcode") == 0
            if ok:
                log.info("已推送文件：%s", path.name)
            return ok
        except Exception as e:
            log.error("文件推送异常：%s", e)
            return False

    # ------------------------------------------------------------ internals
    def _key(self) -> Optional[str]:
        if "key=" in self.webhook:
            return self.webhook.split("key=")[-1].split("&")[0].strip()
        log.error("webhook 中无 key 参数")
        return None
