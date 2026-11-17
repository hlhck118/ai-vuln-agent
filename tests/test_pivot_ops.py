# -*- coding: utf-8 -*-
"""出口路由的节点侧入口（`pivot.run` / `build_chain_specs` / `describe_ops`）。

`test_pivot.py` 管的是**协议与部件**（白名单判定、SOCKS5 握手、半关闭、隧道），
这一个文件管的是**把部件用成一次任务**之后才出现的问题：

* **出口是不是只活一次**。`run()` 返回之后监听端口必须真的关了（用"同端口能立刻
  重新 bind"来证明，理由见下面那条用例里的注释：本机连一个从未绑定过的端口也是
  超时，所以"连一下看拒不拒"在这里证明不了任何东西）。
* **能出去的范围是不是被收紧了**。`allow` 取"立足点背后网段 ∩ 本次目标"，
  白名单之外的目标必须**在没建连的情况下**被判为拒绝——而且拿 `run()` 算出来的
  那份 allow 去起代理，线路上必须真的出现 0x02。这两条合起来才说明白"门"与
  "记录"是同一件事，而不是两张各说各话的表。
* **三态之外的那两态有没有被混进来**。`denied`（我们没放行）与 `unknown`
  （这次没观测成）都必须与 `closed`/`filtered`（目标的状态）分开记：把前者写成
  后者，会让人去查目标的防火墙，或者把"我没看到"当成"那里有过滤设备"。

夹具全部是真实 socket：真的 echo 服务、真的代理、真的隧道、真的线路上字节。
mock 只能证明"我调用了某个函数"，证明不了"经这条出口真的连到了一个服务"。
"""
import ipaddress
import os
import re
import socket
import struct
import sys
import threading
import time
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
sys.path.insert(0, ROOT)

from pentestd import authz, engine, models, pivot               # noqa: E402

# 本文件里那一组授权用例守的是**闸照判**时的拒绝（含"先判能不能打、再判打多少"
# 那个顺序），所以这一模块内把 `authz.FORCE_OPEN` 关掉；跑完还原。
_OPEN = authz.FORCE_OPEN


def setUpModule():
    authz.FORCE_OPEN = False


def tearDownModule():
    authz.FORCE_OPEN = _OPEN


# ---------------------------------------------------------------- 测试夹具

def free_port():
    """拿一个"刚被系统分配过、现在没人监听"的端口。

    比随便写一个数字好：它确定在本机的有效范围内，也确定刚刚没有别的测试占着。
    用完就关掉，于是"连不上"这件事是真实的，不是猜的。
    """
    sock = socket.socket()
    try:
        sock.bind(("127.0.0.1", 0))
        return sock.getsockname()[1]
    finally:
        sock.close()


def _close(sock):
    try:
        sock.close()
    except OSError:
        pass


class EchoService:
    """最小 TCP echo 服务：收到多少原样回多少，并记下accept过几个连接。"""

    def __init__(self):
        self.sock = socket.socket()
        self.sock.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        self.sock.bind(("127.0.0.1", 0))
        self.sock.listen(32)
        self.port = self.sock.getsockname()[1]
        self.accepted = 0
        self._stop = threading.Event()
        self._lock = threading.Lock()
        threading.Thread(target=self._serve, name="ops-echo", daemon=True).start()

    def _serve(self):
        while not self._stop.is_set():
            try:
                conn, _addr = self.sock.accept()
            except OSError:
                return
            with self._lock:
                self.accepted += 1
            threading.Thread(target=self._handle, args=(conn,),
                             name="ops-echo-conn", daemon=True).start()

    def _handle(self, conn):
        try:
            while True:
                chunk = conn.recv(65536)
                if not chunk:
                    break
                conn.sendall(chunk)
            conn.shutdown(socket.SHUT_WR)
        except OSError:
            pass
        finally:
            _close(conn)

    def stop(self):
        self._stop.set()
        _close(self.sock)


