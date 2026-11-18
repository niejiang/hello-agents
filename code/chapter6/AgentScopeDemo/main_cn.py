# -*- coding: utf-8 -*-
"""
三国狼人杀 - 基于AgentScope的中文版狼人杀游戏
融合三国演义角色和传统狼人杀玩法
"""
import asyncio
import os
import random
from pathlib import Path
from typing import List, Dict, Optional

from dotenv import load_dotenv
from pydantic import BaseModel

from agentscope.agent import ReActAgent
from agentscope.message import Msg
from agentscope.model import OpenAIChatModel
from agentscope.pipeline import MsgHub
from agentscope.formatter import OpenAIMultiAgentFormatter

from prompt_cn import ChinesePrompts
from game_roles import GameRoles
from structured_output_cn import (
    DiscussionModelCN,
    get_vote_model_cn,
    get_witch_model_cn,
    get_seer_model_cn,
    get_hunter_model_cn,
    get_werewolf_kill_model_cn,
)
from utils_cn import (
    check_winning_cn,
    majority_vote_cn,
    get_chinese_name,
    format_player_list,
    GameModerator,
    MAX_GAME_ROUND,
    MAX_DISCUSSION_ROUND,
)


class GameChatModel(OpenAIChatModel):
    """有决策工具时显式调用它，避免把普通 JSON 文本当成工具参数。"""

    async def __call__(self, messages, tools=None, **kwargs):
        if any(
            tool.get("function", {}).get("name") == ReActAgent.finish_function_name
            for tool in tools or []
        ):
            kwargs["tool_choice"] = ReActAgent.finish_function_name
            kwargs["parallel_tool_calls"] = False
        return await super().__call__(messages, tools=tools, **kwargs)


