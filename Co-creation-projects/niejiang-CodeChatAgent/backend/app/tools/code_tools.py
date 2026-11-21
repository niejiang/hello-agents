"""提供限定在项目目录内的只读代码浏览、搜索和读取工具，以及按需打开项目的工具。"""

import os
import re
import subprocess
import time
from collections.abc import Callable
from pathlib import Path
from typing import TYPE_CHECKING, Any

from hello_agents.tools import Tool, ToolParameter, ToolRegistry

if TYPE_CHECKING:
    from app.projects.project_set import ProjectSet

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
# 单次搜索最多扫描的文件数和耗时；超出时返回已有结果并说明不完整。
MAX_SCANNED_FILES = 20000
SEARCH_TIME_BUDGET_SECONDS = 10.0
DEFAULT_SEARCH_LIMIT = 50
MAX_SEARCH_LIMIT = 200
MAX_CONTEXT_LINES = 5
MAX_MATCH_LINE_CHARS = 500
MAX_DIRECTORY_ENTRIES = 200
DEFAULT_FIND_LIMIT = 100
MAX_FIND_LIMIT = 300
# 单次读取上限；未指定结束行时从起始行读取 DEFAULT_READ_LINES 行，多数源文件一次即可读完。
MAX_READ_LINES = 1000
DEFAULT_READ_LINES = 500
# 截断说明中最多列出的未显示文件数。
MAX_REMAINING_FILES_LISTED = 15
# 单次工具结果返回给模型的最大字符数，避免一次读取占满上下文。
MAX_TOOL_RESULT_CHARS = 20000
GIT_TIMEOUT_SECONDS = 10
# 依次尝试的文本编码：UTF-8（含 BOM），失败时按 GB18030（兼容 GBK、GB2312）解码，适配国内常见的旧项目。
TEXT_ENCODINGS = ("utf-8-sig", "gb18030")
# 搜索时跳过的生成文件：依赖锁文件和压缩产物，内容庞大且很少是要找的代码。
GENERATED_FILENAMES = frozenset({
    "package-lock.json", "yarn.lock", "pnpm-lock.yaml", "poetry.lock", "uv.lock", "pipfile.lock",
    "cargo.lock", "composer.lock", "gemfile.lock", "go.sum",
})
GENERATED_SUFFIXES = (".min.js", ".min.css", ".map")
SEARCH_OUTPUTS = ("content", "files", "count")


def default_read_end(start: int) -> int:
    """未指定结束行时的结束行号。"""
    return start + DEFAULT_READ_LINES - 1


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


def is_generated_name(name: str) -> bool:
    name = name.lower()
    return name in GENERATED_FILENAMES or name.endswith(GENERATED_SUFFIXES)


def decode_text(data: bytes) -> tuple[str, str] | None:
    """按 TEXT_ENCODINGS 依次解码，返回（文本, 编码）；二进制或无法解码时返回 None。"""
    if b"\0" in data:
        return None
    for encoding in TEXT_ENCODINGS:
        try:
            return data.decode(encoding), ("utf-8" if encoding == "utf-8-sig" else encoding)
        except UnicodeDecodeError:
            continue
    return None


def _split_patterns(value: str) -> list[str]:
    """按逗号拆分多个模式，花括号内的逗号不拆，如 "*.{java,xml},pom.xml"。"""
    parts, depth, current = [], 0, ""
    for char in value:
        if char == "{":
            depth += 1
        elif char == "}":
            depth = max(0, depth - 1)
        if char == "," and depth == 0:
            parts.append(current)
            current = ""
        else:
            current += char
    parts.append(current)
    return [part.strip() for part in parts if part.strip()]


def _glob_to_regex(pattern: str) -> re.Pattern[str]:
    """把 glob 转为正则：** 匹配任意层目录，* 和 ? 不跨目录，{a,b} 表示多选一；不区分大小写。"""
    out, index = "", 0
    while index < len(pattern):
        if pattern.startswith("**/", index):
            out += "(?:.*/)?"
            index += 3
            continue
        if pattern.startswith("**", index):
            out += ".*"
            index += 2
            continue
        char = pattern[index]
        if char == "*":
            out += "[^/]*"
        elif char == "?":
            out += "[^/]"
        elif char == "{" and "}" in pattern[index:]:
            end = pattern.index("}", index)
            out += "(?:" + "|".join(re.escape(option) for option in pattern[index + 1:end].split(",")) + ")"
            index = end + 1
            continue
        else:
            out += re.escape(char)
        index += 1
    return re.compile(out + r"\Z", re.IGNORECASE)


