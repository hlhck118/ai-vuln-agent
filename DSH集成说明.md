# DSH 融合通道（P1）

这份文档只讲**这一版相对上游新增的那条通道**：让本平台的对话循环可以跑在 DSH 运行时上，
并把平台的工具表接进 DSH。其余部分（任务队列、探测、报告、插件市场…）与上游一致，
见 `项目说明.md` / `架构设计.md` / `使用手册.md`。

---

## 一、一句话

平台仍是**会话、落库与闸门**的持有者；打开开关之后，**模型循环、工具执行、上下文压缩**
交给 DSH —— 于是插件带来的能力（工具 / 技能 / 记忆 / 子智能体 / 团队）从那一刻起对平台
对话可见，而平台那 40 个工具反向接进 DSH（桥）。

```
                    ┌──────────────── 平台控制面（8787）─────────────────┐
  浏览器 ──SSE──▶   │  server.send_chat_stream → chat.run_turn_stream   │
                    │        │                                          │
                    │        ├─ runtime="off" → 平台原生循环（默认）    │
                    │        └─ runtime="sdk" → _run_turn_dsh ──┐       │
                    └──────────────────────────────────────────│───────┘
                                                               ▼
                                    dsh_runtime（一行一行 JSON-RPC，stdio）
                                                               ▼
                    ┌────────────── DSH 运行时（app.asar 里的那份）───────┐
                    │  模型循环 · 压缩 · 子智能体 · 技能 · 记忆 · 插件    │
                    │            │                                       │
                    │            └─ mcp__platform__* ──▶ platform_mcp_server.py
                    └──────────────────────────────────────────│─────────┘
                                                               ▼
                    chat.execute_tool（权限档 / 作用域 / 侵入授权 / 审查）
```

**两个方向都要**，少一个都不算融合：只有前者，模型在 DSH 里看不到平台的东西（只能空谈）；
只有后者，平台的对话还是自己那套循环（插件能力一个都用不上）。

---

## 二、怎么打开

设置页 → **运行通道**（管理员）：两档 —— 「平台原生循环（默认）」/「DSH 运行时」。
选「DSH 运行时」后点「保存」即可，**没有第二个开关**：平台工具随这条通道一起挂进去。

命令行等价物：

```
POST /api/v1/dsh/runtime   {"mode": "sdk"|"off"}
GET  /api/v1/dsh           → { dsh, hub, isolation, pending, runtime }
```

它是一次**部署口径**（对所有会话一致），所以要求管理员。打开时会把"能不能开"一并
返回：缺 `dsh` 启动器或缺 SDK 面补丁时，界面上显示的是**原因**，而不是一个看起来正常的
"已打开"。

---

## 三、实测事实（都来自真机跑出来的帧，不是推断）

| 事实 | 证据 |
|---|---|
| 通道能跑通 | 一轮真实对话：`turn.runtime=dsh`、`delta/reasoning/tool/persist/done` 齐全、`finishStatus=completed`、`usage.totalTokens=6396` |
| 上下文真的连续 | 第二轮引用第一轮的工具输出（"刚才那条命令的输出是 42"） |
| 工具行现在有名字 | `mcp__platform__list_engagements`（修之前是空串：名字只在 `tool/call` 上，结果帧里没有） |
| 桥真的接上了 | 模型看到 **47** 个工具，其中 **20** 个是平台的（`mcp__platform__*`，正好等于 `readonly` 档的暴露数） |
| 执行回到平台的闸门 | 桥走 `chat.execute_tool`，返回体就是平台工具的原样输出：`{"ok": true, "summary": "共 0 个 engagement", …}` |
| 档位是活的 | 桥的档位每次调用现读设置，不是启动时定死（改完不用重启 DSH） |
| 子智能体对平台可见 | 2026-10-20 真机：一轮派两个原生 `subagent` → **16 条** `subagent/*` 相位帧（start/note/step/tool/done）；落库 `subagents` **两行**，`state=done`、任务名、步数、耗时、结论、工具芯片都在 |
| 平台工具的审查闸门也在这条通道上 | 桥的每一次平台工具调用先 `POST /api/v1/dsh/approval` 回控制面问一句；待裁决的卡片开在控制面里（等的人的集合在控制面进程内），到期/问不到人按放行口径继续 |
| 单子只开在**这条对话主人**的名下 | `POST /api/v1/dsh/approval` 的 `session.sessionId` 要过 `_require_review_access`（与 `/reviews` 同一把）：拿别人的会话 id 打进来是 `403`，一条单子都不会落库。桥自己带控制台令牌、落到管理员，所以正常链路不受影响——收的是「普通登录会话也能打这条接口」那条路 |

会话与事件的关键形状（`dsh_runtime.Mapping` 按这些写，测试逐字钉住）：

```
tool/call   { event: { type, data: { turn, step, callId, name, arguments } } }
                 ↑ arguments 是 **JSON 字符串**，不是对象
tool/result { event: { type, data: { message: { toolCallId,
                                                content: [{type:"text", text}],
                                                isError } } } }
                 ↑ 正文在**块数组**里；名字**不在**这一帧，要按 callId 从 call 那侧接回来
turn/end    { event: { data: { reason: { kind } } } }     kind=completed 才算成了
session.status → idle                                      **收尾判据**（不是"多久没通知"）
```

### 三之二、子智能体在这条通道上的形状（2026-10-20 真机）

DSH 的原生派活（`subagent` / `subagent_fork`）会开**子会话**，主会话只收到三样东西：

```
subagent/catalog  { event: { data: { childId, label, mode, version } } }
                      ↑ 任务名在 `label`；这是子会话开始前就到的唯一可读字段
subagent.started  { parentSessionId, childSessionId }        ← 没有名字、没有任务
subagent.finished { provider, agentId, parentSessionId, childSessionId,
                    status: 'ok'|'error', stopReason,
                    lastAssistantMessage?: ContentBlock[] }  ← 结论只给最后那条消息
session.event     { sessionId, event: {type, data} }         ← **子会话的事件也走这一帧**
```

`Mapping` 因此做四件事（每一条都有测试订住，`tests/test_dsh_runtime.py`）：

