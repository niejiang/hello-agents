"""对话会话：统一项目校验、运行参数和问答调用，供命令行和网页共用。"""

import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.chat_agent import create_chat_agent
from app.projects.authorized import AuthorizedProjects
from app.projects.permissions import PermissionBroker
from app.projects.project_set import ProjectAccessError, ProjectSet

logger = logging.getLogger(__name__)

MAX_TOOL_ITERATIONS = 6
MAX_ANSWER_TOKENS = 4096
# 已授权项目清单保存在本机，不提交到仓库（见 .gitignore 中的 data/）。
AUTHORIZED_PROJECTS_FILE = Path(__file__).resolve().parents[3] / "data" / "authorized_projects.json"
# 命令行和网页的所有会话共用同一份清单。
authorized_store = AuthorizedProjects(AUTHORIZED_PROJECTS_FILE)

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
    """一个会话对应一个 Agent 和一组按需打开的项目；同一会话同时只处理一条消息，以保护对话历史。"""

    def __init__(self, project_paths: Sequence[Path] = (), options: ChatOptions | None = None,
                 authorized: AuthorizedProjects | None = None) -> None:
        """project_paths 为用户预先指定的项目（如命令行 --project），直接打开并加入已授权项目。"""
        self.options = options or ChatOptions()
        self.projects = ProjectSet(authorized if authorized is not None else authorized_store)
        for path in project_paths:
            self.projects.open(str(path), explicit=True)
        self.projects.drain_opened()
        self.permissions = PermissionBroker()
        self.agent = create_chat_agent(self.projects, self.permissions)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    def ask_stream(self, question: str, should_stop: Callable[[], bool] | None = None) -> Iterator[Event]:
        """开始回答并返回事件迭代器；会话忙时立即抛出 SessionBusyError。

        事件依次为 start、若干 text_delta / tool_start / permission_request / permission_resolved / tool_end /
        project_opened / context_trimmed，
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
        self._stop.clear()

        def stop_requested() -> bool:
            return self._stop.is_set() or bool(should_stop and should_stop())

        try:
            yield {"type": "start"}
            try:
                for event in self.agent.run_stream(question, should_stop=stop_requested, **self.options.run_kwargs()):
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

    def project_list(self) -> list[dict[str, Any]]:
        """当前会话已打开的项目，从最近使用开始；已撤销授权的项目不再列出。"""
        self.projects.prune_revoked()
        return [{**project.profile.to_dict(), "alias": project.alias, "recent": index == 0}
                for index, project in enumerate(self.projects.projects())]

    def add_project(self, path: str) -> None:
        """把已授权项目加入当前会话；不产生新的授权。回答进行中时抛出 SessionBusyError。"""
        root = open_project(path)
        if not self._lock.acquire(blocking=False):
            raise SessionBusyError("上一条消息仍在处理中，请稍后再试")
        try:
            if self.projects.authorized.covering(root) is None:
                raise ProjectAccessError("只能加入已授权项目；新项目请在对话中写出路径")
            self.projects.open(str(root))
            self.projects.drain_opened()
        finally:
            self._lock.release()

    def resolve_permission(self, request_id: str, allow: bool) -> bool:
        """记录用户对访问确认的决定；请求不存在或已结束时返回 False。回答进行中也可调用。"""
        return self.permissions.resolve(request_id, allow)

    def stop(self) -> None:
        """请求停止当前回答；Agent 在收到下一段模型输出或执行下一个工具前结束，并产出 stopped 事件。"""
        self._stop.set()

    def ask(self, question: str) -> str:
        """非流式回答：收集事件并返回最终回答。"""
        for event in self.ask_stream(question):
            if event["type"] == "error":
                error = EmptyAnswerError if event.get("code") == "empty_answer" else ChatError
                raise error(event["message"])
            if event["type"] == "done":
                return event["answer"]
        raise ChatError("回答被中断")
