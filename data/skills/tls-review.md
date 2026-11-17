---
name: tls-review
description: TLS 判定：内置判定的是什么、证书链与名称细节要什么工具、没那个工具时结论的边界。
whenToUse: 目标有 443/8443 或其它 TLS 端口，要判证书与协议版本合规性时。
role: recon
enabled: true
---

# TLS 判定

## 什么时候该用它

探测结果里某个端口带了 `tls` 一节，或者用户直接问"这个站点的证书有没有问题"。先分清
你要回答的是哪一层：

| 问题 | 谁能回答 | 说明 |
|---|---|---|
| 握手成不成功 | 内置探测 | `tls.handshake` |
| 证书能不能过系统 CA 校验 | 内置探测 | `tls.certVerified` |
| 是否接受已废弃协议版本 | 内置探测 | `tls.legacyAccepted` |
| 证书链完不完整、名称匹不匹配、SAN 有哪些 | **要 openssl** | 内置不做这层 |
| 用了什么密码套件、是否支持特定扩展 | **要 openssl** | 内置不做这层 |

内置判定只回答前三个。这不是能力不足的托词，是刻意的分界：**"能不能过校验"是一个可当场
判定的是/否**，而"链为什么断"需要把整条链摊开看——那是另一个工具的工作。

## 内置判定怎么跑出来的

`start_probe` 对 443、8443、2376 这几个端口直接走 TLS 握手，对未知端口先试 HTTP 再试 TLS。
判定结果落在端口条目的 `tls` 字段里：

- `handshake: true/false` —— 握手成没成；
- `certVerified: false` + `verifyError` —— 证书没过系统 CA 校验，`verifyError` 是原因；
- `certSha256` —— 证书指纹，用于跨任务比对"是不是同一张证书"；
- `legacyAccepted: true` —— 服务端仍接受 TLS 1.0/1.1 握手。

`build_findings` 把后两条直接翻成 finding，不需要你再判：

- **TLS 证书未通过校验**（low）：描述里写明可能是自签名、也可能是链不完整或域名不匹配，
  三者内置探测**分不开**。这一点很要紧——报告里写"自签名证书"却拿不出依据，是一种看起来
  专业的错误。
- **TLS 接受已废弃协议版本**（medium）：`negotiated` 里记着实际谈成的版本。

## 要细节就用 openssl

```
list_toolkit
run_tool adapter=openssl target=10.0.0.5 toolArgs=["s_client","-connect","10.0.0.5:443","-servername","app.example.com","-showcerts"]
run_tool adapter=openssl target=10.0.0.5 toolArgs=["s_client","-connect","10.0.0.5:443","-tls1_1"]
```

读的时候：

```
get_job jobId=job-xxxxxxxx              # 第一页
get_job jobId=job-xxxxxxxx offset=6000  # hasMore 为 true 就继续带 nextOffset 读
```

证书链与握手细节**一定**在输出的后半段，所以"只读第一页就下结论"在这里会稳定地丢掉你要
的那一段。翻到 `hasMore` 为 false，再把"链上有几张证书、主题与 SAN、实际协商的版本"写出来。

## 没有 openssl 时，结论的边界在哪

`list_toolkit` 里 `openssl` 的 `source` 为空就是没装。这个适配器**没有自动渠道**
（`toolinstall.RECIPES` 里登记为 `manual`，理由是 Windows 上没有官方免安装构建：官方只发
源码，第三方 slproweb 只发 `.exe` 安装程序；常见的间接来源是 Git for Windows 的 MinGit 或
Strawberry Perl，为了一个二进制拉一整套进来不属于一次工具安装）。系统的 `openssl` 若已在
PATH 上，照常可用。

这时你能说和不能说的：

- **能说**：这张证书过不了系统 CA 校验（有 `verifyError` 原文）；服务端接受 TLS 1.0/1.1。
- **不能说**：这是自签名证书、链不完整还是名称不匹配；证书上写了哪些域名；协商用了哪个
  密码套件。
- **该怎么说**：把上面那一列"能说"的部分给出证据，然后明确写"链与名称的细节需要 openssl，
  本节点没有安装，且这个适配器没有自动安装渠道"，并给出 `list_toolkit` 返回里那条**具体
  原因**。不要说"平台不支持"——那会让人以为连判定都做不了，而判定其实做完了。

同一份纪律适用于所有"缺工具"的场景：**缺口要说成缺口，不要用一句含糊的话盖过去**，
也不要把"没观测到"说成"不存在"。

## 代价与边界

- TLS 握手是真实的网络动作，会进目标日志；对同一目标反复握手（尤其是故意打废弃版本）属于
  会被 IDS 关注的形状，不需要就别重复。
- 废弃版本判定用的是**主动**打一次 TLS 1.0/1.1 握手，不是读配置。所以它证明的是"服务端愿意
  谈"，不是"默认就在用"——两者在报告里是不同结论。
- 证书指纹是跨任务比对的锚：同一台机器两次探测指纹不同，说明证书换了。这个比对是内置能力，
  不需要外部工具。
