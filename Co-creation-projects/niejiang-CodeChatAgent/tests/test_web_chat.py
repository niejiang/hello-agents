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
from app.storage.chat_store import ChatStore
from app.agents.chat_agent import Turn


class FakeAgent:
    """模拟 Agent：回答为历史问题的拼接，并像真实 Agent 一样记录 turns，便于验证恢复。"""

    def __init__(self):
        self.history = []
        self.turns = []

    def restore_turns(self, turns):
        self.turns = list(turns)
        self.history = [turn.question for turn in turns]

    def run_stream(self, question, **kwargs):
        self.history.append(question)
        answer = '|'.join(self.history)
        self.turns.append(Turn(question, answer, ''))
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
        patcher = patch('app.services.chat_service.create_chat_agent', side_effect=lambda projects, permissions=None: FakeAgent())
        patcher.start()
        self.addCleanup(patcher.stop)
        # 测试不写入本机的已授权项目清单。
        authorized = patch('app.services.chat_service.authorized_store', AuthorizedProjects(None))
        authorized.start()
        self.addCleanup(authorized.stop)
        # 测试使用临时对话库，不写入本机 data/chat.db。
        chats = patch('app.services.chat_service.chat_store', ChatStore(Path(self.temp.name) / 'chat.db'))
        chats.start()
        self.addCleanup(chats.stop)

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

    def test_authorized_projects_endpoints(self):
        first = self.create()
        root = first['project_path']
        listed = self.client.get('/api/projects').json()['projects']
        self.assertEqual([p['path'] for p in listed], [root])
        session_projects = self.client.get(f"/api/sessions/{first['session_id']}/projects").json()['projects']
        self.assertEqual([(p['path'], p['recent']) for p in session_projects], [(root, True)])

        # 新对话可以加入已授权项目，但不能借此授权新目录。
        second = self.client.post('/api/sessions').json()['session_id']
        added = self.client.post(f'/api/sessions/{second}/projects', json={'path': root})
        self.assertEqual([p['path'] for p in added.json()['projects']], [root])
        with tempfile.TemporaryDirectory() as other:
            rejected = self.client.post(f'/api/sessions/{second}/projects', json={'path': other})
            self.assertEqual(rejected.status_code, 400)
            self.assertIn('只能加入已授权项目', rejected.json()['detail'])
        self.assertEqual(self.client.post('/api/sessions/missing/projects', json={'path': root}).status_code, 404)

        # 撤销后两个会话都不再列出该项目。
        self.assertEqual(self.client.post('/api/projects/revoke', json={'path': root}).status_code, 204)
        self.assertEqual(self.client.get('/api/projects').json()['projects'], [])
        for session_id in (first['session_id'], second):
            self.assertEqual(self.client.get(f'/api/sessions/{session_id}/projects').json()['projects'], [])

    def test_permission_endpoint(self):
        session_id = self.create()['session_id']
        session = store.get(session_id)
        request_id = session.permissions.create('/x')
        url = f'/api/sessions/{session_id}/permissions/{request_id}'
        self.assertEqual(self.client.post(url, json={'allow': True}).status_code, 204)
        self.assertEqual(session.permissions.wait(request_id, lambda: False), 'allow')
        self.assertEqual(self.client.post(url, json={'allow': False}).status_code, 404)
        self.assertEqual(self.client.post(f'/api/sessions/missing/permissions/{request_id}', json={'allow': True}).status_code, 404)

    def stream(self, session_id, text):
        return parse_sse(self.client.post(f'/api/sessions/{session_id}/messages/stream', json={'message': text}).text)

    def test_conversations_persist_and_restore(self):
        session_id = self.client.post('/api/sessions').json()['session_id']
        self.assertEqual(self.client.get('/api/conversations').json()['conversations'], [])
        self.stream(session_id, '第一问')
        self.send(session_id, '第二问')
        listed = self.client.get('/api/conversations').json()['conversations']
        self.assertEqual([(c['id'], c['title'], c['message_count']) for c in listed], [(session_id, '第一问', 4)])

        store.clear()  # 模拟服务重启：内存中的会话全部丢失
        restored = self.client.get(f'/api/sessions/{session_id}/messages').json()
        self.assertEqual(restored['title'], '第一问')
        self.assertEqual([m['role'] for m in restored['messages']], ['user', 'assistant', 'user', 'assistant'])
        events = restored['messages'][1]['events']
        self.assertEqual([e['type'] for e in events], ['tool_start', 'tool_end', 'text_delta', 'done'])
        # 模型上下文同样恢复：回答包含之前的问题。
        self.assertEqual(self.send(session_id, '第三问').json()['content'][0]['text'], '第一问|第二问|第三问')

        self.assertEqual(self.client.patch(f'/api/sessions/{session_id}', json={'title': '改名'}).status_code, 204)
        self.assertEqual(self.client.get('/api/conversations').json()['conversations'][0]['title'], '改名')
        self.assertEqual(self.client.patch('/api/sessions/missing', json={'title': 'x'}).status_code, 404)
        self.assertEqual(self.client.patch(f'/api/sessions/{session_id}', json={'title': '  '}).status_code, 422)

        self.assertEqual(self.client.delete(f'/api/sessions/{session_id}').status_code, 204)
        self.assertEqual(self.client.get('/api/conversations').json()['conversations'], [])
        store.clear()
        self.assertEqual(self.client.get(f'/api/sessions/{session_id}/messages').status_code, 404)

    def test_failed_first_answer_is_not_saved(self):
        session_id = self.client.post('/api/sessions').json()['session_id']
        empty = lambda self, question, **kwargs: iter([{'type': 'done', 'answer': '  ', 'usage': {}, 'context': {}}])
        with patch.object(FakeAgent, 'run_stream', empty):
            self.assertEqual([name for name, _ in self.stream(session_id, '问')], ['start', 'error'])
        self.assertEqual(self.client.get('/api/conversations').json()['conversations'], [])

    def test_restore_reopens_only_authorized_projects(self):
        with tempfile.TemporaryDirectory() as other:
            first = self.create()
            second = self.client.post('/api/sessions', json={'project_path': other}).json()
            for item in (first, second):
                self.stream(item['session_id'], '看看')
            self.assertEqual(self.client.post('/api/projects/revoke', json={'path': second['project_path']}).status_code, 204)
            store.clear()
            restored = self.client.get(f"/api/sessions/{first['session_id']}/messages").json()
            self.assertEqual([p['path'] for p in restored['projects']], [first['project_path']])
            revoked = self.client.get(f"/api/sessions/{second['session_id']}/messages").json()
            self.assertEqual(revoked['projects'], [])
            self.assertEqual(len(revoked['messages']), 2)

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
