"""验证代码工具的基本结果和项目目录边界。"""

import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "backend"))
from app.tools.code_tools import CodeWorkspace


class CodeWorkspaceTests(unittest.TestCase):
    """使用临时目录验证工具，不依赖真实项目。"""

    def test_read_search_and_boundaries(self):
        """检查浏览与读取结果，以及敏感文件和越界路径的限制。"""
        with tempfile.TemporaryDirectory() as directory, tempfile.TemporaryDirectory() as outside:
            root = Path(directory)
            (root / "main.py").write_text("def hello():\n    return 'world'\n", encoding="utf-8")
            (root / ".env").write_text("PRIVATE=test", encoding="utf-8")
            (root / "node_modules").mkdir()
            (root / "node_modules" / "hidden.py").write_text("hello", encoding="utf-8")
            (root / "link").symlink_to(Path(outside), target_is_directory=True)
            workspace = CodeWorkspace(root)
            self.assertIn("main.py", workspace.list_files())
            self.assertNotIn(".env", workspace.list_files())
            self.assertIn("main.py:1:", workspace.search("hello"))
            self.assertNotIn("hidden.py", workspace.search("hello"))
            self.assertIn("2:     return 'world'", workspace.read_file("main.py", 2, 2))
            for path in ("../outside", ".env", "link/file", "node_modules/hidden.py"):
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    workspace.read_file(path)

    def test_sensitive_names_and_case_insensitive_exclusion(self):
        """凭据目录、扩展的密钥文件名，以及大小写变体都应被拦截。"""
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / ".aws").mkdir()
            (root / ".aws" / "credentials").write_text("secret_token", encoding="utf-8")
            (root / "id_ecdsa").write_text("secret_token", encoding="utf-8")
            (root / ".git-credentials").write_text("secret_token", encoding="utf-8")
            (root / ".git").mkdir()
            (root / ".git" / "config").write_text("secret_token", encoding="utf-8")
            (root / "app.py").write_text("print('ok')\n", encoding="utf-8")
            workspace = CodeWorkspace(root)
            listing = workspace.list_files()
            for name in (".aws", "id_ecdsa", ".git-credentials", ".git"):
                self.assertNotIn(name, listing)
            self.assertIn("未找到匹配内容", workspace.search("secret_token"))
            # 在不区分大小写的文件系统上，.GIT 与 .git 指向同一目录。
            for path in (".aws/credentials", "id_ecdsa", ".git-credentials", ".GIT/config", ".Git/config"):
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    workspace.read_file(path)



