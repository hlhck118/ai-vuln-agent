# -*- coding: utf-8 -*-
"""Hub 目录：字段降级、链接校验、检索与分页的可复核契约。

这个模块没有副作用，"对不对"完全体现在它给出的数据上，所以用例也全部钉在数据上：

* **降级要给出说得通的值**，不是"没抛异常"。断言 `size == 0`、`tags == []`、
  而不是 `assertIsInstance(result, dict)`——后者在任何实现下都能通过。
* **链接校验是安全相关的那一条**。`repositoryUrl` 会变成可点击链接，因此恶意
  `r` 值的期望是空串，而不是"某个还能看的 URL"。
* **翻页必须自洽**。逐页取回的 id 拼起来要恰好等于全集，这是"目录顺序不稳定"
  这个前提唯一能被验证的方式；同时用反序输入复算一遍，证明结果不依赖输入顺序。
* **顺序不许依赖输入的排列**，所以每个排序用例都会把同一批数据反过来再算一次。
"""
import copy
import os
import random
import sys
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import hubcatalog                                   # noqa: E402

# 真实快照里的一条记录（文档中的原始样本，逐字照抄）。它是"所有字段都写全了"
# 的参照物：规整结果应当与这些短键一一对应，一个不多一个不少。
SAMPLE = {
    "s": "dsh-our-free-model",
    "o": "ebony-vinyl",
    "n": "dsh-our-free-model",
    "vr": "v2.1.0",
    "c": "model",
    "t": ["ai-agents", "deepseek", "dsh", "llm"],
    "f": ["免登录免 Key 直接用模型", "本机用量看板与延迟采样"],
    "d": "在 DeepSeek Harness 中装上这个 DSH plugin，无需登录…",
    "r": "ebony-vinyl/dsh-our-free-model",
    "ic": "dsh plugin --profile web add github:ebony-vinyl/dsh-our-free-model",
    "igc": "dsh plugin --profile web add github:ebony-vinyl/dsh-our-free-model",
    "v": "verified",
    "u": "2026-10-11T06:26:18Z",
    "a": "2026-10-05",
    "sg": 8064,
    "fk": 169,
}

SAMPLE_EXPECTED = {
    "id": "ebony-vinyl/dsh-our-free-model",
    "name": "dsh-our-free-model",
    "slug": "dsh-our-free-model",
    "owner": "ebony-vinyl",
    "repo": "ebony-vinyl/dsh-our-free-model",
    "version": "v2.1.0",
    "category": "model",
    "tags": ["ai-agents", "deepseek", "dsh", "llm"],
    "features": ["免登录免 Key 直接用模型", "本机用量看板与延迟采样"],
    "description": "在 DeepSeek Harness 中装上这个 DSH plugin，无需登录…",
    "installCommand": "dsh plugin --profile web add github:ebony-vinyl/dsh-our-free-model",
    "installTarget": "github:ebony-vinyl/dsh-our-free-model",
    "repositoryUrl": "https://github.com/ebony-vinyl/dsh-our-free-model",
    "verified": True,
    "updatedAt": "2026-10-11T06:26:18Z",
    "addedAt": "2026-10-05",
    "size": 8064,
    "forks": 169,
}

KEYS = sorted(SAMPLE_EXPECTED)

# 时间统一取同一刻，凡是要比"名称升序"的用例都靠它把时间因素抹平。
SAME_TIME = "2026-10-01T00:00:00Z"


def raw(slug, **fields):
    """构造一条原始 hub 记录：给一份可用默认值，只覆盖关心的短键。"""
    item = {"s": slug, "n": slug, "o": "acme", "c": "model", "v": "verified",
            "u": SAME_TIME}
    item.update(fields)
    return item


def slugs(page):
    return [entry["slug"] for entry in page["items"]]


def names(page):
    return [entry["name"] for entry in page["items"]]


def catalog():
    """三条覆盖全部匹配字段的目录，供检索用例共用。"""
    return [
        raw("dsh-usage-panel", d="模型用量看板与延迟采样",
            t=["dsh", "model", "面板"], f=["免登录免 Key 直接用模型"]),
        raw("dsh-task-runner", o="other", c="tools", v="unverified",
            d="批量任务编排工具", t=["dsh", "deepseek"], f=["批量下发任务"]),
        raw("model-router", o="third", v="pending",
            d="在多个模型之间路由", t=["router", "llm"], f=["按成本选模型"]),
    ]


