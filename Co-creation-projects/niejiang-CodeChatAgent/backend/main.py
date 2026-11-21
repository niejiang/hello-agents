"""命令行聊天入口：不带参数进入多轮对话，带参数进行单轮问答。"""

import argparse
import sys
from pathlib import Path

# 优先从当前项目的 backend 导入，避免 IDE 中其他项目的 app 同名包干扰。
BACKEND_DIR = Path(__file__).resolve().parent
if str(BACKEND_DIR) not in sys.path or sys.path[0] != str(BACKEND_DIR):
    sys.path.insert(0, str(BACKEND_DIR))

from app.config import load_env
from app.services.chat_service import CodeChatSession, open_project


def format_arguments(arguments: object) -> str:
    """把工具参数显示为一行简短文本。"""
    if isinstance(arguments, dict):
        text = ", ".join(f"{key}={value}" for key, value in arguments.items())
    else:
        text = str(arguments)
    return text if len(text) <= 120 else text[:117] + "..."


def main() -> int:
    """加载配置并启动单轮问答或交互式多轮对话。"""
    parser = argparse.ArgumentParser(description="与助手对话；需要分析项目时，在对话中给出项目路径即可")
    parser.add_argument(
        "--env-file",
        help="模型配置文件，相对路径按项目根目录解析，如 .env.deepseek；默认读取环境变量 CODECHAT_ENV_FILE 指定的文件或 .env",
    )
    parser.add_argument(
        "--project",
        type=Path,
        action="append",
        default=[],
        help="预先打开的本地项目目录，可多次指定；不指定时也可在对话中给出路径按需打开",
    )
    parser.add_argument("question", nargs="*", help="可选问题；不提供则进入多轮对话")
    args = parser.parse_args()
    load_env(args.env_file)
    try:
        projects = [open_project(path) for path in args.project]
        session = CodeChatSession(projects)
    except (ValueError, OSError) as exc:
        print(f"启动失败：{exc}", file=sys.stderr)
        return 1

    def reply(question: str) -> bool:
        """复用同一个会话回答，逐段打印回答和工具调用，保留当前进程中的对话历史。"""
        print("\n助手：", end="", flush=True)
        at_line_start = False
        try:
            for event in session.ask_stream(question):
                kind = event["type"]
                if kind == "text_delta":
                    print(event["text"], end="", flush=True)
                    at_line_start = event["text"].endswith("\n")
                elif kind == "tool_start":
                    prefix = "" if at_line_start else "\n"
                    print(f"{prefix}  · {event['name']} {format_arguments(event['arguments'])}", end="", flush=True)
                elif kind == "tool_end":
                    status = "✓" if event["ok"] else "✗"
                    print(f" → {status} {event['lines']} 行", flush=True)
                    at_line_start = True
                elif kind == "permission_request":
                    prefix = "" if at_line_start else "\n"
                    print(f"{prefix}  ! 助手请求访问 {event['path']}", flush=True)
                    print("    该路径不在你的消息中，也不在已授权项目中；允许后将加入已授权项目（只读）。", flush=True)
                    try:
                        answer = input("    允许吗？[y/N] ").strip().lower()
                    except (EOFError, KeyboardInterrupt):
                        answer = ""
                    session.resolve_permission(event["id"], answer in {"y", "yes", "是"})
                    at_line_start = True
                elif kind == "permission_resolved":
                    labels = {"allow": "已允许", "deny": "已拒绝", "timeout": "确认超时", "stopped": "已停止"}
                    print(f"    （{labels.get(event['decision'], event['decision'])}）", flush=True)
                elif kind == "project_opened":
                    stack = "·".join(event["stack"]) or "未识别技术栈"
                    print(f"  · 已打开项目 {event['name']}（{stack}，{event['path']}）", flush=True)
                    at_line_start = True
                elif kind == "retrying":
                    print(f"\n  （{event['reason']}）", flush=True)
                    at_line_start = True
                elif kind == "context_trimmed":
                    print(f"\n  （{event['reason']}）", flush=True)
                    at_line_start = True
                elif kind == "error":
                    print(f"\n请求失败：{event['message']}", file=sys.stderr)
                    return False
                elif kind == "done":
                    context = event["context"]
                    usage = event["usage"]
                    if event.get("limited"):
                        print(f"\n\n  （已达到工具调用上限 {event['limited']} 轮，分析可能不完整；输入“继续”可接着分析）", end="")
                    print(
                        f"\n\n  （输入 {usage['last_prompt_tokens'] or '—'} token，"
                        f"预算占用 {context['budget_percent']}%，请求 {usage['requests']} 次）\n",
                        flush=True,
                    )
        except Exception as exc:
            print(f"\n请求失败：{exc}", file=sys.stderr)
            return False
        return True

    # 命令行附带问题时只回答一次；不附带问题则持续接收输入。
    if args.question:
        question = " ".join(args.question).strip()
        if not question:
            print("问题不能为空。", file=sys.stderr)
            return 1
        return 0 if reply(question) else 1

    opened = "、".join(f"{p.alias}（{p.root}）" for p in session.projects.projects())
    print(f"已打开项目：{opened or '无，可在对话中给出项目路径'}")
    print("助手已启动。输入 exit 或 quit 退出，Ctrl+D 也可结束对话。")
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