1. **按 `sessionId` 分流**：非本轮父会话的事件进 `_feed_child`，不污染父轮的
delta / 工具行 / 认不出计数器。子会话的 `session.status` 更不能当收尾判据 ——
第一个子会话闲下来就收尾，父轮会被截断（真机一条派两个孩子的轮次正是这个形状）。
2. **任务名只认 catalog**：子会话自己那条 `user/message` 装的是 DSH 注入的
"Current runtime context…"，拿它当任务名就挂一行噪声上去；不认识的前缀一律不用。
3. **行形状与原生循环逐键一致**（`chat._subagent_rows` 那套 12 键）：实时区块走
`subagent` 相位帧（start/step/tool/note/done），刷新后走落库的 `subagents` 行 ——
两条通道共用一个渲染器（`web/chat.js` 的 `applySubagent` / `subagentNode`），
键名一分叉，同一轮就会出现"当时看到、刷新没了"这种最难的缺陷。
4. **收尾多等一扇窗**：父轮 `idle` 时子会话可能还在跑（`subagent` 可异步返回），
直接 break 会让那行永远停在"跑动中"。所以 `SUBAGENT_SETTLE_S=45` 内再听收尾帧，
收完一个重新计窗，窗口过期或队列静着就收；收尾时仍在跑的只加一条 `note`
（"之后跑成什么样，平台这边看不到"），**不改状态、不编结论**。

口径上的配套：平台原口径写着"本平台没有原生 `subagent`"（那是原生循环下的事实）。
运行时通道里恰好相反 —— 原生派活能用，`delegate` 反而跑不起来（它需要平台循环的
模型配置与步数预算）。这段更正由 `agents.DSH_RUNTIME_NOTE` 在 seed 系统提示时追加，
免得模型同时收到"用 delegate"与"delegate 不可用"两条相反的话。

**平台自己的扇出也走这条通道（代码已接，未真机）。** `run_turn_stream` 在 DSH 分支
**之前**先跑一轮平台扇出（规则见 `架构设计.md` §4.8）：报告不塞进 `working`
（那是哈希表，DSH 分支不用），而是作为 `preface` 拼在 seed 的 user prompt 前面；
连同一句"不要再派活，只做汇总"。平台派出的行与该轮 DSH 原生行并入同一个
`message.subagents`，序号按列表重排（`index/total` 连续）——界面上看到的是"这一轮
所有孩子"，不区分是谁派的。这一半目前只有代码路径：本项目 `dsh_runtime` 默认
`off`，上面所有真机证据都来自原生通道；打开 DSH 后要再跑一轮同样的话确认，
尤其是"报告进了 seed、父轮只汇总"这条。

---

## 四、四条边界（都是刻意的，也都写在代码注释里）

1. **单轮步数预算与上下文压缩在这一轮不生效；审查闸门只盖平台工具。** 循环是 DSH 的，
   所以平台自己的步数预算（`max_steps`）管不到它；上下文压缩也按 DSH 的口径走。
   **审查闸门是真的在生效的**，但它挂在平台那 20 个工具上——`platform_mcp_server`
   的 `call_tool` 先 `POST /api/v1/dsh/approval` 回控制面，由 `chat.review_platform_call`
   复用平台审查栈（`approval.submit → waiting → wait`）开单、等裁决；**DSH 自带的
   bash/fs 不过这一道**（那是宿主自己的审批与沙箱）。超时、问不到控制面、没有令牌、
   控制面 500：一律放行并在审查单上写明（B 口径，用户 2026-10-20 决定）。
   平台上会发一条 `notice:runtime` 把这两句说清 —— 不说的话，使用者会以为"我设了 6 步、
   它没听"或者"我开了严格审查、它没问我"。
2. **一条 DSH 会话只能在创建它的那个进程里继续。** 实测：跨进程拿同一个 id 去
   `session/prompt`，服务端回 `-32603 session "<id>" already exists`（新进程会尝试**创建**
   它，而持久层里已经有）。所以会话身份挂在进程上：通道重建之后，DSH 那边是一段新对话，
   平台自己的历史仍在库里、系统提示会重新种一次。
3. **工具面是宿主级的，不是会话级的。** DSH 那棵树是长驻的，会话级的权限档传不进
   它的启动环境；会话级收窄由 `execute_tool` 那一道负责。**但「这条动作写进哪个目标」
   是会话级的**：补丁按会话生成，把 `PENTEST_SESSION` 写进服务启动环境；桥的
   `principal()` 把它作为 `sessionId` 交给 `chat.execute_tool`，平台据此把本轮全部任务
   归到该对话自己的一个目标（`store.ensure_session_engagement`）。漏掉这一步的实测
   形状是一条对话 11 次工具调用建出 11 个目标——「漏洞」「资产」两页于是只剩最后一次
   调用的收获。判据在 `tests/test_platform_bridge_gate.py`：执行那一步的 `session`
   实参里必须有 `sessionId`，没有 `PENTEST_SESSION` 时**不许**留空串键。
4. **绑定在 DSH `0.2.0-rc.2` 上。** 事件类型是实测形状，上游改了就会表现为"某些事件
   认不出"——所以 `Mapping` 把**认不出的只计数、不发明**，并在界面上报一条
   `notice:unknown-events`；而已识别的内部事件（`user/message`、`step/start`… 共 15 类）
   走另一个计数器，免得那条告警被训练成背景噪音。

---

## 五、代码落点

