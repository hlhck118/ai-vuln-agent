# -*- coding: utf-8 -*-
"""一键启动 Web 控制台（部署入口）。

不用 Docker：整套系统的部署形态就是"一个 Python 进程 + 浏览器"。

    python deploy/start.py                 # 起服务并打开浏览器
    python deploy/start.py --port 9000
    python deploy/start.py --no-browser    # 只起服务（适合做成系统服务）
    python deploy/start.py --probe-scope default   # 网络探测只打回环与私有网段
    ./start.sh                             # 仓库根的启动器：只是省去敲 python 的那层包装

它会：检查引擎 → 选一个可用端口 → 起控制面 → 打开浏览器 → 打印节点接入提示。
所有后续运维（生成节点接入命令、下发任务、看进度）都在浏览器里完成。

**退出码**：0 正常退出（含 Ctrl+C）；1 配置或环境有问题；**2 = 指定端口上已经有
一个本平台实例在跑，这次没有起第二个**（见 `probe_instance`）。2 之所以与 1 分开，
是因为它不是失败："服务已经在了"这个结论对调用方有用，而"参数写错了"没用。
"""
import argparse
import json
import os
import socket
import sys
import threading
import urllib.error
import urllib.request
import webbrowser

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from pentestd import __version__ as PLATFORM_VERSION             # noqa: E402
from pentestd import agent, authz, console, dsh_bridge, dsh_runtime, engine, scope  # noqa: E402
from pentestd.server import serve                                  # noqa: E402
from pentestd.store import Store                                   # noqa: E402

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM_ROOT = os.path.dirname(HERE)
WEB_DIR = os.path.join(PLATFORM_ROOT, "web")
DEFAULT_DB = os.path.join(PLATFORM_ROOT, "data", "pentest.db")


def port_available(host: str, port: int) -> bool:
    with socket.socket() as sock:
        try:
            sock.bind((host, port))
            return True
        except OSError:
            return False


def pick_port(host: str, preferred: int, tries: int = 20) -> int:
    for offset in range(tries):
        candidate = preferred + offset
        if port_available(host, candidate):
            return candidate
    raise SystemExit("端口 %d 起连续 %d 个都被占用，请用 --port 指定" % (preferred, tries))


def lan_addresses() -> list:
    """本机的非回环 IPv4，给「监听所有网卡」模式在横幅里列候选地址。

    为什么不是让使用者自己去 `ipconfig`：`--host 0.0.0.0` 时横幅只报
    `http://127.0.0.1:...`（那是给本机浏览器用的），而**手机要的正是另一台
    机器怎么看这台机器**。这一行就是把那个地址直接递到眼前。

    只做展示，不参与监听：取不到就少一行，绝不让启动因此失败。虚拟机、
    WSL、VMware 的网卡也会在列表里——它们本来就是这台机器的地址，过滤掉
    只会让「为什么没列出来」变成一个新的要查的问题。
    """
    seen, out = set(), []
    try:
        infos = socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)
    except OSError:
        infos = []
    for info in infos:
        addr = info[4][0]
        if addr.startswith("127.") or addr.startswith("169.254.") or addr in seen:
            continue
        seen.add(addr)
        out.append(addr)
    return out


# 「这个端口上已经是我们自己」的退出码。与 1 分开：见模块头的说明。
ALREADY_RUNNING = 2
HEALTH_PATH = "/api/v1/health"


