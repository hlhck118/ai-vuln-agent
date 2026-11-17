# -*- coding: utf-8 -*-
'''终端：超时不挂住、kill 的结论不被改写、输出读回有上限、后台并发有上限。

为什么单独一个文件：这四条都不是「某个函数返回什么」，而是**控制面在坏情况下
还能不能自持**。它们的共同形态是「平时看不出来，出事时整条请求挂死」，而挂死的
症状（界面转圈、线程堆积）离原因（守着一个已经死掉的父进程的管道）很远。

四条各自对应一处实测过的缺陷：

1. 超时之后走无超时的 communicate()：Windows 上孙进程继承了 stdout，于是
   timeoutS=2 配 ping -n 30 实测挂 29 秒，配 ping -n 100000 永不返回。
2. _collect 无条件写 status=exited：terminal_kill 刚写的 killed 被改回
   exited，退出码变成 0 —— 被主动停掉的命令事后看起来像自己跑完的。
3. fh.read() 整段读回落盘输出：跑几小时的扫描能写出 GB 级文件，整段读会在
   控制面进程里分配同样大的对象，而落库只要 STORE_LIMIT 那些字符。
4. 后台每次起一个线程且不设上限：多起几个扫描就能把控制面拖垮，症状只是变慢。
'''
import os
import shlex
import sys
import time
import types
import unittest

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

from pentestd import terminal                        # noqa: E402
from tests import support                            # noqa: E402

Q = chr(34)


def long_running_command(seconds=60):
    '''一条会派生孙进程、并让孙进程继承 stdout 的命令。

    孙进程是关键：只杀直接子进程时，管道读端仍被握着，于是「等管道关闭」
    就等于「等孙进程自己跑完」。Windows 上 cmd /c ping 正是这种形状（cmd
    派生 ping.exe）；POSIX 上用管道把第二个 sleep 留在前台进程组里。
    '''
    if os.name == 'nt':
        return 'cmd', 'ping -n %d 127.0.0.1' % max(2, int(seconds))
    return 'bash', 'sleep %d | sleep %d' % (int(seconds), int(seconds))


def child_pids(pid):
    '''直接子进程的 pid 表。问不出来就返回空表（判据退化为「父进程没了」）。'''
    if not pid:
        return []
    if os.name == 'nt':
        attempts = [
            ['wmic', 'process', 'where', 'ParentProcessId=%d' % int(pid),
             'get', 'ProcessId'],
            ['powershell', '-NoProfile', '-Command',
             'Get-CimInstance Win32_Process -Filter ' + Q
             + 'ParentProcessId=%d' % int(pid) + Q
             + ' | Select-Object -ExpandProperty ProcessId'],
        ]
        for argv in attempts:
            try:
                out = terminal.subprocess.run(argv, capture_output=True,
                                           timeout=20)
            except (OSError, terminal.subprocess.SubprocessError):
                continue
            text = (out.stdout or b'').decode('utf-8', 'replace')
            found = [int(t) for t in text.split() if t.isdigit()]
            found = [p for p in found if p != int(pid)]
            if found:
                return found
        return []
    try:
        out = terminal.subprocess.run(
            ['ps', '-o', 'pid=', '--ppid', str(int(pid))],
            capture_output=True, timeout=20)
    except (OSError, terminal.subprocess.SubprocessError):
        return []
    return [int(t) for t in (out.stdout or b'').decode().split() if t.isdigit()]


def _settle(pids, timeout=20.0):
    '''等这些 pid 真的退出（进程退出与句柄释放是两件事）。

    `taskkill` / `killpg` 返回只说明信号发出去了。被杀进程的 cwd 正是上面
    那个临时目录，它退干净之前目录一直被锁着 —— 而那时删目录会撞
    PermissionError，tempfile 在这条路径上的重试是**递归**的，最终以
    `RecursionError: maximum recursion depth exceeded` 收场（栈全在
    tempfile._rmtree 与 shutil._rmtree_unsafe 之间）。那条报错与「删目录时
    目录还被占着」这个原因隔了十几层栈，收尾时的偶发占用就这样盖成一条
    看不懂的失败。
    '''
    deadline = time.time() + timeout
    for pid in pids:
        while time.time() < deadline:
            if not terminal._pid_alive(pid):
                break
            time.sleep(0.05)


def _cleanup_dir(directory):
    '''删临时目录；删不掉就咽下，别让清理时序替真正的断言发话。

    Windows 上「目录被占」会以 PermissionError 进 tempfile 的递归重试，
    极端情况抛 RecursionError —— 它不是 OSError 的子类，所以这里按
    Exception 兜：这条路径上没有任何例外能说明被测行为错在哪里。
    `support.temporary_store` 已经给了 `ignore_cleanup_errors=True`，
    但那只挡得住 rmtree 抛出的 OSError，挡不住递归爆栈这一条。
    '''
    try:
        directory.cleanup()
    except Exception:                       # noqa: BLE001
        pass


