# -*- coding: utf-8 -*-
"""交付打包的安全边界（`tools/make_handoff_zip.py`）。

这个脚本**曾经把包做成 45% 的开发期垃圾**（149/332）：`.scratch/` 的探针脚本与
`.dsh-vision-toolkit/` 的截图转储，里面还包括一份 `pentestd/` 的**备份副本**——
收包人会看到两份 `server.py`。根因是排除清单按目录名枚举，而临时目录是后来才出现的。

所以断言分两层，两层都不能省：

1. **规则本身**——表驱动，逐条钉住"该排除的排除、该留下的留下"，包括
   `data/skills/` 这个**在排除前缀之下**的例外，以及"例外只放开前缀、不放开机密后缀"。
2. **对真实仓库树的收集结果**——只测规则的话，一个"规则写对了但 `collect()` 没调它"
   的改动能全绿通过；反过来只测结果的话，一条规则被删掉也可能侥幸不改变结果。

`data/skills/` 那两条尤其要留着：它在 `data/` 下面，而 `data/` 是整棵排除的。
整棵剪枝先于子目录判定，所以**例外曾经真的不生效**（规则对、包里却什么都没有），
而症状只是"技能列表是空的"。
"""
import contextlib
import importlib.util
import io
import os
import re
import sys
import tempfile
import time
import unittest
import zipfile

HERE = os.path.dirname(os.path.abspath(__file__))
# 布局上移后仓库根与工作台根是同一层；被测模块自己也算出同样的值。
PLATFORM_ROOT = os.path.dirname(HERE)
REPO_ROOT = PLATFORM_ROOT
MODULE_PATH = os.path.join(PLATFORM_ROOT, "tools", "make_handoff_zip.py")


def _load_module():
    spec = importlib.util.spec_from_file_location("make_handoff_zip", MODULE_PATH)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


mod = _load_module()


def _norm(rel):
    return rel.replace("\\", "/")


def _prose(text):
    """去掉围栏代码块：围栏里的 `[x](y)` 是**示例输出**，不是链接。

    第三方技能手册里会粘一整段命令行回显，里面就有
    `[Snapshot](.playwright-cli/page-2026-02-14T19-22-42-679Z.yml)` 这样的行——
    那是上一次运行生成的文件名。渲染器不会把围栏里的字做成链接，所以拿它去要求
    "包里必须有这个文件"，等于要求仓库里留着一个运行期产物。
    """
    kept = []
    fence = None
    for line in text.splitlines():
        stripped = line.lstrip()
        if fence is None:
            if stripped.startswith("```") or stripped.startswith("~~~"):
                fence = stripped[:3]
                continue
            kept.append(line)
        elif stripped.startswith(fence):
            fence = None
    return "\n".join(kept)


# `[文字](目标)` 与 `![说明](目标)`。目标里的空格是标题分隔，所以 `[^)\s]+` 在
# 空格处截断——与渲染器的取法一致。
_LINK = re.compile(r"!?\[[^\]]*\]\(([^)\s]+)\)")


