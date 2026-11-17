# -*- coding: utf-8 -*-
"""侵入授权的**运行时**改动：谁改、改了什么、改完真的生效。

第三道闸原本只有一处入口——启动参数。这一版加了第二处（管理员在「部署」页签上
开关），于是它多出四条必须守住的边界：

  * **只有管理员**能改。与装插件同级：改的都是"这个部署能做什么"，而不是某个作业；
  * 校验是**硬的**：未知类别报错而不静默跳过、开类别必须同时给白名单、空条目拒绝；
  * 每次改动**留痕**，且 before 与 after 都要记——只记 after 答不了"那台机器被打的
    那一刻，这一类是开着的还是刚开的"；
  * 改完**真的生效**：`authz.gate` 当场放行。测试必须落到 gate 上，而不是"接口返回
    里的 enabled 变了"——后者只证明字段被赋值了。
"""
import os
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import authz                       # noqa: E402
from pentestd import chat                       # noqa: E402
from pentestd.auth import hash_password          # noqa: E402
from pentestd.server import ApiError, ControlPlane   # noqa: E402
from pentestd.store import Store                 # noqa: E402

WEB_DIR = os.path.join(ROOT, "web")

# 本文件守的是**闸照判**时的运行时行为：改完 gate 真的放行、关掉之后真的 fail-closed、
# 交给模型那一份视图说的是"哪一条在拦"。强制放行下这些都没有判定对象，所以这一
# 模块内把 `authz.FORCE_OPEN` 关掉；跑完还原。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


