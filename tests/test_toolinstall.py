# -*- coding: utf-8 -*-
"""项目目录内的工具安装：`toolinstall`，以及它与 `toolkit` / `store` 的接缝。

这个文件里最值钱的三条断言，先说清楚它们各自防的是什么：

1. **`test_every_recipe_id_is_a_real_adapter`** —— 配方表的键是适配器 id，而
   两个表在**两个文件**里。多一个键或少一个键，症状分别是"多出一条装不了的
   东西"与"某个适配器永远返回'没有登记'"，两处都不会抛异常。
2. **`ExtractionGuardTestCase`** —— 解压是本模块唯一能写到磁盘之外的步骤，
   而条目名是第三方可控的字符串。这里逐条钉住：逃逸的条目被拒**且文件确实
   没有落到外面**（只断言"抛了异常"是不够的——抛在写之后也满足它）。
3. **`InstallLeavesNoTraceTestCase`** —— 半成品目录与装好的目录长得一模一样，
   所以"失败之后什么都没留下"必须是可断言的事实，而不是一句设计意图。

全部用例**不联网**：取数器是注入的，夹具自己造 zip/tar。
"""
import fnmatch
import io
import json
import os
import shutil
import sqlite3
import sys
import tarfile
import tempfile
import unittest
import urllib.error
import urllib.request
import zipfile
from unittest import mock

from pentestd import chat
from pentestd import store as store_module
from pentestd import toolinstall
from pentestd import toolkit


class _Plane:
    """`_tool_install_tool` 只用到 `plane.store` 这一样东西。"""

    def __init__(self, store):
        self.store = store


# ---------------------------------------------------------------- 夹具


class FakeFetcher:
    """可编程取数器：URL → 要写到 dest 的字节，或要返回的文本。

    它同时**记下每一次调用的 limit**。上限（`DEFAULT_MAX_BYTES`）是防"URL 写错
    指向一个几十 GB 的镜像"的那一道，而它在 `default_fetch` 里；离线测不到那
    段代码，至少可以钉住"调用方确实把上限传下去了"。
    """

    def __init__(self):
        self.files = {}
        self.texts = {}
        self.calls = []
        self.fail = set()

    def serve_bytes(self, url, data):
        self.files[url] = data
        return self

    def serve_text(self, url, text, final=""):
        self.texts[url] = (final or url, text)
        return self

    def break_url(self, url):
        self.fail.add(url)
        return self

    def __call__(self, url, dest=None, timeout=60.0, limit=0):
        self.calls.append({"url": url, "dest": dest, "limit": limit})
        if url in self.fail:
            raise toolinstall.InstallError("夹具：%s 取不到" % url)
        if dest is None:
            final, text = self.texts.get(url, (url, ""))
            return {"status": 200, "url": final, "bytes": len(text), "text": text}
        data = self.files.get(url)
        if data is None:
            raise toolinstall.InstallError("夹具：没有为 %s 准备内容" % url)
        with open(dest, "wb") as handle:
            handle.write(data)
        return {"status": 200, "url": url, "bytes": len(data), "text": ""}


def zip_bytes(entries):
    """把 `[(名字, 字节)]` 打成一个 zip。"""
    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w", zipfile.ZIP_DEFLATED) as bundle:
        for name, data in entries:
            bundle.writestr(name, data)
    return buf.getvalue()


def tar_bytes(entries):
    """把 `[(名字, 字节)]` 打成一个 gzip tar。"""
    buf = io.BytesIO()
    with tarfile.open(fileobj=buf, mode="w:gz") as bundle:
        for name, data in entries:
            info = tarfile.TarInfo(name)
            info.size = len(data)
            bundle.addfile(info, io.BytesIO(data))
    return buf.getvalue()


# 夹具配方。键里刻意放一个**真实适配器 id**（chisel），因为
# `toolkit` 的接缝要用真 id 才走得通；其余几个是纯夹具名。
FIXTURE_RECIPES = {
    "zipbin": {
        "channel": "zip", "url": "https://fixture.invalid/zipbin.zip",
        "member": "pkg-*/bin/zipbin.exe", "size": 111,
        "note": "夹具：单文件压缩包",
    },
    "pytool": {
        "channel": "zip", "url": "https://fixture.invalid/pytool.zip",
        "member": "*/pytool.py", "python": True, "size": 45,
        "note": "夹具：需要解释器的工具",
    },
    "relbin": {
        "channel": "github_release", "repo": "acme/relbin",
        "asset": "*_windows_amd64.zip", "member": "relbin.exe", "size": 22,
        "note": "夹具：从 release 资源里挑一个",
    },
    "arctool": {
        "channel": "github_archive", "repo": "acme/arctool", "ref": "main",
        "member": "*/arctool.py", "python": True, "size": 0,
        "note": "夹具：仓库快照",
    },
    "manualtool": {
        "channel": "manual",
        "note": "夹具：上游只发 .exe 安装程序，没有任何免安装渠道，所以没有自动通道。",
    },
    "chisel": {
        "channel": "zip", "url": "https://fixture.invalid/chisel.zip",
        "member": "chisel.exe", "size": 33,
        "note": "夹具覆盖真 id，用来验 toolkit 的查找顺序",
    },
    # 同上，覆盖另一个真 id：`toolkit.require()` 那条接缝要用一个**真的缺着**
    # 且**真的可自动安装**的适配器，而本机上 nmap 正是这样。
    "nmap": {
        "channel": "zip", "url": "https://fixture.invalid/nmap.zip",
        "member": "nmap-*/nmap.exe", "size": 44,
        "note": "夹具覆盖真 id",
    },
    # 再一个真 id，但走**解释器**那条路：handler 会拿真表里的版本参数去校验，
    # 而一个假造的 .exe 会被 Windows 拒绝（WinError 216），只有脚本跑得起来。
    "sqlmap": {
        "channel": "zip", "url": "https://fixture.invalid/sqlmap.zip",
        "member": "*/sqlmap.py", "python": True, "size": 9,
        "note": "夹具覆盖真 id（需要解释器的工具）",
    },
}


RELEASE_HTML = (
    '<html><a href="/acme/relbin/releases/download/v9.9.9/'
    'relbin_9.9.9_windows_amd64.zip">win</a>'
    '<a href="/acme/relbin/releases/download/v9.9.9/relbin_9.9.9_linux_amd64.tar.gz">lin</a>'
    '<a href="/acme/relbin/releases/download/v9.9.9/relbin_9.9.9_checksums.txt">sum</a>'
    '</html>')


class _RecipeFixtureTestCase(unittest.TestCase):
    """把 `RECIPES` 换成夹具，并给每个用例一个独立的工具根目录。"""

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ti-root-")
        self._saved = toolinstall.RECIPES
        toolinstall.RECIPES = dict(FIXTURE_RECIPES)
        self.fetch = FakeFetcher()
        self.fetch.serve_bytes(
            "https://fixture.invalid/zipbin.zip",
            zip_bytes([("pkg-1.0/README.txt", b"readme"),
                       ("pkg-1.0/bin/zipbin.exe", b"MZ-fake")]))
        self.fetch.serve_bytes(
            "https://fixture.invalid/pytool.zip",
            zip_bytes([("pytool-2.0/README.txt", b"readme"),
                       ("pytool-2.0/pytool.py", b"print('pytool 2.0')\n")]))
        self.fetch.serve_bytes(
            "https://fixture.invalid/chisel.zip",
            zip_bytes([("chisel.exe", b"MZ-fake-chisel")]))
        self.fetch.serve_bytes(
            "https://fixture.invalid/sqlmap.zip",
            zip_bytes([("sqlmap-master/sqlmap.py", b"print('sqlmap 1.0')\n")]))

    def tearDown(self):
        toolinstall.RECIPES = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def leftovers(self):
        """工具根目录里除了正常内容之外的残留（staging / prev）。"""
        if not os.path.isdir(self.root):
            return []
        return sorted(name for name in os.listdir(self.root)
                      if name.startswith("."))


# ---------------------------------------------------------------- 展示路径


class DisplayContractTestCase(unittest.TestCase):
    """`display()` 是对外表面与执行层的分界：工具集绝对路径→相对路径（m05934）。

    对外（链路节点、工具状态页、交付文档）说**相对平台根**的话；执行层
    （`resolve`/`command_prefix`）仍给**绝对**。两件事分开，才能做到"换一台
    机器读得懂"与"执行不依赖 cwd"同时成立。
    """

    def test_inside_project_root_becomes_relative(self):
        proot = toolinstall.project_root()
        inside = os.path.join(proot, "data", "tools", "nmap", "nmap.exe")
        shown = toolinstall.display(inside)
        self.assertFalse(os.path.isabs(shown), shown)
        self.assertEqual(os.path.join("data", "tools", "nmap", "nmap.exe"),
                         shown)

    def test_outside_project_root_is_unchanged(self):
        # 外面没有"相对"可言，硬折成文件名反而丢掉"究竟是哪一份"。
        outside = os.path.join("C:", "elsewhere", "tools", "x.exe")
        self.assertEqual(outside, toolinstall.display(outside))

    def test_wsl_marker_is_unchanged(self):
        marker = "wsl://Ubuntu/usr/bin/nmap"
        self.assertEqual(marker, toolinstall.display(marker))

    def test_empty_is_unchanged(self):
        self.assertEqual("", toolinstall.display(""))

    def test_project_root_is_platform_root(self):
        # 锚在 pentestd/ 的上一级：工具跟着代码走，不是跟着数据库走。
        here = os.path.dirname(os.path.abspath(toolinstall.__file__))
        self.assertEqual(os.path.dirname(here), toolinstall.project_root())


# ---------------------------------------------------------------- 配方表