class ExclusionRuleTestCase(unittest.TestCase):
    """规则层：一条路径该不该进包，以及**为什么**。"""

    def test_the_table(self):
        cases = [
            # ---- 该进包 ----
            ("项目说明.md", False),
            ("pentestd/server.py", False),
            ("web/app.js", False),
            ("tools/make_handoff_zip.py", False),
            ("tests/test_probe.py", False),
            (".gitignore", False),        # 点开头的文件有白名单
            ("prototype/vulnagent/orchestrator.py", False),
            ("docs/adr/0001-x.md", False),
            # ---- 该排除：开发期工具目录 ----
            (".scratch/t48/stream_smoke.py", True),
            (".dsh-vision-toolkit/artifacts/x.png", True),
            (".render-fixture/a.html", True),
            (".pytest_cache/v/cache", True),
            ("__pycache__/x.pyc", True),
            ("prototype/vulnagent/__pycache__/a.cpython-312.pyc", True),
            ("data/dsh-home/profiles/x/node_modules/y.js", True),
            # ---- 该排除：运行期机密 ----
            ("data/pentest.db", True),
            ("data/pentest.db-wal", True),
            ("data/secret.key", True),
            ("data/pki/ca.key", True),
            ("data/access.log", True),
            ("deploy/node.key", True),
            ("deploy/node.crt", True),
            ("deploy/identity.json", True),
            # ---- 该排除：运行期数据与大块下载物 ----
            ("data/tools/nmap/nmap-7.92/nmap.exe", True),
            ("data/hub/plugins-zh.json", True),
            ("memory/cases.db", True),
            ("prototype/memory/x.db", True),
            ("prototype/out/report.md", True),
            # ---- 该进包：排除前缀之下的例外 ----
            ("data/skills/external-recon.md", False),
            ("data/skills/sub/deep.md", False),
            # ---- 例外只放开前缀，不放开机密与缓存 ----
            ("data/skills/leak.key", True),
            ("data/skills/leak.pyc", True),
            ("data/skills/.hidden.md", True),
        ]
        wrong = []
        for rel, should_exclude in cases:
            reason = mod.excluded(rel)
            if bool(reason) != should_exclude:
                wrong.append((rel, should_exclude, reason or "(未排除)"))
        self.assertEqual([], wrong, "规则判定与预期不符：%s" % (wrong,))

    def test_the_reason_names_the_mechanism(self):
        """理由要能区分"哪条规则拦的"。

        只断言布尔值的话，"点开头的目录"那条被删掉、恰好被别的规则兜住时，
        测试还是绿的——而排查包为什么变大时，人正是靠这句话定位。
        """
        self.assertIn("点开头", mod.excluded(".scratch/a.py"))
        self.assertIn("运行期数据", mod.excluded("data/tools/x/y.exe"))
        self.assertIn("运行期机密", mod.excluded("data/secret.key"))
        self.assertIn("缓存", mod.excluded("__pycache__/a.pyc"))

    def test_whole_dir_pruning_does_not_swallow_the_exception(self):
        """整棵剪枝前必须先看一眼"这棵树里还压着不放行的东西吗"。

        少了那一条，`data/` 会在走到 `data/skills/` 之前被整棵剪掉：规则说
        `data/skills/x.md` 不该排除，包里却一份都没有。这是本脚本上一个真实缺陷。
        """
        self.assertEqual("", mod.whole_dir_excluded("data"))
        self.assertEqual("", mod.whole_dir_excluded("data/skills"))
        self.assertNotEqual("", mod.whole_dir_excluded("data/tools"))
        self.assertNotEqual("", mod.whole_dir_excluded(".scratch"))
        self.assertNotEqual("", mod.whole_dir_excluded("prototype/memory"))