class PathMatcher:
    """文件路径匹配：不含 / 的模式匹配文件名，含 / 的模式匹配相对路径；不含通配符的模式按文件名子串匹配。"""

    def __init__(self, value: str) -> None:
        self.patterns = []
        for pattern in _split_patterns(value):
            if not any(char in pattern for char in "*?{"):
                needle = pattern.lower()
                self.patterns.append(("substring", needle))
            else:
                self.patterns.append(("path" if "/" in pattern else "name", _glob_to_regex(pattern.lstrip("./"))))
        if not self.patterns:
            raise ValueError("文件模式不能为空")

    def matches(self, *relative_paths: str) -> bool:
        for kind, pattern in self.patterns:
            for relative in relative_paths:
                name = relative.rsplit("/", 1)[-1]
                if kind == "substring" and pattern in name.lower():
                    return True
                if kind == "name" and pattern.match(name):
                    return True
                if kind == "path" and pattern.match(relative):
                    return True
        return False


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

    def relative(self, path: Path) -> str:
        return path.relative_to(self.root).as_posix()

    # ---- 文件枚举 ----

    def iter_files(self, base: Path) -> list[Path]:
        """列出 base 下可读取的文件（排序）：Git 仓库中遵守 .gitignore，否则遍历目录；两者都排除敏感和依赖目录。"""
        files = self._git_files(base)
        if files is None:
            files = self._walk_files(base)
        return files

    def _allowed(self, path: Path) -> bool:
        relative = path.relative_to(self.root)
        current = self.root
        for part in relative.parts:
            current = current / part
            if is_skipped_name(part) or current.is_symlink():
                return False
        return True

    def _git_files(self, base: Path) -> list[Path] | None:
        """用 git ls-files 列出已跟踪和未被忽略的文件；不是 Git 仓库、git 不可用或没有结果时返回 None。

        只读调用：关闭 fsmonitor（防止仓库配置执行外部命令）和可选锁，不写入索引。
        """
        try:
            result = subprocess.run(
                ["git", "-c", "core.fsmonitor=false", "-c", "core.untrackedCache=false",
                 "ls-files", "-z", "--cached", "--others", "--exclude-standard"],
                cwd=base, capture_output=True, timeout=GIT_TIMEOUT_SECONDS, stdin=subprocess.DEVNULL,
                env={**os.environ, "GIT_OPTIONAL_LOCKS": "0", "GIT_TERMINAL_PROMPT": "0"},
            )
        except (OSError, subprocess.SubprocessError):
            return None
        if result.returncode != 0:
            return None
        if not result.stdout:
            # 目录本身被外层仓库忽略（如位于 .venv 中）或为空：.gitignore 不适用，改为直接遍历。
            return None
        files = []
        for raw in sorted(set(result.stdout.decode("utf-8", errors="surrogateescape").split("\0"))):
            if not raw:
                continue
            path = base / raw
            # 已删除但仍被跟踪的文件、子模块目录等不是普通文件，跳过。
            if path.is_file() and not path.is_symlink() and self._allowed(path):
                files.append(path)
        return files

    def _walk_files(self, base: Path) -> list[Path]:
        files = []
        for directory, directories, names in os.walk(base, followlinks=False):
            directory_path = Path(directory)
            # 原地过滤子目录，避免 os.walk 继续深入这些位置。
            directories[:] = sorted(
                name for name in directories
                if not is_skipped_name(name) and not (directory_path / name).is_symlink()
            )
            for name in sorted(names):
                path = directory_path / name
                if not is_blocked_name(name) and not path.is_symlink():
                    files.append(path)
        return files

    def read_text(self, path: Path) -> tuple[list[str], str] | None:
        """读取文本文件的各行和编码；过大、二进制或无法解码时返回 None。"""
        try:
            if path.stat().st_size > MAX_FILE_SIZE:
                return None
            decoded = decode_text(path.read_bytes())
        except OSError:
            return None
        if decoded is None:
            return None
        text, encoding = decoded
        return text.splitlines(), encoding

    # ---- 工具操作 ----

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

    def read_file(self, path: str, start: int = 1, end: int | None = None) -> str:
        """读取文本文件的指定行，行号从 1 开始；未指定 end 时从 start 起读取 DEFAULT_READ_LINES 行。

        支持 UTF-8 和 GB18030（GBK）编码。
        """
        end = default_read_end(start) if end is None else end
        file_path = self.path(path)
        if not file_path.is_file() or file_path.stat().st_size > MAX_FILE_SIZE:
            raise ValueError("文件不存在、不是普通文件或超过 1 MiB。")
        if start < 1 or end < start or end - start >= MAX_READ_LINES:
            raise ValueError(f"行号无效；每次最多读取 {MAX_READ_LINES} 行。")
        decoded = decode_text(file_path.read_bytes())
        if decoded is None:
            raise ValueError("不支持读取二进制文件或无法识别的编码。")
        text, encoding = decoded
        lines = text.splitlines()
        selected = []
        for number in range(start, min(end, len(lines)) + 1):
            line = lines[number - 1]
            suffix = "…（行已截断）" if len(line) > 1000 else ""
            selected.append(f"{number}: {line[:1000]}{suffix}")
        notes = [f"共 {len(lines)} 行"]
        if encoding != "utf-8":
            notes.append(f"编码 {encoding.upper()}")
        header = f"文件：{self.relative(file_path)}（{'，'.join(notes)}）"
        if end < len(lines):
            selected.append(f"…（以下还有 {len(lines) - end} 行，可继续读取 {end + 1}-{min(len(lines), end + DEFAULT_READ_LINES)}）")
        return header + "\n" + ("\n".join(selected) or "指定范围没有内容。")

    def find_files(self, pattern: str, path: str = ".", limit: int = DEFAULT_FIND_LIMIT, label: str = "") -> str:
        """按文件名或路径模式查找文件，如 "*.java"、"**/service/*Impl.java"、"UserService"。"""
        if not pattern.strip() or len(pattern) > 200:
            raise ValueError("文件模式不能为空或超过 200 字符")
        if not 1 <= limit <= MAX_FIND_LIMIT:
            raise ValueError(f"limit 应在 1～{MAX_FIND_LIMIT} 之间")
        base = self.path(path or ".")
        if not base.is_dir():
            raise ValueError("指定路径不是目录。")
        matcher = PathMatcher(pattern)
        found = [self.relative(file) for file in self.iter_files(base)
                 if matcher.matches(self.relative(file), file.relative_to(base).as_posix())]
        if not found:
            return "未找到匹配的文件。"
        lines = [f"{label}{item}" for item in found[:limit]]
        if len(found) > limit:
            lines.append(f"[结果已截断：共 {len(found)} 个文件，以上显示前 {limit} 个。可用 path 或更具体的模式缩小范围。]")
        return "\n".join(lines)

    def search(self, query: str, path: str = ".", glob: str | None = None, regex: bool = False,
               case_sensitive: bool = False, context: int = 0, output: str = "content",
               limit: int = DEFAULT_SEARCH_LIMIT, label: str = "") -> str:
        """搜索文件内容，结果包含相对路径和行号；结果过多时说明总数和未显示文件的分布。

        label 加在每个路径前（搜索多个项目时为“项目简称:”）。
        """
        if not query or len(query) > 200:
            raise ValueError("搜索词不能为空或超过 200 字符。")
        if output not in SEARCH_OUTPUTS:
            raise ValueError(f"output 只能是 {'、'.join(SEARCH_OUTPUTS)}")
        if not 0 <= context <= MAX_CONTEXT_LINES:
            raise ValueError(f"context 应在 0～{MAX_CONTEXT_LINES} 之间")
        if not 1 <= limit <= MAX_SEARCH_LIMIT:
            raise ValueError(f"limit 应在 1～{MAX_SEARCH_LIMIT} 之间")
        try:
            matcher = re.compile(query if regex else re.escape(query), 0 if case_sensitive else re.IGNORECASE)
        except re.error as exc:
            raise ValueError(f"正则表达式无效（{exc}）；如需按字面搜索请设 regex 为 false") from None
        base = self.path(path or ".")
        file_filter = PathMatcher(glob) if glob else None
        candidates = [base] if base.is_file() else self.iter_files(base)

        counts: dict[str, int] = {}
        shown: list[str] = []
        shown_matches = 0
        shown_files: set[str] = set()
        scanned = 0
        other_encodings = 0
        incomplete = ""
        started = time.monotonic()
        for file in candidates:
            relative = self.relative(file)
            if file_filter and not file_filter.matches(relative, file.relative_to(base).as_posix() if file != base else file.name):
                continue
            if file != base and is_generated_name(file.name):
                continue
            scanned += 1
            if scanned > MAX_SCANNED_FILES or time.monotonic() - started > SEARCH_TIME_BUDGET_SECONDS:
                incomplete = (f"已达到扫描上限（{MAX_SCANNED_FILES} 个文件或 {SEARCH_TIME_BUDGET_SECONDS:.0f} 秒），"
                              "结果不完整，请用 path 或 glob 缩小范围")
                scanned -= 1
                break
            content = self.read_text(file)
            if content is None:
                continue
            lines, encoding = content
            hits = [index for index, line in enumerate(lines) if matcher.search(line)]
            if not hits:
                continue
            if encoding != "utf-8":
                other_encodings += 1
            counts[relative] = len(hits)
            if output == "content" and shown_matches < limit:
                taken = hits[:limit - shown_matches]
                shown_matches += len(taken)
                shown_files.add(relative)
                shown.extend(self._render_hits(f"{label}{relative}", lines, taken, context))

        total = sum(counts.values())
        notes = []
        if other_encodings:
            notes.append(f"其中 {other_encodings} 个文件按 GB18030 编码读取")
        if incomplete:
            notes.append(incomplete)
        if not total:
            scope = f"在 {path or '.'}{f'（{glob}）' if glob else ''} 中未找到匹配内容。"
            return scope + "".join(f"\n[{note}]" for note in notes)

        if output == "count":
            ranked = sorted(counts.items(), key=lambda item: -item[1])
            lines = [f"共 {total} 处匹配，分布在 {len(counts)} 个文件："]
            lines += [f"{label}{name}（{count}）" for name, count in ranked[:limit]]
            if len(ranked) > limit:
                lines.append(f"…另有 {len(ranked) - limit} 个文件")
        elif output == "files":
            names = list(counts)
            lines = [f"{label}{name}（{counts[name]}）" for name in names[:limit]]
            if len(names) > limit:
                lines.append(f"[结果已截断：共 {len(names)} 个文件、{total} 处匹配，以上显示前 {limit} 个文件。"
                             "可用 path 或 glob 缩小范围。]")
        else:
            lines = shown
            if total > shown_matches:
                remaining = sorted(((name, count) for name, count in counts.items() if name not in shown_files),
                                   key=lambda item: -item[1])
                listed = "、".join(f"{label}{name}（{count}）" for name, count in remaining[:MAX_REMAINING_FILES_LISTED])
                more = f" 等 {len(remaining)} 个文件" if len(remaining) > MAX_REMAINING_FILES_LISTED else ""
                lines.append(
                    f"[结果已截断：共 {total} 处匹配、{len(counts)} 个文件，以上显示前 {shown_matches} 处（{len(shown_files)} 个文件）。"
                    + (f"未显示的文件：{listed}{more}。" if remaining else "")
                    + "可用 path / glob 缩小范围、换更具体的搜索词，或设 output 为 files 查看全部文件。]")
        return "\n".join(lines + [f"[{note}]" for note in notes])

    @staticmethod
    def _render_hits(name: str, lines: list[str], hits: list[int], context: int) -> list[str]:
        """命中行写成“路径:行号: 内容”，上下文行写成“路径-行号- 内容”，不相邻的片段之间用 -- 分隔（与 ripgrep 一致）。"""
        if not context:
            return [f"{name}:{index + 1}: {lines[index][:MAX_MATCH_LINE_CHARS]}" for index in hits]
        rendered: list[str] = []
        hit_set = set(hits)
        last = -1
        for index in hits:
            start, end = max(0, index - context), min(len(lines) - 1, index + context)
            if last >= 0 and start > last + 1:
                rendered.append("--")
            for number in range(max(start, last + 1), end + 1):
                separator = ":" if number in hit_set else "-"
                rendered.append(f"{name}{separator}{number + 1}{separator} {lines[number][:MAX_MATCH_LINE_CHARS]}")
            last = max(last, end)
        return rendered