class RecipeTableTestCase(unittest.TestCase):
    """真表上的不变量。**任何一条红了都意味着产品里有一个说不通的地方。**"""

    def test_every_recipe_id_is_a_real_adapter(self):
        """配方表的键必须都是真实适配器 id。

        两个表在两个文件里，多一个键的症状是"多出一条装不了的东西"（模型读到
        它会去调，然后拿到一个"未知外部工具"的错），少一个键的症状是"某个
        适配器永远返回没有登记"。两处都不抛异常。
        """
        # 判据对的是内置表 DEFAULT_ADAPTERS，不是运行期的 ADAPTER_INDEX：后者是
        # data/tools.json 读出来的活表，可能比代码旧（这一轮新加的两个适配器就不在
        # 本机那份文件里），也可能比代码新（使用者自己写的适配器，本来就不该有安装
        # 配方）。两个方向都只有内置表说得清。
        recipes = set(toolinstall.RECIPES)
        adapters = set(item["id"] for item in toolkit.DEFAULT_ADAPTERS)
        self.assertEqual(set(), recipes - adapters,
                         "配方表里有不是适配器的 id")
        self.assertEqual(set(), adapters - recipes,
                         "有适配器没有登记安装方式（连 manual 都没有）")

    def test_manual_recipes_give_a_specific_reason(self):
        """没有自动渠道的，必须写清**为什么**。

        "暂不支持"不是原因：读到它的人只能放弃。所以这里既查长度，也查那些
        等于没说的措辞。这一条是这一整个模块最有价值的约束——它逼着每一条
        manual 去回答"上游到底发了什么"。
        """
        empty = [key for key, spec in toolinstall.RECIPES.items()
                 if str(spec.get("channel")) == "manual"
                 and len(str(spec.get("note") or "").strip()) < 40]
        self.assertEqual([], empty, "这些 manual 配方没有给出像样的原因")
        banned = ("暂不支持", "不支持", "暂时没有", "敬请期待")
        for key, spec in sorted(toolinstall.RECIPES.items()):
            if str(spec.get("channel")) != "manual":
                continue
            note = str(spec.get("note") or "")
            for word in banned:
                self.assertNotIn(word, note,
                                 "%s 的原因是空话（%r），它等于没有原因"
                                 % (key, word))

    def test_downloadable_recipes_are_https_and_name_one_member(self):
        for key, spec in sorted(toolinstall.RECIPES.items()):
            channel = str(spec.get("channel"))
            if channel == "manual":
                continue
            self.assertIn(channel, toolinstall.CHANNELS, key)
            if channel == toolinstall.WSL_CHANNEL:
                # wsl 渠道**不下载归档**，所以"包内成员"这回事不存在：要装的
                # 东西由发行版的包管理器带进来，多一层文件解压只会多一个能
                # 出错的地方。它要声明的是 package 与 binary。
                self.assertTrue(str(spec.get("package") or ""), key)
                self.assertTrue(str(spec.get("binary") or ""), key)
                continue
            if channel == toolinstall.NPM_CHANNEL:
                # npm 渠道也不下载归档：装出来的是 `node_modules` 一棵树，
                # 没有"包内成员"可挑。它要声明的是 package 与那个树里的入口
                # 脚本（`entry`），后者正是版本命令将来要跑的那一份。
                self.assertTrue(str(spec.get("package") or ""), key)
                self.assertTrue(str(spec.get("entry") or ""), key)
                continue
            member = str(spec.get("member") or "")
            self.assertTrue(member, "%s 没有声明包内成员" % key)
            self.assertNotIn("**", member, "%s 的成员模式过于宽松" % key)
            if channel == "zip":
                self.assertTrue(str(spec.get("url") or "").startswith("https://"),
                                "%s 的下载地址不是 https" % key)
            else:
                self.assertIn("/", str(spec.get("repo") or ""),
                              "%s 的 repo 不是 owner/repo" % key)

    def test_plan_and_status_do_not_touch_the_network(self):
        """dry-run 必须是**真的** dry-run：不给取数器、不传 URL，只读配方表。"""
        root = tempfile.mkdtemp(prefix="ti-plan-")
        try:
            plan = toolinstall.plan(root=root)
            self.assertTrue(plan["todo"])
            self.assertTrue(plan["manual"])
            self.assertGreater(plan["downloadBytes"], 0)
            for row in plan["manual"]:
                self.assertTrue(row["reason"], row)
            for row in toolinstall.status(root):
                self.assertIn(row["channel"], toolinstall.CHANNELS)
                if row["channel"] == toolinstall.WSL_CHANNEL:
                    # wsl 渠道的存在性由**发行版**回答，与 `root` 无关——给一个
                    # 临时 root 不会让它变成"没装"，因为它本来就不在那儿。
                    # 这一条要**跳过**而不是把断言放宽：放宽之后"某个下载渠道
                    # 在空 root 下误报已装"也会一起被放过。
                    continue
                self.assertFalse(row["installed"])
                if not row["installable"]:
                    self.assertTrue(row["reason"], row)
        finally:
            shutil.rmtree(root, ignore_errors=True)

    def test_default_fetch_refuses_non_http_schemes(self):
        """`file://` 会让"这个地址指向哪里"变成取决于本机文件系统。"""
        for url in ("file:///etc/passwd", "ftp://x/y", "", "data:text/plain,x"):
            with self.assertRaises(toolinstall.InstallError):
                toolinstall.default_fetch(url)


# ---------------------------------------------------------------- 渠道解析


class ChannelResolutionTestCase(_RecipeFixtureTestCase):

    def test_zip_channel_names_the_url_and_the_member(self):
        source = toolinstall._source("zipbin", toolinstall.RECIPES["zipbin"],
                                     self.fetch, 30.0)
        self.assertEqual("zip", source["channel"])
        self.assertEqual("https://fixture.invalid/zipbin.zip", source["url"])
        self.assertEqual("zipbin.zip", source["name"])
        self.assertEqual("pkg-*/bin/zipbin.exe", source["member"])

    def test_github_release_resolves_the_tag_then_picks_the_asset(self):
        """tag 从**跳转后的地址**读，不从请求的地址读。

        这一条不是细节：`releases/latest` 本身不含版本号，跳转目标才含。
        读错了会拼出一个不存在的下载地址，而它报的是 404。
        """
        self.fetch.serve_text("https://github.com/acme/relbin/releases/latest",
                              "<html>redirect</html>",
                              final="https://github.com/acme/relbin/releases/tag/v9.9.9")
        self.fetch.serve_text(
            "https://github.com/acme/relbin/releases/expanded_assets/v9.9.9",
            RELEASE_HTML)
        source = toolinstall._source("relbin", toolinstall.RECIPES["relbin"],
                                     self.fetch, 30.0)
        self.assertEqual("v9.9.9", source["version"])
        self.assertEqual(
            "https://github.com/acme/relbin/releases/download/v9.9.9/"
            "relbin_9.9.9_windows_amd64.zip", source["url"])
        # linux 那份与 checksums 都在列表里，而模式挑出的是 windows 那份
        self.assertNotIn("linux", source["url"])
        self.assertNotIn("checksums", source["url"])

    def test_github_release_without_a_tag_is_refused(self):
        self.fetch.serve_text("https://github.com/acme/relbin/releases/latest",
                              "<html>no redirect</html>",
                              final="https://github.com/acme/relbin/releases")
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall._source("relbin", toolinstall.RECIPES["relbin"],
                                self.fetch, 30.0)
        self.assertIn("tag", str(caught.exception))

    def test_github_archive_url_is_built_from_repo_and_ref(self):
        """`github_archive` 直连 **codeload**，不走 github.com 的跳转。

        `github.com/<repo>/archive/...` 会 302 到 codeload，而那一次跳转要求
        先够得着 github.com——实测在一台网络被中间设备干扰的机器上，github.com
        的 TLS 握手会被切断，而 codeload 直连是通的。少一跳就是多一条能用的路。
        """
        source = toolinstall._source("arctool", toolinstall.RECIPES["arctool"],
                                     self.fetch, 30.0)
        self.assertEqual(
            "https://codeload.github.com/acme/arctool/tar.gz/refs/heads/main",
            source["url"])
        self.assertTrue(source["url"].startswith("https://codeload.github.com/"),
                        source["url"])
        self.assertNotIn("/archive/", source["url"],
                         "走 github.com 的 /archive/ 就等于多要一次跳转")
        # 分支快照没有版本号，而且这一点要写在 source 里说不清楚才不算隐瞒
        self.assertEqual("", source["version"])
        self.assertIn("快照", source["source"])

    def test_manual_channel_refuses_with_its_own_reason(self):
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall.install("manualtool", fetch=self.fetch, root=self.root)
        message = str(caught.exception)
        self.assertIn("安装程序", message)
        self.assertIn("manualtool", message)

    def test_unknown_adapter_lists_what_is_known(self):
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall.install("nope", fetch=self.fetch, root=self.root)
        self.assertIn("zipbin", str(caught.exception))


class PickMemberTestCase(_RecipeFixtureTestCase):
    """`_pick` 只接受**恰好一个**命中。

    取第一个而不是报错，在"上游多加了一个平台目录"时要静默取到错的那一个。
    """

    def test_one_hit(self):
        self.assertEqual("a/b.exe", toolinstall._pick(["a/b.exe"], "*/b.exe", "x"))

    def test_no_hit_says_what_was_there(self):
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall._pick(["a/c.txt"], "*/b.exe", "x")
        self.assertIn("a/c.txt", str(caught.exception))

    def test_two_hits_is_an_error_not_a_coin_flip(self):
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall._pick(["x86/t.exe", "x64/t.exe"], "*/t.exe", "x")
        message = str(caught.exception)
        self.assertIn("x86/t.exe", message)
        self.assertIn("x64/t.exe", message)

    def test_empty_archive(self):
        with self.assertRaises(toolinstall.InstallError):
            toolinstall._pick([], "*", "x")


