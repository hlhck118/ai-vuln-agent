# -*- coding: utf-8 -*-
"""立足点登记表：形状契约、默认视图、关闭语义、旧库自愈。

这一组用例盯的是几件**从界面看不出来**的事：

1. **读回来必须能喂回 `pivot.Foothold.from_dict()`。** 这是跨模块的形状契约：写入
   路径与读取路径分属两个模块，任何一边改了字段名，症状都不是报错，而是"下游拿到
   一条字段全空的登记记录，然后按默认种类去建通道"——错误在流量真的发出去时才现形。
   所以每条用例读回来之后都顺手 `from_dict` 一次。
2. **`scope` / `tags` 读回来必须是 list。** 存的是 JSON 文本，若读取层忘了反序列化，
   下游拿到的是一串 `'["10.0.0.0/8"]'`——它会当成"一个叫这个奇怪名字的网段"原样
   接受，而不是报错。这是最容易被静默吞掉的一类形状错误。
3. **默认视图只列活着的。** 已关闭的立足点被混进列表，下游就会去连一台不存在的通道；
   而那种失败发生在真正要借道的那一刻，离原因很远。
4. **旧库打开要自动长出这张表，且原有数据完好。** schema 9 的库在新代码下跑一次就
   该能用；不行的话症状是**整个库打不开**，而不是少一个功能。
"""
import json
import os
import shutil
import sqlite3
import sys
import tempfile
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import pivot                                            # noqa: E402
from pentestd.models import ValidationError                           # noqa: E402
from pentestd.store import SCHEMA_VERSION, Store                      # noqa: E402


def sample(kind: str = "ssh", host: str = "10.1.2.3", port: int = 22,
           capability: str = "socks5", note: str = "内网跳板",
           scope=("10.1.0.0/16", "172.16.0.0/12"),
           tags=("lateral", "jumpbox")):
    """一个形状完整的立足点样本（经 `pivot.build_foothold` 生成真实 id）。"""
    return pivot.build_foothold(
        kind, host, port, note=note, scope=list(scope), tags=list(tags),
        capability=capability)


