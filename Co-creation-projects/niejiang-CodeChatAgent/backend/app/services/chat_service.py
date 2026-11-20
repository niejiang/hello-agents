"""代码问答会话：统一项目校验、运行参数和问答调用，供命令行和网页共用。"""

import threading
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from app.agents.chat_agent import create_chat_agent

MAX_TOOL_ITERATIONS = 6
MAX_ANSWER_TOKENS = 4096


class ProjectPathError(ValueError):
    """项目目录不存在、不可访问或不是目录。"""


class EmptyAnswerError(RuntimeError):
    """模型没有返回有效回答。"""


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
    """一个会话绑定一个项目和一个 Agent；同一会话的问答串行执行以保护对话历史。"""

    def __init__(self, project_path: Path | None, options: ChatOptions | None = None) -> None:
        self.project_path = project_path
        self.options = options or ChatOptions()
        self.agent = create_chat_agent(project_path)
        self._lock = threading.Lock()

    def ask(self, question: str) -> str:
        """回答一个问题；模型返回空内容时抛出 EmptyAnswerError。"""
        question = question.strip()
        if not question:
            raise ValueError("问题不能为空")
        with self._lock:
            answer = self.agent.run(question, **self.options.run_kwargs())
        if not answer or not answer.strip():
            raise EmptyAnswerError("模型未返回有效回答，请检查模型配置和服务状态")
        return answer