# ---------------------------------------------------------------- 收尾清理


class PruneTestCase(unittest.TestCase):
    """`prune`：换入前收掉配方声明不要的东西，**但删不掉 member 自己**。

    它要解决的事很具体：FireDaemon 的 openssl 包把 arm64 / x86 / x64 三套、
    头文件、静态库和 `.pdb` 调试符号打在一起，解压 157.7 MB，而运行只需要其中
    9.2 MB。把 74.3 MB 的调试符号解进项目目录不是"多占点磁盘"——它让"这个工具
    到底装了多少东西"这件事变得看不出来。
    """

    ARCHIVE = [("x64/bin/tool.exe", b"MZ"), ("x64/bin/tool.pdb", b"P" * 500),
               ("x64/bin/runtime.dll", b"D" * 300),
               ("x64/include/tool.h", b"H" * 200),
               ("arm64/bin/tool.exe", b"MZ-arm"), ("x86/bin/tool.exe", b"MZ-x86")]

    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ti-prune-")
        self._saved = toolinstall.RECIPES
        self.fetch = FakeFetcher()
        self.fetch.serve_bytes("https://fixture.invalid/threearch.zip",
                               zip_bytes(self.ARCHIVE))

    def tearDown(self):
        toolinstall.RECIPES = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def _install(self, prune, member="x64/bin/tool.exe"):
        toolinstall.RECIPES = {
            "threearch": {"channel": "zip",
                          "url": "https://fixture.invalid/threearch.zip",
                          "member": member, "size": 0, "prune": prune,
                          "note": "夹具：三套架构打在同一个包里"}}
        return toolinstall.install("threearch", fetch=self.fetch, root=self.root,
                                   probe=False)

    def _tree(self, *parts):
        return os.path.join(self.root, "threearch", *parts)

    def test_prune_removes_exactly_what_it_names(self):
        out = self._install(["*.pdb", "arm64/*", "x86/*", "x64/include/*"])
        self.assertEqual(4, out["pruned"]["files"])
        self.assertTrue(os.path.isfile(self._tree("x64", "bin", "tool.exe")))
        self.assertTrue(os.path.isfile(self._tree("x64", "bin", "runtime.dll")),
                        "被保留的运行时 DLL 不该被 *.pdb 波及")
        self.assertFalse(os.path.exists(self._tree("x64", "bin", "tool.pdb")))
        self.assertFalse(os.path.exists(self._tree("arm64")))
        self.assertFalse(os.path.exists(self._tree("x86")))
        self.assertFalse(os.path.exists(self._tree("x64", "include")))

    def test_a_pattern_wide_enough_to_eat_the_member_cannot(self):
        """过宽的模式只能多删别的，**不能**把要装的那个删掉。

        这是 `prune` 唯一重要的不变量：真删掉了，症状要到 probe 或者第一次真实
        调用才现形，而且看起来像"上游把包改样了"——那是最难往回追的一类现场。
        """
        out = self._install(["*", "**/*"])
        self.assertTrue(out["ok"])
        self.assertTrue(os.path.isfile(self._tree("x64", "bin", "tool.exe")))
        self.assertTrue(os.path.isfile(toolinstall.resolve("threearch", self.root)))

    def test_without_prune_nothing_is_removed(self):
        out = self._install(None)
        self.assertEqual({"patterns": [], "files": 0, "bytes": 0, "failed": []},
                         out["pruned"])
        for dead in ("x64/bin/tool.pdb", "arm64/bin/tool.exe", "x86/bin/tool.exe"):
            self.assertTrue(
                os.path.isfile(self._tree(*dead.split("/"))),
                "没声明 prune 时不该删任何东西：%s" % dead)

    def test_a_directory_emptied_out_is_removed_too(self):
        self._install(["arm64/*"])
        self.assertFalse(os.path.exists(self._tree("arm64")),
                         "整个目录被删空之后，空壳也该收掉")

    def test_it_reports_what_actually_landed_on_disk(self):
        """报"装进去多少"要用**留用**那个数，不是下载量。

        只报下载量，使用者会以为项目目录里躺着 40 MB；而配方带 prune 时两者
        可以差一个数量级。
        """
        out = self._install(["*.pdb", "arm64/*", "x86/*", "x64/include/*"])
        self.assertEqual(2, out["treeFiles"], "留用的只有 tool.exe 与 runtime.dll")
        self.assertEqual(2 + 300, out["treeBytes"])
        self.assertEqual(4, out["pruned"]["files"])
        self.assertGreater(out["pruned"]["bytes"], 0)


class DeclaredVersionTestCase(unittest.TestCase):
    """固定 URL 的渠道**本来就知道**自己钉在哪个版本上——地址里就带着。

    不写出来的话 `installed.json` 只能记一个空串，而使用者读到的是"（无版本号）"，
    对 `openssl 3.5.1`、`nmap 7.92` 这种地址里明写着版本的东西，那是一句假话。
    """

    def test_the_pinned_archives_declare_their_version(self):
        for adapter_id, expected in (("nmap", "7.92"), ("curl", "8.23.0"),
                                     ("openssl", "3.5.1")):
            spec = toolinstall.RECIPES[adapter_id]
            self.assertEqual("zip", spec["channel"], adapter_id)
            self.assertEqual(expected, spec.get("version"), adapter_id)
            self.assertIn(expected, spec["url"],
                          "%s 声明的版本与地址里的对不上" % adapter_id)

    def test_the_zip_channel_carries_the_declared_version(self):
        source = toolinstall._source("nmap", toolinstall.RECIPES["nmap"], None, 0)
        self.assertEqual("7.92", source["version"])

    def test_a_zip_recipe_without_one_stays_empty(self):
        """没声明就还是空串——这个字段不能靠猜。夹具配方正是这样。"""
        source = toolinstall._source("zipbin", FIXTURE_RECIPES["zipbin"], None, 0)
        self.assertEqual("", source["version"])


class OpenSslRecipeTestCase(unittest.TestCase):
    """`openssl` 曾被登记成 manual，而那条理由是**错的**。

    原话是"Windows 上没有 openssl 官方的免安装构建……本平台不这么做"。官方确实
    只发源码，但 FireDaemon 一直以 zip 发布基于官方源码编译的 Windows 构建。
    这条配方把"装不了"变成"装得了"——它缺着的时候，模型会把本该用 openssl 核对
    整条证书链的那一步，换成另一条只看得到一半信息的命令，然后把"取不到"写进结论。
    """

    def setUp(self):
        self.spec = toolinstall.RECIPES["openssl"]

    def test_it_has_a_real_channel(self):
        self.assertEqual("zip", self.spec["channel"])
        self.assertTrue(str(self.spec["url"]).startswith("https://"))
        self.assertIn("firedaemon.com", self.spec["url"])
        self.assertGreater(int(self.spec["size"] or 0), 40 * 1024 * 1024)

    def test_the_member_pattern_picks_one_binary(self):
        names = ["arm64/bin/openssl.exe", "x64/bin/openssl.exe", "x86/bin/openssl.exe"]
        self.assertEqual("x64/bin/openssl.exe",
                         toolinstall._pick(names, self.spec["member"], "二进制"))

    def test_it_prunes_the_other_architectures_and_the_debug_symbols(self):
        patterns = list(self.spec["prune"])
        for dead in ("arm64/bin/openssl.exe", "x86/bin/openssl.exe",
                     "x64/bin/openssl.pdb", "x64/include/openssl/ssl.h",
                     "x64/lib/libcrypto.lib"):
            self.assertTrue(any(fnmatch.fnmatchcase(dead, p) for p in patterns),
                            "%s 没有被 prune 覆盖，它会被解进项目目录" % dead)
        for alive in ("x64/bin/openssl.exe", "x64/bin/libcrypto-3-x64.dll",
                      "x64/bin/libssl-3-x64.dll"):
            self.assertFalse(any(fnmatch.fnmatchcase(alive, p) for p in patterns),
                             "%s 会被 prune 删掉，装出来就跑不起来" % alive)

    def test_the_note_says_it_is_not_an_upstream_build(self):
        """它是第三方编译的二进制，来源必须写在配方里，使用者有权知道。"""
        note = self.spec["note"]
        self.assertIn("第三方", note)
        self.assertIn("官方", note)