class TerminalSafetyTestCase(unittest.TestCase):

    def setUp(self):
        self.dir, self.store = support.temporary_store('terminal.db')
        self.plane = types.SimpleNamespace(store=self.store)
        self._old_max = terminal.MAX_RUNNING

    def tearDown(self):
        with terminal._LOCK:
            procs = list(terminal._RUNNING.values())
        for proc in procs:
            try:
                terminal._kill_tree(proc)
            except Exception:                   # noqa: BLE001
                pass
        with terminal._LOCK:
            terminal._RUNNING.clear()
        terminal.MAX_RUNNING = self._old_max
        # 杀完要等树真的退出，**然后**才删临时目录。
        #
        # 为什么不能杀完就删：`taskkill` / `killpg` 返回只说明信号发出去了，
        # 进程退出与句柄释放是之后的事，而被杀的子进程的 cwd 正是这个临时
        # 目录。目录还锁着的时候 `TemporaryDirectory.cleanup()` 会撞
        # PermissionError，而 tempfile 在这条路径上的重试是**递归**的
        # （实测 RecursionError: maximum recursion depth exceeded，栈全在
        # tempfile._rmtree 与 shutil._rmtree_unsafe 之间来回）—— 于是收尾
        # 把一条本来绿的用例盖成一条看不懂的报错，而真正的原因
        # （「删目录时目录还被占着」）在栈顶完全看不见。
        _settle([proc.pid for proc in procs])
        self.store.close()
        _cleanup_dir(self.dir)

    # ------------------------------------------------ 1. 超时不挂住

    def test_a_timeout_does_not_wait_for_grandchildren(self):
        '''超时后要立刻返回，而不是等孙进程把输出管道关掉。

        判据是墙钟：timeoutS=1 加宽限期 5 秒，正常应在 15 秒内返回；修复前
        同一形状实测 29 秒（ping -n 30），更长的命令则永不返回 —— 那时被占住
        的是处理这次请求的线程，也就是整个控制面少一个线程。
        '''
        shell, command = long_running_command(60)
        started = time.time()
        outcome = terminal.run(self.plane, {'command': command,
                                           'shell': shell, 'timeoutS': 1,
                                           'cwd': self.dir.name})
        elapsed = time.time() - started
        self.assertEqual(outcome['data']['status'], 'timeout')
        self.assertLess(elapsed, 15.0,
                        '超时之后仍在等孙进程退出（耗时 %.1f 秒）' % elapsed)

    def test_the_timeout_summary_names_the_number_that_was_used(self):
        '''摘要里的秒数来自本次调用，不来自终端表。

        terminals 表**没有** timeoutS 这一列：超时是每次调用的参数，不是记录
        的事实。早先摘要读 row.get('timeoutS')，于是写成「命令超时（None 秒）
        已被终止」—— 那句话里唯一有用的是数字，而它是 None。
        '''
        shell, command = long_running_command(60)
        outcome = terminal.run(self.plane, {'command': command,
                                           'shell': shell, 'timeoutS': 1,
                                           'cwd': self.dir.name})
        self.assertIn('1 秒', outcome['summary'])
        self.assertNotIn('None', outcome['summary'])

    # ------------------------------------------------ 2. kill 的结论不被改写

    def test_killing_a_terminal_sticks(self):
        '''terminal_kill 写下「已被停止」之后，收割线程不能再改口。

        修复前：kill 返回 killed，1.5 秒后库里变成 exited 配 exitCode=1 ——
        而「是不是我停的」正是事后核对时唯一要问的事。
        '''
        shell, command = long_running_command(120)
        started = terminal.start_background(
            self.plane, {'command': command, 'shell': shell,
                         'cwd': self.dir.name})
        terminal_id = started['data']['terminalId']
        time.sleep(1.0)
        terminal.kill(self.plane, {'terminalId': terminal_id})
        time.sleep(1.5)                       # 让收割线程有机会改写
        row = self.store.get_terminal(terminal_id)
        self.assertEqual(row['status'], 'killed',
                         '收割线程把 kill 的结论改回了 %r' % row['status'])
        self.assertEqual(row['exitCode'], -1)

    def test_killing_takes_the_whole_process_tree(self):
        '''停掉的是整棵树，不只是直接子进程。

        修复前实测：kill 之后 cmd 已经不在了，而 PING.EXE 进程数从 1 变成 2
        —— 被「停掉」的命令其实还在跑，只是没人再管它。判据用孙进程的 pid
        还活不活，而不是 kill 的返回值：返回值只说明我们发过信号。
        '''
        shell, command = long_running_command(120)
        started = terminal.start_background(
            self.plane, {'command': command, 'shell': shell,
                         'cwd': self.dir.name})
        terminal_id = started['data']['terminalId']
        time.sleep(1.5)
        with terminal._LOCK:
            proc = terminal._RUNNING.get(terminal_id)
        self.assertIsNotNone(proc, '后台终端没有登记在 _RUNNING 里')
        descendants = child_pids(proc.pid)
        terminal.kill(self.plane, {'terminalId': terminal_id})
        time.sleep(2.0)
        survivors = [pid for pid in descendants if terminal._pid_alive(pid)]
        self.assertEqual(survivors, [],
                         'kill 之后仍有后代进程活着：%r' % survivors)

    # ------------------------------------------------ 3. 输出读回有上限

    def test_reading_back_a_big_file_is_bounded(self):
        '''读回落盘输出最多 READ_LIMIT 字节，并明说只读回了多少。

        落盘文件本身不设限（它由子进程直接写，不占本进程内存）；设限的是读回。
        '''
        path = terminal.output_path(self.plane, 'T-0000beef')
        with open(path, 'wb') as fh:
            fh.write(b'a' * (terminal.READ_LIMIT + 5000))
        raw, total = terminal._read_output(path)
        self.assertEqual(len(raw), terminal.READ_LIMIT)
        self.assertEqual(total, terminal.READ_LIMIT + 5000)
        text = terminal._load_output(path)
        self.assertIn('只读回', text)
        self.assertLessEqual(len(text), terminal.STORE_LIMIT + 300)

    def test_a_background_terminal_that_spews_reports_the_truncation(self):
        '''端到端：后台写出超过上限的文件，落库时带上截断说明。

        输出源用 `type` / `cat` 而不是起一个 python 孙进程，有两个理由：一是这条
        测的是「读回有上限」，不该顺带测「怎么引用一个带空格的解释器路径」；二是
        `cmd /d /s /c` 在命令以引号开头时会剥掉最外两层引号（`/s` 的规则），于是
        `"C:\\...\\python.exe" -X utf8 ...` 会被理解成一个不存在的命令 —— 那是
        另一个话题，不该把这个判据搅成随机失败。
        '''
        payload = os.path.join(self.dir.name, 'spew.txt')
        with open(payload, 'wb') as fh:
            fh.write(b'x' * (2 * 1024 * 1024))
        if os.name == 'nt':
            # 不加引号：`cmd /d /s /c` 在命令以引号开头时会剥掉最外两层，带引号的
            # 单段路径会被剥成 `type C:\\...` 之外的东西。临时目录名里没有空格。
            command = 'type ' + payload if ' ' not in payload \
                else 'type ' + Q + payload + Q
        else:
            command = 'cat ' + shlex.quote(payload)
        started = terminal.start_background(
            self.plane, {'command': command,
                         'shell': 'cmd' if os.name == 'nt' else 'bash',
                         'cwd': self.dir.name})
        terminal_id = started['data']['terminalId']
        row = {}
        deadline = time.time() + 90
        while time.time() < deadline:
            row = self.store.get_terminal(terminal_id) or {}
            if row.get('status') != 'running':
                break
            time.sleep(0.1)
        self.assertNotEqual(row.get('status'), 'running',
                            '后台命令没有在 90 秒内结束')
        output = row.get('output') or ''
        self.assertIn('只读回', output)
        self.assertLess(len(output), terminal.STORE_LIMIT + 300,
                        '落库的输出远大于上限：%d 字符' % len(output))

    # ------------------------------------------------ 4. 后台并发有上限

    def test_background_terminals_have_a_cap(self):
        '''到上限就拒绝启动，并点名要停哪几个 —— 不排队、不静默截断。

        排队的坏处：使用者拿到一个 running 的 id，而它其实还没开跑；
        若干分钟后才动，中途发生什么（输出为空、超时按起算点还是开跑点）
        都无法解释。
        '''
        shell, command = long_running_command(120)
        terminal.MAX_RUNNING = 1
        first = terminal.start_background(
            self.plane, {'command': command, 'shell': shell,
                         'cwd': self.dir.name})
        with self.assertRaises(terminal.TerminalError) as caught:
            terminal.start_background(
                self.plane, {'command': command, 'shell': shell,
                             'cwd': self.dir.name})
        message = str(caught.exception)
        self.assertIn('上限', message)
        self.assertIn(first['data']['terminalId'], message)


if __name__ == '__main__':
    unittest.main()