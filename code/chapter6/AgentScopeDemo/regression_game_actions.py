"""阶段切换、非法决策和公开结算的离线回归检查，不调用外部模型。"""
import unittest
from copy import deepcopy
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from pydantic import ValidationError
from agentscope.agent import ReActAgent
from agentscope.formatter import OpenAIMultiAgentFormatter
from agentscope.message import Msg, ToolUseBlock
from agentscope.model import ChatModelBase, ChatResponse, OpenAIChatModel

from main_cn import GameChatModel, ThreeKingdomsWerewolfGame
from prompt_cn import ChinesePrompts
from structured_output_cn import (
    DiscussionModelCN,
    get_hunter_model_cn,
    get_seer_model_cn,
    get_vote_model_cn,
    get_werewolf_kill_model_cn,
    get_witch_model_cn,
)


class ScriptedModel(ChatModelBase):
    def __init__(self, replies):
        super().__init__("scripted", stream=False)
        self.replies = iter(replies)
        self.schemas = []

    async def __call__(self, messages, tools=None, **kwargs):
        self.schemas.append(deepcopy(tools))
        return ChatResponse(content=[ToolUseBlock(
            type="tool_use",
            id=f"call-{len(self.schemas)}",
            name="generate_response",
            input=next(self.replies),
        )])


class StageTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.game = ThreeKingdomsWerewolfGame()
        self.game.current_round = 2
        self.game.moderator.disable_console_output()

    def make_agent(self, model):
        agent = ReActAgent(
            "刘备", ChinesePrompts.get_role_prompt("村民", "刘备"),
            model=model, formatter=OpenAIMultiAgentFormatter(), max_iters=1,
        )
        agent.disable_console_output()
        self.game.alive_players = [agent, SimpleNamespace(name="曹操")]
        return agent

    async def test_stage_schema_is_replaced_and_cleared(self):
        discussion = {"response": "证据不足。", "reach_agreement": False, "confidence_level": 2}
        model = ScriptedModel([
            discussion,
            {"response": "我投曹操。", "vote": "曹操", "reason": "前后矛盾", "suspicion_level": 7},
            discussion,
            {"response": "继续讨论。"},
        ])
        agent = self.make_agent(model)
        stages = [
            ("白天讨论", DiscussionModelCN),
            ("白天投票", get_vote_model_cn(self.game.alive_players)),
            ("白天讨论", DiscussionModelCN),
            ("白天讨论", None),
        ]
        for phase, schema in stages:
            reply = await self.game._request_action(agent, phase, schema)
            if schema:
                schema.model_validate(reply.metadata)
            current = agent.toolkit.get_json_schemas()[0]["function"]["parameters"]["properties"]
            self.assertEqual(set(current), {"response"})
        fields = [set(schema[0]["function"]["parameters"]["properties"]) for schema in model.schemas]
        self.assertIn("reach_agreement", fields[0])
        self.assertNotIn("reach_agreement", fields[1])
        self.assertIn("vote", fields[1])
        self.assertNotIn("vote", fields[2])
        self.assertEqual(fields[3], {"response"})

    async def test_invalid_metadata_cannot_become_a_decision(self):
        agent = self.make_agent(ScriptedModel([]))
        bad_reply = Msg("刘备", '{"vote": "曹操"}', "assistant", metadata={})
        with patch.object(agent, "reply", AsyncMock(return_value=bad_reply)):
            with self.assertRaises(ValidationError):
                await self.game._request_action(
                    agent, "白天投票", get_vote_model_cn(self.game.alive_players),
                )
        self.assertEqual(
            set(agent.toolkit.get_json_schemas()[0]["function"]["parameters"]["properties"]),
            {"response"},
        )

    async def test_model_failure_also_cleans_stage_schema(self):
        agent = self.make_agent(ScriptedModel([]))
        with patch.object(agent, "reply", AsyncMock(side_effect=RuntimeError("model unavailable"))):
            with self.assertRaises(RuntimeError):
                await self.game._request_action(agent, "白天讨论", DiscussionModelCN)
        self.assertEqual(
            set(agent.toolkit.get_json_schemas()[0]["function"]["parameters"]["properties"]),
            {"response"},
        )

    async def test_decision_tool_is_forced_only_when_available(self):
        model = GameChatModel("stub", api_key="test")
        try:
            with patch.object(OpenAIChatModel, "__call__", new_callable=AsyncMock) as call:
                await model([], tools=[{"type": "function", "function": {"name": "generate_response"}}])
                self.assertEqual(call.call_args.kwargs["tool_choice"], "generate_response")
                self.assertFalse(call.call_args.kwargs["parallel_tool_calls"])
                await model([])
                self.assertNotIn("tool_choice", call.call_args.kwargs)
        finally:
            await model.client.close()

    async def test_public_death_result_reaches_survivors(self):
        players = [SimpleNamespace(name=name, observe=AsyncMock()) for name in ["狼", "甲", "乙", "丙"]]
        self.game.alive_players = players
        self.game.roles = {p.name: "狼人" if p.name == "狼" else "村民" for p in players}
        self.game.setup_game = AsyncMock()
        self.game.werewolf_phase = AsyncMock(return_value="丙")
        self.game.seer_phase = AsyncMock()
        self.game.witch_phase = AsyncMock(return_value=("丙", None))
        self.game.day_phase = AsyncMock(return_value="狼")
        self.game.hunter_phase = AsyncMock(return_value=None)
        await self.game.run_game()
        for player in players[:3]:
            messages = [call.args[0].get_text_content() for call in player.observe.await_args_list]
            self.assertTrue(any("昨夜，丙不幸遇害" in message for message in messages))
        players[3].observe.assert_not_awaited()


