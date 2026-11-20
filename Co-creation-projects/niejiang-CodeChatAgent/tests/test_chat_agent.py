"""用模拟的模型客户端验证原生工具调用流程，不访问真实模型服务。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.agents.chat_agent import create_chat_agent

ENV = {"LLM_MODEL_ID": "test-model", "LLM_API_KEY": "test-key", "LLM_BASE_URL": "http://127.0.0.1:9/v1"}


def tool_call(call_id, name, arguments):
    return SimpleNamespace(id=call_id, type="function", function=SimpleNamespace(name=name, arguments=arguments))


def reply(content="", tool_calls=None):
    message = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(message=message)])


class FakeCompletions:
    """按顺序返回预设响应，并记录每次请求的参数。"""

    def __init__(self, responses):
        self.responses = list(responses)
        self.requests = []

    def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, default=str)))
        return self.responses.pop(0)


class ChatAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "app.py").write_text("x = foo(a, b)\nitems[0] = 1\n", encoding="utf-8")
        with patch.dict("os.environ", ENV):
            self.agent = create_chat_agent(root)

    def use(self, *responses):
        completions = FakeCompletions(responses)
        self.agent.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        return completions

    def run_agent(self, question="问题", **kwargs):
        with patch("builtins.print"):
            return self.agent.run(question, max_tool_iterations=kwargs.pop("iterations", 6),
                                  max_tokens=1024, extra_body={"enable_thinking": False})

    def tool_messages(self, request):
        return [m for m in request["messages"] if m["role"] == "tool"]

    def test_arguments_with_commas_and_brackets_reach_tool_intact(self):
        """文字协议会把 foo(a, b) 截成 foo(a；原生调用应完整传入。"""
        completions = self.use(
            reply(tool_calls=[
                tool_call("c1", "search_code", json.dumps({"query": "foo(a, b)"})),
                tool_call("c2", "search_code", json.dumps({"query": "items[0]"})),
            ]),
            reply("找到了"),
        )
        self.assertEqual(self.run_agent(), "找到了")
        results = self.tool_messages(completions.requests[1])
        self.assertEqual([m["tool_call_id"] for m in results], ["c1", "c2"])
        self.assertIn("app.py:1: x = foo(a, b)", results[0]["content"])
        self.assertIn("app.py:2: items[0] = 1", results[1]["content"])

    def test_request_uses_strict_schemas_and_passes_options(self):
        completions = self.use(reply("你好"))
        self.run_agent()
        request = completions.requests[0]
        schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in request["tools"]}
        self.assertEqual(set(schemas), {"list_files", "search_code", "read_file"})
        for schema in schemas.values():
            self.assertFalse(schema["additionalProperties"])
        self.assertEqual(schemas["read_file"]["required"], ["path"])
        self.assertEqual(schemas["read_file"]["properties"]["start"]["minimum"], 1)
        self.assertEqual(request["tool_choice"], "auto")
        self.assertEqual(request["max_tokens"], 1024)
        self.assertEqual(request["extra_body"], {"enable_thinking": False})

    def test_invalid_json_arguments_are_reported_to_model(self):
        completions = self.use(
            reply(tool_calls=[tool_call("c1", "read_file", '{"path": "app.py",')]),
            reply(tool_calls=[tool_call("c2", "read_file", '{"path": "app.py", "start": 1, "end": 1}')]),
            reply("第 1 行是赋值"),
        )
        self.assertEqual(self.run_agent(), "第 1 行是赋值")
        first = self.tool_messages(completions.requests[1])[0]["content"]
        self.assertIn("参数不是合法的 JSON", first)
        second = self.tool_messages(completions.requests[2])[1]["content"]
        self.assertIn("1: x = foo(a, b)", second)

    def test_unknown_or_bad_arguments_return_tool_error(self):
        completions = self.use(
            reply(tool_calls=[
                tool_call("c1", "read_file", json.dumps({"path": "app.py", "line": 3})),
                tool_call("c2", "read_file", json.dumps({"path": "../outside"})),
            ]),
            reply("无法读取"),
        )
        self.run_agent()
        results = [m["content"] for m in self.tool_messages(completions.requests[1])]
        self.assertTrue(all(text.startswith("工具执行失败") for text in results), results)

    def test_iteration_limit_forces_final_answer_without_tools(self):
        call = reply(tool_calls=[tool_call("c1", "list_files", "{}")])
        completions = self.use(call, call, reply("根据目录给出回答"))
        self.assertEqual(self.run_agent(iterations=2), "根据目录给出回答")
        self.assertEqual(completions.requests[-1]["tool_choice"], "none")

    def test_history_kept_across_turns(self):
        completions = self.use(reply("第一轮回答"), reply("第二轮回答"))
        self.run_agent("第一问")
        self.run_agent("第二问")
        contents = [m["content"] for m in completions.requests[1]["messages"]]
        self.assertIn("第一问", contents)
        self.assertIn("第一轮回答", contents)
        self.assertEqual(contents[-1], "第二问")

    def test_without_project_has_no_tools(self):
        with patch.dict("os.environ", ENV):
            agent = create_chat_agent(None)
        self.assertEqual(agent._build_tool_schemas(), [])


if __name__ == "__main__":
    unittest.main()