class InstallSummaryTestCase(unittest.TestCase):
    """装完之后模型读到的那句话：数字必须是**留用**量，不是下载量。

    配方带 `prune` 时两者可以差一个数量级（openssl 下载 40 MB、解压 157.7 MB、
    留用 9.2 MB）。只报下载量，使用者和模型都会以为项目目录里躺着 40 MB，而
    这句话是他们唯一的来源。
    """

    def setUp(self):
        directory = tempfile.mkdtemp(prefix="ti-summary-")
        self.directory = directory
        self.store = store_module.Store(os.path.join(directory, "s.db"))
        self.plane = _Plane(self.store)

    def tearDown(self):
        self.store.close()
        shutil.rmtree(self.directory, ignore_errors=True)

    def _run(self, outcome):
        with mock.patch.object(toolinstall, "install", return_value=outcome):
            return chat._tool_install_tool(self.plane, {"adapter": "nmap"}, "", {})

    @staticmethod
    def _install_result(**over):
        """**不能叫 `_outcome`**：`unittest.TestCase` 自己有一个同名属性（那是
        框架记录结果的对象），覆盖它会让两个用例报 `'_Outcome' object is not
        callable`——一个指向框架内部的 Typeerror，和被测代码毫无关系。"""
        base = {"adapter": "nmap", "ok": True, "action": "install",
                "channel": "zip", "source": "https://nmap.org/dist/x.zip",
                "version": "7.92", "root": "R", "bin": "R/nmap.exe", "kind": "exe",
                "bytes": 41943040, "archiveBytes": 41943040,
                "treeBytes": 9678848, "treeFiles": 23,
                "pruned": {"patterns": ["*.pdb"], "files": 520,
                           "bytes": 82_600_000, "failed": []},
                "route": "", "replaced": False,
                "probe": {"ok": True, "output": "Nmap version 7.92\n"},
                "note": "", "elapsedMs": 6000.0}
        base.update(over)
        return base

    def test_the_summary_uses_what_landed_on_disk(self):
        out = self._run(self._install_result())
        self.assertIn("下载 40.0 MB", out["summary"])
        self.assertIn("留用 9.2 MB", out["summary"])
        self.assertIn("已按配方收掉", out["summary"])
        self.assertIn("7.92", out["summary"], "版本要报出来，不能再说「无版本号」")
        self.assertNotIn("无版本号", out["summary"])
        self.assertEqual(9678848, out["data"]["treeBytes"])
        self.assertEqual(520, out["data"]["pruned"]["files"])

    def test_without_prune_it_does_not_pretend_anything_was_dropped(self):
        outcome = self._install_result(bytes=41943040, treeBytes=41943040,
                                       treeFiles=900,
                                       pruned={"patterns": [], "files": 0,
                                               "bytes": 0, "failed": []})
        out = self._run(outcome)
        self.assertIn("留用 40.0 MB", out["summary"])
        self.assertNotIn("已按配方收掉", out["summary"])
        self.assertEqual(0, out["data"]["pruned"]["files"])


# ---------------------------------------------------------------- 解压闸


class ExtractionGuardTestCase(_RecipeFixtureTestCase):
    """解压是唯一能写到磁盘之外的步骤。每一类逃逸各一条。"""

    def test_zip_slip_is_refused_and_nothing_lands_outside(self):
        """必须同时断言"抛了"与"文件不在外面"。

        只断言异常是不够的：写在检查**之后**也满足"抛了异常"这句话。所以这里
        去目标位置**找那个文件**——它不在，才说明闸门是在写之前生效的。
        """
        self.fetch.serve_bytes(
            "https://fixture.invalid/zipbin.zip",
            zip_bytes([("pkg-1.0/bin/zipbin.exe", b"MZ"),
                       ("../escaped.txt", b"pwned")]))
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                probe=False)
        self.assertIn("escaped.txt", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.root, "escaped.txt")),
                         "逃逸条目真的落到了工具根目录外面")
        self.assertFalse(os.path.exists(os.path.join(self.root, "zipbin")),
                         "失败的安装不该留下已换入的目录")
        self.assertEqual([], self.leftovers())

    def test_absolute_member_name_is_refused(self):
        """绝对路径的条目名。

        用 `abspath(sep)` 来构造而不是写死 `/` 或 `C:\\`：这个测试要在两个平台
        上都表达同一件事——一个**真的指向根目录之外**的名字。
        """
        absolute = os.path.join(os.path.abspath(os.sep), "escaped-abs.txt")
        self.fetch.serve_bytes(
            "https://fixture.invalid/zipbin.zip",
            zip_bytes([("pkg-1.0/bin/zipbin.exe", b"MZ"), (absolute, b"x")]))
        with self.assertRaises(toolinstall.InstallError):
            toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                probe=False)
        self.assertFalse(os.path.exists(absolute),
                         "绝对路径条目真的被写到了根目录之外")

    def test_relative_escape_is_refused(self):
        """`a/../../b` 归一化之后仍然在根目录里，**不**算逃逸。

        所以这一条用的是能真正走出去的那一级（`../..` 越过根目录）。把"含 `..`"
        当成"逃逸"会让闸门在不该拒的地方拒——而那种错误会让人去改一段没问题的
        代码，和漏报一样贵。
        """
        self.fetch.serve_bytes(
            "https://fixture.invalid/zipbin.zip",
            zip_bytes([("pkg-1.0/../../../bin/zipbin.exe", b"MZ")]))
        with self.assertRaises(toolinstall.InstallError):
            toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                probe=False)

    def test_symlink_entries_are_skipped_not_extracted(self):
        """符号链接不是"检查后再决定"，是整个不解。

        逐个条目做路径检查挡不住它：链接本身落在解压目录里，而**后面**的条目
        通过它写到外面。
        """
        buf = io.BytesIO()
        with zipfile.ZipFile(buf, "w") as bundle:
            bundle.writestr("pkg-1.0/bin/zipbin.exe", b"MZ")
            info = zipfile.ZipInfo("pkg-1.0/escape")
            info.external_attr = (0o120777 << 16)     # S_IFLNK | 0777
            bundle.writestr(info, "../../outside")
        self.fetch.serve_bytes("https://fixture.invalid/zipbin.zip", buf.getvalue())
        out = toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                  probe=False)
        self.assertTrue(out["ok"])
        self.assertFalse(os.path.lexists(
            os.path.join(self.root, "zipbin", "pkg-1.0", "escape")))

    def test_tar_escape_is_refused(self):
        self.fetch.serve_bytes(
            "https://fixture.invalid/pytool.zip",
            tar_bytes([("pytool-2.0/pytool.py", b"print(1)\n"),
                       ("../../escaped.py", b"print(2)\n")]))
        with self.assertRaises(toolinstall.InstallError):
            toolinstall.install("pytool", fetch=self.fetch, root=self.root,
                                probe=False)
        self.assertEqual([], self.leftovers())

    def test_entry_count_cap(self):
        """条目数上限是 zip 炸弹的第一道。这里把上限调到 3 来验它真的在管。"""
        data = zip_bytes([("p%d" % i, b"x") for i in range(6)])
        self.fetch.serve_bytes("https://fixture.invalid/zipbin.zip", data)
        with mock.patch.object(toolinstall, "MAX_ARCHIVE_ENTRIES", 3):
            with self.assertRaises(toolinstall.InstallError) as caught:
                toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                    probe=False)
        self.assertIn("条目数", str(caught.exception))

    def test_safe_member_accepts_only_inside_paths(self):
        root = os.path.realpath(self.root)
        self.assertTrue(toolinstall._safe_member(root, "a/b.txt").startswith(root))
        # `a/../../b` 归一化之后仍在外面的下一层，所以它确实逃逸；
        # 而 `a/../b` 只是同一个文件换了个写法，**不该**被拒。
        self.assertTrue(toolinstall._safe_member(root, "a/../b.txt").startswith(root))
        for bad in ("../a", "a/../../b", "", os.path.abspath(os.sep)):
            with self.assertRaises(toolinstall.InstallError):
                toolinstall._safe_member(root, bad)


# ---------------------------------------------------------------- 安装流程


class InstallFlowTestCase(_RecipeFixtureTestCase):

    def test_happy_path_records_a_relative_binary(self):
        out = toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                  probe=False)
        self.assertTrue(out["ok"])
        self.assertTrue(out["bin"].startswith(os.path.realpath(self.root)))
        path = toolinstall.resolve("zipbin", self.root)
        self.assertTrue(path.startswith(os.path.realpath(self.root)), path)
        self.assertTrue(os.path.isfile(path))
        # 注册表里必须是**相对路径**：项目换一台机器、换一个盘符之后仍然能用，
        # 而绝对路径会静默失效（文件明明在，只是路径不对）。
        with open(toolinstall.registry_path(self.root), encoding="utf-8") as fh:
            row = json.load(fh)["tools"]["zipbin"]
        self.assertEqual("zipbin/pkg-1.0/bin/zipbin.exe", row["bin"])
        self.assertNotIn(":", row["bin"])
        self.assertEqual("exe", row["kind"])
        self.assertTrue(row["source"])
        self.assertEqual([], self.leftovers())

    def test_python_tool_gets_an_interpreter_in_the_prefix(self):
        out = toolinstall.install("pytool", fetch=self.fetch, root=self.root,
                                  probe=True, version_args=("--version",))
        self.assertEqual("python", out["kind"])
        prefix = toolinstall.command_prefix("pytool", self.root)
        self.assertEqual(2, len(prefix))
        self.assertEqual(os.path.basename(prefix[0]).lower(),
                         os.path.basename(sys.executable).lower())
        self.assertTrue(prefix[1].endswith("pytool.py"))
        # 真跑了一次版本命令，说明它确实起得来
        self.assertTrue(out["probe"]["ok"])
        self.assertIn("pytool 2.0", out["probe"]["output"])

    def test_download_cap_is_passed_to_the_fetcher(self):
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False, max_bytes=1234)
        writes = [call for call in self.fetch.calls if call["dest"]]
        self.assertEqual(1, len(writes))
        self.assertEqual(1234, writes[0]["limit"])

    def test_reinstall_replaces_and_leaves_no_previous_directory(self):
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False)
        self.fetch.serve_bytes(
            "https://fixture.invalid/zipbin.zip",
            zip_bytes([("pkg-2.0/bin/zipbin.exe", b"MZ-v2")]))
        out = toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                  probe=False)
        self.assertTrue(out["replaced"])
        self.assertTrue(toolinstall.resolve("zipbin", self.root)
                        .endswith(os.path.join("pkg-2.0", "bin", "zipbin.exe")))
        self.assertFalse(os.path.exists(os.path.join(
            self.root, "zipbin", "pkg-1.0")), "旧版本没被清掉")
        self.assertEqual([], self.leftovers())

    def test_the_registry_survives_moving_the_whole_tree(self):
        """整棵工具目录换位置之后仍然找得到——这是"可搬运"那条理由的可断言形态。"""
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False)
        moved = self.root + "-moved"
        os.replace(self.root, moved)
        try:
            path = toolinstall.resolve("zipbin", moved)
            self.assertTrue(path.startswith(os.path.realpath(moved)), path)
            self.assertTrue(os.path.isfile(path))
        finally:
            shutil.rmtree(moved, ignore_errors=True)

    def test_a_broken_registry_row_is_ignored_not_followed(self):
        """注册表是可写的文件。一条指向根目录外的记录不能被当成真。"""
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False)
        path = toolinstall.registry_path(self.root)
        with open(path, encoding="utf-8") as fh:
            doc = json.load(fh)
        doc["tools"]["zipbin"]["bin"] = "../../../../windows/system32/calc.exe"
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(doc, fh)
        self.assertEqual("", toolinstall.resolve("zipbin", self.root))
        self.assertEqual([], toolinstall.command_prefix("zipbin", self.root))

    def test_a_registry_row_whose_file_is_gone_resolves_to_nothing(self):
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False)
        os.remove(toolinstall.resolve("zipbin", self.root))
        self.assertEqual("", toolinstall.resolve("zipbin", self.root))

    def test_unparsable_registry_degrades_to_not_installed(self):
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False)
        with open(toolinstall.registry_path(self.root), "w", encoding="utf-8") as fh:
            fh.write("{ 这不是 JSON")
        self.assertEqual("", toolinstall.resolve("zipbin", self.root))
        # 而且还能再装回来：注册表是缓存，真值在文件系统里
        out = toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                  probe=False)
        self.assertTrue(out["ok"])
        self.assertTrue(toolinstall.resolve("zipbin", self.root))