class ActionValidationTests(unittest.TestCase):
    def setUp(self):
        self.players = [SimpleNamespace(name="刘备"), SimpleNamespace(name="关羽")]

    def test_targets_are_limited_to_current_candidates(self):
        cases = [
            (get_werewolf_kill_model_cn, {"target": "刘备", "kill_strategy": "统一目标"}, "target"),
            (get_seer_model_cn, {"target": "刘备", "check_reason": "确认身份", "priority_level": 8}, "target"),
            (get_vote_model_cn, {"vote": "刘备", "reason": "存在矛盾", "suspicion_level": 6}, "vote"),
        ]
        for factory, data, field in cases:
            with self.subTest(factory=factory.__name__):
                schema = factory(self.players)
                schema.model_validate(data)
                with self.assertRaises(ValidationError):
                    schema.model_validate({**data, field: "已出局的张飞"})
                with self.assertRaises(ValidationError):
                    schema.model_validate({**data, "reach_agreement": True})

    def test_witch_requires_explicit_decisions_and_consistent_target(self):
        schema = get_witch_model_cn(self.players)
        schema.model_validate({"use_antidote": True, "use_poison": False})
        schema.model_validate({"use_antidote": False, "use_poison": True, "target_name": "刘备"})
        for invalid in [
            {},
            {"use_antidote": False, "use_poison": True},
            {"use_antidote": False, "use_poison": False, "target_name": "刘备"},
            {"use_antidote": False, "use_poison": True, "target_name": "已出局的张飞"},
        ]:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                schema.model_validate(invalid)

    def test_hunter_target_matches_shoot_decision(self):
        schema = get_hunter_model_cn(self.players)
        schema.model_validate({"shoot": False})
        schema.model_validate({"shoot": True, "target": "刘备"})
        for invalid in [{"shoot": True}, {"shoot": False, "target": "刘备"}]:
            with self.subTest(invalid=invalid), self.assertRaises(ValidationError):
                schema.model_validate(invalid)


if __name__ == "__main__":
    unittest.main()