def probe_instance(host: str, port: int, timeout: float = 2.0) -> str:
    """这个端口上是什么：`free` / `ours` / `other`。

    **为什么必须有这一句。** `pick_port` 在端口被占时顺延到下一个——对"别的程序
    占了 8787"这是对的，对"我们自己已经跑着一个"则是错的，而且错得很安静：

      * 第二个实例会打开**同一个数据库**（`--db` 还是那一个）；
      * 本机执行节点按**名字**复用同一条 `executors` 记录（`store.ensure_executor`），
        所以两个实例的节点 id 相同；
      * 而节点启动时会回收"自己名下"的在途任务（`store.fail_orphan_jobs`）——
        于是第二个实例一起来，就把**第一个实例正在执行的任务**标成失败。

    使用者的观察是"双击了一下启动脚本，正在跑的扫描就变成失败了"，而两次启动
    之间没有任何一处报错。所以这里宁可不启动，也不替人做这个决定。

    `other` 的含义是"占着这个端口的东西不认我们这个健康接口"——照旧顺延端口，
    这是原有行为，也是唯一安全的解释方向：**只在确证是自己时才拒绝。**

    探测绕开系统代理：`urllib` 默认读 `http_proxy`，而本机回环地址走代理会
    要么连不上、要么问到一个不相干的服务器——两种都会让"是不是自己"判错。
    """
    # **先问健康接口，再问端口空闲不空闲。** 这个顺序是本函数的全部要点。
    #
    # 反过来（原来的写法）会漏掉一类真实情形：已经有一个 `--host 0.0.0.0`
    # 的实例在跑，再起一个默认的 `--host 127.0.0.1`。在 Windows 上第二个
    # bind 能成功（两边都开着 SO_REUSEADDR，通配地址与回环地址可以各占一份），
    # 于是 `port_available` 报「空闲」，健康探测根本没机会发言，第二个实例就
    # 和第一个共用同一个数据库跑了起来——正是这个函数要拦的那件事。
    #
    # 反过来问的代价只是一次回环连接：端口真空闲时它立刻 ECONNREFUSED，
    # 不会拖慢启动。
    probe_host = "127.0.0.1" if host in ("", "0.0.0.0") else host
    if health_is_ours(probe_host, port, timeout):
        return "ours"
    if port_available(host, port):
        return "free"
    return "other"


def health_is_ours(host: str, port: int, timeout: float = 2.0) -> bool:
    """这个端口的 `/api/v1/health` 是不是本平台在应。判不出来一律当「不是」。

    只认 200 上的 JSON 形状，不认文案：`status == "ok"` 且同时带
    `runningJobs` / `uptimeSeconds` / `executors` 三个键，别的程序极难凑巧撞上。

    独立成函数是为了让调用方能把「先探健康、再探端口」写成两行——顺序
    本身就带语义（见 `probe_instance`），藏在一个 if 的短路里最难看见。
    """
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))
    try:
        with opener.open("http://%s:%d%s" % (host, port, HEALTH_PATH),
                         timeout=timeout) as response:
            payload = json.loads(response.read().decode("utf-8", "replace"))
    except (urllib.error.URLError, ValueError, OSError):
        # 判不出来就按"不是我们"处理。这里刻意不捕 Exception：真出了别的毛病
        # 应该看见它，而不是被一句"这是别的程序"盖住。
        return False
    if not isinstance(payload, dict) or payload.get("status") != "ok":
        return False
    return {"runningJobs", "uptimeSeconds", "executors"}.issubset(payload)



def build_parser() -> argparse.ArgumentParser:
    """命令行契约单独成函数，默认值才可能被测试钉住。

    默认值散在 main 里时，"改了默认值忘了改 help"或"两处默认各写一遍"只能靠
    人工审代码发现——而探测范围恰好是最不该靠人工审的那一项。
    """
    parser = argparse.ArgumentParser(description="启动 AI 渗透测试工作台 Web 控制台")
    parser.add_argument("--host", default="127.0.0.1",
                        help="监听地址；要让别的机器访问控制台就用 0.0.0.0（务必配反向代理认证）")
    parser.add_argument("--port", type=int, default=8787)
    parser.add_argument("--db", default=DEFAULT_DB)
    parser.add_argument("--enroll-token", default="", help="节点注册凭据")
    parser.add_argument("--no-browser", action="store_true", help="不自动打开浏览器")
    parser.add_argument("--no-auth", action="store_true",
                        help="关闭控制台登录认证（仅限受控内网调试）")
    parser.add_argument("--window", type=int, default=200)
    parser.add_argument("--probe-scope", default=scope.DEFAULT_SCOPE_SPEC,
                        help="网络探测的范围，默认 any（不设限，任意地址可用）。"
                             "要收窄就写网段（逗号分隔，例如 203.0.113.0/24）；"
                             "写 default 只允许回环与私有网段；写 none 关闭网络探测")
    # 侵入式动作的准入。**默认全关**，而且它与 `--probe-scope` 是两条独立的清单：
    # 可连接的网段不等于可打的机器。这两个开关必须在这里也有——它们一度只存在于
    # `pentestd/cli.py`，而 `deploy/start.py` 才是"一键启动 Web 控制台"那个入口，
    # 于是按 项目说明 走的人拿到的是一个**永远打不开侵入式动作**的控制台，
    # 而所有迹象（界面、工具目录、授权面板）看起来都正常：面板如实报"全部关闭"，
    # 使用者只会以为自己没配对地方，不会想到是这个入口没有那个开关。
    parser.add_argument("--allow-intrusive", default="",
                        help="显式放行侵入式动作类别，逗号分隔："
                             "exploit（应用层利用验证）、creds（凭据测试）、"
                             "shell（载荷投递与落地确认）、pivot（立足点与出口路由）、"
                             "toolkit（外部工具调度），或写 all。**默认为空 = 全部关闭**。"
                             "这一类动作不由权限档解锁")
    parser.add_argument("--intrusive-targets", default="",
                        help="侵入式动作的目标白名单，逗号分隔（支持 IP、CIDR、"
                             "host:port）。与 --probe-scope 是两份独立的清单："
                             "可连接的网段不等于可打的机器。"
                             "留空 = 不限制目标（仍受 --probe-scope 约束）；"
                             "写 none = 一律拒绝")
    # 本机执行节点默认开启：本文件的部署宣言是"一个 Python 进程 + 浏览器"，
    # 而执行节点若必须手工另起，这条宣言就不成立——按提示走完的用户会得到
    # 一串永远 pending 的任务。控制面与执行面分离的部署用 --no-local-executor。
    parser.add_argument("--local-executor", dest="local_executor", action="store_true",
                        default=True,
                        help="在控制面进程内同时运行本机执行节点（默认开启）")
    parser.add_argument("--no-local-executor", dest="local_executor",
                        action="store_false",
                        help="只起控制面，任务全部留给远程执行节点")
    return parser


