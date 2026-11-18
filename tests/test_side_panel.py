# -*- coding: utf-8 -*-
"""对话页上下文栏：**目标那一行要说到扫描目标**，**对话记录不能自己消失**。

这两件事是同一次报上来的，而且都在同一栏里，所以放一个文件。与
`test_ui_contract.py` 的关系是"专题与总目"：那一个守整页的结构契约（块的顺序、
折叠、aria、样式表规则），这里只守这一栏里两条容易写歪、写歪了又**不会让任何
功能测试变红**的性质。

## 一、"目标"那一行

报上来的原话是「目标显示详细的扫描目标」。查下去发现根因不在显示层：
**会话自动创建的目标一律叫「渗透目标」**（`store.DEFAULT_SESSION_LABEL`），
于是这一栏里每个目标读起来都一模一样，而"这个目标到底扫了什么"在界面上没有
任何地方回答得了。真正的地址、域名与端口在**任务的 `target`** 上。

所以这里守三件事：

1. 那一行确实把**具体扫描目标**显示出来（去重、封顶、封顶要说、完整清单可达）；
2. 这些数据来自**控制台已经持有的那一份 `/jobs`**，不许在这里再发一次请求
   （两处各拉一次会在某次刷新后给出两份不同的清单）；
3. 任务还没拉回来时**不许写"还没有扫描目标"**——那是"不知道"，不是"没有"，
   而这两件事的下一步动作刚好相反。

## 二、"对话记录"那一块

报上来的原话是「对话记录显示一次消失了」。它在旧代码里有**一条确定的路径**：

    catch (error) { view.sessions = []; … }

任何一次网络抖动、服务重启打断请求、后端 500，都会让这一块改口说
「还没有对话。在右边提问即会新建一条。」——一句关于使用者数据的假话，而且它会
一直停在那儿：`loadSessions` 只在**指纹变化**时被心跳重放，指纹不变时它永远自愈
不了。这一次的修法是不再清空已知列表，另外把三种"还没有"分开：**这次没拉到**、
**还没拉到过**、**确实没有**。

此后（m03903）这一块又多了一条要求：**所有对话都要出现**，按目标分组。此前它只
列当前目标那一批、其余用「另有 N 条属于其他目标」概括——于是"我到底有哪些对话"
得逐个点目标才拼得出来，而"看不见的那批"与"被删掉的"在屏幕上没有区别。

## 写法

与 `test_ui_contract.py` 同一套：切源码文本做断言，**不执行**那份 JS（这个仓库没有
node/jsdom 那一层，`test_pulse.py` 也是这么写的）。每条断言都在线上旧代码上会红——
本轮逐条核过。
"""
import os
import re
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)


def _read(*parts):
    with open(os.path.join(ROOT, *parts), encoding="utf-8") as handle:
        return handle.read()


def _strip_comments(text):
    """去掉 `//` 与 `/* */` 注释，只留下会被执行的代码。

    **必须做这一步**：本文件好几条断言查的是"某个东西有没有出现"，而注释里正好会
    提到它——本轮就踩到了：`goalRow` 里那句"行内不许放 `<details>`"的说明本身让
    `assertNotIn('<details>', …)` 红了。查注释就会把"写清了为什么不能这么做"报成
    "这么做了"。
    """
    text = re.sub(r"/\*.*?\*/", "", text, flags=re.S)
    return re.sub(r"//[^\n]*", "", text)


def _fn_body(text, name):
    """按缩进切出一个函数的函数体（含首行）。找不到返回空串。

    比"从函数名切到下一个函数名"稳：本文件要断言的几个函数后面跟着的未必是函数，
    也可能是注释块或 `};`。
    """
    match = re.search(r"^(  (?:async )?function %s\b[^\n]*\n)" % re.escape(name),
                      text, re.M)
    if not match:
        return ""
    rest = text[match.end():]
    out = [match.group(1)]
    for line in rest.split("\n"):
        if line.strip() and not line.startswith("    "):
            break
        out.append(line)
    return _strip_comments("\n".join(out))


