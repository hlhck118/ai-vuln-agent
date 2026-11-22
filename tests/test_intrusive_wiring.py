# -*- coding: utf-8 -*-
"""五类侵入式动作的**接线**：从对话工具一路走到执行节点，每一环都在不在。

## 为什么单独一组用例守"接线"

`test_authz.py` 守准入闸本身，`test_exploit.py` / `test_creds.py` /
`test_shell.py` / `test_pivot_ops.py` / `test_toolkit.py` 守各自引擎的判据。
它们全绿，用户**仍然可能一件事都做不到**——因为中间还隔着五个环节：

    对话工具 → policy 分级 → create_job 的 kind 白名单 → 命名后的准入判定
             → agent 的 kind 分发 → 结果回传 → 落库

这五个环节里任何一环漏掉，症状都是"某一类动作永远做不到"，而每一环自己的
单元测试都是绿的：引擎的判据没错，闸门也没错，只是它们**没有连在一起**。
用户问的正是这个——引擎写好了但没接线，从使用者那一侧看与"没实现"完全一样。

所以这组用例只测接线，而且只做两类断言：

1. **授权通过时，任务真的进了队列**（不是"函数返回了 201"）。用任务计数说话。
2. **授权被拒时，任务一个都没进队列。** 这条比第一条重要：只断言状态码的话，
   一个"先建任务再判授权"的实现照样全绿，而它的后果是每一条被拒的请求都在
   队列里留下一个永远不会被执行的任务。
"""

import json
import os
import re
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import agent, authz, cli, creds, engine, exploit     # noqa: E402
from pentestd import pivot, policy, server, shell                  # noqa: E402
from tests import support                                          # noqa: E402

# 本文件的核心断言是"**授权被拒时任务一个都没进队列**"（用任务计数说话，不是状态
# 码），而那要求闸真的会拒——所以这一模块内把 `authz.FORCE_OPEN` 关掉；跑完还原。
# 强制放行下"不拒"这件事由 `test_force_open.py` 独立守住。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


def _read(path: str) -> str:
    """读一个工作区文件。

    收成一个函数，是为了让每一处调用都必然关掉句柄——这组用例有两处直接
    `open(...).read()`，Python 会报 ResourceWarning，而警告很容易被当成噪声
    忽略，直到某个更难的资源问题被同一类写法掩盖掉。
    """
    with open(path, encoding="utf-8") as handle:
        return handle.read()


def _cli_serve_parser():
    """取 `cli` 里 `serve` 子命令的解析器。

    从子命令表里取，而不给 `cli` 新加一个只被测试用到的公开函数——那种接口
    一旦存在就会有人"顺手"用它做别的事，而它本来只是为了取一个对象。
    """
    import argparse
    for action in cli.build_parser()._actions:
        if isinstance(action, argparse._SubParsersAction):
            return action.choices["serve"]
    raise AssertionError("cli 的解析器里没有子命令表")