class InstallLeavesNoTraceTestCase(_RecipeFixtureTestCase):
    """失败必须什么都不留下。半成品目录与装好的目录长得一模一样。"""

    def test_download_failure_leaves_nothing(self):
        self.fetch.break_url("https://fixture.invalid/zipbin.zip")
        with self.assertRaises(toolinstall.InstallError):
            toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                probe=False)
        self.assertFalse(os.path.exists(os.path.join(self.root, "zipbin")))
        self.assertFalse(os.path.exists(toolinstall.registry_path(self.root)))
        self.assertEqual([], self.leftovers())

    def test_empty_download_leaves_nothing(self):
        self.fetch.serve_bytes("https://fixture.invalid/zipbin.zip", b"")
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                probe=False)
        self.assertIn("空", str(caught.exception))
        self.assertEqual([], self.leftovers())

    def test_missing_member_pattern_leaves_nothing(self):
        self.fetch.serve_bytes(
            "https://fixture.invalid/zipbin.zip",
            zip_bytes([("pkg-1.0/bin/other.exe", b"MZ")]))
        with self.assertRaises(toolinstall.InstallError):
            toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                probe=False)
        self.assertFalse(os.path.exists(os.path.join(self.root, "zipbin")))
        self.assertEqual([], self.leftovers())

    def test_failed_probe_leaves_nothing(self):
        """这条最重要：文件解出来了、目录也建了，**而它起不来**。

        如果换入发生在校验之前，这里留下的就是一个"看起来装好了"的目录，
        而下一次 resolve 会返回它——错误于是从安装期推迟到第一次 `run_tool`。
        """
        self.fetch.serve_bytes(
            "https://fixture.invalid/zipbin.zip",
            zip_bytes([("pkg-1.0/bin/zipbin.exe", b"this is not a PE file")]))
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                probe=True, version_args=("--version",))
        self.assertIn("起不来", str(caught.exception))
        self.assertFalse(os.path.exists(os.path.join(self.root, "zipbin")))
        self.assertEqual("", toolinstall.resolve("zipbin", self.root))
        self.assertFalse(os.path.exists(toolinstall.registry_path(self.root)))
        self.assertEqual([], self.leftovers())

    def test_a_failed_reinstall_keeps_the_working_one(self):
        """装在的时候重装失败，**原来那份必须还在**。

        换入的顺序是"先把旧的挪走、再把新的放进去，失败就挪回来"。少了挪回
        那一步，一次失败的升级会把一个本来能用的工具变成"没有"。
        """
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False)
        good = toolinstall.resolve("zipbin", self.root)
        self.fetch.serve_bytes(
            "https://fixture.invalid/zipbin.zip",
            zip_bytes([("pkg-9.0/bin/zipbin.exe", b"not a PE")]))
        with self.assertRaises(toolinstall.InstallError):
            toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                probe=True, version_args=("--version",))
        self.assertEqual(good, toolinstall.resolve("zipbin", self.root))
        self.assertEqual([], self.leftovers())

    def test_stale_staging_is_swept_but_a_fresh_one_is_left_alone(self):
        """另一个进程可能正在装。只清**过时**的 staging。"""
        fresh = os.path.join(self.root, ".staging-fresh")
        stale = os.path.join(self.root, ".staging-stale")
        os.makedirs(fresh)
        os.makedirs(stale)
        old = toolinstall.time.time() - 7200
        os.utime(stale, (old, old))
        swept = toolinstall._sweep_staging(self.root)
        self.assertEqual([".staging-stale"], swept)
        self.assertTrue(os.path.isdir(fresh))
        self.assertFalse(os.path.exists(stale))


class RemoveTestCase(_RecipeFixtureTestCase):

    def test_remove_deletes_the_tree_and_the_row(self):
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False)
        out = toolinstall.remove("zipbin", self.root)
        self.assertTrue(out["removed"])
        self.assertTrue(out["hadRegistryRow"])
        self.assertGreater(out["freedBytes"], 0)
        self.assertFalse(os.path.exists(os.path.join(self.root, "zipbin")))
        self.assertEqual("", toolinstall.resolve("zipbin", self.root))
        with open(toolinstall.registry_path(self.root), encoding="utf-8") as fh:
            self.assertEqual({}, json.load(fh)["tools"])

    def test_remove_is_idempotent(self):
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False)
        toolinstall.remove("zipbin", self.root)
        again = toolinstall.remove("zipbin", self.root)
        self.assertFalse(again["removed"])
        self.assertFalse(again["hadRegistryRow"])
        self.assertEqual(0, again["freedBytes"])

    def test_remove_refuses_an_id_that_escapes(self):
        """注册表与调用方都可能给出一个逃出根目录的 id。删除动作不能照它做。"""
        for bad in ("..", "../..", "", ".", "a/../../b"):
            with self.assertRaises(toolinstall.InstallError):
                toolinstall.remove(bad, self.root)


# ---------------------------------------------------------------- 与 toolkit 接缝


