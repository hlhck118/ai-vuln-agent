# -*- coding: utf-8 -*-
"""任务报告：一次执行一篇，写的是那一次跑出了什么。

## 守的是什么

报告此前只有**一个粒度**——一个目标一篇，读全量链路。它能列出全部漏洞，却说不出
其中哪一条是这次扫描跑出来的，也说不出"这台机器上的十条漏洞里，有两条来自一次
跑失败的扫描"。判断一份报告可信度的第一件事恰恰就是那个：这些结论是谁跑出来的、
跑了几次、有没有一次失败的重跑把东西写脏了。

所以这一轮补了两样东西：

  * **溯源**：链路节点记下产出它的任务号（`jobId`）。没有它，"哪一次跑出来的"这个
    问题在数据层就没有答案，报告再好也只是把汇总换个说法；
  * **任务报告**：`GET /engagements/{eid}/report?job=...`，一次执行一篇。

## 三处最容易错、而且都不会报错的地方

  * **`jobId` 被校验函数丢掉**：四个 `validate_*` 返回的都是**封闭**字典，表外键
    一律丢弃。只在 `write_chain` 里把 `jobId` 塞进 payload 是不够的——它会在
    `models.validate` 那一步静默消失，症状是"任务报告里一个节点都没有"，而汇总
    报告里它们照样在，所以没人会发现。
  * **参数名对不上**：前端发 `?job=`、后端读 `job`。对不上时后端忽略它，照常返回
    **汇总报告**——一篇格式完整、内容合理的文档，只是不是你要的那一篇。
  * **一个节点都没写进来**：那可能是"跑失败了"、"还在排队"、"跑通了但没发现东西"
    三种完全不同的情况，读者要采取的行动也完全不同。笼统一句"本次未写入节点"会让
    第三种看起来像第二种。
"""

import os
import re
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import engine, models                       # noqa: E402
from pentestd import server as server_module              # noqa: E402
from pentestd import store as store_module                # noqa: E402
from pentestd.store import Store                          # noqa: E402
from tests import support                                 # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

SPEC_A = [
    {"kind": "intent", "localRef": "intent:recon",
     "payload": {"title": "网络暴露面测绘", "goalId": "goal-1"}},
    {"kind": "asset", "localRef": "asset:root",
     "payload": {"type": "ip", "value": "10.10.10.250", "meta": "存活 22/80"}},
    {"kind": "fact",
     "payload": {"intentId": {"$ref": "intent:recon"}, "kind": "port",
                 "detail": "10.10.10.250 开放 22/80", "confidence": 0.9}},
]


def spec_b(parent_id):
    """第二个任务写进来的那几条。

    `parentId` 用**字面 id** 而不是 `$ref`：`$ref` 只在一次 `write_chain` 内部解析，
    所以真实的写入器之间不会这样引用彼此。但 `/engagements/{id}/nodes` 那条接口允许
    调用方直接给字面 parentId——所以这条路径存在，任务报告必须能把它读通。
    """
    return [
        {"kind": "intent", "localRef": "intent:verify",
         "payload": {"title": "配置判定", "goalId": "goal-1"}},
        {"kind": "asset",
         "payload": {"type": "service", "value": "10.10.10.250:80",
                     "parentId": parent_id}},
        {"kind": "finding",
         "payload": {"intentId": {"$ref": "intent:verify"}, "title": "目录可列出",
                     "severity": "medium",
                     "reproducibleSteps": ["打开 http://10.10.10.250/"],
                     "affectedAssetId": "asset-2"}},
    ]