def _load_deploy_start():
    """按文件路径加载 `deploy/start.py`。

    它不在包内（`deploy/` 不是 Python 包），所以只能这样加载。用 spec 而不是
    往 `sys.path` 里塞 `deploy/`：后者的副作用是让 `start` 这个通用名字变成
    可导入模块，撞名时会静默导到另一个 `start.py` 上去。
    """
    import importlib.util
    path = os.path.join(ROOT, "deploy", "start.py")
    spec = importlib.util.spec_from_file_location("deploy_start", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# 五类各一份**最小可用**的请求体。写全参数是刻意的：这里要验的是接线，
# 参数不全导致的 400 会让"接线断了"与"我参数写错了"混在一起，没法归因。
def _bodies(target: str = "10.0.0.5"):
    return {
        "exploit": {"kind": "exploit", "target": "http://%s/" % target,
                    "authorization": "ENG-1234 授权验证"},
        "creds": {"kind": "creds", "target": target, "driver": "ftp",
                  "authorization": "ENG-1234 授权验证"},
        "shell": {"kind": "shell", "target": "http://%s/" % target,
                  "mode": "upload",
                  "args": {"uploadPath": "/upload", "uploadField": "file",
                           "fetchPath": "/uploads/{{marker}}.txt"},
                  "authorization": "ENG-1234 授权验证"},
        "pivot": {"kind": "pivot", "target": target,
                  "foothold": {"id": "FH-1", "kind": "socks5", "host": target,
                               "port": 1080, "capability": "socks5"},
                  "egress": ["%s:445" % target],
                  "authorization": "ENG-1234 授权验证"},
        "toolkit": {"kind": "toolkit", "target": target, "adapter": "nmap",
                    "toolArgs": ["-sT", "-Pn", "-p", "80", target],
                    "authorization": "ENG-1234 授权验证"},
    }


class WiringTestCase(unittest.TestCase):
    """公共脚手架：一个带授权放行的控制面 + 一个什么授权都没有的控制面。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("wiring.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.target = "10.0.0.5"

    def _serve(self, classes, targets):
        """起一个独立的控制面与库。每个用例自己一套，互不干扰。"""
        directory, store = support.temporary_store("wiring-plane.db")
        httpd, client = support.start(store, intrusive=classes,
                                      intrusive_targets=targets)
        self.addCleanup(support.stop, httpd, store, directory)
        return client, store

    def _job_count(self, store):
        return len(store.jobs())

    def _post(self, client, body):
        return client.post("/api/v1/jobs", body)


class EveryClassReachesTheQueueTestCase(WiringTestCase):
    """授权通过时，五类动作各自真的进得了队列。"""

    def test_every_class_is_accepted_when_authorized(self):
        client, store = self._serve(["all"], [self.target, str(self.target)])
        for kind, body in _bodies(self.target).items():
            with self.subTest(kind=kind):
                status, payload = self._post(client, body)
                self.assertEqual(201, status,
                                 "%s 没能进队列：%s" % (kind, payload))
                self.assertEqual(kind, payload["kind"])
                job = store.get_job(payload["jobId"])
                self.assertIsNotNone(job, "响应说建了任务，库里却没有")
                self.assertEqual(kind, job["kind"])

    def test_queue_grows_by_exactly_one_per_accepted_request(self):
        # 用计数而不是状态码说话：状态码能骗人（先建后判也返回 403），
        # 任务计数不能。
        client, store = self._serve(["all"], [self.target])
        before = self._job_count(store)
        for body in _bodies(self.target).values():
            self.assertEqual(201, self._post(client, body)[0])
        self.assertEqual(before + 5, self._job_count(store))


class RefusalHappensBeforeQueueingTestCase(WiringTestCase):
    """授权被拒时，队列**一个任务都不该多**。

    这条比"状态码是 403"重要得多。只断言状态码的话，一个"先建任务再判授权"
    的实现照样全绿，而它的后果是每一次被拒的请求都在队列里留下一个永远不会
    被执行的任务——用户看到的是"我明明被拒了，队列里却多了一条"。
    """

    def test_nothing_is_queued_when_the_class_is_closed(self):
        client, store = self._serve([], [self.target])
        before = self._job_count(store)
        for kind, body in _bodies(self.target).items():
            with self.subTest(kind=kind):
                status, payload = self._post(client, body)
                self.assertEqual(403, status, "%s 在关闭状态下被放行了" % kind)
                self.assertIn(kind, str(payload.get("error") or ""),
                              "拒绝理由要点名是哪一类动作没开")
        self.assertEqual(before, self._job_count(store),
                         "有任务在授权被拒的情况下进了队列")

    def test_refusal_names_the_flag_not_the_policy(self):
        # 模型最容易在这里读错：它会把准入拒绝当成参数错误然后换个写法重试，
        # 而每一次重试都是一次真实的网络动作。所以拒绝理由必须说清缺的是
        # 部署侧那个开关，而不是"你换个参数试试"。
        client, _store = self._serve([], [self.target])
        _status, payload = self._post(client, _bodies(self.target)["creds"])
        message = str(payload.get("error") or "")
        self.assertIn("--allow-intrusive creds", message)
        self.assertIn("权限档", message,
                      "要说清「这不是权限档能解锁的」，否则模型会去改档位重试")

    def test_short_authorization_is_refused_before_queueing(self):
        client, store = self._serve(["all"], [self.target])
        before = self._job_count(store)
        body = _bodies(self.target)["exploit"]
        body["authorization"] = "x"
        status, payload = self._post(client, body)
        self.assertEqual(403, status)
        self.assertIn("授权说明", str(payload.get("error") or ""))
        self.assertEqual(before, self._job_count(store))

    def test_target_outside_the_intrusive_whitelist_is_refused(self):
        client, store = self._serve(["all"], ["10.9.9.9"])
        before = self._job_count(store)
        status, payload = self._post(client, _bodies(self.target)["exploit"])
        self.assertEqual(403, status)
        self.assertIn("不在侵入白名单", str(payload.get("error") or ""))
        self.assertEqual(before, self._job_count(store))

    def test_the_error_says_the_whitelist_is_extra_not_a_replacement(self):
        # "可连接的网段"与"可打的机器"是两份清单。把地址加进探测作用域**不会**
        # 让它变得可打——这句话必须出现在拒绝理由里，否则使用者会去改错那个开关。
        client, _store = self._serve(["all"], ["10.9.9.9"])
        _status, payload = self._post(client, _bodies(self.target)["exploit"])
        self.assertIn("探测作用域", str(payload.get("error") or ""))


class PivotTwoGatesTestCase(WiringTestCase):
    """出口路由有**两道**准入判定，缺一不可。"""

    def test_foothold_outside_the_whitelist_is_refused(self):
        client, store = self._serve(["pivot"], ["10.9.9.9"])
        before = self._job_count(store)
        status, payload = self._post(client, _bodies(self.target)["pivot"])
        self.assertEqual(403, status)
        self.assertEqual(before, self._job_count(store))

    def test_an_egress_target_outside_the_whitelist_is_refused_even_when_the_foothold_is_inside(self):
        # 这是两道闸存在的全部意义。只判立足点的话，只要跳板进了白名单，
        # 就能经它把流量打到任意网段——而立足点自己那份 `egress` 白名单只是
        # 同一个进程里的约束，它拦的是"代理被当成开放代理用"，不是"这台机器
        # 被授权去打"。所以这条用例失败时的形态是"任务进了队列"，不是"报错"。
        client, store = self._serve(["pivot"], [self.target])
        before = self._job_count(store)
        body = _bodies(self.target)["pivot"]
        body["egress"] = ["10.9.9.9:445"]
        status, payload = self._post(client, body)
        self.assertEqual(403, status,
                         "立足点在白名单、出口目标不在，却被放行了")
        self.assertEqual(before, self._job_count(store))

    def test_every_egress_target_is_checked_not_just_the_first(self):
        client, store = self._serve(["pivot"], [self.target])
        before = self._job_count(store)
        body = _bodies(self.target)["pivot"]
        body["egress"] = ["%s:445" % self.target, "10.9.9.9:445"]
        status, _payload = self._post(client, body)
        self.assertEqual(403, status, "只判了第一个出口目标")
        self.assertEqual(before, self._job_count(store))

    def test_a_network_egress_must_be_covered_by_the_whitelist(self):
        # `exploit.split_target("10.0.0.0/24")` 会给出 `10.0.0.0`。拿这个去比
        # 白名单，一条 `--intrusive-targets 10.0.0.0` 就能让整段 /24 通过，
        # 而实际流量会打到这 256 个地址。所以网段必须**被包含**，不能只判网络地址。
        client, store = self._serve(["pivot"], [self.target])
        before = self._job_count(store)
        body = _bodies(self.target)["pivot"]
        body["egress"] = ["10.0.0.0/24"]
        status, payload = self._post(client, body)
        self.assertEqual(403, status,
                         "一个单地址白名单条目放行了整段 /24")
        self.assertEqual(before, self._job_count(store))
        self.assertIn("10.0.0.0/24", str(payload.get("error") or ""))

    def test_a_network_egress_is_accepted_when_actually_contained(self):
        # 反面：白名单真的覆盖得住时要放行，否则上面那条会变成"禁止写网段"。
        client, store = self._serve(["pivot"], ["10.0.0.0/24"])
        body = _bodies("10.0.0.7")["pivot"]
        body["foothold"]["host"] = "10.0.0.7"
        body["target"] = "10.0.0.7"
        body["egress"] = ["10.0.0.0/25"]
        status, payload = self._post(client, body)
        self.assertEqual(201, status, "被覆盖的网段反而被拒了：%s" % payload)

    def test_pivot_without_egress_targets_is_refused(self):
        # 没有出口目标，任务就退化成"起一个代理然后收工"：既证明不了任何事，
        # 又白担了一次开通道的风险。
        client, store = self._serve(["pivot"], [self.target])
        before = self._job_count(store)
        body = _bodies(self.target)["pivot"]
        body["egress"] = []
        status, payload = self._post(client, body)
        self.assertEqual(400, status)
        self.assertIn("egress", str(payload.get("error") or ""))
        self.assertEqual(before, self._job_count(store))

    def test_hop_beyond_the_limit_is_refused(self):
        client, store = self._serve(["pivot"], ["10.0.0.0/8"])
        before = self._job_count(store)
        body = _bodies(self.target)["pivot"]
        body["hop"] = int(authz.LIMITS["pivot"]["maxHops"]) + 1
        status, payload = self._post(client, body)
        self.assertEqual(403, status)
        self.assertIn("跳", str(payload.get("error") or ""))
        self.assertEqual(before, self._job_count(store))


class ToolkitUsesTheAdapterClassTestCase(WiringTestCase):
    """外部工具的授权类别**由适配器决定**，不由 kind 决定。

    跑 nmap 是 `toolkit` 类，跑 sqlmap 是 `exploit` 类，跑 ssh 是 `pivot` 类。
    "你用的是哪个工具"不改变"这件事有多侵入"。按 kind 一律判成 `toolkit`，
    等于让 `--allow-intrusive toolkit` 顺带放行注入工具与隧道工具——
    而用户开 toolkit 时脑子里想的是"允许我跑个 nmap"。
    """

    def test_sqlmap_is_refused_when_only_toolkit_is_open(self):
        client, store = self._serve(["toolkit"], [self.target])
        before = self._job_count(store)
        body = _bodies(self.target)["toolkit"]
        body["adapter"] = "sqlmap"
        body["toolArgs"] = ["-u", "http://%s/?id=1" % self.target]
        status, payload = self._post(client, body)
        self.assertEqual(403, status, "放行了 exploit 类的工具")
        self.assertIn("exploit", str(payload.get("error") or ""),
                      "拒绝理由要点名它落到哪一类上了")
        self.assertEqual(before, self._job_count(store))

    def test_sqlmap_is_accepted_when_exploit_is_open(self):
        client, _store = self._serve(["exploit"], [self.target])
        body = _bodies(self.target)["toolkit"]
        body["adapter"] = "sqlmap"
        body["toolArgs"] = ["-u", "http://%s/?id=1" % self.target]
        status, payload = self._post(client, body)
        self.assertEqual(201, status, "%s 被拒了" % payload)

    def test_ssh_is_refused_when_only_toolkit_is_open(self):
        client, store = self._serve(["toolkit"], [self.target])
        before = self._job_count(store)
        body = _bodies(self.target)["toolkit"]
        body["adapter"] = "ssh"
        body["toolArgs"] = ["-N", "-D", "1080", self.target]
        status, _payload = self._post(client, body)
        self.assertEqual(403, status, "ssh（pivot 类）被 toolkit 放行了")
        self.assertEqual(before, self._job_count(store))

    def test_unknown_adapter_is_refused_as_a_bad_request(self):
        client, store = self._serve(["all"], [self.target])
        before = self._job_count(store)
        body = _bodies(self.target)["toolkit"]
        body["adapter"] = "no-such-tool"
        status, payload = self._post(client, body)
        self.assertEqual(400, status)
        # 可用清单要给出来，否则使用者只知道"这个不行"而不知道"哪个行"。
        self.assertIn("nmap", str(payload.get("error") or ""))
        self.assertEqual(before, self._job_count(store))

    def test_every_adapter_class_is_a_real_authz_class(self):
        # 适配器声明的类别必须都是 authz 里真实存在的类别。拼错一个会让那
        # 工具有个永远打不开的闸——`resolve_class` 抛错，而错误出现在运行时。
        from pentestd import toolkit
        for entry in toolkit.ADAPTERS:
            with self.subTest(adapter=entry["id"]):
                self.assertIn(entry["class"], authz.CLASS_INDEX)


class AgentDispatchTestCase(unittest.TestCase):
    """执行节点的 kind 分发：三个新类型不能落到"不支持的任务类型"。"""

    def _result(self, kind, payload):
        job = {"id": "job-test", "kind": kind, "target": "10.0.0.5",
               "payload": payload}
        return agent.execute_job(job, os.getcwd(), lambda _m: None)

    def test_new_kinds_are_dispatched_not_rejected_as_unknown(self):
        cases = {
            "shell": {"hostSpec": "http://10.0.0.5/", "mode": "upload",
                      "args": {"uploadPath": "/u", "uploadField": "f"}},
            "pivot": {"foothold": {"id": "FH-1", "kind": "socks5",
                                   "host": "10.0.0.5", "port": 1080},
                      "egress": ["10.0.0.9:445"]},
            "toolkit": {"adapter": "nmap", "toolArgs": ["-V"],
                        "hostSpec": "10.0.0.5"},
        }
        for kind, payload in cases.items():
            with self.subTest(kind=kind):
                result = self._result(kind, payload)
                self.assertNotIn("不支持的任务类型", str(result.get("error") or ""),
                                 "%s 没有分发到执行逻辑" % kind)

    def test_missing_prerequisites_are_named_not_crashed(self):
        # 参数不全时必须给一句能转述的话，而不是把异常抛到控制面。
        self.assertIn("foothold", str(self._result("pivot", {}).get("error") or ""))
        self.assertIn("adapter", str(self._result("toolkit", {}).get("error") or ""))
        self.assertIn("目标", str(self._result("shell", {}).get("error") or ""))


class FootholdRegistrationTestCase(unittest.TestCase):
    """落地确认拿到的立足点要真的登记进库——否则 `pivot` 找不到出口。"""

    def setUp(self):
        self.directory, self.store = support.temporary_store("foothold.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.logs = []
        self.meta = self.store.create_engagement("10.0.0.5", "测试", "ENG-1")
        self.eid = self.meta["id"]

    def _spec(self):
        return pivot.Foothold(id="FH-1", kind="webshell", host="10.0.0.5",
                              port=8080, note="投递落地确认",
                              scope=["10.0.0.0/24"], capability="webshell",
                              created_at=1.0).to_dict()

    def _register(self, **overrides):
        result = {"status": "done", "foothold": self._spec()}
        result.update(overrides)
        job = {"id": "job-1", "engagementId": self.eid}
        agent._register_foothold(self.store, job, result, self.logs.append)

    def test_a_landed_foothold_is_registered_and_rounded_trips(self):
        self._register()
        rows = self.store.list_footholds(self.eid)
        self.assertEqual(1, len(rows))
        row = rows[0]
        self.assertEqual("FH-1", row["id"])
        self.assertEqual("webshell", row["kind"])
        # 跨模块的形状契约：读回来的东西必须能喂回 `Foothold.from_dict`。
        # 只断言"库里有一行"是不够的——字段名对不上时那一行照样在，
        # 而 `pivot` 拿到它会在解析阶段炸掉，届时报错指向的是 pivot 不是登记。
        rebuilt = pivot.Foothold.from_dict(
            {key: row.get(key) for key in
             ("id", "kind", "host", "port", "note", "scope", "tags",
              "capability", "createdAt")})
        self.assertEqual("10.0.0.0/24", rebuilt.scope[0])
        self.assertEqual("webshell", rebuilt.capability)
        self.assertTrue(any("FM" in line or "立足点" in line for line in self.logs))

    def test_a_failed_job_registers_nothing(self):
        # 失败的任务里那个字段即便非空也不可信——它来自一个没能跑完的过程。
        self._register(status="failed")
        self.assertEqual([], self.store.list_footholds(self.eid))

    def test_no_foothold_field_registers_nothing(self):
        self._register(foothold=None)
        self.assertEqual([], self.store.list_footholds(self.eid))

    def test_unconfirmed_delivery_produces_no_foothold(self):
        # **没有落地就没有立足点。** 给它一个默认值会让下游 pivot 去连一台
        # 根本没被控制的机器，而那种失败发生在另一个任务里，归因极难。
        self.assertEqual(None, agent.execute_shell(
            {"kind": "shell", "target": "", "payload": {}}, os.getcwd(),
            lambda _m: None).get("foothold"))

    def test_registration_failure_does_not_turn_the_job_into_a_failure(self):
        # 结论是"投的东西真的落在那台机器上"，这件事已经发生了；它不该因为
        # 一条记账写不进去就被记成失败。但也不能静默——记账丢了意味着下游
        # 找不到这个出口，所以日志里必须有一句。
        class _Broken:
            def add_foothold(self, *a, **k):
                raise RuntimeError("库写不进去")

        agent._register_foothold(_Broken(), {"id": "j", "engagementId": self.eid},
                                 {"status": "done", "foothold": self._spec()},
                                 self.logs.append)
        self.assertTrue(any("立足点登记失败" in line for line in self.logs))


class LabelAndTableConsistencyTestCase(unittest.TestCase):
    """三张表与一个前端映射的**跨文件**一致性。

    这一类漂移的症状永远是"某个东西显示成裸 id 或名不符实"，而两边各自的
    测试都是绿的——所以要跨文件比，不能各测各的。
    """

    @staticmethod
    def _strip_js_comments(source: str) -> str:
        """去掉 JS 注释，只留代码。

        这一步不是洁癖。断言"代码里不许出现某个写法"时，如果注释恰好引用了那个
        写法（解释它为什么被换掉），不剥注释的断言会**命中那句注释**——于是它在
        被守的代码已经改好之后仍然红着。我第一次跑这条用例就是这么栽的：失败
        信息把整份 app.js 打了出来，而命中的是我自己写的"这里曾经写成……"。
        """
        source = re.sub(r"/\*.*?\*/", "", source, flags=re.S)
        return re.sub(r"//[^\n]*", "", source)

    def test_every_job_kind_has_a_handler_a_label_and_a_policy_name(self):
        for kind in server.JOB_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, server.JOB_KIND_LABELS)
                self.assertTrue(server.JOB_KIND_LABELS[kind])

    def test_agent_dispatches_every_declared_kind(self):
        # `JOB_KINDS` 是控制面声明的可下发类型，agent 的分派表必须收得下全部。
        # 少一个的症状是那一类任务**永远**在执行节点上报"不支持的任务类型"，
        # 而控制面那边一路绿灯。
        #
        # 判据曾经是"源码里有 `kind == \"x\"` 那一行"。那守的是分发的**形状**
        # （一串 if）：换成查表、或换个等价写法，它就红，而功能一点没变——那种
        # 红训练人去改断言，而不是去看行为。改成查表之后这条用例正是这么红的，
        # 所以顺手把判据收到行为上：拿一个必然走不到网络的空载荷真调一次，
        # 看它是不是被判成"不支持"。
        handled = set(agent.JOB_HANDLERS) | {"scan"}
        for kind in server.JOB_KINDS:
            with self.subTest(kind=kind):
                self.assertIn(kind, handled, "agent 没有 %s 的分派" % kind)
                # 空目标 / 空载荷：每一类都会在自己的前置检查处返回，不会发包。
                result = agent.execute_job(
                    {"kind": kind, "target": "", "payload": {}}, "",
                    lambda _message: None)
                self.assertNotIn(
                    "不支持的任务类型", result.get("error") or "",
                    "%s 被分派拒绝了：%s" % (kind, result.get("error")))

    def test_the_ui_kind_map_covers_exactly_the_server_kinds(self):
        # 前端那张表曾经写成 `kind === 'probe' ? '网络探测' : '源码审计'`，
        # 于是所有非探测任务都被标成"源码审计"。措辞可以比服务端短，
        # 但**键不能少**：少一个的症状是那一类任务显示成裸 kind。
        source = _read(os.path.join("web", "app.js"))
        code = self._strip_js_comments(source)
        found = re.search(r"const JOB_KIND_LABEL = \{(.*?)\};", code, re.S)
        self.assertIsNotNone(found, "app.js 里找不到 JOB_KIND_LABEL")
        keys = set(re.findall(r"(\w+):", found.group(1)))
        self.assertEqual(set(server.JOB_KINDS), keys,
                         "前端任务类型表与服务端不一致（多=%s 少=%s）"
                         % (sorted(keys - set(server.JOB_KINDS)),
                            sorted(set(server.JOB_KINDS) - keys)))

    def test_no_hardcoded_kind_ternary_remains(self):
        # 按 kind 直接写三元的标签会随类型增加而静默错标，必须一次都不留。
        # 断言要收在**代码**上：注释里引用这个写法是应该的（它解释了为什么
        # 换掉），把注释算进去会让这条用例守的变成"注释里不许提这件事"。
        code = self._strip_js_comments(_read(os.path.join("web", "app.js")))
        hits = re.findall(r"kind\s*===\s*'probe'\s*\?\s*'网络探测'", code)
        self.assertEqual([], hits,
                         "还有按 kind 写死三元的标签（%d 处），加类型时会静默错标" % len(hits))

    def test_new_tools_are_classified_dispatched_and_offered(self):
        from pentestd import chat
        self.assertEqual(set(chat.HANDLERS), set(policy.TOOL_LEVEL),
                         "工具分发表与权限分级表必须严格同集合")
        offered = {tool["name"] for tool in chat.TOOLS}
        self.assertEqual(set(chat.HANDLERS), offered,
                         "能被调用的工具必须都能在工具目录里被看到")
        for name in ("deliver_payload", "open_pivot", "run_tool", "list_footholds"):
            with self.subTest(tool=name):
                self.assertIn(name, policy.TOOL_LEVEL)

    def test_intrusive_tools_are_not_reachable_from_lower_tiers(self):
        # 档位只收窄不放宽。落地确认/出口路由/外部工具都必须落在 full 档。
        for name in ("deliver_payload", "open_pivot", "run_tool"):
            with self.subTest(tool=name):
                self.assertEqual("full", policy.TOOL_LEVEL[name])
        self.assertEqual("readonly", policy.TOOL_LEVEL["list_footholds"])

    def test_every_tool_the_full_tier_offers_is_classified(self):
        # 反方向：full 档拿到的最多，它的清单里不该有未分级项。未分级的工具
        # 所有档位都拿不到（这是刻意的默认方向），所以"分级表里有、清单里也有"
        # 这一条在 full 档上必须成立。
        offered = policy.public(policy.POLICY_INDEX["full"])["tools"]
        unknown = sorted(set(offered) - set(policy.TOOL_LEVEL))
        self.assertEqual([], unknown, "full 档的工具清单里有未分级项：%s" % unknown)


class FootholdAxisTestCase(unittest.TestCase):
    """`kind` 与 `capability` 是两列，不是一回事。"""

    def test_kind_and_capability_are_separate_axes(self):
        """别把 `capability` "修"成 `kind` 的副本。

        这一条是**照着一个误报写的**。有人看到 `build_foothold("ssh", ...)` 给出
        `capability == "socks5"`，判成"ssh 立足点谎报自己是 SOCKS5，一行可修：
        `capability=capability or kind`"。那个改动是错的。

        两列回答的是不同问题：`kind` 是"这是哪一类资产"，`capability` 是"它以什么
        方式给出出口"。一台 SSH 主机用它开 SOCKS5 出口（`ssh -D`）正是最常见的那种
        用法。把默认改成 `kind`，会把"默认的出口方式"这一层含义从库里抹掉——此后
        下游分不清 `capability == "socks5"` 是**观测到的**，还是**没人说过**。

        真缺陷长的是另一个样子：参数被忽略。所以下面两半都要断言。
        """
        built = pivot.build_foothold("ssh", "10.0.0.1", 22)
        self.assertEqual("ssh", built.kind)
        self.assertEqual(pivot.DEFAULT_CAPABILITY, built.capability,
                         "立足点的默认出口方式变了；改动前请确认下游分得清"
                         "「观测到 socks5」与「默认 socks5」")
        # 显式给了就必须照收。参数被忽略才是真缺陷。
        self.assertEqual("tunnel",
                         pivot.build_foothold("ssh", "10.0.0.1", 22,
                                              capability="tunnel").capability)

    def test_producers_agree_that_the_field_survives_a_round_trip(self):
        # `shell` 产出立足点、`store` 存它、对话工具读它——三处对同一份形状。
        # 只在写侧对、读侧漏一个键，症状是"刷新之后立足点少了出口方式"。
        spec = pivot.Foothold(id="FH-r", kind="webshell", host="10.0.0.5",
                              port=80, capability="webshell").to_dict()
        rebuilt = pivot.Foothold.from_dict(spec)
        self.assertEqual("webshell", rebuilt.capability)
        self.assertEqual(spec["capability"], rebuilt.to_dict()["capability"])


class ChainSpecsAreWriteableTestCase(unittest.TestCase):
    """**每一类任务的链路产出都必须真的写得进链路。**

    这一组是照着一个后果最严重的缺陷写的。`exploit` / `creds` / `pivot` / 外部
    工具四处的 `build_chain_specs` 产出过这些值：

    * `asset.type = "web"` —— 不在 `models.ASSET_TYPES` 里
    * `goalId = "GOAL-PENTEST"` —— **字面量**，而写入器要的是 `{"$ref": "goal"}`
    * `fact.kind = "observation"` —— 不在 `models.FACT_KINDS` 里
    * `finding` 缺 `reproducibleSteps`（必填至少一条）

    它们各自的单元测试全绿，因为那些测试断言的是"结构长什么样"。真正的失败发生
    在 `server.job_result` 的 `engine.write_chain` 里：抛 `ValidationError` →
    紧跟着的 `finish_job` 永远跑不到 → **任务永远停在「执行中」，链路一个节点都
    没有**。用户看到的是"点了下发，转了一辈子"，而不是一条错误。

    "结构对"与"写得进去"是两件事，中间隔着一个 `store.add_node`——锚点校验与父
    资产校验都在那里，`models.validate` 根本看不到。所以这里一律**真写一遍**。
    """

    def setUp(self):
        self.directory, self.store = support.temporary_store("specs.db")
        self.addCleanup(self.directory.cleanup)
        self.addCleanup(self.store.close)
        self.eid = self.store.create_engagement("10.0.0.5", "测试", "ENG-1")["id"]

    # -- 五类各自的产出（都是能产出**交付项**的最坏情况） -------------

    def _exploit(self):
        return exploit.build_chain_specs({
            "target": {"name": "10.0.0.5", "base": "http://10.0.0.5/"},
            "results": [{"verdict": "confirmed_static", "template": "auth-header-bypass",
                         "label": "认证绕过"}],
            "requests": 4, "elapsed_ms": 120.0,
            "summary": {"templates": 8, "verified": 1, "static": 1,
                        "refuted": 0, "inconclusive": 6},
            "grant": {"grant": "ENG-1 授权", "targets": ["10.0.0.5"]},
            "degraded": ["没有带外回连监听，回连类判据记未判定"],
            "findings": [{"title": "认证绕过（请求头）", "severity": "critical",
                          "description": "伪造转发头即放行",
                          "steps": ["GET /admin → 200 → 绕过头生效"],
                          "evidence": {"template": "auth-header-bypass",
                                       "marker": "PTM0123456789ab",
                                       "proof": ["回显了本次生成的标记"]}}]})

    def _creds(self):
        return creds.build_chain_specs({
            "target": {"name": "10.0.0.5", "driver": "ftp", "port": 21},
            "attempts": [], "summary": {"attempts": 3, "accounts": 2,
                                        "found": 1, "lockouts": 1},
            "grant": {"grant": "ENG-1 授权", "targets": ["10.0.0.5"]},
            "found": [{"account": "admin", "password": "hunter2",
                       "fingerprint": "ab12cd34", "port": 21}],
            "stopped": [{"account": "root", "reason": "疑似锁定"}],
            "guard": {"enumeration": {"guest": "回显与其它账号不同"}}})

    def _shell(self):
        # 用 **command 模式落地**这条路径，不是 upload：它才是 `landed` 由
        # commands 组成的情形（`landed = uploads + commands` 的并集），
        # 而"立足点的 position 从 landing 记录里取"那一行只是在 upload 上试过。
        return shell.build_chain_specs({
            "target": {"name": "10.0.0.5", "base": "http://10.0.0.5/", "port": 80},
            "mode": "command", "requests": 3, "elapsed_ms": 210.0,
            "uploads": [],
            "commands": [{"verdict": "landed", "position": "cmd:1",
                          "command": "id; echo $(printf 'PTM%s' 0123456789ab)",
                          "matched": True, "output": "uid=33(www-data)"}],
            "summary": {"landed": 1, "notLanded": 0, "undetermined": 0,
                        "uploads": 0, "commands": 1},
            "grant": {"grant": "ENG-1 授权", "targets": ["10.0.0.5"]},
            "degraded": [],
            "foothold": {"id": "webshell-ab12cd34", "kind": "webshell",
                         "host": "10.0.0.5", "port": 8080,
                         "capability": "webshell", "scope": [], "tags": []}})

    def _shell_without_any_landing(self):
        """畸形输入：foothold 非空，但没有任何落地记录。

        生产路径上不会出现（`run` 只在 `landed` 非空时才给 foothold），但
        `build_chain_specs` 是公开函数。它在这里崩掉的后果与"节点形状非法"
        一模一样：整次 `write_chain` 失败、任务停在「执行中」，而报错是
        `IndexError`——离真正的原因很远。
        """
        return shell.build_chain_specs({
            "target": {"name": "10.0.0.5", "base": "http://10.0.0.5/"},
            "uploads": [], "commands": [], "summary": {},
            "foothold": {"id": "webshell-ab12cd34", "kind": "webshell",
                         "host": "10.0.0.5", "port": 8080,
                         "capability": "webshell", "scope": [], "tags": []}})

    def _pivot(self):
        return pivot.build_chain_specs({
            "foothold": {"id": "FH-1", "kind": "socks5", "host": "10.0.0.5",
                         "port": 1080, "capability": "socks5",
                         "scope": ["10.0.0.0/24"]},
            "mode": "socks5", "allow": ["10.0.0.0/24"], "elapsed_ms": 50.0,
            "egress": [{"target": "10.0.0.9:445", "host": "10.0.0.9", "port": 445,
                        "state": "open", "reason": "连上了", "detail": ""},
                       {"target": "10.0.0.8:445", "host": "10.0.0.8", "port": 445,
                        "state": "unknown", "reason": "没观测到", "detail": ""}],
            "summary": {"targets": 2, "probed": 1, "open": 1, "closed": 0,
                        "filtered": 0, "denied": 0, "unknown": 1},
            "grant": {"grant": "ENG-1 授权", "targets": ["10.0.0.5", "10.0.0.9"],
                      "hop": 0},
            "degraded": []})

    def _toolkit(self):
        # 走真实调用点，不另造一份：这两行正是 `execute_toolkit` 里那两行。
        return agent._toolkit_chain_specs({
            "adapter": "nmap", "label": "nmap", "argv": ["nmap", "-sT", "10.0.0.5"],
            "exitCode": 0, "timedOut": False, "truncated": False, "bytes": 512})

    def _all(self):
        return {"exploit": self._exploit(), "creds": self._creds(),
                "shell": self._shell(), "pivot": self._pivot(),
                "toolkit": self._toolkit()}

    # -- 断言 ----------------------------------------------------------

    def test_every_producer_writes_into_a_real_chain(self):
        for label, specs in self._all().items():
            with self.subTest(producer=label):
                written = engine.write_chain(self.store, self.eid, specs)
                total = sum(int(value or 0) for value in written.values())
                self.assertGreater(total, 0,
                                   "%s 的产出一个节点都没写进去：%s" % (label, written))

    def test_every_producer_keeps_its_shape_contract(self):
        for label, specs in self._all().items():
            with self.subTest(producer=label):
                self.assertTrue(specs, "%s 产出了空列表" % label)
                refs = set()
                for index, spec in enumerate(specs):
                    self.assertIn(spec.get("kind"),
                                  ("goal", "intent", "fact", "finding", "asset"))
                    local = spec.get("localRef")
                    self.assertTrue(local, "%s 第 %d 个 spec 没有 localRef"
                                           % (label, index + 1))
                    # `$ref` 只能指向**前面已声明**的 localRef。写入器按顺序解析，
                    # 指向前面的没问题，指向后面或干脆不存在会在写入中途炸。
                    for ref in re.findall(r'"\$ref":\s*"([^"]+)"',
                                          json.dumps(spec.get("payload"), ensure_ascii=False)):
                        self.assertTrue(ref == engine.GOAL_REF or ref in refs,
                                        "%s 第 %d 个 spec 引用了未声明的 %s"
                                        % (label, index + 1, ref))
                    refs.add(local)

    def test_no_producer_writes_a_goal_id_as_a_literal(self):
        # `goalId` 是引用不是 id。写成字面量的症状是"查不到锚点"，而它在四个模块
        # 里各犯过一次——所以这一条按**值**断言，不按模块。
        for label, specs in self._all().items():
            with self.subTest(producer=label):
                for spec in specs:
                    if spec.get("kind") != "intent":
                        continue
                    goal = (spec.get("payload") or {}).get("goalId")
                    self.assertIsInstance(goal, dict,
                                          "%s 的 intent.goalId 是字面量 %r；"
                                          "写入器要的是 {\"$ref\": \"goal\"}"
                                          % (label, goal))
                    self.assertEqual(engine.GOAL_REF, goal.get("$ref"))

    def test_every_finding_carries_reproducible_steps(self):
        # 这是 `store.add_node` 的硬性要求（至少一条），而它是**必填**的：
        # 少了它，一条已证的结论会让整次写入失败——发现了漏洞，反而什么都存不下。
        for label, specs in self._all().items():
            for spec in specs:
                if spec.get("kind") != "finding":
                    continue
                with self.subTest(producer=label):
                    steps = (spec.get("payload") or {}).get("reproducibleSteps")
                    self.assertTrue(steps, "%s 的 finding 缺 reproducibleSteps" % label)

    def test_a_consistent_but_incomplete_result_still_writes(self):
        # 畸形输入不该让整条链路写不进去。写成这样，症状（write_chain 抛错、
        # 任务卡在「执行中」）与"节点形状不合法"完全一样，而报错是 IndexError，
        # 离真正的原因很远。
        written = engine.write_chain(
            self.store, self.eid, self._shell_without_any_landing())
        self.assertGreater(sum(int(v or 0) for v in written.values()), 0)


class ChainWriteFailureMustNotStrandTheJobTestCase(unittest.TestCase):
    """链路写入失败之后，任务必须照样收尾。

    这一组守的是**失败模式**本身，不是某一个节点的形状。`write_chain` 在两条
    回传路径上都是裸调用：一次 `ValidationError`（四个引擎各犯过一次——表外的
    `asset.type`、字面量 `goalId`、表外的 `fact.kind`、缺 `reproducibleSteps`）
    会让 `finish_job` 永远跑不到，**任务永远停在「执行中」**，而页面上一处报错
    都没有。用户看到的是"点了下发，转了一辈子"——最难归因的一类失败。

    四个根因已经分别修掉，但**这个失败模式还在**：下一个引擎、下一次改枚举，
    都会再走一遍同一条路。所以它本身要有一条用例。
    """

    def _executor_with(self, result):
        directory, store = support.temporary_store("strand.db")
        self.addCleanup(directory.cleanup)
        self.addCleanup(store.close)
        eid = store.create_engagement("10.0.0.5", "测试", "ENG-1")["id"]
        job = store.create_job("probe", "10.0.0.5", eid, "", {})
        executor = agent.LocalExecutor(store, name="t", verbose=False)
        original = agent.execute_job
        agent.execute_job = lambda *_a, **_k: result
        self.addCleanup(setattr, agent, "execute_job", original)
        self.assertTrue(executor.run_once(), "没领到任务，这条用例没测到东西")
        return job["id"], store

    def test_a_result_that_cannot_be_written_still_finishes_the_job(self):
        job_id, store = self._executor_with({
            "status": "done", "summary": {"kind": "probe", "findings": 0},
            "nodes": [{"kind": "asset", "localRef": "asset:x",
                       "payload": {"type": "表外类型", "value": "x"}}]})
        after = store.get_job(job_id)
        self.assertNotEqual("running", after["status"],
                            "写入失败把任务卡在「执行中」了")
        self.assertEqual("done", after["status"],
                         "执行本身是成功的；失败的是**记录**它，两件事要分开说")
        self.assertIn("证据没能写进链路", after.get("error") or "",
                      "作业收尾了但错误没挂出来——那等于把一次丢证据静悄悄地咽了")

    def test_a_writeable_result_is_not_flagged(self):
        # 反面：正常路径不该因为这条改动而带上一个假的错误。
        job_id, store = self._executor_with({
            "status": "done", "summary": {"kind": "probe", "findings": 0},
            "nodes": []})
        after = store.get_job(job_id)
        self.assertEqual("done", after["status"])
        self.assertEqual("", after.get("error") or "")


class FlagValueParsingTestCase(unittest.TestCase):
    """部署入口手里是**字符串**，而这两个函数的签名写着 `Iterable[str]`。

    这一组照着一层比"接口没接上"更深的缺陷写：`--allow-intrusive exploit` 报
    `未知的侵入类别 'e'`（`str` 被**逐字符**迭代），而
    `--intrusive-targets 10.0.0.5` **完全不报错**——白名单变成
    `["1","0",".","0",…]` 这套单字符条目，于是每一次调用都被 fail-closed 拒掉，
    而使用者读到的理由是"目标 10.0.0.5 不在侵入白名单里（白名单：1、0、.、0…）"。
    他会去改目标、改网段、改授权说明——把时间花在一个根本不存在的问题上。
    """

    def test_a_bare_string_is_one_spec_not_many_characters(self):
        self.assertEqual(["exploit", "creds"],
                         authz.normalize_classes("exploit,creds"))
        self.assertEqual(["10.0.0.5", "10.0.0.9"],
                         authz.parse_targets("10.0.0.5,10.0.0.9"))

    def test_the_all_keyword_works_from_a_bare_string(self):
        # `all` 从命令行传进来永远是字符串。它曾经被拆成 'a'、'l'、'l'。
        self.assertEqual([item["id"] for item in authz.CLASSES],
                         authz.normalize_classes("all"))

    def test_lists_still_work(self):
        self.assertEqual(["exploit"], authz.normalize_classes(["exploit"]))
        self.assertEqual(["10.0.0.5"], authz.parse_targets(["10.0.0.5"]))

    def test_the_defaults_stay_closed(self):
        # "没配置"与"什么都能打"是两件事。修解析不能顺手把这个默认改掉。
        for value in (None, "", "none"):
            with self.subTest(value=value):
                self.assertEqual([], authz.normalize_classes(value))
        self.assertEqual([], authz.parse_targets(None))

    def test_a_misspelled_class_still_raises(self):
        # 修好之后不能顺手把"拼错"也吞掉——静默当成"没有这一类"正是这个模块
        # 最反对的事：使用者以为开了，实际没开。
        with self.assertRaises(authz.AuthzError):
            authz.normalize_classes("expliot")


class DeploymentEntryTestCase(unittest.TestCase):
    """两个部署入口都要能把侵入式动作打开。

    这一组是**照着一个真缺陷写的**：`serve()` 曾经没有 `intrusive` 形参，而
    `pentestd/cli.py` 一直在传——`python -m pentestd serve --allow-intrusive
    exploit --intrusive-targets 10.0.0.5` 一敲就在**启动时** `TypeError`。
    同时 `deploy/start.py`（项目说明 里那个"一键启动 Web 控制台"入口）压根没有
    这两个开关。

    后果是：整套侵入式执行面（引擎、对话工具、三道闸）各自的测试全绿，而
    **用户从来打不开它**。所有单元测试都不经过部署入口，所以没有一条能发现。
    """

    def test_serve_accepts_every_kwarg_the_cli_passes(self):
        import inspect
        params = set(inspect.signature(server.serve).parameters)
        source = _read(os.path.join("pentestd", "cli.py"))
        call = re.search(r"httpd = serve\((.*?)\)\n", source, re.S)
        self.assertIsNotNone(call, "cli.py 里找不到 serve( 调用")
        passed = set(re.findall(r"(\w+)=", call.group(1)))
        unknown = sorted(passed - params)
        self.assertEqual([], unknown,
                         "cli.py 传了 serve() 不收的参数（%s）。它的症状是**启动即"
                         "崩**，而所有单元测试都不经过部署入口，所以一条都发现不了"
                         % unknown)

    def test_both_deployment_entries_offer_the_intrusive_flags(self):
        # 只有一边有开关，等于"按 项目说明 走的人打不开这个能力"——而界面上一切
        # 正常（授权面板会如实报"全部关闭"），使用者只会以为自己没配对地方。
        import argparse
        import importlib.util
        module = _load_deploy_start()
        for parser in (_cli_serve_parser(), module.build_parser()):
            dests = {action.dest for action in parser._actions}
            with self.subTest(parser=parser.prog):
                self.assertIn("allow_intrusive", dests)
                self.assertIn("intrusive_targets", dests)

    def test_the_cli_serve_path_actually_starts_with_intrusive_enabled(self):
        # 这条是端到端的：真的走一遍 `serve()`，而不是只看签名。签名对了而
        # 转发漏了（`serve` 收下但没传给 `ControlPlane`）的话，开关同样没生效，
        # 而症状是"启动了，但侵入式动作永远被拒"。
        directory, store = support.temporary_store("serve-args.db")
        self.addCleanup(directory.cleanup)
        self.addCleanup(store.close)
        httpd, client = support.start(store, intrusive=["exploit"],
                                      intrusive_targets=["10.0.0.5"])
        self.addCleanup(support.stop, httpd, store, directory)
        _status, payload = client.get("/api/v1/authz")
        self.assertIn("exploit", payload.get("enabled") or [],
                      "放行没有传到控制面：开关被 serve() 收下了却没转下去")
        self.assertIn("10.0.0.5", payload.get("targets") or [])

    def test_the_selfcheck_panel_reports_the_intrusive_state(self):
        # 自检面板是使用者看部署状态的唯一入口。"全部关闭"与控制面压根没收到
        # 配置长得一模一样，不显示它，使用者看到的就是"一切正常"。
        directory, store = support.temporary_store("serve-checks.db")
        self.addCleanup(directory.cleanup)
        self.addCleanup(store.close)
        httpd, client = support.start(store)
        self.addCleanup(support.stop, httpd, store, directory)
        _status, payload = client.get("/api/v1/setup")
        ids = [item.get("id") for item in payload.get("checks") or []]
        self.assertIn("intrusive", ids, "自检面板没有报告侵入式授权状态")


# ---------------------------------------------------------------- 领得走吗

class DefaultExecutorClaimsEveryKindTestCase(WiringTestCase):
    """每一个**控制面接受得了**的任务类型，默认配置的节点都必须真的领得走。

    这一组是照着一次端到端实跑（`tools/intrusive_e2e.py`）里露出来的真缺陷写
    的，而且它比上面所有断言都更靠后一层：`DEFAULT_CAPABILITIES` 只写了
    `["scan", "probe"]`，而分派与 `JOB_KINDS` 早就是七类。`Store.claim_job`
    按 `kind IN (capabilities)` 过滤，于是**五类侵入动作一个节点都不会领走**：
    任务进了队列就停在「待执行」，没有报错、没有失败、没有日志，使用者拿到的
    唯一线索是"它不动"——那看起来像队列堵了或者节点掉线了。

    上面那些用例一条都抓不到它：它们断言的是"进得了队列"，而队列正是这件事
    发生的现场。所以要单独一组，断言的是**领取动作的返回值**。
    """

    def test_the_dispatch_table_covers_every_declared_kind(self):
        """分派表与 `JOB_KINDS` 必须一一对应，两个方向都要看。

        单向检查会漏掉"执行节点支持一个控制面永远不会下发的类型"——那个方向
        没有用户可见的症状，但它是同一处漂移的另一半。
        """
        declared = set(server.JOB_KINDS)
        handled = set(agent.JOB_HANDLERS) | {"scan"}
        self.assertEqual(set(), declared - handled,
                         "控制面接受这些类型，执行节点却没有处理函数：%s"
                         % sorted(declared - handled))
        self.assertEqual(set(), handled - declared,
                         "执行节点有处理函数，控制面却不接受这些类型：%s"
                         % sorted(handled - declared))

    def test_capabilities_are_derived_from_the_dispatch_table(self):
        """能力清单必须是分派表的派生量，不是另一份手写名单。"""
        self.assertEqual(["scan"] + sorted(agent.JOB_HANDLERS),
                         list(agent.DEFAULT_CAPABILITIES),
                         "DEFAULT_CAPABILITIES 与分派表脱钩了。两者各写一份的"
                         "后果是某一类任务永远没有节点会领——静默停摆")

    def test_a_default_executor_really_claims_every_kind(self):
        """每类任务真的建一条，再用默认配置的节点领一次。

        只比名单不够：`claim_job` 里还有一道 `kind IN (...)` 的 SQL 拼装，
        名单对了而拼错的写法照样领不到。断言的是领取的结果。
        """
        executor = agent.LocalExecutor(self.store, name="claim-check",
                                       verbose=False)
        for kind in sorted(server.JOB_KINDS):
            with self.subTest(kind=kind):
                job = self.store.create_job(kind, "127.0.0.1", "", "", {})
                self.assertEqual("pending", job["status"])
                claimed = self.store.claim_job(executor.executor_id)
                self.assertIsNotNone(
                    claimed, "%s 任务建出来了却没人领得走" % kind)
                self.assertEqual(job["id"], claimed["id"])
                self.assertEqual(kind, claimed["kind"])
                self.store.finish_job(job["id"], "done", {"status": "done"})

    def test_a_node_declaring_only_scan_does_not_steal_a_probe_job(self):
        """反方向：能力收窄必须真的收窄。

        如果为了修上面那条而把过滤整个去掉，这条会红——那时一个只声明 scan
        的旧节点会领到 probe 任务，然后以"不支持的任务类型"失败，在任务列表
        里留下一条看起来像"探测没跑通"的记录。
        """
        narrow = self.store.ensure_executor("old-node", ["scan"])
        probe_job = self.store.create_job("probe", "127.0.0.1", "", "", {})
        self.assertIsNone(self.store.claim_job(narrow),
                          "只声明 scan 的节点不该领走 probe 任务")
        self.assertEqual("pending", self.store.get_job(probe_job["id"])["status"])


class OastListenerGateTestCase(unittest.TestCase):
    """回连监听器的**可用性**闸：起得来不等于目标用得上。

    `oast.Listener` 自己不知道目标在哪里，所以它不管这件事；管它的是节点。
    缺了这道闸，最自然的写法 `{"httpPort": 8080}` 会绑 `0.0.0.0`，平台播报
    "回连监听已就绪"，然后把一条目标用不了的地址发出去——最后得到的是
    "窗口内未观测到回连"，一句关于**目标**的话。
    """

    def setUp(self):
        self.notes = []
        self.listeners = []

    def tearDown(self):
        for listener in self.listeners:
            try:
                listener.stop()
            except Exception:
                pass

    def _start(self, config):
        listener, registry, gap = agent._start_oast({"oast": config},
                                                    self.notes.append)
        if listener is not None:
            self.listeners.append(listener)
        return listener, registry, gap

    def test_wildcard_binding_without_public_host_does_not_start_a_listener(self):
        listener, registry, gap = self._start({"httpPort": 0})
        self.assertIsNone(listener, "不该起一个目标用不了的监听器")
        self.assertIsNone(registry)
        self.assertIn("publicHost", gap, gap)
        self.assertIn("0.0.0.0", gap, gap)
        self.assertTrue(any("未启动" in line for line in self.notes), self.notes)

    def test_the_wildcard_refusal_happens_before_binding(self):
        """拦在**起监听之前**，不是先绑上去再拆。

        这两条路都能得到 `listener is None`，所以只断言返回值是分不出来的。
        代价是实打实的：先绑再拆会占用一个端口、起两个线程，而"两条判定必须
        一致"这件事在那种写法下没有任何东西守着——空 `publicHost` 按
        `is_wildcard_host` 不算通配，第一道闸会放行，靠第二道兜。

        断言的是通知里的原话：`未启动` 是第一道闸，`已把它停掉` 是第二道。
        """
        self._start({"httpPort": 0})
        self.assertFalse(any("已把它停掉" in line for line in self.notes),
                         "这一支该在起监听之前就拦住：%s" % self.notes)

    def test_a_real_public_host_starts_the_listener(self):
        listener, registry, gap = self._start({"httpHost": "127.0.0.1",
                                               "httpPort": 0,
                                               "publicHost": "127.0.0.1"})
        self.assertIsNotNone(listener)
        self.assertEqual("", gap)
        self.assertTrue(any("已就绪" in line for line in self.notes), self.notes)

    def test_a_concrete_binding_host_needs_no_public_host(self):
        """绑 `127.0.0.1` 时 `token_url()` 的兜底是可行地址，不该被拦。

        这一条防的是"把闸门做成一律要求 publicHost"——那会让本机自测
        （目标与节点同机）变成一件必须配公网地址才能做的事。
        """
        listener, _registry, gap = self._start({"httpHost": "127.0.0.1",
                                                "httpPort": 0})
        self.assertIsNotNone(listener)
        self.assertEqual("", gap)
        self.assertEqual("127.0.0.1", listener.endpoint["httpHost"])

    def test_absent_oast_config_is_silent(self):
        """没写 `oast` 是常态，不是异常：不起监听，也不该发通知。"""
        listener, registry, gap = self._start({})
        self.assertEqual((None, None, ""), (listener, registry, gap))
        self.assertEqual([], self.notes)

    def test_the_gap_reaches_the_degraded_note(self):
        """闸门说的话必须走到结果里。

        只说在日志里，读结果的人看到的仍是那句笼统的"没有回连监听"，而该去修
        哪一处只有闸门知道。
        """
        _listener, _registry, gap = self._start({"httpPort": 0})
        result = exploit.run("http://127.0.0.1:1/", templates=["ssrf-callback"],
                             paths=["/open"], params=["q"],
                             grants={"grant": "ENG-TEST-0001",
                                     "enabled": ["all"], "targets": ["127.0.0.1"]},
                             callback_gap=gap)
        note = " ".join(result["degraded"])
        self.assertIn("publicHost", note, note)
        self.assertIn("ssrf-callback", note, note)


if __name__ == "__main__":
    unittest.main()
