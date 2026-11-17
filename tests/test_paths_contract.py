# -*- coding: utf-8 -*-
"""路径契约：文档里写的项目文件必须真的存在；源码里不许写死本机目录。

起因是一轮"把路径统一成相对路径"的清扫，里面挖出两类真缺陷：

1. `DSH集成说明.md` 的文件表、复现命令与测试清单还在引用 `pentestd/dsh_web.py`、
   `pentestd/dsh_proxy.py`、`web/dsh.js`、`tools/verify_dsh_web.py` 与两个测试模块，
   而那些文件早已删除。照文档做的人会在第一条命令上撞到"文件不存在"，然后开始怀疑
   自己搭错了环境——**一条指向不存在文件的路径，比没有路径更难查**。
2. 八个探针脚本各抄了一份写死的 `C:\\Program Files\\...` 浏览器候选表。重复只是
   表面；真问题是系统装在别的盘、或浏览器装在自定义目录时，整张表落空，探针打印 SKIP，
   看起来像"本机没有浏览器"，而实际上有。

两条都不会让任何测试变红，所以在这里钉住。
"""
import io
import os
import re
import unittest

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

DOCS = sorted([name for name in os.listdir(ROOT) if name.endswith(".md")])
DOCS += ["docs/" + name for name in sorted(os.listdir(os.path.join(ROOT, "docs")))
         if name.endswith(".md")]
DOCS += ["deploy/expose.md", "prototype/README.md"]

# 反引号里标出来的项目文件：只收项目内的顶层目录，免得把外部路径当成项目文件。
REF = re.compile(r"`((?:pentestd|web|tools|tests|deploy|docs|prototype|dsh-plugins)"
                 r"/[\w.\-]+\.(?:py|js|mjs|html|css|md|json|toml|cmd|service|yml|yaml))`")
# 讲"已删除"的句子是有意保留的历史，不要求文件存在。
HISTORICAL = ("删除", "已删", "删掉", "删了", "历史", "曾经", "不再存在", "被否掉")

CMD = re.compile(r"python(?:3)? +tools/[\w.\-]+\.py")

CODE_DIRS = ("pentestd", "web", "tools", "deploy")
CODE_EXT = (".py", ".js", ".mjs", ".html", ".css", ".cmd", ".ps1", ".service", ".toml")

# 盘符路径。前面的 lookbehind 放过 `http://` / `file:///` / `wsl://` 这类 URL。
DRIVE = re.compile(r"(?<![A-Za-z0-9+])[A-Za-z]:[\\/]")

# 这些名字出现在盘符路径里 = 写死了某台机器的目录。
MACHINE = ("Users", "Desktop", "Documents", "Downloads", "AppData", "ai-vuln-agent")
# git-bash 风格的本机挂载点（文档里已清掉，代码里也不许有）。
MOUNTS = ("/f/Desktop", "/d/AI Programs", "Desktop/ai-vuln-agent")

# 讲 OS 安装目录的例外：只允许这两处。
PROGRAM_FILES_OK = ("tools/_browser.py", "pentestd/toolinstall.py")
# 浏览器候选表的唯一一份。
BROWSER_TABLE = "tools/_browser.py"
BROWSER_NAMES = ("msedge.exe", "chrome.exe", "chromium")


def read(path):
    with io.open(path, encoding="utf-8", errors="replace") as handle:
        return handle.read()


def docs():
    for rel in DOCS:
        path = os.path.join(ROOT, rel)
        if os.path.exists(path):
            yield rel, read(path)


def code_files():
    for top in CODE_DIRS:
        for dirpath, dirnames, filenames in os.walk(os.path.join(ROOT, top)):
            dirnames[:] = [d for d in dirnames if d not in ("__pycache__", "node_modules")]
            for name in filenames:
                if name.endswith(CODE_EXT):
                    yield os.path.join(dirpath, name)


def relpath(path):
    return os.path.relpath(path, ROOT).replace(os.sep, "/")


class PathContractTestCase(unittest.TestCase):
    """路径本身的契约：写着的东西得在，且只能是相对项目根的写法。"""

    def test_documents_only_name_files_that_exist(self):
        missing = []
        for rel, text in docs():
            # 按**段落**判断"历史"：那几处有意保留的删除记录常常跨行，
            # 只认单行会让同一句话的后半行被当成现状。
            for paragraph in text.split("\n\n"):
                if any(word in paragraph for word in HISTORICAL):
                    continue
                for match in REF.finditer(paragraph):
                    ref = match.group(1)
                    if not os.path.exists(os.path.join(ROOT, ref)):
                        missing.append("%s  %s" % (rel, ref))
        self.assertEqual([], missing,
                         "文档里指向了不存在的项目文件（照文档做的人会撞在第一条命令上）："
                         "\n" + "\n".join(missing))

    def test_documented_commands_point_at_real_tools(self):
        missing = []
        for rel, text in docs():
            for number, line in enumerate(text.split("\n"), 1):
                for match in CMD.finditer(line):
                    script = match.group(0).split()[-1]
                    if not os.path.exists(os.path.join(ROOT, script)):
                        missing.append("%s:%d  %s" % (rel, number, match.group(0)))
        self.assertEqual([], missing,
                         "文档里的命令指向了不存在的脚本：\n" + "\n".join(missing))

    def test_code_has_no_hardcoded_machine_paths(self):
        bad = []
        for path in code_files():
            rel = relpath(path)
            text = read(path)
            for number, line in enumerate(text.split("\n"), 1):
                hit = DRIVE.search(line) and any(word in line for word in MACHINE)
                if hit or any(token in line for token in MOUNTS):
                    bad.append("%s:%d  %s" % (rel, number, line.strip()[:100]))
        self.assertEqual([], bad,
                         "源码里写死了本机目录（改成相对路径，或从环境变量推导）："
                         "\n" + "\n".join(bad))

    def test_program_files_is_only_mentioned_where_os_dirs_are_explained(self):
        bad = []
        for path in code_files():
            rel = relpath(path)
            if rel in PROGRAM_FILES_OK:
                continue
            text = read(path)
            if "Program Files" in text:
                bad.append(rel)
        self.assertEqual([], bad,
                         "只有讲 OS 安装目录的那两处可以写 Program Files；"
                         "别处再抄一张安装路径表，换盘/换目录时就会整表落空："
                         "\n" + "\n".join(bad))

    def test_browser_candidates_live_in_one_place(self):
        """浏览器**候选路径表**只能有一份。

        判据是「名字和路径出现在同一行」，不是「出现了这个名字」。要拦的是
        「本机这里有个 chrome.exe」那类抄过来的路径表；而 `chromium` 同时是
        `npx playwright install chromium` 的实参——把那句说明也算成第二张表，
        只会逼人去改写一条正确的话，真正的重复表反而没人管。
        """
        self.assertTrue(
            any(name in read(os.path.join(ROOT, BROWSER_TABLE))
                for name in BROWSER_NAMES),
            "%s 里没有候选名，这条断言就变成恒真的了" % BROWSER_TABLE)
        bad = []
        for path in code_files():
            rel = relpath(path)
            if rel == BROWSER_TABLE:
                continue
            for line in read(path).splitlines():
                if not any(name in line for name in BROWSER_NAMES):
                    continue
                if not (DRIVE.search(line) or "/" in line or "\\" in line):
                    continue
                bad.append(rel)
                break
        self.assertEqual([], bad,
                         "浏览器候选表只能有一份（%s），其余脚本 import 它："
                         "\n" % BROWSER_TABLE + "\n".join(bad))


if __name__ == "__main__":
    unittest.main()
