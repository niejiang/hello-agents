"""提供限定在项目目录内的只读代码浏览、搜索和读取工具。"""

import os
from collections.abc import Callable
from pathlib import Path
from typing import Any

from hello_agents.tools import Tool, ToolParameter, ToolRegistry

# 跳过依赖和构建产物，限制搜索范围及返回给模型的内容量。
# 名称均为小写，比较时统一转小写，避免在不区分大小写的文件系统上被绕过。
EXCLUDED_DIRECTORIES = frozenset(
    {".git", ".idea", ".venv", "__pycache__", "build", "dist", "node_modules", "target", "venv"}
)
# 常见凭据目录，整个目录都不浏览、不搜索、不读取。
SENSITIVE_DIRECTORIES = frozenset({".ssh", ".aws", ".gnupg", ".kube", ".docker", ".azure", ".gcloud"})
SENSITIVE_FILENAMES = frozenset(
    {
        ".npmrc", ".pypirc", ".netrc", "_netrc", ".git-credentials", ".pgpass", ".htpasswd",
        "id_rsa", "id_dsa", "id_ecdsa", "id_ed25519", "credentials", "credentials.json",
    }
)
SENSITIVE_SUFFIXES = (".pem", ".key", ".p12", ".pfx", ".jks", ".keystore")
MAX_FILE_SIZE = 1024 * 1024
MAX_SCANNED_FILES = 5000
MAX_SEARCH_RESULTS = 50
MAX_DIRECTORY_ENTRIES = 200
MAX_READ_LINES = 200


def is_blocked_name(name: str) -> bool:
    """识别默认不读取的配置、凭据及密钥文件名。"""
    name = name.lower()
    is_env_file = name == ".env" or (
        name.startswith(".env.") and name != ".env.example"
    )
    return (
        is_env_file
        or name in SENSITIVE_FILENAMES
        or name in SENSITIVE_DIRECTORIES
        or name.endswith(SENSITIVE_SUFFIXES)
    )


def is_skipped_name(name: str) -> bool:
    """识别不应浏览或读取的目录及文件名，不区分大小写。"""
    return name.lower() in EXCLUDED_DIRECTORIES or is_blocked_name(name)


class CodeWorkspace:
    """对一个指定目录提供有限制的只读操作。"""

    def __init__(self, root: Path) -> None:
        """确定项目根目录；后续工具只读取该目录内的内容。"""
        self.root = root.expanduser().resolve(strict=True)
        if not self.root.is_dir():
            raise ValueError("项目路径必须是目录。")

    def path(self, value: str) -> Path:
        """检查相对路径，拒绝越界、排除目录和符号链接。"""
        relative_path = Path(value)
        if relative_path.is_absolute() or ".." in relative_path.parts:
            raise ValueError("只允许使用项目内的相对路径。")

        # 逐级检查符号链接，避免借由链接访问项目以外的文件。
        current = self.root
        for part in relative_path.parts:
            current = current / part
            if (
                current.is_symlink()
                or is_skipped_name(part)
            ):
                raise ValueError("路径被排除或为符号链接。")

        resolved = current.resolve(strict=True)
        if not resolved.is_relative_to(self.root):
            raise ValueError("路径超出项目目录。")
        return resolved

    def list_files(self, path: str = ".") -> str:
        """列出一个目录的直接子项。"""
        folder = self.path(path or ".")
        if not folder.is_dir():
            raise ValueError("指定路径不是目录。")

        entries: list[str] = []
        with os.scandir(folder) as items:
            for item in items:
                if (
                    item.is_symlink()
                    or is_skipped_name(item.name)
                ):
                    continue
                suffix = "/" if item.is_dir(follow_symlinks=False) else ""
                entries.append(f"{item.name}{suffix}")
                if len(entries) >= MAX_DIRECTORY_ENTRIES:
                    entries.append("（结果达到 200 项上限）")
                    break
        return "\n".join(sorted(entries)) or "目录为空。"

    def read_file(self, path: str, start: int = 1, end: int = 100) -> str:
        """读取 UTF-8 文本文件的指定行，行号从 1 开始。"""
        file_path = self.path(path)
        if not file_path.is_file() or file_path.stat().st_size > MAX_FILE_SIZE:
            raise ValueError("文件不存在、不是普通文件或超过 1 MiB。")
        if start < 1 or end < start or end - start >= MAX_READ_LINES:
            raise ValueError("行号无效；每次最多读取 200 行。")

        # 二进制文件不能当作代码文本读取；长行也需要限制返回长度。
        content = file_path.read_bytes()
        if b"\0" in content:
            raise ValueError("不支持读取二进制文件。")
        lines = content.decode("utf-8", errors="strict").splitlines()
        selected = []
        for number, line in enumerate(lines, start=1):
            if start <= number <= end:
                suffix = "…（行已截断）" if len(line) > 1000 else ""
                selected.append(f"{number}: {line[:1000]}{suffix}")
        result = "\n".join(selected) or "指定范围没有内容。"
        return f"文件：{file_path.relative_to(self.root)}\n{result}"

    def search(self, query: str) -> str:
        """不区分大小写地搜索文件文本，结果包含相对路径和行号。"""
        if not query.strip() or len(query) > 200:
            raise ValueError("搜索词不能为空或超过 200 字符。")

        matches: list[str] = []
        scanned_files = 0
        normalized_query = query.casefold()
        for base, directories, files in os.walk(self.root, followlinks=False):
            base_path = Path(base)
            # 原地过滤子目录，避免 os.walk 继续深入这些位置。
            directories[:] = sorted(
                name
                for name in directories
                if not is_skipped_name(name)
                and not (base_path / name).is_symlink()
            )
            for name in sorted(files):
                file_path = base_path / name
                if is_blocked_name(name) or file_path.is_symlink():
                    continue
                scanned_files += 1
                if scanned_files > MAX_SCANNED_FILES:
                    return "\n".join(matches + ["已达到 5000 个文件扫描上限，结果可能不完整。"])

                try:
                    if file_path.stat().st_size > MAX_FILE_SIZE:
                        continue
                    content = file_path.read_bytes()
                    if b"\0" in content:
                        continue
                    lines = content.decode("utf-8").splitlines()
                except (OSError, UnicodeDecodeError):
                    # 跳过不可读取或非 UTF-8 文件，继续搜索其他文件。
                    continue

                for number, line in enumerate(lines, start=1):
                    if normalized_query in line.casefold():
                        relative_path = file_path.relative_to(self.root)
                        matches.append(f"{relative_path}:{number}: {line[:500]}")
                        if len(matches) >= MAX_SEARCH_RESULTS:
                            return "\n".join(matches + ["已达到 50 条结果上限。"])
        return "\n".join(matches) or "未找到匹配内容。"