class AuthzWriteTestCase(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = Store(os.path.join(self.tmp.name, "authz.db"))
        self.store.create_user("root", hash_password("Passw0rd-Root1!"), "admin")
        self.store.create_user("ops", hash_password("Passw0rd-Ops1!"), "operator")
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=True)
        # **必须是真实的 sqlite3.Row，不是 dict**：真实请求里 `ctx["user"]` 就是
        # store 查出来的那一行。这个差别恰好制造过一个"单测全绿、登录之后 500"
        # 的缺陷——`.get()` 在 Row 上不存在——所以这里刻意用真行。
        self._users = {"admin": self.store.get_user_by_name("root"),
                       "operator": self.store.get_user_by_name("ops")}

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _ctx(self, role="admin", body=None):
        return {"query": {}, "body": body or {}, "headers": {},
                "user": self._users.get("admin" if role == "admin" else "operator")}

    def _set(self, enabled, targets, role="admin"):
        return self.plane.set_authz(self._ctx(role, {"enabled": enabled,
                                                    "targets": targets}))

    # ---------------------------------------------------------- 权限

    def test_only_admin_can_change_it(self):
        with self.assertRaises(ApiError) as ctx:
            self._set(["exploit"], ["10.0.0.5"], role="operator")
        self.assertEqual(403, ctx.exception.status)
        self.assertEqual((), self.plane.intrusive,
                         "被拒绝的调用不得留下任何改动")

    def test_anonymous_gets_401_not_403(self):
        """没登录与"登录了但不是管理员"必须分开报。

        混成一个 403，前端就分不清该去登录还是该找管理员，而这正是使用者下一步
        要做的那件事。这条用例同时说明：`ctx["user"]` 缺席时不能崩在 `.get()` 上。
        """
        with self.assertRaises(ApiError) as ctx:
            self.plane.set_authz({"query": {}, "body": {"enabled": []},
                                  "headers": {}})
        self.assertEqual(401, ctx.exception.status)

    def test_can_edit_follows_the_role(self):
        # 面板靠这个字段决定画不画开关。由**服务端**判定而不是前端读角色名：
        # 无鉴权模式下前端拿到的账号信息是空的，自己推会推出"不能改"，
        # 而那个模式下其实谁都能改。
        _s, admin = self.plane.get_authz(self._ctx("admin"))
        _s, operator = self.plane.get_authz(self._ctx("operator"))
        self.assertTrue(admin["canEdit"])
        self.assertFalse(operator["canEdit"])

    # ---------------------------------------------------------- 校验

    def test_unknown_class_is_refused_not_silently_dropped(self):
        # 静默跳过的症状是"点了开关但没生效"，而使用者会去怀疑别的地方。
        with self.assertRaises(ApiError) as ctx:
            self._set(["expliot"], ["10.0.0.5"])
        self.assertIn("expliot", str(ctx.exception))
        self.assertIn("exploit", str(ctx.exception), "拒绝理由要列出可选值")

    def test_enabling_without_a_whitelist_is_allowed_and_means_unrestricted(self):
        """留空的白名单**不再**是错误：它的意思是"不限制目标"。

        仍然要拦的是自相矛盾的那一种——放行了类别却把清单写成 `none`，那会让每次
        调用都被 fail-closed 拒掉，是"看起来开了却什么都做不了"，比不开更难排查。
        """
        status, payload = self._set(["shell"], [])
        self.assertEqual(200, status)
        self.assertEqual(["shell"], payload["enabled"])
        self.assertEqual("any", payload["targetMode"], "留空要如实报成不限制")

    def test_enabling_together_with_none_is_refused(self):
        with self.assertRaises(ApiError) as ctx:
            self._set(["shell"], ["none"])
        self.assertIn("none", str(ctx.exception))
        self.assertIn("留空", str(ctx.exception), "被拒时要给得出正确的做法")

    def test_missing_enabled_field_is_refused(self):
        # 与"传空数组"必须分得开：前者是请求写漏了，后者是"我就是要全关"。
        # 混在一起会让一次笔误变成一次静默的关闭。
        with self.assertRaises(ApiError):
            self.plane.set_authz(self._ctx(body={"targets": []}))
        status, payload = self.plane.set_authz(self._ctx(body={"enabled": [],
                                                              "targets": []}))
        self.assertEqual(200, status)
        self.assertEqual([], payload["enabled"])

    def test_a_whitelist_of_only_blank_entries_degenerates_to_empty(self):
        """只有空条目的白名单退化成"空"，而"空"就是"不限制"。

        `authz.parse_targets` 本来就丢弃空条目（`if name`），所以"白名单里有空条目"
        这个状态**到不了**接口这一层。要守住的是它退化之后那个结果的意思——
        另写一条"空条目"校验会是一条永远为假的检查，而它的存在会让人以为它是
        那道防线（测试第一版就是这么写的，它当场失败了，这正说明它没在守任何东西）。
        """
        status, payload = self._set(["exploit"], ["  ", ",,", ""])
        self.assertEqual(200, status)
        self.assertEqual([], payload["targets"])
        self.assertEqual("any", payload["targetMode"])

    # ---------------------------------------------------------- 生效

    def test_enabling_takes_effect_for_the_gate(self):
        """最重要的一条：改完 `authz.gate` 真的放行。

        只断言接口返回里的 `enabled` 变了，证明的是"字段被赋值了"——而这块面板
        存在的意义是"现在能不能动手"。两者之间隔着 gate 那一段。
        """
        with self.assertRaises(authz.AuthzError):
            authz.gate("exploit", "AUTH-2024-018", self.plane.intrusive,
                       self.plane.intrusive_targets, "10.0.0.5", 80)
        self._set(["exploit"], ["10.0.0.5"])
        snapshot = authz.gate("exploit", "AUTH-2024-018", self.plane.intrusive,
                              self.plane.intrusive_targets, "10.0.0.5", 80)
        self.assertEqual("exploit", snapshot["kind"])
        self.assertEqual(["10.0.0.5"], snapshot["targets"])
        # 配额是**本次实际生效的值**，随快照落进链路
        self.assertIn("maxSteps", snapshot["limits"])

    def test_disabling_returns_to_fail_closed(self):
        self._set(["exploit", "creds"], ["10.0.0.5"])
        self._set([], [])
        self.assertEqual((), self.plane.intrusive)
        self.assertEqual((), self.plane.intrusive_targets)
        with self.assertRaises(authz.AuthzError):
            authz.gate("creds", "AUTH-2024-018", self.plane.intrusive,
                       self.plane.intrusive_targets, "10.0.0.5", 80)

    def test_boot_snapshot_is_not_touched(self):
        """启动参数那一份必须原样留着。

        面板要能说出"眼前这一份是谁给的"——重启会回到启动参数,所以"启动时是什么"
        与"现在是什么"必须能从数据上分开。混淆它们之后，"我明明开了"与"重启后它
        又关了"就只能靠记忆去对。
        """
        self._set(["pivot"], ["10.0.0.9"])
        _s, payload = self.plane.get_authz(self._ctx())
        self.assertEqual([], payload["boot"]["enabled"], "启动参数是空的，改完还得是空的")
        self.assertEqual([], payload["boot"]["targets"])
        self.assertTrue(payload["runtimeChanged"])

    def test_restart_command_only_lists_what_is_on(self):
        # 命令是给"想让它在重启后依然有效"的人抄的，因此它必须与当前生效的状态
        # 逐字对应——多带一个没开的类别，抄过去就等于多开了一类。
        _s, empty = self.plane.get_authz(self._ctx())
        self.assertEqual("", empty["restartCommand"], "全关时不该给出一条只带 flag 的命令")
        self._set(["creds"], ["10.0.0.5"])
        _s, payload = self.plane.get_authz(self._ctx())
        self.assertIn("--allow-intrusive creds", payload["restartCommand"])
        self.assertIn("--intrusive-targets 10.0.0.5", payload["restartCommand"])
        self.assertNotIn("exploit", payload["restartCommand"])

    # ---------------------------------------------------------- 留痕

    def test_every_change_records_before_and_after(self):
        self._set(["exploit"], ["10.0.0.5"])
        self._set(["exploit", "creds"], ["10.0.0.5", "10.0.0.9:8443"])
        ops = self.store.authz_ops(10)
        self.assertEqual(2, len(ops))
        newest, oldest = ops[0], ops[1]
        self.assertEqual({"enabled": ["exploit"], "targets": ["10.0.0.5"]},
                         newest["before"])
        self.assertEqual(["creds"], [c for c in newest["after"]["enabled"]
                                     if c not in newest["before"]["enabled"]])
        self.assertEqual([], oldest["before"]["enabled"],
                         "第一次改动之前是「全关」")
        self.assertEqual("root", newest["actor"])

    def test_repeating_the_same_state_writes_no_record(self):
        """幂等：重复点同一颗按钮不该在流水里留下一条空记录。

        审计流水的价值来自"每一条都对应一次真实的决定"；被重复提交灌满之后，
        真正要看的那一条会淹没在里面。
        """
        self._set(["exploit"], ["10.0.0.5"])
        before = len(self.store.authz_ops(50))
        status, payload = self._set(["exploit"], ["10.0.0.5"])
        self.assertEqual(200, status, "幂等不是错误")
        self.assertNotIn("opId", payload, "没有改动就不该有新的操作 id")
        self.assertEqual(before, len(self.store.authz_ops(50)))

    def test_change_is_visible_in_the_payload(self):
        status, payload = self._set(["shell"], ["10.0.0.9"])
        self.assertEqual(200, status)
        self.assertEqual(["shell"], payload["enabled"])
        self.assertEqual(["10.0.0.9"], payload["targets"])
        self.assertTrue(payload["anyEnabled"])
        self.assertIn("opId", payload)
        self.assertEqual(1, len(payload["ops"]), "改完的响应里就带着流水")

    def test_the_table_and_the_version_are_both_declared(self):
        """新表必须同时进 DDL 与版本号——两头漏哪一头都有症状。

        只改 `SCHEMA_VERSION` 而忘了建表：打开库一切正常，第一次用到那张表才炸。
        只建表而忘了提版本：**旧程序愿意打开这个库**，而那是版本号存在的全部理由。
        """
        from pentestd.store import DDL, SCHEMA_VERSION
        self.assertIn("CREATE TABLE IF NOT EXISTS authz_ops", DDL)
        self.assertIn("CREATE TABLE IF NOT EXISTS authz_state", DDL)
        self.assertGreaterEqual(SCHEMA_VERSION, 12)


