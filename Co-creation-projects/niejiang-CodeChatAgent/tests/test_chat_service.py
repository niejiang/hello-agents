"""验证会话服务、会话存储和工具结果截断。"""
import sys
import tempfile
import unittest
from pathlib import Path
from unittest.mock import MagicMock, patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'backend'))
from app.services.chat_service import (
    MAX_ANSWER_TOKENS, MAX_TOOL_ITERATIONS, CodeChatSession, EmptyAnswerError, ProjectPathError, SessionBusyError,
    open_project,
)
from app.services.session_store import SessionStore
from app.tools.code_tools import MAX_TOOL_RESULT_CHARS, CodeTool


def done(answer):
    return {'type': 'done', 'answer': answer, 'usage': {}, 'context': {}}


class ChatServiceTests(unittest.TestCase):
    def setUp(self):
        self.agent = MagicMock()
        patcher = patch('app.services.chat_service.create_chat_agent', return_value=self.agent)
        self.create_agent = patcher.start()
        self.addCleanup(patcher.stop)

    def test_open_project(self):
        with tempfile.TemporaryDirectory() as directory:
            self.assertEqual(open_project(f'  {directory}  '), Path(directory).resolve())
            file_path = Path(directory) / 'a.txt'
            file_path.write_text('x', encoding='utf-8')
            for bad in ('/does/not/exist', str(file_path)):
                with self.subTest(path=bad), self.assertRaises(ProjectPathError):
                    open_project(bad)

    def test_ask_passes_run_options(self):
        self.agent.run_stream.return_value = iter([{'type': 'text_delta', 'text': '回答'}, done('回答')])
        session = CodeChatSession(Path('/tmp'))
        self.assertEqual(session.ask('  问题  '), '回答')
        self.create_agent.assert_called_once_with(Path('/tmp'))
        self.agent.run_stream.assert_called_once_with(
            '问题', should_stop=None, max_tool_iterations=MAX_TOOL_ITERATIONS, max_tokens=MAX_ANSWER_TOKENS,
            extra_body={'enable_thinking': False},
        )
        self.assertEqual(MAX_ANSWER_TOKENS, 4096)

    def test_ask_rejects_empty_question_and_answer(self):
        session = CodeChatSession(None)
        with self.assertRaises(ValueError):
            session.ask('   ')
        self.agent.run_stream.return_value = iter([done(' ')])
        with self.assertRaises(EmptyAnswerError):
            session.ask('问题')

    def test_agent_exception_becomes_error_event(self):
        def failing(*args, **kwargs):
            yield {'type': 'text_delta', 'text': '部分'}
            raise RuntimeError('boom')

        self.agent.run_stream.side_effect = failing
        session = CodeChatSession(None)
        with patch('app.services.chat_service.logger'):
            events = list(session.ask_stream('问题'))
        self.assertEqual([e['type'] for e in events], ['start', 'text_delta', 'error'])
        self.assertEqual(events[-1]['code'], 'model_error')

    def test_busy_session_rejects_second_stream_and_releases_lock(self):
        self.agent.run_stream.side_effect = lambda *a, **k: iter([{'type': 'text_delta', 'text': '一'}, done('一')])
        session = CodeChatSession(None)
        first = session.ask_stream('问题')
        self.assertEqual(next(first)['type'], 'start')
        with self.assertRaises(SessionBusyError):
            session.ask_stream('另一个问题')
        first.close()  # 模拟客户端断开
        self.assertEqual(session.ask('再问'), '一')


class SessionStoreTests(unittest.TestCase):
    def test_lru_eviction_and_delete(self):
        store = SessionStore(max_sessions=2, factory=lambda path: object())
        first, _ = store.create(Path('/a'))
        second, _ = store.create(Path('/b'))
        store.get(first)
        third, _ = store.create(Path('/c'))
        self.assertIn(first, store)
        self.assertNotIn(second, store)
        self.assertIn(third, store)
        store.delete(third)
        store.delete('missing')
        self.assertEqual(len(store), 1)
        self.assertIsNone(store.get(third))


class ToolResultLimitTests(unittest.TestCase):
    def test_long_result_is_truncated(self):
        tool = CodeTool('t', '测试', [], lambda: 'x' * (MAX_TOOL_RESULT_CHARS + 10))
        with patch('builtins.print'):
            result = tool.run({})
        self.assertTrue(result.startswith('x' * MAX_TOOL_RESULT_CHARS))
        self.assertIn('结果已截断', result)
        self.assertLess(len(result), MAX_TOOL_RESULT_CHARS + 100)
        short = CodeTool('t', '测试', [], lambda: 'ok')
        with patch('builtins.print'):
            self.assertEqual(short.run({}), 'ok')


if __name__ == '__main__':
    unittest.main()
