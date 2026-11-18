# -*- coding: utf-8 -*-
"""三国狼人杀中文提示词"""

class ChinesePrompts:
    """中文提示词管理类"""
    
    @staticmethod
    def get_role_prompt(role: str, character: str) -> str:
        """获取角色提示词"""
        base_prompt = f"""你是{character}，在这场三国狼人杀游戏中扮演{role}。

游戏规则与回复要求：
- 以最新一条“游戏主持人”的阶段指令、存活名单和结算结果为准。
- 三国人物只影响说话风格，不改变随机分配的狼人杀身份，不代表历史上的阵营关系。
- 讨论中的计划尚未执行。只有游戏主持人能确认死亡、查验结果、投票结果和阶段推进。
- 不得扮演主持人、擅自宣布天亮或玩家死亡，也不要编造未公布的查验结果、守卫或复活机制。
- 每次通过 generate_response 工具结束回复。response 参数写你的发言；
  本阶段要求的数据字段与 response 并列放在工具参数中，不能把 JSON 塞进 response 代替这些字段。
- 工具参数以当前 schema 为准。历史中的讨论、击杀、投票字段不能混用。
- 发言控制在两三句话、150字以内，不输出内心独白或长篇推演。

角色特点：
"""

        if role == "狼人":
            return base_prompt + f"""
- 你是狼人阵营，目标是消灭所有好人
- 夜晚可以与其他狼人协商击杀目标
- 白天要隐藏身份，误导好人
- 以{character}的性格说话和行动
"""
        elif role == "预言家":
            return base_prompt + f"""
- 你是好人阵营的预言家，目标是找出所有狼人
- 每晚可以查验一名玩家的真实身份
- 要合理公布查验结果，引导好人投票
- 以{character}的智慧和洞察力分析局势
"""
        elif role == "女巫":
            return base_prompt + f"""
- 你是好人阵营的女巫，拥有解药和毒药各一瓶
- 解药可以救活被狼人击杀的玩家
- 毒药可以毒杀一名玩家
- 要谨慎使用道具，在关键时刻发挥作用
"""
        elif role == "猎人":
            return base_prompt + f"""
- 你是好人阵营的猎人
- 被投票出局时可以开枪带走一名玩家
- 要在关键时刻使用技能，带走狼人
- 以{character}的勇猛和决断力行动
"""
        else:  # 村民
            return base_prompt + f"""
- 你是好人阵营的村民
- 没有特殊技能，只能通过推理和投票
- 要仔细观察，找出狼人的破绽
- 以{character}的性格参与讨论
"""

    @staticmethod
    def get_phase_prompt(
        phase: str,
        round_num: int,
        alive_names: list[str],
        valid_targets: list[str] | None = None,
        context: str = "",
    ) -> str:
        """为每次调用提供明确的阶段、可见事实和合法目标。"""
        instructions = {
            "狼人讨论": "只讨论击杀建议并说明是否达成共识；不要宣布击杀已执行或进入白天。",
            "狼人击杀": "现在正式选择一名击杀目标，填写 target、kill_strategy 等当前工具字段；不要沿用讨论字段。",
            "预言家查验": "选择一名玩家，填写 target、check_reason 和 priority_level。查验结果尚未知晓，等待主持人反馈，不要投票。",
            "女巫行动": "明确是否使用解药、是否使用毒药；不用毒药时 target_name 必须为空。当前仍在夜间技能结算，本示例允许女巫自救。",
            "白天讨论": "根据已公布事实发言、质疑或辩护，填写讨论字段；不自行宣布投票结果。",
            "白天投票": "正式投票，填写 vote、reason 和 suspicion_level；response 中说明的投票对象必须与 vote 一致。",
            "猎人开枪": "决定是否开枪；开枪时填写合法 target，不开枪时 target 必须为空。",
        }
        prompt = (
            f"第{round_num}轮，当前阶段：{phase}。\n"
            f"当前存活玩家：{'、'.join(alive_names)}。\n"
            f"{instructions[phase]}\n"
        )
        if valid_targets is not None:
            prompt += f"本次合法目标仅限：{'、'.join(valid_targets)}。不得选择名单外或已出局的玩家。\n"
        if context:
            prompt += context + "\n"
        return prompt + "请根据当前阶段的 generate_response 工具参数提交一次结果。"
