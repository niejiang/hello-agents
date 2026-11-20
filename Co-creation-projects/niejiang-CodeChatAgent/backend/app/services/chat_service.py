"""代码问答会话：统一项目校验、运行参数和问答调用，供命令行和网页共用。"""

import logging
import threading
from collections.abc import Callable, Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.chat_agent import create_chat_agent

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6
MAX_ANSWER_TOKENS = 4096

Event = dict[str, Any]


class ProjectPathError(ValueError):
    """项目目录不存在、不可访问或不是目录。"""


class ChatError(RuntimeError):
    """问答失败。"""


class EmptyAnswerError(ChatError):
    """模型没有返回有效回答。"""


class SessionBusyError(RuntimeError):
    """同一会话的上一条消息仍在处理中。"""


@dataclass(frozen=True)
class ChatOptions:
    """每次问答传给 Agent 的运行参数。"""

    max_tool_iterations: int = MAX_TOOL_ITERATIONS
    max_tokens: int = MAX_ANSWER_TOKENS
    # 百炼兼容接口参数：关闭思考模式；切换其他模型服务时可能需要调整。
    extra_body: dict[str, Any] = field(default_factory=lambda: {"enable_thinking": False})

    def run_kwargs(self) -> dict[str, Any]:
        return {
            "max_tool_iterations": self.max_tool_iterations,
            "max_tokens": self.max_tokens,
            "extra_body": dict(self.extra_body),
        }


def open_project(path: str | Path) -> Path:
    """解析并校验项目目录，返回绝对路径。"""
    raw = path.strip() if isinstance(path, str) else path
    try:
        project = Path(raw).expanduser().resolve(strict=True)
    except (OSError, RuntimeError) as exc:
        raise ProjectPathError("项目目录不存在或不可访问") from exc
    if not project.is_dir():
        raise ProjectPathError("项目路径必须是目录")
    return project


class CodeChatSession:
    """一个会话绑定一个项目和一个 Agent；同一会话同时只处理一条消息，以保护对话历史。"""

    def __init__(self, project_path: Path | None, options: ChatOptions | None = None) -> None:
        self.project_path = project_path
        self.options = options or ChatOptions()
        self.agent = create_chat_agent(project_path)
        self._lock = threading.Lock()

    def ask_stream(self, question: str, should_stop: Callable[[], bool] | None = None) -> Iterator[Event]:
        """开始回答并返回事件迭代器；会话忙时立即抛出 SessionBusyError。

        事件依次为 start、若干 text_delta / tool_start / tool_end / context_trimmed，
        最后是 done、stopped 或 error 之一。
        """
        question = question.strip()
        if not question:
            raise ValueError("问题不能为空")
        events = self._events(question, should_stop)
        # 先取出 start 事件：此时已获得会话锁，迭代器被关闭或回收时会释放锁。
        first = next(events)
        return self._chain(first, events)

    @staticmethod
    def _chain(first: Event, rest: Iterator[Event]) -> Iterator[Event]:
        try:
            yield first
            yield from rest
        finally:
            rest.close()

    def _events(self, question: str, should_stop: Callable[[], bool] | None) -> Iterator[Event]:
        if not self._lock.acquire(blocking=False):
            raise SessionBusyError("上一条消息仍在处理中，请稍后再试")
        try:
            yield {"type": "start"}
            try:
                for event in self.agent.run_stream(question, should_stop=should_stop, **self.options.run_kwargs()):
                    if event["type"] == "done" and not event.get("answer", "").strip():
                        yield {"type": "error", "code": "empty_answer",
                               "message": "模型未返回有效回答，请检查模型配置和服务状态"}
                        return
                    yield event
            except Exception:
                logger.exception("对话请求失败")
                yield {"type": "error", "code": "model_error", "message": "模型请求失败，请稍后重试"}
        finally:
            self._lock.release()

    def ask(self, question: str) -> str:
        """非流式回答：收集事件并返回最终回答。"""
        for event in self.ask_stream(question):
            if event["type"] == "error":
                error = EmptyAnswerError if event.get("code") == "empty_answer" else ChatError
                raise error(event["message"])
            if event["type"] == "done":
                return event["answer"]
        raise ChatError("回答被中断")
