"""
DeepSeek API 封装 — OpenAI 兼容接口（带重试/退避/超时）
"""

import json
import time
from openai import APIConnectionError, APIStatusError, APITimeoutError, OpenAI
from config import DEEPSEEK_API_KEY, DEEPSEEK_BASE_URL, DEEPSEEK_MODEL

# 请求超时（秒）与重试策略
REQUEST_TIMEOUT = 180
MAX_RETRIES = 4
RETRY_BASE_DELAY = 5.0  # 首次重试前等待秒数，指数递增（5/10/20s）
MAX_TOKENS_CEILING = 32768


class ReasoningBudgetError(Exception):
    """推理型模型的思考过程耗尽了 max_tokens，正文为空（finish_reason=length）。
    可通过放大 max_tokens 重试恢复。"""


class APIClient:
    """封装 DeepSeek API 调用"""

    def __init__(self, api_key: str = None, model: str = None,
                 base_url: str = None, timeout: int = REQUEST_TIMEOUT):
        self.api_key = api_key or DEEPSEEK_API_KEY
        self.model = model or DEEPSEEK_MODEL
        self.timeout = timeout
        self.client = OpenAI(
            api_key=self.api_key,
            base_url=base_url or DEEPSEEK_BASE_URL,
            timeout=timeout,
        )

    @staticmethod
    def _is_retryable(err: Exception) -> bool:
        """连接失败/超时/限流/服务端错误可重试；鉴权错误、参数错误不重试"""
        if isinstance(err, (APIConnectionError, APITimeoutError)):
            return True
        if isinstance(err, APIStatusError):
            return err.status_code == 429 or err.status_code >= 500
        # openai>=1.x 的 RateLimitError 继承自 APIStatusError，双保险
        return type(err).__name__ in ("RateLimitError", "InternalServerError",
                                      "ServiceUnavailableError", "APIError")

    def chat(
        self,
        system_prompt: str,
        user_prompt: str,
        temperature: float = 0.7,
        max_tokens: int = 8192,
        json_mode: bool = False,
    ) -> str:
        """
        发送 Chat Completion 请求，返回文本响应。
        可重试错误（超时/限流/5xx）自动指数退避重试，最多 MAX_RETRIES 次。
        """
        messages = [
            {"role": "system", "content": system_prompt},
            {"role": "user", "content": user_prompt},
        ]

        kwargs = {
            "model": self.model,
            "messages": messages,
            "temperature": temperature,
            "max_tokens": max_tokens,
        }

        # DeepSeek 支持 JSON mode（beta），通过 response_format 参数
        if json_mode:
            kwargs["response_format"] = {"type": "json_object"}

        last_err = None
        for attempt in range(MAX_RETRIES):
            try:
                response = self.client.chat.completions.create(**kwargs)
                content = response.choices[0].message.content
                finish = response.choices[0].finish_reason
                if not (content or "").strip() and finish == "length":
                    raise ReasoningBudgetError(
                        f"max_tokens={kwargs['max_tokens']} 被推理过程耗尽，正文为空")
                return content.strip() if content else ""
            except ReasoningBudgetError as e:
                last_err = e
                # 放大输出预算后立即重试（不耗时长退避）
                if kwargs["max_tokens"] < MAX_TOKENS_CEILING and attempt < MAX_RETRIES - 1:
                    kwargs["max_tokens"] = min(kwargs["max_tokens"] * 4, MAX_TOKENS_CEILING)
                    print(f"\n[推理耗尽输出预算，放大至 max_tokens={kwargs['max_tokens']} 重试]")
                    continue
                break
            except Exception as e:
                last_err = e
                if attempt < MAX_RETRIES - 1 and self._is_retryable(e):
                    delay = RETRY_BASE_DELAY * (2 ** attempt)
                    print(f"\n[API 调用失败，{delay:.0f}s 后重试 {attempt + 1}/{MAX_RETRIES - 1}] {e}")
                    time.sleep(delay)
                else:
                    break
        print(f"\n[API 调用失败（重试耗尽）] {last_err}")
        raise last_err

    def chat_with_json_output(self, system_prompt: str, user_prompt: str,
                               temperature: float = 0.3, max_tokens: int = 16384) -> dict:
        """
        发送请求并尝试将响应解析为 JSON dict。
        JSON mode 失败或解析失败时，自动降级为普通模式重试一次。
        """
        # 优先尝试 JSON mode
        try:
            raw = self.chat(system_prompt, user_prompt,
                           temperature=temperature, max_tokens=max_tokens, json_mode=True)
            result = self._extract_json(raw)
            if "_parse_error" not in result:
                return result
        except Exception:
            pass
        # 降级重试：普通模式 + 手动提取（长 JSON 偶发截断/格式错误时多一次机会）
        raw = self.chat(system_prompt, user_prompt,
                       temperature=temperature, max_tokens=max_tokens, json_mode=False)
        return self._extract_json(raw)

    def _extract_json(self, text: str) -> dict:
        """从响应中提取 JSON 对象"""
        text = text.strip()
        # 尝试直接解析
        try:
            return json.loads(text)
        except json.JSONDecodeError:
            pass
        # 尝试提取 ```json ... ``` 块
        if "```json" in text:
            start = text.find("```json") + 7
            end = text.find("```", start)
            if end > start:
                try:
                    return json.loads(text[start:end].strip())
                except json.JSONDecodeError:
                    pass
        # 尝试提取 { ... } 块
        brace_start = text.find("{")
        brace_end = text.rfind("}")
        if brace_start >= 0 and brace_end > brace_start:
            try:
                return json.loads(text[brace_start:brace_end + 1])
            except json.JSONDecodeError:
                pass
        # 返回空字典 + 原始文本
        return {"_raw": text, "_parse_error": True}


# 全局单例
_client_instance: APIClient | None = None


def get_client() -> APIClient:
    global _client_instance
    if _client_instance is None:
        _client_instance = APIClient()
    return _client_instance


def reset_client():
    """丢弃单例（API key 变更后重建）"""
    global _client_instance
    _client_instance = None