def socks5_reply_code(proxy_port, host, port):
    """自己拼一次 SOCKS5 握手，把线路上的应答码读回来。

    不用 `pivot.socks5_connect`：那个函数把失败包装成一句人能读的话，而这条用例
    要断言的正是**线路上真实出现的那个字节**（0x02 与 0x00 的区别就是"门在不在"）。
    """
    conn = socket.create_connection(("127.0.0.1", proxy_port), 5)
    conn.settimeout(5)
    try:
        conn.sendall(b"\x05\x01\x00")
        method = _read_exact(conn, 2)
        if method != b"\x05\x00":
            raise AssertionError("无认证模式下代理应选 0x00，实际 %r" % (method,))
        conn.sendall(b"\x05\x01\x00" + _atyp(host) + struct.pack("!H", port))
        return _read_exact(conn, 4)[1]
    finally:
        _close(conn)


def _atyp(host):
    if ":" in host:
        return b"\x04" + socket.inet_pton(socket.AF_INET6, host)
    try:
        return b"\x01" + socket.inet_aton(host)
    except OSError:
        raw = host.encode("idna")
        return b"\x03" + bytes([len(raw)]) + raw


def _read_exact(conn, count):
    data = b""
    while len(data) < count:
        chunk = conn.recv(count - len(data))
        if not chunk:
            raise AssertionError("连接在读满 %d 字节前被关闭（已读 %r）"
                                 % (count, data))
        data += chunk
    return data


def collect_refs(value):
    """把 payload 里所有的 `{"$ref": ...}` 名字收集出来（含嵌套）。"""
    if isinstance(value, dict):
        if set(value) == {"$ref"}:
            return [value["$ref"]]
        out = []
        for item in value.values():
            out.extend(collect_refs(item))
        return out
    if isinstance(value, list):
        out = []
        for item in value:
            out.extend(collect_refs(item))
        return out
    return []


class OpsCase(unittest.TestCase):
    """公共夹具：一个真实的背后目标 + 一份合法的授权快照 + 收摊。"""

    def setUp(self):
        self.echo = EchoService()
        self.addCleanup(self.echo.stop)

    # 观测窗口压到 1 秒：这里验的是"判定的口径"，不是"耐心"。默认值留给生产。
    def fast_limits(self, **overrides):
        limits = {"connectTimeoutS": 1.0, "probeTimeoutS": 1.0}
        limits.update(overrides)
        return limits

    def grant(self, targets=None, enabled=None, grant="ENG-PIVOT-0001", hop=0):
        return {"kind": "pivot", "grant": grant,
                "enabled": ["pivot"] if enabled is None else enabled,
                # 域名目标要按域名放行（authz 的明确口径：不能让一个名称"碰巧"
                # 落进某个网段），所以 localhost 单独列出来。
                "targets": (["127.0.0.1", "localhost"] if targets is None
                            else targets),
                "hop": hop}

    def foothold(self, **overrides):
        data = {"id": "fh-local-1", "kind": "socks5", "host": "127.0.0.1",
                "port": self.echo.port, "note": "本地夹具",
                "scope": ["127.0.0.0/8"], "capability": "socks5"}
        data.update(overrides)
        return data

    def run_ops(self, egress=None, **kwargs):
        kwargs.setdefault("grants", self.grant())
        kwargs.setdefault("limits", self.fast_limits())
        return pivot.run(self.foothold(**kwargs.pop("foothold_overrides", {})),
                         egress=(egress if egress is not None
                                 else ["127.0.0.1:%d" % self.echo.port]),
                         **kwargs)

    def assert_reached(self, result, expected=1):
        self.assertEqual(expected, result["summary"]["open"],
                         "经出口应当连到夹具；实际判定：%s"
                         % [(item["state"], item["reason"])
                            for item in result["egress"]])

    def wait_for(self, predicate, timeout=5.0):
        deadline = time.time() + timeout
        while time.time() < deadline:
            if predicate():
                return True
            time.sleep(0.02)
        return predicate()


# ---------------------------------------------------------------- 经出口观测

