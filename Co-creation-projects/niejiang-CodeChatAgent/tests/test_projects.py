"""验证项目概况、已授权项目清单、多项目会话和按需打开项目的权限规则。"""

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.agents.chat_agent import create_chat_agent
from app.projects.authorized import AuthorizedProjects
from app.projects.profile import detect_profile
from app.projects.project_set import ProjectAccessError, ProjectSet, mentioned_in
from test_chat_agent import ENV, FakeCompletions, chunk, tool_round


def make_project(root: Path, files: dict[str, str]) -> Path:
    for name, content in files.items():
        path = root / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(content, encoding="utf-8")
    return root.resolve()


class ProfileTests(unittest.TestCase):
    def test_detects_stack_readme_and_directories(self):
        with tempfile.TemporaryDirectory() as directory:
            root = make_project(Path(directory) / "demo", {
                "README.md": "# Demo 项目\n\n![badge](x.svg)\n\n一个[示例](http://x)应用，\n用于测试。\n\n## 其他\n正文",
                "backend/pyproject.toml": '[project]\ndependencies = ["fastapi>=0.1"]',
                "frontend/package.json": json.dumps({"dependencies": {"vue": "^3"}, "devDependencies": {"typescript": "~5"}}),
                "node_modules/x/package.json": json.dumps({"dependencies": {"react": "1"}}),
                ".ssh/id_rsa": "secret",
            })
            profile = detect_profile(root)
        self.assertEqual(profile.name, "demo")
        self.assertEqual(profile.stack, ["Python", "FastAPI", "TypeScript", "Vue"])
        self.assertEqual(profile.title, "Demo 项目")
        self.assertEqual(profile.summary, "一个示例应用， 用于测试。")
        self.assertEqual(profile.top_directories, ["backend", "frontend"])


class MentionTests(unittest.TestCase):
    def test_path_must_appear_as_a_complete_path(self):
        cases = [
            ("看看/a/foo的入口", True), ("看看 /a/foo/ 的入口", True), ("看 /a/foo。", True), ("见 /a/foo.", True),
            ("`/a/foo`", True), ("/a/foobar", False), ("/a/foo/src", False), ("/a/foo.py", False), ("x/a/foo", False),
        ]
        for text, expected in cases:
            with self.subTest(text=text):
                self.assertEqual(mentioned_in(text, "/a/foo"), expected)


class ProjectSetTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.base = Path(temp.name).resolve()
        self.store = AuthorizedProjects(self.base / "data" / "authorized.json")
        self.projects = ProjectSet(self.store)
        self.alpha = make_project(self.base / "work" / "alpha", {"main.py": "def alpha():\n    return 1\n"})
        self.beta = make_project(self.base / "work" / "beta", {"app.ts": "export const alpha = 2\n"})

    def test_path_in_user_message_is_opened_and_authorized(self):
        self.projects.begin_turn(f"帮我看看 {self.alpha} 的入口")
        project = self.projects.open(str(self.alpha))
        self.assertEqual(project.alias, "alpha")
        self.assertEqual(self.store.covering(self.alpha), str(self.alpha))
        self.assertEqual([p.name for p in self.projects.drain_opened()], ["alpha"])
        # 清单写入文件，另一个实例（如命令行进程）可以读到。
        self.assertEqual([p.path for p in AuthorizedProjects(self.store.file).list()], [str(self.alpha)])

    def test_inferred_path_is_rejected_and_not_authorized(self):
        self.projects.begin_turn("比较一下这两个项目")
        with self.assertRaisesRegex(ProjectAccessError, "不在用户本轮消息中"):
            self.projects.open(str(self.alpha))
        self.assertIsNone(self.store.covering(self.alpha))

    def test_subdirectory_of_mentioned_directory_is_allowed(self):
        self.projects.begin_turn(f"{self.base / 'work'} 下面的 alpha 项目")
        self.assertEqual(self.projects.open(str(self.alpha)).alias, "alpha")
        # 授权范围只是打开的项目本身，不含上级目录。
        self.assertIsNone(self.store.covering(self.base / "work"))

    def test_denied_directories(self):
        for path in ("/", str(Path.home()), "/etc", "~"):
            with self.subTest(path=path):
                self.projects.begin_turn(f"看看 {path}")
                with self.assertRaises(ProjectAccessError):
                    self.projects.open(path, explicit=True)
        secret = self.base / ".ssh"
        secret.mkdir()
        with self.assertRaisesRegex(ProjectAccessError, "凭据目录"):
            self.projects.open(str(secret), explicit=True)

    def test_open_authorized_project_by_name_and_ambiguous_names(self):
        other = make_project(self.base / "old" / "alpha", {"a.py": "x = 1\n"})
        ProjectSet(self.store).open(str(self.alpha), explicit=True)
        self.projects.begin_turn("看看 beta")
        with self.assertRaisesRegex(ProjectAccessError, "没有名为“beta”的已授权项目"):
            self.projects.open("beta")
        self.assertEqual(self.projects.open("Alpha").root, self.alpha)
        ProjectSet(self.store).open(str(other), explicit=True)
        fresh = ProjectSet(self.store)
        with self.assertRaisesRegex(ProjectAccessError, "有多个名为“alpha”"):
            fresh.open("alpha")
        self.assertEqual(fresh.open("old/alpha").root, other)

    def test_same_directory_names_get_distinct_aliases(self):
        other = make_project(self.base / "old" / "alpha", {"a.py": "x = 1\n"})
        first = self.projects.open(str(self.alpha), explicit=True)
        second = self.projects.open(str(other), explicit=True)
        self.assertEqual((first.alias, second.alias), ("alpha", "old/alpha"))
        self.assertIs(self.projects.open(str(self.alpha), explicit=True), first)

    def test_resolve_requires_project_when_several_are_open(self):
        with self.assertRaisesRegex(ProjectAccessError, "尚未打开任何项目"):
            self.projects.tool_read("main.py")
        self.projects.open(str(self.alpha), explicit=True)
        self.assertIn("1: def alpha():", self.projects.tool_read("main.py"))
        self.projects.open(str(self.beta), explicit=True)
        with self.assertRaisesRegex(ProjectAccessError, "请用 project 参数指定：alpha、beta"):
            self.projects.tool_read("main.py")
        with self.assertRaisesRegex(ProjectAccessError, "未打开名为“gamma”"):
            self.projects.tool_read("main.py", project="gamma")
        self.assertIn("1: export const alpha", self.projects.tool_read("app.ts", project="beta"))

    def test_search_all_open_projects_prefixes_alias(self):
        self.projects.open(str(self.alpha), explicit=True)
        self.projects.open(str(self.beta), explicit=True)
        result = self.projects.tool_search("alpha")
        self.assertEqual(result.splitlines(), ["alpha:main.py:1: def alpha():", "beta:app.ts:1: export const alpha = 2"])
        # 搜索全部项目不改变“最近使用”。
        self.assertEqual([p.alias for p in self.projects.projects()], ["beta", "alpha"])

    def test_revoked_project_can_no_longer_be_read(self):
        self.projects.open(str(self.alpha), explicit=True)
        AuthorizedProjects(self.store.file).remove(str(self.alpha))  # 例如在网页中撤销
        with self.assertRaisesRegex(ProjectAccessError, "授权已被用户撤销"):
            self.projects.tool_read("main.py")
        self.assertEqual(self.projects.projects(), [])

    def test_at_most_five_projects_evicting_least_recently_used(self):
        roots = [make_project(self.base / f"p{i}", {"a.py": "x\n"}) for i in range(6)]
        for root in roots[:5]:
            self.projects.open(str(root), explicit=True)
        self.projects.resolve("p0")  # p0 成为最近使用，p1 变为最久未使用
        self.projects.open(str(roots[5]), explicit=True)
        self.assertEqual(sorted(p.alias for p in self.projects.projects()), ["p0", "p2", "p3", "p4", "p5"])
        self.assertIsNotNone(self.store.covering(roots[1]))  # 移出不影响授权

    def test_catalog_note_lists_open_and_authorized_projects(self):
        self.assertEqual(self.projects.catalog_note(), "")
        ProjectSet(self.store).open(str(self.beta), explicit=True)
        self.projects.open(str(self.alpha), explicit=True)
        note = self.projects.catalog_note()
        self.assertIn("已打开的项目", note)
        self.assertIn(f"- alpha：{self.alpha}；未识别技术栈；最近使用", note)
        self.assertIn(f"已授权、可用 open_project 按简称打开的项目：\n- beta：{self.beta}", note)