def pageable():
    """七条只按名称区分的记录：验证"逐页取回恰好拼出全集"。"""
    return [raw("plugin-%s" % letter, n="Plugin %s" % letter.upper())
            for letter in "abcdefg"]


# ---------------------------------------------------------------- 真实样本

class SampleEntryTestCase(unittest.TestCase):
    """一条字段齐全的真实记录，规整后必须一一对得上。"""

    def test_real_entry_normalizes_every_documented_key(self):
        """样本的每个字段都要落到对应的 camelCase 键上，键集固定为 18 个。"""
        entry = hubcatalog.normalize(SAMPLE)
        self.assertEqual(entry, SAMPLE_EXPECTED)
        self.assertEqual(sorted(entry), KEYS,
                         "规整结果的键集是上游契约，不能多也不能少")

    def test_normalize_does_not_mutate_the_input(self):
        """原地改调用方的记录会让"原始快照"这个前提失效，缓存也没法复用。"""
        before = copy.deepcopy(SAMPLE)
        hubcatalog.normalize(SAMPLE)
        self.assertEqual(SAMPLE, before)

    def test_search_returns_the_same_shape(self):
        """检索结果里装的是规整后的字典，不是原始记录。"""
        entry = hubcatalog.search([SAMPLE])["items"][0]
        self.assertEqual(sorted(entry), KEYS)
        self.assertEqual(entry["id"], SAMPLE_EXPECTED["id"])


# ---------------------------------------------------------------- 字段降级

class DegradationTestCase(unittest.TestCase):
    """缺键、`null`、类型不对：降级成说得通的值，而不是抛异常或丢字段。"""

    def test_entry_without_identity_is_dropped(self):
        """没有任何可用身份的记录返回 None——留着只会在界面上变成一行空白。"""
        for item in ({}, {"s": ""}, {"s": "   ", "n": ""},
                     {"s": None, "n": None, "o": "someone"}, {"o": "someone"},
                     [], "junk", 0, 3.5, True, None):
            self.assertIsNone(hubcatalog.normalize(item), "%r 不该被规整" % (item,))

    def test_missing_fields_degrade_to_empty_values(self):
        """只有名称的记录也要能出结果：其余字段给空值，键一个不少。"""
        entry = hubcatalog.normalize({"n": "solo"})
        self.assertEqual(entry, {
            "id": "solo", "name": "solo", "slug": "solo", "owner": "", "repo": "",
            "version": "", "category": "", "tags": [], "features": [],
            "description": "", "installCommand": "", "installTarget": "",
            "repositoryUrl": "", "verified": False, "updatedAt": "", "addedAt": "",
            "size": 0, "forks": 0})
        self.assertEqual(sorted(entry), KEYS)

    def test_wrong_typed_lists_degrade(self):
        """非列表给空列表；列表里的非字符串元素丢掉，而不是把 repr 塞进标签。"""
        entry = hubcatalog.normalize({"s": "x", "t": "not-a-list",
                                      "f": [1, "ok", None, {"a": 1}, ["nested"],
                                            "   ", "真"]})
        self.assertEqual(entry["tags"], [])
        self.assertEqual(entry["features"], ["1", "ok", "真"])

    def test_numeric_fields_accept_numeric_strings_and_reject_junk(self):
        """`sg` / `fk` 的类型不稳：能读成整数就读，读不到一律给 0。"""
        entry = hubcatalog.normalize(raw("x", sg="8064", fk="12.9", vr=123, a=0))
        self.assertEqual((entry["size"], entry["forks"]), (8064, 12))
        self.assertEqual(entry["version"], "123", "数字版号当文本用比丢掉更接近原意")
        self.assertEqual(entry["addedAt"], "0")
        for item in (raw("x", sg="many", fk=None, vr=None),
                     raw("x", sg=True, fk=[], vr={})):
            entry = hubcatalog.normalize(item)
            self.assertEqual((entry["size"], entry["forks"]), (0, 0))
            self.assertEqual(entry["version"], "")

    def test_numeric_identity_survives(self):
        """`s` 是数字时当文本用：身份有值总比整条记录消失好。"""
        entry = hubcatalog.normalize({"s": 123, "n": 123})
        self.assertEqual((entry["id"], entry["name"], entry["slug"]),
                         ("123", "123", "123"))

    def test_verified_accepts_only_positive_strings(self):
        """`v` 是字符串字段：非字符串、空串、已知否定取值都不算已验证。"""
        table = [("verified", True), ("VERIFIED", True), (" audited ", True),
                 ("unverified", False), ("Pending", False), ("none", False),
                 ("FALSE", False), ("0", False), ("", False), ("   ", False),
                 (None, False), (123, False), (True, False),
                 (["verified"], False)]
        for value, expected in table:
            entry = hubcatalog.normalize({"s": "x", "v": value})
            self.assertIs(entry["verified"], expected, "v=%r 的判定不对" % (value,))

    def test_hostile_values_never_raise(self):
        """一组畸形记录：要么返回 None，要么返回键集完整、类型正确的字典。"""
        hostile = [
            {"s": {"nested": "dict"}}, {"n": ["list"]}, {"o": 1.5},
            {"t": {"a": 1}}, {"f": "string"}, {"u": []}, {"a": {}}, {"c": [1, 2]},
            {"r": {"owner": "a"}}, {"sg": "1e9", "fk": "0x10"}, {"vr": object()},
            {"s": "x", "n": None, "o": None, "r": None, "v": None},
        ]
        for item in hostile:
            entry = hubcatalog.normalize(item)
            self.assertTrue(entry is None or sorted(entry) == KEYS,
                            "%r 的输出键集不对" % (item,))
            if entry is not None:
                self.assertIsInstance(entry["id"], str)
                self.assertTrue(entry["id"])
                self.assertIsInstance(entry["tags"], list)
                self.assertIsInstance(entry["verified"], bool)
                self.assertIsInstance(entry["size"], int)
                self.assertIsInstance(entry["forks"], int)

    def test_normalize_all_isolates_broken_entries(self):
        """坏记录单条丢弃：一条坏数据不能让整份目录不可用。"""
        entries = hubcatalog.normalize_all(
            [{}, raw("good"), "junk", None, {"s": None}, raw("also-good"), 7])
        self.assertEqual([entry["slug"] for entry in entries],
                         ["good", "also-good"])

    def test_non_list_input_gives_an_empty_list(self):
        for value in (None, "junk", 7, {"s": "x"}, True):
            self.assertEqual(hubcatalog.normalize_all(value), [])


