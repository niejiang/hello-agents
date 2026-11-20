"""对话持久化（SQLite）：保存对话、展示用消息、模型上下文和打开过的项目，服务重启后可恢复。

- conversations：对话标题与时间。
- messages：展示用消息。用户消息保存原文；助手消息保存压缩后的事件列表（相邻文字片段合并），
  前端按与实时回答相同的方式重放，避免重复实现展示逻辑。
- turns：恢复模型上下文用的“问题 + 回答 + 工具摘要”（与 Agent 的历史记录一致）。
- conversation_projects：对话中打开过的项目，恢复时重新打开仍在授权清单中的项目。

只保存对话文字、工具摘要和事件，不保存工具读取到的代码原文。
"""

from __future__ import annotations

import json
import sqlite3
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

SCHEMA = """
CREATE TABLE IF NOT EXISTS conversations (
    id TEXT PRIMARY KEY,
    title TEXT NOT NULL DEFAULT '',
    created_at TEXT NOT NULL,
    updated_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    role TEXT NOT NULL,
    text TEXT NOT NULL DEFAULT '',
    events TEXT NOT NULL DEFAULT '[]',
    created_at TEXT NOT NULL
);
CREATE TABLE IF NOT EXISTS turns (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    question TEXT NOT NULL,
    answer TEXT NOT NULL,
    tool_summary TEXT NOT NULL DEFAULT ''
);
CREATE TABLE IF NOT EXISTS conversation_projects (
    conversation_id TEXT NOT NULL REFERENCES conversations(id) ON DELETE CASCADE,
    path TEXT NOT NULL,
    position INTEGER NOT NULL,
    PRIMARY KEY (conversation_id, path)
);
CREATE INDEX IF NOT EXISTS messages_by_conversation ON messages(conversation_id, id);
CREATE INDEX IF NOT EXISTS turns_by_conversation ON turns(conversation_id, id);
"""

MAX_TITLE_CHARS = 40
MAX_LISTED_CONVERSATIONS = 200


def now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def make_title(question: str) -> str:
    """用第一个问题生成默认标题：取第一行，过长时截断。"""
    first = " ".join(question.strip().splitlines()[0].split()) if question.strip() else "新对话"
    return first if len(first) <= MAX_TITLE_CHARS else first[:MAX_TITLE_CHARS] + "…"


