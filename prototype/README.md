# 基于 AI 智能体的自动化漏洞发掘系统（原型）

本目录是《基于AI智能体的自动化漏洞发掘技术创新研究》方案书的**可运行原型**，
实现了方案中承诺的核心闭环：

```
代码接入 → 语义图谱 → 规划 → 假设生成 → 反证质询 → 沙箱差分验证 → 利用链 → 报告/合规
```

**零第三方依赖**：核心链路只用 Python 标准库，离线环境可直接运行。

## 1. 快速开始

```bash
cd prototype                              # 从工作台根目录进入；已在此目录则跳过

python -m vulnagent doctor                 # 环境自检（Python/沙箱/容器运行时/外部工具）
python run_demo.py                         # 端到端演示：三个目标 + 增量扫描场景
python -m unittest discover -s tests -v    # 58 项自动化测试

# 单目标扫描
python -m vulnagent scan --target demo/websvc --patched demo/websvc_patched --out out

# 增量扫描（持续集成场景）
python -m vulnagent scan --target <repo> --incremental --baseline out/baseline.json

# 把扫描结果接入 DSH 渗透模式（生成探索链路回放序列）
python -m vulnagent export-pentest --run-dir out/websvc --authorization "授权说明"
```

## 2. 演示结果（本机实测）

### 目标一 `demo/vulnapp`：单文件，六类典型缺陷

```
规模：1 个文件 / 92 行；函数与单元 7 个；外部入口 1 个
结果：切片 6 个；假设 6 条；确认 6 条

  [中危] CWE-327  verified          userservice.py:66  弱哈希（彩虹表可直接还原）
  [高危] CWE-78   verified          userservice.py:46  命令注入
  [高危] CWE-798  confirmed_static  userservice.py:24  硬编码凭据
  [高危] CWE-89   verified          userservice.py:40  SQL 注入
  [高危] CWE-22   verified          userservice.py:54  路径穿越
  [严重] CWE-502  verified          userservice.py:61  不安全反序列化

  攻击链：凭据泄露叠加远程代码执行，形成完整失陷链
```

### 目标二 `demo/webapp`：三层跨文件调用链

```
controller.handle → service.load_profile → dao.find_user → cur.execute

规模：3 个文件 / 97 行；函数与单元 9 个
解析：调用点 20，内部 10，外部 1，无法静态确定 5，解析率 50%
结果：切片 1 个；确认 1 条 → [高危] CWE-89  verified  dao.py:24
```

该目标专门验证调用图解析：缺陷在 `dao.py`、入口在 `controller.py`，中间隔一层
`service.py`，必须跨三个文件建立调用边才能判定"缺陷从外部入口可达"。

### 目标三 `demo/websvc`：四类需要动态验证的缺陷

```
规模：1 个文件 / 59 行；函数与单元 5 个
结果：切片 4 个；确认 4 条，全部通过差分验证

  [中危] CWE-79   verified  portal.py:19  反射型 XSS
  [中危] CWE-327  verified  portal.py:29  弱哈希
  [高危] CWE-95   verified  portal.py:24  代码注入（eval）
  [高危] CWE-918  verified  portal.py:34  服务端请求伪造
```

### 增量扫描（CI 场景）

```
增量扫描演示（先安全、后回归缺陷）
  ① 首次扫描（建基线）   变更 3 文件 → 审计 0 个切片，确认 0 条
  ② 重新引入缺陷后       变更 1 文件 → 审计 1 个切片，确认 1 条
     [CWE-89] dao.py:24
  ③ 无变更再跑一次       变更 0 文件 → 审计 0 个切片（跳过 1）
```

- 三个目标的注入缺陷**全部检出**；可动态验证的 9 条全部通过**沙箱差分验证**。
- 对**修复版**单独扫描的结果是 **0 条交付项**——验证了误报控制能力。
- 58 项自动化测试全部通过。

## 3. 目录结构

```
prototype/
├── vulnagent/
│   ├── domain.py        领域模型（假设、证据、验证、交付项、攻击链）
│   ├── config.py        配置加载（TOML/JSON，默认离线保守配置）
│   ├── resolver.py      符号解析与调用图（作用域、导入、轻量类型推断）
│   ├── analysis.py      规则引擎与切片构造（Python AST + 多语言正则）
│   ├── incremental.py   增量扫描（指纹清单、变更比对、影响面分析）
│   ├── llm.py           模型接入、推理器与降级路由（heuristic / openai_compat）
│   ├── tools.py         工具白名单、沙箱执行、8 类 PoC 模板、差分验证
│   ├── agents.py        七个智能体角色 + 共享黑板
│   ├── orchestrator.py  编排引擎、预算控制、状态机、增量调度
│   ├── memory.py        经验记忆库（SQLite）
│   ├── report.py        Markdown 报告 + 合规报送要素
│   ├── integrations/
│   │   └── dsh_pentest.py  DSH 渗透模式集成：探索链路回放序列
│   └── cli.py           命令行入口（scan / memory / export-pentest / doctor）
├── demo/vulnapp/          单文件目标（六类缺陷）
├── demo/webapp/           多文件目标（三层调用链）
├── demo/websvc/           Web 服务片段（XSS/代码注入/弱哈希/SSRF）
├── demo/*_patched/        各目标对应的修复版
├── tests/                 58 项自动化测试
│   ├── test_pipeline.py    端到端流水线（编排、差分验证、报告）
│   └── test_agent_roles.py 七个智能体的角色契约（路由各留记录、时间轴、无链留痕）
├── config.example.toml    配置示例
└── run_demo.py            端到端演示脚本
```

