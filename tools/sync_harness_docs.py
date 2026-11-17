# -*- coding: utf-8 -*-
"""把「终端 / 授权审查 / 智能体团队 / Agent 循环」写进三份文档，并**回填**工具计数。

工具数是**算出来的**，不是数出来的：`§5.3 工具全表（25 项）` 这类数字一旦手写就会停在
写的那一刻，而"表里写 25 项、实际有 41 项"这件事没有任何地方会报错 —— 使用者按它去理解
"这个平台能做什么"，然后以为自己少了一大类能力。

**幂等**：计数用正则匹配任意旧数字（重跑按当前代码重算），三块正文各自带"已经写过就跳过"
的守卫。第一版没有守卫，重跑时当场以"锚点找不到"退出 —— 一个只能跑一次的迁移脚本会在
别人第二次跑它的时候留下一句看不懂的话。

架构设计.md 是 **CRLF** 行尾，所以按二进制读、按 `\r\n` 拼、按原样写。另两份是 LF。

用法：
    python tools/sync_harness_docs.py
"""
import io
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
PLATFORM = os.path.dirname(HERE)
sys.path.insert(0, PLATFORM)

from pentestd import policy  # noqa: E402


def read(name, newline=""):
    with io.open(os.path.join(PLATFORM, name), "r", encoding="utf-8",
                 newline=newline) as handle:
        return handle.read()


def write(name, text, newline=""):
    with io.open(os.path.join(PLATFORM, name), "w", encoding="utf-8",
                 newline=newline) as handle:
        handle.write(text)


def sub_once(text, pattern, replacement, where):
    """按正则替换**恰好一次**。命中 0 次或多次都当错误报出来。"""
    new, count = re.subn(pattern, replacement, text, count=0)
    if count != 1:
        raise SystemExit("%s：期望命中 1 处，实际 %d 处（pattern=%r）"
                         % (where, count, pattern))
    return new


def counts():
    levels = ["readonly", "probe", "audit", "full"]
    buckets = dict.fromkeys(levels, 0)
    for level in policy.TOOL_LEVEL.values():
        buckets[level] = buckets.get(level, 0) + 1
    running, total = {}, 0
    for name in levels:
        total += buckets[name]
        running[name] = (buckets[name], total)
    return running, total


LEVELS, TOTAL = counts()
DONE = []

# ---------------------------------------------------------------- 使用手册

guide = read("使用手册.md")
guide = sub_once(guide, r"### 5\.3 工具全表（\d+ 项）",
                 "### 5.3 工具全表（%d 项）" % TOTAL, "使用手册 §5.3")
guide = sub_once(guide, r"\*\*只读（\d+ 项，四档都有）\*\*",
                 "**只读（%d 项，四档都有）**" % LEVELS["readonly"][0],
                 "使用手册 只读档")
guide = sub_once(guide, r"\*\*探测档（\+\d+，共 \d+ 项）\*\*",
                 "**探测档（+%d，共 %d 项）**"
                 % (LEVELS["probe"][0], LEVELS["probe"][1]), "使用手册 探测档")
guide = sub_once(guide, r"\*\*审计档（\+\d+，共 \d+ 项）\*\*",
                 "**审计档（+%d，共 %d 项）**"
                 % (LEVELS["audit"][0], LEVELS["audit"][1]), "使用手册 审计档")
guide = sub_once(guide, r"\*\*完全作业档（\+\d+，共 \d+ 项）\*\*",
                 "**完全作业档（+%d，共 %d 项）**"
                 % (LEVELS["full"][0], LEVELS["full"][1]), "使用手册 完全档")

