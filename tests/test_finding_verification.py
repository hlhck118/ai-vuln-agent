# -*- coding: utf-8 -*-
"""「已证实 / 未证实」这条二分：模型层、存储自愈、逐条记录工具、产出侧。

为什么这条要单独一组用例：用户报的故障是"会话里明明发现了漏洞，漏洞页看不到"。
它同时是**三个**问题——写入通道缺（发现只留在对话正文里）、证据等级没字段
（probe 的实测与 exploit 的静态命中在库里长得一样）、页面只有一栏（读者无法
分辨"打通了"与"看起来像"）。任何一处单独修，另外两处都会让结论继续含糊：
页面分了两栏、库里却没有判据，那两栏就是猜的；有了判据、却没有记录通道，
会话里的发现依旧只能去翻聊天记录。
"""
import json
import os
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import chat, engine, exploit, models, policy, server    # noqa: E402
from pentestd.store import Store                                     # noqa: E402
from tests import support                                            # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")


def finding_payload(ident: str, *, title: str = "TLS 证书未通过校验",
                    severity: str = "low", job_id: str = "",
                    verification=None) -> dict:
    payload = {"id": ident, "intentId": "intent-1", "title": title,
               "severity": severity, "description": "证据正文",
               "reproducibleSteps": ["openssl s_client -connect host:443"]}
    if job_id:
        payload["jobId"] = job_id
    if verification is not None:
        payload["verification"] = verification
    return payload


# ---------------------------------------------------------------- 判据与分组

class VerdictTestCase(unittest.TestCase):
    """`verification` 的取值归一、判真，以及它为什么必须进分组键。"""

    def test_only_the_two_known_verdicts_are_accepted(self):
        self.assertEqual({"verdict": "verified", "method": "直接观测", "note": ""},
                         models.normalize_verification({"verdict": "verified",
                                                        "method": "直接观测"}))
        self.assertEqual("confirmed_static",
                         models.normalize_verification(
                             {"verdict": "confirmed_static"})["verdict"])
        for raw in ({}, {"verdict": ""}, {"verdict": "verifed"},
                    {"verdict": "unverified"}, {"verdict": "VERIFIED"},
                    None, "verified", ["verified"]):
            with self.subTest(raw=raw):
                self.assertIsNone(models.normalize_verification(raw),
                                  "写错的判据必须当没写，而不是折进某一类")

    def test_a_missing_verification_reads_as_unverified(self):
        self.assertFalse(models.finding_verified({}))
        self.assertFalse(models.finding_verified({"verification": {}}))
        self.assertFalse(models.finding_verified(
            {"verification": {"verdict": "confirmed_static"}}))
        self.assertTrue(models.finding_verified(
            {"verification": models.mark_verified("直接观测")}))
        self.assertFalse(models.finding_verified(
            {"verification": models.mark_static("静态匹配")}))

    def test_validate_finding_keeps_the_verdict(self):
        clean = models.validate("finding", dict(
            finding_payload("finding-1"),
            verification=models.mark_verified("直接观测", "5000 端口取回明文配置")))
        self.assertEqual("verified", clean["verification"]["verdict"])
        # 拼错的判据不许留在记录里，也不许被改成任何一类
        dirty = models.validate("finding", dict(
            finding_payload("finding-2"), verification={"verdict": "verifed"}))
        self.assertNotIn("verification", dirty)

    def test_the_two_grades_do_not_merge_into_one_group(self):
        """同一句话、两种证据等级 = 两组。

        这是"先静态命中、后来真打通"的日常形状。并成一组的话，读者看到的是
        「3 处」，而实际是"两处未证实 + 一处已证实"——恰恰把这次要分的那件事
        又糊回去了。
        """
        items = [models.validate("finding", finding_payload(
                     "finding-1", verification=models.mark_static("静态匹配"))),
                 models.validate("finding", finding_payload(
                     "finding-2", verification=models.mark_verified("直接观测")))]
        groups = models.group_findings(items)
        self.assertEqual(2, len(groups), groups)
        self.assertEqual([False, True], sorted(group["verified"] for group in groups))
        self.assertEqual(2, sum(group["count"] for group in groups))
        self.assertEqual([1, 1], [group["count"] for group in groups])

    def test_same_grade_still_merges(self):
        items = [models.validate("finding", finding_payload(
                     "finding-1", verification=models.mark_verified("直接观测"))),
                 models.validate("finding", finding_payload(
                     "finding-2", verification=models.mark_verified("认证成功应答")))]
        groups = models.group_findings(items)
        self.assertEqual(1, len(groups))
        self.assertTrue(groups[0]["verified"])
        self.assertEqual(2, groups[0]["count"])


# ---------------------------------------------------------------- 历史数据自愈