class EgressOutcomeCase(OpsCase):
    """判定本身：可达、不可达、被策略挡住、没观测成，四件事不能互相冒充。"""

    def test_open_target_is_verified_through_the_exit(self):
        result = self.run_ops()
        self.assertEqual("socks5", result["mode"])
        self.assertTrue(result["run_id"].startswith("PV-"))
        entry = result["egress"][0]
        self.assertEqual("open", entry["state"])
        self.assertTrue(entry["probed"], "放行的目标必须真的连过")
        self.assertEqual("target", entry["portSource"])
        self.assertEqual(1, result["summary"]["probed"])
        self.assertTrue(self.wait_for(lambda: self.echo.accepted >= 1),
                        "判定说可达，但 echo 服务一次连接都没收到过——"
                        "说明这个 open 不是从真实往返里来的")

    def test_unreachable_target_is_closed_or_filtered_not_open(self):
        port = free_port()
        result = self.run_ops(egress=["127.0.0.1:%d" % port])
        entry = result["egress"][0]
        self.assertIn(entry["state"], ("closed", "filtered"),
                      "连不上的目标只能记 closed 或 filtered，实际 %s（原因：%s）"
                      % (entry["state"], entry["reason"]))
        self.assertEqual(0, result["summary"]["open"],
                         "\"连不上\"被记成可达是本类判定最坏的错误")
        self.assertEqual(1, result["summary"]["closed"]
                         + result["summary"]["filtered"])

    def test_out_of_scope_target_is_denied_without_connecting(self):
        result = self.run_ops(foothold_overrides={"scope": ["10.0.0.0/8"]})
        entry = result["egress"][0]
        self.assertEqual("denied", entry["state"],
                         "立足点背后的网段不含这个目标，就该在门外被拒")
        self.assertFalse(entry["probed"], "策略拒绝不需要先连一次才知道")
        self.assertIn("10.0.0.0/8", entry["reason"])
        self.assertEqual([], result["allow"])
        self.assertFalse(result["exitStarted"],
                         "一个目标都没通过交集时，起一个白名单为空的代理"
                         "只是在别人的机器上多留一个监听器")
        self.assertEqual(0, self.echo.accepted,
                         "被策略拒绝的目标不该产生任何连接")
        self.assertTrue(result["degraded"], "出口没启动这件事必须写进降级说明")

    def test_denied_is_not_counted_as_closed(self):
        # denied（我们没放行）与 closed（目标拒绝）在报告里必须分开：把前者写成
        # 后者，排查方向会从"谁改了白名单"漂到"目标的防火墙"，差出几小时。
        result = self.run_ops(foothold_overrides={"scope": ["10.0.0.0/8"]})
        self.assertEqual(1, result["summary"]["denied"])
        self.assertEqual(0, result["summary"]["closed"])
        self.assertEqual(0, result["summary"]["filtered"])

    def test_expired_ttl_is_unknown_not_unreachable(self):
        result = self.run_ops(limits=self.fast_limits(ttlS=0.001))
        entry = result["egress"][0]
        self.assertEqual("unknown", entry["state"],
                         "超过出口存活上限时什么都没观测到，不能替目标下结论")
        self.assertFalse(entry["probed"])
        self.assertIn("ttlS", entry["reason"])
        self.assertEqual(1, result["summary"]["unknown"])
        self.assertEqual(0, result["summary"]["closed"] + result["summary"]["filtered"])

    def test_foothold_echo_is_the_normalized_record(self):
        record = self.foothold()
        result = self.run_ops(foothold_overrides={})
        expected = pivot.Foothold.from_dict(record).to_dict()
        self.assertEqual(expected["id"], result["foothold"]["id"])
        self.assertEqual(expected["scope"], result["foothold"]["scope"])
        self.assertEqual(expected["kind"], result["foothold"]["kind"])

    def test_grant_snapshot_is_effective_limits_not_requested_ones(self):
        result = self.run_ops(limits=self.fast_limits(maxEgressTargets=3))
        self.assertEqual(3, result["limits"]["maxEgressTargets"],
                         "生效的配额必须写回结果：报告里写一个从没生效过的上限"
                         "比不写更糟")
        self.assertEqual(3, result["grant"]["limits"]["maxEgressTargets"])
        self.assertEqual("pivot", result["grant"]["kind"])
        self.assertEqual("ENG-PIVOT-0001", result["grant"]["grant"])


# ---------------------------------------------------------------- 白名单这道门