| 文件 | 职责 |
|---|---|
| `pentestd/dsh_runtime.py` | 协议与进程：`SdkProcess`、`DshRuntime`（`open`/`prompt`/`iter_turn`/`wait_idle`/`session_for`）、`Mapping`（事件翻译）、`channel()`（复用）、`state()` |
| `pentestd/dsh_bridge.py` | 运行时补丁：`write_runtime_patch`（只写平台自己的 home，MCP 那一半）/ `patch_path`；插件包的界面补丁不在这里生成 |
| `tools/platform_mcp_server.py` | 桥的另一端：一个 stdio MCP 服务端，把平台工具暴露给 DSH，执行走 `chat.execute_tool` |
| `pentestd/chat.py` | `_run_turn_dsh` + `run_turn_stream` 顶部那条分支（`turn.runtime` 字段说明这一轮由谁执行）；`review_platform_call`（桥的审查闸门在控制面这一侧的实现） |
| `pentestd/agents.py` | `DSH_RUNTIME_NOTE`：运行时通道专属的口径更正（原生 `subagent`/`subagent_fork` 在本轮可用、`delegate` 在本轮不可用），在 seed 系统提示时追加 |
| `pentestd/server.py` | `POST /api/v1/dsh/runtime`、`GET/POST /api/v1/scan-engine`；`GET /api/v1/dsh` 补上 `runtime`/`scanEngine` |
| `pentestd/scan_engine.py` | 「扫描执行」口径：`SETTING`/`LABELS`/`describe`/`name_of`/`payload_for`/`dsh_availability`。读侧宽容（工位上一切没配过都按 `vulnagent`），写侧严格（只认两档） |
| `pentestd/scan_dsh.py` | 推理引擎扫描：开一条**新**会话在目标目录只读审计 → `extract_json`（容忍围栏与前后废话）→ `normalize`（一律 `confirmed_static`）→ `engine.build_chain_specs` 同一套节点形状；树指纹前后对比只用来标 degrade，不写 baseline |
| `web/settings.js` · `web/index.html` | 「运行通道」与「扫描执行」两个设置分组（前者管对话由谁跑，后者管扫描任务由谁跑） |

---

## 五之二、融合进平台的 DSH 网页应用（`/dsh/`）——**已删除，保留作决策记录**

> **这一节不对应任何现存代码。** `pentestd/dsh_web.py`、`pentestd/dsh_proxy.py`、`web/dsh.js`
> 与 `/dsh/` 路由都已删除（为什么删见第八节「两条被否掉的路」），`tools/verify_dsh_web.py`
> 也随之换成 `tools/verify_dsh_channel.py`。下面的行为描述留着是为了记住当时踩过什么；
> 要动手改这一块之前，先读第八节。

平台自己那份 `dsh web` —— 一个**完整的** DSH 网页应用，跑在平台自己的 home 里，而入口
**挂在平台自己的源下**（`/dsh/`）。左侧「会话」页签里嵌的就是它——页签名先后从「DSH 控制台」改成「控制台」、再改成
「会话」，因为它装的是这一份完整的 DSH 网页应用，一屏一条会话，按页里实际的东西命名最
省事；`data-tab` 始终是 `dsh`，改名只动标签。这一页**已不在导航第一位**（第一位是
「仪表盘」），但 `/dsh/` 这个挂载点与页签键都没动。

```
DSH_HOME = <项目>/data/dsh-home         与电脑上那份 ~/.dsh 毫无关系
profile  = pentest-web                  首次启动时从发行模板 web 初始化一次
端口      = 从 8790 起找一个**没人监听**的  没人需要知道它（入口是 /dsh/）
argv     = <dsh> pentest-web [--from-default-profile web] [--patch <桥>] \
                 --port N --host 127.0.0.1 --no-open
环境      = DSH_HOME=<项目>，剥掉 DSH_PROFILE/DSH_SHELL/DSH_SESSION_ID/DSH_WEB_URL
           与 DEEPSEEK_API_KEY（凭据只从它自己的 home 读）
输出      = data/dsh-home/logs/dsh-web.log（**不是管道**，见下）
浏览器看到  = http://<平台>/dsh/            一份 cookie 都不需要（凭据在服务端）
```

**它替掉的是"另开一个实例"**：在那之前，使用者要拿到子进程打印的那个带 `?token=` 的地址、
另开一个标签页 —— 两个端口、两份 cookie、两个登录、一个要手抄的 token。现在这些都在
`dsh_proxy` 那一层里消失了（见那份模块说明里的三条实测规则：`Host` 必须换成上游的
authority、`Origin` 跟着换、请求目标原样转发；以及"升级那一对头不能按逐跳丢掉"与
"凭据只在服务端"）。

**实测（真机，2026-10-16）**：

```
$ python .scratch/p81/web_probe.py
argv      : [...\dsh.CMD, pentest-web, --from-default-profile, web, --port, 8790, ...]
DSH_HOME  : <项目根>\data\dsh-home
leaked    : []
phase     : running        pid 41784   url http://127.0.0.1:8790/
profileReady: False -> True          ← 模板初始化在 4.6 秒内完成
```

（启动器那一行是当时那张实测的原文：它是 PATH 上的 `dsh.CMD`。2026-10-20 起启动器
固定为 `node …/vendor/dsh/…/lib/bin.js`，见下面「DSH 本体从哪来」那一节。）

它写到了哪里（同一时刻扫两份 home，窗口 09:26:40 起）：

```
项目 home      .credentials.yaml / profiles（新 profile 4 个文件）/ storages     合计 6 个
电脑那份 ~/.dsh  .dshw-usage.json / dsh-purge / sessions / storages             合计 4 个
                ↑ 时间戳一直在往后走，是**桌面应用自己的心跳**（就是你现在这个对话），
                  两份窗口里是同样 4 个名字；把我起的宿主停掉之后它照旧在动。
```

**三条踩过的坑（都是实测踩出来的）**：

1. **输出不能接管道。** 第一版 `stdout=PIPE` + 读线程：启动脚本一退出，管道读端关闭，
   `dsh web` 下一次写 stdout 吃到 EPIPE 直接退出。症状是"显示 running、4.6 秒后
   一切正常、再去看端口上什么都没有"——而日志跟着进程一起没了。落文件之后两个问题
   一起消失。
2. **失败原因要按实际原因报。** "起来就死"和"迟迟不就绪"原本打印的是同一句话
   （"在 Ns 内没有开始监听"），于是子进程自己写在 stderr 上那行唯一能解释原因的
   字成了摆设。
3. **端口先探测再交给子进程**，不用 `--port 0`：让 OS 挑的话那个值只会打印在子进程
   输出里，而"打开哪个地址"是这一块唯一必须确定的东西。

### DSH 本体从哪来：项目自带一份（2026-10-20）

