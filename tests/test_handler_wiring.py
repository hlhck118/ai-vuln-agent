# -*- coding: utf-8 -*-
"""工具注册表的**可调用性**契约。

这个文件的存在有具体来历：`list_toolkit` 曾经**每一次调用**都抛 NameError。
`chat.py` 用 `from . import toolkit as toolkit_module` 导入，而 `run_tool`
改用别名之后，`_tool_list_toolkit` 里的三处裸名 `toolkit` 没跟着改。于是：

* `invoke_tool` 的 `except Exception` 把它变成一句「内部错误: name 'toolkit'
  is not defined」**回灌给模型**——它长得像一个偶发故障，实际是必然失败；
* 1175 项测试全绿，因为这个缺口的位置很具体：既有用例遍历 `chat.TOOLS` 时
  只检查**名字**与**schema**，从不调用任何一个工具。

**注册齐全不等于可以调用。** 这个文件补的就是后半句。两层：

1. `HandlerWiringGuardTestCase` —— 生产侧那道闸（`chat._handler_wiring_guard`
   在 import 时运行），以及它**自己能不能失败**；
2. `RegisteredToolIsCallableTestCase` —— 端到端：真的把工具调一次。

第三层 `PackageBoundNamesTestCase` 是同一类缺陷在**整个包**上的网：
handler 之外的地方（`creds.py`、`subagent.py`……）写错全局名同样只在运行到那
一行时才炸，而那里没有 handler 闸门。
"""
import builtins
import inspect
import io
import os
import symtable
import unittest

from pentestd import chat
from pentestd import policy
from pentestd import subagent
from pentestd import toolinstall

PACKAGE_DIR = os.path.join(
    os.path.dirname(os.path.dirname(os.path.abspath(__file__))), "pentestd")

# 原始的缺陷形态，用来证明闸门抓得住它。刻意保留裸名 `toolkit`：
# 这条源码就是当初那一行，改掉它这个用例就不再证明任何事。
BROKEN_SOURCE = (
    "def handler(plane, args, fallback_eid, session):\n"
    "    return toolkit.detect()\n"
)


def _build(source, namespace):
    """把一段源码编译进给定命名空间，返回它定义的 `handler`。"""
    scope = dict(namespace)
    exec(compile(source, "<fixture>", "exec"), scope)
    return scope["handler"]


def _unbound_names(source, filename="<string>"):
    """静态列出「被引用、但任何作用域都没有绑定」的名字。

    `py_compile` 不查名字，所以这类错能一路过 CI；`python -O` 也照过。
    只有到了那一行才炸，而炸的表现取决于外面有没有 `except Exception`。
    """
    table = symtable.symtable(source, filename, "exec")
    module_names = {sym.get_name() for sym in table.get_symbols()}
    # 由 import 机制注入的模块级名字。它们**在源码里没有被赋值**，所以不进
    # 模块符号表；但引用它们永远不是 NameError。
    #
    # 这一条是补的，起因是一次假红：`tools_root()` 在**函数体里**用 `__file__`
    # 找自己的位置。模块级引用它不会触发（那次引用本身让 `__file__` 进了模块
    # 符号表），函数体里的不会——于是检查器把一句完全正常的话报成了"运行到就会
    # NameError"。假红比漏报更消耗人：它会让人去改一段没有问题的代码。
    known = set(dir(builtins)) | module_names | {
        "__file__", "__name__", "__doc__", "__package__", "__spec__",
        "__loader__", "__builtins__", "__cached__", "__path__", "__dict__",
    }
    found = []

    def visit(tbl, chain):
        for sym in tbl.get_symbols():
            name = sym.get_name()
            if (not sym.is_referenced() or sym.is_assigned()
                    or sym.is_parameter() or sym.is_imported()
                    or name == "*"):
                continue
            # 只有落到「模块全局或内置」的引用才可能未绑定；
            # 闭包变量（LOAD_DEREF）在符号表里是自由的，不算在内。
            if not (sym.is_global() or tbl.get_type() == "module"):
                continue
            if name in known:
                continue
            found.append((chain, name))

    stack = [(table, ())]
    while stack:
        tbl, chain = stack.pop()
        visit(tbl, chain)
        for child in tbl.get_children():
            stack.append((child, chain + (child.get_name(),)))
    return found


