"""第七章最小体验：加载配置、创建 Agent、对话、查看历史。"""

import os
import sys
from pathlib import Path

from dotenv import load_dotenv
from hello_agents import HelloAgentsLLM, SimpleAgent


def main() -> None:
    load_dotenv(Path(__file__).with_name(".env"))

    llm = HelloAgentsLLM(
        model=os.getenv("LLM_MODEL_ID"),
        api_key=os.getenv("LLM_API_KEY"),
        base_url=os.getenv("LLM_BASE_URL"),
    )
    agent = SimpleAgent(
        name="学习助手",
        llm=llm,
        system_prompt="你是一个友好的学习助手，请用简短的中文回答。",
    )

    question = " ".join(sys.argv[1:]) or "请用一句话介绍什么是智能体。"
    print(f"问题：{question}")
    response = agent.run(
        question,
        max_tokens=128,
        # 沿用第六章的千问服务，非流式调用时关闭思考模式。
        extra_body={"enable_thinking": False},
    )
    print(f"回答：{response}")
    print(f"历史消息数：{len(agent.get_history())}")


if __name__ == "__main__":
    main()