# ---------------------------------------------------------------- 链接校验

class RepositoryUrlTestCase(unittest.TestCase):
    """`repositoryUrl` 会变成可点击链接，因此只允许由严格校验过的 owner/repo 生成。"""

    def test_sample_builds_the_github_url(self):
        self.assertEqual(hubcatalog.normalize(SAMPLE)["repositoryUrl"],
                         "https://github.com/ebony-vinyl/dsh-our-free-model")

    def test_malicious_or_malformed_repo_values_yield_no_link(self):
        """注入式与畸形 `r` 值的期望是空串：宁可少一个链接，也不要错的链接。"""
        bad = ("javascript:alert(1)", "../etc", "a/b/c", "a/", "a b", "", None,
               "/etc/passwd", "https://evil.example/a/b", "a/b/../c", ".../x",
               "owner/repo?x=1", "owner/repo#frag", "owner\\repo", "a//b")
        for value in bad:
            entry = hubcatalog.normalize(raw("x", r=value))
            self.assertEqual(entry["repositoryUrl"], "", "r=%r 不该生成链接" % (value,))
            self.assertEqual(entry["repo"], "", "r=%r 不该被当作仓库坐标" % (value,))
            # 身份退回 owner/slug：链接被拒不代表这条记录没有身份
            self.assertEqual(entry["id"], "acme/x")

    def test_valid_repo_values_build_github_links(self):
        for value, expected in (
                ("ebony-vinyl/dsh-our-free-model",
                 "https://github.com/ebony-vinyl/dsh-our-free-model"),
                ("Some-Owner/Repo.Name", "https://github.com/Some-Owner/Repo.Name"),
                ("a/b", "https://github.com/a/b"),
                ("  owner/repo  ", "https://github.com/owner/repo")):
            entry = hubcatalog.normalize(raw("x", r=value))
            self.assertEqual(entry["repositoryUrl"], expected)
            self.assertEqual(entry["id"], expected[len("https://github.com/"):])

    def test_link_is_recomputed_not_taken_from_the_input(self):
        """规整过的数据不能被透传当成链接，否则校验就有了后门。"""
        tampered = dict(SAMPLE_EXPECTED, repositoryUrl="javascript:alert(1)")
        self.assertEqual(hubcatalog.normalize(tampered)["repositoryUrl"],
                         "https://github.com/ebony-vinyl/dsh-our-free-model")
        self.assertEqual(
            hubcatalog.normalize({"s": "x", "repositoryUrl": "javascript:alert(1)"}
                                 )["repositoryUrl"], "")
        self.assertEqual(hubcatalog.normalize(
            dict(SAMPLE_EXPECTED, repo="evil.example/x"))["repositoryUrl"], "",
            "owner 里的点不合法，不能拼出链接")


