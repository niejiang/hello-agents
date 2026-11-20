"""本机 Web 聊天接口：只负责 HTTP 层，会话与问答逻辑在 services 中。"""
import json
import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse, StreamingResponse
from starlette.concurrency import iterate_in_threadpool
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator

from app.projects.project_set import ProjectAccessError
from app.services.chat_service import ProjectPathError, SessionBusyError, open_project
from app.services.session_store import SessionStore

load_dotenv(Path(__file__).resolve().parents[3] / '.env')
logger = logging.getLogger(__name__)
app = FastAPI(title='CodeChatAgent')
app.add_middleware(TrustedHostMiddleware, allowed_hosts=['127.0.0.1', 'localhost', 'testserver'])
store = SessionStore()


@app.middleware('http')
async def check_browser_origin(request: Request, call_next):
    """限制浏览器跨站请求，前端通过同源代理调用接口。"""
    origin = request.headers.get('origin')
    if origin and origin not in {'http://127.0.0.1:5173', 'http://localhost:5173',
                                 'http://127.0.0.1:8018', 'http://localhost:8018'}:
        return JSONResponse(status_code=403, content={'detail': '请求来源不允许'})
    return await call_next(request)


class CreateSessionRequest(BaseModel):
    # 可选：不指定时直接开始对话，项目在对话中按需打开。
    project_path: str | None = Field(default=None, max_length=4096)


class MessageRequest(BaseModel):
    message: str = Field(min_length=1, max_length=16000)

    @field_validator('message')
    @classmethod
    def nonempty(cls, value: str) -> str:
        if not value.strip():
            raise ValueError('问题不能为空')
        return value.strip()


@app.post('/api/sessions')
def create_session(body: CreateSessionRequest | None = None):
    project = None
    if body is not None and body.project_path and body.project_path.strip():
        try:
            project = open_project(body.project_path)
        except ProjectPathError:
            raise HTTPException(status_code=400, detail='项目目录不存在或不可访问')
    try:
        session_id, _ = store.create([project] if project else [])
    except ProjectAccessError as exc:
        # 例如指定了系统目录或主目录本身。
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception:
        logger.exception('创建会话失败')
        raise HTTPException(status_code=500, detail='创建助手失败，请检查服务端配置')
    return {'session_id': session_id, 'project_path': str(project) if project else None}


@app.delete('/api/sessions/{session_id}', status_code=204)
def delete_session(session_id: str):
    """前端切换项目时释放旧会话；会话不存在时同样视为成功。"""
    store.delete(session_id)


def require_session(session_id: str):
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='会话不存在，请重新创建')
    return session


@app.post('/api/sessions/{session_id}/messages')
def send_message(session_id: str, body: MessageRequest):
    session = require_session(session_id)
    try:
        answer = session.ask(body.message)
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))
    except Exception:
        logger.exception('对话请求失败')
        raise HTTPException(status_code=502, detail='模型请求失败，请稍后重试')
    return {'content': [{'type': 'text', 'text': answer}]}


def to_sse(event: dict) -> str:
    return f"event: {event['type']}\ndata: {json.dumps(event, ensure_ascii=False)}\n\n"


@app.post('/api/sessions/{session_id}/messages/stream')
def stream_message(session_id: str, body: MessageRequest):
    """以 SSE 逐条推送回答事件。"""
    session = require_session(session_id)
    try:
        events = session.ask_stream(body.message)
    except SessionBusyError as exc:
        raise HTTPException(status_code=409, detail=str(exc))

    async def body_stream():
        try:
            async for event in iterate_in_threadpool(events):
                yield to_sse(event)
        finally:
            # 客户端断开时，后台线程可能仍在等待模型输出：通知 Agent 尽快结束，结束后会话锁随之释放。
            session.stop()

    return StreamingResponse(body_stream(), media_type='text/event-stream',
                             headers={'Cache-Control': 'no-cache', 'X-Accel-Buffering': 'no'})


@app.post('/api/sessions/{session_id}/stop', status_code=204)
def stop_message(session_id: str):
    """停止会话中正在生成的回答；没有进行中的回答时不产生影响。"""
    require_session(session_id).stop()