class CodeTool(Tool):
    """将工作区操作适配为 Hello-Agents 工具，并提供严格的 JSON Schema。"""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: list[ToolParameter],
        operation: Callable[..., str],
        constraints: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        """保存参数定义、额外的 Schema 约束和对应的工作区操作。"""
        super().__init__(name, description)
        self.parameters = parameters
        self.operation = operation
        self.constraints = constraints or {}

    def get_parameters(self) -> list[ToolParameter]:
        """供 Hello-Agents 获取工具的参数定义。"""
        return self.parameters

    def parameters_schema(self) -> dict[str, Any]:
        """生成原生工具调用使用的参数 Schema，禁止未声明的参数。"""
        properties: dict[str, Any] = {}
        for parameter in self.parameters:
            schema: dict[str, Any] = {"type": parameter.type, "description": parameter.description}
            if parameter.default is not None:
                schema["default"] = parameter.default
            schema.update(self.constraints.get(parameter.name, {}))
            properties[parameter.name] = schema
        return {
            "type": "object",
            "properties": properties,
            "required": [parameter.name for parameter in self.parameters if parameter.required],
            "additionalProperties": False,
        }

    def run(self, parameters: dict[str, Any]) -> str:
        """执行工具并将参数或文件错误转换成模型可读的结果。"""
        try:
            result = self.operation(**parameters)
        except (ValueError, TypeError, OSError, UnicodeError) as exc:
            return f"工具执行失败：{exc}。请检查参数后重试。"
        if len(result) > MAX_TOOL_RESULT_CHARS:
            result = (
                result[:MAX_TOOL_RESULT_CHARS]
                + f"\n…（结果已截断：超过 {MAX_TOOL_RESULT_CHARS} 字符，请缩小行范围或搜索词）"
            )
        return result


