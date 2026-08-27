# -*- coding: utf-8 -*-
"""智谱客户端（mock）+ 企业微信通知（mock）+ 统计测试。"""
import json
import unittest
from unittest import mock
from pathlib import Path

from wcr.ai.zhipu_client import (ZhipuClient, ZhipuError, ModelStat,
                                 parse_json_loose)
from wcr.notify.wecom import WeComNotifier
from wcr.models import Chat, Message
from wcr.report.stats import compute_stats, stats_brief, extract_keywords


class TestZhipuClient(unittest.TestCase):
    def _client(self, **kw):
        kw.setdefault("retry_times", 2)
        kw.setdefault("retry_backoff", 0.01)
        return ZhipuClient(api_key="test-key", **kw)

    def test_requires_key(self):
        with self.assertRaises(ZhipuError):
            ZhipuClient(api_key="")

    def test_rotation(self):
        c = self._client(text_models=["glm-4.5-flash", "glm-4-flash-250414"])
        picks = {c._pick_text_model() for _ in range(10)}
        # 粘性主力：无冷却时始终用池中第一个模型（免费档限流下最稳）
        self.assertEqual(picks, {"glm-4.5-flash"})
        # 主力冷却 → 切换到备用
        import time as _t
        stat = c.stats.setdefault("glm-4.5-flash", ModelStat("glm-4.5-flash"))
        stat.cooldown_until = _t.time() + 60
        self.assertEqual(c._pick_text_model(), "glm-4-flash-250414")

    def test_chat_success(self):
        c = self._client()
        fake = mock.Mock(status_code=200)
        fake.json.return_value = {"choices": [{"message": {"content": "你好"}}]}
        with mock.patch("requests.post", return_value=fake) as p:
            out = c.chat("写一句话")
        self.assertEqual(out, "你好")
        args, kwargs = p.call_args
        body = kwargs["json"]
        self.assertIn(body["model"], ("glm-4.5-flash", "glm-4-flash-250414"))
        self.assertEqual(kwargs["headers"]["Authorization"], "Bearer test-key")

    def test_retry_on_500_then_ok(self):
        c = self._client()
        bad = mock.Mock(status_code=500)
        bad.text = "server error"
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"choices": [{"message": {"content": "好"}}]}
        with mock.patch("requests.post", side_effect=[bad, ok]):
            out = c.chat("再试")
        self.assertEqual(out, "好")
        self.assertEqual(c.usage.requests, 1)

    def test_gives_up(self):
        c = self._client(retry_times=2)
        bad = mock.Mock(status_code=500)
        bad.text = "err"
        with mock.patch("requests.post", return_value=bad):
            with self.assertRaises(ZhipuError):
                c.chat("必败")

    def test_parse_json_loose(self):
        self.assertEqual(parse_json_loose('{"a":1}'), {"a": 1})
        self.assertEqual(parse_json_loose('```json\n{"a":2}\n```'), {"a": 2})
        self.assertEqual(parse_json_loose('前置说明 {"a":3} 后缀'), {"a": 3})
        self.assertIn("_raw", parse_json_loose("完全不是JSON"))


class TestWeCom(unittest.TestCase):
    URL = "https://qyapi.weixin.qq.com/cgi-bin/webhook/send?key=TESTKEY"

    def test_requires_webhook(self):
        with self.assertRaises(ValueError):
            WeComNotifier("")

    def test_send_text_ok(self):
        n = WeComNotifier(self.URL)
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"errcode": 0}
        with mock.patch("requests.post", return_value=ok) as p:
            self.assertTrue(n.send_text("任务完成"))
        body = p.call_args.kwargs["json"]
        self.assertEqual(body["msgtype"], "text")
        self.assertEqual(body["text"]["content"], "任务完成")

    def test_send_text_truncated(self):
        n = WeComNotifier(self.URL)
        ok = mock.Mock(status_code=200)
        ok.json.return_value = {"errcode": 0}
        with mock.patch("requests.post", return_value=ok) as p:
            n.send_text("x" * 5000)
        self.assertLessEqual(len(p.call_args.kwargs["json"]["text"]["content"]), 2000)

    def test_key_extract(self):
        n = WeComNotifier(self.URL)
        self.assertEqual(n._key(), "TESTKEY")


class TestStats(unittest.TestCase):
    def _chat(self):
        msgs = [
            Message(kind="text", text="视频监控平台今天完成上线调试，摄像头在线",
                    speaker="李琦", timestamp=None),
            Message(kind="text", text="视频监控平台下午出现花屏问题",
                    speaker="张俊伟"),
            Message(kind="image", img_path="x.png"),
            Message(kind="voice", text="12''"),
        ]
        c = Chat(name="测试群", messages=msgs)
        return [c]

    def test_counts(self):
        s = compute_stats(self._chat())
        self.assertEqual(s["total"], 4)
        self.assertEqual(s["by_kind"]["text"], 2)
        self.assertEqual(s["by_kind"]["image"], 1)
        self.assertEqual(s["by_kind"]["voice"], 1)
        self.assertIn("测试群", s["by_chat"])

    def test_keywords(self):
        kws = extract_keywords("视频监控平台上线 视频监控平台调试 视频监控平台测试", 5)
        self.assertTrue(any(w == "视频" or w == "监控" for w, _ in kws))

    def test_brief(self):
        s = compute_stats(self._chat())
        b = stats_brief(s)
        self.assertIn("消息总量：4 条", b)


if __name__ == "__main__":
    unittest.main()
