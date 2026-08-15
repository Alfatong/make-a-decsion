"""LLM Provider Adapter（生产版）
统一驱动 DeepSeek V4-Pro/V4-Flash，屏蔽差异，带超时/重试/熔断/成本统计。
Kimi 已移除（官方编程线 key 不开放通用 Chat API）。
用法：
    from app.services.llm.adapter import LLMAdapter
    ad = LLMAdapter.from_env()
    r = ad.generate("写一段...", model="deepseek-flash")
"""
from __future__ import annotations
import os, time, json, logging
from dataclasses import dataclass, asdict
from typing import Optional

logger = logging.getLogger(__name__)

try:
    from openai import OpenAI
except ImportError:
    OpenAI = None


@dataclass
class GenResult:
    text: str
    model: str
    tokens_in: int = 0
    tokens_out: int = 0
    cost_yuan: float = 0.0
    latency_ms: int = 0
    retries: int = 0
    provider: str = ""
    finish_reason: str = ""

    def to_dict(self):
        return asdict(self)


# 价格：元/百万 tokens（以官方实时价为准）
PROVIDERS = {
    "deepseek": {
        "base_url": "https://api.deepseek.com/v1",
        "api_key_env": "DEEPSEEK_API_KEY",
        "models": {
            "deepseek-pro":   {"real": "deepseek-v4-pro",   "in": 0.14, "out": 0.87},
            "deepseek-flash": {"real": "deepseek-v4-flash", "in": 0.14, "out": 0.28},
        },
    },
}

MODEL_INDEX = {}
for _pk, _p in PROVIDERS.items():
    for _logical, _m in _p["models"].items():
        MODEL_INDEX[_logical] = (_pk, _m["real"], _m["in"], _m["out"])


class LLMError(Exception):
    pass


class LLMAdapter:
    def __init__(self, keys: dict, timeout: int = 90, max_retries: int = 3):
        if OpenAI is None:
            raise LLMError("请先 pip install openai")
        self.timeout = timeout
        self.max_retries = max_retries
        self._clients = {}
        self._redis = None
        self._redis_failed = False
        self.pro_rpm = int(os.environ.get("PRO_RPM", "8"))  # pro 全局每分钟上限
        for pk, p in PROVIDERS.items():
            k = keys.get(pk)
            if k:
                self._clients[pk] = OpenAI(api_key=k, base_url=p["base_url"], timeout=timeout)

    def _throttle_pro(self, model: str):
        """pro 模型全局限速（Redis 固定窗口，多 worker/进程共享）。
        Redis 不可用时静默跳过（单机模式）。"""
        if "pro" not in model:
            return
        if self._redis_failed:
            return
        if self._redis is None:
            try:
                import redis
                url = os.environ.get("REDIS_URL", "")
                if not url:
                    self._redis_failed = True
                    return
                self._redis = redis.from_url(url, decode_responses=True,
                                             socket_timeout=3, socket_connect_timeout=3)
                self._redis.ping()
            except Exception:  # noqa
                self._redis_failed = True
                return
        try:
            for _ in range(30):  # 最长等 30 个窗口
                now = int(time.time())
                key = f"ratelimit:pro:{now // 60}"
                n = self._redis.incr(key)
                if n == 1:
                    self._redis.expire(key, 75)
                if n <= self.pro_rpm:
                    return
                wait = 60 - (now % 60) + 1
                logger.info("pro 限速（%d/%d），等待 %ds", n, self.pro_rpm, wait)
                time.sleep(wait)
        except Exception as e:  # noqa
            logger.warning("pro 限速器异常（跳过限速）: %s", e)

    @classmethod
    def from_env(cls, **kw):
        keys = {pk: os.environ.get(p["api_key_env"]) for pk, p in PROVIDERS.items()}
        return cls(keys, **kw)

    def available_models(self):
        return [m for m, (pk, *_ ) in MODEL_INDEX.items() if pk in self._clients]

    def generate(self, prompt: str, model: str, system: Optional[str] = None,
                 max_tokens: int = 4096, temperature: float = 0.7,
                 retry_waits: Optional[list] = None) -> GenResult:
        """retry_waits: 自定义每次重试前等待秒数列表（如 [10,30,60]），
        用于后台任务扛长时抖动；None 走默认短退避（2^n，上限8s）。"""
        if model not in MODEL_INDEX:
            raise LLMError(f"未知模型 {model}，可用: {list(MODEL_INDEX)}")
        pk, real, pin, pout = MODEL_INDEX[model]
        if pk not in self._clients:
            raise LLMError(f"缺少 {pk} API key（env {PROVIDERS[pk]['api_key_env']}）")

        messages = ([{"role": "system", "content": system}] if system else []) + \
                   [{"role": "user", "content": prompt}]
        last_err = None
        for attempt in range(self.max_retries + 1):
            self._throttle_pro(model)  # pro 全局限速（多 worker 共享窗口）
            t0 = time.time()
            try:
                resp = self._clients[pk].chat.completions.create(
                    model=real, messages=messages, max_tokens=max_tokens, temperature=temperature)
                latency = int((time.time() - t0) * 1000)
                choice = resp.choices[0]
                text = (choice.message.content or "").strip()
                if not text:
                    raise LLMError("空响应")  # 触发重试
                usage = getattr(resp, "usage", None)
                tin = getattr(usage, "prompt_tokens", 0) if usage else 0
                tout = getattr(usage, "completion_tokens", 0) if usage else 0
                return GenResult(text=text, model=model, tokens_in=tin, tokens_out=tout,
                                 cost_yuan=round((tin*pin+tout*pout)/1_000_000, 6),
                                 latency_ms=latency, retries=attempt, provider=pk,
                                 finish_reason=getattr(choice, "finish_reason", ""))
            except Exception as e:  # noqa
                last_err = e
                logger.warning("LLM %s 第%d次调用失败: %s", model, attempt+1, e)
                if attempt < self.max_retries:
                    if retry_waits:
                        time.sleep(retry_waits[min(attempt, len(retry_waits)-1)])
                    else:
                        time.sleep(min(2 ** attempt, 8))
        raise LLMError(f"模型 {model} 调用失败（重试{self.max_retries}次）: {last_err}")