class AllowDerivationCase(OpsCase):
    """`allow` = 立足点背后网段 ∩ 本次目标；它必须与代理那扇门是同一份。"""

    def test_allow_is_exactly_the_verified_targets(self):
        result = self.run_ops()
        self.assertEqual(["127.0.0.1/32"], result["allow"],
                         "放行项应当是解析后的地址，而不是原始写法")
        self.assertEqual(result["allow"], result["endpoint"]["allow"])

    def test_derived_allow_refuses_the_wire_with_0x02(self):
        # 拿 run() 算出来的那份 allow 去起一个真代理：线路上必须出现 0x02
        # （connection not allowed by ruleset）。这条用例把"记录"与"门"钉在一起：
        # 报告里写着"没放行"而线路上其实放行了，是最不该发生的一种偏差。
        result = self.run_ops(foothold_overrides={"scope": ["10.0.0.0/8"]})
        proxy = pivot.Socks5Server(allow=result["allow"])
        self.addCleanup(proxy.stop)
        proxy.start()
        code = socks5_reply_code(proxy.endpoint["port"], "127.0.0.1",
                                 self.echo.port)
        self.assertEqual(0x02, code,
                         "空白名单必须回 0x02（规则拒绝），而不是先连上再失败")
        self.assertEqual(0, self.echo.accepted,
                         "被规则拒绝的目标身上不该出现连接")
        self.assertEqual(1, proxy.stats["denied"], "拒绝要计进 denied")

    def test_derived_allow_lets_the_target_through_on_the_wire(self):
        result = self.run_ops()
        proxy = pivot.Socks5Server(allow=result["allow"])
        self.addCleanup(proxy.stop)
        proxy.start()
        code = socks5_reply_code(proxy.endpoint["port"], "127.0.0.1",
                                 self.echo.port)
        self.assertEqual(0x00, code,
                         "同一个目标在同一份 allow 下必须是放行的——否则"
                         "0x02 那条用例只是证明了代理什么都不让过")

    def test_domain_targets_are_judged_after_resolution(self):
        # ① 白名单只放行 10/8：域名 localhost 解析出来的是回环地址，必须在门外。
        denied = self.run_ops(egress=["localhost:%d" % self.echo.port],
                              foothold_overrides={"scope": ["10.0.0.0/8"]})
        self.assertEqual("denied", denied["egress"][0]["state"],
                         "判定的必须是解析后的地址，而不是域名字符串本身")
        self.assertIn("解析", denied["egress"][0]["reason"])
        self.assertEqual(0, self.echo.accepted)

        # ② 放行回环之后，同一个域名必须真的能通——反过来证明 ① 不是因为
        #    "域名一律拒绝"。
        allowed = self.run_ops(
            egress=["localhost:%d" % self.echo.port],
            foothold_overrides={"scope": ["127.0.0.0/8", "::1/128"]})
        self.assertEqual("open", allowed["egress"][0]["state"],
                         "原因：%s" % allowed["egress"][0]["reason"])
        self.assertIn("127.0.0.1/32", allowed["allow"])
        self.assertTrue(self.wait_for(lambda: self.echo.accepted >= 1))

    def test_multi_address_domain_is_denied_if_any_address_is_outside(self):
        # 解析出多个地址时，只要有一个越界就整体拒绝：实际连出去的是哪一个由
        # 解析顺序决定，不由我们决定，所以不能只要求"有一个命中"。
        #
        # 这里不硬编码 localhost 解析成哪个地址：双栈机器上它可能同时给出
        # 127.0.0.1 与 ::1，而"哪一族有"取决于这台机器——硬编码会让这条用例
        # 在一半的机器上测到别的东西。改为拿本模块自己的解析结果来构造名单。
        addresses, reason = pivot._resolve_addresses("localhost")
        self.assertTrue(addresses, "本机应当能解析 localhost：%s" % reason)
        full = [str(ipaddress.ip_network(address)) for address in addresses]
        self.assertTrue(pivot.in_allow("localhost", full),
                        "解析出的每个地址都在名单内时应当放行（%s）" % full)
        if len(addresses) > 1:
            self.assertFalse(pivot.in_allow("localhost", full[:-1]),
                             "少放行其中一个解析结果就必须整体拒绝：连出去的是"
                             "哪一个不由我们决定")
        self.assertFalse(pivot.in_allow("localhost", ["10.0.0.0/8"]))

    def test_foothold_without_scope_refuses_to_open_anything(self):
        with self.assertRaises(pivot.PivotError) as caught:
            self.run_ops(foothold_overrides={"scope": []})
        self.assertIn("scope", str(caught.exception))
        self.assertEqual(0, self.echo.accepted,
                         "连门都算不出来的时候不该有任何连接")

    def test_unparsable_scope_cannot_widen_the_door(self):
        # 立足点登记表是地图（坏值留着给人看），但门必须严格：解析不出来的
        # scope 条目只能等价于"不放行"，绝不能"当没写、剩下照放"。
        with self.assertRaises(pivot.PivotError) as caught:
            self.run_ops(foothold_overrides={"scope": ["10.0.0.0/33"]})
        self.assertIn("10.0.0.0/33", str(caught.exception))
        self.assertEqual(0, self.echo.accepted)

    def test_scope_any_is_explicit_and_then_works(self):
        result = self.run_ops(foothold_overrides={"scope": ["any"]})
        self.assertEqual("open", result["egress"][0]["state"],
                         "显式写 any 是放行全部网段，这是部署方的选择")

    def test_network_target_is_expanded_within_the_quota(self):
        result = self.run_ops(egress=["127.0.0.1/32"],
                              ports=[self.echo.port])
        self.assertEqual(1, result["summary"]["targets"])
        self.assertEqual("open", result["egress"][0]["state"])
        self.assertEqual("ports", result["egress"][0]["portSource"])

    def test_network_target_without_ports_is_flagged_as_an_assumption(self):
        result = self.run_ops(egress=["127.0.0.1/32"],
                              limits=self.fast_limits(ttlS=0.001))
        self.assertEqual("assumed", result["egress"][0]["portSource"])
        self.assertTrue(any("默认端口" in note for note in result["degraded"]),
                        "按默认端口验证是一个假设，必须写进降级说明：%s"
                        % result["degraded"])

    def test_over_quota_is_refused_before_any_connection(self):
        # 一个目标 × 三个端口 = 3 个组合，上限给 2：必须拒绝。
        with self.assertRaises(pivot.PivotError) as caught:
            self.run_ops(egress=["127.0.0.1:%d" % self.echo.port],
                         ports=[80, 443, 8080],
                         limits=self.fast_limits(maxEgressTargets=2))
        self.assertIn("上限", str(caught.exception))
        self.assertEqual(0, self.echo.accepted,
                         "超限是拒绝而不是截断：截断会给出一个看起来扫过了的结果")

    def test_oversized_network_is_refused_without_expanding_it(self):
        with self.assertRaises(pivot.PivotError) as caught:
            self.run_ops(egress=["127.0.0.0/24"])
        self.assertIn("地址", str(caught.exception))
        self.assertEqual(0, self.echo.accepted)

    def test_empty_egress_is_refused(self):
        with self.assertRaises(pivot.PivotError) as caught:
            self.run_ops(egress=[])
        self.assertIn("egress", str(caught.exception))

    def test_duplicate_targets_are_observed_once(self):
        target = "127.0.0.1:%d" % self.echo.port
        result = self.run_ops(egress=[target, target])
        self.assertEqual(1, result["summary"]["targets"],
                         "同一个目标观测两次不会得到两个结论，只会让计数骗人")


