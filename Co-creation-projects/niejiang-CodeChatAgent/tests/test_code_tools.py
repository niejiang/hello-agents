"""验证代码工具的基本结果和项目目录边界。"""

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
            self.assertEqual(workspace.search("secret_token"), "未找到匹配内容。")
            # 在不区分大小写的文件系统上，.GIT 与 .git 指向同一目录。
            for path in (".aws/credentials", "id_ecdsa", ".git-credentials", ".GIT/config", ".Git/config"):
                with self.subTest(path=path), self.assertRaises((ValueError, OSError)):
                    workspace.read_file(path)


if __name__ == "__main__":
    unittest.main()
