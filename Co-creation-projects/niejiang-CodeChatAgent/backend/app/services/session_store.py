"""会话缓存：内存中保留最近使用的会话，超过上限时淘汰最久未使用的；被淘汰或服务重启后按需从对话库恢复。"""

import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import uuid4

from app.services import chat_service
from app.services.chat_service import CodeChatSession

MAX_SESSIONS = 20

Factory = Callable[[Sequence[Path], str], CodeChatSession]
Restorer = Callable[[str], "CodeChatSession | None"]


class SessionStore:
    """线程安全的会话存取。"""

    def __init__(self, max_sessions: int = MAX_SESSIONS, factory: Factory | None = None,
                 restorer: Restorer | None = None) -> None:
        self.max_sessions = max_sessions
        # 默认在调用时读取 chat_service 中的函数，便于测试替换。
        self._factory = factory or (lambda paths, session_id: chat_service.new_session(paths, session_id))
        self._restorer = restorer or (lambda session_id: chat_service.restore_session(session_id))
        self._sessions: OrderedDict[str, CodeChatSession] = OrderedDict()
        self._lock = threading.Lock()

    def _put(self, session_id: str, session: CodeChatSession) -> CodeChatSession:
        with self._lock:
            # 并发恢复同一会话时保留先放入的那个。
            session = self._sessions.setdefault(session_id, session)
            self._sessions.move_to_end(session_id)
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
        return session

    def create(self, project_paths: Sequence[Path] = ()) -> tuple[str, CodeChatSession]:
        """创建会话并返回会话 ID；project_paths 为用户预先指定、直接打开的项目。"""
        session_id = uuid4().hex
        # 创建 Agent 可能较慢，放在锁外执行。
        return session_id, self._put(session_id, self._factory(project_paths, session_id))

    def get(self, session_id: str) -> CodeChatSession | None:
        """取出会话并标记为最近使用；不在内存中时尝试从对话库恢复。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                self._sessions.move_to_end(session_id)
                return session
        restored = self._restorer(session_id)
        return self._put(session_id, restored) if restored is not None else None

    def delete(self, session_id: str) -> None:
        """从内存中移除；不存在时忽略。"""
        with self._lock:
            self._sessions.pop(session_id, None)

    def clear(self) -> None:
        with self._lock:
            self._sessions.clear()

    def __contains__(self, session_id: object) -> bool:
        with self._lock:
            return session_id in self._sessions

    def __len__(self) -> int:
        with self._lock:
            return len(self._sessions)