# ---------------------------------------------------------------- 授权闸

class GateCase(OpsCase):
    """执行侧必须自己再判一次，而且拒绝时不留下任何东西。"""

    def test_run_without_any_grant_is_refused(self):
        base = threading.active_count()
        with self.assertRaises(authz.AuthzError):
            self.run_ops(grants={})
        self.assertEqual(0, self.echo.accepted, "准入没过就一个连接都不许发")
        self.assertLessEqual(threading.active_count(), base,
                             "准入被拒时不该起监听线程")

    def test_short_authorization_text_is_refused(self):
        with self.assertRaises(authz.AuthzError) as caught:
            self.run_ops(grants=self.grant(grant="abc"))
        self.assertIn("授权说明", str(caught.exception))
        self.assertEqual(0, self.echo.accepted)

    def test_foothold_outside_allowlist_is_refused(self):
        # 立足点是"把网络位置借出来"的那台机器；它自己没被授权，就不能在它身上
        # 开出口——哪怕出口目标在白名单里。
        with self.assertRaises(authz.AuthzError) as caught:
            self.run_ops(grants=self.grant(targets=["10.0.0.0/8"]))
        self.assertIn("127.0.0.1", str(caught.exception))
        self.assertEqual(0, self.echo.accepted)

    def test_egress_target_outside_allowlist_is_refused(self):
        with self.assertRaises(authz.AuthzError):
            self.run_ops(egress=["10.0.0.5:%d" % self.echo.port],
                         grants=self.grant(targets=["127.0.0.1"]))
        self.assertEqual(0, self.echo.accepted)

    def test_disabled_class_is_refused(self):
        with self.assertRaises(authz.AuthzError) as caught:
            self.run_ops(grants=self.grant(enabled=["exploit"]))
        self.assertIn("--allow-intrusive", str(caught.exception))

    def test_hop_beyond_the_limit_is_refused(self):
        limit = authz.LIMITS["pivot"]["maxHops"]
        with self.assertRaises(authz.AuthzError) as caught:
            self.run_ops(grants=self.grant(hop=int(limit) + 1))
        self.assertIn("跳", str(caught.exception))


