"""打开项目时生成概况：技术栈、README 简介和顶层目录，只读取少量文件，不调用模型。"""

import json
import os
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from app.tools.code_tools import is_blocked_name, is_skipped_name

MAX_SUBDIRECTORIES = 30
MAX_TOP_DIRECTORIES = 12
MAX_MARKER_BYTES = 256 * 1024
MAX_SUMMARY_CHARS = 200

# 依赖名 → 展示名。依赖列表中出现即视为使用了该框架。
NODE_FRAMEWORKS = {
    "vue": "Vue", "react": "React", "next": "Next.js", "nuxt": "Nuxt", "svelte": "Svelte",
    "@angular/core": "Angular", "express": "Express", "@nestjs/core": "NestJS", "electron": "Electron",
}
# 依赖文件中出现关键字（小写）即视为使用了该框架。
PYTHON_FRAMEWORKS = {"fastapi": "FastAPI", "django": "Django", "flask": "Flask", "hello-agents": "Hello-Agents"}
JVM_FRAMEWORKS = {"spring-boot": "Spring Boot"}


@dataclass
class ProjectProfile:
    """项目概况，供模型按语义判断用户指的是哪个项目。"""

    name: str
    path: str
    stack: list[str] = field(default_factory=list)
    title: str = ""
    summary: str = ""
    top_directories: list[str] = field(default_factory=list)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "ProjectProfile":
        return cls(
            name=str(data.get("name", "")), path=str(data.get("path", "")),
            stack=[str(item) for item in data.get("stack") or []],
            title=str(data.get("title", "")), summary=str(data.get("summary", "")),
            top_directories=[str(item) for item in data.get("top_directories") or []],
        )

    def stack_text(self) -> str:
        return "·".join(self.stack) or "未识别技术栈"


def read_text(path: Path) -> str:
    """读取标记文件；不存在、过大、是符号链接或无法解码时返回空字符串。"""
    try:
        if path.is_symlink() or not path.is_file() or path.stat().st_size > MAX_MARKER_BYTES:
            return ""
        return path.read_text(encoding="utf-8")
    except (OSError, UnicodeDecodeError):
        return ""


def candidate_directories(root: Path) -> list[Path]:
    """根目录和一层子目录（如 frontend/、backend/），都是可能存放标记文件的位置。"""
    directories = [root]
    try:
        with os.scandir(root) as items:
            names = sorted(item.name for item in items
                           if item.is_dir(follow_symlinks=False) and not is_skipped_name(item.name)
                           and not item.name.startswith("."))
    except OSError:
        return directories
    return directories + [root / name for name in names[:MAX_SUBDIRECTORIES]]


def detect_stack(root: Path) -> list[str]:
    stack: list[str] = []

    def add(*items: str) -> None:
        for item in items:
            if item not in stack:
                stack.append(item)

    for directory in candidate_directories(root):
        package = read_text(directory / "package.json")
        if package:
            try:
                data = json.loads(package)
            except json.JSONDecodeError:
                data = {}
            deps = {**(data.get("dependencies") or {}), **(data.get("devDependencies") or {})} if isinstance(data, dict) else {}
            add("TypeScript" if "typescript" in deps or (directory / "tsconfig.json").is_file() else "JavaScript")
            add(*(label for dep, label in NODE_FRAMEWORKS.items() if dep in deps))
        python = " ".join(read_text(directory / name) for name in ("pyproject.toml", "requirements.txt", "setup.py"))
        if python.strip():
            add("Python")
            lowered = python.lower()
            add(*(label for key, label in PYTHON_FRAMEWORKS.items() if key in lowered))
        maven = read_text(directory / "pom.xml")
        gradle = read_text(directory / "build.gradle") or read_text(directory / "build.gradle.kts")
        if maven or gradle:
            add("Kotlin" if (directory / "build.gradle.kts").is_file() else "Java", "Maven" if maven else "Gradle")
            lowered = (maven + gradle).lower()
            add(*(label for key, label in JVM_FRAMEWORKS.items() if key in lowered))
        if (directory / "go.mod").is_file():
            add("Go")
        if (directory / "Cargo.toml").is_file():
            add("Rust")
    return stack


def read_readme(root: Path) -> tuple[str, str]:
    """返回 README 的标题和首段简介（去掉图片、链接地址和多余空白）。"""
    for name in ("README.md", "readme.md", "README.MD", "README", "README.txt", "README.rst"):
        text = read_text(root / name)
        if text:
            break
    else:
        return "", ""
    title, paragraph = "", []
    for raw in text.splitlines():
        line = raw.strip()
        if not title and line.startswith("# "):
            title = line[2:].strip()
            continue
        if not line:
            if paragraph:
                break
            continue
        if line.startswith(("#", "!", "[!", "<", "```", "|", "---", "===")):
            if paragraph:
                break
            continue
        paragraph.append(line)
    summary = " ".join(paragraph)
    summary = re.sub(r"!\[[^\]]*\]\([^)]*\)", "", summary)
    summary = re.sub(r"\[([^\]]*)\]\([^)]*\)", r"\1", summary)
    summary = re.sub(r"\s+", " ", summary).strip()
    if len(summary) > MAX_SUMMARY_CHARS:
        summary = summary[:MAX_SUMMARY_CHARS] + "…"
    return title[:100], summary


def top_directories(root: Path) -> list[str]:
    try:
        with os.scandir(root) as items:
            names = sorted(item.name for item in items
                           if item.is_dir(follow_symlinks=False) and not is_skipped_name(item.name)
                           and not is_blocked_name(item.name) and not item.name.startswith("."))
    except OSError:
        return []
    return names[:MAX_TOP_DIRECTORIES]


def detect_profile(root: Path, name: str | None = None) -> ProjectProfile:
    title, summary = read_readme(root)
    return ProjectProfile(
        name=name or root.name, path=str(root), stack=detect_stack(root),
        title=title, summary=summary, top_directories=top_directories(root),
    )