在这之前，平台的 DSH 是用 `shutil.which("dsh")` 在 PATH 上找的 —— “本机装没装
桌面版”会决定平台用哪一份，而桌面版一次自动更新就悄悄换掉了平台的行为（版本差异
最后都以“某个工具忽然不认某个参数”的形式从很远的地方冒出来）。现在**可执行文件、
SDK 补丁、插件 CLI、pnpm 全部来自项目内的 `vendor/dsh/`**，PATH 上的 `dsh` 一律
当不存在：

```
vendor/dsh/
  package.json      ← 版本钉在这里（@deepseek-ai/dsh 0.2.1-rc.2、dsh-sdk-app、
                       dsh-sdk-jsonrpc-server、pnpm 11.28.5）
  install.cmd       ← 装/升级：npm install，校验 node ≥22，结尾打印版本
  bin/dsh.cmd       ← 手写启动器（找不到入口退 3、没有 node 退 4）
  node_modules/@deepseek-ai/dsh/lib/bin.js          ← 真正的入口（JS，由 node 跑）
  node_modules/@deepseek-ai/dsh-sdk-app/cordis.patch.yml  ← SDK 面补丁
  node_modules/.bin/pnpm                            ← 插件安装用的 pnpm
```

四条不显然的地方（都是踩出来的）：

1. **入口是 `.js`，不是 `.cmd`。** 直接 `Popen([path])` 吃 `.js` 会 `WinError 193`，
   所以所有起 DSH 的地方都走 `dsh_runtime.command_for()`：`.js` → `[node, path]`，
   `.cmd`/`.bat` → `[COMSPEC, /c, path]`。漏一处就有一个入口还在旧假设上。
2. **pnpm 是第二个“从哪来”。** `dsh plugin add/remove` 与 `workspace-dependencies`
   工具都去 PATH 上找 pnpm；桌面版靠自己 primary-runtime 里那份。自封装之后由
   `dsh_runtime.prepend_vendor_bin()` 把 `vendor/dsh/node_modules/.bin` 前置进子进程
   PATH（`DshRuntime.env()`、`hub.run_cli`、`install_console_plugin.child_env` 三处）。
   不塞的实测症状：`'pnpm' is not recognized as an internal or external command`。
3. **SDK 面补丁也自带。** `locate_sdk_patch()` 只认 vendor 包里的 `cordis.patch.yml`——
   它随 npm 包装进来，是项目自己的文件（不再是“从 app.asar 里借一份”，那个 Python
   本来也读不到）。平台自己那份 `patches/pentest-sdk.patch.yml` 照旧叠加。
4. **升级 = 改一行 + 一条命令。** `vendor/dsh/package.json` 里改三个包的版本（同一
   版本号），跑 `vendor\dsh\install.cmd`；启动器与平台代码都不用动。

断链的判据在 `DshRuntime.state()` 里：`vendored`（是不是项目内那份）、`dshBin`、
`dshVersion`；`python tools/verify_dsh_channel.py` 打印的就是它们。离线验收在
`tests/test_dsh_runtime.py::VendorTests`——其中一条专门钉死：vendor 里没有时
`locate_dsh()` 必须返回空串，**不许回头去 PATH 上找**。

### 对话用的 profile 里装什么：团队插件（2026-10-20）

这里有两个**完全不同**的目录，以前混着说，现在必须分开：

| 目录 | 谁在用 | 决定什么 |
|---|---|---|
| `data/dsh-home/profiles/pentest-platform` | 插件市场 / `dsh plugin`（`dshplugins.PLATFORM_PROFILE`） | 平台自己的插件容器里有哪些包 |
| `data/dsh-home/profiles/<app_settings.dsh_profile>`（今天 `v2sdk`） | **对话**（`DshRuntime`、`dsh_runtime.channel()`） | 模型这一轮到底加载了哪些插件 |

对话改由项目内的 DSH 执行之后，第二行第一次变得“有后果”：装进它的宿主插件是**真的
会加载**。子智能体/团队插件就是这么接进来的——用项目里的脚本，比自己拼命令稳
（它读设置里的 profile、用 vendor 的 pnpm、装完再核三处，重复跑不会重装）：

```bat
python -X utf8 tools\install_conversation_plugin.py        :: 装/自检默认的团队插件
python -X utf8 tools\install_conversation_plugin.py --check :: 只看状态
```

它等价于下面这条手工命令（`--profile` 要换成设置里 `dsh_profile` 的值，今天是
`v2sdk`）：

```bat
:: 在项目里装（DSH_HOME 指向项目自己的 home；pnpm 由 vendor 提供）
set DSH_HOME=%~dp0data\dsh-home       :: %~dp0 就是这条 .bat 所在目录（放项目根）
vendor\dsh\bin\dsh.cmd plugin --profile v2sdk add @nanmicoder/dsh-agent-teams
```

有一处 pnpm 的坑脚本已经处理、手工做时要知道：pnpm 把 store 与 virtual store 的
**绝对路径**记进 profile 的 `node_modules/.modules.yaml`，而默认 store 跟着
**项目所在盘**走。所以把一份 home 从别的盘拷过来之后，`pnpm add` 会报 “pnpm now
wants to use the store at …” 直接退出——那不是插件的问题，先删掉该 profile 的
`node_modules` 与 `pnpm-lock.yaml` 再装。

装完要**两样都成立**才算生效：包的 `dependencies` 里有它，而且 `dsh.profile.bundles`
里有它。`@deepseek-ai/dsh-plugin-manager` 的 `reconcile()` 会顺手加进 bundles，但**别
只看“安装成功”**：只有 `dependencies` 而没有 bundles 是安装残留，模型看不到
`agent_teams_*`。这一条判据只写在一处——`dshplugins.conversation_teams()` 的 `active`，
`chat`（是否附团队口径、是否发 notice）、`DshRuntime.state()`、`/api/v1/dsh`、设置页
全部读它，避免“界面说生效、模型却没有工具”这种最难查的分歧。

加载之后模型拿到 14 个 `agent_teams_*`（建队 / 加成员 / 计划审批 / 任务板 / 消息 /
暂停恢复 / 质量闸）与插件自己注入的一段系统提示；`chat.conversation_seed()` 再补一段
`agents.DSH_TEAMS_NOTE`，把“计划要人批、不要自己 approve、一次性探索仍用 `subagent`、
平台 `team_*` 这一轮不存在”说清。