class CollectOnTheRealTreeTestCase(unittest.TestCase):
    """结果层：对真实仓库树跑一遍。"""

    @classmethod
    def setUpClass(cls):
        cls.included, cls.skipped = mod.collect()

    def test_nothing_included_can_be_excluded(self):
        offenders = [(r, mod.excluded(r)) for r in self.included if mod.excluded(r)]
        self.assertEqual([], offenders[:5],
                         "收录清单里有能被排除规则说中的条目")

    def test_no_dot_directory_reaches_the_package(self):
        offenders = [r for r in self.included
                     if any(seg.startswith(".")
                            for seg in _norm(r).split("/")[:-1])]
        self.assertEqual([], offenders[:5])

    def test_known_junk_trees_are_absent(self):
        needles = (".scratch", ".dsh-vision-toolkit", ".render-fixture",
                   "__pycache__", "data/tools/", "secret.key", "pentest.db",
                   "memory/", "prototype/out/", ".pyc", ".log")
        for needle in needles:
            hits = [r for r in self.included if needle in _norm(r)]
            self.assertEqual([], hits[:3], "包里出现了 %r" % needle)

    def test_the_skills_exception_really_lands_in_the_package(self):
        """例外必须**在包里**，而不只是"规则说它不该排除"。

        判据不是"这棵树里只有 `.md`"：一份手册可以是一个目录（`<名字>/SKILL.md`
        加参照表与 `scripts/`），那些附属文件本来就是它的一部分，跟着进包是对的。
        所以每一条要么自己是 `.md`，要么**待在某个有 `SKILL.md` 的目录之下**；
        照 `.md` 一刀切，会把 `api-recon/scripts/*.py` 这种正常附件判成脏东西。
        """
        skills = [r for r in self.included
                  if _norm(r).startswith("data/skills/")]
        self.assertTrue(skills, "data/skills/ 下的作业手册没有进包")
        entries = set(_norm(r) for r in skills)
        loose = []
        for rel in skills:
            rel = _norm(rel)
            if rel.endswith(".md"):
                continue
            parts = rel.split("/")
            owners = ["/".join(parts[:i] + ["SKILL.md"])
                      for i in range(2, len(parts))]
            if any(candidate in entries for candidate in owners):
                continue
            loose.append(rel)
        self.assertEqual([], loose[:3],
                         "data/skills/ 里出现了不属于任何手册的文件：%s" % (loose[:3],))

    def test_the_package_is_mostly_product_code(self):
        """包体积的大头必须是产品代码，不是转储。

        上面几条是"某棵树不在"，这一条是它的反面：**包必须主要是有用的东西**。
        只删不补的改法（比如把规则收紧到什么都不剩）会被这一条抓住。
        """
        core = [r for r in self.included
                if _norm(r).startswith(("pentestd/",
                                        "web/",
                                        "tests/",
                                        "prototype/vulnagent/"))]
        self.assertGreater(len(core), 100,
                           "产品代码只有 %d 个文件，包结构可能被改坏了" % len(core))
        self.assertLess(len(self.included), 400,
                        "收录 %d 个文件，包体积失控" % len(self.included))

    def test_every_delivered_doc_link_resolves_inside_the_package(self):
        """包是**自包含**的：收包人只拿到这个 zip，没有开发树。

        所以包内文档的相对链接必须落在包里。这个坑踩过两次，方向相反：
        发掘引擎并入 `prototype/` 时多了一层，`../docs/` 于是指到了 `docs/`；
        2026-10-21 整体上移之后又少了一层，`../../docs/` 指到了包外。
        两次的症状都只是一句话点不开，**收包人不说，写的人永远不会知道**，
        所以用测试钉住。

        解析到仓库之外的链接跳过：那说明本机不是完整交付包（只拷了工作台），
        本机无从判断。
        """
        included = set(_norm(r) for r in self.included)
        root = os.path.abspath(mod.REPO_ROOT)
        offenders = []
        for rel in self.included:
            if not _norm(rel).endswith(".md"):
                continue
            path = os.path.join(root, rel)
            with open(path, encoding="utf-8", errors="replace") as fh:
                text = _prose(fh.read())
            for target in _LINK.findall(text):
                if target.startswith(("http://", "https://", "mailto:", "#")):
                    continue
                target = target.split("#")[0]
                if not target:
                    continue
                resolved = os.path.abspath(
                    os.path.normpath(os.path.join(os.path.dirname(path), target)))
                if resolved != root and not resolved.startswith(root + os.sep):
                    continue
                inside = _norm(os.path.relpath(resolved, root))
                if inside in included:
                    continue
                # 指向目录也算数：只要那棵树里有文件进包
                if any(r.startswith(inside + "/") for r in included):
                    continue
                offenders.append((rel, target))
        self.assertEqual([], offenders[:5],
                         "包内文档引用了包里没有的东西：%s" % (offenders[:3],))