class AuthzPersistenceTestCase(unittest.TestCase):
    """"下次加载保留上次设置"——以及它与启动参数之间的仲裁。

    这块设置原本不持久（重启回到启动参数），本版改成会落库。于是多出一条必须钉住
    的规则：**启动参数变过就用新的那一份**。

    少了这条仲裁，两条路各自都有一种静默的错法：持久化总是赢，部署者改了
    `--allow-intrusive` 却看到旧授权，他会以为参数写错了然后去改别的地方；
    启动参数总是赢，管理员在界面上开的授权每次重启都消失，"保留"这件事没做成。
    判据是 `authz.grant_fingerprint`：参数没变就沿用保存的那份，变了就用新的。
    """

    def setUp(self):
        # `ignore_cleanup_errors`：断言在 `store.close()` 之前失败时，Windows 上
        # 那个 db 文件仍被占用，清理会抛 WinError 32 —— 而那个错误会**盖住**
        # 真正的断言失败，让人去查临时目录而不是查断言。
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.db = os.path.join(self.tmp.name, "persist.db")

    def tearDown(self):
        self.tmp.cleanup()

    def _boot(self, enabled=("exploit",), targets=(),
              auth_required=False):
        store = Store(self.db)
        plane = ControlPlane(store, WEB_DIR, auth_required=auth_required,
                             intrusive=list(enabled),
                             intrusive_targets=list(targets))
        return plane, store

    @staticmethod
    def _admin(store):
        """真实的 `sqlite3.Row`，不是 dict——理由见 `AuthzWriteTestCase` 的 setUp。"""
        if store.get_user_by_name("root") is None:
            store.create_user("root", hash_password("Passw0rd-Root1!"), "admin")
        return store.get_user_by_name("root")

    def _change(self, plane, store, enabled, targets=()):
        return plane.set_authz({"body": {"enabled": list(enabled),
                                         "targets": list(targets)},
                                "user": self._admin(store)})

    def test_a_change_survives_a_restart_with_the_same_boot_params(self):
        plane, store = self._boot()
        self._change(plane, store, ["exploit", "creds"])
        store.close()

        plane2, store2 = self._boot()
        try:
            self.assertEqual(("exploit", "creds"), plane2.intrusive)
            self.assertTrue(plane2.authz_restore["restored"])
        finally:
            store2.close()

    def test_changed_boot_params_win_and_the_saved_copy_is_dropped(self):
        plane, store = self._boot()
        self._change(plane, store, ["exploit", "creds"])
        store.close()

        plane2, store2 = self._boot(enabled=("exploit", "creds", "shell"))
        try:
            self.assertEqual(("exploit", "creds", "shell"), plane2.intrusive,
                             "部署者这次写的参数更新，该用它")
            self.assertEqual("boot-changed", plane2.authz_restore["reason"])
            self.assertEqual(["exploit", "creds"],
                             plane2.authz_restore["previous"]["enabled"],
                             "丢弃要说得出来丢的是什么，否则'我上次明明开着'无据可查")
            self.assertIsNone(store2.authz_state(), "被丢弃的那份要真的删掉")
        finally:
            store2.close()

    def test_nothing_saved_means_boot_params_are_used_verbatim(self):
        plane, store = self._boot()
        try:
            self.assertEqual("none", plane.authz_restore["reason"])
            self.assertIsNone(store.authz_state())
        finally:
            store.close()

    def test_a_saved_all_off_is_not_confused_with_nothing_saved(self):
        """"没有"与"有一份空的"是两件事。

        前者表示没人在界面上动过（于是用启动参数），后者表示有人把类别全关了——
        那也是一次明确的改动，重启后应当保留。用一个 `None` 把它们分开；混起来的
        症状是"我明明关掉了，重启又开了"，而那次关闭本身是有效的。
        """
        plane, store = self._boot()
        self._change(plane, store, [])
        store.close()

        plane2, store2 = self._boot()           # 启动参数仍然放行 exploit
        try:
            self.assertEqual((), plane2.intrusive,
                             "保存过的「全关」必须盖住启动参数的放行")
            self.assertTrue(plane2.authz_restore["restored"])
        finally:
            store2.close()

    def test_get_authz_reports_whether_the_current_state_is_saved(self):
        plane, store = self._boot()
        try:
            _s, payload = plane.get_authz({"query": {}})
            self.assertFalse(payload["persisted"])
            self.assertEqual("any", payload["targetMode"])
            self._change(plane, store, [], [])
            _s, payload = plane.get_authz({"query": {}})
            self.assertTrue(payload["persisted"])
        finally:
            store.close()

    def test_a_whitelist_survives_a_restart(self):
        plane, store = self._boot()
        self._change(plane, store, ["exploit"], ["10.0.0.0/24", "10.0.0.9:8443"])
        store.close()

        plane2, store2 = self._boot()
        try:
            self.assertEqual(("10.0.0.0/24", "10.0.0.9:8443"), plane2.intrusive_targets)
            _s, payload = plane2.get_authz({"query": {}})
            self.assertEqual("list", payload["targetMode"])
        finally:
            store2.close()

    def test_saved_none_is_not_read_back_as_empty(self):
        """`none` 被读成"没写"就等于把闸打开了——三态里最危险的一次混淆。

        这里**直接写 store**：`set_authz` 会拦"放行了类别 + none"那种自相矛盾的
        组合（另一条用例守着它），而本条要测的是**恢复路径**对 `none` 的处理。
        走接口的话就测不到真正危险的那一步。
        """
        store = Store(self.db)
        store.create_user("root", hash_password("Passw0rd-Root1!"), "admin")
        store.save_authz_state([], ["none"],
                               boot_fingerprint=authz.grant_fingerprint([], []))
        store.close()

        plane, store2 = self._boot(enabled=(), targets=())
        try:
            _s, payload = plane.get_authz({"query": {}})
            self.assertEqual("none", payload["targetMode"])
            self.assertNotEqual("any", payload["targetMode"])
        finally:
            store2.close()

    def test_the_discard_leaves_a_durable_audit_row(self):
        """丢弃必须**持久地**留痕，而不只是活在进程内存里。

        `clear_authz_state()` 会把持久化那行删掉，所以如果痕迹只写进
        `self.authz_restore`，进程一停就什么都不剩——而"我上次明明设了
        10.10.10.250"恰恰是最需要证据的那类抱怨。断言的是**流水**里有它。
        """
        plane, store = self._boot()
        self._change(plane, store, ["exploit"], ["10.10.10.250"])
        store.close()

        plane2, store2 = self._boot(enabled=())      # 这次一个参数都没带
        try:
            self.assertEqual("boot-changed", plane2.authz_restore["reason"])
            boot_rows = [r for r in store2.authz_ops(20) if r["actor"] == "boot"]
            self.assertEqual(1, len(boot_rows), "丢弃要写进流水，而且只写一条")
            self.assertEqual(["10.10.10.250"], boot_rows[0]["before"]["targets"],
                             "流水要说得出来丢的是什么")
            self.assertEqual([], boot_rows[0]["after"]["targets"])
            self.assertIn("指纹", boot_rows[0]["note"],
                          "要说清判据是什么，否则这条记录只是又一次断言")
        finally:
            store2.close()