if "terminal_read` / `list_terminals" not in guide:
    guide = guide.replace(
        "| **`list_footholds`** | **查已登记的立足点**：哪台机器、凭什么给出口、"
        "背后标注了哪些网段 |",
        "| **`list_footholds`** | **查已登记的立足点**：哪台机器、凭什么给出口、"
        "背后标注了哪些网段 |\n"
        "| `terminal_read` / `list_terminals` | 读回一条终端命令的输出 / "
        "列出跑过的终端命令 |\n"
        "| `team_status` | 看团队任务板：每张任务的状态、就绪没有、"
        "没就绪是在等谁 |")
    guide = guide.replace(
        "| **`delegate`** | **派活**：一次同步调用拉起最多 6 个子智能体并发执行"
        "（见 §5.3） |",
        "| **`delegate`** | **派活**：一次同步调用拉起最多 6 个子智能体并发执行"
        "（见 §5.4） |\n"
        "| `team_create` / `team_add_member` / `team_add_task` / "
        "`team_update_task` / `team_done` | 组队：建团队、加成员、排任务（带依赖）、"
        "改一张还没跑的任务、把留给自己的活记完成 |\n"
        "| **`team_run`** | **派团队任务**：把就绪的任务各派一个成员去跑"
        "（同步返回，见 §5.9） |\n"
        "| **`agent_loop`** | **循环推进一个目标**：一轮一轮地推同一个目标，"
        "直到完成 / 收敛 / 轮数用尽（见 §5.9） |")
    guide = guide.replace(
        "| `start_scan` | 下发源码审计（目标路径、修复版路径、增量开关） |",
        "| `start_scan` | 下发源码审计（目标路径、修复版路径、增量开关） |\n"
        "| `terminal_kill` | 停掉一个后台终端命令 |")
    guide = guide.replace(
        "| **`run_tool`** | 跑一个本机已装的成熟客户端。**授权类别由工具本身决定**"
        "——跑 nmap 要放行 `toolkit` |",
        "| **`run_tool`** | 跑一个本机已装的成熟客户端。**授权类别由工具本身决定**"
        "——跑 nmap 要放行 `toolkit` |\n"
        "| **`run_command`** | **在控制面这台机器上执行任意命令**（见 §5.8）。"
        "它比 `run_tool` 更宽：不受适配器表约束。**每次执行前都会先要人批准** |")
    guide = guide.replace("> 粗体的 10 项是**本版新增**。",
                          "> 粗体的 14 项是**本版新增**。")
    DONE.append("使用手册 工具表行")

# §5.8 / §5.9（终端与授权审查、团队与循环）是**正文**，不由这个脚本生成 —— 它的职责
# 只有"回填计数"。所以这里只核在不在，不在就提示，而不是把一段正文塞进脚本里当模板
# （那种模板会与正文各自演化，然后有一天脚本跑出来的东西和文档里的东西不一样）。
if "### 5.8 终端与授权审查" not in guide:
    print("注意：使用手册 缺 §5.8 / §5.9（终端与授权审查、团队与循环），本脚本不生成正文")
write("使用手册.md", guide)

# ---------------------------------------------------------------- 项目说明

readme = read("项目说明.md")
if "### 终端与授权审查（DSH" not in readme:
    print("注意：项目说明 缺「终端与授权审查」/「智能体团队与 Agent 循环」两节，本脚本不生成正文")
write("项目说明.md", readme)

# ---------------------------------------------------------------- 架构设计
#
# 这一份是 **CRLF** 行尾。按二进制读、按 `\r\n` 拼、按原样写：用 `\n` 去匹配是永远
# 对不上的（`edit` 工具按原始字节比对，所以手工改它时也是同一条）。