class ThreeKingdomsWerewolfGame:
    """三国狼人杀游戏主类"""
    
    def __init__(self):
        self.players: Dict[str, ReActAgent] = {}
        self.roles: Dict[str, str] = {}
        self.moderator = GameModerator()
        self.alive_players: List[ReActAgent] = []
        self.werewolves: List[ReActAgent] = []
        self.villagers: List[ReActAgent] = []
        self.seer: List[ReActAgent] = []
        self.witch: List[ReActAgent] = []
        self.hunter: List[ReActAgent] = []
        self.current_round = 0
        
        # 女巫道具状态
        self.witch_has_antidote = True
        self.witch_has_poison = True
        
    async def create_player(self, role: str, character: str) -> ReActAgent:
        """创建具有三国背景的玩家"""
        name = get_chinese_name(character)
        self.roles[name] = role
        
        agent = ReActAgent(
            name=name,
            sys_prompt=ChinesePrompts.get_role_prompt(role, character),
            model=GameChatModel(
                model_name=os.environ.get("DASHSCOPE_MODEL", "qwen3.7-plus"),
                api_key=os.environ["DASHSCOPE_API_KEY"],
                client_args={
                    "base_url": os.environ.get(
                        "DASHSCOPE_BASE_URL",
                        "https://dashscope.aliyuncs.com/compatible-mode/v1",
                    ),
                },
                generate_kwargs={"extra_body": {"enable_thinking": False}},
            ),
            formatter=OpenAIMultiAgentFormatter(),
            max_iters=3,
        )
        
        # 角色身份确认
        await agent.observe(
            await self.moderator.announce(
                f"【{name}】你在这场三国狼人杀中扮演{GameRoles.get_role_desc(role)}，"
                f"你的角色是{character}。{GameRoles.get_role_ability(role)}"
            )
        )
        
        self.players[name] = agent
        return agent

    async def _request_action(
        self,
        agent: ReActAgent,
        phase: str,
        structured_model: type[BaseModel] | None = None,
        valid_targets: List[ReActAgent] | None = None,
        context: str = "",
    ) -> Msg:
        """每次调用绑定当前阶段的 schema，并在返回后清理，防止跨阶段残留。"""
        prompt = ChinesePrompts.get_phase_prompt(
            phase,
            self.current_round,
            [player.name for player in self.alive_players],
            [player.name for player in valid_targets] if valid_targets is not None else None,
            context,
        )
        if structured_model is not None:
            prompt += "\n当前数据字段：" + "、".join(structured_model.model_fields)
        agent.toolkit.set_extended_model(agent.finish_function_name, structured_model)
        try:
            reply = await agent(
                Msg("游戏主持人", prompt, "system"),
                structured_model=structured_model,
            )
            if reply is None:
                raise ValueError(f"{agent.name} 在{phase}阶段没有返回结果")
            if structured_model is not None:
                # 达到重试上限后的自然语言总结不能当作有效游戏决策。
                reply.metadata = structured_model.model_validate(reply.metadata).model_dump()
            return reply
        finally:
            agent.toolkit.set_extended_model(agent.finish_function_name, None)
    
    async def setup_game(self, player_count: int = 6):
        """设置游戏"""
        print("🎮 开始设置三国狼人杀游戏...")
        
        # 获取角色配置
        roles = GameRoles.get_standard_setup(player_count)
        characters = random.sample([
            "刘备", "关羽", "张飞", "诸葛亮", "赵云",
            "曹操", "司马懿", "周瑜", "孙权"
        ], player_count)
        
        # 创建玩家
        for i, (role, character) in enumerate(zip(roles, characters)):
            agent = await self.create_player(role, character)
            self.alive_players.append(agent)
            
            # 分配到对应阵营
            if role == "狼人":
                self.werewolves.append(agent)
            elif role == "预言家":
                self.seer.append(agent)
            elif role == "女巫":
                self.witch.append(agent)
            elif role == "猎人":
                self.hunter.append(agent)
            else:
                self.villagers.append(agent)
        
        # 游戏开始公告
        await self.moderator.announce(
            f"三国狼人杀游戏开始！参与者：{format_player_list(self.alive_players)}"
        )
        
        print(f"✅ 游戏设置完成，共{len(self.alive_players)}名玩家")
    
    async def werewolf_phase(self, round_num: int):
        """狼人阶段"""
        if not self.werewolves:
            return None
            
        await self.moderator.announce(f"🐺 狼人请睁眼，选择今晚要击杀的目标...")
        valid_targets = [p for p in self.alive_players if p not in self.werewolves]
        if not valid_targets:
            return None
        
        # 狼人讨论
        async with MsgHub(
            self.werewolves,
            enable_auto_broadcast=True,
            announcement=await self.moderator.announce(
                f"狼人们，请讨论今晚的击杀目标。存活玩家：{format_player_list(self.alive_players)}"
            ),
        ) as werewolves_hub:
            # 讨论阶段
            for _ in range(MAX_DISCUSSION_ROUND):
                for wolf in self.werewolves:
                    await self._request_action(
                        wolf, "狼人讨论", DiscussionModelCN, valid_targets,
                        context=f"狼人同伴：{format_player_list(self.werewolves)}。",
                    )
            
            # 投票击杀
            werewolves_hub.set_auto_broadcast(False)
            await self.moderator.announce("请选择击杀目标")
            kill_model = get_werewolf_kill_model_cn(valid_targets)
            kill_votes = [
                await self._request_action(wolf, "狼人击杀", kill_model, valid_targets)
                for wolf in self.werewolves
            ]
            
            # 统计投票
            votes = {
                wolf.name: vote_msg.metadata["target"]
                for wolf, vote_msg in zip(self.werewolves, kill_votes)
            }
            
            killed_player, _ = majority_vote_cn(votes)
            await werewolves_hub.broadcast(await self.moderator.announce(
                f"狼人击杀投票已确认目标：{killed_player}。死亡结果尚待夜间技能结算。"
            ))
            return killed_player
    
    async def seer_phase(self):
        """预言家阶段"""
        if not self.seer:
            return
            
        seer_agent = self.seer[0]
        await self.moderator.announce("🔮 预言家请睁眼，选择要查验的玩家...")
        valid_targets = [p for p in self.alive_players if p is not seer_agent]
        if not valid_targets:
            return
        
        check_result = await self._request_action(
            seer_agent, "预言家查验", get_seer_model_cn(valid_targets), valid_targets,
        )
        target_name = check_result.metadata["target"]

        target_role = self.roles.get(target_name, "村民")
        
        # 告知预言家结果
        result_msg = f"查验结果：{target_name}是{'狼人' if target_role == '狼人' else '好人'}"
        await seer_agent.observe(await self.moderator.announce(result_msg))
    
    async def witch_phase(self, killed_player: str):
        """女巫阶段"""
        if not self.witch:
            return killed_player, None
            
        witch_agent = self.witch[0]
        await self.moderator.announce("🧙‍♀️ 女巫请睁眼...")
        
        # 告知女巫死亡信息
        death_info = f"今晚{killed_player}被狼人击杀" if killed_player else "今晚平安无事"
        await witch_agent.observe(await self.moderator.announce(death_info))
        
        # 女巫行动
        witch_action = await self._request_action(
            witch_agent,
            "女巫行动",
            get_witch_model_cn(self.alive_players),
            self.alive_players,
            context=(
                f"本夜狼人选择的目标：{killed_player or '无'}，尚未结算死亡。"
                f"解药剩余：{int(self.witch_has_antidote)}瓶；毒药剩余：{int(self.witch_has_poison)}瓶。"
                "没有对应药物时必须选择不使用；没有被刀目标时不能使用解药。"
            ),
        )

        saved_player = None
        poisoned_player = None

        if witch_action.metadata["use_antidote"]:
            if not self.witch_has_antidote or not killed_player:
                raise ValueError("女巫在无解药或无被刀目标时尝试使用解药")
            saved_player = killed_player
            self.witch_has_antidote = False
            await witch_agent.observe(await self.moderator.announce(f"你使用解药救了{killed_player}"))

        if witch_action.metadata["use_poison"]:
            if not self.witch_has_poison:
                raise ValueError("女巫在无毒药时尝试使用毒药")
            poisoned_player = witch_action.metadata["target_name"]
            self.witch_has_poison = False
            await witch_agent.observe(await self.moderator.announce(f"你使用毒药毒杀了{poisoned_player}"))
        
        # 确定最终死亡玩家
        final_killed = killed_player if not saved_player else None
        
        return final_killed, poisoned_player
    
    async def hunter_phase(self, shot_by_hunter: str):
        """猎人阶段"""
        if not self.hunter:
            return None
            
        hunter_agent = self.hunter[0]
        if hunter_agent.name == shot_by_hunter:
            await self.moderator.announce("🏹 猎人发动技能，可以带走一名玩家...")
            
            valid_targets = [p for p in self.alive_players if p is not hunter_agent]
            if not valid_targets:
                return None
            hunter_action = await self._request_action(
                hunter_agent, "猎人开枪", get_hunter_model_cn(valid_targets), valid_targets,
                context="你已被投票淘汰，现在是离场前的开枪机会。",
            )

            if hunter_action.metadata.get("shoot"):
                target = hunter_action.metadata.get("target")
                if target:
                    await self.moderator.announce(f"猎人{hunter_agent.name}开枪带走了{target}")
                    return target
                else:
                    print(f"⚠️ 猎人选择开枪但未指定目标,视为放弃")
                    return None
        
        return None
    
    def update_alive_players(self, dead_players: List[str]):
        """更新存活玩家列表"""
        for dead_name in dead_players:
            if dead_name:
                # 从存活列表移除
                self.alive_players = [p for p in self.alive_players if p.name != dead_name]
                # 从各阵营移除
                self.werewolves = [p for p in self.werewolves if p.name != dead_name]
                self.villagers = [p for p in self.villagers if p.name != dead_name]
                self.seer = [p for p in self.seer if p.name != dead_name]
                self.witch = [p for p in self.witch if p.name != dead_name]
                self.hunter = [p for p in self.hunter if p.name != dead_name]
    
    async def day_phase(self, round_num: int):
        """白天阶段"""
        await self.moderator.day_announcement(round_num)
        
        # 讨论阶段
        async with MsgHub(
            self.alive_players,
            enable_auto_broadcast=True,
            announcement=await self.moderator.announce(
                f"现在开始自由讨论。存活玩家：{format_player_list(self.alive_players)}"
            ),
        ) as all_hub:
            # 每人发言一轮
            for player in self.alive_players:
                await self._request_action(player, "白天讨论", DiscussionModelCN)
            
            # 投票阶段
            all_hub.set_auto_broadcast(False)
            await self.moderator.announce("请投票选择要淘汰的玩家")
            vote_model = get_vote_model_cn(self.alive_players)
            vote_msgs = [
                await self._request_action(
                    player, "白天投票", vote_model, self.alive_players,
                )
                for player in self.alive_players
            ]
            
            # 统计投票
            votes = {
                player.name: vote_msg.metadata["vote"]
                for player, vote_msg in zip(self.alive_players, vote_msgs)
            }
            
            voted_out, vote_count = majority_vote_cn(votes)
            await all_hub.broadcast(
                await self.moderator.vote_result_announcement(voted_out, vote_count)
            )
            
            return voted_out
    
    async def run_game(self):
        """运行游戏主循环"""
        try:
            await self.setup_game()
            
            for round_num in range(1, MAX_GAME_ROUND + 1):
                self.current_round = round_num
                print(f"\n🌙 === 第{round_num}轮游戏开始 ===")
                
                # 夜晚阶段
                await self.moderator.night_announcement(round_num)
                
                # 狼人击杀
                killed_player = await self.werewolf_phase(round_num)
                
                # 预言家查验
                await self.seer_phase()
                
                # 女巫行动
                final_killed, poisoned_player = await self.witch_phase(killed_player)
                
                # 更新死亡玩家
                night_deaths = [p for p in [final_killed, poisoned_player] if p]
                self.update_alive_players(night_deaths)
                
                # 死亡公告
                death_msg = await self.moderator.death_announcement(night_deaths)
                for player in self.alive_players:
                    await player.observe(death_msg)
                
                # 检查胜利条件
                winner = check_winning_cn(self.alive_players, self.roles)
                if winner:
                    await self.moderator.game_over_announcement(winner)
                    return
                
                # 白天阶段
                voted_out = await self.day_phase(round_num)
                
                # 猎人技能
                hunter_shot = await self.hunter_phase(voted_out)
                
                # 更新死亡玩家
                day_deaths = [p for p in [voted_out, hunter_shot] if p]
                self.update_alive_players(day_deaths)
                
                # 检查胜利条件
                winner = check_winning_cn(self.alive_players, self.roles)
                if winner:
                    await self.moderator.game_over_announcement(winner)
                    return
                
                print(f"第{round_num}轮结束，存活玩家：{format_player_list(self.alive_players)}")
        
        except Exception as e:
            print(f"❌ 游戏运行出错：{e}")
            raise


async def main():
    """主函数"""
    load_dotenv(Path(__file__).with_name(".env"))

    # 检查环境变量
    if not os.environ.get("DASHSCOPE_API_KEY", "").strip():
        print("❌ 请在同目录 .env 文件中填写 DASHSCOPE_API_KEY，或设置该环境变量")
        return
    
    print("🎮 欢迎来到三国狼人杀！")
    
    # 创建并运行游戏
    game = ThreeKingdomsWerewolfGame()
    await game.run_game()


if __name__ == "__main__":
    asyncio.run(main())