def main(argv=None) -> int:
    # 第一件事，早于任何输出：cp936 控制台打不出下面横幅里的 `⚠`，而那种异常会
    # **在开始监听之前**结束进程（加了 --no-auth / --host 0.0.0.0 就起不来）。
    console.make_safe()
    args = build_parser().parse_args(argv)
    try:
        probe_scope = scope.parse_scope_spec(args.probe_scope)
    except scope.ScopeError as exc:
        print("错误：--probe-scope 配置无效：%s" % exc)
        return 1
    try:
        intrusive = authz.normalize_classes(args.allow_intrusive)
    except authz.AuthzError as exc:
        print("错误：--allow-intrusive 配置无效：%s" % exc)
        return 1
    intrusive_targets = authz.parse_targets(args.intrusive_targets)
    if authz.incomplete_grant(intrusive, intrusive_targets):
        # 放行了类别，却把目标清单写成 none —— 每一次调用都会被 fail-closed 拒掉。
        # 那不是"更安全"，是"看起来开了却什么都做不了"，而使用者会去怀疑别的地方。
        # 条件本身归 `authz.incomplete_grant`：三个入口（这里、cli.cmd_serve、
        # toolclient）用的是同一个判断，只是各自把话说得适合自己。
        print("错误：--allow-intrusive 与 --intrusive-targets none 互相矛盾："
              "前者放行类别，后者一律拒绝，结果是开了却什么都做不了。"
              "去掉 --intrusive-targets 即可（留空 = 不限制目标）")
        return 1

    if not os.path.exists(os.path.join(WEB_DIR, "index.html")):
        print("错误：未找到 Web 界面资源 %s" % WEB_DIR)
        return 1

    # 先问一句"这个端口上是不是我们自己"，再决定要不要起。放在 `pick_port` 之前：
    # 那一步在端口被占时会顺延到下一个端口，而那正是这里最不该发生的事。
    if probe_instance(args.host, args.port) == "ours":
        display_host = "127.0.0.1" if args.host in ("", "0.0.0.0") else args.host
        url = "http://%s:%d/" % (display_host, args.port)
        print("=" * 68)
        print("  这个端口上已经有一个本平台实例在跑，没有再起第二个")
        print("=" * 68)
        print("  控制台   %s" % url)
        print("-" * 68)
        print("  为什么不替你起第二个：两个实例会打开**同一个数据库**，而本机执行")
        print("  节点按名字复用同一条记录（`store.ensure_executor`），新起来的那一个")
        print("  于是会把它名下「执行中」的任务标成失败——也就是打断你手上正在跑的")
        print("  那次作业，而两次启动之间不会有任何一处报错。")
        print("-" * 68)
        if args.no_browser:
            print("  要用它就直接开上面那个地址。")
        else:
            print("  已经替你打开上面那个地址。")
        # **这一屏是第一现场，所以必须把"怎么换成新起的那一份"说完。** 改了
        # `pentestd/` 下的代码之后，正确动作是"停掉再起"；而使用者的第一反应是
        # 再双击一次本文件——那只会又看到同一屏，没有任何一行字告诉他下一步。
        # 于是"改了没生效"会被读成"这次改动没用"，去改别的地方。实测发生过：改完
        # `pentestd/agents.py` 又双击启动器，服务端仍是 65 分钟前起的那一份，
        # 保存依然报改掉之前的那句错，而屏幕上每一处看起来都像重启成功了。
        #
        # 给的命令按 PID 而不是 `taskkill /IM python.exe /F`：后者会连**同机别的
        # python 一起杀掉**——包括正在跑测试的那个，而那通常不是使用者想要的。
        print("-" * 68)
        print("  想换成**新起的那一份**（例如刚改过 pentestd/ 或 deploy/ 下的代码）——")
        print("  这个判断只到「有没有在跑」为止，它不会替你重启：直接再双击一次，")
        print("  只会又看到这一屏。停掉正在跑的那个，再双击：")
        print("    它自己的窗口里按 Ctrl+C（最稳：会跑 finally 有序关停）")
        print("    找不到窗口，就先查是谁占着端口： netstat -ano | findstr :%d"
              % args.port)
        print("    再按那个 PID 结束：               taskkill /PID <上面那个 PID> /F")
        print("-" * 68)
        print("  确实需要另一个实例：换端口 `--port 9000`，或换数据库 `--db <路径>`。")
        print("=" * 68)
        if not args.no_browser:
            webbrowser.open(url)
        return ALREADY_RUNNING

    port = pick_port(args.host, args.port)
    if port != args.port:
        print("端口 %d 被占用，改用 %d" % (args.port, port))

    store = Store(args.db)
    # 本机执行节点与控制面同进程：共用 Store（一条连接 + 可重入锁，见 Store
    # 的线程模型说明），不经 HTTP，因此不需要令牌。
    executor = node_thread = None
    if args.local_executor:
        executor, node_thread = agent.start_local_executor(
            store,
            work_root=os.path.join(os.path.dirname(os.path.abspath(args.db)), "work"))
    httpd = serve(store, WEB_DIR, args.host, port, args.window,
                  enroll_token=args.enroll_token,
                  auth_required=not args.no_auth,
                  probe_scope=probe_scope,
                  intrusive=intrusive,
                  intrusive_targets=intrusive_targets)
    display_host = "127.0.0.1" if args.host == "0.0.0.0" else args.host
    url = "http://%s:%d/" % (display_host, port)
    user_count = store.count_users()
    active = store.active_profile()

    print("=" * 68)
    print("AI 智能体渗透测试工作台（Web 端部署）")
    # 版本在横幅里，与 health 的 `version` 同源：使用者重启后第一眼就能确认
    # 跑的是不是刚才改的那一份（踩过：改完又双击一次，进程还是旧的）。
    # 标签后面是 5 个空格，不是 3 个：对齐按**显示列**算，一个汉字占两列，
    # `版本` 比 `控制台` 窄两列。写成 3 个空格时这一行的值会左移两列，而
    # 其余各行都在第 12 列——错位看起来像输出坏了，也让人怀疑版本号是不是没取到。
    print("  版本     v2（%s）" % PLATFORM_VERSION)
    print("=" * 68)
    print("  控制台   %s" % url)
    if args.host == "0.0.0.0":
        # 本机浏览器用上面那条；下面这条是给同一内网里的手机/别的机器的。
        # 端口可能被 pick_port 顺延过，所以这里用的是真正的 `port`。
        lan = lan_addresses()
        if lan:
            print("  局域网   %s" % "  ".join("http://%s:%d/" % (ip, port) for ip in lan))
            print("           ^ 手机/别的机器用与你同网段的那个地址")
        else:
            print("  局域网   没取到本机网卡地址（用 ipconfig 查）")
    print("  数据库   %s" % args.db)
    home = engine.locate_engine()
    print("  发掘引擎 %s" % (home or "未找到（扫描任务会失败，请设置 VULNAGENT_HOME）"))
    print("  登录认证 %s" % ("已启用" if not args.no_auth else "已关闭（--no-auth）"))
    if not args.no_auth:
        print("  账号     %s" % ("%d 个" % user_count if user_count
                                 else "尚无——首次打开工作台会要求创建管理员"))
    print("  推理模型 %s" % (("%s（%s · %s）"
                             % (active["name"], active["provider"], active["model"]))
                            if active else "未配置，使用离线启发式推理"))
    print("  注册凭据 %s" % ("已配置" if args.enroll_token else "未配置（建议加 --enroll-token）"))
    print("  探测范围 %s" % scope.scope_text(probe_scope))
    # 侵入式授权必须在启动横幅里可见。它与探测范围是**两条独立清单**，而"全部
    # 关闭"与"控制面压根没被告知任何放行"长得一模一样——命令行里这一行是使用者
    # 唯一能在启动时确认自己配对地方的机会。缺了它，一个拼错的类别会一路静默到
    # 使用者去点「下发」的那一刻。
    if authz.FORCE_OPEN:
        # 强制放行是**代码里的**开关，横幅必须说出来。不说的后果有一条：为了让
        # 侵入式动作可用，使用者会去翻命令行、改参数、重启——而那件事已经不需要
        # 做了，改了也没有任何区别，他会以为是参数写错了，然后去查别的地方。
        print("  侵入授权 %s" % authz.force_open_text())
    else:
        print("  侵入授权 %s" % ("、".join(intrusive) if intrusive
                                 else "全部关闭（默认；用 --allow-intrusive 放行）"))
        if intrusive:
            print("    目标白名单 %s" % authz.targets_text(intrusive_targets))
    print("  本机节点 %s" % ("已启动（scan + probe，控制面进程内）" if executor
                             else "未启动（--no-local-executor）"))
    # DSH 有三半，横幅各说一句：
    #   * 本体与 pnpm：项目自带的 `vendor/dsh`（不靠 PATH、不靠桌面安装）；
    #   * 插件包（界面）随仓库分发，装不装由使用者自己的 DSH 决定；
    #   * 运行时补丁只在「DSH 运行时」真正拉起时生成，写在平台自己的 home 下。
    _home = os.path.join(os.path.dirname(os.path.abspath(args.db)), "dsh-home")
    print("  DSH 本体   %s" % ("自带 %s（%s）" % (dsh_runtime.vendored_version(),
                                                  dsh_runtime.VENDOR_DSH_BIN)
                              if dsh_runtime.vendored_version()
                              else "未安装（跑 vendor\\dsh\\install.cmd）"))
    _plugin = os.path.join(PLATFORM_ROOT, dsh_bridge.PLUGIN_DIR, dsh_bridge.PLUGIN_PATCH)
    _patch = dsh_bridge.runtime_patch_path(_home)
    print("  DSH 插件   %s" % ("界面已就绪（%s）" % _plugin
                              if os.path.isfile(_plugin) else "界面缺失（%s）" % _plugin))
    print("  DSH 运行时 %s" % ("平台工具补丁已生成（%s）" % _patch
                              if os.path.isfile(_patch)
                              else "工具补丁未生成（第一次走运行时通道时生成）"))
    print("-" * 68)
    print("  后续步骤都在浏览器里完成：")
    print("    0. 首次打开 → 创建管理员账号（没有默认口令）")
    print("    1. 「设置」页签 → 选择模型预设（云端或本机）→ 测试连接 → 设为启用")
    print("    2. 「部署」页签查看自检；本机已能执行任务，要扫别的网段再加节点")
    print("    3. 在「会话」里用对话下发作业（源码审计 / 网络探测）→ 「作业」页看执行记录")
    if args.host == "0.0.0.0" or args.no_auth:
        print("-" * 68)
        print("  ⚠ 已监听所有网卡或关闭认证，请只在受控内网这样用；")
        print("    对外必须置于带认证的反向代理之后并启用 TLS。")
    print("=" * 68)
    print("按 Ctrl+C 停止")

    if not args.no_browser:
        threading.Timer(1.0, lambda: webbrowser.open(url)).start()
    try:
        httpd.serve_forever()
    except KeyboardInterrupt:
        print("\n已停止")
    finally:
        # 顺序要紧：先让节点停手，再关连接。反过来的话节点线程会用到已关闭的
        # 连接（SQLite 会直接抛 "Cannot operate on a closed database"）。
        if executor is not None:
            executor.shutdown()
        if node_thread is not None:
            node_thread.join(timeout=5.0)
            if node_thread.is_alive():
                print("  ⚠ 本机节点仍在执行任务，未能在 5s 内停止；进程退出会中断它，"
                      "这条作业会停在「执行中」，需重新下发一次。")
        httpd.server_close()
        store.close()
    return 0


if __name__ == "__main__":
    sys.exit(main())