class FootholdStoreTestCase(unittest.TestCase):
    """登记表的基本读写。每个用例一个临时库，避免互相污染。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="footholds-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = Store(os.path.join(self.dir, "store.db"))
        self.addCleanup(self.store.close)

    def test_schema_version_covers_this_release(self):
        """footholds 引入时版本提到 10；此后只增不减。

        写死成 `== 10` 会让"下一张表"的每一轮都来改这一行——而它要守的其实是
        **下界**（"footholds 那一版已经生效"）。精确的那个数字归各自新表的用例管：
        `test_authz_ops` 里有一条同时校验 DDL 与版本号的守卫，那才是"加了表忘了
        提版本"真正会漏掉的地方（只建表不提版本的症状是**旧程序愿意打开这个库**）。
        """
        self.assertGreaterEqual(SCHEMA_VERSION, 10)

    def test_add_then_read_back_keeps_every_field(self):
        """加了什么读回来就是什么，且能原样喂回 `Foothold.from_dict`。

        逐个字段比而不是整体比字典：整体比在失败时只告诉你"不一样"，而这里想知道的是
        **哪一个字段漂了**——写错列、读错列、漏一列的症状各不相同。
        """
        foothold = sample()
        stored = self.store.add_foothold("E-1", foothold.to_dict(), job_id="J-1")
        self.assertIsNotNone(stored)
        for key, expected in foothold.to_dict().items():
            self.assertEqual(stored[key], expected,
                             "字段 %s 读回来与写进去的不一致" % key)
        self.assertEqual(stored["engagementId"], "E-1")
        self.assertEqual(stored["jobId"], "J-1")
        self.assertEqual(stored["status"], "open")
        self.assertIsNone(stored["closedAt"])
        # 跨模块契约：读回来必须能构造回 pivot 的对象，否则下游根本用不了这条记录。
        rebuilt = pivot.Foothold.from_dict(stored)
        self.assertEqual(rebuilt.id, foothold.id)
        self.assertEqual(rebuilt.scope, foothold.scope)
        self.assertEqual(rebuilt.tags, foothold.tags)
        self.assertEqual(rebuilt.summary(), foothold.summary())

    def test_scope_and_tags_stay_lists(self):
        """`scope`/`tags` 是 JSON 文本落库的，读回来必须是 list 而不是那串文本。"""
        stored = self.store.add_foothold("E-1", sample().to_dict())
        self.assertIsInstance(stored["scope"], list)
        self.assertIsInstance(stored["tags"], list)
        self.assertEqual(stored["scope"], ["10.1.0.0/16", "172.16.0.0/12"])
        self.assertEqual(stored["tags"], ["lateral", "jumpbox"])
        self.assertEqual(1, len(self.store.list_footholds()))

    def test_id_is_generated_when_missing(self):
        """id 为空时由存储层生成，且带种类前缀（让人一屏看出这是哪一类）。"""
        record = sample().to_dict()
        record["id"] = ""
        stored = self.store.add_foothold("E-1", record)
        self.assertTrue(stored["id"], "存储层没有补出 id")
        self.assertTrue(stored["id"].startswith("ssh-"),
                        "id 应带种类前缀，实际为 %r" % stored["id"])

    def test_re_observation_overwrites_instead_of_duplicating(self):
        """同 id 重复登记是**覆盖**：同一台机器被第二次看到不是新事实。

        同时钉住两件容易做错的事：`created_at` 不被改写（否则记录会在列表里自己换
        位置），以及覆盖会清掉关闭状态（同一通道又在了，不该继续被当成已关闭）。
        """
        foothold = sample()
        first = self.store.add_foothold("E-1", foothold.to_dict())
        self.store.close_foothold(foothold.id, "口令失效")

        record = foothold.to_dict()
        record["note"] = "第二次观测：多了 192.168.0.0/16"
        record["scope"] = ["10.1.0.0/16", "192.168.0.0/16"]
        again = self.store.add_foothold("E-1", record)

        self.assertEqual(1, len(self.store.list_footholds(include_closed=True)),
                         "重复登记产生了第二条记录")
        self.assertEqual(again["note"], record["note"])
        self.assertEqual(again["scope"], ["10.1.0.0/16", "192.168.0.0/16"])
        self.assertEqual(again["createdAt"], first["createdAt"],
                         "重复登记改写了 createdAt，记录会在列表里自己换位置")
        self.assertEqual(again["status"], "open",
                         "重新观测到的通道仍是已关闭状态，下游会拒绝使用它")
        self.assertIsNone(again["closedAt"])

    def test_explicit_created_at_is_honoured(self):
        """调用方给了 `created_at` 就用它——批量导入历史记录靠这一条。"""
        stored = self.store.add_foothold("E-1", sample().to_dict(),
                                         created_at=1700000000.5)
        self.assertEqual(stored["createdAt"], 1700000000.5)

    def test_bad_kind_is_rejected_before_it_reaches_the_table(self):
        """拼错的 kind 必须当场抛错，而不是被静默存成一条"能用"的记录。

        这是登记表被下游当作"下一跳往哪走"唯一依据的直接后果：一条 `SOCK5` 若落到
        库里，错误会在流量已经按错误方式发出去的时刻才现形。
        """
        record = sample().to_dict()
        record["kind"] = "SOCK5"
        with self.assertRaises(pivot.PivotError):
            self.store.add_foothold("E-1", record)
        self.assertEqual([], self.store.list_footholds(),
                         "被拒的写入不得留下半条记录")

    def test_non_dict_input_is_a_validation_error(self):
        with self.assertRaises(ValidationError):
            self.store.add_foothold("E-1", ["not", "a", "dict"])

    def test_missing_host_is_rejected(self):
        record = sample().to_dict()
        record["host"] = "   "
        with self.assertRaises(pivot.PivotError):
            self.store.add_foothold("E-1", record)

    def test_get_unknown_id_is_none(self):
        self.assertIsNone(self.store.get_foothold("ssh-不存在"))

    # ------------------------------------------------------------ 默认视图
    def test_default_view_hides_closed_ones(self):
        """默认只列活着的：已关闭的通道出现在列表里，下游会去连一台不存在的机器。"""
        alive = sample(host="10.1.2.3")
        dead = sample(host="10.1.2.4")
        self.store.add_foothold("E-1", alive.to_dict())
        self.store.add_foothold("E-1", dead.to_dict())
        self.assertTrue(self.store.close_foothold(dead.id, "目标已下线"))

        open_ids = [item["id"] for item in self.store.list_footholds()]
        self.assertEqual([alive.id], open_ids)
        both = [item["id"] for item in self.store.list_footholds(include_closed=True)]
        self.assertEqual(sorted([alive.id, dead.id]), sorted(both))

    def test_close_is_a_one_way_transition(self):
        """关掉之后能看到 status/reason/closed_at，且**重复关闭不再生效**。

        重复关闭若生效，第二次的 reason 会覆盖第一次的——于是"这台跳板因为口令失效
        断的"变成"刚刚因为已处理关的"，而后者是操作者的主观结论，不是事实。
        """
        foothold = sample()
        self.store.add_foothold("E-1", foothold.to_dict())
        self.assertTrue(self.store.close_foothold(foothold.id, "口令失效"))

        closed = self.store.get_foothold(foothold.id)
        self.assertEqual(closed["status"], "closed")
        self.assertEqual(closed["reason"], "口令失效")
        self.assertIsInstance(closed["closedAt"], float)
        self.assertTrue(closed["closedAt"] > 0)
        # 关掉之后依旧要能喂回 pivot —— 已关闭的记录仍可能被拿到（历史复盘）。
        self.assertEqual(pivot.Foothold.from_dict(closed).id, foothold.id)

        self.assertFalse(self.store.close_foothold(foothold.id, "已处理"),
                         "已经关掉的立足点被重复关闭了")
        self.assertEqual(self.store.get_foothold(foothold.id)["reason"], "口令失效",
                         "重复关闭覆盖了第一次的原因，审计线索被抹掉")

    def test_close_of_a_missing_id_reports_false(self):
        self.assertFalse(self.store.close_foothold("ssh-不存在", "随便"))

    # ------------------------------------------------------------ 过滤与排序
    def test_filter_by_engagement(self):
        """按作业过滤：两个作业的立足点是两份互不相干的事实。"""
        first = sample(host="10.1.2.3")
        second = sample(host="10.1.2.4")
        self.store.add_foothold("E-1", first.to_dict())
        self.store.add_foothold("E-2", second.to_dict())
        self.assertEqual([first.id],
                         [item["id"] for item in self.store.list_footholds("E-1")])
        self.assertEqual([second.id],
                         [item["id"] for item in self.store.list_footholds("E-2")])
        self.assertEqual(2, len(self.store.list_footholds()))
        self.assertEqual([], self.store.list_footholds("E-3"))

    def test_engagement_filter_does_not_leak_closed_ones(self):
        """两个过滤条件是 AND：按作业看时，已关闭的默认同样不出现。"""
        foothold = sample()
        self.store.add_foothold("E-1", foothold.to_dict())
        self.store.close_foothold(foothold.id)
        self.assertEqual([], self.store.list_footholds("E-1"))
        self.assertEqual(1, len(self.store.list_footholds("E-1", include_closed=True)))

    def test_list_is_newest_first_and_stable_on_ties(self):
        """按 `created_at DESC` 排，同秒用 id 兜底——顺序随机会让截图比对无意义。"""
        older = sample(host="10.1.2.3")
        newer = sample(host="10.1.2.4")
        same_a = sample(host="10.1.2.5")
        same_b = sample(host="10.1.2.6")
        self.store.add_foothold("E-1", older.to_dict(), created_at=1000.0)
        self.store.add_foothold("E-1", newer.to_dict(), created_at=2000.0)
        self.store.add_foothold("E-1", same_a.to_dict(), created_at=1500.0)
        self.store.add_foothold("E-1", same_b.to_dict(), created_at=1500.0)

        order = [item["id"] for item in self.store.list_footholds()]
        self.assertEqual([newer.id, sorted([same_a.id, same_b.id])[0],
                          sorted([same_a.id, same_b.id])[1], older.id], order)
        # 再读一次必须一模一样（SQLite 不保证稳定排序，兜底键就是为这个加的）。
        self.assertEqual(order,
                         [item["id"] for item in self.store.list_footholds()])

    def test_kinds_are_queryable_through_the_flat_column(self):
        """平铺 `kind` 列的意义就是能查——"这个作业里所有 ssh 立足点"。

        顺带钉住一个容易被默认值盖住的事实：`capability`（**以什么方式给出出口**）与
        `kind`（**这是哪一类资产**）是两列而不是一回事——一台 SSH 主机可以用它开
        SOCKS5 出口。所以这里的 webshell 立足点能力写成 `webshell`，而它的 kind 也是
        `webshell`；查询要能按两列各自过滤。
        """
        self.store.add_foothold("E-1", sample(kind="ssh").to_dict())
        self.store.add_foothold(
            "E-1", sample(kind="webshell", port=80, capability="webshell").to_dict())
        rows = self.store.conn.execute(
            "SELECT id,kind,host,port,capability,status FROM footholds "
            "WHERE kind='ssh'").fetchall()
        self.assertEqual(1, len(rows))
        self.assertEqual("ssh", rows[0]["kind"])
        self.assertEqual(22, rows[0]["port"])
        self.assertEqual("socks5", rows[0]["capability"],
                         "能力与种类是两列，不该被当成同一个值")
        webshells = self.store.conn.execute(
            "SELECT id FROM footholds WHERE capability='webshell'").fetchall()
        self.assertEqual(1, len(webshells))

    def test_corrupt_payload_does_not_break_the_list(self):
        """一条坏 JSON 不该让整张登记表读不出来。

        登记表是列表页的唯一数据源；抛异常会把错误锁在页面上——用户连"把这条关掉"
        这个动作都做不了。所以坏行退化成字段不全的记录，而不是让整个视图 500。
        """
        good = sample()
        self.store.add_foothold("E-1", good.to_dict())
        self.store.conn.execute("UPDATE footholds SET payload='{不是 JSON'")
        self.store.conn.commit()
        listed = self.store.list_footholds()
        self.assertEqual(1, len(listed))
        # 平铺列仍然是权威来源，所以核心字段依然完好。
        self.assertEqual(listed[0]["id"], good.id)
        self.assertEqual(listed[0]["host"], good.host)
        self.assertEqual(listed[0]["scope"], ["10.1.0.0/16", "172.16.0.0/12"])


class LegacyDatabaseTestCase(unittest.TestCase):
    """旧库（schema 9）在新代码下必须直接可用：表自动补齐，原有数据一件不少。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="footholds-legacy-")
        self.addCleanup(shutil.rmtree, self.dir, True)

    def _legacy_nine(self, path: str) -> None:
        """造一个 schema 9 的库：有作业与对话表，**没有 footholds**。

        刻意不建全 15 张表：真正要验的是"新表能长出来、且已有数据还在"，而全部照抄
        DDL 会让这个用例在 DDL 改一次之后跟着改——那时它就不再是"旧库"了。
        """
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE engagements (id TEXT PRIMARY KEY, target TEXT NOT "
                    "NULL, objective TEXT DEFAULT '', authorization TEXT DEFAULT '', "
                    "created_at REAL, updated_at REAL)")
        raw.execute("INSERT INTO engagements(id,target,created_at,updated_at) "
                    "VALUES('E-old','192.168.1.0/24',111.0,222.0)")
        raw.execute("CREATE TABLE chat_sessions (id TEXT PRIMARY KEY, "
                    "title TEXT DEFAULT '', user_id TEXT DEFAULT '', "
                    "engagement_id TEXT DEFAULT '', created_at REAL, updated_at REAL)")
        raw.execute("PRAGMA user_version=9")
        raw.commit()
        raw.close()

    def test_legacy_database_gains_the_table_and_keeps_its_data(self):
        path = os.path.join(self.dir, "legacy.db")
        self._legacy_nine(path)
        store = Store(path)
        self.addCleanup(store.close)

        # 1) 新表长出来了，而且立刻可用。
        self.assertTrue(store._table_exists("footholds"))
        foothold = sample()
        stored = store.add_foothold("E-old", foothold.to_dict())
        self.assertEqual(stored["id"], foothold.id)
        self.assertEqual([foothold.id],
                         [item["id"] for item in store.list_footholds("E-old")])
        # 2) 版本号被提到当前值。
        self.assertEqual(int(store.conn.execute(
            "PRAGMA user_version").fetchone()[0]), SCHEMA_VERSION)
        # 3) 旧数据一件不少——"自动补表"最怕的是顺手把库重建了一遍。
        meta = store.get_engagement_meta("E-old")
        self.assertEqual(meta["target"], "192.168.1.0/24")
        self.assertEqual(meta["createdAt"], 111.0)
        self.assertEqual(meta["updatedAt"], 222.0)

    def test_partial_table_gets_the_missing_columns(self):
        """结构自愈：表在、但缺 `status` / `closed_at` 的库也要能开。

        `CREATE TABLE IF NOT EXISTS` 对已存在的表什么都不做，所以任何在本机开发期
        建过这张表的库都不会自己长出这两列。缺 `status` 的症状不是"少个字段"，
        而是立足点列表直接 `no such column` 打不开。
        """
        path = os.path.join(self.dir, "partial.db")
        raw = sqlite3.connect(path)
        raw.execute("CREATE TABLE footholds (id TEXT PRIMARY KEY, kind TEXT NOT NULL, "
                    "host TEXT NOT NULL, port INTEGER DEFAULT 0, note TEXT DEFAULT '', "
                    "scope TEXT DEFAULT '[]', tags TEXT DEFAULT '[]', payload TEXT "
                    "NOT NULL DEFAULT '{}', created_at REAL)")
        raw.execute("INSERT INTO footholds(id,kind,host,port,note,scope,tags,payload,"
                    "created_at) VALUES('ssh-old','ssh','10.9.9.9',22,'旧记录',"
                    "'[\"10.9.0.0/16\"]','[\"old\"]','{}',1234.0)")
        raw.execute("PRAGMA user_version=9")
        raw.commit()
        raw.close()

        store = Store(path)
        self.addCleanup(store.close)
        listed = store.list_footholds(include_closed=True)
        self.assertEqual(1, len(listed))
        self.assertEqual(listed[0]["id"], "ssh-old")
        self.assertEqual(listed[0]["status"], "open", "后补的 status 列没有默认成 open")
        self.assertEqual(listed[0]["scope"], ["10.9.0.0/16"])
        self.assertIsNone(listed[0]["closedAt"])
        self.assertTrue(store.close_foothold("ssh-old", "旧记录清理"))