## 4. 调用图符号解析

早期版本用"调用写法最后一段"匹配，`a.save()` 与 `b.save()` 会混为一谈，且
**跨文件调用边无法建立**。现在改为带作用域与轻量类型推断的解析器：

| 能力 | 说明 |
|---|---|
| 限定名 | 统一使用 `文件路径::限定名`，如 `dao.py::UserDao.save` |
| 作用域与继承 | `self.method()` 解析到当前类方法，找不到时回退基类 |
| 导入解析 | `import a.b`、`import a.b as c`、`from a import b as c` 均解析到确切符号 |
| 类型推断 | `x = UserDao()`、`self.dao = UserDao()`、参数注解 `dao: UserDao` |
| 跨文件 | 目标模块在被分析集合内时建立真实调用边 |
| 未解析留痕 | 动态调用、反射、回调一律记为 `unknown`，**不按名字硬猜** |

可达性结果直接进入反证环节：未确认可达者置信度下调 0.2；存在无法静态确定的
调用时再下调 0.05 并记录原因。

## 5. 增量扫描

```bash
python -m vulnagent scan --target <repo> --incremental [--baseline <path>] [--git-ref HEAD~1]
```

- **省的是推理与验证成本，不是解析成本。** 调用图必须全量构建，否则无法判断
  未变更的函数是否被变更的入口影响；图谱是确定性计算，成本远低于模型与沙箱。
- **影响面双向传播**：变更函数的调用者（决定可达性）与被调用者（决定危险汇聚面）
  都纳入，默认传播 2 层。
- **两种变更来源**：指纹清单比对（默认，任意环境可用）或 `git diff`（Git 仓库）。
- **删除也算变更**：被删文件的符号会从图中消失，可能让原本可达的路径变为不可达。
- **无变更直接跳过**，有变更但影响面为空时保守回退全量，避免静默漏审。
- 基线默认写在**输出目录**（`out/vulnagent-baseline.json`），不污染被扫描仓库。

## 6. 验证用例（PoC）模板

系统按缺陷类型自动生成验证用例，统一以 `路径::符号` 定位目标函数，并把观测结果
以固定前缀的结构化 JSON 输出。当前覆盖 8 类：

| 缺陷类型 | 载荷与观测点 | 判定条件 |
|---|---|---|
| CWE-89 SQL 注入 | 注入恒真条件，统计返回记录数 | 返回记录数多于正常调用 |
| CWE-78 命令注入 | 拼接平台适配的分隔符与第二条命令 | 输出中出现独立标记行 |
| CWE-22 路径穿越 | 受控目录外放置标记文件并尝试读取 | 读到的内容包含标记 |
| CWE-502 反序列化 | 构造带副作用的序列化载荷 | 副作用标记文件存在 |
| CWE-327 弱哈希 | 计算常见口令摘要并与已知值比对 | 与已知 MD5 一致（可被彩虹表直接还原） |
| CWE-79 反射型 XSS | 提交脚本标签并观察输出 | 载荷未经转义原样出现 |
| CWE-95 代码注入 | 提交写文件的表达式并观察副作用 | 副作用文件生成 |
| CWE-918 SSRF | 传入 `file://` URI 尝试读取本地文件 | 读到受控文件内容 |

**模板的已知假设**：目标函数的第一个参数即外部输入，且返回值可观测（返回值或
副作用）。当签名不匹配时，用例会执行失败——此时系统判为"待人工确认"而**不是**
"误报"（见下一节）。

## 7. 两条安全规则：不确定 ≠ 不存在

自动化漏洞发掘最危险的错误不是漏报，而是**把漏报伪装成"已排除"**。系统为此
设置了两条硬规则，并有专门的回归测试：

1. **沙箱拿不到执行结果**（异常/超时/无结构化输出）→ 判"待人工确认"，不判误报。
2. **缺陷版上用例执行失败**（调用签名不匹配、目标函数抛错）→ 判"待人工确认"。
   只有用例**正常执行完成**且判定条件明确不成立，才允许判为误报。

## 8. 容器化沙箱

```toml
[sandbox]
backend = "docker"
docker_image = "python:3.12-slim"
allow_fallback = false    # 生产必须为 false
```

请求容器后端时生成的命令包含完整隔离参数：

