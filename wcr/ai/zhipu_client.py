# -*- coding: utf-8 -*-
"""智谱（BigModel）免费大模型客户端：多模型轮换 + 限流退避 + 用量统计。

只用免费模型（glm-4.5-flash / glm-4-flash / glm-4v-flash），杜绝任何收费模型。
API 兼容 OpenAI Chat Completions：
    POST {base_url}/chat/completions   Bearer {api_key}
"""
from __future__ import annotations

import json
import logging
import random
import threading
import time
from dataclasses import dataclass, field
from typing import Optional

log = logging.getLogger("wcr.zhipu")


class ZhipuError(RuntimeError):
    pass


@dataclass
class ModelStat:
    name: str
    calls: int = 0
    ok: int = 0
    fail: int = 0
    prompt_chars: int = 0
    completion_chars: int = 0
    cooldown_until: float = 0.0


@dataclass
class Usage:
    """累计用量（字符口径，粗估 token≈字符/1.6）。"""
    requests: int = 0
    prompt_chars: int = 0
    completion_chars: int = 0

    @property
    def est_tokens(self) -> int:
        return int((self.prompt_chars + self.completion_chars) / 1.6)


class ZhipuClient:
    """线程安全的轮换客户端。

    - text 模型池轮换（round-robin），429/5xx 时该模型进入冷却并切换下一个；
    - 指数退避重试；
    - 绝不调用池外（收费）模型。
    """

    def __init__(self, api_key: str,
                 base_url: str = "https://open.bigmodel.cn/api/paas/v4",
                 text_models: Optional[list[str]] = None,
                 vision_model: str = "glm-4v-flash",
                 max_tokens: int = 4096,
                 temperature: float = 0.3,
                 retry_times: int = 4,
                 retry_backoff: float = 2.0,
                 request_timeout: int = 120,
                 disable_thinking: bool = True):
        if not api_key:
            raise ZhipuError("未配置智谱 API Key（config.ini [ai] api_key 或环境变量 WCR_ZHIPU_API_KEY）")
        self.api_key = api_key
        self.base_url = base_url.rstrip("/")
        self.text_models = [m for m in (text_models or ["glm-4.5-flash"]) if m]
        self.vision_model = vision_model
        self.max_tokens = max_tokens
        self.temperature = temperature
        self.retry_times = retry_times
        self.retry_backoff = retry_backoff
        self.request_timeout = request_timeout
        self.disable_thinking = disable_thinking

        self._rr = 0
        self._lock = threading.Lock()
        self.stats: dict[str, ModelStat] = {
            m: ModelStat(m) for m in set(self.text_models) | {vision_model}
        }
        self.usage = Usage()

    # ------------------------------------------------------------ public
    def chat(self, prompt: str, system: str = "", model: Optional[str] = None,
             temperature: Optional[float] = None,
             max_tokens: Optional[int] = None,
             on_progress=None) -> str:
        """文本对话。model 省略时从免费池轮换。返回助手文本。"""
        say = on_progress or (lambda m: None)
        msgs = []
        if system:
            msgs.append({"role": "system", "content": system})
        msgs.append({"role": "user", "content": prompt})

        last_err: Optional[Exception] = None
        rate_limit_hits = 0
        max_rate_waits = 5   # 限流额外等待预算（分钟级窗口，等 65s 才有意义）
        attempt = 0
        while attempt < self.retry_times:
            m = model or self._pick_text_model()
            stat = self.stats.setdefault(m, ModelStat(m))
            if stat.cooldown_until > time.time() and model is None:
                m = self._pick_text_model(exclude=m)
                stat = self.stats.setdefault(m, ModelStat(m))
            say(f"🤖 {m}（第 {attempt + 1} 次尝试）")
            try:
                resp_text = self._post_chat(m, msgs,
                                            temperature if temperature is not None else self.temperature,
                                            max_tokens or self.max_tokens)
                stat.calls += 1
                stat.ok += 1
                stat.prompt_chars += len(prompt) + len(system)
                stat.completion_chars += len(resp_text)
                with self._lock:
                    self.usage.requests += 1
                    self.usage.prompt_chars += len(prompt) + len(system)
                    self.usage.completion_chars += len(resp_text)
                return resp_text
            except Exception as e:  # 429/5xx/网络
                stat.calls += 1
                stat.fail += 1
                last_err = e
                attempt += 1
                if _is_rate_limit(e):
                    rate_limit_hits += 1
                    stat.cooldown_until = time.time() + 65
                    if rate_limit_hits <= max_rate_waits:
                        # 免费档为分钟级限流：固定等 65s 跨过窗口，不计入普通重试次数
                        wait = 65 + random.uniform(0, 5)
                        attempt -= 1
                    else:
                        wait = self.retry_backoff ** attempt
                    log.warning("模型 %s 限流，等待 %.0fs", m, wait)
                    say(f"   ⏳ {m} 触发免费档限流，等待 {wait:.0f}s 后重试")
                else:
                    wait = self.retry_backoff ** attempt + random.uniform(0, 1)
                    log.warning("模型 %s 失败（%s），%.1fs 后重试", m, e, wait)
                    say(f"   ⚠ {m} 失败：{e}；{wait:.0f}s 后退避重试")
                time.sleep(wait)
        raise ZhipuError(f"调用智谱 API 失败（已重试 {self.retry_times} 次）：{last_err}")

    def vision(self, prompt: str, image_b64: str,
               on_progress=None) -> str:
        """图片理解（glm-4v-flash，免费）。image_b64 为不带前缀的 base64。"""
        msgs = [{
            "role": "user",
            "content": [
                {"type": "image_url",
                 "image_url": {"url": f"data:image/png;base64,{image_b64}"}},
                {"type": "text", "text": prompt},
            ],
        }]
        last_err: Optional[Exception] = None
        for attempt in range(self.retry_times):
            m = self.vision_model
            say = on_progress or (lambda x: None)
            say(f"🖼 {m}（第 {attempt + 1} 次尝试）")
            try:
                return self._post_chat(m, msgs, self.temperature, 1024)
            except Exception as e:
                last_err = e
                time.sleep(self.retry_backoff ** attempt + random.uniform(0, 1))
        raise ZhipuError(f"图片理解失败：{last_err}")

    def report_stats(self) -> str:
        lines = []
        for s in self.stats.values():
            if s.calls:
                lines.append(f"{s.name}: {s.ok}/{s.calls} 成功")
        return "；".join(lines) + f" ｜ 估算 tokens ≈ {self.usage.est_tokens:,}"

    # ------------------------------------------------------------ internals
    def _pick_text_model(self, exclude: Optional[str] = None) -> str:
        pool = [m for m in self.text_models if m != exclude] or self.text_models
        now = time.time()
        with self._lock:
            # 粘性主力：优先选池中未冷却的第一个模型。免费档限流按模型独立计数，
            # 严格轮换会把限额小的模型也打满；主力扛流量、备用救场更稳。
            for m in pool:
                stat = self.stats.get(m)
                if stat is None or stat.cooldown_until <= now:
                    return m
            self._rr = (self._rr + 1) % len(pool)
            return pool[self._rr]

    def _post_chat(self, model: str, msgs: list, temperature: float,
                   max_tokens: int) -> str:
        import requests

        url = f"{self.base_url}/chat/completions"
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json",
        }
        payload = {
            "model": model,
            "messages": msgs,
            "temperature": temperature,
            "max_tokens": max_tokens,
            "stream": False,
        }
        # 混合推理模型（glm-4.5+）：默认关闭深度思考——报告类任务追求稳定输出，
        # 思考链易耗尽 max_tokens 导致 content 为空
        if self.disable_thinking and _is_hybrid_model(model):
            payload["thinking"] = {"type": "disabled"}
        r = requests.post(url, headers=headers, json=payload,
                          timeout=self.request_timeout)
        if r.status_code == 429:
            raise ZhipuError(f"429 限流：{r.text[:200]}")
        if r.status_code >= 500:
            raise ZhipuError(f"{r.status_code} 服务端错误：{r.text[:200]}")
        if r.status_code != 200:
            raise ZhipuError(f"{r.status_code}：{r.text[:300]}")
        data = r.json()
        try:
            choice = data["choices"][0]
            msg = choice["message"]
            content = (msg.get("content") or "").strip()
            # 兜底1：思考型模型可能把答案留在 reasoning_content
            if not content:
                content = (msg.get("reasoning_content") or "").strip()
            # 兜底2：截断 → 视为可重试错误（让模型轮换/加倍退避）
            if not content and choice.get("finish_reason") == "length":
                raise ZhipuError("finish_reason=length 且无内容（token 预算不足）")
            return content
        except (KeyError, IndexError, TypeError) as e:
            raise ZhipuError(f"响应结构异常：{json.dumps(data, ensure_ascii=False)[:300]}") from e


def _is_rate_limit(e: Exception) -> bool:
    return "429" in str(e) or "限流" in str(e)


def _is_hybrid_model(model: str) -> bool:
    """glm-4.5 及以上的 flash 系为混合思考模型（支持 thinking 开关）。"""
    import re as _re
    m = _re.search(r"glm-(\d+)\.(\d+)", model or "")
    if not m:
        return False
    major, minor = int(m.group(1)), int(m.group(2))
    return (major, minor) >= (4, 5)


def parse_json_loose(text: str) -> dict:
    """容错解析模型返回的 JSON（剥代码块围栏 / 找首尾大括号）。"""
    t = (text or "").strip()
    if t.startswith("```"):
        t = t.split("```")[1] if "```" in t[3:] else t[3:]
        if t.startswith("json"):
            t = t[4:]
        t = t.strip()
    try:
        return json.loads(t)
    except json.JSONDecodeError:
        pass
    s, e = t.find("{"), t.rfind("}")
    if s != -1 and e > s:
        try:
            return json.loads(t[s:e + 1])
        except json.JSONDecodeError:
            pass
    return {"_raw": text}
