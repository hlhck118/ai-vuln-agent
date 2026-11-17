# -*- coding: utf-8 -*-
"""侵入式动作的准入闸：默认全关、三重条件、配额。

这一组用例守的是**默认方向**。前两道闸（`scope` 的连通范围、`policy` 的会话
动作类别）的默认值都能推出"不使用它也不会出事"；侵入式动作没有这个性质，
所以它的每一条默认都必须是"拒绝"，而每一条拒绝都要说清缺什么、怎么开。
"""
import unittest

from pentestd import authz

# 本文件守的是**闸照判**时的四条判定（各自的拒绝理由与措辞），所以这一模块内把
# `authz.FORCE_OPEN` 关掉。跑完还原：`unittest discover` 会把全部模块一起
# import，把全局状态留下去会污染别的文件。强制放行**本身**的用例在
# `test_force_open.py` 里，两者互相独立，谁都不掩盖谁。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


class ClassTableTestCase(unittest.TestCase):
    def test_all_five_missing_capabilities_have_a_class(self):
        # 用户列的"不能做"逐条对应到这里的一个类别。少一条意味着那件事
        # 连"申请授权"的入口都没有。
        self.assertEqual(["exploit", "creds", "shell", "pivot", "toolkit"],
                         [item["id"] for item in authz.CLASSES])

    def test_every_class_explains_what_it_will_do(self):
        for item in authz.CLASSES:
            self.assertTrue(item["summary"], item["id"])
            self.assertTrue(item["requires"],
                            "%s 必须说清它会改变目标的什么" % item["id"])

    def test_unknown_class_raises_instead_of_falling_back(self):
        # 与权限档相反：那里读路径回退默认是对的，因为写坏的值只会让人少个功能。
        # 这里把 `expliot` 静默当成"没这一类"，会让人以为开了而实际没开。
        with self.assertRaises(authz.AuthzError) as ctx:
            authz.resolve_class("expliot")
        self.assertIn("exploit", str(ctx.exception))


class DefaultDenyTestCase(unittest.TestCase):
    """默认全关。"""

    def test_nothing_enabled_by_default(self):
        self.assertEqual([], authz.enabled_classes(None))
        self.assertEqual([], authz.enabled_classes([]))

    def test_every_class_is_refused_when_not_enabled(self):
        for item in authz.CLASSES:
            with self.assertRaises(authz.AuthzError):
                authz.gate(item["id"], "ENG-1234", [], ["10.0.0.5"], "10.0.0.5", 80)

    def test_refusal_names_the_flag_and_the_consequence(self):
        with self.assertRaises(authz.AuthzError) as ctx:
            authz.gate("creds", "ENG-1234", [], ["10.0.0.5"], "10.0.0.5", 80)
        message = str(ctx.exception)
        self.assertIn("--allow-intrusive creds", message)
        self.assertIn("账号锁定", message,
                      "拒绝理由要指向具体后果，而不是一句「敏感操作」")

    def test_all_keyword_must_be_explicit(self):
        # 空默认值如果被解释成"全部"，这个开关就不存在了。
        self.assertEqual(["exploit", "creds", "shell", "pivot", "toolkit"],
                         authz.enabled_classes(["all"]))
        self.assertEqual([], authz.enabled_classes([""]))
        self.assertEqual([], authz.enabled_classes(["none"]))

    def test_comma_and_space_separated_forms(self):
        self.assertEqual(["exploit", "creds"],
                         authz.enabled_classes(["exploit, creds"]))
        self.assertEqual(["exploit", "creds"],
                         authz.enabled_classes(["exploit", "creds"]))
        self.assertEqual(["exploit"], authz.enabled_classes(["exploit,exploit"]))


class GrantTextTestCase(unittest.TestCase):
    """授权说明：它是"谁在什么凭据下打的"的凭据。"""

    def test_short_grant_is_refused(self):
        with self.assertRaises(authz.AuthzError) as ctx:
            authz.gate("exploit", "x", ["all"], ["10.0.0.5"], "10.0.0.5", 80)
        self.assertIn(str(authz.MIN_GRANT_CHARS), str(ctx.exception))

    def test_exactly_minimum_is_accepted(self):
        text = "E" * authz.MIN_GRANT_CHARS
        snapshot = authz.gate("exploit", text, ["all"], ["10.0.0.5"], "10.0.0.5", 80)
        self.assertEqual(text, snapshot["grant"])

    def test_grant_is_carried_into_the_snapshot(self):
        snapshot = authz.gate("exploit", "ENG-2026-0007", ["all"],
                              ["10.0.0.5"], "10.0.0.5", 80)
        self.assertEqual("ENG-2026-0007", snapshot["grant"])
        self.assertTrue(snapshot["grantedAt"])