```
docker run --rm --network none --memory 512m --memory-swap 512m --cpus 1 \
  --pids-limit 128 --security-opt no-new-privileges --read-only \
  --tmpfs /tmp:rw,size=64m,noexec -v <workdir>:/w:rw -w /w \
  python:3.12-slim python -I /w/poc_case.py /w/target
```

**不静默降级**：容器运行时不可用时记录降级事件并标明"进程级隔离"；
`allow_fallback = false` 时任务直接失败，绝不把不可信用例放进宿主进程。

> **本机状态**：开发机未安装 Docker（仅有 WSL），因此容器后端的隔离参数通过
> 单元测试验证，尚未在真实容器中执行。报告中沙箱等级显示为"进程级"。

## 9. 接入私有化大模型

默认 `provider = "heuristic"`（纯离线确定性推理，无模型也能跑通全流程）。

```toml
[model]
provider = "openai_compat"
base_url = "http://127.0.0.1:8000/v1"   # vLLM / Ollama / TGI / 一体机
model = "deepseek-r1-distill"
api_key_env = "VULNAGENT_API_KEY"
```

模型不可用、超时或返回非法 JSON 时自动降级为确定性推理，并在报告"降级说明"中
如实记录。云端模型默认关闭，需同时满足 `allow_cloud = true` 且显式配置
`cloud_model`：**《网络产品安全漏洞管理规定》第九条禁止将未公开漏洞信息提供给
境外组织或个人**，扫描未公开漏洞时必须保持云端通道关闭。

## 10. 安全边界（原型 vs 生产）

| 维度 | 原型实现 | 生产要求 |
|---|---|---|
| 沙箱隔离 | 进程级；容器后端已实现未实测 | 强制容器/gVisor，`allow_fallback = false` |
| 网络出口 | 默认不外发；云端开关默认关闭 | 出网白名单 + 流量审计 |
| 工具调用 | 白名单注册，未注册即拒绝并记审计 | 同策略 + 双人复核高危工具 |
| 权限 | 单机单用户 | 最小权限账号 + 操作审计入库 |
| 数据 | 结果落本地磁盘 | 加密存储 + 分级管控 + 不允许出域 |

## 11. 与 DSH 渗透模式（`@howmp/dsh-pentest`）集成

把扫描结果转成 DSH 渗透页里的**探索链路**（目标 → 意图 → 事实 → 漏洞 → 资产）：

```bash
python -m vulnagent export-pentest --run-dir out/vulnapp \
    --authorization "内部授权测试：授权对象=本单位自有系统，书面许可=SEC-2026-0918"
```

产物 `pentest_replay.json` 是一份**按依赖顺序排列的 `pentest_*` 工具调用序列**，
在 DSH 的「渗透模式」会话里按序执行即可复现整张图。

设计要点：

| 决策 | 原因 |
|---|---|
| 导出回放序列，**不直接写插件的 SQLite** | 节点 id 由插件分配、图由插件维护、存储域带版本校验；直写等于依赖其内部实现 |
| 用 `{"$ref":"name"}` 占位而非写死 id | id 不可预知；回放方用上一步返回的 id 替换 |
| 映射到插件既有词汇（spawns/yields/derived_from/proves） | 不自造关系，图才能被插件正确渲染 |
| 只登记一个根资产（`app`） | 插件资产词汇里没有"代码文件"，不为凑图硬塞 |
| 契约校验 + 对插件源码的工具名核对 | 插件升级导致签名变化时测试立即失败，而非静默失效 |

集成细节、语义映射表与边界见 [docs/integration-dsh-pentest.md](../docs/integration-dsh-pentest.md)；
平台该做 C/S 还是 B/S 的架构决策见 [docs/adr/0001](../docs/adr/0001-pentest-architecture-cs-vs-bs.md)。

两份文档都在**交付包根目录**的 `docs/` 下，所以从本目录要上一层（`../`）——引擎位于工作台根
之下的 `prototype/`，与 `docs/` 是同一层的兄弟目录；只把工作台单独拷到目标机时没有这两份
文件，它们是参赛交付物的一部分，不是运行依赖。

## 12. 已知局限

- 规则引擎以 Python AST 为主，其他语言为语法级正则规则（置信度低、不参与动态验证）；
- 类型推断只覆盖"变量 = 类名(...)"与参数注解，工厂函数、依赖注入、反射构造的
  接收者类型记为 `unknown`（解析率因此不高，但这是刻意的保守取舍）；
- PoC 模板假设目标函数首参即外部输入、返回值可观测，签名不匹配时只能转人工；
- 差分验证依赖提供"修复版"基线，无基线时只能给出"待人工确认"；
- 增量扫描仍需全量构建调用图（省的是推理与验证成本）；
- 回放序列需要人工或在 DSH 会话中执行，原型**不会**自动调用插件工具；
- `heuristic` 推理器不具备大模型的开放语义推理能力，仅用于保证流程可跑通。