class ToolkitIntegrationTestCase(_RecipeFixtureTestCase):
    """`toolkit` 的查找顺序：**先项目目录，再 PATH**。

    顺序反了会出现一件没有单独一条错误信息能解释的事：安装成功、文件确实在、
    而 `run_tool` 用的还是系统那一份旧版本。
    """

    def setUp(self):
        super().setUp()
        self.path_dir = tempfile.mkdtemp(prefix="ti-path-")
        with open(os.path.join(self.path_dir, "chisel.exe"), "wb") as fh:
            fh.write(b"MZ-system-one")
        self._env = mock.patch.dict(os.environ, {
            "PENTEST_TOOLS_ROOT": self.root,
            "PATH": self.path_dir + os.pathsep + os.environ.get("PATH", ""),
        })
        self._env.start()
        toolkit.detect(refresh=True)

    def tearDown(self):
        self._env.stop()
        toolkit.detect(refresh=True)
        toolinstall.RECIPES = self._saved
        shutil.rmtree(self.path_dir, ignore_errors=True)
        shutil.rmtree(self.root, ignore_errors=True)

    def _chisel(self):
        return {row["id"]: row for row in toolkit.detect(refresh=True)}["chisel"]

    def test_path_installed_tool_is_reported_as_path(self):
        row = self._chisel()
        self.assertTrue(row["installed"])
        self.assertEqual("path", row["source"])

    def test_project_install_wins_over_path(self):
        toolinstall.install("chisel", fetch=self.fetch, root=self.root, probe=False)
        row = self._chisel()
        self.assertTrue(row["installed"])
        self.assertEqual("project", row["source"],
                         "PATH 上那份把项目目录那份盖住了")
        self.assertTrue(row["path"].startswith(os.path.realpath(self.root)))
        self.assertEqual([row["path"]], row["command"])

    def test_removing_the_project_copy_falls_back_to_path(self):
        toolinstall.install("chisel", fetch=self.fetch, root=self.root, probe=False)
        self.assertEqual("project", self._chisel()["source"])
        toolinstall.remove("chisel", self.root)
        row = self._chisel()
        self.assertEqual("path", row["source"])
        self.assertTrue(row["installed"])

    def test_detect_reports_installability_for_every_adapter(self):
        rows = toolkit.detect(refresh=True)
        self.assertEqual(len(toolkit.ADAPTERS), len(rows))
        for row in rows:
            self.assertIn("installable", row)
            self.assertIn("source", row)
            self.assertIn("command", row)
        expected = sorted(
            adapter_id for adapter_id in toolkit.ADAPTER_INDEX
            if str((toolinstall.RECIPES.get(adapter_id) or {}).get("channel")
                   or "manual") != "manual")
        self.assertEqual(expected,
                         sorted(row["id"] for row in rows if row["installable"]))

    def test_require_says_the_platform_can_install_it(self):
        """缺一个可自动安装的工具时，错误信息必须指向**动作**。

        只说"未安装"，模型的下一个动作是让用户自己去下载；而平台自己就能装。
        同时"为什么不自己实现"那一段不能被挤掉：它回答的是另一个问题，
        而且正是"重造会得到更坏的结果"这条判断的落点。
        """
        with self.assertRaises(toolkit.ToolkitError) as caught:
            toolkit.require("nmap")
        message = str(caught.exception)
        self.assertIn("install_tool", message)
        self.assertIn("nmap", message)
        self.assertIn("服务指纹库", message)

    def test_missing_report_names_the_tools_root(self):
        report = toolkit.missing_report()
        self.assertIn(toolinstall.display(toolinstall.tools_root()), report)
        self.assertIn("install_tool", report)

    def test_build_command_uses_the_project_prefix(self):
        toolinstall.install("chisel", fetch=self.fetch, root=self.root, probe=False)
        # `build_command` 读的是 `detect()` 的缓存，所以这里显式重探一次——
        # 产品路径上做这件事的是 `_tool_install_tool`，见下面那条用例。
        toolkit.detect(refresh=True)
        argv = toolkit.build_command("chisel", ["--version"])
        self.assertTrue(argv[0].startswith(os.path.realpath(self.root)), argv)
        self.assertEqual("--version", argv[1])

    def test_require_refreshes_a_missed_cache(self):
        """缓存说"没装"时要再探一次再下结论。

        覆盖的是**执行节点**：它的缓存在进程启动时建立，而工具是之后由控制面
        装进来的。不重探的话，"装好了却还是用不了"会持续到它重启——而使用者
        没有任何理由认为需要重启。
        """
        os.remove(os.path.join(self.path_dir, "chisel.exe"))
        toolkit.detect(refresh=True)                 # 缓存：chisel 哪儿都没有
        self.assertFalse({r["id"]: r for r in toolkit.detect()}["chisel"]["installed"])
        toolinstall.install("chisel", fetch=self.fetch, root=self.root, probe=False)
        # 刻意**不** refresh：这条路要由 require 自己走完
        entry = toolkit.require("chisel")
        self.assertTrue(entry["path"].startswith(os.path.realpath(self.root)),
                        entry["path"])

    def test_the_handler_refreshes_the_cache_after_installing(self):
        """产品路径上真正保证"装完立刻可用"的是 handler 里那一句 refresh。

        `run_tool` 读的是 `detect()` 的缓存。少了那次重探，症状是"装完了但
        run_tool 说没有"——一处单独看都说得通、合起来不成立的错。所以这里
        **全程自己不 refresh**，读到的就是产品当下看到的。

        用 sqlmap 而不是 chisel：handler 会拿真表里的版本参数去校验装下来的
        东西，而一个假造的 `.exe` 会被 Windows 直接拒（WinError 216）。这条
        顺带证明了那一层校验真的在跑——假二进制过不去。
        """
        self.assertFalse({r["id"]: r for r in toolkit.detect()}["sqlmap"]["installed"])
        directory = tempfile.mkdtemp(prefix="ti-ops-")
        try:
            ledger = store_module.Store(os.path.join(directory, "p.db"))
            plane = _Plane(ledger)
            # handler 不接受取数器参数（那是给测试用的注入点，不是产品接口），
            # 所以换掉模块里那个默认值。`install()` 是在调用时读这个名字的，
            # 替换它就等于替换了"从哪儿下"。
            with mock.patch.object(toolinstall, "default_fetch", self.fetch):
                out = chat._tool_install_tool(plane, {"adapter": "sqlmap"}, "", {})
            self.assertTrue(out["summary"].startswith("已把"), out["summary"])
            row = {r["id"]: r for r in toolkit.detect()}["sqlmap"]
            self.assertTrue(row["installed"],
                            "装完之后缓存没刷新，run_tool 会继续报「没装」")
            self.assertEqual("project", row["source"])
            # `path` 是**工具本身**（脚本），不是解释器。搞混的后果是状态页把
            # sqlmap 显示成 `…\python.exe`，而报错信息指着一个无关的程序。
            self.assertTrue(row["path"].endswith("sqlmap.py"), row["path"])
            self.assertNotEqual(sys.executable, row["path"])
            self.assertEqual([sys.executable, row["path"]], row["command"])
            ops = ledger.tool_ops()
            self.assertEqual(1, len(ops))
            self.assertEqual("done", ops[0]["status"])
            self.assertEqual("sqlmap", ops[0]["adapter"])
            self.assertGreater(ops[0]["bytes"], 0)
            ledger.close()
        finally:
            shutil.rmtree(directory, ignore_errors=True)


# ---------------------------------------------------------------- 取数器


class _FakeResponse:
    """够用的 HTTP 响应替身：够 `_fetch_over` 用就行。"""

    def __init__(self, body, url="https://fixture.invalid/x", declared=None,
                 status=200):
        self._body = body
        self._pos = 0
        self._url = url
        self.status = status
        self.headers = {}
        if declared is not None:
            self.headers["Content-Length"] = str(declared)

    def geturl(self):
        return self._url

    def read(self, size=-1):
        if size is None or size < 0:
            size = len(self._body) - self._pos
        chunk = self._body[self._pos:self._pos + size]
        self._pos += len(chunk)
        return chunk

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        return False


class FetchRoutingTestCase(unittest.TestCase):
    """取数器的两条路。**这一组是真实故障倒逼出来的。**

    实测：一次 22.9 MB 的下载在传输中途被切断，而 `read()` 在连接被干净地关掉
    时返回空串——循环正常结束，落盘的是一份少了尾巴的 zip。它最后报成"不认识
    的归档格式"，把原因指向了解压库。
    """

    def setUp(self):
        self._proxy = mock.patch.object(toolinstall, "system_proxy", lambda: "")
        self._proxy.start()
        self.dir = tempfile.mkdtemp(prefix="ti-fetch-")

    def tearDown(self):
        self._proxy.stop()
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_a_short_body_is_refused_by_the_declared_length(self):
        body = b"PK\x03\x04" + b"x" * 96            # 100 字节
        with mock.patch.object(toolinstall, "_open",
                               lambda url, timeout, proxy="": _FakeResponse(body, declared=250)):
            with self.assertRaises(toolinstall.InstallError) as caught:
                toolinstall.default_fetch("https://fixture.invalid/a.zip",
                                          os.path.join(self.dir, "a.zip"))
        message = str(caught.exception)
        self.assertIn("截断", message)
        self.assertIn("100", message)
        self.assertIn("250", message)

    def test_a_complete_body_passes(self):
        body = b"PK\x03\x04" + b"x" * 96
        with mock.patch.object(toolinstall, "_open",
                               lambda url, timeout, proxy="": _FakeResponse(body)):
            got = toolinstall.default_fetch("https://fixture.invalid/a.zip",
                                            os.path.join(self.dir, "a.zip"))
        self.assertEqual(100, got["bytes"])
        self.assertEqual("direct", got["route"])

    def test_a_failed_direct_route_falls_back_to_the_system_proxy(self):
        """这台机器上实测过的那件事：直连被切断，走代理立刻通。"""
        seen = []

        def opener(url, timeout, proxy=""):
            seen.append(proxy)
            if not proxy:
                raise urllib.error.URLError("SSL: UNEXPECTED_EOF_WHILE_READING")
            return _FakeResponse(b"hello")

        with mock.patch.object(toolinstall, "system_proxy",
                               lambda: "http://127.0.0.1:7897"):
            with mock.patch.object(toolinstall, "_open", opener):
                got = toolinstall.default_fetch("https://fixture.invalid/x")
        self.assertEqual(["", "http://127.0.0.1:7897"], seen,
                         "应当先直连、失败再走代理")
        self.assertEqual("proxy", got["route"])
        self.assertEqual("hello", got["text"])

    def test_an_http_error_is_not_retried_over_the_proxy(self):
        """404 换条路还是 404。重试只会让错误晚一点出现。"""
        seen = []

        def opener(url, timeout, proxy=""):
            seen.append(proxy)
            raise urllib.error.HTTPError(url, 404, "Not Found", {}, None)

        with mock.patch.object(toolinstall, "system_proxy",
                               lambda: "http://127.0.0.1:7897"):
            with mock.patch.object(toolinstall, "_open", opener):
                with self.assertRaises(toolinstall.InstallError) as caught:
                    toolinstall.default_fetch("https://fixture.invalid/x")
        self.assertEqual([""], seen)
        self.assertIn("404", str(caught.exception))

    def test_when_both_routes_fail_the_error_names_both(self):
        def opener(url, timeout, proxy=""):
            raise urllib.error.URLError("boom")

        with mock.patch.object(toolinstall, "system_proxy",
                               lambda: "http://127.0.0.1:7897"):
            with mock.patch.object(toolinstall, "_open", opener):
                with self.assertRaises(toolinstall.InstallError) as caught:
                    toolinstall.default_fetch("https://fixture.invalid/x")
        message = str(caught.exception)
        self.assertIn("direct", message)
        self.assertIn("proxy", message)
        self.assertIn("HTTPS_PROXY", message)


class SystemProxyTestCase(unittest.TestCase):

    def test_environment_variable_wins_and_gets_a_scheme(self):
        with mock.patch.dict(os.environ,
                             {"HTTPS_PROXY": "proxy.local:3128"}, clear=False):
            self.assertEqual("http://proxy.local:3128", toolinstall.system_proxy())
        with mock.patch.dict(os.environ,
                             {"HTTPS_PROXY": "https://proxy.local:3128"},
                             clear=False):
            self.assertEqual("https://proxy.local:3128", toolinstall.system_proxy())

    def test_an_empty_environment_falls_back_to_the_registry(self):
        """这台机器上 `github.com` 直连是断的，救场的代理只在 WinINET 里。

        `ProxyEnable=0` 但设了 PAC：PAC 的存在说明出站是被人安排过的，而
        `ProxyServer` 是唯一能拿到的那一份地址。
        """
        env = {key: "" for key in
               ("HTTP_PROXY", "HTTPS_PROXY", "ALL_PROXY",
                "http_proxy", "https_proxy", "all_proxy")}
        with mock.patch.dict(os.environ, env):
            self.assertEqual({}, urllib.request.getproxies(),
                             "环境变量清空后 getproxies 不该还有货")
            proxy = toolinstall.system_proxy()
            if os.name == "nt":
                # 值取决于这台机器的注册表，所以只钉**形状**
                self.assertTrue(proxy == "" or proxy.startswith("http"), proxy)
            else:
                self.assertEqual("", proxy)