class CodeTool(Tool):
    """将工作区操作适配为 Hello-Agents 工具。"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: list[ToolParameter],
        operation: Callable[..., str],
    ) -> None:
        """保存工具的参数定义和对应的工作区操作。"""
        super().__init__(name, description)
        self.parameters = parameters
        self.operation = operation

    def get_parameters(self) -> list[ToolParameter]:
        """供 Hello-Agents 获取工具的参数定义。"""
        return self.parameters

    def run(self, parameters: dict[str, Any]) -> str:
        """执行工具并将常见参数或文件错误转换成可读结果。"""
        print(f"\n[工具] {self.name}：{parameters}", flush=True)
        # SimpleAgent 对简写调用可能使用 input，转换为工具实际参数名。
        if "input" in parameters and len(parameters) == 1:
            key = "query" if self.name == "search_code" else "path"
            parameters = {key: parameters["input"]}
        try:
            return self.operation(**parameters)
        except (ValueError, TypeError, OSError, UnicodeError) as exc:
            return f"工具执行失败：{exc}"


def create_code_registry(root: Path) -> ToolRegistry:
    """创建与指定项目关联的工具注册表。"""
    workspace = CodeWorkspace(root)
    registry = ToolRegistry()
    registry.register_tool(
        CodeTool(
            name="list_files",
            description="列出目录中的文件。path 为项目内相对路径，根目录用 .",
            parameters=[
                ToolParameter(
                    name="path", type="string", description="相对目录路径",
                    required=False, default=".",
                )
            ],
            operation=workspace.list_files,
        )
    )
    registry.register_tool(
        CodeTool(
            name="search_code",
            description="按字面文本搜索项目代码，返回文件和行号。",
            parameters=[ToolParameter(name="query", type="string", description="搜索关键词")],
            operation=workspace.search,
        )
    )
    registry.register_tool(
        CodeTool(
            name="read_file",
            description="读取项目文件的行范围，返回行号；每次最多 200 行。",
            parameters=[
                ToolParameter(name="path", type="string", description="文件相对路径"),
                ToolParameter(
                    name="start", type="integer", description="起始行，默认 1",
                    required=False, default=1,
                ),
                ToolParameter(
                    name="end", type="integer", description="结束行，默认 100",
                    required=False, default=100,
                ),
            ],
            operation=workspace.read_file,
        )
    )
    return registry
