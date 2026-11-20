"""命令行聊天入口：不带参数进入多轮对话，带参数进行单轮问答。"""

import argparse
import sys
from pathlib import Path

from dotenv import load_dotenv

# 优先从当前项目的 backend 导入，避免 IDE 中其他项目的 app 同名包干扰。
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path or sys.path[0] != str(BACKEND_DIR):
    sys.path.insert(0, str(BACKEND_DIR))

from app.services.chat_service import CodeChatSession, open_project

PROJECT_ROOT = Path(__file__).resolve().parent.parent


def main() -> int:
    """加载配置并启动单轮问答或交互式多轮对话。"""
    load_dotenv(PROJECT_ROOT / ".env")
    parser = argparse.ArgumentParser(description="与代码分析助手对话")
    parser.add_argument(
        "--project",
        type=Path,
        default=None,
        help="要分析的本地项目目录；不指定时进入普通聊天模式，无法访问本地文件",
    )
    parser.add_argument("question", nargs="*", help="可选问题；不提供则进入多轮对话")
    args = parser.parse_args()
    try:
        project = open_project(args.project) if args.project is not None else None
        session = CodeChatSession(project)
    except (ValueError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    def reply(question: str) -> bool:
        """复用同一个会话回答，保留当前进程中的对话历史。"""
        try:
            answer = session.ask(question)
        except Exception as exc:
            print(f"请求失败：{exc}", file=sys.stderr)
            return False
        print(f"\n助手：{answer}\n")
        return True

    # 命令行附带问题时只回答一次；不附带问题则持续接收输入。
    if args.question:
        question = " ".join(args.question).strip()
        if not question:
            print("问题不能为空。", file=sys.stderr)
            return 1
        return 0 if reply(question) else 1

    print(f"当前项目：{project or '未选择（普通聊天模式）'}")
    print("代码分析助手已启动。输入 exit 或 quit 退出，Ctrl+D 也可结束对话。")
    while True:
        try:
            question = input("你：").strip()
        except (EOFError, KeyboardInterrupt):
            print("\n对话已结束。")
            return 0
        if question.lower() in {"exit", "quit"}:
            print("对话已结束。")
            return 0
        if not question:
            print("请输入问题，或输入 exit 退出。")
            continue
        reply(question)


if __name__ == "__main__":
    raise SystemExit(main())