# ---------------------------------------------------------------- 安装命令

class InstallTargetTestCase(unittest.TestCase):
    """粘贴进来的整条命令要能收敛成裸安装目标。"""

    def test_every_documented_shape(self):
        table = (
            ("dsh plugin --profile web add github:ebony-vinyl/dsh-our-free-model",
             "github:ebony-vinyl/dsh-our-free-model"),
            ("dsh plugin add dsh-plugin", "dsh-plugin"),
            ("@howmp/dsh-pentest", "@howmp/dsh-pentest"),
            ("npm:some-pkg@1.2.3", "npm:some-pkg@1.2.3"),
            ("  github:owner/repo  ", "github:owner/repo"),
            ("", ""),
            (None, ""),
            (123, ""),
            (["github:o/r"], ""),
            ({"ic": "dsh plugin add x"}, ""),
        )
        for spec, expected in table:
            self.assertEqual(hubcatalog.parse_install_target(spec), expected,
                             "输入 %r 的解析结果不对" % (spec,))

    def test_cli_variants_and_trailing_tokens(self):
        """`--profile` 在 `add` 前后、`=` 写法、以及尾巴上的开关都要能认。"""
        table = (
            ("dsh plugin --profile=web add github:o/r", "github:o/r"),
            ("dsh plugin add --profile web github:o/r", "github:o/r"),
            ("dsh plugin add github:o/r --save", "github:o/r"),
            ("dsh add dsh-plugin", "dsh-plugin"),
            ("add github:o/r", "github:o/r"),
            ("--profile web add github:o/r", "github:o/r"),
            ("dsh plugin --profile web add github:o/r\n", "github:o/r"),
            ("DSH PLUGIN --profile web ADD github:o/r", "github:o/r"),
        )
        for spec, expected in table:
            self.assertEqual(hubcatalog.parse_install_target(spec), expected,
                             "输入 %r 的解析结果不对" % (spec,))

    def test_empty_or_option_only_specs_give_nothing(self):
        """命令里没有目标时给空串，不能把某个开关当成要安装的包。"""
        for spec in ("dsh plugin add", "dsh plugin", "--save", "--profile web",
                     "   ", "\n"):
            self.assertEqual(hubcatalog.parse_install_target(spec), "",
                             "输入 %r 不该有目标" % (spec,))

    def test_entry_uses_install_command_then_raw_repo(self):
        """有安装命令就用命令，没有才退回原始 `r`。"""
        self.assertEqual(hubcatalog.normalize(SAMPLE)["installTarget"],
                         "github:ebony-vinyl/dsh-our-free-model")
        self.assertEqual(
            hubcatalog.normalize({"s": "x", "r": "owner/repo"})["installTarget"],
            "owner/repo")
        self.assertEqual(hubcatalog.normalize({"s": "x"})["installTarget"], "")


# ---------------------------------------------------------------- 检索