**状态目录必须显式写，否则会堆在项目根。** 插件解析的是
`join(session.cwd ?? process.cwd(), config.stateDir)`，`cwd` 是项目根，默认的
`.agent-teams` 就会长在 `<项目根>\.agent-teams`。绝对路径不行：
`join` 会把它拼成“项目根/绝对路径”。落地方式是在**运行时补丁**里覆盖那一行：

```yaml
- id: agent-teams
  config:
    stateDir: 'data/agent-teams'
    memberProvider: spawn
```

两个键都要写：补丁按行**整块替换** `config`（不合并），只写 `stateDir` 会把
`memberProvider` 抹掉——成员就换了一套提供者，而且不会报错。`dsh_bridge.patch_text()`
在 `conversation_teams().active` 为真时生成这一段；`--dump-config` 里能看到
“patched by …” 那一行说明它真的生效（离线验收 `tests/test_agent_teams.py`）。

### 入口地址必须带 token，而且必须是**这一次**的

应用起来之后会自己打印一行：

```
dsh web: http://127.0.0.1:8790/?token=RTyuyCPXGlc1vrGNC6uPyfB0GnHOl-8b-W6uQUITBuQ
```

裸 socket 实测（同一台机器、同一时刻，只差一个 query）：

```
无 token 的 /     HTTP/1.1 401 Unauthorized     ← 应用自己的 browser-trust fence
带 token 的 /     HTTP/1.1 303 See Other        ← 收下了，redirect
```

所以界面上那一格**必须**用带 token 的那一条。给 `http://host:port/` 的链接，使用者
点开看到的是一个 401 页面，然后会认为这一块坏了。

而这里还有第二层坑：日志是**跨多次启动追加**的，应用要过一会儿才打印新行。只按
"从后往前找第一行带 token 的"去捞，会在那个窗口里捞到**上一次**的 token —— 它已经
失效，点开**还是 401**，而地址看起来完全正确（端口都对）。第一次真机跑就撞上了：

```
openUrl  http://127.0.0.1:8790/?token=JFb8eS79…      ← 上一次的（错的）
日志最后  dsh web: …?token=nbvuMU8eg…                 ← 这一次的
```

修法是记一个**字节下界**：写下本次启动的表头之后取一次文件大小，只从那里往后找。
修完同一个检查的输出两边一致，并且裸 socket 对上了 303。当时这条由 `tests/test_dsh_web.py`
钉着（替身每次打印**不同**的 token，否则这个缺陷在测试里看不出来）——那个模块随宿主一起
删了；同一个"按字节下界往后找"的读法现在只活在 `dsh_runtime.state()` 里，`--turn` 跑一轮
会把它读到的东西打出来。

**token 只跟着管理员走。** 它是进宿主界面的凭据，而 `GET /api/v1/dsh` 任何登录用户
都能调。所以 `state(masked=True)` 会把 `openUrl` 抹掉、连日志里那一行也一起抹 ——
"能看到宿主在跑"与"能进去"是两件事。

**一处需要人知道的边界**：网页版宿主是**另一个进程**，它的 `--patch`（桥）在启动
那一刻就定了。改了桥的档位之后要自己停一次再起；接口会回一个 `webRestartNeeded`
提醒，但**不会替你重启**——那会掐掉你正在网页里进行的一段对话。

**另一处**：它是控制面的子进程，正常退出（`Ctrl+C` / `python -m pentestd serve` 的
finally）会把它一起收掉；但**强杀**（`taskkill /F`、任务管理器结束进程）收不到它，
那个宿主会继续跑、继续占着端口。平台**不会**替你去杀一个"不是本进程起的"进程——
下次点「启动」时它会自动往后找一个空闲端口，并在状态行里说明"端口 N 上已有别的进程
在监听"。要收掉那个旧的，用 `python tools/verify_dsh_channel.py`（只读状态）看还有没有通道
活着，再手工 `taskkill /PID <pid> /T /F`。




测试：`tests/test_dsh_runtime.py`（42 项，含按真机帧写的工具形状与内部事件归类）、
`tests/test_dsh_chat.py`（10 项，替身通道跑完整条事件契约，完全离线）、
`tests/test_dsh_bridge.py`（16 项，桥两端各调一次）、`tests/test_dsh_approval_gate.py`
（审查闸门：哪些调用放行、哪些要人批）。四个模块都离线，不起真的 DSH。

---

## 五之三、扫描执行：一次扫描由谁跑

设置页里有两个相邻、但各说一件事的开关：

* 「**推理引擎**（DSH 运行时）」决定**对话**由谁执行（平台原生循环 / DSH 会话）；
* 「**扫描执行**」决定**扫描任务**由谁执行（发掘引擎 / DSH 会话）。

两者互相独立：可以把对话留在原生循环里、只把扫描交给 DSH，反过来也行。口径是**部署级**
的（和权限档、默认模型同一类），所以写接口只给管理员（`_require_admin`），读接口任何登录
用户可读——任务面板要显示这一轮是谁跑的。

| 口径 | 谁在做 | 证据强度 |
|---|---|---|
| `vulnagent`（默认） | 发掘引擎的七段流水线：切片 → 假设 → 差分验证 | `verified`（差分复现）或 `confirmed_static` |
| `dsh` | 一条**新建的项目内 DSH 会话**，在目标目录里用原生 `pwsh` / 文件检索工具只读审计 | 一律 `confirmed_static`（method 写「DSH 会话只读分析」），**绝不声称差分验证** |

**为什么不是"二选一之后悄悄换"**：控制面在下发作业时就解析口径，并把 `scanEngine` /
`dshHome` / `dshProfile` 一起塞进 payload——执行节点可能不在这台机器上，它不读平台库，
这和 `model` 是同一个理由。DSH 路径不可用（缺 vendor 运行时 / 缺 profile / 缺 node /
目标目录不存在）时任务直接 `failed` 并给出原因，**不静默回退**到 vulnagent：一个打着
DSH 旗号、实际用别的引擎出的结果，是比失败更坏的谎。

