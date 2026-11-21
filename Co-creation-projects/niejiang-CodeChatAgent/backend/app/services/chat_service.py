"""对话会话：统一项目校验、运行参数和问答调用，供命令行和网页共用。"""

import logging
import threading
from collections.abc import Callable, Iterator, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from app.agents.chat_agent import Turn, create_chat_agent
from app.projects.authorized import AuthorizedProjects
from app.projects.permissions import PermissionBroker
from app.projects.project_set import ProjectAccessError, ProjectSet
from app.storage.chat_store import ChatStore, StoredTurn

logger = logging.getLogger(__name__)

# 每个问题最多的工具调用轮数，作为防止失控和控制费用的安全阀；上下文增长由 M1/M2 兜底。
# 用完后强制作答，回答标记 limited，用户可回复“继续”接着分析。
MAX_TOOL_ITERATIONS = 60
MAX_ANSWER_TOKENS = 4096
# 已授权项目清单保存在本机，不提交到仓库（见 .gitignore 中的 data/）。
AUTHORIZED_PROJECTS_FILE = Path(__file__).resolve().parents[3] / "data" / "authorized_projects.json"
# 命令行和网页的所有会话共用同一份清单。
authorized_store = AuthorizedProjects(AUTHORIZED_PROJECTS_FILE)
# 网页对话保存在本机 SQLite 中（同样位于 data/，不提交到仓库）；命令行对话不保存。
CHAT_DB_FILE = Path(__file__).resolve().parents[3] / "data" / "chat.db"
chat_store = ChatStore(CHAT_DB_FILE)
# 结束一轮回答的事件；保存时若缺少（如客户端断开），补一个 stopped。
TERMINAL_EVENTS = {"done", "stopped", "error"}

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
    # 模型服务的专有参数（如关闭思考模式）由配置 LLM_EXTRA_BODY 提供，见 create_chat_agent。

    def run_kwargs(self) -> dict[str, Any]:
        return {"max_tool_iterations": self.max_tool_iterations, "max_tokens": self.max_tokens}


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
                 authorized: AuthorizedProjects | None = None, *, conversation_id: str | None = None,
                 store: ChatStore | None = None) -> None:
        """project_paths 为用户预先指定的项目（如命令行 --project），直接打开并加入已授权项目。

        提供 store 和 conversation_id 时，每轮问答结束后保存到该对话。
        """
        self.options = options or ChatOptions()
        self.conversation_id = conversation_id
        self.store = store if conversation_id else None
        self.projects = ProjectSet(authorized if authorized is not None else authorized_store)
        for path in project_paths:
            self.projects.open(str(path), explicit=True)
        self.projects.drain_opened()
        self.permissions = PermissionBroker()
        self.agent = create_chat_agent(self.projects, self.permissions)
        self._lock = threading.Lock()
        self._stop = threading.Event()

    @classmethod
    def restore(cls, conversation_id: str, store: ChatStore, authorized: AuthorizedProjects | None = None,
                options: ChatOptions | None = None) -> "CodeChatSession":
        """恢复已保存的对话：重新打开仍在授权清单中的项目，并恢复模型上下文。"""
        session = cls(options=options, authorized=authorized, conversation_id=conversation_id, store=store)
        # 从最久未使用的开始打开，使最近使用的项目恢复后仍排在最前。
        for path in reversed(store.project_paths(conversation_id)):
            try:
                session.projects.open(path)
            except ProjectAccessError:
                logger.info("恢复对话时跳过未授权或不可用的项目：%s", path)
        session.projects.drain_opened()
        session.agent.restore_turns([Turn(t.question, t.answer, t.tool_summary) for t in store.turns(conversation_id)])
        return session

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

        produced: list[Event] = []
        answered = False

        def emit(event: Event) -> Event:
            produced.append(event)
            return event

        try:
            yield {"type": "start"}
            try:
                for event in self.agent.run_stream(question, should_stop=stop_requested, **self.options.run_kwargs()):
                    if event["type"] == "done" and not event.get("answer", "").strip():
                        yield emit({"type": "error", "code": "empty_answer",
                                    "message": "模型未返回有效回答，请检查模型配置和服务状态"})
                        return
                    answered = answered or event["type"] == "done"
                    yield emit(event)
            except Exception:
                logger.exception("对话请求失败")
                yield emit({"type": "error", "code": "model_error", "message": "模型请求失败，请稍后重试"})
        finally:
            try:
                self._save(question, produced, answered)
            finally:
                self._lock.release()

    def _save(self, question: str, events: list[Event], answered: bool) -> None:
        """保存本轮问答。没有任何内容就出错时不保存（前端会撤回这次提问）。"""
        if self.store is None or not any(event["type"] != "error" for event in events):
            return
        if events[-1]["type"] not in TERMINAL_EVENTS:
            events = [*events, {"type": "stopped"}]
        last = self.agent.turns[-1] if answered and self.agent.turns else None
        turn = StoredTurn(last.question, last.answer, last.tool_summary) if last else None
        try:
            self.store.save_exchange(self.conversation_id, question, events, turn, self._project_paths())
        except Exception:
            logger.exception("保存对话失败")

    def _project_paths(self) -> list[str]:
        return [str(project.root) for project in self.projects.projects()]

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
            if self.store is not None:
                self.store.create(self.conversation_id)
                self.store.save_projects(self.conversation_id, self._project_paths())
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


def new_session(project_paths: Sequence[Path], conversation_id: str) -> CodeChatSession:
    """网页新建对话：保存到本机对话库。"""
    return CodeChatSession(project_paths, conversation_id=conversation_id, store=chat_store)


def restore_session(conversation_id: str) -> CodeChatSession | None:
    """从本机对话库恢复对话；不存在时返回 None。"""
    if not chat_store.exists(conversation_id):
        return None
    return CodeChatSession.restore(conversation_id, chat_store)