class SearchTestCase(unittest.TestCase):
    """多词 AND、大小写、分类、已核验，以及 total 与 count 的分工。"""

    def test_multi_term_query_requires_every_term(self):
        """两个词都要命中：只含其中一个的条目不出现。"""
        one = hubcatalog.search(catalog(), query="dsh")
        self.assertEqual(one["total"], 2, "单词查询是 AND 的对照组")
        both = hubcatalog.search(catalog(), query="dsh panel")
        self.assertEqual((both["total"], slugs(both)), (1, ["dsh-usage-panel"]))
        other = hubcatalog.search(catalog(), query="模型 路由")
        self.assertEqual((other["total"], slugs(other)), (1, ["model-router"]))

    def test_query_is_case_insensitive(self):
        for query, expected in (("DSH-USAGE-PANEL", 1), ("PaNeL", 1),
                                ("MODEL", 2), ("DeepSeek", 1)):
            self.assertEqual(hubcatalog.search(catalog(), query=query)["total"],
                             expected, "查询 %r 的命中数不对" % (query,))

    def test_query_covers_the_documented_fields(self):
        """名称、slug、owner、描述、标签、特性都在匹配范围内。"""
        for query, expected in (("acme", "dsh-usage-panel"),
                                ("deepseek", "dsh-task-runner"),
                                ("批量下发任务", "dsh-task-runner"),
                                ("在多个模型之间路由", "model-router"),
                                ("dsh-task-runner", "dsh-task-runner"),
                                ("按成本选模型", "model-router")):
            page = hubcatalog.search(catalog(), query=query)
            self.assertEqual((page["total"], slugs(page)), (1, [expected]),
                             "查询 %r 的结果不对" % (query,))

    def test_category_filter_is_exact_and_case_insensitive(self):
        """分类是整串精确匹配，不做前缀；大小写与两侧空白都不影响判定。"""
        for value, expected in (("TOOLS", ["dsh-task-runner"]),
                                ("tools", ["dsh-task-runner"]),
                                (" tools ", ["dsh-task-runner"]),
                                ("model", ["dsh-usage-panel", "model-router"])):
            page = hubcatalog.search(catalog(), category=value)
            self.assertEqual(sorted(slugs(page)), sorted(expected),
                             "分类 %r 的结果不对" % (value,))
        # 前缀不算命中：分类是枚举值，不是自由文本
        self.assertEqual(hubcatalog.search(catalog(), category="tool")["total"], 0)

    def test_non_string_filters_are_ignored(self):
        """读不成条件的过滤值视作没给，而不是让整页变空。"""
        for value in (None, 123, ["model"], {"c": "model"}):
            self.assertEqual(hubcatalog.search(catalog(), query=value)["total"], 3)
            self.assertEqual(hubcatalog.search(catalog(), category=value)["total"], 3)

    def test_verified_only_filters_on_the_flag(self):
        page = hubcatalog.search(catalog(), verified_only=True)
        self.assertEqual((page["total"], slugs(page)), (1, ["dsh-usage-panel"]))
        self.assertTrue(all(entry["verified"] for entry in page["items"]))
        self.assertEqual(hubcatalog.search(catalog())["total"], 3)
        self.assertEqual(
            hubcatalog.search(catalog(), category="model",
                              verified_only=True)["total"], 1)

    def test_total_counts_before_paging_while_count_is_the_page(self):
        """一个数字同时承担两种含义，是分页界面最常见的错误来源。"""
        first = hubcatalog.search(catalog(), limit=2)
        self.assertEqual((first["total"], first["count"], len(first["items"])),
                         (3, 2, 2))
        self.assertEqual((first["limit"], first["offset"], first["hasMore"]),
                         (2, 0, True))
        second = hubcatalog.search(catalog(), limit=2, offset=2)
        self.assertEqual((second["total"], second["count"], second["hasMore"]),
                         (3, 1, False))

    def test_empty_query_returns_the_whole_catalog(self):
        page = hubcatalog.search(catalog())
        self.assertEqual((page["total"], page["count"]), (3, 3))

    def test_non_list_input_gives_an_empty_page(self):
        for value in (None, "junk", 7, {"s": "x"}, True):
            page = hubcatalog.search(value)
            self.assertEqual((page["total"], page["count"]), (0, 0))
            self.assertEqual((page["items"], page["hasMore"]), ([], False))
            self.assertEqual((page["limit"], page["offset"]),
                             (hubcatalog.DEFAULT_PAGE, 0))


# ---------------------------------------------------------------- 翻页

class PagingTestCase(unittest.TestCase):
    """"逐页取回的 id 拼起来恰好等于全集"——顺序不稳这个前提下唯一能验的事。"""

    def test_pages_do_not_overlap_and_cover_everything(self):
        items = pageable()
        collected = []
        for offset in (0, 3, 6):
            page = hubcatalog.search(items, limit=3, offset=offset)
            self.assertEqual(page["total"], 7)
            self.assertEqual(page["count"], 3 if offset < 6 else 1)
            self.assertEqual(page["hasMore"], offset < 6)
            collected.extend(entry["id"] for entry in page["items"])
        self.assertEqual(collected, ["acme/plugin-%s" % letter for letter in "abcdefg"])
        self.assertEqual(len(set(collected)), 7, "翻页不能重复也不能漏")

    def test_offset_past_the_end_is_an_empty_page_with_the_right_total(self):
        for offset in (7, 8, 99, 1000):
            page = hubcatalog.search(pageable(), limit=3, offset=offset)
            self.assertEqual((page["items"], page["count"], page["hasMore"]),
                             ([], 0, False))
            self.assertEqual((page["total"], page["offset"]), (7, offset))

    def test_an_already_returned_page_can_be_paged_again(self):
        """调用方拿到的是规整后的条目，把它们当输入再翻页必须照常工作。"""
        first = hubcatalog.search(pageable(), limit=3)["items"]
        self.assertEqual([entry["slug"] for entry in first],
                         ["plugin-a", "plugin-b", "plugin-c"])
        beyond = hubcatalog.search(first, limit=3, offset=5)
        self.assertEqual((beyond["total"], beyond["count"], beyond["hasMore"]),
                         (3, 0, False))
        inside = hubcatalog.search(first, limit=2, offset=1)
        self.assertEqual([entry["slug"] for entry in inside["items"]],
                         ["plugin-b", "plugin-c"])


