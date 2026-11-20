"""本机 Web 聊天接口：只负责 HTTP 层，会话与问答逻辑在 services 中。"""
import logging
from pathlib import Path

from dotenv import load_dotenv
from fastapi import FastAPI, HTTPException, Request
from fastapi.responses import JSONResponse
from fastapi.middleware.trustedhost import TrustedHostMiddleware
from pydantic import BaseModel, Field, field_validator

from app.services.chat_service import ProjectPathError, open_project
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
        project = open_project(body.project_path)
    except ProjectPathError:
        raise HTTPException(status_code=400, detail='项目目录不存在或不可访问')
    try:
        session_id, _ = store.create(project)
    except Exception:
        logger.exception('创建会话失败')
        raise HTTPException(status_code=500, detail='创建助手失败，请检查服务端配置')
    return {'session_id': session_id, 'project_path': str(project)}


@app.delete('/api/sessions/{session_id}', status_code=204)
def delete_session(session_id: str):
    """前端切换项目时释放旧会话；会话不存在时同样视为成功。"""
    store.delete(session_id)


@app.post('/api/sessions/{session_id}/messages')
def send_message(session_id: str, body: MessageRequest):
    session = store.get(session_id)
    if session is None:
        raise HTTPException(status_code=404, detail='会话不存在，请重新创建')
    try:
        answer = session.ask(body.message)
    except Exception:
        logger.exception('对话请求失败')
        raise HTTPException(status_code=502, detail='模型请求失败，请稍后重试')
    return {'content': [{'type': 'text', 'text': answer}]}