class CollectOnASyntheticTreeTestCase(unittest.TestCase):
    """用自建的树测 `collect()` 本身，不依赖开发树里恰好有什么。

    **这一条是被交付包打红才补上的。** 原来有一条断言"`.scratch/` 在剪枝明细里"，
    在开发树上恒绿——但 `.scratch/` 正是被排除掉的，所以**打包出来的那份代码跑
    自己的测试会红**：包里没有那棵树，也就没有那一行明细。

    教训不是"少断言"，是**对交付物要验交付物**：同一个测试在源码树和包里都得成立。
    这里改成把 `REPO_ROOT` / `INCLUDE_TOP` 指到一棵自建的树上，两种环境都一样。
    """

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="handoff-tree-")
        self.addCleanup(tmp.cleanup)
        self.root = tmp.name

        def put(rel):
            path = os.path.join(self.root, rel.replace("/", os.sep))
            os.makedirs(os.path.dirname(path), exist_ok=True)
            # 一定要 with：`open(...).write(...)` 会漏一个句柄，而它的症状只是一条
            # ResourceWarning——在测试输出里长得很像偶发失败。
            with open(path, "w", encoding="utf-8") as handle:
                handle.write("x")

        for rel in ("项目说明.md",
                    "pentestd/server.py",
                    "web/app.js",
                    "data/pentest.db",       # 运行期机密
                    "data/tools/nmap/nmap.exe",   # 大块下载物
                    "data/skills/a.md",      # 例外
                    "data/skills/deep/b.md",  # 例外（深层）
                    ".scratch/probe.py",     # 点开头
                    ".scratch/sub/dom.html",  # 点开头（深层）
                    "__pycache__/x.pyc",
                    "prototype/memory/cases.db",
                    "prototype/vulnagent/orchestrator.py"):
            put(rel)

        saved = (mod.REPO_ROOT, mod.INCLUDE_TOP)
        self.addCleanup(lambda: setattr(mod, "REPO_ROOT", saved[0]))
        self.addCleanup(lambda: setattr(mod, "INCLUDE_TOP", saved[1]))
        mod.REPO_ROOT = self.root
        # 白名单写的是**顶层交付物**，不再是"包着一切的那一个总目录"。
        # `.scratch/`、`__pycache__/` 故意不在里面：它们该由"顶层未收录"报出来，
        # 而不是靠走进它们再逐条排除。
        mod.INCLUDE_TOP = ("项目说明.md", "pentestd", "web", "data", "prototype")

    def test_included_is_exactly_what_should_survive(self):
        included, _ = mod.collect()
        self.assertEqual(
            {"项目说明.md",
             "pentestd/server.py",
             "web/app.js",
             "data/skills/a.md",
             "data/skills/deep/b.md",
             "prototype/vulnagent/orchestrator.py"},
            {_norm(r) for r in included})

    def test_pruned_trees_are_reported_one_line_each(self):
        """剪掉的树要在排除明细里留一行，否则"少了一棵树"查不出来。

        两种失法方向相反，所以两种都钉：
        - 收录进来的树**内部**被剪掉的子树（`data/tools/`、`prototype/memory/`），
          由 `whole_dir_excluded()` 剪枝时记一行；
        - 顶层整棵不在 `INCLUDE_TOP` 里的目录（`.scratch/`、`__pycache__/`），
          走不到剪枝，由"顶层未收录"那段记一行，理由取命中的规则。
        """
        _, skipped = mod.collect()
        pruned = {_norm(rel) for rel, _ in skipped if rel.endswith("/")}
        for expected in (".scratch/",
                         "data/tools/",
                         "__pycache__/",
                         "prototype/memory/"):
            self.assertIn(expected, pruned, "排除明细里没有 %s" % expected)
        # 整棵剪掉就不再往里走：里面那一层不该单独出现一行
        self.assertNotIn("data/tools/nmap/", pruned)
        self.assertNotIn(".scratch/sub/", pruned)
        # 每一条明细都要带理由，光有路径读的人不知道是规则拦的还是漏装的
        for rel, reason in skipped:
            self.assertTrue(reason, "排除项没有理由：%s" % rel)

    def test_the_exception_survives_pruning_without_taking_the_secrets(self):
        """例外与机密在**同一棵树**下，两条方向都要成立。"""
        _, skipped = mod.collect()
        excluded = {_norm(rel) for rel, _ in skipped}
        self.assertNotIn("data/skills/a.md", excluded)
        self.assertIn(_norm("data\\pentest.db"), excluded)

    def test_a_missing_top_level_entry_is_reported_not_skipped(self):
        """顶层条目整棵不见时，光靠 `collect()` 是会静默通过的。

        这一条守的是 `main()` 那道前置判定：`collect()` 遇到不存在的顶层条目直接
        `continue`，而 `verify_package()` 只比对「包内 == 收录清单」——两条合起来，
        少一整棵顶层目录照样自检通过。所以缺项必须由 `missing_top()` 单独报出来。
        """
        mod.INCLUDE_TOP = ("项目说明.md", "docs")      # 这棵自建树上没有 docs
        self.assertEqual(["docs"], mod.missing_top())
        # 而 collect() 确实会静默跳过它——这正是需要那道前置判定的原因
        self.assertEqual({"项目说明.md"}, {_norm(r) for r in mod.collect()[0]})