ARCH_BLOCK = """### 4.9 智能体团队与 Agent 循环（team / loop）

**队长是这一轮对话本身**，不是另一个进程：`teams` 表里没有队长那一行。成员是花名册里的
**角色**，也不是常驻进程 —— `team_run` 给每张就绪的任务起一个子智能体，跑完就结束，结果
写回任务行。三张表（`teams` / `team_members` / `team_tasks`，schema v19）分开的理由是三种
东西的生命周期不同：团队建一次、成员偶尔增删、任务反复改状态，而后两件事是并发读的
（界面在看板，队长在派活）。

**就绪判定不在 SQL 里。** `team.ready()` 与 `team.blocked_reason()` 要区分"依赖还没跑"与
"依赖已经失败"，而这在 `status='done'` 这一个条件里长得一样；混起来的话，界面上显示的是
"等待中"，而它永远等不到。`dependsOn` 存的是同队任务 id 的 JSON 数组，不是序号 —— 序号会
随插入顺序漂移，而依赖是语义。

**两段的顺序不能反：先分「谁去」，再认领。** 把"全部认领、再分出留给队长的那几张"写成
一段，会让那些任务停在 `running`：没有人会去跑它们，而依赖它们的任务永远不就绪 —— 与
"中断留下的 running"是同一个形状，只是这次是自己造的。认领本身是条件更新
（`start_team_task` 只从 `pending` 认，靠 `rowcount` 判断），失败就跳过，因为跑两张一样的
任务会在目标上做两遍而任务板上只有一行。

**Agent 循环**每一轮都是**一个新的子智能体**（`loop.run` 里每轮 `spawn([spec])` 一次）。
两处关键：剩余预算**每轮重算**（`max_steps - steps - 1 - charged`，`charged` 是累加的），
所以"循环"在代码上不是预算加倍；"无进展"的判据是**报告摘要去空白与标点后逐字相同**，它是
唯一能从一轮传到下一轮的东西（循环不共享上下文），所以轮数上限始终是兜底。

三种运行时工具（`delegate` / `team_run` / `agent_loop`）跑的不是 handler，而是**对话循环
自己**：要模型配置、api_key、步数预算，以及一连串要 `yield` 出去的一等事件。三者签名与
返回形状刻意做成一样（`yield` 事件、`return (结果, 扣掉的预算)`），所以
`chat._RUNTIME_TOOLS` 那张调度表只写一次 —— 每加一种就复制一段的话，"哪条路径忘了把
`charged` 加回去"会变成要靠数代码行才能发现的事，而它的症状是循环静默地少算预算。

### 4.10 授权审查一路带到子智能体

`subagent.spawn(..., gate=...)`：`gate(call, emit) -> 工具结果`。父循环传下去的是
`chat._child_gate`，它做的事逐句与父轮那段一样 —— `check_tool` 过档 → 需要审查时挂起等人
裁决 → `invoke_tool` 执行。差别只有一处：子智能体在**它自己的工作线程**里跑，不能用 `yield`
把审查事件交出去，所以它就地抽干 `_reviewed` 这个生成器，把事件交给 `emit`（线程安全的
收集器），由父轮转发给界面。

**审查的判定与执行是分开的两件事**（`_review_only` 判定，调用方执行）。理由是执行有两种
形态：`invoke_tool` 一条 handler 就完事，而运行时工具要同步跑几分钟、中间过程必须在界面上
看得见。审查如果自己把执行也包上，第二种就用不了；而"给第二种另写一套审查"正是闸门分裂成
两处的开始。

**闸门，不是清单。** `CHILD_FORBIDDEN` 在 `_run_child` 里判定，而且必须在 `runner` **之前**
判定 —— 放它进去再改结论，活已经干了。此前这个集合只作用在 `resolve_tools`（那张给模型看
的表）上，而 `chat.execute_tool` 并不知道"我是不是一个孩子"，于是子智能体调 `team_add_task`
会**真的改掉队长的任务板**，调 `team_run` 只是碰巧被"本轮没有挂载运行时"挡住。名单与原因
现在是同一个字典（`FORBIDDEN_REASONS`）：加一个禁止项必须顺带回答"为什么"。

**审查单上的材料由工具自己的模块提供**（`chat._REVIEW_HOOKS` → `team.review_facts` /
`loop.review_facts`）。`harness.classify` 是纯函数（只拿到工具名与参数），取不到"这一批任务
到底是什么"—— 而计划审批卡的全部价值就在那份材料里：没有它，卡片上只有一行"派出子智能体
执行计划"，读的人没有任何判断材料。

### 4.8 派活（子智能体）"""

arch = read("架构设计.md")
# 行尾**不假设**：这一份曾经是 CRLF（当时手改它必须拿 `\r\n` 去匹配），后来在某一次
# 改动里变成了 LF。写死"必须是 CRLF"的版本会在那一刻以一句看不懂的 SystemExit 退出 ——
# 而我差点就把那句留在了这里。读的时候 `newline=""` 不做任何转换，写回去用的是同一个
# 参数，所以是原样进、原样出，无论它是哪一种。
if "### 4.9 智能体团队与 Agent 循环" not in arch:
    # `ARCH_BLOCK` **自带的末行就是那个锚点**（这样它前面接的是什么、后面接的是什么
    # 在常量里一眼看得全）。所以这里是"用整块替换锚点"，不是"块 + 锚点" —— 写成后者
    # 会得到一行 `### 4.8 派活（子智能体）### 4.8 派活（子智能体）`，而它在 markdown
    # 里仍然渲染成一个标题，所以看不出错，只有 `Select-String` 数行时才发现有两条。
    arch = arch.replace("### 4.8 派活（子智能体）", ARCH_BLOCK, 1)
    DONE.append("架构设计 §4.9/§4.10")
write("架构设计.md", arch)

print("工具总数 %d（只读 %d / 探测 %d / 审计 %d / 完全 %d）"
      % (TOTAL, LEVELS["readonly"][1], LEVELS["probe"][1], LEVELS["audit"][1],
         LEVELS["full"][1]))
print("本次写入：%s" % ("、".join(DONE) if DONE else "无（三份文档都已经是当前形态）"))
print("架构设计 行尾：CRLF %d / 裸 LF %d"
      % (arch.count("\r\n"), arch.count("\n") - arch.count("\r\n")))