class ProbeRetryTestCase(_RecipeFixtureTestCase):
    """起版本命令失败时，**权限/占用那一类要重试一次**。

    实测过：紧跟写盘之后运行刚解出来的 `.exe`，偶发拿到 `WinError 5（拒绝访问）`
    ——文件在这一小段时间里被谁的扫描/索引持有；隔一会儿再运行同一个文件，正常。
    把这种失败当成"这个二进制是坏的"，会把一次本来成功的安装回滚掉。
    """

    def _run_probe(self, side_effect, attempts=2, **kwargs):
        with mock.patch.object(toolinstall.subprocess, "run",
                               side_effect=side_effect):
            return toolinstall.probe_binary(["x", "--version"], pause=0.0,
                                            attempts=attempts, **kwargs)

    def test_access_denied_is_retried(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            if len(calls) == 1:
                raise PermissionError(13, "Access is denied")
            return toolinstall.subprocess.CompletedProcess(argv, 0,
                                                           stdout=b"tool 1.0\n")

        result = self._run_probe(run)
        self.assertTrue(result["ok"])
        self.assertEqual(2, len(calls), "权限类失败必须重试")
        self.assertEqual(2, result["attempts"])
        self.assertIn("tool 1.0", result["output"])

    def test_other_errors_are_not_retried(self):
        """`WinError 193`（不是有效的 Win32 程序）重试多少次都一样。"""
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            raise OSError("not a valid Win32 application")

        result = self._run_probe(run)
        self.assertFalse(result["ok"])
        self.assertEqual(1, len(calls))
        self.assertEqual(1, result["attempts"])
        self.assertIn("Win32", result["output"])

    def test_it_gives_up_after_the_attempt_budget(self):
        calls = []

        def run(argv, **kwargs):
            calls.append(argv)
            raise PermissionError(13, "Access is denied")

        result = self._run_probe(run, attempts=3)
        self.assertFalse(result["ok"])
        self.assertEqual(3, len(calls))
        self.assertEqual(3, result["attempts"])

    def test_a_probe_that_times_out_still_counts_as_started(self):
        """超时说明它起来了（没有 OSError），只是这个参数让它停不下来。"""
        def run(argv, **kwargs):
            raise toolinstall.subprocess.TimeoutExpired(argv, 5.0)

        result = self._run_probe(run)
        self.assertTrue(result["ok"])
        self.assertTrue(result["timedOut"])


    def test_a_transient_access_denied_replace_is_retried(self):
        """同一类瞬时占用也会打在 `os.replace` 上，不是只打在启动上。

        实测：把刚解好的目录 rename 进去拿到 `WinError 5`，隔一会儿再执行成功。
        不重试的表现是"装不下来"，而真正的原因在几百毫秒的时序里。
        """
        calls = []
        real = os.replace

        def flaky(src, dst, **kwargs):
            calls.append((src, dst))
            if len(calls) == 1:
                raise PermissionError(13, "Access is denied")
            return real(src, dst, **kwargs)

        with mock.patch.object(toolinstall.os, "replace", flaky):
            out = toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                      probe=False)
        self.assertTrue(out["ok"])
        self.assertGreaterEqual(len(calls), 2, "rename 必须重试")
        self.assertTrue(toolinstall.resolve("zipbin", self.root))

    def test_a_failed_replace_that_cannot_roll_back_says_where_the_old_one_is(self):
        """回滚也失败时**必须说清旧那份在哪**。

        否则一次失败的升级会把一个本来能用的工具变成"没有"，而使用者手上没有
        任何线索去找它——文件还在，只是名字变了。
        """
        toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                            probe=False)
        real = os.replace
        blocked = os.path.join(self.root, "zipbin")

        def flaky(src, dst, **kwargs):
            # 目标恒为 `zipbin` 的那两次（换入、以及回滚）都拒；
            # 挪去 `.prev-*` 的那一次放行。
            if dst == blocked:
                raise OSError(13, "Access is denied")
            return real(src, dst, **kwargs)

        self.fetch.serve_bytes(
            "https://fixture.invalid/zipbin.zip",
            zip_bytes([("pkg-2.0/bin/zipbin.exe", b"MZ")]))
        with mock.patch.object(toolinstall.os, "replace", flaky):
            # 重试之间要 sleep，这里把它抹掉：不然这条用例要等好几秒
            with mock.patch.object(toolinstall.time, "sleep", lambda *_: None):
                with self.assertRaises(toolinstall.InstallError) as caught:
                    toolinstall.install("zipbin", fetch=self.fetch,
                                        root=self.root, probe=False)
        message = str(caught.exception)
        self.assertIn("挪回去也失败了", message)
        self.assertIn(".prev-", message)
        # 旧那份必须还在，而且**没有被删**
        leftovers = [n for n in os.listdir(self.root)]
        self.assertTrue(any(".prev-" in name for name in leftovers), leftovers)


class MissingMemberDiagnosisTestCase(_RecipeFixtureTestCase):
    """解压后成员不在时，**必须把两种成因分开说**。

    它们的下一步完全不同：一种是配方/布局错了（去改模式），另一种是这台机器上
    有东西在解压过程中把文件拿走了（去改安全软件或换目录）。而只看结果是分不开
    的——两种情况都表现为"文件不在"。分法是一次就地测量（把那个成员单独再解
    一遍），这一组就是在钉那次测量。
    """

    def test_it_says_the_file_appeared_and_was_taken(self):
        with mock.patch.object(toolinstall, "_extract", lambda archive, dest: 2):
            with self.assertRaises(toolinstall.InstallError) as caught:
                toolinstall.install("zipbin", fetch=self.fetch, root=self.root,
                                    probe=False)
        message = str(caught.exception)
        self.assertIn("单独再解一次它落得下来", message)
        self.assertIn("安全软件", message)
        self.assertIn(self.root, message)
        self.assertIn("PENTEST_TOOLS_ROOT", message)
        self.assertEqual([], self.leftovers())

    def test_it_says_the_layout_changed_when_even_one_member_fails(self):
        with mock.patch.object(toolinstall, "_extract", lambda archive, dest: 2):
            with mock.patch.object(toolinstall, "_extract_one",
                                   side_effect=toolinstall.InstallError("没有")):
                with self.assertRaises(toolinstall.InstallError) as caught:
                    toolinstall.install("zipbin", fetch=self.fetch,
                                        root=self.root, probe=False)
        message = str(caught.exception)
        self.assertIn("配方或上游布局变了", message)
        self.assertNotIn("安全软件", message)

    def test_extract_one_pulls_exactly_one_member(self):
        archive = os.path.join(self.root, "one.zip")
        with open(archive, "wb") as handle:
            handle.write(zip_bytes([("pkg-1.0/bin/zipbin.exe", b"MZ"),
                                    ("pkg-1.0/other.txt", b"x")]))
        dest = os.path.join(self.root, "one")
        os.makedirs(dest)
        toolinstall._extract_one(archive, "pkg-1.0/bin/zipbin.exe", dest)
        self.assertTrue(os.path.isfile(
            os.path.join(dest, "pkg-1.0", "bin", "zipbin.exe")))
        self.assertFalse(os.path.exists(os.path.join(dest, "pkg-1.0", "other.txt")))
        with self.assertRaises(toolinstall.InstallError):
            toolinstall._extract_one(archive, "nope.exe", dest)


# ---------------------------------------------------------------- 登记表


