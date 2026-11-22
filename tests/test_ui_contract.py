# -*- coding: utf-8 -*-
"""界面与后端的契约核对（静态检查，不需要浏览器）。

前端与后端是两个并行开发的部分，最容易出现的故障是"接口路径改了、界面还在调旧
地址"或"JS 引用了页面里不存在的 DOM id"——两者都要等到人工点开页面才会暴露。
这组用例把这类问题变成提交即可发现：

  * app.js 里出现的每个 /api/v1 路径，必须能匹配后端路由表；
  * app.js 引用的每个 DOM id，必须存在于 index.html；
  * 界面不得依赖任何外部网络资源（离线内网可用）；
  * 四个视图与关键交互元素必须存在。
"""
import json
import os
import re
import subprocess
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agents, authz, chat                # noqa: E402
from pentestd.server import ControlPlane           # noqa: E402
from pentestd.store import Store                   # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

# 本文件里有几条断言盯的是"默认必须是全部关闭""未开启的类要给出准确的开启方式"。
# 那说的是**闸照判**时的形态，所以这一模块内把 `authz.FORCE_OPEN` 关掉；跑完还原
# ——`unittest discover` 会把全部模块一起 import，把全局状态留下去会污染别的文件。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


def _read(name: str) -> str:
    with open(os.path.join(WEB_DIR, name), "r", encoding="utf-8") as handle:
        return handle.read()


def _scripts() -> list:
    """web/ 下所有前端脚本。

    界面已经拆成多个文件（app/auth/settings/boot），契约检查必须覆盖全部：
    只查主文件，新增的文件就成了检查盲区——而这恰恰是最容易漏的地方。
    """
    names = sorted(name for name in os.listdir(WEB_DIR) if name.endswith(".js"))
    if not names:
        raise AssertionError("web/ 下没有找到任何 js 文件")
    return names


def _strip_js_comments(source: str) -> str:
    """去掉注释，长度与换行保持原样（注释换成等量空格）。

    **扫描选择器之前必须去掉注释**，两个方向都踩过：

      * 注释里提到一个选择器是常事（"这里原本是 `$('#job-log')`，已经改成委托"），
        把它当代码读，守卫就会去报一段**已经修好**的代码——而守卫报错在正确代码
        上，比漏报更糟，因为处理它最省事的办法是把守卫删掉；
      * 反过来，上一轮的 `test_overflow_hidden_containers_are_known` 是"在规则
        上方写一段文档注释就能让它看不见那条规则"。

    `//` 前面是冒号时不当注释（`'http://www.w3.org/2000/svg'` 这种是字符串里的
    斜杠）。这只是个启发式，漏掉的极端写法最多让扫描少看几个字符。
    """
    out = []
    i, size = 0, len(source)
    while i < size:
        char = source[i]
        if char == "/" and i + 1 < size and source[i + 1] == "*":
            end = source.find("*/", i + 2)
            end = size if end < 0 else end + 2
            out.append(re.sub(r"[^\n]", " ", source[i:end]))
            i = end
        elif char == "/" and i + 1 < size and source[i + 1] == "/" and not (
                i > 0 and source[i - 1] == ":"):
            end = source.find("\n", i)
            end = size if end < 0 else end
            out.append(" " * (end - i))
            i = end
        else:
            out.append(char)
            i += 1
    return "".join(out)


def _static_ids(html: str) -> set:
    """index.html 里写死的 id。"""
    return set(re.findall(r'id="([\w-]+)"', html))


def _js_made_ids(js: str) -> set:
    """运行期造出来的 id（`host.id = 'x'` / `setAttribute('id', …)` / `svgEl(…,{id:…})`）。

    只收这三种**明确的建节点**写法，不用泛泛的 `id: '…'`——后者会命中
    `FIELD_LABEL = { id: 'ID' }` 之类的普通字段名，把守卫的允许集越撑越大。
    """
    js = _strip_js_comments(js)
    made = set(re.findall(r"""\.id\s*=\s*['"]([\w-]+)['"]""", js))
    made |= set(re.findall(r"""setAttribute\(\s*['"]id['"]\s*,\s*['"]([\w-]+)['"]""", js))
    made |= set(re.findall(r"""svgEl\([^)]*?id:\s*['"]([\w-]+)['"]""", js))
    return made


def _id_selectors(source: str) -> set:
    """一段源码里用到的 id 选择器，**含 `$('#x')` 这个简写**。

    本仓库取节点基本都用 `$` / `$$` / `byId`（见 app.js 第 49 行那三个简写）。
    守卫若只认 `getElementById` 与 `querySelector('#…')` 的字面写法，简写就是一片
    完全没人看的盲区——而盲区里的东西不会自己出错，它只在出事之后才被发现。

    只看代码，不看注释（见 `_strip_js_comments`）。
    """
    source = _strip_js_comments(source)
    used = set(re.findall(r"""getElementById\(\s*['"]([\w-]+)['"]\s*\)""", source))
    used |= set(re.findall(r"""querySelector(?:All)?\(\s*['"]#([\w-]+)""", source))
    used |= set(re.findall(r"""\$+\('#([\w-]+)""", source))
    used |= set(re.findall(r"""byId\(\s*['"]([\w-]+)['"]\s*\)""", source))
    return used


def _bare_calls(body: str) -> set:
    """函数体里**作为一条独立语句**调用的函数名（`  foo();`）。

    只看这种形态：挂在回调 / then / forEach 里的调用不算——那些是渲染路径，跑到
    它们的时候节点早就存在了。把它们算进来，这条守卫就会去报正确代码，而守卫报
    正确代码的代价是有人把守卫删掉。
    """
    return set(re.findall(r"(?m)^\s*([A-Za-z_$][\w$]*)\([^;]*\);\s*$",
                          _strip_js_comments(body)))


def _js_class_names(js: str) -> set:
    """前端脚本里造出来的类名。

    渲染函数用 `el('div', 'some-class')` 拼类名，这些类在 index.html 里找不到，
    所以判断"某个类选择器是不是死的"时必须把它们算进来——不然
    `.agent-summary` / `.model-name` / `.plan-intent` 这三个真在用的规则会被冤枉。

    **只取 `el(...)` 的第二个参数与 `classList` 那几支**，不取"看起来像类名的
    字符串字面量"。后者会把 `byId('chat-side-subagent-list')` 里的 **id** 也算成
    类名，于是守卫对正主（`.chat-side-subagent-list` 写错了符号）**不报**——
    本节守卫的第一版就是这么写的：把那个错误故意写回去验证时它没有红。
    守卫本身也要被验证过，否则它只是占了十几行。
    """
    names = set()
    for chunk in re.findall(r"""el\(\s*['"][^'"]*['"]\s*,\s*'([^']*)'""", js):
        names.update(chunk.split())
    for chunk in re.findall(r"""classList\.(?:add|remove|toggle)\(\s*'([\w -]+)'""", js):
        names.update(chunk.split())
    for chunk in re.findall(r"""setAttribute\(\s*'class'\s*,\s*'([^']*)'""", js):
        names.update(chunk.split())
    return names


def _normalize(path: str) -> str:
    """把模板占位与路由占位统一成同一个形状，便于比较。"""
    path = path.strip("^$")                       # 路由正则是 ^...$ 形式
    path = re.sub(r"\$\{[^}]+\}", "<var>", path)
    path = re.sub(r"\(\?P<\w+>\[\^/\]\+\)", "<var>", path)
    path = re.sub(r"<var>[^/]*", "<var>", path)
    return path.rstrip("/")


def _api_candidates(js: str) -> set:
    """从全部前端脚本中还原实际会请求的接口路径。

    界面把基址抽成 API_BASE 常量、把两类前缀抽成 engagementPath() / executorPath()
    / reportPath() 函数，因此不能只抓字面量，要把这几处都拼回来：
      * apiGet('...') / apiPost('...') / apiStream('...')  —— 字面量路径
      * engagementPath('/findings')     —— /engagements/{id} 的子资源
      * executorPath(id, '/revoke')     —— /executors/{id} 的子资源
      * reportPath('job-x', 'E-y')      —— /engagements/{id}/report（query 是后缀，
                                            与 `'/graph' + query` 同一个形状）

    `apiStream` 必须一起抓：流式接口同样是一条真实路由，漏掉它等于让"界面调了一个
    后端没有的接口"这类缺陷在流式通道上成为盲区。

    **这份名单要跟着拼路径的函数走。** 多一个拼 `/engagements/...` 的辅助函数而这里
    不知道它，那条路由就从守卫里消失了——而症状是"守卫全绿、界面 404"。所以另有一条
    用例（`test_every_path_helper_is_known_here`）盯着这份名单是不是齐的。
    """
    direct = set(re.findall(r"""api(?:Get|Post|Stream)\(['"`]([^'"`]+)['"`]""", js))
    suffixes = set(re.findall(r"""engagementPath\(['"`]([^'"`]*)['"`]\)""", js))
    executor_suffixes = set(re.findall(
        r"""executorPath\([^,()]+,\s*['"`]([^'"`]*)['"`]\s*\)""", js))
    # 反引号模板里拼出来的路径（如 `/models/${id}/test`）也要抓：
    # 只认引号字面量会让动态路径成为契约检查的盲区
    templated = set(re.findall(r"""api(?:Get|Post|Stream)\(\s*`([^`]+)`""", js))
    candidates = {"/api/v1" + path for path in direct | templated}
    candidates |= {"/api/v1/engagements/<var>" + suffix for suffix in suffixes}
    candidates |= {"/api/v1/executors/<var>" + suffix
                   for suffix in executor_suffixes}
    # `reportPath` 内部拼的是 `/engagements/{eid}/report`（可能再挂一个 `?job=`）。
    # 查询串不算路由的一部分，所以只取到 `/report` 为止。
    if "reportPath" in js:
        candidates.add("/api/v1/engagements/<var>/report")
    # **查询串一律剥掉。** 上面那条注释说的规则其实是全体通用的：路由表里登记的是
    # `/pulse`，而 `'/pulse?sessionId=' + id` 在提取阶段只会取到 `'?'` 之前那一段
    # 字面量。不在这里剥，"界面调了不存在的接口"会报一条**假**的——而它指向的是一个
    # 真缺陷的形状（名字拼错 / 路由没挂），于是下一个人会去查一个不存在的问题，最后
    # 要么改测试、要么把产品代码绕成 `'/pulse' + '?sessionId='` 来讨好提取器。
    # 两者都比在这里剥一次差。
    candidates = {path.split("?", 1)[0] for path in candidates}
    return candidates


# 守卫认识的两类"拼 `/engagements/...` 前缀"的辅助函数。每多一个都要显式加进来
# ——`test_every_path_helper_is_known_here` 盯着这件事。
ENGAGEMENT_PATH_HELPERS = ("engagementPath", "reportPath")


def _router_keys(plane: ControlPlane) -> set:
    return {_normalize(regex.pattern) for _m, regex, _h, _a in plane.router.routes}


class UiContractTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.scripts = _scripts()
        cls.sources = {name: _read(name) for name in cls.scripts}
        # 所有脚本拼起来做内容级检查；单文件检查另有用途
        cls.js = "\n".join(cls.sources[name] for name in cls.scripts)
        cls.html = _read("index.html")
        cls.css = _read("style.css")
        cls.tmp = tempfile.TemporaryDirectory()
        store = Store(os.path.join(cls.tmp.name, "ui.db"))
        cls.plane = ControlPlane(store, WEB_DIR, auth_required=False)
        cls.store = store

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    def test_api_paths_used_by_ui_exist_in_router(self):
        patterns = _router_keys(self.plane)
        used = _api_candidates(self.js)
        self.assertTrue(used, "前端脚本未发现任何接口调用，界面可能没接后端")
        missing = sorted(path for path in used
                         if _normalize(path) not in patterns)
        self.assertEqual(missing, [], "界面调用了后端不存在的接口：%s" % missing)
        # 各视图的数据源都必须在
        normalized = {_normalize(p) for p in used}
        for required in ("/api/v1/health", "/api/v1/engagements",
                         # 链路现在是**全局的**：合并视图走 `/graph`，界面不再按目标
                         # 逐个取图。`/engagements/<var>` 本身仍是合法路由（CLI 与
                         # 脚本在用），但它已经不是界面的数据源——继续写在这里，
                         # 这条断言在界面回退成"按目标取图"时仍会通过。
                         "/api/v1/graph",
                         "/api/v1/engagements/<var>/findings",
                         "/api/v1/engagements/<var>/assets",
                         "/api/v1/engagements/<var>/report",
                         "/api/v1/setup", "/api/v1/executors",
                         "/api/v1/executors/enroll",
                         "/api/v1/models", "/api/v1/users",
                         # 入口层（会话入口三项选择 / 技能 / DSH 集成 / 插件市场）：
                         # 它们是新界面的数据源，少一个对应一整块功能静默变成空壳。
                         # `/hub/ops` 不在此列：界面从 `/hub/installed` 一次拿全
                         # （dsh + ops + 队列积压），那个接口留给脚本与 CLI 用。
                         "/api/v1/session-options", "/api/v1/agents",
                         "/api/v1/policies", "/api/v1/skills", "/api/v1/dsh",
                         "/api/v1/hub/catalog", "/api/v1/hub/installed",
                         "/api/v1/hub/diagnostics",
                         # 第三道闸的状态。缺了它，"现在能不能动手"就只剩启动横幅
                         # 与问模型两条路，而两条都在浏览器之外。
                         "/api/v1/authz"):
            self.assertIn(required, normalized, "界面缺少必要数据源：%s" % required)

    def test_every_path_helper_is_known_here(self):
        """每一处自己拼的 `/engagements/...` 路径都要能被守卫还原出来。

        守卫还原动态路径有两条路：`apiGet(`...`)` 这类**直接当实参**的模板（由
        `templated` 那条正则收走），以及 `engagementPath` / `reportPath` 这两个
        辅助函数（由 `ENGAGEMENT_PATH_HELPERS` 声明）。这条用例数的是**第三种**：
        既不在 `api*` 的实参位上、又不在任何已登记的辅助函数里——那样的路径守卫
        完全看不见，而症状是这一组用例全绿、界面对着一个不存在的接口发请求。

        判据取"`` `/engagements/ `` 这个前缀出现了几次"，因为凡是拼这条路径的人都
        绕不开它；不认识的函数、内联拼出来的字符串，都会在这里露出来。
        新加函数的回退方式：登记进 `ENGAGEMENT_PATH_HELPERS`，并在 `_api_candidates`
        里把它的路由加进去——两处都改，才说明写的人真的想过那条路径长什么样。
        """
        for name in ENGAGEMENT_PATH_HELPERS:
            self.assertIn(name, self.js, "守卫登记的辅助函数 %s 已不在前端里" % name)
        orphans = []
        for match in re.finditer(r"`/engagements/", self.js):
            head = self.js[max(0, match.start() - 60):match.start()]
            if re.search(r"api(?:Get|Post|Stream)\(\s*$", head):
                continue                      # 直接当 api* 的实参，已由 templated 收走
            window = self.js[:match.start()][-400:]
            if not any(re.search(r"(?:const|function)\s+%s\b" % re.escape(name), window)
                       for name in ENGAGEMENT_PATH_HELPERS):
                orphans.append(self.js[match.start():match.start() + 70].split("\n")[0])
        self.assertEqual(orphans, [],
                         "这些 `/engagements/` 路径守卫看不见（既不在 api* 的实参位、"
                         "也不在任何已登记的辅助函数里）：%s" % orphans)

    def test_auth_endpoints_used_by_ui(self):
        """登录链路必须齐全，否则控制台打不开。"""
        used = {_normalize(p) for p in _api_candidates(self.js)}
        for required in ("/api/v1/auth/state", "/api/v1/auth/login",
                         "/api/v1/auth/bootstrap", "/api/v1/auth/logout",
                         "/api/v1/auth/me"):
            self.assertIn(required, used, "界面未接入认证接口：%s" % required)

    def test_no_auth_boot_must_not_refresh_me(self):
        """`--no-auth` 启动时，/auth/me 的兜底刷新必须在入口被短路。

        实测现场：state 回"已认证 + 本地调试用户"但 csrfToken 为空，`start()`
        于是补 /auth/me；no-auth 服务端对它答 401，控制台被弹回登录门
        （"登录状态已失效"）。前端必须用 state 里的 authRequired 短路进入控制台，
        而且**发生在** refreshSession 调用之前——排序断言才是这条契约的本体：
        没有它，下一次有人把短路挪到刷新之后，测试不会叫。
        """
        self.assertIn("authRequired === false", self.js,
                      "start() 需要按 authRequired 短路 no-auth 启动")
        skip_at = self.js.index("authRequired === false")
        refresh_at = self.js.index("refreshSession()")
        self.assertLess(skip_at, refresh_at,
                        "no-auth 短路必须发生在 /auth/me 兜底刷新之前")

    def test_dom_ids_referenced_by_ui_exist(self):
        """脚本里出现的每个 id，都得有地方把它造出来（index.html 或运行期）。

        **`$('#x')` 这个简写必须算进来。** 本仓库取节点基本都用它，而这条断言原先
        只认 `getElementById('x')` / `querySelector('#x')` 两种**字面**写法——于是
        三百多处简写是一整片盲区。2026-10-15 的初始化失败就是从这片盲区出去的：
        `bindEvents()` 里 `$('#job-log')` 指向一个渲染期才造的节点，谁都没拦住。

        "造出来"包括**运行期造的**：`host.id = 'job-log'`、
        `svgEl('g', { id: 'graph-viewport' })`。只认 index.html 会把这类正常用法
        判成缺陷，而"守卫逼出错误改动"比漏报更坏。
        """
        defined = _static_ids(self.html) | _js_made_ids(self.js)
        missing = sorted(_id_selectors(self.js) - defined)
        self.assertEqual(missing, [],
                         "前端脚本引用了既不在 index.html、也没有在运行期造出来的 id：%s"
                         % missing)

    def test_no_external_network_dependency(self):
        """界面不许引入外部网络资源——离线内网部署会白屏。

        扫描前先剔除**文档位**（`placeholder=` 与 HTML 注释）。那里出现一个地址是
        给人看的输入示例，浏览器不会去连它；把它判成外部依赖，会让"写一句示例"
        变成一条测试失败，而处理这种失败最省事的办法——把示例里的 scheme 删掉——
        恰好把提示变得更难懂（`例如 api.deepseek.com` 不是 URL）。**守卫不该逼出
        这种改动**，所以该收的是守卫，不是文案。

        同时补上**协议相对**地址（`//cdn…/x.js`）：它没有 scheme，`https?://`
        一条都匹配不到，而它是最常见的外部引用写法之一。这里只判**加载点**
        （`src=` / `href=` / `@import`），不判注释与正则字面量里的 `//`。
        """
        html = re.sub(r"""placeholder\s*=\s*["'][^"']*["']""", "", self.html)
        html = re.sub(r"<!--.*?-->", "", html, flags=re.S)
        external = set(re.findall(r"https?://[^\s\"'<>)]+",
                                  self.js + html + self.css))
        # 允许 SVG 命名空间等非网络用途的 URL
        external = {url for url in external if "w3.org" not in url}
        self.assertEqual(sorted(external), [],
                         "界面引用了外部网络资源，内网离线环境会白屏：%s"
                         % sorted(external))
        for match in re.finditer(
                r"""(?:src|href)\s*=\s*["']\s*//[^"']*["']"""
                r"""|@import\s+(?:url\()?\s*["']?\s*//[^"')\s;]+""",
                self.html + self.css, re.I):
            self.fail("界面用协议相对地址引用了外部资源，离线内网会白屏：%s"
                      % match.group(0).strip())

    def test_all_scripts_and_styles_are_local(self):
        for name in self.scripts:
            self.assertIn('src="%s"' % name, self.html,
                          "index.html 未引入脚本：%s" % name)
        self.assertIn('href="style.css"', self.html)
        self.assertNotIn("integrity=", self.html, "不应引用带 SRI 的外部资源")

    def test_all_views_present(self):
        """十五个视图全在。**这份清单必须和导航段落一一对应**。

        它原先只列了七项——`guide`（使用说明）从头就漏在外面。漏掉一项的代价不是
        "少查一条"，而是**新增页签时没有任何断言看着它**：从部署页拆出「任务」页
        之后，这个清单还停在八个中文标签 / 七个 `data-tab` 上，新页整页无人看守。
        所以这里把 `rail-nav` 的三个段落（工作 / 产出 / 系统）按实际顺序列全。

        **页签名以这里为准**。改名史：先「DSH 控制台」→「控制台」，再「控制台」
        →「会话」（这一页装的是平台里那个完整的 DSH 网页应用，按页里实际的东西命名，
        `data-tab` 仍是 `dsh`）；「入口」徽章已撕掉（第一页本身就是入口）。
        「渗透任务」→「任务」、「任务」（一次执行的记录）→「作业」：两页同名找不到
        对应关系，所以按内容分开命名——**任务是一个目标，作业是一次执行**。
        改名要同步这一处，否则守卫会逼着旧名字留在页签上。
        """
        #
        # 2026-11 的第二轮改版把导航从三段合并成两段（功能 / 系统），并按 artex
        # 补了六页：仪表盘、流量、请求重发、工作空间、审批记录、日志。清单跟着
        # 从九项变十五项——**这里刻意不写"新增的六项"这种例外**，仍然逐条列全：
        # 例外条款等于给下一轮开一个豁免口。
        for label in ("会话", "仪表盘", "任务", "计划任务", "探索链路", "漏洞",
                      "资产", "流量", "请求重发", "报告", "作业", "部署",
                      "工作空间", "审批记录", "日志", "设置", "使用说明"):
            self.assertIn(label, self.html, "缺少视图：%s" % label)
        for tab in ("dsh", "dashboard", "tasks", "schedules", "graph", "findings",
                    "assets", "traffic", "repeater", "report", "jobs", "setup",
                    "workspace", "approvals", "logs", "settings", "guide"):
            self.assertIn('data-tab="%s"' % tab, self.html,
                          "缺少页签按钮：%s" % tab)
            self.assertIn('id="panel-%s"' % tab, self.html,
                          "缺少页签面板：%s" % tab)

        # 「Skills」是设置页的一组：技能手册的目录、查看、导入与删除。它不再是页签，
        # 也不再是「演练台」——那道授权门已经撤了（读一份 markdown 不需要授权）。
        self.assertIn('data-settings-group="skills"', self.html,
                      "设置页缺少「Skills」分组")
        self.assertIn('id="skills-list"', self.html, "Skills 的列表容器不见了")

    def test_chat_console_reads_conversation_contract(self):
        """「控制台」页签必须真的消费对话接口的字段，而不是画一个静态聊天框。

        这条测试针对的故障很具体：把对话框接成一个"发出去、原样显示"的文本区，
        页面上一切正常，只是模型永远不会调用任何工具。因此这里钉住"轨迹"与
        "会话 id"这类**只有真正跑通了工具循环才会出现**的字段。
        """
        for field in ("sessionId", "sessions", "trace", "durationMs", "messageCount",
                      "parameters", "hasModel"):
            self.assertIn(field, self.js, "对话页未使用字段：%s" % field)
        self.assertIn("/chat/tools", self.js,
                      "对话页必须拉取可用能力目录，否则使用者不知道它能做什么")
        self.assertIn("<textarea", self.html,
                      "对话输入应为多行输入框（目标路径与说明常常一行写不完）")

    def test_every_phrasing_names_a_real_tool(self):
        """现成说法表（`PHRASINGS`）里每条都必须绑**真实存在**的工具。

        「提示」那一格撤掉之后这张表没有跟着消失——**能力目录那二十多张卡片用的
        就是它**：点一张卡，填进去的是这句话而不是"用「start_scan」"，配不到就退回
        「用「<名称>」」的骨架。名字写错一个不会报错，只会静静地少一句现成说法，
        而那个症状看起来也像「一种设计」。所以拿 `chat.HANDLERS` 当权威名单逐个核。
        """
        from pentestd import chat
        bound = re.findall(r"\{\s*tool:\s*'([a-z_]+)'", self.js)
        self.assertGreaterEqual(len(bound), 8,
                                "说法表条目太少或正则已失配（重构后要同步改这条）")
        unknown = sorted(set(bound) - set(chat.HANDLERS))
        self.assertEqual(unknown, [],
                         "说法表绑了不存在的工具，这些说法永远不会被用上：%s" % unknown)

    def test_dock_is_data_driven_and_click_fills_the_input(self):
        """目录来自服务端过滤过的清单、点是按钮、点了真的填进输入框。

        第一条针对的故障叫「承诺做不到的事」：候选若是本地写死的，选了只读档的会话
        照样会看到「扫描 /srv/app」——照着点、照着发，收获一串权限拒绝。坞里现在
        两格都是服务端**按权限档过滤过**的目录（`/chat/tools`、`/skills`），所以
        这条断言盯的是"渲染真的逐条读那份目录"，而不是本地另铺一份。
        """
        self.assertIn("function renderTools", self.js, "缺少能力目录的渲染")
        self.assertIn("function renderSkills", self.js, "缺少技能手册的渲染")
        self.assertIn("view.tools.forEach", self.js,
                      "能力目录必须逐条读 /chat/tools 的结果，"
                      "本地另写一份清单会承诺这一档做不到的事")
        self.assertNotIn("SUGGESTIONS", self.js,
                         "写死的示例清单又回来了；它一回来，上面那条就形同虚设")
        self.assertIn("function fillInput", self.js,
                      "缺少「点一下填进去」的实现")
        self.assertIn("setSelectionRange", self.js,
                      "填完要把光标放到末尾，否则接着打字会插进句子中间")
        self.assertIn("dockChip", self.js,
                      "目录项必须走同一个建项函数，否则「显示的说法」与「填进去的说法」"
                      "会变成两份可以互相矛盾的文案")
        # `dockChip()` **返回**卡片，调用方必须自己 append。只建不挂的后果是面板一片
        # 空白，而控制台、页面上都不报任何错——单测全绿、只有截图或 DOM 才看得出。
        # 这一版真的踩过：`renderTools` / `renderSkills` 两处都漏了 append。
        defined = len(re.findall(r"function dockChip\(", self.js))
        calls = len(re.findall(r"dockChip\(", self.js)) - defined
        appended = len(re.findall(r"append\(\s*dockChip\(", self.js))
        self.assertGreater(calls, 0, "没有找到 dockChip 的调用")
        self.assertEqual(appended, calls,
                         "有 %d 处调了 dockChip 却没 append：卡片建出来当场被丢掉，"
                         "面板会是空的但不报任何错" % (calls - appended))

    def test_dock_modes_use_one_shared_attribute(self):
        """坞的模式在 CSS 与 JS 两侧必须用**同一个属性名**。

        样式表的模式块按 `[data-dock=…]` 取，JS 负责把它写上去。两边对不上时页面
        不报任何错——工具目录只是**悄悄退回**成流式换行的几行胶囊，而那看起来也像
        「一种设计」。这是改版前 `.tab-entry` 栽的坑的另一种形态，所以模式用属性而
        不是再加一个类名：同为单类名的两条规则，谁赢只看源码顺序。
        """
        for mode in ("none", "tools", "skills"):
            self.assertIn('[data-dock="%s"]' % mode, self.css,
                          "样式表里缺少坞模式 %s 的规则" % mode)
        self.assertIn("data-dock", self.js, "JS 没有把当前模式写回 DOM")
        self.assertIn('data-dock="none"', self.html,
                      "内容区的初始模式必须与服务端的默认值一致")
        self.assertIn("dock: 'none'", self.js,
                      "JS 的默认模式必须是 none（收起），与 index.html 的初始属性一致")
        # 收起必须**可达**。默认态就是收起，若"点当前这一格"仍是空操作，第一次展开
        # 之后就再也收不回去——而那一格占的正是输入框上方最贵的几十像素。
        # 改版前这句是 `(view.dock === mode && mode !== 'prompts') ? 'prompts' : mode`：
        # 当时有默认格可回，所以"点当前格"只能是空操作；现在没有默认格了。
        self.assertIn("view.dock === mode ? 'none' : mode", self.js,
                      "点当前这一格必须回到收起态，否则展开之后收不回去")
        self.assertNotIn("mode !== 'prompts'", self.js,
                         "旧的「点当前格回到提示」分支还在，说明撤得不干净")

    def test_the_two_composer_dials_sit_below_the_input(self):
        """模型与权限必须在**输入框下方**那一行里，不再挂在页头。

        这两个旋钮改的是"这一轮怎么跑"，改完的下一步就是按发送——所以它们和
        「发送」属于同一次动作。挂在页头时，改完要往回、往上走一趟去确认，而
        改它们的目的恰恰是马上发一句。这条钉的是位置，不是外观：`<textarea>` 在
        两者**之前**，且两者都在 `.chat-actions` 这一格里面。

        Agent 刻意**留在页头**：它是"这批活按什么口径跑"，一轮对话从头发到尾是
        同一个值，和"我在哪一页"是一类东西。所以这条也顺带钉住它没有跟着搬走。
        """
        head_end = self.html.index('<div class="chat-body">')
        self.assertLess(self.html.index('id="chat-agent"'), head_end,
                        "Agent 是会话级约定，应留在页头的配置行里")
        input_at = self.html.index('id="chat-input"')
        row_start = self.html.index('<div class="chat-actions">')
        self.assertGreater(row_start, input_at,
                           "发送那一行必须在输入框**之后**（DOM 顺序即视觉顺序）")
        row = self.html[row_start:]
        row = row[:row.index("</div>")]
        for node in ('id="chat-model-select"', 'id="chat-policy"'):
            self.assertGreater(self.html.index(node), input_at,
                               "%s 必须排在输入框之后" % node)
            self.assertIn(node, row,
                          "%s 必须在 .chat-actions 这一格里面，与「发送」同排"
                          "（放到别的行里就又会离输入框很远）" % node)

    def test_the_prompts_panel_is_gone(self):
        """「提示」那一格应已整体撤掉，且撤得干净。

        它推荐的话正是能力目录里那几句（同一个动作在两边说法不同，看起来像两件事），
        唯一独有的是"哪四条最常用"——那件事现在由目录的排序承担。撤的时候最容易
        留下的东西是**半条链路**：按钮没了、`renderPrompts` 还在，或者默认值还指着
        一个已经不存在的格。两种情况都不报错，只在某个分支上画出空白。
        """
        self.assertNotIn('id="chat-prompts"', self.html, "「提示」入口应已撤掉")
        for residue in ("promptsButton", "renderPrompts", "availablePrompts", "PROMPTS"):
            self.assertNotIn(residue, self.js,
                             "「提示」撤得不干净，%s 还在 JS 里" % residue)
        self.assertIn("function phrasingFor", self.js,
                      "说法表本身要留下——能力目录的卡片靠它给现成说法")

    def test_the_composer_row_stays_one_line_and_gives_way_in_order(self):
        """卡片最后一行的**一行契约**：与目录入口并排，宽度不够时按顺序让位。

        用户原话：「输入框下面几个选项并成一行」「数字大小和文字大小不一致」
        「模型两个字去掉」「右侧模型选择随窗口变窄逐渐变窄、淡化、最后只剩一个图标」。

        这一行自然宽 720px（目录格 272 + 模型 240 + 权限 150 + 发送 34 + 三个 8px
        缝），而卡片内容宽随窗口和分栏变化，实测：
        1920 → 846、1600 → 526、1440 → 366（详情栏开着！）、1180 → 511、
        900（堆叠）→ 772。**1440 那 366 才是最紧的常用档**，这也是为什么让位必须
        逐级、按容器宽度判，而不是按视口宽度拍一个媒体查询。

        为什么容器挂在 `.chat-field` 上而不是 `.chat-model` 上：先在后者试过
        `container-type: inline-size`，量到模型格只剩 **111px**（应为 240）——
        行内轴 containment 把这一格的 max-content 贡献算成 0，整行的自然宽度塌到
        238，模型名与权限名一起被挤。容器只能挂在"宽度与内容无关"的元素上。

        这里守七件事，每件都对应一个**已量到的坏法**：
          1. `.chat-actions` 与目录入口并排（`flex: 0 1 auto`），不是独占一行；
          2. `.chat-field` 是容器，模型格**不是**（回退成后者 = 111px 那种塌陷）；
          3. 发送键 `flex: 0 0 auto`——不写就被按比例压缩，1416 窗口下量到 **23px**
             宽（本该 34），圆变成压扁的椭圆；
          4. 计数字号与标签**同号**（`font: inherit`）——此前是 11px 等宽，比标签小
             一号，这就是"数字比文字小"的来源；
          5. 档位阈值**递减**且都是 `@container chat-field`：720 → 680 → 640 →
             536 → 522 → 392 → 344（每档的数字是"这一档除模型外所有东西的实测总宽"，
             写在这里是为了让后人改档时看得见它从哪来）；
          6. 最深那一档把目录格整组收起（卡片只剩 230~344px 时，132px 装不下
             190px 的三颗入口，裁出来的「可用 技能」和折行都更糟）。
          7. 模型格**跟着文字走**（`width: var(--model-w, auto)`；变量由 `chat.js`
             量选中项文字 + 左右内边距，`field-sizing: content` 兜首帧，240 只是
             封顶）。两种坏法都量到过：只给 `auto` 时框按**最长选项**量宽——
             短名字后面空出一大片，用户第二次指出的「右边还是有空白」就是它。
        """
        css = self.css
        field = self._top_rule(".chat-field")
        self.assertEqual(self._decl(field, "container-type"), "inline-size",
                         "卡片必须是容器：这一行的让位档位全靠它判宽度")
        self.assertEqual(self._decl(field, "container-name"), "chat-field")
        actions = self._top_rule(".chat-actions")
        self.assertEqual(self._decl(actions, "flex"), "0 1 auto",
                         "并排，且可让宽")
        model = self._top_rule(".chat-model")
        self.assertEqual(self._decl(model, "container-type"), "",
                         "模型格**不许**自己是行内轴容器：那会把它的 max-content "
                         "贡献算成 0（量过：240 → 111），整行一起塌")
        self.assertEqual(self._decl(model, "flex"), "0 1 auto",
                         "模型格要让宽（`0 1 auto`）：它现在是「跟着文字走」的"
                         "`width: auto`，固定在 240 又恰好是用户嫌宽的那件事")
        # ⚠️ t89：宽度不再由 `--chat-model-w` 派，而是**由内容给**。
        # 用户原话：「模型选择框太宽了，和文字大小不一致，能跟随文字大小，自动适配」。
        # 下面三条是那件事的机械判据，缺一条都会退回「框比字宽」：
        self.assertEqual(self._decl(model, "width"), "auto",
                         "宽度必须由内容给，不能是固定 240")
        self.assertEqual(self._decl(model, "max-width"), "240px",
                         "240 降级为**封顶**：长名字到这里为止")
        chip = self._top_rule(".chat-model-btn")
        self.assertEqual(self._decl(chip, "field-sizing"), "",
                         "t100：量宽的对象换成了自绘的 `<button>`。`field-sizing` 对"
                         "button 没有意义（它的固宽行为本就是取内容），留着只会让下一个人"
                         "以为还有第二层兜底在生效")
        self.assertEqual(self._decl(chip, "width"), "var(--model-w, auto)",
                         "主那一层是 `chat.js` 量出来的 `--model-w`（选中项文字 + "
                         "左右内边距）。写回 `auto` 会按最长选项量宽，写 `100%` 会撑满"
                         "封顶后的格子——两种都量到过")
        native = self._top_rule(".chat-model-native")
        self.assertEqual(self._decl(native, "position"), "absolute",
                         "原生 `#chat-model-select` 退成真相源之后仍在页面上：它要能被"
                         "`fillSelect` 写、被 `change` 读，所以不能是 `display: none`")
        self.assertEqual(self._decl(native, "opacity"), "0",
                         "它只占 1px 且透明：看得见的那一层是 chip")
        self.assertEqual(self._decl(native, "pointer-events"), "none",
                         "鼠标点不到它（`tabindex` 已经是 -1，也拿不到焦点）：点开菜单的是 chip")
        bare = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        self.assertNotIn("--chat-model-w", bare,
                         "档位改用 `max-width` 封顶；再用自定义属性派固定宽度，"
                         "就把「跟着文字走」重新按死了（短名字又会空出一大片）"
                         "（只在注释里提这个名字不算：那几段记载的正是它被撤掉的理由）")
        tier_720 = css[css.index("@container chat-field (max-width: 720px)"):]
        self.assertIn("max-width: calc(100cqw - 490px)", tier_720,
                      "宽档是封顶而不是派宽：名字短就窄，名字长才吃到 490 这一段")
        tier_392 = css[css.index("@container chat-field (max-width: 392px)"):]
        self.assertIn(".chat-model {", tier_392,
                      "只剩图标那一档是**唯一钉死**的宽度（30px = 图标直径）")
        self.assertIn("width: 30px", tier_392,
                      "图标档必须钉死 30px：`width: auto` 在 `color: transparent` "
                      "之后量到的是文字的宽，图标会飘")
        send = self._top_rule(".chat-send")
        self.assertEqual(self._decl(send, "flex"), "0 0 auto",
                         "发送键是该行唯一不可压的一项（不写就会被压到 23px）")
        dock_count = self._top_rule(".dock-count")
        self.assertEqual(self._decl(dock_count, "font"), "inherit",
                         "计数与标签同号：此前 11px 等宽比 13px 标签小一号，"
                         "看起来像「数字没长齐」")
        tiers = [int(m) for m in re.findall(
            r"@container chat-field \(max-width: (\d+)px\)", css)]
        self.assertEqual(tiers, sorted(tiers, reverse=True),
                         "档位必须从宽到窄写：同一条自定义属性靠**后写的赢**，"
                         "顺序一乱，窄窗口会套上宽窗口那一档")
        self.assertEqual(tiers, [720, 680, 640, 536, 522, 392, 344],
                         "档位表变了：每一档的阈值都是「除模型外其余控件的实测总宽」，"
                         "改之前先量（见本用例的 docstring）")
        deepest = css[css.index("@container chat-field (max-width: 344px)"):]
        self.assertIn(".chat-field-tabs { display: none; }", deepest,
                      "最窄那一档收起目录格整组，而不是裁字或折行")
        # 文字退场之后，控件只剩图形：名字必须另有出处。
        auto = self.html[self.html.index('id="chat-auto"'):]
        auto = auto[:auto.index(">")]
        self.assertIn('aria-label="自动继续"', auto,
                      "≤522px 时「自动继续」四个字被收起，那颗开关只剩一个胶囊——"
                      "没有 aria-label，读屏与悬停都叫不出它的名字")
        self.assertIn("title=", auto, "同理，鼠标悬停也要有个名字")
        select = self.html[self.html.index('id="chat-model-select"'):]
        select = select[:select.index(">")]
        self.assertIn('aria-label=', select,
                      "「模型」两个字删掉之后，选择器自己只有模型名（「跟随全局启用项」），"
                      "读屏听到的会是「一个下拉框」而不是「模型选择」")

    def test_the_send_button_icon_fills_a_third_of_the_disc(self):
        """圆钮里那颗图标要占到直径的**三分之一左右**：它是这一页唯一的动作，不是徽标。

        t90 用户原话：「发送和暂停按钮中间的图标有点小」。改之前是 16 / 14，量下来
        **箭头只有 9.3px 高**——那套路径在 viewBox 里只占 14/24（先乘 0.58），落到
        34px 的圆里就是 27%，比同一行里 14px 的盾牌还轻。

        钉五件事，每一件错了都不报错、只是"看起来不对"：

          1. 两套图标各自的 px（20 / 18）：这两个数是量出来再挑的，见本轮对照表；
          2. **方块比箭头小两档**：实心方块占 viewBox 的 11/24（46%），箭头占 14/24
             且只有一圈描线——同尺寸下方块会显得更重，这个 2px 的差正是"换身份时
             粗细相当、而不是一样大"；
          3. 两个数都是**偶数**，且 `圆钮直径 − px` 也是偶数：图形落在整数像素上，
             奇数会让中心落在半像素，竖杆在 1x 屏上糊一层；
          4. 区间 18..24：再小就是改回原样，再大（量过 22，箭头已占直径的 45%）
             图形贴住圆边，34px 的圆就只剩"一圈描边"的作用；
          5. **圆钮自己的几何不许动**：图标变大不等于按钮变大——它同时是动作行的下限。
        """
        disc = re.search(r"--chat-action-row:\s*(\d+)px", self.css)
        self.assertIsNotNone(disc, "--chat-action-row 不见了：动作行下限与圆钮直径同源")
        disc_px = int(disc.group(1))
        self.assertEqual(disc_px, 34, "圆钮直径被改了：它是动作行高度的唯一来源")

        send = self._top_rule(".chat-send svg")
        stop = self._top_rule(".chat-send.is-stop svg")
        send_px = int(self._decl(send, "width").rstrip("px"))
        stop_px = int(self._decl(stop, "width").rstrip("px"))
        self.assertEqual(self._decl(send, "height"), "%dpx" % send_px,
                         "图标必须是正方形：宽高不同会把箭头压扁")
        self.assertEqual(self._decl(stop, "height"), "%dpx" % stop_px, "同上")
        self.assertEqual((send_px, stop_px), (20, 18),
                         "两套图标的 px 变了：先量（本用例 docstring 记着改之前的 9.3px）")
        for name, px in (("箭头", send_px), ("方块", stop_px)):
            self.assertEqual(px % 2, 0, "%s 的 px 取了奇数：中心会落在半像素上" % name)
            self.assertEqual((disc_px - px) % 2, 0,
                             "%s：%d−%d 不是偶数，图形落不到整数像素上"
                             % (name, disc_px, px))
            self.assertGreaterEqual(px, 18, "%s 缩回 %dpx 了：那正是「有点小」的那一版"
                                    % (name, px))
            self.assertLessEqual(px, 24, "%s 顶到 %dpx：图形会贴住圆边" % (name, px))
        self.assertEqual(send_px - stop_px, 2,
                         "实心方块与线性箭头之间那个 2px 的差被拉平了：同尺寸下方块更重")

        btn = self._top_rule(".chat-send")
        self.assertEqual(self._decl(btn, "width"), "var(--chat-action-row)",
                         "图标一改按钮就跟着长大 —— 动作行的下限会算错")
        self.assertEqual(self._decl(btn, "height"), "var(--chat-action-row)", "同上")

    def test_hovering_the_send_button_keeps_it_its_own_colour(self):
        """悬停时这颗圆**必须还是它自己的颜色**，不许被 `.btn:hover` 换成中性面。

        t91 用户原话：「鼠标悬停按钮变白了，颜色略微减淡或者增加动效，比如按钮边框
        增加白色渐变框」。根因是一条**通用的**、看起来完全无害的规则：
        `.btn:hover { background: var(--panel-3) }`，选择器 (0,2,0)；而 `.chat-send`
        是 (0,1,0)——鼠标划过一次就赢。这颗圆的墨色是 `--on-accent`，于是：

          · 默认的液态玻璃主题：底 rgba(255,255,255,.92) + 字 #fff = **1.0:1**，
            箭头悬停的瞬间**整个消失**（"变白"就是它）；
          · 深色主题：底 #292930 + 近黑墨色，同一个 bug 的另一边。

        这不是配色偏好：**最常被指的那颗按钮，在指上去的时候看不见了**。而它一个字都
        不会报错——静态审查看到的是"悬停有反馈"，只有量过对比度才知道反馈是"消失"。

        钉六件事，每一件错了都只是"看起来不对，或者反过来看着挺对"：

          1. 悬停底色是 `color-mix(... var(--accent) ... var(--text))`——**同一个色相、
             只挪一档**，且挪的方向由 `--text` 决定（每套主题里与面板对比最高的那一极），
             不是亮度过滤器：`brightness()` 对深色主题的浅蓝与亮色主题的深蓝方向相反；
          2. 悬停底色**不许出现任何面板令牌**（`--panel` / `--panel-2` / `--panel-3`）：
             写回中性面就退回这条 bug，且它在深色主题下**看起来是对的**；
          3. **底色**对所有悬停生效，含禁用态——`.btn:hover` 自己不排禁用态，而我们
             一排除，禁用态（无模型）就会退回"白底白箭头"，那恰好是用户会停下来
             想问"为什么不能发"的那一档。**白光**则只给能按的；
          4. 三层白光各就各位（白边 / 重点色雾 / 白晕）：白光落在浅色画布上只有约
             1.17:1，亮色与玻璃主题**只能**靠那层雾；去掉它，两套主题就只剩"底深了
             12%"这一个几乎看不见的信号。禁用态有自己的 `box-shadow: none` 撤掉它；
          5. 忙碌态自己也有一条悬停：`.chat-send:hover` 与 `.chat-send.is-stop` 都是
             (0,2,0)，只能靠"后写的赢"，而悬停那条就在后面——不写那条 (0,4,0) 的，
             跑起来之后把鼠标放上去，红底变回蓝底，看起来像"点了停止没反应"；
          6. 几何一个字都不动（宽高 / 圆角 / 位移 / 图标），过渡只留基类一份：那是
             上面那条用例钉过的事，悬停不是换一个按钮。
        """
        css = self.css
        hover = self._top_rule(".chat-send:hover")
        background = self._decl(hover, "background")
        self.assertIn("color-mix(", background,
                      "悬停底色不再是 color-mix 了：这条用例钉的是「同色相挪一档」，"
                      "换成别的写法前先想清楚它在四套主题下的方向")
        self.assertIn("var(--accent)", background,
                      "悬停底色必须从 --accent 派生：换了色相，那颗圆在悬停时就不是它自己了")
        self.assertIn("var(--text)", background,
                      "混色的另一端必须是 --text（每套主题里与面板对比最高的那一极）。"
                      "换成白色字面量会在深色主题里变脏，换成 --panel-* 就是这条 bug 本身")
        for token in ("--panel", "--panel-2", "--panel-3"):
            self.assertNotIn(token, background,
                             "悬停底色里出现了 %s：这正是「悬停变白」那条 bug —— "
                             ".btn:hover 当年就是把它写在这里" % token)

        for prop, why in (("width", "圆钮直径"), ("height", "圆钮直径"),
                          ("border-radius", "圆形"), ("transform", "抬 2px"),
                          ("padding", "内边距")):
            self.assertEqual(self._decl(hover, prop), "",
                             "悬停规则里写了 %s（%s）：悬停是换一层光，不是换一个按钮"
                             % (prop, why))

        shadow = self._decl(hover, "box-shadow")
        self.assertIn("var(--focus-halo-edge)", shadow,
                      "白边（1px）不见了：深色主题下那道「白色边框」就是它")
        self.assertIn("var(--accent-soft)", shadow,
                      "重点色雾不见了：白光落在浅色画布上只有约 1.17:1，"
                      "亮色与玻璃主题**只有**这一层看得见")
        self.assertIn("var(--focus-halo-glow)", shadow,
                      "白晕不见了：使用者说的「白色渐变框」就是这一层的模糊")
        self.assertEqual(shadow.count("0 0"), 3,
                         "白光层数变了（应为 1px 边 + 3px 雾 + 12px 晕三层）："
                         "每一层都对应一组主题，去掉一层就有主题只剩「底深了 12%」")

        # 过渡写在**基类**那一条里：过渡按元素"新的"计算取值取，写进 `:hover` 只在
        # 进去时生效、出来时硬切——白光会"啪"一下消失。基类那份是唯一声明处。
        base = self._top_rule(".chat-send")
        self.assertIn("box-shadow", self._decl(base, "transition"),
                      "基类没把 box-shadow 写进 transition：白光会硬切，"
                      "鼠标离开时也不会淡出（使用者要求的是「增加动效」）")
        self.assertEqual(self._decl(hover, "transition"), "",
                         "过渡又写回悬停规则里了：进出两个方向都要动，只能留一份在基类")

        # 禁用态整颗压到 40% 不透明，不该再亮一圈；用整条规则的形状判，
        # 而不是拿正文反查选择器——`_top_rule` 返回的是压平空白后的正文，
        # 已经不是原文的子串了。
        self.assertRegex(self.css, r"\.chat-send:disabled:hover\s*\{[^}]*box-shadow:\s*none",
                         "禁用态的悬停没撤掉白光：无模型时那颗按钮是 40% 不透明的，"
                         "高光会把「按不动」画成「可以按」")
        # 同特异性 (0,2,0) 对 (0,2,0)：只能靠书写顺序赢。这条一旦被人重排就**静默**
        # 失效，而白底白箭头在深色主题里看起来还挺"正常"，只有量对比度才发现。
        btn_hover = re.search(r"\.btn:hover\s*\{", self.css)
        chat_hover = re.search(r"\.chat-send:hover\s*\{", self.css)
        self.assertIsNotNone(btn_hover, "样式表里找不到 `.btn:hover`")
        self.assertIsNotNone(chat_hover, "样式表里找不到 `.chat-send:hover`")
        self.assertLess(btn_hover.start(), chat_hover.start(),
                        "`.chat-send:hover` 必须排在 `.btn:hover` 之后：两者都是 (0,2,0)，"
                        "靠后写的赢；顺序一换，悬停立刻退回中性面")
        self.assertIn(".chat-send.is-stop:not(:disabled):hover", css,
                      "忙碌态（停止）缺一条悬停：红底会被 (0,2,0) 那条悬停盖回蓝底")

    def test_the_dock_entries_carry_icons_and_the_icons_sit_optically_centred(self):
        """t92：两颗入口改名并加了图标，图标与中文标签是「光学对齐」而不是「盒居中」。

        用户原话：「输入框中的可用能力、技能手册修改名字为能力、技能，同时增加图标；
        完全作业前面的图标和文字完全对齐」。三件事各有一条能**静默**坏掉的路径：

        * 名字改一半：按钮上是新的、提示行或读屏还是旧的，读起来像两个东西；
        * 图标不带 `aria-hidden`：读屏把装饰念成内容；不吃 `currentColor`：换主题
          不跟色，深色主题下会留下一个永远深色的图标；
        * 只写 `align-items: center`：盒中心是重合的，但 13px 汉字的墨迹中心比 em
          框中心低半个像素（实测 `fontBoundingBoxAscent 14 / descent 3`、
          墨迹 `ascent 11 / descent 1`），14px 的图标看着比字高半格——这就是
          「没有完全对齐」的全部来源。0.5px 是量出来的，不是眼估的。
        """
        for button_id, label in (("chat-tools", "能力"), ("chat-skills", "技能")):
            found = re.search(r'<button id="%s".*?</button>' % button_id, self.html, re.S)
            self.assertIsNotNone(found, "找不到坞入口 #%s" % button_id)
            markup = " ".join(found.group(0).split())
            self.assertIn('class="dock-tab-ico"', markup, "#%s 没有图标" % button_id)
            self.assertIn('aria-hidden="true"', markup,
                          "#%s 的图标没标 aria-hidden：读屏会把装饰念成内容" % button_id)
            self.assertIn('stroke="currentColor"', markup,
                          "#%s 的图标没吃 currentColor：换主题时会脱离文字色" % button_id)
            self.assertIn('stroke-width="1.7"', markup,
                          "#%s 的描边粗细与权限 chip 的盾牌不一致——它们是同一套画法" % button_id)
            self.assertIn('</svg>%s<span class="dock-count"' % label, markup,
                          "#%s 的标签不是「%s」，或者没接在图标后面" % (button_id, label))

        ico = self._top_rule(".dock-tab-ico")
        self.assertEqual(self._decl(ico, "width"), "14px", "坞入口的图标尺寸变了")
        self.assertEqual(self._decl(ico, "height"), "14px")
        self.assertEqual(self._decl(ico, "flex"), "0 0 auto",
                         "图标可被压缩：窄档下它会先于标签变形，而标签才是名字")

        # 这一条必须是**分组**规则：`.chat-policy-ico` 另有一条写尺寸的规则，
        # 分成两条时后写的那条会把补偿盖掉一半（只有盾牌不补偿）。
        group = re.search(r"\.dock-tab-ico,\s*\n\s*\.chat-policy-ico\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(group, "两个图标的光学补偿必须写在同一条分组规则里")
        self.assertEqual(self._decl(group.group(1), "transform"), "translateY(.5px)",
                         "缺半个像素的补偿：盒对齐了，看着还是高半格")
        self.assertRegex(self.css, r"\.chat-policy-ico\s*\{\s*width:\s*14px",
                         "权限 chip 的盾牌尺寸变了：光学补偿是按 14px 量的")

        # 加了图标不等于可以长高：卡片下限 `--chat-field-floor` 里那个 28 写的就是它。
        self.assertEqual(self._decl(self._top_rule(".dock-tab"), "height"), "28px",
                         "坞入口高度变了，卡片下限（4 + 8 + 96 + 34）随之失真")

    def _top_rule(self, selector):
        """取一条顶层规则的花括号内容；找不到就失败，而不是返回空串。

        这里按**原始**选择器匹配（`self.css` 是未剥注释的原文）：`.chat-input` 与
        `.chat-input:focus` 是两条不同的规则，若先剥注释再比对，前者有可能命中后者。
        """
        match = re.search(r"%s\s*\{([^}]*)\}" % re.escape(selector), self.css)
        self.assertIsNotNone(match, "样式表里找不到规则 %s" % selector)
        return " ".join(match.group(1).split())

    def _decl(self, rule, prop):
        """按**声明**取一条属性：只有行首或 `;` 之后的 `prop:` 才算。

        本文件另有两个测试类各带一份同名助手，语义一致。这里之所以不直接
        `assertNotIn("border-top", …)`：`_top_rule` 返回的正文里**含注释**
        （`.chat-compose` 与 `.chat-input` 的规则体内都写了大段理由），于是"注释里
        提到过某个属性名"会被读成"这条声明还在"——报的错指向 CSS，而 CSS 是好的。
        t57 已经栽过一次。

        t62 又被咬了一次，方向相反：注释紧跟在**下一条声明之前**时（`.chat-log` 里那条
        讲 `--chat-overlap` 的说明就落在 `padding:` 与 `padding-bottom:` 之间），
        `(?:^|;)` 这个锚点要求声明前面是分号或行首，而注释的 `*/` 后面什么都没有——
        于是声明读成空串，报出来是"内边距没有把叠进的那一段算进去"。**CSS 是对的**，
        挡住的是助手自己。所以先剥掉规则体内的注释再找。
        """
        body = re.sub(r"/\*.*?\*/", "", rule, flags=re.S)
        match = re.search(r"(?:^|;)\s*%s:\s*([^;]+)" % re.escape(prop), body)
        return match.group(1).strip() if match else ""

    def test_the_composer_strip_is_borderless_and_the_input_does_not_float(self):
        """输入区那一整条**没有底色、也没有上边框**；输入框自己**没有底色、也没有投影**。

        输入区原来铺 `--panel-2`，而输入框自己也是 `--panel-2`：两者同色，于是在
        亮色与液态玻璃主题下那一整条就是一块白底，输入框的圆角与描边在白底里看不
        出来——看过去是「一条白色横带里有个光标」，而不是一个输入框。先撤掉的是
        **外层**那一条（`.chat-compose`），接着撤的是**控件本身**（`.chat-input`）：
        两处都撤掉之后，输入框的内壁与它周围同色，它成为一圈"井口"。

        本轮又撤了两样（用户原话：「取消悬浮，去掉输入框上面的横杠」），方向都是
        "让这一片与页面同一平面"，两样都先量过：
          * **composer 的上边框**——那条横贯主栏的线。撤之前它是一行 `(210,215,224)`、
            x 565..1415，而输入框自己的描边只跨 x 581..1400——**左右各宽 16px**，正好
            是 composer 的 `--sp-4` 内边距：这个差额就是"画在边框盒上"而不是"画在输入框
            上"的签名（`.scratch/t58/line_scan.py` + `row.py`，纸主题。工具报的 x_min
            是 552，那是主栏左缘的**竖**线，别当线的起点）。
          * **输入框的悬浮投影**——撤之前是上沿一段 6 行的渐变（683..688 行，最深比
            画布暗 16 个单位），撤之后那一带与画布同色。于是 `--shadow-float` 在全
            产品**没有消费者**了（它的定义处写着这件事）。

        这条钉七件事：白底不许回来（`background: none`）；**上边框不许回来**；
        输入框自己是透明的，而通用控件**仍然不透明**（这一处是全文件唯一的例外，不许
        泄漏成全局）；输入框**不带投影**（也不许换回 `--shadow-2` 那一档）；默认高度
        有一个**不依赖 `rows` 属性**的下限；圆角是卡片那一档；以及一条关于**源码顺序**
        的承重关系（见下）。

        为什么用 `_decl` 而不是 `assertIn("border-top", …)`：`_top_rule` 返回的正文
        **含注释**，而这两条规则的正文里都写着大段理由——"注释里提到某个属性名"会被
        读成"那条声明还在"，报的错指向 CSS 而 CSS 是好的。按**声明**取就没有这个歧义
        （`_decl` 的 docstring 里写了这条教训的来处）。

        最后一条是关于**源码顺序**的：`.chat-input`（0,1,0）与 `.input`（0,1,0）同
        特异性，透明之所以生效全靠前者排在后者之后。把这一条挪到 `.input` 上面，
        底色会被静默铺回来——用例全绿、只有那一块又变成白板。本文件里同类承重关系
        见 `.chat-quick` 与模式选择器那一段注释（那一处靠提高特异性解决，这一处只能
        靠顺序，所以必须由断言来守）。
        """
        compose = self._top_rule(".chat-compose")
        self.assertIn("background: none", compose,
                      "输入区那一整条的白底应已撤掉，不能让 --panel-2 再铺回来")
        self.assertEqual("", self._decl(compose, "border-top"),
                         "顶部分隔线本轮按用户要求撤掉，不许回来——那条横杠就是它")
        inp = self._top_rule(".chat-input")
        self.assertEqual("", self._decl(inp, "box-shadow"),
                         "输入框不再悬浮：投影撤掉后它与页面同一平面"
                         "（--shadow-float 因此没有消费者，见它的定义处）")
        self.assertIn("min-height", inp,
                      "默认高度要有 CSS 下限，不能只靠 rows 属性（字号一变就飘）")
        self.assertIn("border-radius: var(--r-3)", inp,
                      "输入框用与卡片同级的圆角档，不是通用控件那一档")
        self.assertIn("background: transparent", inp,
                      "输入框自己也不再铺控件那一档：铺上 --panel-2 就是屏幕下半部的一块白板")
        base = self._top_rule(".btn, .select, .input")
        self.assertIn("background: var(--panel-2)", base,
                      "通用控件的底色要留着：透明只针对会话输入框这一格，不是全局改底色")
        self.assertLess(self.css.index(".btn, .select, .input {"),
                        self.css.index(".chat-input {"),
                        "`.chat-input` 必须排在 `.input` 之后（同特异性，靠源码顺序取胜）")

    def test_the_log_fades_out_at_its_bottom_edge(self):
        """记录区底边**渐隐**，且渐隐必须**落在内边距里**（不许化掉文字）。

        为什么需要它（用户原话：「页面下拉时，输入框能够看到后面的对话」）：
        输入框上边框与记录区底边之间只有 12px（`--chat-gap`），而输入区那一条上边框与
        输入框自己的悬浮投影都在 t58 撤掉了 —— 于是**边界上再没有任何东西**：滚到底时
        最后一行字被齐口切断，断面距输入框 11px。
        量过（`.scratch/t59/probe5.py`：冻结夹具 + 判定用的每一条边都从像素里反推）：
        输入框上下边框在图上落在 y 694 / 817，滚到底时对话最低可见行 681，
        而 `x∈(560,1400)` 里 y694..856 的**变化像素为 0** —— 记录内容**从不**画进
        输入框内，但会**贴到**它上方 11px。

        ⚠️ 第一版把渐隐**从内容下沿量起**（`calc(100% - var(--sp-4) - var(--chat-fade))`），
        于是滚到底时被化掉的 22px 正好是**最后一行字**：字淡掉、读不全（用户截图即此，
        `.scratch/t60/probe7.py` 把两版并排量了一遍）。

        ⚠️ **t61 曾把端点挪到卡片上沿，t63 又挪回 `100%`**：t61 的参考图要的是"对话贴着
        卡片上沿被切开"，t63 的用户要的是反过来的——「整个卡片背景应该是透明的，对话框
        经过输入框时，通过输入框你能够看到对话框的字」。端点留在卡片上沿的话，内容恰好
        在卡片所在这段区间里淡到全透明：卡片透明了、后面却是空的（那时无论怎么调卡片
        材质都看不到字）。现在的两层约定，缺一条就看不到对话：
          1. 渐隐的透明端 = **`100%`**（记录区盒底，落在卡片下沿附近）；
          2. `padding-bottom: calc(var(--chat-overlap) + var(--chat-fade))` —— 内边距 ≥
             叠进深度 + 渐隐长度，同样写成一个表达式。
        两条合起来 = 滚到底时渐隐区整段落在**空内边距**里，文字一个都不动（可读性优先）；
        而**往上滚**时内容在卡片后面保持实底地滑过 —— 那正是这一轮要的效果。两个数都定义
        在 `.chat-main` 上（用它们的两个元素是兄弟，只有共同祖先能同时给到），由
        test_the_card_overlaps_the_log_by_one_shared_number 钉住"两边引用同一个数"。

        另三件事也是构造级的（渐隐的**效果**由量测负责，单测不渲染）：
          · `mask-image` 必须是 `linear-gradient`（悬停：删掉它，齐口切回来而没人会红）；
          · 到透明端那一处必须**全透明**，不是"还剩一点"；
          · 不透明那一端必须是**令牌**：mask 里只有 alpha 有意义，而 `test_theme` 那条
            "没有漏网的颜色"总闸禁止在令牌区之外散写颜色（第一版写 `#000` 就是被它拦下）。
        """
        log = self._top_rule(".chat-log")
        mask = self._decl(log, "mask-image")
        self.assertIn("linear-gradient", mask,
                      "记录区底边要有渐隐：撤掉它，最后一行字会被齐口切断")
        self.assertIn("var(--text) calc(100% - var(--chat-fade))", mask,
                      "渐隐要落在记录区自己的最后 --chat-fade 那一段（滚到底时那一段是"
                      "空内边距，所以文字不会被化掉）")
        self.assertIn("transparent 100%", mask,
                      "透明端必须在记录区盒底 `100%`：写在卡片上沿的话，内容恰好在卡片"
                      "所在这段区间里淡到全透明，于是卡片再透明也看不到后面的字")
        self.assertNotIn("var(--chat-overlap)", mask,
                         "mask 里不许再出现叠进深度：那是 t61 的写法（端点钉在卡片上沿），"
                         "正是本轮要撤掉的那条 —— 它让卡片后面永远没有可见内容")
        self.assertNotIn("var(--sp-4)", mask,
                         "渐隐不许再减内边距：第一版就是这么写的，滚到底时它化掉的"
                         "正是最后一行字（用户截图）")
        inner = re.sub(r"/\*.*?\*/", "", log, flags=re.S)
        self.assertRegex(
            inner,
            r"padding-bottom:\s*calc\(\s*var\(--chat-overlap\)\s*\+\s*var\(--chat-fade\)\s*\)",
            "底部内边距必须 = 叠进深度 + 渐隐长度（写成 calc(...) 这一条不等式）："
            "少了它，滚到底时渐隐会去化掉最后一行字，而且字面上看不出来")
        self.assertIn("var(--text)", inner,
                      "渐隐的不透明那一端必须是**令牌**，不许写成颜色字面量："
                      "mask 里只有 alpha 有意义，而 test_theme 那条总闸禁止在令牌区之外"
                      "散写颜色")
        self.assertIn("overflow: auto", log,
                      "记录区仍是滚动容器 —— 渐隐是叠加在滚动之上的，不是替代它")

    def test_the_card_overlaps_the_log_by_one_shared_number(self):
        """卡片叠进记录区的那一段（`--chat-overlap`）与记录区的内边距/渐隐端**同一个数**。

        参考图（`.scratch/t61/ref_topleft.png`、`ref_cardTL.png`）里，对话是**贴着卡片上沿**
        被切开的：气泡在图上从 y42 一直画到 y48，而卡片的上边框就在 y49 —— 中间没有空当。
        t61 让卡片用 `margin-top: calc(-1 * var(--chat-overlap))` 叠上来。

        ⚠️ **t63：这个数从手填的 20px 改成"卡片自己的高度"**（引用 `--chat-field-floor`）。
        用户要"透过输入框看到对话"，那就得让记录区的盒子伸到**整张卡片**底下——只伸 20px
        的话，能透出来的只有顶上那条 20px 的窄带（下面 128px 里根本没有记录内容）。
        量过（`.scratch/t63/probe.py`，两个矩形都从像素里反推）：叠进 20px 时卡片上沿
        只比记录区盒底高 **8px**（.chat-compose 的上内边距吃掉了 12px）；改后是 **136px**。

        这一条钉的是**"一个数、三处引用"**，因为这几个值分居两个元素：
          · `.chat-log`（兄弟之一）：`padding-bottom` 与 `mask-image` 里都要出现它；
          · `.chat-field`（兄弟之二）：负的 `margin-top` 里要出现它；
          · 两处都从 `.chat-main` 继承 —— 各自定义一份的话，改一处不改另一处时，记录区会
            **永久**多留一段空白（或反过来：内容被卡片吃掉一条），而两个文件分开看都没错。
            这正是这个项目已经付过几次代价的那类缺陷。
        """
        main = self._top_rule(".chat-main")
        self.assertIn("--chat-overlap:", main,
                      "--chat-overlap 必须定义在 .chat-main 上：用它的两个元素是兄弟，"
                      "它们唯一的共同祖先是这一层")
        self.assertIn("--chat-fade:", main,
                      "--chat-fade 同理（t61 从 .chat-log 挪到这里，与 --chat-overlap 同处）")
        self.assertIn("--chat-overlap: var(--chat-field-floor)", main,
                      "叠进的深度必须**引用卡片自己的高度**，不许再手填一个数：手填的数"
                      "在卡片变高时（展开目录 / 窄窗口折行 / 拉大输入框）不会跟着变，"
                      "症状是卡片下半截后面没有内容——而那时看不出是哪个数错了")
        self.assertIn("--chat-field-floor:", main,
                      "卡片的高度必须定义在 .chat-main 上：`.chat-log` 要用它，而自定义"
                      "属性只向下继承——留在 .chat-compose 上，记录区那一对 calc 解析不到"
                      "它，整条声明「无效于计算值」，症状是记录区底部内边距消失、内容被"
                      "卡片吃掉一条")
        field = self._top_rule(".chat-field")
        self.assertIn("margin-top: calc(-1 * var(--chat-overlap))", field,
                      "卡片要叠进记录区：那个负外边距就是叠进去的深度")
        log = self._top_rule(".chat-log")
        self.assertIn("var(--chat-overlap)", self._decl(log, "padding-bottom"),
                      "记录区的内边距必须把叠进的那一段算进去，否则渐隐会落在内容上")

    def test_the_input_box_is_a_card_with_surface_and_elevation(self):
        """输入框那一格**是有面的卡片**，形状照官方 DSH 输入区。

        这条用例在 t78 **反转**了 t63 的判据。那一轮用户要的是「整个卡片背景应该
        是透明的，对话框经过输入框时，通过输入框你能够看到对话框的字」；本轮用户
        要的是「控制台输入框及内容和官方保持一致」，而官方那张卡片（DSH 客户端
        `InputBar.module.css` 的 `.card`）是**有面、有软投影、没有描边**的：

            background: var(--dsw-specific-input-major);   ← 实底
            box-shadow: var(--dsw-elevation-soft);         ← 软投影
            border: 0;                                     ← 没有描边

        所以这一格从"透明 + 1px 描边"回到"面 + 投影、无描边"。（描边与投影同时上
        会显得脏，官方只留投影；焦点环另算，见下一条用例。）

        钉四件事：
          1. `background` 是 `--panel-2`（本表的"输入区面"），不是 `transparent`；
          2. `box-shadow` 是 `--shadow-float`（软投影）；
          3. `border: 0`——描边让位给投影；
          4. 圆角仍是 `--r-3`，且**不进**玻璃主题的模糊清单（有面的卡片再上
             `backdrop-filter` 只是把面糊成半透明，官方没有这一层）。
        """
        field = self._top_rule(".chat-field")
        self.assertEqual("var(--panel-2)", self._decl(field, "background"),
                         "卡片要有面：官方那张输入卡片是实底的。"
                         "⚠️ 判据必须走 `_decl`：这条规则的理由里**正写着** "
                         "`background: transparent` 那串字，按原文搜会命中注释")
        self.assertEqual("var(--shadow-float)", self._decl(field, "box-shadow"),
                         "软投影是这张卡片唯一的边界（官方 border: 0）")
        self.assertEqual("0", self._decl(field, "border"),
                         "描边让位给投影：两者同时上会显得脏，官方只有投影")
        self.assertIn("border-radius: var(--r-3)", field, "圆角留着")
        stripped = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        self.assertIsNone(re.search(r'\[data-theme="glass"\] \.chat-field\b', stripped),
                          "卡片不许进玻璃主题的模糊清单：有面的卡片再叠一层模糊，"
                          "只会把面糊成半透明，与官方那张实底卡片不是一回事")

    def test_the_send_button_is_a_round_icon_button(self):
        """「发送」是**圆形图标按钮**：直径 = 动作行的下限，且名字退到 aria-label/title。

        参考图里它是一颗实心圆 + 向上的箭头（量到直径约 36px，落在 100px 高的卡片里）。
        钉三件事：
          1. `.chat-send` 存在且 `border-radius: var(--r-pill)`（圆形）；
          2. **尺寸 = `--chat-action-row`** —— 动作行的下限正是由它撑起来的，两个数必须
             相等，否则下限低估（矮窗口下这一行被顶出视口，那是 t57 量过的坏法）；
          3. 按钮只有图标时，`aria-label` 与 `title` 是它**唯一的名字**，两个都不能省。
        """
        send = self._top_rule(".chat-send")
        self.assertIn("border-radius: var(--r-pill)", send, "发送是圆形按钮")
        self.assertIn("width: var(--chat-action-row)", send,
                      "圆的直径必须就是动作行的下限：两个数分开写就会各自漂")
        self.assertIn("height: var(--chat-action-row)", send, "同上，高也要引同一个数")
        self.assertIn("background: var(--accent)", send,
                      "圆钮要有强调色填充（参考图里它是整行唯一带填充的东西）："
                      "只改尺寸与圆角的话，形状对了、重量不对")
        # t80：形状照 DSH 桌面端那颗圆钮（34px 圆、无边框、抬 2px），但墨色仍取
        # **成对令牌** `--on-accent`：颜色字面量在令牌区之外一律不允许（见
        # test_theme 的总闸），而 `--on-accent` 在每个主题里都是强调色的配对墨色。
        self.assertIn("color: var(--on-accent)", send,
                      "底与字必须是成对令牌：强调色填充配它自己的配对墨色")
        at = self.html.index('id="chat-send"')
        tag = self.html[at:at + 200]
        self.assertIn('aria-label="发送"', tag, "只有图标的按钮，aria-label 是它唯一的名字")
        self.assertIn('title="发送"', tag, "悬停提示同理")

    def test_the_focus_signal_moves_to_the_single_box(self):
        """输入区只有**一个框**：焦点信号也跟着搬到卡片上，输入框自己不画任何框。

        这条用例**替换**了 t63 的 test_the_focus_state_comes_from_the_base_rule。
        那一条禁止写 `.chat-input:focus`，理由是"`box-shadow` 整条覆盖，写窄了会把
        焦点环抹掉"。t64 把环搬到卡片时判据就反转过一次；t84 只换颜色，判据不变：

          · 输入框的边框撤掉（内嵌框就是它），于是它自己不再是一个框；
          · 焦点信号若还画在它身上，卡片里就又多出一个框——只在点进去的瞬间可见；
          · 于是信号归**卡片**（`.chat-field:focus-within`），`.chat-input:focus`
            必须存在，用来把通用规则给 textarea 的那圈环显式抹掉——t63 禁止的那一条。

        t84 换的是**色族**：蓝环（`--focus-ring`）→ 一圈 1px 白边 + 一层 16px 白晕
        （`--focus-halo-edge` / `--focus-halo-glow`）。两族分开的理由写在 CSS 里：
        蓝族是"可交互 / 已选中"、跨主题跟着重点色走，白光说的是"光标在这里"；
        混成一族的话，白光会在某次换重点色之后跟着变成品红。

        浏览器实测（`.scratch/t84/probe_focus2.py`，真把焦点给到 `#chat-input`，
        五套主题各一次。夹具默认停在登录页，先摘掉 `#console-shell.hidden` 再
        `switchTab('dsh')`——否则输入框的祖先 `#panel-dsh` 是 `display:none`，
        `focus()` 静默失败，`activeElement` 留在 BODY，`:focus-within` 永不匹配）：

            glass     .chat-field  rgba(255,255,255,.98) 0 0 0 1px,
                                   rgba(255,255,255,.80) 0 0 16px,
                                   rgba(20,28,48,.1) 0 1px 4px, rgba(20,28,48,.14) 0 4px 16px
            paper     .chat-field  #ffffff 0 0 0 1px,
                                   rgba(255,255,255,.95) 0 0 16px, 同款深投影
            midnight  .chat-field  rgba(255,255,255,.60) 0 0 0 1px,
                                   rgba(255,255,255,.22) 0 0 16px, …

        失焦时卡片只剩 `--shadow-float`（白边与白晕都不在），输入框 border 0、
        box-shadow none——同一次实测。
        """
        inp = self._top_rule(".chat-input")
        self.assertEqual("0", self._decl(inp, "border"),
                         "输入框自己的边框必须撤掉：它就是用户说的「内嵌框」。"
                         "⚠️ 判据走 `_decl`——这条规则的理由里正写着 `border: 1px solid`，"
                         "按原文搜会命中注释（本文件第 6 次踩这个坑）")
        cancel = self._top_rule(".chat-input:focus")
        self.assertEqual("none", self._decl(cancel, "box-shadow"),
                         "通用焦点规则会给 textarea 一圈 3px 环，必须在卡片里抹掉")
        bare = re.sub(r"/\*.*?\*/", "", cancel, flags=re.S)
        self.assertNotIn("border", bare,
                         "这条只准取消焦点环：顺手改边框会把卡片那一圈也改掉")
        card = self._top_rule(".chat-field:focus-within")
        self.assertIn("var(--focus-halo-edge)", card, "t84：焦点信号是一圈白边，画在卡片上")
        self.assertIn("var(--focus-halo-glow)", card, "再来一层白晕，它才是「光晕」那半句")
        self.assertIn("var(--shadow-float)", card,
                      "t78：卡片有了投影之后，白光要**叠在投影之上**——写成单值会把 "
                      "elevation 抹掉，点进去的瞬间卡片会「掉」在页面上")
        self.assertNotIn("var(--focus-ring)", card,
                         "t84：蓝环整条撤掉。白光与焦点蓝说的不是一件事——前者是「光标在这里」，"
                         "后者跨主题跟着重点色走；混用会让白光在某次换档之后变成品红")
        layers = [card.find(x) for x in ("var(--focus-halo-edge)", "var(--focus-halo-glow)",
                                         "var(--shadow-float)")]
        self.assertEqual(layers, sorted(layers),
                         "图层先写的在上面：白光排在 --shadow-float 后面会被两层深色投影压暗，"
                         "症状是白晕在卡片左右看得见、正下方是灰的")
        # t91：那一圈环不只 textarea 有。`.select:focus` 用的是 `:focus`（鼠标点一下
        # 就命中），于是点开模型下拉时卡片里会冒出蓝描边 + 蓝光晕——同一个内框，
        # 只是换了控件。用户原话：「模型选择时去掉蓝色边框」。
        # t100：这一格换成自绘 chip 之后，`:focus` 与 `:focus-visible` 写在**同一条
        # 分组规则**里（键盘 Tab 与鼠标点开走的是两个伪类，只抹一个，另一次就会冒出环）。
        # 判据不写正则里的换行（换行在源码里不好盯）：先找到 `:focus-visible` 那一行，
        # 再看它紧前面 200 个字符里有没有 `:focus,`。
        at = self.css.find(".chat-model-btn:focus-visible {")
        self.assertGreaterEqual(at, 0, "chip 的焦点规则里必须有 `:focus-visible`")
        near = self.css[max(0, at - 200):at]
        self.assertIn(".chat-model-btn:focus,", near,
                      "两个伪类必须写在同一条分组规则里：分两条写，改一处会漏另一处")
        model = self._top_rule(".chat-model-btn:focus-visible")
        self.assertEqual("none", self._decl(model, "box-shadow"),
                         "模型选择器自己的焦点环必须抹掉：这一格的焦点信号归卡片白光，"
                         "留下它就是一个只在点开下拉那一刻可见的蓝框")
        model_bare = re.sub(r"/\*.*?\*/", "", model, flags=re.S)
        self.assertNotIn("border", model_bare,
                         "只准取消环：这一格 `border: 0`，改 border-color 看不见却会让"
                         "下一个人以为它在起作用")
        self.assertIn("outline: none", model_bare,
                         "chip 是 `<button>`：不抹 `outline` 的话，点开菜单时字外面会"
                         "多一圈默认描边（原生 select 没有这一圈）")
        self.assertNotIn("var(--focus-ring)", model_bare, "蓝环整条撤掉，不要换一种写法留着")

        for selector in (":root", ':root[data-theme="glass"]', ':root[data-theme="paper"]'):
            block = self._top_rule(selector)
            edge = self._decl(block, "--focus-halo-edge")
            glow = self._decl(block, "--focus-halo-glow")
            self.assertTrue(edge and glow,
                            "%s 必须同时定义白边与光晕两个令牌（缺一个就会有一层退回蓝色）"
                            % selector)
            for name, value in (("edge", edge), ("glow", glow)):
                self.assertRegex(value, r"^(?:rgba\(255,\s*255,\s*255|#ffffff)",
                                 "%s 的 %s 不是白的：%s——白光一旦带别的色相，"
                                 "「换个重点色试试」就会暴露" % (selector, name, value))

    # ------------------------------------------------------- 材质（磨砂）

    def _material_rules(self):
        """材质层那两条规则（外壳 / 卡片）的 (选择器, 正文)。

        判据是"正文里**用了**材质令牌"（`backdrop-filter: blur(var(--glass…)`），不是
        "提到了令牌名"——`--glass-blur: 22px` 那几条**定义**就写在 `:root` 里，
        按名字找会命中整个令牌块（第一版就是这么错的，报错信息长达一屏）。
        """
        out = []
        for m in re.finditer(r"([^{}]*)\{([^}]*)\}", self.css):
            body = m.group(2)
            if "backdrop-filter: blur(var(--glass" in body:
                out.append((" ".join(m.group(1).split()), " ".join(body.split())))
        return out

    def test_the_blur_and_the_edge_travel_together(self):
        """模糊与上边缘高光必须写在**同一条规则**里，一个档一条。

        这两样原先分处两张手写清单，于是清单之间漂了：8 个卡片类
        （finding-card / model-card / theme-card / guide-status-card / notice / stat /
        empty，以及当时已改名的 mode-card）只吃到模糊、没吃到高光。症状是
        **磨砂调到「关」时这些卡片的顶部高光不消失**——用户 2026-10-16 报的
        「磨砂应用所有卡片」就是它。合成一条之后"成对"不再靠人记：多一项两样一起多。

        判据用"高光那串字在整份样式表里出现的次数 == 材质规则的条数"：
        一旦有人为了改某张卡片又把高光拆出去，这条立刻红。
        """
        rules = self._material_rules()
        self.assertGreaterEqual(len(rules), 2, "材质层至少要有外壳与卡片两条规则")
        for sel, body in rules:
            self.assertIn("backdrop-filter: blur(var(--glass", body,
                          "%s 没有模糊" % sel)
            self.assertIn("linear-gradient(180deg, var(--glass-edge) 0, transparent 1px)",
                          body, "%s 没有上边缘高光：两样必须在同一条规则里" % sel)
        stripped = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        edge = "linear-gradient(180deg, var(--glass-edge) 0, transparent 1px)"
        self.assertEqual(stripped.count(edge), len(rules),
                         "上边缘高光只许出现在材质层这几条规则里：拆出去就会再漂一次")

    def test_every_card_that_exists_is_in_the_material_list(self):
        """磨砂要落在**所有卡片**上：UI 里每个 `*-card` 都要在清单里，清单里每一项
        也都必须是真实存在的类名。

        两个方向都会烂，而且都不报错：

          · **漏项**：新加一张卡片忘了登记 —— 它静默地不吃磨砂；
          · **死项**：`.mode-card` —— Agent 预设卡的类名早改成了 `.mode-option`，
            清单没跟上，那张卡**从来没有材质**。这一条断言就是为它写的。

        ⚠️ 覆盖面说清楚：这条只扫 `-card` 命名的家族（那正是本产品的卡片命名法）。
        不叫 `-card` 的卡片（`.notice` / `.stat` / `.job-log` / `.rpt-row` /
        `.dock-chip` / `.plan-item` / `.chat-msg` / `.chat-quick` / `.empty`）由
        `.scratch/t64/inventory.py` 的**运行时清点**覆盖——那是脚本，不进单测：
        单测不跑浏览器。
        """
        material = " ".join(sel + " " + body for sel, body in self._material_rules())
        # **类名只可能从这四处出来**，就按这四处枚举，而不是扫整份源码里的字符串：
        #   · HTML 的 class 属性；
        #   · JS 里 `el('div', 'finding-card')` 的第二个参数（本产品的建节点助手）；
        #   · `node.className = '…'` 与 `node.classList.add('…')`。
        # 前两版分别按"整份源码"和"类名形状的字符串"扫，两次都把 **id** `authz-card`
        # 当成漏登记的卡片（它是 `id="authz-card"` / `node.id = 'authz-card'`）——
        # 按形状过滤拦不住它，只有按写法枚举才拦得住。
        pats = (r"""class\s*=\s*["']([^"']*)["']""",
                r"""\bel\(\s*['"`][a-z0-9]+['"`]\s*,\s*['"`]([^'"`]*)['"`]""",
                r"""\.className\s*=\s*['"]([^'"]*)['"]""",
                r"""\.classList\.add\(\s*['"]([^'"]*)['"]""",
                r"""\bsetAttribute\(\s*['"]class['"]\s*,\s*['"]([^'"]*)['"]""")
        used = set()
        for name in ["index.html"] + _scripts():
            source = _read(name)
            if name.endswith(".js"):
                source = _strip_js_comments(source)
            for pat in pats:
                for value in re.findall(pat, source):
                    used |= set(re.findall(r"\b[a-z][a-z0-9-]*-card\b", value))
        self.assertTrue(len(used) >= 4,
                        "只从 web/ 里扫到 %s 个 *-card 类名，判据本身失效了" % len(used))
        # **扫类名之前先剥注释**：上面那段注释里正写着 `.mode-card / .mode-option`
        # （说明死项），按原文扫会把注释里的名字当成清单项——本文件第 7 次踩这个坑。
        # 差集先算出来再断言，失败消息里才只有名字，而不是几万字符的样式表正文。
        listed = set(re.findall(r"\.([a-z][a-z0-9-]*-card)\b",
                                re.sub(r"/\*.*?\*/", "", material, flags=re.S)))
        missing = sorted(used - listed)
        self.assertEqual(missing, [],
                         "这些卡片不在材质清单里（漏项，不会吃到磨砂）：%s" % missing)
        where = "".join(_read(n) for n in ["index.html"] + _scripts())
        dead = sorted(name for name in listed if name not in where)
        self.assertEqual(dead, [],
                         "材质清单里这些类名在产品里不存在（死项，类名改了没跟着改）：%s"
                         % dead)



    def test_the_directory_entries_live_inside_the_input_box(self):
        """两颗目录入口在**输入框内部**的左下角，展开的内容在输入框正上方。

        入口原先和内容同处一个坞里、一起挂在输入框上方：占掉一整条高度，而它平时
        是收起的——那条高度只换来两颗按钮。搬进输入框之后，手不用离开输入框。

        这条钉四件事，每一件错了都**不报错**、只是长得不对：
        1. 两颗按钮既排在 `<textarea>` 之后、又和它在**同一个容器**里。只满足后者
           的话按钮会掉到输入框外面去；`#chat-tools` / `#chat-skills` 这两个 id 是
           JS 与夹具按名取用的，改名不会有任何提示；
        2. **t61：模型/权限/发送那一行也搬进了同一个容器**（`.chat-actions` 原先在
           `.chat-compose` 里、是卡片的**下一个兄弟**）。参考图里那一排控件与输入框
           同处一张卡片，所以它现在是 `.chat-field` 的最后一个子元素 —— 判据是
           "`#chat-send` 之后紧跟 `</button></div></div>`"（发送 → 动作行 → 卡片）。
           ⚠️ 这一条**必须**钉住：搬回去只会让卡片少了一行、多出一行控件浮在卡片外，
           而 CSS 里没有任何一处会因为"少了一个子元素"而报错；
        3. 两颗入口**不再是绝对定位**。t61 之前它们是 `position: absolute`（贴在输入框
           内的左下角，因为那时输入框自己就是卡片），现在它们是卡片那一行里的普通 flex
           项 —— 绝对定位还留着的话，它们会脱出文档流去贴卡片的左下角，盖在 placeholder
           上，而"输入框的下内边距"（原先为它们让位的那 38px）已经一起去掉了；
        4. 内容区 `#chat-quick` 在输入框**之前**（展开时在上方），且卡片样式在
           它自己身上——卡片若留在外层容器，收起时（`display: none` 只作用于
           `#chat-quick`）会剩下一个带边框和背景的空盒子，看起来像"点了没反应"。
           ② t93：它从"卡片之前的兄弟"搬到"卡片里面的第一个子节点"，视觉位置由
           `position: absolute; bottom: 100%` 决定。搬家之前它是被卡片盖住的
           （理由与实测见 style.css 的 `.chat-quick` 那段注释）。
        """
        field_at = self.html.index('<div class="chat-field">')
        tabs_at = self.html.index('class="chat-field-tabs"')
        textarea_at = self.html.index('id="chat-input"')
        actions_at = self.html.index('<div class="chat-actions">')
        self.assertLess(textarea_at, tabs_at, "两颗入口要排在输入框之后")
        self.assertLess(tabs_at, actions_at, "两颗入口要排在动作行之前（同一行，靠左）")
        tail = self.html[self.html.index('id="chat-send"'):]
        self.assertRegex(tail, r"</button>\s*</div>\s*</div>",
                         "动作行必须是 .chat-field 的**最后一个子元素**（t61：控件与输入框"
                         "同处一张卡片）。发送 → 动作行 → 卡片，三层收口缺一不可")
        # 窗口右端跟着 `#chat-send` 走，不写死 `actions_at + 2000`：动作行里的
        # 注释一长（t100 在那里写了模型 chip 的来龙去脉），写死的字数窗口就会把
        # 发送键挤出窗口——判据会红，但代码是对的。
        send_at = self.html.index('id="chat-send"')
        self.assertGreater(send_at, actions_at,
                           "发送键在动作行里（在它之后，不是之前）")
        for node in ('id="chat-input"', 'id="chat-tools"', 'id="chat-skills"',
                     'id="chat-send"'):
            self.assertIn(node, self.html[field_at:send_at + 200],
                          "%s 必须与输入框同处 .chat-field 这一格" % node)
        # t93：坞搬进卡片里（`.chat-field` 的第一个子节点）。它原先排在卡片**之前**，
        # 而卡片用负外边距叠进记录区渲染在上层，坞于是整块被盖住。
        quick_at = self.html.index('id="chat-quick"')
        self.assertLess(field_at, quick_at,
                        "坞要在 `.chat-field` **里面**：留在 `.chat-compose` 里就排在卡片"
                        "之前，被卡片（不透明的 `background: var(--panel-2)`）整块盖住，"
                        "点开只剩卡片上沿外的一条缝")
        self.assertLess(quick_at, textarea_at,
                        "坞排在输入框之前：Tab 序先走展开出来的内容，再到输入框"
                        "（视觉位置由 CSS 的 bottom: 100% 决定，与 DOM 顺序无关）")
        self.assertNotIn('class="chat-dock"', self.html,
                         "外层卡片容器应已撤掉：留着它，「收起」就只剩一个空盒子")
        self.assertNotIn('class="dock-hint"', self.html,
                         "坞里那一行说明在输入框里没有位置可放")
        self.assertIn("position: relative", self._top_rule(".chat-field"),
                      "锚丢了 relative，卡片内的层叠会跑到整页左上角")
        tabs = self._top_rule(".chat-field-tabs")
        self.assertNotIn("position: absolute", tabs,
                         "入口不再是绝对定位：t61 起它们占卡片最后一行的一个位置")
        self.assertNotIn("z-index", tabs,
                         "入口不再需要浮在输入框之上——没有浮层了")
        quick = self._top_rule(".chat-quick")
        self.assertIn("border", quick, "卡片样式要在内容区自己身上，收起才不留空盒")
        self.assertIn("background", quick, "内容区自己要给底色，否则收起态之外都透明")

    def test_the_send_row_puts_the_session_dials_on_the_right(self):
        """发送那一行：模型 / 权限与那颗动作按钮全在右；部署级告警搬到**卡片上方**。

        t78 改了这一格的归属。改版前 `#chat-model` 那条告警排在动作行最左，理由是
        "它是唯一会说「这件事现在不成立」的一句"；但把一句长告警和三个控件塞进同一
        行之后，窄窗口下发送键会被挤出卡片（1436 宽时已经能看见它压在边界外），而
        官方输入区里根本没有这种提示行——它是部署级状态，占页面上的位置。所以它
        搬到了卡片正上方（`.chat-notice`），动作行只剩"这一轮怎么跑、现在发不发"。

        钉两件事：
          1. `#chat-model` **在**卡片里出现是错的，它必须在 `.chat-field` 之前；
          2. 动作行里的顺序仍是 `spacer → 模型 → 权限 → 发送`（`.spacer` 是"靠右"
             的实现，模型/权限必须排在它之后）。
        """
        field_at = self.html.index('<div class="chat-field">')
        model_at = self.html.index('id="chat-model"')
        self.assertLess(model_at, field_at,
                        "#chat-model 的告警必须在卡片**之前**（官方输入区里没有这种"
                        "提示行；留在动作行里会和选择器抢宽度、把发送键挤出卡片）")
        row = self.html[self.html.index('<div class="chat-actions">'):]
        row = row[:row.index("</div>")]
        self.assertNotIn('id="chat-model"', row,
                         "告警不该再出现在动作行里")
        nodes = ['class="spacer"', 'id="chat-model-select"',
                 'id="chat-policy"', 'id="chat-send"']
        for node in nodes:
            self.assertIn(node, row, "%s 必须在 .chat-actions 这一格里面" % node)
        positions = [row.index(node) for node in nodes]
        self.assertEqual(positions, sorted(positions),
                         "发送那一行的顺序应是：spacer → 模型 → 权限 → 动作按钮")

    def test_send_and_stop_are_one_button(self):
        """「发送」与「停止」是**同一颗**按钮：跑着的时候它换名字、换图标、换色，不换位置。

        改版前是两颗（`#chat-send` 圆形图标 + 一颗 `hidden` 开关的 `#chat-stop`），而
        那两个身份本来就是**互斥**的：跑着的时候发不出去。两颗按钮等于给同一件事要两处
        挂点——多出来的那颗平时禁用着（"可以发"是假的承诺），窄窗口下还要和选择器抢那
        一行的宽度。

        钉四件事，每一件错了都不报错、只是长得不对或点不动：

          1. **DOM 里没有第二颗动作按钮**，chat.js 也不许再引用那个 id。留下一个
             `byId('chat-stop')` 是 null，之后每一处 `dom.stop.X` 都会抛——而它抛在
             渲染路径上，表现是"这一块不见了"；
          2. **忙碌态的配色走 danger 那一对成对令牌**（`--tint-danger` + `--text-danger`），
             不是自己配的"红底白字"：`--on-accent` 是 `--accent` 的配对墨色，深色主题里
             它是**近黑**，压到红底上就是深色字压红底（`.btn.danger` 那条规则警告过的）；
          3. **忙碌时那颗按钮不许被禁用**：它是这一轮唯一的刹车。写回 `disabled = locked`
             就是把刹车一起锁上（`locked` 在跑着的时候恒为真），而失败形态是"按钮在、
             点不动"——与"我没点到"长得一模一样；
          4. **点击要分派**（`view.sending` 时走停止）。不分派的话，跑着的时候点它等于
             再发一次，而 `send()` 开头那句 `if (view.sending) return` 会让它**什么都不
             做**：一颗按下去没有任何反应的按钮。
        """
        self.assertNotIn('id="chat-stop"', self.html,
                         "DOM 里又出现了第二颗动作按钮：「发送」与「停止」是同一颗")
        chat = _read("chat.js")
        self.assertNotIn("chat-stop", _strip_js_comments(chat),
                         "chat.js 里还引用着 `chat-stop`：那个 id 已经不存在，取到的是 "
                         "null，后面每一处 dom.stop.X 都会抛")
        rule = self._top_rule(".chat-send.is-stop")
        self.assertIn("var(--tint-danger)", rule,
                      "忙碌态要用 danger 那一对的**底**（成对令牌：浅填充配深字 / 深填充配浅字）")
        self.assertIn("var(--text-danger)", rule, "同上，字也要用与之配对的那一个")
        self.assertNotIn("--on-accent", rule,
                         "忙碌态用 --on-accent 当字色：它是 --accent 的配对墨色，深色主题里"
                         "是近黑——压到红底上就是深色字压红底")
        # 看一眼**剥掉注释**的那一份：这一条里有一个负向断言，而它的理由本身就要引用
        # `disabled = locked` 这串字（"不能写它"）——按原文比就是自证失败。同一坑
        # chat.js 那边也踩过一次（见 LiveStepBadgeTestCase._code）。
        body = _fn_body(_strip_js_comments(chat), "setSending")
        self.assertIn("dom.send.disabled = !busy && !canSend()", body,
                      "忙碌条件被改了：跑着的时候**必须可点**（它是唯一的刹车），"
                      "而空闲且没有模型时仍要禁用（与改版前的发送按钮一致）")
        self.assertNotIn("dom.send.disabled = locked", body,
                         "`locked` 在跑着的时候恒为真——用它禁用这颗按钮，就是把唯一的刹车锁上"
                         "（⚠️ 断言要指名 `dom.send.`：`dom.input.disabled = locked` 是**对的**"
                         "一行，只比 `disabled = locked` 会命中它）")
        # 名字要跟着状态走；**不钉写法**（`setAttribute('title', …)` 与 `.title = …`
        # 对 HTML 元素是同一种结果），钉的是"这两个名字都被重写过"这件事。
        # ⚠️ t99：真正写这两个属性的地方已经挪到 `syncSendName()`——一颗键有三种身份
        # （「发送」/「停止」/「发送所选（N 件）」），面板里那颗键删掉之后件数也只有它能说，
        # 所以把"重写名字"这件事写在 setSending 里反而会漏掉件数。这里看两处合起来的那段。
        naming = body + _fn_body(_strip_js_comments(chat), "syncSendName")
        for field, pattern in (("aria-label", r"setAttribute\('aria-label'"),
                               ("title", r"\.title\s*=")):
            self.assertRegex(naming, pattern,
                             "名字没有跟着状态走：只有图标的按钮，%s 是它唯一的名字——"
                             "念着「发送」而去停止，与点错按钮没有区别" % field)
        bind = _fn_body(chat, "cacheAndBind")
        self.assertIn("view.sending", bind,
                      "点击没有分派：跑着的时候点它必须走「停止」而不是「发送」")
        self.assertIn("stopTurn()", bind, "分派到了停止那一支，但没接上 stopTurn()")

    def test_coming_back_to_the_console_does_not_end_the_running_turn(self):
        """切回「控制台」不能顺手把"正在跑"的那一轮收尾。

        使用者报的是"切换控制台页面，最新会话会消失一下"，实测是**主区最新那条**
        （正在生成的气泡）：跑着的时候切走再切回来，它先没了，过一会儿才回来。
        稳定复现，因为它压在"点页签"这条路径上。

        链条有两节，缺一节这条缺陷都不成立：

          1. `renderLog()` 只在 `view.sending && view.live` 时画那张气泡——`sending`
             一假，页面上最新的一条内容就整个消失；
          2. `ensureLoaded()` 是"点页签"的入口（`cacheAndBind` 把它挂在
             `.tab[data-tab="dsh"]` 上），它原来**无条件** `setSending(false)`：把
             "我从别的页签回来"当成了"这一轮结束了"。

        同一个标志还管着两件不该在跑着的时候发生的事：`syncFromPulse` 会因此整份
        重取当前会话的消息（与 SSE 打架，见那里的注释），输入区也会被解锁（能再发
        一条，而服务端那一轮还占着这个会话）。所以判据用 `view.abort`——它在这一轮
        开始前赋值、`finish()` 里清空，是"有一轮在飞"的唯一真相；没有在跑的轮次才
        清 `sending`，那是登录后重载这类"页面不确定上一轮收没收尾"的兜底。
        """
        chat = _strip_js_comments(_read("chat.js"))
        render_log = _fn_body(chat, "renderLog")
        self.assertIn("view.sending && view.live", render_log,
                      "气泡的显示条件改了：`ensureLoaded` 里那句清状态之所以会让它"
                      "消失，靠的就是这一个判断——条件一变，这条断言守的链条就断了")
        body = _fn_body(chat, "ensureLoaded")
        calls = [line.strip() for line in body.splitlines() if "setSending(false)" in line]
        self.assertEqual(len(calls), 1,
                         "`ensureLoaded` 里清 sending 的地方不止一处或不见了：现在只有"
                         "一处，且必须带 `view.abort` 判据")
        self.assertIn("view.abort", calls[0],
                      "切回页签时又无条件清 `view.sending`：正在生成的那条会先消失"
                      "（直到下一帧增量才画回来；若正卡在工具调用里就一直不在），"
                      "输入区与消息重取也会按'这一轮结束了'来")

    def test_the_send_row_selects_can_shrink(self):
        """选择器要**真的**能收缩，否则窄窗口下「发送」会被顶到第二行。

        通用控件那一档给的是 `min-width: 230px`，而 CSS 里 **min-width 赢
        max-width**：只写 `max-width: 200px` 是个空操作——选择器牢牢占住 230px，
        两个加起来 460px 再加标签已经吃掉半行。这条把它钉住。

        封顶还要**装得下最长的那条标签**，并且要有 `text-overflow`：200px 时渲染
        出来是「跟随全局启用项（流式演示模」——少了最后一个字，而
        `scrollWidth > clientWidth` 查不出来（`<select>` 的 clientWidth 不含原生
        箭头预留的宽度）。这条缺陷只有把图渲染出来才看得见，所以数是从渲染结果
        反推的：180（标签）+ 20（箭头）+ 22（左右内边距）= 222。
        """
        rule = self._top_rule(".chat-actions .select")
        self.assertIn("min-width: 0", rule,
                      "min-width 会压过 max-width：不显式归零，上面那句封顶形同虚设")
        m = re.search(r"max-width:\s*(\d+)px", rule)
        self.assertIsNotNone(m, "封顶哪去了？没有它选择器会占满整行")
        self.assertGreaterEqual(
            int(m.group(1)), 222,
            "封顶要装得下最长的那条占位符：180px 的文字 + 原生箭头约 20px + 左右内边距 "
            "22px —— 小于这个数就会把最后一个字吃掉，而且只体现在渲染结果上")
        self.assertIn("text-overflow: ellipsis", rule,
                      "被切时要看得见省略号：静默少一个字看起来像标签写错了")

    def test_the_send_row_selects_are_transparent_but_their_popup_is_not(self):
        """两个下拉自己**不铺底**；而它们的**弹出列表必须铺**。

        控件透明与列表透明默认是同一件事，这一点反直觉：`option` 的底色本来就
        `transparent`，Chrome 的列表用**行**的底色（拿不到才退回控件那一层）。所以
        "把控件改透明"这一步如果不配 `option` 规则，弹出的列表会跟着变透明——选项
        文字直接叠在页面文字上。实测（无头浏览器读 computed style）：改之前两个
        主题里 `option` 的 background 都是 `rgba(0, 0, 0, 0)`。

        列表的底取 `--bg` 而不是 `--panel*`：后者在液态玻璃下是 rgba（那套材质就是
        建立在半透明上，由该主题段的断言守着"不能改成不透明"），而弹出列表必须是
        实底。hover / 选中行同理，且用的是产品里已有的成对令牌（浅填充配深字 /
        深填充配白字），不另编颜色。
        """
        rule = self._top_rule(".chat-actions .select")
        self.assertIn("background-color: transparent", rule,
                      "两个下拉与输入框同为「没有面」的一档：填色撤掉、只留描边。"
                      "（万一要把底色加回来，必须同步删掉下面这几条 option 规则里的"
                      "理由——那三段注释是互相引用的。）")
        # 裸 `option` 必须锚在行首：`_top_rule` 用的是 `re.search`，而 `.mode-option {`
        # 里也含子串 "option {"，会先命中——那是另一件事（Agent 预设卡片）。
        m = re.search(r"(?m)^option\s*\{([^}]*)\}", self.css)
        self.assertIsNotNone(m, "找不到裸 option 规则（弹出列表的行谁给底？）")
        base = " ".join(m.group(1).split())
        self.assertIn("background-color: var(--bg)", base,
                      "弹出列表的行要有**实**底：--panel 系在液态玻璃下是半透明的")
        self.assertIn("color: var(--text)", base, "行文字要跟着主题走")
        for sel, fill, ink in (("option:checked", "--accent-dim", "--text-accent"),
                               ("option:hover", "--accent", "--on-accent")):
            m = re.search(r"(?m)^%s\s*\{([^}]*)\}" % re.escape(sel), self.css)
            self.assertIsNotNone(m, "找不到 %s 规则（那一行会落回系统高亮色）" % sel)
            body = " ".join(m.group(1).split())
            self.assertIn("background-color: var(%s)" % fill, body,
                          "%s 的底要用成对令牌里的填充色" % sel)
            self.assertIn("color: var(%s)" % ink, body,
                          "%s 的文字要用与之配对的前景色——只给底不给字，就是"
                          "深色字压在蓝底上" % sel)

    def test_the_composer_holds_its_height_so_the_send_row_stays_inside_the_window(self):
        """模型 / 权限 / 发送那一行**必须落在窗口里**——用"不让这一条被压"来保证。

        这条契约翻过两次面，每次都留着量测，因为"该让谁缩"这件事只能靠数字定：

        * t56（`.scratch/t56/probe_selects.py`）：`.chat-compose` 原本 `flex: 0 0 auto`，
          窗口 1440×700 把输入框拉到 520px 时那一行跑到窗口下面 → 改成 `0 1 auto`。
          **那时输入框可以拖大**（`resize: vertical`），高度没有上界，所以"能压"是对的。
        * t57（`.scratch/t57/probe_input.py`）：可压之后压缩全落在 `.chat-field` 上，
          它被压穿、textarea 溢出重叠 —— 于是给卡片补了下限（现在那条下限还多了
          "目录行 + 动作行"两段，见 `--chat-field-floor`）。
        * **t82（本轮的返工）**：输入框 `resize: none`、下限 4 行、内容长了自动长高到
          336px 封顶（`chat.js` 的 `INPUT_MAX_HEIGHT`）——这一条的内容高度从此**有
          上界**；而它仍然是 `0 1 auto`，于是被压。量过（1440×900，详情栏
          开着、聊天列 414px）：这一条拿到 243px，卡片自己的下限是 204px，卡片从它下面
          伸出去——发送圆钮的底边落在 917px，比窗口底还低 17px，右侧详情栏把它盖住。
          改成 `0 0 auto` 之后：这一条 301px，记录区让到 437px（它 `overflow-y: auto`，
          天生就是用来缩的那一个），卡片底边 860px，整行在窗口里。

        所以现在钉的是：**这一条不许被压缩**；该让路的记录区自己会让。
        代价写在 `web/style.css` 那段注释里：窗口矮到 470px 以下时这一条会把底部塞出
        视口——那是"4 行输入框 + 一排控件"的硬下限，比压缩之后**切掉发送键**好。
        """
        compose = self._top_rule(".chat-compose")
        # 声明上的断言一律**先剥注释**：这一段的注释正解释着 `flex: 0 1 auto`、
        # `min-height: auto` 与 `0` 为什么被否掉，用原文比会命中注释里的那几串字。
        # （本文件第四次踩这个坑，理由见 test_the_wordmark_has_no_text_stroke。）
        bare = re.sub(r"/\*.*?\*/", "", compose, flags=re.S)
        self.assertIn("flex: 0 0 auto", bare,
                      "这一条不许被压：输入框不能再被拖大（`resize: none`；下限 4 行、"
                      "自动长高到 336px 封顶），压缩只会把卡片顶出窗口（t82 量测见 docstring）")
        self.assertNotIn("flex: 0 1 auto", bare,
                         "`0 1 auto` 就是 t82 修掉的那一版：卡片底部会被推出视口")
        self.assertIn("min-height: calc(", bare,
                      "这一条的下限必须显式算出来：`0` 会被压穿（子节点溢出→被 body 裁），"
                      "`auto` 会把目录的 max-height 算进去（391px→整条出窗口）")
        self.assertNotIn("min-height: 0", bare, "`0` 就是被压穿的那一版")
        self.assertNotIn("min-height: auto", bare, "`auto` 就是 391px 的那一版")
        # 下限里**不再含坞的那一段**（t93）。坞已脱出文档流（贴卡片上沿的浮层），
        # 不占布局，` :has(> .chat-quick …)` 那条规则于是只剩"白占 184px + 12px"，
        # 而它正是矮窗口下把卡片顶出视口的那一段（量过：808 高的窗口里 compose
        # 因此涨到 660px、记录区塌到 10px）。断言"它不许回来"——因为一旦回来，
        # 症状只在矮窗口里出现，而那时没人会想到是这里。
        no_comments = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        self.assertNotIn(":has(> .chat-quick", no_comments,
                         "坞不再占布局，compose 的下限里不许再给它留一段："
                         "那一段在矮窗口下会把卡片连同动作行一起顶出视口")
        self.assertIn("--chat-input-floor", self._top_rule(".chat-main"),
                      "下限变量必须定义在 `.chat-compose` **或它的祖先**上：`:has()` 那条"
                      "规则引用它，而自定义属性只向下继承——挪到 `.chat-field`（后代）会让"
                      "那条 calc 变成无效声明并被整条丢弃，症状是**展开态又出窗口**，且不报错。\n"
                      "⚠️ t63 起它住在 `.chat-main`：记录区也要用卡片的高度（见 `.chat-main`）")
        self.assertNotIn("--chat-input-floor:", bare,
                         "一个数只写一处：搬家之后不许两边各留一份（改一处不改另一处时，"
                         "两条下限会各自漂，而分开看都没错）")
        self.assertIn("var(--chat-field-floor)", bare,
                      "t61 起 compose 的下限引用**卡片**那一层：动作行现在在卡片里面，"
                      "再单独加一次就是算重（矮窗口下白占 32px）")
        self.assertIn("var(--chat-overlap)", bare,
                      "卡片用负外边距叠进记录区，实际占的高度就少那一段——"
                      "下限不减它的话，矮窗口下会从记录区身上多抠 20px")
        # 坞从"链上唯一该让路的一项"变成**卡片上沿的浮层**（t93）。
        # 三条一起钉：脱出文档流（不然被卡片盖住）、贴在卡片上沿（bottom: 100%
        # 而不是 JS 量高度，卡片怎么长高都跟得上）、不再有 flex 那两条
        # （有 `flex` 就说明它又回 `.chat-compose` 的流里了）。
        quick = self._top_rule(".chat-quick")
        quick_bare = re.sub(r"/\*.*?\*/", "", quick, flags=re.S)
        self.assertIn("position: absolute", quick_bare,
                      "坞必须脱出文档流：留在流里它就排在卡片**之前**，被卡片的负外边距"
                      "整块盖住——真页面实测（elementFromPoint）坞的顶/中/底三点全部"
                      "命中卡片里的 textarea，用户只看得见卡片上沿外面一条缝")
        self.assertIn("bottom: 100%", quick_bare,
                      "贴住卡片上沿。用 bottom: 100% 而不是 JS 量卡片高度：卡片随输入框"
                      "长高、随选项面板变高，量出来的数一定会过期")
        self.assertNotIn("flex: 0 1 auto", quick_bare,
                         "`flex: 0 1 auto` 是 t57 那版（坞在 `.chat-compose` 的流里让路）："
                         "它现在不参与布局，这条写回来就意味着坞又回到了卡片下面")
        actions = self._top_rule(".chat-actions")
        # t82：这一行**宽度可让、高度不让**。旧契约是 `flex: 0 0 auto`（"不许被压"），
        # 但那把**宽度**也钉住了 = 内容宽 465px；卡片内容宽在 1440 下只有 390px，
        # 于是整行溢出卡片（发送键落在卡片外面）。现在让出来的是模型名的省略号。
        # ⚠️ t88：契约**反过来了**。t82 把这一行钉成 `flex: 1 0 100%`（独占一行），
        # 用户这次的要求正好是「输入框下面几个选项并成一行」。
        # 这里用 `_decl` 而不是 `assertIn`：`_top_rule` 的正文含注释，而这三种写法
        # 现在**同时**散落在注释里（"t82 那一轮改成 `flex: 1 0 100%`"），
        # `assertIn("flex: 1 0 100%", actions)` 会读到自己写的注释而**永远为真**——
        # 这条断言在改版后曾经"绿着"却什么都没守（见 `_decl` 的 docstring）。
        self.assertEqual(self._decl(actions, "flex"), "0 1 auto",
                         "动作行与目录入口**并排**：`1 0 100%` 是独占一行（t82），"
                         "`0 0 auto` 会把宽度钉在内容宽 465px、1440 下把发送键挤到卡片外")
        self.assertEqual(self._decl(actions, "min-width"), "0",
                         "不写 min-width: 0，flex 项的自动最小宽度（内容宽）会挡住收缩")
        self.assertIn("min-height: var(--chat-action-row)", actions,
                      "高度仍然钉着——这一行的高度也要有名字，卡片的下限引用了它")
        self.assertIn("flex-wrap: nowrap", actions,
                      "折行会把第二行压在 --chat-action-row 这个下限之外，顶出卡片")
        # 先剥注释再看规则：这段代码里的注释正引着那条被禁的选择器。
        # （本文件第三次踩这个坑，理由见 test_the_wordmark_has_no_text_stroke。）
        stripped = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        self.assertIsNone(re.search(r"(?m)^\.chat-compose\s*>\s*:not\(", stripped),
                          "「除输入框外都不许压」那条合并规则**不许回来**：它把目录也"
                          "钉成不可压缩，压缩于是全落在 .chat-field 上——而那一层挡不住"
                          "子元素溢出，界面坏成「输入框显示不全」，这条用例却是绿的")
        field = self._top_rule(".chat-field")
        self.assertIn("flex: 0 1 auto", field,
                      "这一层**可缩但不可长大**：写 `1 1 auto` 时卡片会填满 compose"
                      "的内容盒，下限里多算一个像素、卡片就高一个像素——症状是动作行"
                      "被顶出视口（量过：182 vs 148）")
        self.assertIn("min-height: var(--chat-field-floor)", field,
                      "t61 起它的下限是**卡片两行的高度和**，不再是输入框那一段："
                      "动作行搬进来之后，只按输入框算会让卡片在矮窗口下裁掉自己那一行")
        inp = self._top_rule(".chat-input")
        self.assertIn("min-height: var(--chat-input-floor)", inp,
                      "输入框与卡片共用**同一个输入框下限变量**：抄两份数字正是上一轮的"
                      "缺陷（手填 118px，而 4 行 + 上下内边距是 116px，差的那 4px 切掉了"
                      "第 4 行）")

    def test_the_dock_matches_the_card_and_shows_exactly_one_row_of_cards(self):
        """坞（能力 / 技能 的弹层）：宽度 = 卡片宽，高度 = 一行卡片（t96）。

        用户两次原话：「可用能力、技能手册弹出框长度和输入框保持一致」「高度进[仅]
        能够显示 1 行卡片」。

        宽度（`left/right: 0`）：绝对定位的包含块是 `.chat-field` 的 **padding
        box**，而卡片 `border: 0`，所以它的左右边**就是**卡片外沿。t93 写的是
        `calc(-1 * var(--sp-3))`（各外扩 12px），本意是「抵消卡片的左右内边距」——
        要抵消的那东西不存在，坞于是比卡片宽 24px。实测
        （`.scratch/t96/probe_dock.py`，夹具用真实 style.css + 真实 index.html 的
        整块 `.chat-field`，230~870 七档卡片宽）：
            改前 卡片 838 / 坞 862，左右间隙各 -12.00
            改后 卡片 838 / 坞 838，左右间隙各 0.00（七档全为 0）

        高度：一行的高度必须是**具名令牌**，而不是又手填一个数——它与
        `grid-auto-rows` 共用同一个 `--dock-chip-h`，于是「看得见几行」由算式决定：
            `2 × --sp-2(8) + 2px(上下描边) + 94 = 112`
        实测 坞 112 / 内容 94：第一行三张卡片**完整可见**，第二行**一张都没露出**
        （部分可见 = 0），卡片内容的最高 scrollHeight 是 92 ≤ 94（没有被裁）。

        行间距为什么必须等于内边距：尺子量不出来，但它是「不露半张」的必要条件——
        第二行上边 = 上内边距 + 行高 + 行间距；封顶线 = 上内边距 + 行高 + 下内边距。
        两个 8 相等时封顶线正好压在第二行上边（可见 0px）；行间距若是 6，会留下 2px
        的一条——那一条就是「半张卡」的缝，而它只在第一行之外还有内容时才出现。
        """
        quick = self._top_rule(".chat-quick")
        bare = re.sub(r"/\*.*?\*/", "", quick, flags=re.S)
        self.assertEqual(self._decl(quick, "left"), "0",
                         "坞的左沿贴卡片外沿：包含块就是卡片的 padding box，"
                         "`calc(-1 * var(--sp-3))` 会把它撑到卡片外面 12px")
        self.assertEqual(self._decl(quick, "right"), "0", "同上，右沿")
        self.assertNotIn("calc(-1 * var(--sp-3))", bare,
                         "「抵消内边距」那条写法不许回来：要抵消的对象并不存在")
        self.assertEqual(self._decl(quick, "--dock-chip-h"), "94px",
                         "一行卡片的高度要有名字：`grid-auto-rows` 与封顶共用它，"
                         "手填两处迟早只改一处（改完的症状是「第一行被切一条」）")
        self.assertEqual(
            self._decl(quick, "max-height"),
            "min(calc(2 * var(--sp-2) + 2px + var(--dock-chip-h)), 40vh)",
            "封顶 = 上内边距 + 一行卡片 + 下内边距 + 上下描边；写死 184px 就回到三行")
        self.assertNotIn("184px", bare,
                         "184px 是旧的三行封顶：它一回来坞就重新长高，"
                         "而「一行」这件事没有任何别的用例会失败")
        self.assertEqual(self._decl(quick, "gap"), "var(--sp-2)",
                         "行间距等于内边距，封顶线才正好压在第二行上边（见 docstring）")
        mode = re.search(r'\.chat-quick\[data-dock="tools"\][^{]*\{([^}]*)\}',
                         self.css, re.S)
        self.assertIsNotNone(mode, "展开态的规则不见了（`[data-dock=tools]`）")
        mode_body = " ".join(mode.group(1).split())
        self.assertEqual(self._decl(mode_body, "grid-auto-rows"), "var(--dock-chip-h)",
                         "每一行都要钉成一行卡片的高度：不钉时行高由该行内容决定"
                         "（实测 72.7 / 90.3 两种），封顶算式只对最矮的那种成立——"
                         "第一行是高的那种时，下沿被切掉一截")
        fill = self._top_rule(".dock-chip-fill")
        self.assertEqual(self._decl(fill, "white-space"), "nowrap",
                         "「点一下填」那一行必须压成一行：折成两行卡片就从 72.7 长到 "
                         "90.3，于是要么切掉它、要么把行高令牌涨上去（封顶就把第一行切了）")
        self.assertEqual(self._decl(fill, "text-overflow"), "ellipsis",
                         "截断必须是**声明式**的：末尾出现省略号，完整那句在 title 里")
        self.assertEqual(self._decl(fill, "align-self"), "stretch",
                         "列向 flex 容器 `align-items: flex-start`，不拉伸这一格的宽度"
                         "等于文字宽，`text-overflow` 就没有可裁的边界（文字会溢出边框）")

    def test_the_input_floor_is_derived_from_the_rows_it_promises(self):
        """输入框的下限 = `rows="1"` 的 1 行 + 上下内边距（t78 起只有一行起长）。

        每一段数各有出处，**一段都不许手填**：
          · `1`    ←→ index.html 的 `rows="1"`（跨文件；CSS 读不到，只能两边对）
          · `1.5`  ←→ `.input` 的 `font: var(--fs-small)/1.5`（无单位行高倍数）
          · `--sp-3`（两段）←→ `.chat-input` 的 `padding: var(--sp-3) var(--sp-4)`
          · **t64 起没有第三段了**：原先还有 `2px` ←→ 输入框自己的上下边框。用户
            要求"去掉内嵌框"之后 `.chat-input` 的边框撤了，那段必须一起去掉——留着
            的话下限比自然高度高 2px，多出来的 2px 变成输入框里的一小段空白（症状
            与下面那两次完全相同）。
        量出来的自然高度（`--fs-small: 12px`、`--sp-3: 12px`）是
        `24 + 4×18 = 96px`。**这个算式我少写过两段**，两次的症状一样：
        手填 118px 时能放文字的高度只剩 68px；算式里漏掉边框时是 120px——
        两次都读到「可显示 3.89 行」，第 4 行被切掉一截，而那就是用户说的「显示不全」。
        只看高度数字看不出来，得看行数（`.scratch/t57/probe_input.py` 会打印）。

        ⚠️ **t61：算式里的第三段换过一次**。原先它是 `--chat-chip-band`（38px）——
        给"浮在输入框左下角的两颗入口"让位的下内边距。那两颗入口现在占卡片自己的最后
        一行，那一段连同 `padding-bottom` 一起去掉。所以这里除了钉算式，还钉一条
        **不许回来**：`--chat-chip-band` 与那句 `padding-bottom` 若回来，输入框会永久
        多出 26px 空白，而症状只是"第 4 行下面空一截"，没人会想到去查它。
        """
        row = re.search(r"(?m)^\s*--fs-small:\s*([0-9.]+)px", self.css)
        self.assertIsNotNone(row, "找不到 --fs-small：下限的算式靠它")
        pad = re.search(r"(?m)^\s*--sp-3:\s*([0-9.]+)px", self.css)
        self.assertIsNotNone(pad, "找不到 --sp-3：下限的算式靠它")
        # t63：这几个 floor 变量搬到了 `.chat-main`（记录区也要用卡片的高度，而自定义
        # 属性只向下继承、兄弟之间看不到彼此），算式本身一个字符没改。
        compose = self._top_rule(".chat-main")
        # 同上：这段注释里也写着 `min-height: auto` 那几个被否掉的写法。
        bare = re.sub(r"/\*.*?\*/", "", compose, flags=re.S)
        self.assertIn("--chat-row: calc(1.5 * var(--fs-small))", bare,
                      "行高倍数必须与 `.input` 的 `font: .../1.5` 一致")
        self.assertIn("--chat-input-floor: calc(4 * var(--chat-row) + 2 * var(--sp-3))",
                      bare,
                      "下限要由两段拼出来（行数 + 上下内边距），不能是个数——它同时是"
                      "`.chat-field` 下限的一段，抄两份数字正是 t57 的缺陷。"
                      "t80 起起点抬到两行（用户要求「对话框的高度有点低」），t82 再抬到"
                      "**四行**（用户要求「输入框高度增加」）：下限只保证空框也占这么高，"
                      "不影响跟着内容长的那条上限")
        self.assertIn("--chat-field-floor: calc(var(--sp-2) + var(--sp-1)"
                      " + var(--chat-input-floor)", bare,
                      "卡片的下限 = 自己的上下内边距 + 输入框下限 + 目录行 + 动作行"
                      "（t61 起动作行在卡片里面，所以这一段必须出现在**卡片**的下限里；"
                      "t82 补上目录行——目录入口与动作行是两个 flex 行，只算一段时"
                      "动作行会从卡片底边溢出去）")
        self.assertIn("--chat-dock-row: 28px", bare,
                      "目录行的高度要有名字：卡片下限与 .chat-actions 都引用它")
        self.assertNotIn("--chat-chip-band", bare,
                         "给绝对定位胶囊让位的那一段**不许回来**：入口已经搬进卡片"
                         "自己的最后一行，留着它就是输入框永久多 26px 空白")
        self.assertIn("--chat-frame: calc(var(--sp-3) + var(--sp-4) - var(--chat-overlap))",
                      self._top_rule(".chat-compose"),
                      "这一条自己的下限算式别丢：它曾经被一段**没有开头的注释**吞掉"
                      "（那句正文里有悬空的注释收尾符，解析器按错误恢复一路吞到下一个"
                      "ASCII 分号，而前面那些分号都是全角 `；`）。后果是 min-height 在"
                      "计算值阶段无效、退回 `auto` —— 正是 t57 花整轮修掉的那一版。\n"
                      "量过（.scratch/t63/probe.py）：修复前 computed min-height = `auto`，"
                      "修复后 = `28px`")
        m = re.search(r'id="chat-input"[^>]*\srows="(\d+)"', self.html)
        self.assertIsNotNone(m, "找不到 #chat-input 的 rows —— 算式里的乘数就是它")
        rows = int(m.group(1))
        self.assertGreaterEqual(rows, 2,
                                "t80：起点至少两行（用户要求「对话框的高度有点低」）")
        # 乘数不再钉死：t80 把起点从 1 行抬到 2 行，这里改为**两边必须相等**——
        # 算式与 `rows` 各写一个数才是 t57 那类缺陷（改了 HTML 忘了算式）。
        mul = re.search(r"--chat-input-floor: calc\((\d+) \* var\(--chat-row\)", bare)
        self.assertIsNotNone(mul, "下限算式里要有一个行数乘数")
        self.assertEqual(int(mul.group(1)), rows,
                         "算式里的乘数必须等于 index.html 承诺的行数")
        # 自然高度 = 上下内边距 + `rows` 行（每行 1.5 × --fs-small）。
        # ⚠️ t82：这里原先只加**一行**的行高（`1.5 * --fs-small`），rows=2 时靠
        # "那一行 + 余量"碰巧过线；rows 抬到 4 就露馅了——它想守的是"下限装得下
        # 承诺的行数"，所以乘数必须是 rows。
        natural = 2 * float(pad.group(1)) + rows * 1.5 * float(row.group(1))
        self.assertGreaterEqual(natural, rows * 1.5 * float(row.group(1)),
                                "下限至少要装下承诺的行数")
        inp = self._top_rule(".chat-input")
        self.assertNotIn("padding-bottom", inp,
                         "输入框的下内边距回到与上内边距同一条 `padding` 简写上："
                         "t61 之后下面没有浮层，单独一行 padding-bottom 就是残留")

    def test_the_send_row_does_not_repeat_the_global_model_name(self):
        """全局启用项的名字**只印一处**：模型选择器的占位符。

        那一行左边原来还有一条状态字，写着「DeepSeek · deepseek-flash 全局启用项」
        ——和紧挨着它的占位符「跟随全局启用项（DeepSeek · deepseek-flash）」是同一
        句话的两种写法。删掉名字那半之后，另一半必须还在：**"一个模型都不存在"**的
        告警 + 「去设置」。那种状态下下拉是空的，而空下拉既不解释原因、也不给路；
        这句话在任何别的地方都没有第二份。
        """
        js = _read("chat.js")
        # t100：这个名字现在**该在**——chip 上那一行字就是它（`syncModelChip` 从原生
        # select 的选中项抄过来）。要守的仍是原来那件事：名字只有**一个**出处。
        # 判据改成"告警格是空的"：`#chat-model` 只装"一个模型都没有"那句话，
        # 名字若被写回这里，就又是两份。
        self.assertRegex(self.html, r'id="chat-model"\s*>\s*</div>',
                         "`#chat-model` 必须是空节点：名字只印在 chip 上（一处）")
        self.assertIn("syncModelChip", js,
                      "chip 上的字由 `syncModelChip` 从原生 select 抄，不许另写一份")
        name = _fn_body(js, "syncModelChip")
        self.assertIn("selectedIndex", name,
                      "字必须取**选中那一项**（抄 select 的现状），不能另外拼一句")
        self.assertIn("'chat-model-missing'", js,
                      "「一个模型都不存在」的告警必须留着——它在别处没有第二份")
        self.assertIn("switchTab('settings')", js,
                      "告警必须带出「去设置」那条路，只报错不给路等于拦人不放行")

    def test_deployment_console_reads_setup_contract(self):
        """「部署」页签必须真的消费自检接口的字段，而不是画几张静态卡片。"""
        for field in ("checks", "commands", "executorId",
                      "baselines", "counts", "auth"):
            self.assertIn(field, self.js,
                          "部署控制台未使用自检/接入字段：%s" % field)
        self.assertIn("X-Enroll-Token", self.js,
                      "注册凭据必须能作为请求头带上（否则配了 enroll-token 就用不了）")
        self.assertIn("X-Enroll-Token", self.html + self.js)
        self.assertIn("type=\"password\"", self.html,
                      "注册凭据输入框应为 password，避免肩窥")

    def test_settings_console_reads_model_contract(self):
        """「设置」页签必须真的消费模型与用户接口的字段。"""
        for field in ("presets", "apiKeySource", "activate", "latencyMs",
                      "hasInlineKey", "profiles"):
            self.assertIn(field, self.js, "设置页未使用模型字段：%s" % field)
        self.assertIn("X-CSRF-Token", self.js,
                      "写操作必须带 CSRF 头，否则全部会被 403 拒绝")
        for field in ("username", "role", "disabled", "lastLoginAt"):
            self.assertIn(field, self.js, "用户管理未使用字段：%s" % field)

    def test_no_debug_leftovers(self):
        for name in self.scripts:
            for keyword in ("console.log", "TODO", "FIXME", "待补"):
                self.assertNotIn(keyword, self.sources[name],
                                 "%s 中存在调试残留：%s" % (name, keyword))
        for name, source in (("index.html", self.html), ("style.css", self.css)):
            for keyword in ("TODO", "FIXME", "待补"):
                self.assertNotIn(keyword, source,
                                 "%s 中存在调试残留：%s" % (name, keyword))

    def test_html_escapes_untrusted_data(self):
        """接口数据不得直接拼进 innerHTML。"""
        for name in self.scripts:
            unsafe = re.findall(r"innerHTML\s*=\s*[^;]*\$\{", self.sources[name])
            self.assertEqual(unsafe, [],
                             "%s 的 innerHTML 拼接了模板变量，存在 XSS 风险" % name)

    def test_graph_uses_server_layout(self):
        """前端不得自行计算布局——布局由服务端给出，便于测试与离线导出。"""
        self.assertIn("layout", self.sources.get("app.js", self.js))
        self.assertNotIn("dagre", self.js.lower())
        self.assertNotIn("elk.", self.js.lower())


class AgentCatalogContractTestCase(unittest.TestCase):
    """会话入口的来源标签与重名消歧。

    这两条都属于"错了不会报错"的那一类：来源写错只是把人引去插件市场里找一个不存在
    的包；重名不消歧只是让人在两条一模一样的选项里选错一条——而选错的那一下决定的
    是**整轮作业口径**。它们没有异常、没有报错、没有空列表，所以只能把规则钉在代码
    形状上，再靠 `tools/render_ui_fixture.py --expand-agent` 真看一眼。
    """

    def setUp(self):
        self.chat = _read("chat.js")

    def test_agent_tag_comes_from_kind_not_from_a_boolean(self):
        """`builtin` 只有两位，分不出"插件"和"自定义"。

        真实缺陷（本版修掉）：`item.builtin ? '内置' : '插件'` 把用户自己写的自定义
        预设标成了"插件"——他会去插件市场里找一个不存在的包，找不到就再来一轮。
        """
        self.assertNotIn("item.builtin ? '内置' : '插件'", self.chat,
                         "来源标签又按布尔猜了，分类要读服务端给的 kind")
        self.assertIn("AGENT_KIND_TAGS", self.chat)
        for kind in ("builtin", "custom", "plugin"):
            self.assertIn(kind + ":", self.chat, "来源标签缺少分类：%s" % kind)
        # 兜底必须存在：kind 缺失（老后端、手拼的字典）时要退回 sourceLabel 而不是空
        self.assertIn("sourceLabel", self.chat)

    def test_duplicate_labels_are_disambiguated(self):
        for helper in ("countLabels", "agentNote"):
            self.assertIn(helper, self.chat, "缺少重名消歧：%s" % helper)

    def test_new_session_starts_from_the_last_choice(self):
        """新建会话的三项要沿上一次的选择，而不是每次弹回「跟随默认」。

        界面这一侧必须读服务端给的 `draft`（`/session-options`）：自己在浏览器
        里记一份会变成**第二份真相**——换台机器、换个人登进来就带着别人的预选，
        而预选值会原样随第一条消息发出去。两件事都要钉：

          * 没有会话时用 `view.draft` 回填（`view.config || view.draft`）；
          * 文案区分「沿用上次选择」与「本会话已选」——都写"已选"的话，使用者
            会以为这已经落到当前会话上了，而它要等发出第一条消息才生效。
        """
        self.assertIn("view.draft = payload.draft", self.chat,
                      "必须读 /session-options 给的 draft")
        self.assertIn("view.config || view.draft", self.chat,
                      "没有会话时下拉必须用服务端的上次选择回填")
        self.assertIn("（沿用上次选择）", self.chat)
        # 浏览器侧存一份就是第二份真相：换人登录会带着上一个人的选择。
        self.assertNotIn("localStorage", self.chat)

    def test_public_agent_items_carry_kind(self):
        """服务端必须给分类，前端才有得读——否则只能回去猜布尔。"""
        from pentestd import agents
        for item in agents.catalog("", None, []):
            self.assertIn("kind", item, item["id"])
            self.assertIn(item["kind"], ("builtin", "custom", "plugin"), item["id"])
            self.assertNotIn("prompt", item, item["id"])

    def test_fixture_keeps_both_views_of_the_agent_catalog_in_sync(self):
        """`/agents` 与 `/session-options` 是同一份目录的两个视图。

        夹具只同步前者的话，它会自己造出"新建成功、会话入口下拉框里却没有"这个症状
        ——而那和真实缺陷长得一模一样，排查会一路查到后端去。假后端内部保持一致是
        它自己的职责。
        """
        path = os.path.join(ROOT, "tools", "render_ui_fixture.py")
        with open(path, "r", encoding="utf-8") as handle:
            fixture = handle.read()
        self.assertIn("/api/v1/session-options", fixture,
                      "夹具没有同步会话入口读的那份目录")


class AgentPromptLengthTestCase(unittest.TestCase):
    """作业口径的**长度**：上限撤掉了，成本提示留下来了。

    两件事必须同时成立，只做一件都是坏状态：

      * **服务端不再校长度**（`agents.validate_preset`），前端也**不许留 `maxlength`**。
        `maxlength` 是浏览器侧的**静默截断**：粘一段长正文进去，后半段当场被丢掉，
        输入框里一点也看不出来，提交上去的是截断后的那一份。服务端放开而前端留着它，
        上限就还在一半的地方生效——而这半边的坏法是"存下来的不是你粘的"；
      * **字数读数必须在**（`#agent-prompt-count`）。撤上限不等于撤成本提示：这段正文
        每一轮都随系统提示原样下发，长度乘以轮数。静态文案能写"没有上限"，写不出
        "你现在写了多少"——而后者才是使用者判断的依据。

    这一组没有运行期症状：少写一处，界面照样能用，只是有人在某一轮里多付了一份他
    不知道的钱。所以只能钉在形状上。
    """

    def test_the_cap_is_gone_on_both_sides(self):
        from pentestd import agents
        self.assertFalse(hasattr(agents, "MAX_PROMPT_CHARS"),
                         "上限常量又回来了（或留了个没人用的死常量）")
        tag = re.search(r'<textarea[^>]*id="agent-prompt"[^>]*>', _read("index.html"))
        self.assertIsNotNone(tag, "找不到作业口径输入框")
        self.assertNotIn("maxlength", tag.group(0),
                         "输入框又有 maxlength 了：粘贴长正文会被静默截断")

    def test_the_readout_is_in_the_form_and_says_there_is_no_cap(self):
        html = _read("index.html")
        self.assertIn('id="agent-prompt-count"', html, "字数读数没有落在表单上")
        self.assertIn("没有字数上限", html,
                      "表单上那句「没有上限」不见了——上限撤了而话没改，等于两边都错")

    def test_the_char_count_is_wired_without_a_fresh_definition(self):
        """读数要活着，三处缺一都不行：进 dom 表、回填时刷、打字时刷。

        只做"打字时刷"的话，点「编辑」打开一条预设，旁边会显示上一次留下的那个数
        ——而它看起来完全正常，只有对着正文数一遍才发现不对。
        """
        js = _strip_js_comments(_read("settings.js"))
        self.assertIn("function syncPromptCount", js, "读数函数没有定义")
        self.assertIn("agentPromptCount", js, "读数元素没有进 dom 表")
        self.assertIn("dom.agentPrompt.addEventListener('input', syncPromptCount)", js,
                      "打字时不刷这个数")
        self.assertGreaterEqual(js.count("syncPromptCount();"), 2,
                                "回填与重置里没有刷新它（至少缺一处）")


class ReadmeAuthTableTestCase(unittest.TestCase):
    """项目说明 的鉴权列必须与路由表一致。

    这条测试是被一次真实事故逼出来的：项目说明 里 `/setup`、`/engagements*`、
    `/executors` 都写着"无鉴权"，但代码里路由默认就是 `session`——开了登录后，
    文档说可以匿名访问的接口实际全部 401。文档漂移不会让功能测试失败，
    却会让照着文档调接口或写脚本的人白跑一趟。
    """

    LEVEL_BY_WORD = {"无": "public", "**会话**": "session",
                     "**注册凭据**": "node", "**节点令牌**": "node"}

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "项目说明.md"), "r", encoding="utf-8") as handle:
            cls.doc = handle.read()
        cls.tmp = tempfile.TemporaryDirectory()
        store = Store(os.path.join(cls.tmp.name, "readme.db"))
        cls.plane = ControlPlane(store, WEB_DIR, auth_required=False)
        cls.store = store
        # 路由表按"归一化后的路径 → 方法 → 鉴权级别"建索引，便于逐行比对
        cls.routes = {}
        for method, regex, _handler, auth in cls.plane.router.routes:
            cls.routes.setdefault(cls._norm(regex.pattern.strip("^$")),
                                  {})[method] = auth

    @classmethod
    def tearDownClass(cls):
        cls.store.close()
        cls.tmp.cleanup()

    @staticmethod
    def _norm(path: str) -> str:
        """把 {id} 与 (?P<eid>[^/]+) 之类的占位统一成 <var>，否则两边对不上。

        这里**只归一化占位符**，不碰查询串：同一个函数也用来给路由正则建索引
        （见 `setUpClass`），而路由正则里的 `?` 是可选组语法（`(?P<eid>[^/]+)?`），
        一旦在这里 `split("?", 1)[0]`，17 条 `{id}` 路由会被截断成不存在的路径，
        测试反而报一堆"文档写了但路由表没有"。文档那侧的查询串在 `_rows()` 里剥。
        """
        path = re.sub(r"\(\?P<\w+>\[\^/\]\+\)", "<var>", path)
        path = re.sub(r"\{[^}]+\}", "<var>", path)
        path = re.sub(r"\[\^/\]\+", "<var>", path)
        return path.rstrip("/")

    def _rows(self):
        """解析接口表：只取反引号里的绝对路径（相对片段如 `/assets` 无法独立定位）。"""
        found = []
        for line in self.doc.splitlines():
            if not (line.startswith("| GET") or line.startswith("| POST")):
                continue
            cells = [cell.strip() for cell in line.strip("|").split("|")]
            if len(cells) < 3:
                continue
            # 文档会把查询串写进路径格子（`GET /api/v1/chat/stream?sessionId=…&since=`）：
            # 查询参数说明的是**怎么调**，路由表里只有路径，所以在这里剥掉再比。
            paths = [p.split("?", 1)[0] for p in re.findall(r"`([^`]+)`", cells[1])
                     if p.startswith("/api/v1")]
            if not paths:
                continue
            found.append((cells[0], paths, cells[2]))
        return found

    def test_every_row_matches_the_router(self):
        rows = self._rows()
        self.assertGreaterEqual(len(rows), 15, "项目说明 的接口表被改坏了")
        problems = []
        for methods, paths, level in rows:
            word = level.replace("+管理员", "").replace("+注册凭据", "")
            expected = self.LEVEL_BY_WORD.get(word)
            if expected is None:
                continue
            for method in [m.strip().upper() for m in methods.split("·")]:
                for path in paths:
                    actual = self.routes.get(self._norm(path), {}).get(method)
                    if actual is None:
                        problems.append("文档写了但路由表没有：%s %s" % (method, path))
                    elif actual != expected:
                        problems.append("鉴权级别不符：%s %s 文档=%s 代码=%s"
                                        % (method, path, expected, actual))
        self.assertEqual(problems, [], "项目说明 接口表与路由表不一致：%s" % problems)

    def test_no_stale_public_claims(self):
        """不允许"标为无鉴权、实际需要会话"——这正是当初那处漂移。"""
        for _methods, paths, level in self._rows():
            for path in paths:
                if path in ("/api/v1/setup", "/api/v1/engagements",
                            "/api/v1/executors", "/api/v1/baselines"):
                    self.assertNotEqual(level, "无",
                                        "%s 实际需要登录会话，项目说明 不能写「无鉴权」"
                                        % path)

    def test_gate_status_endpoints_are_documented(self):
        """三道闸的状态接口必须在表里。

        它们是使用者**看懂门禁**的入口，而接口表正是发给使用者的那份材料——表里没有，
        对读它的人来说就是"这个能力不存在"。`/scope`（第一道闸）与 `/authz`（第三道闸）
        是一对：只列其中一个，会让人以为另一道闸的状态无处可查。
        """
        documented = {path for _m, paths, _l in self._rows() for path in paths}
        for path in ("/api/v1/scope", "/api/v1/authz"):
            self.assertIn(path, documented, "接口表缺少闸门状态接口：%s" % path)


class CssLayoutGuardTestCase(unittest.TestCase):
    """CSS 布局守卫：防"边框里的内容被切掉"这类只看渲染才能发现的问题。
    起因是一个真实缺陷：`.kv` / `.setup-kv` 用了裸 `1fr` 网格列。`1fr` 的
    最小尺寸默认是 `auto`，等于内容的 min-content——遇到路径这种没有空格的长串
    （`E:\\Documents\\...\\pentest.db`）列就不收缩，网格被撑得比卡片还宽；
    而卡片为了圆角裁切设了 `overflow: hidden`，超出部分在边框处被直接切掉，
    既没有省略号也没有滚动条，内容就此丢失。

    这类问题的共同点是**测试全绿、只有肉眼可见**，所以在这里用静态断言兜住。
    """

    @classmethod
    def setUpClass(cls):
        cls._css = _read("style.css")

    @staticmethod
    def _rules(css):
        return [(" ".join(sel.split()), " ".join(body.split()))
                for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css)]

    def test_every_css_variable_reference_is_defined(self):
        """`var(--x)` 引用的每个变量都必须在同一份样式表里被定义过。

        这一条在**大规模重写样式表**之后最值钱：删掉一个 `--x` 的定义，引用它的声明
        会整条作废（没写 fallback 时），而症状是"某一处的颜色/间距没了"——没有任何测试
        会红，渲染出来也不像坏掉，只像"设计本来就是这样"。

        **先剥注释再抓**：说明性文字里提到 `var(--x)` 是常事（"全关用 var(--muted)"），
        而注释里出现的变量名不需要被定义——不剥的话，写一句文档就可能让守卫变红，
        而失败信息指向一个根本没在生效的引用。
        """
        css = re.sub(r"/\*.*?\*/", "", self._css, flags=re.S)
        defined = set(re.findall(r"(--[\w-]+)\s*:", css))
        used = set(re.findall(r"var\(\s*(--[\w-]+)", css))
        self.assertTrue(used, "样式表里一个 var() 都没有？多半是读错了文件")
        self.assertTrue(defined, "样式表里一个自定义属性都没有？多半是读错了文件")
        missing = sorted(used - defined)
        self.assertEqual(missing, [], "这些 CSS 变量被引用却没有定义：%s" % missing)

    def test_no_selector_is_defined_twice_at_top_level(self):
        """同一条规则不能在**顶层**定义两次。

        CSS 里重复定义不报错：后一条静默覆盖前一条。症状是"我改的那条没生效"，而
        打开样式表一看两条都在、都长得对——所以它比语法错误难查得多。

        这次改版正好撞上：竖排导航一节里新写了一条 `.tab-entry`（把「入口」徽章推到
        行尾、改成轮廓样式），而"设计原则"一节里早就有一条同名的 `margin-left: 6px`。
        后者在后面、特异性相同，于是新写的那条**整条作废**——徽章既没靠右，轮廓样式
        也从未生效，两处代码却都看不出问题。同一次检查还翻出一条**逐字节重复**的
        `.cmd-block`（同一份内容抄了两遍）。

        @media 块内部不算：同一选择器在基准层与断点层各写一次是正常写法，所以只看
        花括号嵌套深度为 0 的选择器；`@` 开头的 at-rule 当然也要排除。
        """
        css = re.sub(r"/\*.*?\*/", "", self._css, flags=re.S)
        counts, dupes = {}, []
        depth, buf = 0, ""
        for ch in css:
            if ch == "{":
                if depth == 0:
                    selector = " ".join(buf.split())
                    if selector and not selector.startswith("@"):
                        counts[selector] = counts.get(selector, 0) + 1
                        if counts[selector] == 2:
                            dupes.append(selector)
                depth += 1
                buf = ""
            elif ch == "}":
                depth = max(0, depth - 1)
                buf = ""
            elif depth == 0:
                buf += ch
        self.assertEqual(dupes, [],
                         "这些选择器在顶层定义了两次，后一条会静默覆盖前一条：%s"
                         % sorted(dupes))

    def test_fr_columns_allow_shrinking(self):
        """凡是用到 1fr 的网格列，都要写成 minmax(0, 1fr) 或 minmax(min(...), 1fr)。"""
        offenders = []
        for sel, body in self._rules(self._css):
            if sel.startswith("/*") or "grid-template-columns" not in body:
                continue
            value = re.search(r"grid-template-columns:\s*([^;]+)", body).group(1)
            for token in value.split():
                if token == "1fr":
                    offenders.append("%s → %s" % (sel, value))
                    break
        self.assertEqual(offenders, [],
                         "裸 1fr 不允许作为网格列（长内容会撑宽并溢出被裁切）：%s"
                         % offenders)

    def test_kv_values_can_wrap_long_paths(self):
        body = " ".join(b for _s, b in self._rules(self._css) if _s.split() == [".kv-val"]
                        or _s.startswith(".kv-val"))
        self.assertTrue(body, "找不到 .kv-val 规则")
        # break-word 不影响 min-content 宽度，长串仍会撑宽；要 anywhere 或 break-all
        self.assertNotIn("word-break: break-word", body,
                         ".kv-val 用 break-word 挡不住长路径撑宽列，改用 "
                         "overflow-wrap: anywhere")
        self.assertIn("overflow-wrap: anywhere", body)

    def test_overflow_hidden_containers_are_known(self):
        """把 overflow: hidden 的容器列出来——每新增一个都是潜在裁切点。

        列表写死在这里是有意的：新增时必须显式改这条断言，也就等于被迫确认
        "这个容器里的内容不会超过它的边框"。
        """
        # **先剥注释再抓**。`_rules` 的选择器组是"上一个 } 到下一个 {"之间的全部
        # 文本，会把上一条规则与本条之间的注释一起吃进来；既有写法靠
        # `sel.startswith("/*")` 跳过它们——代价是**在规则上方写一段文档注释就能让
        # 这条守卫看不见它**。
        #
        # 这次改版正好撞上：`.rail-scope` 上面写了"名字很长所以省略号收尾"，于是它
        # 从集合里消失了。顺带暴露出两个**一直以来**被注释挡住的容器
        # （`.finding-desc` / `.job-mode-switch`）——它们早就该出现在这个列表里。
        # 剥掉注释之后，注释不再有豁免权。
        css = re.sub(r"/\*.*?\*/", "", self._css, flags=re.S)
        containers = sorted({sel for sel, body in self._rules(css)
                             if sel.startswith("/*") is False
                             and "overflow: hidden" in body})
        # 逐项的"内容不会超出边框"确认（这条断言的意义就在这里，所以不写成一串名字）：
        #   body / .cmd-block         —— 既有；命令行块内部横向滚动
        #   .finding-desc             —— 既有；长描述按行数截断，完整文本在 title 里
        #   .ph-sub / .rail-user-name
        #                             —— 本次新增，两者都是**声明式截断**：带
        #                                text-overflow: ellipsis，截断在界面上以"…"
        #                                显式可见，不是静默裁掉；两者都把完整值放进 title
        #                                （`.rail-scope` 曾在这一列，随栏脚那颗范围读数
        #                                一起撤掉了）
        #   .dock-chip .muted         —— 输入辅助坞的目录项说明，按两行截断
        #                                （-webkit-line-clamp）。不截断的话二十多份技能手册
        #                                会在坞里撑成一面墙；完整说明在 title 里。
        #   .dock-chip-fill           —— t96 新增：卡片最后一行「点一下填：「…」」。
        #                                **声明式截断**（单行 + ellipsis，完整那句在
        #                                title 里、点下去填进输入框的也是完整的）：那一行
        #                                折成两行，卡片就会比"一行卡片"的封顶高，而封顶
        #                                正是坞只显示一行的依据（见
        #                                test_the_dock_matches_the_card_and_shows_exactly_one_row_of_cards）。
        #   .side-panel              —— 卡片式改版。这个侧栏从"贴边的竖带"变成了
        #                                圆角卡片，而它的**卡头**（.panel-head）自己
        #                                画不到父元素的圆角外面，所以必须由卡片来裁。
        #                                确认：它的直接子元素只有 .panel-head（不滚动）
        #                                与滚动容器（自己带 overflow: auto），没有会溢出
        #                                的静态内容。
        #   .chat-side-block         —— 卡片化改成"一块一张卡"之后，裁切的责任从整栏
        #                                下沉到每一块。撤掉的是 `.chat-side`（它现在只是
        #                                画布，连面都没有）。保留这条是因为 `.fold-head`
        #                                是**按钮**，悬停时底色换成 `--panel-2`，不裁就
        #                                会从卡片上面两个圆角外露出来。
        #   .rpt-target               —— 报告目录里那一行目标名。同样是**声明式截断**
        #                                （ellipsis + title 里放完整值）：320px 宽的目录栏
        #                                里放不下 `E:\Documents\...\vulnapp`，而它只需要
        #                                能被比对，不需要被读完。
        #   .panel-head > .doc-name   —— 报告正文卡头那行"我读的是哪一份"。同上：
        #                                目标是完整路径时有 60 多个字符，换行会把卡头
        #                                撑成两三行，完整值在 title 里。
        #   .chat-policy-name         —— t80 新增：输入区那颗权限 chip 上的档名。
        #                                它是**声明式截断**（ellipsis + title 放完整
        #                                summary）：四档里最长的一档在窄窗口下会把
        #                                chip 撑开，而 chip 与模型选择器抢同一行。
        #   .chat-model-name          —— t100 新增：模型 chip 上那一行字。
        #                                **声明式截断**（ellipsis + title 放完整名）：
        #                                名字最长是「跟随全局启用项（DeepSeek ·
        #                                deepseek-flash）」，窄档里 chip 只剩 30px 的
        #                                图标位，不截会把三颗 chip 挤到换行。
        #   .chat-policy-item-note    —— t80 新增：菜单里每档下面那句短注。同样是
        #                                ellipsis，完整定义在菜单项的 title 里。
        #   .chat-policy-item-note    —— t80 新增：菜单里每档下面那句短注。同样是
        #                                ellipsis，完整定义在菜单项的 title 里。
        #   .chat-detail-title        —— 卡头那行标题。**声明式截断**（ellipsis，完整值
        #                                在 title 里）：任务号 / 目标路径可能很长，而不截断
        #                                会把卡头撑破——flex 项的 min-width 默认是 auto，
        #                                光写 ellipsis 不够，还要显式 min-width: 0。
        #   .target-name              —— 漏洞页左栏那一行的目标名。同样是**声明式截断**
        #                                （ellipsis，完整值在 title 里）：左栏只有 300px，
        #                                IP / 域名 / 一长串 URL 取同样宽，不截会把条数挤掉。
        #   .chat-auto-label          —— t82 新增：「自动继续」那颗开关的文字。**声明式
        #                                截断**（ellipsis，完整值就是它自己，按钮另有
        #                                aria-label）：它和两颗目录入口挤在同一行，
        #                                360px 视口下那一行只有 256px，自然宽 271px——
        #                                不截就折行，折出来的第二行会撑破卡片的下限。
        #   .chat-sessions-goal-name   —— 对话按目标分组后的组标题里那个目标名。
        #                                **声明式截断**（ellipsis，完整名在 title 里，
        #                                见 chat.js 的 sessionGroupRow）：左栏只有 300px，
        #                                不截会把"几条"与「当前」挤出去，而这一行唯一
        #                                的用处就是认出是哪台机器。
        #   .chat-queue                —— 排队任务那一块（卡片上方）的外壳。同
        #                                .side-panel / .chat-detail：头那一行自己画不到
        #                                圆角外面，只能由块裁；它的直接子元素只有不滚动
        #                                的头与自带 overflow-y: auto 的列表。
        #   .chat-queue-hint           —— 排队标题右边那句随状态变的说明。**声明式
        #                                截断**（ellipsis，完整值在 title 里）：它与
        #                                「排队中 · N 条」挤同一行，不截会把条数挤出去。
        #   .chat-queue-text           —— 一条排队任务的正文，按三行截断
        #                                （-webkit-line-clamp）。一行可以是一整段需求，
        #                                全展开会把输入框顶出屏幕；完整那句在 title 里，
        #                                点「编辑」也能看到并改。
        #   .dash-row-main            —— 仪表盘里一行任务/一条漏洞的**主标题**。
        #                                **声明式截断**（ellipsis，完整值在 title 里，
        #                                见 panels.js 两处 `mainXxx.title = textContent`）：
        #                                这一行右边还要放状态与时间，标题不截就会把
        #                                后两列顶出卡片。
        #   .data-table td.clip       —— 数据表里"内容是外部输入"的那一列（URL、摘要、
        #                                值），`max-width: 46ch` + ellipsis，完整值一律
        #                                写进 `cell.title`（panels.js 三处）。表格不能
        #                                横向滚动：一滚表头就与内容错位。
        #   .kv-table td.clip         —— 同上，键值表那一侧（工作空间的路径、模型名）。
        #                                与 .data-table 分开写是因为两者列宽上限不同
        #                                （46ch / 62ch），合成一条就得二选一。
        expected = [".chat-auto-label", ".chat-detail-title",
                    ".chat-model-name",
                    # 菜单注脚：权限与模型两颗菜单**共用一条规则**（同一套形状，
                    # 不复制数值），所以它在这里是合并后的一个选择器组。
                    ".chat-policy-item-note, .chat-model-item-note",
                    ".chat-policy-name", ".chat-queue", ".chat-queue-hint", ".chat-queue-text",
                    ".chat-session-title",
                    ".chat-sessions-goal-name", ".chat-side-block",
                    ".cmd-block", ".dash-row-main", ".data-table td.clip",
                    ".dock-chip .muted", ".dock-chip-fill", ".finding-desc",
                    ".kv-table td.clip",
                    ".panel-head > .doc-name", ".ph-sub",
                    ".rail-user-name", ".rpt-target", ".side-panel", ".target-name",
                    ".task-sub",
                    "body"]
        self.assertEqual(containers, expected,
                         "overflow: hidden 的容器集合变了：%s；"
                         "新增容器前请确认其内容不会超出边框（否则会被静默裁掉）"
                         % containers)

    # ------------------------------------------------- 分栏守卫（部署页签）
    #
    # 这一组守的是"多列网格 + 单列内容"这个组合。它不是报错，是**整片黑**：
    # 用户看到的是"页面空的太多"，而不是"布局错了"——所以静态断言比渲染截图
    # 更适合当回归网，截图会被下一次改版覆盖掉。
    #
    # 共同判据：会排成多列的容器（auto-fit / 显式多列），内容必须能横着排。

    @staticmethod
    def _selector_name(selector):
        """选择器去掉夹在其中的注释。

        `_rules` 的选择器组是"上一个 } 到下一个 {"之间的全部文本，会把上一条规则
        与本条之间的注释一起吃进来——这正是既有断言要写 `sel.startswith("/*")` 才能
        跳过注释块的原因。这里按选择器名精确查规则，所以必须先剥掉。
        """
        return re.sub(r"/\*.*?\*/", "", selector, flags=re.S).strip()

    def _first_body(self, selector):
        """某个选择器的规则体。选择器已按 _rules 规范化（单空格）。"""
        for sel, body in self._rules(self._css):
            if self._selector_name(sel) == selector:
                return body
        return ""

    def _bodies_containing(self, needle):
        """所有选择器里含 needle 的规则的规则体。"""
        return [body for sel, body in self._rules(self._css)
                if needle in self._selector_name(sel)]

    @staticmethod
    def _columns_of(body):
        found = re.search(r"grid-template-columns:\s*([^;]+)", body or "")
        return found.group(1).strip() if found else ""

    @staticmethod
    def _flows_into_multiple_columns(columns):
        """这组列会不会排出多于 1 列。

        auto-fit / auto-fill 随容器宽度变，一律当多列；显式列表按**顶层**空格数条数。
        不能直接 `columns.split()`：`minmax(0, 1fr)` 里的那个空格不是列分隔符，
        split 会把它数成两列，于是单列布局被误判成多列。
        """
        if "auto-fit" in columns or "auto-fill" in columns:
            return True
        depth = tokens = 0
        for char in columns:
            if char == "(":
                depth += 1
            elif char == ")":
                depth -= 1
            elif char.isspace() and depth == 0:
                tokens += 1
        return tokens + 1 > 1

    @staticmethod
    def _function_body(js, name):
        """取出某个顶层函数的源码（到下一个顶层 function 为止）。"""
        start = js.index("function %s(" % name)
        nxt = js.find("\nfunction ", start + 1)
        return js[start:nxt if nxt > 0 else len(js)]

    def _panel_html(self, name):
        """取一个面板自己的 HTML 片段（到下一个面板为止；最后一个取到结尾）。"""
        html = _read("index.html")
        rest = html[html.index('id="panel-%s"' % name):]
        end = rest.find('id="panel-', 1)
        return rest[:end] if end > 0 else rest

    def _deploy_panel_html(self):
        return self._panel_html("setup")

    def test_setup_cards_agree_with_the_page_column_count(self):
        """非 wide 的卡存在 ⇒ 页面网格必须是单列。

        两个事实必须一致。不一致时每张非 wide 卡独占第一列，右侧整行空着：
        实测 1416px 可用宽度下 `auto-fit minmax(420px,1fr)` 排成 **3 列**，
        每张卡占 464px，旁边 944px 什么都没有。

        **两个面板共用一份 `.setup-body` 网格定义**（「任务」页的下发/列表/基线三张
        卡从部署页拆出去之后，两边用的是同一条规则）。所以这条要对两个面板都成立：
        只查其中一个的话，另一页里那张非 wide 卡会在同一套网格上单独踩坑，而断言
        全绿——"拆页时把守卫留在了原来那一半"。
        """
        columns = self._columns_of(self._first_body(".setup-body"))
        self.assertTrue(columns, "找不到 .setup-body 的网格列定义")
        for panel in ("setup", "jobs"):
            narrow = [cls for cls in re.findall(r'class="(setup-card[^"]*)"',
                                                self._panel_html(panel))
                      if "wide" not in cls]
            if self._flows_into_multiple_columns(columns):
                self.assertEqual(narrow, [],
                                 "%s 面板是多列网格，这些卡却不是 wide——右侧会整行空着：%s"
                                 % (panel, narrow))
            else:
                # 单列之下"卡旁边空着"不可能发生。但**必须真的有非 wide 卡**，
                # 否则这条守卫在守一个已不存在的场景：面板里全是宽卡时，说明当初
                # 促成单列的窄内容已经没了，那时应当重新评估列数、而不是留着这条断言。
                self.assertTrue(narrow,
                                "%s 面板已是单列，却不再有任何非 wide 卡——"
                                "促成单列的原因消失了，请重新评估列数并改掉这条断言"
                                % panel)

    def test_setup_kv_pairs_stay_grouped_when_columns_flow(self):
        """部署信息：多列时每组键值必须自成一组（.kv-item）。

        `dl` 里直接平铺 dt/dd 时，网格把它们当成彼此独立的项：第 2 列的标签会
        配到第 1 列的值上去，读出来是"引擎 = C:\\...\\pentest.db"。**错配比空着更糟**——
        空着看得出没排版，错配是一份看着像数据的错误答案。
        """
        columns = self._columns_of(self._first_body(".setup-kv"))
        self.assertTrue(columns, "找不到 .setup-kv 的网格列定义")
        if not self._flows_into_multiple_columns(columns):
            return
        self.assertIn(".setup-kv > .kv-item", self._css,
                      ".setup-kv 是多列，却没有 .kv-item 分组规则")
        js = _read("app.js")
        self.assertIn("'kv-item'", js, "appendInfoRow 没有把每组键值包成 .kv-item")
        # 只查 appendInfoRow 自己：节点详情那处 `list.append(el('dt'…` 是另一张表
        # （纯 .kv、单列），它平铺是对的——把断言写成全文禁止会连它一起冤枉掉。
        body = self._function_body(js, "appendInfoRow")
        self.assertIn("item.append(el('dt'", body, "dt 应当挂在分组里")
        self.assertNotIn("list.append(el('dt'", body,
                         "dt 直接挂到 dl 上就又平铺了，多列时标签与值会错配")

    def test_the_self_scrolling_containers_paint_no_scrollbar(self):
        """t97：输入框与坞**自带滚动、但不画滚动条**（使用者原话「隐藏输入框和浮层中的滚动条」）。

        为什么不干脆撤掉 `overflow`：那会变成"内容被裁掉且滚不动"。这两处都真的会溢出
        （输入框超过四行、坞超过一行卡片），要取消的是**滚动条的画法**，不是滚动能力。

        为什么两半必须成对写：
          * 只有 `::-webkit-scrollbar` → Firefox 仍画一条细杠；
          * 只有元素上的 `scrollbar-width` → 本机 Edge 仍按全局那条 11px 规则给它们
            **留位**（探针量到 `offsetWidth − clientWidth − 边框` 还是 11，文字行宽
            因此比可用宽度窄 11px，滚一下还会重新折行）。
        改前 / 改后的四个数（.scratch/t97/probe_sb.py，两种主题各一遍，数字相同）：

            输入框 11 → 0，clientWidth 771 → 782
            坞     11 → 0，内容宽 777 → 788（列数 3 不变，卡片宽 253.7 → 257.3）

        而 `scrollTop` 改后仍能设进去再读回来（输入框 60、坞 80），坞仍是"完整可见
        3 张、半张 0 张"——所以这条用例同时钉住"藏的是画法、不是能力"。

        t98 又加进两处（使用者原话「任务选择框中还有」）：选项面板 `#chat-choice`
        自己会滚（候选超过 `max-height: min(48vh, 360px)`），面板里「其他」那一格的
        textarea 也会滚（`field-sizing` 长到 96px 上限之后框内滚）。这两处还多一个害处：
        滚动条吃掉 11px 行宽，正在写的那一行会当场重新折行、光标跳一格。

        改前 / 改后（.scratch/t98/probe_choice.py，卡片宽 358，两种主题各一遍）：
            面板     gutter 11 → 0，内宽 329 → 340，文字行宽 290 → 301，行数 2 → 2，
                     scrollHeight 748 不变（滚得动），scrollTop 仍能设成 120，
                     品红滑块像素 7388 → 0（贴着右内边的一条竖条）
            textarea gutter 11 → 0，宽 329 → 340，scrollHeight 207 → 188，滚得动
        面板那把是**主题化**的（`::-webkit-scrollbar-thumb` 用 `--scrollbar`），像素能看见；
        textarea 那把是 Chromium 的**原生**滚动条，`::-webkit-scrollbar` 对它不生效
        （把 `--scrollbar` 改成品红后那一格里一个品红像素都没有，gutter 仍是 11），
        真正去掉它的是**元素上的** `scrollbar-width: none`。所以两半都得写，
        五个选择器还必须同一条分组规则——分两条时只有一处生效。

        t110 又加进一处（使用者原话「悬浮框隐藏滚动条」）：待批浮层 `.chat-review`
        （高度锁在一张卡上之后自己滚，见 `--review-pin`）。它与坞、选项面板同一形态——
        浮在输入卡片上沿的一条带，11px 的竖杠正好压在这条带上，看着像"框里又套了个框"。
        """
        css = re.sub(r"/\*.*?\*/", "", self._css, flags=re.S)
        for sel in (".chat-input", ".chat-quick", ".chat-choice",
                    ".chat-choice-other-input", ".chat-review"):
            rule = re.search(r"%s\s*\{([^}]*)\}" % re.escape(sel), css)
            self.assertIsNotNone(rule, "样式表里找不到规则 %s" % sel)
            self.assertRegex(rule.group(1), r"scrollbar-width:\s*none\b",
                             "%s 少了 `scrollbar-width: none`：Firefox 会画一条细杠；"
                             "面板里那把是原生滚动条，只有这一条能去掉" % sel)

        # 两条伪元素必须写在**同一条分组规则**里：分两条时，后写的那条会把前一条的
        # 某个声明盖掉一半，症状是"只有一个容器没有滚动条"。
        # 分组里可以不止这两个：选项面板自己也会滚（`.chat-choice`、
        # `.chat-choice-other-input`），待批浮层也会滚（`.chat-review`），
        # 它们画的滚动条同样不该出现。但必须**同一条规则**。
        group = re.search(r"\.chat-input::-webkit-scrollbar\s*,\s*"
                          r"\.chat-quick::-webkit-scrollbar"
                          r"(?:\s*,\s*\.[\w-]+::-webkit-scrollbar)*"
                          r"\s*\{([^}]*)\}", css)
        self.assertIsNotNone(group, "两条 `::-webkit-scrollbar` 必须写成一条分组规则")
        for more in (".chat-choice", ".chat-choice-other-input",
                     ".chat-review"):
            self.assertIn("%s::-webkit-scrollbar" % more, group.group(0),
                          "面板里的滚动条也该在这条分组规则里：%s" % more)
        body = group.group(1)
        self.assertRegex(body, r"width:\s*0", "webkit 那条没把宽度归零，仍会占 11px")
        self.assertRegex(body, r"display:\s*none", "webkit 那条没把滚动条本身藏掉")

        # 全局那条 11px 是给**长列表**（记录区、侧栏、控制台）的，不能因为这两处就删掉；
        # 一旦删掉，这两条用例会"绿得很轻松"，而整站滚动条一起变样。
        self.assertRegex(css, r"::-webkit-scrollbar\s*\{\s*width:\s*11px",
                         "全局滚动条规则被改了：这两处之外的列表还需要它")

        # 藏画法不许顺手连能力一起藏掉。
        dock = re.search(r"\.chat-quick\s*\{([^}]*)\}", css)
        self.assertRegex(dock.group(1), r"overflow-y:\s*auto",
                         "坞不再可滚：卡片超过一行之后，「装得下一行」变成「只剩一行可见且拿不到其余」")
        self.assertNotRegex(css[:css.index(".chat-input::-webkit-scrollbar")],
                            r"\.chat-input\s*\{[^}]*overflow:\s*hidden",
                            "输入框被改成 overflow: hidden：长文本会被裁掉且滚不动")
        # 面板：同样只藏画法，能力与上限都得留着。
        def rule_of(selector):
            head = chr(10) + selector + " {"
            i = css.index(head)
            j = css.index("}", i)
            return css[i:j]

        panel = rule_of(".chat-choice")
        self.assertIn("overflow-y: auto", panel,
                      "面板不再可滚：候选超出上限之后，框外那些就永远拿不到了")
        self.assertIn("max-height: min(", panel,
                      "面板丢了 max-height：八条候选会把输入框一路挤出屏幕")
        ta = rule_of(".chat-choice-other-input")
        self.assertIn("field-sizing: content", ta,
                      "「其他」那一格丢了 field-sizing：写第二件事时框不会长高，"
                      "只剩一行、框内滚，用户看到的就是框又在滚")


    def test_the_floats_scroll_under_the_pointer_without_taking_focus(self):
        """三张浮层（选项面板 / 输入辅助坞 / 待批浮层）：划过就能滚，且不抢焦点。

        用户原话是「不抢鼠标焦点（能过随时滚动）」。这两件事在浏览器里本来是
        **同一条**：滚动是"指针在谁身上就滚谁"，不需要先点一下拿到焦点。能破坏
        它的只有两种写法，所以各钉一条：

        * 浮层出现时替使用者 `.focus()` —— 光标被抢走，正在输入框里写的那半句话
          就断了（`sendChoice()` 的注释里已经写明这条，它不是新顾虑）；
        * 在浮层上 `preventDefault()` 掉 wheel —— 滚轮不再滚这一层，而指针明明
          停在它上面，"过一下就能滚"退化成"先把鼠标挪开再挪回来"。

        另外 `overscroll-behavior: contain` 必须三张都在：缺了它，滚到头的余量会
        链式传给记录区，于是"往下翻这张浮层"读着读着整页开始往上跑——那正是
        "抢"的另一种形态，只不过抢的是滚动而不是焦点。
        """
        js = _strip_js_comments(_read("chat.js"))
        for name in ("quick", "reviewFloat", "choice"):
            self.assertNotRegex(js, r"dom\.%s\.focus\(" % name,
                                "浮层出现时替使用者抢了焦点：dom.%s" % name)
        css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)
        for selector in (".chat-choice", ".chat-quick", ".chat-review"):
            head = chr(10) + selector + " {"
            self.assertIn(head, css, "%s 的规则块找不到了" % selector)
            start = css.index(head)
            block = css[start:css.index("}", start)]
            self.assertIn("overflow-y: auto", block,
                          "%s 不再可滚：装不下的那部分永远拿不到" % selector)
            self.assertIn("overscroll-behavior: contain", block,
                          "%s 滚到头会把余量甩给记录区" % selector)

    def test_the_page_head_starts_on_the_same_line_as_the_rail(self):
        """页面上沿与下沿都要和左导航栏对齐：同一条起点线，同一条终点线。

        用户两次指出来，说的是同一件事的两端。第一次是「红线这块平齐」——他在控制台
        截图上横着画了一条线，那条线同时压过 `.rail` 的上沿与右侧第一张卡
        `header.page-head` 的上沿；第二次是「主页布局高度和左侧边栏保持一样的高度」
        ——左栏一路排到底，右边却在自己的最后一张卡下方空出一条 12px 的带子。

        形状本来就是对的：外壳 `.console-shell` 自己留了一圈 12px，`.rail` 与
        `.workspace` 都从这条线往下开始，左右方向也已经对齐（`rail.right + gap`
        正好等于 `head.left`）。多出来的那 12px 只在上下两端：`.panel` 又给了一圈
        内边距，而每页的第一个元素就是页头、最后一张卡贴着底排，于是页头比左栏低一格、
        卡片下沿又比左栏高一格。

        实测（Edge headless，1440×900 / 1280×780 / 1920×1080 三档，逐页切换 15 个
        页签）：顶部改前 `rail.top=12 / head.top=24`，改后同为 12；底部改前
        `rail.bottom=888 / 卡片下沿=876`，改后同为 888。两项偏差全为 0。

        左右那道 12px **要留着**：它是卡片与整页滚动条之间唯一的呼吸位。所以这条用例
        查的是「三值写法里上下都是 0、左右不为 0」，而不是「四边都归零」。

        这一条守两件事，缺一不可：`.panel` 的上下内边距为 0，**且**每个页签的第一个
        元素确实是页头。只守前者，某页把页头换成一排行内元素之后页头又会被推下去；
        只守后者，`padding: var(--sp-3)` 那种整体内边距回来时谁都看不出来。
        """
        css = re.sub(r"/\*.*?\*/", "", self._css, flags=re.S)
        panel = re.search(r"(?:^|\n)\.panel\s*\{([^}]*)\}", css)
        self.assertIsNotNone(panel, "样式表里找不到 `.panel` 的规则块")
        padding = re.search(r"(?:^|;)\s*padding:\s*([^;]+)", panel.group(1))
        self.assertIsNotNone(padding, ".panel 没有 padding：页内的块会贴到外壳边上")
        parts = padding.group(1).split()
        self.assertEqual(len(parts), 3,
                         "`.panel` 的 padding 要写成「上 左右 下」三个值，收到的"
                         "是 %r——两值写法改不动顶部，四值写法会与这条用例的口径脱钩"
                         % padding.group(1).strip())
        self.assertEqual(parts[0], "0",
                         "`.panel` 的顶部内边距回来了：外壳已经留了一圈 12px，页内"
                         "再叠一层，页头就比左导航栏低 12px（截图里那条线会断成两截）")
        self.assertEqual(parts[2], "0",
                         "`.panel` 的底部内边距回来了：外壳那圈 12px 已经是页与窗口"
                         "之间的留白，页内再垫一层，最后一张卡的下沿就停在 rail.bottom-12"
                         "上——整页看起来比左导航栏矮一截（原话「主页布局高度和左侧边栏"
                         "保持一样的高度」）")
        self.assertNotEqual(parts[1], "0",
                            "左右那道 12px 画布缝没了：卡片会直接贴到工作区的边线上，"
                            "整页的滚动条也会压住卡片的圆角")

        html = re.sub(r"<!--.*?-->", "", _read("index.html"), flags=re.S)
        sections = re.findall(
            r'<section\s+id="(panel-[a-z-]+)"\s+class="([^"]*)"\s*>(.*?)'
            r'(?=<section\s+id="panel-|\Z)', html, flags=re.S)
        self.assertTrue(sections, "一个 `section.panel` 都没抓到，多半是读错了文件")
        self.assertEqual(len(sections), 17,
                         "页签数量变了（现在是 %d 个）：新增的页也要用页头当第一张卡"
                         % len(sections))
        wrong = []
        for pid, cls, body in sections:
            self.assertIn("panel", cls.split(), "%s 的 class 里没有 panel" % pid)
            first = re.match(r"\s*<(\w+)([^>]*)>", body)
            if not first or first.group(1) != "header" \
                    or 'class="page-head"' not in first.group(2):
                wrong.append(pid)
        self.assertEqual(wrong, [],
                         "这些页签的第一个元素不是 `header.page-head`：%s"
                         % wrong)


class FixtureScriptCoverageTestCase(unittest.TestCase):
    """离线渲染夹具必须知道 web/ 下的**每一个**脚本。

    `tools/render_ui_fixture.py` 把脚本内联进产物（夹具是脱离项目目录打开的，
    留在 `<script src="...">` 里的相对路径会 404），而它靠**两张手写的名单**决定
    内联哪些、丢掉哪些。手写的名单一定会被忘掉——那份文件自己的注释里就写着
    `split.js` 漏过一次（"分栏条画得出来、按下去没反应"），而本轮新增的 `fold.js`
    又漏了第二次。

    第二次比第一次更难看出来：折叠头是纯 HTML + CSS，**画得出来、箭头也在**，
    只是点下去没有任何反应——而那与"用户根本没有点"长得一模一样。截图看不出来，
    任何测试也不会红。

    所以完整不靠人记，靠这条断言：两张表合起来必须正好等于 web/ 下的 .js 集合。
    """

    @classmethod
    def setUpClass(cls):
        path = os.path.join(ROOT, "tools", "render_ui_fixture.py")
        with open(path, "r", encoding="utf-8") as handle:
            cls._source = handle.read()

    def _tuple(self, name):
        match = re.search(r"^%s = \((.*?)\)" % name, self._source, re.M | re.S)
        self.assertIsNotNone(match, "找不到 render_ui_fixture.py 里的 %s" % name)
        # **两种引号都收**：这份文件里两张表恰好一种用双引号、一种用单引号，
        # 只认一种的话会静默返回空集合——那样这条断言就变成"永远通过"，
        # 而它要挡的恰好是"名单漏了一个脚本"。
        return set(re.findall(r"""['"]([\w.]+)['"]""", match.group(1)))

    def test_the_two_lists_cover_every_frontend_script(self):
        known = self._tuple("INLINE_SCRIPTS") | self._tuple("DROPPED_SCRIPTS")
        actual = set(_scripts())
        self.assertEqual(sorted(actual - known), [],
                         "这些脚本既没被内联、也没被显式丢掉：夹具里会 404，"
                         "而页面照常渲染，只是少了那个模块的全部行为——"
                         "截图恰恰看不出来。加进 INLINE_SCRIPTS（或 DROPPED_SCRIPTS"
                         "并写明为什么不需要）：%s" % sorted(actual - known))
        self.assertEqual(sorted(known - actual), [],
                         "这两张表里有 web/ 下已经不存在的脚本：%s"
                         % sorted(known - actual))
        # 两张表不许重叠：同时出现在两边说明有人改名单时没想清楚它到底该不该跑。
        self.assertEqual(sorted(self._tuple("INLINE_SCRIPTS")
                                & self._tuple("DROPPED_SCRIPTS")), [],
                         "同一个脚本同时出现在内联表与丢弃表里")


class CssIdSelectorTestCase(unittest.TestCase):
    """样式表里那些**匹配不到任何东西**的选择器。

    这一节是本轮真踩出来的：上下文栏子智能体那三条规则写成了
    `.chat-side-subagent-list`（类），而它其实是 index.html 上的一个 **id**。
    三条规则**整条不生效**，症状是"那行标题重复了一遍、外面还多套一个边框"——
    加上面板里那一份本来就长这样，几乎不可能被当成缺陷看出来。**任何测试都没红**，
    它是靠截图发现的。

    这类"写错了但什么都不报"的选择器有两种可静态判定的形状，各一条：

      A. `#id` —— 这个 id 在 index.html 里根本不存在（改名之后的残留、拼错）；
      B. `.name` —— name 恰好是 index.html 里的一个 id，而它从没被当作类用过。
         这几乎一定是 `.` 与 `#` 写反了。

    **判 B 需要 JS 也一起看**：有一批类名是渲染函数造出来的、只在 JS 里出现
    （`.chat-subagent-row` / `.empty` …），只看 HTML 会把它们全判成可疑。
    """

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")
        # 先剥注释：说明性文字里提到 `#foo` 是常事，而注释里的选择器不参与匹配。
        cls._css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)
        cls._js = "\n".join(_read(name) for name in _scripts())

    def _selectors(self):
        out = []
        for sel in re.findall(r"([^{}]+)\{", self._css):
            sel = sel.strip()
            if sel and not sel.startswith("@"):
                out.append(" ".join(sel.split()))
        return out

    def test_id_selectors_exist_in_the_markup(self):
        ids = set(re.findall(r'\bid="([\w-]+)"', self._html))
        missing = []
        for sel in self._selectors():
            for name in re.findall(r"#([\w-]+)", sel):
                if name not in ids:
                    missing.append("%s  →  #%s" % (sel, name))
        self.assertEqual(sorted(set(missing)), [],
                         "这些规则引用了 index.html 里不存在的 id，整条是死的：%s"
                         % sorted(set(missing)))

    def test_class_selectors_are_not_ids_in_disguise(self):
        ids = set(re.findall(r'\bid="([\w-]+)"', self._html))
        classes = set()
        for chunk in re.findall(r'\bclass="([^"]*)"', self._html):
            classes.update(chunk.split())
        classes |= _js_class_names(self._js)
        suspects = []
        for sel in self._selectors():
            for name in re.findall(r"\.([\w-]+)", sel):
                if name in ids and name not in classes:
                    suspects.append("%s  →  .%s 应当写成 #%s" % (sel, name, name))
        self.assertEqual(sorted(set(suspects)), [],
                         "这些规则把 id 当类选了，整条是死的：%s"
                         % sorted(set(suspects)))


class SettingsNavContractTestCase(unittest.TestCase):
    """设置页导航与设置卡片必须一一对应，而且导航那几行标签要**写得干净**。

    两件事都是真实发生过的：

    * 加了一组设置（DSH 运行时）却忘了写导航按钮，或者反过来——这一组会永远打不开，
      而页面上看不出少了什么；
    * 导航里某一行被编辑成了**两个开标签叠在一起**
      （`<button … role="tab"       <button … role="tab" data-settings-group="dsh">`）。
      浏览器很宽容，照样解析出正确的按钮，所以肉眼看不出问题 —— 只有把那一行拿出来
      看才看得出它已经不是合法的标记了。
    """

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")

    def _nav(self):
        match = re.search(r'<nav[^>]*id="settings-nav".*?</nav>', self._html, re.S)
        self.assertIsNotNone(match, "找不到设置页导航")
        return match.group(0)

    def _nav_groups(self):
        return re.findall(r'data-settings-group="([\w-]+)"', self._nav())

    def _card_groups(self):
        return re.findall(r'<section[^>]*data-settings-group="([\w-]+)"', self._html)

    def test_every_nav_group_has_exactly_one_card_and_the_order_matches(self):
        nav, cards = self._nav_groups(), self._card_groups()
        self.assertEqual(sorted(nav), sorted(cards),
                         "导航与设置卡片对不上：多出/缺少 %s"
                         % sorted(set(nav) ^ set(cards)))
        self.assertEqual(len(set(nav)), len(nav), "导航里有重复的分组：%s" % nav)
        self.assertEqual(nav, cards, "导航顺序与卡片顺序不一致——切换会跳到别处")

    def test_the_nav_markup_is_not_double_tagged(self):
        """一个标签一行：`<button` 的个数要与 `</button>` 相等，且每行最多一个。"""
        nav = self._nav()
        self.assertEqual(nav.count("<button"), nav.count("</button>"),
                         "导航里的按钮标签不成对（多半是编辑时叠了一个开标签）")
        for line in nav.splitlines():
            self.assertLessEqual(line.count("<button"), 1,
                                 "这一行叠了两个开标签：%s" % line.strip())


class GraphClickGuardTestCase(unittest.TestCase):
    """图交互守卫：点选与拖拽的分界必须按**屏幕像素**判，不能按 viewBox 坐标。

    起因是一个真实缺陷，症状是"切换节点，节点详情不变"：判定写的是用户坐标里的
    `|dx| + |dy| > 0.6`，而 viewBox 往往比显示区域宽好几倍（实测 2190 用户单位
    挤在 456px 里，1 屏幕像素 = 4.8 用户单位）。于是鼠标只要抖 1 像素就跨过阈值，
    抬手时的点选被 `endDrag` 当成拖拽吞掉——只有"一点不动"的点击才选得中，
    而这在真实鼠标上基本不会发生。

    这类缺陷没有 DOM 断言很难钉住（需要真实 PointerEvent 序列），所以这里守的是
    **源码形状**：阈值是屏幕像素常量、按下时记了起点、判定在坐标换算之前。
    """

    @classmethod
    def setUpClass(cls):
        cls._js = _read("app.js")

    def _handler(self, event_name):
        """取出某个 svg 事件处理函数的源码片段（到下一个 addEventListener 为止）。"""
        start = self._js.index("svg.addEventListener('%s'" % event_name)
        nxt = self._js.find("svg.addEventListener('", start + 5)
        return self._js[start:nxt if nxt > 0 else len(self._js)]

    def test_slop_is_a_screen_pixel_constant(self):
        self.assertIn("CLICK_SLOP_PX", self._js, "缺少屏幕像素阈值常量")

    def test_press_origin_is_recorded_in_client_coordinates(self):
        body = self._handler("pointerdown")
        self.assertIn("pressX: event.clientX", body, "按下时必须记住起点，否则没得比")
        self.assertIn("pressY: event.clientY", body)

    def test_drag_decision_precedes_coordinate_conversion(self):
        """顺序是关键：先按屏幕像素判是不是拖拽，再按比例换算位移。

        反过来写就会把"换算后的坐标"变回判定依据，等于把缺陷原样写回去——
        而两条语句都在同一个函数里，光看"有没有出现 clientX"是分不出来的。

        **断言必须跑在去注释之后的代码上。** 本轮改写法时踩过一次：拖动改成不再
        逐事件做坐标换算，而新注释里解释这件事时正好写了 `toUserSpace` 这个词，
        于是原来的 `body.index("toUserSpace")` 命中的是**注释里的那个词**：测试照旧
        绿，而"顺序"这条实际上已经没人守了。假通过比红难发现——红会有人去看。
        """
        body = _strip_js_comments(self._handler("pointermove"))
        self.assertIn("graphView.pressX", body)
        self.assertIn("event.clientX", body)
        self.assertIn("graphView.moved = true", body)
        self.assertIn("graphView.dragScale", body, "位移没有用按下时冻结的比例")
        self.assertLess(body.index("graphView.moved = true"), body.index("graphView.dragScale"),
                        "必须先按屏幕像素判定拖拽，再按比例换算位移")

    def test_cancelled_gesture_is_not_a_click(self):
        """被系统取消的手势（右键菜单、触摸被打断）不该改选中态。"""
        self.assertIn("svg.addEventListener('pointercancel'", self._js)
        start = self._js.index("svg.addEventListener('pointercancel'")
        self.assertIn("true", self._js[start:start + 80],
                      "pointercancel 必须走「已取消」分支，否则一次取消会选出一个节点")


class GraphDragCostTestCase(unittest.TestCase):
    """链路拖动的**代价**：三样东西各自只许发生一次。

    守的是形状，而每条形状都对应一个在真浏览器里量出来的数——Edge 155 / 1440×900 /
    合并链路 352 节点 344 边（2806 个 SVG 元素）/ 初始缩放 2215%：

      * **逐事件读画布矩形**：`toUserSpace` 每次都 `getBoundingClientRect()`，而它紧跟
        在上一次 transform 写入之后，会把那一帧还没做的布局当场做完——**10.8 ms/次**，
        而一次 pointermove 读两次（当前点 + 上一点）→ 连发 200 次 **4190 ms**；
      * **逐事件写缩放读数**：多写那一行文本，同样的操作从 10.8 → **21.5 ms**，
        而屏幕上那个数字一个字都没变（平移不改缩放）；
      * **逐事件写 transform**：一帧里写几次就算几次布局，而只有最后一次看得见。

    修好之后同一套测量：连发 200 次 4190 → **1.2 ms**，读矩形 400 → **0** 次，
    帧间隔中位 27.8 → **7.0 ms**（36 → 142.9 FPS）。

    三条都在拖动路径上，而且**坏了看不出来**：图还是能拖，只是一帧一帧地卡。
    "能不能拖"那几条用例永远绿，这一组才是有牙的那条。
    """

    @classmethod
    def setUpClass(cls):
        cls._js = _read("app.js")

    def _handler(self, event_name):
        start = self._js.index("svg.addEventListener('%s'" % event_name)
        nxt = self._js.find("svg.addEventListener('", start + 5)
        return _strip_js_comments(self._js[start:nxt if nxt > 0 else len(self._js)])

    def test_drag_never_measures_the_canvas_per_event(self):
        """拖动路径里不许出现逐事件的坐标换算。"""
        body = self._handler("pointermove")
        self.assertNotIn("toUserSpace", body,
                         "拖动又变成逐事件读画布矩形了：它每次强制一次布局（实测 10.8 ms）")
        self.assertIn("event.clientX - graphView.lastX", body,
                      "位移没有写成「差值 × 冻结比例」——写成绝对值相减就必须重新量矩形")

    def test_the_scale_is_frozen_at_press_time(self):
        """比例在按下时量一次，整个手势用它。"""
        down = self._handler("pointerdown")
        self.assertIn("dragScale: measureDragScale()", down,
                      "按下时没有冻结比例，拖动只能退回逐事件量")
        self.assertIn("function measureDragScale()", self._js, "缺少冻结比例的那一步")

    def test_the_transform_is_written_once_per_frame_not_once_per_event(self):
        """一帧最多写一次 transform：拖动走队列，不自己写。"""
        body = self._handler("pointermove")
        self.assertIn("scheduleTransform()", body, "拖动没有走每帧一次的统一出口")
        self.assertNotIn("applyTransform", body,
                         "拖动自己写 transform：一帧里的每一次都算一遍布局，只有最后一次看得见")
        self.assertIn("function scheduleTransform()", self._js)

    def test_the_zoom_readout_is_only_written_when_it_changes(self):
        """缩放读数只在**显示出来的百分比真的变了**的时候写。

        无条件写它，是"多花一倍时间、屏幕上一个字都不变"（10.8 → 21.5 ms）。
        """
        body = _fn_body(self._js, "applyTransform")
        self.assertIn("graphView.zoomText === text", body,
                      "读数又变成无条件写了——平移时那个数根本没变")
        self.assertIn("return", body)

    def test_the_viewport_element_is_not_cached_but_the_readout_is(self):
        """`#graph-viewport` **不能**缓存，`#zoom-level` 可以。

        `renderGraph` 每次都 `clear(svg)` 之后重建 viewport，缓存下来的引用在第一次
        重画之后就指向一个已经不在文档里的节点——那之后所有的缩放平移都会写到一个
        没人看的节点上，而画面上完全看不出来（图就是不动）。
        读数那个节点是 index.html 里的静态标记，反过来缓存它才是对的那一边。
        """
        body = _fn_body(self._js, "applyTransform")
        self.assertIn("$('#graph-viewport')", body, "viewport 被缓存了：重画之后会写到死节点上")
        self.assertNotIn("graphView.viewport", body)


class GoalDeleteUiTestCase(unittest.TestCase):
    """目标行的删除按钮：它删的是**证据**，不是一条调度记录。

    界面这一侧要守两件事：

      * 外层必须是 `<div>` 而不是 `<button>`——`<button>` 里嵌 `<button>` 时浏览器会把
        内层那颗甩到外面，于是"点删除"实际点到的是外层，变成切换目标范围（一个不报错、
        只是做错事的失败）；
      * 确认框要带上这个目标有多少节点。一句话的确认框会把"删掉一个扫错的地址"与
        "丢掉一整轮作业的结论"问成同一件事。
    """

    @classmethod
    def setUpClass(cls):
        cls._js = _read("chat.js")
        cls._css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)

    def test_the_row_is_a_container_with_two_buttons(self):
        self.assertIn("const row = el('div', 'chat-goal'", self._js,
                      "外层必须是 div：button 里嵌 button 会被浏览器甩开")
        self.assertIn("'chat-goal-pick'", self._js)
        self.assertIn("'chat-goal-del'", self._js)

    def test_the_delete_hits_the_engagement_endpoint(self):
        self.assertIn("/delete", self._js)
        self.assertIn("engagements/", self._js)

    def test_the_confirm_says_how_much_evidence_goes_with_it(self):
        self.assertIn("全部证据", self._js)
        self.assertIn("不可撤销", self._js)

    def test_the_delete_button_is_always_visible(self):
        """常显而不是悬停才出现：悬停显形对键盘与触摸用户等于不存在。"""
        rules = {sel.strip(): " ".join(body.split())
                 for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", self._css)}
        rule = rules.get(".chat-goal-del", "")
        self.assertTrue(rule, "缺少 .chat-goal-del 规则")
        self.assertNotIn("opacity: 0", rule)
        self.assertNotIn("visibility: hidden", rule)


class SetupOptionalBlockTestCase(unittest.TestCase):
    """「部署」页签里的多机执行：**这一节是可选的，但默认展开**。

    它原本默认折叠，理由是"远程接入不是必经步骤，别占视野"。那个理由漏算了**已经在跑
    的节点**——折叠之后，一个在线节点只以「共 N 个 · 在线 M」出现在摘要行里，要看节点
    列表得先点开一次。而"这一节可选"与"这一节的内容要藏起来"是两件事：前者仍然成立
    （本机节点随控制面启动，单机部署不用碰它），后者在这一节真有内容时是净损失。

    以前它是两张平级卡片（节点接入 + 节点列表）摆在部署页签里，看上去像"必须先配好节点
    才能下发任务"，而事实上不配任何节点也能跑。
    """

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")
        cls._js = _read("app.js")

    def _block(self):
        start = self._html.index('id="multi-node"')
        return self._html[start:self._html.index("</details>", start)]

    def test_multi_node_is_a_details_block_and_starts_expanded(self):
        """默认**展开**（本版改的），但它仍然是 `<details>`。

        它原本默认折叠，理由是"远程接入不是必经步骤，别占视野"。那个理由漏算了
        **已经在跑的节点**：折叠之后，一个在线节点只以"共 N 个 · 在线 M"出现在摘要
        行里，要看节点列表得先点开一次。而"这一节是可选的"与"这一节的内容要藏起来"
        是两件事——前者仍然成立，正文第一句就写着单机不需要配。

        保留 `<details>` 而不是换成普通卡片：**能收起来**本身有价值，改的只是初值。
        """
        match = re.search(r"<details[^>]*id=\"multi-node\"[^>]*>", self._html)
        self.assertIsNotNone(match, "多机执行应当是一个 <details> 折叠块")
        self.assertIn(" open", match.group(0),
                      "默认必须展开：节点列表不该以「先点一下」为代价")

    def test_enroll_form_and_node_list_collapse_together(self):
        block = self._block()
        for anchor in ("enroll-name", "enroll-btn", "enroll-result", "executor-list"):
            self.assertIn(anchor, block, "%s 应随多机执行一起折叠" % anchor)

    def test_summary_says_single_machine_needs_nothing(self):
        """折叠时摘要是唯一可见的一行，它要回答"我需不需要打开这块"。

        只回显 `共 1 个 · 在线 1` 做不到这件事——看不出那一个是不是自己这台机器。
        同时这条断言也挡住"按节点名猜哪个是本机"的写法：本机节点的名字是可配的，
        猜错就会把唯一那个本机节点报成"0 个远程节点接入"，比不说更糟。
        """
        self.assertIn("executor-summary", self._html, "摘要行缺 id，JS 无从写入")
        body = self._js[self._js.index("executor-summary"):
                        self._js.index("executor-summary") + 1600]
        self.assertIn("executor-summary", self._js, "摘要行必须有渲染逻辑，否则永远停在占位文字")
        self.assertIn("单机部署无需配置", body, "要有「不用管它」这句结论")
        self.assertNotIn("=== 'local'", body,
                         "不得按名字等于 local 判本机节点：名字可配，判错会报出"
                         "「0 个远程节点接入」这种自相矛盾的状态")


class AuthzPanelTestCase(unittest.TestCase):
    """「侵入授权」必须在「部署」页签里有自己的面板。

    此前它只存在于两处：启动横幅，以及模型调 `get_authz` 的回答。而
    `GET /api/v1/authz` 早就逐类返回了 `enabled` / `flag` / `limits` / 目标白名单
    ——**没有任何界面消费它**。`flag`（"开启方式"）这个字段的唯一用途就是给界面
    显示，它从来没有被显示过；这与 `/agents` 曾经是死路由是同一个形状。

    这块面板最容易出的错不是"没做"，而是**做反了**：把默认态画成故障。全关是本
    平台的推荐默认（`authz.py` 的模块注释："默认全关不是保守，是唯一自洽的默认"），
    涂成红色警示会让人去"修"它——而"修"的方向恰好是把这道闸打开。
    """

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")
        cls._js = _read("app.js")
        cls._css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)

    def _rule(self, selector):
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", self._css):
            if " ".join(sel.split()) == selector:
                return " ".join(body.split())
        return ""

    def _decl(self, rule, prop):
        # 先剥掉规则体内的注释再找声明：`_top_rule` 折叠空白之后，注释与紧随其后的
        # 声明之间**没有分号**，而 `(?:^|;)` 这个锚点要求声明前面是行首或分号——
        # 于是那条声明读不到，断言报出来是"CSS 里没有这一条"。`.chat-log` 的 t61
        # 说明正好夹在 `padding:` 与 `padding-bottom:` 之间，就是这么红的一处**假红**：
        # CSS 是对的，挡住的是助手自己。
        body = re.sub(r"/\*.*?\*/", "", rule, flags=re.S)
        match = re.search(r"(?:^|;)\s*%s:\s*([^;]+)" % re.escape(prop), body)
        return match.group(1).strip() if match else ""

    def _left_color(self, rule):
        """左边框的颜色，两种写法都要读得出来。

        基础规则用速记 `border-left: 3px solid var(--muted)`，`.on` 只覆盖颜色、
        写成 `border-left-color`（避免把宽度常量抄两遍，改宽度时漏改一处）。
        只认其中一种的话，另一种会静默读成空串，断言报的是"CSS 里没有这一条"
        ——而 CSS 其实好好的，找错方向。
        """
        return self._decl(rule, "border-left-color") or self._decl(rule, "border-left")

    def _fn(self, name):
        """某个顶层函数的源码：到下一个顶层函数声明为止。

        不套用 `_function_body`：它找的是 `\\nfunction `，而紧跟在这些函数后面的是
        `async function loadAuthz` 一类，匹配不上——取到的会是后面很长一段代码，
        断言照样通过，却什么也没守住。
        """
        start = self._js.index("function %s(" % name)
        nxt = re.search(r"\n(?:async )?function ", self._js[start + 1:])
        return self._js[start:start + 1 + nxt.start()] if nxt else self._js[start:]

    def _render(self):
        return self._fn("renderAuthz")

    def _deploy_panel(self):
        panel = self._html[self._html.index('id="panel-setup"'):]
        return panel[:panel.index('id="panel-settings"')]

    def test_panel_exists_in_the_deploy_tab(self):
        """面板在「部署」页签里，与「探测范围」相邻但**各自成卡**。

        合成一行会让"能连哪些地址"与"能改哪些目标的状态"重新变成一个概念——而
        这两件事最容易被读成同一件，正是它们必须分开显示的理由。
        """
        for element in ('id="authz-card"', 'id="authz-panel"', 'id="authz-summary"'):
            self.assertIn(element, self._html, "缺少元素：%s" % element)
        panel = self._deploy_panel()
        self.assertIn('id="authz-card"', panel, "面板必须在「部署」页签内")
        self.assertLess(panel.index('id="setup-info"'), panel.index('id="authz-card"'),
                        "面板应在部署信息之后")

    def test_every_field_the_endpoint_returns_is_consumed(self):
        """接口返回什么，面板就得显示什么——否则接口白给了。"""
        body = self._render()
        # `toolkit` 不在这张列表里：本机工具清单归「部署 → 外部工具」卡
        # （`/api/v1/tools`），这里再显示一份就是同一件事两个来源（m01812）。
        for field in ("enabled", "flag", "limits", "targets"):
            self.assertIn(field, body, "面板未消费 /authz 的字段：%s" % field)
        self.assertIn("'/authz'", self._js, "面板没有拉取 /authz")

    def test_limits_come_from_the_server_not_from_the_frontend(self):
        """配额数字必须来自服务端那一份。

        写死数字的后果不是"显示得不对"，而是**部署侧改过配额之后界面还显示旧值**
        ——而使用者正是拿它判断"这次能试几次口令、能投几个载荷"的。
        """
        body = self._render()
        self.assertIn("item.limits", body, "配额应读 /authz 返回的 limits")
        self.assertNotRegex(body, r"max(?:Steps|Requests|Accounts|PayloadBytes)\s*:",
                            "配额数字不得在前端硬编码")

    def test_default_state_is_not_painted_as_a_fault(self):
        """全关是推荐默认 ⇒ 中性色；只有**已放行**的那几类才加亮。"""
        base = self._rule(".authz-item")
        self.assertTrue(base, "缺少 .authz-item 规则")
        self.assertIn("var(--muted)", self._left_color(base),
                      "未开启的行必须用中性色，不能用警示色")
        self.assertIn("var(--sev-medium)", self._left_color(self._rule(".authz-item.on")),
                      "已放行的行才加亮")
        # 灰底走的是 --muted-fill 而不是 --muted。拆出这个令牌是因为 --muted 同时
        # 承担"说明文字色"：亮色主题下它必须够深才读得清，而作为徽章底色又必须够浅
        # 才能压住前景字——同一个令牌被要求同时满足两个相反的方向。
        # 中性由 ThemeTokenTestCase 里"--muted-fill 默认就是 var(--muted)"那条钉死，
        # 这里只钉"用的是中性填充"。
        self.assertIn("var(--muted-fill)",
                      self._decl(self._rule(".authz-badge"), "background"))
        self.assertIn("var(--sev-medium)",
                      self._decl(self._rule(".authz-badge.on"), "background"))

    def test_markdown_emphasis_from_the_server_is_not_printed_as_asterisks(self):
        """`requires` 同时供给对话与模型，那边 `**` 是强调；面板走 textContent。

        `toolkit` 那一类的 `requires` 里带着 `**执行节点的权限**`，不过一道就原样
        印成星号——读者看到的是 markdown 的痕迹，而那句话说的是"这五类里最重的一类"，
        正是最该读清楚的一句。
        """
        body = self._render()
        self.assertIn("plainEmphasis(item.requires", body,
                      "requires 必须先去掉强调标记再上屏")
        self.assertIn("plainEmphasis(item.summary", body,
                      "summary 走同一条路，否则下一个带 ** 的字段还会漏")

    def test_boundary_with_probe_scope_is_stated_on_the_panel(self):
        """面板必须自己说清它与「探测范围」的关系。

        两者在页面上相邻，是整份界面里最容易被读成同一件事的两项。少这句话的后果
        很具体：使用者以为"地址已经加进探测范围了，所以我能打它"。
        """
        body = self._render()
        self.assertIn("取交集", body, "必须写明三道闸取交集")
        self.assertIn("可打", body, "必须写明「可连接的网段不等于可打的机器」")

    def test_endpoint_returns_what_the_panel_renders(self):
        """两侧契约对齐：面板读的每个字段，`/authz` 都得真的给。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = Store(os.path.join(tmp, "authz.db"))
            try:
                plane = ControlPlane(store, WEB_DIR, auth_required=False)
                status, payload = plane.get_authz({"query": {}})
            finally:
                store.close()
        self.assertEqual(status, 200)
        self.assertEqual(len(payload["classes"]), 5, "五类侵入动作必须都在")
        for item in payload["classes"]:
            for field in ("id", "label", "enabled", "flag", "limits", "summary"):
                self.assertIn(field, item, "类别 %s 缺字段 %s" % (item.get("id"), field))
            self.assertFalse(item["enabled"], "默认必须是全部关闭")
            self.assertEqual(item["flag"], "--allow-intrusive %s" % item["id"],
                             "未开启的类必须给出准确的开启方式")
        self.assertEqual(payload["targets"], [], "白名单默认为空")
        self.assertFalse(payload["anyEnabled"])
        # 字段既然没人消费，就不该继续留在接口上——工具清单由 `/api/v1/tools` 给。
        self.assertNotIn("toolkit", payload,
                         "本机工具清单归「外部工具」卡，不在 /authz 里")

    def test_switches_render_only_when_the_server_says_editable(self):
        """开关由服务端的 `canEdit` 决定，不由前端读角色名。

        前端在无鉴权模式下拿到的账号信息是空的——让它自己推会推出"这个人不能改"，
        而那个模式下其实谁都能改。权限的判定只应有一处，而且那一处必须能看见
        "这次请求有没有经过鉴权"。
        """
        body = self._render()
        self.assertIn("canEdit", body, "面板必须读服务端给的 canEdit")
        self.assertIn("authz-toggle", body, "可编辑时才画开关")
        self.assertIn("authz-readonly", body,
                      "只读账号要看到一行解释，而不是一颗点不动的按钮")
        with open(os.path.join(ROOT, "pentestd", "server.py"),
                  "r", encoding="utf-8") as handle:
            self.assertIn("canEdit", handle.read(),
                          "服务端必须真的下发这个字段（面板读了它才有意义）")

    def test_the_write_is_a_whole_state_not_a_patch(self):
        """提交整份状态，不做增量补丁。

        补丁式接口（`{add: 'exploit'}`）在"两次点击之间有人从别处改了同一份状态"
        时会叠加成一个谁都没要求过的结果；整份提交天然幂等，也与服务端那条
        "同一状态重复提交不写流水"的幂等语义配套。
        """
        body = self._fn("submitAuthz")
        self.assertIn("apiPost('/authz'", body)
        self.assertIn("enabled", body)
        self.assertNotIn("add:", body, "不要补丁式的 add")
        self.assertNotIn("remove:", body, "不要补丁式的 remove")

    def test_whitelist_three_states_are_all_stated(self):
        """白名单的三态都要能在界面上说出来。

        **界面不能从 `targets` 是否为空推断形态**：空与 `none` 字面上都是"没有目标"，
        意思却相反——一个全放行、一个全拒绝。所以形态读服务端给的 `targetMode`，
        整句话读 `targetsText`（三个入口共用一份措辞）。少了这一句，使用者看到空白名单
        时会以为它必填，而那个误会会让他去填一个本来不需要的地址。
        """
        body = self._render()
        self.assertIn("targetMode", body, "形态要读服务端给的，不要自己从长度推断")
        self.assertIn("targetsText", body, "整句由服务端给（与启动横幅共用一份措辞）")
        self.assertIn("留空 = 不限制目标", body, "输入框的占位要说明留空是什么意思")
        self.assertIn("'none'", body, "显式拒绝也要有一个写得出来的形式")

    def test_fixture_primes_the_authz_endpoint(self):
        """夹具必须预抓 `/authz`。

        夹具的假后端只回放它预抓过的接口（`ENDPOINTS`），漏一个的后果**不是空白，
        而是"加载失败：夹具未准备该接口"**——那和真实缺陷长得一模一样，排查时会
        一路往后端查。加面板时已经踩过一次，这条断言是为了不用再踩第二次。
        """
        with open(os.path.join(ROOT, "tools", "render_ui_fixture.py"),
                  "r", encoding="utf-8") as handle:
            source = handle.read()
        self.assertIn('"/api/v1/authz"', source,
                      "夹具的 ENDPOINTS 里没有 /authz，面板在夹具里会显示成加载失败")

    def test_no_surface_still_claims_there_is_no_panel(self):
        """做成面板之后，"界面上看不到"这句话必须从各处消失。

        留着它的后果与那条被清除过的旧口径同型：**一条声称平台不会做某件它已经
        会做的事的说明，比没有说明更糟**——它看起来可信，而使用者无法核对。
        """
        root = os.path.dirname(WEB_DIR)
        for name in ("使用手册.md", "架构设计.md", "项目说明.md", "交互规范.md",
                     os.path.join("web", "guide.js")):
            path = os.path.join(root, name)
            if not os.path.exists(path):
                continue
            with open(path, "r", encoding="utf-8") as handle:
                text = handle.read()
            self.assertNotIn("没有图形面板", text,
                             "%s 仍在说侵入授权没有面板。若这里其实是**引用旧措辞**"
                             "并说明它已不成立，请改成不含该字面的写法——这条断言按字面"
                             "检查，分不出「声称」与「引用」" % name)
            self.assertNotIn("唯一能看到侵入授权状态的地方", text,
                             "%s 仍把问模型说成唯一途径（同上，按字面检查）" % name)


class SubagentNoticeTestCase(unittest.TestCase):
    """侧边栏那条「派活口径」常驻说明**已被移除**，这里守的是"它不回来"。

    它原来讲的是用户**看不见**的那件事：插件人设要求用 `subagent` 派活，而本平台
    没有该工具、由 `agents.MISSING_TEMPLATE` 改写成 `delegate`。移除是一次产品决定，
    理由与代价写在 `test_subagent_notice_is_gone` 的 docstring 里。

    **为什么移除也要有守卫**：没有它，下一次有人顺手把那段 `<p>` 加回来，界面上就会
    多出一行谁都没要求过的说明。反向断言是"移除"这个动作的产物，不是它的补充——
    断言不写下来，移除在下一次编辑里就不成立。
    """

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")
        cls._js = _read("chat.js")
        # 注释要先剥掉再精确查规则：`_rules` 那种"按 {} 切"的写法会把上一条规则与
        # 本条之间的注释并进选择器组（`.chat-entry-hint` 的既有断言就因此要写
        # startswith("/*")）。注释里写着"与 .chat-entry-hint 同规格"，不剥掉的话
        # 恰好会命中这句注释，断言就成了自证。
        cls._css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)

    def _rule(self, selector):
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", self._css):
            if " ".join(sel.split()) == selector:
                return " ".join(body.split())
        return ""

    def _decl(self, rule, prop):
        # 先剥掉规则体内的注释再找声明：`_top_rule` 折叠空白之后，注释与紧随其后的
        # 声明之间**没有分号**，而 `(?:^|;)` 这个锚点要求声明前面是行首或分号——
        # 于是那条声明读不到，断言报出来是"CSS 里没有这一条"。`.chat-log` 的 t61
        # 说明正好夹在 `padding:` 与 `padding-bottom:` 之间，就是这么红的一处**假红**：
        # CSS 是对的，挡住的是助手自己。
        body = re.sub(r"/\*.*?\*/", "", rule, flags=re.S)
        match = re.search(r"(?:^|;)\s*%s:\s*([^;]+)" % re.escape(prop), body)
        return match.group(1).strip() if match else ""

    def test_subagent_notice_is_gone(self):
        """侧边栏「子智能体」块下面那行常驻说明**已按产品决定移除**。

        它原来是全平台唯一一处把"插件人设写的是 DSH 的 `subagent`、本平台用的是
        `delegate`"送到界面上的地方。移除的理由是对比出来的：这行**永远在**，而它
        讲的那件事只在选中插件预设时才成立；内置与自定义档下它退化成一句通用的能力
        边界，占着侧边栏的高度说一件与当前选择无关的事。同样的话现在在「使用指南」
        页（`guide.js`）里，需要时去看。

        **代价要写明**：使用者不再有一个常驻的地方去核对"助手说的派活是哪一个"。
        模型侧没变——`agents.MISSING_TEMPLATE` 仍然要求它换名字与形态说话，所以
        模型不会说错；变的是"用户能不能一眼看出来"，它从常驻降级成了翻指南。

        这条用例守的是**移除本身**：没有它，下一次有人顺手把那段 `<p>` 加回来，
        界面上就会多出一行谁都没要求过的说明——而它读的是 `agents.public()` 的
        `delegation` 字段，那个字段今天已经没有任何界面消费者了（仍在算、仍被测，
        见 `test_plugin_session.py`）。
        """
        self.assertNotIn('id="chat-subagent-note"', self._html, "提示元素又回到了 index.html")
        self.assertNotIn("renderSubagentNote", self._js, "提示渲染函数又回来了")
        self.assertNotIn("byId('chat-subagent-note')", self._js, "dom 绑定又回来了")
        # 样式要按**整条选择器**判，不能按子串：`.chat-subagent-note-line`（子智能体
        # 行里那句原因，仍在用）含有 `.chat-subagent-note` 这个前缀，用 assertNotIn
        # 一定会命中它——那种断言在移除**成功**时反而报红，是反向的假阳性。
        self.assertFalse(self._rule(".chat-subagent-note"), "提示的样式又回来了")
        self.assertFalse(self._rule(".chat-subagent-note.warn"), "暖色变体又回来了")
        # 旧口径在今天仍然是假话。这条反向断言与移除无关，继续留着。
        for stale in ("不提供 subagent", "不会真的派出子智能体", "改写为按阶段顺序执行"):
            self.assertNotIn(stale, self._js,
                             "提示正文还留着旧口径，现在是假话：%s" % stale)


class SourceEscapeHygieneTestCase(unittest.TestCase):
    """全仓 Python 源码在 `-W error::SyntaxWarning` 下必须能编译。

    起因是一个真实缺陷：`tools/render_ui_fixture.py` 里那段注入到夹具的 JS 含有
    `replace(/\\s+/g, ' ')`，而承载它的字符串**漏了 `r` 前缀**。Python 不认 `\\s`
    这个转义，于是发了 SyntaxWarning —— 但脚本照样能跑，因为 Python 对不认识的
    转义**原样保留**（这正是它能"工作"的原因）。这类问题只在 `-W error` 下才现形，
    而它的终局是变成硬错误：靠"未定义行为"跑通的代码，某天升级解释器就停止工作，
    且症状是脚本直接报语法错，与当初引入它的那行 JS 毫无关系。

    所以这里守的不是某个具体转义，而是**会进仓库的那棵树**：任何 `.py` 出现无效
    转义即失败，免得下次换一段正则、换一个人写，又静悄悄地依赖上同一条容忍规则。

    `.scratch/` 不在守卫范围：那是每轮的一次性补丁与探针脚本（证据，不是交付物），
    里面出现半截代码、正则片段是正常的，把它们当源码校验会让守卫天天误报，最后
    被加一串 `# noqa` —— 守卫失效的方式往往就是先变得吵。

    `workspace/` 同理：那是对话里产生的一次性脚本，跟着每一次对话长出新目录，
    不属于"会进仓库的那棵树"（`.gitignore` 也排除了它）。
    """

    def test_every_python_file_compiles_without_syntax_warnings(self):
        import warnings

        failures = []
        checked = 0
        for dirpath, dirnames, filenames in os.walk(ROOT):
            # __pycache__ 是产物；夹具目录里可能有临时文件；.scratch 是一次性脚本
            dirnames[:] = [d for d in dirnames
                           if d not in ("__pycache__", ".render-fixture", "data",
                                        "node_modules", ".scratch", "workspace")]
            for name in sorted(filenames):
                if not name.endswith(".py"):
                    continue
                path = os.path.join(dirpath, name)
                checked += 1
                rel = os.path.relpath(path, ROOT)
                try:
                    # utf-8-sig：带 BOM 的源码由解释器自己剥掉（PEP 263），
                    # 而 compile() 拿到的是字符串，BOM 会变成 U+FEFF 被当成非法字符。
                    # 这里按解释器的读法读，守卫才不会把合法 BOM 报成转义错误。
                    with open(path, "r", encoding="utf-8-sig") as handle:
                        source = handle.read()
                    # 用 compile() 而不是 py_compile.compile()：后者要落一个 .pyc，
                    # 而 Windows 上 os.devnull（"nul"）不是合法的 cfile 路径，
                    # 于是每个文件都会被误报成失败——守卫自己先坏了。
                    with warnings.catch_warnings():
                        warnings.simplefilter("error", SyntaxWarning)
                        compile(source, rel, "exec")
                except Exception as exc:                     # noqa: BLE001 —— 报出全部
                    failures.append("%s: %s" % (rel, exc))

        self.assertGreater(checked, 30, "没走到几个文件，检查范围本身就有问题")
        self.assertEqual([], failures, "存在无效转义（把承载它的字符串改成 r\"\"\" 即可）：\n"
                         + "\n".join(failures))


class SubagentStreamContractTestCase(unittest.TestCase):
    """「子智能体」流事件的前端契约：认领、按 index 归并、兜底，以及它画在哪。

    后端往**同一条** SSE 流上插播了 `subagent` 帧（`pentestd/subagent.py`），前端要
    在"正在生成"的那条气泡里把它画成一组按子智能体分行、可原地更新的区块。四条里
    最后一条最要紧，它守的不是"少画一行"，而是"整轮对话会不会被打断"：

      * 事件认领——`case 'subagent'` 确实在流循环里；
      * 四种 phase——`start` / `step` / `tool` / `done` 都被识别；
      * 同一个 `index` 是**更新**那一行，不是再开一行（否则一个跑 2 步、调 1 次工具的
        子智能体会占 4 行，而 `total` 说这次只派了 3 个）；
      * 不认识的 phase 一律忽略、绝不抛。

    最后一条不是假想：后端此刻就在发第五种 `phase: "note"`（报告格式没跟上时），
    它不在契约的四类里。渲染期抛异常的表现是整条流的消费循环终止——屏幕上正往外
    冒的字停住、还弹一条错误，而症状离"多了一帧进度事件"非常远。

    渲染本身另有 `tools/render_ui_fixture.py --subagent --png` 一张图可看：那三个
    状态（跑动中 / 已完成 / 失败）是不是真的画出来了，静态断言说不了。
    """

    @classmethod
    def setUpClass(cls):
        cls._js = _read("chat.js")
        # 注释必须先剥掉：本文件既有断言就吃过这个亏（`_rules` 按 {} 切规则时会把上一条
        # 规则与本条之间的注释一起吃进选择器组）。而这里的注释里恰好写着
        # `.chat-subagent-*` 这些选择器名和"绝不抛"这类词——不剥掉，断言全是自证。
        cls._css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)

    @staticmethod
    def _function(js, name):
        """取出 IIFE 内的一个函数源码（到下一个同级 function 为止）。"""
        start = js.index("function %s(" % name)
        nxt = js.find("\n  function ", start + 1)
        return js[start:nxt if nxt > 0 else len(js)]

    def _rule(self, selector):
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", self._css):
            if " ".join(sel.split()) == selector:
                return " ".join(body.split())
        return ""

    def test_stream_loop_claims_the_subagent_event(self):
        self.assertIn("case 'subagent':", self._js, "流循环没有认领 subagent 事件")
        self.assertIn("applySubagent", self._js, "没有把这一帧交给归并函数")

    def test_every_phase_is_recognized(self):
        """契约里的 phase 一个都不能漏——漏掉的那个会被兜底静默丢掉。

        `note` 是**后端真会发**的第五种（`subagent.py` 在报告没按三行格式给出时发，
        见 `ChildResult.note`）。只冻结四种的话，这一帧会被当成未知事件忽略，
        于是"报告没排好版"这件事在界面上永远不显示。
        """
        body = self._function(self._js, "applySubagent")
        for phase in ("'start'", "'step'", "'tool'", "'note'", "'done'"):
            self.assertIn(phase, body, "applySubagent 没有处理 phase %s" % phase)

    def test_persisted_subagents_are_rendered_after_a_refresh(self):
        """刷新之后那一块要还在——结论留下了、依据不能没。

        落库在 `chat._subagent_rows()`（形状与流里的行逐键一致），前端这里必须
        用**同一个渲染器**重画，而不是只在流里画一次。
        """
        body = self._function(self._js, "messageNode")
        self.assertIn("message.subagents", body, "落库的子智能体行没有被读")
        self.assertIn("subagentBlock(", body,
                      "落库的行没有走流里那个渲染器——另写一份迟早会长歪")

    def test_same_index_updates_the_row_instead_of_adding_one(self):
        """`index` 相同的事件属于同一个子智能体——查找必须在新建之前。"""
        row = self._function(self._js, "subagentRow")
        self.assertIn("one.index === index", row,
                      "没有按 index 找那一行，同一个子智能体会被画成好几行")
        self.assertIn("rows.push(", row, "找不到时应当新建一行")
        self.assertLess(row.index("one.index === index"), row.index("rows.push("),
                        "先查后建的顺序反了：那条 filter 成了死代码，每帧都会新开一行")

    def test_unknown_phase_is_ignored_and_never_throws(self):
        body = self._function(self._js, "applySubagent")
        self.assertIn("SUBAGENT_PHASES", body, "phase 白名单没了")
        self.assertIn("indexOf(phase) < 0", body, "缺少「不认识的 phase 就放行」的判据")
        self.assertIn("return false", body, "不认识的 phase 必须直接返回")
        self.assertNotIn("throw", body, "未知 phase 不能抛异常：会打断整条流")

    def test_fallback_precedes_any_state_mutation(self):
        """兜底必须在动任何状态之前。

        排在后面（比如先把行建出来、再看 phase）会留下一个"行已经出现、状态和内容
        却没更新"的半截状态——而它看起来和一个正常的行没有区别。
        """
        body = self._function(self._js, "applySubagent")
        self.assertLess(body.index("return false"), body.index("subagentRow("),
                        "兜底排在了建行之后")

    def test_block_is_rendered_inside_the_live_bubble(self):
        """挂在"正在生成"的那条气泡里，且由 `renderLive` 触发——不是另开一块。

        子智能体区块与既有的工具芯片同处一条消息：派活是同步的，父轮要等它们全跑完，
        这一块本来就是那一轮的内容。
        """
        live = self._function(self._js, "renderLive")
        self.assertIn("subagentBlock", live, "实时气泡没有画子智能体区块")
        self.assertIn("view.live.subagents", live)
        block = self._function(self._js, "subagentBlock")
        self.assertIn("subagentNode", block, "区块没有逐行渲染")
        self.assertIn("rows.forEach", block)
        # 一行一个子智能体：数量取自归并结果，不随事件条数增长
        self.assertIn("rows.length", block)

    def test_row_shows_state_steps_and_summary(self):
        node = self._function(self._js, "subagentNode")
        for anchor in ("chat-subagent-index", "chat-subagent-role", "chat-subagent-state",
                       "chat-subagent-meta", "chat-subagent-summary",
                       "chat-subagent-error", "chat-live-chip"):
            self.assertIn(anchor, node, "子智能体那一行缺少：%s" % anchor)
        self.assertIn("SUBAGENT_STATE", node, "状态文案应当走映射表")
        for label in ("跑动中", "已完成", "失败"):
            self.assertIn(label, self._js, "缺少状态文案：%s" % label)
        # 工具芯片复用 live 那套类名，不另造一份
        self.assertIn("chat-live-chip-name", node)
        self.assertIn("chat-live-chip-time", node)

    def test_subagent_case_does_not_touch_the_parent_text(self):
        """子智能体帧不能像 `tool` 帧那样把父轮正文切成"已完成段落"。

        `tool` 分支里那句 `live.segments = … live.text = ''` 是有前提的：模型说完
        一段话才去调工具。子智能体在跑的时候父轮并没有"说完一段话"，照搬会把同一段
        回答切成好几块——而切完的每一块都长得像一条独立结论。
        """
        start = self._js.index("case 'subagent':")
        body = self._js[start:self._js.index("default:", start)]
        self.assertIn("applySubagent", body)
        self.assertNotIn("live.segments", body)
        self.assertNotIn("live.text = ''", body)

    def test_block_styles_reuse_the_existing_tokens(self):
        """样式：四种状态各有规则，颜色取自本表顶部的变量。"""
        self.assertTrue(self._rule(".chat-subagents"), "找不到 .chat-subagents 规则")
        for state in ("running", "done", "failed"):
            rule = self._rule(".chat-subagent-row.%s" % state)
            self.assertTrue(rule, "缺少状态规则：.%s" % state)
            self.assertIn("var(--", rule, "状态色应当复用变量：%s" % state)
        self.assertIn("var(--danger)", self._rule(".chat-subagent-row.failed"))
        self.assertIn("var(--ok)", self._rule(".chat-subagent-row.done"))
        # 长结论（"10.0.0.0/24 有 7 台存活主机…"）必须能换行，否则会把气泡撑宽
        self.assertIn("overflow-wrap: anywhere",
                      self._rule(".chat-subagent-summary"))

    def test_the_error_prefix_follows_the_state(self):
        """`error` 装的是**两件事**：跑挂了（failed）与"跑完但被截断"（done）。

        一律写成"未完成："会让一行同时挂着绿色的「已完成」和红色的「未完成：」——
        而这一行正是使用者判断"这趟算不算数"的地方，自相矛盾比不显示更糟。所以
        前缀与颜色都跟状态走，而且**两个渲染点（实时、落库）必须走同一个函数**：
        分开写的话，刷新前后同一行会给出两种说法。
        """
        body = self._function(self._js, "subagentErrorLine")
        self.assertIn("'done'", body, "前缀没有按状态分叉")
        self.assertIn("受限：", body)
        self.assertIn("未完成：", body)
        node = self._function(self._js, "subagentNode")
        self.assertIn("subagentErrorLine(", node, "实时渲染点绕过了前缀函数")
        # 上下文栏那一支（本轮加的）**不是**第二个渲染点：它把行交给同一个
        # subagentBlock()，所以前缀函数一并管住了它。这一句是 1588 那条用例的
        # 补充断言——那里查的是"侧栏块存在"，这里查的是"它走的是同一个渲染器"，
        # 因为 1588 名字里的 "single renderer" 只有在这一句成立时才为真。
        side = _fn_body(self._js, "renderSideSubagents")
        self.assertIn("subagentBlock(", side, "上下文栏那一支绕过了共享渲染器")
        # 反面断言：当年逐行自绘的那条路径（sideSubagentRow）不许回来。它渲染的是
        # `message.subagents`，和主区是同一份数据却只有摘要——两者对不上时读的人
        # 没有依据判断该信哪一份。剥掉注释再查（同
        # test_topbar_no_longer_owns_the_target_selector）：这条守的是"没有真的
        # 引用它"，不是"这几个字没有出现过"——记录这段来龙去脉的注释里恰好写着它。
        code = re.sub(r"//[^\n]*", "", self._js)
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
        self.assertNotIn("sideSubagentRow", code,
                         "子智能体在上下文栏里的重复渲染路径又回来了："
                         "同一份数据两处渲染、两处详细程度不同时，没人知道该信哪一份")
        # 被截断（done）那一行里那句原因不能再是红的——红字配绿状态是同一处矛盾
        self.assertIn("var(--text-dim)",
                      self._rule(".chat-subagent-row.done .chat-subagent-error"))


class PluginIsolationUiContractTestCase(unittest.TestCase):
    """插件隔离在界面上的契约。

    隔离这一层最麻烦的地方是**它在界面上看不见**：装成功了、列表出现了，副作用却
    可能落在电脑上那份 DSH 里。后端有独立的用例去盯目录，界面这一侧则要盯两件事：

      1. 文案不能再说反话（"这会在本机 DSH profile 里跑 pnpm" 之类）；
      2. 那一整块说明**拆干净了**——它以前把两个 home、已采纳/未采纳清单、以及
         "从本机清单迁移插件"的入口都摆在这里，现在插件相关的界面只剩插件市场里
         那三节：目录、已安装、操作记录。

    第 2 条不是形式主义：拆一半的形态（HTML 去掉、渲染函数留着）在任何后端用例里
    都是绿的，而那段函数会一直随代码漂移，直到某天有人"修好"它、把它再接回去。
    """

    @classmethod
    def setUpClass(cls):
        cls._js = _read("settings.js")
        cls._css = _read("style.css")
        cls._html = _read("index.html")

    def test_the_old_claims_that_installs_touch_the_local_dsh_are_gone(self):
        """这三句在隔离之后全是错的，而且它们恰好是**最有说服力**的那种错误：
        使用者会照着它去预期"我的 DSH 里也会多一个插件"，然后发现没有。"""
        for stale in ("这会从本机 DSH profile 卸载",
                      "这会在本机 DSH profile 里跑 pnpm",
                      "完成后需要重启 DSH",
                      "插件在 DSH 侧要重启才会加载运行时"):
            self.assertNotIn(stale, self._js, "隔离后不该再出现：%s" % stale)

    def test_the_isolation_block_is_gone_and_nothing_is_left_over(self):
        """那块「DSH 插件集成状态」整块去掉了，而且**不许留半截**。

        半拆有三种残留，每一种都不报错、也没人会发现：

          · 渲染函数留着而没人调 —— 一段永远跑不到、却还在随代码漂移的死代码；
          · `dom.dshStatus` 留着 —— 一个恒为 null 的缓存项，而所有 `if (dom.x)`
            都静默跳过，看起来与"这个部署没有这一块"一模一样；
          · `.dsh-*` 规则留着 —— 它们已经匹配不到任何元素，是死样式。

        所以三样一起查：标记、渲染入口、样式。哪一样回来，都算这块没拆干净。
        """
        self.assertNotIn('id="dsh-status"', self._html)
        self.assertNotIn("renderDshStatus", self._js)
        self.assertNotIn("dom.dshStatus", self._js)
        for name in ("renderIsolation", "renderMachineOffer", "adoptPlugin"):
            self.assertNotIn("function " + name, self._js,
                             "渲染入口还在，成一具尸体了：%s" % name)
        leftovers = sorted(set(re.findall(r"\.dsh-[\w-]+", self._css)))
        self.assertEqual(leftovers, [],
                         "这些样式规则已经匹配不到任何元素了：%s" % leftovers)

    def test_the_empty_state_does_not_cry_wolf(self):
        """`available: false` 在隔离之后几乎总是"项目内还没装插件"（正常状态）。

        若照旧说"未在本机检测到 DSH 安装"，使用者会以为集成坏了——而它其实只是
        空着，装一个就行。所以这里要求那段文案按 `isolated` 分支。
        """
        self.assertIn("项目内还没有安装任何插件", self._js)
        self.assertIn("未检测到 DSH 安装，已安装列表为空", self._js)

    def test_a_failed_snapshot_is_not_reported_as_signed_out(self):
        """拉取失败不能显示成"登录后加载……"。

        两件事的下一步动作完全不同：没登录要去登录，拉取失败要去看接口。合并成
        一句话的症状是"我已经登录了，它却让我登录"。
        """
        self.assertIn("'插件状态加载失败：' + view.dshError", self._js)

    def test_the_ops_block_is_foldable_through_the_shared_mechanism(self):
        """操作记录用 fold.js 那一套，**不是再写一个 `<details>`**。

        `<details>` 也能折，但它折在哪儿不落设备，刷新之后自己弹开——那正是
        fold.js 存在的理由。真正要紧的是结构：`[data-fold-part]` 容器 + 一个
        `.panel-head.fold-head` 按钮 + **直接子级**的 `.panel-body`。最后这条是
        承重的，"折起时藏正文"那条规则写的是 `.is-folded > .panel-body`；
        隔一层就整条不生效，而症状是"点下去箭头翻了、内容还在"。
        """
        self.assertIn('data-fold-part="hub-ops"', self._html)
        self.assertIn('data-fold="hub-ops"', self._html)
        self.assertIn('class="panel-body hub-ops" id="hub-ops"', self._html,
                      "正文不是折叠部件的直接子级 .panel-body，藏不起来")
        for selector in (".settings-fold-block", ".settings-fold-row"):
            self.assertIn(selector, self._css, "缺少样式规则：%s" % selector)

    def test_the_ops_clear_button_posts_to_the_admin_route(self):
        """清空走 `POST /hub/ops/clear`，而且**确认框要把"保留"写成数字**。

        只写"将清空操作记录"是不准确的：执行中那几条会被留下（后端默认只清已
        终结的，理由见 `store.clear_plugin_ops`），而"留下的东西没被事先说过"
        正是那种事后才发现的选择。
        """
        self.assertIn('id="hub-ops-clear"', self._html)
        self.assertIn("clearHubOps", self._js)
        self.assertIn("'/hub/ops/clear'", self._js)
        start = self._js.index("async function clearHubOps")
        body = self._js[start:self._js.index("\n  }", start)]
        self.assertIn("已结束", body)
        self.assertIn("条会保留", body)
        self.assertIn("跨用户", body)

    def test_the_migrated_plugin_is_installed_into_the_project(self):
        """安装确认框要写明落点：这是使用者唯一一次能看见"装到哪儿"的机会
        （后面的操作记录只记目标串）。"""
        start = self._js.index("async function installPlugin")
        body = self._js[start:self._js.index("\n  }", start)]
        self.assertIn("装进项目自己的插件目录", body)
        self.assertIn("与电脑上的 DSH 隔离", body)

    def test_a_reason_is_not_pointed_at_a_section_that_is_gone(self):
        """已安装列表里不许再把"预设没变成 Agent"的原因写成"见下"。

        那句话原来指向状态区里那份「已发现但未采纳」清单，而那一块已经拆掉——
        "见下"却没有下文，读起来像**界面漏渲染了一段**，于是没人会去怀疑是插件
        自己的声明有问题。原因必须就地给出，取自 `/dsh` 的 `ignored`。
        """
        self.assertNotIn("原因见下", self._js)
        self.assertIn("function ignoredReason", self._js)
        self.assertIn("ignoredReason(item.name)", self._js)

    def test_the_in_app_guide_does_not_offer_a_removed_entry(self):
        """页内使用说明里不许再指向那个撤掉的迁移入口。

        这一条是**真发生过**的：设置页那块拆掉之后，`guide.js` 里还写着"状态区里
        另有一份「本机已装」清单，每个后面跟着「装入本项目」"——而界面上既没有
        状态区、也没有那个按钮了。说明书写了一个不存在的入口，比不写更糟：使用者
        会照着去找，找不到，然后**合理地怀疑是界面坏了**。

        它也是这一轮唯一一处**只读被改的那个文件发现不了**的残留：那句假话在另一个
        界面文件里。凡"删掉一块界面"都要连说明书一起搜——所以这条按字面钉住。

        **第二次改动（同一类）**：原先这里钉的是"界面上已经没有这个入口"，因为那时
        入口没了、接口还在。这一版连接口一起撤了（`POST /api/v1/hub/adopt` 回 410），
        于是"要迁移就直接调接口"本身成了指向死路的半真话。所以断言跟着换成"说明书
        必须说出替代动作"——**能力变了，钉住的字也得跟着变**，否则这条守卫会反过来
        保护一句过期的话。
        """
        guide = _read("guide.js")
        self.assertNotIn("状态区里另有一份", guide)
        self.assertNotIn("要迁移就直接调接口", guide)
        self.assertIn("现在回 **410**", guide)
        self.assertIn("搜索框", guide)

    def test_every_local_call_target_exists(self):
        """settings.js 里调用的本地函数必须真的定义过。

        起因是一个真实缺陷：新加的 `adoptPlugin` 里写了 `startOpWatch()`，而那个名字
        压根不存在（真实函数叫 `syncOpsPolling`）。它会抛 `ReferenceError`——但只在
        **用户点击那一刻**，任何静态检查与后端用例都不会发现，表现是"点了没反应"。

        所以这里逐个核对调用点与定义。允许清单只放两类：浏览器/宿主注入的全局，
        以及本文件里用 `const` 声明的东西（`el` / `asArray` 就是注入进来的）。
        """
        defined = set(re.findall(r"function\s+([A-Za-z_$][\w$]*)\s*\(", self._js))
        defined |= set(re.findall(r"(?:const|let|var)\s+([A-Za-z_$][\w$]*)\s*=",
                                  self._js))
        injected = {"el", "asArray", "ui", "byId", "clear", "window", "document",
                    "setTimeout", "setInterval", "clearInterval", "clearTimeout",
                    "encodeURIComponent", "decodeURIComponent", "parseInt",
                    "parseFloat", "isNaN", "String", "Number", "Boolean", "Array",
                    "Object", "JSON", "Math", "Date", "Set", "Map", "Promise",
                    "RegExp", "Error", "fetch", "confirm", "alert", "requestAnimationFrame",
                    "AbortController", "URLSearchParams", "console", "localStorage",
                    # 关键字与语句形式。`async (event) => {}` 这种写法会被下面的正则
                    # 抓成"调用 async"，所以关键字必须一起放进来。
                    "async", "await", "if", "else", "for", "while", "do", "switch",
                    "case", "break", "continue", "catch", "finally", "try", "throw",
                    "return", "typeof", "instanceof", "function", "class", "extends",
                    "new", "delete", "void", "in", "of", "yield", "super", "this"}
        # 只在"语句或表达式位置"上出现的裸调用：行首、`= ` 之后、`return ` 之后。
        # 不做通用 JS 解析——这条例行检查的价值在于零维护地挡住"名字写错"，
        # 而一个会误报的检查很快就会被关掉。
        called = set()
        for match in re.finditer(r"(?:^|[=(,;:?]|\breturn\b|\bawait\b)\s*"
                                 r"([a-z][\w$]*)\s*\(", self._js, re.M):
            called.add(match.group(1))
        unknown = sorted(called - defined - injected)
        self.assertEqual([], unknown,
                         "调用了未定义的本地函数（会抛 ReferenceError）：%s" % unknown)

def _fn_body(js: str, name: str) -> str:
    """取一个函数体的源码（到下一个顶层 `function` / 段注释为止）。

    JS 没有解析器可用，所以这里的"结构断言"只能是文本层面的。它挡不住所有
    改写，但挡得住最常见的那种回退：**在某处又加一条加载路径**。写下每条的
    失败信息时就按这个目的写——"这会被改回什么样子"，而不是"这一行不在"。
    """
    start = js.find("function %s(" % name)
    if start < 0:
        return ""
    end = len(js)
    for marker in ("\n  function ", "\n  /*", "\n}"):
        found = js.find(marker, start + 1)
        if found >= 0:
            end = min(end, found)
    return js[start:end]


class ChatSidebarAndGlobalGraphTestCase(unittest.TestCase):
    """侧边栏改版（目标 / 对话 / 子智能体）与链路全局化。

    这一批断言守的都是**接线**，不是算法：引擎、存储、渲染各自有自己的用例，
    而"两边都实现对了、就是没接上"是这类改动最容易留下的缺陷形态——它的表现
    是界面上一块空白，而任何单元测试都是绿的。
    """

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")
        cls._app = _read("app.js")
        cls._chat = _read("chat.js")
        cls._css = _read("style.css")

    # ---------------------------------------------------------- 侧边栏结构

    def test_sidebar_order_and_the_single_subagent_renderer(self):
        """两块在左、两块在右，各自守自己那边的顺序。

        左栏是**导航**：对话记录 → 目标。顺序换过一次（用户原话："把对话记录放在
        第一，把新建会话放到对话记录右边"）：进来这一页要做的第一件事是**挑一条
        对话继续**，不是先选目标——目标决定的是"这一轮按什么范围跑"，而多数时候它
        跟着当前对话走、不用动。把它放在下面还有个好处：它是唯一常驻不滚的一块，
        沉到栏底之后，长列表再长也顶不走它。

        任务与子智能体在**右侧详情栏**（用户原话："左侧栏放对话、目标，右侧放任务、
        子智能体"）：它们回答"我正在等什么"，而答案要跟同一栏里的详情并排对着看。
        上一版它们在左栏（原话："把 job 和子智能体放到左侧会话下面"），那一版把
        导航挤成了四块，切换会话时要读的东西被列表压到看不见。

        子智能体这一块**曾经被整块删掉过**，删它的理由写在当时的注释里：侧栏那份
        只有摘要、主区那份带完整工具链，两者对不上时读的人没有依据判断该信哪一份。
        它后来又被搬回栏里（位置换到了右侧），而那条理由**没有被推翻，是被消掉的**
        ——两处现在调同一个 `subagentBlock()`。所以这条用例守的不是"它不在这里"，
        而是"它在这里、并且只有一个渲染器"：谁为了省事再写一个简版，这里就红。
        """
        side = self._html.index('class="chat-side"')
        aside_end = self._html.index("</aside>", side)
        left = self._html[side:aside_end]
        rail = self._html.index('class="chat-detail"')
        rail_end = self._html.index("</aside>", rail)
        right = self._html[rail:rail_end]

        # 左栏两块：我在跟谁说话 → 这件事是什么。
        left_marks = [left.index('id="%s"' % one)
                      for one in ("chat-sessions", "chat-goals")]
        self.assertEqual(left_marks, sorted(left_marks),
                         "左侧栏的顺序必须是 对话记录 → 目标")
        # 右栏两块：我正在等什么。它们从左栏搬过来（用户原话："左侧栏放对话、
        # 目标，右侧放任务、子智能体"），因为读它们时要跟同一栏里的详情对着看。
        right_marks = [right.index('id="%s"' % one)
                       for one in ("chat-side-job-list", "chat-side-subagent-list")]
        self.assertEqual(right_marks, sorted(right_marks),
                         "右侧栏的顺序必须是 任务 → 子智能体")
        # 位置关系要**两边都查**：只查"在这一边出现"，把同一块复制到另一边也照样绿。
        for one in ("chat-side-job-list", "chat-side-subagent-list"):
            self.assertNotIn('id="%s"' % one, left,
                             "%s 还在左侧栏里：那里只剩导航" % one)
        for one in ("chat-goals", "chat-sessions"):
            self.assertNotIn('id="%s"' % one, right,
                             "%s 跑到右侧详情栏去了：那里是看详情的地方" % one)
        for cls_name in ("chat-side-goals", "chat-side-sessions",
                         "chat-side-jobs", "chat-side-subagents"):
            self.assertIn(cls_name, self._html, "缺少分块样式：%s" % cls_name)

        # 侧栏那一块必须**复用**主区的渲染函数。分三处查：主区那个函数还在、
        # 侧栏那一支在、而且它的正文确实把行交给了它。
        self.assertIn("function subagentBlock(", self._chat, "主区的子智能体渲染函数没了")
        body = _fn_body(self._chat, "renderSideSubagents")
        self.assertTrue(body, "找不到 renderSideSubagents（函数被改名了？）")
        self.assertIn("subagentBlock(", body,
                      "上下文栏的子智能体块必须复用主区的 subagentBlock()："
                      "另写一个简版就是当年被删掉的那条重复路径")
        # 反面断言：当年那一对函数里，只有"取数据并转交"这个位置该回来，
        # 逐行自绘的那个（sideSubagentRow）不许回来。
        code = re.sub(r"//[^\n]*", "", self._chat)
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
    def test_the_new_chat_button_sits_at_the_right_end_of_the_sessions_head(self):
        """「新建对话」挂在对话记录卡头的**右端**，是折叠头的**兄弟**，不是它的孩子。

        用户原话：「把新建会话放到对话记录右边」。摆在页头（`.ph-actions`）时它与
        自己新建的那一列隔着整个消息流；搬到卡头上之后，按钮与它产生的列表在同一张
        卡里。三处硬要求，每一处都对应一种「看着对、其实坏了」的写法：

          · **不能塞进折叠头里面**：折叠头必须自成一体的一颗按钮（fold.js 按
            `data-fold` 找它、把 `aria-expanded` 挂在它身上），按钮里套按钮既不合
            语义，点击也会被折叠的监听吞掉——症状是「点新建，那一块自己折起来了」。
          · **必须在同一个行容器里**：只把按钮塞进卡片而不管行，它会掉到卡头底下
            独占一行，或者被折叠头的箭头挤到奇怪的位置。
          · **必须靠右**：行把剩余宽度给折叠头（`flex: 1 1 auto` + `min-width: 0`，
            少了后者长标题会把按钮挤出卡外），并且由行接管那条分隔线——折叠头自己
            那条只会画到按钮左边就断掉，卡头下面看起来缺一截。
        """
        side = self._html.index("class=\"chat-side\"")
        aside_end = self._html.index("</aside>", side)
        left = self._html[side:aside_end]

        block_at = left.index("id=\"chat-side-sessions\"")
        row_at = left.index("class=\"panel-head-row\"", block_at)
        new_at = left.index("id=\"chat-new\"", block_at)
        body_at = left.index("id=\"chat-sessions\"", block_at)
        self.assertLess(row_at, new_at,
                        "「新建对话」排在卡头那一行之后去了")
        # 行的结束位置：行里只有两颗按钮、没有嵌套 div，所以行的第一个 `</div>`
        # 就是它自己的收尾。少了这一条，把按钮挪到行**后面**（仍旧在卡头与正文之间）
        # 也照样绿——实测过，那是"看着在卡头上、其实掉出了行"的那一种。
        row_end = left.index("</div>", row_at)
        self.assertLess(new_at, row_end,
                        "「新建对话」掉出了卡头那一行：它在行外面，靠右就无从谈起")
        self.assertLess(new_at, body_at,
                        "「新建对话」跑到正文下面去了，没有贴在卡头上")

        head_at = left.index("data-fold=\"sessions\"", block_at)
        head_end = left.index("</button>", head_at)
        self.assertLess(head_at, new_at, "「新建对话」排到了折叠头左边")
        self.assertGreater(new_at, head_end,
                           "「新建对话」被塞进了折叠头按钮里面：按钮里套按钮，"
                           "点击会先被折叠的监听接走")

        # 反面查一遍**全部**折叠头：任何一个头里都不许再套按钮。只查上面那一处的话，
        # 下一块卡头照抄一个反向结构仍然绿。
        for match in re.finditer(
                r"<button\b[^>]*data-fold=\"([\w-]+)\"[^>]*>(.*?)</button>",
                self._html, re.S):
            self.assertNotIn("<button", match.group(2),
                             "折叠头 %s 里面套了一颗按钮" % match.group(1))

        # 行接管分隔线、折叠头让它，这两件事都在 CSS 里，静态看 HTML 看不出来。
        rules = re.findall(r"([^{}]+)\{([^}]*)\}",
                           re.sub(r"/\*.*?\*/", "", self._css, flags=re.S))
        rules = [(" ".join(sel.split()), " ".join(body.split()))
                 for sel, body in rules]
        row = [body for sel, body in rules if sel == ".panel-head-row"]
        self.assertTrue(row, "找不到 `.panel-head-row`：卡头那行没有样式")
        self.assertIn("display: flex", row[0],
                      "卡头行不是 flex，动作按钮不会靠右")
        head = [body for sel, body in rules if sel == ".panel-head-row > .fold-head"]
        self.assertTrue(head, "找不到 `.panel-head-row > .fold-head` 规则")
        self.assertIn("flex: 1 1 auto", head[0],
                      "折叠头没有吃掉剩余宽度，动作按钮贴不到右端")
        self.assertIn("min-width: 0", head[0],
                      "折叠头缺 min-width: 0：标题一长就把按钮挤出卡外")
        folded = [body for sel, body in rules
                  if sel == ".is-folded > .panel-head-row"]
        self.assertTrue(folded,
                        "折起来时行没有撤掉分隔线：会和卡的下边框叠成 2px 粗线")
        self.assertIn("border-bottom: 0", folded[0])

    def test_topbar_no_longer_owns_the_target_selector(self):
        """顶栏的 `<select id="engagement-select">` 必须撤掉。

        留着它比"多一个入口"更糟：两处选择各自驱动一份数据，界面上会出现
        "顶栏显示 A、漏洞列表读的是 B"。真需要恢复顶栏选择器时，这条用例会失败
        ——那时要一并处理同步，而不是把选择器加回来就完事。
        """
        self.assertNotIn('id="engagement-select"', self._html,
                         "顶栏的目标选择器应当已经撤掉（选择搬进了对话侧边栏）")
        # 剥掉注释再查：这条守的是"没有真的引用它"，不是"这几个字没有出现过"
        # ——撤掉它的那行注释里恰好写着这个名字，不剥掉就会把说明当成违规。
        code = re.sub(r"//[^\n]*", "", self._app)
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
        self.assertNotIn("engagement-select", code, "app.js 不该再引用顶栏选择器")
        # 撤掉选择器之后，范围必须仍然看得见：读漏洞列表的人需要知道它属于哪个目标。
        # 承载它的是**对话侧边栏**那块「目标」（给的是"扫了哪些地址、多少条任务"），
        # 不是栏脚——栏脚那颗 `#stat-engagement` 只重复作业名（自动建的目标一律叫
        # 「渗透目标」，于是它长年显示「目标 渗透目标」，两个词同义），说的同一件事
        # 而说得更差，已经撤掉。这一条**反过来守**：不许它再长回来。
        self.assertIn('id="chat-side-goals"', self._html, "对话侧边栏缺少「目标」那一块")
        self.assertNotIn('id="stat-engagement"', self._html, "栏脚的范围读数应当已经撤掉")
        self.assertNotIn("stat-engagement", code, "app.js 不该再引用栏脚的范围读数")

    def test_the_rail_foot_has_no_refresh_button(self):
        """栏脚那颗全局「刷新」必须撤掉，而**函数** `refreshAll()` 必须留着。

        撤那颗按钮的理由（写在 `index.html` 的栏脚注释里）是它把两件事混在一起：
        换用户 / 重新登录后的**整站重载**，与"手动让当前页变新"。前者的三处调用者
        都是**代码**（`boot.js` 换用户后、`chat.js` 删掉当前目标后、同一页面内重新
        登录后），从来不经过按钮；后者属于**当前那一页**，而原先靠它的只有链路页与
        报告页——那两页现在各有一颗自己的，放在各自的 `.ph-actions` 里，与部署页的
        `#setup-refresh` 同一个位置、同一个写法。

        这条用例要挡的是两种改法：(1) 谁把按钮加回栏脚；(2) 谁顺着"没按钮了"把
        `refreshAll()` 一起删掉——那会让换用户之后**上一个用户的数据留在屏幕上**。
        """
        self.assertNotIn('id="refresh-btn"', self._html, "栏脚那颗全局「刷新」应当已经撤掉")
        code = re.sub(r"//[^\n]*", "", self._app)
        code = re.sub(r"/\*.*?\*/", "", code, flags=re.S)
        self.assertNotIn("refresh-btn", code, "app.js 不该再引用栏脚那颗刷新按钮")

        # 两页各有一把自己那颗，而且必须在**各自页头**的动作区里——挂回全局栏脚就是
        # 这一条要挡的第一种改法。
        for panel, button in (("graph", "graph-refresh"), ("report", "report-refresh")):
            mark = self._html.index('id="panel-%s"' % panel)
            head = self._html[mark:self._html.index("</header>", mark)]
            self.assertIn('class="ph-actions"', head, "%s 页头缺少动作区" % panel)
            self.assertIn('id="%s"' % button, head,
                          "%s 的刷新按钮必须在它自己的页头里" % panel)
            self.assertIn(button, code, "app.js 没有接线 %s" % button)

        # 函数本身和它的三处调用者都得在：删掉按钮**不是**删掉能力。
        self.assertIn("function refreshAll", self._app, "refreshAll 被连着按钮一起删掉了")
        self.assertIn("refreshAll", self._chat, "chat.js 删掉目标后不再整站重载")
        self.assertIn("refreshAll", _read("boot.js"), "boot.js 换用户后不再整站重载")
        exported = self._app[self._app.index("window.PentestUI = {"):]
        exported = exported[:exported.index("};")]
        self.assertIn("refreshAll", exported,
                      "refreshAll 没有对外暴露（chat.js 与 boot.js 都经 PentestUI 调它）")

    def test_selected_target_has_a_single_owner(self):
        """目标选中态只在控制台存一份，对话侧边栏**读**它、经命名空间**改**它。

        两处各存一份的后果是分叉，而且分叉只在某一条路径上出现——"在侧边栏点了
        新目标，漏洞页签还是旧的"。
        """
        self.assertIn("ui().state", self._chat,
                      "对话侧边栏没有从 PentestUI.state 读当前目标")
        self.assertIn("selectEngagement", self._chat,
                      "侧边栏没有走控制器暴露的选择入口（改了也传不出去）")
        self.assertIn("selectEngagement", self._app,
                      "控制器没有暴露 selectEngagement")

    def test_goal_list_is_rendered_by_chat_from_data_owned_by_app(self):
        """目标列表：数据归 `app.js`（`/engagements` 只请求一次），渲染归 `chat.js`。

        两边各拉一次 `/engagements` 也能跑，但会各自持有快照，然后在新目标出现时
        显示得不一样。这条断言守住"只有一处请求、另一处渲染"这个分工。
        """
        self.assertIn("renderGoals", self._app, "app.js 没有把目标列表交给对话侧边栏渲染")
        self.assertIn("renderGoals", self._chat, "chat.js 没有实现 renderGoals")
        self.assertIn("renderGoals", self._html + self._chat + self._app, "renderGoals 丢失")
        self.assertIn("engagementList", self._app, "app.js 不再持有目标列表")

    def test_the_url_does_not_carry_the_target(self):
        """地址栏不承载目标：`?engagement=` 既不写，也不读。

        这一条盯的是一次**回退**——那个后缀曾经是目标选择的唯一持久载体（写：
        `selectEngagement()` 里的 `replaceState`；读：`loadEngagements()` 里的
        `searchParams.get`）。撤掉之后它有一条最容易重新长回来的路："刷新也能记住
        目标，多方便"。所以撤销与恢复必须在这一条断言上分开。

        理由也要留在这里，否则下一个人只看到"少了个功能"：同一个事实界面里也写着
        （对话侧边栏那块「目标」的选中态），而 URL 是两份里唯一会被复制、存成书签、
        出现在访问日志里的那一份——最容易过期的那一份。
        """
        self.assertNotIn("searchParams.set('engagement'", self._app,
                         "URL 又写上了目标：每次启动的地址会随选中的目标变化")
        self.assertNotIn("searchParams.get('engagement')", self._app,
                         "URL 又成了目标的第二个来源：它会与列表定位分叉")
        # 旧链接、历史记录仍会带着这个参数到达，所以必须清掉——否则地址栏里那截
        # 后缀会一直挂在那儿，而它看起来仍然像是能决定目标。
        self.assertIn("searchParams.delete('engagement')", self._app,
                      "没有清掉遗留的 ?engagement=：启动地址上会留着一条失效的后缀")

    def test_the_launcher_opens_the_bare_root(self):
        """启动方式 = 根地址，不带查询串。

        `deploy/start.py` 打印出来的那一行与交给 `webbrowser.open` 的是**同一个变量**
        ——横幅上写着什么、浏览器开的是什么，不能是两份。后缀不是它加的（是界面回写
        的），但"启动地址长什么样"由它定：它一旦开始拼查询串，说明目标又从界面漏回了
        启动路径。
        """
        with open(os.path.join(ROOT, "deploy", "start.py"), "r", encoding="utf-8") as handle:
            launcher = handle.read()
        self.assertIn('url = "http://%s:%d/" % (display_host, port)', launcher,
                      "启动地址不再是裸根路径")
        self.assertNotIn("webbrowser.open(url +", launcher,
                         "启动时给根地址拼了查询串")
        self.assertIn("webbrowser.open(url)", launcher,
                      "启动横幅与浏览器打开的不是同一个地址")

    def test_session_creation_does_not_carry_the_selected_target(self):
        """建会话时**不带**当前选中的目标：归属由第一条任务打的那个地址决定。

        旧契约正相反（"必须把当前目标带上，否则会话永远是未归属的"）。它在最常见
        的多目标用法上出事：在 A 目标下新开一条对话去打 B，新会话照样带着 A——于是
        B 的证据写进 A 的目标，目标页永远只有一行，也看不出哪条对话对应哪台机器。

        判据因此移到服务端：会话第一次下发产出型任务时，
        `store.ensure_session_engagement(..., target=…)` 按那个地址建/找目标
        （`test_engagement_merge.GoalPerTargetTestCase` 钉住）。这里守的是**界面
        不许把它填回去**——填回去之后服务端那条判据仍然收得到 target，但会话已经
        有归属了，target 只参与"建"、救不回一个错的目标，而症状只是"目标页少一行"。
        """
        self.assertIn("engagementId: ''", self._chat,
                      "建会话时又把目标填回去了：新对话会继承侧边栏当前的选中目标")
        body = _fn_body(self._chat, "streamTurn")
        self.assertTrue(body, "找不到 streamTurn（函数被改名了？）")
        self.assertNotIn("selectedEid()", body,
                         "streamTurn 还在用侧边栏选中的目标当会话归属")

    # ------------------------------------------------------------ 全局链路

    def test_chain_is_fetched_from_the_global_endpoint(self):
        """链路走**全局**端点 `/graph`，不是按目标的 `/engagements/{id}`。

        断言用 `assertRegex` 而不是一个写死的字面量：查询串会变（节点上限撤掉之后
        它就只剩 `?nodes=all` 一个取值），而这条用例要守的是**端点**——写死整串
        会让一次与端点无关的改动在这里报"链路没走全局端点"。
        """
        self.assertRegex(self._app, r"'/graph(\?[^']*)?'",
                         "链路没有走全局端点 /graph")
        self.assertIn("function loadGraph(", self._app, "没有独立的全局链路加载路径")

    def test_switching_target_does_not_refetch_the_chain(self):
        """切目标**不重取**链路——它现在是全局的。

        这条是"链路取消使用 engagement 方式"最容易被改回去的地方：把
        `loadGraph()` 加进 `selectEngagement` 看起来无害，实际会让每次点目标
        都重下一遍全量图，而症状只是"点起来有点卡"，没人会当成缺陷报上来。
        """
        body = _fn_body(self._app, "selectEngagement")
        self.assertTrue(body, "找不到 selectEngagement（函数被改名了？）")
        self.assertNotIn("loadGraph", body,
                         "切换目标不该重取链路：它是全局的，与选中目标无关")
        self.assertIn("loadTabData", body,
                      "切换目标后仍要按当前页签加载漏洞/资产/报告")

    def test_the_graph_asks_for_every_node(self):
        """链路**不再有节点上限**：客户端只发 `nodes=all`。

        曾经有一个 `#graph-window` 下拉框、默认 200，理由是"全量上千节点铺开是一条
        竖线"。那条理由要靠**缩放**解决，不是靠砍数据——砍掉的部分在界面上只表现为
        "链路断在半路"，读起来像证据本来就不全。所以两件事一起钉：查询串只剩一个
        取值，页面上不再有那个控件。

        仍然必须是**查询参数**而不是前端过滤：窗口在服务端切时会连带保留锚点与每个
        目标的 goal，前端过滤则会把边一起丢掉，留下一堆孤立节点。
        """
        body = _fn_body(self._app, "loadGraph")
        self.assertIn("'/graph?nodes=all'", body, "客户端没有取全部节点")
        self.assertNotIn("graphLimit", body, "节点上限已经撤掉")
        self.assertNotIn('id="graph-window"', _read("index.html"))

    def test_the_default_zoom_is_never_below_the_600_percent_baseline(self):
        """**600% 是基准，不是墙。** 一进来的落点不许低于它。

        这是用户报的"链路显示还是太小"的正面判据。原来那两条分支合起来把默认钉在
        了"适应窗口"上：`if (!fit || fit >= 0.5) return 1`——一张刚好放得下的图
        （fit 0.5 上下）就停在 100%，而节点框 220×60 布局单位在那个倍率下只有
        一百来像素宽、文字 5–10px：画出来了，读不了。使用者只能自己往上滚，
        而滚到的尽头是常数上限 600%。

        所以两条都要钉：返回值取 `max(基准, 可读)`，并且**那条 `fit >= 0.5`
        的提前返回必须不在**——它就是"刚好放得下 → 就按整幅来"的那句。
        """
        self.assertIn("const BASE_SCALE = 6;", self._app, "基准倍率不是 600%")
        body = _fn_body(self._app, "initialGraphScale")
        self.assertIn("Math.max(BASE_SCALE, 1 / fit)", body,
                      "默认缩放没有取「基准与可读之中较大的那个」")
        self.assertNotIn("fit >= 0.5", body,
                         "「fit ≥ 0.5 就按整幅来」那句会把默认钉在 100%")
        # 不再用负向断言钉"不许出现 `Math.min(initial, …)`"：那样会匹配到**注释里
        # 引用的旧写法**（`app.js` 顶部就写着改之前是什么）。下面那条正向断言才是
        # 真正的守卫——把初始缩放赋成别的值，它就找不到这一行了。
        # 本轮起允许外面再套一层 `clamp`（上下限都来自这张图自己），但**下限不能
        # 被抬到基准上**：`clamp` 的第二参必须是 `MIN_SCALE`，否则量不到尺寸那条
        # 路（`initialGraphScale` 返回 `BASE_SCALE`）也会被夹回基准，等于没修。
        self.assertIn("clamp(initial, MIN_SCALE, graphView.ceiling)", self._app,
                      "初始缩放仍是算出来的那个值，只允许夹在上下限之内")
        self.assertNotIn("clamp(initial, BASE_SCALE", self._app,
                         "下限被抬到基准上：量不到尺寸时再也回不到可读倍率")

    def test_an_unmeasured_canvas_is_recomputed_once_it_can_be_measured(self):
        """画布量不到尺寸时算出来的初始缩放不可信，必须**在量到之后重算一次**。

        `init()` 无条件调 `loadGraph`，而那一刻页签可能还没露出来——`#graph-canvas`
        的 rect 是 0×0，`fitScale()` 返回 0。原来那种情况下退回 1（= 100%），
        而 `renderGraph` 又被 `graphLayoutKey` 挡在"换图才复位"之外，于是那次
        算错的缩放**一直留着**：切到链路页签也还是那张小图。

        两个动作各守一半：量不到时落在基准上（确定能用），以及第一次量到时重算。
        """
        body = _fn_body(self._app, "initialGraphScale")
        self.assertIn("if (!fit) return BASE_SCALE;", body,
                      "量不到尺寸时应当落在基准上，而不是退回 100%")
        render = _fn_body(self._app, "renderGraph")
        self.assertIn("fitScale() > 0", render, "没有判断这一次画布到底量到没有")
        self.assertIn("if (!state.graphScaleMeasured)", render,
                      "量到尺寸之后没有重算——那次算错的值会一直留着")
        # 这条**不再是**"布局指纹一变就复位"：链路接进心跳之后，那样写等于每 3 秒
        # 把使用者刚调好的视角拽回去一次。复位条件只剩"这一张图还没量过尺寸"。
        self.assertNotIn("state.graphLayoutKey !== layoutKey ||", render,
                         "布局指纹一变就复位视角——自动刷新会每 3 秒毁掉缩放")

    def test_the_zoom_ceiling_follows_the_graph_and_clears_the_baseline(self):
        """缩放上限跟着**每张图**算，两个下界都要满足。

        这是一处真实缺陷的回归：常数上限 6 配上 `Math.min(initial, MAX_SCALE)`，
        合并视图（`1 / fit` 可以到 18 以上）一打开就被夹到 600%，而且**再也放不
        回去**——使用者看到的只是"放大到头了、字还是看不清"。

        第一项 `BASE_SCALE * BASE_HEADROOM`（600% → 3000%）是无条件的：它是
        "**基准之上还能再放大**"这件事本身。少了它，一张刚好放得下的图上限只有
        900%，而"想再看大一点"正是这种图的常态。
        """
        self.assertIn("Math.max(BASE_SCALE * BASE_HEADROOM, initial * 1.5)", self._app,
                      "上限没有同时容下基准余量与本图自己的初始缩放")
        self.assertGreaterEqual(self._app.count("graphView.ceiling"), 2,
                                "上限要有赋值点与使用点两处")
        self.assertIn("graphView.ceiling", _fn_body(self._app, "zoomAt"),
                      "滚轮缩放没有用这张图自己的上限")
        self.assertIn("clamp(initial, MIN_SCALE, graphView.ceiling)", self._app,
                      "初始缩放仍是算出来的那个值，只允许夹在上下限之内")

    def test_adapt_to_window_still_shows_the_whole_graph(self):
        """「适应窗口」必须还能回到整幅。

        默认改成 600% 之后，这条路是**唯一的**退路——它坏了，一张几千节点的图就
        永远只能看到一个角，而"看全貌"这件事在这个页签上是刚需（撤掉节点上限的
        理由就是它）。
        """
        body = _fn_body(self._app, "resetView")
        self.assertIn("graphView.scale = 1", body, "适应窗口没有回到整幅（100%）")
        self.assertIn('id="zoom-fit"', _read("index.html"))

    def test_the_readout_placeholder_is_the_baseline_not_a_hundred(self):
        """读数旁边那个**静态占位**也得是基准。

        它会被 `applyTransform` 覆盖，所以只在"图画好了但还没应用变换"这一瞬间
        看得见——正因为看得见的时间这么短，写错才没人发现：它写着 100%，而实际
        落点是 600%。
        """
        html = _read("index.html")
        self.assertIn('<span class="zoom-level" id="zoom-level">600%</span>', html,
                      "缩放占位符写的不是 600%")

    # -------------------------------------------------- 链路页的自动更新（本轮）

    def test_the_graph_refreshes_when_the_node_or_goal_stamp_changes(self):
        """链路页**会自己更新**，但只在指纹变了时整图重取。

        这一页原先故意不接心跳（旧用例 `test_the_graph_tab_is_deliberately_left_out`
        守过那条理由）。接上来时不能"每 3 秒重取"：`loadGraph(true)` 会重跑一遍服务
        端布局，一张几千节点的图那是秒级的活，而 `/pulse` 只是一次紧凑查询。所以判
        的是 `nodes` / `engagements` 两块指纹——新节点与新证据边都从这两处来。
        """
        src = self._app

        def slice_fn(marker):
            head = src.index(marker)
            tail = src.find("\nfunction ", head + 1)
            return src[head:] if tail < 0 else src[head:tail]

        load = slice_fn("function loadTabData(tab)")
        self.assertIn("tab === 'graph' && state.graphAuto", load,
                      "链路没有接进心跳（切回来仍停在进入那一刻的图）")
        self.assertIn("loadGraph(state.graphAuto)", load,
                      "切回链路页不重取：目标改名与就地补写的内容都看不见")
        pump = slice_fn("function pumpPulse()")
        self.assertIn("state.tab === 'graph' && state.graphAuto", pump,
                      "心跳里没有链路这一支")
        self.assertIn("changed('nodes')", pump, "链路重取没有判节点指纹")
        self.assertIn("changed('engagements')", pump, "新目标是新图的一部分：也要判")
        self.assertIn("loadGraph(true)", pump, "判到变化却没有重取")
        self.assertEqual(pump.count("await loadGraph(true);"), 1,
                         "心跳里出现了不止一条重取路径（会重复取）")

    def test_the_auto_refresh_chip_can_turn_it_off(self):
        """「自动刷新」必须能关——关掉之后这一页就回到改版前那个样子。

        为什么不直接不给开关：自动刷新是默认体验（数据长出来就该看见），但有人正在
        盯着一张调好视角的图、不希望它自己动，那时的出口只能是"关掉"，而不是"别打开
        这个页签"。
        """
        self.assertIn('id="graph-auto"', self._html, "页头没有「自动刷新」芯片")
        self.assertIn('aria-pressed="true"', self._html,
                      "开关没有可读的按下状态（默认开）")
        body = _fn_body(self._app, "bindGraphEvents")
        self.assertIn("#graph-auto", body, "芯片没有接线")
        self.assertIn("state.graphAuto = !state.graphAuto", body, "点它不翻转状态")
        self.assertIn("aria-pressed", body, "翻转之后没有同步可读状态")
        self.assertIn("startPulseAuto()", body, "打开之后没有把心跳拉起来")
        self.assertIn("stopPulseAuto()", body, "关掉之后心跳没停（仍在后台每 3 秒取一次）")

    def test_a_refresh_keeps_the_viewport_and_the_selected_node(self):
        """重取之后**不许**重置缩放、平移与选中项——这正是原来不自动刷新的原因。

        这条是本轮改动的地基：`renderGraph` 里那个"布局指纹变了就复位"的分支如果
        回来，自动刷新就又会每 3 秒把视角拽回去，而症状（图自己弹回去）比"不刷新"
        更难查。所以正负两条都钉。
        """
        render = _fn_body(self._app, "renderGraph")
        self.assertIn("if (!state.graphScaleMeasured)", render,
                      "只剩「画布还没量到尺寸」这一条复位路径")
        self.assertNotIn("graphLayoutKey !== layoutKey ||", render,
                         "布局指纹又成了复位条件（自动刷新会毁掉缩放）")
        self.assertIn("graphView.touched", render,
                      "没有区分「人动过视角」与「没动过」")
        self.assertEqual(render.count("graphView.tx = 0"), 1,
                         "重取不该再动平移（除了首次摆正）")
        self.assertIn("renderNodeDetail(state.selectedNodeId)", render,
                      "重画之后没有保留选中项（右侧详情会自己关掉）")
        self.assertIn("graphView.ceiling = Math.max", render,
                      "新布局的上限没跟着算（新节点可能落在上限之外）")

    def test_a_changed_layout_is_announced_instead_of_forcing_a_refit(self):
        """新数据落在视野外时要**提示**，而不是替人重新适配。

        "保留视角"与"让人看见新东西"是冲突的。解法是把决定权交回去：提示条只在
        "布局变了、并且人确实动过视角"时出现——没动过的人由 `renderGraph` 自动跟着
        新布局适配，不需要提示；动过的人看见那句「链路已更新」，自己决定要不要点。
        """
        self.assertIn('id="graph-stale"', self._html, "缺少「链路已更新」提示元素")
        self.assertIn("链路已更新", self._html, "提示文案没有说清发生了什么")
        self.assertIn("缩放与平移会重置", self._html,
                      "提示没有交代点下去的后果（视角会重置）")
        self.assertIn("function setGraphStale(flag)", self._app)
        render = _fn_body(self._app, "renderGraph")
        self.assertIn("setGraphStale(layoutChanged && graphView.touched)", render,
                      "提示条件不是「布局变了且人动过视角」")
        body = _fn_body(self._app, "bindGraphEvents")
        self.assertIn("#graph-stale", body, "提示没有接线（点了不会重新适配）")
        self.assertIn("resetView()", body, "点它没有把视角重新适配")

    # ------------------------------------------- 上下文栏的高度分配（本轮改）

    def test_the_rail_allocates_height_by_content_not_by_fixed_slots(self):
        """上下文栏的高度必须由**内容**决定，而不是四个百分比槽位。

        守的是一次真实的"杂乱"：四块原先各钉一个上限（26% / `flex: 1` / 30% / 34%），
        于是高度与内容**完全脱钩**。实测两组数字（对照实验脚本量出来的）：
          · 把「对话记录」的正文换成一句空态：201.5 → 201.5，一像素不变
            ——它是唯一的 `flex: 1 1 auto`，拿的是"剩下的空间"，不是"它需要多少"；
          · 任务从 4 行加到 12 行：223.2 → 223.2，也不变——被 `max-height: 30%` 卡住。
        两件事叠在一起就是那个画面：**一个空着的块占掉半栏，同时一个装满的块在它
        那 30% 里自己滚**。侧栏里因此有两个独立的小滚动条，而它们都不是"内容太多"
        造成的——这才是"杂乱"的物理来源。

        改法：目标留在外层（它必须一直看得见），列表那一块包进滚动容器、按内容取
        高度、超出的部分整段滚。这条用例守的就是这四件事。（顺序后来换过一次——
        对话记录排第一、目标排到栏底；滚动区这件事没换。）
        """
        css = re.sub(r"/\*.*?\*/", "", self._css, flags=re.S)
        rules = CssLayoutGuardTestCase._rules(css)
        # 只取单段选择器，顺带排除 `@media (...)` 那种（split 后是多段）。
        #
        # 用 `setdefault` 保留**第一次**出现的那条：媒体查询块在文件末尾，所以同名规则
        # 在它里面出现时是"覆盖层"，而这里要查的都是**基准层**那条。写成字典推导会让
        # 末位的覆盖先到——加窄屏那条 vh 覆盖时正好踩到：`.chat-side-goals` 在媒体查询
        # 里也有一条，于是 ④ 查到的成了 `max-height: 14vh;`，那条里当然没有
        # `flex: 0 0 auto`，断言报的是"目标块不能参与收缩"——指向一个不存在的问题。
        single = {}
        for sel, body in rules:
            if len(sel.split()) == 1:
                single.setdefault(sel, body)

        # ① 三块不再各钉一个槽位。重新钉上去的那一刻，高度就重新与内容脱钩。
        for name in (".chat-side-sessions", ".chat-side-jobs", ".chat-side-subagents"):
            self.assertNotIn(name, single,
                             "%s 又被钉成了固定槽位——高度会重新与内容脱钩" % name)

        # ② 滚动容器必须真能滚。`min-height: 0` 是承重的那一条：没有它，flex 项的
        #    最小高度默认是**内容高度**，容器就永远不会滚——症状是"滚动区把侧栏顶破"
        #    而不是出现滚动条，且静态看代码没有任何不对。
        scroll = single.get(".chat-side-scroll", "")
        self.assertTrue(scroll, "找不到 `.chat-side-scroll`（对话记录没有包进滚动容器）")
        self.assertIn("min-height: 0", scroll,
                      "滚动容器少了 min-height: 0，它在内容超高时不会滚")
        self.assertIn("overflow-y: auto", scroll, "滚动容器没有 overflow-y: auto")

        # ③ 三块按内容取高。这是"内容多则高、内容少则矮"的唯一来源。
        child = [body for sel, body in rules
                 if sel == ".chat-side-scroll > .chat-side-block"]
        self.assertTrue(child, "找不到 `.chat-side-scroll > .chat-side-block` 规则")
        self.assertIn("flex: 0 0 auto", child[0],
                      "滚动容器里的块必须是 `flex: 0 0 auto`，否则高度又不由内容决定")

        # ④ 目标不参与收缩。写成 `0 1 auto` 时它会跟着滚动区一起被压：实测把任务
        #    从 4 行加到 12 行，这一块从 193.4 掉到 178.1——而它是整栏里唯一
        #    "必须一直看得见"的东西（滚动区里读的正是它选中的那个目标）。
        #    空间不够时该让路的是**能滚的那个**，不是这个。
        goals = single.get(".chat-side-goals", "")
        self.assertTrue(goals, "找不到 `.chat-side-goals`")
        self.assertIn("flex: 0 0 auto", goals,
                      "目标块不能参与收缩：被压缩之后它自己就在内部滚，"
                      "而滚动区里读的正是它选中的那个目标")
        self.assertIn("max-height", goals,
                      "目标块需要一个上限：目标多到十几个时它会吃掉整栏，"
                      "而它是唯一常驻的一块，只能靠滚动而不是靠占满来容纳")

        # ⑤ 窄屏那一档必须**换一种单位**。
        #    桌面下 `.chat-side` 的高度来自 `align-items: stretch`（交叉轴拉伸），是确定
        #    值，所以 `26%` 有基数。折到顶部之后这个栏是纵向 flex 项、`flex-basis: auto`，
        #    高度由内容决定——百分比 max-height 的包含块因此不是确定的。实测它解析到了
        #    当时的最终栏高（258.5px），目标块只剩 66.7px、正文 30px，**一个目标行都放
        #    不下**，四个目标全挤在那 30px 里滚。
        #    这条断言只看"有没有一条 vh 覆盖"，因为具体几个 vh 是可以调的，而"用没用
        #    一个不依赖布局结果的单位"是承重的。
        #    注意查法：媒体查询里那条的 **body 只有声明**，选择器进不了 body（`_rules`
        #    的匹配方式决定）。所以要按选择器名比，而不是拿正则在 body 里找选择器。
        vh_goals = [body for sel, body in rules
                    if sel == ".chat-side-goals"
                    and re.search(r"max-height:\s*[\d.]+vh", body)]
        self.assertTrue(vh_goals,
                        "窄屏那一档缺少 `.chat-side-goals` 的 vh 上限："
                        "折到顶部之后百分比没有确定的基数，目标块会被压到连一行都放不下")

    def test_the_target_stays_outside_the_scroll_and_the_rail_is_its_own_layer(self):
        """HTML 结构：对话记录在滚动容器**里面**且排第一，目标在它外面且排在后面；
        右栏那两块也不进详情正文。

        查位置关系而不是"这两个字符串在不在文件里"：每一块都可能被单独挪走，
        而挪走之后字符串检查仍然全绿。方向调换过一次（用户原话："把对话记录放在
        第一"）：原先查的是"目标在前、对话记录在后"，现在反过来。**但两边查的仍然是
        同一件事**——谁在滚动区里、谁在外层，而这一条与先后顺序无关：外层那一块
        不滚，所以它必须一直看得见；滚动区里那一块可以被长列表顶走，因为它本来就是
        列表。顺序换了，"目标不滚"这条不变量没换。

        右栏那一条查的是"那两块与栏头的关系"。详情正文（`.chat-detail-body`）已经
        拿掉了（用户原话："右侧边栏取消详情内容，把功能放到任务和子智能体中，详细
        内容在这两个框中直接显示"）：右栏现在只有栏头与一层滚动区，任务与子智能体
        就在这一层里，每一块自己就地展开被点中的那一行。所以这里改查两件事——
        **正文那层不再存在**，以及那两块确实落在 `.chat-detail-rail` 里面。
        查"还在不在墙里"而不是只查"字段有没有删掉"：留着 rail 却把两块挪到它外面，
        栏头与滚动区之间就断开，长列表会把整栏顶破。
        """
        start = self._html.index('class="chat-side"')
        end = self._html.index("</aside>", start)
        region = self._html[start:end]

        scroll_at = region.index('class="chat-side-scroll"')
        sessions_at = region.index('id="chat-sessions"')
        goals_at = region.index('id="chat-goals"')
        self.assertLess(scroll_at, sessions_at,
                        "对话记录不在滚动容器里：它是列表，条数没有上限，只有跟着"
                        "滚动区一起滚才不会把别的块顶走")

        self.assertGreater(goals_at, scroll_at,
                           "目标块被包进了滚动容器：它会跟着下面的列表一起滚走，"
                           "而滚动区里读的正是它选中的那个目标")

        rail = self._html.index('class="chat-detail"')
        rail_end = self._html.index("</aside>", rail)
        right = self._html[rail:rail_end]
        self.assertNotIn("chat-detail-body", right,
                         "右栏还留着详情正文那一层：详情现在展开在被点中那一行下面，"
                         "正文层没有东西可画，只会多出一块空白")
        layer_at = right.index('class="chat-detail-rail"')
        for one in ("chat-side-job-list", "chat-side-subagent-list"):
            at = right.index('id="%s"' % one)
            self.assertGreater(at, layer_at,
                               "%s 不在右栏的滚动层里：栏头与滚动区之间就断开了" % one)

    def test_each_section_is_its_own_card_and_the_panes_are_only_canvas(self):
        """卡片化：栏本体退成**画布**，里面每一段各自是一张卡。

        改版前两条侧栏各是一张大卡，内部靠 1px 横线切分（`.chat-side-block` 的
        `border-bottom`、`.chat-detail-head` 的 `border-bottom`）。那种写法读起来像
        一张表：四行属于同一个面，行与行只隔一条线。所以"卡片化"落在这里不是给栏
        再加一层边框，而是反过来——**把面从栏上拿掉，交给里面每一段**。

        判据只有一条：一张卡 = 一个面（`background`）+ 一道边（`border`）+ 一个圆角
        （`border-radius`），三样必须同时出现在同一个选择器上。只有边没有面，是"分隔
        线"；只有面没有边，是"色块"；两者都不是卡。四块 + 卡头 + 正文每一段都要齐。

        这条用例同时守住**反面**：栏本体不能再有面/边/圆角/裁切，块与卡头不能再有
        `border-bottom`——任何一项回来，"一张大卡切四行"的读法就跟着回来，而静态看
        代码一切正常（只是多了一条 1px 线）。
        """
        css = re.sub(r"/\*.*?\*/", "", self._css, flags=re.S)
        rules = CssLayoutGuardTestCase._rules(css)
        single = {}
        for sel, body in rules:
            if len(sel.split()) == 1:
                single.setdefault(sel, body)

        card = ("background: var(--panel)", "border: 1px solid var(--border)",
                "border-radius: var(--r-3)")

        # ① 栏本体 = 画布。四样都撤掉：面、边、圆角、裁切。
        for pane in (".chat-side", ".chat-detail"):
            body = single.get(pane, "")
            self.assertTrue(body, "找不到 %s 规则" % pane)
            for dec in ("background:", "border:", "border-radius:", "overflow:"):
                self.assertNotIn(dec, body,
                                 "%s 又画上了 `%s`——它现在是画布，卡是里面每一段；"
                                 "栏自己再有面/边/圆角，就又变回「一张大卡」" % (pane, dec))
            self.assertIn("gap:", body,
                          "%s 少了块间距：没有间距的两张卡读起来仍是一张表" % pane)

        # ② 左栏四块：一块 = 一张卡。
        block = single.get(".chat-side-block", "")
        self.assertTrue(block, "找不到 `.chat-side-block`")
        for dec in card:
            self.assertIn(dec, block,
                          "`.chat-side-block` 少了 `%s`——「一块一张卡」就是这三样凑齐" % dec)
        self.assertIn("overflow: hidden", block,
                      "块要自己裁：`.fold-head` 是按钮，悬停底色会从卡片圆角外露出来")
        self.assertNotIn("border-bottom", block,
                         "块之间又用 1px 横线切分了——那是「整栏一张大卡、四行一条线」的写法")

        # ③ 滚动容器是透明的，但间距要它给（块与块之间的 8px 画布归它）。
        scroll = single.get(".chat-side-scroll", "")
        self.assertIn("display: flex", scroll, "滚动容器不是 flex 列，块间距不会生效")
        self.assertIn("gap:", scroll, "滚动容器少了 gap：里面的三块会贴在一起")

        # ④ 右栏：卡头一张卡，正文里每一段各一张卡。
        head = single.get(".chat-detail-head", "")
        self.assertTrue(head, "找不到 `.chat-detail-head`")
        for dec in card:
            self.assertIn(dec, head, "卡头少了 `%s`——它现在是独立的一张卡" % dec)
        self.assertNotIn("border-bottom", head,
                         "卡头又用横线与正文连在一起了——那是「整栏一张大卡」的写法")

        # ④ 就地展开面**自己是一张卡**：它长在一块卡的 `.panel-body` 里、紧贴被点中
        #    那一行，所以它必须有面有边有圆角——否则展开的内容与列表行糊成一片，
        #    看不出"上面这一行是我点开的"。
        inline = [body for sel, body in rules if sel == ".chat-detail-inline"]
        self.assertTrue(inline,
                        "找不到 `.chat-detail-inline`：被点中那一行的展开面没有自己的面，"
                        "详情看起来像列表的一部分")
        for dec in card:
            self.assertIn(dec, inline[0],
                          "展开面少了 `%s`——它要自己是一张卡" % dec)

        # ⑤ 滚动层自己不能带内边距：带了的话两块卡到不了栏的边缘，两侧各让出 8px，
        #    看起来像"卡里还有一层卡"。间距由 `gap` 给，不由 `padding` 给。
        layer = single.get(".chat-detail-rail", "")
        self.assertNotIn("padding", layer,
                         "滚动层自己带了内边距：卡会缩在栏内 8px 处，"
                         "而栏的宽度是这两块卡共用的")

    def test_a_fixed_width_pane_keeps_its_split_width_not_the_content_min_content(self):
        """定宽分栏的宽度归 `--split-chat` / `--split-detail` 管，不归内容管。

        这一条来自一个**量出来的** bug，不是防御性写法。卡片化把栏上的
        `overflow: hidden` 撤掉（必须撤：卡片圆角与块间那 8px 画布都在栏的边界上，
        留着裁切就会把画布一起裁掉）之后，`.chat-side` 的宽度从 296 变成 **327.09**，
        而它的 `flex` 明明是 `0 0 296px`——`flex-basis` 被覆盖了。

        机制：flex 项的 `min-width` 默认是 `auto`，它的"自动最小尺寸"是内容的
        min-content；而这条自动最小值**只在 overflow 可见时才生效**。原来的
        `overflow: hidden` 恰好把它压住了（内容里那条长路径要 327px，多出来的部分
        一直被静默裁掉，所以栏宽始终是 296）。撤掉裁切，自动最小值立刻接管。

        后果不是"栏宽了一点"：`--split-chat` 是拖拽分栏的落点，宽度一旦由内容拍板，
        拖到 240 也会被内容顶回去，两栏的比例就不再是用户设定的那个。
        """
        css = re.sub(r"/\*.*?\*/", "", self._css, flags=re.S)
        single = {}
        for sel, body in CssLayoutGuardTestCase._rules(css):
            if len(sel.split()) == 1:
                single.setdefault(sel, body)
        for pane in (".chat-side", ".chat-detail"):
            body = single.get(pane, "")
            self.assertTrue(body, "找不到 %s 规则" % pane)
            self.assertIn("min-width: 0", body,
                          "%s 少了 min-width: 0：flex 项的自动最小尺寸会拿内容的 "
                          "min-content 顶掉 flex-basis（实测 296 → 327.09），"
                          "而这条栏的宽度归分栏令牌管，不归内容管" % pane)


class LiveStepBadgeTestCase(unittest.TestCase):
    """气泡上那个步数徽章：报**实际**步数，上限只在它真会拦人时才报。

    使用者把预设的步数调到顶格（`agents.MAX_STEPS`）之后，徽章上就成了
    「第 18 步 / 最多 99999 步」——后一半拦不住任何人，它只是把配置复述一遍，而前半句
    已经就是"本轮实际步数"。于是：顶格时不再显示上限，真预算（6/10/12/40…）照旧显示
    （那个数会在一轮中途把作业切停），收尾之后把这一轮**实际**走了多少步留在徽章上
    （原来只剩「已完成」三个字，而步数只活在流里，刷新即消失）。

    这里守的仍是**接线**，不是算法：判据在服务端（`chat.steps_at_ceiling`，边界值另在
    `test_chat.py` 与 `test_chat_stream.py` 里钉），而"两边都实现对了、就是没接上"正是
    这类改动最容易留下的缺陷形态——界面上少一句提示，没有任何单元测试会红。
    """

    @classmethod
    def setUpClass(cls):
        cls._chat = _read("chat.js")

    @staticmethod
    def _code(js: str) -> str:
        """剥掉注释再看。

        负向断言（"不许出现某个常数"）不剥注释就是自证失败——**写下这条断言的理由本身
        就要引用那个常数**（"`settings.js` 里原来那份 `AGENT_STEPS_MAX = 40` 就是这么
        烂掉的"）。同一份文件里 `style.css` 那条断言吃过一样的亏。
        """
        return re.sub(r"/\*.*?\*/", "", js, flags=re.S)

    def test_both_events_feed_the_flag(self):
        """`start` 先到、`turn` 随后覆盖，两处都要接。

        只接一处的话，"上限藏没藏起来"取决于两个事件的到达顺序，而那种缺陷在快机器上
        不复现。
        """
        body = _fn_body(self._chat, "applyEvent")
        self.assertEqual(2, body.count("live.stepsAtCeiling"),
                         "start 与 turn 两个分支没有都接上这个字段")

    def test_the_ceiling_is_hidden_and_a_real_budget_is_shown(self):
        body = _fn_body(self._chat, "liveProgress")
        self.assertIn("!live.stepsAtCeiling", body, "没有按「是不是顶格」分岔")
        self.assertIn("/ 最多 ${live.maxSteps} 步", body, "真预算的上限不显示了")
        self.assertIn("第 ${live.step} 步", body)

    def test_a_missing_flag_falls_back_to_showing_the_cap(self):
        """旧服务端 + 新页面时这个字段是缺的：那时必须按「有上限」处理。

        反过来的方向（缺字段就藏上限）会让每一个真有限额的会话都少一句提示，而"少了
        这句话"与"这一轮本来就没有上限"在界面上完全一样。
        """
        body = _fn_body(self._chat, "applyEvent")
        self.assertIn("data.stepsAtCeiling !== undefined", body,
                      "turn 分支没有留出「字段缺失」这条路径")

    def test_the_finished_round_keeps_its_actual_step_count(self):
        body = _fn_body(self._chat, "ranSteps")
        self.assertIn("本轮共 ${live.step} 步", body,
                      "收尾后没有留下「这一轮实际走了多少步」")
        head = _fn_body(self._chat, "liveHead")
        self.assertIn("'已完成' + ranSteps(live)", head, "「已完成」那一支没带上步数")
        self.assertIn("'这一轮没有跑完' + ranSteps(live)", head,
                      "「没有跑完」那一支没带上步数")

    def test_the_ceiling_is_not_copied_into_the_frontend(self):
        """平台上限不许在前端留副本。

        它属于 `pentestd/agents.py`。`settings.js` 里原来那份 `AGENT_STEPS_MAX = 40`
        就是这么烂掉的：上限抬到顶格之后它没跟，而下一个照它校验的人会得出"预设最多
        40 步"——一个早就不存在的数。
        """
        self.assertNotIn("99999", self._code(self._chat),
                         "chat.js 里抄了平台上限；它必须读服务端给的字段")
        self.assertNotIn("AGENT_STEPS_MAX", self._code(_read("settings.js")),
                         "预设那边又出现了一份自己的步数上限")

    def test_the_steps_input_matches_the_server_ceiling(self):
        """预设表单里那个「单轮步数预算」输入框的 `max` 与提示文字要等于服务端上限。

        这是**唯一**一处刻意镜像：输入框的 max 得跟服务端校验一致，偏小会挡住合法值
        （用户以为保存失败），偏大则要等提交才报错。上一轮抬上限正是靠手工同步才没漏
        掉这一处，所以它值得一条用例。
        """
        html = _read("index.html")
        match = re.search(r'id="agent-steps"[^>]*\bmax="(\d+)"', html)
        self.assertIsNotNone(match, "找不到步数输入框的上限声明")
        self.assertEqual(agents.MAX_STEPS, int(match.group(1)),
                         "输入框的 max 与服务端上限不一致")
        self.assertIn("1–%d" % agents.MAX_STEPS, html, "提示文字里的区间没有跟着改")


class FoldPanelTestCase(unittest.TestCase):
    """折叠块：上下文栏四块 + 「资产」页的资产目录。

    折叠有三种失败形态，每一种都**不会让任何功能测试变红**，而且三种都只在界面上
    看得出来，所以只能靠静态断言兜住：

      · **只藏内容、不藏外壳** —— 折起来只剩一个空壳，"折着"与"本来就没内容"
        分不出来；
      · **折了之后没有展开入口** —— 入口恰恰是被藏起来的那个头；
      · **刷新之后自己弹开** —— 刚折起来的东西下次打开又占着视野。

    另有一类不属于折叠本身、但和它绑在一起：**折起来的资产目录不能还留着一条能拖的
    分栏线**。按住没反应的手柄比没有手柄更糟——它看起来像坏了。
    """

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")
        cls._css = _read("style.css")
        cls._js = _read("fold.js")
        cls._split = _read("split.js")

    @staticmethod
    def _rules(css):
        css = re.sub(r"/\*.*?\*/", "", css, flags=re.S)
        return [(" ".join(sel.split()), " ".join(body.split()))
                for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css)]

    def _heads(self):
        """每个折叠头的 (key, 开标签, 内部 HTML)。"""
        out = []
        for match in re.finditer(
                r'<button\b[^>]*data-fold="([\w-]+)"[^>]*>(.*?)</button>',
                self._html, re.S):
            whole = match.group(0)
            out.append((match.group(1), whole[:whole.index(">") + 1], match.group(2)))
        return out

    def _folded_rules(self):
        return [(sel, body) for sel, body in self._rules(self._css)
                if ".is-folded" in sel]

    # ------------------------------------------------------------- 结构

    def test_every_head_is_a_real_button(self):
        """头必须是 `<button type="button">`，而且带一个 aria-hidden 的箭头。

        用真按钮而不是 `div` + `tabindex`：聚焦、Enter/Space、进无障碍树这三件事
        浏览器已经做好了，自己重写一遍一定会漏掉其中一件——而漏掉的那件只在键盘
        或读屏下才现形。
        `type="button"` 不能省：button 在 `<form>` 里默认是 submit，而设置页那几个
        折叠块真的在 form 里。

        箭头挂在一个 `aria-hidden` 的 span 上，**不写成 `.fold-head::before`**：
        CSS 生成的内容会参与无障碍名称的计算，读屏念出来会变成"▾ 对话记录"。
        """
        heads = self._heads()
        self.assertTrue(heads, "一个折叠头都没有——多半是 HTML 的写法变了")
        for key, tag, inner in heads:
            self.assertIn('type="button"', tag, "%s：缺 type=button" % key)
            self.assertIn("panel-head", tag, "%s：没有复用卡头样式" % key)
            self.assertIn("fold-head", tag, "%s：没有挂上折叠头的类" % key)
            self.assertIn('class="fold-mark" aria-hidden="true"', inner,
                          "%s：箭头缺失、或没有 aria-hidden（会被读屏念出来）" % key)
            self.assertIn("panel-head-hint", inner,
                          "%s：卡头里那个读数位不见了" % key)

    def test_every_key_has_parts_and_no_part_is_orphaned(self):
        """头与部件必须成对。少一边的失败形态都是**静默**的：

          · 头没有部件 → 点下去什么都不发生（连报错都没有）；
          · 部件没有头 → 它会一直保持初始状态，而"折不起来"看起来像这一块本来
            就长这样。
        """
        heads = [key for key, _tag, _inner in self._heads()]
        parts = re.findall(r'data-fold-part="([\w-]+)"', self._html)
        self.assertEqual(sorted(set(heads) - set(parts)), [],
                         "这些头没有对应的部件（点下去不会有任何反应）")
        self.assertEqual(sorted(set(parts) - set(heads)), [],
                         "这些部件没有对应的头（永远折不起来）")
        self.assertEqual(len(heads), len(set(heads)),
                         "有折叠头共用了同一个 key：它们会一起开合，"
                         "而界面上看不出两者被绑在一起")

    def test_every_key_has_a_part_with_an_id(self):
        """每个 key 至少要有一个带 `id` 的部件。

        `aria-controls` 由 fold.js **从部件身上取 id 拼出来**，不手写在 HTML 上
        ——手写就是两份可以互相矛盾的声明，而矛盾的症状是读屏用户被告知"这按钮
        控制着 X"、实际收起来的是 Y，没有任何测试会红。
        一个带 id 的部件都没有时，那个属性就整个消失了。

        查的是**整个开标签**里有没有 id，不假设属性顺序：`id` 排在
        `data-fold-part` 前面（资产树那一栏就是）时，按"属性在前"写的正则会漏掉它，
        而漏掉的表现是这条断言永不失败。
        """
        for key, _tag, _inner in self._heads():
            found = [tag.group(0) for tag in
                     re.finditer(r'<[^>]*data-fold-part="%s"[^>]*>' % key, self._html)]
            self.assertTrue(found, "%s：找不到部件" % key)
            self.assertTrue(any(re.search(r'\bid="[\w-]+"', tag) for tag in found),
                            "%s：没有任何带 id 的部件，aria-controls 会缺失" % key)

    def test_the_five_asked_for_blocks_are_all_foldable(self):
        """用户点名的东西一个都不能少：对话记录、任务、子智能体、资产目录。

        （「目标」不在原话里，但它和另外三块同处一栏、同一种头；单独让它折不了，
        表现是"这一栏里只有它那一行点了没反应"。）
        """
        keys = [key for key, _tag, _inner in self._heads()]
        for key in ("goals", "sessions", "jobs", "subagents", "assets"):
            self.assertIn(key, keys, "缺少可折叠的块：%s" % key)

    # ------------------------------------------------------------- 形态

    def test_folding_hides_the_body_and_never_the_head(self):
        """折起来 = 正文不占高度；**头必须留着**——它同时是展开入口。

        只藏正文这一条由 `.is-folded > .panel-body` 承担；另一半是反面断言：
        任何 `.is-folded` 规则都不许选中折叠头本身（`.fold-head`）或它里面那两样
        必要的东西（`.panel-head` 那一行、`.fold-mark` 那个箭头）。藏掉其中任何一个，
        这一块就再也打不开了。
        """
        bodies = [body for sel, body in self._folded_rules()
                  if re.search(r"\.is-folded\s*>\s*\.panel-body\b", sel)]
        self.assertTrue(bodies, "找不到 `.is-folded > .panel-body` 规则")
        self.assertIn("display: none", bodies[0],
                      "折起来之后正文还在占位置，等于没折")

        for sel, body in self._folded_rules():
            if "display: none" not in body:
                continue
            for banned in (r"\.fold-head(?![\w-])", r"\.panel-head(?![\w-])",
                           r"\.fold-mark(?![\w-])"):
                self.assertIsNone(
                    re.search(banned, sel),
                    "%s：折叠把展开入口自己藏掉了——这一块再也打不开" % sel)

    def test_folding_never_touches_the_draggable_widths(self):
        """折叠不得碰 `--split-*`。

        这是 split.js 文件头那句"**不做吸附、不做折叠**"能不能继续成立的关键。
        它当时的理由是"折叠需要一套『折叠前宽度』的状态，而那条状态在刷新之后
        无法与用户的意图对齐"——那条理由在这里**不适用，因为这里根本没有那条状态**：
        折叠只是藏起正文，宽度一个字节都不动，展开时宽度自己就在。
        一旦有 `.is-folded` 规则去写宽度，这个论证就断了，两边会开始互相打架。
        """
        for sel, body in self._folded_rules():
            self.assertNotIn("var(--split-", body,
                             "%s：折叠去动了可拖动的栏宽，展开时会回不到原宽" % sel)
            self.assertNotRegex(body, r"(?<!min-)(?<!max-)\bwidth\s*:",
                                "%s：折叠去写了宽度。它只该负责「藏正文」。" % sel)

    def test_the_folded_asset_tree_hides_its_handle(self):
        """资产目录折起来时，那条分栏线一起隐藏。

        留着它的表现是"按住没反应"——比没有手柄更难查，因为它看起来是坏的而不是
        没有的。树折起来之后那条线也没有可拖的对象：左边那一栏已经收缩成只够放下
        标题。

        **split.js 因此不需要改一行**：它对不可见手柄本来就有一个早退
        （`handleBox.width <= 0.5`）。这一句断言的是那个前提仍然在——底下的实现
        换了而这里还留着"反正会早退"的注释，是最容易烂掉的那种注释。
        """
        self.assertIn('data-fold-part="assets"', self._html)
        handle = re.search(r'<div[^>]*class="split-handle"[^>]*data-split="assets"[^>]*>',
                           self._html)
        self.assertIsNotNone(handle, "找不到资产分栏的那条拖拽条")
        self.assertIn('data-fold-part="assets"', handle.group(0),
                      "拖拽条没有跟着折叠的部件列表")
        hidden = [body for sel, body in self._folded_rules()
                  if "split-handle" in sel and "display: none" in body]
        self.assertTrue(hidden, "折起来时没有隐藏那条拖拽条")
        self.assertIn("handleBox.width <= 0.5", self._split,
                      "split.js 对不可见手柄的早退不见了——折叠之后它会去拖一个不存在的宽度")

    def test_a_button_head_is_exactly_as_tall_as_a_div_head(self):
        """折叠头（`button`）必须与卡头（`div`）一样高。

        两者都带 `.panel-head`、字号都是 12px、内边距都是 8px，但 **UA 重置按钮用
        的是 `font` 简写**（Chromium：`font: 400 13.3333px Arial`），简写里带着
        `line-height: normal`——所以按钮上的行高被一起重置了，而旁边那个 `div`
        继承 body 的 `1.55`。12px 字号下两条行盒差 **2.6px**（实测 33.0 vs 35.6）。

        这个差**只在两个卡头并排时**看得见：报告页与资产页折起来之后，折叠条与
        正文卡头直接相邻，两条底边错开 2.6px，读起来是"一条横条上并排两个标签"。
        分开的两处各自都正常，所以它在任何"单独看一个卡头"的检查里都不露头。

        判据写成**通则**而不是"这一条里有 line-height"：被 UA 简写重置的是整组，
        凡是给 `button` 还 `font-family` 的规则都必须同时还 `line-height`——只还
        其中一项，留下的那一项会静默地与旁边的 `div` 不同。
        """
        restored = [(sel, body) for sel, body in self._rules(self._css)
                    if re.search(r"\bbutton\b", sel) and "font-family" in body]
        self.assertTrue(restored, "找不到任何给 button 还字体的规则——这条断言失效了")
        for sel, body in restored:
            self.assertIn("line-height", body,
                          "%s：还了 font-family 却没还 line-height。UA 用 `font` 简写"
                          "重置按钮，简写里带着 line-height:normal，于是这个按钮比"
                          "同样带 .panel-head 的 div 矮 2.6px" % sel)

    def test_the_folded_rail_keeps_a_gap_from_the_document(self):
        """折起来的目录条与正文卡头之间必须有间隙。

        折起来时那条 1px 分栏线是 `display: none` 的，而它是两栏之间**唯一**的
        分隔物。于是两栏直接相接（实测 gap = 0），加上两侧卡头的底色又是同一个
        `--panel`，结果读起来是"一条横条上并排两个标签"，而不是"一个窄条 +
        一张卡"——再叠上那 2.6px 的高度差，同一条横条上就有两个高度不同的标签。

        所以这两件事必须同时成立：**既然分栏线被藏掉了，折叠的栏就得自己带一条
        间隙**。只做一半，症状就回到"两个标签贴在一起"。
        """
        hidden = [body for sel, body in self._folded_rules()
                  if "split-handle" in sel and "display: none" in body]
        self.assertTrue(hidden, "找不到「折叠时分栏线隐藏」的规则——这条断言的前提没了")
        gap = [sel for sel, body in self._folded_rules()
               if re.search(r"\.(tree-pane|report-side)\.is-folded", sel)
               and re.search(r"margin-(left|right)\s*:", body)]
        self.assertTrue(gap,
                        "折叠的目录条没有自己的间隙：分栏线被藏掉之后两栏直接相接"
                        "（实测 gap=0），两个同色的卡头连成一条横条")

    # ------------------------------------------------------------- 状态

    def test_a_folded_row_does_not_double_its_separator(self):
        """折起来的那一行不许把分隔线画两遍。

        每块自己有 `border-bottom`（它与下一块的分界），头自己也有 `border-bottom`
        （它与正文的分界）。展开时两条离得很远、各司其职；折起来之后正文没有了，
        两条就贴在一起——**量出来是一条 2px 的线**，而旁边展开的块是 1px。
        症状是"折起来的那几行看起来被压扁了"，只有量像素才看得出来。

        本轮真量过：折起来那一行上连着两组 1px 像素（220,223,226 与 215,219,226），
        展开的块只有一组。
        """
        fixed = [body for sel, body in self._rules(self._css)
                 if "chat-side-block.is-folded" in sel and "fold-head" in sel
                 and "border-bottom: 0" in body]
        self.assertTrue(fixed,
                        "折起来时没有撤掉头那条下边框：它会与外壳那条叠成 2px 的粗线，"
                        "而展开的块是 1px——折起来的几行看起来被压扁了")

    def test_the_state_is_persisted_and_survives_a_blocked_storage(self):
        """折叠状态要落盘，而且读写都必须包 try。

        localStorage 在隐私模式、被企业策略禁用、或配额写满时**读取本身就会抛**。
        这条路径上漏一个 try，控制台启动就会被一份"上次折到哪"的偏好带崩，
        而症状与折叠毫无关系（整页起不来）。与 split.js 的 read/write 同一写法，
        所以这一条按同一个形状查。
        """
        self.assertIn("localStorage", self._js)
        for fn_name in ("read", "write"):
            start = self._js.index("function %s(" % fn_name)
            body = self._js[start:self._js.index("\n  }", start)]
            self.assertIn("try", body, "%s() 缺少 try" % fn_name)
            self.assertIn("catch", body, "%s() 缺少 catch" % fn_name)
        # 存储键带版本号：以后改动结构时旧值直接作废，比写迁移更安全。
        self.assertRegex(self._js, r"STORAGE_KEY\s*=\s*'pentest\.fold\.v\d+'",
                         "存储键没有版本号，将来改结构时旧值会被当成新格式读")
        # 只记**折起来的**那些：没写过的按展开算，于是新增一块不需要迁移。
        self.assertIn("delete folded[", self._js,
                      "折叠状态看起来是在记「展开」的那一份——那样新增一块会默认折着")

    def test_the_head_is_static_html_not_built_by_the_renderers(self):
        """折叠头必须写在 index.html 里，**不允许由渲染函数生成**。

        这几块的正文每一帧都可能被重画（任务列表 10 秒一刷、子智能体行跟着流式
        事件走）。头跟着重画的话，`aria-expanded` 与 `aria-controls` 会在两次渲染
        之间被抹掉，而表现是"点开之后过一会儿自己又变成折着的"——它取决于流式
        事件什么时候来，所以没人能复现。
        """
        for name in ("chat.js", "app.js", "settings.js"):
            source = _read(name)
            self.assertNotIn("fold-head", source,
                             "%s 在生成折叠头——它会被下一次重画抹掉状态" % name)
            self.assertNotIn("data-fold", source,
                             "%s 在拼折叠属性，那些声明应当留在 index.html" % name)

    # ------------------------------------------------- 上下文栏另两块的接线

    def test_the_sidebar_jobs_block_shows_them_all(self):
        """侧栏任务列表**不再截断**，所以也不再有「查看全部」那颗按钮（m14866）。

        它原先只列前 5 条，多出来的靠一颗按钮指去「任务」页签。使用者的判断是
        「所有任务全部展示出来」：这一块回答的正是「这条对话还派着几件活」，而
        "另有 3 条要再点一次才看得见"恰好是这个问题回答不了的地方。上限与那颗
        按钮必须一起走——只留按钮就变成一颗点了没事发生的假按钮。
        """
        chat = _read("chat.js")
        self.assertNotIn("SIDE_JOB_MAX", chat,
                         "侧栏任务列表又在截断了：这一块要的是全部")
        self.assertNotIn("switchTab('jobs')", chat,
                         "「查看全部」那颗按钮还在：这一块本来就不截断")

    def test_the_sidebar_jobs_block_keeps_itself_fresh(self):
        """那一块要会自己长——它不在任务页签里，所以不会跟着任务页签的刷新走。

        少了这一句的表现很安静：它停在登录那一刻的"登录后显示"，直到用户切到任务
        页签再切回来。**看起来像"这个部署没有任务"**，而它其实只是没被问过。
        这是本轮截图里抓到的第二个问题。

        同时钉住"只能有一个定时器"：`watchJobs` 必须走 `startJobsAuto()`（它自己
        有 `if (state.jobsTimer) return`），而不能自己再 `setInterval` 一个——
        两个定时器会让刷新节奏翻倍，而它们拉到的是同一份数据。
        """
        chat = _read("chat.js")
        app = _read("app.js")
        self.assertIn("ui().watchJobs()", chat,
                      "对话页签没有让上下文栏那一块任务读数保持新鲜")
        body = _fn_body(app, "watchJobs")
        self.assertTrue(body, "app.js 里没有 watchJobs")
        self.assertIn("loadJobs(", body, "没有先拉一次")
        self.assertIn("startJobsAuto(", body, "没有接上定时器，那一块只会刷新一次")
        self.assertNotIn("setInterval", body,
                         "自己又起了一个定时器——两处会各自刷同一份数据")
        # 任务页签那条路不许被顺手撤掉。
        self.assertIn("watchJobs", re.search(
            r"function loadTabData\(tab\) \{.*?\n\}", app, re.S).group(0),
            "对话页签没有接进 loadTabData：靠点击事件之外没有第二条路径")

    def test_the_sidebar_jobs_block_shares_the_status_vocabulary(self):
        """侧栏与任务页签渲染同一批任务，所以**词表、排序、结束与否只能有一份**。

        两处各写一份（哪怕只是抄字面量）的失败形态是：「执行中」与「运行中」并存，
        而读的人会以为那是两种不同的状态。所以这里查的是"侧栏从 `jobView` 拿"、
        "app.js 把同一批函数挂出去"，而不是"两边的字符串一样"——后者在有人改了
        其中一处之后仍然会通过。
        """
        chat = _read("chat.js")
        app = _read("app.js")
        self.assertIn("ui().jobView", chat, "上下文栏没有从 jobView 拿状态词表")
        for name in ("statusOf", "label", "finished", "compare", "kindLabel"):
            self.assertIn("%s:" % name, app, "app.js 的 jobView 少了 %s" % name)

    def test_the_sidebar_jobs_block_shows_the_load_failure(self):
        """加载失败要传到侧栏，否则它会安静地留在上一批数据上。

        面板刚说过"加载失败"，侧栏却列着几条任务——同一件事在两个地方给出相反
        的答案，而侧栏那一份看起来更可信（它没有报错）。所以 app.js 必须在
        `renderJobs` 的**两个早退之前**把 error 递下去。
        """
        app = _read("app.js")
        body = app[app.index("function renderJobs("):]
        body = body[:body.index("\n}")]
        call = body.index("renderJobList(")
        early = min(body.index("if (error)"), body.index("if (!state.jobs.length)"))
        self.assertLess(call, early,
                        "renderJobList 排在早退之后：加载失败与空列表两条路径"
                        "都不会走到它，侧栏会继续显示上一批任务")
        self.assertIn("renderJobList(error)", body, "失败原因没有传下去")


class SplitPaneResizeTestCase(unittest.TestCase):
    """内部分栏可调节：三条分栏线的宽度由使用者拖动决定。

    这一批守的是**形态与接线**，不是拖拽算法本身（那要浏览器）。可调节这个能力
    有两种失败形态，都不会让任何算法测试变红：

      * 宽度写成了 inline-style —— 拖得动，但窄屏下会把栏顶成一个横条：
        inline-style 的优先级高于任何媒体查询，那一档的规则管不着它；
      * 媒体查询里仍然钉着固定宽度 —— **看着能拖、其实拖不动**。媒体查询在
        文件更后面、优先级相同，会盖掉 :root 上的变量；症状不是报错，
        是"这条线在这台机器上不好使"，比干脆没有这个功能更难查。

    两者都只在某个屏幕宽度下、或某一档下才出现，所以静态断言比截图更适合当回归网。
    """

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")
        cls._css = _read("style.css")
        cls._js = _read("split.js")

    # ------------------------------------------------------------- 工具

    @staticmethod
    def _rules(css):
        return [(" ".join(sel.split()), " ".join(body.split()))
                for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", css)]

    @staticmethod
    def _name(selector):
        """选择器去掉夹在其中的注释（与 CssLayoutGuardTestCase 同一套约定）。"""
        return re.sub(r"/\*.*?\*/", "", selector, flags=re.S).strip()

    def _body(self, selector):
        for sel, body in self._rules(self._css):
            if self._name(sel) == selector:
                return body
        return ""

    @staticmethod
    def _media_blocks(css):
        """全部 @media 块的 (条件, 块内文本)。

        不能直接复用 `_rules`：它把嵌套在 @media 里的规则与顶层规则混在一起，
        于是"这条规则属于哪一档"就丢了——而这正是本节要问的问题。
        按花括号配对切，因为块里还有规则自身的一层花括号。
        """
        blocks = []
        for match in re.finditer(r"@media[^{]*", css):
            start = match.end()                       # 指向条件后的那个 {
            depth, index = 0, start
            while index < len(css):
                if css[index] == "{":
                    depth += 1
                elif css[index] == "}":
                    depth -= 1
                    if depth == 0:
                        break
                index += 1
            blocks.append((match.group(0).strip(), css[start + 1:index]))
        return blocks

    def _splitters(self):
        """split.js 的 SPLITTERS 表 → `[{"id","pane","token"}, ...]`。

        **从这里取配置，而不是在各个用例里各抄一份。** 本节原先有三条用例各自写着
        `("chat", "graph", "assets")` 和三个 `--split-*`：加第四条分栏时，只要有人
        漏改了其中一条，那一条就从"守卫"变成了"摆设"——它仍然全绿，只是不再覆盖
        新的那一条。现在加一条分栏、漏改这里的代价是**这条用例自己先红**（它数出来
        的手柄数与 split.js 对不上），而不是安静地少管一条。
        """
        out = []
        for match in re.finditer(
                r"\{\s*id:\s*'([\w-]+)',\s*pane:\s*'([^']+)',\s*token:\s*'([^']+)'",
                self._js):
            out.append({"id": match.group(1), "pane": match.group(2),
                        "token": match.group(3)})
        return out

    def _handle_ids(self):
        return sorted(re.findall(r'class="split-handle"[^>]*data-split="([\w-]+)"',
                                 self._html))

    # ----------------------------------------------------------- 手柄结构

    def test_every_handle_is_a_keyboard_reachable_separator(self):
        ids = self._handle_ids()
        self.assertTrue(ids, "一个分栏拖拽条都没有——多半是 HTML 的写法变了")
        for split_id in ids:
            found = re.search(
                r'<div[^>]*class="split-handle"[^>]*data-split="%s"[^>]*>' % split_id,
                self._html)
            self.assertIsNotNone(found, "缺少 %s 分栏的拖拽条" % split_id)
            tag = found.group(0)
            self.assertIn('role="separator"', tag,
                          "%s：拖拽条必须以 separator 角色暴露" % split_id)
            self.assertIn('aria-orientation="vertical"', tag,
                          "%s：这是竖直分隔条，方向必须写对" % split_id)
            self.assertIn('tabindex="0"', tag,
                          "%s：只能拖的话，「可调节」就只对鼠标成立" % split_id)

    # 每条分栏两侧的锚点。这一份**必须**显式列出来（不能从 split.js 推）：栏的
    # 类名在配置里，但"栏后面那一栏长什么样"没有统一的形状——对话页是
    # `.chat-main`、链路页是 `.side-panel`、资产与报告页是裸 `.split-pane`。
    # 所以配一条 `test_the_anchor_table_covers_every_handle` 盯着这张表是否齐全。
    PANE_ANCHORS = {
        "chat": ('class="chat-side"', 'class="chat-main"'),
        # 对话页第二条线：消息流与右侧详情栏之间（任务 / 目标 / 子智能体的展开面）。
        "detail": ('class="chat-main"', 'class="chat-detail"'),
        "graph": ('class="graph-canvas"', 'class="side-panel"'),
        "assets": ('class="split-pane tree-pane"', 'class="panel-head">资产详情'),
        # 漏洞页：目标栏与漏洞列表之间。左栏换范围（全部目标 / 某一个），右栏
        # 在「全部」时按目标分组。
        "findings": ('class="split-pane findings-side"', 'class="split-pane findings-main"'),
        "report": ('class="split-pane report-side"', 'class="panel-head" id="report-doc-head"'),
        # 流量页：记录表与那一条的原文并排。左栏可折起来，右栏只剩一行标题。
        "traffic": ('class="split-pane traffic-list-pane"', 'class="split-pane traffic-detail-pane"'),
        # 请求重发页：左边那张表单，右边挂着流量列表（点一行填进去）。
        "repeater": ('class="split-pane repeater-main"', 'class="split-pane traffic-side"'),
    }

    def test_handles_sit_between_the_two_panes(self):
        """拖拽条必须在两栏之间——它**就是**那条 hairline，不是栏内的一块装饰。

        位置错了不会报错：它照样是 1px 的竖线、照样能拖，只是拖的是别的宽度。

        三个判断分开写、各有消息：锚点找不到时 `str.index` 抛的是
        `ValueError: substring not found`——那读起来像"HTML 坏了"，而不像
        "这张锚点表过时了"，归因会跑偏到完全不相干的地方。
        """
        for split_id, (before, after) in sorted(self.PANE_ANCHORS.items()):
            for side, needle in (("左", before), ("右", after)):
                self.assertIn(needle, self._html,
                              "%s 的%s栏锚点在 index.html 里找不到：%s"
                              "（这张表过时了，这条分栏的位置没人检查）"
                              % (split_id, side, needle))
            handle = self._html.index('data-split="%s"' % split_id)
            self.assertLess(self._html.index(before), handle,
                            "%s 的拖拽条排在左侧那栏之前" % split_id)
            self.assertLess(handle, self._html.index(after),
                            "%s 的拖拽条排在右侧那栏之后" % split_id)

    def test_the_anchor_table_covers_every_handle(self):
        """上面那张锚点表必须覆盖每一个手柄。

        它是本节唯一一处写死的名单，因此**必须有东西盯着它**：漏一条的表现是那条
        分栏的位置完全没人检查——而位置错了正好是"照样能拖、只是拖错了宽度"，
        看是看不出来的。
        """
        self.assertEqual(sorted(self.PANE_ANCHORS), self._handle_ids(),
                         "有手柄没有出现在 PANE_ANCHORS 里：它的位置不会被检查")

    def test_every_handle_is_wired_in_split_js(self):
        """DOM 里的手柄与 split.js 的配置必须一一对应。

        少配一条 = 页面上有一条**看起来能拖、按下去没反应**的线。

        这里**不再写死名单**：两边各自数出来再比——写死的那一份迟早会在加第四条
        分栏时被漏掉，而漏掉之后这条用例仍然全绿。
        """
        ids = self._handle_ids()
        wired = sorted(item["id"] for item in self._splitters())
        self.assertEqual(wired, ids, "手柄与 split.js 的 SPLITTERS 对不上")
        self.assertEqual(len(self._splitters()), len(
            re.findall(r"\{\s*id:\s*'[\w-]+'", self._js)),
            "SPLITTERS 里有一条不是 `{ id, pane, token }` 这个形状：宽度或栏名"
            "取不到，本节的断言会静默漏掉它")

    # ----------------------------------------------------------- 宽度来源

    def test_width_comes_from_custom_properties_not_inline_styles(self):
        """宽度必须落在 CSS 自定义属性上。

        inline-style 的优先级高于任何媒体查询：那样窄屏下上下文栏会被顶成一个
        296px 高的横条，恰好压掉它本该让给消息流的高度。
        令牌与栏名都从 split.js 取，理由见 `_splitters`。
        """
        for item in self._splitters():
            self.assertIn("%s:" % item["token"], self._css,
                          "%s 没有在 :root 里定义（引用它的声明会整条作废）"
                          % item["token"])
        self.assertIn("setProperty", self._js, "宽度没有经 setProperty 下发")
        for banned in (".style.width", ".style.flexBasis", ".style.flex"):
            self.assertNotIn(banned, self._js,
                             "宽度不得写成 inline-style（%s）" % banned)

    def test_each_pane_reads_its_own_token(self):
        for item in self._splitters():
            self.assertIn("var(%s" % item["token"], self._body(item["pane"]),
                          "%s 没有读它自己的宽度变量 %s——拖那条线改的是别的栏"
                          % (item["pane"], item["token"]))

    def test_the_divider_has_a_single_owner(self):
        """分栏线只能有一个主体。

        .chat-side 的右边框、.side-panel 的左边框原先就是那条线；拖拽条接手之后
        留着它们会并排出现两条 1px 的线，看起来像"分隔线被加粗了"。
        """
        self.assertNotIn("border-right", self._body(".chat-side"))
        self.assertNotIn("border-left", self._body(".side-panel"))
        self.assertNotIn(".split-pane + .split-pane", self._css,
                         "相邻兄弟选择器已经不成立（中间多了拖拽条），该规则应撤掉")

    # --------------------------------------------------------------- 窄屏

    def test_stacked_layout_hides_the_vertical_handle(self):
        """窄屏下对话栏折成横条，那条竖线不成立，必须连手柄一起隐藏。

        不隐藏的话它仍然可见、仍然能按住——而"把一个已经变成横条的栏拖宽"，
        改的是它此刻毫无意义的那个尺寸，回到宽屏才发现宽度被改过了。
        """
        block = ""
        for cond, body in self._media_blocks(self._css):
            if "max-width: 900px" in cond:
                block = body
        self.assertTrue(block, "找不到 900px 那一档媒体查询")
        self.assertIn('.split-handle[data-split="chat"]', block)
        self.assertIn("display: none", block)

    def test_no_media_query_repins_the_draggable_widths(self):
        """任何媒体查询都不得再给这些栏写死宽度。

        这是"看着能拖、其实拖不动"的唯一来源：媒体查询在后、优先级相同，
        会盖掉 :root 上的变量。踩过一次就够——在这里静态钉死，
        而不是等下次改版再靠肉眼发现。`max-height` 不算：它不含 width 这个词。

        栏名从 split.js 取（见 `_splitters`）：写死的那一份在加栏时会被漏掉，
        而漏掉的那一栏正是"看着能拖、其实拖不动"会再次发生的地方。
        """
        panes = {item["pane"] for item in self._splitters()}
        offenders = []
        for cond, body in self._media_blocks(self._css):
            for sel, rule in re.findall(r"([^{}]+)\{([^}]*)\}", body):
                name = self._name(sel)
                if name not in panes:
                    continue
                if "width" in rule and "var(--split-" not in rule:
                    offenders.append("%s → %s" % (cond, name))
        self.assertEqual(offenders, [],
                         "媒体查询重新钉死了可拖动的栏宽：%s" % offenders)

    def test_reading_the_stored_width_cannot_break_the_console(self):
        """localStorage 在隐私模式、被企业策略禁用时会**读取即抛**。

        这条路径上漏一个 try，控制台启动就会被一份"上次拖到哪"的偏好带崩，
        而症状与宽度毫无关系（整页起不来），排查要花很久。
        """
        for fn_name in ("read", "write"):
            start = self._js.index("function %s(" % fn_name)
            body = self._js[start:self._js.index("\n  }", start)]
            self.assertIn("try", body, "%s() 缺少 try" % fn_name)
            self.assertIn("catch", body, "%s() 缺少 catch" % fn_name)

    def test_drag_below_the_slop_does_not_change_the_width(self):
        """手抖不算拖动：按下就改宽度会让"我只是想点一下这条线"变成一次误操作。

        阈值必须与链路图的点选/拖拽同源，两处的手感才是一致的。
        """
        self.assertIn("CLICK_SLOP", self._js)
        self.assertIn("CLICK_SLOP_PX = 4", _read("app.js"),
                      "split.js 的阈值应当与 app.js 的 CLICK_SLOP_PX 一致")


class ReportSidebarTestCase(unittest.TestCase):
    """报告页左侧的**作业报告目录**：一次执行一篇报告，点一条读那一篇。

    这一块有三件"看起来对、其实错"的事，都不会让任何功能测试变红：

      * **只列当前目标那几条** —— 于是跨目标的那部分执行记录被藏起来了，而它正是
        解释"为什么报告里有几条资产看着陌生"的答案。清单本身完全没有异样。
      * **点了之后把当前目标也换掉** —— 读一份归档顺手挪走使用者的目标，漏洞 / 资产
        两个页签跟着变，而报告正文看起来完全正常。
      * **前端发的查询参数与后端读的不是同一个名字** —— 后端会忽略它，照常返回
        **汇总报告**：一篇格式完整、内容合理的文档，只是不是你要的那一篇。
        最后这一条最危险，因为两边都不报错，所以它由一条跨文件断言钉住。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = _read("index.html")
        cls.js = _read("app.js")

    def _panel(self):
        rest = self.html[self.html.index('id="panel-report"'):]
        end = rest.find('id="panel-', 1)
        return rest[:end] if end > 0 else rest

    # ------------------------------------------------------------- 结构

    def test_the_panel_is_a_split_with_a_foldable_left_pane(self):
        """报告页是"目录 | 正文"两栏，目录那一栏可折、可拖。"""
        panel = self._panel()
        self.assertIn('class="split"', panel, "报告页不是一个分栏结构")
        self.assertIn('id="report-side-pane"', panel)
        self.assertIn('data-fold-part="report-side"', panel)
        self.assertIn("fold-head", panel, "目录那一栏没有折叠头")
        handle = panel.index('data-split="report"')
        self.assertLess(panel.index('id="report-side-pane"'), handle,
                        "分栏线排在了目录栏之前")
        self.assertLess(handle, panel.index('id="report-body"'),
                        "分栏线排在了正文之后")
        # 正文仍然是 `.markdown` 的**直接宿主**（.split-pane 就是它的 flex 容器）：
        # 多套一层 .panel-body 会把 padding 与 overflow 各叠一份，那个形态看起来
        # 只是"报告页两边留白变宽了"。
        self.assertIn('article class="markdown" id="report-body"', panel)

    def test_the_document_head_is_a_span_so_it_can_ellipsize(self):
        """正文卡头那行字必须装在一个元素里。

        `.panel-head` 是 flex，纯文本节点吃不到 `text-overflow: ellipsis`——目标是
        完整路径时有 60 多个字符，换成纯文本就会把卡头撑成两三行。
        """
        panel = self._panel()
        self.assertIn('id="report-doc-head"', panel)
        self.assertIn('id="report-doc-name"', panel)
        self.assertIn("doc-name", _read("style.css"),
                      "卡头那行字没有样式：它需要 ellipsis，否则会换行")

    # ------------------------------------------------------------- 数据

    def test_the_sidebar_lists_every_task_not_only_the_current_target(self):
        """目录列**全部**任务，不是只列当前目标名下那几条。

        判据是"渲染时读的是 `state.jobs` 全表"。按 `engagementId` 过滤一次就会
        悄悄退化成"只看得到当前目标的执行记录"，而那正是这一块要解决的问题。
        """
        body = _fn_body(self.js, "renderReportSide")
        self.assertTrue(body, "app.js 里没有 renderReportSide")
        self.assertIn("state.jobs", body, "目录没有读全部任务")
        self.assertNotIn("engagementId ===", body,
                         "目录按目标过滤了任务——跨目标那几次执行会整批消失，"
                         "而它们往往正是解释这份报告的关键")
        # 但每一行必须自己说清是哪个目标的（跨目标那几条否则看起来像本目标的）。
        row = _fn_body(self.js, "reportSideRow")
        self.assertIn("shortTarget", row, "行里没有标出它属于哪个目标")

    def test_the_archive_is_ordered_by_time_not_by_status(self):
        """目录按时间倒序：它是归档，不是队列。

        任务页按状态分组（执行中 → 待执行 → 失败 → 已完成），那回答的是"接下来轮到
        谁"。归档回答的是"最近跑过什么"。两处用同一个排序，两边都不对——而"顺序
        不对"没人会当成缺陷报上来。
        """
        body = _fn_body(self.js, "renderReportSide")
        self.assertIn("createdAt", body, "目录没有按时间排序")
        self.assertNotIn("compare(", body,
                         "目录用了任务页那个状态排序：归档应当按时间倒序")

    def test_clicking_a_row_switches_the_document_not_the_target(self):
        """点一行只换文档，**不换当前目标**。

        目录是一个跨目标的归档（见上面那条），读它不该把使用者的目标挪走：漏洞、
        资产两个页签的范围都跟着目标走。换目标时那份文档由 `selectEngagement` 单独
        重置——两件事的语义不同，所以入口也必须是两个。
        """
        self.assertIn("showReport", self.js)
        body = _fn_body(self.js, "showReport")
        self.assertTrue(body, "app.js 里没有 showReport")
        self.assertNotIn("selectEngagement", body,
                         "点任务行时切换了当前目标——读一份归档不该动使用者的目标")
        # 事件委托挂在容器上（目录每 10 秒重建一次，逐行绑定会叠加监听器）。
        self.assertIn("'#report-side'", self.js, "目录没有挂事件委托")
        self.assertIn("rpt-row", self.js, "委托里没有按行类名命中")

    def test_the_head_reads_the_document_the_server_confirmed(self):
        """卡头读**响应里**的那一份，不按 `state.reportJob` 猜。

        `state.jobs` 可能还没加载出来（那一刻按任务号找不到 job），猜的话卡头会写
        「本目标汇总」而正文是某次任务的报告——两处都言之凿凿，读者只能猜该信哪个。
        """
        body = _fn_body(self.js, "loadReport")
        self.assertIn("data.jobId", body,
                      "没有把响应里的 jobId 落进 state.reportDoc：卡头只能猜")
        head = _fn_body(self.js, "renderReportDocHead")
        self.assertTrue(head, "app.js 里没有 renderReportDocHead")
        self.assertIn("state.reportDoc", head, "卡头没有读服务端确认过的那一份")

    def test_the_query_parameter_matches_what_the_backend_reads(self):
        """前端的 `?job=` 与后端读的键必须是同一个。

        **这条是最要紧的一条。** 名字对不上时后端会忽略它、照常返回汇总报告——
        一篇格式完整、内容合理的文档，只是不是你要的那一篇，而两边都不报错。
        """
        self.assertIn("?job=", self.js, "前端没有拼 ?job= 查询参数")
        with open(os.path.join(ROOT, "pentestd", "server.py"), encoding="utf-8") as handle:
            server = handle.read()
        self.assertIn('_query_text(ctx["query"], "job")', server,
                      "后端读的不是 `job` 这个查询参数名——前端发的那一个会被忽略，"
                      "报告页会安静地显示汇总报告")

    def test_copy_and_download_follow_the_shown_document(self):
        """下载的文件名必须能区分是哪一篇。

        目录里可以同时存着好几篇报告，而它们往往还是同一个目标的（同一个目标跑了
        很多次）。文件名全一样的话，落到下载目录里就再也分不出谁是谁。
        """
        body = _fn_body(self.js, "downloadReport")
        self.assertTrue(body, "app.js 里没有 downloadReport")
        self.assertIn("state.reportJob", body, "文件名没有带任务号")
        copy = _fn_body(self.js, "copyReport")
        self.assertIn("state.markdown", copy,
                      "复制读的不是**当前显示的**那一份")

    # ------------------------------------------------------------- 接线

    def test_a_jobs_refresh_redraws_the_sidebar_without_losing_the_scroll(self):
        """任务刷新要带上目录，而且不能把读者的滚动位置顶回去。

        `renderJobs` 是"任务数据变了"的唯一入口，目录必须挂在它里面——挂在别处会
        漏掉自动刷新那条路径。而它每 10 秒跑一次，重画会清空容器、滚动位置回到
        顶部：正在读的那一条会被顶走，表现是"列表自己跳了一下"。
        """
        jobs = self.js[self.js.index("function renderJobs("):]
        jobs = jobs[:jobs.index("\n}")]
        self.assertIn("renderReportSide(", jobs, "任务刷新没有重画报告目录")
        early = min(jobs.index("if (error)"), jobs.index("if (!state.jobs.length)"))
        self.assertLess(jobs.index("renderReportSide("), early,
                        "renderReportSide 排在早退之后：加载失败与空列表两条路径"
                        "都不会走到它，目录会继续显示上一批任务")
        body = _fn_body(self.js, "renderReportSide")
        self.assertIn("scrollTop", body, "重画没有保住滚动位置")

    def test_the_report_tab_keeps_the_task_list_fresh(self):
        """报告页签也要让任务读数自己长，而且要排在"没选目标"那个早退之前。

        目录列的是全部任务，所以没有选中目标时它照样该显示。停在进入那一刻的内容的
        表现是：一条正在跑的任务永远写着「执行中」，看起来像卡住了。
        """
        body = self.js[self.js.index("function loadTabData("):]
        body = body[:body.index("\n}")]
        self.assertIn("tab !== 'report'", body,
                      "离开报告页签时没有停掉自动刷新：那会是一次没人看的请求")
        report = body[body.index("tab === 'report'"):]
        self.assertIn("watchJobs()", report, "报告页签没有接上任务刷新")
        early = body.index("if (!state.eid) return;")
        self.assertLess(body.index("tab === 'report'"), early,
                        "报告页签那一支排在 eid 判空之后：没有选中目标时目录是空的，"
                        "而它本来就该列得出任务")

    def test_the_reset_to_the_summary_happens_only_when_the_target_changes(self):
        """换目标时报告回到汇总；点任务行时不回（只换文档）。

        前者是"我要看这个目标"（报告页的范围跟着走），后者是"我要读这一篇"
        （范围不动）。两处混在一起的表现是：点一下任务，目标也跟着跳了，而使用者
        以为自己只是看了份报告。

        导航里那个「报告」入口是**第三处**、也是回到汇总的另一条路，见
        `test_the_rail_entry_opens_the_summary_not_the_last_task`。
        """
        body = _fn_body(self.js, "selectEngagement")
        self.assertTrue(body, "app.js 里没有 selectEngagement")
        self.assertIn("reportJob: ''", body.replace('reportJob: ""', "reportJob: ''"),
                      "换目标时没有把报告重置回汇总：正文会停在另一个目标那篇上")

    def test_the_rail_entry_opens_the_summary_not_the_last_task(self):
        """左侧「产出 → 报告」进来必须是**本目标汇总**，不是上次读到的那一篇。

        目录里点一行是"我要读这一篇"，导航点一次是"看这个目标的报告"——后者是这
        一页的入口，而这一页交付的就是汇总那一篇。实测过的缺陷：点开一篇作业报告
        → 切去任务页 → 从导航回报告，屏幕上还是那一篇，页头写着它的任务号，左栏
        高亮也还压在那条任务上。判据因此落在"进这一页时 `reportJob` 被清掉"，
        而不是"卡头写了一句话"——`/report` 不带 `?job=` 才是汇总。
        """
        body = _fn_body(self.js, "switchTab")
        self.assertTrue(body, "app.js 里没有 switchTab")
        self.assertIn("enteringReport", body,
                      "进报告页签时没有把文档重置回汇总：导航点一次会停在上次读到的那一篇")
        self.assertIn("state.reportJob = ''", body.replace('state.reportJob = ""',
                                                           "state.reportJob = ''"),
                      "重置没有落到 `reportJob` 上——正文仍会按旧任务号取那一篇报告")
        self.assertIn("state.tab !== 'report'", body.replace('state.tab !== "report"',
                                                             "state.tab !== 'report'"),
                      "没有判「从别的页签进来」：连点两次导航会把读者刚打开的那一篇也清掉")

    def test_switching_the_document_drops_the_cached_one(self):
        """换一篇文档必须重取，不能复用上一篇的缓存。

        `loadResource` 的缓存只记 `loaded[key]` 一个布尔，它不知道取的是**哪一篇**。
        于是"从作业报告回汇总"这条路上，`apply` 一次都不会跑：正文停在作业报告
        的 markdown 上，而卡头读的是刚刚清过的 `reportDoc`。这一页没有报错、没有
        空态，只是纸上写着另一篇——读完就交出去的那种错。
        """
        body = _fn_body(self.js, "loadReport")
        self.assertIn("state.reportPath", body,
                      "报告缓存没有按文档分辨：换一篇后正文会停在上一篇上")
        self.assertIn("switchedDoc", body, "没有算出\"这一篇和缓存里那篇不是同一篇\"")
        self.assertIn("force || switchedDoc", body,
                      "算出了换了文档却没有让它失效——缓存照样命中")

    def test_the_sidebar_highlight_follows_the_document(self):
        """点一行之后左栏高亮要立刻跟手，不能等下一次任务刷新。

        目录只在任务读数变化时重画（`renderJobs`，每 10 秒一次）。不在这里补一次，
        点完之后最长 10 秒里正文已经换了、高亮还压在上一行——而高亮是读者判断
        "这一篇是哪儿来的"的唯一线索。
        """
        body = _fn_body(self.js, "showReport")
        self.assertTrue(body, "app.js 里没有 showReport")
        self.assertIn("renderReportSide", body,
                      "换文档后没有重画目录：选中态要等 10 秒后的任务刷新才更新")

    def test_the_summary_row_is_the_way_back(self):
        """目录里那一行「本目标汇总」必须存在且可点回——它是唯一的回头路。

        换文档只走 `showReport('')`，所以"空串 = 汇总"这件事在目录里必须是一个
        能点的行，而不是只写在注释里。
        """
        side = _fn_body(self.js, "renderReportSide")
        self.assertIn("reportSideRow(null)", side,
                      "目录里没有「本目标汇总」那一行：读了一篇作业报告就回不去了")

class InitTimeSelectorTestCase(unittest.TestCase):
    """初始化那一刻**不许**去找一个还没渲染出来的节点。

    `bindEvents()` / `bindGraphEvents()` 跑在任何渲染之前，此刻文档里只有
    index.html 写死的那批节点。在那里 `$('#x')` 落到一个渲染期才造的节点上，拿到
    null——而 `$` 就是 `querySelector`，它**不抛**，抛的是紧跟着的
    `.addEventListener`。于是整个 `init()` 在中途断掉，它后面的
    `loadStatus` / `loadGraph` / `loadEngagements` 一行都不执行。

    界面上的表现是**顶栏、漏洞、资产全空**，而错误条上那句话在讲一个日志面板：
    症状离原因很远，靠肉眼从截图里根本回不到现场。2026-10-15 用户报的就是这个
    （`$('#job-log')`，而 `#job-log` 是 `jobLogPanel()` 现造的）。

    这一条不看运行、不看截图，只看"初始化那一片代码用到的 id 是不是静态的"。
    """

    @classmethod
    def setUpClass(cls):
        cls.app = _read("app.js")
        cls.html = _read("index.html")

    # 一次性绑定函数：启动时跑一次，跑在任何渲染之前。**新增绑定函数要加进这张表**
    # ——忘掉的代价是这一段失去覆盖（另外两条守卫仍然看着它），不是误报。
    BINDING_FUNCTIONS = ("bindEvents", "bindGraphEvents", "bindPlanEvents")

    def _init_region(self) -> str:
        """`init()` 本身 + 它**独立语句**调用的那些函数 + 上面那几个绑定函数。

        闭合就停在这一跳上，不再往深走：`init → bindEvents → …` 再往下的那些是
        渲染与视图变换（`applyTransform` / `clearHighlight` / `renderExecutors` …），
        它们跑到的时候 DOM 已经在了——把 `$('#graph-viewport')` 这种"图先画出来、
        再取那个 <g>"的正当写法也算进来，守卫就会去报正确代码，而守卫报正确代码的
        代价是有人把它删掉。再深处的异常由 `BootChainTestCase` 那台真浏览器兜住。
        """
        first = _fn_body(self.app, "init")
        parts = [first]
        for name in _bare_calls(first) | set(self.BINDING_FUNCTIONS):
            parts.append(_fn_body(self.app, name))
        return "\n".join(part for part in parts if part)

    def test_the_startup_path_only_touches_static_ids(self):
        region = self._init_region()
        self.assertTrue(region.strip(), "没有取到 init 那一片源码，守卫本身要修")
        # 自检：这条守卫必须真的把 bindEvents 收了进来，否则它只是"碰巧绿"
        self.assertIn("bindEvents", region, "启动路径没走到 bindEvents，取源方式要修")
        missing = sorted(_id_selectors(region) - _static_ids(self.html))
        self.assertEqual(
            missing, [],
            "启动路径引用了 index.html 里没有的 id：%s。"
            "这些节点要等渲染才有，而这段代码跑在渲染之前——"
            "`$()` 拿到 null 不会在那一行报错，报错的是下一步的 .addEventListener，"
            "于是 init() 从中途断掉，后面几次数据加载全部不执行"
            "（表现：顶栏、漏洞、资产全空）。渲染期才造的节点要在渲染之后取，要么"
            "改成事件委托挂在静态容器上——本文件的既有写法就是后一种。" % missing)


class BootChainTestCase(unittest.TestCase):
    """真实启动链（auth.js + boot.js + app.js）在真浏览器里跑一遍。

    为什么静态守卫之外还要这一条：`tools/render_ui_fixture.py` **丢掉了 auth.js 与
    boot.js**（它不需要登录门），于是 app.js 走的是它自己那条自启动分支，而真实
    页面走的是 `boot.js enterConsole()`——那一段整个跑在 auth.js 监听器的
    try/catch 里，任何一步抛出，它后面的步骤都不执行。上一类静态守卫能拦住
    "初始化时找一个还没有的节点"，拦不住启动链上别的异常。

    断言五件事，缺一不可：
      * 启动过程中没有异常、也没有未处理的 Promise 拒绝；
      * 错误条是空的（它就是使用者看到的那句"界面初始化失败"）；
      * `/health`、`/graph`、`/engagements`、**`/pulse`** 四个请求都发生了——前三个
        不取，界面上就是"顶栏、漏洞、资产全空"；而 `/pulse` 不取，侧边栏那两个读数块
        **永远不会自己变新**（它此前在真实启动链上一条入口都没接上，所以这一条是补的）；
      * 探针那两场"**进了会话页之后没人再动过它**"（目标与对话是启动之后才出现的 /
        启动那份列表是旧的）里，侧边栏自己跟上了：目标行与对话行都出现了。
        那两场是**可以红的**——把三处修复各自改回旧语义，红的正是对应的那一场。
        "进过会话页"这一下是场景的一部分，不是替被测代码铺路：默认页签是「仪表盘」，
        其余面板都是 `display:none`，而这两场读的 `#chat-goals` / `#chat-sessions`
        与会话页那张审查卡浮层都住在会话页里（浮层的判据是 `getClientRects()`，
        面板不显示时这个数**必然**是 0，与"浮层收起了"长得一模一样）。点这一下走的
        就是使用者的真实入口（`.tab[data-tab="dsh"]` → `switchTab` → `loadTabData`），
        之后探针不再碰页面。反过来说，把默认页签改回会话让它变绿是不行的——那等于把
        "进了这一页心跳有没有起"这条覆盖一起改回旧形态。
      * **第四场：一颗按钮两种身份**（发送 / 停止）。空闲时它叫发送且能点，跑起来之后
        它叫停止、**仍然能点**，而第二下点击真的把请求中止掉了。这一改动的三处风险
        （忙碌时被一起禁用 / 点击没分派 / 名字没跟着状态走）没有一处会让别的用例变红，
        界面上却分别表现成"按钮在、点不动""点了没反应""名字与做的事对不上"。
      * **第五场：审查卡的去留**。点之前那一格是「待批」、点完 900ms 还看得见「已批准」、
        停留时间过后那一格**空掉**。这一场的判别力押在"点击那一刻这一轮在跑"上（没有
        活流时，裁决后的记录区重取会顺手清掉那一格，于是它永远绿——第一次写正是如此），
        所以探针另外报一个 `live`，没跑起来就算没通过。三个时刻的先后由
        `ReviewBandTimingTestCase` 钉着。

    没有 Edge / Chrome 的机器上跳过（探针会报 `browser: ""`）。**探针连 JSON 都
    没打出来时是失败、不是跳过**：那说明探针自己坏了，跳过会让覆盖静默消失。
    """

    @classmethod
    def setUpClass(cls):
        cls.probe = os.path.join(ROOT, "tools", "probe_boot_chain.py")

    def test_the_real_startup_chain_completes_without_errors(self):
        self.assertTrue(os.path.exists(self.probe), "启动链探针不见了：%s" % self.probe)
        proc = subprocess.run([sys.executable, self.probe, "--json"], cwd=ROOT,
                              capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=900)
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        self.assertTrue(lines, "探针没有输出 JSON（stderr：%s）" % (proc.stderr or "")[-400:])
        try:
            report = json.loads(lines[-1])
        except ValueError as error:
            self.fail("探针最后一行的 JSON 解不开（%s）：%s" % (error, lines[-1][:300]))
        if not report.get("browser"):
            self.skipTest("本机没有 Edge / Chrome，跳过启动链探针")
        problems = []
        if report.get("errors"):
            problems.append("启动链抛了异常：%s" % report["errors"])
        if report.get("error_bar"):
            problems.append("错误条不是空的：%s" % report["error_bar"])
        if report.get("missing_core"):
            problems.append("这几个接口一次都没取过：%s（前三个不取，界面上就是"
                            "顶栏 / 漏洞 / 资产全空；`/pulse` 不取，侧边栏那两个读数块"
                            "不会自己变新）" % report["missing_core"])
        for key, label in (("late_goal", "目标与对话**启动之后才出现**（进过会话页之后没人再动它）"),
                           ("stale_list", "启动那份列表是旧的、而第一次探询服务端已有目标"),
                           ("button", "一颗按钮两种身份（发送 / 停止）"),
                           ("review", "审查卡的去留（那一格只摆「要人动手」的东西）")):
            one = report.get(key) or {}
            if one and not one.get("ok", True):
                problems.append("%s 那一场没通过：%s" % (label, one.get("problems")))
        self.assertEqual(problems, [],
                         "真实启动链没跑完：\n  - " + "\n  - ".join(problems)
                         + "\n（完整请求序列：%s）" % report.get("paths"))


class ReviewBandTimingTestCase(unittest.TestCase):
    """输入框上方那一格：裁决过的卡片停留一下就走 —— 三个时刻必须严格有序。

    这一条守的不是"某段源码长什么样"，而是**第五场那个浏览器用例还有没有判别力**。
    （实测过：这三个数错位时，用例照旧全绿，而它本该抓住的回退一个都没抓住。）

        淡出结束 < 探针的 after 快照 < 该走 < 探针的 later 快照

      · `after` 若早于淡出结束：把停留时间改成 0，卡片在那一刻**还在**（淡出没走完），
        "点完看不见结果"这个回退就抓不到；
      · `later` 若早于"该走"：卡片本来还在，快照却以为它没了（假红）；
      · `该走` 若晚于 `refreshTranscriptSoon` 的最后一次重取（9000）：那一格会因为
        **别的原因**空掉，"自己走的"与"被清掉的"再也分不开——而"裁决过的卡片该走"
        这条规则本身也就失去了界面上的可分辨性。
    """

    def _source(self, *parts):
        path = os.path.join(ROOT, *parts)
        with open(path, "r", encoding="utf-8") as handle:
            return handle.read()

    def _number(self, source, pattern, what):
        found = re.search(pattern, source)
        self.assertIsNotNone(found, "在源码里找不到 %s（正则 %r）" % (what, pattern))
        return int(found.group(1))

    def test_the_probe_snapshots_straddle_the_card_leaving(self):
        chat = self._source("web", "chat.js")
        probe = self._source("tools", "probe_boot_chain.py")
        css = self._source("web", "style.css")
        linger = self._number(chat, r"DONE_LINGER_MS = (\d+);", "停留时间")
        fade = self._number(chat, r"DONE_FADE_MS = (\d+);", "淡出时长")
        after = self._number(probe, r"snap\('review-after'\); \}, (\d+)\)", "after 快照")
        later = self._number(probe, r"snap\('review-later'\); \}, (\d+)\)", "later 快照")
        polls = max(int(x) for x in re.findall(
            r"\[(\d+(?:, \d+)+)\]\.forEach\(\(delay\)", chat)[0].split(", "))
        self.assertLess(fade, after,
                        "after 快照（%dms）落在淡出（%dms）之内：停留时间被改成 0 时它"
                        "看不出区别，第五场就抓不住「点完看不见结果」" % (after, fade))
        self.assertLess(linger + fade, later,
                        "later 快照（%dms）早于卡片该走的时刻（%dms）：那时它还该在，"
                        "用例会假红" % (later, linger + fade))
        self.assertLess(linger + fade, polls,
                        "卡片该走的时刻（%dms）不早于 `refreshTranscriptSoon` 的最后一次"
                        "重取（%dms）：那一格会因为**别的原因**空掉，第五场永远绿"
                        % (linger + fade, polls))
        # 淡出那一步靠 CSS，两边的数必须是一个（CSS 拿不到常量，只能这样钉）。
        rule = css[css.find(".chat-review-leaving"):]
        rule = rule[:rule.find("}")]
        self.assertIn("opacity", rule, "`.chat-review-leaving` 没有 opacity")
        found = re.search(r"transition:\s*opacity\s*([0-9.]+)s", rule)
        self.assertIsNotNone(found, "`.chat-review-leaving` 没有写淡出时长：%r" % rule)
        self.assertAlmostEqual(float(found.group(1)), fade / 1000.0, places=3,
                               msg="`.chat-review-leaving` 的时长（%ss）与 `DONE_FADE_MS`"
                                   "（%dms）不是同一个数——淡出比删除早结束会露出一瞬间"
                                   "的空白高度" % (found.group(1), fade))


class GuideToolCountTestCase(unittest.TestCase):
    """`guide.js` 里那张"权限档 → 工具"的表必须与 `policy.TOOL_LEVEL` 一致。

    这不是形式主义：写这份用例时它已经漂了很久——表上写着 13 / 16 / 17 / 23·24，
    而代码里是 20 / 30 / 33 / 40·42。漂了十八项而没有任何地方会红，因为
    **文档里的数字没有任何东西在对**。使用者照着它判断"调到这一档能多拿到什么"，
    而那个判断从第一项起就是错的。

    钉住的是**两样**：总数量，以及每一档新增的那几个工具名。只钉总数是不够的
    ——总数对而名单错（换了工具但数量没变）同样会误导。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(ROOT, "web", "guide.js"), "r",
                  encoding="utf-8") as handle:
            cls.guide = handle.read()
        from pentestd import policy
        cls.policy = policy

    LEVEL_LABEL = {"readonly": "只读", "probe": "探测", "audit": "审计",
                   "full": "完全作业"}

    def _levels(self):
        """(档位 id, 该档允许的工具集, 相对上一档新增的工具集)。"""
        out, previous = [], set()
        for item in self.policy.POLICIES:
            current = set(self.policy.allowed_tools(item["id"], "admin"))
            out.append((item["id"], current, current - previous))
            previous = current
        return out

    def test_the_total_count_matches_the_code(self):
        total = len(self.policy.TOOL_LEVEL)
        self.assertIn("一共 **%d 项**" % total, self.guide,
                      "guide.js 里的工具总数与 policy.TOOL_LEVEL 不一致"
                      "（代码里是 %d 项）" % total)

    def test_each_levels_count_and_names_match_the_code(self):
        problems = []
        for policy_id, tools, added in self._levels():
            # 只读那一档写「只读（20）」，其余写「探测（+10）」——与 guide.js 同形
            label = ("%s（%d）" % (self.LEVEL_LABEL[policy_id], len(tools))
                     if policy_id == "readonly"
                     else "%s（+%d）" % (self.LEVEL_LABEL[policy_id], len(added)))
            row = self._row_of(label)
            if row is None:
                problems.append("guide.js 里没有这一档的标签：%s" % label)
                continue
            # 名字必须出现在**它该出现的那一行**上：跑错行就是"这一档拿得到什么"
            # 说错了，而总数仍然是对的。
            for name in sorted(added):
                if name not in row:
                    problems.append("%s 那一行里没有 %s" % (label, name))
        self.assertEqual([], problems, "guide.js 的档位表与代码不一致：\n  - "
                         + "\n  - ".join(problems))

    def _row_of(self, label):
        for line in self.guide.splitlines():
            if line.strip().startswith("['" + label):
                return line
        return None




class AdminToolDeltaTestCase(unittest.TestCase):
    """「管理员那档多几项」这件事三份文档各写了一遍，而它们谁都没在跟代码对。

    实测漏掉的形状：`使用手册.md` 的权限档表写着 20 / 32 / 35 / 42·44（管理员「多两项」），
    `web/guide.js` 写着 45（操作员）/ 47（管理员），`项目说明.md` 写着 45 项（管理员 47 项）
    —— 而代码里操作员是 23 / 35 / 38 / 44、管理员是 47，`policy.ADMIN_TOOLS` 是三而不是二。
    只读那一档少写了 3 项，照表判断的人会以为「这一档只有 20 个动作可用」。

    为什么 `GuideToolCountTestCase` 没发现：它守的是 `guide.js` 里那张**档位 → 工具名**
    的明细表（`只读（23）` / `探测（+12）` …），那张表一直是对的。漂的是**另一张**
    表（设置页里「档 / 能做什么 / 工具数」三列那张）和三份 Markdown——同一件事写两遍，
    只守一处就等于另一处没有守卫。

    钉住三样：每档的工具数、完全作业那两个数、以及差额归给管理员的那几名工具被点了名。
    只钉总数不够：总数对而差额归错（某个工具从操作员挪到管理员、两边总数都不变）
    照样会误导。
    """

    # 中文数字只用来核对文案里「多三项」这类写法。改字面就让它红，比放宽正则好。
    CN_NUM = {1: "一", 2: "二", 3: "三", 4: "四", 5: "五"}

    @classmethod
    def setUpClass(cls):
        from pentestd import policy
        cls.policy = policy
        cls.guide = _read("guide.js")
        cls.manual = cls._doc("使用手册.md")
        cls.project = cls._doc("项目说明.md")

    @staticmethod
    def _doc(name):
        with open(os.path.join(ROOT, name), "r", encoding="utf-8") as handle:
            return handle.read()

    def _counts(self):
        """(档位 id, 标签, 操作员能调用的项数, 管理员相对操作员多几项)。"""
        out = []
        for item in self.policy.POLICIES:
            operator = len(self.policy.allowed_tools(item["id"], "operator"))
            admin = len(self.policy.allowed_tools(item["id"], "admin"))
            out.append((item["id"], item["label"], operator, admin - operator))
        return out

    @staticmethod
    def _row(text, prefix):
        for line in text.splitlines():
            if line.strip().startswith(prefix):
                return line.strip()
        return None

    def test_guide_policy_table_matches_the_code(self):
        """设置页那张「档 / 能做什么 / 工具数」的表。"""
        problems = []
        for policy_id, label, operator, delta in self._counts():
            # 表里最后一档写着「完全作业（默认）」——默认档带后缀。
            prefix = "['%s%s'" % (label, "（默认）" if policy_id == "full" else "")
            row = self._row(self.guide, prefix)
            if row is None:
                problems.append("guide.js 里没有这一档：%s" % label)
                continue
            if policy_id != "full":
                if not row.endswith("'%d']," % operator):
                    problems.append("%s 那一行写的不是 %d：%s" % (label, operator, row))
                continue
            admin = operator + delta
            expected = "'%d（操作员）/ %d（管理员）']," % (operator, admin)
            if not row.endswith(expected):
                problems.append("完全作业那一行要写成 %s，实际是：%s"
                                % (expected, row))
        self.assertEqual([], problems,
                         "guide.js 的权限档表与代码不一致：\n  - "
                         + "\n  - ".join(problems))

    def test_manual_table_matches_the_code(self):
        """`使用手册.md` §4.4 那张表——出事的就是它（四档全错）。"""
        problems = []
        for policy_id, label, operator, delta in self._counts():
            prefix = "| **%s**%s |" % (label, "（默认）" if policy_id == "full" else "")
            row = self._row(self.manual, prefix)
            if row is None:
                problems.append("使用手册.md 里没有这一档：%s" % label)
                continue
            if policy_id != "full":
                if not row.endswith("| %d |" % operator):
                    problems.append("%s 那一行写的不是 %d：%s" % (label, operator, row))
                continue
            admin = operator + delta
            head = "**%d**（操作员）<br>**%d**（管理员" % (operator, admin)
            if head not in row:
                problems.append("完全作业那一行要含 %s，实际是：%s" % (head, row))
            word = "多%s项" % self.CN_NUM.get(delta, "?")
            if word not in row:
                problems.append("管理员那一侧要写「%s」（ADMIN_TOOLS 有 %d 个），"
                                "实际是：%s" % (word, delta, row))
        self.assertEqual([], problems,
                         "使用手册.md 的权限档表与代码不一致：\n  - "
                         + "\n  - ".join(problems))

    def test_project_doc_table_matches_the_code(self):
        """`项目说明.md` 的「含哪些工具」列。"""
        problems = []
        for policy_id, label, operator, delta in self._counts():
            prefix = "| **%s**%s |" % (label, "（默认）" if policy_id == "full" else "")
            row = self._row(self.project, prefix)
            if row is None:
                problems.append("项目说明.md 里没有这一档：%s" % label)
                continue
            if policy_id != "full":
                if not row.endswith("| %d 项 |" % operator):
                    problems.append("%s 那一行写的不是 %d：%s" % (label, operator, row))
                continue
            admin = operator + delta
            text = "%d 项（管理员 %d 项） |" % (operator, admin)
            if text not in row:
                problems.append("完全作业那一行要含 %s，实际是：%s" % (text, row))
            word = "后%s者另需管理员" % self.CN_NUM.get(delta, "?")
            if word not in row:
                problems.append("要写「%s」（ADMIN_TOOLS 有 %d 个），实际是：%s"
                                % (word, delta, row))
        self.assertEqual([], problems,
                         "项目说明.md 的权限档表与代码不一致：\n  - "
                         + "\n  - ".join(problems))

    def test_the_delta_paragraphs_name_every_admin_tool(self):
        """差额那一段得点名是哪几个工具。只写「另需管理员」读者查不到是哪些。"""
        admin_tools = sorted(self.policy.ADMIN_TOOLS)
        self.assertTrue(admin_tools, "ADMIN_TOOLS 是空的，这条用例失去意义")
        problems = []
        for where, text in (("guide.js", self.guide),
                            ("使用手册.md", self.manual),
                            ("项目说明.md", self.project)):
            for name in admin_tools:
                if name not in text:
                    problems.append("%s 里一次都没提到 %s" % (where, name))
        self.assertEqual([], problems,
                         "管理员专属工具没有被点名：\n  - "
                         + "\n  - ".join(problems))




class ChildForbiddenDocTestCase(unittest.TestCase):
    """派活那一段写的「子智能体拿不到哪些工具」也得跟 `subagent.CHILD_FORBIDDEN` 对得上。

    这一条的漂法比权限档那次更隐蔽：原来两处都只点了 `install_plugin` 与
    `clear_finished_jobs` 两个名字，漏掉的 `install_tool` 是三个部署级动作里
    后果最重的一个（它往这台机器上写一个**随后会被 `run_tool` 拉起来**的可执行文件）。
    而任务板的五个写操作（`team_create` / `team_add_member` / `team_add_task` /
    `team_update_task` / `team_done`）一处都没提——使用者从文档里根本读不到
    「兄弟循环不能改队长的任务板」这件事。

    钉的是**点名**而不是措辞：只要这八个名字出现在「不能再派活」那一条里就行。
    不递归那三个（`delegate` / `team_run` / `agent_loop`）不逐名要求，
    因为它们被「不递归」这句话整体覆盖，逐个列出来反而让那一行读不下去；但
    「不递归」这四个字必须在，否则那一句就落空了。
    """

    MARKER = "不能再派活"

    @classmethod
    def setUpClass(cls):
        from pentestd import subagent
        cls.subagent = subagent

    def _named(self):
        """文档里必须逐一点名的那些工具：部署级动作 + 任务板的写操作。"""
        from pentestd import policy
        return sorted((set(policy.ADMIN_TOOLS) | {
            "team_create", "team_add_member", "team_add_task",
            "team_update_task", "team_done",
        }) & set(self.subagent.CHILD_FORBIDDEN))

    @staticmethod
    def _bullet(text, marker, extra):
        lines = text.splitlines()
        for index, line in enumerate(lines):
            if marker in line:
                return "".join(lines[index:index + extra + 1])
        return ""

    def test_the_bullet_names_every_tool_it_should(self):
        wanted = self._named()
        self.assertTrue(wanted, "一个都没取到，这条用例失去意义")
        problems = []
        for where, text, extra in (("web/guide.js", _read("guide.js"), 0),
                                   ("使用手册.md", self._doc("使用手册.md"), 3)):
            bullet = self._bullet(text, self.MARKER, extra)
            if not bullet:
                problems.append("%s 里找不到「%s」那一条" % (where, self.MARKER))
                continue
            for name in wanted:
                if name not in bullet:
                    problems.append("%s 那一条里没有 %s" % (where, name))
            if "不递归" not in bullet:
                problems.append("%s 那一条里没有「不递归」——delegate / team_run / "
                                "agent_loop 就靠这四个字覆盖" % where)
        self.assertEqual([], problems,
                         "派活那一段与 subagent.CHILD_FORBIDDEN 不一致：\n  - "
                         + "\n  - ".join(problems))

    @staticmethod
    def _doc(name):
        with open(os.path.join(ROOT, name), "r", encoding="utf-8") as handle:
            return handle.read()


class BrandLockupTestCase(unittest.TestCase):
    """字标：一句话、一条黑体、一个左边缘、一条算出来的等宽。

    这一组断言各自对应一种会**静默**发生的错 —— 没有人会报错，只会慢慢变得难看：

    * **图标长回来** —— 谁都不会有意见，只是那个方块又占掉 208px 栏宽里最贵的一段；
    * **两处文案分叉** —— 登录页说一个名字、左侧栏说另一个，而两处都"看起来正常"；
    * **标签页与页内不一致** —— 浏览器标签是别人最先看到的那一处；
    * **字面掉出黑体** —— 链条末位写错一个字（`serif` 而不是 `sans-serif`），落进的
      兜底是**宋体**：不掉字、不报错、不少一行，只是笔画上多了衬线；
    * **两行不再等宽** —— 改一次字距、或把英文字号改回绝对 px 就够了。症状是
      "登录门齐、左侧栏不齐"，而两处**分开看都很正常**。

    最后两条是换成黑体这一版才加进来的。上一版守的是另外两条（书法字体的单字重、
    以及"两行向中轴对齐"），它们随字面一起作废 —— 作废的理由写在 `style.css` 那一
    段里，这里只钉结果。

    还有一条是给设计系统守的：`视觉评审.md` 把"字重只有 400 / 600 / 700 三档"记成
    了一条观察结论，而那正是这个界面看起来整齐的原因之一 —— 字标是最容易被"再粗一点"
    的地方，所以把这条规则钉在它身上。

    **几何那部分不在这里**，在 `WordmarkGeometryTestCase`：静态断言钉不住"左边缘差
    2.6px"，而这一版最容易犯的错恰好都是那一种（横向负外边距、行末字距空档、两处
    比例不一致 —— 三个都只在渲染结果里看得见）。
    """

    ALLOWED_WEIGHTS = ("400", "600", "700")

    @staticmethod
    def _rules(css):
        """剥掉注释之后的 CSS。

        **每一次查属性都要先过它** —— 这个类里已经栽过三次：注释里写着"改版前这里
        是 `.brand-mark`"、"不要写 `-webkit-text-stroke`"，而不剥注释的断言会命中
        那句说明。两个方向都会出错，而且**方向相反**：

        * `assertNotIn` —— 假红：注释写得越清楚，测试越红，修法会退化成删注释；
        * `assertIn` —— **假绿**：注释里提过一句 `justify-content: center` 就能让
          "字标被居中"这条断言永远通过，哪怕规则早被删了。

        假绿比假红坏得多：没有人会去修一个永远通过的断言。
        """
        return re.sub(r"/\*.*?\*/", "", css, flags=re.S)

    def setUp(self):
        with open(os.path.join(ROOT, "web", "index.html"),
                  encoding="utf-8") as handle:
            self.html = handle.read()
        with open(os.path.join(ROOT, "web", "style.css"),
                  encoding="utf-8") as handle:
            self.css = handle.read()
        self.plain = self._rules(self.css)

    def test_the_icon_is_gone_and_no_class_survives_it(self):
        # **先剥注释再看规则**：style.css 里那段注释本身就在讲"改版前这里是
        # `.brand-mark`"，不剥的话这条断言命中的是那句说明 —— 它会变成"注释写得越
        # 清楚、测试越红"，而修法会退化成把注释删掉。（同一坑这个文件里已经踩过
        # 一次，见 `SubagentNoticeTestCase` 里那次 `re.sub(r"/\*.*?\*/", ...)`。）
        rules = self._rules(self.css)
        self.assertEqual([], re.findall(r"\.brand-mark\b", rules),
                         "CSS 里还留着 .brand-mark 的规则：一个永不命中的选择器")
        self.assertEqual([], re.findall(r'class="brand-mark"', self.html),
                         "index.html 里又出现了图标（class=\"brand-mark\"）")

    def test_both_brand_spots_say_exactly_the_same_thing(self):
        cn = re.findall(r'class="brand-cn"[^>]*>([^<]+)<', self.html)
        en = re.findall(r'class="brand-en"[^>]*>([^<]+)<', self.html)
        self.assertEqual(len(cn), 2, "字标应当只有两处（登录门 + 左侧栏）")
        self.assertEqual(len(en), 2)
        self.assertEqual(len(set(cn)), 1, "两处中文名不一致：%s" % cn)
        self.assertEqual(len(set(en)), 1, "两处英文名不一致：%s" % en)
        self.assertEqual(cn[0], "AI 渗透测试平台")

    def test_the_tab_title_matches_the_wordmark(self):
        title = re.search(r"<title>([^<]+)</title>", self.html).group(1)
        cn = re.findall(r'class="brand-cn"[^>]*>([^<]+)<', self.html)[0]
        self.assertEqual(title, cn,
                         "浏览器标签与页内字标是同一个名字才不会被当成两个东西")

    def test_the_wordmark_stays_inside_the_type_scale(self):
        cn = re.search(r"\.brand-cn \{([^}]*)\}", self.css).group(1)
        weight = re.search(r"font-weight:\s*(\d+)", cn).group(1)
        self.assertIn(weight, self.ALLOWED_WEIGHTS,
                      "字标用了第 4 种字重（%s）—— 这个界面只有 400 / 600 / 700 三档"
                      % weight)
        # 而且必须是 700。上一版这一行钉的是 400，理由是那些书法字体**只有一个字重**，
        # 请求加粗会走浏览器合成加粗（把字形向外抹一圈），而毛笔字抹一圈就糊成一块墨。
        # 换成黑体之后这条正好反过来：思源 / 微软雅黑 / 苹方都有**真的 Bold**，700 拿到
        # 的是设计过的笔画；而黑体配 400 是单薄的（"字标的重"本来就该落在这一行上）。
        # 所以这一条不是"随便挑一档"，两个方向都渲染出来看过。
        self.assertEqual(weight, "700",
                         "黑体配 400 是单薄的；而这里的黑体都有真 Bold，700 不走合成加粗")
        # 英文那行在源码里保持自然大小写，大写是**样式**决定的：源码可读，样式可改。
        en = re.search(r"\.brand-en \{([^}]*)\}", self.css).group(1)
        self.assertIn("text-transform: uppercase", en)

    def test_the_block_and_both_lines_are_centered(self):
        """字标**整块居中**：块在容器里居中、块里两行各自居中、短横也居中。

        这条是使用者最后定的形态（"AI渗透测试平台整体改为居中"）。它叠在前面的
        等宽结论上，而不是替换它：先由 ④ 把两行算成**等宽**，再让两个等宽的盒子
        居中 —— 左右边缘于是各重合一次，"对齐"与"居中"同时成立。所以这里断三组：

        * 两个容器各居中（块的位置）；
        * `.brand-lockup` 的 `align-items` 与 `text-align` 都是 center（块内两行）；
        * `::after` 没有任何横向偏移（短横在这个 flex 列里跟着居中；一旦有人给它
          `margin-left` 或 `translateX`，它会单独跑到一边，而那在渲染上只差十几像素，
          看着像"本来就该偏一点"）。
        """
        rail = re.search(r"\.rail-brand \{([^}]*)\}", self.plain).group(1)
        self.assertIn("justify-content: center", rail,
                      "左侧栏字标没有在栏里居中（用户明确提过这一条）")
        auth = re.search(r"\.auth-brand \{([^}]*)\}", self.plain).group(1)
        self.assertIn("justify-content: center", auth,
                      "登录门那处字标没有跟着居中：两处不同构，改一处就会分叉")
        lockup = re.search(r"\.brand-lockup \{([^}]*)\}", self.plain).group(1)
        self.assertIn("align-items: center", lockup,
                      "两行没有居中：等宽之后居中才会让左右边缘同时重合")
        self.assertIn("text-align: center", lockup,
                      "文字对齐没跟着改：align-items 只管盒子的边，"
                      "text-align 管的是行内文字")
        after = re.search(r"\.brand-lockup::after \{([^}]*)\}", self.plain).group(1)
        for offset in ("margin-left", "margin-right", "translateX", "align-self"):
            self.assertNotIn(offset, after,
                             "字标下面那道短横被单独偏移（%s）：它在居中列里本来就"
                             "跟着居中，再加偏移只会偏到一边" % offset)

    def test_the_chinese_line_carries_no_horizontal_margin(self):
        """中文那行**不许有横向外边距**。

        它原来带着 `padding: .09em .11em` + `margin: -.09em -.11em`（给描边出血留位）。
        那一对在布局上是**净零**，所以居中排时完全无害 —— 正因如此它才危险：改成左对齐
        之后，那个 −.11em 的左外边距会把中文那行**单独**往左拽 0.11em（24px 下 2.6px），
        两行的左边缘差着一个肉眼可见的量，而"中文那行有个负外边距"这件事在渲染结果里
        看不出来，只能量。纵向那一对留着（它仍然是净零，且与对齐无关）。
        """
        cn = re.search(r"\.brand-cn \{([^}]*)\}", self.plain).group(1)
        parts = re.search(r"margin:\s*([^;]+);", cn).group(1).split()
        left = parts[3] if len(parts) == 4 else parts[1] if len(parts) == 2 else parts[0]
        self.assertEqual("0", left,
                         "中文那行有横向外边距（%s）：负的把它单独往左拽，正的把英文"
                         "甩在后面 —— 两个方向都破坏左对齐" % left)

    def test_the_display_face_falls_back_to_sans_serif_not_serif(self):
        """黑体链条的末位必须是 `sans-serif`，**不能是 `serif`**。

        上一版这里写的是 `serif`，而那时它不算错：书法字体落进 serif 兜底，至少还是
        "更像正文"的那一类。换成黑体之后同一个字是**反的** —— 中文的 serif 兜底在多数
        机器上是宋体，于是"要求黑体、拿到宋体"。它不掉字、不报错、不少一行，只是每个
        笔画上多了衬线。

        判据必须按**逗号分段**看，不能写 `family.index("serif")`：`sans-serif` 里面就
        含 `serif` 五个字母，那种写法会把**恰好正确**的那一份判成错的。
        """
        cn = re.search(r"\.brand-cn \{([^}]*)\}", self.plain).group(1)
        family = re.search(r"font-family:\s*([^;]+);", cn).group(1)
        names = [part.strip().strip('"') for part in family.split(",")]
        self.assertIn("var(--sans)", names,
                      "黑体链条没有落回 --sans：缺字体时会掉到不可控的兜底字体上")
        self.assertEqual("sans-serif", names[-1],
                         "末位不是 sans-serif（是 %r）—— 黑体落进 serif 兜底是宋体"
                         % names[-1])
        self.assertNotIn("serif", names,
                         "链条里出现了裸的 serif：它是宋体，与黑体正好相反")
        for face in ("Microsoft YaHei", "PingFang SC"):
            self.assertLess(names.index(face), names.index("var(--sans)"),
                            "%s 排在 --sans 后面：那一档永远命中不到它" % face)

    def test_the_fill_is_a_gloss_not_a_two_stop_ramp(self):
        """底色是**主色实色**，亮带只能靠**白 alpha**。

        这一版把上一版的多色停换掉了，起因是渲染出来的一句反馈：「整体发黑」。根因
        不是"颜色选深了"，是**拿错了令牌**：`--text` / `--text-accent` / `--accent-dim`
        这三者在**不同主题族里极性相反**（浅色主题里 `--text-accent` 是深海军蓝
        #0c306a，深色主题里是淡蓝 #cfe0ff），所以一条"深→更深→亮"的渐变在浅色主题里
        成立、在深色主题里整个反了。白 alpha 是唯一**不翻极性**的调制：往白里混，
        在任何主题里都只会更亮。

        判据因此有三条：底色取 `--accent`；亮带至少两道（≥18%）；亮带之间要有 0% 的
        间隔 —— 间隔没了就不是"带"，是整片泛白（那会让字在浅色主题里糊掉）。
        """
        block = self._supports_block()
        self.assertIn("background-color: var(--accent)", block,
                      "底色要取主色令牌 —— 它是每族主题里唯一的「这一族的主色」")
        bands = [int(v) for v in re.findall(r"rgb\(255 255 255 / (\d+)%\)", block)]
        self.assertGreaterEqual(len([b for b in bands if b >= 18]), 2,
                                "亮带少于两道：看不出金属感，只剩一块平色：%s" % bands)
        self.assertIn(0, bands, "亮带之间没有 0% 的间隔 —— 那是整片泛白，不是亮带")

    def test_the_outline_is_gone_and_only_the_glow_remains(self):
        """这一版**没有描边**，只留光晕 —— 方向与上一版正相反，所以理由要写清楚。

        上一版有一条 `-webkit-text-stroke: .03em` + `color-mix`，叫"收边"，是给书法
        字体那些细而对比剧烈的笔画定边的。配黑体的效果是**浮雕**：`text-stroke` 骑在
        字形轮廓上画（一半盖住填充、一半伸到外面），笔画等宽之后每个笔画都得到一圈比
        填充更深或更浅的边，笔画密的字（渗 / 透 / 测 / 试）内白被那圈边吃掉，八个字
        叠起来读作"描边字"。

        这条判据钉的是**去掉**，因为它是**对照出来**才定下的：
        `.scratch/t51/try_variants.py` 把每一层分别去掉、一次出齐五张 —— 只去描边那张
        明显干净，**只去光晕那张反而更硬**（那圈深边失去了外面对称的浅边，读起来更像
        硬描边）。所以正面是"光晕在、描边不在"；反面有两个，都是看着不对但不会报错的：
        都留着（浮雕）、都去掉（像没做完的平色）。

        注意 `assertNotIn` 查的是 `self.plain`（**剥过注释**的）：这条判据的说明里就
        写着 `text-stroke` 这个词，不剥注释的话断言命中的是它自己的说明。
        """
        self.assertNotIn("text-stroke", self.plain,
                         "描边回来了：配黑体是浮雕效果，笔画密的字会糊成一团")
        block = self._supports_block()
        self.assertIn("background-clip: text", block,
                      "渐变填充没了：那时剩下的光晕会变成围着一块平色的影子")
        self.assertIn("filter: drop-shadow", block,
                      "光晕要用 filter —— text-shadow 会盖住渐变")
        self.assertNotIn("text-shadow", block,
                         "渐变之上又出现 text-shadow：它会把填充盖成实心色")

    def test_the_english_size_is_a_ratio_of_the_chinese_one(self):
        """英文那行的字号**必须是中文的一个比例**，不能是另一个绝对字号。

        这不是"少写一个数"的省事。两行等宽那个约束（见 style.css ④）只在两处字标
        **共用同一个比例**时才有唯一解：登录门 24px、左侧栏 18px，上一版英文各自写的
        是绝对字号（8.5px 与 7.5px），两处比例分别是 .354 与 .417 —— 一个
        `letter-spacing` 满足不了两个方程，**总有一处差几像素**。症状是"登录门齐、
        左侧栏不齐"，而两处分开看都很正常，很难归因到"比例"这两个字上。
        """
        en = re.search(r"\.brand-en \{([^}]*)\}", self.plain).group(1)
        size = re.search(r"font-size:\s*([^;]+);", en).group(1)
        self.assertIn("var(--brand-size)", size,
                      "英文那行的字号没有挂在 --brand-size 上：两处的比例会各自漂")
        self.assertNotRegex(size, r"\d(?:\.\d+)?px",
                            "英文那行的字号是写死的 px（%s）：两处比例会不一样" % size)
        lockup = re.search(r"\.brand-lockup \{([^}]*)\}", self.plain).group(1)
        self.assertIn("--brand-size: 24px", lockup,
                      "中文那行的字号不在 --brand-size 上：英文的比例就没有基准")

    def test_the_rail_changes_only_the_size(self):
        """`.rail-brand` 那一档**只准改 `--brand-size`**。

        它一旦再覆盖中文的字距、或英文的字号/字距，就等于换了那个等式的解 ——
        "一处齐一处不齐"会毫无察觉地回来。上一版那里覆盖了三条（中文 `font-size` +
        `letter-spacing`，英文 `font-size` + `letter-spacing` + `margin-right`），
        而那三条正是偏差的来源：字距进 V_cn，改它就是改 K。
        """
        blocks = re.findall(r"\.rail-brand[^{]*\{([^}]*)\}", self.plain)
        self.assertTrue(blocks, "没找到 .rail-brand 的规则")
        joined = " ".join(blocks)
        self.assertIn("--brand-size: 18px", joined,
                      "左侧栏没有把字号降一档：它会和登录门一样大")
        for forbidden in ("font-size", "letter-spacing", "font-weight"):
            self.assertNotIn(forbidden, joined,
                             ".rail-brand 里覆盖了 %s —— 那等于换了等宽那个等式的解"
                             % forbidden)

    def test_the_tracking_and_the_gap_it_leaves_move_together(self):
        """字距，以及它造成的那截行末空档，必须**同源**。

        `letter-spacing` 会在最后一个字母右边留出一个空档，而那一截**算进行宽**：留着
        它，布局盒就比画出来的字宽出一截，"块在栏里居中"于是偏左一点点 —— 而"一点点"
        没有人能归因到"行末有个空档"上。所以要用等量的负右外边距收回来；要紧的是
        **等量**，所以两者读同一个变量，谁改都不会只改一半。

        另外字距必须是 `em`：写成 `px` 就不随字号缩放，而两处字标走的是同一条规则 ——
        登录门与左侧栏只有一个能对齐。
        """
        en = re.search(r"\.brand-en \{([^}]*)\}", self.plain).group(1)
        track = re.search(r"--brand-en-track:\s*([\d.]+)(\w+)", en)
        self.assertIsNotNone(track, "字距没有走变量：它与行末那个空档会各改各的")
        self.assertEqual("em", track.group(2),
                         "字距用了 %s：它不随字号缩放，两处字标只有一个能对齐"
                         % track.group(2))
        self.assertIn("letter-spacing: var(--brand-en-track)", en)
        self.assertIn("margin-right: calc(-1 * var(--brand-en-track))", en,
                      "行末那个空档没有收回来：块会偏左一点点，而它没法归因")

    def _supports_block(self):
        """`@supports (background-clip: text)` 那一块的正文（剥过注释）。

        单独抽出来是因为**有两条断言都读它**（填充、以及描边在不在）。写在每一处的
        话，「块到哪里结束」这件事会被抄两遍，而它的边界恰好很容易抄错：块里有嵌套的
        `{}`，按第一个 `}` 切会在改动之后静默地少读半块。
        """
        start = self.plain.index("@supports ((-webkit-background-clip: text)")
        block = self.plain[start:]
        return block[:block.index("\n}\n") + 3]


class WordmarkGeometryTestCase(unittest.TestCase):
    """字标两行的几何，在**真浏览器**里量出来。

    静态守卫钉得住"样式表里写着 `align-items: flex-start`"，钉不住"量出来左边缘差
    2.6px"。而这一版最容易犯的错恰好全是后者，它们在样式表里每一行都"看着对"：

    * 中文那行的横向负外边距（净零，居中时无害，左对齐时把这一行单独拽走）；
    * 行末那个字距空档（布局盒比画出来的字宽，居中时偏左一点点）；
    * 两处字标的比例不一致（一个 `letter-spacing` 满足不了两个方程）。

    所以判据只有一条：**两行的左边缘、右边缘、宽度在同一个位置上重合**。

    两个容差不同，因为来源不同：

    * **左边缘**：与字体无关（没有左边距、左侧也没有字距），所以给 1px；
    * **宽度**：两行都取决于**本机装的是哪个字面** —— CJK 的 advance 固定 1em，但
      "AI" 那两个字母与整行英文都随字面变。按比例给 3%：它比"字距写错一档"
      （实测 4.8%）小，比"换了一个拉丁字面"（1~2%）大。

    没有 Edge / Chrome 的机器上跳过（探针报 `browser: ""`）。**探针连 JSON 都没打出来
    时是失败、不是跳过**：那说明探针自己坏了，跳过会让覆盖静默消失。
    """

    TOLERANCE_LEFT = 1.0
    TOLERANCE_WIDTH = 0.03

    @classmethod
    def setUpClass(cls):
        cls.probe = os.path.join(ROOT, "tools", "probe_wordmark.py")

    def _measure(self, anon):
        self.assertTrue(os.path.exists(self.probe), "字标度量探针不见了：%s" % self.probe)
        args = [sys.executable, self.probe, "--json"]
        if anon:
            args.append("--anon")
        proc = subprocess.run(args, cwd=ROOT, capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=600)
        lines = [line for line in (proc.stdout or "").splitlines() if line.strip()]
        self.assertTrue(lines, "探针没有输出 JSON（stderr：%s）" % (proc.stderr or "")[-400:])
        try:
            report = json.loads(lines[-1])
        except ValueError as error:
            self.fail("探针最后一行的 JSON 解不开（%s）：%s" % (error, lines[-1][:300]))
        if not report.get("browser"):
            self.skipTest("本机没有 Edge / Chrome，跳过字标几何探针")
        self.assertFalse(report.get("fatal") or report.get("parse_error"),
                         "探针自己坏了：%s"
                         % (report.get("fatal") or report.get("parse_error")))
        return report

    def test_both_lines_are_flush_at_the_login_gate(self):
        self._assert_flush(self._measure(anon=True), "auth", "登录门")

    def test_both_lines_are_flush_in_the_rail(self):
        self._assert_flush(self._measure(anon=False), "rail", "左侧栏")

    def _assert_flush(self, report, spot, label):
        data = (report.get("spots") or {}).get(spot)
        self.assertTrue(data and data.get("present"),
                        "%s 那一处字标没有渲染出来（spot=%r）" % (label, data))
        if data.get("englishHidden"):
            self.skipTest("%s 这一档只留中文，没有第二行可比" % label)
        cn, en = data["cn"], data["en"]
        self.assertFalse(cn.get("error") or en.get("error"),
                         "%s 量不到字符矩形：%r / %r" % (label, cn, en))
        problems = []
        if abs(data["leftDelta"]) > self.TOLERANCE_LEFT:
            problems.append("左边缘差 %+.2fpx（上限 %.1f）"
                            % (data["leftDelta"], self.TOLERANCE_LEFT))
        ratio = abs(data["widthDelta"]) / max(1.0, cn["visual"])
        if ratio > self.TOLERANCE_WIDTH:
            problems.append("宽度差 %+.2fpx = %.1f%%（上限 %.0f%%）"
                            % (data["widthDelta"], ratio * 100,
                               self.TOLERANCE_WIDTH * 100))
        self.assertEqual([], problems,
                         "%s 的两行没有对齐：\n  - %s\n  中文 %.2fpx / 英文 %.2fpx，"
                         "字号 %.1f / %.1f，字距 %.3f / %.3f"
                         % (label, "\n  - ".join(problems), cn["visual"],
                            en["visual"], cn["fontSize"], en["fontSize"],
                            cn["letterSpacing"], en["letterSpacing"]))


class ChatDetailRailTestCase(unittest.TestCase):
    """对话页右侧详情栏（任务 / 目标 / 子智能体的展开面）。

    这一组守的同样是**接线**：三种行各自渲染各自的详情，任何一处漏了点击处理，
    症状都是“点上去没反应”——页面上看不出缺什么，单元测试也全是绿的。
    """

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")
        cls._chat = _read("chat.js")
        cls._css = _read("style.css")
        cls._split = _read("split.js")

    # ------------------------------------------------------------- 结构

    def test_the_rail_sits_after_the_message_flow(self):
        """顺序是 消息流 → 拖拽条 → 右栏，且栏头三个部件的 id 都在。

        放到消息流之前就等于“把详情夹在左栏与对话之间”：点一条任务，屏幕中间
        凭空多出一列，而它读的是左边点的那一项——位置与因果颠倒。

        栏头只剩**三件**：类型、标题、收起。原先第四件是正文那一层的 id
        （`.chat-detail-body`），正文取消之后它不再存在——详情展开在被点中那一行
        下面，栏头只说明"现在展开的是哪一条"。这里改成正面断言它**不在**：
        留着那层空 div 没有东西可画，只会在栏底多一块空白。
        """
        main = self._html.index('class="chat-main"')
        rail = self._html.index('class="chat-detail"')
        self.assertLess(main, rail, "右栏跑到消息流前面去了")
        marks = [self._html.index('id="%s"' % one) for one in
                 ("chat-detail-kind", "chat-detail-title", "chat-detail-close")]
        self.assertEqual(marks, sorted(marks),
                         "右栏卡头的顺序必须是 类型 → 标题 → 收起")
        self.assertNotIn('id="chat-detail-body"', self._html,
                         "右栏还留着详情正文那一层：详情现在就地展开，正文层没有"
                         "东西可画")
        self.assertIn('aria-label="详情"', self._html,
                      "右栏没有可读的名字（读屏里它是一块无标题的地）")

    # ------------------------------------------------------- 三种行的接线

    def test_goals_jobs_and_subagents_all_open_the_rail(self):
        """左栏三种行各有一条通往 `openDetail()` 的路，一个都不能少。

        分开断言、各有消息：三处漏一处的表现完全相同（那一类点不开），而合并成
        一条的话报错只告诉你“有一处”，不告诉你是哪一处。
        """
        goal = _fn_body(self._chat, "goalRow")
        self.assertIn("openDetail('goal'", goal,
                      "目标行点开之后没有打开右侧详情")
        job = _fn_body(self._chat, "sideJobRow")
        # 任务行现在不是**直接**调 `openDetail`：先走 `pickJob`（它把范围对齐到那条任务
        # 所属的目标，否则"右边写着乙的任务、左边三块还是甲的东西"）。所以两头都要钉：
        # 行上要有那一跳，`pickJob` 里要真有 `openDetail('job'`——只钉一头的话，
        # 中间那一跳断了（改名、漏调）这条守卫照样绿。
        self.assertIn("pickJob(job)", job,
                      "任务行点开之后没有打开右侧详情")
        self.assertIn("openDetail('job'", _fn_body(self._chat, "pickJob"),
                      "pickJob 没有接到 `openDetail('job'`：那一跳断在中间")
        side = _fn_body(self._chat, "renderSideSubagents")
        self.assertIn("wireSubagentPicks(", side,
                      "上下文栏的子智能体行没有接上“点开详情”")
        main = _fn_body(self._chat, "messageNode")
        self.assertIn("wireSubagentPicks(", main,
                      "主区消息下面那份子智能体记录也要能点开——"
                      "两处是同一份数据，只接一处会让人以为其中一处是死的")
        wire = _fn_body(self._chat, "wireSubagentPicks")
        self.assertIn("openDetail('subagent'", wire)
        # 键盘可达：它仍是 `div`（里面还有一层 chip），所以靠自己补 role/tabindex。
        self.assertIn("setAttribute('role', 'button')", wire,
                      "子智能体行成了鼠标专属：键盘用户按不进去")
        self.assertIn("tabindex", wire)
        self.assertIn("'Enter'", wire)

    def test_dom_collections_are_not_passed_to_asArray(self):
        """DOM 集合不能交给 `asArray`。

        `asArray` 是 `PentestUI.asArray` 的别名，而它的定义是
        `Array.isArray(v) ? v : []`（给接口返回的 JSON 用的）。`HTMLCollection`
        与 `NodeList` 传进去得到的是**空数组**：高亮一条都不画、子智能体行
        一个都不接线，而且不报错——页面看起来“渲染好了”，只是点了没反应。

        真机上撞到过两次（`markDetailPicks` 的 `children`、`wireSubagentPicks`
        的 `querySelectorAll`），静态断言看不见，所以在这里钉住。
        """
        picks = _fn_body(self._chat, "markDetailPicks")
        self.assertNotIn("asArray(dom.", picks,
                         "markDetailPicks 把 DOM 集合交给了 asArray——高亮会静默地一条都不画")
        self.assertIn("Array.from(", picks)
        wire = _fn_body(self._chat, "wireSubagentPicks")
        self.assertNotIn("asArray(block.querySelectorAll", wire,
                         "wireSubagentPicks 把 NodeList 交给了 asArray——「点开详情」会静默失效")
        self.assertIn("Array.from(block.querySelectorAll", wire)

    def test_one_renderer_per_kind(self):
        """每种详情只能有一个渲染函数。

        第二个渲染器的失败形态与当年那份“侧栏简版子智能体”一模一样：两处渲染
        同一份数据、详细程度却不同，对不上时读的人不知道信哪一份。
        """
        for name in ("renderJobDetail", "renderGoalDetail", "renderSubagentDetail"):
            self.assertEqual(self._chat.count("function %s(" % name), 1,
                                            "%s 只能有一份" % name)
        self.assertEqual(self._chat.count("function renderDetail("), 1,
                         "详情栏的重画入口只能有一个")

    # ------------------------------------------------------------- 宽度

    def test_the_rail_has_its_own_width_token(self):
        """宽度经由 split.js 的分栏令牌下发，不是写死的 `width`。

        写死的宽度会与窄屏媒体查询打架：窄屏下详情栏要变成底部一块，写死的
        380px 会把它顶成一个 380px 高的横条。
        """
        self.assertIn("id: 'detail'", self._split)
        self.assertIn("pane: '.chat-detail'", self._split)
        self.assertIn("token: '--split-detail'", self._split)
        self.assertIn("--split-detail:", self._css,
                      "`--split-detail` 没有在 :root 里定义（引用它的声明会整条作废）")
        body = self._body_of(".chat-detail")
        self.assertIn("var(--split-detail", body,
                      "详情栏没有读它自己的宽度变量——拖那条线改的是别的栏")

    def test_closed_rail_leaves_the_layout_instead_of_shrinking_to_zero(self):
        """收起时整块 `display: none`，而不是宽度归零。

        归零的栏会留下边框与那一份 `gap`，看起来像一条“忘了画内容的竖线”；
        而它还会继续占着分栏器的命中区。
        """
        self.assertIn(".chat-detail:not(.is-open) { display: none; }", self._css)
        self.assertIn(".chat-detail-close", self._css)
        # Esc 与 × 都只“收起”、不清选中：收起是“先不看”，重新拉开应该还是刚才那条。
        self.assertIn("if (view.detailOpen) setDetailOpen(false);", self._chat)

    def test_the_rail_opens_once_by_default_on_wide_screens(self):
        """宽屏默认拉开一次，窄屏不自动拉开。

        收起状态下这一栏整块不可见，而“能在右边看详情”这件事只存在于那行空态
        提示里——功能在、但看不见，与“没做”对用户是同一件事（这个页面已经因为
        同一类原因被投诉过一次）。门槛是 1200px 而不是 900px：左栏 296 + 详情栏
        380 两条固定宽度，窄于 1200 时消息流只剩 300 多像素，一进页面就三栏挤着。
        """
        loaded = _fn_body(self._chat, "ensureLoaded")
        self.assertIn("detailAutoOpened", loaded,
                      "默认拉开没有做“只做一次”，切页签回来会反复弹开")
        self.assertIn("matchMedia('(max-width: 1199px)')", loaded,
                      "默认拉开没有区分窄屏：窄于 1200 时三栏会把消息流挤到只剩一条")
        self.assertIn("setDetailOpen(true)", loaded)

    def test_narrow_screen_hides_its_handle_and_caps_its_height(self):
        """≤900px：手柄隐藏、详情栏封顶 40vh。

        竖线不隐藏的话它仍然能拖，而“拖一个已经变成横条的栏”改的是它此刻毫无
        意义的那个尺寸，回到宽屏才发现宽度被改过了。
        """
        self.assertIn('.split-handle[data-split="detail"] { display: none; }',
                      self._css)
        self.assertIn(".chat-detail { flex: 0 0 auto; max-height: 40vh; }", self._css)

    # --------------------------------------------------- 就地展开的那一块

    def test_detail_lands_next_to_its_row_not_in_a_shared_body(self):
        """展开面必须插在**被点中那一行后面**，而不是另起一片共用的正文区。

        这是 t110 的全部要点（使用者原话：「右侧边栏取消详情内容，把功能放到任务
        和子智能体中，详细内容在这两个框中直接显示」）。反过来写——插到容器末尾、
        或插进 `host` 而不是行的父节点——在列表只有一条时看起来完全正确，只有
        列表里有两行以上才露出来，而那一刻的错误是"点第二行，详情长在第一行下面"
        或者"详情飘到整个子智能体块外面"。所以三条一起钉：函数存在、用
        `insertBefore(panel, row.nextSibling)`、找不到行时才退到 `host.append`。
        """
        mount = _fn_body(self._chat, "mountInline")
        self.assertIn("row.parentNode.insertBefore(panel, row.nextSibling)", mount,
                      "展开面没有插在被点中那一行后面：列表里超过一条时就会错位")
        self.assertIn("host.append(panel)", mount,
                      "找不到行时没有退路——被选的条目刚好被重画掉时会静默不画")
        self.assertIn("chat-detail-inline", mount,
                      "展开面没有自己的类名，样式与后续清理都无从下手")
        self.assertEqual(self._chat.count("function mountInline("), 1)

    def test_the_three_kinds_hang_off_their_own_containers(self):
        """三种详情各自的落点：目标 → 目标块、任务 → 任务块、子智能体 → 子智能体块。

        映射写错的失败形态很隐蔽：`openDetail("job", ...)` 照样设好了状态、栏头也
        写了任务名，只是展开面长在了别的块里——读的人会以为"详情跑到别处去了"。
        """
        host = _fn_body(self._chat, "detailHostOf")
        for kind, target in (("goal", "dom.goals"), ("job", "dom.sideJobs"),
                             ("subagent", "dom.sideSubagents")):
            self.assertIn(target, host,
                          "%s 的展开面没有落到它自己那一块（%s）" % (kind, target))

    def test_only_one_panel_is_open_across_the_whole_page(self):
        """同一时刻只能有一块展开面，且清除是**整页**扫不是只清当前块。

        只清当前块的话，"从任务切到目标"会把任务那块旧的展开面留在原处——两片
        详情同时摊着，而两片都写着"现在展开的是我"。
        """
        clear = _fn_body(self._chat, "clearInline")
        self.assertIn("document.querySelectorAll", clear,
                      "clearInline 没有整页扫：换块之后旧的展开面会留下")
        self.assertIn(".chat-detail-inline", clear)
        render = _fn_body(self._chat, "renderDetail")
        self.assertIn("clearInline()", render,
                      "renderDetail 没有先清旧的展开面，重复点会叠起来")

    def test_every_renderer_takes_a_host(self):
        """三个渲染器与执行过程都收一个 `host`，且都挡住空 host。

        它们早先无参、直接写进右栏那片正文；正文拿掉之后，宿主成了唯一出口。
        漏掉 `if (!host) return;` 的那一个会在 host 为空时抛异常，把整次重画打断，
        而那一次的调用点通常正在别的块里——错误现场与原因隔着一层。
        """
        for name in ("renderJobDetail", "renderGoalDetail", "renderSubagentDetail"):
            body = _fn_body(self._chat, name)
            self.assertIn("host", body, "%s 没有接宿主" % name)
        self.assertIn("function startDetailLog(job, finished, host)", self._chat,
                      "执行过程没有接宿主，会无处可画")
        self.assertNotIn("dom.detailBody", self._chat,
                         "还留着写右栏正文的旧出口：正文层已经不存在了")

    def test_the_inline_panel_is_its_own_card(self):
        """展开面自己是一张卡（底 + 边 + 圆角），且与上面的行留白分开。

        没有底与边的话，它与行挤在同一平面里，读不出"这一段是那一行的展开"。
        左边那道 2px 的强调线是它与普通卡片唯一的区别——没有它，一块展开面与
        另一块卡片长得一模一样，扫一眼分不清哪块是"点出来的"。
        """
        body = self._body_of(".chat-detail-inline")
        self.assertIn("background:", body)
        self.assertIn("border:", body)
        self.assertIn("border-left:", body)
        self.assertIn("border-radius:", body)
        # 只在**剥掉注释之后**查旧选择器：样式表里有一句注释专门解释"以前每一段
        # 是独立一张卡（旧正文层那一层）"，那是留给后人的来历，不是活的规则。
        # 直接对原文查会把它当成残留——判红的是注释，不是样式。
        bare = re.sub(r"/\*.*?\*/", "", self._css, flags=re.S)
        self.assertNotIn(".chat-detail-body", bare,
                         "旧正文层的规则还留着：它已经没有任何元素可命中")

    def test_job_log_is_pulled_incrementally(self):
        """任务执行过程用 `/jobs/{id}/events` 增量拉，不复用任务页签那套面板。

        详情栏与任务页签是两个页面：复用面板会把 `state.jobLog` 这份“当前跟随
        哪条任务”的状态变成两处共写。而两条 SSE 也不该由这一栏再开一条。
        """
        self.assertIn("/events", self._chat)
        self.assertIn("after=", self._chat)
        self.assertIn("limit=", self._chat)
        self.assertNotIn("jobLogPanel", self._chat,
                         "详情栏又去借任务页签的面板了——那是另一个页面的状态")

    # ------------------------------------------------------------- 小工具

    def _body_of(self, selector):
        match = re.search(re.escape(selector) + r"\s*\{([^{}]*)\}", self._css)
        self.assertIsNotNone(match, "找不到规则：%s" % selector)
        return match.group(1)


class RailGroupLabelTestCase(unittest.TestCase):
    """段标签是**分类**，不是条目：要比条目更像标题，还要有一条条目没有的记号。

    使用者原话是「工作 / 产出 / 系统三个标签字号太小，另外和下面的分项做个区别」。
    这两句都能钉住：字号是 CSS 里的一个声明，而「区别」是 字号 / 字重 / 颜色 /
    装饰 / 与首条目的间距 这五项里至少几项不能与 `.tab` 重合。

    为什么值得钉：段标签只有两条规则，回退成一个数字（10px）不会让任何功能坏掉，
    页面照样能点——所以除了这里，没有别的东西会红。
    """

    @classmethod
    def setUpClass(cls):
        cls._css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)

    def _rule(self, selector):
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", self._css):
            if " ".join(sel.split()) == selector:
                return " ".join(body.split())
        return ""

    def _decl(self, rule, prop):
        match = re.search(r"(?:^|;)\s*%s:\s*([^;]+)" % re.escape(prop), rule)
        return match.group(1).strip() if match else ""

    def test_the_label_sits_on_the_small_type_step(self):
        """字号落在 `--fs-small`（12px）这一档，不再回到 10px。

        `.tab` 用 `--fs-body`（13px）。两者贴得太近时，「这是标题 / 这是条目」就只能
        靠颜色分辨，而颜色恰好是那 10px 版本里最弱的一项（`--muted` 比条目还淡）。
        """
        rule = self._rule(".rail-group-label")
        self.assertTrue(rule, "style.css 里找不到 `.rail-group-label` 规则")
        size = self._decl(rule, "font-size")
        self.assertIn("var(--fs-small)", size,
                      "段标签应落在 `--fs-small` 这一档（条目用 --fs-body）；现在是：%s" % size)
        self.assertRegex(self._css, r"--fs-small:\s*12px",
                         "`--fs-small` 被改小了——段标签会跟着缩回去")

    def test_the_label_outweighs_the_tabs(self):
        """字重 ≥ 700，且颜色不能退回 `--muted`。

        `.tab` 常态 400、选中 600。段标签压到 600 就与「当前这一项」同重了——
        分类不该和「你正站在哪」争同一个视觉权重。
        """
        rule = self._rule(".rail-group-label")
        weight = self._decl(rule, "font-weight")
        self.assertTrue(weight, "段标签没有自己的字重，会继承 400，读起来仍是条目")
        self.assertGreaterEqual(int(weight), 700, "段标签字重 %s 太轻" % weight)
        color = self._decl(rule, "color")
        self.assertIn("var(--text-dim)", color,
                      "段标签颜色：%s（回到 --muted 就又是「比条目更淡的小字」了）" % color)

    def test_the_label_no_longer_draws_a_trailing_rule(self):
        """段标签右侧那条延伸线**已按使用者要求删掉**（原话：「把工作、产出、系统
        后面的线去掉」）。

        这条用例原来断言的是"线在、1px、走 `--border-strong`"。删线时必须一起改，
        否则下一个改样式的人会被迫把线加回来 —— 而"加回来"在渲染上几乎看不出来
        （1px、低对比），只有用例会红，很容易被当成"用例太旧"而顺手改回去。

        删掉之后层级只剩字号 / 字重 / 颜色 / 与首条目的间距：前两样由本类的另外两条
        用例钉着，间距由下一条钉着。这里钉的是"线真的没了"——包括标签本体也不该自己
        画底色或边框（那是同一件事换了一层实现）。
        """
        self.assertEqual(self._rule(".rail-group-label::after"), "",
                         "段标签又长出了右侧延伸线 —— 使用者要的是没有这条线")
        rule = self._rule(".rail-group-label")
        self.assertNotIn("background", rule,
                         "段标签本体画了底色/边线：那还是「一条线」，只是换了层")
        self.assertNotIn("border", rule,
                         "段标签本体画了边框：那还是「一条线」，只是换了层")

    def test_the_label_keeps_a_paragraph_gap_from_its_first_item(self):
        """与首条目的间距 ≥ 10px（8px 内边距 + 段内 2px gap）。

        改前是 6px：标题与正文贴在一起时，延伸线就只是字后面的一条装饰，
        「下面几项属于上面这个词」这层关系反而没画出来。
        """
        rule = self._rule(".rail-group-label")
        padding = self._decl(rule, "padding")
        self.assertTrue(padding, "段标签没有内边距")
        parts = padding.split()
        # CSS 简写：1 值=四周，2 值=上下/左右，3 值=上/左右/下，4 值=上/右/下/左。
        bottom = parts[0] if len(parts) <= 2 else (parts[2] if len(parts) == 3 else parts[3])
        match = re.match(r"(\d+(?:\.\d+)?)px$", bottom)
        self.assertTrue(match, "读不出段标签的下内边距：%s" % padding)
        self.assertGreaterEqual(float(match.group(1)), 8,
                                "下内边距只有 %s，标题又和正文贴回去了" % bottom)
        group = self._rule(".rail-group")
        self.assertEqual(self._decl(group, "gap"), "2px",
                         "段内 gap 变了：与首条目的总间距 = 下内边距 + 这个 gap")

    def test_the_line_is_not_repeated_on_the_items(self):
        """条目也长延伸线的话，那条线就不再是「分类」的记号了。"""
        self.assertEqual(self._rule(".tab::after"), "",
                         "`.tab` 上出现了 ::after——层级的记号被复制到条目上就失效了")


class RailTabIconTestCase(unittest.TestCase):
    """九个导航条目**各带一枚内联 SVG 图标**，且不依赖任何外部资源。

    使用者原话：「给每个选项增加图标」。这条需求最容易的两种错法都不是"忘了加"：

    * 只加了 `<svg>` 没加尺寸样式 —— SVG 作为替换元素在没有宽高时取 **300×150**，
      一栏图标会直接撑爆导航（真发生过：只改 HTML 没改样式，截图里是一排黑块）；
    * 用图标字体或 CDN —— 这套界面要跑在断网的内网里，字体取不到就是九个方框，
      而那时页面不报错、控制台也没有红字。

    所以这里分三组断：HTML 侧（每项一枚、`aria-hidden`、无外链）、CSS 侧（尺寸 /
    `fill` / `currentColor` 描边 / 不被压扁）、以及图标与文字之间的间距。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = _read("index.html")
        cls._css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)

    def _rule(self, selector):
        for sel, body in re.findall(r"([^{}]+)\{([^}]*)\}", self._css):
            if " ".join(sel.split()) == selector:
                return " ".join(body.split())
        return ""

    def _decl(self, rule, prop):
        match = re.search(r"(?:^|;)\s*%s:\s*([^;]+)" % re.escape(prop), rule)
        return match.group(1).strip() if match else ""

    # 导航条目数。**改这个数就是改导航**：两个 9 被钉在这里，是为了让"加了一页
    # 却忘了给它画图标"和"删了一页却留着图标"都变成红灯。2026-11 按 artex 补六页
    # 之后是 15（功能 11 + 系统 4），`test_all_views_present` 也列全了这 15 项；
    # 再加上「任务」「计划任务」两页（最初是「渗透任务」）就是现在 rail 上的 17 枚。
    TAB_COUNT = 17

    def test_every_rail_tab_carries_exactly_one_inline_icon(self):
        self.assertEqual(len(re.findall(r'<svg class="tab-ico"', self.html)),
                         self.TAB_COUNT, "导航条目不是每项一枚图标")
        tabs = re.findall(
            r'<button class="tab[^"]*"[^>]*data-tab="([a-z]+)"[^>]*>(.*?)</button>',
            self.html, flags=re.S)
        self.assertEqual(len(tabs), self.TAB_COUNT,
                         "页签按钮数量变了：%d" % len(tabs))
        for name, inner in tabs:
            self.assertEqual(inner.count("<svg"), 1,
                             "「%s」没有（或不止）一枚图标" % name)
            self.assertIn("</svg>", inner, "「%s」的图标没有闭合" % name)
            self.assertLess(inner.index("<svg"), inner.index("</svg>"),
                            "「%s」的图标结构反了" % name)
            self.assertNotIn("<img", inner, "「%s」用位图当图标：离线会变空框" % name)

    def test_the_icons_are_decorative_and_local(self):
        for tag in re.findall(r'<svg class="tab-ico"[^>]*>', self.html):
            self.assertIn('aria-hidden="true"', tag,
                          "图标没有 aria-hidden：读屏会把图形也念一遍")
            self.assertIn('focusable="false"', tag,
                          "旧版 Edge 会让 SVG 自己进 Tab 键序列（多出九个空停靠点）")
            self.assertIn('viewBox="0 0 24 24"', tag,
                          "图标没有统一坐标系：九个图形会各自缩放，粗细不一")
            self.assertNotIn("xlink:href", tag, "图标引用了外部 sprite")
        self.assertNotIn("<use ", self.html,
                         "图标应当自带图形，而不是引用一份不存在的 sprite")

    def test_the_icon_is_sized_and_drawn_in_current_colour(self):
        rule = self._rule(".tab-ico")
        self.assertTrue(rule,
                        "style.css 里找不到 `.tab-ico` 规则：图标会按 300×150 渲染")
        self.assertEqual(self._decl(rule, "width"), "16px")
        self.assertEqual(self._decl(rule, "height"), "16px")
        self.assertEqual(self._decl(rule, "fill"), "none",
                         "图标是线条：留着默认 fill 会得到实心黑块")
        self.assertEqual(self._decl(rule, "stroke"), "currentColor",
                         "图标没有用 currentColor：选中态不会跟着变亮")
        self.assertIn("0 0 auto", self._decl(rule, "flex"),
                      "图标会被长标签压扁（同一套图形于是有的圆有的扁）")

    def test_the_icon_keeps_a_gap_from_the_label(self):
        gap = self._decl(self._rule(".tab"), "gap")
        self.assertRegex(gap, r"^\d+(?:\.\d+)?px$",
                         "读不出 `.tab` 的 gap：%r" % gap)
        self.assertGreaterEqual(float(gap[:-2]), 8,
                                "图标与文字只隔 %s：16px 的图形贴着字会读成一团" % gap)

class ChoiceRowContractTestCase(unittest.TestCase):
    """选项面板：模型问"要哪几件"时，输入框上方长出一块竖排面板。

    勾一件或几件（还可以在「其他」里自己写）→ 点「其他」框右端那颗发送图标
    = 使用者说了这几句话，他不必把措辞抄回输入框。这条链跨四处，**任何一处断了都不报错、只是不出现**，
    症状还各不相同，所以四处各钉一次：

      * 提示词里没有那个围栏约定（`pentestd/chat.py` 工作方式第 7 条）→ 模型继续写
        普通列表，面板永远不出现，而"要你自己抄"正是这次要消掉的东西；
      * 脚本不认这个围栏（`web/chat.js` 的 splitOptions）→ 面板不出现，正文里那段
        ```options 原样显示成一坨代码；
      * 面板竖不起来（`web/style.css` 退回横排）→ 长选项又变成一条横带、只能截断或
        横滚才看得全，而"竖着排、一眼看几件"正是 t89 要的；
      * 面板与卡片的高度不同源（t94：面板已搬出卡片、自己让出 `--chat-overlap`）→
        面板被卡片吃掉一块，或者两段留白叠成一个洞（"卡片多高就叠多深"那条不变式）。
    """

    @classmethod
    def setUpClass(cls):
        cls.html = _read("index.html")
        cls.css = _read("style.css")
        cls.chat = _read("chat.js")

    def _rule(self, selector: str) -> str:
        start = self.css.find(selector + " {")
        self.assertGreaterEqual(start, 0, "style.css 里找不到规则：%s" % selector)
        end = self.css.find("\n}", start)
        self.assertGreater(end, start, "规则没有收尾：%s" % selector)
        return self.css[start:end]

    def test_the_panel_sits_above_the_card_and_outside_it(self):
        """它在卡片**外面**、卡片之前（t94 用户要求「输入框中的任务选择放到输入框上方」）。

        t89 时它在卡片里面（`flex: 0 0 100%` 占卡片一行），卡片的 `min-height` 于是要
        跟着一块条数不定的面板长。搬出来之后它只是"卡片上沿的一条流内兄弟"——高度各
        自算。这一条钉的是**归属**：面板一旦被搬回卡片里，`--chat-choice-h` 那套量高度
        的机制就得跟着回来，而两套几何同时算同一张卡片，正是这条链最容易断的地方。
        """
        queue = self.html.index('id="chat-queue"')
        row = self.html.index('id="chat-choice"')
        field = self.html.index('<div class="chat-field">')
        textarea = self.html.index('id="chat-input"')
        self.assertLess(queue, row, "面板排在排队块之后：先看排队的那几条，再看这一轮要你选什么")
        self.assertLess(row, field, "面板必须在卡片**外面**：它现在是卡片的上一位兄弟，不在卡片里")
        self.assertLess(field, textarea, "输入框仍在卡片里面（这一层是框，textarea 是它的子节点）")
        self.assertIn('class="chat-choice"', self.html)

    def test_the_panel_is_hidden_until_there_are_options(self):
        """默认 `hidden`：没有待选时整块不存在，卡片高度与叠进深度都不变。"""
        # **按标签看，不按属性顺序看**：`role="group"` / `aria-label` 插进来之后，
        # 一条 `id="chat-choice" hidden` 的字面断言会假红——而它想守住的是"初始不
        # 占位"，不是属性的先后。所以取 id 到 `>` 之间那一段再看 hidden。
        start = self.html.index('id="chat-choice"')
        tag = self.html[start:self.html.index('>', start)]
        self.assertIn(' hidden', tag,
                      "初始必须是 hidden：空着的面板会白占一块高度，卡片凭空变高")
        # `.chat-choice` 自己写着 `display: flex`，作者样式高于浏览器给 `[hidden]`
        # 的那条规则——不补这一条，藏起来的面板照画。
        self.assertRegex(self.css, r"\.chat-choice\[hidden\]\s*\{[^}]*display:\s*none")

    def test_the_panel_stacks_its_rows_vertically(self):
        """竖排：一行一个候选、文字可折行，不再是一条横带加横向滚动。"""
        panel = self._rule(".chat-choice")
        self.assertIn("flex-direction: column", panel,
                      "面板要竖排：横排时长选项只能截断或横滚，读不全")
        self.assertNotIn("flex-wrap: nowrap", panel,
                         "竖排面板不需要 nowrap：那是横带的做法")
        self.assertNotIn("overflow-x: auto", panel,
                         "竖向排列不该再需要横向滚动")
        listing = self._rule(".chat-choice-list")
        self.assertIn("flex-direction: column", listing, "候选行要一列排下来")
        text = self._rule(".chat-choice-text")
        self.assertNotIn("text-overflow: ellipsis", text,
                         "竖排之后没有理由再截断选项文字：折行就好，完整那句就在眼前")
        self._rule(".chat-choice-other-input")  # 「其他」的输入框必须有自己的样式

    def test_the_panel_reserves_the_overlap_the_card_eats(self):
        """卡片靠负外边距叠进上一位兄弟，抬多少就吃掉多少：面板得把那一段让出来。

        搬出卡片之后这条分工变了，三处必须同时成立，缺一处就是一块盖住对话字的面板
        （t57 / t82 在这个位置踩过两次）：
          * 面板自己让出 `--chat-overlap`（+ 一条缝 = `--sp-1`）；
          * 排队块在"面板在场"时退回普通间距，否则两段留白叠成一个洞；
          * 自动继续那一行只在**两块都不在**时才让——`:has()` 里必须写两个**相邻兄弟**，
            不是两条 `:has(+ …)`（后者要求同一个元素同时紧跟两块，永远不成立）。

        同时钉住旧的量高度机制**真的退场了**：`--chat-choice-h` 与 `measureChoice()`
        一起走，留一个就是两套几何算同一张卡片。
        """
        panel = self._rule(".chat-choice")
        self.assertIn("margin: 0 0 calc(var(--chat-overlap) + var(--sp-1))", panel,
                      "面板没有让出叠进深度：卡片那张负外边距会压上来盖住它的下半截")
        queue = self._rule(".chat-queue:has(+ .chat-choice:not([hidden]))")
        self.assertIn("margin-bottom: var(--sp-1)", queue,
                      "面板在场时排队块还让着 146px：两块之间会凭空多出一个洞")
        note = self._rule(".chat-auto-note:has(+ .chat-queue[hidden] + .chat-choice[hidden])")
        self.assertIn("calc(var(--chat-overlap) + var(--sp-1))", note,
                      "两块都不在时这一行没让出叠进深度：它会被卡片整块吃掉（字看不见）")
        self.assertNotIn("--chat-choice-h:", self.css,
                         "旧的量高度变量还在：两套几何同时算同一张卡片")
        self.assertNotIn("measureChoice", self.chat,
                         "旧的量高度函数还在：面板已不在卡片里，没有人需要它的高度")

    def test_the_panel_is_the_one_that_gives_way(self):
        """窗口装不下时，**先让记录区、再让面板**，卡片（和发送键）一步不让。

        这是 t94 才出现的一类几何：面板从卡片里搬到卡片上方之后，它和记录区抢同一块
        竖直空间。三处缺一不可，三处都量过（`.scratch/t94/probe_matrix.py`）：
          * `.chat-compose` 只在面板在场时可缩——平时它是 `0 0 auto`，写成可缩会让卡片
            被压（t57 把发送键压出过视口）；
          * 记录区先让（`flex-shrink: 60` + `min-height: 96px`）。默认系数 1 时实测
            1600×900 下记录区还剩 199px，面板却先被压掉 70px —— 让错的那一个；
          * 面板自己是可缩的那个（`flex: 0 1 auto` + `min-height: 0`），它的内容本来
            就在框内滚动，让高度不等于让内容。
        改这里的后果很具体：1280×900 三栏全开时不这么做，卡片下半截（含发送键）会被
        推到窗口外 170px。
        """
        shrink = self._rule(".chat-main:has(.chat-choice:not([hidden])) .chat-compose")
        self.assertIn("flex: 0 1 auto", shrink,
                      "面板在场时 compose 不可缩：窗口一矮，卡片就被推出视口")
        log = self._rule(".chat-main:has(.chat-choice:not([hidden])) .chat-log")
        self.assertIn("flex-shrink: 60", log,
                      "记录区没有优先让路：默认系数下先被压的是面板（实测让错 70px）")
        self.assertIn("min-height: 96px", log,
                      "记录区没有下限：它可以被压到 0，看起来像「对话没了」")
        panel = self._rule(".chat-choice")
        self.assertIn("flex: 0 1 auto", panel, "面板不可缩：它没法把自己让出去")
        self.assertIn("min-height: 0", panel, "面板有隐式下限：缩不动")
        # 平时（面板不在）那条 `0 0 auto` 必须还在原处：它是 t57 的结论，不能被这条新
        # 规则"顺手"改成可缩。
        compose = self._rule(".chat-compose")
        self.assertIn("flex: 0 0 auto", compose,
                      "平时的 compose 不再钉死：t57 那条「卡片不许被压」被推翻了")

    def test_the_only_send_key_is_the_composers_one(self):
        """t99：页面上**只有一颗**发送键（输入框那颗），面板里那颗已经删掉。

        用户原话：「不再使用任务选择框内的发送，去掉这个按钮，统一使用输入框中的发送」。
        t95/t96 那两版是把面板里的键"做成"输入框那一颗——类名相同、外观相同，但它仍是
        **第二颗按钮**：多一个 DOM 节点、多一处 `sendChoice()` 的调用点、多一份禁用/名字的
        同步责任。这一版删掉它，于是三件事要重新钉住，否则下次改面板又会把它加回来：

          * 面板里不许再出现按钮 → 整批的**唯一**入口是输入框那颗键的 `sendPrimary()`；
          * `sendPrimary()` 必须一次只做一件事 → 有勾选发那一批、否则发框里的字。两件一起
            发会同时开两条流（第二条撞 409，失败路径还把字塞回框里）；
          * 那颗键的名字必须跟着件数走 → 它现在是唯一能回答"会发出去几件"的地方，
            `syncSendName()` 在勾选、手写、跑起来、面板收起时都要重算一次。
        """
        build = _fn_body(self.chat, "buildChoice")
        self.assertTrue(build, "chat.js 里没有 buildChoice")
        self.assertNotIn("el('button'", build,
                         "面板里又造了一颗发送键：整批该由输入框那一颗发，"
                         "第二颗按钮迟早变成第二套外观与第二个调用点")
        self.assertNotIn("ICON_SEND", build,
                         "面板里还有发送图标：那一枚只该属于输入框那颗键")
        self.assertNotIn("'btn chat-send'", build,
                         "面板里还造着输入框那颗键的类名：键已删，"
                         "同一个按钮在页面里出现两次，迟早各自演化")
        self.assertIn("other.append(otherLabel, input)", build,
                         "「其他」这一格的结构变了：勾标一行、下面一个整宽的框（没有键）")
        self.assertNotIn("other-row", build,
                         "行容器没了键还留着：那一层 flex 只为给键让位而存在")
        self.assertIn("input.rows = 1", build,
                         "「其他」框又变回多行：这一格只是清单外补一件的地方")
        # 整批的唯一入口：一颗键分派两件事，且只做一件。
        primary = _fn_body(self.chat, "sendPrimary")
        self.assertTrue(primary, "chat.js 里没有 sendPrimary：整批没有入口了")
        self.assertIn("choiceTasks(pendingOptions()).length", primary,
                         "sendPrimary 没有先看勾选：面板上勾着几件，按发送还是只发输入框的字")
        self.assertIn("sendChoice();", primary,
                         "sendPrimary 不发那一批：面板上的勾选成了摆设")
        self.assertIn("send();", primary,
                         "sendPrimary 不退回输入框那条路：没勾选时按发送什么都不做")
        self.assertIn("else sendPrimary();", self.chat,
                         "主发送键的点击绑定没有走 sendPrimary：它只发框里的字，整批发不出去")
        self.assertNotIn("else send();", self.chat,
                         "主发送键还直接调 send()：绕过了 sendPrimary 的分派")
        # 回车仍然"发我打的这行字"，不被整批劫走。
        enter = self.chat.find("event.key === 'Enter' && !event.shiftKey")
        self.assertGreater(enter, 0, "输入框的回车绑定不见了")
        handler = self.chat[enter:enter + 240]
        self.assertIn("send();", handler,
                         "回车不再发送输入框里的字（被整批入口替换了？）")
        self.assertNotIn("sendPrimary", handler,
                         "回车也走 sendPrimary：勾着几件时按回车会发出那一批，"
                         "而使用者刚写下的那行字留在框里——他按的明明是回车")
        # 名字：一颗键三种身份，件数由 syncSendName() 现算。
        sync = _fn_body(self.chat, "syncSendName")
        self.assertTrue(sync, "chat.js 里没有 syncSendName：件数只能靠它说出来")
        self.assertIn("setAttribute('aria-label', label)", sync,
                         "发送键没有读屏名：件数变了名字不跟着变")
        self.assertIn("dom.send.title = label", sync,
                         "发送键没有悬浮提示：鼠标停上去什么也不说明")
        self.assertIn("'发送所选（' + tasks.length + ' 件）'", sync,
                         "名字里没有件数：面板里那颗键删掉之后，这是唯一能回答"
                         "'会发出去几件'的地方")
        self.assertIn("if (view.sending) label = '停止'", sync,
                         "跑着的时候它该叫「停止」：两种身份共用一个名字")
        self.assertIn("syncSendName()", _fn_body(self.chat, "setSending"),
                         "setSending 不再重算名字：跑起来之后它还叫「发送」")
        self.assertNotIn("busy ? '停止' : '发送'", _fn_body(self.chat, "setSending"),
                         "名字又写死成两种：面板上有勾选时它该叫「发送所选（N 件）」")
        self.assertIn("syncSendName()", _fn_body(self.chat, "renderChoice"),
                         "面板收起时不重算名字：发完这批之后它还挂着件数")
        # 样式：那一行容器的规则要跟着节点一起删，否则下次会被引回来。
        bare_css = re.sub(r"/\*.*?\*/", "", self.css, flags=re.S)
        self.assertNotIn("chat-choice-other-row", bare_css,
                         "行容器的样式还在：没有键了，这一层 flex 只是遗留")
        self.assertNotIn("chat-choice-send", bare_css,
                         "面板又给发送键写了专属规则：这一格已经不需要任何键的样式")

        def body(selector):
            # 本类的 _rule 取的是"从选择器到下一个行首 }"之前的**原文**（后面还揃着注释与
            # 下一条规则）：单行规则自带收尾的 }（要切掉），多行规则的 } 已被它排除在外。
            text = self._rule(selector)
            cut = text.find("}")
            return text if cut < 0 else text[:cut]

        box = body(".chat-choice-other-input")
        self.assertIn("width: 100%", box,
                         "「其他」框没有占满整宽：键撤走之后右侧会空出一条无人使用的槽")
        self.assertIn("min-height: var(--chat-action-row)", box,
                         "「其他」框不是一行高：与面板里其它行不再同一档")
        self.assertIn("field-sizing: content", box,
                         "写第二件事时框不再长高：两行内容挤在一片一行的窗里")
        self.assertNotIn("flex: 1 1 auto", box,
                         "框还是按「给键让位」的方式写的（flex 项收缩）：键已经不在了")
        key = body(".chat-send")
        self.assertIn("translateY(var(--send-lift, 0px))", key,
                         "上抬量没有变成所在行的变量：位置是所在行的事实，不是按钮的属性")
        self.assertNotIn("translateY(-2px)", key,
                         "上抬量又写死回按钮自己：面板里那一颗复用时会比框高出 2px")

    def test_the_panel_offers_checkboxes_and_an_other_slot(self):
        """多选：清单里勾几件，清单外自己写一件——都进同一批。"""
        build = _fn_body(self.chat, "buildChoice")
        self.assertTrue(build, "chat.js 里没有 buildChoice")
        for needle in ("box.type = 'checkbox'", "chat-choice-other-input",
                       "addEventListener('input'", "syncChoice(options)"):
            self.assertIn(needle, build,
                          "buildChoice 少了 %r：勾不上、写不进或多选发不出去" % needle)
        self.assertIn("choiceSignature(options)", _fn_body(self.chat, "renderChoice"),
                      "选项没变却重建面板：勾到一半的勾、写到一半的字会被清掉")
        self.assertNotIn("pickChoice", self.chat,
                         "旧的单点即发路径还在：两套发送逻辑迟早对不上")

    def test_the_send_path_reuses_send_message(self):
        """勾完点「发送所选」= 发一句话。回路**复用** sendMessage，不另开一条。"""
        send = _fn_body(self.chat, "sendChoice")
        self.assertTrue(send, "chat.js 里没有 sendChoice")
        for needle in ("sendMessage(choiceMessage(tasks))", "canSend()",
                       "choiceTasks(pendingOptions())", "cancelAutoContinue()"):
            self.assertIn(needle, send, "sendChoice 少了 %r：选择 / 开流 / 失败分类会少一份" % needle)
        message = _fn_body(self.chat, "choiceMessage")
        self.assertIn("if (tasks.length === 1) return tasks[0]", message,
                      "只勾一件时必须逐字发那一句（t89 之前的行为要原样保留）")
        self.assertIn("'继续这几件事：", message, "多件时要排成编号列表")

    def test_the_panel_is_recomputed_with_the_log(self):
        self.assertIn("renderChoice()", _fn_body(self.chat, "renderLog"),
                      "renderLog 不重算选项面板：切会话、刷新、跑完都不会更新")

    def test_the_old_button_row_is_gone(self):
        """t89 换成竖排面板：横带按钮（旧类名）连同它的样式一起退场。"""
        self.assertNotIn("chat-choice-btn'", self.chat,
                         "chat.js 还在用旧按钮类：面板会是半新半旧")
        self.assertNotIn(".chat-choice-btn {", self.css,
                         "style.css 里旧按钮样式还在：删了类不删样式，下次还会被引回来")

    def test_the_raw_fence_is_not_drawn_in_the_bubble(self):
        """选项块搬进面板之后，气泡里不许再画一遍原文。"""
        node = _fn_body(self.chat, "messageNode")
        self.assertIn("splitOptions(message.text)", node,
                      "气泡没有切掉 options 块：同一个问题会显示两份（一份还像要你抄）")
        self.assertIn("parts.body", node)
        live = _fn_body(self.chat, "liveBody")
        self.assertIn("splitOptions(", live, "流式正文里没切：跑到一半会闪出一段代码")

    def test_the_prompt_teaches_the_fence(self):
        """不写进提示词，模型只会继续写列表——界面这边再对也永远等不到那个块。"""
        self.assertIn("```options", chat.SYSTEM_PROMPT,
                      "系统提示词里没有 options 围栏的约定")
        self.assertIn("输入框", chat.SYSTEM_PROMPT,
                      "约定只说形状、没说它变成什么：模型不知道点了就等于说了那句")

class QueueActionIconTestCase(unittest.TestCase):
    """排队任务那一排动作按钮是"图标 + 悬浮提示"，不是文字（t89）。

    只有图标、没有名字的按钮：读屏软件念"按钮"，鼠标停上去也没有提示——使用者只能
    靠猜。所以三件都在才算过：图形是 SVG（`btn.innerHTML = icon`）、名字同时写在
    `title`（悬浮提示）与 `aria-label`（读屏）上，且五个动作各自传的是图标常量而不是
    中文字面量（改回文字按钮会让这一类断言立刻变红）。
    """

    @classmethod
    def setUpClass(cls):
        cls.chat = _read("chat.js")

    def test_each_action_passes_an_icon_constant(self):
        calls = re.findall(r"queueButton\('([a-z]+)',\s*(ICON_QUEUE_[A-Z_]+)", self.chat)
        self.assertEqual(sorted(act for act, _icon in calls), ["cancel", "delete", "edit", "save"],
                         "排队任务的动作按钮又回到文字了：查到的调用是 %r；"
                         "每颗都应当是 queueButton('<act>', ICON_QUEUE_*, '<名字>')" % (calls,))

    def test_the_send_icon_follows_the_state(self):
        self.assertIn("busy ? ICON_QUEUE_INTERJECT : ICON_QUEUE_START", self.chat,
                      "第一颗图标没有跟着状态换：跑着（箭头）与停着（播放）应当不一样，"
                      "否则两种状态在屏幕上是同一颗按钮")

    def test_the_name_is_on_title_and_aria_label(self):
        body = _fn_body(self.chat, "queueButton")
        self.assertIn("btn.innerHTML = icon", body,
                      "按钮没有把图形当图标塞进去（改回文字，或者图标是空的）")
        self.assertIn("btn.title = label", body,
                      "没有 title：鼠标停上去什么也不显示")
        self.assertIn("btn.setAttribute('aria-label', label)", body,
                      "没有 aria-label：读屏软件只会念\"按钮\"，不说是哪一个")

    def test_no_chinese_labels_remain(self):
        for text in ("queueButton('save', '保存'", "queueButton('edit', '编辑'",
                     "queueButton('delete', '删除'"):
            self.assertNotIn(text, self.chat,
                             "还有文字按钮（%s）：这一排已经改成图标，名字移到了 title/aria-label" % text)


class ChatLogTailContractTestCase(unittest.TestCase):
    """记录区末尾与输入卡片之间那道空隙：它必须是**量出来的**，不是算出来的。

    背景（t92）：用户截图里「已批准」审查卡下方空了近 90px，卡片离输入框很远。
    量出来是三段拼起来的——

      * `.chat-log` 的 `padding-bottom: calc(var(--chat-overlap) + var(--chat-fade))`
        按「叠进深度 = 卡片高度」那条不变式留白，而卡片顶上还压着 `.chat-compose`
        的 12px 上内边距与 16px flex `gap`（实测记录区盒底只比卡片上沿高 148px，
        算式却按 176px 留）→ 末尾白白多出 40px；
      * `.chat-log .chat-review` 自己的 12px 下边距挂在了最后一格上 → 52px；
      * 那一轮还挂着一个空的（临时的）assistant 气泡 → 90px。

    修法是把第一条从「算」改成「量」：`syncChatTail()` 读 `#chat-log` 盒底与
    `.chat-field` 上沿的真实差值再写内边距。这里守四件事，任何一件被改回去都
    不会报错——只会重新空出一截，而那是只有量像素才看得见的症状。
    """

    @classmethod
    def setUpClass(cls):
        cls.css = _read("style.css")
        cls.js = _read("app.js")

    def _rule(self, selector: str) -> str:
        start = self.css.find(selector + " {")
        self.assertGreaterEqual(start, 0, "style.css 里找不到规则：%s" % selector)
        end = self.css.find("\n}", start)
        self.assertGreater(end, start, "规则没有收尾：%s" % selector)
        return self.css[start:end]

    def test_the_last_card_drops_its_bottom_margin(self):
        """最后一格是审查卡时，那 12px 下边距就是纯空白：末格与卡片上沿之间只剩渐隐。"""
        rule = self._rule(".chat-log .chat-review:last-child")
        self.assertRegex(rule, r"margin-bottom:\s*0\b",
                         "末格审查卡必须去掉下边距：它是最后一样东西，"
                         "留着就是卡片下方多出的 12px")

    def test_a_card_that_is_not_last_keeps_its_margin(self):
        """不是最后一格时那 12px 是**两格之间的间距**，撤了会让卡片贴着下一条消息。"""
        rule = self._rule(".chat-log .chat-review")
        self.assertIn("margin: var(--sp-2) 0 var(--sp-3)", rule,
                      "普通状态下仍要有上下边距；`:last-child` 只该在末格生效")

    def test_the_tail_is_measured_from_the_real_boxes(self):
        """量的必须是**两个矩形**，不是一个推导出来的数。

        卡片上方会长出排队行 / 告警行 / 自动继续行，它们各自的
        `margin|padding-bottom: var(--chat-overlap)` 会把卡片整张推到记录区下面去
        ——那时叠进深度是负的，任何「按卡片高度算」的算式都不成立。
        """
        start = self.js.index("function syncChatTail()")
        body = self.js[start:self.js.index("\n}", start)]
        self.assertIn("getBoundingClientRect", body,
                      "内边距必须从两个真实矩形推出（记录区盒底 − 卡片上沿）")
        self.assertIn("chatTailFade()", body,
                      "末端只留渐隐那一段：那个长度也要从 `--chat-fade` 读，不能手填 12")
        self.assertIn("log.style.paddingBottom", body,
                      "要覆盖的正是 `.chat-log` 的 padding-bottom（CSS 那条留作无 JS 兜底）")
        self.assertNotRegex(body, r"paddingBottom\s*=\s*['\"]188",
                            "188 是那条被打破的算式的结果，不许作为常量写进来")

    def test_the_sync_runs_on_every_layout_change(self):
        """三处都要接上：首帧、换页签、以及尺寸/结构变化那条共用链路。"""
        self.assertGreaterEqual(self.js.count("syncChatTail()"), 3,
                                "首帧 / switchTab / 观察器回调都要重算一次")
        run = self.js[self.js.index("const run = () => {"):]
        run = run[:run.index("};")]
        self.assertIn("syncChatTail()", run,
                      "尺寸与结构变化走 `scheduleNoticeAnchor()` 那条共用链路，"
                      "漏接就只在刚加载时对、之后一直错")
        switch = self.js[self.js.index("state.tab = name;"):]
        switch = switch[:switch.index("\n}")]
        self.assertIn("syncChatTail()", switch,
                      "换页签会换掉整块面板（卡片可能刚出现），必须当场重算")

    def test_the_css_formula_survives_as_a_fallback(self):
        """没有 JS（或 JS 报错）时仍要有个合理的值，不能退成 0。"""
        rule = self._rule(".chat-log")
        self.assertIn("padding-bottom: calc(var(--chat-overlap) + var(--chat-fade))", rule,
                      "CSS 那条是兜底：JS 没跑起来时按叠进深度 + 渐隐留白")

class ModelChipHugsItsTextTestCase(unittest.TestCase):
    """模型格**只比自己那行字宽一点**，两层都要在（t90）。

    用户两次指出同一件事：先「模型选择框太宽了…能跟随文字大小」，改完
    `field-sizing` 之后又贴一张截图说「右边还是有空白的」。

    原因是 `<select>` 的 intrinsic 宽度取**最长那个选项**，而最长的一项通常是
    「跟随全局启用项（DeepSeek · deepseek-flash）」——比选中的短名字长出一大截。
    `field-sizing: content` 只在部分浏览器上按选中项算宽，只写它等于把这件事交给
    浏览器版本决定；量出来的 `--model-w` 才是与浏览器无关的那一层。

    守四件事，缺一条就退回去：
      1. CSS 首选 `var(--model-w, auto)`（光 `auto` 会按最长选项量宽）；
      2. 量的字体取**控件自己的 computed font**（拿默认字体猜会差像素，偏小就切字）；
      3. 量的是选中项的文字 + 左右内边距（少内边距，箭头会压到字上）；
      4. 填完选项、换完选项都要重算（`openSession` 走的是 `renderEntry`）。
    """

    @classmethod
    def setUpClass(cls):
        cls.css = _read("style.css")
        cls.chat = _read("chat.js")

    def test_css_prefers_the_measured_width(self):
        start = self.css.find(".chat-model-btn {")
        self.assertGreaterEqual(start, 0, "style.css 里找不到 `.chat-model-btn`")
        end = self.css.find(chr(10) + "}", start)
        rule = " ".join(self.css[start:end].split())
        self.assertIn("width: var(--model-w, auto)", rule,
                      "宽度必须以量出来的 `--model-w` 为首选：退回 `auto` 就按"
                      "**最长内容**量宽，短名字后面又空出一大片")
        self.assertNotIn("field-sizing", rule,
                         "t100：这一格已经是 `<button>`，`field-sizing` 对它没有意义；"
                         "留着会让下一个人以为还有一层兜底在生效")
        self.assertIn("max-width: 100%", rule,
                      "封顶在内层：外层 `.chat-model` 的 240px 是给让位档位用的，"
                      "chip 自己不许越过去")

    def test_the_measurement_uses_the_control_own_font(self):
        body = _fn_body(self.chat, "textWidth")
        self.assertIn("getComputedStyle", body,
                      "字体必须问控件自己要：拿主题的默认字体猜，长名字会差几像素")
        self.assertIn("measureText", body)
        for part in ("fontStyle", "fontWeight", "fontSize", "fontFamily"):
            self.assertIn(part, body,
                          "canvas 的 font 要按 computed style 的四段拼，漏一段都用错字体")

    def test_measured_width_covers_text_paddings_and_a_subpixel_margin(self):
        body = _fn_body(self.chat, "syncModelWidth")
        name = _fn_body(self.chat, "syncModelChip")
        self.assertIn("selectedIndex", name,
                      "量的是**选中那一项**的文字：量最长选项等于没改")
        self.assertIn("--model-w", name + body,
                      "chip 上的字与被量的宽度必须来自同一处")
        self.assertIn("paddingLeft", body)
        self.assertIn("paddingRight", body)
        self.assertIn("--model-w", body)
        self.assertIn("Math.ceil", body)
        self.assertIn("+ 1 + 'px'", body,
                      "末尾那 1px 是亚像素余量：没有它，末字会被 ellipsis 切出「…」")
        self.assertIn("columnGap", body,
                      "箭头与字之间那道缝也要算进宽度：漏掉它，长名字会把箭头挤到边框上")
        self.assertIn("offsetWidth", body,
                      "箭头自己的宽度要量（12px 的 svg），不能写死一个数")

    def test_it_reruns_after_filling_and_on_change(self):
        entry = _fn_body(self.chat, "renderEntry")
        self.assertIn("syncModelChip()", entry,
                      "填完模型选项之后先把 chip 上的字换掉（宽度要按新字量）")
        self.assertLess(entry.index("syncModelChip()"), entry.index("syncModelWidth()"),
                        "顺序反了会按**上一项**的字量宽：字还没换，量出来的就是旧的")
        self.assertIn("if (node === dom.modelSelect) { syncModelChip(); syncModelWidth(); }",
                      self.chat,
                      "换选项也要重量：否则短名字后面留着上一个模型的宽度")

    def test_the_model_menu_stays_inside_the_card(self):
        """菜单 268px、向上弹：它必须**整个落在卡片里**，否则窄窗口下就是屏幕外。

        这不是"好看"的问题：菜单超出卡片右沿之后，视口边缘就是下一道裁切——最窄那一档
        卡片离窗口边只剩十几像素，用户点了模型只看到半截菜单。
        锚点分三档（宽档朝右、536 起朝左、392 起钉到卡片右沿），每一档的取值都在
        交互规范 3.21.2 里写了理由与实测。
        """
        flat = " ".join(self.css.split())
        self.assertIn("--chat-row-tail: 104px", flat,
                      "这一簇（权限 chip + 发送键）的宽度是量出来的 104，菜单要靠它定位")
        self.assertIn("width: 268px; max-width: 80vw;", flat,
                      "菜单的宽度与封顶（与权限那颗共用一条规则）")
        self.assertIn(".chat-model-menu { left: 0; right: auto; }", flat,
                      "宽档朝右展开：chip 在动作行左半边，右边有位置")
        self.assertIn(".chat-model-menu { left: auto; right: 0; }", flat,
                      "536 档起朝左对齐到 chip 右沿：再朝右长，field 486 时右溢 2px")
        # 最窄那一档的锚点是**多行**规则（要写三条），单独切出那一段再看。
        tier = flat.index("@container chat-field (max-width: 392px)")
        segment = flat[flat.index(".chat-model-menu {", tier):][:420]
        self.assertIn("right: calc(-1 * var(--chat-row-tail))", segment,
                      "只剩图标那一档把右沿钉到**卡片右沿**：改前 card 390 时右溢 134px")
        self.assertIn("max-width: calc(100cqw - 8px)", segment,
                      "极端窄（card 230）时靠它撑住：菜单比卡片还宽时，朝哪边都出界")

    def test_choosing_a_row_goes_through_the_native_select(self):
        """点菜单里的那一行 = 改原生 select + 派发 change。

        原生 `#chat-model-select` 仍是唯一真相源：落库（`commitEntry` / 发送带
        `modelId`）只认它的值。菜单如果自己去调接口，就会出现"界面上换了、发出去的
        还是旧的"这种最难查的一类不一致——与权限那颗 chip 同一做法（`applyPolicy`）。
        """
        body = _fn_body(self.chat, "chooseModel")
        self.assertIn("dom.modelSelect.value = id", body,
                      "先改原生 select：它是真相源")
        self.assertIn("new Event('change', { bubbles: true })", body,
                      "再派发 change：落库走的是原来那条监听，不另开一条路")
        self.assertIn("dispatchEvent", body)
        self.assertIn("syncModelChip()", body,
                      "选完要把 chip 上的字同步过来（change 监听里也会做，但这一步"
                      "在菜单收起之前，先换字才不会闪一下旧值）")
        self.assertIn("hideModelMenu(true)", body,
                      "收菜单要把焦点还给 chip：不然焦点落在被摘掉的菜单项上，"
                      "下一次 Tab 会从文档开头重新数")



class ChatHistoryWindowHintTestCase(unittest.TestCase):
    """长对话的窗口：接口只送尾巴，「更早的 N 条」要能一键取回来。

    只说"上面还有"不给路，等于把一句实话变成一句没法处理的坏消息；而翻上去的
    这几百条还要在下一次收尾重画时活着（`adoptMessages` 就是为那一步写的）。
    """

    def test_the_script_reads_and_renders_the_missed_count(self):
        script = _read("chat.js")
        self.assertIn("payload.earlierCount", script,
                      "接口给的 earlierCount 必须读进来，否则页面不知道上面还有")
        self.assertIn("view.earlierCount > 0", script)
        self.assertIn("function earlierHint(", script,
                      "提示行要是一个函数：加载完之后得按新数字重画")
        self.assertIn("chat-earlier-btn", script)
        self.assertIn("function loadEarlier(", script)

    def test_the_earlier_page_is_requested_by_cursor(self):
        script = _read("chat.js")
        self.assertIn("?before=", script,
                      "往回翻必须带游标，不能又要一遍尾巴")
        self.assertIn("encodeURIComponent(first.seq)", script)
        self.assertIn("function messageVisible(", script,
                      "旧消息插到顶上要用同一份可见性判据，否则会插出空气泡")
        self.assertIn("if (!messageVisible(message)) return;", script)

    def test_wholesale_refresh_keeps_the_older_slice(self):
        script = _read("chat.js")
        self.assertIn("function adoptMessages(", script)
        self.assertIn("keep.concat(list)", script)
        self.assertIn("Number(keep[keep.length - 1].seq) + 1 === first", script,
                      "只有两段首尾相接才拼：中间有洞说明旧段不完整")
        self.assertGreaterEqual(
            script.count("adoptMessages(payload.messages, payload.earlierCount)"), 2,
            "打开会话与收尾刷新都要走它，否则翻出来的旧消息会被截掉")

    def test_the_hint_has_its_own_rules(self):
        css = _read("style.css")
        self.assertIn(".chat-earlier {", css)
        self.assertIn(".chat-earlier-btn {", css)

    def test_a_new_or_failed_session_forgets_the_count(self):
        # 新会话 / 加载失败之后必须归零：留着就会在新会话顶部说一句"上面还有"。
        self.assertGreaterEqual(_read("chat.js").count("view.earlierCount = 0;"), 2)
class ChatHistoryShortcutTestCase(unittest.TestCase):
    """「加载更早」的两个无按钮入口：Alt+↑ 与滚到顶上自动取。

    按钮能点就够"有入口"，但往回翻发生在正往上读的时候：手在键盘上、视线在记录里。
    自动取若没有"使用者自己滚过"这道门，一打开长对话就会自己往下翻——把一次打开
    变成连翻好几页；若插页之后不补偿滚动位置，它会一路翻到最早一条。
    """

    def test_alt_up_loads_the_previous_page(self):
        script = _read("chat.js")
        self.assertIn(
            "event.altKey && !event.ctrlKey && !event.metaKey && !event.shiftKey",
            script,
            "Alt+↑ 不能带 Ctrl/⌘/Shift：那是浏览器与系统自己的快捷键")
        self.assertIn("loadEarlier({ auto: true });", script,
                      "键盘入口与自动取必须走同一份取页逻辑，否则滚动补偿会有三个版本")
        self.assertIn("event.preventDefault();", script)
        self.assertIn("Alt+↑ 也能加载", script,
                      "快捷方式写在按钮 title 上：不写就没人知道有这条路")

    def test_typing_in_the_composer_is_not_hijacked(self):
        script = _read("chat.js")
        self.assertIn("node.isContentEditable", script)
        self.assertIn("node.tagName === 'TEXTAREA'", script,
                      "输入框里 Alt+↑ 是编辑器的行移动，抢过来就是坏")
        self.assertIn("editing(event.target)", script)

    def test_auto_load_waits_until_the_reader_scrolled_himself(self):
        script = _read("chat.js")
        body = _fn_body(script, "earlierAutoLoad")
        self.assertIn("view.logTouched", body,
                      "程序化滚动不置位：否则一打开会话就会自己连翻几页")
        self.assertIn("box.scrollTop > 2", body)
        self.assertIn("box.scrollHeight <= box.clientHeight + 4", body,
                      "内容还没长过一屏时，'在顶上'没有意义")
        self.assertIn("dom.log.addEventListener('scroll', earlierAutoLoad", script)
        self.assertIn("['wheel', 'touchmove'].forEach", script)

    def test_one_page_per_reach_and_no_throttle_needed(self):
        # 插一页会把 scrollTop 顶高同样多（loadEarlier 末尾），"在顶上"因此立刻
        # 不再成立——链式触发自己停住。这条补偿是自动取不失控的唯一依据。
        body = _fn_body(_read("chat.js"), "loadEarlier")
        self.assertIn("box.scrollTop = keepTop + (box.scrollHeight - oldHeight)", body)

    def test_an_auto_failure_falls_back_to_the_button(self):
        body = _fn_body(_read("chat.js"), "loadEarlier")
        self.assertIn("if (auto) view.logTouched = false;", body,
                      "自动取失败一次就退回手动：停在顶上会一秒一个请求")
        self.assertIn("if (!auto) ui().showError", body,
                      "自动取失败不弹窗，但按钮按下去仍然要说原因")

    def test_the_record_is_reset_when_the_log_is_repainted(self):
        body = _fn_body(_read("chat.js"), "renderLog")
        self.assertIn("view.logTouched = false;", body,
                      "重画会贴到底，'他滚过'必须重新归零")



class SetupWorkspaceRowTestCase(unittest.TestCase):
    """部署信息卡上的「工作区」一行。

    "文件写到哪去了"必须能在界面上答出来：答不出来的时候，使用者会去项目根目录找，
    而那儿本来就不该有。所以这一行有契约，不是"顺手加的一行字"。
    """

    def test_the_row_exists_and_reads_the_payload(self):
        body = _fn_body(_read("app.js"), "renderSetupInfo")
        self.assertIn("appendInfoRow(list, '工作区'", body)
        self.assertIn("data.workspace", body)
        self.assertIn("sessionCount", body)

    def test_the_row_sits_under_the_database(self):
        body = _fn_body(_read("app.js"), "renderSetupInfo")
        # 路径类信息排在一起：数据库 → 工作区 → 引擎。
        self.assertLess(body.index("'数据库'"), body.index("'工作区'"))
        self.assertLess(body.index("'工作区'"), body.index("'推理引擎'"))


class PanelsWiringTestCase(unittest.TestCase):
    """panels.js 与 index.html 之间的接线（侧边栏新增的六页）。

    这一批守的是**同一类静默失效**：渲染函数写对了、接口也通了，只是它去取的那个
    节点名拼错一个字母——症状是「点开页签一片空白」，而红的东西一个都没有。
    `test_dom_ids_referenced_by_ui_exist` 已经挡下了 `$('#x')` 这一种写法，但它
    看不见另外两种，这里各补一条：

      * `bind('x', …)` —— 这六页的按钮走的是文件里那个小工具 `bind`，它把 id 当
        **字符串参数**传给 `document.getElementById(id)`。`_id_selectors` 只认字面
        量写法，于是这六行在它眼里是空气。拼错的后果比空白更难看出来：按钮从此
        没有反应，页面本身却是好好的。
      * `LOADERS` 的键 —— 键名就是页签名（`data-tab` 的值），写错时那一页永远
        不加载，点开是一片空白。

    另两条守这个文件自己的约定（见 panels.js 文件头）：接口数据一律经 `textContent`
    落 DOM；失败要说清是哪一块失败。
    """

    # 侧边栏这一轮新增的六页。**改这里等于改界面**：多一页就要多一组接线，
    # 少一页就该有人问它去哪了——所以键名钉死在用例里，而不是从文件里读回来
    # 再跟自己比（那种写法在整块被删掉时仍然全绿）。
    PAGES = ("dashboard", "tasks", "schedules", "traffic", "repeater",
             "workspace", "approvals", "logs")

    @classmethod
    def setUpClass(cls):
        cls._html = _read("index.html")
        cls._app = _read("app.js")
        cls._js = _read("panels.js")
        cls._code = _strip_js_comments(cls._js)

    def test_every_dom_id_it_looks_up_exists(self):
        missing = sorted(_id_selectors(self._js) - _static_ids(self._html))
        self.assertEqual(missing, [],
                         "panels.js 引用了 index.html 里不存在的 id：%s" % missing)

    def test_every_button_it_binds_exists_in_the_markup(self):
        """`bind('x', …)` 的 id 必须真的在页面上。

        `bind` 拿不到节点时是**静默返回**（`if (node) …`）。这是对的：六页里任何
        一页被裁掉都不该把整个脚本拖崩。代价是拼错一个字母之后，那颗按钮只是从此
        没有反应，而这只能在静态上判。
        """
        bound = set(re.findall(r"""\bbind\(\s*['\"]([\w-]+)['\"]""", self._code))
        self.assertTrue(bound, "panels.js 里没有找到任何 bind(…)，这条守卫失效了")
        missing = sorted(bound - _static_ids(self._html))
        self.assertEqual(missing, [],
                         "panels.js 绑定了页面上不存在的按钮 id：%s" % missing)

    def _loader_keys(self):
        block = re.search(r"const LOADERS = \{(.*?)\n  \};", self._code, re.S)
        self.assertIsNotNone(block, "panels.js 里找不到 LOADERS 表")
        return set(re.findall(r"(?m)^\s{4}([a-z]+):", block.group(1)))

    def test_every_loader_key_is_a_real_tab(self):
        """`LOADERS` 的键就是页签名；每一页都得有按钮和 section。

        键名写错时不会报任何错：`load(tab)` 查不到就什么都不做，而调用方那一侧
        早就 return 了——点开页签就是一次纯本地切换，正文停在第一次进来时的样子。
        """
        keys = self._loader_keys()
        self.assertEqual(sorted(keys), sorted(self.PAGES),
                         "LOADERS 的键与这六页对不上：%s" % sorted(keys))
        tabs = set(re.findall(r'data-tab="([\w-]+)"', self._html))
        for key in sorted(keys):
            self.assertIn(key, tabs, "LOADERS 里的 %s 不是一个页签" % key)
            self.assertIn('id="panel-%s"' % key, self._html,
                          "%s 没有对应的 section，渲染出来无处可放" % key)

    def test_every_page_is_dispatched_from_load_tab_data(self):
        """`loadTabData` 里必须有一支把这六页接进来。

        没有它，点开页签就是一次纯本地切换：那一块永远是空的，而没有任何东西会
        失败。这条断言还要求它排在 `!state.eid` 早退**之前**——六页回答的是「整个
        平台上发生了什么」，与有没有选中目标无关。
        """
        body = _fn_body(self._app, "loadTabData")
        self.assertTrue(body, "app.js 里找不到 loadTabData")
        for key in self.PAGES:
            self.assertIn("'%s'" % key, body,
                          "loadTabData 没有分支处理「%s」这一页" % key)
        self.assertIn("PentestPanels.load", body,
                      "这六页的加载没有接到 PentestPanels 上")
        # 拿**语句**当锚点、不拿 `!state.eid` 这个片段：函数体里有注释写着
        # 「必须排在下面 !state.eid 那个早退之前」，按片段找会命中那句注释。
        gate = "if (!state.eid) return;"
        self.assertIn(gate, body, "loadTabData 里找不到未选目标的早退")
        self.assertLess(body.index("PentestPanels.load"), body.index(gate),
                        "这六页的分支必须排在未选目标的早退之前")

    def test_it_never_builds_markup_out_of_strings(self):
        """这个文件经手的每个值都来自网络（URL、响应头、响应正文），一律 textContent。

        `test_html_escapes_untrusted_data` 只挡 `innerHTML = … + ${…}` 这一种形状：
        把整段模板先存进变量再赋给 innerHTML，在那里是看得见但不报的。六页里出现
        的值全部不可信，所以这里直接不许出现拼接 HTML 的入口。
        """
        for api in ("innerHTML", "outerHTML", "insertAdjacentHTML", "document.write"):
            self.assertNotIn(api, self._code,
                             "panels.js 不许用 %s：这里的值全部来自网络" % api)

    def test_every_failure_names_what_failed(self):
        """`errorBlock(...)` 的实参不许直接是那个异常对象。

        `errorBlock`（见 app.js 里那一行）只写 textContent：传 Error 进去，界面上
        就是一句「Error: Failed to fetch」。在一个有十五个页签的工作台里，这句话
        没有回答「是哪一块坏了」——六页的第一版八处全写成了这个样子。
        """
        bare = re.findall(r"errorBlock\(\s*error\b", self._code)
        self.assertEqual(bare, [],
                         "有 %d 处 errorBlock 直接传了异常对象，界面上会显示成"
                         "「Error: …」，看不出是哪一块失败" % len(bare))

    def test_every_destructive_call_asks_first(self):
        """删东西的那几次点击必须先问一句。

        界上其它删除动作全都这么做（`clearJobs`、`clearHubOps`、`removePlugin`、
        `removeSkill`），只有「清空全部流量」这一颗漏了——一次点击删掉一整张表，
        而那张表里躺着请求头与响应正文（Cookie、令牌就在里面）。

        这一条漏得更隐蔽的地方在于：**按钮写着「全部」，看到的却是自己的那些**。
        `list_traffic` 按 `userId` 收口，`clear_traffic()` 不带 `user_id` 就
        `DELETE FROM traffic` —— 管理员点下去清的是全库，含别人的记录。确认框里
        必须把这句话说出来，只写「确定吗」等于没有说。
        """
        # 判据：每个删东西的 apiPost 之前，同一个处理块里要有一句 window.confirm。
        # 用「往前找最近的那个 bind( / function」切块，而不是按行号钉死——按行号
        # 钉死的守卫只会提醒"这一行变了"，说不出"少了那句话"。
        pattern = re.compile(r"apiPost\(\s*['\"][^'\"]*(?:clear|delete|remove|purge)")
        calls = list(pattern.finditer(self._code))
        self.assertTrue(calls, "panels.js 里没有找到任何删除类调用，这条守卫失效了")
        for call in calls:
            head = self._code[:call.start()]
            start = max(head.rfind("bind("), head.rfind("function "))
            block = self._code[start:call.start()]
            self.assertIn("window.confirm", block,
                          "这条删除调用没有二次确认：%s"
                          % self._code[call.start():call.start() + 60])

    def test_the_clear_dialog_says_it_clears_other_peoples_records(self):
        """清空是跨用户的动作，确认框里得写出来。

        列表与清空的**范围不一样**：列表按 `userId` 收口，清空是整张表。
        「确定清空这 N 条」会让管理员以为删的正好是眼前这些。
        """
        # 看**去掉注释的代码**：这句话必须真的进确认框，写在注释里不算。
        self.assertIn("整张表", self._code,
                      "确认框没有说明清空的范围比列表大")

    def test_the_audit_pages_can_take_it_away_and_erase_it(self):
        """审批记录与日志两页都要有「导出」和「清空」。

        这两页原先只有「刷新」：看得到、擦不掉、也拿不走。「事后核账」这个诉求
        一半在屏幕上、一半在文件里——把某一时段的账导出去交给别人对一遍，和
        「现在就盯着这一屏看」是两件不同的事，只有前者需要按钮。

        两页的删除按钮文案必须带范围：审批记录的范围是「管理员整张表 / 其他身份
        只清自己名下那些」，日志的范围是「整份文件（含滚动出来的上一份）」——
        两条都比眼前这一屏大，而不说清时人默认按看到的那些理解。
        """
        ids = _static_ids(self._html)
        for name in ("approvals", "logs"):
            for suffix in ("export", "clear"):
                self.assertIn("%s-%s" % (name, suffix), ids,
                              "「%s」页没有%s按钮的挂点" % (name, suffix))
                self.assertIn("bind('%s-%s'" % (name, suffix), self._code,
                              "「%s-%s」这颗按钮没接上处理函数" % (name, suffix))
        # 清空的确认框必须说清范围比这一屏大：两句各自对应一条真实差额。
        self.assertIn("/reviews/clear", self._code)
        self.assertIn("/logs/clear", self._code)

    def test_the_export_does_not_go_through_the_listing_limit(self):
        """导出走的是另一条接口，不是「把列表再拉一遍」。

        列表夹在 100 条以内是为了「一屏看得完」。导出的价值恰恰在更早那些记录上
        ——照列表那条路导，交出去的文件看着完整、其实只有最近一百条，而拿到
        文件的人看不出缺了（它没有总行数可比）。
        """
        self.assertIn("/reviews/export", self._code,
                      "审批记录的导出没有自己的接口")
        self.assertIn("/logs/export", self._code,
                      "日志的导出没有自己的接口")
        # 导出落盘走 Blob + 临时 <a>：服务端没有 Content-Disposition 通道，
        # 文件名与编码本来就得由界面负责一次。
        self.assertIn("URL.createObjectURL", self._code)
        self.assertIn("URL.revokeObjectURL", self._code)

    def test_status_words_come_from_the_shared_table(self):
        """仪表盘与审批页不许印后端原值。

        这两页各漏过一次，症状一样：同一个状态在两条页签上是两个词。仪表盘印
        `pending` / `done`，任务页印「待执行」/「已完成」；审批页印 `always` /
        `pending`，对话里的审批卡片印「等待批准」。读的人得自己在脑子里建一张
        对照表，而建错的那次会把「已完成」当成「还在跑」。

        词表两边都有现成的：任务状态是 `PentestUI.jobView.label`（同一个
        `jobStatusOf`、同一张表，`app.js` 里那份导出就是为此），审查单的档位与
        状态是后端 `approval.public` 的 `tierLabel` / `statusLabel`。
        """
        self.assertIn("jobView.label", self._code,
                      "仪表盘没有用 jobView 的中文表，状态会印成英文")
        self.assertNotIn("job.status ||", self._code,
                         "仪表盘又直接印了 job.status 原值")
        self.assertIn("item.statusLabel", self._code,
                      "审批页没有用后端给的 statusLabel")
        self.assertIn("item.tierLabel", self._code,
                      "审批页没有用后端给的 tierLabel")

    def test_the_approval_page_admits_when_it_is_capped(self):
        """列表一次最多 100 条，拿满时要说出来。

        接口那侧的上限写死在 `server.list_reviews`（`min(100, limit)`），而这一页
        标题是「审批记录」、文档里写的是「全部审查单」。屏幕上只有最近一百条却
        不说，读的人会以为早先那些单子被谁删了。**不上调上限**：这页会被开着
        轮询，放大上限等于放大读放大。"""
        # 判据是 `data.limit`，不是字面量 "100"：上限若在前端写死一份，接口那边
        # 改了它不会知道，于是要么在没截断时宣称截断、要么反过来不说。这一条与
        # 流量页那条同一个道理（那边的判据一开始就是 `data.limit`）。
        self.assertIn("data.limit", self._code,
                      "审批页没有读响应里的 limit，上限是写死的")
        self.assertIn("只到最近", self._code,
                      "拿满上限时没有在页面上说明只列了最近这些")

    def test_the_traffic_page_admits_when_it_is_capped(self):
        """流量列表也有上限，拿满时同样要说。

        这一页比审批页更容易骗人：记录按时间倒序，屏幕上第一条就是刚刚发的，
        于是「看不到了」会被读成「被删了」。上限取响应里的 `limit` 而不是写死，
        前端改口径时这句话会跟着变。"""
        self.assertIn("data.limit", self._code,
                      "流量页没有读响应里的 limit，上限是写死的")
        self.assertIn("条）", self._code, "拿满时没有拼出「只到最近 N 条」")

    def test_the_empty_approvals_page_names_the_mode_in_chinese(self):
        """空态里的档位也要印中文。

        `GET /reviews` 除了 `mode` 还给 `modeLabel`（词表 `harness.MODE_LABELS`，
        与设置页那个下拉框同源）。原先这里拼的是 `data.mode` 原值，于是空态写
        成「闸照状态是「ask」」，而同一档在设置页叫「审查任意命令」——同一个设置
        项在一页里两个名字。"""
        self.assertIn("data.modeLabel", self._code,
                      "空态没有用后端给的 modeLabel，档位会印成机器词")

    def test_the_logs_page_does_not_call_a_partial_read_the_whole_file(self):
        """只读了文件尾部时，不许报「共 N 行」。

        日志有两种截断，页面上必须分开说：`limit` 是页面自己要几行（截了也知道
        `total`）；`server.read_logs` 的 `MAX_LOG_BYTES`（512 KB）是服务端只读了
        尾部，此时响应里的 `total` 只是那一段的行数。原先两种情况共用一句话，于是
        一个 4 MB 的日志会被报成「共 200 行」。"""
        self.assertIn("data.truncated", self._code,
                      "日志页没有区分「只要几行」与「服务端只读了尾部」")
        self.assertIn("只读了尾部", self._code, "截断时没有说出文件只读了一段")

    def test_the_dashboard_readings_use_the_shared_count_format(self):
        """仪表盘的读数是給人扫一眼的，要千分位。

        那段注释一直写着「数值用 `formatCount` 会加千分位」，代码写的却是 `String()`。
        注释承诺的东西不会自己生效。"""
        self.assertIn("formatCount", self._code,
                      "仪表盘没有解构 formatCount，千分位那句话是空话")
        self.assertNotIn("dash-stat-value', String(value)", self._code,
                         "仪表盘读数还在直接 String()，千分位没生效")

    def test_the_repeater_says_when_the_body_was_cut(self):
        """正文被 64 KB 上限截过要说出来。

        `httpreplay.MAX_BODY` 是 64 KB。截过的响应在前端看起来与一份完整响应毫无
        区别，而「流量」页的详情早就标了「（已截断）」——同一次请求的两个出口，
        两套说法就是其中一处漏了。"""
        # 只断言 `responseTruncated` 是不够的：流量详情页 (`showTraffic`) 里也有
        # 这个字段，那句断言会被那一处满足，于是重发页去掉整段提示照样绿。钉的
        # 是这句话本身——上限数字与「只是前一段」这几个字。
        self.assertIn("超过 64 KB", self._code,
                      "重发页没有在正文被截断时说出来，那份残页会被当成全文")

    def test_every_dash_dot_colour_matches_its_badge(self):
        """同一状态在两处的颜色必须同义。

        仪表盘的那颗圆点与任务页的那枚徽章说的是同一件事。`done` 起初根本没有
        规则，回落成 `--muted` 的灰——看上去像「没有状态」；`running` 起初也是
        绿，跟 `done` 撞色，于是「还在跑」与「已经完了」只差文字。

        判据取 `.status-badge` 那一组现成的配色：那是全站已被接受的语义，
        圆点跟它对齐就不必再发明一套。
        """
        css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)

        # 这两组规则都是**合并写**的（`.check-badge.check-ok, .status-badge.status-online,
        # .status-badge.status-done { … }`），所以不能按「本条规则只有这一个选择器」
        # 去匹配：那样四条里有两条查不到，而报出来的错是「style.css 里找不到这条规则」，
        # 看起来像样式表缺了东西。逐个 `{…}` 块找，命中逗号列表里任意一项即可。
        blocks = [(sel, body) for sel, body in
                  re.findall(r"([^{}]+)\{([^{}]*)\}", css)]

        def rule(selector):
            for selectors, body in blocks:
                if selector not in [item.strip() for item in selectors.split(",")]:
                    continue
                colour = re.search(r"background:\s*var\((--[\w-]+)\)", body)
                self.assertTrue(colour, "%s 没用 background: var(--token)" % selector)
                return colour.group(1)
            self.fail("style.css 里找不到 %s 这条规则" % selector)

        for status in ("done", "running", "pending", "failed"):
            badge = rule(".status-badge.status-%s" % status)
            dot = rule(".dash-dot.is-%s" % status)
            self.assertEqual(badge, dot,
                             "%s 状态：徽章用 %s、圆点用 %s，两处不该是两种颜色"
                             % (status, badge, dot))
        self.assertNotEqual(rule(".dash-dot.is-done"), rule(".dash-dot.is-running"),
                            "「已完成」与「执行中」同色，圆点就分不出跑没跑完")

    def test_every_displayed_count_goes_through_the_shared_format(self):
        """凡是拼进句子的数字，都要走同一条格式化。

        千分位不是装饰：这几个页面上的数字是「扫一眼读量级」用的，1200 与 1,200
        在读的时候不是一回事。原先只有四张读数卡走了 `formatCount`，而同屏的
        提示语（「其中高危漏洞 N 条」「待执行 N · 执行中 N」）、审批页的「N 条 ·
        待处理 N」、清空后的「已清空 N 条」全是裸插值——一张页面上两种写法。

        判据取裸插值这个形状（`${… || 0}`）：它只在把数字直接塞进句子时出现，
        而那种地方正是格式化被漏掉的地方。`card()` 的实参不算——它自己会格式化。
        """
        raw = re.findall(r"\$\{[^}]*\|\| 0\}", self._code)
        self.assertEqual([], raw,
                         "这些数字没走 formatCount，同一张页面上会有两种写法：%r" % raw)
        self.assertIn("formatCount(trafficCount)", self._code,
                      "清空确认框里的条数没走 formatCount")
        # 裸 `String(...)` 与裸 `${data.总行数}` 是同一个疏漏的另两种形状：
        # 工作区的会话子目录数、日志页的「共 N 行 / 显示最后 M 行」都曾经直接插值，
        # 于是同一张页面上「1,200 条」与「1200 行」并排出现。
        self.assertIn("formatCount(data.total)", self._code,
                      "日志页的「共 N 行」没走 formatCount")
        self.assertIn("formatCount(data.lines.length)", self._code,
                      "日志页的「显示最后 N 行」没走 formatCount")
        self.assertIn("formatCount(data.sessionCount || 0)", self._code,
                      "工作区的会话子目录数没走 formatCount")

    def test_the_dashboard_picks_high_risk_with_the_shared_order(self):
        """「高危及以上」要按同一份顺序判，不许在页面上写死一堆档位名。

        后端 `panel()` 数 `highFindings` 用的是 `severity_rank(...) <= 1`（权威顺序
        在 `models.SEVERITIES`）。仪表盘原先写的是 `['critical', 'high'].includes(...)`，
        两份字面量一旦有一边改了（新增档位、调整排序），这一页列出来的与计数说的
        就不是同一批记录，而没有任何东西会报警。

        判据取 `severityView` 这个共享入口：它在 `app.js` 里与 `SEVERITY_ORDER` /
        `severityOf` 同源，仪表盘读它就不必自己维护第二份顺序。"""
        self.assertIn("severityView.rank", self._code,
                      "仪表盘没有用共享顺序判高危，写死的档位名会与接口口径漂开")
        self.assertNotIn("['critical', 'high']", self._code,
                         "仪表盘还在写死高危档位名")

    def test_every_dynamic_class_it_builds_has_a_rule(self):
        """拼出来的类名必须在样式表里真有规则。

        `.sev-dot` 原先只活在 `panels.js` 的类名里，`style.css` 一行也没有：那颗
        `span` 于是零尺寸、无背景——仪表盘「高危漏洞」每一行的数字与标题之间空着
        一块，什么也看不见。类名拼错与规则没写，在浏览器里是同一种表现，在源码里
        也是同一句话。

        判据只看**固定前缀**（`dash-dot is-` / `sev-dot sev-`）的那一截：后缀是
        状态或档位，由归一函数给，逐个枚举等于把词表抄第三遍。"""
        css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)
        blocks = [(sel, body) for sel, body in re.findall(r"([^{}]+)\{([^{}]*)\}", css)]

        def has_rule(selector):
            return any(selector in [item.strip() for item in sel.split(",")]
                       for sel, _body in blocks)

        self.assertIn(".sev-dot {", css, "样式表里没有 `.sev-dot` 的基础规则")
        self.assertIn(".dash-dot {", css, "样式表里没有 `.dash-dot` 的基础规则")
        for base, prefix in ((".dash-dot", ".dash-dot.is-"), (".sev-dot", ".sev-dot.sev-")):
            self.assertTrue(has_rule(base), "样式表里没有 %s" % base)
            self.assertTrue(
                any(sel.startswith(prefix) for sel in (s.strip() for s, _b in blocks)),
                "样式表里没有任何 %s 规则：圆点会没有颜色" % prefix)

    def test_the_severity_class_is_normalised_before_it_is_used(self):
        """拼 `sev-` 类名之前先把档位归一。

        后端给的 `severity` 是原值。认不出的或空的直接拼进去就成了 `sev-` 或
        `sev-unknown`，样式表里没有这两种，圆点回到无色的老问题。全站其他的档位
        渲染（漏洞卡片、详情页、过滤条）都先过 `severityOf`，这一处不该例外。"""
        self.assertIn("sev-${severityView.of(", self._code,
                      "高危圆点的档位没归一，认不出的值会拼出没有规则的类名")
        self.assertNotIn("sev-${item.severity", self._code,
                         "还在用后端原值拼类名")

    def test_the_approvals_page_can_show_why_a_call_was_flagged(self):
        """审查单要能展开看理由与参数。

        使用手册写着「展开看参数与判定」，而这一页原先只画六个格子：摘要是一行标题，
        要回答「这条命令当时要干什么、平台为什么觉得它该让人看一眼」，得看 `reasons`
        与 `arguments`——`approval.public` 随列表一起给了，不展示就是白扔。

        判据钉两件事：有那颗按钮，且展开的内容取的是这两个字段（而不是只把状态词
        再印一遍）。"""
        self.assertIn("item.reasons", self._code, "审查单没有展示判定理由")
        self.assertIn("item.arguments", self._code, "审查单没有展示调用参数")
        self.assertIn("收起", self._code, "展开了却收不回去")

    def test_the_repeater_does_not_fire_twice_on_a_double_click(self):
        """「发送」发的是真请求，双击就是两条。

        重发页那颗按钮把请求真的发出去：POST 连点两次就是建了两个用户、下了两次单，
        而界面上只显示最后那份响应，看起来像只发了一次。所以要在飞的这一条没回来
        之前把按钮按住。

        判据有三句话，缺一条就有一个具体的坏结果：
          * 有锁（否则双击两条都发出去）；
          * 有 `finally` 里的解锁（否则一次网络错误之后按钮永久失灵）；
          * 上锁排在请求头解析之后——解析那段里有一条 `return`（第 N 行少了个冒号），
            它走不到 `finally`，锁在它前面上就会把按钮永久按死，只能刷新整页。"""
        self.assertIn("if (send.disabled) return;", self._code, "发送按钮没有防重复点击")
        self.assertIn("send.disabled = false;", self._code,
                      "失败之后没有放开按钮（一次网络错误会让它永久失灵）")
        lock = self._code.find("send.disabled = true;")
        parse = self._code.find("行请求头格式不对")
        self.assertNotEqual(-1, lock, "没有上锁")
        self.assertNotEqual(-1, parse, "请求头解析不见了")
        self.assertGreater(lock, parse,
                           "锁上在解析之前：解析里那条 return 走不到 finally，按钮会被永久按死")

    def test_elapsed_times_go_through_the_shared_duration_format(self):
        """耗时也走共享那把尺子（`PentestUI.formatDuration`）。

        同屏两种写法最容易发生在数字上：任务页把 1200 ms 写成「1.2 s」，而流量列表与
        重发结果直接拼 `durationMs + ' ms'`。同一个数在两张表上两种写法，读的人不会
        觉得是格式问题，只会觉得其中一个是别的量。"""
        self.assertIn("formatDuration(record.durationMs)", self._code,
                      "流量页没有用共享的耗时格式化")
        self.assertNotIn("durationMs + ' ms'", self._code,
                         "还在自己拼毫秒单位")

    def test_the_asset_type_word_comes_from_the_platform(self):
        """资产类型的中文说法只该有一份来源（后端 `models.ASSET_TYPE_LABELS`）。

        这一条原先是有三份：`web/app.js` 自带的 `ASSET_TYPE_LABEL`、插件里直接印的
        `type`、链路图节点标签拼的 `type`。同一个资产在资产树上是「服务」、在插件
        资产页是 `service`、在导出的 SVG 上是 `service · 8080`——三处没有任何东西会
        报警，因为每一处单看都说得通。

        判据钉取值顺序：先看平台贴的 `asset.typeLabel`，本地那张表只作兜底（离线缓存
        里的旧快照没有这个字段）。三个渲染点（影响资产、资产树、详情徽章）都必须走
        同一个 `assetTypeWord`——各写一遍就等于把顺序抄三遍。
        """
        self.assertIn("assetTypeWord", self._app,
                      "资产类型没有统一的取值函数")
        self.assertIn("asset.typeLabel || ASSET_TYPE_LABEL[asset.type]", self._app,
                      "取值没有先看平台贴的 typeLabel")
        self.assertNotIn("ASSET_TYPE_LABEL[asset.type] || asset.type", self._app,
                         "还有渲染点在跳过 typeLabel 直接查本地表")
    def test_every_class_token_it_builds_has_a_rule(self):
        """每一档写死的类名都要在样式表里真有规则。

        上一条只看两个圆点族的固定前缀，因为它们的后缀是状态词。这一条管另一种：
        整串都是固定字符串的那些（`el('button', 'btn primary', …)`）。

        这一族原先漏了四处，症状各不相同、来源却是同一个——写了名字，样式表没接：
          * `.btn.primary`：视觉设计系统 §2.5 把「主按钮」列成四级之一，还点名了
            「发送」，panels.js 也确实挂了 `btn primary`，可样式表一条规则都没有，
            于是那颗按钮长得跟旁边任何一颗一样；
          * `.btn.ghost`：查看正文 / 删除 / 清空已结束三处都挂着，同样没有规则，
            于是「静默级」在视觉上不存在；
          * `.check-badge.check-bad`：体检页「目录缺失」那颗最该报警，却与中性那颗
            同灰；
          * `.tag-accent`：`.tag` 族里唯一没有规则的一档，强调标签与普通标签同色。

        写错的类名与没写的规则在浏览器里是同一种表现（什么都没发生），在源码里也是
        同一句话（一个字符串）。只能靠读一遍样式表把它对上。
        """
        css = re.sub(r"/\*.*?\*/", "", _read("style.css"), flags=re.S)
        blocks = [(sel, body) for sel, body in
                  re.findall(r"([^{}]+)\{([^{}]*)\}", css)]

        def has(selector):
            return any(selector in [item.strip() for item in sel.split(",")]
                       for sel, _body in blocks)

        # 只取「第二个实参是一整串字面量」的写法：带 `$` 的是模板串，后缀交给归一
        # 函数；这里管的是人直接把名字敲进来的地方。
        #
        # 反过来也不能撒网：`el('div', 'chat-msg-body')` 这类布局块各有各的父级，
        # 全都不写规则也照样对（`chat-msg-body` 靠 `.chat-msg` 的排版）。所以只查
        # **按「基类 + 修饰」这套约定组织起来的五族**——它们的存在意义就是让同一个
        # 基类按修饰变样子，缺一档就等于缺那个状态。
        FAMILIES = ("btn", "tag", "check-badge", "status-badge", "test-badge")
        scanner = re.compile("el\\(\\s*'[a-zA-Z0-9]+'\\s*,\\s*'([^'$]*)'\\s*[,)]")
        missing = []
        for name in ("app.js", "panels.js", "chat.js",
                     "settings.js", "skills.js", "guide.js"):
            source = _strip_js_comments(_read(name))
            for lineno, line in enumerate(source.splitlines(), 1):
                for found in scanner.finditer(line):
                    tokens = [tok for tok in found.group(1).split()
                              if re.fullmatch(r"[a-z][a-z0-9-]*", tok)]
                    if not tokens:
                        continue
                    base_cls, modifiers = tokens[0], tokens[1:]
                    if base_cls not in FAMILIES:
                        continue
                    if not has("." + base_cls):
                        missing.append("%s:%d 的 .%s 没有规则" % (name, lineno, base_cls))
                    for modifier in modifiers:
                        if not (has("." + base_cls + "." + modifier)
                                or has("." + modifier)):
                            missing.append("%s:%d 的 .%s.%s 没有规则"
                                           % (name, lineno, base_cls, modifier))
        self.assertEqual([], sorted(set(missing)),
                         "这些类名拼出来却没有规则，在页面上等于不存在：%s"
                         % sorted(set(missing)))

    def test_no_class_name_that_nothing_reads(self):
        """不留「只挂在 DOM 上、没有任何选择器读」的名字。

        `chat-side-more chat-side-scope` 那两颗按钮原先还挂着个 `chat-side-scope`：
        样式表里没有它的规则，全仓也没有任何脚本查它。写的人以为在表达「这是范围
        开关」，看的人查不到任何东西——它既不改变长相也不改变行为，却让后来的人
        花时间去确认这个类是不是有用。

        与上一条的区别：上一条问「名字有没有样式」，这一条问「这个名字还有没有人
        在用」。空类名不会报错，只会积累。
        """
        # 读剥过注释的源码：注释里提到这个名字是**对的**（那段注释正是在说它），
        # 只有挂在 `el(...)` 的类名串里才算又冒出一个人。
        for name in ("chat.js", "app.js", "panels.js",
                     "settings.js", "skills.js", "guide.js"):
            self.assertNotIn("chat-side-scope", _strip_js_comments(_read(name)),
                             "%s 里又挂了一个没有规则、也没有脚本读的类名" % name)
        self.assertNotIn("chat-side-scope", _read("style.css"),
                         "样式表里给它写了规则就更说不通了：没有脚本会挂上它")
    def test_executor_online_is_decided_in_one_place(self):
        """执行节点「在线 / 离线」只有一个判定与一张词表。

        这个判定原先在四个地方各写了一遍（顶栏 title、顶栏读数、部署页摘要、部署页
        每行徽章），每一处都是 `String(item.status || '').toLowerCase() === 'online'`，
        第四处还多写了 `item.online === true ||`。库里存的就是 `online` / `offline`
        （`store.list_executors` 现算，超时的那条就地改成 offline），所以四处都是同一个
        字面量比对——而顶栏 title 那一行更直接印 `item.status` 原值，鼠标停上去看到
        的是 `fixture-node [online] 负载 0`，同一颗节点的徽章上却写着「在线」。

        四份同样的字面量，改一处的失败形态是：顶栏说 1/2 在线，部署页里那两个都显示
        「离线」——没有任何东西会报错，两处都「对」。

        插件侧 `dsh-plugins/dsh-pentest-console/client.js` 的 `executorLabel` 是同一套
        规矩（认不出的照原样给，不编「未知」），只是它拿不到这里的函数。
        """
        code = _strip_js_comments(self._app)
        self.assertIn("const EXECUTOR_STATUS_LABEL = { online: '在线', offline: '离线' };", code)
        # 四处调用点全部走同一个函数，而不是各自再比一次字面量。
        self.assertGreaterEqual(code.count("executorIsOnline"), 4,
                                "在线判定要四处共用同一个函数")
        self.assertNotIn("toLowerCase() === 'online'", code,
                         "又出现了逐处写死的在线比对")
        # title 那一行与徽章都印中文，不能一处中文一处机器词。
        self.assertIn("executorStatusWord(item)", code)
        self.assertIn("executorStatusWord(executor)", code)
    def test_the_tool_op_note_speaks_chinese_about_the_status(self):
        """装失败之后那一行说明，也要说人话。

        `toolNoteText` 给「最近操作」收尾：成功了列出装好的适配器，失败了列出失败的那些
        连原因。原因可能是空的（请求没到、进程被杀），此时原先直接插 `op.status` 原值，
        于是同一张表上出现「（安装） — failed」，而它上面那行的徽章写着「失败」
        ——`TOOL_STATUS` 就在旁边（`done/running/queued/pending/failed/cancelled` 六档
        中文），这一处没用上。

        判据不能只查 `TOOL_STATUS[op.status]` 在不在：徽章那一行（`toolOpList`）本来就有
        一模一样的一句，查字段名等于没查——这是第 26 条那个坑的同一形状。所以钉的是
        失败分支自己那一行：`split` 出来的首行、`||` 之后接词表，三者连在一起才唯一。
        """
        code = _strip_js_comments(self._app)
        anchor = "String(op.reason || '').split('\\n')[0] || TOOL_STATUS[op.status]"
        self.assertIn(anchor, code,
                      "失败那一行又直接印了 op.status 原值")
        self.assertNotIn("split('\\n')[0] || op.status ||", code,
                         "失败原因缺失时不许把机器词顶上")

class McpLevelWordTestCase(unittest.TestCase):
    """MCP 服务的权限档在设置页只许有一种说法。

    同一张卡片上有三处在说这件事：卡片头的徽章（「档位不够」）、卡片头的档位名
    （`server.levelLabel || server.level`）、以及页头那句「这些服务当前档位拿不到
    ……（注册档位 X）」的播报。前两处早就走 `levelLabel`，第三处印的是 `server.level`
    原值——于是同一屏出现「档位不够」「只读观察」「readonly」三种写法，而最后那句
    「要用它们，把对话入口的权限档调高」正是**因为读者不知道 `readonly` 是高还是低**
    才读不懂的。

    `levelLabel` 由后端 `mcp.public_server()` 按 `policy.POLICY_INDEX` 贴（只读观察 /
    网络探测 / 源码审计 / 完全作业），认不出的档位给空串，所以回落原值是必要的——
    这一条与 `asset_type_label` / `fact_kind_label` 是同一条规矩。
    """

    @classmethod
    def setUpClass(cls):
        cls._js = _strip_js_comments(_read("settings.js"))

    def test_the_blocked_note_names_the_level_in_chinese(self):
        """钉的是那一句本身：`（注册档位 ` 之后紧跟 `levelLabel || level`。

        只查 `server.levelLabel` 在文件里出现过等于没查：卡片头那一行
        （`mode-note`）本来就有一模一样的一句，第 26 条与第 39 条都是同一个坑。
        所以两边一起钉——正向钉这一句的写法，反向钉那个已经修掉的拼接。
        """
        self.assertIn("server.id + '（注册档位 '", self._js)
        self.assertIn("(server.levelLabel || server.level) + '）'", self._js,
                      "档位播报又印回 server.level 原值了")
        self.assertNotIn("'（注册档位 ' + server.level + '）'", self._js,
                         "同一张卡片上的档位必须与卡片头用同一个词")

class GuideGroupOrdinalTestCase(unittest.TestCase):
    """`guide.js` 说「第 N 组」的那个 N，必须与导航上真正的次序一致。

    真发生过：设置页加到十一组之后，导航里插进了「扫描执行」与「工具审批」两组，
    而使用说明那一章仍是旧编号——「插件市场」写着第六组（真身第八）、「MCP 服务」
    写着第七组（真身第九）、「用户管理」写着第八组（真身第十一）。三个编号同时错，
    而页面上一切正常：读者拿着「第七组」去数胶囊按钮，数到的是「工具审批」。

    更早一版还整段缺了「扫描执行」与「工具审批」两组的正文——编号错与正文缺是
    同一件事的两面：那段文字是照着旧列表写的，加组的时候没人回来改。

    判据取的是「序号与标签一起对」：只查标签出现过会被正文里的任意一处满足；
    只查序号递增，则三处一起偏一位也照样递增。
    """

    @classmethod
    def setUpClass(cls):
        cls._guide = _read("guide.js")
        cls._html = _read("index.html")

    def _nav_labels(self):
        match = re.search(r'<nav[^>]*id="settings-nav".*?</nav>', self._html, re.S)
        self.assertIsNotNone(match, "找不到设置页导航")
        return re.findall(r'data-settings-group="[\w-]+"[^>]*>([^<]+)<', match.group(0))

    def _guide_groups(self):
        found = re.findall(r"第([一二三四五六七八九十]+)组「\*\*([^*]+)\*\*」", self._guide)
        self.assertTrue(found, "guide.js 里一个「第 N 组」都没找到")
        return found

    def test_every_ordinal_matches_the_real_position_in_the_nav(self):
        numerals = ["一", "二", "三", "四", "五", "六", "七", "八", "九", "十", "十一", "十二", "十三", "十四"]
        labels = self._nav_labels()
        self.assertEqual(13, len(labels), "导航不是十三组了：%s" % labels)
        problems = []
        for ordinal, name in self._guide_groups():
            if ordinal not in numerals:
                problems.append("认不出的中文序号：%s" % ordinal)
                continue
            position = numerals.index(ordinal)
            if position >= len(labels):
                problems.append("「%s」写成了第 %s 组，但导航只有 %d 组"
                                % (name, ordinal, len(labels)))
                continue
            if labels[position] != name:
                problems.append("「%s」写的是第 %s 组，而导航上第 %d 组是「%s」"
                                % (name, ordinal, position + 1, labels[position]))
        self.assertEqual([], problems,
                         "guide.js 的分组编号与设置页导航对不上：\n  - "
                         + "\n  - ".join(problems))

    def test_the_two_groups_that_are_easy_to_forget_are_written_about(self):
        """扫描执行与工具审批曾经整段没有正文——编号错是症状，缺正文才是病。"""
        for name in ("扫描执行", "工具审批"):
            self.assertIn("组「**%s**」" % name, self._guide,
                          "guide.js 没有讲到设置页的这一组：%s" % name)
