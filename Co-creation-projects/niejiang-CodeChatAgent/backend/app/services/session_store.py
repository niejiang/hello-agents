"""内存会话存储：超过上限时淘汰最久未使用的会话，服务重启后清空。"""

import threading
from collections import OrderedDict
from collections.abc import Callable, Sequence
from pathlib import Path
from uuid import uuid4

from app.services.chat_service import CodeChatSession

MAX_SESSIONS = 20


class SessionStore:
    """线程安全的会话存取。"""

    def __init__(
        self,
        max_sessions: int = MAX_SESSIONS,
        factory: Callable[[Sequence[Path]], CodeChatSession] = CodeChatSession,
    ) -> None:
        self.max_sessions = max_sessions
        self._factory = factory
        self._sessions: OrderedDict[str, CodeChatSession] = OrderedDict()
        self._lock = threading.Lock()

    def create(self, project_paths: Sequence[Path] = ()) -> tuple[str, CodeChatSession]:
        """创建会话并返回会话 ID；project_paths 为用户预先指定、直接打开的项目。"""
        # 创建 Agent 可能较慢，放在锁外执行。
        session = self._factory(project_paths)
        session_id = uuid4().hex
        with self._lock:
            self._sessions[session_id] = session
            while len(self._sessions) > self.max_sessions:
                self._sessions.popitem(last=False)
        return session_id, session

    def get(self, session_id: str) -> CodeChatSession | None:
        """取出会话并标记为最近使用。"""
        with self._lock:
            session = self._sessions.get(session_id)
            if session is not None:
                self._sessions.move_to_end(session_id)
            return session

    def delete(self, session_id: str) -> None:
        """删除会话；不存在时忽略。"""
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