**DSH 路径的形状**（`pentestd/scan_dsh.py`）：

1. 先查可用性（`scan_engine.dsh_availability`：`vendor/dsh` 的 bin.js、`data/dsh-home/
   profiles/<profile>`、node 三者都在才算可用），不可用即失败；
2. 起一条**新会话**（`session_for("scan-" + uuid4())`），不复用对话那条：扫描是后台任务，
   不该被前台正在进行的回合打断，反过来也一样；
3. `cwd` 设成目标目录（DSH 的 sandbox `workspaceRoot` 取 `process.cwd()`），
   `prompt` 一段 JSON 契约：只读审计、每条 finding 给 `title/severity/description/
   file/line/cwe/evidence_chain/reproducible_steps`，不要改文件；
4. 收 `iter_turn` 的 delta 与工具事件，`summary.turn.reason == "completed"` 才算数；
5. 从回复里取 JSON（容忍 ```json 围栏与前后废话），逐条规范化后**每一条都标
   `confirmed_static`**、`source = "dsh"`；
6. 用 `engine.build_chain_specs(result)` 生成链路节点——与发掘引擎**同一套节点形状**
   （`asset:root` / `intent:recon` / 每切片 / `intent:verify` / `finding`），界面、台账、
   报告都不用知道这轮是谁跑的；
7. 扫描前后各算一次目标树指纹：途中文件变了就把 `summary.incremental.degraded`
   记一条「审计期间目标目录发生变化」，结论仍只按静态匹配看。

**不写 baseline**：发掘引擎的 baseline 是「这轮读过哪些文件、各自什么哈希」的增量语义，
DSH 路径没有同一份清单；写一个形状相似但语义不同的 baseline，会让下一次增量扫描静默
跳过切片——比不写更危险。所以 DSH 路径的 `baseline` 是空对象 `{}`（任务详情按「没有基线」
处理），代码注释里写明原因。

**权限**：扫描会话的补丁把 sandbox 固定为 `read-only`、审批 `never`——不需要人来点批准，
因为它**不能写**；即使模型手里有 `pwsh`，也只读。发现 DSH 不可用时，设置页把原因直接写在
卡片上；运行期起不来（含未按约定输出 JSON），任务直接 `failed`，失败原因原样落在任务
详情的 `error` 字段，**不静默回退**到发掘引擎。

**测试**：`tests/test_scan_engine.py`（20 项：设置读写 / 两档都收 / 非法值 400 /
非管理员 403 / 下发三个字段 / 设置页与导航契约 / 分流到 DSH 分支 / 失败不改用发掘引擎），
另有既有 `test_engines` / `test_dsh_runtime` / `test_drill` / `test_agent_trace` /
`test_auth_models` 共 160 项一起跑绿。

---

## 六、怎么自己复现一遍

```powershell
# 1) 起通道（隔离在项目目录：DSH_HOME=data/dsh-home，不碰电脑上那份）
python -c "from pentestd import store,dsh_runtime; s=store.Store(r'data\pentest.db'); ..."

# 2) 跑一轮并打印事件（.v2work/e2e_dsh_turn.py 就是这个）
#    期望：turn.runtime=dsh · notice:runtime · delta/reasoning/persist/done

# 3) 接桥并让模型调一次平台工具（.v2work/e2e_bridge_turn.py）
#    期望：原始帧里 tool/call 的 name 形如 mcp__platform__list_engagements

# 4) 通道自检：只读状态 / 刷新运行时补丁 / 真跑一轮（花模型额度）
python tools/verify_dsh_channel.py               # 状态，不起进程、不发请求
python tools/verify_dsh_channel.py --patch       # 只动平台自己那份 home
python tools/verify_dsh_channel.py --turn "在吗"  # 真跑一轮，打印事件

# 5) 融合那一半（历史）：`.scratch/p84/fusion_e2e.py` 验的是 `/dsh/` 反代，那段代码已删，
#    脚本也不再随包发出。要验桥，用上面第 3 步那一轮：工具名形如 mcp__platform__* 就通了。
```

诊断口味的两件小事：`DshRuntime.on_raw` 能把**每一条原始通知**拿走一份（形状取证用）；
`dsh_runtime.state()` 给出"现在是哪条 profile、缺不缺件、几条通道活着"。

---

## 七、还没做的（P2–P4）

- **界面融合（半步改成"DSH 为主"了）**：见下一节——现在**平台的功能长在 DSH 里**
  （`dsh-plugins/dsh-pentest-console`，左侧「平台」面板）。仍未做的：把平台的
  链路图整页搬成一个 slot 组件（现在链路仍在平台自己的工作台里）。
- **嵌套会话的隔离**：`delegate` / `team_run` / `agent_loop` 在 DSH 模式下与主对话共用同一条
  DSH 会话（`session` 里带的 `sessionId` 相同）——它们的产出会进同一条 DSH 上下文。
  修法是为子轮派生一个 id 后缀，但那是 P2 的形状，这里先如实记下。
- **步数上报**：DSH 不报它的循环步数，`done.steps` 因此如实报 0（而不是编一个数）。
- **审查档位下沉**：已接（2026-10-20）—— 见四之1；边界是只盖平台那 20 个工具，
  DSH 原生工具走宿主自己的审批/沙箱。
- **子智能体的 `role`**：真机的 `subagent.started/finished` 只给会话 id，`subagent/catalog`
  只给任务名（`label`）；所以落库行的 `role` 留空，任务名放在 `note`（`任务：…`）。
  要真角色名得让 DSH 把它放进通知（或从子会话 token/请求头里读），还没接。

---

## 八、界面整体搬到 DSH 网页端（本节现行口径）

五之二那一节是"**平台嵌 DSH**"（平台里挂一页 `/dsh/`），六、七那两节是"**平台驱动 DSH**"
（headless / SDK）。**这一节是现行的方向，也是唯一在长的那一条**：

> **DSH 是底座，平台的界面长在它里面。** 不再有"单独的 DSH 页面"，也不把 DSH 嵌进平台。

落地物是一个 DSH 插件包 `dsh-plugins/dsh-pentest-console`。装进某个 profile 之后：

```
DSH 网页端（宿主：会话 · 技能 · 子智能体 · 模型 · 插件 —— 全是原生的，一个都没换）
  ├── 侧栏「平台」 ── 主面板（DSH 自己的 main 槽，按 id 寻址）
  │     概览 · 目标 · 资产 · 漏洞 · 任务 · 报告 · 节点 · 连接
  └── 设置 →「渗透测试平台」── DSH 原生设置页里的一节（settings.section）
                │  Authorization: Bearer <控制台令牌>
                ▼
        平台控制面（另一个进程）：库 · 任务队列 · 探测与审计引擎
          /api/v1/panel · /engagements/{eid}/report · /executors · /setup · /dsh
