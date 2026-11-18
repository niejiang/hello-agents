"""Compatibility fixes for streaming OpenAI-compatible model responses."""

from collections.abc import Iterator
from typing import Any

from hello_agents import HelloAgentsLLM
from hello_agents.core.exceptions import HelloAgentsException


class CompatibleHelloAgentsLLM(HelloAgentsLLM):
    """Skip stream metadata chunks that have no completion choices."""

    def _request_options(self, kwargs: dict[str, Any]) -> dict[str, Any]:
        options = dict(kwargs)
        base_url = (getattr(self, "base_url", "") or "").lower()
        if "qwen" in (self.model or "").lower() and ("aliyuncs.com" in base_url or "dashscope" in base_url):
            options["extra_body"] = {"enable_thinking": False, **(options.get("extra_body") or {})}
        return options

    def invoke(self, messages: list[dict[str, str]], **kwargs: Any) -> str:
        return super().invoke(messages, **self._request_options(kwargs))

    def stream_invoke(self, messages: list[dict[str, str]], **kwargs: Any) -> Iterator[str]:
        try:
            options = self._request_options(kwargs)
            stream = self._client.chat.completions.create(
                model=self.model,
                messages=messages,
                temperature=options.pop("temperature", self.temperature),
                max_tokens=options.pop("max_tokens", self.max_tokens),
                stream=True,
                **options,
            )
            for chunk in stream:
                if chunk.choices:
                    content = chunk.choices[0].delta.content
                    if content:
                        yield content
        except Exception as exc:
            raise HelloAgentsException(f"LLM调用失败: {exc}") from exc
