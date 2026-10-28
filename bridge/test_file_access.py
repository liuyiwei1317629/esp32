"""授权目录只读检索的测试。

覆盖三件事：清单默认空、只能手动加/关/删；检索只认文本、跳过密钥与日志；
拼出来的片段有上限，没命中时 prompt 一字不改。
"""

import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import file_access


class PermissionStoreTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.store = file_access.PermissionStore(
            os.path.join(self.root, "permissions.json"))
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(self.vault)

    def test_starts_empty(self):
        self.assertEqual(self.store.items(), [])
        self.assertEqual(self.store.enabled_folders(), [])

    def test_add_requires_confirm(self):
        with self.assertRaises(ValueError):
            self.store.add(self.vault)

    def test_add_rejects_file_and_missing(self):
        f = os.path.join(self.root, "a.txt")
        with open(f, "w", encoding="utf-8") as fh:
            fh.write("x")
        with self.assertRaises(ValueError):
            self.store.add(f, confirm=True)
        with self.assertRaises(ValueError):
            self.store.add(os.path.join(self.root, "nope"), confirm=True)

    def test_add_is_idempotent_and_enabled(self):
        first = self.store.add(self.vault, confirm=True)
        self.assertFalse(first["existed"])
        self.assertTrue(first["item"]["enabled"])
        again = self.store.add(self.vault + "\\", confirm=True)
        self.assertTrue(again["existed"])
        self.assertEqual(len(self.store.items()), 1)
        self.assertEqual(self.store.enabled_folders(), [self.vault])

    def test_toggle_and_remove(self):
        item = self.store.add(self.vault, confirm=True)["item"]
        self.assertIsNone(self.store.set_enabled("nope", False))
        self.store.set_enabled(item["id"], False)
        self.assertEqual(self.store.enabled_folders(), [])
        self.assertTrue(self.store.remove(item["id"]))
        self.assertFalse(self.store.remove(item["id"]))
        self.assertEqual(self.store.items(), [])


class SearchTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.store = file_access.PermissionStore(
            os.path.join(self.root, "permissions.json"))
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(os.path.join(self.vault, "notes"))
        os.makedirs(os.path.join(self.vault, "logs"))
        self._write("notes/plan.md", "# 计划\n喂食时间是早上九点\n再喂一次在下午五点\n")
        self._write("notes/plain.txt", "nothing interesting here\n")
        self._write("secrets.md", "喂食时间 token 12345\n")
        self._write("logs/run.md", "喂食时间 日志\n")
        self._write("blob.bin", "喂食时间\n")
        self.store.add(self.vault, confirm=True)

    def _write(self, rel, text):
        full = os.path.join(self.vault, rel)
        os.makedirs(os.path.dirname(full), exist_ok=True)
        with open(full, "w", encoding="utf-8") as fh:
            fh.write(text)

    def test_finds_cjk_snippet(self):
        hits = file_access.search("喂食时间是什么时候", self.store.enabled_folders())
        self.assertEqual(len(hits), 1)
        self.assertEqual(hits[0]["path"], "notes/plan.md")
        self.assertIn("早上九点", hits[0]["text"])
        self.assertEqual(hits[0]["line"], 1)  # 命中行是第 2 行，带 2 行上下文 → 从 1 开始

    def test_skips_secret_logs_and_binary(self):
        hits = file_access.search("喂食时间", self.store.enabled_folders())
        paths = [h["path"] for h in hits]
        self.assertIn("notes/plan.md", paths)
        self.assertNotIn("secrets.md", paths)
        self.assertNotIn("logs/run.md", paths)
        self.assertNotIn("blob.bin", paths)

    def test_unknown_term_hits_nothing(self):
        self.assertEqual(file_access.search("量子纠缠", self.store.enabled_folders()), [])

    def test_block_is_capped(self):
        snippets = [{"path": "a.md", "line": 1, "end_line": 5, "text": "字" * 400},
                    {"path": "b.md", "line": 1, "end_line": 5, "text": "字" * 400},
                    {"path": "c.md", "line": 1, "end_line": 5, "text": "字" * 400}]
        block = file_access.build_block(snippets)
        self.assertLessEqual(len(block), file_access.MAX_TOTAL_CHARS + 120)
        self.assertIn("a.md", block)


