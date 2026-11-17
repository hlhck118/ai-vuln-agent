---
name: web-surface
description: Web 面清点：入口、参数、认证方式怎么列，哪些交给内置、哪些交给 sqlmap/curl。
whenToUse: 目标是 Web 应用，要先把入口与参数清点清楚再谈漏洞时。
role: vuln-scan
enabled: true
---

# Web 面清点

## 什么时候该用它

目标是 `http://…`，或者探测发现了一个 web 端口。这时**不要**直接上 sqlmap——先在纸面上
说清"有哪些入口、每个入口吃哪些参数、认证怎么做的"。清单本身是结论的一部分，而且它是
后面每一次利用验证的输入：不知道路径与参数名，`start_exploit` 只能跑根路径，那等于没跑。

## 第一步：入口清点

内置探测已经把最小的一份给了你：HTTP 状态行、`Server`/`X-Powered-By` 一类头、页面标题。
读它：

```
get_chain_nodes kind=fact limit=40
get_chain_nodes kind=asset limit=40
get_assets limit=100
```

对同一目标再补一次手工收口（`-w` 是数据档，`%{http_code}` 里的 `{}` 与 `%` 都放行）：

```
run_tool adapter=curl target=http://10.0.0.5 toolArgs=["-s","-o","NUL","-w","%{http_code}\\n","http://10.0.0.5/admin"]
run_tool adapter=curl target=http://10.0.0.5 toolArgs=["-i","-X","OPTIONS","http://10.0.0.5/"]
```

第一行里的 `NUL` 是 Windows 上的空设备；`-o` 的值是**路径**，进严格档字符集（`/dev/null`
虽然能过字符集，但它在 Windows 上不是一个存在的路径——手册里的命令是照 POSIX 写的，
搬进 `run_tool` 时要把这一层一起改）。想看响应体就去掉 `-o`，原样读回输出。

`-i` 要看的是 `Allow` 头：`OPTIONS` 常把这一路径允许的方法直接列出来，而 `PUT`/`DELETE`
出现在那里是一个可以当场记下来的事实。

## 第二步：哪些交给内置，哪些交给外部工具

| 要回答的问题 | 归谁 |
|---|---|
| 某路径是否存在、返回什么状态码 | 内置（探测的 HTTP 指纹）或 `curl` 复核 |
| 认证绕过（头、路径、方法覆盖） | **内置** `start_exploit` |
| SSRF / Host 头带外回连 | **内置** `start_exploit`（需带外回连配置） |
| 命令注入回显标记、上传后取值 | **内置** `deliver_payload` |
| 强制浏览（有限清单） | **内置** `start_exploit` 的 `forced-browsing` |
| SQL 注入检测与利用 | **sqlmap**（`run_tool adapter=sqlmap`） |
| 无边界目录爆破 | **没有**。内置强制浏览用一份很小的内置清单，不猜路径 |

分界的理由：内置那几类都配了**差分对照**（不带载荷的基线请求），所以它们能给"成立/不成立"；
sqlmap 的注入面广得多，但它的结论要在报告里单独标注来源，不能和内置判定混成一类。

## 第三步：引号与特殊字符怎么过

`run_tool` 的参数逐字符校验，**`& | ^ < > "` 在任何档都不放行**——Windows 上很多工具是
`.cmd` 包装，参数会被 `cmd.exe` 重新解析一遍。三条改写规则，遇到被拒就按这三条改，
**不要放弃那条命令**：

1. **带引号的内联请求体 → 改用文件写法。** curl 是 `-d @payload.json`，sqlmap 是
   `-r 请求文件`。禁 `"` 不是为了防这条参数本身，而是让"数据档放行的字符"与"为什么挡
   `& | ^ < >`"用同一条理由，不开口子。
2. **`-w` 格式串里要换行 → 写两字符的 `\n`。** 上面 JSON 里的 `"\\n"` 就是那两个字符
   （反斜杠 + n），curl 认这个写法（本机 8.21.0 实测）；真的换行会把一条命令在链路节点里
   读成两条，所以字符集直接禁掉它。
3. **需要多个查询参数 → 用该工具的原生写法。** `&` 是 cmd 的命令分隔符，任何档都不放行；
   curl 可以直接把完整 URL 写进参数值（`?` 在白名单里，正是为了这个）。

```
run_tool adapter=sqlmap target=http://10.0.0.5 toolArgs=["-u","http://10.0.0.5/item?id=1","--batch","--level","2","--risk","1"]
run_tool adapter=curl target=http://10.0.0.5 toolArgs=["-s","-o","NUL","-w","%{http_code}\\n","http://10.0.0.5/login"]
```

注意 `?` 与 `=` 都在严格档白名单里（`?` 对 `cmd.exe` 完全惰性，cmd 不做通配展开），所以
带查询串的 URL 是能直接传的。`-A` / `--user-agent` 也在数据档里——伪造 UA 是这类复核最
常见的开头，它是允许的。

## 读回来

`run_tool` 产出的是**原始输出，不是结论**。sqlmap 的输出很长，而且结论在末尾：

```
get_job jobId=job-xxxxxxxx
get_job jobId=job-xxxxxxxx offset=6000     # hasMore 为 true 就用 nextOffset 继续
```

按**字符**分页，默认 6000、上限 24000。**只读第一页会把"还没读到"当成"只有这些"**——
sqlmap 的"parameter X appears to be injectable"通常在几万字符之后。翻到 `hasMore` 为 false。

输出会被打码并截断到 256 KB；被截断时任务 summary 里 `degraded` 会写"输出已截断"。看到它
就要说明"这份输出不完整"，而不是把前 256 KB 当全部。

## 代价与边界

- `run_tool adapter=sqlmap` 的授权类别是 **exploit**（由适配器决定），不是 toolkit。
- 本部署 `authz.FORCE_OPEN=True`，五类侵入动作的准入判定不参与判定——**不要**让用户去配
  `--allow-intrusive`。
- sqlmap 的 `--risk 3`、`--level 5` 这类参数会真的往目标写/改东西。**验证不是破坏**：先
  `--level 2 --risk 1` 拿到判定，要不要升级是使用者的决定，不要自己替他决定。
- 探测作用域与权限档仍然生效：越界的 URL 在 `create_job` 就被拒了，报成"目标不可达"是错的。