class HealTestCase(unittest.TestCase):
    """老库里的漏洞没有这个字段，启动时按**产它的任务类型**补。

    判据不是"猜哪条更可信"，而是"这类任务有没有能力当场判定"：probe 只在真的
    连上并取回结论时才写 finding，creds 只在登录被接受时写 finding。找不到 job
    的一律不补——把"不知道谁写的"标成已证实，是本模块最不该犯的错。
    """

    def setUp(self):
        self.dir, self.store = support.temporary_store("heal.db")
        self.eid = self.store.create_engagement("/srv/app")["id"]
        self.store.conn.execute(
            "INSERT INTO nodes(id, engagement_id, kind, seq, payload, created_at)"
            " VALUES(?,?,?,?,?,?)",
            ("intent-1", self.eid, "intent", 1,
             '{"title":"验证 TLS 配置","goalId":"goal-1"}', 1.0))
        self.store.conn.commit()

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _job(self, ident: str, kind: str) -> None:
        self.store.conn.execute(
            "INSERT INTO jobs(id, engagement_id, kind, target, created_at)"
            " VALUES(?,?,?,?,?)", (ident, self.eid, kind, "/srv/app", 1.0))
        self.store.conn.commit()

    def _node(self, ident: str, payload: dict) -> None:
        self.store.conn.execute(
            "INSERT INTO nodes(id, engagement_id, kind, seq, payload, created_at)"
            " VALUES(?,?,?,?,?,?)",
            (ident, self.eid, "finding", 2,
             json.dumps(payload, ensure_ascii=False), 2.0))
        self.store.conn.commit()

    def test_probe_and_creds_findings_become_verified(self):
        self._job("job-probe", "probe")
        self._job("job-creds", "creds")
        self._job("job-toolkit", "toolkit")
        self._node("finding-1", finding_payload("finding-1", job_id="job-probe"))
        self._node("finding-2", finding_payload("finding-2", job_id="job-creds"))
        self._node("finding-3", finding_payload("finding-3", job_id="job-toolkit"))
        self._node("finding-4", finding_payload("finding-4"))               # 没有 jobId
        path, self.store = self.store.path, None
        self.store = Store(path)                                             # 重开触发自愈
        found = {node["id"]: node for node in self.store.findings(self.eid)}
        self.assertEqual("verified", found["finding-1"]["verification"]["verdict"])
        self.assertEqual("verified", found["finding-2"]["verification"]["verdict"])
        self.assertEqual("confirmed_static",
                         found["finding-3"]["verification"]["verdict"])
        self.assertNotIn("verification", found["finding-4"],
                         "不知道谁写的漏洞不许猜成已证实")
        self.assertIn("probe", found["finding-1"]["verification"]["note"],
                      "自愈要把判据写清：凭什么是已证实")

    def test_healing_is_idempotent(self):
        self._job("job-probe", "probe")
        self._node("finding-1", finding_payload("finding-1", job_id="job-probe"))
        path = self.store.path
        self.store = Store(path)
        first = self.store.findings(self.eid)[0]["verification"]
        self.store.close()
        self.store = Store(path)
        self.assertEqual(first, self.store.findings(self.eid)[0]["verification"])

    def test_a_written_verdict_is_never_overwritten(self):
        """人工判过的记录不能被自愈改写。"""
        self._job("job-probe", "probe")
        self._node("finding-1", finding_payload(
            "finding-1", job_id="job-probe", verification=models.mark_static("人工复核")))
        path = self.store.path
        self.store = Store(path)
        self.assertEqual("confirmed_static",
                         self.store.findings(self.eid)[0]["verification"]["verdict"])


# ---------------------------------------------------------------- 记录通道