class JsonShapeTestCase(unittest.TestCase):
    """JSON 落库的两份副本必须对得上：平铺列是权威，JSON 是给未来字段留的位置。"""

    def setUp(self):
        self.dir = tempfile.mkdtemp(prefix="footholds-json-")
        self.addCleanup(shutil.rmtree, self.dir, True)
        self.store = Store(os.path.join(self.dir, "store.db"))
        self.addCleanup(self.store.close)

    def test_stored_payload_is_a_faithful_copy_of_to_dict(self):
        foothold = sample()
        self.store.add_foothold("E-1", foothold.to_dict())
        raw = self.store.conn.execute("SELECT payload FROM footholds WHERE id=?",
                                      (foothold.id,)).fetchone()["payload"]
        self.assertEqual(json.loads(raw), foothold.to_dict())

    def test_flat_columns_win_over_a_hand_edited_payload(self):
        """平铺列与 JSON 冲突时以列为准——列是能被 SQL 看见、被人工修补的那一份。"""
        foothold = sample(host="10.1.2.3")
        self.store.add_foothold("E-1", foothold.to_dict())
        self.store.conn.execute(
            "UPDATE footholds SET payload=?, host='10.1.2.99' WHERE id=?",
            (json.dumps({"id": foothold.id, "kind": "ssh", "host": "错误的主机",
                         "port": 22, "scope": [], "tags": [],
                         "capability": "socks5", "createdAt": 1.0}, ensure_ascii=False),
             foothold.id))
        self.store.conn.commit()
        listed = self.store.get_foothold(foothold.id)
        self.assertEqual("10.1.2.99", listed["host"])


if __name__ == "__main__":
    unittest.main()
