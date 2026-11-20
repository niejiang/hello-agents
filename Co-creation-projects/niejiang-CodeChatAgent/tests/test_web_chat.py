"""验证网页会话绑定和消息接口。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi.testclient import TestClient
from app.api import web
from app.api.web import app, sessions


class FakeAgent:
    def __init__(self):
        self.history = []

    def run(self, question, **kwargs):
        self.history.append(question)
        return '|'.join(self.history)


class WebChatTests(unittest.TestCase):
    def setUp(self):
        sessions.clear()
        self.client = TestClient(app)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.patcher = patch('app.api.web.create_chat_agent', side_effect=lambda path: FakeAgent())
        self.patcher.start()
        self.addCleanup(self.patcher.stop)

    def test_sessions_remember_and_isolate(self):
        first = self.client.post('/api/sessions', json={'project_path': self.temp.name}).json()
        second = self.client.post('/api/sessions', json={'project_path': self.temp.name}).json()
        def send(session, text):
            return self.client.post(f"/api/sessions/{session['session_id']}/messages", json={'message': text})
        self.assertEqual(send(first, '一').json()['content'][0]['text'], '一')
        self.assertEqual(send(first, '二').json()['content'][0]['text'], '一|二')
        self.assertEqual(send(second, '三').json()['content'][0]['text'], '三')
        self.assertEqual(send(first, '  ').status_code, 422)
        self.assertEqual(self.client.post('/api/sessions/missing/messages', json={'message': 'hi'}).status_code, 404)

    def test_invalid_path(self):
        self.assertEqual(self.client.post('/api/sessions', json={'project_path': '/does/not/exist'}).status_code, 400)

    def test_delete_and_evict_sessions(self):
        created = self.client.post('/api/sessions', json={'project_path': self.temp.name}).json()
        self.assertEqual(self.client.delete(f"/api/sessions/{created['session_id']}").status_code, 204)
        self.assertEqual(self.client.post(f"/api/sessions/{created['session_id']}/messages", json={'message': 'hi'}).status_code, 404)
        with patch.object(web, 'MAX_SESSIONS', 2):
            ids = [self.client.post('/api/sessions', json={'project_path': self.temp.name}).json()['session_id'] for _ in range(2)]
            # 使用第一个会话后，第二个成为最久未使用，新建会话时被淘汰。
            self.client.post(f'/api/sessions/{ids[0]}/messages', json={'message': 'hi'})
            self.client.post('/api/sessions', json={'project_path': self.temp.name})
            self.assertEqual(len(sessions), 2)
            self.assertIn(ids[0], sessions)
            self.assertNotIn(ids[1], sessions)
