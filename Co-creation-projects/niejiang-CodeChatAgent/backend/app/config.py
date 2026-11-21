"""模型配置文件的定位与加载，供命令行和网页共用。"""

import os
from pathlib import Path

from dotenv import load_dotenv

PROJECT_ROOT = Path(__file__).resolve().parents[2]
# 选择配置文件的环境变量：未设置时使用项目根目录的 .env；可指向 .env.deepseek 等其他模型服务的配置，便于对比。
ENV_FILE_VARIABLE = "CODECHAT_ENV_FILE"
DEFAULT_ENV_FILE = ".env"


def env_file(name: str | None = None) -> Path:
    """返回配置文件路径；相对路径按项目根目录解析。"""
    chosen = (name or os.getenv(ENV_FILE_VARIABLE, "")).strip() or DEFAULT_ENV_FILE
    path = Path(chosen).expanduser()
    return path if path.is_absolute() else PROJECT_ROOT / path


def load_env(name: str | None = None) -> Path:
    """加载配置文件（已存在的环境变量优先），返回实际使用的文件。"""
    path = env_file(name)
    load_dotenv(path)
    return path
