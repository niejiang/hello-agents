"""验证对话持久化：保存、列出、恢复、重命名和删除。"""

import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.storage.chat_store import ChatStore, StoredTurn, compact_events, make_title


class ChatStoreTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.file = Path(temp.name) / "data" / "chat.db"
        self.store = ChatStore(self.file)

    def test_compact_events_and_title(self):
        events = [{"type": "start"}, {"type": "text_delta", "text": "你"}, {"type": "text_delta", "text": "好"},
                  {"type": "tool_start", "id": "c1"}, {"type": "text_delta", "text": "！"}, {"type": "done", "answer": "你好！"}]
        self.assertEqual([e["type"] for e in compact_events(events)], ["text_delta", "tool_start", "text_delta", "done"])
        self.assertEqual(compact_events(events)[0]["text"], "你好")
        self.assertEqual(make_title("  解释一下\n第二行"), "解释一下")
        self.assertEqual(make_title("长" * 50), "长" * 40 + "…")

    def test_save_list_restore_across_instances(self):
        self.store.create("empty")  # 未发送消息的对话不列出
        self.store.save_exchange("c1", "第一问", [{"type": "text_delta", "text": "答"}, {"type": "done", "answer": "答"}],
                                 StoredTurn("第一问", "答", "读取了 a.py:1-10"), ["/p/b", "/p/a"])
        self.store.save_exchange("c1", "第二问", [{"type": "stopped"}], None, ["/p/a"])
        reopened = ChatStore(self.file)  # 模拟服务重启
        listed = reopened.list()
        self.assertEqual([(c["id"], c["title"], c["message_count"]) for c in listed], [("c1", "第一问", 4)])
        messages = reopened.messages("c1")
        self.assertEqual([m["role"] for m in messages], ["user", "assistant", "user", "assistant"])
        self.assertEqual(messages[1]["events"][0], {"type": "text_delta", "text": "答"})
        self.assertEqual(reopened.turns("c1"), [StoredTurn("第一问", "答", "读取了 a.py:1-10")])
        self.assertEqual(reopened.project_paths("c1"), ["/p/a"])

    def test_rename_and_delete(self):
        self.store.save_exchange("c1", "问", [{"type": "done", "answer": "答"}], StoredTurn("问", "答", ""), [])
        self.assertTrue(self.store.rename("c1", "  新标题 "))
        self.assertFalse(self.store.rename("missing", "x"))
        # 已有标题时，后续问答不覆盖。
        self.store.save_exchange("c1", "再问", [{"type": "done", "answer": "答"}], None, [])
        self.assertEqual(self.store.get("c1")["title"], "新标题")
        self.store.delete("c1")
        self.assertFalse(self.store.exists("c1"))
        self.assertEqual(self.store.messages("c1"), [])
        self.assertEqual(self.store.turns("c1"), [])


if __name__ == "__main__":
    unittest.main()