# ---------------------------------------------------------------- 排序

class OrderingTestCase(unittest.TestCase):
    """全序：已验证 → 时间新 → 名称 → id；且与输入排列无关。"""

    def fixture(self):
        return [
            raw("beta-new", n="Beta", u="2026-10-10T00:00:00Z"),
            raw("alpha-old", n="Alpha", u="2026-10-05T00:00:00Z"),
            raw("delta-notime", n="Delta", u=None),
            raw("zulu-garbage-time", n="Zulu", u="not-a-date"),
            raw("gamma-unverified-new", n="Gamma", u="2026-10-20T00:00:00Z",
                v="unverified"),
            raw("epsilon-unverified-notime", n="Epsilon", u=None, v="unverified"),
        ]

    def test_order_is_verified_then_newest_then_name(self):
        page = hubcatalog.search(self.fixture(), limit=10)
        self.assertEqual(names(page),
                         ["Beta", "Alpha", "Delta", "Zulu", "Gamma", "Epsilon"])

    def test_entries_without_a_usable_timestamp_sort_last_in_their_group(self):
        """没有时间戳（或有但读不出来）的条目垫底，而不是靠字典序霸占首页。"""
        page = hubcatalog.search(self.fixture(), limit=10)
        self.assertLess(names(page).index("Alpha"), names(page).index("Zulu"))
        self.assertEqual(names(page)[-1], "Epsilon")

    def test_input_order_does_not_change_the_page(self):
        """Hub 数组自身的顺序在两次抓取之间会变，结果不能跟着变。"""
        expected = names(hubcatalog.search(self.fixture(), limit=10))
        reversed_input = list(reversed(self.fixture()))
        self.assertEqual(names(hubcatalog.search(reversed_input, limit=10)), expected)
        shuffled = self.fixture()
        random.Random(20261013).shuffle(shuffled)
        self.assertEqual(names(hubcatalog.search(shuffled, limit=10)), expected)
        # 反序输入下"第 2 页"也必须是同一批条目
        self.assertEqual(
            names(hubcatalog.search(reversed_input, limit=2, offset=2)),
            expected[2:4])

    def test_timestamps_are_compared_as_instants_not_as_strings(self):
        """同一时刻的两种写法（时区偏移、小数秒）必须判为并列，再按名称定序。"""
        offsets = [raw("z-offset", n="Zed", u="2026-10-11T08:26:18+02:00"),
                   raw("a-zulu", n="Ann", u="2026-10-11T06:26:18Z")]
        self.assertEqual(names(hubcatalog.search(offsets, limit=10)), ["Ann", "Zed"])
        fraction = [raw("b-frac", n="Bee", u="2026-10-11T06:26:18.000Z"),
                    raw("c-z", n="Cee", u="2026-10-11T06:26:18Z")]
        self.assertEqual(names(hubcatalog.search(fraction, limit=10)), ["Bee", "Cee"])

    def test_name_tiebreak_ignores_case_then_id_breaks_the_tie(self):
        same = [raw("x-zeta", n="zeta"), raw("x-alpha", n="Alpha")]
        self.assertEqual(names(hubcatalog.search(same, limit=10)), ["Alpha", "zeta"])
        twins = [raw("b-twin", n="Same", o="acme"), raw("a-twin", n="Same", o="acme")]
        self.assertEqual([entry["id"] for entry in
                          hubcatalog.search(twins, limit=10)["items"]],
                         ["acme/a-twin", "acme/b-twin"])

    def test_updated_at_field_keeps_the_raw_text(self):
        """排序用的规范化值不外泄：`updatedAt` 仍是去空白的原文。"""
        self.assertEqual(hubcatalog.normalize(raw("x"))["updatedAt"], SAME_TIME)
        self.assertEqual(hubcatalog.normalize(raw("x", u=" not-a-date "))["updatedAt"],
                         "not-a-date")


