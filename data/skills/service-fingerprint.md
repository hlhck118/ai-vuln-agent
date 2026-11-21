---
name: service-fingerprint
description: 服务与版本识别：内置探测与 nmap 各自负责什么，-sV 的输出怎么读回来。
whenToUse: 已知端口开放，需要知道端口后面是什么服务、什么版本时。
role: recon
enabled: true
---

# 服务与版本识别

## 什么时候该用它

`start_probe` 已经给了你一份开放端口清单，但你要的是"这个端口后面到底是什么"。这时有
两条路，选哪条取决于你要的精度：

- 内置探测**已经在做**一轮识别（HTTP 状态行与标题、TLS 版本与证书、Redis `PING`、
  Docker `/version`、ES 根接口）。要的只是"大概是什么"，直接用 `get_job` 读结果就够。
- 要**版本号、更全的服务指纹库、NSE 脚本**，那要 nmap。内置探测不做这个——它的目标
  是"够判未授权与 TLS 配置"，不是"精确到补丁版本"。

## 用 nmap 补版本识别

```
list_toolkit
run_tool adapter=nmap target=10.0.0.5 toolArgs=["-sT","-Pn","-sV","-p","22,80,443,445","10.0.0.5"]
run_tool adapter=nmap target=10.0.0.5 toolArgs=["-sT","-Pn","--top-ports","100","10.0.0.5"]
```

两个必须知道的约束，都是这个部署上实测的：

**一、`-sS`（SYN 扫描）在这台机器上用不了。** 本机没有 Npcap，nmap 启动时会打一行
`Resorting to connect() mode`，`-sS` 会直接退出。用 `-sT`（连接扫描）——适配器声明的
参数表里也只有 `-sT`。这一行警告**一定会出现**，它是本机事实，不是配置错误。

**二、缺 Npcap 时，需要原始套接字的 NSE 脚本一行都不会输出。** `smb-enum-shares`、
`smb-enum-users`、`smb-os-discovery` 这类脚本属于这一类：结果里端口照常列出，脚本那一节
**是空的**，而"空的脚本节"与"脚本跑了、没发现问题"读起来一模一样。平台把这一行识别成
降级项写进任务的 `degraded`：

```
--script smb-enum-shares   →   没有 Npcap，nmap 已退回 connect() 模式……
                                （degraded 里会写明；输出为空 ≠ 没有共享）
```

**看到 `degraded` 就必须把它转述给使用者**，并且不要把空结果说成"没有发现共享"。
要消除它：把 nmap 装机目录里的 `npcap-*.exe` 装上（需要管理员）。本平台只解压安装包、
**从不运行**它——那一步是人的决定。

## 参数怎么写

`run_tool` 的参数走 argv 列表，逐字符校验，分两档：

- **严格档**（默认）：目标串、路径、端口、开关本身。白名单字符集
  `A-Za-z0-9_.:/@[]-+\=?`。
- **数据档**：适配器表里声明为数据值的开关——nmap 的 `-p`、`--script`、`--script-args`。
  逗号列表 `-p 80,443` 与 `--script http-title,ssl-cert` 都在这一档，放行。

**`& | ^ < >` 与反引号、`$` 在任何档都不放行。** 理由是 Windows 上很多工具是 `.cmd` 包装，
参数最终会进 `cmd.exe` 被重新解析一遍——即使用列表参数、`shell=False` 也一样。需要多个
查询参数时改用该工具的原生写法；被拒时看错误信息里那句"若这是数据……"，它指出该去哪儿
声明。**引号则放行**（见 `web-surface` 第一步）：内联 JSON 请求体可以直接传，
只有 `.cmd`/`.bat` 包装上才整档回落。

`target` 是**必须显式给的**：平台不替你从参数里猜目标，猜错会让授权判在一台没被打的机器上。

## 读回来

`run_tool` 产出的是**原始输出，不是结论**。链路里那条事实只记着"退出码 0，输出 1235 字节"，
真正的输出在任务里：

```
list_jobs status=done limit=20
get_job jobId=job-xxxxxxxx              # 不带 offset：第一页
get_job jobId=job-xxxxxxxx offset=6000  # 带 nextOffset 继续读
```

`get_job` 按**字符**分页，默认 6000、上限 24000。**读一页就下结论是这套工具最常见的错**：
nmap 的 `-sV` 输出里，端口清单在前、脚本节与版本号在后，而截断点正好落在中间。所以先读
一页，看 `hasMore`；为 true 就带 `nextOffset` 接着读，直到 false 才算读全。

工具输出还会被**打码**（凭据、hash、ticket），并且被截断到 256 KB。被截断时 `degraded`
里会有"输出已截断"那一句——连同 `truncated: true` 一起看。

## 代价与边界

- `run_tool` 走的是**外部工具**那一类授权（`toolkit`）；跑 sqlmap 要 `exploit` 类、跑
  ssh 要 `pivot` 类——授权类别由**适配器**决定，不由"它只是个外部程序"决定。
- `run_tool` 属于 full 档，并且**关键的是**：本部署 `authz.FORCE_OPEN=True`，五类侵入
  动作的准入判定不参与判定，所以不要告诉用户"需要先配 `--allow-intrusive`"。
- 一次 nmap 跑完**不代表"这些端口后面有什么"**——那是另一次任务（探测或利用验证）该证明
  的事。不要拿 nmap 的输出直接当结论。
- 超时 600 秒上限、输出 256 KB 上限，都是这一类动作的既定配额。
- 表外的程序拉不起来：`ssh -o ProxyCommand=…` 这类会执行另一个进程的选项会被拒。这不是
  参数写错，是适配器表决定"哪些二进制会被执行"的**结构前提**。
