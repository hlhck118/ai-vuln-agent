# 把平台开放出去（runbook）

平台现在**只有一个进程**要开：控制面（8787）。DSH 由使用者自己跑，它的端口、它的
`Host` 栅栏、它的 TLS 都不归平台管 —— 所以这份 runbook 比上一版短了一大半。

上一版还有两节（控制面之外再开 8790、以及在 DSH 上声明受信地址），那两节随
`pentestd/dsh_web.py` 与 `pentestd/dsh_proxy.py` 一起删掉了：平台不再拉起 DSH，
也不再把自己的界面客串成 DSH 的。

---

## 一、控制面绑到所有网卡

```powershell
cd <项目>
python deploy\start.py --host 0.0.0.0 --port 8787 --no-browser
```

判据（`LocalAddress` 必须是 `0.0.0.0`，不是 `127.0.0.1`）：

```powershell
Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 8787 |
  Select-Object LocalAddress,LocalPort,OwningProcess
```

> **防火墙放行不会改变监听地址。** 只放行而进程仍绑回环，外面照样连不上 —— 那一步
> 只是"门开在墙上"，这一步才是"墙上有门"。

---

## 二、让 DSH 那个源能读平台接口（跨源）

插件的界面跑在**你自己的 DSH** 里，那是一个独立的 web 源。它读平台 8787 是一次
**跨源读**：按回环规则一律拒绝，症状是面板显示「连不上平台」而平台自己一切正常
（拒绝发生在浏览器里，服务端日志上连一条都没有）。

```powershell
python -m pentestd access add-origin http://127.0.0.1:19387      # 本机 DSH
python -m pentestd access list                                   # 看现状
```

**立即生效**，不需要重启。逐字比对：端口不同、scheme 不同、多一个子域都不放行。

> 只放行 `scheme://host[:port]`。带路径的条目永远不会命中（`Origin` 头里没有路径），
> 所以写入时就被拒 —— 收下死项的代价是"清单里有、就是不生效"。

---

## 三、防火墙

```powershell
New-NetFirewallRule -DisplayName "pentest-platform 8787" -Direction Inbound `
  -Protocol TCP -LocalPort 8787 -Action Allow -Profile Any
```

---

## 四、公网那一跳在**路由器**上

平台的监听解决不了"外面能不能进"：那要在路由器上把 8787 转给这台机器，或者把它
放在一个反代之后。

| 外部端口 | 协议 | 内部地址 | 内部端口 |
|---|---|---|---|
| 8787 | TCP | <这台机器的局域网地址> | 8787 |

出口 IP 会变的话（动态地址）配 DDNS，并把那个域名写进 `access add-origin` —— 跨源
清单里写的是**你从浏览器打开 DSH 时用的那个源**，域名换了它也要跟着换。

---

## 五、验证（每一层分开验）

```powershell
# 1. 监听地址
Get-NetTCPConnection -State Listen | Where-Object LocalPort -eq 8787 |
  Select-Object LocalAddress,LocalPort,OwningProcess

# 2. 局域网可达
Invoke-WebRequest -UseBasicParsing http://<局域网地址>:8787/api/v1/health | % StatusCode

# 3. 跨源：从 DSH 那个源打一条，看响应里有没有 Access-Control-Allow-Origin
#    （在浏览器控制台里执行，或 curl 带 Origin 头）
curl -i -H "Origin: http://127.0.0.1:19387" -H "Authorization: Bearer <令牌>" `
     http://<局域网地址>:8787/api/v1/panel
```

---

## 六、踩过的坑（都记在这里，免得下次重付一遍）

1. **`127.0.0.1` 与 `0.0.0.0` 是两个概念**。只 `--host 0.0.0.0` 有效；`access` 那套
   清单**不改变监听地址**。
2. **跨源清单是"给哪个源"而不是"给哪个域名"**。DSH 换了端口（或从 `127.0.0.1` 换成
   局域网地址打开），来源就变了，要再加一条。
3. **明文 HTTP**。这样开出去，登录口令与控制台令牌都在链路上明文。平台自带 TLS
   （`--tls-cert/--tls-key`）——上了 TLS，跨源清单里那几条也要跟着改成 `https://`，
   否则浏览器会因为"https 页面读 http 接口"把它当混合内容拦下。
4. **强杀控制面会留下没读完的请求**，但它不再留下孤儿 DSH 宿主 —— 那个子进程已经
   不存在了。