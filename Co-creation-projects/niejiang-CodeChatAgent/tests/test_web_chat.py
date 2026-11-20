"""验证网页会话绑定和消息接口。"""
import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from fastapi.testclient import TestClient
from app.api.web import app, store
from app.projects.authorized import AuthorizedProjects


class FakeAgent:
    def __init__(self):
        self.history = []

    def run_stream(self, question, **kwargs):
        self.history.append(question)
        answer = '|'.join(self.history)
        yield {'type': 'tool_start', 'id': 'c1', 'name': 'list_files', 'arguments': {}}
        yield {'type': 'tool_end', 'id': 'c1', 'name': 'list_files', 'ok': True, 'summary': 'a.py', 'lines': 1,
               'chars': 4, 'truncated': False}
        yield {'type': 'text_delta', 'text': answer}
        yield {'type': 'done', 'answer': answer, 'usage': {}, 'context': {}}


def parse_sse(text):
    events = []
    for block in text.strip().split('\n\n'):
        lines = dict(line.split(': ', 1) for line in block.splitlines())
        events.append((lines['event'], json.loads(lines['data'])))
    return events


class WebChatTests(unittest.TestCase):
    def setUp(self):
        store.clear()
        self.addCleanup(store.clear)
        self.client = TestClient(app)
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        patcher = patch('app.services.chat_service.create_chat_agent', side_effect=lambda projects: FakeAgent())
        patcher.start()
        self.addCleanup(patcher.stop)
        # 测试不写入本机的已授权项目清单。
        authorized = patch('app.services.chat_service.authorized_store', AuthorizedProjects(None))
        authorized.start()
        self.addCleanup(authorized.stop)

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

    def test_session_without_project(self):
        for kwargs in ({}, {'json': {}}, {'json': {'project_path': '  '}}):
            with self.subTest(kwargs=kwargs):
                response = self.client.post('/api/sessions', **kwargs)
                self.assertEqual(response.status_code, 200)
                self.assertIsNone(response.json()['project_path'])
                self.assertEqual(self.send(response.json()['session_id'], '你好').json()['content'][0]['text'], '你好')

    def test_denied_project_path(self):
        response = self.client.post('/api/sessions', json={'project_path': '/'})
        self.assertEqual(response.status_code, 400)
        self.assertIn('系统目录', response.json()['detail'])

    def test_invalid_path(self):
        self.assertEqual(self.client.post('/api/sessions', json={'project_path': '/does/not/exist'}).status_code, 400)

    def test_empty_answer_returns_502(self):
        session_id = self.create()['session_id']
        empty = lambda self, question, **kwargs: iter([{'type': 'done', 'answer': '  ', 'usage': {}, 'context': {}}])
        with patch.object(FakeAgent, 'run_stream', empty):
            self.assertEqual(self.send(session_id, 'hi').status_code, 502)

    def test_stream_endpoint_sends_sse_events(self):
        session_id = self.create()['session_id']
        response = self.client.post(f'/api/sessions/{session_id}/messages/stream', json={'message': '一'})
        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.headers['content-type'].startswith('text/event-stream'))
        events = parse_sse(response.text)
        self.assertEqual([name for name, _ in events], ['start', 'tool_start', 'tool_end', 'text_delta', 'done'])
        self.assertEqual(events[-1][1]['answer'], '一')
        # 流结束后会话可继续使用，并保留历史。
        self.assertEqual(self.send(session_id, '二').json()['content'][0]['text'], '一|二')

    def test_stream_endpoint_rejects_busy_session(self):
        session_id = self.create()['session_id']
        session = store.get(session_id)
        session._lock.acquire()
        try:
            busy = self.client.post(f'/api/sessions/{session_id}/messages/stream', json={'message': 'hi'})
            self.assertEqual(busy.status_code, 409)
            self.assertEqual(self.send(session_id, 'hi').status_code, 409)
        finally:
            session._lock.release()
        missing = self.client.post('/api/sessions/missing/messages/stream', json={'message': 'hi'})
        self.assertEqual(missing.status_code, 404)

    def test_stop_endpoint_stops_running_answer(self):
        session_id = self.create()['session_id']

        def run_stream(agent, question, should_stop, **kwargs):
            yield {'type': 'text_delta', 'text': '部分'}
            # 模拟用户在回答过程中点击停止。
            self.assertEqual(self.client.post(f'/api/sessions/{session_id}/stop').status_code, 204)
            if should_stop():
                yield {'type': 'stopped'}
                return
            yield {'type': 'done', 'answer': '不应到达', 'usage': {}, 'context': {}}

        with patch.object(FakeAgent, 'run_stream', run_stream):
            response = self.client.post(f'/api/sessions/{session_id}/messages/stream', json={'message': '一'})
        self.assertEqual([name for name, _ in parse_sse(response.text)], ['start', 'text_delta', 'stopped'])
        self.assertEqual(self.client.post('/api/sessions/missing/stop').status_code, 404)

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