def compact_events(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """去掉 start，合并相邻的文字片段，减小存储体积。"""
    compacted: list[dict[str, Any]] = []
    for event in events:
        if event.get("type") == "start":
            continue
        if event.get("type") == "text_delta" and compacted and compacted[-1].get("type") == "text_delta":
            compacted[-1] = {**compacted[-1], "text": compacted[-1]["text"] + event["text"]}
            continue
        compacted.append(event)
    return compacted


@dataclass
class StoredTurn:
    question: str
    answer: str
    tool_summary: str


class ChatStore:
    """线程安全的 SQLite 存取；每次操作使用独立连接，适合本机单用户场景。"""

    def __init__(self, file: Path) -> None:
        self.file = file
        self._lock = threading.Lock()
        self._ready = False

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        with self._lock:
            if not self._ready:
                self.file.parent.mkdir(parents=True, exist_ok=True)
            conn = sqlite3.connect(self.file)
            try:
                conn.row_factory = sqlite3.Row
                conn.execute("PRAGMA foreign_keys = ON")
                if not self._ready:
                    conn.executescript(SCHEMA)
                    self._ready = True
                with conn:
                    yield conn
            finally:
                conn.close()

    # ---- 对话 ----

    def exists(self, conversation_id: str) -> bool:
        with self._connect() as conn:
            return conn.execute("SELECT 1 FROM conversations WHERE id = ?", (conversation_id,)).fetchone() is not None

    def create(self, conversation_id: str) -> None:
        stamp = now()
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) VALUES (?, '', ?, ?)",
                         (conversation_id, stamp, stamp))

    def rename(self, conversation_id: str, title: str) -> bool:
        with self._connect() as conn:
            cursor = conn.execute("UPDATE conversations SET title = ? WHERE id = ?", (title.strip()[:100], conversation_id))
            return cursor.rowcount > 0

    def delete(self, conversation_id: str) -> None:
        with self._connect() as conn:
            conn.execute("DELETE FROM conversations WHERE id = ?", (conversation_id,))

    def list(self, limit: int = MAX_LISTED_CONVERSATIONS) -> list[dict[str, Any]]:
        """有消息的对话，按最近更新排序；只创建未发送消息的对话不列出。"""
        with self._connect() as conn:
            rows = conn.execute(
                """SELECT c.id, c.title, c.created_at, c.updated_at, COUNT(m.id) AS message_count
                   FROM conversations c JOIN messages m ON m.conversation_id = c.id
                   GROUP BY c.id ORDER BY c.updated_at DESC, c.rowid DESC LIMIT ?""", (limit,)).fetchall()
        return [dict(row) for row in rows]

    def get(self, conversation_id: str) -> dict[str, Any] | None:
        with self._connect() as conn:
            row = conn.execute("SELECT id, title, created_at, updated_at FROM conversations WHERE id = ?",
                               (conversation_id,)).fetchone()
        return dict(row) if row else None

    # ---- 一轮问答 ----

    def save_exchange(self, conversation_id: str, question: str, events: list[dict[str, Any]],
                      turn: StoredTurn | None, project_paths: list[str]) -> None:
        """保存一轮问答：用户消息、助手事件、（成功时）模型上下文，以及当前打开的项目。"""
        stamp = now()
        with self._connect() as conn:
            conn.execute("INSERT OR IGNORE INTO conversations (id, title, created_at, updated_at) VALUES (?, '', ?, ?)",
                         (conversation_id, stamp, stamp))
            conn.execute("UPDATE conversations SET title = ? WHERE id = ? AND title = ''",
                         (make_title(question), conversation_id))
            conn.execute("UPDATE conversations SET updated_at = ? WHERE id = ?", (stamp, conversation_id))
            conn.execute("INSERT INTO messages (conversation_id, role, text, created_at) VALUES (?, 'user', ?, ?)",
                         (conversation_id, question, stamp))
            conn.execute("INSERT INTO messages (conversation_id, role, events, created_at) VALUES (?, 'assistant', ?, ?)",
                         (conversation_id, json.dumps(compact_events(events), ensure_ascii=False), stamp))
            if turn is not None:
                conn.execute("INSERT INTO turns (conversation_id, question, answer, tool_summary) VALUES (?, ?, ?, ?)",
                             (conversation_id, turn.question, turn.answer, turn.tool_summary))
            self._replace_projects(conn, conversation_id, project_paths)

    def save_projects(self, conversation_id: str, project_paths: list[str]) -> None:
        with self._connect() as conn:
            self._replace_projects(conn, conversation_id, project_paths)

    @staticmethod
    def _replace_projects(conn: sqlite3.Connection, conversation_id: str, project_paths: list[str]) -> None:
        conn.execute("DELETE FROM conversation_projects WHERE conversation_id = ?", (conversation_id,))
        conn.executemany("INSERT INTO conversation_projects (conversation_id, path, position) VALUES (?, ?, ?)",
                         [(conversation_id, path, index) for index, path in enumerate(project_paths)])

    # ---- 恢复 ----

    def messages(self, conversation_id: str) -> list[dict[str, Any]]:
        with self._connect() as conn:
            rows = conn.execute("SELECT role, text, events, created_at FROM messages WHERE conversation_id = ? ORDER BY id",
                                (conversation_id,)).fetchall()
        result = []
        for row in rows:
            if row["role"] == "user":
                result.append({"role": "user", "text": row["text"], "created_at": row["created_at"]})
            else:
                result.append({"role": "assistant", "events": json.loads(row["events"]), "created_at": row["created_at"]})
        return result

    def turns(self, conversation_id: str) -> list[StoredTurn]:
        with self._connect() as conn:
            rows = conn.execute("SELECT question, answer, tool_summary FROM turns WHERE conversation_id = ? ORDER BY id",
                                (conversation_id,)).fetchall()
        return [StoredTurn(row["question"], row["answer"], row["tool_summary"]) for row in rows]

    def project_paths(self, conversation_id: str) -> list[str]:
        """按最近使用从新到旧。"""
        with self._connect() as conn:
            rows = conn.execute("SELECT path FROM conversation_projects WHERE conversation_id = ? ORDER BY position",
                                (conversation_id,)).fetchall()
        return [row["path"] for row in rows]