class TargetAllowlistTestCase(unittest.TestCase):
    """侵入白名单是**额外**的收窄，不是连通范围的替代。"""

    def test_empty_allowlist_means_no_extra_narrowing(self):
        """留空 = **不限制目标**，只受探测作用域约束。

        白名单按本模块自己的定义就是"额外的收窄"（见模块说明第 3 条），而
        "没有额外收窄"就该回到没有它的样子。把它读成"一律拒绝"更严，代价却是
        "放行了类别却什么都做不了"，而那种组合的症状读起来像网络或节点的问题。
        """
        snapshot = authz.gate("exploit", "ENG-1234", ["all"], [], "10.0.0.5", 80)
        self.assertEqual(snapshot["targets"], [])
        self.assertEqual(authz.target_mode([]), "any")

    def test_explicit_none_still_refuses_everything(self):
        """`none` 是那个"真要一律拒绝"的**写得出来**的形式。

        少了它，想收紧的部署只能靠留空——而留空现在的意思正好相反。所以这条
        用例守的不只是行为，还有"两种相反的意图各自有一个字面表达"这件事。
        """
        for spec in ("none", "NONE", " none "):
            with self.assertRaises(authz.AuthzError) as ctx:
                authz.gate("exploit", "ENG-1234", ["all"], [spec], "10.0.0.5", 80)
            self.assertIn("none", str(ctx.exception))
        self.assertEqual(authz.target_mode("none"), "none")

    def test_address_outside_allowlist_is_refused(self):
        with self.assertRaises(authz.AuthzError) as ctx:
            authz.gate("exploit", "ENG-1234", ["all"], ["10.0.0.0/8"],
                       "192.168.1.5", 80)
        self.assertIn("192.168.1.5", str(ctx.exception))
        self.assertIn("留空表示不限制", str(ctx.exception),
                      "被拒时要说明空白名单是什么意思，否则使用者会以为它必填")

    def test_cidr_and_exact_host_forms(self):
        authz.gate("exploit", "ENG-1234", ["all"], ["10.0.0.0/8"], "10.9.9.9", 80)
        authz.gate("exploit", "ENG-1234", ["all"], ["10.0.0.5"], "10.0.0.5", 80)

    def test_host_port_entry_is_checked(self):
        authz.gate("exploit", "ENG-1234", ["all"], ["10.0.0.5:8443"],
                   "10.0.0.5", 8443)
        with self.assertRaises(authz.AuthzError):
            authz.gate("exploit", "ENG-1234", ["all"], ["10.0.0.5:8443"],
                       "10.0.0.5", 80)

    def test_a_domain_never_matches_a_cidr_by_accident(self):
        # 域名要放行就写域名。让一个名称"碰巧"落进某个网段，取决于解析结果，
        # 而解析结果会变——那不是一个可审计的授权。
        with self.assertRaises(authz.AuthzError):
            authz.gate("exploit", "ENG-1234", ["all"], ["10.0.0.0/8"],
                       "internal.example", 80)

    def test_host_may_be_omitted_for_a_class_wide_check(self):
        # 有些调用点只知道"要开这一类"，还不知道打谁（比如调度前的预检）。
        snapshot = authz.gate("pivot", "ENG-1234", ["all"], ["10.0.0.0/8"])
        self.assertEqual(["10.0.0.0/8"], snapshot["targets"])


class HopLimitTestCase(unittest.TestCase):
    def test_hop_limit_comes_from_pivot_limits(self):
        limit = int(authz.LIMITS["pivot"]["maxHops"])
        authz.gate("exploit", "ENG-1234", ["all"], ["10.0.0.5"], "10.0.0.5", 80,
                   hop=limit)
        with self.assertRaises(authz.AuthzError) as ctx:
            authz.gate("exploit", "ENG-1234", ["all"], ["10.0.0.5"], "10.0.0.5",
                       80, hop=limit + 1)
        self.assertIn("跳", str(ctx.exception))


class LimitsTestCase(unittest.TestCase):
    def test_limits_are_returned_in_the_snapshot(self):
        # 执行侧要把**本次实际生效的**配额写进报告。报告里写一个从没生效过的
        # 上限，比不写更糟。
        snapshot = authz.gate("creds", "ENG-1234", ["all"], ["10.0.0.5"],
                              "10.0.0.5", 21)
        self.assertEqual(authz.LIMITS["creds"]["maxAttemptsPerAccount"],
                         snapshot["limits"]["maxAttemptsPerAccount"])

    def test_unknown_class_has_no_limits(self):
        with self.assertRaises(authz.AuthzError):
            authz.limits_for("nope")

    def test_overrides_only_touch_known_keys(self):
        merged = authz.limits_for("exploit", {"maxRequests": 3, "bogus": 99})
        self.assertEqual(3, merged["maxRequests"])
        self.assertNotIn("bogus", merged,
                         "未知键不能悄悄进到生效配置里")

    def test_credential_testing_has_a_delay_floor(self):
        # 速率下限是防锁定的第一道闸，它不能是 0。
        self.assertGreaterEqual(authz.LIMITS["creds"]["minDelayMs"], 100)


class FingerprintTestCase(unittest.TestCase):
    def test_fingerprint_is_stable_and_not_the_secret(self):
        first = authz.fingerprint("Hunter2!", salt="h|admin")
        second = authz.fingerprint("Hunter2!", salt="h|admin")
        self.assertEqual(first, second)
        self.assertNotIn("Hunter2", first)
        self.assertEqual(12, len(first))

    def test_different_salt_gives_different_fingerprint(self):
        self.assertNotEqual(authz.fingerprint("Hunter2!", salt="a"),
                            authz.fingerprint("Hunter2!", salt="b"))

    def test_empty_secret_is_empty_fingerprint(self):
        self.assertEqual("", authz.fingerprint(""))


class DescribeTestCase(unittest.TestCase):
    def test_describe_states_the_three_gates_are_independent(self):
        text = authz.describe(["exploit"], ["10.0.0.5"])
        self.assertIn("未开启", text)
        self.assertIn("--allow-intrusive creds", text)
        self.assertIn("三道独立的闸", text)

    def test_describe_marks_what_is_on(self):
        text = authz.describe(["all"], ["10.0.0.0/8"])
        self.assertNotIn("未开启", text)
        self.assertIn("10.0.0.0/8", text)


if __name__ == "__main__":
    unittest.main()