# ---------------------------------------------------------------- 生命周期

class LifecycleCase(OpsCase):
    """出口必须只活一次：返回即关闭，且不残留线程与监听器。"""

    def test_listen_port_is_closed_when_run_returns(self):
        result = self.run_ops()
        port = result["endpoint"]["port"]
        self.assertNotEqual(0, port, "endpoint 必须是真实监听端口，不是请求值")
        self.assertTrue(result["exitClosed"])
        # 本机的实测行为要先说清，否则下面这段断言看起来像在测别的东西：
        # 连一个**从未绑定过**的 TCP 端口在 Windows 上也是超时而不是立即
        # ECONNREFUSED，"连一下看拒不拒"因此证明不了端口关没关。
        # 能立刻重新 bind 才是直接证据：监听套接字真的释放了。
        rebind = socket.socket()
        rebind.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rebind.bind(("127.0.0.1", port))
        except OSError as exc:
            raise AssertionError(
                "run() 返回后端口 %d 还被占着（%s）：出口没有在任务结束时关闭，"
                "留下了一个常开的代理" % (port, exc))
        finally:
            _close(rebind)

    def test_no_threads_leak_across_runs(self):
        base = threading.active_count()
        for _ in range(3):
            self.run_ops()
        deadline = time.time() + 5
        while threading.active_count() > base and time.time() < deadline:
            time.sleep(0.05)
        self.assertLessEqual(threading.active_count(), base,
                             "反复跑出口任务泄漏了线程：基线 %d，现在 %d"
                             % (base, threading.active_count()))

    def test_each_run_uses_a_fresh_port(self):
        first = self.run_ops()
        second = self.run_ops()
        self.assertNotEqual(0, second["endpoint"]["port"])
        self.assertNotEqual(0, first["endpoint"]["port"])

    def test_progress_is_reported_to_the_caller(self):
        notes = []
        self.run_ops(on_progress=notes.append)
        self.assertTrue(notes, "任务必须能对外说清它做到哪一步了")
        self.assertTrue(any("出口已就绪" in note for note in notes))
        self.assertTrue(any("出口验证完成" in note for note in notes))


# ---------------------------------------------------------------- 隧道模式