# ---------------------------------------------------------------- 页大小

class LimitClampTestCase(unittest.TestCase):
    """`limit` 永远落在 1..MAX_PAGE；坏值退回默认值，超上限则截断。"""

    def test_documented_fallbacks(self):
        for value, expected in ((0, hubcatalog.DEFAULT_PAGE),
                                (-5, hubcatalog.DEFAULT_PAGE),
                                ("abc", hubcatalog.DEFAULT_PAGE),
                                (None, hubcatalog.DEFAULT_PAGE),
                                ("0", hubcatalog.DEFAULT_PAGE),
                                ("-3", hubcatalog.DEFAULT_PAGE),
                                (True, hubcatalog.DEFAULT_PAGE),
                                (3.5, hubcatalog.DEFAULT_PAGE),
                                (99999, hubcatalog.MAX_PAGE),
                                (201, hubcatalog.MAX_PAGE),
                                (200, hubcatalog.MAX_PAGE),
                                ("12", 12),
                                (5, 5)):
            page = hubcatalog.search(pageable(), limit=value)
            self.assertEqual(page["limit"], expected, "limit=%r 的收敛结果不对"
                             % (value,))

    def test_result_stays_within_bounds_for_any_input(self):
        """任何输入都不得抛出，也不得返回空页或超限页。"""
        for value in (0, -5, 99999, "abc", None, "", [], {}, object(),
                      float("nan"), float("inf"), "1e400", "--", 1, 200):
            page = hubcatalog.search(pageable(), limit=value)
            self.assertTrue(1 <= page["limit"] <= hubcatalog.MAX_PAGE)
            self.assertEqual(page["count"], min(7, page["limit"]))
            self.assertEqual(page["total"], 7)

    def test_offset_is_clamped_to_non_negative(self):
        for value in (-3, -1, "abc", None, 1.5, [], {}, float("nan")):
            page = hubcatalog.search(pageable(), offset=value)
            self.assertEqual(page["offset"], 0, "offset=%r 的收敛结果不对" % (value,))
            self.assertEqual(page["count"], 7)
        self.assertEqual(hubcatalog.search(pageable(), offset="3")["count"], 4)


# ---------------------------------------------------------------- 分类

class CategoriesTestCase(unittest.TestCase):
    """分类计数：空分类不计，大小写变体合并，按数量降序、id 升序。"""

    def fixture(self):
        return [raw("a"), raw("b"), raw("c", c="Model"), raw("d"),
                raw("e", c=""), raw("f", c="api"), raw("g", c="zoo"),
                raw("h", c="api"), raw("i", c="zoo"), raw("j"),
                raw("k", c=None), raw("l", c=123)]

    def test_counts_skip_empty_and_merge_case_variants(self):
        self.assertEqual(hubcatalog.categories(self.fixture()), [
            {"id": "model", "label": "model", "count": 5},
            {"id": "api", "label": "api", "count": 2},
            {"id": "zoo", "label": "zoo", "count": 2},
            {"id": "123", "label": "123", "count": 1},
        ])

    def test_row_shape_and_types(self):
        for row in hubcatalog.categories(self.fixture()):
            self.assertEqual(sorted(row), ["count", "id", "label"])
            self.assertIsInstance(row["count"], int)
            self.assertIsInstance(row["id"], str)
            self.assertTrue(row["id"])

    def test_counts_match_the_category_filter(self):
        """点进某个分类拿到的条数必须等于列表上的数字，否则用户会认为计数坏了。"""
        for row in hubcatalog.categories(self.fixture()):
            page = hubcatalog.search(self.fixture(), category=row["id"], limit=200)
            self.assertEqual(page["total"], row["count"], "分类 %s 对不上" % row["id"])

    def test_non_list_input_gives_an_empty_list(self):
        for value in (None, "junk", 7, {"c": "model"}):
            self.assertEqual(hubcatalog.categories(value), [])


# ---------------------------------------------------------------- 面板统计