PROJECT_PARAMETER_DESCRIPTION = "已打开项目的简称；只打开了一个项目时可省略，打开多个项目时必须指定"


def project_parameter() -> ToolParameter:
    return ToolParameter(name="project", type="string", description=PROJECT_PARAMETER_DESCRIPTION, required=False)


def create_code_registry(projects: "ProjectSet") -> ToolRegistry:
    """创建绑定会话项目集合的工具注册表；工具集固定，未打开项目时文件工具返回提示。"""
    registry = ToolRegistry()
    registry.register_tool(
        CodeTool(
            name="open_project",
            description=(
                "打开一个本地项目，返回其简称、技术栈、README 简介和顶层目录。"
                "参数为用户消息中给出的绝对路径（可用 ~/ 开头），或已授权项目的简称。"
                "用户提到某个项目而它尚未打开时先调用本工具；无法确定是哪个项目时先询问用户。"
            ),
            parameters=[ToolParameter(name="path", type="string", description="项目绝对路径，或已授权项目的简称")],
            operation=projects.tool_open,
            constraints={"path": {"minLength": 1, "maxLength": 1024}},
        )
    )
    registry.register_tool(
        CodeTool(
            name="list_files",
            description="列出已打开项目中某个目录的直接子项，目录以 / 结尾。用于了解项目结构。",
            parameters=[
                project_parameter(),
                ToolParameter(
                    name="path", type="string", description="项目内的相对目录路径，根目录用 .",
                    required=False, default=".",
                ),
            ],
            operation=projects.tool_list,
            constraints={"project": {"maxLength": 200}, "path": {"maxLength": 1024}},
        )
    )
    registry.register_tool(
        CodeTool(
            name="search_code",
            description=(
                "搜索项目文件内容（遵守 .gitignore，支持 UTF-8 和 GBK/GB18030），返回“相对路径:行号: 该行内容”。"
                "用于定位函数、类、配置项的定义和调用处。默认按字面文本、不区分大小写匹配。"
                "结果过多时会说明总数和未显示的文件，此时应用 path、glob 缩小范围或换更具体的搜索词，不要只依据部分结果下结论。"
                "打开多个项目时省略 project 会搜索全部已打开项目，结果以“项目简称:”开头，适合比较项目。"
            ),
            parameters=[
                project_parameter(),
                ToolParameter(name="query", type="string", description="搜索内容；regex 为 false 时按字面匹配，可包含括号、逗号等符号"),
                ToolParameter(name="path", type="string", description="只在该子目录（或文件）中搜索，项目内相对路径，默认 .",
                              required=False, default="."),
                ToolParameter(name="glob", type="string",
                              description="只搜索匹配的文件，如 *.java、src/**/*.ts、*.{java,xml}；多个用逗号分隔",
                              required=False),
                ToolParameter(name="regex", type="boolean",
                              description="为 true 时 query 按正则表达式匹配，如 \\bcreateOrder\\b、class\\s+\\w+Service",
                              required=False, default=False),
                ToolParameter(name="case_sensitive", type="boolean", description="是否区分大小写", required=False, default=False),
                ToolParameter(name="context", type="integer", description="每处匹配前后附带的行数（0～5）",
                              required=False, default=0),
                ToolParameter(name="output", type="string",
                              description="content 返回匹配行；files 只列出文件及匹配数；count 只统计数量",
                              required=False, default="content"),
                ToolParameter(name="limit", type="integer",
                              description="content 模式最多返回的匹配行数，files/count 模式最多列出的文件数（1～200）",
                              required=False, default=DEFAULT_SEARCH_LIMIT),
            ],
            operation=projects.tool_search,
            constraints={
                "project": {"maxLength": 200}, "query": {"minLength": 1, "maxLength": 200},
                "path": {"maxLength": 1024}, "glob": {"minLength": 1, "maxLength": 200},
                "context": {"minimum": 0, "maximum": MAX_CONTEXT_LINES},
                "output": {"enum": list(SEARCH_OUTPUTS)},
                "limit": {"minimum": 1, "maximum": MAX_SEARCH_LIMIT},
            },
        )
    )
    registry.register_tool(
        CodeTool(
            name="find_files",
            description=(
                "按文件名或路径模式查找文件（遵守 .gitignore）。模式如 *Controller.java、**/service/*.py、pom.xml；"
                "不含通配符时按文件名包含该文本匹配，如 UserService。用于在不知道位置时先找到文件。"
            ),
            parameters=[
                project_parameter(),
                ToolParameter(name="pattern", type="string", description="文件名或路径模式，多个用逗号分隔"),
                ToolParameter(name="path", type="string", description="只在该子目录中查找，默认 .", required=False, default="."),
                ToolParameter(name="limit", type="integer", description="最多返回的文件数（1～300）",
                              required=False, default=DEFAULT_FIND_LIMIT),
            ],
            operation=projects.tool_find,
            constraints={"project": {"maxLength": 200}, "pattern": {"minLength": 1, "maxLength": 200},
                         "path": {"maxLength": 1024}, "limit": {"minimum": 1, "maximum": MAX_FIND_LIMIT}},
        )
    )
    registry.register_tool(
        CodeTool(
            name="read_file",
            description=(
                "读取已打开项目中文本文件（UTF-8 或 GBK/GB18030）的指定行范围（包含首尾行），返回带行号的内容和文件总行数，"
                f"省略 end 时从 start 起读取 {DEFAULT_READ_LINES} 行，每次最多 {MAX_READ_LINES} 行。"
                "通常先用 search_code 找到行号，再读取附近代码；结果提示“以下还有”时按提示的行范围继续读取。"
            ),
            parameters=[
                project_parameter(),
                ToolParameter(name="path", type="string", description="项目内的文件相对路径"),
                ToolParameter(
                    name="start", type="integer", description="起始行号，从 1 开始",
                    required=False, default=1,
                ),
                ToolParameter(
                    name="end", type="integer",
                    description=f"结束行号（包含），与起始行相差不超过 {MAX_READ_LINES - 1}；省略时读取 {DEFAULT_READ_LINES} 行",
                    required=False,
                ),
            ],
            operation=projects.tool_read,
            constraints={"project": {"maxLength": 200}, "path": {"minLength": 1, "maxLength": 1024},
                         "start": {"minimum": 1}, "end": {"minimum": 1}},
        )
    )
    return registry
