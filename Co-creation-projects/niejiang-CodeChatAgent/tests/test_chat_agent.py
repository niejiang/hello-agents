"""用模拟的流式模型客户端验证工具调用循环、事件和上下文预算，不访问真实模型服务。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

import httpx
import openai

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.agents.chat_agent import ContextBudget, create_chat_agent
from app.projects.authorized import AuthorizedProjects
from app.projects.project_set import ProjectSet

ENV = {
    "LLM_MODEL_ID": "test-model", "LLM_API_KEY": "test-key", "LLM_BASE_URL": "http://127.0.0.1:9/v1",
    "LLM_MAX_INPUT_TOKENS": "991808", "LLM_CONTEXT_BUDGET": "128000", "LLM_EXTRA_BODY": '{"enable_thinking": false}',
}


def chunk(content=None, tool_calls=None):
    delta = SimpleNamespace(content=content, tool_calls=tool_calls)
    return SimpleNamespace(choices=[SimpleNamespace(delta=delta)], usage=None)


def call_delta(index, call_id=None, name=None, arguments=None):
    return SimpleNamespace(index=index, id=call_id, function=SimpleNamespace(name=name, arguments=arguments))


def tool_round(*calls):
    """一轮只包含工具调用：calls 为 (id, name, arguments)。"""
    return [chunk(tool_calls=[call_delta(i, call_id, name, arguments)]) for i, (call_id, name, arguments) in enumerate(calls)]


def usage_chunk(prompt_tokens, completion_tokens=5, cached_tokens=0):
    usage = SimpleNamespace(prompt_tokens=prompt_tokens, completion_tokens=completion_tokens,
                            prompt_tokens_details=SimpleNamespace(cached_tokens=cached_tokens))
    return SimpleNamespace(choices=[], usage=usage)


def overflow_error():
    response = httpx.Response(400, request=httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions"))
    return openai.BadRequestError("Range of input length should be [1, 991808]", response=response, body=None)


class FakeStream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.closed = False

    def __iter__(self):
        # 列表中的异常表示读取到此处时连接中断。
        for item in self.chunks:
            if isinstance(item, Exception):
                raise item
            yield item

    def close(self):
        self.closed = True


class FakeCompletions:
    """按顺序返回预设的流式响应（或抛出预设异常），并记录每次请求。"""

    def __init__(self, rounds):
        self.rounds = list(rounds)
        self.requests = []
        self.streams = []

    def create(self, **kwargs):
        self.requests.append(json.loads(json.dumps(kwargs, ensure_ascii=False, default=str)))
        current = self.rounds.pop(0)
        if isinstance(current, Exception):
            raise current
        stream = FakeStream(current)
        self.streams.append(stream)
        return stream


class ChatAgentTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        root = Path(self.temp.name)
        (root / "app.py").write_text("x = foo(a, b)\nitems[0] = 1\n", encoding="utf-8")
        self.projects = ProjectSet(AuthorizedProjects(None))
        self.alias = self.projects.open(str(root), explicit=True).alias
        self.projects.drain_opened()
        with patch.dict("os.environ", ENV):
            self.agent = create_chat_agent(self.projects)

    def use(self, *rounds):
        completions = FakeCompletions(rounds)
        self.agent.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        return completions

    def events(self, question="问题", iterations=6, should_stop=None):
        return list(self.agent.run_stream(question, max_tool_iterations=iterations, max_tokens=4096,
                                          extra_body={"enable_thinking": False}, should_stop=should_stop))

    @staticmethod
    def tool_messages(request):
        return [m for m in request["messages"] if m["role"] == "tool"]

    def test_streams_text_and_assembles_split_parallel_tool_calls(self):
        """参数分多块返回、两个调用交错到达时，按 index 拼接，特殊字符完整保留。"""
        completions = self.use(
            [
                chunk("我先搜索。"),
                chunk(tool_calls=[call_delta(0, "c1", "search_code", '{"query": "foo')]),
                chunk(tool_calls=[call_delta(1, "c2", "search_code", '{"query":')]),
                chunk(tool_calls=[call_delta(0, None, None, '(a, b)"}')]),
                chunk(tool_calls=[call_delta(1, None, None, ' "items[0]"}')]),
            ],
            [chunk("找到"), chunk("了")],
        )
        events = self.events()
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["text_delta", "tool_start", "tool_end", "tool_start", "tool_end",
                                 "text_delta", "text_delta", "done"])
        self.assertEqual(events[1]["arguments"], {"query": "foo(a, b)"})
        self.assertEqual(events[3]["arguments"], {"query": "items[0]"})
        self.assertTrue(events[2]["ok"])
        self.assertEqual(events[-1]["answer"], "找到了")
        results = self.tool_messages(completions.requests[1])
        self.assertEqual([m["tool_call_id"] for m in results], ["c1", "c2"])
        self.assertIn("app.py:1: x = foo(a, b)", results[0]["content"])
        self.assertIn("app.py:2: items[0] = 1", results[1]["content"])
        self.assertTrue(all(stream.closed for stream in completions.streams))

    def test_request_uses_streaming_strict_schemas_and_options(self):
        completions = self.use([chunk("你好")])
        self.events()
        request = completions.requests[0]
        self.assertTrue(request["stream"])
        self.assertEqual(request["stream_options"], {"include_usage": True})
        schemas = {tool["function"]["name"]: tool["function"]["parameters"] for tool in request["tools"]}
        self.assertEqual(set(schemas), {"open_project", "list_files", "search_code", "find_files", "read_file"})
        self.assertTrue(all(schema["additionalProperties"] is False for schema in schemas.values()))
        self.assertEqual(schemas["read_file"]["properties"]["start"]["minimum"], 1)
        self.assertEqual(request["tool_choice"], "auto")
        self.assertEqual(request["max_tokens"], 4096)
        self.assertEqual(request["extra_body"], {"enable_thinking": False})

    def test_invalid_json_arguments_are_reported_to_model(self):
        completions = self.use(
            tool_round(("c1", "read_file", '{"path": "app.py",')),
            tool_round(("c2", "read_file", '{"path": "app.py", "start": 1, "end": 1}')),
            [chunk("第 1 行是赋值")],
        )
        events = self.events()
        first_end = next(e for e in events if e["type"] == "tool_end")
        self.assertFalse(first_end["ok"])
        self.assertIn("参数不是合法的 JSON", self.tool_messages(completions.requests[1])[0]["content"])
        self.assertIn("1: x = foo(a, b)", self.tool_messages(completions.requests[2])[1]["content"])
        self.assertEqual(events[-1]["answer"], "第 1 行是赋值")

    def test_unknown_or_bad_arguments_return_tool_error(self):
        completions = self.use(
            tool_round(("c1", "read_file", json.dumps({"path": "app.py", "line": 3})),
                       ("c2", "read_file", json.dumps({"path": "../outside"})),
                       ("c3", "no_such_tool", "{}")),
            [chunk("无法读取")],
        )
        ends = [e for e in self.events() if e["type"] == "tool_end"]
        self.assertEqual([e["ok"] for e in ends], [False, False, False])
        contents = [m["content"] for m in self.tool_messages(completions.requests[1])]
        self.assertTrue(all(text.startswith("工具执行失败") for text in contents), contents)

    def test_iteration_limit_forces_final_answer_without_tools(self):
        listing = tool_round(("c1", "list_files", "{}"))
        completions = self.use(listing, listing, [chunk("根据目录给出回答")])
        events = self.events(iterations=2)
        self.assertEqual(events[-1]["answer"], "根据目录给出回答")
        self.assertEqual([r["tool_choice"] for r in completions.requests], ["auto", "auto", "none"])
        # 强制作答的请求末尾说明轮数已用完，该说明不写入历史；回答标记 limited。
        self.assertIn("工具调用次数已用完", completions.requests[2]["messages"][-1]["content"])
        self.assertEqual(events[-1]["limited"], 2)
        self.assertNotIn("工具调用次数已用完", json.dumps(self.agent.turns[-1].to_messages(), ensure_ascii=False))

    def test_wrap_up_reminder_when_few_rounds_remain(self):
        listing = tool_round(("c1", "list_files", "{}"))
        completions = self.use(*[listing] * 5, [chunk("总结")])
        events = self.events(iterations=5)
        self.assertEqual(events[-1]["limited"], 5)
        # 第 1 轮后还剩 4 次，不提醒；第 2 轮后还剩 3 次，开始提醒。
        self.assertNotIn("还剩", self.tool_messages(completions.requests[1])[-1]["content"])
        self.assertIn("还剩 3 次", self.tool_messages(completions.requests[2])[-1]["content"])
        self.assertIn("还剩 1 次", self.tool_messages(completions.requests[4])[-1]["content"])

    def test_answer_without_limit_is_not_marked(self):
        self.use([chunk("直接回答")])
        self.assertNotIn("limited", self.events()[-1])

    def test_pseudo_tool_call_text_is_discarded_after_limit(self):
        listing = tool_round(("c1", "list_files", "{}"))
        self.use(listing, [chunk('[{"function": "list_files", '), chunk('"arguments": {"path": "static/"}}]')])
        events = self.events(iterations=1)
        shown = "".join(e["text"] for e in events if e["type"] == "text_delta")
        self.assertNotIn("function", shown)
        self.assertEqual(events[-1]["answer"], "（工具调用次数已用完，尚未得出结论。）")
        self.assertEqual(shown, events[-1]["answer"])
        # 兜底回答仍写入历史，用户回复“继续”时模型知道查过什么。
        self.assertEqual(self.agent.turns[-1].answer, events[-1]["answer"])
        self.assertIn("列出了", self.agent.turns[-1].tool_summary)

    def test_forced_answer_starting_with_bracket_is_still_shown(self):
        listing = tool_round(("c1", "list_files", "{}"))
        self.use(listing, [chunk("  [README"), chunk("](README.md) 说明了用途")])
        events = self.events(iterations=1)
        shown = "".join(e["text"] for e in events if e["type"] == "text_delta")
        self.assertEqual(shown, "  [README](README.md) 说明了用途")
        self.assertEqual(events[-1]["answer"], shown)

    def test_forced_answer_streams_normally_after_plain_start(self):
        listing = tool_round(("c1", "list_files", "{}"))
        self.use(listing, [chunk("项目"), chunk("结构如下")])
        deltas = [e["text"] for e in self.events(iterations=1) if e["type"] == "text_delta"]
        self.assertEqual(deltas, ["项目", "结构如下"])

    def test_retries_once_when_stream_breaks(self):
        """流式读取中途断开时重发同一请求，已执行的工具不重复执行。"""
        listing = tool_round(("c1", "list_files", "{}"))
        broken = [chunk("部分"), httpx.RemoteProtocolError("peer closed connection")]
        with patch("app.agents.chat_agent.time.sleep") as sleep:
            completions = self.use(listing, broken, [chunk("完整回答")])
            events = self.events()
        kinds = [e["type"] for e in events]
        self.assertEqual(kinds, ["tool_start", "tool_end", "text_delta", "retrying", "text_delta", "done"])
        self.assertEqual(events[-1]["answer"], "完整回答")
        self.assertEqual(completions.requests[1], completions.requests[2])
        self.assertEqual(len(self.tool_messages(completions.requests[2])), 1)
        self.assertTrue(completions.streams[1].closed)
        sleep.assert_called_once()

    def test_retries_connection_error_before_stream(self):
        request = httpx.Request("POST", "http://127.0.0.1:9/v1/chat/completions")
        with patch("app.agents.chat_agent.time.sleep"):
            self.use(openai.APIConnectionError(request=request), [chunk("好")])
            events = self.events()
        self.assertEqual([e["type"] for e in events], ["retrying", "text_delta", "done"])

    def test_gives_up_after_one_retry(self):
        broken = [httpx.RemoteProtocolError("peer closed connection")]
        with patch("app.agents.chat_agent.time.sleep"):
            self.use(broken, broken)
            with self.assertRaises(httpx.RemoteProtocolError):
                self.events()

    def test_no_retry_after_stop(self):
        broken = [httpx.RemoteProtocolError("peer closed connection")]
        with patch("app.agents.chat_agent.time.sleep") as sleep:
            completions = self.use(broken, [chunk("不应请求")])
            events = self.events(should_stop=lambda: True)
        self.assertEqual([e["type"] for e in events], ["stopped"])
        self.assertEqual(len(completions.requests), 1)
        sleep.assert_not_called()

    def test_history_keeps_answer_and_tool_summary(self):
        completions = self.use(
            tool_round(("c1", "read_file", json.dumps({"path": "app.py", "start": 1, "end": 1}))),
            [chunk("第一轮回答")],
            [chunk("第二轮回答")],
        )
        self.events("第一问")
        self.events("第二问")
        messages = completions.requests[2]["messages"]
        self.assertEqual([m["role"] for m in messages], ["system", "user", "assistant", "user"])
        self.assertIn(f"[系统记录：回答本问题时在 {self.alias} 中读取了 app.py:1-1]", messages[1]["content"])
        self.assertEqual(messages[2]["content"], "第一轮回答")
        self.assertTrue(messages[3]["content"].startswith("第二问\n\n[系统记录（自动附加"))

    def test_history_trimmed_to_half_of_budget(self):
        """M1：历史超过工作预算的一半时，整轮删除最早的对话，至少保留最新一轮。"""
        self.agent.context = ContextBudget(budget=200, max_input=991808)
        long_answer = "很长的回答" * 30
        completions = self.use([chunk(long_answer)], [chunk(long_answer)], [chunk("好")])
        self.events("第一问")
        done = self.events("第二问")[-1]
        self.assertEqual(done["context"]["trimmed_turns"], 1)
        self.assertEqual(done["context"]["history_turns"], 1)
        self.events("第三问")
        contents = [m["content"] for m in completions.requests[2]["messages"]]
        self.assertNotIn("第一问", contents)
        self.assertIn("第二问", contents)

    def test_restore_turns_applies_history_limit(self):
        from app.agents.chat_agent import Turn
        self.agent.context = ContextBudget(budget=200, max_input=991808)
        turns = [Turn(f"问{i}", "很长的回答" * 30, "") for i in range(3)]
        self.agent.restore_turns(turns)
        self.assertEqual([t.question for t in self.agent.turns], ["问2"])
        self.agent.context = ContextBudget(budget=128000, max_input=991808)
        self.agent.restore_turns(turns)
        self.assertEqual(len(self.agent.turns), 3)

    def test_usage_is_reported_and_calibrates_estimate(self):
        """M0：读取接口返回的用量，校准字符与 Token 的换算比例。"""
        completions = self.use([chunk("你好"), usage_chunk(500, completion_tokens=7, cached_tokens=120)])
        done = self.events()[-1]
        request = completions.requests[0]
        expected = 500 / ContextBudget.chars({"messages": request["messages"], "tools": request["tools"]})
        self.assertAlmostEqual(self.agent.context.tokens_per_char, expected, places=6)
        self.assertEqual(done["usage"]["last_prompt_tokens"], 500)
        self.assertEqual(done["usage"]["completion_tokens"], 7)
        self.assertEqual(done["usage"]["cached_tokens"], 120)
        self.assertEqual(done["usage"]["requests"], 1)
        self.assertEqual(done["context"]["budget_percent"], round(100 * 500 / 128000, 1))

    def test_context_overflow_drops_history_and_retries_once(self):
        completions = self.use([chunk("第一轮回答")], overflow_error(), [chunk("重试成功")])
        self.events("第一问")
        events = self.events("第二问")
        self.assertIn("context_trimmed", [e["type"] for e in events])
        self.assertEqual(events[-1]["answer"], "重试成功")
        retry = [m["content"] for m in completions.requests[-1]["messages"]]
        self.assertNotIn("第一问", retry)
        self.assertTrue(retry[-1].startswith("第二问"))

    def test_hard_limit_trims_before_request(self):
        self.agent.context = ContextBudget(budget=100, max_input=100)
        completions = self.use([chunk("回答")])
        events = self.events("问题" * 200)
        self.assertEqual(events[0]["type"], "context_trimmed")
        self.assertEqual(len(completions.requests), 1)

    def test_repeated_call_keeps_only_latest_result(self):
        """M2：同一文件的同一行范围被重复读取时，只保留最新一次内容。"""
        read = json.dumps({"path": "app.py", "start": 1, "end": 2})
        completions = self.use(tool_round(("c1", "read_file", read)), tool_round(("c2", "read_file", read)),
                               [chunk("回答")])
        done = self.events()[-1]
        first, second = self.tool_messages(completions.requests[2])
        self.assertTrue(first["content"].startswith(f"[已清理：后文重新执行了相同调用（在 {self.alias} 中读取了 app.py:1-2）"))
        self.assertIn("1: x = foo(a, b)", second["content"])
        self.assertEqual(first["tool_call_id"], "c1")
        self.assertEqual(done["context"]["cleared_tool_results"], 1)

    def test_old_tool_results_cleared_over_60_percent_of_budget(self):
        """M2：本轮占用超过预算 60% 时清理较早的工具结果，保留最近 3 条原文和全部工具调用。"""
        root = Path(self.temp.name)
        (root / "big.py").write_text("".join(f"value_{i} = {'x' * 40}\n" for i in range(500)), encoding="utf-8")
        self.agent.context = ContextBudget(budget=8000, max_input=991808)
        reads = [tool_round((f"c{i}", "read_file", json.dumps({"path": "big.py", "start": i * 100 + 1,
                                                               "end": i * 100 + 100}))) for i in range(5)]
        completions = self.use(*reads, [chunk("回答")])
        events = self.events()
        cleared = [e for e in events if e["type"] == "context_trimmed"]
        self.assertTrue(cleared)
        self.assertTrue(all(e["scope"] == "tool_results" for e in cleared))
        last = completions.requests[-1]["messages"]
        results = self.tool_messages(completions.requests[-1])
        self.assertEqual([r["content"].startswith("[已清理") for r in results], [True, True, False, False, False])
        self.assertIn(f"此前在 {self.alias} 中读取了 big.py:1-100", results[0]["content"])
        self.assertIn("value_499", results[-1]["content"])
        # 工具调用与结果仍一一配对。
        call_ids = [c["id"] for m in last if m["role"] == "assistant" and m.get("tool_calls") for c in m["tool_calls"]]
        self.assertEqual(call_ids, [r["tool_call_id"] for r in results])
        self.assertEqual(events[-1]["context"]["cleared_tool_results"], 2)
        self.assertEqual(self.agent.turns[-1].tool_summary.count("中读取了 big.py"), 5)

    def test_stop_ends_turn_without_recording_history(self):
        completions = self.use([chunk("一"), chunk("二"), chunk("三")])
        seen = []

        def should_stop():
            return len(seen) >= 1

        events = []
        for event in self.agent.run_stream("问题", should_stop=should_stop):
            events.append(event)
            if event["type"] == "text_delta":
                seen.append(event)
        self.assertEqual([e["type"] for e in events], ["text_delta", "stopped"])
        self.assertEqual(self.agent.turns, [])
        self.assertTrue(completions.streams[0].closed)

    def test_without_project_tools_ask_to_open_one(self):
        """未打开项目时工具集不变，文件工具提示先打开项目；用户消息不附加空的项目清单。"""
        with patch.dict("os.environ", ENV):
            agent = create_chat_agent(None)
        self.assertEqual(len(agent._build_tool_schemas()), 5)
        agent.llm._client = self.agent.llm._client
        completions = FakeCompletions([tool_round(("c1", "read_file", json.dumps({"path": "a.py"}))), [chunk("请提供路径")]])
        agent.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        list(agent.run_stream("你好", max_tool_iterations=2))
        self.assertEqual(completions.requests[0]["messages"][-1]["content"], "你好")
        self.assertIn("尚未打开任何项目", self.tool_messages(completions.requests[1])[0]["content"])

    def test_context_settings_are_required(self):
        for override, message in (({"LLM_CONTEXT_BUDGET": ""}, "LLM_CONTEXT_BUDGET"),
                                  ({"LLM_MAX_INPUT_TOKENS": "abc"}, "LLM_MAX_INPUT_TOKENS"),
                                  ({"LLM_CONTEXT_BUDGET": "2000000"}, "不能大于"),
                                  ({"LLM_EXTRA_BODY": ""}, "LLM_EXTRA_BODY"),
                                  ({"LLM_EXTRA_BODY": "[1]"}, "LLM_EXTRA_BODY")):
            with self.subTest(override=override), patch.dict("os.environ", {**ENV, **override}):
                with self.assertRaisesRegex(ValueError, message):
                    create_chat_agent(None)


if __name__ == "__main__":
    unittest.main()