class SummarizeTestCase(unittest.TestCase):
    """统计接口与本地条数合成面板数据：拿不到就用本地值，绝不编造。"""

    def test_well_formed_payload(self):
        self.assertEqual(
            hubcatalog.summarize({"total": 13680, "verified": 13680}, 13680),
            {"total": 13680, "verified": 13680, "catalogCount": 13680})

    def test_empty_payload_falls_back_to_the_local_count(self):
        self.assertEqual(hubcatalog.summarize({}, 7),
                         {"total": 7, "verified": 0, "catalogCount": 7})

    def test_numeric_strings_are_used_and_malformed_ones_fall_back(self):
        self.assertEqual(
            hubcatalog.summarize({"total": "13680", "verified": "12"}, 500),
            {"total": 13680, "verified": 12, "catalogCount": 500})
        self.assertEqual(
            hubcatalog.summarize({"total": "很多", "verified": None}, 5),
            {"total": 5, "verified": 0, "catalogCount": 5})
        self.assertEqual(
            hubcatalog.summarize({"total": None, "verified": "12 条"}, 5),
            {"total": 5, "verified": 0, "catalogCount": 5})

    def test_non_dict_payload_is_treated_as_absent(self):
        for value in (None, "13680", [{"total": 13680}], 13680, True):
            self.assertEqual(hubcatalog.summarize(value, 5),
                             {"total": 5, "verified": 0, "catalogCount": 5})

    def test_zero_is_a_real_value_not_a_missing_one(self):
        """读到 0 就是 0：把它当"没读到"会让空市场显示成有 5 条。"""
        self.assertEqual(hubcatalog.summarize({"total": 0, "verified": 0}, 5),
                         {"total": 0, "verified": 0, "catalogCount": 5})

    def test_both_numbers_are_reported_when_they_disagree(self):
        """两个来源不一致时都给出：界面才能显示"13680 条中已载入 500 条"。"""
        self.assertEqual(hubcatalog.summarize({"total": 13680, "verified": 13680}, 500),
                         {"total": 13680, "verified": 13680, "catalogCount": 500})


# ---------------------------------------------------------------- 规整后的输入

class NormalizedInputTestCase(unittest.TestCase):
    """整份目录只规整一次、之后反复检索：这个用法必须成立且稳定。"""

    def test_normalize_is_idempotent(self):
        once = hubcatalog.normalize(SAMPLE)
        self.assertEqual(hubcatalog.normalize(once), once)
        self.assertEqual(hubcatalog.normalize_all([once]), [once])

    def test_normalized_entries_can_be_searched(self):
        once = hubcatalog.normalize_all([SAMPLE,
                                         raw("other", n="Other plugin", c="tools")])
        page = hubcatalog.search(once, query="free model")
        self.assertEqual([entry["slug"] for entry in page["items"]],
                         ["dsh-our-free-model"])
        self.assertEqual(hubcatalog.search(once, category="model")["total"], 1)

    def test_normalized_entry_without_identity_is_dropped(self):
        self.assertIsNone(hubcatalog.normalize({"id": "", "slug": "", "name": ""}))
        self.assertIsNone(hubcatalog.normalize(SAMPLE_EXPECTED["id"]))


# ---------------------------------------------------------------- 规模

class ScaleTestCase(unittest.TestCase):
    """整份目录的规模：13680 条是常态，不是边角情况。"""

    def test_thirteen_thousand_entries_are_handled(self):
        items = [{"s": "plugin-%05d" % index, "o": "bulk",
                  "n": "plugin-%05d" % index,
                  "c": "model" if index % 2 else "tools",
                  "v": "verified" if index % 3 else "pending",
                  "u": "2026-10-%02dT00:00:00Z" % (index % 28 + 1)}
                 for index in range(13000)]

        catalog_all = hubcatalog.normalize_all(items)
        self.assertEqual(len(catalog_all), 13000)
        self.assertEqual(hubcatalog.search(items)["total"], 13000)

        # 五位零填充下，名字里含 `plugin-12` 的只有 12000–12999 这一千条
        self.assertEqual(hubcatalog.search(items, query="plugin-12")["total"], 1000)
        # i % 3 == 0 的 4334 条判为未核验
        self.assertEqual(hubcatalog.search(items, verified_only=True)["total"], 8666)
        self.assertEqual(hubcatalog.search(items, category="tools")["total"], 6500)

        first = hubcatalog.search(items, limit=hubcatalog.MAX_PAGE)
        second = hubcatalog.search(items, limit=hubcatalog.MAX_PAGE,
                                   offset=hubcatalog.MAX_PAGE)
        self.assertEqual((first["count"], second["count"], first["hasMore"]),
                         (200, 200, True))
        self.assertFalse({entry["id"] for entry in first["items"]} &
                         {entry["id"] for entry in second["items"]})

        self.assertEqual([(row["id"], row["count"])
                          for row in hubcatalog.categories(items)],
                         [("model", 6500), ("tools", 6500)])


if __name__ == "__main__":
    unittest.main()