class JobProvenanceTestCase(unittest.TestCase):
    """节点上的 `jobId`：写上去了、没被丢掉、而且提交方说了不算。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "prov.db"))
        self.eid = self.store.create_engagement("10.10.10.250")["id"]

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _jobs_of(self, kind):
        return [n["payload"].get("jobId") for n in self.store.list_nodes(self.eid, kind)]

    def test_every_written_node_carries_the_job_id(self):
        """四类内容节点全部要带上——漏一类就是"那一类在任务报告里看不见"。"""
        job = self.store.create_job("probe", "10.10.10.250", self.eid, "", {})
        engine.write_chain(self.store, self.eid, SPEC_A, job_id=job["id"])
        for kind in ("intent", "fact", "asset"):
            self.assertEqual(self._jobs_of(kind), [job["id"]],
                             "%s 节点没有记下产出的任务号：任务报告里会少了它"
                             % kind)
        job2 = self.store.create_job("scan", "/srv/x", self.eid, "", {})
        engine.write_chain(self.store, self.eid, spec_b("asset-1"), job_id=job2["id"])
        self.assertEqual(self._jobs_of("finding"), [job2["id"]])

    def test_a_write_without_a_job_id_records_none(self):
        """不传 job_id 就**不带这个键**——不能拿一个"上一次"的任务号来填。

        这一支是旧调用方（脚本、`/engagements/{id}/nodes`）走的路。给它编一个
        jobId 会让任务报告里出现"这次跑出来的"实际是别的进程写的节点。

        而且必须是**不带这个键**而不是空串：`public_node` 把 payload 摊平到节点上，
        节点详情面板逐个字段展示，一个恒为 `""` 的 `jobId` 会在每一个节点上都多出
        一行「jobId —」。
        """
        engine.write_chain(self.store, self.eid, SPEC_A)
        self.assertEqual(self._jobs_of("intent"), [None])

    def test_the_caller_cannot_forge_the_provenance(self):
        """调用方在 payload 里预先塞的 `jobId` 会被**覆盖**，不是被采纳。

        否则"这个节点是谁写的"就成了提交方能决定的事，而溯源的全部意义正是它不由
        提交方决定。
        """
        job = self.store.create_job("probe", "10.10.10.250", self.eid, "", {})
        forged = [dict(SPEC_A[0])]
        forged[0] = {"kind": "intent",
                     "payload": {"title": "伪造归属", "goalId": "goal-1",
                                 "jobId": "job-别人"}}
        engine.write_chain(self.store, self.eid, forged, job_id=job["id"])
        self.assertEqual(self._jobs_of("intent"), [job["id"]])

    def test_the_goal_node_has_no_provenance(self):
        """目标节点不是任何一次执行的产出，因此没有 `jobId`。

        给它加一个恒为空串的字段，只会让"这个字段是不是漏填了"变成每次读报告都要
        重新判断一次的问题。
        """
        goal = self.store.get_node(self.eid, "goal-1")
        self.assertNotIn("jobId", goal["payload"])

    def test_the_validators_keep_job_id_for_every_kind(self):
        """四个校验函数都要保留 `jobId`。

        它们返回的是**封闭**字典（表外键一律丢弃），所以少写一个 `"jobId"` 的症状是
        那一类节点的归属**静默消失**——而 `write_chain` 看起来完全正常。
        """
        cases = {
            "intent": {"title": "t", "goalId": "goal-1", "jobId": "job-1"},
            "fact": {"intentId": "intent-1", "detail": "d", "jobId": "job-1"},
            "finding": {"intentId": "intent-1", "title": "t",
                        "reproducibleSteps": ["s"], "jobId": "job-1"},
            "asset": {"type": "ip", "value": "1.2.3.4", "jobId": "job-1"},
        }
        for kind, payload in cases.items():
            self.assertEqual(models.validate(kind, payload).get("jobId"), "job-1",
                             "%s 的校验函数把 jobId 丢了" % kind)


class TaskReportTestCase(unittest.TestCase):
    """任务报告的正文：主语是这次执行，不是这个目标。"""

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "report.db"))
        self.eid = self.store.create_engagement(
            "10.10.10.250", "内网自查", "AUTH-TEST-1")["id"]
        self.job = self.store.create_job("probe", "10.10.10.250", self.eid, "", {})
        engine.write_chain(self.store, self.eid, SPEC_A, job_id=self.job["id"])
        self.store.finish_job(self.job["id"], "done", {
            "summary": {"hosts": 1, "openPorts": 2, "elapsedMs": 4321},
            "written": {"intent": 1, "fact": 1, "finding": 0, "asset": 1},
            "agents": [], "chainError": "", "error": ""})
        self.job2 = self.store.create_job("scan", "/srv/app", self.eid,
                                          "/srv/app.patched", {})
        engine.write_chain(self.store, self.eid, spec_b("asset-1"),
                           job_id=self.job2["id"])
        self.store.finish_job(self.job2["id"], "failed", {
            "summary": {}, "written": {"intent": 1, "fact": 0, "finding": 1, "asset": 1},
            "agents": [{"role": "audit", "label": "语义审计智能体", "duty": "切片",
                        "actions": 3, "elapsedMs": 2100, "state": "done",
                        "summary": "命中 1 条"}],
            "chainError": "", "error": "patched 目录不存在"})

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    # ------------------------------------------------------------- 汇总

    def test_the_aggregate_report_lists_every_execution(self):
        """汇总报告要有一节执行记录。

        原先它完全没有这一节：能列出全部漏洞，却说不出这些结论是谁跑出来的。
        数据一直在 `jobs` 表里，只是从来没进过报告。
        """
        markdown = self.store.report_markdown(self.eid)
        self.assertIn("## 二、执行记录", markdown)
        for job in (self.job, self.job2):
            self.assertIn(job["id"], markdown, "执行记录里少了 %s" % job["id"])
        self.assertIn("网络探测", markdown)
        self.assertIn("已完成", markdown)
        self.assertIn("失败", markdown)

    def test_the_aggregate_report_says_who_the_target_and_authorization_are(self):
        """「目标与授权」那一节必须真读到目标节点。

        `windowed_view()["goal"]` 是**原始行**，要 `models.public_node` 摊平才能读
        `.target`。漏了它那张表三行恒为 `-` / `未填写`——而报告抬头读的是
        `engagements` 表，同样的信息印得整整齐齐，所以那一节看起来只是"这几项没填"。
        一张写着自己没填授权的表，比不印那张表更误导。
        """
        markdown = self.store.report_markdown(self.eid)
        table = markdown[markdown.index("## 一、目标与授权"):markdown.index("## 二、")]
        self.assertIn("| 目标 | 10.10.10.250 |", table)
        self.assertIn("| 授权说明 | AUTH-TEST-1 |", table)
        self.assertNotIn("| 授权说明 | 未填写 |", table)

    # ----------------------------------------------------------- 任务报告

    def test_the_task_report_is_about_that_one_execution(self):
        markdown = self.store.report_markdown(self.eid, 200, self.job["id"])
        self.assertIn("# 任务报告", markdown)
        self.assertIn(self.job["id"], markdown)
        self.assertNotIn("`/srv/app`", markdown, "把别的任务的目标也写进来了")
        # 本次写进来的三类都在
        self.assertIn("intent-1", markdown)
        self.assertIn("fact-1", markdown)
        # 事实那一行的子类型印中文：`[端口]` 而不是 `[port]`，也不是结构类型 `[fact]`
        self.assertIn("[端口] 10.10.10.250 开放 22/80", markdown)
        self.assertNotIn("[fact]", markdown, "事实那一格印的是结构类型，子类型丢了")
        self.assertIn("asset-1", markdown)
        # 第二个任务写的东西一个都不该出现
        self.assertNotIn("intent-2", markdown, "任务报告混进了别的任务的节点")
        self.assertNotIn("finding-1", markdown, "任务报告混进了别的任务的漏洞")

    def test_the_task_report_resolves_the_affected_asset_beyond_its_own_slice(self):
        """漏洞的"影响资产"要能指名道姓，哪怕那条资产是别的任务登记的。

        只在本批次里查名字的话，这一行会退化成裸 id（`asset-2`），而读者要的是
        "打到了哪台机器"。
        """
        markdown = self.store.report_markdown(self.eid, 200, self.job2["id"])
        # 类型印中文：这一列原先写 `service …`，而同一份报告里资产树写「服务」
        self.assertIn("服务 10.10.10.250:80", markdown)

    def test_a_borrowed_parent_asset_is_marked(self):
        """上游节点必须一并列出，而且要带 `*` 记号。

        资产树按 `parentId` 递归渲染。只带自己那几条时，一条父主机不在本批次里的
        服务资产**既不是根、其父也不在列表里**——它一行都不会出现，而报告读起来
        完全正常，只是少了几条资产。
        """
        markdown = self.store.report_markdown(self.eid, 200, self.job2["id"])
        self.assertIn("asset-1", markdown, "父资产没有一并列出：那条服务资产会整个消失")
        self.assertIn("* [IP] 10.10.10.250", markdown, "上游节点没有打上记号")
        self.assertIn("不是本次任务登记的", markdown, "没有说明那个记号是什么意思")
        # 本次自己登记的**不**该带记号；类型印中文，不印库里的 `ip` / `service`
        self.assertIn("  - [服务] 10.10.10.250:80", markdown)

    def test_the_task_report_names_the_foothold_in_chinese(self):
        """「本次任务登记的立足点」那一行也要说中文。

        原先印的是 `[socks5] 10.0.0.5:1080 — open`：同一份报告里「严重程度：高危」
        是中文，这一行是英文，读的人只会觉得这一处没翻译。词表在 `pivot` 里
        （种类本来就由那个模块定义），报告只是取用。

        注意 `open` 在这一行说的是**这条立足点还活着**，与出口验证里的三态
        （open/closed/filtered）不是同一个量——所以它翻成「存活」而不是「开放」。
        """
        self.store.add_foothold(self.eid, {"kind": "socks5",
                                         "host": "10.10.10.250", "port": 1080,
                                         "note": "落地确认", "scope": ["10.20.0.0/16"]},
                               self.job2["id"])
        markdown = self.store.report_markdown(self.eid, 200, self.job2["id"])
        self.assertIn("[SOCKS5 出口] 10.10.10.250:1080 — 存活", markdown)
        self.assertNotIn("[socks5]", markdown, "报告把库里的种类原样印出来了")
        self.assertNotIn("— open", markdown, "报告把库里的状态原样印出来了")

    def test_the_summary_drops_keys_that_already_have_a_home(self):
        """抬头已有的项不再在摘要里出现第二遍。

        `elapsedMs` 最典型：抬头那行是控制面记的墙钟时间，摘要里是执行节点自己报的，
        两个不同的数字顶着同一个标签，读的人只会觉得其中一个写错了。
        """
        markdown = self.store.report_markdown(self.eid, 200, self.job["id"])
        summary = markdown[markdown.index("## 一、结果摘要"):markdown.index("## 二、")]
        self.assertIn("**开放端口**：2", summary)
        self.assertNotIn("耗时", summary, "摘要里又列了一遍耗时")
        self.assertNotIn("runId", summary)
        self.assertNotIn("target", summary)

    def test_nothing_written_says_which_of_the_three_cases_it_is(self):
        """一个节点都没写进来时，三句话必须分得开。

        `failed` / `pending` / `done` 三种情况读者要采取的行动完全不同：查原因、
        等着、把这份报告当结论。笼统一句"本次未写入节点"会让第三种看起来像第二种。
        """
        words = {}
        for status in ("failed", "pending", "done"):
            job = self.store.create_job("probe", "10.10.10.251", self.eid, "", {})
            if status != "pending":
                self.store.finish_job(job["id"], status, {"summary": {}, "written": {}})
            markdown = self.store.report_markdown(self.eid, 200, job["id"])
            words[status] = markdown[markdown.index("## 二、"):markdown.index("## 三、")]
        self.assertIn("失败", words["failed"])
        self.assertIn("还没有跑完", words["pending"])
        self.assertIn("确定的结论", words["done"])
        self.assertNotEqual(words["done"], words["pending"])

    def test_a_failed_job_still_prints_its_reason(self):
        markdown = self.store.report_markdown(self.eid, 200, self.job2["id"])
        self.assertIn("patched 目录不存在", markdown)

    def test_the_chain_write_error_is_called_out(self):
        """`chainError` 要显式说出来，不能只写在任务列表里。

        它意味着"产出了证据但没写进链路"——报告里那些数字因此比实际发现的东西少。
        不说的话，读者会拿一份残缺的清单当完整的。
        """
        job = self.store.create_job("probe", "10.10.10.252", self.eid, "", {})
        self.store.finish_job(job["id"], "done", {
            "summary": {}, "written": {},
            "chainError": "证据没能写进链路：ValidationError: asset.type 非法"})
        markdown = self.store.report_markdown(self.eid, 200, job["id"])
        self.assertIn("链路写入错误", markdown)
        self.assertIn("asset.type 非法", markdown)
        self.assertIn("比实际发现的东西少", markdown)

    def test_a_missing_or_mismatched_job_is_refused(self):
        """不存在的任务、以及"任务属于别的目标"都必须报错。

        照着空集合渲染会得到一篇**看起来正常、实则全空**的报告，而读者会以为那次
        任务什么都没发现。
        """
        with self.assertRaises(KeyError):
            self.store.report_markdown(self.eid, 200, "job-不存在")
        other = self.store.create_engagement("别处")["id"]
        with self.assertRaises(ValueError):
            self.store.report_markdown(other, 200, self.job["id"])

    def test_jobs_of_filters_by_engagement(self):
        other = self.store.create_engagement("别处")["id"]
        self.store.create_job("probe", "别处", other, "", {})
        self.assertEqual({j["id"] for j in self.store.jobs_of(self.eid)},
                         {self.job["id"], self.job2["id"]})

    def test_the_job_row_exposes_what_it_wrote_and_why_it_failed(self):
        """`written` 与 `chainError` 要解析出来。

        它们一直在 `jobs.result` 那份 JSON 里，只是从来没被解析——于是任务列表上
        一条"证据没能写进链路"的任务，看起来和一条正常完成的一模一样（都写着
        「已完成」），而报告页左侧的目录要靠它显示证据条数。
        """
        row = self.store.get_job(self.job["id"])
        self.assertEqual(row["written"]["intent"], 1)
        self.assertEqual(row["chainError"], "")
        failed = self.store.get_job(self.job2["id"])
        self.assertEqual(failed["written"]["finding"], 1)
        self.assertEqual(failed["error"], "patched 目录不存在")


class TaskReportRouteTestCase(unittest.TestCase):
    """HTTP 层：`/report` 与 `/report?job=` 是**两篇不同的文档**。"""

    @classmethod
    def setUpClass(cls):
        cls.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        cls.store = Store(os.path.join(cls.tmp.name, "route.db"))
        cls.eid = cls.store.create_engagement("10.10.10.250")["id"]
        cls.job = cls.store.create_job("probe", "10.10.10.250", cls.eid, "", {})
        engine.write_chain(cls.store, cls.eid, SPEC_A, job_id=cls.job["id"])
        cls.store.finish_job(cls.job["id"], "done",
                             {"summary": {"hosts": 1}, "written": {"intent": 1}})
        cls.other_job = cls.store.create_job("scan", "/srv/y", cls.eid, "", {})
        cls.httpd, cls.client = support.start(cls.store, WEB_DIR, admin=True)

    @classmethod
    def tearDownClass(cls):
        support.stop(cls.httpd, cls.store, cls.tmp)

    def test_without_a_job_it_is_the_aggregate(self):
        status, payload = self.client.get("/api/v1/engagements/%s/report" % self.eid)
        self.assertEqual(status, 200)
        self.assertEqual(payload["jobId"], "")
        self.assertIn("# 渗透测试报告", payload["markdown"])

    def test_with_a_job_it_is_that_task_report(self):
        status, payload = self.client.get(
            "/api/v1/engagements/%s/report?job=%s" % (self.eid, self.job["id"]))
        self.assertEqual(status, 200)
        self.assertEqual(payload["jobId"], self.job["id"])
        self.assertEqual(payload["job"]["id"], self.job["id"])
        self.assertIn("# 任务报告", payload["markdown"])
        self.assertNotIn("# 渗透测试报告", payload["markdown"])

    def test_the_two_documents_really_differ(self):
        """参数被忽略时返回的是**汇总报告**——一篇格式完整、内容合理的文档。

        所以必须断言两者不同：只断言"200 且不是空的"的话，参数名写错、后端不认它
        这条路径会完全通过。
        """
        _, aggregate = self.client.get("/api/v1/engagements/%s/report" % self.eid)
        _, task = self.client.get(
            "/api/v1/engagements/%s/report?job=%s" % (self.eid, self.job["id"]))
        self.assertNotEqual(aggregate["markdown"], task["markdown"])

    def test_an_unknown_job_is_404(self):
        # 用纯 ASCII 的任务号：`http.client` 会把请求行按 ascii 编码，路径里带中文
        # 会在**发出去之前**就抛 UnicodeEncodeError，而那看起来像测试写错了。
        status, payload = self.client.get(
            "/api/v1/engagements/%s/report?job=job-deadbeef" % self.eid)
        self.assertEqual(status, 404)
        self.assertIn("任务不存在", payload["error"])

    def test_a_job_of_another_engagement_is_400(self):
        """400 而不是 404:任务确实存在，错的是这一对组合。

        回 404 会让人去查一个明明在任务列表里躺着的 id。
        """
        other = self.store.create_engagement("别处")["id"]
        status, payload = self.client.get(
            "/api/v1/engagements/%s/report?job=%s" % (other, self.job["id"]))
        self.assertEqual(status, 400)
        self.assertIn("不属于目标", payload["error"])

    def test_the_two_argument_call_sites_still_work(self):
        """`report_markdown(eid, window)` 这个两参数写法必须继续有效。

        `/export`、`/svg` 与 CLI 都是这么调的。把新增的 `job_id` 插到 `limit` 前面
        会让那些调用点**静默地传错**——`window`（一个整数）变成"任务号"，`limit`
        保持默认，于是导出里少掉较早的记录，而它看起来只是"导出得不太一样"。
        """
        markdown = self.store.report_markdown(self.eid, 50)
        self.assertIn("# 渗透测试报告", markdown)
        self.assertIn("## 二、执行记录", markdown)


class VocabularyAgreementTestCase(unittest.TestCase):
    """报告与界面说的是同一套词。

    报告是中文文档，而任务类型 / 状态 / 结果摘要的中文名两边各有一份。分叉的症状不是
    报错，是同一类任务在报告里叫「应用层利用验证」、在界面上叫「应用层利用」——
    看的人只会以为自己看错了。
    """

    @classmethod
    def setUpClass(cls):
        with open(os.path.join(WEB_DIR, "app.js"), encoding="utf-8") as handle:
            cls.app = handle.read()

    @staticmethod
    def _js_object(js, name):
        start = js.index("const %s = {" % name)
        depth, index = 0, js.index("{", start)
        body = js[index:]
        for offset, char in enumerate(body):
            if char == "{":
                depth += 1
            elif char == "}":
                depth -= 1
                if depth == 0:
                    return body[:offset + 1]
        raise AssertionError("找不到 %s 的定义" % name)

    @classmethod
    def _js_keys(cls, name):
        import re
        return set(re.findall(r"([A-Za-z_][\w]*)\s*:", cls._js_object(cls.app, name)))

    def test_kind_labels_are_one_table(self):
        """`server.JOB_KIND_LABELS` 就是 `store.KIND_LABELS`，不是同名副本。

        两份字面量的失败形态是同一类任务在报告里叫「应用层利用验证」、在界面上叫
        「应用层利用」——而它们说的是同一件事，看的人只会以为自己看错了。
        """
        self.assertIs(server_module.JOB_KIND_LABELS, store_module.KIND_LABELS,
                      "服务端与报告各存了一份任务类型表：改一处漏一处就会分叉")

    def test_the_kind_table_covers_every_declared_kind(self):
        self.assertEqual(set(server_module.JOB_KIND_LABELS), set(server_module.JOB_KINDS),
                         "有一类任务没有中文名——报告里会显示成裸 id")
        self.assertEqual(set(store_module.KIND_LABELS), set(server_module.JOB_KINDS))

    def test_kind_keys_agree_with_the_frontend(self):
        self.assertEqual(self._js_keys("JOB_KIND_LABEL"),
                         set(server_module.JOB_KIND_LABELS),
                         "界面的任务类型键集与服务端不一致：漏一个键的症状是那一类"
                         "显示成裸 id")

    def test_status_keys_agree_with_the_frontend(self):
        self.assertEqual(self._js_keys("JOB_STATUS_LABEL"),
                         set(store_module.TASK_STATUS_LABELS),
                         "报告与界面的任务状态词表对不上")

    def test_summary_keys_agree_with_the_frontend(self):
        front = self._js_keys("SUMMARY_LABEL")
        back = set(store_module.SUMMARY_LABELS)
        # 报告是**超集**：它要覆盖全部任务类型，而界面那份是各类型共用的短表。
        self.assertEqual(front - back, set(),
                         "报告缺了这些摘要键的中文名：%s" % sorted(front - back))

    def test_the_omit_list_agrees_with_the_frontend(self):
        """报告里不列的那几项，与界面摘要里跳过的是同一批。

        对不上的症状是：同一条摘要，界面上干净、报告里多出 `runId` 与 `target`
        ——而那两个在抬头里已经有各自的位置了。
        """
        found = re.search(r"const SUMMARY_SKIP = \[([^\]]*)\]", self.app)
        self.assertIsNotNone(found, "app.js 里找不到 SUMMARY_SKIP")
        front = set(re.findall(r"'([^']+)'", found.group(1)))
        self.assertEqual(front, set(store_module.SUMMARY_OMIT))


if __name__ == "__main__":
    unittest.main(verbosity=2)
