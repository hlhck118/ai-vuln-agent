# -*- coding: utf-8 -*-
"""计划任务：一次性或按周的时间窗口。

窗口计算（跨午夜、时区、「这个窗口跑过没有」）全在 pentestd/schedules 里，store 只
负责存取。这一组用例钉两件事：人配错了要在保存那一步被拦住（而不是等窗口不生效
再回来查），以及窗口开启恢复任务、窗口结束暂停任务这条唯一的副作用。
"""
import datetime
import unittest
from urllib.parse import quote

from tests import support
from pentestd import auth, schedules


def _today():
    return datetime.date.today().isoformat()


def _values(items):
    """下拉选项一律是 {value, label}；这个 helper 也接受纯字符串，免得断言被
    形状变化绊住（形状本身另有接口用例钉）。"""
    return [item if isinstance(item, str) else item.get("value")
            for item in items]


def _payload(**over):
    body = {"name": "夜间放行", "rule": "once", "timezone": "beijing",
            "startDate": _today(), "startTime": "00:00", "endTime": "23:59",
            "enabled": True}
    body.update(over)
    return body


def _engagement(store, target="10.10.10.110"):
    return store.create_engagement(target, "拿下控制台", "本人授权")["id"]


class SchedulesTests(unittest.TestCase):
    def setUp(self):
        self.directory, self.store = support.temporary_store("schedules.db")
        self.httpd, self.client = support.start(self.store)

    def tearDown(self):
        support.stop(self.httpd, self.store, self.directory)

    def _operator_client(self):
        self.store.create_user("operator", auth.hash_password("Operator-Pass1!"),
                              role="operator")
        base = "http://127.0.0.1:%d" % self.httpd.server_address[1]
        client = support.Client(base)
        status, payload = client.login("operator", "Operator-Pass1!")
        self.assertEqual(200, status, payload)
        return client

    def _create(self, **over):
        status, payload = self.client.post("/api/v1/schedules",
                                          _payload(**over))
        self.assertEqual(201, status, payload)
        return payload["schedule"]

    def test_requires_session(self):
        directory, store = support.temporary_store("schedules-anon.db")
        httpd, client = support.start(store, admin=False)
        try:
            status, payload = client.get("/api/v1/schedules")
            self.assertEqual(401, status, payload)
        finally:
            support.stop(httpd, store, directory)

    def test_empty_catalog_carries_the_option_tables(self):
        status, payload = self.client.get("/api/v1/schedules")
        self.assertEqual(200, status, payload)
        self.assertEqual([], payload["schedules"])
        self.assertEqual(0, payload["counts"]["total"])
        # 下拉框的取值也由后端给：前端自己列一遍，加一个时区就要改两个文件。
        self.assertEqual(["once", "weekly"], _values(payload["rules"]))
        self.assertIn("beijing", _values(payload["timezones"]))
        self.assertEqual(["waiting", "running", "outside", "completed", "error"],
                         _values(payload["statuses"]))

    def test_empty_name_is_rejected(self):
        status, payload = self.client.post("/api/v1/schedules",
                                          _payload(name="   "))
        self.assertEqual(400, status, payload)
        self.assertIn("计划名称", payload.get("error") or "")

    def test_rule_is_required(self):
        status, payload = self.client.post("/api/v1/schedules",
                                          _payload(rule=""))
        self.assertEqual(400, status, payload)
        self.assertTrue(payload.get("error"))

    def test_weekly_needs_a_weekday(self):
        status, payload = self.client.post(
            "/api/v1/schedules", _payload(rule="weekly", weekdays=[]))
        self.assertEqual(400, status, payload)
        self.assertIn("星期", payload.get("error") or "")

    def test_bad_clock_is_rejected(self):
        status, payload = self.client.post("/api/v1/schedules",
                                          _payload(startTime="25:00"))
        self.assertEqual(400, status, payload)

    def test_create_and_read_back(self):
        schedule = self._create()
        self.assertTrue(schedule["id"].startswith("sc-"))
        self.assertEqual("夜间放行", schedule["name"])
        self.assertTrue(schedule["ruleLabel"])
        self.assertTrue(schedule["timezoneLabel"])
        # 00:00–23:59 覆盖整天，所以"现在"一定在窗口里。
        self.assertEqual("running", schedule["state"])
        status, payload = self.client.get("/api/v1/schedules/%s" % schedule["id"])
        self.assertEqual(200, status, payload)
        self.assertEqual(schedule["id"], payload["id"])
        self.assertEqual(0, payload["runs"]["total"])

    def test_unknown_schedule_is_404(self):
        status, payload = self.client.get("/api/v1/schedules/sc-nope")
        self.assertEqual(404, status, payload)
        status, payload = self.client.post("/api/v1/schedules/sc-nope/delete")
        self.assertEqual(404, status, payload)
        status, payload = self.client.post("/api/v1/schedules/sc-nope/run")
        self.assertEqual(404, status, payload)

    def test_tick_resumes_tasks_inside_the_window(self):
        eid = _engagement(self.store)
        self.client.post("/api/v1/tasks/%s/pause" % eid)
        schedule = self._create(taskIds=[eid])
        status, payload = self.client.post("/api/v1/schedules/tick")
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["created"], payload)
        status, task = self.client.get("/api/v1/tasks/%s" % eid)
        self.assertFalse(task["paused"])
        status, runs = self.client.get(
            "/api/v1/schedules/%s/runs" % schedule["id"])
        self.assertEqual(200, status, runs)
        self.assertTrue(runs["runs"], runs)
        self.assertEqual("enter", runs["runs"][0]["kind"])

    def test_tick_is_idempotent_within_one_window(self):
        eid = _engagement(self.store)
        self._create(taskIds=[eid])
        self.client.post("/api/v1/schedules/tick")
        status, payload = self.client.post("/api/v1/schedules/tick")
        self.assertEqual(200, status, payload)
        # 同一个窗口扫两次不该恢复两次、也不该写两条记录。
        self.assertEqual([], payload["created"])
        self.assertTrue(payload["skipped"], payload)

    def test_tick_pauses_tasks_when_the_window_closes(self):
        eid = _engagement(self.store)
        schedule = self._create(taskIds=[eid])
        self.client.post("/api/v1/schedules/tick")
        # 把窗口收窄到"只有午夜那一分钟"，再让判定时间落在窗口外：走的是同一条
        # tick，不需要等真的到点。
        status, payload = self.client.post(
            "/api/v1/schedules/%s" % schedule["id"],
            {"startTime": "00:00", "endTime": "00:01"})
        self.assertEqual(200, status, payload)
        moment = datetime.datetime.now().replace(hour=12, minute=0, second=0)
        result = schedules.tick(self.store, moment=moment.timestamp())
        self.assertTrue(result["created"], result)
        status, task = self.client.get("/api/v1/tasks/%s" % eid)
        self.assertTrue(task["paused"])
        status, runs = self.client.get(
            "/api/v1/schedules/%s/runs" % schedule["id"])
        kinds = [item["kind"] for item in runs["runs"]]
        self.assertIn("exit", kinds)

    def test_run_now_enters_a_window_by_hand(self):
        eid = _engagement(self.store)
        self.client.post("/api/v1/tasks/%s/pause" % eid)
        schedule = self._create(taskIds=[eid])
        status, payload = self.client.post(
            "/api/v1/schedules/%s/run" % schedule["id"])
        self.assertEqual(200, status, payload)
        self.assertEqual("manual", payload["run"]["kind"])
        status, task = self.client.get("/api/v1/tasks/%s" % eid)
        self.assertFalse(task["paused"])

    def test_disable_and_delete(self):
        schedule = self._create()
        status, payload = self.client.post(
            "/api/v1/schedules/%s/disable" % schedule["id"])
        self.assertEqual(200, status, payload)
        self.assertFalse(payload["enabled"])
        status, payload = self.client.get("/api/v1/schedules")
        self.assertEqual(1, payload["counts"]["disabled"])
        status, payload = self.client.post(
            "/api/v1/schedules/%s/enable" % schedule["id"])
        self.assertEqual(200, status, payload)
        self.assertTrue(payload["enabled"])
        status, payload = self.client.post(
            "/api/v1/schedules/%s/delete" % schedule["id"])
        self.assertEqual(200, status, payload)
        self.assertEqual(schedule["id"], payload["deleted"])
        status, payload = self.client.get("/api/v1/schedules")
        self.assertEqual([], payload["schedules"])

    def test_update_keeps_the_id(self):
        schedule = self._create()
        status, payload = self.client.post(
            "/api/v1/schedules/%s" % schedule["id"], {"name": "改名了"})
        self.assertEqual(200, status, payload)
        self.assertEqual(schedule["id"], payload["schedule"]["id"])
        self.assertEqual("改名了", payload["schedule"]["name"])
        # 没传的字段沿用原值：编辑表单只提交改过的格子，不该把别的清空。
        self.assertEqual(schedule["startTime"], payload["schedule"]["startTime"])

    def test_search_matches_name_and_bound_task(self):
        eid = _engagement(self.store)
        self._create(name="夜间放行", taskIds=[eid])
        status, payload = self.client.get(
            "/api/v1/schedules?q=%s" % quote("夜间"))
        self.assertEqual(1, len(payload["schedules"]))
        status, payload = self.client.get("/api/v1/schedules?q=%s" % eid)
        self.assertEqual(1, len(payload["schedules"]))
        status, payload = self.client.get("/api/v1/schedules?q=nothing")
        self.assertEqual([], payload["schedules"])

    def test_operator_reads_but_cannot_write(self):
        client = self._operator_client()
        status, payload = client.get("/api/v1/schedules")
        self.assertEqual(200, status, payload)
        status, payload = client.post("/api/v1/schedules", _payload())
        self.assertEqual(403, status, payload)
        status, payload = client.post("/api/v1/schedules/tick")
        self.assertEqual(403, status, payload)