class VerifyPackageTestCase(unittest.TestCase):
    """校验层：一个错的包必须被拒绝，一个对的包必须通过。"""

    def _zip(self, tmp, entries):
        path = os.path.join(tmp, "p.zip")
        with zipfile.ZipFile(path, "w") as zf:
            for name, body in entries:
                zf.writestr(name, body)
        return path

    def test_accepts_a_correct_package(self):
        with tempfile.TemporaryDirectory() as tmp:
            included = ["pentestd/server.py", "项目说明.md"]
            entries = [("root/索引.md", "i")] + \
                      [("root/" + r, "x") for r in included]
            path = self._zip(tmp, entries)
            self.assertEqual([], mod.verify_package(path, included, "root"))

    def test_rejects_an_extra_entry(self):
        with tempfile.TemporaryDirectory() as tmp:
            included = ["项目说明.md"]
            entries = [("root/索引.md", "i"), ("root/notes.md", "x"),
                       ("root/.scratch/a.py", "leak")]
            path = self._zip(tmp, entries)
            problems = mod.verify_package(path, included, "root")
            self.assertTrue(any("未收录" in p for p in problems), problems)

    def test_rejects_a_missing_entry(self):
        """漏装是这类打包最常见的失败方式，而解压不报错、只是少了文件。"""
        with tempfile.TemporaryDirectory() as tmp:
            included = ["项目说明.md", "web/app.js"]
            entries = [("root/索引.md", "i"), ("root/notes.md", "x")]
            path = self._zip(tmp, entries)
            problems = mod.verify_package(path, included, "root")
            self.assertTrue(any("缺少条目" in p for p in problems), problems)

    def test_rejects_a_secret_that_the_list_itself_carries(self):
        """集合相等只能防"漏装"，防不住"清单自己错了"。

        这一条是第二道复核存在的理由：如果校验只做集合相等，那么"收录清单里带着
        `data/secret.key`"的包会完全通过——而它正是最该被拦下的那一种。
        """
        with tempfile.TemporaryDirectory() as tmp:
            bad = "data/secret.key"
            entries = [("root/索引.md", "i"), ("root/" + bad, "KEY")]
            path = self._zip(tmp, entries)
            problems = mod.verify_package(path, [bad], "root")
            self.assertTrue(any("应排除" in p for p in problems), problems)