class WindowRankingTest(unittest.TestCase):
    """滑窗排序：文件头只提一次关键词，不该压过中间那段真正的参数表。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.store = file_access.PermissionStore(
            os.path.join(self.root, "permissions.json"))
        self.vault = os.path.join(self.root, "vault")
        os.makedirs(self.vault)
        filler = "\n".join("普通的一行说明 %d" % i for i in range(20))
        with open(os.path.join(self.vault, "pins.md"), "w", encoding="utf-8") as fh:
            fh.write("# 引脚说明\n" + filler + "\n"
                     "PIN_MIC_SD = 6    // 话筒数据\n"
                     "PIN_MIC_SCK = 4   // 话筒位时钟\n"
                     "PIN_MIC_WS = 5    // 话筒字选择\n"
                     "PIN_AMP_DIN = 7   // 功放数据\n")
        with open(os.path.join(self.vault, "index.md"), "w", encoding="utf-8") as fh:
            fh.write("# 引脚索引\n" + filler + "\n")
        self.store.add(self.vault, confirm=True)

    def test_dense_block_wins(self):
        hits = file_access.search("话筒引脚", self.store.enabled_folders())
        self.assertTrue(hits)
        self.assertEqual(hits[0]["path"], "pins.md")
        self.assertIn("PIN_MIC_SD", hits[0]["text"])

    def test_one_snippet_per_file(self):
        hits = file_access.search("话筒引脚", self.store.enabled_folders())
        paths = [h["path"] for h in hits]
        self.assertEqual(len(paths), len(set(paths)))


class FileAccessApplyTest(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = self.tmp.name
        self.store = file_access.PermissionStore(
            os.path.join(self.root, "permissions.json"))
        self.access = file_access.FileAccess(store=self.store)
        self.messages = [{"role": "system", "content": "人格"},
                         {"role": "user", "content": "喂食时间是什么时候"}]

    def _vault(self):
        vault = os.path.join(self.root, "vault")
        os.makedirs(vault, exist_ok=True)
        with open(os.path.join(vault, "plan.md"), "w", encoding="utf-8") as fh:
            fh.write("喂食时间是早上九点\n")
        self.store.add(vault, confirm=True)
        return vault

    def test_no_permission_is_free(self):
        out, info = self.access.apply(self.messages, "喂食时间")
        self.assertIs(out, self.messages)
        self.assertIsNone(info)

    def test_no_match_keeps_prompt(self):
        self._vault()
        out, info = self.access.apply(self.messages, "量子纠缠")
        self.assertEqual(out, self.messages)
        self.assertEqual(info["hits"], 0)
        self.assertEqual(info["folders"], 1)

    def test_match_prepends_snippet(self):
        self._vault()
        out, info = self.access.apply(self.messages, "喂食时间")
        self.assertEqual(info["hits"], 1)
        self.assertEqual(info["paths"], ["plan.md"])
        self.assertGreater(info["chars"], 0)
        self.assertEqual(out[0], self.messages[0])
        self.assertIn("plan.md", out[-1]["content"])
        self.assertIn("用户问题：喂食时间是什么时候", out[-1]["content"])
        self.assertNotIn("plan.md", self.messages[-1]["content"])

    def test_disabled_folder_not_searched(self):
        vault = self._vault()
        item = self.store.items()[0]
        self.store.set_enabled(item["id"], False)
        out, info = self.access.apply(self.messages, "喂食时间")
        self.assertIsNone(info)
        self.assertEqual(out, self.messages)
        self.assertNotIn("plan.md", out[-1]["content"])


if __name__ == "__main__":
    unittest.main()