class TunnelModeCase(OpsCase):
    """隧道的判据与代理不同：它总是先接住本机连接，所以"连上了"什么都不证明。"""

    def test_tunnel_mode_verifies_the_fixed_target(self):
        result = pivot.run(self.foothold(kind="tunnel"),
                           egress=["127.0.0.1:%d" % self.echo.port],
                           grants=self.grant(), limits=self.fast_limits(),
                           tunnel=True)
        self.assertEqual("tunnel", result["mode"])
        self.assertEqual(1, result["summary"]["open"])
        entry = result["egress"][0]
        self.assertEqual("open", entry["state"])
        self.assertIn("字节", entry["reason"],
                      "经隧道应当读到目标返回的探针字节，实际理由：%s"
                      % entry["reason"])

    def test_tunnel_reads_the_same_bytes_as_a_direct_connection(self):
        # 直连读一次，再让 run 的隧道读一次：两边的字节数必须一致。
        direct = socket.create_connection(("127.0.0.1", self.echo.port), 5)
        direct.settimeout(5)
        try:
            direct.sendall(pivot.EGRESS_PROBE_BYTES)
            direct_data = direct.recv(256)
        finally:
            _close(direct)
        self.assertEqual(pivot.EGRESS_PROBE_BYTES, direct_data,
                         "夹具是 echo：直连读回来的应当就是发出去的那两字节")

        result = pivot.run(self.foothold(kind="tunnel"),
                           egress=["127.0.0.1:%d" % self.echo.port],
                           grants=self.grant(), limits=self.fast_limits(),
                           tunnel=True)
        self.assertEqual(len(direct_data), result["egress"][0]["bytesRead"],
                         "经隧道读到的数据必须等于直连读到的")

    def test_dead_tunnel_is_not_reported_open(self):
        # 一条连目标都不通的隧道也会接住本机连接。按"连上了"判定，这里会报出
        # 满屏的 open —— 那是这个模式最容易写出的假阳性。
        port = free_port()
        result = pivot.run(self.foothold(kind="tunnel"),
                           egress=["127.0.0.1:%d" % port],
                           grants=self.grant(), limits=self.fast_limits(),
                           tunnel=True)
        entry = result["egress"][0]
        self.assertNotEqual("open", entry["state"],
                            "隧道连不上目标时必须记 closed（原因：%s）"
                            % entry["reason"])
        self.assertEqual("closed", entry["state"])

    def test_tunnel_requires_exactly_one_target(self):
        with self.assertRaises(pivot.PivotError) as caught:
            pivot.run(self.foothold(kind="tunnel"),
                      egress=["127.0.0.1:%d" % self.echo.port,
                              "127.0.0.1:%d" % free_port()],
                      grants=self.grant(), limits=self.fast_limits(),
                      tunnel=True)
        self.assertIn("隧道", str(caught.exception))
        self.assertEqual(0, self.echo.accepted)

    def test_tunnel_listen_port_is_closed_when_run_returns(self):
        result = pivot.run(self.foothold(kind="tunnel"),
                           egress=["127.0.0.1:%d" % self.echo.port],
                           grants=self.grant(), limits=self.fast_limits(),
                           tunnel=True)
        port = result["endpoint"]["listenPort"]
        self.assertNotEqual(0, port)
        rebind = socket.socket()
        rebind.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
        try:
            rebind.bind(("127.0.0.1", port))
        except OSError as exc:
            raise AssertionError("隧道模式返回后端口 %d 还被占着（%s）"
                                 % (port, exc))
        finally:
            _close(rebind)


# ---------------------------------------------------------------- 链路契约