class SearchTests(unittest.TestCase):
    """search_code / find_files / read_file 的增强行为。"""

    def setUp(self):
        temp = tempfile.TemporaryDirectory()
        self.addCleanup(temp.cleanup)
        self.root = Path(temp.name).resolve()
        files = {
            "src/main/java/OrderService.java": "class OrderService {\n  void createOrder() {}\n  void created() {}\n}\n",
            "src/main/java/OrderController.java": "class OrderController {\n  // calls createOrder\n}\n",
            "src/test/java/OrderServiceTest.java": "class OrderServiceTest {\n  void testCreateOrder() {}\n}\n",
            "web/app.ts": "export function createOrder() {}\n",
            "web/app.min.js": "function createOrder(){}\n",
            "package-lock.json": "{\"createOrder\": 1}\n",
        }
        for name, content in files.items():
            path = self.root / name
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text(content, encoding="utf-8")
        # GBK 编码、含中文注释的旧代码。
        (self.root / "src/main/java/Legacy.java").write_bytes("// 订单服务\nclass Legacy { void createOrder() {} }\n".encode("gbk"))
        self.ws = CodeWorkspace(self.root)

    def test_gbk_files_are_searched_and_read(self):
        result = self.ws.search("createOrder", glob="Legacy.java")
        self.assertIn("src/main/java/Legacy.java:2: class Legacy", result)
        self.assertIn("按 GB18030 编码读取", result)
        content = self.ws.read_file("src/main/java/Legacy.java")
        self.assertIn("编码 GB18030", content.splitlines()[0])
        self.assertIn("1: // 订单服务", content)

    def test_generated_files_skipped_and_scope_filters(self):
        everywhere = self.ws.search("createOrder")
        self.assertNotIn("app.min.js", everywhere)
        self.assertNotIn("package-lock.json", everywhere)
        java = self.ws.search("createOrder", glob="*.java", path="src/main")
        self.assertNotIn("web/app.ts", java)
        self.assertNotIn("OrderServiceTest", java)
        self.assertIn("src/main/java/OrderService.java:2:", java)
        self.assertEqual(self.ws.search("createorder", case_sensitive=True, glob="*.ts"), "在 .（*.ts） 中未找到匹配内容。")
        braces = self.ws.search("createOrder", glob="*.{ts,java}", output="files")
        self.assertIn("web/app.ts（1）", braces)

    def test_regex_and_context(self):
        exact = self.ws.search(r"\bcreateOrder\(", regex=True, glob="OrderService*.java")
        self.assertIn("OrderService.java:2:", exact)
        self.assertNotIn("testCreateOrder", exact)
        with self.assertRaisesRegex(ValueError, "正则表达式无效"):
            self.ws.search("(", regex=True)
        context = self.ws.search("void createOrder", context=1, glob="OrderService.java")
        self.assertEqual(context.splitlines(), [
            "src/main/java/OrderService.java-1- class OrderService {",
            "src/main/java/OrderService.java:2:   void createOrder() {}",
            "src/main/java/OrderService.java-3-   void created() {}",
        ])

    def test_truncation_reports_totals_and_remaining_files(self):
        result = self.ws.search("order", limit=2)
        summary = next(line for line in result.splitlines() if line.startswith("[结果已截断"))
        self.assertIn("以上显示前 2 处", summary)
        self.assertIn("未显示的文件：", summary)
        self.assertIn("web/app.ts（1）", summary)
        count = self.ws.search("createOrder", output="count")
        self.assertTrue(count.startswith("共 5 处匹配，分布在 5 个文件："), count)

    def test_find_files(self):
        self.assertEqual(self.ws.find_files("*Controller.java"), "src/main/java/OrderController.java")
        self.assertEqual(self.ws.find_files("OrderService").splitlines(),
                         ["src/main/java/OrderService.java", "src/test/java/OrderServiceTest.java"])
        self.assertEqual(self.ws.find_files("src/**/*Test.java"), "src/test/java/OrderServiceTest.java")
        self.assertIn("共 4 个文件", self.ws.find_files("*.java", limit=2))
        self.assertEqual(self.ws.find_files("*.py"), "未找到匹配的文件。")

    def test_read_file_limits_and_total_lines(self):
        (self.root / "long.py").write_text("".join(f"line{i}\n" for i in range(1, 1201)), encoding="utf-8")
        # 省略 end：从起始行读取 500 行，并提示后续范围。
        result = self.ws.read_file("long.py")
        self.assertTrue(result.startswith("文件：long.py（共 1200 行）"))
        self.assertIn("500: line500", result)
        self.assertNotIn("501: line501", result)
        self.assertIn("以下还有 700 行，可继续读取 501-1000", result)
        # 只给 start 时从该行往后读，不再因默认结束行小于起始行而报错。
        result = self.ws.read_file("long.py", 801)
        self.assertIn("801: line801", result)
        self.assertIn("1200: line1200", result)
        self.assertNotIn("以下还有", result)
        self.assertIn("1000: line1000", self.ws.read_file("long.py", 1, 1000))
        with self.assertRaisesRegex(ValueError, "最多读取 1000 行"):
            self.ws.read_file("long.py", 1, 1001)

    @unittest.skipUnless(shutil.which("git"), "需要 git")
    def test_gitignore_is_respected(self):
        subprocess.run(["git", "init", "-q"], cwd=self.root, check=True)
        (self.root / ".gitignore").write_text("generated/\n", encoding="utf-8")
        (self.root / "generated").mkdir()
        (self.root / "generated" / "Out.java").write_text("void createOrder() {}\n", encoding="utf-8")
        self.assertNotIn("generated/Out.java", self.ws.search("createOrder"))
        self.assertNotIn("generated/Out.java", self.ws.find_files("*.java"))
        self.assertIn("src/main/java/OrderService.java", self.ws.find_files("*.java"))
        # 打开的目录本身被外层仓库忽略时，仍能搜索其中的文件。
        ignored = CodeWorkspace(self.root / "generated")
        self.assertIn("Out.java:1:", ignored.search("createOrder"))

if __name__ == "__main__":
    unittest.main()
