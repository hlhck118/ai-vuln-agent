# -*- coding: utf-8 -*-
"""Skills 管理（`pentestd/skills.py` 的 import_dir / delete_local + 两条路由）。

这份测试守的是三件在别处看不出来的事：

1. **导入只读调用方点名的目录**，同名不覆盖，坏输入说清原因；
2. **删除只认 `data/skills/` 下自己那份**：插件贡献的手册不从这里删——删了会在
   下次安装时回来，而"删了又回来"只会被读成"这个按钮没生效"；
3. 技能目录是**设置页的一组**，那次性的授权门与 `/api/v1/drill` 已经撤掉。
"""
import os
import shutil
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import skills                                           # noqa: E402
from tests import support                                            # noqa: E402


class ImportTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="skills-import-")
        self.data_dir = self.directory
        self.source = os.path.join(self.directory, "source")
        os.makedirs(self.source)
        with open(os.path.join(self.source, "active-scan.md"), "w", encoding="utf-8") as fh:
            fh.write("# 主动扫描\n\n先用 ICMP，再上 TCP。\n")
        with open(os.path.join(self.source, "notes.txt"), "w", encoding="utf-8") as fh:
            fh.write("不是 markdown，应当忽略。\n")
        with open(os.path.join(self.source, "with-front.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: with-front\ndescription: 已带 frontmatter\n---\n\n正文\n")

    def tearDown(self):
        skills.invalidate()
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_imports_markdown_and_skips_everything_else(self):
        outcome = skills.import_dir(self.data_dir, self.source)
        names = [item["name"] for item in outcome["imported"]]
        self.assertEqual(["active-scan", "with-front"], names)
        self.assertEqual([], [item for item in outcome["skipped"]
                              if item["name"] == "notes.txt"])

    def test_the_original_title_becomes_a_description(self):
        skills.import_dir(self.data_dir, self.source)
        path = os.path.join(self.data_dir, "skills", "active-scan.md")
        with open(path, encoding="utf-8") as fh:
            text = fh.read()
        self.assertTrue(text.startswith("---"))
        self.assertIn("source: " + skills.IMPORT_MARK, text)
        self.assertIn("description: 主动扫描", text)

    def test_existing_files_are_never_overwritten(self):
        target = os.path.join(self.data_dir, "skills")
        os.makedirs(target)
        with open(os.path.join(target, "active-scan.md"), "w", encoding="utf-8") as fh:
            fh.write("我自己改过的那一份\n")
        outcome = skills.import_dir(self.data_dir, self.source)
        self.assertEqual(["with-front"], [item["name"] for item in outcome["imported"]])
        self.assertTrue(any(item["name"] == "active-scan.md"
                            and "不覆盖" in item["reason"] for item in outcome["skipped"]))
        with open(os.path.join(target, "active-scan.md"), encoding="utf-8") as fh:
            self.assertEqual("我自己改过的那一份\n", fh.read())

    def test_a_missing_directory_is_an_error_not_a_silent_noop(self):
        with self.assertRaises(ValueError) as caught:
            skills.import_dir(self.data_dir, os.path.join(self.directory, "nope"))
        self.assertIn("目录不存在", str(caught.exception))

    def test_an_oversized_file_is_skipped_with_the_limit_named(self):
        big = os.path.join(self.source, "huge.md")
        with open(big, "w", encoding="utf-8") as fh:
            fh.write("x" * (skills.MAX_SKILL_BYTES + 10))
        outcome = skills.import_dir(self.data_dir, self.source)
        reasons = {item["name"]: item["reason"] for item in outcome["skipped"]}
        self.assertIn("huge.md", reasons)
        self.assertIn("200 KB", reasons["huge.md"])

    def test_imported_skills_show_up_in_the_catalog_and_say_where_they_came_from(self):
        skills.import_dir(self.data_dir, self.source)
        rows = {item["name"]: item for item in skills.catalog(None, self.data_dir)}
        self.assertIn("active-scan", rows)
        self.assertIn("with-front", rows)
        self.assertTrue(rows["active-scan"]["imported"])
        self.assertEqual("本地", rows["active-scan"]["source"])
        # 界面读的是 public()：它多一个 deletable（只有本地那份能删）。
        self.assertTrue(skills.public(rows["active-scan"])["deletable"])
        # 手放进去、没带标记的那一份：可删，但不算"导入的"。
        plain = os.path.join(self.data_dir, "skills", "hand-written.md")
        with open(plain, "w", encoding="utf-8") as fh:
            fh.write("# 手写\n")
        skills.invalidate()
        rows = {item["name"]: item for item in skills.catalog(None, self.data_dir)}
        self.assertFalse(rows["hand-written"]["imported"])
        self.assertTrue(skills.public(rows["hand-written"])["deletable"])


class DeleteTests(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="skills-delete-")
        self.data_dir = self.directory
        self.target = os.path.join(self.data_dir, "skills")
        os.makedirs(self.target)

    def tearDown(self):
        skills.invalidate()
        shutil.rmtree(self.directory, ignore_errors=True)

    def _write(self, name, body="# 手册\n"):
        path = os.path.join(self.target, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        skills.invalidate()
        return path

    def test_deletes_the_local_copy_and_reports_where_it_was(self):
        path = self._write("mine.md")
        outcome = skills.delete_local(self.data_dir, "mine")
        self.assertTrue(outcome["ok"])
        self.assertEqual(path, outcome["path"])
        self.assertFalse(os.path.exists(path))
        self.assertEqual([], skills.catalog(None, self.data_dir))

    def test_a_name_that_is_not_local_here_is_a_key_error(self):
        """插件手册不在 `data/skills` 里，所以这里删不到它 —— 调用方回 404 并指路。"""
        self._write("mine.md")
        with self.assertRaises(KeyError):
            skills.delete_local(self.data_dir, "plugin-handbook")
    def test_an_empty_name_is_a_value_error(self):
        with self.assertRaises(ValueError):
            skills.delete_local(self.data_dir, "   ")

    def test_only_a_markdown_file_inside_the_directory_can_be_deleted(self):
        """`../` 之类拼出来的路径不在本地目录里，一定删不到。"""
        outside = os.path.join(self.data_dir, "outside.md")
        with open(outside, "w", encoding="utf-8") as fh:
            fh.write("# 不属于技能\n")
        with self.assertRaises((KeyError, ValueError)):
            skills.delete_local(self.data_dir, "../outside")
        self.assertTrue(os.path.exists(outside), "删到了 data/skills 之外的文件")

    def test_the_listed_name_is_what_deletion_takes(self):
        """列表里显示什么名字，删除就收什么名字（不是文件名）。"""
        self._write("from-title.md", "---\nname: 中文标题\n---\n\n正文\n")
        names = [item["name"] for item in skills.catalog(None, self.data_dir)]
        self.assertEqual(["中文标题"], names)
        with self.assertRaises(KeyError):
            skills.delete_local(self.data_dir, "from-title")
        self.assertTrue(skills.delete_local(self.data_dir, "中文标题")["ok"])


class HttpTests(unittest.TestCase):
    """走真实 HTTP：列表里的读数、两条写路由的管理员闸、旧路由已经 404。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("skills-http.db")
        self.httpd, self.client = support.start(self.store)
        self.source = os.path.join(self.directory.name, "source")
        os.makedirs(self.source)
        with open(os.path.join(self.source, "imported-manual.md"), "w",
                  encoding="utf-8") as fh:
            fh.write("# 导入来的\n\n正文。\n")

    def tearDown(self):
        skills.invalidate()
        support.stop(self.httpd, self.store, self.directory)

    def test_the_listing_reports_the_directory_and_the_imported_count(self):
        status, payload = self.client.get("/api/v1/skills")
        self.assertEqual(200, status, payload)
        self.assertIn("skills", payload)
        self.assertIn("dir", payload)
        self.assertEqual(0, payload.get("imported"))
        self.assertTrue(payload["dir"].endswith("skills"))

    def test_the_drill_routes_are_gone(self):
        for method, path in (("get", "/api/v1/drill"),):
            status, _payload = getattr(self.client, method)(path)
            self.assertEqual(404, status, "%s 还在" % path)

    def test_import_needs_a_directory_and_refuses_a_missing_one(self):
        status, payload = self.client.post("/api/v1/skills/import", {})
        self.assertEqual(400, status, payload)
        self.assertIn("sourceDir", payload.get("error", ""))

        status, payload = self.client.post(
            "/api/v1/skills/import",
            {"sourceDir": os.path.join(self.directory.name, "nope")})
        self.assertEqual(400, status, payload)
        self.assertIn("目录不存在", payload.get("error", ""))

    def test_admin_imports_then_deletes(self):
        status, payload = self.client.post("/api/v1/skills/import",
                                           {"sourceDir": self.source})
        self.assertEqual(200, status, payload)
        self.assertEqual(["imported-manual"],
                         [item["name"] for item in payload["imported"]])
        # 目录里没有 dsh-purge 时，破甲那一步只是没做，不该让整次导入失败。
        self.assertFalse(payload["purge"]["imported"])

        status, payload = self.client.get("/api/v1/skills")
        self.assertEqual(200, status, payload)
        self.assertEqual(1, payload.get("imported"))

        status, payload = self.client.post("/api/v1/skills/remove",
                                           {"name": "imported-manual"})
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["deleted"])

        status, payload = self.client.post("/api/v1/skills/remove",
                                           {"name": "imported-manual"})
        self.assertEqual(404, status, payload)

    def test_a_stranger_cannot_write(self):
        """换一个**没登录过**的客户端：导入与删除都是写动作，未登录不该能写。"""
        import json
        import urllib.error
        import urllib.request
        for path, body in (("/api/v1/skills/import", {"sourceDir": self.source}),
                           ("/api/v1/skills/remove", {"name": "whatever"})):
            request = urllib.request.Request(
                self.client.base + path, data=json.dumps(body).encode("utf-8"),
                headers={"Content-Type": "application/json"}, method="POST")
            try:
                with urllib.request.urlopen(request, timeout=20) as response:
                    status = response.status
            except urllib.error.HTTPError as exc:
                status = exc.code
            self.assertIn(status, (401, 403), "%s 对未登录客户端回了 %d" % (path, status))


class DirectorySkillTests(unittest.TestCase):
    """目录型手册：一个目录 + 一份 SKILL.md 算**一份**技能。

    这套布局来自 Artex 的 `skills/`：`api-recon/SKILL.md` 旁边还有 `reference.md` 与
    `scripts/*.py`。旧规则（`os.walk` 收所有 `.md`）会把它拆成三份互相独立的手册，
    其中 `reference` 那一份脱离 `SKILL.md` 没有意义——所以认到入口文件就停止下钻。
    """

    def setUp(self):
        self.directory = tempfile.mkdtemp(prefix="skills-dir-")
        self.data_dir = self.directory
        self.base = os.path.join(self.data_dir, "skills")
        self.skill = os.path.join(self.base, "browser-thing")
        os.makedirs(os.path.join(self.skill, "references"))
        os.makedirs(os.path.join(self.skill, "scripts"))
        with open(os.path.join(self.skill, "SKILL.md"), "w", encoding="utf-8") as fh:
            fh.write("---\nname: browser-thing\ndescription: 点页面用的\n---\n\n正文\n")
        with open(os.path.join(self.skill, "reference.md"), "w", encoding="utf-8") as fh:
            fh.write("# 参照表\n\nA 到 J 节。\n")
        with open(os.path.join(self.skill, "references", "deep.md"), "w", encoding="utf-8") as fh:
            fh.write("# 更深的一层\n")
        with open(os.path.join(self.skill, "scripts", "run.py"), "w", encoding="utf-8") as fh:
            fh.write("print(1)\n")

    def tearDown(self):
        skills.invalidate()
        shutil.rmtree(self.directory, ignore_errors=True)

    def test_one_directory_is_one_skill_and_it_lists_its_files(self):
        rows = skills.catalog(None, self.data_dir)
        names = [item["name"] for item in rows]
        self.assertIn("browser-thing", names)
        # `reference` / `deep` / `run` 都**不该**成为独立技能。
        self.assertNotIn("reference", names)
        self.assertNotIn("deep", names)
        item = [row for row in rows if row["name"] == "browser-thing"][0]
        self.assertTrue(item["directory"])
        self.assertEqual("本地", item["source"])
        self.assertIn("reference.md", item["files"])
        self.assertIn("scripts/run.py", item["files"])
        # 入口文件自己不算附属文件——它就是正文。
        self.assertNotIn("SKILL.md", item["files"])
        public = skills.public(item)
        self.assertTrue(public["directory"])
        self.assertEqual(item["files"], public["files"])

    def test_a_single_file_skill_still_works_and_reports_no_files(self):
        with open(os.path.join(self.base, "plain.md"), "w", encoding="utf-8") as fh:
            fh.write("# 单个文件\n")
        skills.invalidate()
        item = [row for row in skills.catalog(None, self.data_dir)
                if row["name"] == "plain"][0]
        self.assertFalse(item["directory"])
        self.assertEqual([], item["files"])

    def test_reading_one_attachment(self):
        item = [row for row in skills.catalog(None, self.data_dir)
                if row["name"] == "browser-thing"][0]
        got = skills.file_of(item, "reference.md")
        self.assertTrue(got["ok"], got)
        self.assertIn("参照表", got["text"])
        self.assertFalse(got["truncated"])

    def test_the_entry_file_is_the_body_not_an_attachment(self):
        item = [row for row in skills.catalog(None, self.data_dir)
                if row["name"] == "browser-thing"][0]
        got = skills.file_of(item, skills.SKILL_ENTRY)
        self.assertFalse(got["ok"])
        self.assertIn("load_skill", got["reason"])

    def test_a_traversal_attempt_is_refused(self):
        """附属文件名是调用方给的，`../../windows/system32/x` 必须挡在手册目录里。"""
        item = [row for row in skills.catalog(None, self.data_dir)
                if row["name"] == "browser-thing"][0]
        got = skills.file_of(item, "../../../secret.txt")
        self.assertFalse(got["ok"])
        self.assertTrue(got["reason"])
        self.assertEqual("", got["text"])

    def test_a_missing_attachment_says_so(self):
        item = [row for row in skills.catalog(None, self.data_dir)
                if row["name"] == "browser-thing"][0]
        got = skills.file_of(item, "nope.md")
        self.assertFalse(got["ok"])
        self.assertIn("没有这个文件", got["reason"])


if __name__ == "__main__":
    unittest.main(verbosity=2)
