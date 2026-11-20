"""本机 Web 聊天接口；每个会话独立绑定项目与 Agent。"""
import logging
import threading
from collections import OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from uuid import uuid4

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator

from app.agents.chat_agent import create_chat_agent

load_dotenv(Path(__file__).resolve().parents[3] / '.env')
logger = logging.getLogger(__name__)
app = FastAPI(title='CodeChatAgent')
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])


@app.middleware('http')
async def check_browser_origin(request: Request, call_next):
    """限制浏览器跨站请求，前端通过同源代理调用接口。"""
    origin = request.headers.get('origin')
    if origin and origin not in {'http://127.0.0.1:5173', 'http://localhost:5173',
                                 'http://127.0.0.1:8018', 'http://localhost:8018'}:
        return JSONResponse(status_code=403, content={'detail': '请求来源不允许'})
    return await call_next(request)


@dataclass
class Session:
    project_path: Path
    agent: object
    lock: threading.Lock = field(default_factory=threading.Lock)


# 会话只保存在内存中，超过上限时淘汰最久未使用的会话，避免长期运行时内存持续增长。
MAX_SESSIONS = 20
sessions: OrderedDict[str, Session] = OrderedDict()
sessions_lock = threading.Lock()


def get_session(session_id: str) -> Session | None:
    """取出会话并标记为最近使用。"""
    with sessions_lock:
        session = sessions.get(session_id)
        if session is not None:
            sessions.move_to_end(session_id)
        return session


class CreateSessionRequest(BaseModel):
    project_path: str = Field(min_length=1, max_length=4096)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=16000)

    @field_validator('message')
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('问题不能为空')
        return value.strip()


@app.post('/api/sessions')
def create_session(body: CreateSessionRequest):
    try:
        project = Path(body.project_path.strip()).expanduser().resolve(strict=True)
        if not project.is_dir():
            raise ValueError('项目路径必须是目录')
    except (ValueError, OSError, RuntimeError):
        raise HTTPException(status_code=400, detail='项目目录不存在或不可访问')
    try:
        agent = create_chat_agent(project)
    except Exception:
        logger.exception('创建 Agent 失败')
        raise HTTPException(status_code=500, detail='创建助手失败，请检查服务端配置')
    session_id = uuid4().hex
    with sessions_lock:
        sessions[session_id] = Session(project, agent)
        while len(sessions) > MAX_SESSIONS:
            sessions.popitem(last=False)
    return {'session_id': session_id, 'project_path': str(project)}


@app.delete('/api/sessions/{session_id}', status_code=204)
def delete_session(session_id: str):
    """前端切换项目时释放旧会话；会话不存在时同样视为成功。"""
    with sessions_lock:
        sessions.pop(session_id, None)


@app.post('/api/sessions/{session_id}/messages')
def send_message(session_id: str, body: MessageRequest):
    session = get_session(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='会话不存在，请重新创建')
    try:
        with session.lock:
            answer = session.agent.run(body.message, max_tool_iterations=6,
                                       max_tokens=1024, extra_body={'enable_thinking': False})
        if not answer or not answer.strip():
            raise ValueError('模型返回空内容')
    except Exception:
        logger.exception('对话请求失败')
        raise HTTPException(status_code=502, detail='模型请求失败，请稍后重试')
    return {'content': [{'type': 'text', 'text': answer}]}
