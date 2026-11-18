# -*- coding: utf-8 -*-
"""三国狼人杀游戏的结构化输出模型"""
from typing import List, Literal, Optional
from pydantic import BaseModel, ConfigDict, Field, model_validator
from agentscope.agent import AgentBase


class GameActionModelCN(BaseModel):
    """不同阶段不能混入其他阶段的字段。"""

    model_config = ConfigDict(extra="forbid", str_strip_whitespace=True)


def _target_names(agents: list[AgentBase]) -> tuple[str, ...]:
    names = tuple(agent.name for agent in agents)
    if not names:
        raise ValueError("当前阶段没有合法目标")
    return names


class DiscussionModelCN(GameActionModelCN):
    """中文版讨论输出格式"""
    
    reach_agreement: bool = Field(
        description="是否已达成一致意见",
    )
    confidence_level: int = Field(
        description="对当前推理的信心程度(1-10)",
        ge=1, le=10
    )
    key_evidence: Optional[str] = Field(
        description="支持你观点的关键证据",
        default=None
    )


def get_vote_model_cn(agents: list[AgentBase]) -> type[BaseModel]:
    """获取中文版投票模型"""
    
    names = _target_names(agents)

    class VoteModelCN(GameActionModelCN):
        """中文版投票输出格式"""
        
        vote: Literal[names] = Field(
            description="你要投票淘汰的玩家姓名",
        )
        reason: str = Field(
            description="投票理由，简要说明为什么选择此人",
        )
        suspicion_level: int = Field(
            description="对被投票者的怀疑程度(1-10)",
            ge=1, le=10
        )
    
    return VoteModelCN


class WitchActionModelCN(GameActionModelCN):
    """中文版女巫行动模型"""
    
    use_antidote: bool = Field(
        description="是否使用解药救人",
    )
    use_poison: bool = Field(
        description="是否使用毒药杀人", 
    )
    target_name: Optional[str] = Field(
        description="目标玩家姓名（救人或毒杀的对象）",
        default=None
    )
    action_reason: Optional[str] = Field(
        description="行动理由",
        default=None
    )

    @model_validator(mode="after")
    def validate_poison_target(self):
        if self.use_poison and not self.target_name:
            raise ValueError("使用毒药时必须指定 target_name")
        if not self.use_poison and self.target_name is not None:
            raise ValueError("不使用毒药时 target_name 必须为空")
        return self


def get_witch_model_cn(agents: list[AgentBase]) -> type[BaseModel]:
    """毒药目标只能来自当前存活名单。"""
    names = _target_names(agents)

    class CurrentWitchActionModelCN(WitchActionModelCN):
        target_name: Optional[Literal[names]] = Field(
            default=None,
            description="毒药目标；仅限当前存活玩家，不使用毒药时为空",
        )

    return CurrentWitchActionModelCN


def get_seer_model_cn(agents: list[AgentBase]) -> type[BaseModel]:
    """获取中文版预言家模型"""
    
    names = _target_names(agents)

    class SeerModelCN(GameActionModelCN):
        """中文版预言家查验格式"""
        
        target: Literal[names] = Field(
            description="要查验的玩家姓名",
        )
        check_reason: str = Field(
            description="查验此人的原因",
        )
        priority_level: int = Field(
            description="查验优先级(1-10)",
            ge=1, le=10
        )
    
    return SeerModelCN


def get_hunter_model_cn(agents: list[AgentBase]) -> type[BaseModel]:
    """获取中文版猎人模型"""
    
    names = _target_names(agents)

    class HunterModelCN(GameActionModelCN):
        """中文版猎人开枪格式"""
        
        shoot: bool = Field(
            description="是否使用开枪技能",
        )
        target: Optional[Literal[names]] = Field(
            description="开枪目标玩家姓名",
            default=None
        )
        shoot_reason: Optional[str] = Field(
            description="开枪理由",
            default=None
        )

        @model_validator(mode="after")
        def validate_shot_target(self):
            if self.shoot and not self.target:
                raise ValueError("开枪时必须指定 target")
            if not self.shoot and self.target is not None:
                raise ValueError("不开枪时 target 必须为空")
            return self
    
    return HunterModelCN


class WerewolfKillModelCN(GameActionModelCN):
    """中文版狼人击杀模型"""
    
    target: str = Field(
        description="要击杀的玩家姓名",
    )
    kill_strategy: str = Field(
        description="击杀策略说明",
    )
    team_coordination: Optional[str] = Field(
        description="与狼队友的配合计划",
        default=None
    )


def get_werewolf_kill_model_cn(agents: list[AgentBase]) -> type[BaseModel]:
    """击杀目标只能来自当前存活的非狼人玩家。"""
    names = _target_names(agents)

    class CurrentWerewolfKillModelCN(WerewolfKillModelCN):
        target: Literal[names] = Field(description="本次正式击杀的合法目标")

    return CurrentWerewolfKillModelCN


class GameAnalysisModelCN(BaseModel):
    """中文版游戏分析模型"""
    
    suspected_werewolves: List[str] = Field(
        description="怀疑的狼人名单",
        default_factory=list
    )
    trusted_players: List[str] = Field(
        description="信任的玩家名单", 
        default_factory=list
    )
    key_clues: List[str] = Field(
        description="关键线索列表",
        default_factory=list
    )
    next_strategy: str = Field(
        description="下一步策略",
    )