class MultiProjectAgentTests(unittest.TestCase):
    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        base = Path(temp.name).resolve()
        self.alpha = make_project(base / "alpha", {"main.py": "def alpha():\n    return 1\n"})
        self.beta = make_project(base / "beta", {"app.ts": "export const alpha = 2\n"})
        self.projects = ProjectSet(AuthorizedProjects(None))
        with patch.dict("os.environ", ENV):
            self.agent = create_chat_agent(self.projects)

    def use(self, *rounds):
        completions = FakeCompletions(rounds)
        self.agent.llm._client = SimpleNamespace(chat=SimpleNamespace(completions=completions))
        return completions

    def test_opens_mentioned_projects_and_compares_in_parallel(self):
        completions = self.use(
            tool_round(("c1", "open_project", json.dumps({"path": str(self.alpha)})),
                       ("c2", "open_project", json.dumps({"path": str(self.beta)}))),
            tool_round(("c3", "search_code", json.dumps({"query": "alpha"})),
                       ("c4", "read_file", json.dumps({"project": "beta", "path": "app.ts", "start": 1, "end": 1}))),
            [chunk("两者都定义了 alpha")],
        )
        events = list(self.agent.run_stream(f"比较 {self.alpha} 和 {self.beta}", max_tool_iterations=4))
        opened = [e for e in events if e["type"] == "project_opened"]
        self.assertEqual([(e["name"], e["path"]) for e in opened], [("alpha", str(self.alpha)), ("beta", str(self.beta))])
        results = [m["content"] for m in completions.requests[2]["messages"] if m["role"] == "tool"]
        self.assertTrue(results[0].startswith("已打开项目：alpha"))
        self.assertIn("alpha:main.py:1: def alpha():", results[2])
        self.assertIn("beta:app.ts:1: export const alpha = 2", results[2])
        self.assertTrue(results[3].startswith("项目：beta\n文件：app.ts"))
        summary = self.agent.turns[-1].tool_summary
        self.assertIn("在全部已打开项目中搜索了“alpha”", summary)
        self.assertIn("在 beta 中读取了 app.ts:1-1", summary)
        # 下一轮的用户消息附带项目清单，最近使用的是 beta。
        self.use([chunk("好")])
        list(self.agent.run_stream("它用了什么语言？"))
        last = self.agent.llm._client.chat.completions.requests[0]["messages"][-1]["content"]
        self.assertIn("- beta：", last)
        self.assertTrue(last.index("- beta：") < last.index("- alpha："))
        self.assertIn("最近使用", last.split("- alpha：")[0])

    def test_model_cannot_open_path_user_did_not_give(self):
        completions = self.use(
            tool_round(("c1", "open_project", json.dumps({"path": str(self.alpha)}))),
            [chunk("请提供项目路径")],
        )
        events = list(self.agent.run_stream("看看我的项目", max_tool_iterations=2))
        self.assertFalse(next(e for e in events if e["type"] == "tool_end")["ok"])
        self.assertNotIn("project_opened", [e["type"] for e in events])
        result = [m for m in completions.requests[1]["messages"] if m["role"] == "tool"][0]["content"]
        self.assertIn("不在用户本轮消息中", result)


if __name__ == "__main__":
    unittest.main()