class ToolOpsLedgerTestCase(unittest.TestCase):
    """`store.tool_ops`：装工具是部署级动作，必须先记后做。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="ti-db-")
        self.path = os.path.join(self.dir, "pentest.db")
        self.store = store_module.Store(self.path)

    def tearDown(self):
        try:
            self.store.close()
        except Exception:
            pass
        shutil.rmtree(self.dir, ignore_errors=True)

    def test_schema_version_covers_the_tool_ops_ledger(self):
        """工具安装台账是 schema 13 引入的，版本号必须**不低于**它。

        刻意写成 `assertGreaterEqual` 而不是 `== 13`：这个断言要回答的是"这一版的库
        有没有 tool_ops"，而不是"版本号恰好是 13"。写成等号之后，**任何**别的表升一次
        版本都会把它弄红，而那次改动跟工具安装毫无关系——红出来的原因（13 != 14）还会
        把人引到错误的方向去。同一件事在 `test_authz_ops` / `test_footholds` 里也是这么
        写的（`assertGreaterEqual(SCHEMA_VERSION, 12)` / `(…, 10)`）。
        """
        self.assertGreaterEqual(store_module.SCHEMA_VERSION, 13)

    def test_record_and_finish_round_trip(self):
        op = self.store.record_tool_op("install", "nmap", "zip",
                                       "https://nmap.org/dist/nmap-7.92-win32.zip",
                                       "F:/proj/data/tools", "u-1")
        rows = self.store.tool_ops()
        self.assertEqual(1, len(rows))
        self.assertEqual("running", rows[0]["status"])
        self.assertEqual("nmap", rows[0]["adapter"])
        self.assertEqual("zip", rows[0]["channel"])
        self.assertTrue(rows[0]["source"])
        self.assertEqual(0, rows[0]["finishedAt"] or 0)
        self.assertTrue(self.store.finish_tool_op(op, True, size=22892266,
                                                  output="Nmap version 7.92",
                                                  source="https://example/x.zip"))
        row = self.store.tool_ops()[0]
        self.assertEqual("done", row["status"])
        self.assertEqual(22892266, row["bytes"])
        self.assertIn("7.92", row["output"])
        self.assertEqual("https://example/x.zip", row["source"])
        self.assertGreater(row["finishedAt"], 0)

    def test_finish_without_source_keeps_the_recorded_one(self):
        """失败的安装常常停在"地址还没解析出来"那一步。

        此时若拿空串去覆盖 `source`，失败记录就丢掉了唯一的来源线索。
        """
        self.store.record_tool_op("install", "chisel", "github_release",
                                  "jpillora/chisel@v1.12.0")
        op = self.store.tool_ops()[0]["id"]
        self.store.finish_tool_op(op, False, reason="下载失败", source="")
        row = self.store.tool_ops()[0]
        self.assertEqual("failed", row["status"])
        self.assertEqual("jpillora/chisel@v1.12.0", row["source"])
        self.assertEqual("下载失败", row["reason"])

    def test_an_old_database_gets_the_table_back(self):
        """旧库（schema 12）没有 tool_ops；用新代码打开要自动补出来。

        模拟方式是把表删掉并把版本号写回去——这比手写一份旧 DDL 更接近真实的
        "库比代码旧"：结构确实缺了，而其它数据都还在。
        """
        self.store.conn.execute("DROP TABLE tool_ops")
        self.store.conn.execute("PRAGMA user_version=12")
        self.store.conn.commit()
        self.store.close()
        self.store = store_module.Store(self.path)
        names = {row[0] for row in self.store.conn.execute(
            "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertIn("tool_ops", names)
        # 拿常量比而不是写死 13：这条要钉的是"打开旧库之后版本被抬到**当前**版本"，
        # 而当前版本会随别的表变动。写死数字会让它变成一条"版本号没变过"的断言。
        self.assertEqual(store_module.SCHEMA_VERSION, self.store.conn.execute(
            "PRAGMA user_version").fetchone()[0])
        self.assertEqual([], self.store.tool_ops())

    def test_newer_database_is_refused(self):
        """反过来要挡住：用旧程序打开新库会把它的结构静默用坏。"""
        self.store.close()
        conn = sqlite3.connect(self.path)
        conn.execute("PRAGMA user_version=99")
        conn.commit()
        conn.close()
        with self.assertRaises(Exception):
            store_module.Store(self.path)


# ---------------------------------------------------------------- npm 渠道


class _FakeNpm:
    """替身 npm：按 `--prefix` 落一棵最小的 `node_modules`，并记下 argv。

    真的 npm 不在测试里跑（它要联网、要几十秒）。但这一层要证明的不是"npm 能
    装包"，而是**平台围着它做的那几件事**：staging 在工具根目录内部、入口脚本
    按 `entry` 定位、失败不动项目目录、注册表记 `kind=node` 与解释器路径。
    """

    def __init__(self, returncode=0, output=b"added 1 package\n", make_entry=True,
                 explode=None):
        self.returncode = returncode
        self.output = output
        self.make_entry = make_entry
        self.explode = explode
        self.calls = []

    def __call__(self, argv, **kwargs):
        self.calls.append({"argv": list(argv), "kwargs": kwargs})
        if self.explode is not None:
            raise self.explode
        prefix = ""
        for index, item in enumerate(argv):
            if item == "--prefix" and index + 1 < len(argv):
                prefix = argv[index + 1]
        if prefix and self.make_entry:
            target = os.path.join(prefix, "node_modules", "@acme", "cli", "cli.js")
            os.makedirs(os.path.dirname(target), exist_ok=True)
            with open(target, "w", encoding="utf-8") as handle:
                handle.write("console.log(1)\n")
        done = type("_Done", (), {})()
        done.returncode = self.returncode
        done.stdout = self.output
        return done


NPM_RECIPE = {
    "channel": "npm",
    "package": "@acme/cli",
    "version": "1.2.3",
    "entry": "node_modules/@acme/cli/cli.js",
    "size": 1000,
    "note": "夹具：npm 渠道",
}


class NpmChannelTestCase(unittest.TestCase):
    def setUp(self):
        self.root = tempfile.mkdtemp(prefix="ti-npm-")
        self._saved = toolinstall.RECIPES
        toolinstall.RECIPES = dict(self._saved)
        toolinstall.RECIPES["npmtool"] = dict(NPM_RECIPE)
        self.node = os.path.join(self.root, "node.exe")
        open(self.node, "wb").close()
        self.cli = os.path.join(self.root, "npm-cli.js")
        with open(self.cli, "w", encoding="utf-8") as handle:
            handle.write("// npm\n")

    def tearDown(self):
        toolinstall.RECIPES = self._saved
        shutil.rmtree(self.root, ignore_errors=True)

    def _patched(self, fake):
        patches = [
            mock.patch.object(toolinstall, "node_executable",
                              return_value=self.node),
            mock.patch.object(toolinstall, "npm_cli", return_value=self.cli),
            mock.patch.object(toolinstall.subprocess, "run", fake),
        ]
        for item in patches:
            item.start()
            self.addCleanup(item.stop)
        return patches

    def test_it_runs_npm_cli_through_node_not_through_a_cmd_wrapper(self):
        """argv 是 `[node, npm-cli.js, install, --prefix, …]`。

        `npm.cmd` 会把每个参数再过一遍 cmd.exe 的解析，而本模块的前提是"没有
        shell 被插进来"。这一条钉住那个形状：前缀两段都是文件。
        """
        fake = _FakeNpm()
        self._patched(fake)
        out = toolinstall.install("npmtool", root=self.root, probe=False)
        argv = fake.calls[0]["argv"]
        self.assertEqual([self.node, self.cli, "install"], argv[:3], argv)
        self.assertEqual("@acme/cli@1.2.3", argv[-1], argv)
        self.assertIn("--no-audit", argv)
        self.assertIn("--no-save", argv)
        self.assertFalse(fake.calls[0]["kwargs"].get("shell"),
                         "npm 必须不经 shell")
        self.assertEqual("npm", out["channel"])
        self.assertEqual("node", out["kind"])
        self.assertEqual("npm:@acme/cli@1.2.3", out["source"])
        self.assertGreater(out["treeFiles"], 0)

    def test_the_registry_remembers_which_node_ran_it(self):
        """跑的时候要用**装的那一刻那个 node**：版本管理器换过版本之后 PATH
        上那个可能已经不是它了。"""
        fake = _FakeNpm()
        self._patched(fake)
        toolinstall.install("npmtool", root=self.root, probe=False)
        entry = toolinstall._load_registry(self.root)["tools"]["npmtool"]
        self.assertEqual("node", entry["kind"])
        self.assertEqual(self.node, entry["interpreter"])
        self.assertTrue(entry["bin"].endswith("node_modules/@acme/cli/cli.js"),
                        entry["bin"])
        self.assertEqual([self.node, toolinstall.resolve("npmtool", self.root)],
                         toolinstall.command_prefix("npmtool", self.root))

    def test_without_node_it_says_what_to_install(self):
        with mock.patch.object(toolinstall, "node_executable", return_value=""):
            with mock.patch.object(toolinstall, "npm_cli", return_value=""):
                with self.assertRaises(toolinstall.InstallError) as caught:
                    toolinstall.install("npmtool", root=self.root, probe=False)
        message = str(caught.exception)
        self.assertIn("node", message)
        self.assertIn("Node.js", message)
        self.assertFalse(os.path.isdir(os.path.join(self.root, "npmtool")),
                         "失败不该留下已换入的目录")

    def test_a_node_without_npm_is_named_as_such(self):
        with mock.patch.object(toolinstall, "node_executable", return_value=self.node):
            with mock.patch.object(toolinstall, "npm_cli", return_value=""):
                with self.assertRaises(toolinstall.InstallError) as caught:
                    toolinstall.install("npmtool", root=self.root, probe=False)
        message = str(caught.exception)
        self.assertIn("npm-cli.js", message)
        self.assertIn(self.node, message)

    def test_a_failed_install_leaves_the_project_directory_untouched(self):
        kept = os.path.join(self.root, "npmtool")
        os.makedirs(kept)
        with open(os.path.join(kept, "keep.txt"), "w", encoding="utf-8") as handle:
            handle.write("上一版")
        fake = _FakeNpm(returncode=1, output=b"npm ERR! 404 Not Found\n")
        self._patched(fake)
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall.install("npmtool", root=self.root, probe=False)
        message = str(caught.exception)
        self.assertIn("404", message)
        self.assertIn("退出码 1", message)
        self.assertTrue(os.path.isfile(os.path.join(kept, "keep.txt")),
                        "失败之后原来那份必须还在")
        self.assertEqual([], [name for name in os.listdir(self.root)
                              if name.startswith(".staging-")],
                         "staging 没被收掉")

    def test_a_timeout_tells_the_caller_about_timeoutS(self):
        fake = _FakeNpm(explode=toolinstall.subprocess.TimeoutExpired("npm", 30))
        self._patched(fake)
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall.install("npmtool", root=self.root, timeout=30.0, probe=False)
        message = str(caught.exception)
        self.assertIn("timeoutS", message)

    def test_a_missing_entry_is_called_a_recipe_error(self):
        fake = _FakeNpm(make_entry=False)
        self._patched(fake)
        with self.assertRaises(toolinstall.InstallError) as caught:
            toolinstall.install("npmtool", root=self.root, probe=False)
        message = str(caught.exception)
        self.assertIn("配方错误", message)
        self.assertIn("cli.js", message)

    def test_the_plan_reports_the_package_as_its_origin(self):
        """dry-run 那一行要回答"它从哪来"。npm 没有 URL，来源就是包名。"""
        rows = {item["adapter"]: item for item in
                toolinstall.plan(root=self.root)["todo"]}
        self.assertIn("npmtool", rows)
        self.assertEqual("npm:@acme/cli@1.2.3", rows["npmtool"]["url"])


if __name__ == "__main__":
    unittest.main()