class CrossVersionTestCase(unittest.TestCase):
    """上一版有、这一版没有的文件，必须让打包**停下来**。

    这是本文件里唯一一条"失败发生在两次打包之间"的断言，背景是真实事故：
    2026-10-21 19:22 与 19:28 两次打包之间，`使用手册.docx` 被删掉，包从 267 个
    文件变成 266，而"条目集合相等、排除规则复核、压缩流完整性 均通过"照样打印。
    三条检查问的都是"收录清单被遵守了吗"，**没有一条问"上一版有的东西还在吗"**。
    """

    LABEL = "root"

    def setUp(self):
        tmp = tempfile.TemporaryDirectory(prefix="handoff-diff-")
        self.addCleanup(tmp.cleanup)
        self.dir = tmp.name
        self.out = os.path.join(self.dir, "out")
        os.makedirs(self.out)
        self.root = os.path.join(self.dir, "tree")
        os.makedirs(self.root)
        self.target = os.path.join(self.out, "new.zip")

        saved = (mod.REPO_ROOT, mod.INCLUDE_TOP, sys.argv)
        self.addCleanup(lambda: (setattr(mod, "REPO_ROOT", saved[0]),
                                 setattr(mod, "INCLUDE_TOP", saved[1]),
                                 setattr(sys, "argv", saved[2])))
        mod.REPO_ROOT = self.root
        # 白名单真正取值在 `put()` 里：那时这棵假树才长出顶层条目。
        mod.INCLUDE_TOP = ()

    # -- 小工具 -------------------------------------------------------------
    def put(self, rel, body):
        path = os.path.join(self.root, rel.replace("/", os.sep))
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8", newline="") as fh:
            fh.write(body)
        # 白名单跟着这棵假树长：`setUp` 时树还是空的，条目是之后一条条 `put` 进来的。
        mod.INCLUDE_TOP = tuple(sorted(os.listdir(self.root)))

    def package(self, name, entries, age=3600):
        """造一个旧包；`age` 秒是它的"年龄"，用来决定谁是上一版。"""
        path = os.path.join(self.out, name)
        with zipfile.ZipFile(path, "w") as zf:
            for rel, body in entries.items():
                zf.writestr(rel, body)
        stamp = time.time() - age
        os.utime(path, (stamp, stamp))
        return path

    def run_main(self, *extra):
        sys.argv = ["make_handoff_zip.py", "--out", self.target,
                    "--label", self.LABEL, "--no-test-count"] + list(extra)
        buffer = io.StringIO()
        with contextlib.redirect_stdout(buffer):
            code = mod.main()
        return code, buffer.getvalue()

    # -- 前提：磁盘签名与 zip 条目可比 ---------------------------------------
    def test_a_disk_signature_equals_the_zip_entry_it_becomes(self):
        """比对的整条链都建立在这条上：磁盘算出来的 (size, crc) 与 zip 条目同源。

        换成 mtime 会失效：实测里 5 个文件内容变了而修改时间全早于打包时间
        （写盘工具保留 mtime），"没变"会被误判成"变了"或反过来。
        """
        self.put("a.md", "hello")
        sig = mod.entry_signatures(["a.md"])
        pkg = self.package("old.zip", {self.LABEL + "/a.md": "hello"})
        self.assertEqual({"a.md": sig["a.md"]},
                         mod.read_package_entries(pkg, self.LABEL))

    # -- 检出 ---------------------------------------------------------------
    def test_a_file_only_in_the_previous_package_is_a_removal(self):
        self.put("项目说明.md", "r")
        self.put("a.md", "a")
        prev = self.package("old.zip", {
            "%s/项目说明.md" % self.LABEL: "r",
            "%s/a.md" % self.LABEL: "a",
            "%s/gone.md" % self.LABEL: "g"})

        path, entries = mod.find_previous_package(self.target, self.LABEL)
        self.assertEqual(prev, path)
        diff = mod.compare_versions(entries, mod.entry_signatures(mod.collect()[0]))
        self.assertEqual(["gone.md"],
                         [rel for rel, _ in diff["removed"]])
        self.assertEqual(2, diff["identical"])
        self.assertEqual([], diff["added"])

    def test_a_foreign_package_is_never_taken_as_the_previous_one(self):
        """别的东西的备份也在同一个目录里。认错了，第一次打包就会报一屏"删失"。"""
        other = self.package("other.zip", {"something-else/README.md": "x"})
        self.assertIsNone(mod.read_package_entries(other, self.LABEL))
        self.assertEqual((None, None),
                         mod.find_previous_package(self.target, self.LABEL))

    def test_a_broken_zip_does_not_stop_the_search(self):
        """半成品包（`.part` 中途被打断）不该让打包整个失败。"""
        junk = os.path.join(self.out, "broken.zip")
        with open(junk, "wb") as fh:
            fh.write(b"not a zip")
        os.utime(junk, (time.time(), time.time()))       # 它比旧包新
        good = self.package("old.zip", {"%s/项目说明.md" % self.LABEL: "r"}, age=60)
        self.assertEqual(good, mod.find_previous_package(self.target, self.LABEL)[0])

    # -- 拦下与放行 ---------------------------------------------------------
    def test_packaging_refuses_while_a_file_is_missing(self):
        self.put("项目说明.md", "r")
        self.put("a.md", "a2")          # 与旧包内容不同
        self.package("old.zip", {
            "%s/项目说明.md" % self.LABEL: "r",
            "%s/a.md" % self.LABEL: "a1",
            "%s/gone.md" % self.LABEL: "g"})

        code, output = self.run_main()
        self.assertEqual(3, code, output)
        self.assertFalse(os.path.exists(self.target),
                         "拒出包时不该留下任何 zip")
        self.assertIn("可疑删失", output)
        self.assertIn("gone.md", output)
        self.assertIn("--allow-removals", output)
        # 变更也要报出来：收包人靠这张表知道"这一版动了什么"
        self.assertIn("a.md", output)
        self.assertIn("内容变更 1", output)

    def test_allow_removals_writes_the_decision_into_the_index(self):
        """放行不是静默放行：删除清单要跟着包一起走。"""
        self.put("项目说明.md", "r")
        self.put("a.md", "a2")
        self.package("old.zip", {
            "%s/项目说明.md" % self.LABEL: "r",
            "%s/a.md" % self.LABEL: "a1",
            "%s/gone.md" % self.LABEL: "g"})

        code, output = self.run_main("--allow-removals")
        self.assertEqual(0, code, output)
        with zipfile.ZipFile(self.target) as zf:
            self.assertEqual({"root/项目说明.md", "root/a.md",
                              "root/索引.md"}, set(zf.namelist()))
            index = zf.read("root/索引.md").decode("utf-8")
        self.assertIn("## 与上一版的变化", index)
        self.assertIn("本包比上一版少了 1 个文件", index)
        self.assertIn("`-` gone.md", index)
        self.assertIn("内容变更", index)

    def test_a_huge_change_list_is_summarised_not_dumped(self):
        """整树搬迁会产出几百条差异：索引里截断，控制台仍报总数与形状。

        2026-10-21 的布局上移让 255 条路径同时"上一版有、这一版没有"。这种表如果
        原样写进 索引，一份索引会从 16 KB 涨到 42 KB，而多出来的四百行与四十行
        说的是同一件事——清单后半段本来就逐条列了包内全部文件。
        """
        self.put("项目说明.md", "r")
        old = {"%s/项目说明.md" % self.LABEL: "r"}
        for i in range(60):
            old["%s/old-%02d.md" % (self.LABEL, i)] = "o"
        self.package("old.zip", old)

        code, output = self.run_main("--allow-removals")
        self.assertEqual(0, code, output)
        self.assertIn("删除 60", output)
        self.assertIn("整棵树改名", output)      # 说出形状，不替人下结论
        with zipfile.ZipFile(self.target) as zf:
            index = zf.read("root/索引.md").decode("utf-8")
        self.assertIn("| 删除 | 60 |", index)    # 计数必须精确
        self.assertIn("另有 20 项未列出", index)
        self.assertNotIn("old-59.md", index)

    def test_a_clean_version_is_not_blocked(self):
        self.put("项目说明.md", "r")
        self.put("a.md", "a")
        self.package("old.zip", {"%s/项目说明.md" % self.LABEL: "r",
                                 "%s/a.md" % self.LABEL: "a"})
        code, output = self.run_main()
        self.assertEqual(0, code, output)
        self.assertIn("删除 0", output)
        with zipfile.ZipFile(self.target) as zf:
            index = zf.read("root/索引.md").decode("utf-8")
        self.assertIn("| 未变 | 2 |", index)


if __name__ == "__main__":
    unittest.main()