class RecordFindingTestCase(unittest.TestCase):
    """`record_finding`：会话里看到的发现必须能落成漏洞节点。"""

    def setUp(self):
        self.dir, self.store = support.temporary_store("record.db")
        self.plane = server.ControlPlane(self.store, WEB_DIR, auth_required=False)
        # **先有一条真会话、并且它已经绑了目标**：记录工具靠
        # `ensure_session_engagement` 找归属，而这条绑定是 `chat_sessions.engagement_id`
        # 里唯一一处"这条对话归哪个目标"的记录。测试里跳过它，每次记录都会新建一个
        # 目标——那不是被测代码的行为，而是测试没把前提摆好。
        self.eid = self.store.create_engagement("10.0.0.5", "确认暴露面")["id"]
        session = self.store.create_chat_session("测试会话", user_id="u-1",
                                                  engagement_id=self.eid)
        self.session = {"sessionId": session["id"], "userId": "u-1",
                        "username": "admin", "role": "admin"}

    def tearDown(self):
        self.store.close()
        self.dir.cleanup()

    def _call(self, **arguments):
        return chat.execute_tool(self.plane, "record_finding", arguments, "",
                                 session=self.session)

    def test_the_whole_session_writes_to_one_engagement(self):
        """同一会话里下发的任务与记下的漏洞必须落在同一个目标上。"""
        probe = chat.execute_tool(self.plane, "start_probe", {"target": "10.0.0.5"},
                                  "", session=self.session)
        self.assertTrue(probe["ok"], probe["summary"])
        self.assertEqual(self.eid, probe["data"]["engagementId"])
        self._call(title="配置接口未认证", severity="high", verified=True,
                   evidence="GET /api/config → 200")
        self.assertEqual(1, len(self.store.findings(self.eid)))

    def test_verified_needs_evidence(self):
        outcome = self._call(title="配置接口未认证", severity="high",
                             verified=True, evidence="")
        self.assertFalse(outcome["ok"], outcome["summary"])
        self.assertIn("evidence", outcome["summary"])
        self.assertEqual([], self.store.findings(self.eid),
                         "没有证据的『已证实』不许落库")

    def test_verified_lands_in_the_confirmed_column(self):
        outcome = self._call(title="配置接口未认证且返回明文口令", severity="high",
                             verified=True,
                             evidence="GET /api/config → 200，正文含 DB_PASSWORD",
                             description="无需认证即可读到数据库口令",
                             steps=["curl -s http://10.0.0.5:5000/api/config"])
        self.assertTrue(outcome["ok"], outcome["summary"])
        rows = self.store.findings(self.eid)
        self.assertEqual(1, len(rows))
        self.assertEqual("verified", rows[0]["verification"]["verdict"])
        self.assertIn("无需认证", rows[0]["description"])
        self.assertIn("DB_PASSWORD", rows[0]["verification"]["note"],
                      "证据要落在判据里：界面上「依据」那一行读的就是它")
        self.assertEqual(1, len(rows[0]["reproducibleSteps"]))
        self.assertIn("已证实", outcome["summary"])

    def test_unverified_lands_in_the_pending_column(self):
        outcome = self._call(title="疑似路径遍历", severity="medium",
                             verified=False, evidence="200 响应里出现 ../")
        self.assertTrue(outcome["ok"], outcome["summary"])
        rows = self.store.findings(self.eid)
        self.assertEqual("confirmed_static", rows[0]["verification"]["verdict"])
        self.assertIn("未证实", outcome["summary"])

    def test_a_bad_severity_is_refused_by_name(self):
        outcome = self._call(title="随便", severity="severe")
        self.assertFalse(outcome["ok"])
        self.assertIn("severity", outcome["summary"])
        self.assertEqual([], self.store.findings(self.eid))

    def test_an_asset_from_another_engagement_is_refused(self):
        other = self.store.create_engagement("/srv/other")["id"]
        self.store.add_node(other, "intent", {"title": "别处的意图", "goalId": "goal-1"})
        asset = self.store.add_node(other, "asset",
                                    {"type": "service", "value": "22/tcp ssh"})
        outcome = self._call(title="跨目标引用", severity="low", assetId=asset["id"])
        self.assertFalse(outcome["ok"], outcome["summary"])
        self.assertIn("assetId", outcome["summary"])

    def test_it_is_a_probe_tier_tool(self):
        self.assertEqual("probe", policy.TOOL_LEVEL["record_finding"])
        self.assertFalse(policy.permits("readonly", "record_finding"))
        self.assertTrue(policy.permits("probe", "record_finding"))


# ---------------------------------------------------------------- 产出侧

class ProducerTestCase(unittest.TestCase):
    """自动产出的漏洞也要带判据，否则页面的两栏只是把同一堆记录切了一刀。"""

    def test_probe_findings_are_verified_and_say_where(self):
        specs = engine.build_probe_chain_specs({
            "target": {"name": "10.0.0.5"},
            "hosts": [{"host": "10.0.0.5", "open": [{"port": 6379}]}],
            "findings": [{"title": "Redis 未授权访问", "severity": "high",
                          "host": "10.0.0.5", "port": 6379,
                          "description": "未认证即可读写", "evidence": {"PING": "PONG"},
                          "steps": ["redis-cli -h 10.0.0.5 ping"]}],
        })
        findings = [spec for spec in specs if spec["kind"] == "finding"]
        self.assertEqual(1, len(findings))
        self.assertEqual("verified", findings[0]["payload"]["verification"]["verdict"])
        self.assertIn("10.0.0.5:6379", findings[0]["payload"]["verification"]["note"])

    def test_static_leads_become_unverified_findings(self):
        spec = {"verdict": "confirmed_static", "template": "auth-header-bypass",
                "label": "认证头绕过", "severity": "medium",
                "target": "http://10.0.0.5/abort",
                "steps": [{"verdict": "confirmed_static", "request": "GET /abort",
                           "response": "200", "reason": "受保护路径返回 200"}]}
        leads = exploit.build_static_findings([spec])
        self.assertEqual(1, len(leads))
        self.assertEqual("confirmed_static", leads[0]["verification"]["verdict"])
        specs = exploit.build_chain_specs({"target": {"name": "10.0.0.5"},
                                           "results": [spec]})
        written = [item for item in specs if item["kind"] == "finding"]
        self.assertEqual(1, len(written), "静态命中要进漏洞台账")
        self.assertEqual("confirmed_static",
                         written[0]["payload"]["verification"]["verdict"])
        # 待复核的那条 fact 仍然留着：事实读作"我看到了什么"，漏洞读作"这算不算问题"
        observed = [item for item in specs if item["kind"] == "fact"
                    and str(item.get("localRef") or "").startswith("fact:observe")]
        self.assertEqual(1, len(observed))


if __name__ == "__main__":
    unittest.main()
