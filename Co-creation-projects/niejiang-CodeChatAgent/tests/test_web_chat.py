"""验证网页会话绑定和消息接口。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi.testclient import TestClient
from app.api.web import app, store


class FakeAgent:
    def __init__(self):
        self.history = []

    def run(self, question, **kwargs):
        self.history.append(question)
        return '|'.join(self.history)


class WebChatTests(unittest.TestCase):
    def setUp(self):
        store.clear()
        self.addCleanup(store.clear)
        self.client = TestClient(app)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch('app.services.chat_service.create_chat_agent', side_effect=lambda path: FakeAgent())
        patcher.start()
        self.addCleanup(patcher.stop)

    def create(self):
        return self.client.post('/api/sessions', json={'project_path': self.temp.name}).json()

    def send(self, session_id, text):
        return self.client.post(f'/api/sessions/{session_id}/messages', json={'message': text})

    def test_sessions_remember_and_isolate(self):
        first, second = self.create(), self.create()
        self.assertEqual(self.send(first['session_id'], '一').json()['content'][0]['text'], '一')
        self.assertEqual(self.send(first['session_id'], '二').json()['content'][0]['text'], '一|二')
        self.assertEqual(self.send(second['session_id'], '三').json()['content'][0]['text'], '三')
        self.assertEqual(self.send(first['session_id'], '  ').status_code, 422)
        self.assertEqual(self.send('missing', 'hi').status_code, 404)

    def test_invalid_path(self):
        self.assertEqual(self.client.post('/api/sessions', json={'project_path': '/does/not/exist'}).status_code, 400)

    def test_empty_answer_returns_502(self):
        session_id = self.create()['session_id']
        with patch.object(FakeAgent, 'run', return_value='  '):
            self.assertEqual(self.send(session_id, 'hi').status_code, 502)

    def test_delete_and_evict_sessions(self):
        created = self.create()
        self.assertEqual(self.client.delete(f"/api/sessions/{created['session_id']}").status_code, 204)
        self.assertEqual(self.send(created['session_id'], 'hi').status_code, 404)
        with patch.object(store, 'max_sessions', 2):
            ids = [self.create()['session_id'] for _ in range(2)]
            # 使用第一个会话后，第二个成为最久未使用，新建会话时被淘汰。
            self.send(ids[0], 'hi')
            self.create()
            self.assertEqual(len(store), 2)
            self.assertIn(ids[0], store)
            self.assertNotIn(ids[1], store)
