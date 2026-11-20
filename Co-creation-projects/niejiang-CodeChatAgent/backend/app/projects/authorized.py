"""已授权项目清单：进入清单即允许 Agent 读取该目录及其子目录，保存在本机文件中，可随时撤销。

只记录简称、路径、概况和授权时间，不保存代码内容。命令行和网页服务可能同时使用同一文件，
因此每次读取前检查文件修改时间，必要时重新加载。
"""

import json
import logging
import os
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.projects.profile import ProjectProfile

logger = logging.getLogger(__name__)


class AuthorizedProjects:
    """线程安全的已授权项目清单；file 为 None 时只保存在内存中（用于测试）。"""

    def __init__(self, file: Path | None) -> None:
        self.file = file
        self._lock = threading.Lock()
        self._entries: dict[str, dict[str, Any]] = {}
        self._mtime: float | None = None

    def _reload(self) -> None:
        if self.file is None:
            return
        try:
            mtime = self.file.stat().st_mtime
        except FileNotFoundError:
            self._entries, self._mtime = {}, None
            return
        if mtime == self._mtime:
            return
        try:
            data = json.loads(self.file.read_text(encoding="utf-8"))
            entries = {item["path"]: item for item in data.get("projects", []) if isinstance(item, dict) and item.get("path")}
        except (OSError, ValueError, AttributeError) as exc:
            logger.warning("已授权项目清单无法读取，按空清单处理：%s", exc)
            entries = {}
        self._entries, self._mtime = entries, mtime

    def _save(self) -> None:
        if self.file is None:
            return
        self.file.parent.mkdir(parents=True, exist_ok=True)
        temp = self.file.with_suffix(".tmp")
        payload = {"projects": sorted(self._entries.values(), key=lambda item: item["authorized_at"])}
        temp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
        os.replace(temp, self.file)
        self._mtime = self.file.stat().st_mtime

    def add(self, profile: ProjectProfile) -> None:
        """授权项目；已授权时只更新概况，保留原授权时间。"""
        with self._lock:
            self._reload()
            previous = self._entries.get(profile.path, {})
            self._entries[profile.path] = {
                **profile.to_dict(),
                "authorized_at": previous.get("authorized_at") or datetime.now(timezone.utc).isoformat(timespec="seconds"),
            }
            self._save()

    def remove(self, path: str) -> bool:
        """撤销授权；返回是否确实移除了。"""
        with self._lock:
            self._reload()
            removed = self._entries.pop(path, None) is not None
            if removed:
                self._save()
            return removed

    def covering(self, path: Path) -> str | None:
        """返回覆盖该路径的授权项目路径（项目本身或其上级），没有则返回 None。"""
        with self._lock:
            self._reload()
            for candidate in (path, *path.parents):
                if str(candidate) in self._entries:
                    return str(candidate)
        return None

    def list(self) -> list[ProjectProfile]:
        """按授权时间从新到旧列出。"""
        with self._lock:
            self._reload()
            entries = sorted(self._entries.values(), key=lambda item: item.get("authorized_at", ""), reverse=True)
        return [ProjectProfile.from_dict(item) for item in entries]