class HandlerWiringGuardTestCase(unittest.TestCase):
    """生产侧那道闸：`chat._handler_wiring_guard`。"""

    def test_the_shipped_registry_passes_the_guard(self):
        """已注册的 24 个工具，全局名全部解析得到。"""
        chat._handler_wiring_guard()          # 不抛就是通过

    def test_the_guard_is_called_at_module_level(self):
        """闸门必须在 import 时跑，不是等着谁来调。

        写成对源码行的精确匹配（含行首无缩进）：被挪进某个函数体之后，
        「任何人 import 这个模块都会触发」这句话就不再成立，而那种情况下
        代码照样能跑、测试照样能过。
        """
        with io.open(chat.__file__, encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn("_handler_wiring_guard()", source.splitlines())
    def test_the_checker_finds_the_exact_original_pattern(self):
        """把原缺陷的**原始源码**放进去，检查器要报出 `toolkit`。

        这里刻意只提供 `toolkit_module`，不提供 `toolkit`——就是当初那个
        命名空间的样子。
        """
        handler = _build(BROKEN_SOURCE, {"toolkit_module": object()})
        self.assertEqual(["toolkit"], chat._unresolved_globals(handler))

    def test_attribute_names_are_not_mistaken_for_globals(self):
        """**精度**：`toolkit_module.detect()` 里只有 `toolkit_module` 是全局名。

        `detect` 是 `LOAD_ATTR`，运行时才解析。按源码文本扫描分不开这两者，
        会把每个方法名都算成「未定义」，报出一堆假阳性——那样的检查很快
        就没人看了，等于没有。
        """
        good = ("def handler(plane, args, fallback_eid, session):\n"
                "    return toolkit_module.detect()\n")
        handler = _build(good, {"toolkit_module": object()})
        self.assertEqual([], chat._unresolved_globals(handler))

    def test_inner_functions_are_reached(self):
        """内层函数有自己的 code object，只看外层会漏掉它引用的全局名。

        刻意**不用**列表推导式当夹具：3.12 起 PEP 709 把列表/集合/字典推导
        内联进了外层 code object。用它写这个用例会「因为错误的原因」通过——
        真正该覆盖的那段递归一次都没被执行过。这一点是实测出来的：把递归
        关掉的变异体（`if hasattr(const, "co_code")` → `if False:`）照样全绿。
        """
        nested = ("def handler(plane, args, fallback_eid, session):\n"
                  "    def inner():\n"
                  "        return missing_module.rows\n"
                  "    return inner()\n")
        handler = _build(nested, {})
        self.assertIn("missing_module", chat._unresolved_globals(handler))

    def test_generator_expressions_are_reached_too(self):
        """生成器表达式仍是独立的 code object（PEP 709 没有内联它）。

        这是与列表推导的分界，所以单独钉一条——将来 Python 把生成器也内联了，
        这条会红，提醒把递归那段连同它的用例一起重新审一遍。
        """
        nested = ("def handler(plane, args, fallback_eid, session):\n"
                  "    return (row for row in missing_module.rows)\n")
        handler = _build(nested, {})
        self.assertIn("missing_module", chat._unresolved_globals(handler))

    def test_the_guard_names_the_tool_and_the_missing_name(self):
        """报错必须点名到「哪个工具、缺哪个名字」。

        只说「有 handler 写错了」等于让人自己去找 24 个函数里的某一个。
        """
        handler = _build(BROKEN_SOURCE, {"toolkit_module": object()})
        chat.HANDLERS["__guard_probe__"] = handler
        self.addCleanup(chat.HANDLERS.pop, "__guard_probe__", None)
        with self.assertRaises(RuntimeError) as caught:
            chat._handler_wiring_guard()
        message = str(caught.exception)
        self.assertIn("__guard_probe__", message)
        self.assertIn("toolkit", message)

    def test_every_handler_accepts_the_dispatch_signature(self):
        """`invoke_tool` 固定按 `(plane, args, fallback_eid, session)` 调用。

        签名对不上的表现也是「工具一调就报内部错误」，与全局名丢失同类。
        """
        for name, handler in sorted(chat.HANDLERS.items()):
            try:
                inspect.signature(handler).bind(None, {}, "", {})
            except TypeError as exc:
                self.fail("工具 %s 的 handler 收不下派发签名：%s" % (name, exc))


class RegisteredToolIsCallableTestCase(unittest.TestCase):
    """端到端：走过闸门真的调一次。这是当初缺的那种用例。"""

    def _call(self, name, arguments=None, policy_id="readonly"):
        denied, args = chat.check_tool(
            None, name, arguments or {}, "", policy_id, "", True, {})
        self.assertIsNone(denied, "工具 %s 被闸门挡了：%s" % (name, denied))
        return chat.invoke_tool(None, name, args, "", {})

    def test_list_toolkit_runs_through_the_registered_path(self):
        """回归：这个工具曾经每次都抛 NameError。

        断言的是**返回体形状**而不只是「没抛异常」——`invoke_tool` 会把任何
        异常吞成 `ok: False`，所以「没抛异常」这句话在这里毫无意义。
        """
        out = self._call("list_toolkit")
        self.assertTrue(out["ok"], out.get("summary"))
        data = out["data"] or {}
        self.assertEqual(["capabilities", "installable", "installed", "missing",
                          "recentOps", "report", "toolsRoot", "unavailable"],
                         sorted(data.keys()))
        self.assertIsInstance(data["capabilities"], dict)
        self.assertIsInstance(data["report"], str)
        # 工具根目录：不写出来，使用者找不到刚装的东西，而平台说它装好了。
        self.assertTrue(data["toolsRoot"].endswith(os.path.join("data", "tools")),
                        data["toolsRoot"])
        for row in data["installed"]:
            # 已装的必须给出路径：这个工具的用途就是回答「装在哪、能不能用」。
            # `source` 是后加的，它回答的是**别的**一问：现在生效的是项目目录
            # 那一份、项目内那份发行版里的，还是系统 PATH 那一份——三者可能
            # 同时存在且版本不同，而 run_tool 用的是前两者。
            self.assertEqual(["id", "label", "path", "purpose", "source"],
                             sorted(row.keys()))
            self.assertTrue(row["path"], row)
            self.assertIn(row["source"], ("project", "wsl", "path"))
        for row in data["missing"]:
            # 缺的必须给出「怎么装」与「为什么不自己实现」。
            # `installable`/`channel`/`note` 是后加的：它们把"缺"分成两类——
            # 平台能自己装的，和不能的（不能的那些必须带上**具体**原因）。
            self.assertEqual(["channel", "id", "install", "installable", "label",
                              "note", "purpose", "why"], sorted(row.keys()))
            self.assertTrue(row["install"], row)
            self.assertTrue(row["why"], row)
            self.assertIsInstance(row["installable"], bool)
            if not row["installable"]:
                self.assertTrue(row["note"], "没有自动渠道就必须给出具体原因：%s" % row)
        # 能装的与不能装的必须正好切开所有缺失项，不重不漏。
        self.assertEqual(sorted(data["installable"] + data["unavailable"]),
                         sorted(row["id"] for row in data["missing"]))
        # 这两个集合的**大小**依赖本机装了什么——把可自动安装的都装上之后，
        # `installable` 合理地就是空的。所以这里钉的是"配方表里这两种都存在"，
        # 而不是"本机此刻缺的里面有这两种"；后者会在装完东西之后变红，
        # 而那句话本身没有任何问题。
        channels = {str(spec.get("channel") or "manual")
                    for spec in toolinstall.RECIPES.values()}
        self.assertTrue(channels - {"manual"}, "配方表里应当有可自动安装的")
        self.assertIn("manual", channels, "配方表里应当有明确不可自动安装的")
        # 一个适配器同时出现在两个列表里，表现是界面上既说「已装」又说「缺」
        ids = [row["id"] for row in data["installed"] + data["missing"]]
        self.assertGreater(len(ids), 0)
        self.assertEqual(sorted(set(ids)), sorted(ids))

    def test_list_toolkit_summary_says_what_is_missing(self):
        """摘要要能直接回答「装什么能解决」，以及「有多少平台能自己装」。

        这两件事在两种状态下**答案不同**，所以断言必须跟着分叉。原先判的是
        `data["missing"]`，也就是"只要有缺项就必须出现 install_tool"——那是把
        测试绑在本机装了什么上：把可自动安装的都装好之后（本机现在就是这样），
        那句话合理地不该再出现，因为它会把读的人指向一个**不存在**的目标。
        """
        out = self._call("list_toolkit")
        self.assertIn("已装", out["summary"])
        data = out["data"] or {}
        if data["installable"]:
            self.assertIn("install_tool", out["summary"])
            for adapter_id in data["installable"]:
                self.assertIn(adapter_id, out["summary"],
                              "摘要要点名缺的那一个，否则模型还得再查一次")
        elif data["missing"]:
            self.assertIn("不能自动装", out["summary"])
            self.assertIn("已经装好了", out["summary"],
                          "一个可装的空洞都没有时，必须说清这是「能装的都装好了」；"
                          "否则「0 项可以自己装」会被读成「平台装不了东西」")

    def test_install_tool_is_registered_and_admin_gated(self):
        """`install_tool` 要真的可调，并且**不是**任何档位都能拿到。

        它往这台机器上写可执行文件，而那个文件随后会被 `run_tool` 拉起来。
        所以判据是两条一起：full + 管理员。只判档位不够——它决定的是
        "适配器表里接下来有什么"，与 `run_tool` 能拉起什么不是同一件事。
        """
        self.assertIn("install_tool", chat.HANDLERS)
        self.assertIn("install_tool", chat.TOOL_INDEX)
        self.assertTrue(policy.permits("full", "install_tool", role="admin"))
        self.assertFalse(policy.permits("full", "install_tool", role=""),
                         "装机动作不能只靠档位放行，还要管理员身份")
        self.assertFalse(policy.permits("probe", "install_tool", role="admin"))
        # 子智能体拿不到它：它能改的是"这台机器上有什么"，属于部署级动作
        self.assertIn("install_tool", subagent.CHILD_FORBIDDEN)


class PackageBoundNamesTestCase(unittest.TestCase):
    """同一类缺陷在**整个包**上的网。

    handler 闸门只管 `chat.HANDLERS`。`creds.py`、`subagent.py` 里写错一个
    全局名，同样只在运行到那一行时才炸，而那里没有这道闸门。
    """

    def test_no_module_references_an_unbound_name(self):
        offenders = []
        for base, dirs, files in os.walk(PACKAGE_DIR):
            dirs[:] = [d for d in dirs if d != "__pycache__"]
            for filename in sorted(files):
                if not filename.endswith(".py"):
                    continue
                path = os.path.join(base, filename)
                with io.open(path, encoding="utf-8") as handle:
                    source = handle.read()
                for chain, name in _unbound_names(source, path):
                    offenders.append("%s -> %s（作用域 %s）"
                                     % (os.path.basename(path), name,
                                        ".".join(chain) or "<module>"))
        self.assertEqual([], offenders,
                         "以下引用在任何作用域里都没有绑定，运行到就会 NameError：\n"
                         + "\n".join(offenders))


if __name__ == "__main__":
    unittest.main()