```

**平台这一刻的角色是"引擎 + 接口"**：库、任务队列、引擎都还在它这边，界面这一侧整体
让给 DSH。

### 两条被否掉的路（**代码已删**，不再是路径）

| 不做 | 为什么 | 现状 |
|---|---|---|
| **单独的 DSH 页面** | 那是第二套界面：一个地址、一个 token、两次登录，而它画的还是 DSH 自己的界面 | `pentestd/dsh_web.py` **已删除**；`--no-dsh-web` / `--dsh-web-host` / `--dsh-web-trusted-host` 三个开关一并删掉（留着"什么都不做"的开关比没有更坏，它会让人以为还有东西可以关） |
| **把 DSH 嵌进平台** | 反方向：DSH 的原生能力（会话、审批、插件、子智能体）会被平台自己那套循环替代掉 | `pentestd/dsh_proxy.py`、`/dsh/` 路由、「会话」页签、`web/dsh.js` 全部删除 |

删这两半的连带后果，一条一条写在这里，因为它们都不是"少一个文件"：

* **`dsh_web_trusted_hosts` 那份清单没有消费者了** —— 它守的是"平台自己拉起的那个宿主
  认哪些 `Host`"，而宿主现在是使用者自己的 DSH，栅栏由 DSH 自己管。`access add-host` /
  `remove-host` 一起删掉：留着它们会"成功"地往一个没人读的设置里写东西。
* **桥的挂载行搬进了插件包**（`dsh-plugins/dsh-pentest-console/cordis.patch.yml`）——
  它原先只在"平台自己拉起 DSH"时靠 `--patch` 生效，那条路一删，挂载行就没有载体了。
  现在装插件＝同时装上界面那一格与工具那一半，卸载即全摘。
* **`PORT_PATTERN` 搬回 `server.py`** —— 它原本借的是 `dsh_web` 的常量（跨源来源与
  受信地址共用一份"端口长什么样"）。删掉一半之后，剩下的那一半自己持有它。

### 用到哪些宿主能力（都是原生服务，不是自己搭的）

| 宿主能力 | 用在哪 | 不用它的话 |
|---|---|---|
| `ctx.slots`（`main` / `sidebar.panellist` / `settings.section`） | 主面板、侧栏那一格、设置页那一节 | 自己画一个路由器 → 与 DSH 的选中态、键盘导航、折叠态全都对不上 |
| `ctx.locale` | 中英两套文案跟着界面语言切 | 自己判断语言 → 界面切了英文，面板还是中文 |
| `ctx.effect` | 语言/样式/槽位注册都挂在插件生命周期上 | 卸载后残留样式与监听 |
| `ctx.get('timer')` | 轮询（拿不到才退回 `window.setTimeout`） | 定时器与卸载脱钩，面板没了请求还在 |
| 主题令牌 `--dsw-*` | 全部配色，**零硬编码颜色** | 黑夜模式下某一页刺眼 |

刻意**不** import 宿主的 UI 组件包：那会把面板与 DSH 内部组件版本绑死，上游一次重构
就能让它在**加载期**崩掉——那时连"我坏了"都显示不出来。

### 装（两种目标）

```powershell
# 平台自带的那一份 DSH（与电脑上的完全隔离；验证与试用用这个）
python tools/install_console_plugin.py

# 你正在用的那一份 DSH：装完刷新页面，左侧栏就多一格
python tools/install_console_plugin.py --home "$env:USERPROFILE\.dsh" --profile desktop
```

脚本会先把目标的 profile `package.json` 备份一份（`package.json.bak-<时间戳>`），结尾
打印撤销命令。**它动的是另一份 DSH 的数据目录**，所以这一步要写清楚：`--home` 指到
哪里就装到哪里，`DSH_HOME` 由脚本显式设定并剥掉继承来的 `DSH_PROFILE*` /
`DSH_SESSION_ID`（与 `dsh_runtime.LEAKED_ENV` 同一份清单）。

配两样东西：控制台令牌（`python -m pentestd console-token`）与跨源来源
（`python -m pentestd access add-origin http://127.0.0.1:19387`，回环内置放行）。

### 这一版为它新增的平台能力

| 新增 | 为什么需要它 |
|---|---|
| `GET/POST /api/v1/console-token` + `auth.ensure/rotate/verify_console_token` | DSH 网页端跑在**另一个源**上，拿不到平台的登录 Cookie（`SameSite=Lax` 拦掉跨源携带），而"为了嵌一块面板把 Cookie 放宽成 `SameSite=None`"等于给所有站点开一条带凭据的通道。令牌走 `Authorization` 头，浏览器不会自动附加它——跨源也就没有 CSRF 可言 |
| **来源清单 + `OPTIONS` 预检**（`PENTESTD_CORS_ORIGINS` / `pentestd access add-origin`） | 跨源读必须由服务端允许。清单之外的来源**不发** `Access-Control-Allow-Origin`，也**不发** `Allow-Credentials` |
| `GET /api/v1/panel` | 一次请求换四块内容，**同一次查询里派生**——不会出现"概览写 18 个资产、资产页签列 17 条" |
| `pentestd console-token` / `access` 两条 CLI | 第一次配置总得有地方拿到令牌、放行来源 |

`_require_session` 里的顺序是刻意的：**Cookie 优先，令牌兜底**——一个人已经登录时，
他的身份应当来自那条会话（退出登录也就真的退出），而不是被一个留在头里的令牌顶回去。