class ChainSpecCase(OpsCase):
    """与 scan/probe/exploit 共用同一套 `{kind, localRef, payload}` 契约。"""

    def specs(self):
        return pivot.build_chain_specs(self.run_ops())

    def test_every_spec_has_kind_localref_and_payload(self):
        specs = self.specs()
        kinds = {spec["kind"] for spec in specs}
        self.assertTrue({"asset", "intent", "fact"} <= kinds,
                        "出口路由至少要落成：立足点资产、意图、事实")
        for spec in specs:
            self.assertIn(spec["kind"], ("asset", "intent", "fact", "finding"))
            self.assertTrue(spec.get("localRef"), "每个规格都要有 localRef")
            self.assertIn("payload", spec)

    def test_refs_resolve_and_are_declared_before_use(self):
        # 与 `engine.write_chain` 的写入顺序一致：边写边解析 $ref，所以一个
        # `$ref` 只能指向**前面**已经出现过的 localRef。这一条钉住"顺序"，
        # 而不只是"存在"——弄反了会在真正写链路时才炸。
        declared = {engine.GOAL_REF}
        for spec in self.specs():
            for ref in collect_refs(spec["payload"]):
                self.assertIn(ref, declared,
                              "%s 引用了尚未出现的 %s" % (spec["localRef"], ref))
            declared.add(spec["localRef"])

    def test_specs_pass_the_store_validators(self):
        # 逐条按 `write_chain` 的方式解析引用后交给模型校验：这一步过了，规格就
        # 真的写得进链路，而不是"看起来长得像"。
        table = {engine.GOAL_REF: engine.GOAL_ID}
        for spec in self.specs():
            payload = engine.deref(spec["payload"], table)
            models.validate(spec["kind"], payload)
            table[spec["localRef"]] = "%s-x" % spec["kind"]
        self.assertEqual(engine.GOAL_ID, table[engine.GOAL_REF])

    def test_grant_text_is_visible_in_the_chain(self):
        blob = " ".join(str(spec.get("payload")) for spec in self.specs())
        self.assertIn("ENG-PIVOT-0001", blob,
                      "链路里必须读得出这次是在哪条授权下做的")

    def test_reachable_target_becomes_an_asset_below_the_foothold(self):
        specs = self.specs()
        egress_assets = [spec for spec in specs
                         if str(spec["localRef"]).startswith("asset:egress:")]
        self.assertEqual(1, len(egress_assets),
                         "经立足点可达的目标要单独落成资产")
        self.assertEqual({"$ref": "asset:root"},
                         egress_assets[0]["payload"]["parentId"],
                         "可达目标要挂在立足点下面：这是「凭什么能到它」的登记")

    def test_no_findings_are_produced(self):
        # 可达性不是漏洞。把"经跳板能连上一个端口"写成交付项，报告里就会出现
        # 一批没有影响说明的高危。
        kinds = [spec["kind"] for spec in self.specs()]
        self.assertNotIn("finding", kinds)

    def test_unreachable_and_denied_targets_become_facts(self):
        result = self.run_ops(foothold_overrides={"scope": ["10.0.0.0/8"]})
        specs = pivot.build_chain_specs(result)
        facts = [spec for spec in specs
                 if str(spec["localRef"]).startswith("fact:egress:")]
        self.assertEqual(1, len(facts), "被拒绝的目标也是一条观测结果")
        self.assertIn("denied", facts[0]["payload"]["detail"])
        self.assertEqual(0.5, facts[0]["payload"]["confidence"])

    def test_unknown_targets_are_low_confidence(self):
        specs = pivot.build_chain_specs(
            self.run_ops(limits=self.fast_limits(ttlS=0.001)))
        facts = [spec for spec in specs
                 if str(spec["localRef"]).startswith("fact:egress:")]
        self.assertEqual(1, len(facts))
        self.assertEqual(0.2, facts[0]["payload"]["confidence"],
                         "「我没看到」不能和「目标不可达」用同一个置信度")

    def test_degraded_notes_land_in_the_chain(self):
        specs = pivot.build_chain_specs(
            self.run_ops(egress=["127.0.0.1/32"],
                         limits=self.fast_limits(ttlS=0.001)))
        blob = " ".join(str(spec.get("payload")) for spec in specs)
        self.assertIn("默认端口", blob)

    def test_chain_survives_an_empty_result(self):
        # 结果为空也要能翻译成链路，而不是抛异常：这条路径在接口层真的会出现。
        specs = pivot.build_chain_specs({})
        self.assertTrue(specs)
        for spec in specs:
            self.assertTrue(spec["localRef"])


# ---------------------------------------------------------------- 说明页

class DescribeOpsCase(unittest.TestCase):
    """`describe_ops()` 是给模型与使用者看的一页说明，必须自己说清三把钥匙。"""

    def test_describe_ops_names_the_switches_and_the_limits(self):
        text = pivot.describe_ops()
        self.assertTrue(text.startswith("##"), "应当是可直接展示的 markdown")
        for needle in ("--allow-intrusive pivot", "--intrusive-targets",
                       "fail-closed",
                       str(authz.LIMITS["pivot"]["maxEgressTargets"]),
                       str(authz.LIMITS["pivot"]["maxHops"]),
                       "denied", "unknown"):
            self.assertIn(needle, text,
                          "说明页里必须写明 %r：模型据此才知道该怎么申请、"
                          "以及哪些结论不能混着读" % (needle,))

    def test_describe_ops_warns_that_nothing_persists(self):
        text = pivot.describe_ops()
        self.assertRegex(text, re.compile("不(起|留).*通道|通道.*不是"))
        self.assertIn("stop", text)


if __name__ == "__main__":
    unittest.main()
