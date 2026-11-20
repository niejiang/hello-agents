"""访问确认：模型推断出的路径需要用户确认后才能打开。

Agent 在工具循环中创建请求并推送 permission_request 事件，随后在当前线程等待；
网页通过确认接口、命令行通过终端输入作出决定。等待期间响应停止请求，超时视为拒绝。
"""

import threading
import time
from collections.abc import Callable
from dataclasses import dataclass, field
from uuid import uuid4

PERMISSION_TIMEOUT_SECONDS = 120
POLL_SECONDS = 0.2

ALLOW, DENY, TIMEOUT, STOPPED = "allow", "deny", "timeout", "stopped"


@dataclass
class _Request:
    path: str
    event: threading.Event = field(default_factory=threading.Event)
    decision: str | None = None


class PermissionBroker:
    """一个会话的待确认请求；线程安全。"""

    def __init__(self, timeout: float = PERMISSION_TIMEOUT_SECONDS) -> None:
        self.timeout = timeout
        self._lock = threading.Lock()
        self._requests: dict[str, _Request] = {}

    def create(self, path: str) -> str:
        request_id = uuid4().hex[:12]
        with self._lock:
            self._requests[request_id] = _Request(path)
        return request_id

    def resolve(self, request_id: str, allow: bool) -> bool:
        """记录用户的决定；请求不存在或已结束时返回 False。"""
        with self._lock:
            request = self._requests.get(request_id)
            if request is None or request.decision is not None:
                return False
            request.decision = ALLOW if allow else DENY
            request.event.set()
            return True

    def wait(self, request_id: str, should_stop: Callable[[], bool]) -> str:
        """等待决定，返回 allow、deny、timeout 或 stopped；结束后移除请求。"""
        deadline = time.monotonic() + self.timeout
        with self._lock:
            request = self._requests[request_id]
        try:
            while True:
                if request.event.wait(POLL_SECONDS):
                    return request.decision or DENY
                if should_stop():
                    return STOPPED
                if time.monotonic() >= deadline:
                    return TIMEOUT
        finally:
            with self._lock:
                # 结束后不再接受决定，避免过期的点击生效。
                request.decision = request.decision or "closed"
                self._requests.pop(request_id, None)