### 界面（八页，都是真数据）

| 页 | 读什么 | 交互 |
|---|---|---|
| 概览 | 当前目标 + 8 个计数 + 最近作业 | 点一条作业 → 跳到它的报告 |
| 目标 | 全部目标（换目标会改变其它页的数据范围） | 点一个目标 = 选中/取消 |
| 资产 | 目标下的资产**树**（按 `parentId`/`children` 还原层级） | 文本筛选（父被筛掉但子命中时保留父） |
| 漏洞 | 按目标成篇的漏洞 | 严重度筛选（档位措辞由平台贴）+ 文本筛选 + 展开看描述与证据 |
| 作业 | 目标下的作业表（类型/目标/状态/耗时/摘要） | 状态筛选 + 文本筛选 + 跳到该次报告 |
| 报告 | 目标报告，或**某一次执行**的报告 | 切换目标报告/作业报告；正文按 markdown 渲染 |
| 节点 | 执行节点表 + 控制面信息 + DSH 集成状态（桥档位、profile、网页宿主） | 刷新 |
| 连接 | 平台地址与令牌、当前通不通 | 保存/清除（与 DSH 设置页那一节是同一份配置） |

### 边界（都是刻意的）

- **只读**。不建目标、不下发作业、不改配置：那些动作要过权限档与作用域两道闸门，而闸门
  住在平台的会话里；在这里另开一条写路径等于绕过它们。**动作仍然走 DSH 的原生对话**——
  平台的工具已经通过 MCP 桥接进 DSH（`mcp__platform__*`）。
- **报告只走 React 文本节点**：正文里有第三方可控的字符串（目标地址、端口横幅、证据
  原文），所以没有任何一处拼 HTML（`test_console_plugin` 里有一条反向断言钉住）。
- **5 秒轮询**（链式，不堆积请求），卸载时清干净；**每类前 40 条**，总数在页签上。
- **资产类型印平台给的词**（「服务」而不是 `service`）：这一页原先直接印库里的 `type`，
  理由是「平台自己没有资产类型对照表」。那句话在网页侧早就不成立——`web/app.js` 一直
  自带着一张同样的表，于是同一份资产在资产树上写「服务」、在这一页写 `service`；
  链路图导出的 SVG 上又是第三种写法（`service · 8080`）。现在词表只有一份，
  在后端 `pentestd/models.py` 的 `ASSET_TYPE_LABELS`，由 `store.asset_tree()` 贴成
  `typeLabel`；插件读 `typeLabel || type`，本地不再维护第二张表。
- **宿主半是空的**：平台的能力已经有两条出口（工具走 MCP 桥、界面走插件），宿主半再
  注册一遍只会让模型看到两套名字指向同一批动作。

### 实测（真机，2026-10-16）

临时数据目录 + 真控制面 + 真 DSH 宿主 + 真 Edge，八个页签逐个点开、逐张截图，
数据都是**造出来的真结构**（两个目标：一个 3 资产 / 3 漏洞 / 3 任务，一个 1 资产 /
1 漏洞，用来验"换目标真的换了数据"）：

```
侧栏那一格     <button aria-label="平台">
页签           概览 · 目标 2 · 资产 3 · 漏洞 3 · 作业 3 · 报告 · 节点 · 连接
概览           当前目标 10.10.10.250 · 3 资产 · 3 漏洞 · 1 高危及以上 · 2 目标 ·
               1 在线节点 · 1/0 待跑/在跑 · 最近三条作业（已完成/失败/待跑各一）
资产           树：IP 10.10.10.250 → 服务 :80 nginx/1.24.0 / :6379 redis/7.0 未授权
               （类型印平台贴的 typeLabel，中文；词表在 models.ASSET_TYPE_LABELS）
漏洞           严重 Redis 未授权访问 · 中危 nginx 版本信息外泄 · 低危 SSH 允许口令登录
任务           probe · 10.10.10.250 · 已完成 · 摘要「开放 22/80/6379，Redis 无认证」
               audit · 失败「目标在扫描中途不可达」· probe · 待跑
报告           目标报告 markdown 渲染（标题 / 表格 / 列表 / 证据原文）
节点           执行节点表（online）· 控制面（监听/库/schema/探测范围/模型/鉴权）·
               DSH 集成（桥档位、profile、网页宿主 phase）
换目标后       资产页只剩 10.10.10.251 那一条（左侧卡片点击 → 数据范围真的变了）
DSH 设置页     多一节「渗透测试平台」（与 DSH 自己的设置并列）
跨源取数       从**这个页面**直接打平台接口：200 + 真 payload
宿主自己的报错  无（console 0 条、page-error 0 条）
```

复现：`python -u .scratch/p91/e2e.py`（起来后打印 `READY {...}`），再用
`.scratch/p91/probe.js` 走 CDP 逐个页签截图。

### 对外访问（局域网 / 公网）

**平台只需要开一个端口**（8787）。DSH 由使用者自己跑 —— 它监听在哪、要不要对公网，
是那个 DSH 的事，平台既不拉起它也不知道它的端口。

| 门 | 是什么 | 怎么开 |
|---|---|---|
| 平台的监听 | 控制面自己 | `--host 0.0.0.0`（或部署脚本的 `--host`） |
| 跨源读 | 插件从**DSH 那个源**读平台接口 | `python -m pentestd access add-origin http://<对外地址>:<端口>`（**立即生效**） |
| 防火墙 | 8787 入站 | 一条放行规则（放行 ≠ 监听放开：进程绑回环时放行再多也进不来） |

那条跨源来源要逐字写对：`Origin` 头里没有路径，所以带路径的条目永远不会命中，而
写入时就会被拒（`normalize_cors_origin`）。收下死项的代价不是报错，而是"清单里有、
就是不生效"，使用者只能从"面板还是连不上"倒推。

> **没有"受信地址"这一栏了。** 需要声明 `Host` 的是 DSH 自己那道 browser-trust 栅栏，
> 用 DSH 的 `--trusted-host` 去配 —— 那是它的参数，不是平台的。

完整的命令、判据与踩过的坑在 [deploy/expose.md](deploy/expose.md)。