class AuthzToolViewTestCase(unittest.TestCase):
    """`get_authz` 交给**模型**的那一份里必须有判据。

    这是唯一一处会把授权状态讲给用户听的地方——浏览器那一份再准，用户听到的
    仍是模型转述的。两处曾经只剩猜的余地，各翻过一次车：

      * `targets` 为空时打印「（空）」。空与 `none` 字面上都是"没有目标"，意思
        却相反。界面那条路后来改读 `targetMode` / `targetsText` 了，**这条路
        漏了**，于是同一个歧义留在了唯一会把它讲出口的地方；
      * `restore` / `boot` 被丢掉。白名单被清空之后，模型看不到
        `restore.reason == 'boot-changed'`，把一次启动时的丢弃猜成了"你是不是
        把 `--intrusive-targets` 去掉了"——而那两个进程从来没带过参数。
        **猜出来的因果与查到的一样自信**，所以判据要交到它手上。
    """

    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory(ignore_cleanup_errors=True)
        self.store = Store(os.path.join(self.tmp.name, "toolview.db"))
        self.store.create_user("root", hash_password("Passw0rd-Root1!"), "admin")
        self.plane = ControlPlane(self.store, WEB_DIR, auth_required=False,
                                  intrusive=["exploit"], intrusive_targets=[])

    def tearDown(self):
        self.store.close()
        self.tmp.cleanup()

    def _view(self):
        return chat._tool_get_authz(self.plane, {}, "", None)

    def test_an_empty_whitelist_is_stated_as_unrestricted_not_as_empty(self):
        out = self._view()
        self.assertEqual("any", out["data"]["targetMode"])
        self.assertIn("不限制", out["data"]["targetsText"])
        self.assertIn("不限制", out["summary"])
        self.assertNotIn("（空）", out["summary"],
                         "「（空）」正是那次误判的成因：它不说空是什么意思")

    def test_the_hint_names_scope_as_the_only_remaining_bound(self):
        out = self._view()
        self.assertIn("唯一", out["data"]["hint"])
        self.assertNotIn("每个目标仍要落在侵入白名单里", out["data"]["hint"],
                         "留空时白名单里一个目标都没有，「仍要落在里面」是假话")

    def test_the_view_carries_the_why(self):
        out = self._view()
        for key in ("targetMode", "targetsText", "restore", "boot",
                    "runtimeChanged", "restartCommand", "scope"):
            self.assertIn(key, out["data"],
                          "少了 %s，「为什么变成这样」就只能靠猜" % key)

    def test_a_list_whitelist_still_says_every_target_must_be_in_it(self):
        self.plane.intrusive_targets = ("10.0.0.0/24",)
        out = self._view()
        self.assertEqual("list", out["data"]["targetMode"])
        self.assertIn("仍要落在侵入白名单里", out["data"]["hint"])


if __name__ == "__main__":
    unittest.main()