class GoalRowShowsScanTargetsTestCase(unittest.TestCase):
    """第一件事：「目标」那一行要说清**扫了什么**，而不只是一个通用名字。"""

    @classmethod
    def setUpClass(cls):
        cls.chat = _read("web", "chat.js")
        cls.app = _read("web", "app.js")
        cls.css = re.sub(r"/\*.*?\*/", "", _read("web", "style.css"), flags=re.S)
        cls.row = _fn_body(cls.chat, "goalRow")
        cls.rules = {sel.strip(): " ".join(body.split())
                     for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", cls.css)}

    def test_the_host_collapse_needs_the_host_to_be_a_target_itself(self):
        """折叠地址的**安全条件**：只有"主机本身也在清单里"才允许合并。

        真库里 12 个目标里 10 个是 `182.117.165.148:<端口>`，不压就要占三行。
        但只按最后一个冒号切是错的：IPv6（`[::1]:80`）、带端口的 URL、
        以及 `a.example:443` 与 `a.example` 本不相干的巧合都会被切错，
        而切错的症状是**某个目标从清单里消失了**——不报错，只是少一个。
        所以合并前必须确认 `h` 自己是这一批里的一个目标。
        """
        body = _fn_body(self.chat, "goalScanGroups")
        self.assertTrue(body, "找不到 goalScanGroups")
        self.assertIn("lastIndexOf(':')", body, "取主机的方式变了（最后一个冒号）")
        self.assertIn("scans.indexOf(host) >= 0", body,
                      "没有确认主机本身也是目标就合并了：切错时目标会静默消失")
        self.assertIn("个端口", body, "合并后的词条没有说明端口数")

    def test_the_counts_line_can_be_one_line(self):
        """计数行的字符预算：**一行只有 21 个字符**（真浏览器量过）。

        折成两行会把行高从 91px 顶到 125px，而这一块的正文在最常见的窗口高度下
        只有 100px——最后一行被拦腰截断，且截断处落在字中间。
        丢掉的数字没有丢，都在 `pick.title` 里。
        """
        body = _fn_body(self.chat, "goalRow")
        self.assertIn("pick.append(el('span', 'chat-goal-meta'", body)
        # 这一行只许有三个读数；四个就会折行（数过：28 字符仍然折）。
        self.assertNotIn("`节点 ${nodes}`", body,
                         "又把总量塞回可见那一行了：会折行并把最后一行挤出可视区")
        # 拿掉的数字必须还在 title 里：完整四项 + 总量。丢了才是回归。
        self.assertIn("['intent', 'fact', 'finding', 'asset']", body,
                      "完整四项计数没有进 title：这一项丢了")
        self.assertIn("节点 ${nodes}", body, "总量没有进 title")

    def test_the_row_renders_the_concrete_targets_it_scanned(self):
        self.assertTrue(self.row, "找不到 goalRow（被改名了？）")
        self.assertIn("goalScanTargets(", self.row,
                      "目标行没有取具体扫描目标：这一栏里每个目标都会叫「渗透目标」")
        self.assertIn("'chat-goal-scan'", self.row, "缺少扫描目标那一行的容器")
        self.assertIn("chat-goal-scan-list", self.row, "缺少具体扫描目标的清单行")

    def test_the_address_list_is_gated_on_the_selected_row(self):
        """**地址清单只画在选中那一行上**——这是量出来的，不是省事。

        这一块的高度上限是整栏的 26%。真浏览器里量过（1120×860，栏高 620px）：
        正文只有 124px，而一行目标带完整四行是 149px，一个都装不下，会被拦腰截断，
        而截断处正好在第 4 行——看起来像"这行本来就这么长"，不会像缺陷。
        所以每行都给"扫了多少"（`chat-goal-scan` 无条件 append），
        而"扫的是哪些地址"只给选中那个；其余目标的完整清单在 `pick.title` 里。
        谁把这一行改回无条件渲染，就得同时把这一块的高度上限一起改——所以这里红。
        """
        self.assertRegex(self.row, r"if \(on &&[^\n]*scans\.length\)",
                         "地址清单变成无条件渲染了：会把选中行挤出可视区")
        self.assertIn("pick.title", self.row,
                      "非选中行没有拿到完整清单的出口（title）")
        # "扫了多少"必须每行都有：它在 if 之外。
        head, _, tail = self.row.partition("if (on &&")
        self.assertIn("pick.append(scanLine)", head,
                      "「扫描目标 N 个」被一起挪进选中分支了："
                      "那样非选中行就完全看不出扫过什么")

    def test_scan_targets_come_from_the_console_snapshot_not_a_second_request(self):
        """数据必须来自 `PentestUI.state.jobs`（与「任务」页同一份快照）。

        另写一次 `/jobs` 请求的失败形态是：目标行与任务块在某一次刷新之后显示
        不同的任务数，而两处看起来都正常。
        """
        body = _fn_body(self.chat, "goalScanTargets") + _fn_body(self.chat, "goalJobs")
        self.assertTrue(body, "找不到 goalJobs / goalScanTargets")
        self.assertIn("ui().state", body, "没有从控制台那份快照取任务")
        self.assertIn("engagementId", body, "没有按目标过滤任务")
        self.assertNotIn("apiGet", body, "目标行自己发了一次请求——两处快照会分叉")

    def test_the_target_list_is_deduplicated(self):
        body = _fn_body(self.chat, "goalScanTargets")
        self.assertIn("indexOf(target) < 0", body,
                      "没有去重：同一个地址扫 20 次就会在行里出现 20 遍")

    def test_truncation_is_disclosed_and_the_full_list_stays_reachable(self):
        """封顶必须说出来，而且封掉的那些要能拿到。"""
        self.assertIn("GOAL_SCAN_SHOWN", self.chat, "没有封顶常量")
        self.assertIn("另有", self.row, "截断了却不说，读起来像「总共只有这几个」")
        self.assertIn("title = scans.join", self.row,
                      "完整清单没有出口——行内不许放可交互元素，只能靠 title")

    def test_no_scan_targets_is_never_claimed_before_jobs_load(self):
        """`jobsLoaded` 是必须看的：没拉到任务时写「还没有扫描目标」是假话。"""
        self.assertIn("jobsLoaded", self.row,
                      "目标行没看 jobsLoaded——加载中会被写成「还没有扫描目标」")
        self.assertIn("正在加载", self.row, "缺少加载态文案")

    def test_goal_row_has_no_interactive_child(self):
        """`.chat-goal-pick` 是一颗 `<button>`，里面再放可交互元素是非法结构。

        这条不是洁癖：`<button>` 里嵌 `<button>` 时浏览器把内层甩到外面（文件里
        已经为删除按钮记过这件事），而 `<details>` 的内建摘要同理——它会让
        "点这一行"变成"展开那个东西"，也就是丢了切换目标这个唯一动作。
        """
        self.assertNotIn("<details>", self.row)
        self.assertNotIn("createElement('details')", self.row)
        # 行里只许造两颗按钮：pick（选中范围）与 del（删除）。
        made = re.findall(r"el\('button'", self.row)
        self.assertEqual(len(made), 2,
                         "目标行里出现了第三颗按钮：它落在 pick 里就是非法结构，"
                         "落在外面就是与删除按钮抢位置")

    def test_per_kind_counts_reuse_the_console_word_table(self):
        """逐类计数（意图/事实/漏洞/资产）而不是四项之和。

        「47 节点」分不出这 47 个里有没有漏洞。而词表**必须**来自控制台：
        在 chat.js 里再写一份 `intent: '意图'` 的失败形态是同一个 kind 在两处
        叫两个名字。
        """
        self.assertIn("ui().kindLabel", self.row,
                      "目标行自己造了词表——两处会用两套名字")
        self.assertIn("kindLabel", self.app, "app.js 没有把词表交出去")
        for kind in ("intent", "fact", "finding", "asset"):
            for quoted in ("'%s': '意图'" % kind, "'%s': '事实'" % kind,
                           "'%s': '漏洞'" % kind, "'%s': '资产'" % kind):
                self.assertNotIn(quoted, self.chat,
                                 "chat.js 里又出现了一份节点类型词表：%s" % quoted)

    def test_goal_counts_do_not_invent_a_zero_conversation_count(self):
        """`对话 0` 与 `对话 —` 不是同一件事：列表还没拉回来时是"不知道"。"""
        self.assertIn("view.loaded", self.row,
                      "目标行在列表没拉回来时会把对话数写成 0")

    def test_task_changes_repaint_the_goal_row(self):
        """任务一变，目标行上那两个数（扫描目标 N 个 · 任务 M 条）就得跟着变。

        断言的是**行为挂在谁身上**而不是写在哪一行：重画目标行可以在
        `renderSideJobs` 里，也可以在它包着的 `paintSideJobs` 里——重要的是那一句
        必须真的在任务块重画这条路径上，并且不能落在早退之后（空列表同样要重画）。
        """
        jobs = _fn_body(self.chat, "renderSideJobs")
        self.assertTrue(jobs, "找不到 renderSideJobs")
        paint = _fn_body(self.chat, "paintSideJobs")
        self.assertTrue(paint, "找不到 paintSideJobs")
        self.assertIn("paintSideJobs(", jobs,
                      "renderSideJobs 不再走到真正画任务块的那个函数")
        self.assertIn("renderGoals()", paint,
                      "任务块重画时没有连带重画目标行："
                      "任务列表清空之后目标行会永远停在旧数字上")

    def test_every_new_part_has_a_style_rule(self):
        for cls_name in (".chat-goal-head", ".chat-goal-id", ".chat-goal-scan",
                         ".chat-goal-scan-list", ".chat-goal-auth"):
            self.assertIn(cls_name, self.rules, "缺少样式规则：%s" % cls_name)


class SessionListNeverClaimsEmptyTestCase(unittest.TestCase):
    """第二件事：**对话记录不许自己消失**，也不许把"没拉到"说成"没有"。"""

    @classmethod
    def setUpClass(cls):
        cls.chat = _read("web", "chat.js")
        cls.css = re.sub(r"/\*.*?\*/", "", _read("web", "style.css"), flags=re.S)
        cls.paint = _fn_body(cls.chat, "paintSessions")
        cls.load = _fn_body(cls.chat, "loadSessions")
        cls.rules = {sel.strip(): " ".join(body.split())
                     for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", cls.css)}

    def test_a_failed_load_does_not_wipe_the_known_list(self):
        """这条是本轮报上来的那个现象的直接成因，一个字都不能退回去。"""
        self.assertTrue(self.load, "找不到 loadSessions")
        self.assertNotIn("view.sessions = []", self.load,
                         "加载失败又清空了列表：一次抖动就会让这一块改说"
                         "「还没有对话」——一句关于使用者数据的假话")
        self.assertIn("view.sessionsError", self.load, "失败没有被记下来")

    def test_failure_is_shown_as_failure_and_not_as_empty(self):
        """失败态要有**自己那句正文**，而且不能复用空态那句。

        只断言"函数里出现过『加载失败』"是不够的——栏头那一行也写着它，于是把失败
        正文换成「还没有对话。在右边提问即会新建一条。」之后，断言照样过，而屏幕上
        显示的正是我们正在修的那句假话。这一条是本轮变异检验抓出来的（M9）。
        """
        self.assertIn("view.sessionsError", self.paint, "面板没有区分失败与为空")
        fail_text = re.search(r"'对话列表加载失败[^']*'", self.paint)
        self.assertIsNotNone(fail_text, "失败态没有自己的正文")
        empty_text = re.search(r"'还没有对话[^']*'", self.paint)
        self.assertIsNotNone(empty_text, "空态那句话不见了")
        self.assertNotEqual(fail_text.group(0), empty_text.group(0),
                            "失败态复用了空态那句话：屏幕上看就是「还没有对话」")

    def test_never_loaded_is_not_rendered_as_empty(self):
        """`loaded` 与"列表为空"必须分开——一个该等，一个该去提问。"""
        self.assertIn("view.loaded", self.paint, "面板没看 loaded")
        self.assertIn("正在加载对话列表", self.paint, "缺少加载态文案")
        self.assertNotEqual(
            re.search(r"'正在加载对话列表[^']*'", self.paint).group(0),
            re.search(r"'还没有对话[^']*'", self.paint).group(0),
            "加载态与空态用了同一句话：这两种情况的下一步动作是相反的")

    def test_the_failure_line_has_a_retry(self):
        """不给重试入口的话，唯一出路是刷新整个页面。"""
        self.assertIn("chat-sessions-retry", self.paint, "失败态没有重试按钮")
        self.assertIn("loadSessions(true)", self.paint, "重试没有强制重拉")
        retry = _fn_body(self.chat, "paintSessions")
        self.assertIn("addEventListener", retry, "重试按钮没有挂事件")

    def test_the_head_hint_carries_the_count(self):
        """栏头是这一块**折起来之后唯一的读数**（与子智能体那一块同一条理由）。"""
        self.assertIn("setHint(dom.sessionsHint", self.paint, "栏头没有读数")
        self.assertIn("条'", self.paint, "栏头没有带上条数")

    def test_other_goals_conversations_are_listed_not_summarized(self):
        """**全部对话都列出来**，按目标分组（m03903）。

        此前这里只画当前目标那一批，其余用「另有 N 条属于其他目标」一行概括。于是
        "我到底有哪些对话"得逐个点目标去拼，而"看不见"与"被删掉"在屏幕上没有区别。
        这条守的是分组本身：所有会话都必须真的走到 `append`，而不是被一句话吃掉。
        """
        self.assertIn("sessionGroups()", self.paint, "没有按目标分组")
        self.assertIn("sessionGroupRow(group", self.paint, "组标题没有画出来")
        self.assertIn("group.sessions.forEach", self.paint,
                      "分组之后没有把组里的每条对话都 append 出来")
        self.assertIn("orphans.forEach", self.paint, "未归属那一组没有画")
        self.assertNotIn("另有", self.paint,
                         "又用「另有 N 条」把其他目标的对话藏起来了")

    def test_orphans_are_collected_once_and_never_listed_twice(self):
        """未归属的对话只进 `orphans` 这一条路，所以不会画两遍。

        `eid === ''`（删掉当前目标之后）与"有归属"两种情况下都只得一份：分组发生在
        `sessionGroups()` 里，归属为空的进 `orphans` 并 `return`，不会同时留在某个组里。
        可达路径：删掉当前选中的目标（`deleteGoal` 把 `eid` 清空）之后重画。
        """
        groups = _fn_body(self.chat, "sessionGroups")
        self.assertTrue(groups, "找不到 sessionGroups")
        self.assertIn("if (!owner) { orphans.push(session); return; }", groups,
                      "归属为空与有归属没有分开：同一批对话会被画两遍")
        self.assertIn("{ groups, orphans }", self.paint, "面板没接住分组结果")

    def test_signout_clears_the_previous_users_failure(self):
        out = _fn_body(self.chat, "onSignedOut")
        self.assertTrue(out, "找不到 onSignedOut")
        self.assertIn("view.sessionsError = null", out,
                      "上一个用户那次请求的失败留给了下一个人")

    def test_the_inflight_guard_only_clears_its_own_request(self):
        """无条件清在途标记会让后一次请求被第三个调用者重复发起。"""
        self.assertIn("view.sessionsInflight === inflight", self.load,
                      "在途标记被无条件清掉：三条并发响应谁后到谁说了算")

    def test_the_failure_style_differs_from_the_empty_style(self):
        self.assertIn(".chat-sessions-fail", self.rules, "缺少失败态样式")
        rule = self.rules[".chat-sessions-fail"]
        self.assertIn("border-left", rule,
                      "失败态与空态长得一样：读者分不出「确实没有」与「这次没拉到」")


class FourBlockLinkageTestCase(unittest.TestCase):
    """四块联动（m03903）：目标、对话、任务、子智能体共用**一个**范围。

    报上来的原话是「所有的对话全部显示，点击对话或者目标进行目标、对话、任务、
    子智能体的选项联动」。这里的几条各挡一种"看起来正常、实际各走各的"：

    1. 点一个目标 → 另外三块必须重画。只调 `selectEngagement` 的话，漏洞页换了、
       任务与子智能体还停在旧目标上，而屏幕上没有任何地方说它们不同步；
    2. 打开会话也是**点了目标**的一种（会话必然属于某个目标）：采纳之后同样要重画；
    3. 「任务」块与「子智能体」块**只列这条对话的**（m14866 撤掉了三档范围闸门）：
       范围由主区开着哪条对话唯一决定，没有手工开关，也不借 `state.eid`——
       那个字段同时决定漏洞 / 资产 / 报告的范围；
    4. 撤掉范围之后仍然只许有**一条**渲染路径：两块各自从本会话已有的快照里筛，
       不另发请求，也不再出现"正在取 / 取派活失败 / 本目标还没有对话"这些只因为
       "在读另一条对话的数据"才存在的状态。
    """

    @classmethod
    def setUpClass(cls):
        cls.chat = _read("web", "chat.js")
        cls.css = re.sub(r"/\*.*?\*/", "", _read("web", "style.css"), flags=re.S)
        cls.rules = {sel.strip(): " ".join(body.split())
                     for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", cls.css)}

    def test_clicking_a_goal_repaints_the_other_three_blocks(self):
        """目标行点击：另外三块必须跟上，任务块的"看全部"要收回。"""
        goal = _fn_body(self.chat, "goalRow")
        self.assertTrue(goal, "找不到 goalRow")
        self.assertIn("renderSideJobs()", goal,
                      "点了目标，任务块还停在旧目标上")
        self.assertIn("renderSideSubagents()", goal,
                      "点了目标，子智能体还是上一场对话的")
        # 范围只有一档（m14866），换目标时不需要再收什么手工档；反过来，
        # 谁把那颗开关写回来，这里就红。
        self.assertNotIn("sideJobsScope", goal,
                         "任务块的范围开关又回来了：它已经只剩「这条对话」一档")

    def test_adopting_a_sessions_goal_repaints_them_too(self):
        """打开／自动采纳一条对话的目标，等于点了一次目标——同样三块跟上。"""
        body = _fn_body(self.chat, "adoptSessionEngagement")
        self.assertTrue(body, "找不到 adoptSessionEngagement")
        self.assertIn("selectEngagement(", body)
        self.assertIn("renderSessions()", body)
        self.assertIn("renderSideJobs()", body,
                      "采纳了目标，任务块没跟上")
        self.assertIn("renderSideSubagents()", body,
                      "采纳了目标，子智能体块没跟上")

    def test_the_session_list_refresh_also_refreshes_the_subagent_block(self):
        """会话列表拉回来之后，"目标名下最近一条对话"可能换了。"""
        load = _fn_body(self.chat, "loadSessions")
        self.assertTrue(load, "找不到 loadSessions")
        self.assertIn("renderSideSubagents()", load,
                      "会话列表变了、子智能体块没重取：会一直显示上一轮那一条的派活")

    def test_a_group_header_switches_the_whole_scope(self):
        """组标题是"从对话切到目标"的入口：四块一起走。"""
        row = _fn_body(self.chat, "sessionGroupRow")
        self.assertTrue(row, "找不到 sessionGroupRow")
        self.assertIn("el('button'", row,
                      "组标题不是按钮：点它切范围就成了鼠标专属")
        self.assertIn("chooseGoal(group.eid)", row, "组标题没有接上切范围")
        self.assertIn("if (!meta) return row;", row,
                      "目标已删时仍做成可点：选一个不存在的 id 会让三块面板各报一次 404")
        pick = _fn_body(self.chat, "chooseGoal")
        self.assertTrue(pick, "找不到 chooseGoal")
        for call in ("selectEngagement(", "renderSessions()",
                     "renderSideJobs()", "renderSideSubagents()"):
            self.assertIn(call, pick, "点组标题之后 %s 没有跟上" % call)
        self.assertNotIn("sideJobsScope", pick,
                         "换范围时还在收手工档：那一档已经不存在了")

    def test_switching_the_goal_also_switches_the_open_conversation(self):
        """目标与对话必须**双向**对应：点目标 = 换到它名下的对话（m00482）。

        只做"换对话 → 范围跟过去"这一个方向（`adoptSessionEngagement`）时，范围停在
        甲、打开着的还是乙的对话：「目标」块高亮甲，「对话记录」里蓝底那条属于乙——
        同一屏上两个"当前"，而右边那份数据说的是另一台机器。三个换范围的入口
        （目标行 / 对话组头 / 任务行）都要走上这条对齐，漏一个就从一个口子回来。
        """
        for name in ("goalRow", "chooseGoal", "pickJob"):
            body = _fn_body(self.chat, name)
            self.assertTrue(body, "找不到 %s" % name)
            self.assertIn("alignSessionToGoal(", body,
                          "%s 换了范围，却没把打开着的对话换过去" % name)
        body = _fn_body(self.chat, "alignSessionToGoal")
        self.assertTrue(body, "找不到 alignSessionToGoal")
        for marker in ("view.pendingGoalSwitch", "view.sending",
                       "openSession(", "newSession()", "engagementId"):
            self.assertIn(marker, body, "对齐少了一步：%s" % marker)
        # 守卫必须只挡"有 id 但列表里没有"：「新对话」是 id 为空的草稿，换目标时
        # 照样要能跟着走（否则空目标下起了新对话、再点回有对话的目标就停在空白里）。
        self.assertIn("if (view.sessionId && !open) return;", body,
                      "列表里还没有这一条时就下判断：猜错一次会把刚发出去的那一轮清掉")
        self.assertIn("if (!view.loaded) return;", body,
                      "列表还没到就拿「这个目标名下没有对话」下结论")
        self.assertIn("if (open) newSession();", body,
                      "草稿再清一遍会把刚排上的东西收掉")

    def test_a_switch_blocked_by_a_running_turn_lands_at_finish(self):
        """一轮在跑时**不硬切**：那一轮的流会把正文逐条写进当前这个视图。

        所以对齐只记一个待办（`view.pendingGoalSwitch`），由 `finish()` 收尾时补上；
        收尾若又把队列里的下一条接了上来（`setSending(true)`），还得把它留着，否则
        那一次切换就永远丢了。
        """
        self.assertIn("pendingGoalSwitch: ''", self.chat, "没有待办字段")
        body = _fn_body(self.chat, "finish")
        self.assertTrue(body, "找不到 finish")
        self.assertIn("alignSessionToGoal(pending)", body)
        self.assertIn("else if (pending) view.pendingGoalSwitch = pending;", body,
                      "收尾时又把下一条接了上来，待办被清掉就再也不会补")
        self.assertLess(body.index("await openSession(sessionId)"),
                        body.index("alignSessionToGoal(pending)"),
                        "待办必须在 `openSession` 之后补：先补会被它覆盖")

    def test_the_job_block_lists_only_this_conversation(self):
        """任务块只有一档范围：**这条对话派下去的那些**（m14866）。

        原先它是三档闸门（这条对话 → 本目标 → 全部目标，m15165），每一档还各配一颗
        往回按的按钮。使用者的判断是「只显示当前会话的内容，取消看全部」：一块会
        自己改范围的列表，读之前永远要先确认"这一屏是哪一批"，而它回答的问题只有
        一个——这条对话还派着几件活。所以这里正向钉住「按 `sessionId` 筛、一条都
        不截」，反向钉住「那三档连同开关都不许回来」。
        """
        body = _fn_body(self.chat, "paintSideJobs")
        self.assertTrue(body, "找不到 paintSideJobs")
        self.assertIn("sessionJobs(jobView)", body, "任务块没走按对话筛的那条路")
        self.assertNotIn("jobScope(", body, "三档范围闸门又回来了")
        self.assertNotIn("appendJobsScopeToggle(", body, "范围开关又回来了")
        self.assertNotIn("slice(", body,
                         "任务块又在截断了：这一块要的是全部，多的那几条正是最新的")
        self.assertNotIn("switchTab(", body,
                         "「查看全部」那颗按钮还在：这一块本来就不截断，它会让人以为不全")
        # 反面：不许借 `state.eid` 做范围——它同时决定漏洞/资产/报告的范围
        self.assertNotIn("state.eid =", body)
        scope = _fn_body(self.chat, "sessionJobs")
        self.assertTrue(scope, "找不到 sessionJobs")
        self.assertIn("view.sessionId", scope, "没看开着的是哪一条对话")
        self.assertIn("String(job.sessionId || '') === id", scope,
                      "没有按对话过滤任务：别的对话的任务会混进来")
        self.assertIn("jobView.compare", scope,
                      "没沿用任务页签那套排序：执行中的不再排在最前")
        self.assertNotIn("view.sessions", scope,
                         "还在从会话列表里挑「同目标最近一条」：那是跟目标走的旧判据")
        self.assertNotIn("selectedEid()", scope, "任务块又在看选中的目标了")
        self.assertNotIn("state.eid =", scope)

    def test_a_session_switch_repaints_both_blocks(self):
        """换对话 / 新建对话都要**重画**这两块。

        范围只剩「这条对话」一档之后，"收回手工档"这件事自然消失了（没有档可收），
        但**重画**这件事反而更要紧：这一块的内容现在完全由 `view.sessionId` 决定。"""
        # 重画：`adoptSessionEngagement` 在"这条对话的目标就是当前目标"时第一句
        # 就 return 了，而那正是最常见的情形——只靠它，切到同一目标下的另一条
        # 对话时任务块会停在上一条对话的任务上，而且看起来完全正常。
        for name in ("openSession", "newSession"):
            body = _fn_body(self.chat, name)
            self.assertTrue(body, "找不到 %s" % name)
            self.assertIn("renderSideJobs()", body,
                          "%s 之后任务块没重画：会停在上一对话的任务上" % name)
            self.assertIn("renderSideSubagents()", body,
                          "%s 之后子智能体块没重画" % name)
        body = _fn_body(self.chat, "openSession")
        self.assertNotIn("sideJobsScope", body,
                         "换对话时还在收手工档：那一档已经不存在了")
    def test_clicking_a_job_aligns_the_scope_first(self):
        """点一条属于别的目标的任务：先把范围对齐过去，再开详情。"""
        row = _fn_body(self.chat, "sideJobRow")
        self.assertIn("pickJob(job)", row)
        pick = _fn_body(self.chat, "pickJob")
        self.assertTrue(pick, "找不到 pickJob")
        self.assertIn("selectEngagement(", pick)
        self.assertIn("openDetail('job'", pick)
        self.assertIn("engagementList", pick,
                      "没判断目标是否还在：已删目标下会选一个不存在的 id")

    def test_the_subagent_block_shows_this_conversation_only(self):
        """子智能体块也只剩一档：**主区这一屏的派活**（m14866）。

        它原先是跟选中目标走的：主区开着的那条对话不属于选中目标时，就去拉「这个
        目标名下最近一条对话」，再在正文里标一行来源、并把失败留痕。那条路整条
        没了——少掉的不只是一次网络请求与两层缓存，还有一整类状态：这一块可能停在
        「正在取…」、停在「取派活失败」、停在「本目标还没有对话」，而使用者要看的
        只是「刚才那几个兄弟走到哪了」。
        """
        scope = _fn_body(self.chat, "subagentScope")
        self.assertTrue(scope, "找不到 subagentScope")
        self.assertIn("currentSubagentRows()", scope,
                      "没从本会话的消息里取派活")
        for gone in ("selectedEid()", "/chat/sessions/", "goalSubagentCache",
                     "goalSubagentWaiting", "engagementId"):
            self.assertNotIn(gone, scope,
                             "子智能体块还在走跟目标那条旧路：%s" % gone)
        paint = _fn_body(self.chat, "renderSideSubagents")
        self.assertTrue(paint, "找不到 renderSideSubagents")
        for gone in ("subagentSourceRow", "scope.error", "取派活失败", "waiting"):
            self.assertNotIn(gone, paint,
                             "失败 / 在途 / 来源那几档又回来了：%s" % gone)
        # 只有一条渲染路径这条性质不受范围改动影响：两块仍然共用 subagentBlock()。
        self.assertIn("subagentBlock(rows)", paint)
        self.assertIn("wireSubagentPicks(block, rows)", paint)

    def test_every_new_part_has_a_style_rule(self):
        for cls_name in (".chat-sessions-goal", ".chat-sessions-goal-name",
                         ".chat-sessions-goal-count", ".chat-sessions-goal-tag"):
            self.assertIn(cls_name, self.rules, "缺少样式规则：%s" % cls_name)


class OrderIsWhatTheBackendGaveTestCase(unittest.TestCase):
    """顺序**照后端给的画**，点谁都不改变谁的位置（m14866 撤销 m15733）。

    m15733 曾经让「点一下，那一行就挪到最前」。使用者的判断是取消：目标与对话是
    **两列要反复对照的清单**，位置每点一次就变，读到第三行之后手上的坐标全作废了；
    而"现在是哪一项"这件事本来就由高亮 +「当前」标出来，不需要靠位移再表达一遍。

    所以这里反向钉住两件事：排序函数不许回来、组内排序不再看 `view.sessionId`。
    同时正向钉住那条**与顺序无关但一起改过**的性质：`#chat-goals` 自己就是滚动区
    （它是 `.panel-body`），心跳每 3 秒整体重画，不保住 `scrollTop` 就把正在往下
    看的人顶回顶部。
    """

    @classmethod
    def setUpClass(cls):
        cls.chat = _read("web", "chat.js")
        cls.goals = _fn_body(cls.chat, "renderGoals")
        cls.groups = _fn_body(cls.chat, "sessionGroups")

    def test_the_clicked_goal_is_not_moved_to_the_top(self):
        self.assertEqual(_fn_body(self.chat, "orderedGoals"), "",
                         "按选中项排序的函数又回来了：点一次位置就变一次")
        self.assertNotIn("orderedGoals(", self.goals,
                         "目标行又在按选中项排前")
        self.assertIn("view.goals.forEach(", self.goals,
                      "目标行没有照 `view.goals` 给的顺序画")

    def test_the_conversation_group_ignores_which_one_is_open(self):
        self.assertTrue(self.groups, "找不到 sessionGroups")
        self.assertNotIn("view.sessionId", self.groups,
                         "组内排序又在看打开着的是哪一条")
        self.assertIn("localeCompare", self.groups,
                      "组内不再按更新时间倒序了")

    def test_the_group_order_ignores_the_selected_goal(self):
        """组序只看组内最近一条对话的时间（m17536）。

        点一条对话会把 `state.eid` 换成它归属的目标；组序若再看选中目标，表现就是
        "点谁、谁那一组跳到最顶"，也就是用户报的「会话记录还是会置顶」。谁是当前
        那一组由 `sessionGroupRow()` 的「当前」标签说，靠位置说会一碰就变。
        """
        self.assertNotIn("selectedEid()", self.groups,
                         "组序又在看当前选中的目标：点一条对话它就往上窜")
        self.assertNotIn("unshift", self.groups,
                         "又把某一组提到最前了")
        self.assertIn("groups.sort(", self.groups,
                      "组序不再按时间排了")

    def test_a_repaint_keeps_the_goal_scroll_position(self):
        """`#chat-goals` 自己就是滚动区（它是 `.panel-body`），清空会把 `scrollTop` 归零。"""
        self.assertIn("dom.goals.scrollTop", self.goals,
                      "重画把滚到一半的人顶回了顶部")


class TraceRowJumpsToSidebarTestCase(unittest.TestCase):
    """会话里一行工具轨迹点下去，右栏要换成那一条任务（m14866）。

    这一下的失败形态全都**长得像正常**：点了没反应（右栏那块折着）、右栏高亮换
    了但列表还在原处（滚动区外面）、或者跳到了另一条任务上（详情写着别人的名字）。
    所以这里分开钉四件事：认出是哪一条、三个动作都做了、主区那行与右栏同步、
    以及样式表真的给可点与选中画了规则。
    """

    @classmethod
    def setUpClass(cls):
        cls.chat = _read("web", "chat.js")
        cls.app = _read("web", "app.js")
        cls.css = re.sub(r"/\*.*?\*/", "", _read("web", "style.css"), flags=re.S)
        cls.pick = _fn_body(cls.chat, "traceJobId")
        cls.focus = _fn_body(cls.chat, "focusJob")
        cls.block = _fn_body(cls.chat, "traceBlock")
        cls.marks = _fn_body(cls.chat, "markDetailPicks")

    def test_a_row_knows_which_job_it_is_about(self):
        """`data` 是**截断后的 JSON 字符串**（后端 `_clip(_dump(...))`），要 parse。"""
        self.assertTrue(self.pick, "找不到 traceJobId")
        self.assertIn("JSON.parse", self.pick,
                      "后端把 data 落成字符串，不 parse 就取不到 jobId")
        self.assertIn("item.data", self.pick, "没有读这次调用下发了什么")
        self.assertIn("data.jobId", self.pick, "没有认 jobId 这个字段")

    def test_the_three_things_a_click_must_do(self):
        self.assertTrue(self.focus, "找不到 focusJob")
        self.assertIn("PentestFold.set('jobs', false)", self.focus,
                      "右栏那块折着时，点了什么也看不见")
        self.assertIn("pickJob(", self.focus, "没有把右栏换成这一条")
        self.assertIn("scrollIntoView", self.focus,
                      "高亮落在了滚动区外面，等于没跳")
        self.assertIn("openDetail('job'", self.focus,
                      "快照里没有这条任务时什么也不做，看起来就是点了没反应")

    def test_a_row_without_a_job_stays_plain(self):
        self.assertTrue(self.block, "找不到 traceBlock")
        self.assertIn("traceJobId(item)", self.block, "轨迹行没有去认任务")
        self.assertIn("dataset.jobId", self.block, "行上没有记下是哪一条")
        self.assertIn("is-pickable", self.block, "可点这件事没有落到类名上")
        # 撤回折叠块那一下不能连详情栏一起换：点「原始返回」不该跳走。
        self.assertIn("closest('details')", self.block,
                      "点开原始返回的同时把详情栏换掉了")

    def test_the_main_column_and_the_right_column_agree(self):
        self.assertTrue(self.marks, "找不到 markDetailPicks")
        self.assertIn(".chat-trace-item[data-job-id]", self.marks,
                      "主区的轨迹行不跟着右栏高亮，两处会各指一条")

    def test_both_entries_are_exported(self):
        """两个入口都要露出来：`focusJob` 给测试与后续调用点，`loadJobs` 给那次补拉。"""
        self.assertIn("focusJob }", self.chat, "focusJob 没有对外暴露")
        self.assertIn("  loadJobs,", self.app,
                      "补拉任务列表的入口没有给出去，focusJob 里那次补拉是空的")

    def test_the_two_new_states_have_rules(self):
        self.assertIn(".chat-trace-item.is-pickable", self.css,
                      "可点的轨迹行没有手型光标")
        self.assertIn(".chat-trace-item.is-picked", self.css,
                      "选中的轨迹行与别的行看不出区别")


class JobAndSubagentAreLinkedTestCase(unittest.TestCase):
    """任务与子智能体互相跳得过去（m17670）。

    用户原话：「任务和子智能体没有联动」。缺的是同一根线：子智能体的工具记录里
    没有它开出来的任务号，任务那边也没有派出者。两头都得补上，而且**不许给
    `jobs` 表加列**——那条线本来就存在（派活工具的返回值里就带着 jobId），只是
    没有一路传到界面上。

    所以这里分四段钉：后端把 jobId 抄进工具记录、落库那一份也带、前端把这一帧
    接下来的 jobId 收进工具对象、以及两个方向各自画出了一行可点的东西。
    """

    @classmethod
    def setUpClass(cls):
        cls.chat = _read("web", "chat.js")
        cls.css = re.sub(r"/\*.*?\*/", "", _read("web", "style.css"), flags=re.S)
        cls.issuer = _fn_body(cls.chat, "jobIssuer")
        cls.link = _fn_body(cls.chat, "jobLink")
        cls.node = _fn_body(cls.chat, "subagentNode")
        cls.sub_detail = _fn_body(cls.chat, "renderSubagentDetail")
        cls.job_detail = _fn_body(cls.chat, "renderJobDetail")
        cls.apply = _fn_body(cls.chat, "applySubagent")

    def test_the_live_frame_keeps_the_job_id(self):
        """`subagent.py` 的 tool 帧带 jobId，`applySubagent` 要把它收进工具对象。"""
        self.assertTrue(self.apply, "找不到 applySubagent")
        self.assertIn("data.jobId", self.apply, "工具帧里的 jobId 被丢掉了")
        self.assertIn("tool.jobId", self.apply, "没有落到工具对象上")

    def test_the_issuer_is_computed_not_stored(self):
        """反查：扫这一批子智能体的工具记录，谁的 jobId 对上就是谁派的。

        不落库是刻意的——`jobs` 表里没有下发者那一列，为显示加一列要在迁移、
        `create_job` 载荷、以及每一个派活点各改一遍，而这条线本来就是现成的。
        """
        self.assertTrue(self.issuer, "找不到 jobIssuer")
        self.assertIn("currentSubagentRows()", self.issuer, "没有去看这一批子智能体")
        self.assertIn("tools", self.issuer, "没有扫工具记录")
        self.assertIn("jobId", self.issuer, "没有认 jobId")
        self.assertIn("return null", self.issuer, "找不到时应当安静地什么都不给")

    def test_both_directions_have_a_click_target(self):
        """两个方向都要有落点：任务详情里点去子智能体，子智能体里点去任务。"""
        self.assertIn("jobIssuer(", self.job_detail,
                      "任务详情里没有「派出它的子智能体」")
        self.assertIn("openDetail('subagent'", self.job_detail,
                      "那一行点了没有去处")
        self.assertIn("jobLink(", self.sub_detail,
                      "子智能体详情里的工具步骤没有给出它开出的任务")
        # 芯片：有 jobId 才换成按钮。换成 span 就点不动，全都换成按钮则点空。
        self.assertIn("tool.jobId ? 'button' : 'span'", self.node,
                      "工具芯片没有按有没有任务区分")
        self.assertIn("focusJob(tool.jobId)", self.node,
                      "可点的芯片点了不跳")
        self.assertIn("stopPropagation", self.node,
                      "点芯片会顺手把外层那一行也点开")

    def test_the_clickable_chip_has_its_own_rules(self):
        self.assertIn(".chat-live-chip-job", self.css,
                      "可点的芯片没有手型光标与焦点环")


if __name__ == "__main__":
    unittest.main()
